"""BazaarManifestSource — discovery provider for the Coinbase Agentic Wallet marketplace.

S22-BAZAAR-INGEST-01. Mirrors the pay.sh manifest crawler shape against
Bazaar's public ``/v1/services`` catalog at
``https://api.agentic.market/`` — the multi-chain x402 service registry
hosted by Coinbase's Agentic Wallet team.

Module path note: this file lives at ``gecko_core/sources/bazaar_manifest.py``
(NOT ``gecko_core/sources/bazaar.py``) because the ``bazaar/`` sibling
package already exists from S16-BAZAAR-CONSUMER-* and would shadow a
top-level ``bazaar.py`` at import time. The naming mirrors the
``paysh_manifest`` / ``paysh_live`` pair on the pay.sh side.

Why a dedicated provider (and not "just a Tavily query"):
    - Bazaar IS one of Gecko's actual close competitors AND a peer
      retrieval source (per the 2026-05-07 competitor map). Reading the
      catalog directly gives us the machine-readable list of paid x402
      services across Base / Solana / Polygon — Tavily would surface
      marketing pages.
    - Each service entry is already a self-contained chunk (name +
      description + provider + category + supported networks). No
      client-side chunking required.
    - No auth, no rate limit at our session volumes — discovery is open
      by design. Operator wallet auth is needed ONLY for the live caller
      (``bazaar_live.py``); this module is purely free discovery.

Trust profile:
    - Free, unauthenticated, public REST endpoint.
    - SSRF-guarded: the request URL is host-locked to
      ``api.agentic.market``; redirects to a different host abort the
      fetch with an error rather than following blindly.

The provider returns a ``SourceResult`` whose ``payload["chunks"]`` is a
list of dicts shaped like a ``BazaarChunk`` (matching ``arxiv/provider``)
so the downstream rendering + economics ledger code paths don't fork on
provider type. Cost is always 0.0 (free).

Wire into the dispatch graph in a future N5-equivalent ticket; this
module is import-safe and side-effect free until ``fetch_catalog`` is
awaited.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gecko_core.sources import SourceResult

logger = logging.getLogger(__name__)


BAZAAR_CATALOG_URL: str = "https://api.agentic.market/v1/services"
BAZAAR_SEARCH_URL: str = "https://api.agentic.market/v1/services/search"
BAZAAR_HOST: str = "api.agentic.market"
DEFAULT_TIMEOUT_SECONDS: float = 8.0
DEFAULT_MAX_ENTRIES: int = 100

# Categories worth surfacing for neobank-flavored ideas. Pinned against
# the live category vocabulary observed 2026-05-07: Bazaar uses
# Title-Case category labels (``Search``, ``Inference``, ``Data``,
# ``Infra``, ``Media``, ``Travel``, ``Social``, ``Storage``). Finance /
# Identity are reserved for forward-compat — they may not appear in the
# 2026-05-07 page but the spec includes them.
_NEOBANK_RELEVANT_CATEGORIES: frozenset[str] = frozenset(
    {"Search", "Inference", "Data", "Infra", "Finance", "Identity"}
)
_NEOBANK_RELEVANT_KEYWORDS: tuple[str, ...] = (
    "kyc",
    "ocr",
    "identity",
    "verification",
    "market",
    "price",
    "compliance",
    "treasury",
    "balance",
    "rpc",
)

SOURCE_NAME: str = "bazaar_manifest"


# ---------------------------------------------------------------------------
# Catalog schema — pinned 2026-05-07 against ``/v1/services``.
# ---------------------------------------------------------------------------


class BazaarPricing(BaseModel):
    """One endpoint's pricing block.

    Permissive — Bazaar emits ``amount`` as a string-encoded USDC value
    (e.g. ``"0.001"``); we keep it a string and let the caller cast.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    amount: str = ""
    currency: str = ""
    network: str = ""
    scheme: str = ""
    maxAmount: str = ""
    minAmount: str = ""


