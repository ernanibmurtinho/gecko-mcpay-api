"""S25 #13 — unit tests for retrieval scoring boosts.

Pure-function tests over `_apply_retrieval_boosts` and `_parse_chunk_date`.
No Mongo, no LLM, no autogen. Lighter-tests pattern per CLAUDE.md:
test the helper directly, don't fire the full retrieval orchestration to
validate one scoring rule.

Why these matter: S24 close-out synthesis showed citation_relevance=0.38
because Atlas raw vectorSearchScore buries protocol-manifest chunks
(paysh_live for Kamino, 7 chunks of sparse JSON) beneath tangentially-
related canon PDFs (a Damodaran ERP paragraph mentioning "leverage"
scores higher than a Kamino vault manifest for a Kamino leverage
question). The boost is the wedge — verify it composes correctly.
"""

from __future__ import annotations

from gecko_core.orchestration.trade_panel import (
    _apply_retrieval_boosts,
    _parse_chunk_date,
)


def _row(
    *,
    score: float,
    protocol: list[str] | str | None = None,
    provider_kind: str = "canon_damodaran",
    freshness_tier: str = "static",
    text: str = "",
    source_url: str = "",
    metadata: dict | None = None,
    captured_at: str | None = None,
) -> dict:
    return {
        "score": score,
        "protocol": protocol,
        "provider_kind": provider_kind,
        "freshness_tier": freshness_tier,
        "text": text,
        "source_url": source_url,
        "metadata": metadata or {},
        "captured_at": captured_at,
    }


# ----------------------------- protocol boost -----------------------------


def test_protocol_exact_boost_promotes_paysh_above_canon() -> None:
    """paysh_live with protocol=kamino at raw=0.70 must rank above
    canon_damodaran proto=[] at raw=0.78 after boost (+0.25 for
    paysh_live protocol-exact > +0.08 raw gap)."""
    rows = [
        _row(
            score=0.78,
            protocol=[],
            provider_kind="canon_damodaran",
        ),
        _row(
            score=0.70,
            protocol=["kamino"],
            provider_kind="paysh_live",
            freshness_tier="daily",
        ),
    ]
    out = _apply_retrieval_boosts(rows, protocol="kamino", as_of_date=None)
    assert out[0]["provider_kind"] == "paysh_live"
    assert out[0]["score_boost"] == 0.25  # +0.15 protocol + +0.10 provider
    assert out[1]["provider_kind"] == "canon_damodaran"
    assert out[1]["score_boost"] == 0.0


def test_canon_protocol_empty_does_not_boost() -> None:
    """canon chunks carry protocol=[] — they MUST NOT get the protocol-exact
    boost (Pattern F: canon is cross-cutting, not protocol-matched)."""
    rows = [
        _row(score=0.72, protocol=[], provider_kind="canon_marks"),
        _row(score=0.72, protocol=["kamino"], provider_kind="bazaar_live", freshness_tier="daily"),
    ]
    out = _apply_retrieval_boosts(rows, protocol="kamino", as_of_date=None)
    assert out[0]["provider_kind"] == "bazaar_live"
    assert out[0]["score_boost"] == 0.25
    assert out[1]["score_boost"] == 0.0


def test_protocol_mismatch_no_boost() -> None:
    """A paysh_live chunk for jupiter must NOT get a boost when the query
    is for kamino — even though provider_kind is in the specific set."""
    rows = [
        _row(score=0.72, protocol=["jupiter"], provider_kind="paysh_live"),
    ]
    out = _apply_retrieval_boosts(rows, protocol="kamino", as_of_date=None)
    assert out[0]["score_boost"] == 0.0


def test_scalar_protocol_field_works() -> None:
    """Defensive: some legacy chunks may carry scalar protocol, not array."""
    rows = [
        _row(score=0.70, protocol="kamino", provider_kind="paysh_live", freshness_tier="daily"),
    ]
    out = _apply_retrieval_boosts(rows, protocol="kamino", as_of_date=None)
    assert out[0]["score_boost"] == 0.25


# ----------------------------- date boost -----------------------------


