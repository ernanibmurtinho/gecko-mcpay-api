"""Tests for `gecko_core.sources.twit_sh.TwitshSource`.

Strategy: inject an `httpx.AsyncClient` backed by `respx.MockRouter` so we
never go near a real x402 facilitator or Solana RPC. Cache layer is mocked at
the function level via monkeypatch.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from gecko_core.sources import twit_sh as twitsh_mod
from gecko_core.sources.twit_sh import (
    ASSUMED_PER_CALL_USD,
    SPEND_CAP_USD,
    TwitshSource,
    _keyword_set,
    _normalize_tweet,
)

# ---------------------------------------------------------------------------
# applies_to gating
# ---------------------------------------------------------------------------


@pytest.fixture
def configured_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `_is_twitsh_configured()` return True."""
    monkeypatch.setenv("TWITSH_ENABLED", "true")
    monkeypatch.setenv(
        "TWITSH_WALLET_PRIVATE_KEY",
        "0x" + "11" * 32,  # well-formed hex; never used in tests (http injected)
    )
    monkeypatch.setenv("TWITSH_WALLET_ADDRESS", "0x7cc33a7BbA8409374f754f1f811BC63D1ea5bCFC")
    monkeypatch.setenv("TWITSH_BASE_URL", "https://x402.twit.sh")


@pytest.fixture
def no_mongo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the Mongo cache to behave as unconfigured for cache-isolation tests."""
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGO_URI", raising=False)


@pytest.mark.usefixtures("configured_env", "no_mongo")
async def test_applies_to_fires_for_crypto() -> None:
    src = TwitshSource(http_client=httpx.AsyncClient())
    assert await src.applies_to(categories={"crypto"}) is True
    assert await src.applies_to(categories={"defi"}) is True
    assert await src.applies_to(categories={"hackathon-team"}) is True
    await src.aclose()


@pytest.mark.usefixtures("configured_env", "no_mongo")
async def test_applies_to_skips_for_non_matching_category() -> None:
    src = TwitshSource(http_client=httpx.AsyncClient())
    assert await src.applies_to(categories={"saas"}) is False
    assert await src.applies_to(categories={"regulated"}) is False
    assert await src.applies_to(categories=set()) is False
    await src.aclose()


@pytest.mark.usefixtures("no_mongo")
async def test_applies_to_skips_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with crypto categories, no-config → no fire."""
    monkeypatch.setenv("TWITSH_ENABLED", "false")
    src = TwitshSource(http_client=httpx.AsyncClient())
    assert await src.applies_to(categories={"crypto"}) is False
    await src.aclose()


@pytest.mark.usefixtures("no_mongo")
async def test_applies_to_skips_with_sentinel_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWITSH_ENABLED", "true")
    monkeypatch.setenv("TWITSH_WALLET_PRIVATE_KEY", "__unset__")
    monkeypatch.setenv("TWITSH_WALLET_ADDRESS", "__unset__")
    src = TwitshSource(http_client=httpx.AsyncClient())
    assert await src.applies_to(categories={"crypto"}) is False
    await src.aclose()


# ---------------------------------------------------------------------------
# fetch — live path (mocked HTTP)
# ---------------------------------------------------------------------------


_SAMPLE_RESPONSE: dict[str, Any] = {
    "tweets": [
        {
            "text": "shipping a Solana credit card for retail",
            "user": {"screen_name": "solbuilder"},
            "url": "https://x.com/solbuilder/status/1",
            "public_metrics": {"like_count": 312, "reply_count": 18, "retweet_count": 22},
            "created_at": "2026-04-27T12:00:00Z",
        },
        {
            "text": "USDC payments on Base are getting interesting",
            "user": {"screen_name": "basecaller"},
            "url": "https://x.com/basecaller/status/2",
            "public_metrics": {"like_count": 45, "reply_count": 3, "retweet_count": 5},
            "created_at": "2026-04-27T13:00:00Z",
        },
    ]
}


@pytest.mark.usefixtures("configured_env", "no_mongo")
async def test_fetch_returns_normalized_citation_shape() -> None:
    async with respx.mock(base_url="https://x402.twit.sh", assert_all_called=False) as router:
        router.get("/tweets/search").mock(return_value=httpx.Response(200, json=_SAMPLE_RESPONSE))

        client = httpx.AsyncClient(base_url="https://x402.twit.sh")
        src = TwitshSource(http_client=client)
        result = await src.fetch(
            idea="credit card on Solana for retail merchants",
            categories={"crypto"},
        )
        await src.aclose()

    assert result.fired is True
    assert result.error is None
    tweets = result.payload["tweets"]
    assert len(tweets) == 2
    t0 = tweets[0]
    assert set(t0.keys()) == {"text", "author_handle", "url", "engagement", "created_at"}
    assert t0["author_handle"] == "@solbuilder"
    assert t0["engagement"] == {"likes": 312, "replies": 18, "reposts": 22}
    assert t0["url"].startswith("https://")
    # Spend was debited at exactly one call (1 * ASSUMED_PER_CALL_USD).
    assert result.cost_usd == pytest.approx(ASSUMED_PER_CALL_USD)
    assert result.payload["from_cache"] is False


