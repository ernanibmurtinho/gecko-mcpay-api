"""S16-INTEGRATE-01 — research dispatch carries Bazaar + twit.sh.

Regression for the 2026-05-01 stub-smoke gap: Sprint 14 + Sprint 16
landed providers that were never invoked by `bb research`. This test
exercises the basic-tier dispatch in stub mode and asserts both
provider attributions show up — `bazaar:*` chunks AND `twit_sh`
tweets. If this fails, the wedge is off the hot path again.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

import pytest
from gecko_core.payments.bazaar_discovery import (
    BazaarResource,
    PaymentRequirements,
)
from gecko_core.payments.x402_consumer import StubX402Consumer
from gecko_core.sources import dispatch_sources
from gecko_core.sources.bazaar.provider import BazaarSourceProvider
from gecko_core.sources.twit_sh import TwitshSource


class _FakeDiscovery:
    """Stub-mode discovery that returns a single priced Base USDC resource.

    Avoids the live agentic.market fixture's SSRF DNS resolution by
    pointing at a domain we never actually fetch (the stub adapter
    short-circuits on consumer.mode=='stub').
    """

    name = "fake-discovery"
    mode: Literal["stub", "live"] = "stub"

    def __init__(self, resource: BazaarResource) -> None:
        self._resource = resource

    async def list_resources(
        self,
        *,
        network: str | None = None,
        asset: str | None = None,
        max_usd_price: Decimal | None = None,
        limit: int = 20,
    ) -> list[BazaarResource]:
        return [self._resource]

    async def search(
        self,
        query: str,
        *,
        network: str | None = None,
        asset: str | None = None,
        max_usd_price: Decimal | None = None,
        limit: int = 20,
    ) -> list[BazaarResource]:
        return [self._resource]


def _priced_resource() -> BazaarResource:
    req = PaymentRequirements(
        network="Base",
        asset="USDC",
        pay_to="0xstubpaytotest",
        amount="10000",
        max_amount_required=Decimal("0.01"),
    )
    return BazaarResource(
        resource_url="https://example.com/x402/stub-resource",
        resource_type="http",
        x402_version=2,
        accepts=[req],
        metadata={
            "score": 1.0,
            "description": "Stub Bazaar resource for the dispatch regression test.",
            "category": "Data",
            "domain": "example.com",
        },
        source_directory="stub",
    )


@pytest.mark.asyncio
async def test_research_dispatch_fires_bazaar_and_twitsh_in_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both providers must produce a chunk when research dispatches them."""
    # Force stub mode regardless of the developer's local .env.
    monkeypatch.setenv("X402_MODE", "stub")

    bazaar = BazaarSourceProvider(
        discovery_client=_FakeDiscovery(_priced_resource()),
        x402_consumer=StubX402Consumer(),
        session_id=uuid4(),
    )
    twitsh = TwitshSource()

    results = await dispatch_sources(
        idea="Lisbon hotel for digital nomads",
        categories={"saas"},
        sources=[bazaar, twitsh],
        timeout_seconds=10.0,
    )

    bazaar_result = results.get("bazaar")
    twitsh_result = results.get("twit_sh")

    assert bazaar_result is not None, "bazaar provider missing from dispatch output"
    assert twitsh_result is not None, "twit_sh provider missing from dispatch output"

    # Bazaar fired with at least one chunk attributed to a `bazaar:*`
    # provider_kind and a non-zero stub spend.
    assert bazaar_result.fired, f"bazaar did not fire: {bazaar_result.error}"
    chunks = bazaar_result.payload.get("chunks") or []
    assert len(chunks) >= 1, "bazaar produced zero chunks in stub mode"
    kinds = {c["provider_kind"] for c in chunks}
    assert any(k.startswith("bazaar:") for k in kinds), f"unexpected provider_kinds: {kinds}"
    assert float(bazaar_result.cost_usd) > 0.0, "bazaar synthetic spend was zero"

    # twit.sh fired with at least one tweet and a non-zero stub spend.
    assert twitsh_result.fired, f"twit_sh did not fire: {twitsh_result.error}"
    tweets = twitsh_result.payload.get("tweets") or []
    assert len(tweets) >= 1, "twit_sh produced zero tweets in stub mode"
    assert float(twitsh_result.cost_usd) > 0.0, "twit_sh synthetic spend was zero"


@pytest.mark.asyncio
async def test_research_dispatch_attribution_carries_to_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator helper returns a one-liner with non-zero counts."""
    monkeypatch.setenv("X402_MODE", "stub")
    from unittest.mock import AsyncMock

    from gecko_core.sessions.store import SessionStore
    from gecko_core.workflows import _dispatch_stub_integration_providers

    sid = uuid4()
    store = AsyncMock(spec=SessionStore)
    # ``add_cost`` is called by the helper — make it a no-op coroutine.
    store.add_cost = AsyncMock(return_value=None)

    # Patch make_bazaar_provider so we don't need a SUPABASE-backed ledger
    # or live discovery client during the unit test.
    import gecko_core.sources.bazaar as bazaar_pkg

    real_make = bazaar_pkg.make_bazaar_provider

    def _fake_make(**kw: Any) -> BazaarSourceProvider:
        return BazaarSourceProvider(
            discovery_client=_FakeDiscovery(_priced_resource()),
            x402_consumer=StubX402Consumer(),
            session_id=kw.get("session_id"),
        )

    bazaar_pkg.make_bazaar_provider = _fake_make
    # The helper imports via ``from gecko_core.sources.bazaar import
    # make_bazaar_provider`` inside the function body, so the rebind on
    # the module surface is what gets resolved.
    try:
        summary = await _dispatch_stub_integration_providers(
            session_id=sid,
            idea="real-time options pricing for retail traders",
            store=store,
            payment_mode="stub",
        )
    finally:
        bazaar_pkg.make_bazaar_provider = real_make

    assert summary is not None
    assert "bazaar" in summary
    assert "twitsh" in summary
    # Both chunk counts should be > 0 in stub.
    assert "0 chunks" not in summary
    assert "0 tweets" not in summary

    # add_cost was called for both surfaces.
    kinds_called = [call.args[1] for call in store.add_cost.call_args_list]
    assert "twitsh" in kinds_called
    assert "v1_sources" in kinds_called  # bazaar rolled up under v1_sources
