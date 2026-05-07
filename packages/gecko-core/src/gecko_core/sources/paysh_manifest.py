"""PayshManifestSource — discovery provider for the pay.sh agent-skills index.

Backed by the public well-known endpoint:
    https://pay.sh/.well-known/agent-skills/index.json

Why a dedicated provider (and not "just a Tavily query"):
    - The manifest IS the canonical registry of x402 / pay-MCP skills.
      Tavily would surface marketing pages; the manifest gives us the
      machine-readable skill list directly.
    - Each entry is already a self-contained chunk (name + title +
      description + url). No client-side chunking required.
    - No key, no rate limit at our session volumes — the endpoint is
      public and small (a handful of skills today, expected to grow into
      the dozens over the year).

Gating policy (S22-N2):
    - Always fires when the classifier returns a vertical in
      {neobank, dex, marketplace, fintech} — these are the verticals
      where x402 / paid-API discovery moves the verdict.
    - Also fires when the idea text contains an x402/paysh signal
      pattern ("x402", "pay.sh", "paysh", "agent skill", "402", "paid api",
      "micropayment", "wallet-approved", "pay mcp", ...).

Trust profile:
    - Free, unauthenticated, public ``.well-known`` endpoint.
    - SSRF-guarded: the request URL is host-locked to ``pay.sh``;
      redirects to a different host abort the fetch with an error rather
      than following blindly.

The provider returns a ``SourceResult`` whose ``payload["chunks"]`` is a
list of dicts shaped like a ``BazaarChunk`` (matching ``arxiv/provider``)
so the downstream rendering + economics ledger code paths don't fork on
provider type. Cost is always 0.0 (free).

Wired into the dispatch graph by S22-N5; this module is import-safe and
side-effect free until ``fetch_manifest`` is awaited.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gecko_core.sources import SourceResult

logger = logging.getLogger(__name__)


PAYSH_MANIFEST_URL: str = "https://pay.sh/.well-known/agent-skills/index.json"
PAYSH_CATALOG_URL: str = "https://pay.sh/api/catalog"
PAYSH_HOST: str = "pay.sh"
DEFAULT_TIMEOUT_SECONDS: float = 8.0
DEFAULT_MAX_ENTRIES: int = 75
DEFAULT_CATALOG_MAX_ENTRIES: int = 100

# Categories worth surfacing for neobank-flavored ideas. Pinned against
# the live category vocabulary observed 2026-05-07: ``ai_ml`` /
# ``finance`` are the actual strings (not ``ai/ml`` / ``crypto/finance``)
# — pay.sh uses snake_case and a single ``finance`` bucket.
_NEOBANK_RELEVANT_CATEGORIES: frozenset[str] = frozenset(
    {"messaging", "ai_ml", "data", "search", "finance"}
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

SOURCE_NAME: str = "paysh_manifest"

# Verticals where pay.sh discovery is reliably useful. ``fintech`` is
# intentionally outside the canonical ``Vertical`` Literal — the
# classifier sometimes emits it as a string for legacy ideas; the
# membership check is a plain string-set intersection so both shapes
# work without a coercion layer.
_VERTICAL_FIRES_FOR: frozenset[str] = frozenset({"neobank", "dex", "marketplace", "fintech"})

# Idea-text signals that flag an x402-adjacent topic. Lowercased
# substring match. Conservative — we don't want every "AI agent" idea
# pulling the manifest.
_PAYSH_SIGNALS: tuple[str, ...] = (
    "x402",
    "pay.sh",
    "paysh",
    "agent skill",
    "agent-skill",
    "paid api",
    "paid-api",
    "micropayment",
    "wallet-approved",
    "pay mcp",
    "pay-mcp",
    "402",
)


def _idea_has_paysh_signal(idea: str) -> bool:
    text = (idea or "").lower()
    return any(sig in text for sig in _PAYSH_SIGNALS)


# ---------------------------------------------------------------------------
# Manifest schema — pinned 2026-05-07 against the live endpoint.
# ---------------------------------------------------------------------------


class PayshSkillEntry(BaseModel):
    """One entry under ``skills[]`` in the pay.sh manifest.

    Permissive: ``name`` is the only field we strictly require (it's the
    URL anchor and the citation key). Other fields default to empty
    strings so a partial manifest still produces usable chunks.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str
    title: str = ""
    description: str = ""
    url: str = ""


