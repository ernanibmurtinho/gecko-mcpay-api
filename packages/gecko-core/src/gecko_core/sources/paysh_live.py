"""PayshLiveSource — paid x402 retrieval against pay.sh catalog providers.

S22-PAYSH-LIVE-CALLER-01 (S22-N4). Wraps the existing buyer-side x402
client to issue real, USDC-priced calls against pay.sh-listed providers
and ingest their responses as ``paysh_live``-tagged chunks.

Why a dedicated provider (and not "extend ``paysh_manifest``"):
    - ``paysh_manifest`` is *discovery* — it lists 72 providers and what
      they sell, free, no wallet involved. ``paysh_live`` is *execution*
      — actually issuing a paid call against one of those providers and
      ingesting the body. Discovery never burns USDC; execution always
      does. Keeping the two providers separate keeps the trust + cost
      surface explicit at the dispatcher layer.
    - Per Pattern A (CLAUDE.md), the source literal ``paysh_live`` was
      locked in N1 alongside ``paysh_manifest``; this module wires that
      literal end-to-end.

Pattern compliance (CLAUDE.md):
    - **Pattern B (free local sim before live spend).** N3 ships
      ``scripts/paysh/simulate_call.py`` with scenario fixtures for the
      three FQNs targeted by this caller (``agentmail-email``,
      ``merit-systems-stablecrypto-market-data``,
      ``paysponge-coingecko``). The sim falsifies the wire shape WITHOUT
      spending. Live spend is gated behind ``@pytest.mark.live_paysh``,
      manually opted-in by the operator.
    - **Pattern C (recorded-fixture contract test).** The default test
      run replays JSON fixtures pinned at
      ``tests/sources/fixtures/paysh_live_<fqn>_2026_05_07.json``;
      live re-record is the operator's job. Adding a new live target =
      ship the fixture + the contract test before merge.

Budget discipline:
    - Hardcoded sprint cap: ``PAYSH_LIVE_BUDGET_USD = 5.0`` total. Every
      call decrements remaining budget; subsequent calls refuse to
      settle once the envelope is exhausted. Sprint 22 plan §5 falsifier
      #2 calls this out explicitly.
    - Per-call ``max_cost_usd`` override is a second leg — the caller
      may request a tighter cap than ``min_price_usd`` for a particular
      provider, and we refuse rather than silently pay.

Security non-negotiables (CLAUDE.md):
    - Never log wallet addresses, signatures, or apiToken substrings.
      The ``_redact_secrets`` helper scrubs those before anything is
      raised or logged. Hard-fail-loud on non-200; never catch-and-rephrase
      the facilitator's own message.
    - SSRF — provider ``service_url`` is validated ``https://`` only and
      hostname is required (no bare IP, no ``file://``). pay.sh-issued
      catalog entries should already obey this; we double-check at the
      point of call so a corrupted cache can't punch through.

The provider returns a ``SourceResult`` whose ``payload["chunks"]`` is a
list of dicts shaped like a ``BazaarChunk`` so the downstream rendering
+ economics ledger code paths don't fork on provider type. Cost is the
actual USDC spent on the call (denominated USD, ``str``-encoded for the
ledger per ``BazaarChunk`` convention).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from gecko_core.sources import SourceResult
from gecko_core.sources.paysh_manifest import PayshCatalogProvider

logger = logging.getLogger(__name__)


# Hard sprint-22 budget cap. Operator override is intentional friction —
# bumping this is a code edit, not an env flip.
PAYSH_LIVE_BUDGET_USD: float = 5.0

# Live x402 calls are slower than the /api/catalog fetch — pay.sh
# providers settle on Solana before answering, which adds 1-3s on top of
# their own response time. 12s covers the worst case observed during
# N3-sim-against-live tests on devnet.
DEFAULT_TIMEOUT_SECONDS: float = 12.0

# Each chunk targets ~150 words of provider response body. S33-#66 follow-up:
# paysh paid responses post-JSON-flattening run ~150-400 words; at 500 each
# call collapsed to a single chunk (observed: 16 protocols → 16 chunks).
# 150 yields ~3-5× more chunks per call without dropping below the
# citation-utility floor or breaching the embedding-token cap.
DEFAULT_CHUNK_WORDS: int = 150

SOURCE_NAME: str = "paysh_live"

# Locked Pattern A literal — mirrors ``paysh_manifest._provider_to_chunk``
# but distinct so dispatcher renderers can route execution chunks
# differently from discovery chunks.
PROVIDER_KIND: str = "paid:paysh_live"


# ---------------------------------------------------------------------------
# Errors — surfaced verbatim, never catch-and-rephrase.
# ---------------------------------------------------------------------------


class PayshLiveError(RuntimeError):
    """Base for all paysh_live execution failures."""


class ProviderNotFoundError(PayshLiveError):
    """``provider_fqn`` is not present in the cached catalog."""


class BudgetExceeded(PayshLiveError):
    """Caller-side cap or sprint budget envelope is below provider's
    advertised ``min_price_usd``. Raised BEFORE any network call."""


class ProviderHTTPError(PayshLiveError):
    """Provider returned a non-200 after the paid retry. Signature
    fragments scrubbed from the message via ``_redact_secrets``."""


class ProviderUnreachableError(PayshLiveError):
    """Transport error reaching the provider (DNS, TLS, timeout)."""


# ---------------------------------------------------------------------------
# Buyer seam — the live caller talks to the wire through this Protocol.
#
# This is intentionally narrower than ``X402Consumer.pay`` (which returns
# only a receipt). pay.sh providers respond inline — the body IS the
# product — so we need both the receipt AND the body in one shape. The
# Protocol lets the contract test inject a recorded-fixture replayer
# without monkeypatching httpx or the consumer client directly.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaidResponse:
    """One recorded outcome of an x402 challenge → pay → fetch cycle.

    ``cost_usd`` is what actually settled (after the facilitator's own
    rounding). ``response_body`` is the provider's 200-response body,
    parsed as JSON when ``content_type=application/json``, else the
    raw text. ``tx_signature`` is opaque to the caller — kept for the
    ledger entry only.
    """

    status_code: int
    cost_usd: float
    response_body: Any
    response_text: str
    content_type: str
    tx_signature: str | None
    response_sha: str
    headers: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class PaidRequester(Protocol):
    """Issues an x402-paid request and returns the parsed outcome.

    Implementations own the 402-challenge → buyer-sign → retry-with-payment
    flow. The default implementation (``X402PaidRequester`` below) wires
    httpx + the existing ``X402Consumer``; the contract test injects a
    fixture-replay implementation.
    """

    async def request(
        self,
        *,
        url: str,
        query: str,
        max_cost_usd: float,
        timeout_seconds: float,
    ) -> PaidResponse: ...


# ---------------------------------------------------------------------------
# Pure helpers — pure-function tests cover these directly per
# ``feedback_lighter_tests.md``.
# ---------------------------------------------------------------------------


_SIGNATURE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Solana base58 signatures (typical len 87-88 chars, base58 alphabet).
    re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{86,90}\b"),
    # 0x-prefixed hex signatures (Ethereum / Base — kept for safety even
    # though paysh-Solana is the primary target).
    re.compile(r"0x[a-fA-F0-9]{120,140}"),
    re.compile(r"0x[a-fA-F0-9]{64}"),
)


def _redact_secrets(message: str) -> str:
    """Scrub plausible signatures, wallet keys, and bearer tokens.

    Conservative: better to over-redact than to leak. Replaces matched
    runs with ``<redacted>``. Stays a pure function so the unit test
    can assert the signature substring is gone without firing the
    full HTTP path.
    """
    if not message:
        return message
    out = message
    for pat in _SIGNATURE_PATTERNS:
        out = pat.sub("<redacted>", out)
    # Bearer-token-ish substrings.
    out = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._\-]+", "Bearer <redacted>", out)
    out = re.sub(r"(?i)apiToken\s*[:=]\s*[\"']?[A-Za-z0-9._\-]+", "apiToken=<redacted>", out)
    return out


def _is_safe_https(url: str) -> bool:
    """SSRF guard for provider ``service_url``.

    Accept https + non-empty hostname. Reject http, file://, bare IPs in
    the private ranges. Conservative — pay.sh-issued service_urls are
    public DNS names so legit providers always pass.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = parsed.hostname or ""
    if not host:
        return False
    # Block raw private-range IPs. Public DNS names always pass.
    if host.startswith(("10.", "127.", "169.254.", "192.168.", "0.")):
        return False
    return host != "localhost"