@pytest.mark.usefixtures("configured_env", "no_mongo")
async def test_fetch_caps_results_at_max() -> None:
    """Top-N cap honored even when upstream returns more."""
    big_response = {"tweets": [{"text": f"t{i}", "user": {"screen_name": "x"}} for i in range(30)]}
    async with respx.mock(base_url="https://x402.twit.sh", assert_all_called=False) as router:
        router.get("/tweets/search").mock(return_value=httpx.Response(200, json=big_response))
        client = httpx.AsyncClient(base_url="https://x402.twit.sh")
        src = TwitshSource(http_client=client)
        result = await src.fetch(idea="defi liquidity routing", categories={"defi"})
        await src.aclose()
    assert len(result.payload["tweets"]) == twitsh_mod.MAX_RESULTS


@pytest.mark.usefixtures("configured_env", "no_mongo")
async def test_fetch_propagates_http_error_verbatim() -> None:
    async with respx.mock(base_url="https://x402.twit.sh", assert_all_called=False) as router:
        router.get("/tweets/search").mock(return_value=httpx.Response(503, text="upstream sad"))
        client = httpx.AsyncClient(base_url="https://x402.twit.sh")
        src = TwitshSource(http_client=client)
        result = await src.fetch(idea="onchain fx swap", categories={"crypto"})
        await src.aclose()
    assert result.fired is False
    assert result.error is not None and "503" in result.error


# ---------------------------------------------------------------------------
# Spend-cap guard
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("configured_env", "no_mongo")
async def test_spend_cap_halts_further_fetches() -> None:
    """When spend would exceed the cap, no HTTP call is issued."""
    async with respx.mock(base_url="https://x402.twit.sh", assert_all_called=False) as router:
        route = router.get("/tweets/search").mock(
            return_value=httpx.Response(200, json=_SAMPLE_RESPONSE)
        )
        client = httpx.AsyncClient(base_url="https://x402.twit.sh")
        # Cap below the per-call assumed cost — first attempt must be blocked.
        src = TwitshSource(http_client=client, spend_cap_usd=ASSUMED_PER_CALL_USD / 10)
        result = await src.fetch(idea="zk rollup builders", categories={"crypto"})
        await src.aclose()

    assert route.call_count == 0, "spend cap did not block HTTP call"
    assert result.fired is True
    assert result.cost_usd == 0.0
    assert result.payload["tweets"] == []


@pytest.mark.usefixtures("configured_env", "no_mongo")
async def test_spend_cap_at_default_allows_one_call() -> None:
    """At default cap ($0.05) one $0.005 call comfortably fits."""
    async with respx.mock(base_url="https://x402.twit.sh", assert_all_called=False) as router:
        route = router.get("/tweets/search").mock(
            return_value=httpx.Response(200, json=_SAMPLE_RESPONSE)
        )
        client = httpx.AsyncClient(base_url="https://x402.twit.sh")
        src = TwitshSource(http_client=client, spend_cap_usd=SPEND_CAP_USD)
        result = await src.fetch(idea="hackathon team coordination", categories={"hackathon-team"})
        await src.aclose()
    assert route.called
    assert result.cost_usd == pytest.approx(ASSUMED_PER_CALL_USD)