class BazaarEndpoint(BaseModel):
    """One endpoint under ``service.endpoints[]``.

    Only ``url`` is strictly necessary for the live caller; everything
    else is optional and defaults so a partial entry still produces a
    usable chunk.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    url: str = ""
    description: str = ""
    method: str = ""
    pricing: BazaarPricing | None = None


class BazaarPriceSummary(BaseModel):
    """Per-service price summary block (avg, min, max).

    Bazaar emits all numeric values as strings — ``"0.001"`` — so we
    preserve that shape and let consumers cast.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    minAmount: str = ""
    maxAmount: str = ""
    avgCostPerTransaction: str = ""
    avgCostBasis: str = ""
    currency: str = ""


class BazaarService(BaseModel):
    """One entry under ``services[]`` in the Bazaar catalog.

    Permissive on optional fields. ``id`` is the only strict requirement
    — it's the URL anchor / citation key. Everything else defaults so a
    partial service record still produces a usable chunk.

    Pinned shape (2026-05-07): ``id``, ``name``, ``description``,
    ``domain``, ``provider``, ``providerUrl``, ``category``,
    ``networks[]``, ``enriched``, ``endpoints[]``, ``integrationType``,
    ``isNew``, ``priceSummary``.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: str
    name: str = ""
    description: str = ""
    domain: str = ""
    provider: str = ""
    providerUrl: str = ""
    category: str = ""
    networks: list[str] = Field(default_factory=list)
    enriched: bool = False
    endpoints: list[BazaarEndpoint] = Field(default_factory=list)
    integrationType: str = ""
    isNew: bool = False
    priceSummary: BazaarPriceSummary | None = None


class BazaarCatalog(BaseModel):
    """Top-level shape served from ``/v1/services``.

    Pinned 2026-05-07: ``services[]``, ``total``, ``limit``, ``offset``.
    Unknown top-level keys are ignored so Bazaar can grow the response
    without breaking us.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    services: list[BazaarService] = Field(default_factory=list)
    total: int = 0
    limit: int = 0
    offset: int = 0


# ---------------------------------------------------------------------------
# SSRF guard.
# ---------------------------------------------------------------------------


