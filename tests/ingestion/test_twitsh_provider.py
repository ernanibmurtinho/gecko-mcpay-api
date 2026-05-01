"""Tests for TwitshProvider + provider router — Sprint 14 S14-TWITSH-01.

Stub-mode only — never fires real twit.sh calls. The underlying
``TwitshSource`` is constructed with an injected ``httpx.AsyncClient``
backed by ``respx`` so the provider exercises its real wire path
without touching x402 / Solana RPC.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from gecko_core.ingestion.providers import (
    ProviderHealth,
    SourceChunk,
    SourceProvider,
)
from gecko_core.ingestion.providers.router import build_provider_plan, fanout_fetch
from gecko_core.ingestion.providers.twitsh_provider import (
    TWITSH_PER_CALL_USD,
    TwitshProvider,
    load_colosseum_judges,
)
from gecko_core.sources.twit_sh import TwitshSource


@pytest.fixture
def configured_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make `_is_twitsh_configured()` return True + research flag on."""
    monkeypatch.setenv("TWITSH_ENABLED", "true")
    monkeypatch.setenv("TWITSH_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("TWITSH_WALLET_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("TWITSH_WALLET_ADDRESS", "0x7cc33a7BbA8409374f754f1f811BC63D1ea5bCFC")
    monkeypatch.setenv("TWITSH_BASE_URL", "https://x402.twit.sh")
    # Mongo cache off so each test starts cold.
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGO_URI", raising=False)
    # Redirect the daily-aggregate ledger to a tmp file so tests don't
    # touch ~/.gecko/twitsh_daily_spend.json.
    from gecko_core.sources import twitsh_circuit

    monkeypatch.setattr(twitsh_circuit, "DEFAULT_LEDGER_PATH", tmp_path / "twitsh_daily.json")


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_twitsh_provider_satisfies_protocol() -> None:
    provider = TwitshProvider()
    assert isinstance(provider, SourceProvider)
    assert provider.name == "twitsh"
    assert provider.kind == "x402-bazaar"


@pytest.mark.asyncio
async def test_cost_estimate_matches_per_call_constant() -> None:
    provider = TwitshProvider()
    assert await provider.cost_estimate("anything") == TWITSH_PER_CALL_USD
    # And that constant is the corrected $0.01, not the legacy $0.005.
    assert TWITSH_PER_CALL_USD == 0.01


# ---------------------------------------------------------------------------
# Feature-flag gating (TWITSH_RESEARCH_ENABLED)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_unavailable_when_research_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TWITSH_RESEARCH_ENABLED", raising=False)
    provider = TwitshProvider()
    health = await provider.health()
    assert isinstance(health, ProviderHealth)
    assert health.available is False
    assert "research-time disabled" in (health.last_error or "")


@pytest.mark.asyncio
async def test_fetch_returns_empty_when_research_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with twit.sh fully configured, the research flag must gate
    the provider — that's how V1 keeps the eval-gate path unchanged."""
    monkeypatch.setenv("TWITSH_ENABLED", "true")
    monkeypatch.delenv("TWITSH_RESEARCH_ENABLED", raising=False)
    provider = TwitshProvider()
    chunks = await provider.fetch("solana DEX with sandwich-protection")
    assert chunks == []


@pytest.mark.usefixtures("configured_env")
@pytest.mark.asyncio
async def test_health_unavailable_when_source_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWITSH_ENABLED", "false")
    provider = TwitshProvider()
    health = await provider.health()
    assert health.available is False


# ---------------------------------------------------------------------------
# fetch — happy path with allowlist
# ---------------------------------------------------------------------------


_SAMPLE_RESPONSE = {
    "tweets": [
        {
            "text": "shipping a Solana DEX with sandwich-protection",
            "user": {"screen_name": "aeyakovenko"},
            "url": "https://x.com/aeyakovenko/status/1",
            "public_metrics": {"like_count": 100, "reply_count": 5, "retweet_count": 10},
        },
        {
            "text": "noise from a non-judge account",
            "user": {"screen_name": "randomperson"},
            "url": "https://x.com/randomperson/status/2",
        },
        {
            "text": "another judge weighs in",
            "user": {"screen_name": "rajgokal"},
            "url": "https://x.com/rajgokal/status/3",
        },
    ]
}


@pytest.mark.usefixtures("configured_env")
@pytest.mark.asyncio
async def test_fetch_with_allowlist_drops_non_allowed_authors() -> None:
    allowlist = frozenset({"@aeyakovenko", "@rajgokal"})
    async with respx.mock(base_url="https://x402.twit.sh", assert_all_called=False) as router:
        router.get("/tweets/search").mock(return_value=httpx.Response(200, json=_SAMPLE_RESPONSE))
        client = httpx.AsyncClient(base_url="https://x402.twit.sh")
        source = TwitshSource(http_client=client)
        provider = TwitshProvider(source=source, author_allowlist=allowlist)
        chunks = await provider.fetch("solana DEX with sandwich-protection")
        await provider.aclose()

    # Two of three sample tweets are by allowed authors.
    assert len(chunks) == 2
    handles = [c.metadata.get("creator_handle") for c in chunks]
    assert all(h in {"@aeyakovenko", "@rajgokal"} for h in handles)
    for chunk in chunks:
        assert isinstance(chunk, SourceChunk)
        assert chunk.metadata.get("provider") == "twitsh"


@pytest.mark.usefixtures("configured_env")
@pytest.mark.asyncio
async def test_fetch_without_allowlist_emits_all_tweets() -> None:
    async with respx.mock(base_url="https://x402.twit.sh", assert_all_called=False) as router:
        router.get("/tweets/search").mock(return_value=httpx.Response(200, json=_SAMPLE_RESPONSE))
        client = httpx.AsyncClient(base_url="https://x402.twit.sh")
        source = TwitshSource(http_client=client)
        provider = TwitshProvider(source=source)  # no allowlist
        chunks = await provider.fetch("anything")
        await provider.aclose()
    assert len(chunks) == 3


# ---------------------------------------------------------------------------
# Cache key includes allowlist hash (filtered/unfiltered isolation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filtered_and_unfiltered_runs_use_distinct_cache_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S14-TWITSH-01 acceptance: ``allowlist_hash`` in the cache key
    means a filtered run does not re-use an unfiltered cache entry and
    vice versa. We don't need a live HTTP probe — the cache layer's
    write/read fixture can prove the key shape on its own."""
    monkeypatch.setenv("TWITSH_ENABLED", "true")
    monkeypatch.setenv("TWITSH_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("TWITSH_WALLET_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("TWITSH_WALLET_ADDRESS", "0x7cc33a7BbA8409374f754f1f811BC63D1ea5bCFC")

    cache_store: dict[str, dict[str, object]] = {}

    async def fake_get(collection: str, key: str) -> dict[str, object] | None:
        return cache_store.get(f"{collection}:{key}")

    async def fake_set(
        collection: str, key: str, value: dict[str, object], ttl_seconds: int
    ) -> None:
        cache_store[f"{collection}:{key}"] = value

    from gecko_core.sources import twit_sh as twitsh_mod

    monkeypatch.setattr(twitsh_mod, "is_mongo_configured", lambda: True)
    monkeypatch.setattr(twitsh_mod, "get_cached", fake_get)
    monkeypatch.setattr(twitsh_mod, "set_cached", fake_set)

    async with respx.mock(base_url="https://x402.twit.sh", assert_all_called=False) as router:
        route = router.get("/tweets/search").mock(
            return_value=httpx.Response(200, json=_SAMPLE_RESPONSE)
        )
        # Run 1: unfiltered. Writes cache key for "none" allowlist_hash.
        client_a = httpx.AsyncClient(base_url="https://x402.twit.sh")
        provider_a = TwitshProvider(source=TwitshSource(http_client=client_a))
        await provider_a.fetch("solana dex")
        await provider_a.aclose()
        # Run 2: filtered. MUST hit HTTP again — different cache key.
        client_b = httpx.AsyncClient(base_url="https://x402.twit.sh")
        provider_b = TwitshProvider(
            source=TwitshSource(http_client=client_b),
            author_allowlist=frozenset({"@aeyakovenko"}),
        )
        await provider_b.fetch("solana dex")
        await provider_b.aclose()

    # Both runs hit HTTP; if the cache key collided, the second would have
    # short-circuited and route.call_count would be 1.
    assert route.call_count == 2
    # Two distinct keys persisted.
    assert len(cache_store) == 2


# ---------------------------------------------------------------------------
# Allowlist loader
# ---------------------------------------------------------------------------


def test_load_colosseum_judges_returns_union_of_cycles(tmp_path: Path) -> None:
    p = tmp_path / "judges.json"
    p.write_text(
        json.dumps(
            {
                "updated_at": "2026-04-01",
                "cycles": {
                    "renaissance_2026": ["@a", "@b"],
                    "radar_2026": ["@b", "@c"],
                },
            }
        )
    )
    handles = load_colosseum_judges(p)
    assert handles == frozenset({"@a", "@b", "@c"})


def test_load_colosseum_judges_returns_empty_on_missing_file(tmp_path: Path) -> None:
    handles = load_colosseum_judges(tmp_path / "missing.json")
    assert handles == frozenset()


def test_load_colosseum_judges_filters_to_requested_cycle(tmp_path: Path) -> None:
    p = tmp_path / "judges.json"
    p.write_text(
        json.dumps(
            {
                "cycles": {
                    "old_cycle": ["@old1"],
                    "renaissance_2026": ["@new1"],
                }
            }
        )
    )
    handles = load_colosseum_judges(p, cycles=["renaissance_2026"])
    assert handles == frozenset({"@new1"})


def test_shipped_colosseum_judges_file_exists_and_parses() -> None:
    """The static data shipped with the package must load cleanly. Five
    handle minimum is the build-plan acceptance criterion."""
    handles = load_colosseum_judges()
    # At least 5 unique handles across all cycles.
    assert len(handles) >= 5


# ---------------------------------------------------------------------------
# Provider router rules
# ---------------------------------------------------------------------------


def test_router_includes_free_provider_always() -> None:
    plan = build_provider_plan(idea="anything", category="saas")
    names = [p.name for p in plan]
    assert "tavily" in names


def test_router_adds_filtered_twitsh_for_solana_crypto() -> None:
    plan = build_provider_plan(
        idea="Solana DEX with adversarial sandwich-protection",
        category="crypto",
    )
    names = [p.name for p in plan]
    assert "twitsh" in names
    # Find the twitsh provider; allowlist should be populated (judges file ships).
    twitsh = next(p for p in plan if p.name == "twitsh")
    assert isinstance(twitsh, TwitshProvider)
    assert twitsh._allowlist is not None
    assert len(twitsh._allowlist) >= 5


def test_router_adds_unfiltered_twitsh_for_non_solana_crypto() -> None:
    plan = build_provider_plan(
        idea="ethereum L2 ux improvement",
        category="defi",
    )
    twitsh = next((p for p in plan if p.name == "twitsh"), None)
    assert twitsh is not None
    assert isinstance(twitsh, TwitshProvider)
    assert twitsh._allowlist is None


def test_router_skips_twitsh_for_non_matching_category() -> None:
    plan = build_provider_plan(
        idea="Solana payments app",  # solana keyword present
        category="saas",  # but category does not match
    )
    names = [p.name for p in plan]
    assert "twitsh" not in names


def test_router_handles_none_category_safely() -> None:
    plan = build_provider_plan(idea="solana stuff", category=None)
    names = [p.name for p in plan]
    assert "twitsh" not in names
    assert "tavily" in names


# ---------------------------------------------------------------------------
# Parallel fan-out (S14-TWITSH-04)
# ---------------------------------------------------------------------------


class _DelayedProvider:
    """Test double — sleeps ``delay_s`` then returns ``chunks``.

    Conforms structurally to ``SourceProvider`` so ``fanout_fetch`` can
    iterate it without a real Tavily/twit.sh instance. Captures the
    fetch start/end timestamps on instance for assertion.
    """

    name: str
    kind: str = "free"

    def __init__(
        self, name: str, *, delay_s: float, chunks: list[SourceChunk] | Exception | None = None
    ) -> None:
        self.name = name
        self._delay_s = delay_s
        self._chunks = chunks if chunks is not None else []

    async def cost_estimate(self, query: str) -> float:
        return 0.0

    async def health(self) -> ProviderHealth:
        return ProviderHealth(available=True)

    async def fetch(self, query: str) -> list[SourceChunk]:
        import asyncio

        await asyncio.sleep(self._delay_s)
        if isinstance(self._chunks, Exception):
            raise self._chunks
        return self._chunks


@pytest.mark.asyncio
async def test_fanout_fetch_runs_providers_in_parallel() -> None:
    """Wall-clock must be max(delay), not sum(delay)."""
    import asyncio
    import time

    p1 = _DelayedProvider("tavily", delay_s=0.30)
    p2 = _DelayedProvider("twitsh", delay_s=0.30)
    p3 = _DelayedProvider("paragraph", delay_s=0.30)

    t0 = time.monotonic()
    chunks, degraded = await fanout_fetch([p1, p2, p3], query="anything")
    elapsed = time.monotonic() - t0

    # Sequential would be ~0.9s; parallel ~0.3s. Allow generous slack.
    assert elapsed < 0.6, f"fanout took {elapsed:.2f}s — appears sequential"
    assert chunks == []
    assert degraded == []
    # Sanity: gather did the right thing across all 3.
    _ = asyncio  # keep import linted


@pytest.mark.asyncio
async def test_fanout_fetch_marks_failed_provider_degraded() -> None:
    """A raising provider must NOT halt the rest; surfaces in degraded list."""
    p_ok = _DelayedProvider("tavily", delay_s=0.05)
    p_bad = _DelayedProvider("twitsh", delay_s=0.05, chunks=RuntimeError("boom"))
    chunks, degraded = await fanout_fetch([p_ok, p_bad], query="x")
    assert degraded == ["twitsh"]
    assert chunks == []  # both providers had no chunks; failure didn't crash


@pytest.mark.usefixtures("configured_env")
@pytest.mark.asyncio
async def test_circuit_breaker_skips_provider_past_daily_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S14-TWITSH-05: past the daily cap, fetch returns [] without
    hitting HTTP. The dispatcher's fanout_fetch then surfaces 'twitsh'
    in degraded_sources via the empty result + the in-memory health
    check the next round."""
    from gecko_core.sources import twitsh_circuit

    # Cap at $0.005 — anything below the per-call $0.01 reservation
    # trips the breaker on the very first call.
    monkeypatch.setenv("TWITSH_DAILY_CAP_USD", "0.005")
    monkeypatch.setattr(twitsh_circuit, "DEFAULT_LEDGER_PATH", tmp_path / "spend.json")

    async with respx.mock(base_url="https://x402.twit.sh", assert_all_called=False) as router:
        route = router.get("/tweets/search").mock(
            return_value=httpx.Response(200, json=_SAMPLE_RESPONSE)
        )
        client = httpx.AsyncClient(base_url="https://x402.twit.sh")
        provider = TwitshProvider(source=TwitshSource(http_client=client))
        chunks = await provider.fetch("anything")
        await provider.aclose()

    assert chunks == []
    # No HTTP call when breaker tripped.
    assert route.call_count == 0


def test_circuit_breaker_resets_on_utc_date_roll(tmp_path: Path) -> None:
    """Yesterday's spend doesn't count against today's cap."""
    from gecko_core.sources import twitsh_circuit

    ledger_path = tmp_path / "spend.json"
    # Pre-seed yesterday with the full $10 cap consumed.
    yesterday = "2025-04-30"  # any past date
    ledger_path.write_text('{"' + yesterday + '": 10.0}')

    # Today's call must succeed regardless.
    ok = twitsh_circuit.reserve_spend(0.01, ledger_path=ledger_path)
    assert ok is True
    today_spend = twitsh_circuit.get_today_spend(ledger_path=ledger_path)
    assert today_spend == 0.01


def test_circuit_breaker_accumulates_within_cap(tmp_path: Path) -> None:
    from gecko_core.sources import twitsh_circuit

    ledger_path = tmp_path / "spend.json"
    # Cap = $0.05; three $0.01 reservations fit, fourth rejected.
    for _ in range(5):
        ok = twitsh_circuit.reserve_spend(0.01, ledger_path=ledger_path, cap_usd=0.05)
        assert ok is True
    # 6th would exceed.
    rejected = twitsh_circuit.reserve_spend(0.01, ledger_path=ledger_path, cap_usd=0.05)
    assert rejected is False
    # And the ledger does NOT count the rejected reservation.
    assert twitsh_circuit.get_today_spend(ledger_path=ledger_path) == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_fanout_fetch_per_provider_timeout() -> None:
    """A provider exceeding the timeout is degraded, not awaited forever."""
    p_slow = _DelayedProvider("twitsh", delay_s=2.0)
    p_ok = _DelayedProvider("tavily", delay_s=0.01)
    chunks, degraded = await fanout_fetch([p_ok, p_slow], query="x", timeout_s=0.1)
    assert "twitsh" in degraded
    assert chunks == []
