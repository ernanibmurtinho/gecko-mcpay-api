"""BazaarSourceProvider — catalog-led x402 source.

Conforms to the existing ``Source`` Protocol from ``gecko_core.sources``.
``applies_to`` is **always True**: the catalog is universal and per-query
relevance is decided at fetch time by ``BazaarDiscoveryClient.search``.

This provider is *composition*: discovery decides what to buy; an
adapter normalizes the response. The whole point of S16-BAZAAR-CONSUMER-03
is that **one generic adapter** consumes whatever the catalog returns.
Vendor shims under ``adapters/<name>.py`` are escape hatches.

Live-mode ``pay()`` is gated on S16-BAZAAR-CONSUMER-04 (recorded-fixture
contract test). Stub mode works end-to-end today.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from gecko_core.ingestion.web import UnsafeURLError, validate_url
from gecko_core.payments.bazaar_discovery import (
    BazaarDiscoveryClient,
    BazaarResource,
)
from gecko_core.payments.modes import ConsumerMode
from gecko_core.payments.spend_ledger import BazaarSpendLedger
from gecko_core.payments.x402_consumer import (
    BudgetExceededError,
    X402Consumer,
)
from gecko_core.sources import SourceResult
from gecko_core.sources.bazaar.adapters import BazaarAdapter
from gecko_core.sources.bazaar.adapters.generic import GenericBazaarAdapter
from gecko_core.sources.bazaar.types import BazaarChunk

logger = logging.getLogger(__name__)

DEFAULT_SESSION_USD_CAP = Decimal("0.50")
DEFAULT_DAILY_USD_CAP = Decimal("5.00")

# V1: pick top-K=1. S17 will fan out to K<=3.
_TOP_K = 1


def _resource_score(resource: BazaarResource) -> float:
    """Best-effort metadata score for ranking discovery results.

    agentic.market and CDP each surface a different rank field. We accept
    any of (``score``, ``composite_score``, ``quality``, ``rank``) and
    default to 0.
    """
    meta = resource.metadata or {}
    for key in ("score", "composite_score", "quality", "rank"):
        value = meta.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _min_advertised_usd(resource: BazaarResource) -> Decimal | None:
    """Cheapest USD-denominated price across this resource's accepts[].

    None if every entry's ``max_amount_required`` is None (price was not
    parseable as USD by the discovery boundary).
    """
    priced = [a.max_amount_required for a in resource.accepts if a.max_amount_required is not None]
    return min(priced) if priced else None


class BazaarSourceProvider:
    """Catalog-led Bazaar source. Conforms to ``Source`` Protocol."""

    name: str = "bazaar"

    def __init__(
        self,
        *,
        discovery_client: BazaarDiscoveryClient,
        x402_consumer: X402Consumer,
        session_cap_usd: Decimal = DEFAULT_SESSION_USD_CAP,
        daily_cap_usd: Decimal = DEFAULT_DAILY_USD_CAP,
        adapter_registry: list[BazaarAdapter] | None = None,
        spend_ledger: BazaarSpendLedger | None = None,
        session_id: UUID | None = None,
    ) -> None:
        self._discovery = discovery_client
        self._consumer = x402_consumer
        self._session_cap_usd = session_cap_usd
        # S16-BAZAAR-CONSUMER-02 — daily cap enforced via the ledger
        # below. ``spend_ledger=None`` is the unit-test path: skip both
        # the pre-flight gate and the post-pay record (still safe — the
        # per-session ``session_cap_usd`` filter remains in place).
        self._daily_cap_usd = daily_cap_usd
        self._ledger = spend_ledger
        # Provider-level session id. Synthesized when not provided so
        # ``record()`` always has a UUID to write — the gecko-core
        # research pipeline injects the real session id; tests + adhoc
        # ``bb research`` get a synthetic one for ledger forensics.
        self._session_id = session_id or uuid4()
        # Adapters tried in order; first ``applies_to`` wins. The generic
        # adapter is registered last as the universal fallback (catalog-led
        # default).
        self._adapters: list[BazaarAdapter] = list(adapter_registry or [])
        if not any(isinstance(a, GenericBazaarAdapter) for a in self._adapters):
            self._adapters.append(GenericBazaarAdapter())

    async def applies_to(self, *, categories: set[str]) -> bool:
        # Catalog is universal — relevance decided per-query at fetch time.
        return True

    def _resolve_adapter(self, resource: BazaarResource) -> BazaarAdapter:
        for adapter in self._adapters:
            if adapter.applies_to(resource):
                return adapter
        # Should be unreachable: GenericBazaarAdapter.applies_to() is True.
        raise RuntimeError("no adapter applies — GenericBazaarAdapter missing from registry")

    def _filter_and_rank(self, resources: list[BazaarResource]) -> list[BazaarResource]:
        """Drop SSRF-unsafe URLs + over-budget resources; rank by score desc."""
        kept: list[BazaarResource] = []
        for resource in resources:
            try:
                validate_url(resource.resource_url)
            except UnsafeURLError as exc:
                logger.warning(
                    "bazaar: skipping SSRF-unsafe resource %s: %s",
                    resource.resource_url,
                    exc,
                )
                continue
            min_price = _min_advertised_usd(resource)
            if min_price is None and resource.accepts:
                logger.info(
                    "bazaar: skipping unpriced resource %s (no parseable USD on accepts[])",
                    resource.resource_url,
                )
                continue
            if min_price is not None and min_price > self._session_cap_usd:
                logger.info(
                    "bazaar: skipping over-budget resource %s (min price $%s > cap $%s)",
                    resource.resource_url,
                    min_price,
                    self._session_cap_usd,
                )
                continue
            kept.append(resource)
        kept.sort(key=_resource_score, reverse=True)
        return kept

    async def fetch(self, *, idea: str, categories: set[str]) -> SourceResult:
        query = " ".join([idea, *sorted(categories)]).strip()

        try:
            candidates = await self._discovery.search(
                query,
                max_usd_price=self._session_cap_usd,
            )
        except Exception as exc:
            logger.warning("bazaar: discovery failed: %s", exc)
            return SourceResult(
                source_name=self.name,
                payload={},
                fired=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        ranked = self._filter_and_rank(candidates)
        if not ranked:
            return SourceResult(
                source_name=self.name,
                payload={
                    "chunks": [],
                    "degraded_sources": ["bazaar:no-eligible-candidates"],
                },
                fired=False,
                error="no eligible Bazaar candidates after SSRF + cap filter",
            )

        picks = ranked[:_TOP_K]
        all_chunks: list[BazaarChunk] = []
        total_cost = Decimal("0")
        degraded: list[str] = []

        for resource in picks:
            adapter = self._resolve_adapter(resource)
            # S16-BAZAAR-CONSUMER-02 — daily-cap pre-flight. Use the
            # cheapest advertised price as the upper bound on what the
            # adapter is about to spend (the adapter picks the cheapest
            # in-budget accepts[] entry; this matches that policy).
            est_spend = _min_advertised_usd(resource) or Decimal("0")
            if self._ledger is not None and await self._ledger.would_exceed_daily(
                est_spend, daily_cap_usd=self._daily_cap_usd
            ):
                logger.info(
                    "bazaar: daily cap $%s would be exceeded by $%s on %s — degrading source",
                    self._daily_cap_usd,
                    est_spend,
                    resource.resource_url,
                )
                degraded.append("daily_cap_exceeded")
                continue

            # Wrap the consumer so we can record immediately after a
            # successful settle without changing the adapter signature.
            recording_consumer = _LedgerRecordingConsumer(
                inner=self._consumer,
                ledger=self._ledger,
                session_id=self._session_id,
                resource_url=resource.resource_url,
            )
            try:
                chunks = await adapter.fetch_and_normalize(
                    resource,
                    recording_consumer,
                    max_usd=self._session_cap_usd,
                )
            except BudgetExceededError as exc:
                logger.info("bazaar: budget exceeded for %s: %s", resource.resource_url, exc)
                degraded.append(f"bazaar:budget:{resource.resource_url}")
                continue
            except Exception as exc:
                logger.warning(
                    "bazaar: adapter %s failed on %s: %s",
                    adapter.name,
                    resource.resource_url,
                    exc,
                )
                degraded.append(f"bazaar:adapter-error:{resource.resource_url}")
                continue
            all_chunks.extend(chunks)
            for chunk in chunks:
                total_cost += chunk.cost_usd

        if not all_chunks:
            return SourceResult(
                source_name=self.name,
                payload={"chunks": [], "degraded_sources": degraded or ["bazaar:no-chunks"]},
                fired=False,
                error="no chunks produced from Bazaar resources",
            )

        payload: dict[str, Any] = {
            "chunks": [_chunk_to_dict(c) for c in all_chunks],
            "picked_resources": [r.resource_url for r in picks],
            "degraded_sources": degraded,
        }
        return SourceResult(
            source_name=self.name,
            payload=payload,
            cost_usd=float(total_cost),
        )


class _LedgerRecordingConsumer:
    """X402Consumer proxy: forwards ``pay()`` then records to the ledger.

    Lives here (not in spend_ledger.py) because it's purely a
    composition shim used by the provider — keeping it private to the
    provider module avoids leaking ledger plumbing into the buyer
    Protocol. ``ledger=None`` makes it transparent for the unit-test
    path.
    """

    def __init__(
        self,
        *,
        inner: X402Consumer,
        ledger: BazaarSpendLedger | None,
        session_id: UUID,
        resource_url: str,
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._session_id = session_id
        self._resource_url = resource_url
        # Settable attrs (not properties) so the Protocol shape on
        # ``X402Consumer`` is satisfied — Protocol members declared with
        # bare type annotations require settable attributes.
        self.name: str = inner.name
        self.mode: ConsumerMode = inner.mode

    async def pay(self, requirements: Any, *, max_usd: Decimal) -> Any:
        receipt = await self._inner.pay(requirements, max_usd=max_usd)
        if self._ledger is not None:
            amount = requirements.max_amount_required or Decimal("0")
            # ``record`` swallows write errors — chain already settled.
            await self._ledger.record(
                session_id=self._session_id,
                resource_url=self._resource_url,
                amount_usd=amount,
                receipt=receipt,
                mode=self._inner.mode,
            )
        return receipt


def _chunk_to_dict(chunk: BazaarChunk) -> dict[str, Any]:
    return {
        "text": chunk.text,
        "provider_kind": chunk.provider_kind,
        "cost_usd": str(chunk.cost_usd),
        "metadata": chunk.metadata,
        "creator_handle": chunk.creator_handle,
    }


def make_bazaar_provider(
    *,
    discovery_client: BazaarDiscoveryClient | None = None,
    x402_consumer: X402Consumer | None = None,
    spend_ledger: BazaarSpendLedger | None = None,
    session_id: UUID | None = None,
) -> BazaarSourceProvider:
    """Factory: read env caps, resolve discovery + consumer, wire adapters.

    Resolves concrete clients via web3-eng's ``resolve_*`` factories when
    they land (S16-BAZAAR-DISCOVERY-01 / S16-BAZAAR-CONSUMER-01). Until
    then the caller must inject — the production callsite is gated on
    those tickets, so this scaffold accepts injection for tests now and
    will read ``X402_CONSUMER_MODE`` once the resolver lands.
    """
    session_cap = Decimal(os.getenv("GECKO_BAZAAR_SESSION_USD_CAP", str(DEFAULT_SESSION_USD_CAP)))
    daily_cap = Decimal(os.getenv("GECKO_BAZAAR_DAILY_USD_CAP", str(DEFAULT_DAILY_USD_CAP)))

    if x402_consumer is None:
        # web3-eng's S16-BAZAAR-CONSUMER-01 landed `resolve_consumer_client`;
        # default to stub mode unless caller overrode `X402_CONSUMER_MODE`.
        from gecko_core.payments.x402_consumer import resolve_consumer_client

        # Cast string env value to ConsumerMode literal at the boundary; the
        # resolver validates and raises ValueError on an unknown mode.
        mode_str = os.getenv("X402_CONSUMER_MODE", "stub")
        x402_consumer = resolve_consumer_client(mode_str)  # type: ignore[arg-type]

    if discovery_client is None:
        from gecko_core.payments.bazaar_discovery import resolve_discovery_client

        disc_mode = os.getenv("GECKO_BAZAAR_DISCOVERY_MODE", "stub")
        discovery_client = resolve_discovery_client(disc_mode)  # type: ignore[arg-type]

    # S16-BAZAAR-CONSUMER-02 — when no ledger is injected, build one from
    # env iff Supabase is configured. Stub-mode dev with no SUPABASE_URL
    # set keeps the legacy pass-through (ledger=None) behaviour so
    # local-only smoke tests don't require a database.
    if (
        spend_ledger is None
        and os.getenv("SUPABASE_URL")
        and os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    ):
        try:
            spend_ledger = BazaarSpendLedger.from_env()
        except Exception as exc:
            logger.warning("bazaar: ledger from_env failed (%s); proceeding without", exc)
            spend_ledger = None

    return BazaarSourceProvider(
        discovery_client=discovery_client,
        x402_consumer=x402_consumer,
        session_cap_usd=session_cap,
        daily_cap_usd=daily_cap,
        adapter_registry=[GenericBazaarAdapter()],
        spend_ledger=spend_ledger,
        session_id=session_id,
    )


__all__ = [
    "DEFAULT_DAILY_USD_CAP",
    "DEFAULT_SESSION_USD_CAP",
    "BazaarChunk",
    "BazaarSourceProvider",
    "make_bazaar_provider",
]