class PayshManifest(BaseModel):
    """Top-level manifest shape served from
    ``https://pay.sh/.well-known/agent-skills/index.json``.

    Pinned shape (2026-05-07): ``version``, ``name``, ``description``,
    ``skills[]``. Unknown top-level keys are ignored so pay.sh can grow
    the manifest without breaking us.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    version: str = ""
    name: str = ""
    description: str = ""
    skills: list[PayshSkillEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SSRF guard.
# ---------------------------------------------------------------------------


def _is_paysh_url(url: str) -> bool:
    """True iff ``url`` is on the ``pay.sh`` host over HTTPS.

    Used as the SSRF guard. Conservative by design: we accept the apex
    only (no subdomains today). Subdomain support, if pay.sh ever needs
    it, ships as a deliberate change here so the threat model stays
    explicit.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    return parsed.hostname == PAYSH_HOST


# ---------------------------------------------------------------------------
# Entry → chunk normalization.
# ---------------------------------------------------------------------------


def _entry_to_chunk(entry: PayshSkillEntry) -> dict[str, Any] | None:
    """Render a manifest entry as a chunk-shaped dict for the dispatcher.

    Returns ``None`` for entries that lack any usable text (no title,
    no description) — those chunks would be unciteable. The caller
    filters Nones out and continues; we never raise on a single bad
    entry (per the per-entry resilience policy in the module docstring).

    ``provider_kind="free:paysh_manifest"`` mirrors the arxiv adapter's
    ``free:<src>`` convention. Citation reads as
    ``paysh_manifest:<name>``; the anchored URL stays in metadata for
    the renderer.
    """
    name = entry.name.strip()
    title = entry.title.strip()
    description = entry.description.strip()
    if not name:
        return None

    text_parts = [p for p in (title or name, description) if p]
    if not text_parts:
        return None
    text = ": ".join(text_parts) if len(text_parts) > 1 else text_parts[0]

    anchored_url = f"{PAYSH_MANIFEST_URL}#{name}"
    citation_id = f"paysh_manifest:{name}"

    return {
        "text": text,
        "provider_kind": "free:paysh_manifest",
        "cost_usd": "0",
        "metadata": {
            "citation_id": citation_id,
            "name": name,
            "title": title,
            "description": description,
            "skill_url": entry.url,
            "source_url": anchored_url,
        },
        "creator_handle": None,
    }


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def applies_to(*, categories: set[str], idea: str = "") -> bool:
    """Gating predicate for the dispatch graph (S22-N5 wires this up).

    Matches the ``Source`` Protocol shape so a future ``PayshManifestSource``
    class can delegate here. The dispatcher today calls ``applies_to``
    with ``categories`` only — ``idea`` is the optional extension that
    powers the keyword-signal fallback when the classifier returned ∅.
    """
    if categories & _VERTICAL_FIRES_FOR:
        return True
    return _idea_has_paysh_signal(idea)


