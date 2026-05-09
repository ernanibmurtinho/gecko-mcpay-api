"""Issue #15 — top-level structured ``citations`` array on the wire.

Pre-#15, the trade-oracle response exposed cites only as inline ``[N]``
markers inside ``turns[].content``. Skill authors had to regex-extract
from prose to render or audit. This test pins the additive
``citations: list[Citation]`` contract:

- Each entry carries ``{id, source, url, chunk_id, provider_kind,
  freshness_tier, snippet}``.
- ``id`` is 1-indexed and matches the inline ``[N]`` markers the panel
  injects.
- ``provider_kind`` and ``freshness_tier`` round-trip the canonical
  Literals from :mod:`gecko_core.sources.types` (Pattern A).
- The basic envelope MUST also carry ``citations`` (it is not pro-only —
  the wedge claim "grounded citations" applies to both tiers); only
  ``backtest`` is pro-only.

Mocks ``run_trade_panel_with_retrieval`` so this never fires AG2,
CoinGecko, or Mongo.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("X402_MODE", "stub")
os.environ.setdefault("GECKO_WALLET_ADDRESS", "STUB_WALLET_ADDRESS_NOT_FOR_LIVE")


_REQUIRED_CITATION_KEYS = {
    "id",
    "source",
    "url",
    "chunk_id",
    "provider_kind",
    "freshness_tier",
    "snippet",
}


def _decode_payment_required_header(value: str) -> dict:
    return json.loads(base64.b64decode(value).decode("utf-8"))


def _build_payment_payload_header(accepts_entry: dict) -> str:
    payload_obj = {
        "x402Version": 2,
        "payload": {"signature": "stub-sig", "transaction": "stub-tx"},
        "accepted": accepts_entry,
    }
    return base64.b64encode(json.dumps(payload_obj).encode("utf-8")).decode("utf-8")


def _paid_post(client: TestClient, *, path: str, body: dict) -> tuple[int, dict]:
    r0 = client.post(path, json=body)
    assert r0.status_code == 402, r0.text
    accepts_entry = _decode_payment_required_header(r0.headers["payment-required"])["accepts"][0]
    payment_header = _build_payment_payload_header(accepts_entry)
    r = client.post(path, json=body, headers={"PAYMENT-SIGNATURE": payment_header})
    out: dict = {}
    try:
        out = r.json()
    except Exception:
        out = {}
    return r.status_code, out


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Patch the panel runner with a fake that attaches two structured citations.

    The fake mirrors what the real wrapper does post-#15:
    ``run_trade_panel_with_retrieval`` projects the chunks-that-fed-the-
    panel into a ``Citation`` list and attaches them to the verdict.
    """
    os.environ["X402_NETWORK"] = "solana-devnet"
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)

    from gecko_api.main import app
    from gecko_core.orchestration.trade_panel import (
        Citation,
        TradePanelTurn,
        TradePanelVerdict,
    )

    fake_citations = [
        Citation(
            id=1,
            source="paysh",
            url="https://paysh.sh/listing/jito",
            chunk_id="chunk-jito-1",
            provider_kind="paysh_live",
            freshness_tier="daily",
            snippet="JTO TVL grew 12% over the trailing 7d window across mainnet.",
        ),
        Citation(
            id=2,
            source="bazaar",
            # No URL — ensure the gecko://chunk/<hash> fallback is acceptable
            # too. The real wrapper synthesizes it; here we exercise the
            # fully-populated path explicitly to assert the shape contract.
            url="gecko://chunk/abcd1234567890ef",
            chunk_id="chunk-jito-2",
            provider_kind="bazaar_live",
            freshness_tier="live_only",
            snippet="Coordinator transcript snippet about validator rotation.",
        ),
    ]

    base_turns = [
        TradePanelTurn(
            agent="technical_analyst",
            content="Bullish, citing [1] and [2].",
            parsed_verdict={"trend_verdict": "bullish"},
        ),
        TradePanelTurn(
            agent="coordinator",
            content='```json\n{"verdict": "act"}\n```',
            parsed_verdict={"verdict": "act"},
        ),
    ]

    async def fake_panel(
        *,
        idea: str,
        protocol: str,
        vertical: str = "dex",
        tier: str = "basic",
        top_k: int = 15,
        llm_config: dict | None = None,
        agent_factory: object | None = None,
        enable_backtest: bool = False,
        history_source: object | None = None,
    ) -> TradePanelVerdict:
        verdict = TradePanelVerdict(
            verdict="act",
            confidence=0.7,
            key_drivers=["technical alignment"],
            dissent_count=0,
            blocker_questions=[],
            turns=base_turns,
            citations=fake_citations,
        )
        if enable_backtest:
            from gecko_core.orchestration.trade_panel.backtest import BacktestReport

            verdict = verdict.model_copy(
                update={
                    "backtest": BacktestReport(
                        pnl_pct=4.2,
                        drawdown_pct=1.1,
                        n_similar_setups=1,
                        hit_rate=1.0,
                        source="coingecko",
                        unbacktestable=False,
                    )
                }
            )
        return verdict

    with (
        patch(
            "gecko_core.orchestration.trade_panel.run_trade_panel_with_retrieval",
            new=AsyncMock(side_effect=fake_panel),
        ),
        TestClient(app) as c,
    ):
        yield c


