"""S17-DISCOVERY-01 — provider rebalance dispatch tests.

Asserts that `_dispatch_stub_integration_providers` (the workflows
seam) now fans out to Bazaar + twit.sh + Arxiv when the idea matches
research-adjacent signals, and that the per-provider quota envs are
read at dispatch time.

We don't run the actual providers — they're each covered in their own
contract tests. Here we patch them at the import boundary used by the
dispatch helper and assert the wiring + summary line.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from gecko_core.sources import SourceResult
from gecko_core.workflows import _dispatch_stub_integration_providers, _quota


def test_quota_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GECKO_PROVIDER_QUOTA_ARXIV", "7")
    assert _quota("GECKO_PROVIDER_QUOTA_ARXIV", 5) == 7


def test_quota_falls_back_on_bad_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GECKO_PROVIDER_QUOTA_ARXIV", "not-a-number")
    assert _quota("GECKO_PROVIDER_QUOTA_ARXIV", 5) == 5


def test_quota_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_PROVIDER_QUOTA_ARXIV", raising=False)
    assert _quota("GECKO_PROVIDER_QUOTA_ARXIV", 5) == 5


class _FakeProvider:
    """Stand-in for any of bazaar / twit.sh / arxiv during dispatch.

    Returns a deterministic ``SourceResult`` whose payload shape mirrors
    what the real provider would surface, so the helper's
    payload-extraction path runs against the right keys.
    """

    def __init__(
        self,
        name: str,
        *,
        chunks_key: str,
        chunk_count: int,
        cost_usd: float,
        applies: bool = True,
    ) -> None:
        self.name = name
        self._chunks_key = chunks_key
        self._chunk_count = chunk_count
        self._cost = cost_usd
        self._applies = applies

    async def applies_to(self, *, categories: set[str], idea: str = "") -> bool:
        return self._applies

    async def fetch(self, *, idea: str, categories: set[str]) -> SourceResult:
        return SourceResult(
            source_name=self.name,
            payload={self._chunks_key: [{"i": i} for i in range(self._chunk_count)]},
            cost_usd=self._cost,
            fired=True,
        )


@pytest.mark.asyncio
async def test_dispatch_includes_arxiv_for_technical_idea(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idea with x402/agentic signal → arxiv shows up in dispatch + summary."""
    monkeypatch.setenv("X402_MODE", "stub")

    # Patch make_bazaar_provider, TwitshSource, make_arxiv_source at
    # their import sites inside the helper. The helper uses
    # `from gecko_core.sources.bazaar import make_bazaar_provider` etc.,
    # so patching the parent module attributes is what gets resolved.
    import gecko_core.sources.arxiv as arxiv_mod
    import gecko_core.sources.bazaar as bazaar_mod
    import gecko_core.sources.twit_sh as twitsh_mod

    monkeypatch.setattr(
        bazaar_mod,
        "make_bazaar_provider",
        lambda **kw: _FakeProvider("bazaar", chunks_key="chunks", chunk_count=3, cost_usd=0.03),
    )
    monkeypatch.setattr(
        twitsh_mod,
        "TwitshSource",
        lambda *a, **kw: _FakeProvider(
            "twit_sh", chunks_key="tweets", chunk_count=5, cost_usd=0.05
        ),
    )
    monkeypatch.setattr(
        arxiv_mod,
        "make_arxiv_source",
        lambda **kw: _FakeProvider("arxiv", chunks_key="chunks", chunk_count=5, cost_usd=0.0),
    )

    store = AsyncMock()
    store.add_cost = AsyncMock(return_value=None)

    summary = await _dispatch_stub_integration_providers(
        session_id=uuid4(),
        idea="Gecko: makes judgment tradeable. agentic marketplace x402.",
        store=store,
        payment_mode="stub",
    )

    assert summary is not None
    # All three providers appear in the summary.
    assert "bazaar 3 chunks" in summary
    assert "twitsh 5 tweets" in summary
    assert "arxiv 5 abstracts" in summary

    # Costs were debited for paid surfaces only.
    kinds_called = [call.args[1] for call in store.add_cost.call_args_list]
    assert "twitsh" in kinds_called
    assert "v1_sources" in kinds_called  # bazaar rolled up under v1_sources
    # Arxiv is free — no add_cost call for it.


