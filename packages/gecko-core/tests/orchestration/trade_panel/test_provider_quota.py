"""S27 #21 — unit tests for the provider_kind quota allocator.

Pure-function tests over `_provider_quota_floor`. No Mongo, no LLM,
no autogen. Per CLAUDE.md feedback_lighter_tests: ≤5 fixtures per
test, no monkeypatch.setattr churn — the helper is pure data-in /
data-out so we exercise it directly.

Why these matter: the S26 lost-in-middle rubric showed
`provider_kinds_present=['protocol_native']` on 2 of 3 Kamino
fixtures because S25 #13's boost stacks +0.25 on protocol_native and
the straight top_k slice starved canon/market_data. The quota floor
is the fix; these tests pin its behavior.
"""

from __future__ import annotations

from gecko_core.orchestration.trade_panel import (
    _PROVIDER_QUOTAS,
    _provider_quota_floor,
)


def _row(
    *,
    score: float,
    provider_kind: str,
    text: str = "x" * 400,  # default to substantive text (>200 chars)
) -> dict:
    return {
        "score": score,
        "provider_kind": provider_kind,
        "text": text,
    }


def test_quota_fills_all_buckets_when_supply_sufficient() -> None:
    """Each provider_kind has enough chunks to fill its quota; the
    allocator should return exactly the per-bucket quota for each kind
    (up to top_k). protocol_native (+0.25 raw boost in production) must
    not steal slots from canon."""
    rows = [
        # protocol_native heavily boosted; would normally dominate.
        *[_row(score=1.00 - i * 0.01, provider_kind="protocol_native") for i in range(10)],
        # canon supply.
        *[_row(score=0.80 - i * 0.01, provider_kind="canon_marks") for i in range(5)],
        *[_row(score=0.78 - i * 0.01, provider_kind="canon_damodaran") for i in range(5)],
        *[_row(score=0.76 - i * 0.01, provider_kind="canon_berkshire") for i in range(3)],
        *[_row(score=0.85 - i * 0.01, provider_kind="market_data") for i in range(3)],
        *[_row(score=0.82, provider_kind="bazaar_live") for _ in range(2)],
    ]
    picked = _provider_quota_floor(rows, top_k=15)

    counts: dict[str, int] = {}
    for r in picked:
        counts[r["provider_kind"]] = counts.get(r["provider_kind"], 0) + 1

    assert len(picked) == 15
    # Per-bucket caps respected. protocol_native must not exceed its quota
    # of 5 even though it has 10 candidates and the highest scores.
    assert counts["protocol_native"] == 5
    assert counts["canon_marks"] == 3
    assert counts["canon_damodaran"] == 3
    assert counts["canon_berkshire"] == 2
    assert counts["market_data"] == 2
    # That sums to 15 — no room for bazaar_live's quota of 1.
    # Acceptable: the higher-quota buckets fill first; bazaar gets squeezed
    # when total demand exceeds top_k.
    assert counts.get("bazaar_live", 0) == 0


def test_empty_paysh_live_overflows_to_other_buckets() -> None:
    """paysh_live bucket has only empty `{"data":[]}` short chunks
    (production reality for kamino: 7 chunks, 0 substantive). The
    quota slot must overflow to a substantive bucket instead of
    surfacing an empty chunk."""
    rows = [
        # 1 empty paysh_live (text below the 200-char threshold).
        _row(score=0.95, provider_kind="paysh_live", text='{"data":[]}'),
        # Plenty of canon.
        *[_row(score=0.80 - i * 0.01, provider_kind="canon_marks") for i in range(8)],
        *[_row(score=0.78 - i * 0.01, provider_kind="canon_damodaran") for i in range(8)],
    ]
    picked = _provider_quota_floor(rows, top_k=5)
    kinds = [r["provider_kind"] for r in picked]
    assert "paysh_live" not in kinds  # empty chunk filtered out
    assert len(picked) == 5
    # Slots reallocated to canon via overflow.
    assert kinds.count("canon_marks") + kinds.count("canon_damodaran") == 5


def test_total_demand_under_top_k_overflows_by_global_score() -> None:
    """Sum of available quotas < top_k → remaining slots fill from
    the global pool by score, regardless of bucket. Highest-scored
    extras win, even if they're from an already-full bucket."""
    rows = [
        # 6 protocol_native (quota=5 → 1 leftover surfaces via overflow).
        *[_row(score=0.95 - i * 0.01, provider_kind="protocol_native") for i in range(6)],
        # 1 canon_marks (quota=3 → bucket short).
        _row(score=0.50, provider_kind="canon_marks"),
        # Nothing else.
    ]
    picked = _provider_quota_floor(rows, top_k=10)
    counts: dict[str, int] = {}
    for r in picked:
        counts[r["provider_kind"]] = counts.get(r["provider_kind"], 0) + 1
    # Got every available row (7 total < top_k=10).
    assert len(picked) == 7
    # protocol_native got its quota of 5 + the 1 overflow = 6.
    assert counts["protocol_native"] == 6
    assert counts["canon_marks"] == 1


def test_within_bucket_ordering_preserves_boost_winner() -> None:
    """Within a single bucket, the highest-scored chunk wins. Boost
    decisions made by _apply_retrieval_boosts must be honored — the
    quota allocator only constrains *who* gets in, not relative rank."""
    rows = [
        _row(score=0.95, provider_kind="canon_marks", text="HIGH" * 100),
        _row(score=0.50, provider_kind="canon_marks", text="LOW" * 100),
        _row(score=0.70, provider_kind="canon_marks", text="MID" * 100),
    ]
    # quota for canon_marks is 3 → all three picked; verify order.
    picked = _provider_quota_floor(rows, top_k=3)
    assert [r["score"] for r in picked] == [0.95, 0.70, 0.50]


def test_short_chunks_fill_last_resort_when_corpus_thin() -> None:
    """If the eligible pool is smaller than top_k, ineligible (short-text)
    chunks can fill out the rest so the panel always sees top_k slots."""
    rows = [
        _row(score=0.90, provider_kind="canon_marks"),  # eligible, 400 chars
        _row(score=0.85, provider_kind="paysh_live", text='{"data":[]}'),  # short
        _row(score=0.80, provider_kind="bazaar_live", text="ok"),  # short
    ]
    picked = _provider_quota_floor(rows, top_k=3)
    assert len(picked) == 3
    # Strongest eligible chunk is at position [0]; short chunks fall in
    # by score behind it.
    assert picked[0]["provider_kind"] == "canon_marks"


def test_quota_table_matches_documented_total() -> None:
    """Pin the V1 quota table so future edits trigger a test break and
    a deliberate decision. Sum is intentionally > top_k(15) so the
    highest-quota buckets win when total demand exceeds the budget."""
    assert sum(_PROVIDER_QUOTAS.values()) == 19
    assert _PROVIDER_QUOTAS["protocol_native"] == 5
    assert _PROVIDER_QUOTAS["canon_marks"] == 3
    assert _PROVIDER_QUOTAS["canon_damodaran"] == 3
    assert _PROVIDER_QUOTAS["canon_berkshire"] == 2
    assert _PROVIDER_QUOTAS["market_data"] == 2