_BODY = {"idea": "Should I open a JTO long around the next FOMC?", "protocol": "jito"}


def _assert_citation_shape(citations: list[dict]) -> None:
    """Per-entry shape contract from issue #15."""
    assert isinstance(citations, list)
    assert len(citations) >= 1, "expected at least one citation entry"
    for entry in citations:
        assert isinstance(entry, dict), f"non-dict citation: {entry!r}"
        missing = _REQUIRED_CITATION_KEYS - entry.keys()
        assert not missing, f"citation missing keys {missing!r}: {entry!r}"
        assert isinstance(entry["id"], int) and entry["id"] >= 1
        assert isinstance(entry["snippet"], str)
        assert len(entry["snippet"]) <= 240


def test_basic_envelope_carries_structured_citations(client: TestClient) -> None:
    """Basic /trade_research must surface citations[] sibling to inline [N]."""
    status, body = _paid_post(client, path="/trade_research", body=_BODY)
    assert status == 200, body
    assert "citations" in body, f"missing citations field; keys={sorted(body)!r}"
    _assert_citation_shape(body["citations"])
    # Issue #15: basic still must NOT carry backtest (pro-only field).
    assert "backtest" not in body, f"basic leaked backtest: {body.get('backtest')!r}"


def test_pro_envelope_carries_both_citations_and_backtest(client: TestClient) -> None:
    """Pro envelope must carry the new citations[] AND #14's backtest field."""
    status, body = _paid_post(client, path="/trade_research/pro", body=_BODY)
    assert status == 200, body
    assert "citations" in body
    _assert_citation_shape(body["citations"])
    assert body.get("backtest") is not None
    assert isinstance(body["backtest"], dict)


def test_citations_carry_canonical_provider_kind_and_freshness(
    client: TestClient,
) -> None:
    """Pattern A: provider_kind and freshness_tier must round-trip the canonical Literals.

    Wire shape mirrors :mod:`gecko_core.sources.types` — no adapter-internal
    tags ('bazaar:dataset', 'free:arxiv') leak onto the wire.
    """
    from gecko_core.sources.types import FRESHNESS_TIER_VALUES, PROVIDER_KINDS

    status, body = _paid_post(client, path="/trade_research", body=_BODY)
    assert status == 200, body
    cites = body["citations"]
    for entry in cites:
        assert entry["provider_kind"] in PROVIDER_KINDS, (
            f"provider_kind {entry['provider_kind']!r} not in canonical set"
        )
        assert entry["freshness_tier"] in FRESHNESS_TIER_VALUES, (
            f"freshness_tier {entry['freshness_tier']!r} not in canonical set"
        )


def test_citation_ids_are_one_indexed_and_match_marker_count(
    client: TestClient,
) -> None:
    """The 1-indexed id field is the contract that links inline [N] to array index."""
    status, body = _paid_post(client, path="/trade_research", body=_BODY)
    assert status == 200, body
    cites = body["citations"]
    assert [c["id"] for c in cites] == list(range(1, len(cites) + 1))
