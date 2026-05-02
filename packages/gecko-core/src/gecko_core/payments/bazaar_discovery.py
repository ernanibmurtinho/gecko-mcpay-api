"""Bazaar discovery — read-only catalog browse + search across x402 services.

S16-BAZAAR-DISCOVERY-01.

Two backends, dual-composed:

  * **Primary** ``AgenticMarketDiscoveryClient`` — ``api.agentic.market``
    surfaces a richer, human-readable catalog with named services,
    per-endpoint pricing, parameter schemas, etc. (see
    ``docs/research/agentic-market-skill-2026-05-01.md``). No auth.
  * **Fallback** ``CDPDiscoveryClient`` — ``api.cdp.coinbase.com`` surfaces
    the raw CDP-native discovery feed; lower-level, but its ``accepts[]``
    arrays already match the x402 spec wire shape.

This module is **read-only**. It MUST NOT import ``gecko_core.sources``
(the SourceProvider that consumes discovery lives one layer up) and it
MUST NOT initiate any payment (``X402Consumer`` lives in
``x402_consumer.py`` and is a separate seam).

Field-name normalization happens at the boundary so the rest of the
codebase never sees ``pricing.amount`` (agentic.market USDC float-string)
vs ``accepts[].amount`` (CDP atomic-units string) divergence. If either
upstream schema drifts, fix the parser here, not at the call site.

Schema notes (verified 2026-05-01 against live endpoints):

  * agentic.market actually serves at ``api.agentic.market`` (the
    ``agentic.market/v1/services`` URL in the public skill doc 404s in
    the browser host; the API host is the ``api.`` subdomain). One
    "service" entry exposes one or more ``endpoints[]``; each endpoint
    has its own pricing. We flatten — one ``BazaarResource`` per
    endpoint — so a buyer can target a specific URL.
  * CDP discovery sits at ``/platform/v2/x402/discovery/{resources,search}``
    (note the ``/platform/`` prefix — bare ``/v2/...`` 404s).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal, Protocol, runtime_checkable

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENTIC_MARKET_BASE_URL: Final[str] = "https://api.agentic.market"
CDP_DISCOVERY_BASE_URL: Final[str] = "https://api.cdp.coinbase.com/platform/v2/x402/discovery"

DEFAULT_HTTP_TIMEOUT_S: Final[float] = 8.0
DEFAULT_CACHE_TTL_S: Final[float] = 300.0  # 5 min, per ticket

# Source directory the resource was discovered from. ``stub`` retained
# (in addition to ticket-spec'd "agentic.market" | "cdp") so fixture-only
# stub-mode resources are tagged distinctly when synthesized without an
# upstream source.
SourceDirectory = Literal["agentic.market", "cdp", "stub"]
DiscoveryMode = Literal["stub", "live"]


# ---------------------------------------------------------------------------
# Domain model — one normalized shape for both backends.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaymentRequirements:
    """One element of an x402 ``accepts[]`` list.

    Wire shape mirrors the x402 spec (one accept entry is called
    ``PaymentRequirements`` in upstream ``x402.schemas``; we keep the
    same name internally). ``amount`` is an atomic-units string per
    spec — USDC has 6 decimals on Base / Solana. ``max_amount_required``
    (USD Decimal) is a derived convenience for budget caps; the source
    of truth on the wire stays ``amount``.

    For agentic.market, where pricing is published as a
    USDC-denominated string ("0.01"), we set ``amount`` to the atomic
    representation ("10000") and stash the raw upstream payload on
    ``raw`` for forensic round-tripping.
    """

    network: str
    asset: str
    pay_to: str
    amount: str  # atomic units, string per x402 spec
    scheme: str = "exact"
    max_timeout_seconds: int = 60
    max_amount_required: Decimal | None = None  # USD-denominated, derived
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BazaarResource:
    """A single discoverable x402 endpoint, normalized across backends.

    Shape per S16-BAZAAR-DISCOVERY-01 ticket:
    ``resource_url, resource_type, x402_version, accepts, last_updated,
    metadata, source_directory``.
    """

    resource_url: str
    resource_type: str  # "http" for CDP; same for normalized agentic.market
    x402_version: int
    accepts: list[PaymentRequirements]
    last_updated: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    source_directory: SourceDirectory = "stub"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class BazaarDiscoveryClient(Protocol):
    """Read-only browse + search over x402-enabled service catalogs.

    Mirrors the seller-side ``X402Client`` Protocol style: narrow,
    method-first, two class attrs (``name``, ``mode``) so consumers can
    dispatch / log without importing the concrete classes.
    """

    name: str
    mode: DiscoveryMode

    async def list_resources(
        self,
        *,
        network: str | None = None,
        asset: str | None = None,
        max_usd_price: Decimal | None = None,
        limit: int = 20,
    ) -> list[BazaarResource]: ...

    async def search(
        self,
        query: str,
        *,
        network: str | None = None,
        asset: str | None = None,
        max_usd_price: Decimal | None = None,
    ) -> list[BazaarResource]: ...


# ---------------------------------------------------------------------------
# TTL cache (tiny, in-process)
# ---------------------------------------------------------------------------


CacheKey = tuple[str, str | None, str | None, str | None, str | None, int | None]


class _TTLCache:
    """Per-process TTL cache keyed on (op, query, network, asset, max_usd, limit).

    Intentionally simple: no LRU, no single-flight. Sized for one
    orchestration session at a time; restart clears it.
    """

    def __init__(self, ttl_s: float = DEFAULT_CACHE_TTL_S) -> None:
        self._ttl = ttl_s
        self._store: dict[CacheKey, tuple[float, list[BazaarResource]]] = {}

    def get(self, key: CacheKey) -> list[BazaarResource] | None:
        hit = self._store.get(key)
        if hit is None:
            return None
        ts, value = hit
        if (time.monotonic() - ts) > self._ttl:
            del self._store[key]
            return None
        return value

    def put(self, key: CacheKey, value: list[BazaarResource]) -> None:
        self._store[key] = (time.monotonic(), value)


# ---------------------------------------------------------------------------
# Helpers — amount normalization + filtering
# ---------------------------------------------------------------------------


def _usd_from_amount_string(amount: str | None) -> Decimal | None:
    """Return a USD Decimal for an amount string assumed to be USDC.

    Heuristic: a ``.`` means USD-denominated already (agentic.market
    "0.01"); no ``.`` means atomic with 6 decimals (CDP "100000").
    """
    if amount is None or amount == "":
        return None
    s = amount.strip()
    if "." in s:
        try:
            return Decimal(s)
        except Exception:
            return None
    try:
        atomic = Decimal(s)
    except Exception:
        return None
    return atomic / Decimal(10) ** 6


def _amount_to_atomic_string(amount: str | None) -> str:
    """Normalize agentic.market's ``"0.01"`` → ``"10000"`` (USDC atomic)."""
    if amount is None or amount == "":
        return ""
    s = amount.strip()
    if "." not in s:
        return s
    try:
        return str(int((Decimal(s) * (Decimal(10) ** 6)).to_integral_value()))
    except Exception:
        return s


