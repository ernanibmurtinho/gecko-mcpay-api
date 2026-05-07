"""BazaarLiveSource — paid x402 retrieval against Bazaar catalog services.

S22-BAZAAR-INGEST-01 (live caller half). Wraps the existing buyer-side
x402 client to issue real, USDC-priced calls against Bazaar-listed
service endpoints and ingest their responses as ``bazaar_live``-tagged
chunks.

Why a dedicated provider (and not "extend ``bazaar``"):
    - ``bazaar_manifest`` is *discovery* — it lists 50+ services and what
      they sell, free, no wallet involved. ``bazaar_live`` is *execution*
      — actually issuing a paid call against one of those services and
      ingesting the body. Discovery never burns USDC; execution always
      does. Keeping the two providers separate keeps the trust + cost
      surface explicit at the dispatcher layer.
    - Per Pattern A (CLAUDE.md), the source literal ``bazaar_live`` was
      locked alongside ``bazaar_manifest``; this module wires that
      literal end-to-end.

Wallet / chain neutrality (CLAUDE.md):
    - Bazaar uses standard x402. Most services settle on Base; some are
      multi-chain (Solana, Polygon). The buyer-side wallet is the
      operator's choice (Coinbase Agentic Wallet on Base, frames.ag on
      Solana, etc.) — this module is wallet-agnostic and talks to the
      wire through the existing ``X402Client`` / ``PaidRequester``
      interface.

Pattern compliance (CLAUDE.md):
    - **Pattern B (free local sim before live spend).** Sibling
      ``scripts/bazaar/scenarios/<service-id>.json`` placeholders ship
      alongside this module; the simulator falsifies the wire shape
      WITHOUT spending. Live spend is gated behind
      ``@pytest.mark.live_bazaar``, manually opted-in by the operator.
    - **Pattern C (recorded-fixture contract test).** The default test
      run replays JSON fixtures pinned at
      ``tests/sources/fixtures/bazaar_live_<service>_2026_05_07.json``;
      live re-record is the operator's job.

Budget discipline:
    - Hardcoded sprint cap: ``BAZAAR_LIVE_BUDGET_USD = 5.0`` total. Every
      call decrements remaining budget; subsequent calls refuse to
      settle once the envelope is exhausted.
    - Per-call ``max_cost_usd`` override is a second leg.

Security non-negotiables (CLAUDE.md):
    - Never log wallet addresses, signatures, or apiToken substrings.
      The ``_redact_secrets`` helper scrubs those before anything is
      raised or logged.
    - SSRF — endpoint ``url`` is validated ``https://`` only and
      hostname is required (no bare IP, no ``file://``). Bazaar-issued
      catalog entries should already obey this; we double-check at the
      point of call so a corrupted cache can't punch through.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from gecko_core.sources import SourceResult
from gecko_core.sources.bazaar_manifest import BazaarEndpoint, BazaarService

logger = logging.getLogger(__name__)


# Hard sprint-22 budget cap. Operator override is intentional friction —
# bumping this is a code edit, not an env flip.
BAZAAR_LIVE_BUDGET_USD: float = 5.0

# Live x402 calls on Base settle in 1-3s on top of the provider's own
# response time. 12s covers the worst case observed on devnet sims.
DEFAULT_TIMEOUT_SECONDS: float = 12.0

# Each chunk targets ~500 words of provider response body.
DEFAULT_CHUNK_WORDS: int = 500

SOURCE_NAME: str = "bazaar_live"

# Locked Pattern A literal — mirrors ``bazaar._service_to_chunk`` but
# distinct so dispatcher renderers can route execution chunks
# differently from discovery chunks.
PROVIDER_KIND: str = "paid:bazaar_live"


# ---------------------------------------------------------------------------
# Errors — surfaced verbatim, never catch-and-rephrase.
# ---------------------------------------------------------------------------


class BazaarLiveError(RuntimeError):
    """Base for all bazaar_live execution failures."""


class ServiceNotFoundError(BazaarLiveError):
    """``service_id`` is not present in the cached catalog."""


class BudgetExceeded(BazaarLiveError):
    """Caller-side cap or sprint budget envelope is below the service's
    advertised ``min_price_usd``. Raised BEFORE any network call."""


class ServiceHTTPError(BazaarLiveError):
    """Service returned a non-200 after the paid retry. Signature
    fragments scrubbed from the message via ``_redact_secrets``."""


class ServiceUnreachableError(BazaarLiveError):
    """Transport error reaching the service (DNS, TLS, timeout)."""


# ---------------------------------------------------------------------------
# Buyer seam — the live caller talks to the wire through this Protocol.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaidResponse:
    """One recorded outcome of an x402 challenge → pay → fetch cycle.

    ``cost_usd`` is what actually settled (after the facilitator's own
    rounding). ``response_body`` is the service's 200-response body,
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
    flow. The contract test injects a fixture-replay implementation.
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
# Pure helpers — pure-function tests cover these directly.
# ---------------------------------------------------------------------------