async def fetch_manifest(
    client: httpx.AsyncClient | None = None,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    manifest_url: str = PAYSH_MANIFEST_URL,
) -> SourceResult:
    """Fetch the pay.sh agent-skills manifest and normalize into chunks.

    Returns a ``SourceResult`` with ``payload["chunks"]`` shaped like
    ``BazaarChunk`` dicts. Errors (timeout, non-200, redirect to a
    non-pay.sh host, unparsable JSON, schema mismatch) surface as
    ``fired=False`` with a structured ``error`` string — never raised —
    so a flaky pay.sh deploy doesn't take down a dispatch.

    Per-entry validation failures are logged at WARN and skipped; a
    single malformed skill never poisons the whole batch.
    """
    if not _is_paysh_url(manifest_url):
        return SourceResult(
            source_name=SOURCE_NAME,
            payload={"chunks": []},
            fired=False,
            error=f"paysh_manifest: SSRF guard rejected url host={urlparse(manifest_url).hostname!r}",
        )

    owns_local = False
    http = client
    if http is None:
        # follow_redirects=False is the SSRF guard's second leg — we
        # never want a 302 to a different host to slip past the
        # _is_paysh_url check above.
        http = httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
        owns_local = True

    try:
        try:
            resp = await http.get(manifest_url)
        except httpx.HTTPError as exc:
            return SourceResult(
                source_name=SOURCE_NAME,
                payload={"chunks": []},
                fired=False,
                error=f"paysh_manifest: http error: {type(exc).__name__}: {exc}",
            )

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            if not _is_paysh_url(location):
                return SourceResult(
                    source_name=SOURCE_NAME,
                    payload={"chunks": []},
                    fired=False,
                    error=(
                        "paysh_manifest: SSRF guard rejected redirect "
                        f"host={urlparse(location).hostname!r}"
                    ),
                )

        if resp.status_code >= 400:
            return SourceResult(
                source_name=SOURCE_NAME,
                payload={"chunks": []},
                fired=False,
                error=f"paysh_manifest: HTTP {resp.status_code}",
            )

        try:
            raw = resp.json()
        except ValueError as exc:
            return SourceResult(
                source_name=SOURCE_NAME,
                payload={"chunks": []},
                fired=False,
                error=f"paysh_manifest: non-json body: {exc}",
            )

        try:
            manifest = PayshManifest.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                "paysh_manifest.schema.invalid url=%s errors=%s",
                manifest_url,
                exc.errors()[:3],
            )
            return SourceResult(
                source_name=SOURCE_NAME,
                payload={"chunks": []},
                fired=False,
                error=f"paysh_manifest: schema validation failed: {exc.error_count()} errors",
            )

        chunks: list[dict[str, Any]] = []
        skipped = 0
        for entry in manifest.skills[:max_entries]:
            chunk = _entry_to_chunk(entry)
            if chunk is None:
                skipped += 1
                logger.warning(
                    "paysh_manifest.entry.skipped name=%r reason=empty_text",
                    entry.name,
                )
                continue
            chunks.append(chunk)

        logger.info(
            "paysh_manifest.fetch.success url=%s skill_count=%d chunk_count=%d skipped=%d",
            manifest_url,
            len(manifest.skills),
            len(chunks),
            skipped,
        )

        return SourceResult(
            source_name=SOURCE_NAME,
            payload={
                "chunks": chunks,
                "manifest_version": manifest.version,
                "skill_count": len(manifest.skills),
            },
            cost_usd=0.0,
            fired=True,
        )
    finally:
        if owns_local:
            await http.aclose()


# ---------------------------------------------------------------------------
# Catalog schema — pinned 2026-05-07 against ``/api/catalog``.
# ---------------------------------------------------------------------------


class PayshCatalogProvider(BaseModel):
    """One entry under ``providers[]`` in the pay.sh catalog.

    Permissive on optional fields. ``fqn`` is the only strict
    requirement — it's the URL anchor / citation key. Everything else
    defaults so a partial provider record still produces a usable chunk.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    fqn: str
    title: str = ""
    description: str = ""
    use_case: str = ""
    category: str = ""
    service_url: str = ""
    endpoint_count: int = 0
    has_metering: bool = False
    has_free_tier: bool = False
    min_price_usd: float = 0.0
    max_price_usd: float = 0.0
    sha: str = ""


class PayshCatalog(BaseModel):
    """Top-level shape served from ``https://pay.sh/api/catalog``.

    Pinned 2026-05-07: ``version`` (int), ``generated_at``, ``base_url``,
    ``provider_count``, ``providers[]``. Unknown top-level keys are
    ignored so pay.sh can grow the response without breaking us.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    version: int = 0
    generated_at: str = ""
    base_url: str = ""
    provider_count: int = 0
    providers: list[PayshCatalogProvider] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Catalog → chunk normalization.
# ---------------------------------------------------------------------------