def _filter_resources(
    resources: list[BazaarResource],
    *,
    network: str | None,
    asset: str | None,
    max_usd_price: Decimal | None,
) -> list[BazaarResource]:
    """Apply network / asset / price filters at the accepts[] level.

    Drop resources whose accepts list becomes empty after filtering —
    they're un-payable under the caller's constraints.
    """
    out: list[BazaarResource] = []
    for r in resources:
        if not r.accepts:
            continue
        kept: list[PaymentRequirements] = []
        for a in r.accepts:
            if network is not None and network.lower() not in a.network.lower():
                continue
            if asset is not None and a.asset != asset:
                continue
            if (
                max_usd_price is not None
                and a.max_amount_required is not None
                and a.max_amount_required > max_usd_price
            ):
                continue
            kept.append(a)
        if kept:
            out.append(
                BazaarResource(
                    resource_url=r.resource_url,
                    resource_type=r.resource_type,
                    x402_version=r.x402_version,
                    accepts=kept,
                    last_updated=r.last_updated,
                    metadata=r.metadata,
                    source_directory=r.source_directory,
                )
            )
    return out


# ---------------------------------------------------------------------------
# agentic.market parser
# ---------------------------------------------------------------------------


def _parse_agentic_market_services(payload: dict[str, Any]) -> list[BazaarResource]:
    """Map ``api.agentic.market`` services-list shape → BazaarResource list.

    Schema (verified 2026-05-01, see fixture
    ``agentic_market_search_lisbon_hotel.json``):

    .. code-block:: json

      {"services": [
        {"id": ..., "name": ..., "category": ..., "domain": ...,
         "networks": ["Base", "Solana"],
         "endpoints": [
           {"url": ..., "method": "POST",
            "description": ...,
            "pricing": {"amount": "0.01", "currency": "USDC",
                        "network": "Base"},
            "providerName": ..., "parameters": [...]}
         ]}]}

    One service exposes multiple endpoints; we flatten so each endpoint
    becomes its own ``BazaarResource``. Service-level metadata (id,
    name, category, description) is stashed on ``metadata`` for
    downstream ranking.
    """
    out: list[BazaarResource] = []
    services = payload.get("services") or []
    for svc in services:
        svc_meta = {
            "service_id": svc.get("id"),
            "service_name": svc.get("name"),
            "category": svc.get("category"),
            "description": svc.get("description"),
            "domain": svc.get("domain"),
            "provider": svc.get("provider"),
            "service_networks": svc.get("networks") or [],
        }
        for ep in svc.get("endpoints") or []:
            url = ep.get("url")
            if not url:
                continue
            pricing = ep.get("pricing") or {}
            amount_str = pricing.get("amount")
            if not amount_str:
                # Free / unpriced endpoint — not actionable on the buyer
                # path. Skip; the SourceProvider can re-discover via a
                # separate "free Bazaar" route if needed.
                continue
            atomic = _amount_to_atomic_string(amount_str)
            usd = _usd_from_amount_string(amount_str)
            req = PaymentRequirements(
                network=pricing.get("network", ""),
                asset=pricing.get("currency", "USDC"),
                pay_to="",  # not exposed in agentic.market schema
                amount=atomic,
                max_amount_required=usd,
                description=ep.get("description", "") or "",
                extra={
                    "method": ep.get("method", "GET"),
                    "provider_name": ep.get("providerName", ""),
                    "parameters": ep.get("parameters") or [],
                },
                raw={"pricing": pricing, "endpoint": ep},
            )
            ep_meta = dict(svc_meta)
            ep_meta["endpoint_description"] = ep.get("description", "")
            ep_meta["method"] = ep.get("method", "GET")
            out.append(
                BazaarResource(
                    resource_url=url,
                    resource_type="http",
                    x402_version=2,
                    accepts=[req],
                    last_updated=None,
                    metadata=ep_meta,
                    source_directory="agentic.market",
                )
            )
    return out