def test_date_aligned_market_data_boost_in_window() -> None:
    rows = [
        _row(
            score=0.70,
            protocol=["kamino"],
            provider_kind="market_data",
            freshness_tier="hot",
            metadata={"timestamp": "2024-11-03T08:00:00Z"},
        ),
    ]
    out = _apply_retrieval_boosts(rows, protocol="kamino", as_of_date="2024-11-05")
    # +0.15 protocol exact + +0.05 date aligned (no provider-specific since
    # market_data isn't in {paysh_live, bazaar_live}).
    assert out[0]["score_boost"] == 0.2


def test_date_misaligned_market_data_demoted() -> None:
    rows = [
        _row(
            score=0.78,
            protocol=["kamino"],
            provider_kind="market_data",
            freshness_tier="hot",
            metadata={"timestamp": "2026-05-12T08:00:00Z"},
        ),
        _row(
            score=0.70,
            protocol=["kamino"],
            provider_kind="paysh_live",
            freshness_tier="daily",
            metadata={"timestamp": "2024-11-04T08:00:00Z"},
        ),
    ]
    out = _apply_retrieval_boosts(rows, protocol="kamino", as_of_date="2024-11-05")
    # market_data: 0.78 + 0.15 (proto) - 0.10 (date) = 0.83
    # paysh_live: 0.70 + 0.15 (proto) + 0.10 (provider) + 0.05 (date) = 1.00
    assert out[0]["provider_kind"] == "paysh_live"
    assert out[1]["provider_kind"] == "market_data"


def test_static_canon_ignores_date() -> None:
    """Static canon (Marks, Damodaran) must not be date-scored — the
    captured_at on those rows is ingest time, not content time."""
    rows = [
        _row(
            score=0.72,
            protocol=[],
            provider_kind="canon_damodaran",
            freshness_tier="static",
            captured_at="2026-05-01T00:00:00Z",
        ),
    ]
    out = _apply_retrieval_boosts(rows, protocol="kamino", as_of_date="2024-11-05")
    assert out[0]["score_boost"] == 0.0


def test_no_as_of_date_skips_date_scoring() -> None:
    rows = [
        _row(
            score=0.70,
            protocol=["kamino"],
            provider_kind="market_data",
            freshness_tier="hot",
            metadata={"timestamp": "2024-11-03T08:00:00Z"},
        ),
    ]
    out = _apply_retrieval_boosts(rows, protocol="kamino", as_of_date=None)
    # Only protocol-exact (+0.15), no date scoring.
    assert out[0]["score_boost"] == 0.15


# ----------------------------- _parse_chunk_date -----------------------------


def test_parse_chunk_date_metadata_timestamp_wins() -> None:
    chunk = {
        "metadata": {"timestamp": "2024-11-03T08:00:00Z"},
        "source_url": "https://gecko.local/market_data/kamino/2025-01-01T00:00:00Z",
        "freshness_tier": "hot",
    }
    assert _parse_chunk_date(chunk) == "2024-11-03"


def test_parse_chunk_date_source_url_anchor() -> None:
    chunk = {
        "metadata": {},
        "source_url": "https://example.com/feed#2024-08-12T08:00:00Z",
        "freshness_tier": "hot",
        "text": "",
    }
    assert _parse_chunk_date(chunk) == "2024-08-12"


def test_parse_chunk_date_text_fallback() -> None:
    chunk = {
        "metadata": {},
        "source_url": "",
        "freshness_tier": "hot",
        "text": "DefiLlama TVL snapshot for kamino (as of 2024-11-05T08:00:00Z). ...",
    }
    assert _parse_chunk_date(chunk) == "2024-11-05"


def test_parse_chunk_date_none_when_no_signal() -> None:
    chunk = {
        "metadata": {},
        "source_url": "",
        "freshness_tier": "static",
        "text": "no dates here",
    }
    assert _parse_chunk_date(chunk) is None


def test_parse_chunk_date_skips_captured_at_for_static() -> None:
    """captured_at is ingest time; for static canon it must NOT be used."""
    chunk = {
        "metadata": {},
        "freshness_tier": "static",
        "captured_at": "2026-05-01T00:00:00Z",
        "text": "",
        "source_url": "",
    }
    assert _parse_chunk_date(chunk) is None
