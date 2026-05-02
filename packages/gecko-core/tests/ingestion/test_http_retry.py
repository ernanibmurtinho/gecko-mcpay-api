"""S17-INGEST-FALLBACK-01 — HTTP retry chain + Tavily fallback budget.

Three scenarios:

  1. RemoteProtocolError twice then 200 → third attempt wins, no Tavily.
  2. Persistent 5xx three times → Tavily Extract fallback fires once,
     budget counter increments, returned text is the Tavily payload.
  3. Tavily fallback budget exhausted → no Tavily call, last_exc raised.

Wall-clock time is removed by stubbing both ``asyncio.sleep`` and the
jitter draw to zero (mirrors ``test_embed_retry._fast_backoff``).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from gecko_core.ingestion import web as web_mod


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip wall-clock retry sleeps so tests stay fast (~ms)."""

    async def _instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(web_mod.asyncio, "sleep", _instant_sleep)
    monkeypatch.setattr(web_mod, "_http_full_jitter_backoff", lambda _attempt: 0.0)


@pytest.fixture(autouse=True)
def _bypass_ssrf(monkeypatch: pytest.MonkeyPatch) -> None:
    """``validate_url`` does DNS resolution against the public host —
    skip in unit tests so respx-mocked hosts don't need a DNS record."""
    monkeypatch.setattr(web_mod, "validate_url", lambda u: u)


@pytest.fixture
def _budget() -> Any:
    """Fresh per-test Tavily fallback budget."""
    return web_mod.bind_tavily_extract_budget(cap=3)


@pytest.mark.asyncio
async def test_two_transient_then_success(_budget: Any) -> None:
    """RemoteProtocolError twice → third attempt returns 200."""
    url = "https://example.com/flaky"
    body = b"<html><body>hello world</body></html>"
    with respx.mock(assert_all_called=True) as router:
        route = router.get(url)
        route.side_effect = [
            httpx.RemoteProtocolError("connection terminated"),
            httpx.RemoteProtocolError("connection terminated"),
            httpx.Response(200, content=body, headers={"Content-Type": "text/html"}),
        ]
        text, cost = await web_mod.extract(url)

    assert "hello world" in text
    assert cost == 0.0
    # Tavily must NOT have been touched — direct fetch eventually won.
    assert _budget.used == 0


@pytest.mark.asyncio
async def test_persistent_5xx_falls_back_to_tavily(
    monkeypatch: pytest.MonkeyPatch, _budget: Any
) -> None:
    """Three consecutive 503s → Tavily Extract fallback fires (within cap)."""
    url = "https://example.com/server-down"
    tavily_text = "extracted by tavily"

    async def fake_extract_via_tavily(_u: str) -> tuple[str, bool]:
        return tavily_text, True  # billed=True → counts against budget

    monkeypatch.setattr(web_mod, "extract_via_tavily", fake_extract_via_tavily)

    with respx.mock(assert_all_called=True) as router:
        router.get(url).mock(return_value=httpx.Response(503))
        text, cost = await web_mod.extract(url)

    assert text == tavily_text
    assert cost == web_mod.TAVILY_EXTRACT_USD_PER_URL
    assert _budget.used == 1


@pytest.mark.asyncio
async def test_tavily_fallback_budget_cap_blocks_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the per-session cap is already hit, the Tavily call is NOT made
    and the original exception is raised — protects against runaway spend."""
    budget = web_mod.bind_tavily_extract_budget(cap=0)
    url = "https://example.com/server-down"

    async def fail_if_called(_u: str) -> tuple[str, bool]:  # pragma: no cover
        raise AssertionError("tavily fallback must not run when cap is reached")

    monkeypatch.setattr(web_mod, "extract_via_tavily", fail_if_called)

    with respx.mock(assert_all_called=True) as router:
        router.get(url).mock(return_value=httpx.Response(503))
        with pytest.raises(httpx.HTTPStatusError):
            await web_mod.extract(url)

    assert budget.used == 0


@pytest.mark.asyncio
async def test_4xx_skips_retry_and_jumps_to_tavily(
    monkeypatch: pytest.MonkeyPatch, _budget: Any
) -> None:
    """403 (bot wall) → no retry, straight to Tavily fallback."""
    url = "https://example.com/bot-walled"
    calls: list[int] = []

    async def fake_extract_via_tavily(_u: str) -> tuple[str, bool]:
        return "tavily got through", True

    monkeypatch.setattr(web_mod, "extract_via_tavily", fake_extract_via_tavily)

    with respx.mock(assert_all_called=True) as router:

        def _h(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(403)

        router.get(url).mock(side_effect=_h)
        text, cost = await web_mod.extract(url)

    assert text == "tavily got through"
    assert cost == web_mod.TAVILY_EXTRACT_USD_PER_URL
    # 4xx must short-circuit — exactly one direct fetch attempt.
    assert len(calls) == 1
    assert _budget.used == 1


@pytest.mark.asyncio
async def test_429_honors_retry_after_header(monkeypatch: pytest.MonkeyPatch, _budget: Any) -> None:
    """429 with Retry-After triggers retry; subsequent 200 wins.

    Distinct from a generic 5xx: the rate-limit branch reads the
    Retry-After header rather than drawing a jittered sleep.
    """
    url = "https://example.com/rate-limited"
    body = b"<html>ok</html>"
    sleep_calls: list[float] = []

    async def _capture_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(web_mod.asyncio, "sleep", _capture_sleep)

    with respx.mock(assert_all_called=True) as router:
        route = router.get(url)
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, content=body, headers={"Content-Type": "text/html"}),
        ]
        text, _cost = await web_mod.extract(url)

    assert "ok" in text
    # First (and only) sleep should match Retry-After.
    assert sleep_calls and sleep_calls[0] == 2.0
    assert _budget.used == 0
