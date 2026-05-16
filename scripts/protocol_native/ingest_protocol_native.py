"""S26 #14 — ingest direct protocol-native API content into the corpus.

Mirrors the shape of ``scripts/market_data/ingest_market_data.py``.
Fetches public protocol-API + docs endpoints, chunks, embeds, and
inserts into Mongo under ``provider_kind="protocol_native"`` with
``freshness_tier="daily"``.

The rubric eval (2026-05-12) flagged paysh_live Kamino chunks as
empty ``{"data":[]}`` responses. This script replaces them with
substantive vault-params / market-config / fee manifests pulled
directly from each protocol's public API (no x402, no payment).

Run:
    set -a; source .env; set +a   # MONGODB_URI + embedder keys
    uv run python scripts/protocol_native/ingest_protocol_native.py --sample
    uv run python scripts/protocol_native/ingest_protocol_native.py --protocols kamino,drift
    uv run python scripts/protocol_native/ingest_protocol_native.py --dry-run --sample

Idempotent (S33-#80): ``source_id = UUID5(NAMESPACE_URL, "<endpoint_url>")``
— STABLE, no day bucket. Each ingest first deletes the prior chunks for
that endpoint's ``(provider_kind, source_url, protocol)`` via
``delete_chunks_for_source_mongo`` and then inserts the fresh set. A daily
re-ingest is therefore a true REPLACE, not an append — the pre-S33-#80
``#<day_bucket>`` suffix minted a new ``source_id`` every day, so the
``(source_id, chunk_index)`` unique index never collided and the corpus
accreted one full duplicate set per ingest day (19.9% of the dex corpus
was duplicate text by 2026-05-16).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import click
import httpx

# --- Minimal HTML → text cleanup ------------------------------------------
# We don't pull in BeautifulSoup for this script — keep deps narrow. The
# cleanup is two passes: strip script/style blocks, then collapse tags +
# entities. Output is whitespace-collapsed prose that the embedder
# tokenizes happily. NOT a full HTML parser — handles malformed pages by
# returning the regex-cleaned text rather than raising.
_HTML_BLOCKS_TO_DROP = re.compile(
    r"<(script|style|noscript|svg|iframe|head)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_ENTITY = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")
_MULTI_WS = re.compile(r"\s+")

_ENTITY_MAP = {
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "quot": '"',
    "apos": "'",
    "nbsp": " ",
    "ndash": "-",
    "mdash": "—",
    "hellip": "…",
}


def _decode_entity(m: re.Match[str]) -> str:
    raw = m.group(1)
    if raw.startswith("#x") or raw.startswith("#X"):
        try:
            return chr(int(raw[2:], 16))
        except ValueError:
            return " "
    if raw.startswith("#"):
        try:
            return chr(int(raw[1:]))
        except ValueError:
            return " "
    return _ENTITY_MAP.get(raw.lower(), " ")


def html_to_text(html: str) -> str:
    """Strip HTML to readable plaintext. Pure function.

    Drops script/style blocks, collapses tags, decodes a small set of
    common entities, collapses whitespace. Caps output at 40k chars to
    bound chunk count (5-10 chunks of ~4k each at the 512-token chunker
    boundary). Anything longer is doc bloat — abstract is enough.
    """
    if not html:
        return ""
    text = _HTML_BLOCKS_TO_DROP.sub(" ", html)
    text = _HTML_TAG.sub(" ", text)
    text = _HTML_ENTITY.sub(_decode_entity, text)
    text = _MULTI_WS.sub(" ", text).strip()
    # Bound for sanity — docs pages can be huge with rendered React state
    # inlined as JSON. Take the first 40k chars; the panel doesn't need
    # the whole site.
    return text[:40000]


_REPO = Path(__file__).resolve().parents[2]
_GC_SRC = _REPO / "packages" / "gecko-core" / "src"
if str(_GC_SRC) not in sys.path:
    sys.path.insert(0, str(_GC_SRC))

from gecko_core.db.mongo_chunks import (  # noqa: E402
    delete_chunks_for_source_mongo,
    insert_chunks_mongo,
)
from gecko_core.ingestion.chunker import chunk as chunk_text  # noqa: E402
from gecko_core.ingestion.embedder import embed  # noqa: E402
from gecko_core.sources.protocol_native import (  # noqa: E402
    ALL_PROTOCOL_ENDPOINTS,
    ProtocolEndpoint,
    endpoints_for_protocol,
    render_chunk_pairs,
)

log = logging.getLogger("protocol_native.ingest")

# Stable session namespace — all protocol_native chunks belong to this
# session_id. Per Pattern F, retrieval is scoped by provider_kind +
# protocol, not session_id, so this is purely provenance.
_PROTOCOL_NATIVE_SESSION = uuid5(NAMESPACE_URL, "gecko.protocol_native.session.v1")

SAMPLE_PROTOCOLS = ("kamino", "drift", "jupiter", "jito", "sanctum")


def _day_bucket(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


async def _fetch(client: httpx.AsyncClient, ep: ProtocolEndpoint) -> str | None:
    """Fetch one endpoint. Returns body text (str) or None on failure.

    Both JSON and HTML/docs endpoints are fetched as text and embedded
    verbatim — the panel reads prose, not parsed JSON. We do NOT do any
    cleverness like "extract only the relevant fields" here; the chunker
    + retrieval boost classes that down. Logging emits content length
    and a 200-char preview so substantive vs empty can be eyeballed.
    """
    try:
        headers = {
            "User-Agent": "gecko-mcpay-api/1.0 (corpus-ingest; +https://geckovision.tech)",
            "Accept": "application/json, text/html, */*",
        }
        resp = await client.get(ep.url, timeout=30.0, headers=headers, follow_redirects=True)
        if resp.status_code != 200:
            log.warning(
                "FETCH-FAIL %s status=%d body_head=%r",
                ep.slug,
                resp.status_code,
                resp.text[:200],
            )
            return None
        body = resp.text
        # Pretty-print JSON for stable embedding. HTML pages get cleaned
        # to plain text — docs landings carry mechanism prose buried in
        # React markup; we want the prose, not the markup.
        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype.lower():
            try:
                parsed = json.loads(body)
                # Cap JSON arrays at 40 entries to keep chunk count
                # bounded — a 600-entry vault catalog produces 2k+ chunks
                # of dim address metadata, which buries the meaningful
                # content (vault types, params, status) under noise and
                # costs ~$3 in embeddings per ingest. 40 entries surfaces
                # the canonical product types without bloating the corpus.
                if isinstance(parsed, list) and len(parsed) > 40:
                    sample_note = (
                        f"NOTE: API returned {len(parsed)} entries; showing "
                        f"first 40 for citation grounding. Full catalog "
                        "available at the source URL."
                    )
                    parsed = [sample_note, *parsed[:40]]
                body = json.dumps(parsed, indent=2, sort_keys=True, ensure_ascii=False)
            except json.JSONDecodeError:
                pass
        elif "text/html" in ctype.lower() or body.lstrip().startswith("<"):
            cleaned = html_to_text(body)
            log.info(
                "HTML-CLEAN %s html_bytes=%d text_bytes=%d",
                ep.slug,
                len(body),
                len(cleaned),
            )
            body = cleaned
        # Reject empties — this is exactly the bug we're fixing.
        if not body or not body.strip() or body.strip() in ('{"data":[]}', "[]", "{}"):
            log.warning(
                "FETCH-EMPTY %s body=%r — skipping (would re-create the bug)",
                ep.slug,
                body[:100],
            )
            return None
        log.info(
            "FETCH-OK %s bytes=%d preview=%r",
            ep.slug,
            len(body),
            body[:120].replace("\n", " "),
        )
        return body
    except Exception as exc:
        log.warning("FETCH-ERR %s exc=%s", ep.slug, exc)
        return None


async def ingest_endpoint(
    ep: ProtocolEndpoint, *, dry_run: bool, http: httpx.AsyncClient
) -> dict[str, int]:
    """Fetch + render + chunk + embed + insert one endpoint."""
    now = datetime.now(UTC)
    day_iso = _day_bucket(now)

    body = await _fetch(http, ep)
    if body is None:
        return {"chunks": 0, "skipped": 1}

    # S33-#61/#63/#64 — render_chunk_pairs emits per-entity prose chunks
    # (one per kamino vault / jupiter token; tip-floor ladder flattened),
    # each as a (display_text, embed_text) pair.
    #
    # S33-#80 — embed_text is the SIGNAL ONLY (provenance header stripped);
    # display_text keeps the header for citation/provenance. The shared
    # boilerplate header dominated short chunks' embedding vectors and
    # compressed cosine spread (retrieval-quality diagnosis §4). We chunk
    # and embed the body-only text, and rebuild the display text by
    # prepending the header to each chunker segment. Per-entity chunks are
    # short and pass through the chunker as a single segment; a long
    # docs-prose body splits into N segments, each of which then carries
    # the header for display so every cited segment keeps provenance.
    pairs = render_chunk_pairs(ep, body, day_iso)
    embed_texts: list[str] = []
    display_texts: list[str] = []
    for display, embed_body in pairs:
        # The display header is whatever display had minus embed_body's
        # content; reconstruct it from the known prefix length.
        header = display[: len(display) - len(embed_body)].rstrip()
        for segment in chunk_text(embed_body) or [embed_body]:
            embed_texts.append(segment)
            display_texts.append(f"{header} {segment}".strip() if header else segment)
    log.info(
        "RENDER %s prose_chunks=%d embed_chunks=%d embed_chars=%d display_chars=%d",
        ep.slug,
        len(pairs),
        len(embed_texts),
        sum(len(c) for c in embed_texts),
        sum(len(c) for c in display_texts),
    )

    if not embed_texts:
        log.warning("RENDER-EMPTY %s — all chunks degenerate, skipping", ep.slug)
        return {"chunks": 0, "skipped": 1}

    if dry_run:
        return {"chunks": len(embed_texts), "skipped": 0}

    # S33-#80 — embed the SIGNAL-ONLY text, store the DISPLAY text.
    vectors, _tokens = await embed(embed_texts)
    rows: list[tuple[int, str, list[float]]] = [
        (i, display_texts[i], list(vectors[i])) for i in range(len(embed_texts))
    ]

    # S33-#80 — stable source_id (no day bucket). Combined with the
    # replace-before-insert below, a daily re-ingest is a true replace.
    source_key = f"protocol_native:{ep.slug}"
    source_id = uuid5(NAMESPACE_URL, source_key)

    # S33-#80 — replace, not append: drop the prior day's chunks for this
    # endpoint before inserting the fresh set. Matched on the stable
    # source_url so it works regardless of how source_id was minted on the
    # prior ingest (pre-#80 chunks carry a day-bucketed source_id).
    deleted = await delete_chunks_for_source_mongo(
        provider_kind="protocol_native",
        source_url=ep.url,
        protocol=ep.protocol,
    )
    if deleted:
        log.info("REPLACE %s deleted_prior_chunks=%d", ep.slug, deleted)

    # Pattern F: protocol_native carries the exact protocol tag (NOT
    # protocol=[]), so retrieval admittance routes via the protocol-exact
    # match. Canon chunks remain protocol=[]; new protocol_native chunks
    # carry the protocol slug so the +0.15 protocol-exact + +0.10
    # PROVIDER_SPECIFIC boosts stack.
    inserted = await insert_chunks_mongo(
        session_id=_PROTOCOL_NATIVE_SESSION,
        source_id=source_id,
        chunks=rows,
        category="market_intelligence",
        vertical="dex",
        source="protocol_native",
        provider_kind="protocol_native",
        source_url=ep.url,
        freshness_tier="daily",
        protocol=(ep.protocol,),
        content_kind=ep.content_kind,  # type: ignore[arg-type]
        # S33-#68 — the data's as-of date (the day bucket), distinct from
        # captured_at (ingest wall-clock). Lets retrieval + the panel
        # reason about freshness without parsing the chunk text.
        as_of_date=day_iso,
    )
    log.info(
        "INSERT %s new_chunks=%d (as_of_date=%s) source_id=%s",
        ep.slug,
        inserted,
        day_iso,
        str(source_id),
    )
    return {"chunks": inserted, "skipped": 0}


async def amain(*, endpoints: list[ProtocolEndpoint], dry_run: bool, sleep_seconds: float) -> int:
    log.info(
        "=== protocol_native ingest: %d endpoints (dry_run=%s) ===",
        len(endpoints),
        dry_run,
    )
    total_chunks = 0
    total_skipped = 0
    by_protocol: dict[str, int] = {}
    async with httpx.AsyncClient() as http:
        for i, ep in enumerate(endpoints, start=1):
            log.info("[%d/%d] %s (%s)", i, len(endpoints), ep.slug, ep.protocol)
            stats = await ingest_endpoint(ep, dry_run=dry_run, http=http)
            total_chunks += stats["chunks"]
            total_skipped += stats["skipped"]
            by_protocol[ep.protocol] = by_protocol.get(ep.protocol, 0) + stats["chunks"]
            if i < len(endpoints):
                await asyncio.sleep(sleep_seconds)
    log.info(
        "=== DONE: endpoints=%d chunks=%d skipped=%d by_protocol=%s ===",
        len(endpoints),
        total_chunks,
        total_skipped,
        by_protocol,
    )
    return 0


@click.command()
@click.option(
    "--protocols",
    type=str,
    default=None,
    help="Comma-separated protocol slugs. Mutually exclusive with --sample.",
)
@click.option(
    "--sample",
    is_flag=True,
    default=False,
    help=f"Ingest all 5 sample protocols ({', '.join(SAMPLE_PROTOCOLS)}).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Fetch + render + chunk only; no embed, no Mongo writes.",
)
@click.option(
    "--sleep-seconds",
    type=float,
    default=1.0,
    show_default=True,
    help="Politeness delay between endpoint fetches.",
)
@click.option("--verbose", "-v", is_flag=True, default=False)
def main(
    protocols: str | None,
    sample: bool,
    dry_run: bool,
    sleep_seconds: float,
    verbose: bool,
) -> None:
    """S26 #14 — ingest protocol-native API content into the corpus."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    if sample and protocols:
        raise click.UsageError("--sample and --protocols are mutually exclusive")

    eps: list[ProtocolEndpoint]
    if sample:
        eps = list(ALL_PROTOCOL_ENDPOINTS)
    elif protocols:
        chosen_protocols = [p.strip().lower() for p in protocols.split(",") if p.strip()]
        eps = []
        for p in chosen_protocols:
            cat = endpoints_for_protocol(p)
            if not cat:
                log.warning("SKIP-UNKNOWN-PROTOCOL %s", p)
                continue
            eps.extend(cat)
    else:
        raise click.UsageError("provide --sample or --protocols")

    rc = asyncio.run(amain(endpoints=eps, dry_run=dry_run, sleep_seconds=sleep_seconds))
    sys.exit(rc)


if __name__ == "__main__":
    main()