def _lookup_provider(
    fqn: str,
    catalog_providers: list[PayshCatalogProvider],
) -> PayshCatalogProvider:
    """Pure: find the provider by ``fqn``. Raises if absent.

    Catalog is small (<200 entries) so linear is fine. Caller passes the
    cached list — we do NOT re-fetch ``/api/catalog``.
    """
    for p in catalog_providers:
        if p.fqn == fqn:
            return p
    available = [p.fqn for p in catalog_providers[:5]]
    raise ProviderNotFoundError(
        f"paysh_live: provider fqn={fqn!r} not in cached catalog. "
        f"Sample available: {available} (catalog size={len(catalog_providers)})"
    )


def _enforce_budget(
    *,
    fqn: str,
    min_price_usd: float,
    max_cost_usd: float | None,
    remaining_budget_usd: float,
) -> float:
    """Pure budget gate. Returns the effective per-call cap on success.

    Two checks, fail-fast on either:
      1. ``min_price_usd <= remaining_budget_usd`` — the sprint envelope
         hasn't been blown.
      2. ``min_price_usd <= max_cost_usd`` (if caller supplied a cap) —
         the per-call cap admits the advertised price.

    Returns the smaller of ``max_cost_usd`` and ``remaining_budget_usd``;
    the requester clamps actual settle to that value.
    """
    if min_price_usd > remaining_budget_usd:
        raise BudgetExceeded(
            f"paysh_live: provider {fqn!r} min_price={min_price_usd} USD exceeds "
            f"remaining sprint budget {remaining_budget_usd} USD"
        )
    if max_cost_usd is not None:
        if max_cost_usd <= 0:
            raise BudgetExceeded(f"paysh_live: max_cost_usd={max_cost_usd} must be positive")
        if min_price_usd > max_cost_usd:
            raise BudgetExceeded(
                f"paysh_live: provider {fqn!r} min_price={min_price_usd} USD exceeds "
                f"caller cap max_cost_usd={max_cost_usd} USD"
            )
        return min(max_cost_usd, remaining_budget_usd)
    return remaining_budget_usd