# ---------------------------------------------------------------------------
# Cache short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("configured_env")
async def test_cache_hit_short_circuits_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second call with same idea+categories must not hit HTTP — the cache
    layer returns the previous payload."""
    cache_store: dict[str, dict[str, Any]] = {}

    async def fake_get(collection: str, key: str) -> dict[str, Any] | None:
        return cache_store.get(f"{collection}:{key}")

    async def fake_set(collection: str, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        cache_store[f"{collection}:{key}"] = value

    monkeypatch.setattr(twitsh_mod, "is_mongo_configured", lambda: True)
    monkeypatch.setattr(twitsh_mod, "get_cached", fake_get)
    monkeypatch.setattr(twitsh_mod, "set_cached", fake_set)

    async with respx.mock(base_url="https://x402.twit.sh", assert_all_called=False) as router:
        route = router.get("/tweets/search").mock(
            return_value=httpx.Response(200, json=_SAMPLE_RESPONSE)
        )
        client = httpx.AsyncClient(base_url="https://x402.twit.sh")
        src = TwitshSource(http_client=client)

        # First call: cold → 1 HTTP call, cost > 0.
        first = await src.fetch(idea="liquid restaking ux", categories={"crypto"})
        assert first.cost_usd > 0
        assert first.payload["from_cache"] is False
        assert route.call_count == 1

        # Second call (same key): warm → 0 HTTP calls, cost == 0.
        second = await src.fetch(idea="liquid restaking ux", categories={"crypto"})
        await src.aclose()

    assert route.call_count == 1, "cache hit did not short-circuit HTTP"
    assert second.cost_usd == 0.0
    assert second.payload["from_cache"] is True
    # Same tweets surfaced both times.
    assert second.payload["tweets"] == first.payload["tweets"]


@pytest.mark.usefixtures("configured_env")
async def test_bypass_cache_forces_http_even_when_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S11-F18-01: with bypass_cache=True the source must hit HTTP and pay
    even when an unexpired entry exists in the Mongo cache. Models the
    --live-rag eval gate's need to measure true cold-signal spend."""
    cache_store: dict[str, dict[str, Any]] = {}

    async def fake_get(collection: str, key: str) -> dict[str, Any] | None:
        return cache_store.get(f"{collection}:{key}")

    async def fake_set(collection: str, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        cache_store[f"{collection}:{key}"] = value

    monkeypatch.setattr(twitsh_mod, "is_mongo_configured", lambda: True)
    monkeypatch.setattr(twitsh_mod, "get_cached", fake_get)
    monkeypatch.setattr(twitsh_mod, "set_cached", fake_set)

    async with respx.mock(base_url="https://x402.twit.sh", assert_all_called=False) as router:
        route = router.get("/tweets/search").mock(
            return_value=httpx.Response(200, json=_SAMPLE_RESPONSE)
        )

        # Warm the cache with a non-bypass instance.
        warm_client = httpx.AsyncClient(base_url="https://x402.twit.sh")
        warm = TwitshSource(http_client=warm_client)
        await warm.fetch(idea="liquid restaking ux", categories={"crypto"})
        await warm.aclose()
        assert route.call_count == 1

        # Now a bypass-cache instance: same key must hit HTTP again.
        bypass_client = httpx.AsyncClient(base_url="https://x402.twit.sh")
        bypass_src = TwitshSource(http_client=bypass_client, bypass_cache=True)
        result = await bypass_src.fetch(idea="liquid restaking ux", categories={"crypto"})
        await bypass_src.aclose()

    assert route.call_count == 2, "bypass_cache=True should re-issue HTTP"
    assert result.cost_usd == pytest.approx(ASSUMED_PER_CALL_USD)
    assert result.payload["from_cache"] is False


@pytest.mark.usefixtures("configured_env")
async def test_bypass_cache_via_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """`TWITSH_BYPASS_CACHE=true` should be picked up when the constructor
    arg is left at its default — that's how scripts/run_eval_gate_live.sh
    propagates the flag without modifying the runner code path."""
    cache_store: dict[str, dict[str, Any]] = {
        # Pre-seed: any successful read here would short-circuit HTTP.
    }

    async def fake_get(collection: str, key: str) -> dict[str, Any] | None:
        return cache_store.get(f"{collection}:{key}")

    async def fake_set(collection: str, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        cache_store[f"{collection}:{key}"] = value

    monkeypatch.setattr(twitsh_mod, "is_mongo_configured", lambda: True)
    monkeypatch.setattr(twitsh_mod, "get_cached", fake_get)
    monkeypatch.setattr(twitsh_mod, "set_cached", fake_set)
    monkeypatch.setenv("TWITSH_BYPASS_CACHE", "true")

    async with respx.mock(base_url="https://x402.twit.sh", assert_all_called=False) as router:
        route = router.get("/tweets/search").mock(
            return_value=httpx.Response(200, json=_SAMPLE_RESPONSE)
        )
        # Pre-seed cache directly so we can prove it was ignored.
        cache_store["twitsh_cache:any"] = {"tweets": [{"text": "stale"}], "query": "x"}
        client = httpx.AsyncClient(base_url="https://x402.twit.sh")
        src = TwitshSource(http_client=client)  # no explicit bypass arg
        result = await src.fetch(idea="zk proofs", categories={"crypto"})
        await src.aclose()

    assert route.call_count == 1
    assert result.cost_usd == pytest.approx(ASSUMED_PER_CALL_USD)
    assert result.payload["from_cache"] is False


# ---------------------------------------------------------------------------
# Helper-level units (worth pinning since downstream depends on the shape)
# ---------------------------------------------------------------------------


def test_keyword_set_drops_stopwords_and_caps_size() -> None:
    keys = _keyword_set("the quick brown fox jumps over the lazy dog quickly", set())
    assert "the" not in keys
    assert "over" not in keys  # stopword
    assert len(keys) <= 6


def test_keyword_set_includes_categories() -> None:
    keys = _keyword_set("payments rails", {"crypto", "defi"})
    assert "crypto" in keys
    assert "defi" in keys


def test_normalize_tweet_handles_alt_keys() -> None:
    raw = {
        "full_text": "alt-shape tweet",
        "author": {"username": "alice"},
        "permalink": "https://x.com/alice/status/9",
        "favorite_count": 7,
        "retweet_count": 2,
        "timestamp": "2026-01-01T00:00:00Z",
    }
    norm = _normalize_tweet(raw)
    assert norm is not None
    assert norm["text"] == "alt-shape tweet"
    assert norm["author_handle"] == "@alice"
    assert norm["engagement"]["likes"] == 7
    assert norm["engagement"]["reposts"] == 2


def test_normalize_tweet_returns_none_on_empty() -> None:
    assert _normalize_tweet({}) is None
    assert _normalize_tweet({"user": {"screen_name": "x"}}) is None