# ---------------------------------------------------------------------------
# CDP parser
# ---------------------------------------------------------------------------


def _parse_cdp_accept(a: dict[str, Any]) -> PaymentRequirements:
    amount = a.get("amount", "")
    return PaymentRequirements(
        network=a.get("network", ""),
        asset=a.get("asset", ""),
        pay_to=a.get("payTo", ""),
        amount=str(amount) if amount is not None else "",
        scheme=a.get("scheme", "exact"),
        max_timeout_seconds=int(a.get("maxTimeoutSeconds", 60)),
        max_amount_required=_usd_from_amount_string(str(amount) if amount is not None else ""),
        extra=dict(a.get("extra") or {}),
        raw=dict(a),
    )


def _parse_cdp_resources(payload: dict[str, Any]) -> list[BazaarResource]:
    """Map CDP discovery shape (``items[]`` for /resources, ``resources[]``
    for /search) → BazaarResource."""
    rows = payload.get("items") or payload.get("resources") or []
    out: list[BazaarResource] = []
    for row in rows:
        url = row.get("resource") or row.get("resourceUrl") or row.get("url")
        if not url:
            continue
        accepts = [_parse_cdp_accept(a) for a in (row.get("accepts") or [])]
        out.append(
            BazaarResource(
                resource_url=url,
                resource_type=row.get("type", "http"),
                x402_version=int(row.get("x402Version", 2)),
                accepts=accepts,
                last_updated=row.get("lastUpdated"),
                metadata={
                    "description": row.get("description", ""),
                    "extensions": row.get("extensions") or {},
                    "quality": row.get("quality"),
                },
                source_directory="cdp",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Stub backend — fixtures
# ---------------------------------------------------------------------------


def _default_fixtures_dir() -> Path:
    """Repo-root /tests/payments/fixtures/bazaar_discovery."""
    here = Path(__file__).resolve()
    # …/packages/gecko-core/src/gecko_core/payments/bazaar_discovery.py
    # parents[5] == repo root
    return here.parents[5] / "tests" / "payments" / "fixtures" / "bazaar_discovery"


class StubDiscoveryClient:
    """Reads recorded fixtures off disk; no network.

    Default for tests + dev. Mirrors live filtering / cache semantics
    so a stub-only test isn't lying about the live shape (Pattern C
    spirit — the *parser* is identical between stub and live; only the
    bytes source differs).
    """

    name = "stub-discovery"
    mode: DiscoveryMode = "stub"

    def __init__(
        self,
        *,
        fixtures_dir: Path | str | None = None,
        ttl_s: float = DEFAULT_CACHE_TTL_S,
    ) -> None:
        self._fixtures_dir = Path(fixtures_dir) if fixtures_dir else _default_fixtures_dir()
        self._cache = _TTLCache(ttl_s=ttl_s)

    def _load(self, name: str) -> dict[str, Any]:
        p = self._fixtures_dir / name
        if not p.exists():
            return {}
        loaded: dict[str, Any] = json.loads(p.read_text())
        return loaded

    async def list_resources(
        self,
        *,
        network: str | None = None,
        asset: str | None = None,
        max_usd_price: Decimal | None = None,
        limit: int = 20,
    ) -> list[BazaarResource]:
        cdp = _parse_cdp_resources(self._load("cdp_resources_sample.json"))
        return _filter_resources(
            cdp[:limit],
            network=network,
            asset=asset,
            max_usd_price=max_usd_price,
        )

    async def search(
        self,
        query: str,
        *,
        network: str | None = None,
        asset: str | None = None,
        max_usd_price: Decimal | None = None,
    ) -> list[BazaarResource]:
        am = _parse_agentic_market_services(self._load("agentic_market_search_lisbon_hotel.json"))
        return _filter_resources(am, network=network, asset=asset, max_usd_price=max_usd_price)


# ---------------------------------------------------------------------------
# agentic.market live client
# ---------------------------------------------------------------------------


class AgenticMarketDiscoveryClient:
    """Primary live backend — ``api.agentic.market``."""

    name = "agentic-market"
    mode: DiscoveryMode = "live"

    def __init__(
        self,
        *,
        base_url: str = AGENTIC_MARKET_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        ttl_s: float = DEFAULT_CACHE_TTL_S,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout_s = timeout_s
        self._cache = _TTLCache(ttl_s=ttl_s)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._base_url}{path}"
        if self._client is not None:
            r = await self._client.get(url, params=params, timeout=self._timeout_s)
        else:
            async with httpx.AsyncClient(timeout=self._timeout_s) as c:
                r = await c.get(url, params=params)
        r.raise_for_status()
        return r.json()

    async def list_resources(
        self,
        *,
        network: str | None = None,
        asset: str | None = None,
        max_usd_price: Decimal | None = None,
        limit: int = 20,
    ) -> list[BazaarResource]:
        key: CacheKey = (
            "list",
            None,
            network,
            asset,
            str(max_usd_price) if max_usd_price else None,
            limit,
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        payload = await self._get("/v1/services")
        resources = _parse_agentic_market_services(payload)
        filtered = _filter_resources(
            resources[:limit],
            network=network,
            asset=asset,
            max_usd_price=max_usd_price,
        )
        self._cache.put(key, filtered)
        return filtered

    async def search(
        self,
        query: str,
        *,
        network: str | None = None,
        asset: str | None = None,
        max_usd_price: Decimal | None = None,
    ) -> list[BazaarResource]:
        key: CacheKey = (
            "search",
            query,
            network,
            asset,
            str(max_usd_price) if max_usd_price else None,
            None,
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        payload = await self._get("/v1/services/search", params={"q": query})
        resources = _parse_agentic_market_services(payload)
        filtered = _filter_resources(
            resources, network=network, asset=asset, max_usd_price=max_usd_price
        )
        self._cache.put(key, filtered)
        return filtered


# ---------------------------------------------------------------------------
# CDP live client
# ---------------------------------------------------------------------------


class CDPDiscoveryClient:
    """Fallback live backend — ``api.cdp.coinbase.com``."""

    name = "cdp-discovery"
    mode: DiscoveryMode = "live"

    def __init__(
        self,
        *,
        base_url: str = CDP_DISCOVERY_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        ttl_s: float = DEFAULT_CACHE_TTL_S,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout_s = timeout_s
        self._cache = _TTLCache(ttl_s=ttl_s)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._base_url}{path}"
        if self._client is not None:
            r = await self._client.get(url, params=params, timeout=self._timeout_s)
        else:
            async with httpx.AsyncClient(timeout=self._timeout_s) as c:
                r = await c.get(url, params=params)
        r.raise_for_status()
        return r.json()

    async def list_resources(
        self,
        *,
        network: str | None = None,
        asset: str | None = None,
        max_usd_price: Decimal | None = None,
        limit: int = 20,
    ) -> list[BazaarResource]:
        key: CacheKey = (
            "list",
            None,
            network,
            asset,
            str(max_usd_price) if max_usd_price else None,
            limit,
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        payload = await self._get("/resources")
        resources = _parse_cdp_resources(payload)
        filtered = _filter_resources(
            resources[:limit],
            network=network,
            asset=asset,
            max_usd_price=max_usd_price,
        )
        self._cache.put(key, filtered)
        return filtered

    async def search(
        self,
        query: str,
        *,
        network: str | None = None,
        asset: str | None = None,
        max_usd_price: Decimal | None = None,
    ) -> list[BazaarResource]:
        key: CacheKey = (
            "search",
            query,
            network,
            asset,
            str(max_usd_price) if max_usd_price else None,
            None,
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        payload = await self._get("/search", params={"q": query})
        resources = _parse_cdp_resources(payload)
        filtered = _filter_resources(
            resources, network=network, asset=asset, max_usd_price=max_usd_price
        )
        self._cache.put(key, filtered)
        return filtered


# ---------------------------------------------------------------------------
# Dual composition
# ---------------------------------------------------------------------------


_FALLBACK_TRIGGERS: Final[tuple[type[BaseException], ...]] = (
    httpx.HTTPError,  # covers timeouts, connect errors, HTTPStatusError
    asyncio.TimeoutError,
)


class DualDiscoveryClient:
    """Primary = agentic.market; on 5xx/timeout, fall back to CDP.

    Result merging: dedupe by ``resource_url`` (primary wins). Cache TTL
    applies to the *merged* result.
    """

    name = "dual-discovery"
    mode: DiscoveryMode = "live"

    def __init__(
        self,
        *,
        primary: BazaarDiscoveryClient | None = None,
        fallback: BazaarDiscoveryClient | None = None,
        ttl_s: float = DEFAULT_CACHE_TTL_S,
    ) -> None:
        self._primary: BazaarDiscoveryClient = primary or AgenticMarketDiscoveryClient(ttl_s=ttl_s)
        self._fallback: BazaarDiscoveryClient = fallback or CDPDiscoveryClient(ttl_s=ttl_s)
        self._cache = _TTLCache(ttl_s=ttl_s)

    @staticmethod
    def _merge(
        primary: list[BazaarResource], fallback: list[BazaarResource]
    ) -> list[BazaarResource]:
        seen: set[str] = {r.resource_url for r in primary}
        merged = list(primary)
        for r in fallback:
            if r.resource_url in seen:
                continue
            seen.add(r.resource_url)
            merged.append(r)
        return merged

    @staticmethod
    def _is_fallback_worthy(exc: BaseException) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code >= 500
        return isinstance(exc, _FALLBACK_TRIGGERS)

    async def list_resources(
        self,
        *,
        network: str | None = None,
        asset: str | None = None,
        max_usd_price: Decimal | None = None,
        limit: int = 20,
    ) -> list[BazaarResource]:
        key: CacheKey = (
            "list",
            None,
            network,
            asset,
            str(max_usd_price) if max_usd_price else None,
            limit,
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            primary = await self._primary.list_resources(
                network=network,
                asset=asset,
                max_usd_price=max_usd_price,
                limit=limit,
            )
        except Exception as exc:
            if not self._is_fallback_worthy(exc):
                raise
            primary = []
        fallback = await self._fallback.list_resources(
            network=network,
            asset=asset,
            max_usd_price=max_usd_price,
            limit=limit,
        )
        merged = self._merge(primary, fallback)
        self._cache.put(key, merged)
        return merged

    async def search(
        self,
        query: str,
        *,
        network: str | None = None,
        asset: str | None = None,
        max_usd_price: Decimal | None = None,
    ) -> list[BazaarResource]:
        key: CacheKey = (
            "search",
            query,
            network,
            asset,
            str(max_usd_price) if max_usd_price else None,
            None,
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            primary = await self._primary.search(
                query, network=network, asset=asset, max_usd_price=max_usd_price
            )
        except Exception as exc:
            if not self._is_fallback_worthy(exc):
                raise
            primary = []
        fallback = await self._fallback.search(
            query, network=network, asset=asset, max_usd_price=max_usd_price
        )
        merged = self._merge(primary, fallback)
        self._cache.put(key, merged)
        return merged


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def resolve_discovery_client(mode: DiscoveryMode = "stub") -> BazaarDiscoveryClient:
    """Resolve a discovery client by mode.

    ``stub`` — fixtures only, default.
    ``live`` — agentic.market primary + CDP fallback (DualDiscoveryClient).
    """
    if mode == "stub":
        return StubDiscoveryClient()
    if mode == "live":
        return DualDiscoveryClient()
    raise ValueError(f"unknown discovery mode: {mode!r}")


__all__ = [
    "AGENTIC_MARKET_BASE_URL",
    "CDP_DISCOVERY_BASE_URL",
    "AgenticMarketDiscoveryClient",
    "BazaarDiscoveryClient",
    "BazaarResource",
    "CDPDiscoveryClient",
    "DiscoveryMode",
    "DualDiscoveryClient",
    "PaymentRequirements",
    "SourceDirectory",
    "StubDiscoveryClient",
    "resolve_discovery_client",
]