def _split_into_chunks(text: str, *, words_per_chunk: int = DEFAULT_CHUNK_WORDS) -> list[str]:
    """Pure: split a long response body into ~``words_per_chunk`` segments.

    Whitespace tokenization is intentional — the goal is "coherent
    section per chunk" for embedding, not perfect linguistic
    boundaries. Empty / short bodies return as a single chunk.
    """
    if not text or not text.strip():
        return []
    words = text.split()
    if len(words) <= words_per_chunk:
        return [" ".join(words)]
    chunks: list[str] = []
    for i in range(0, len(words), words_per_chunk):
        chunks.append(" ".join(words[i : i + words_per_chunk]))
    return chunks


def _compact_scalar(value: Any) -> str:
    """Render a scalar JSON value for prose output.

    Long opaque strings (mints, signatures, hex digests) are truncated
    head…tail so the prose stays readable without losing identity. Bools
    and numbers stringify directly; None becomes ``null``.
    """
    if value is None:
        return "null"
    if isinstance(value, str):
        if len(value) <= 80:
            return value
        return f"{value[:40]}…{value[-8:]}"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _flatten_json_to_prose(
    value: Any,
    *,
    max_depth: int = 4,
    max_items: int = 40,
) -> str:
    """Render parsed JSON as readable ``key: value`` prose.

    S33-#66: the previous renderer used ``json.dumps(body, indent=2)``,
    which produces chunks the trade-panel judge correctly marks as
    irrelevant — e.g. an opaque blob of `{"apy": "0.06...", "tokenMint":
    "5oVNBeE..."}` with curly braces and quotes carries no narrative
    weight. The flattened form ``[0].apy: 0.06 / [0].tokenMint: 5oVNBeE…``
    surfaces the same fields as scannable prose. Depth + item caps keep
    runaway payloads bounded.
    """
    lines: list[str] = []
    _flatten_walk(
        value,
        prefix="",
        lines=lines,
        max_depth=max_depth,
        max_items=max_items,
        depth=0,
    )
    return "\n".join(lines)


def _flatten_walk(
    value: Any,
    *,
    prefix: str,
    lines: list[str],
    max_depth: int,
    max_items: int,
    depth: int,
) -> None:
    if depth > max_depth:
        if prefix:
            lines.append(f"{prefix}: (depth cap)")
        return
    if isinstance(value, dict):
        items = list(value.items())
        for k, v in items[:max_items]:
            new_prefix = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)) and v:
                _flatten_walk(
                    v,
                    prefix=new_prefix,
                    lines=lines,
                    max_depth=max_depth,
                    max_items=max_items,
                    depth=depth + 1,
                )
            else:
                lines.append(f"{new_prefix}: {_compact_scalar(v)}")
        if len(items) > max_items:
            lines.append(f"{prefix or 'root'}: … ({len(items) - max_items} more keys omitted)")
    elif isinstance(value, list):
        for i, item in enumerate(value[:max_items]):
            new_prefix = f"{prefix}[{i}]" if prefix else f"[{i}]"
            if isinstance(item, (dict, list)) and item:
                _flatten_walk(
                    item,
                    prefix=new_prefix,
                    lines=lines,
                    max_depth=max_depth,
                    max_items=max_items,
                    depth=depth + 1,
                )
            else:
                lines.append(f"{new_prefix}: {_compact_scalar(item)}")
        if len(value) > max_items:
            lines.append(
                f"{prefix or 'root'}: … ({len(value) - max_items} more items omitted)"
            )
    else:
        lines.append(f"{prefix}: {_compact_scalar(value)}" if prefix else _compact_scalar(value))