@pytest.mark.asyncio
async def test_dispatch_skips_arxiv_for_non_technical_idea(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off-topic ideas don't waste an Arxiv HTTP probe."""
    monkeypatch.setenv("X402_MODE", "stub")

    import gecko_core.sources.arxiv as arxiv_mod
    import gecko_core.sources.bazaar as bazaar_mod
    import gecko_core.sources.twit_sh as twitsh_mod

    arxiv_calls: list[Any] = []

    class _ArxivProbeRecorder(_FakeProvider):
        def __init__(self) -> None:
            super().__init__(
                "arxiv", chunks_key="chunks", chunk_count=0, cost_usd=0.0, applies=False
            )

        async def fetch(self, *, idea: str, categories: set[str]) -> SourceResult:
            arxiv_calls.append((idea, categories))
            return await super().fetch(idea=idea, categories=categories)

    monkeypatch.setattr(
        bazaar_mod,
        "make_bazaar_provider",
        lambda **kw: _FakeProvider("bazaar", chunks_key="chunks", chunk_count=1, cost_usd=0.01),
    )
    monkeypatch.setattr(
        twitsh_mod,
        "TwitshSource",
        lambda *a, **kw: _FakeProvider(
            "twit_sh", chunks_key="tweets", chunk_count=2, cost_usd=0.02
        ),
    )
    monkeypatch.setattr(arxiv_mod, "make_arxiv_source", lambda **kw: _ArxivProbeRecorder())

    store = AsyncMock()
    store.add_cost = AsyncMock(return_value=None)

    summary = await _dispatch_stub_integration_providers(
        session_id=uuid4(),
        idea="hotel booking app for nomads",
        store=store,
        payment_mode="stub",
    )

    assert summary is not None
    # Arxiv is gated out → applies_to returned False → fetch never ran.
    assert arxiv_calls == []
    # Summary still mentions arxiv, but with 0 abstracts.
    assert "arxiv 0 abstracts" in summary


@pytest.mark.asyncio
async def test_dispatch_skipped_in_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live mode short-circuits — no providers run from this seam."""
    monkeypatch.setenv("X402_MODE", "live")
    store = AsyncMock()
    summary = await _dispatch_stub_integration_providers(
        session_id=uuid4(),
        idea="agentic marketplace",
        store=store,
        payment_mode="live",
    )
    assert summary is None
    store.add_cost.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_reads_arxiv_quota_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The arxiv quota env should reach `make_arxiv_source` at dispatch."""
    monkeypatch.setenv("X402_MODE", "stub")
    monkeypatch.setenv("GECKO_PROVIDER_QUOTA_ARXIV", "9")

    import gecko_core.sources.arxiv as arxiv_mod
    import gecko_core.sources.bazaar as bazaar_mod
    import gecko_core.sources.twit_sh as twitsh_mod

    captured: dict[str, Any] = {}

    def _make_arxiv(**kw: Any) -> _FakeProvider:
        captured.update(kw)
        return _FakeProvider("arxiv", chunks_key="chunks", chunk_count=0, cost_usd=0.0)

    monkeypatch.setattr(
        bazaar_mod,
        "make_bazaar_provider",
        lambda **kw: _FakeProvider("bazaar", chunks_key="chunks", chunk_count=0, cost_usd=0.0),
    )
    monkeypatch.setattr(
        twitsh_mod,
        "TwitshSource",
        lambda *a, **kw: _FakeProvider("twit_sh", chunks_key="tweets", chunk_count=0, cost_usd=0.0),
    )
    monkeypatch.setattr(arxiv_mod, "make_arxiv_source", _make_arxiv)

    store = AsyncMock()
    store.add_cost = AsyncMock(return_value=None)

    await _dispatch_stub_integration_providers(
        session_id=uuid4(),
        idea="agentic protocol research",
        store=store,
        payment_mode="stub",
    )

    assert captured.get("max_results") == 9