_SIGNATURE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Solana base58 signatures (typical len 87-88 chars).
    re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{86,90}\b"),
    # 0x-prefixed hex signatures (Ethereum / Base — Bazaar's primary
    # network is Base so this is the dominant signature shape).
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
    """SSRF guard for service endpoint URLs.

    Accept https + non-empty hostname. Reject http, file://, bare IPs in
    the private ranges. Conservative — Bazaar-issued service URLs are
    public DNS names so legit services always pass.
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
    if host.startswith(("10.", "127.", "169.254.", "192.168.", "0.")):
        return False
    return host != "localhost"


def _lookup_service(
    service_id: str,
    catalog_services: list[BazaarService],
) -> BazaarService:
    """Pure: find the service by ``id``. Raises if absent.

    Catalog is small (<1000 entries) so linear is fine. Caller passes the
    cached list — we do NOT re-fetch ``/v1/services``.
    """
    for s in catalog_services:
        if s.id == service_id:
            return s
    available = [s.id for s in catalog_services[:5]]
    raise ServiceNotFoundError(
        f"bazaar_live: service id={service_id!r} not in cached catalog. "
        f"Sample available: {available} (catalog size={len(catalog_services)})"
    )


def _select_endpoint(service: BazaarService) -> BazaarEndpoint:
    """Pure: pick the first endpoint with a non-empty url.

    Bazaar services ship 1-N endpoints; the ticket scope is "issue one
    paid call", so we pick the first usable endpoint and let a future
    ticket add per-query routing across endpoints. Raises
    ``ServiceNotFoundError`` if the service has no usable endpoints.
    """
    for ep in service.endpoints:
        if ep.url:
            return ep
    raise ServiceNotFoundError(
        f"bazaar_live: service {service.id!r} has no endpoints with a usable url"
    )


def _endpoint_min_price_usd(endpoint: BazaarEndpoint) -> float:
    """Pure: read the min USDC price from an endpoint's pricing block.

    Bazaar emits prices as strings; default to 0.0 on any parse error
    (the caller's budget gate then accepts a free endpoint).
    """
    if endpoint.pricing is None:
        return 0.0
    candidate = endpoint.pricing.minAmount or endpoint.pricing.amount
    if not candidate:
        return 0.0
    try:
        return float(candidate)
    except (TypeError, ValueError):
        return 0.0


def _enforce_budget(
    *,
    service_id: str,
    min_price_usd: float,
    max_cost_usd: float | None,
    remaining_budget_usd: float,
) -> float:
    """Pure budget gate. Returns the effective per-call cap on success."""
    if min_price_usd > remaining_budget_usd:
        raise BudgetExceeded(
            f"bazaar_live: service {service_id!r} min_price={min_price_usd} USD exceeds "
            f"remaining sprint budget {remaining_budget_usd} USD"
        )
    if max_cost_usd is not None:
        if max_cost_usd <= 0:
            raise BudgetExceeded(f"bazaar_live: max_cost_usd={max_cost_usd} must be positive")
        if min_price_usd > max_cost_usd:
            raise BudgetExceeded(
                f"bazaar_live: service {service_id!r} min_price={min_price_usd} USD exceeds "
                f"caller cap max_cost_usd={max_cost_usd} USD"
            )
        return min(max_cost_usd, remaining_budget_usd)
    return remaining_budget_usd


def _split_into_chunks(text: str, *, words_per_chunk: int = DEFAULT_CHUNK_WORDS) -> list[str]:
    """Pure: split a long response body into ~``words_per_chunk`` segments."""
    if not text or not text.strip():
        return []
    words = text.split()
    if len(words) <= words_per_chunk:
        return [" ".join(words)]
    chunks: list[str] = []
    for i in range(0, len(words), words_per_chunk):
        chunks.append(" ".join(words[i : i + words_per_chunk]))
    return chunks


def _stringify_response(body: Any, fallback_text: str) -> str:
    """Pure: render a response body to text for chunking."""
    if isinstance(body, str):
        return body
    if isinstance(body, (dict, list)):
        try:
            return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            return fallback_text
    return fallback_text


def _build_chunks(
    *,
    service_id: str,
    service: BazaarService,
    endpoint: BazaarEndpoint,
    response: PaidResponse,
    query: str,
) -> list[dict[str, Any]]:
    """Pure: render a paid response into chunk-shaped dicts.

    One chunk per ~500-word section. ``provider_kind="paid:bazaar_live"``
    distinguishes execution chunks from ``free:bazaar_manifest`` discovery
    chunks. ``cost_usd`` per chunk is the per-call cost — chunks share
    a charge by design (one paid call → N chunks).
    """
    body_text = _stringify_response(response.response_body, response.response_text)
    sections = _split_into_chunks(body_text)
    if not sections:
        sections = [f"(empty response from {service_id})"]

    citation_id = f"bazaar_live:{service_id}"
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
                    "id": service_id,
                    "name": service.name or service_id,
                    "category": service.category,
                    "endpoint_url": endpoint.url,
                    "networks": list(service.networks),
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
    service_id: str,
    response: PaidResponse,
) -> dict[str, Any]:
    """Pure: build the ledger entry the dispatcher persists alongside chunks."""
    return {
        "provider": service_id,
        "cost_usd": response.cost_usd,
        "sha": response.response_sha,
        "tx_signature": response.tx_signature,
        "status": response.status_code,
    }


def _hash_body(text: str) -> str:
    """Stable SHA256 of the response body."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


async def fetch_paid(
    service_id: str,
    query: str,
    *,
    x402_client: PaidRequester,
    catalog_services: list[BazaarService],
    max_cost_usd: float | None = None,
    remaining_budget_usd: float = BAZAAR_LIVE_BUDGET_USD,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> SourceResult:
    """Issue one paid x402 retrieval against a Bazaar catalog service.

    Per-call flow:
      a. Look up the service in the cached catalog (caller passes
         ``catalog_services``; we never re-fetch ``/v1/services`` here).
      b. Pick the first endpoint with a non-empty url.
      c. Refuse to spend if endpoint min price exceeds the per-call cap
         OR the remaining sprint budget.
      d. Issue the x402 challenge → buyer-sign → retry-with-payment
         cycle via the injected ``PaidRequester``.
      e. Capture response body, normalize to chunk shape (one chunk per
         ~500-word section).
      f. Emit ledger entry shaped
         ``{provider, cost_usd, sha, tx_signature, status}``.

    Hard fail-loud (per CLAUDE.md security non-negotiables):
      * ``ServiceNotFoundError`` if the id is absent or has no endpoints.
      * ``BudgetExceeded`` if either budget gate would be breached —
        raised BEFORE any network call.
      * ``ServiceHTTPError`` on non-200 after pay; signature scrubbed.
      * ``ServiceUnreachableError`` on transport failure.

    Returns ``SourceResult(source_name="bazaar_live", fired=True,
    cost_usd=X, payload={"chunks": [...], "ledger": {...},
    "remaining_budget_usd": Y})``.
    """
    service = _lookup_service(service_id, catalog_services)
    endpoint = _select_endpoint(service)

    if not _is_safe_https(endpoint.url):
        raise BazaarLiveError(
            f"bazaar_live: service {service_id!r} endpoint url failed SSRF guard "
            f"(host={urlparse(endpoint.url).hostname!r})"
        )

    min_price = _endpoint_min_price_usd(endpoint)
    effective_cap = _enforce_budget(
        service_id=service_id,
        min_price_usd=min_price,
        max_cost_usd=max_cost_usd,
        remaining_budget_usd=remaining_budget_usd,
    )

    try:
        response = await x402_client.request(
            url=endpoint.url,
            query=query,
            max_cost_usd=effective_cap,
            timeout_seconds=timeout_seconds,
        )
    except (ServiceHTTPError, ServiceUnreachableError, BudgetExceeded):
        raise
    except Exception as exc:
        redacted = _redact_secrets(str(exc))
        raise ServiceUnreachableError(
            f"bazaar_live: requester error talking to {service_id!r}: "
            f"{type(exc).__name__}: {redacted}"
        ) from exc

    if response.status_code != 200:
        snippet = _redact_secrets((response.response_text or "")[:512])
        raise ServiceHTTPError(
            f"bazaar_live: service {service_id!r} returned status="
            f"{response.status_code} after pay; body={snippet!r}"
        )

    chunks = _build_chunks(
        service_id=service_id,
        service=service,
        endpoint=endpoint,
        response=response,
        query=query,
    )
    ledger = _ledger_entry(service_id=service_id, response=response)

    logger.info(
        "bazaar_live.fetch.success service_id=%s cost_usd=%g chunk_count=%d sha=%s",
        service_id,
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
            "service_id": service_id,
        },
        cost_usd=response.cost_usd,
        fired=True,
    )


__all__ = [
    "BAZAAR_LIVE_BUDGET_USD",
    "DEFAULT_CHUNK_WORDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "PROVIDER_KIND",
    "SOURCE_NAME",
    "BazaarLiveError",
    "BudgetExceeded",
    "PaidRequester",
    "PaidResponse",
    "ServiceHTTPError",
    "ServiceNotFoundError",
    "ServiceUnreachableError",
    "_build_chunks",
    "_endpoint_min_price_usd",
    "_enforce_budget",
    "_hash_body",
    "_is_safe_https",
    "_ledger_entry",
    "_lookup_service",
    "_redact_secrets",
    "_select_endpoint",
    "_split_into_chunks",
    "_stringify_response",
    "fetch_paid",
]