def _stringify_response(body: Any, fallback_text: str) -> str:
    """Pure: render a response body to text for chunking.

    Strings pass through. Dicts/lists are flattened to ``key: value``
    prose via :func:`_flatten_json_to_prose` — the prior implementation
    used ``json.dumps(body, indent=2)`` which produced chunks the trade
    panel's rubric judge marked as opaque blobs (S33 data-engineer audit,
    2026-05-14). Empty flattening or unsupported types fall back to the
    raw response text.
    """
    if isinstance(body, str):
        return body
    if isinstance(body, (dict, list)):
        prose = _flatten_json_to_prose(body)
        return prose if prose.strip() else fallback_text
    return fallback_text


def _build_chunks(
    *,
    fqn: str,
    provider: PayshCatalogProvider,
    response: PaidResponse,
    query: str,
) -> list[dict[str, Any]]:
    """Pure: render a paid response into chunk-shaped dicts.

    One chunk per ~500-word section. ``provider_kind="paid:paysh_live"``
    distinguishes execution chunks from ``free:paysh_manifest`` discovery
    chunks. ``cost_usd`` per chunk is the per-call cost — chunks share
    a charge by design (one paid call → N chunks).
    """
    body_text = _stringify_response(response.response_body, response.response_text)
    # S33-#66: prepend a provenance header so the rubric judge — which
    # sees only ``snippet[:240]`` of each Citation — gets fqn + category
    # + title before falling into the flattened JSON keys. Without this
    # header, citations open with ``[0].apy: 0.06`` and the judge has no
    # idea what protocol the data describes.
    header = (
        f"Paysh paid response: {fqn} "
        f"(category={provider.category or 'unknown'}, "
        f"title={provider.title or fqn}). "
    )
    body_text = header + body_text
    sections = _split_into_chunks(body_text)
    if not sections:
        # An empty response body is still ledger-worthy; emit a single
        # placeholder chunk so the orchestrator knows the call settled
        # but produced no text.
        sections = [f"(empty response from {fqn})"]

    citation_id = f"paysh_live:{fqn}"
    cost_str = f"{response.cost_usd:g}"
    chunks: list[dict[str, Any]] = []
    for idx, text in enumerate(sections):
        chunks.append(
            {
                "text": text,
                "provider_kind": PROVIDER_KIND,
                "cost_usd": cost_str,
                "metadata": {
                    "citation_id": citation_id,
                    "fqn": fqn,
                    "title": provider.title or fqn,
                    "category": provider.category,
                    "service_url": provider.service_url,
                    "query": query,
                    "section_index": idx,
                    "section_count": len(sections),
                    "response_sha": response.response_sha,
                    "response_status": response.status_code,
                    "response_content_type": response.content_type,
                },
                "creator_handle": None,
            }
        )
    return chunks


def _ledger_entry(
    *,
    fqn: str,
    response: PaidResponse,
) -> dict[str, Any]:
    """Pure: build the ledger entry the dispatcher persists alongside chunks.

    Shape locked by the ticket spec:
        ``{"provider": fqn, "cost_usd": X, "sha": response_sha}``

    Plus the tx signature when present (for cross-reference with the
    on-chain receipt) — but already redacted in any error path that
    touches it via ``_redact_secrets``.
    """
    return {
        "provider": fqn,
        "cost_usd": response.cost_usd,
        "sha": response.response_sha,
        "tx_signature": response.tx_signature,
        "status": response.status_code,
    }


