"""CoinGecko OHLC history-source tests (Phase 9.5).

Light fakes only — never fires real CoinGecko requests. The httpx client
is monkeypatched at the call boundary; the candle-mapping helper is
exercised through the public ``fetch`` surface.
"""

from __future__ import annotations

from typing import Any

import pytest
from gecko_core.orchestration.trade_panel.backtest import (
    Candle,
    CoinGeckoOhlcHistorySource,
)


class _FakeResponse:
    """Light httpx.Response stand-in — only the bits the source reads."""

    def __init__(self, *, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _ScriptedClient:
    """Async context-managerless fake httpx.AsyncClient with a scripted queue.

    The source owns its client when ``http_client=None``, so we inject this
    instance to skip httpx entirely and assert what URL/params it sees.
    """

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(self, url: str, *, params: dict[str, str]) -> _FakeResponse:
        self.calls.append((url, dict(params)))
        if not self._responses:
            raise AssertionError("scripted client exhausted")
        return self._responses.pop(0)

    async def aclose(self) -> None:  # pragma: no cover - never owned
        return None


@pytest.mark.asyncio
async def test_fetch_returns_candles_from_canned_response() -> None:
    """Canned OHLC array maps cleanly into ``list[Candle]`` with source='coingecko'."""
    payload = [
        # [ts_ms, open, high, low, close]
        [1_700_000_000_000, 2.0, 2.1, 1.95, 2.05],
        [1_700_086_400_000, 2.05, 2.2, 2.0, 2.15],
    ]
    client = _ScriptedClient([_FakeResponse(status_code=200, payload=payload)])
    src = CoinGeckoOhlcHistorySource(http_client=client)  # type: ignore[arg-type]

    out = await src.fetch(
        "jito",
        granularity="1d",
        ts_start=1_700_000_000,
        ts_end=1_700_200_000,
    )

    assert len(out) == 2
    assert all(isinstance(c, Candle) for c in out)
    assert out[0].source == "coingecko"
    assert out[0].protocol == "jito"
    assert out[0].ts == 1_700_000_000  # ms → s conversion
    assert out[0].open == pytest.approx(2.0)
    assert out[0].close == pytest.approx(2.05)
    assert out[0].vol_usd == 0.0  # free tier has no volume

    # Confirm the request shape.
    assert len(client.calls) == 1
    url, params = client.calls[0]
    assert url.endswith("/coins/jito-governance-token/ohlc")
    assert params["vs_currency"] == "usd"
    assert "days" in params


@pytest.mark.asyncio
async def test_fetch_unknown_protocol_returns_empty() -> None:
    """Protocols not in the pinned dict return [] gracefully (no raise)."""
    src = CoinGeckoOhlcHistorySource()
    out = await src.fetch(
        "doesnotexist",
        granularity="1d",
        ts_start=1_700_000_000,
        ts_end=1_700_200_000,
    )
    assert out == []


@pytest.mark.asyncio
async def test_fetch_handles_429_with_backoff() -> None:
    """First response 429, second 200 — caller sees the second response's candles."""
    payload = [[1_700_000_000_000, 2.0, 2.1, 1.95, 2.05]]
    client = _ScriptedClient(
        [
            _FakeResponse(status_code=429, payload=None),
            _FakeResponse(status_code=200, payload=payload),
        ]
    )
    # backoff_seconds=0 keeps the test fast (no real sleep).
    src = CoinGeckoOhlcHistorySource(
        http_client=client,  # type: ignore[arg-type]
        max_retries=1,
        backoff_seconds=0.0,
    )
    out = await src.fetch(
        "jito",
        granularity="1d",
        ts_start=1_700_000_000,
        ts_end=1_700_200_000,
    )
    assert len(out) == 1
    assert out[0].close == pytest.approx(2.05)
    assert len(client.calls) == 2  # one retry happened


@pytest.mark.asyncio
async def test_default_history_source_is_coingecko_when_none_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_trade_panel_with_retrieval(history_source=None, enable_backtest=True)``
    instantiates a CoinGeckoOhlcHistorySource (Phase 9.5 default flip).

    Stubs out retrieval + the inner panel + ``backtest_intent`` so the test
    only asserts the source-construction branch. No AG2, no httpx, no Mongo.
    """
    captured: dict[str, Any] = {}

    import gecko_core.orchestration.trade_panel as panel_pkg
    import gecko_core.orchestration.trade_panel.backtest as bt_pkg
    from gecko_core.orchestration.trade_panel import TradePanelTurn, TradePanelVerdict
    from gecko_core.orchestration.trade_panel.backtest import BacktestReport

    fake_verdict = TradePanelVerdict(
        verdict="defer",
        confidence=0.5,
        key_drivers=[],
        dissent_count=0,
        blocker_questions=[],
        turns=[
            TradePanelTurn(
                agent="strategist",
                content="open jito long horizon=14d size=1%",
                parsed_verdict=None,
            ),
        ],
    )

    async def _fake_retrieve(**kwargs: Any) -> list[Any]:
        return []

    async def _fake_inner_panel(**kwargs: Any) -> TradePanelVerdict:
        return fake_verdict

    async def _fake_backtest_intent(intent: dict[str, Any], source: Any) -> Any:
        captured["source"] = source
        return BacktestReport(unbacktestable=True, reason="no_candles")

    monkeypatch.setattr(panel_pkg, "retrieve_trade_corpus_chunks", _fake_retrieve)
    monkeypatch.setattr(panel_pkg, "run_trade_panel", _fake_inner_panel)
    monkeypatch.setattr(bt_pkg, "backtest_intent", _fake_backtest_intent)

    await panel_pkg.run_trade_panel_with_retrieval(
        idea="x",
        protocol="jito",
        vertical="dex",
        tier="basic",
        llm_config={"config_list": []},
        enable_backtest=True,
        history_source=None,
    )
    assert "source" in captured, "backtest_intent never invoked"
    assert isinstance(captured["source"], CoinGeckoOhlcHistorySource)