def _format_price_band(provider: PayshCatalogProvider) -> str:
    """Pure helper: render a one-sentence price-band suffix.

    Examples::

        "Pricing: $0.001 – $1.00 across 141 endpoints, free-tier available."
        "Pricing: $0.01 across 105 endpoints."
        "Pricing: free across 16 endpoints, free-tier available."

    Stays a pure function so the unit test stays light per
    ``feedback_lighter_tests.md``.
    """
    lo = provider.min_price_usd
    hi = provider.max_price_usd
    if lo == 0 and hi == 0:
        price_part = "free"
    elif lo == hi:
        price_part = f"${lo:g}"
    else:
        price_part = f"${lo:g} – ${hi:g}"  # noqa: RUF001 (intentional en dash)

    endpoints = provider.endpoint_count
    endpoint_part = f"across {endpoints} endpoint{'s' if endpoints != 1 else ''}"
    suffix = ", free-tier available" if provider.has_free_tier else ""
    return f"Pricing: {price_part} {endpoint_part}{suffix}."


def _provider_to_chunk(provider: PayshCatalogProvider) -> dict[str, Any] | None:
    """Render one catalog provider as a chunk-shaped dict.

    Returns ``None`` for an entry with no ``fqn`` (uncitable). The
    ``source`` literal is the same ``paysh_manifest`` string used by the
    well-known fetcher — semantic re-meaning per Pattern A discipline; we
    do not introduce a parallel ``KnowledgeSource`` literal.
    """
    fqn = provider.fqn.strip()
    if not fqn:
        return None

    title = provider.title.strip() or fqn
    description = provider.description.strip()
    use_case = provider.use_case.strip()
    category = provider.category.strip()

    body_parts: list[str] = [title]
    if category:
        body_parts.append(f"[{category}]")
    if description:
        body_parts.append(description)
    if use_case:
        body_parts.append(use_case)
    body = " ".join(body_parts)
    text = f"{body} {_format_price_band(provider)}".strip()

    anchored_url = f"{PAYSH_CATALOG_URL}#{fqn}"
    citation_id = f"paysh_manifest:{fqn}"

    return {
        "text": text,
        "provider_kind": "free:paysh_manifest",
        "cost_usd": "0",
        "metadata": {
            "citation_id": citation_id,
            "fqn": fqn,
            "title": title,
            "category": category,
            "service_url": provider.service_url,
            "endpoint_count": provider.endpoint_count,
            "has_metering": provider.has_metering,
            "has_free_tier": provider.has_free_tier,
            "min_price_usd": provider.min_price_usd,
            "max_price_usd": provider.max_price_usd,
            "source_url": anchored_url,
        },
        "creator_handle": None,
    }


# ---------------------------------------------------------------------------
# Filter helper for downstream selection (N4 will consume this).
# ---------------------------------------------------------------------------


def filter_neobank_relevant(
    providers: list[PayshCatalogProvider],
) -> list[PayshCatalogProvider]:
    """Pure selector for neobank-relevant catalog providers.

    Two-stage filter:
      1. ``category`` is in the neobank-relevant set
         (``messaging`` / ``ai_ml`` / ``data`` / ``search`` / ``finance``).
      2. ``description`` or ``use_case`` contains at least one of the
         neobank keywords (kyc, ocr, identity, verification, market,
         price, compliance, treasury, balance, rpc).

    Lives in the data layer per the lane boundary — N4 picks 3-5 targets
    from this filtered slice rather than re-implementing the selection
    logic itself.
    """
    out: list[PayshCatalogProvider] = []
    for p in providers:
        if p.category not in _NEOBANK_RELEVANT_CATEGORIES:
            continue
        haystack = f"{p.description}\n{p.use_case}".lower()
        if any(kw in haystack for kw in _NEOBANK_RELEVANT_KEYWORDS):
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Catalog fetcher — same shape as ``fetch_manifest``.
# ---------------------------------------------------------------------------