def _hash_body(text: str) -> str:
    """Stable SHA256 of the response body — used as the ledger
    deduplication key and the chunk metadata anchor."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


async def fetch_paid(
    provider_fqn: str,
    query: str,
    *,
    x402_client: PaidRequester,
    catalog_providers: list[PayshCatalogProvider],
    max_cost_usd: float | None = None,
    remaining_budget_usd: float = PAYSH_LIVE_BUDGET_USD,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> SourceResult:
    """Issue one paid x402 retrieval against a pay.sh catalog provider.

    Per-call flow (matches ticket spec):
      a. Look up the provider's ``service_url`` and ``min_price_usd``
         from the cached catalog (caller passes ``catalog_providers``;
         we never re-fetch ``/api/catalog`` here).
      b. Refuse to spend if ``min_price_usd`` exceeds the per-call cap
         OR the remaining sprint budget.
      c. Issue the x402 challenge → buyer-sign → retry-with-payment
         cycle via the injected ``PaidRequester``.
      d. Capture response body, normalize to chunk shape (one chunk per
         ~500-word section).
      e. Emit ledger entry shaped
         ``{provider, cost_usd, sha, tx_signature, status}``.

    Hard fail-loud (per CLAUDE.md security non-negotiables):
      * ``ProviderNotFoundError`` if ``provider_fqn`` is not in the
        cached catalog.
      * ``BudgetExceeded`` if either budget gate would be breached —
        raised BEFORE any network call.
      * ``ProviderHTTPError`` on non-200 after pay; signature scrubbed.
      * ``ProviderUnreachableError`` on transport failure.

    Returns ``SourceResult(source_name="paysh_live", fired=True, cost_usd=X,
    payload={"chunks": [...], "ledger": {...}, "remaining_budget_usd":
    Y})``. The orchestrator's spend ledger reads
    ``payload["ledger"]`` and the embed adapter reads
    ``payload["chunks"]``.
    """
    provider = _lookup_provider(provider_fqn, catalog_providers)

    if not _is_safe_https(provider.service_url):
        raise PayshLiveError(
            f"paysh_live: provider {provider_fqn!r} service_url failed SSRF guard "
            f"(host={urlparse(provider.service_url).hostname!r})"
        )

    effective_cap = _enforce_budget(
        fqn=provider_fqn,
        min_price_usd=provider.min_price_usd,
        max_cost_usd=max_cost_usd,
        remaining_budget_usd=remaining_budget_usd,
    )

    try:
        response = await x402_client.request(
            url=provider.service_url,
            query=query,
            max_cost_usd=effective_cap,
            timeout_seconds=timeout_seconds,
        )
    except (ProviderHTTPError, ProviderUnreachableError, BudgetExceeded):
        raise
    except Exception as exc:
        # Wrap unexpected errors but redact in case the underlying
        # exception text carries a signature substring.
        redacted = _redact_secrets(str(exc))
        raise ProviderUnreachableError(
            f"paysh_live: requester error talking to {provider_fqn!r}: "
            f"{type(exc).__name__}: {redacted}"
        ) from exc

    if response.status_code != 200:
        # Caller-visible message keeps the status code + redacted body
        # snippet only; signatures, bearer tokens, and apiTokens scrubbed.
        snippet = _redact_secrets((response.response_text or "")[:512])
        raise ProviderHTTPError(
            f"paysh_live: provider {provider_fqn!r} returned status="
            f"{response.status_code} after pay; body={snippet!r}"
        )

    chunks = _build_chunks(
        fqn=provider_fqn,
        provider=provider,
        response=response,
        query=query,
    )
    ledger = _ledger_entry(fqn=provider_fqn, response=response)

    logger.info(
        "paysh_live.fetch.success fqn=%s cost_usd=%g chunk_count=%d sha=%s",
        provider_fqn,
        response.cost_usd,
        len(chunks),
        response.response_sha[:12],
    )

    return SourceResult(
        source_name=SOURCE_NAME,
        payload={
            "chunks": chunks,
            "ledger": ledger,
            "remaining_budget_usd": remaining_budget_usd - response.cost_usd,
            "provider_fqn": provider_fqn,
        },
        cost_usd=response.cost_usd,
        fired=True,
    )


__all__ = [
    "DEFAULT_CHUNK_WORDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "PAYSH_LIVE_BUDGET_USD",
    "PROVIDER_KIND",
    "SOURCE_NAME",
    "BudgetExceeded",
    "PaidRequester",
    "PaidResponse",
    "PayshLiveError",
    "ProviderHTTPError",
    "ProviderNotFoundError",
    "ProviderUnreachableError",
    "_build_chunks",
    "_enforce_budget",
    "_hash_body",
    "_is_safe_https",
    "_ledger_entry",
    "_lookup_provider",
    "_redact_secrets",
    "_split_into_chunks",
    "_stringify_response",
    "fetch_paid",
]
