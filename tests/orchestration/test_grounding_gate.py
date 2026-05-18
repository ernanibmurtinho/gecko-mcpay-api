"""S36-#106 — tests for the numeric grounding-or-abstain gate.

The gate scans every numeric claim in a verdict against the EXACT citation
snippet text the rubric judge sees. A claim grounded in a snippet passes
the judge by construction; an ungrounded claim is flagged and, when the
grounded fraction is weak, the verdict abstains (confidence floored + a
blocker appended).

Pure-function tests: no LLM, no rubric run, no DB — light fakes only.
"""

from __future__ import annotations

from gecko_core.orchestration.trade_panel.grounding_gate import (
    apply_grounding_gate,
    check_grounding,
    extract_numeric_claims,
)
from gecko_core.orchestration.trade_panel.models import (
    Citation,
    TradePanelTurn,
    TradePanelVerdict,
)


def _cite(idx: int, snippet: str) -> Citation:
    return Citation(
        id=idx,
        source="protocol_native",
        url=f"https://x.test/{idx}",
        chunk_id=f"chunk-{idx}",
        provider_kind="protocol_native",
        freshness_tier="daily",
        snippet=snippet,
    )


def _verdict(
    *,
    key_drivers: list[str] | None = None,
    turns: list[TradePanelTurn] | None = None,
    evidence: list[Citation] | None = None,
    confidence: float = 0.8,
) -> TradePanelVerdict:
    return TradePanelVerdict(
        verdict="act",
        confidence=confidence,
        key_drivers=key_drivers or [],
        turns=turns or [],
        evidence_citations=evidence or [],
    )


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


class TestExtraction:
    def test_extracts_percent_and_dollar_suffix(self) -> None:
        claims = extract_numeric_claims("APY of 7.42% and TVL of $12.5M", surface="t")
        raws = {c.raw for c in claims}
        assert "7.42%" in raws
        assert "$12.5M" in raws

    def test_extracts_precise_decimal_and_scientific(self) -> None:
        claims = extract_numeric_claims("funding rate 0.000575916 (8.8785e-06 SOL)")
        raws = {c.raw for c in claims}
        assert "0.000575916" in raws
        assert "8.8785e-06" in raws

    def test_years_are_not_claims(self) -> None:
        claims = extract_numeric_claims("the October 2025 flash-crash")
        assert all(c.raw != "2025" for c in claims)

    def test_empty_text_yields_nothing(self) -> None:
        assert extract_numeric_claims("") == []


# ---------------------------------------------------------------------------
# grounding — gate reads the citation snippet, the judge's exact text
# ---------------------------------------------------------------------------


class TestGrounding:
    def test_grounded_figure_passes(self) -> None:
        # Number-first snippet — the figure is visible to the judge.
        verdict = _verdict(
            key_drivers=["The JitoSOL-SOL vault yields 7.42% APY"],
            evidence=[_cite(1, "Kamino JitoSOL-SOL Vault: APY 7.42%, TVL 12.5M.")],
        )
        report = check_grounding(verdict)
        assert report.claims_total >= 1
        assert report.ungrounded == []
        assert report.grounded_fraction == 1.0
        assert not report.abstained

    def test_ungrounded_figure_is_flagged(self) -> None:
        # The verdict cites a number that appears in NO snippet — the
        # genuine-invented-number failure mode (diagnosis class 3).
        verdict = _verdict(
            key_drivers=["Historical 26.12% APY during the October flash-crash"],
            evidence=[_cite(1, "Sanctum reserve endpoint — describes instant unstake.")],
        )
        report = check_grounding(verdict)
        assert any(c.raw == "26.12%" for c in report.ungrounded)

    def test_fuzzy_tolerance_grounds_a_paraphrase(self) -> None:
        # Panel paraphrases 7.42% as 7.4% — within 1% tolerance.
        verdict = _verdict(
            key_drivers=["roughly 7.4% APY"],
            evidence=[_cite(1, "Kamino vault: APY 7.42%.")],
        )
        report = check_grounding(verdict)
        assert report.ungrounded == []

    def test_gate_reads_snippet_not_full_chunk(self) -> None:
        # Self-consistency: the gate must NOT ground against text the judge
        # cannot see. A figure present only in a (hypothetical) full chunk
        # but absent from the snippet must be flagged ungrounded — the
        # snippet is the only text the gate is allowed to read.
        verdict = _verdict(
            key_drivers=["TVL is $854.93M"],
            evidence=[_cite(1, "Jupiter LST snapshot endpoint description, no figures.")],
        )
        report = check_grounding(verdict)
        assert any(c.raw == "$854.93M" for c in report.ungrounded)

    def test_no_numeric_claims_is_fully_grounded(self) -> None:
        verdict = _verdict(key_drivers=["qualitative driver, no numbers"])
        report = check_grounding(verdict)
        assert report.claims_total == 0
        assert report.grounded_fraction == 1.0
        assert not report.abstained

    def test_claims_in_turn_content_are_scanned(self) -> None:
        verdict = _verdict(
            turns=[TradePanelTurn(agent="fundamental_analyst", content="cites $999M TVL")],
            evidence=[_cite(1, "endpoint description with no numbers")],
        )
        report = check_grounding(verdict)
        assert any(c.raw == "$999M" for c in report.ungrounded)


# ---------------------------------------------------------------------------
# abstain action
# ---------------------------------------------------------------------------


class TestAbstain:
    def test_weak_grounding_abstains_and_floors_confidence(self) -> None:
        # Both figures ungrounded → grounded_fraction 0.0 → abstain.
        verdict = _verdict(
            key_drivers=["TVL $500M", "APY 99%"],
            evidence=[_cite(1, "endpoint description, no figures")],
            confidence=0.8,
        )
        adjusted, report = apply_grounding_gate(verdict)
        assert report.abstained
        assert adjusted.confidence < 0.8
        # An explicit blocker names the ungrounded figures.
        assert any("Grounding gate" in q for q in adjusted.blocker_questions)
        assert any("$500M" in q for q in adjusted.blocker_questions)

    def test_fully_grounded_verdict_is_untouched(self) -> None:
        verdict = _verdict(
            key_drivers=["APY 7.42%"],
            evidence=[_cite(1, "Kamino vault: APY 7.42%.")],
            confidence=0.8,
        )
        adjusted, report = apply_grounding_gate(verdict)
        assert not report.abstained
        assert adjusted.confidence == 0.8
        assert adjusted.blocker_questions == verdict.blocker_questions

    def test_abstain_does_not_flip_the_verdict_literal(self) -> None:
        verdict = _verdict(
            key_drivers=["TVL $500M", "APY 99%"],
            evidence=[_cite(1, "no figures here")],
        )
        adjusted, _report = apply_grounding_gate(verdict)
        # The gate signals distrust via confidence + blocker, never by
        # rewriting the panel's KILL/REFINE/BUILD decision.
        assert adjusted.verdict == verdict.verdict
