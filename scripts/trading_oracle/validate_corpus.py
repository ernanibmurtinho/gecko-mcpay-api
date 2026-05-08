"""Read-only corpus health check for the trading-oracle vertical.

Why this exists
---------------
After each ingest pass we want a fast, repeatable answer to "is the
corpus healthy?" — per-protocol chunk counts, provider_kind mix,
freshness_tier mix, content_kind mix, embedding dimension consistency,
embedding presence ratio, stale-chunk count. Today this is ad-hoc Mongo
queries every time. This script makes it a single command.

Hard scope:
* Read-only. No insert/update/delete on Mongo.
* Does not modify ``mongo_chunks.py``, ``run.py``, or any
  ``packages/gecko-core/`` file.
* If Mongo is not configured, prints a clear message and exits 1 (no
  hang, no async deadlock).

Canonical literal sets are imported from
``gecko_core.sources.types`` per the project's "single source of truth
for shared Literal types" rule (CLAUDE.md, Pattern A).
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from typing import Any

import click
from gecko_core.db.mongo import chunks_collection, is_chunk_store_configured, mongo_uri
from gecko_core.sources.types import (
    CONTENT_KIND_VALUES,
    FRESHNESS_TIER_VALUES,
    PROVIDER_KINDS,
)
from rich.console import Console
from rich.table import Table

EXPECTED_PROTOCOLS: tuple[str, ...] = ("jupiter", "kamino", "pyth", "drift", "jito")
EXPECTED_EMBEDDING_DIM = 1024
EMBEDDING_SAMPLE_LIMIT = 100
UNTAGGED_LABEL = "<untagged>"


# ---------------------------------------------------------------------------
# Pure validation core. Operates on a list[dict] sample + breakdown dicts.
# Tests target this directly via a fake collection.
# ---------------------------------------------------------------------------


def classify_provider_kind_mix(chunks: list[dict[str, Any]]) -> Counter[str]:
    return Counter(str(c.get("provider_kind", "<missing>")) for c in chunks)


def classify_freshness_tier_mix(chunks: list[dict[str, Any]]) -> Counter[str]:
    return Counter(str(c.get("freshness_tier", "<missing>")) for c in chunks)


def classify_content_kind_mix(chunks: list[dict[str, Any]]) -> Counter[str]:
    return Counter(str(c.get("content_kind", "unknown")) for c in chunks)


def protocol_breakdown(chunks: list[dict[str, Any]]) -> Counter[tuple[str, str]]:
    """Return Counter keyed by (protocol, provider_kind). Empty protocol
    list maps to ``<untagged>``."""
    out: Counter[tuple[str, str]] = Counter()
    for c in chunks:
        protos = c.get("protocol")
        pk = str(c.get("provider_kind", "<missing>"))
        if not protos:
            out[(UNTAGGED_LABEL, pk)] += 1
            continue
        if isinstance(protos, str):
            protos = [protos]
        for p in protos:
            out[(str(p), pk)] += 1
    return out


def check_embedding_dims(
    chunks: list[dict[str, Any]],
) -> tuple[list[str], list[str], int | None]:
    """Inspect embedding dimensions on a sample.

    Returns ``(missing_ids, drift_ids, observed_dim)`` where
    ``observed_dim`` is the first non-null dim seen (used to confirm
    consistency in the report).
    """
    missing: list[str] = []
    drift: list[str] = []
    observed: int | None = None
    for c in chunks:
        cid = str(c.get("_id") or c.get("id") or "<no-id>")
        emb = c.get("embedding")
        if emb is None:
            missing.append(cid)
            continue
        dim = len(emb)
        if observed is None:
            observed = dim
        if dim != EXPECTED_EMBEDDING_DIM:
            drift.append(f"{cid} (dim={dim})")
    return missing, drift, observed


def find_unknown_values(values: Counter[str], canonical: tuple[str, ...]) -> list[str]:
    valid = set(canonical)
    return sorted({v for v in values if v not in valid and v != "<missing>"})


# ---------------------------------------------------------------------------
# Mongo I/O — thin wrappers. Read-only.
# ---------------------------------------------------------------------------


async def _gather_corpus_stats(
    *, collection: Any, base_filter: dict[str, Any], sample: int
) -> dict[str, Any]:
    """One async pass that collects every stat we need.

    Issues several aggregation pipelines + one find() for the embedding
    sample. All read-only.
    """
    total = await collection.count_documents(base_filter)

    # provider_kind mix
    provider_mix: Counter[str] = Counter()
    async for d in collection.aggregate(
        [{"$match": base_filter}, {"$group": {"_id": "$provider_kind", "n": {"$sum": 1}}}]
    ):
        provider_mix[str(d.get("_id"))] = int(d.get("n", 0))

    # freshness_tier mix
    fresh_mix: Counter[str] = Counter()
    async for d in collection.aggregate(
        [{"$match": base_filter}, {"$group": {"_id": "$freshness_tier", "n": {"$sum": 1}}}]
    ):
        fresh_mix[str(d.get("_id"))] = int(d.get("n", 0))

    # content_kind mix (default unknown when missing)
    content_mix: Counter[str] = Counter()
    async for d in collection.aggregate(
        [
            {"$match": base_filter},
            {
                "$group": {
                    "_id": {"$ifNull": ["$content_kind", "unknown"]},
                    "n": {"$sum": 1},
                }
            },
        ]
    ):
        content_mix[str(d.get("_id"))] = int(d.get("n", 0))

    # protocol x provider_kind. $unwind preserveNull so untagged chunks survive.
    proto_mix: Counter[tuple[str, str]] = Counter()
    async for d in collection.aggregate(
        [
            {"$match": base_filter},
            {
                "$unwind": {
                    "path": "$protocol",
                    "preserveNullAndEmptyArrays": True,
                }
            },
            {
                "$group": {
                    "_id": {
                        "protocol": {"$ifNull": ["$protocol", UNTAGGED_LABEL]},
                        "provider_kind": "$provider_kind",
                    },
                    "n": {"$sum": 1},
                }
            },
        ]
    ):
        key = d.get("_id", {}) or {}
        proto = str(key.get("protocol") or UNTAGGED_LABEL)
        pk = str(key.get("provider_kind"))
        proto_mix[(proto, pk)] = int(d.get("n", 0))

    # is_stale count
    stale_count = await collection.count_documents({**base_filter, "is_stale": True})

    # empty-text count (missing or empty string)
    empty_text_count = await collection.count_documents(
        {**base_filter, "$or": [{"text": {"$exists": False}}, {"text": ""}]}
    )

    # Embedding sample (capped) — fetch _id + embedding only.
    emb_cursor = collection.find(base_filter, {"_id": 1, "embedding": 1}).limit(
        EMBEDDING_SAMPLE_LIMIT
    )
    emb_sample: list[dict[str, Any]] = []
    async for d in emb_cursor:
        emb_sample.append(d)

    # Optional snippet sample.
    snippets: list[dict[str, Any]] = []
    if sample > 0:
        snip_cursor = collection.find(
            base_filter,
            {"_id": 1, "text": 1, "protocol": 1, "provider_kind": 1, "source_url": 1},
        ).limit(sample)
        async for d in snip_cursor:
            snippets.append(d)

    return {
        "total": total,
        "provider_mix": provider_mix,
        "fresh_mix": fresh_mix,
        "content_mix": content_mix,
        "proto_mix": proto_mix,
        "stale_count": stale_count,
        "empty_text_count": empty_text_count,
        "embedding_sample": emb_sample,
        "snippets": snippets,
    }


# ---------------------------------------------------------------------------
# Reporting + exit-code logic. Pure: takes stats, console, options.
# Tests drive this directly via run_with_collection().
# ---------------------------------------------------------------------------


def _format_mix(c: Counter[str]) -> str:
    if not c:
        return "(empty)"
    return ", ".join(f"{k}={v}" for k, v in c.most_common())


def render_and_validate(
    *,
    console: Console,
    stats: dict[str, Any],
    vertical: str,
    filter_was_applied: bool,
    sample: int,
    quiet: bool,
) -> int:
    """Render Rich tables + accumulate failures. Returns exit code."""
    failures: list[str] = []
    warnings: list[str] = []

    total = int(stats["total"])
    provider_mix: Counter[str] = stats["provider_mix"]
    fresh_mix: Counter[str] = stats["fresh_mix"]
    content_mix: Counter[str] = stats["content_mix"]
    proto_mix: Counter[tuple[str, str]] = stats["proto_mix"]
    stale_count = int(stats["stale_count"])
    empty_text_count = int(stats["empty_text_count"])
    emb_sample: list[dict[str, Any]] = stats["embedding_sample"]
    snippets: list[dict[str, Any]] = stats["snippets"]

    # FAIL 6: zero chunks with no filter.
    if total == 0 and not filter_was_applied:
        failures.append("NO CHUNKS — corpus is empty for this vertical")

    # FAIL 3, 4, 5: unknown enum values.
    unknown_pk = find_unknown_values(provider_mix, PROVIDER_KINDS)
    if unknown_pk:
        failures.append(f"UNKNOWN provider_kind: {', '.join(unknown_pk)}")
    unknown_ck = find_unknown_values(content_mix, CONTENT_KIND_VALUES)
    if unknown_ck:
        failures.append(f"UNKNOWN content_kind: {', '.join(unknown_ck)}")
    unknown_ft = find_unknown_values(fresh_mix, FRESHNESS_TIER_VALUES)
    if unknown_ft:
        failures.append(f"UNKNOWN freshness_tier: {', '.join(unknown_ft)}")

    # FAIL 1, 2: embedding drift / missing.
    missing_emb, drift_emb, observed_dim = check_embedding_dims(emb_sample)
    if drift_emb:
        failures.append(
            f"EMBEDDING DIM DRIFT — expected {EXPECTED_EMBEDDING_DIM}, offending: "
            + ", ".join(drift_emb[:10])
        )
    if missing_emb:
        failures.append(
            "MISSING EMBEDDING — chunks without embedding field: " + ", ".join(missing_emb[:10])
        )

    # WARN: missing expected protocols.
    seen_protocols = {p for (p, _) in proto_mix if p != UNTAGGED_LABEL}
    missing_protocols = [p for p in EXPECTED_PROTOCOLS if p not in seen_protocols]
    if missing_protocols and total > 0:
        warnings.append(f"MISSING PROTOCOL {' '.join(missing_protocols)}")

    # WARN: empty-text chunks.
    if empty_text_count > 0:
        warnings.append(f"EMPTY TEXT — {empty_text_count} chunks have empty/missing text")

    # WARN: high untagged ratio.
    if total > 0:
        untagged_total = sum(n for (p, _), n in proto_mix.items() if p == UNTAGGED_LABEL)
        if untagged_total / total > 0.5:
            warnings.append(
                f"HIGH UNTAGGED RATIO — {untagged_total}/{total} chunks have no protocol tag"
            )

    # ----- Render -----
    embedding_dim_str: str
    if not emb_sample:
        embedding_dim_str = "(no chunks sampled)"
    elif drift_emb:
        embedding_dim_str = f"{observed_dim} (DRIFT — see failures)"
    elif missing_emb and observed_dim is None:
        embedding_dim_str = "(all sampled missing)"
    else:
        embedding_dim_str = f"{observed_dim} (consistent)"

    presence_str = (
        f"{len(emb_sample) - len(missing_emb)}/{len(emb_sample)} "
        f"({(len(emb_sample) - len(missing_emb)) * 100 // max(len(emb_sample), 1)}%)"
        if emb_sample
        else "0/0 (n/a)"
    )

    if quiet:
        verdict = "FAIL" if failures else ("WARN" if warnings else "OK")
        console.print(
            f"[bold]{verdict}[/bold] vertical={vertical} chunks={total} "
            f"provider_mix=({_format_mix(provider_mix)}) "
            f"failures={len(failures)} warnings={len(warnings)}"
        )
    else:
        summary = Table(title="trading-oracle corpus health")
        summary.add_column("metric", style="bold")
        summary.add_column("value")
        summary.add_row("vertical", vertical)
        summary.add_row("total chunks", str(total))
        summary.add_row("provider_kind mix", _format_mix(provider_mix))
        summary.add_row("freshness_tier mix", _format_mix(fresh_mix))
        summary.add_row("content_kind mix", _format_mix(content_mix))
        summary.add_row("embedding dim", embedding_dim_str)
        summary.add_row("embedding presence", presence_str)
        summary.add_row("stale chunks", f"{stale_count} (is_stale=true)")
        console.print(summary)

        proto_table = Table(title="per protocol")
        proto_table.add_column("protocol")
        proto_table.add_column("provider_kind")
        proto_table.add_column("count", justify="right")
        for (proto, pk), n in sorted(proto_mix.items(), key=lambda kv: (-kv[1], kv[0][0])):
            proto_table.add_row(proto, pk, str(n))
        console.print(proto_table)

        if sample > 0 and snippets:
            console.print("[bold]sample chunks[/bold]")
            for d in snippets:
                proto = d.get("protocol") or [UNTAGGED_LABEL]
                if isinstance(proto, list):
                    proto_str = ",".join(str(p) for p in proto) or UNTAGGED_LABEL
                else:
                    proto_str = str(proto)
                pk = str(d.get("provider_kind", "<missing>"))
                src = str(d.get("source_url", ""))
                text = str(d.get("text", ""))[:160]
                console.print(f"- [{proto_str}] {pk}: {src}")
                console.print(f'  text: "{text}"')

        for w in warnings:
            console.print(f"[yellow]⚠ {w}[/yellow]")
        for f in failures:
            console.print(f"[red]✗ {f}[/red]")

        if not failures and not warnings:
            console.print("[green]✓ corpus healthy[/green]")

    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Test seam — synchronous entry point that drives a (possibly fake)
# collection. The Click CLI is a thin wrapper over this.
# ---------------------------------------------------------------------------


def run_with_collection(
    *,
    collection: Any,
    vertical: str,
    provider_kind: str | None,
    protocol: str | None,
    sample: int,
    quiet: bool,
    console: Console | None = None,
) -> int:
    base_filter: dict[str, Any] = {"vertical": vertical}
    filter_was_applied = False
    if provider_kind:
        base_filter["provider_kind"] = provider_kind
        filter_was_applied = True
    if protocol:
        base_filter["protocol"] = protocol
        filter_was_applied = True

    cons = console or Console()
    stats = asyncio.run(
        _gather_corpus_stats(collection=collection, base_filter=base_filter, sample=sample)
    )
    return render_and_validate(
        console=cons,
        stats=stats,
        vertical=vertical,
        filter_was_applied=filter_was_applied,
        sample=sample,
        quiet=quiet,
    )


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


@click.command()
@click.option("--vertical", default="dex", show_default=True, help="Vertical to validate.")
@click.option(
    "--provider-kind",
    default=None,
    help="Filter to a specific provider_kind (e.g. bazaar_live).",
)
@click.option("--protocol", default=None, help="Filter to a specific protocol tag (e.g. kamino).")
@click.option(
    "--sample",
    type=int,
    default=0,
    show_default=True,
    help="Print N sample chunks' text snippets.",
)
@click.option(
    "--quiet",
    is_flag=True,
    help="Print one-line summary only; still exits 0/1 by validation outcome.",
)
def main(
    vertical: str,
    provider_kind: str | None,
    protocol: str | None,
    sample: int,
    quiet: bool,
) -> None:
    """Validate the trading-oracle corpus health (read-only)."""
    console = Console()

    if not is_chunk_store_configured():
        if mongo_uri() is None:
            console.print(
                "[red]Mongo not configured: MONGODB_URI is unset.[/red] "
                "Set it (and GECKO_CHUNK_STORE=mongo) before running this script."
            )
        else:
            console.print("[red]Mongo chunk store disabled:[/red] set GECKO_CHUNK_STORE=mongo.")
        sys.exit(1)

    coll = chunks_collection()
    if coll is None:
        console.print("[red]Mongo not configured: chunks_collection() returned None.[/red]")
        sys.exit(1)

    code = run_with_collection(
        collection=coll,
        vertical=vertical,
        provider_kind=provider_kind,
        protocol=protocol,
        sample=sample,
        quiet=quiet,
        console=console,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