def _is_bazaar_url(url: str) -> bool:
    """True iff ``url`` is on the ``api.agentic.market`` host over HTTPS.

    Used as the SSRF guard. Conservative by design: we accept this exact
    apex-with-subdomain only. Subdomain-prefix support, if Bazaar ever
    needs it, ships as a deliberate change here so the threat model
    stays explicit.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    return parsed.hostname == BAZAAR_HOST


# ---------------------------------------------------------------------------
# Service → chunk normalization.
# ---------------------------------------------------------------------------


def _format_networks(networks: list[str]) -> str:
    """Pure helper: render the supported-networks list as a phrase.

    Examples::

        "Networks: Base, Solana."
        "Networks: Base."
        "" (empty when no networks)
    """
    if not networks:
        return ""
    return f"Networks: {', '.join(networks)}."


def _format_price_band(summary: BazaarPriceSummary | None) -> str:
    """Pure helper: render a one-sentence price-band suffix.

    Examples::

        "Pricing: $0.001 – $0.015 USDC avg $0.001."
        "Pricing: $0.01 USDC."
        "" (empty when no summary or all zero)

    Stays a pure function so the unit test stays light per
    ``feedback_lighter_tests.md``.
    """
    if summary is None:
        return ""
    lo = summary.minAmount.strip()
    hi = summary.maxAmount.strip()
    avg = summary.avgCostPerTransaction.strip()
    cur = summary.currency.strip() or "USDC"
    if not lo and not hi and not avg:
        return ""
    if lo and hi and lo != hi:
        body = f"${lo} – ${hi} {cur}"  # noqa: RUF001 (en dash)
    elif lo:
        body = f"${lo} {cur}"
    elif avg:
        body = f"${avg} {cur}"
    else:
        return ""
    if avg and (lo != hi):
        body += f" avg ${avg}"
    return f"Pricing: {body}."


def _service_to_chunk(service: BazaarService) -> dict[str, Any] | None:
    """Render one catalog service as a chunk-shaped dict.

    Returns ``None`` for an entry with no ``id`` (uncitable). The
    ``provider_kind="free:bazaar_manifest"`` mirrors the arxiv adapter's
    ``free:<src>`` convention. Citation reads as ``bazaar_manifest:<id>``;
    the anchored URL stays in metadata for the renderer.
    """
    sid = service.id.strip()
    if not sid:
        return None

    name = service.name.strip() or sid
    description = service.description.strip()
    category = service.category.strip()

    body_parts: list[str] = [name]
    if category:
        body_parts.append(f"[{category}]")
    if description:
        body_parts.append(description)
    networks_phrase = _format_networks(service.networks)
    if networks_phrase:
        body_parts.append(networks_phrase)
    price_phrase = _format_price_band(service.priceSummary)
    if price_phrase:
        body_parts.append(price_phrase)
    text = " ".join(body_parts).strip()

    anchored_url = f"https://agentic.market/services/{sid}"
    citation_id = f"bazaar_manifest:{sid}"

    return {
        "text": text,
        "provider_kind": "free:bazaar_manifest",
        "cost_usd": "0",
        "metadata": {
            "citation_id": citation_id,
            "id": sid,
            "name": name,
            "category": category,
            "domain": service.domain,
            "provider": service.provider,
            "provider_url": service.providerUrl,
            "networks": list(service.networks),
            "endpoint_count": len(service.endpoints),
            "integration_type": service.integrationType,
            "is_new": service.isNew,
            "source_url": anchored_url,
        },
        "creator_handle": None,
    }


# ---------------------------------------------------------------------------
# Filter helper for downstream selection.
# ---------------------------------------------------------------------------


def filter_neobank_relevant(services: list[BazaarService]) -> list[BazaarService]:
    """Pure selector for neobank-relevant catalog services.

    Two-stage filter:
      1. ``category`` is in the neobank-relevant set
         (``Search`` / ``Inference`` / ``Data`` / ``Infra`` / ``Finance`` /
         ``Identity``).
      2. ``description`` contains at least one of the neobank keywords
         (kyc, ocr, identity, verification, market, price, compliance,
         treasury, balance, rpc).

    Lives in the data layer per the lane boundary — the live caller
    picks targets from this filtered slice rather than re-implementing
    the selection logic itself.
    """
    out: list[BazaarService] = []
    for s in services:
        if s.category not in _NEOBANK_RELEVANT_CATEGORIES:
            continue
        haystack = s.description.lower()
        if any(kw in haystack for kw in _NEOBANK_RELEVANT_KEYWORDS):
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Catalog fetcher.
# ---------------------------------------------------------------------------


async def _fetch_and_parse(
    *,
    url: str,
    client: httpx.AsyncClient | None,
    timeout_seconds: float,
    max_entries: int,
    source_tag: str,
) -> SourceResult:
    """Internal: shared fetch + parse + chunkify path for catalog and search.

    ``source_tag`` is the value pasted into the error string + log
    namespace; it does NOT change the ``source_name`` on the result
    (which always stays ``bazaar_manifest`` per Pattern A).
    """
    if not _is_bazaar_url(url):
        return SourceResult(
            source_name=SOURCE_NAME,
            payload={"chunks": []},
            fired=False,
            error=f"{source_tag}: SSRF guard rejected url host={urlparse(url).hostname!r}",
        )

    owns_local = False
    http = client
    if http is None:
        # follow_redirects=False is the SSRF guard's second leg.
        http = httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
        owns_local = True

    try:
        try:
            resp = await http.get(url)
        except httpx.HTTPError as exc:
            return SourceResult(
                source_name=SOURCE_NAME,
                payload={"chunks": []},
                fired=False,
                error=f"{source_tag}: http error: {type(exc).__name__}: {exc}",
            )

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            if not _is_bazaar_url(location):
                return SourceResult(
                    source_name=SOURCE_NAME,
                    payload={"chunks": []},
                    fired=False,
                    error=(
                        f"{source_tag}: SSRF guard rejected redirect "
                        f"host={urlparse(location).hostname!r}"
                    ),
                )

        if resp.status_code >= 400:
            return SourceResult(
                source_name=SOURCE_NAME,
                payload={"chunks": []},
                fired=False,
                error=f"{source_tag}: HTTP {resp.status_code}",
            )

        try:
            raw = resp.json()
        except ValueError as exc:
            return SourceResult(
                source_name=SOURCE_NAME,
                payload={"chunks": []},
                fired=False,
                error=f"{source_tag}: non-json body: {exc}",
            )

        try:
            catalog = BazaarCatalog.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                "%s.schema.invalid url=%s errors=%s",
                source_tag,
                url,
                exc.errors()[:3],
            )
            return SourceResult(
                source_name=SOURCE_NAME,
                payload={"chunks": []},
                fired=False,
                error=f"{source_tag}: schema validation failed: {exc.error_count()} errors",
            )

        chunks: list[dict[str, Any]] = []
        skipped = 0
        for service in catalog.services[:max_entries]:
            chunk = _service_to_chunk(service)
            if chunk is None:
                skipped += 1
                logger.warning(
                    "%s.service.skipped id=%r reason=empty_id",
                    source_tag,
                    service.id,
                )
                continue
            chunks.append(chunk)

        logger.info(
            "%s.fetch.success url=%s service_count=%d chunk_count=%d skipped=%d",
            source_tag,
            url,
            len(catalog.services),
            len(chunks),
            skipped,
        )

        return SourceResult(
            source_name=SOURCE_NAME,
            payload={
                "chunks": chunks,
                "service_count": len(catalog.services),
                "total": catalog.total,
                "limit": catalog.limit,
                "offset": catalog.offset,
            },
            cost_usd=0.0,
            fired=True,
        )
    finally:
        if owns_local:
            await http.aclose()


async def fetch_catalog(
    client: httpx.AsyncClient | None = None,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    catalog_url: str = BAZAAR_CATALOG_URL,
) -> SourceResult:
    """Fetch the Bazaar service catalog and normalize into chunks.

    Returns a ``SourceResult`` with ``payload["chunks"]`` shaped like
    ``BazaarChunk`` dicts — one chunk per service. Errors (timeout,
    non-200, redirect to a non-Bazaar host, unparsable JSON, schema
    mismatch) surface as ``fired=False`` with a structured ``error``
    string — never raised — so a flaky Bazaar deploy doesn't take down
    a dispatch.

    Per-entry validation failures are logged at WARN and skipped; a
    single malformed service never poisons the whole batch.
    """
    return await _fetch_and_parse(
        url=catalog_url,
        client=client,
        timeout_seconds=timeout_seconds,
        max_entries=max_entries,
        source_tag="bazaar_manifest",
    )


def _build_search_url(query: str, *, base: str = BAZAAR_SEARCH_URL) -> str:
    """Pure: build the ``/v1/services/search?q=...`` URL.

    Stays a pure function so the unit test stays light per
    ``feedback_lighter_tests.md``.
    """
    return f"{base}?{urlencode({'q': query})}"


async def fetch_search(
    query: str,
    client: httpx.AsyncClient | None = None,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    search_url: str = BAZAAR_SEARCH_URL,
) -> SourceResult:
    """Query Bazaar's ``/v1/services/search`` and normalize into chunks.

    Same chunk shape as :func:`fetch_catalog`. Tagged via the same
    ``bazaar_manifest`` source literal — search is just a filtered view
    of the catalog, not a different KnowledgeSource.
    """
    url = _build_search_url(query, base=search_url)
    return await _fetch_and_parse(
        url=url,
        client=client,
        timeout_seconds=timeout_seconds,
        max_entries=max_entries,
        source_tag="bazaar_manifest.search",
    )


__all__ = [
    "BAZAAR_CATALOG_URL",
    "BAZAAR_HOST",
    "BAZAAR_SEARCH_URL",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TIMEOUT_SECONDS",
    "SOURCE_NAME",
    "BazaarCatalog",
    "BazaarEndpoint",
    "BazaarPriceSummary",
    "BazaarPricing",
    "BazaarService",
    "_build_search_url",
    "_format_networks",
    "_format_price_band",
    "_is_bazaar_url",
    "_service_to_chunk",
    "fetch_catalog",
    "fetch_search",
    "filter_neobank_relevant",
]