async def fetch_catalog(
    client: httpx.AsyncClient | None = None,
    *,
    max_entries: int = DEFAULT_CATALOG_MAX_ENTRIES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    catalog_url: str = PAYSH_CATALOG_URL,
) -> SourceResult:
    """Fetch the pay.sh provider catalog and normalize into chunks.

    Returns a ``SourceResult`` with ``payload["chunks"]`` shaped like
    ``BazaarChunk`` dicts — one chunk per provider. Same error-handling
    contract as ``fetch_manifest``: HTTP / redirect / JSON / schema
    failures become ``fired=False`` results, never raised.

    The catalog is the *real* discovery payload (72 paid x402 services
    today vs. 5 meta-skills in the well-known manifest); both fetchers
    share the ``paysh_manifest`` source literal so downstream renderers
    don't fork.
    """
    if not _is_paysh_url(catalog_url):
        return SourceResult(
            source_name=SOURCE_NAME,
            payload={"chunks": []},
            fired=False,
            error=f"paysh_manifest: SSRF guard rejected url host={urlparse(catalog_url).hostname!r}",
        )

    owns_local = False
    http = client
    if http is None:
        http = httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
        owns_local = True

    try:
        try:
            resp = await http.get(catalog_url)
        except httpx.HTTPError as exc:
            return SourceResult(
                source_name=SOURCE_NAME,
                payload={"chunks": []},
                fired=False,
                error=f"paysh_manifest: http error: {type(exc).__name__}: {exc}",
            )

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            if not _is_paysh_url(location):
                return SourceResult(
                    source_name=SOURCE_NAME,
                    payload={"chunks": []},
                    fired=False,
                    error=(
                        "paysh_manifest: SSRF guard rejected redirect "
                        f"host={urlparse(location).hostname!r}"
                    ),
                )

        if resp.status_code >= 400:
            return SourceResult(
                source_name=SOURCE_NAME,
                payload={"chunks": []},
                fired=False,
                error=f"paysh_manifest: HTTP {resp.status_code}",
            )

        try:
            raw = resp.json()
        except ValueError as exc:
            return SourceResult(
                source_name=SOURCE_NAME,
                payload={"chunks": []},
                fired=False,
                error=f"paysh_manifest: non-json body: {exc}",
            )

        try:
            catalog = PayshCatalog.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                "paysh_manifest.catalog.schema.invalid url=%s errors=%s",
                catalog_url,
                exc.errors()[:3],
            )
            return SourceResult(
                source_name=SOURCE_NAME,
                payload={"chunks": []},
                fired=False,
                error=f"paysh_manifest: schema validation failed: {exc.error_count()} errors",
            )

        chunks: list[dict[str, Any]] = []
        skipped = 0
        for provider in catalog.providers[:max_entries]:
            chunk = _provider_to_chunk(provider)
            if chunk is None:
                skipped += 1
                logger.warning(
                    "paysh_manifest.provider.skipped fqn=%r reason=empty_fqn",
                    provider.fqn,
                )
                continue
            chunks.append(chunk)

        logger.info(
            "paysh_manifest.catalog.fetch.success url=%s provider_count=%d chunk_count=%d skipped=%d",
            catalog_url,
            len(catalog.providers),
            len(chunks),
            skipped,
        )

        return SourceResult(
            source_name=SOURCE_NAME,
            payload={
                "chunks": chunks,
                "catalog_version": catalog.version,
                "provider_count": catalog.provider_count,
                "generated_at": catalog.generated_at,
            },
            cost_usd=0.0,
            fired=True,
        )
    finally:
        if owns_local:
            await http.aclose()


__all__ = [
    "DEFAULT_CATALOG_MAX_ENTRIES",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TIMEOUT_SECONDS",
    "PAYSH_CATALOG_URL",
    "PAYSH_HOST",
    "PAYSH_MANIFEST_URL",
    "SOURCE_NAME",
    "PayshCatalog",
    "PayshCatalogProvider",
    "PayshManifest",
    "PayshSkillEntry",
    "_entry_to_chunk",
    "_format_price_band",
    "_idea_has_paysh_signal",
    "_is_paysh_url",
    "_provider_to_chunk",
    "applies_to",
    "fetch_catalog",
    "fetch_manifest",
    "filter_neobank_relevant",
]
