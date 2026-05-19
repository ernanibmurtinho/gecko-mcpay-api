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
# redaction action — S37-WS1a: ungrounded figures are SCRUBBED from the
# verdict text (rephrase when a direction can be grounded, else drop the
# clause), and confidence is NOT multiplied down.
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_ungrounded_figure_is_removed_from_key_drivers(self) -> None:
        # The fabricated number must not survive in the verdict text — the
        # judge reads turns[]/key_drivers and penalizes any figure it sees.
        verdict = _verdict(
            key_drivers=["TVL $500M", "APY 99%"],
            evidence=[_cite(1, "endpoint description, no figures")],
            confidence=0.8,
        )
        adjusted, report = apply_grounding_gate(verdict)
        assert report.abstained
        joined = " ".join(adjusted.key_drivers)
        assert "$500M" not in joined
        assert "99%" not in joined

    def test_groundable_direction_rephrases_the_number(self) -> None:
        # The snippet says "elevated" — so the ungrounded 26.12% may become
        # a grounded qualitative descriptor rather than being dropped.
        verdict = _verdict(
            key_drivers=["APY of 26.12% looks attractive"],
            evidence=[_cite(1, "Kamino vault note: yields are currently elevated.")],
            confidence=0.7,
        )
        adjusted, report = apply_grounding_gate(verdict)
        assert report.abstained
        joined = " ".join(adjusted.key_drivers)
        assert "26.12%" not in joined
        # The grounded descriptor replaced the number; the driver survives.
        assert "elevated" in joined
        assert adjusted.key_drivers, "rephrased driver should not be dropped"

    def test_no_groundable_direction_drops_the_clause(self) -> None:
        # No directional word in any snippet → never invent an adjective →
        # the clause carrying the ungrounded figure is dropped entirely.
        verdict = _verdict(
            key_drivers=["APY of 26.12% looks attractive, and liquidity is deep"],
            evidence=[_cite(1, "Sanctum reserve endpoint — describes instant unstake.")],
            confidence=0.7,
        )
        adjusted, report = apply_grounding_gate(verdict)
        assert report.abstained
        joined = " ".join(adjusted.key_drivers)
        assert "26.12%" not in joined
        # No invented adjective — neither "elevated" nor "high" appears.
        assert "elevated" not in joined
        assert "high" not in joined
        # The clause WITHOUT the figure ("liquidity is deep") survives.
        assert "liquidity is deep" in joined

    def test_ungrounded_figure_removed_from_turn_content(self) -> None:
        verdict = _verdict(
            turns=[TradePanelTurn(agent="strategist", content="The vault holds $999M TVL")],
            evidence=[_cite(1, "endpoint description with no numbers")],
        )
        adjusted, report = apply_grounding_gate(verdict)
        assert report.abstained
        assert all("$999M" not in t.content for t in adjusted.turns)

    def test_confidence_is_not_floored_post_redaction(self) -> None:
        # #116 — the old gate multiplied confidence × grounded_fraction,
        # manufacturing "confidence 0.00 but verdict ACT". Once the figure
        # is scrubbed the text is honest, so confidence is left intact.
        verdict = _verdict(
            key_drivers=["TVL $500M", "APY 99%"],
            evidence=[_cite(1, "endpoint description, no figures")],
            confidence=0.8,
        )
        adjusted, report = apply_grounding_gate(verdict)
        assert report.abstained
        assert adjusted.confidence == 0.8

    def test_blocker_question_still_names_the_redacted_figures(self) -> None:
        # The blocker append is a legitimate audit signal — kept.
        verdict = _verdict(
            key_drivers=["TVL $500M", "APY 99%"],
            evidence=[_cite(1, "endpoint description, no figures")],
            confidence=0.8,
        )
        adjusted, _report = apply_grounding_gate(verdict)
        assert any("Grounding gate" in q for q in adjusted.blocker_questions)
        assert any("$500M" in q for q in adjusted.blocker_questions)

    def test_fully_grounded_verdict_is_untouched(self) -> None:
        verdict = _verdict(
            key_drivers=["APY 7.42%"],
            turns=[TradePanelTurn(agent="strategist", content="APY 7.42% is fair")],
            evidence=[_cite(1, "Kamino vault: APY 7.42%.")],
            confidence=0.8,
        )
        adjusted, report = apply_grounding_gate(verdict)
        assert not report.abstained
        assert adjusted.confidence == 0.8
        assert adjusted.key_drivers == verdict.key_drivers
        assert [t.content for t in adjusted.turns] == [t.content for t in verdict.turns]
        assert adjusted.blocker_questions == verdict.blocker_questions

    def test_abstain_does_not_flip_the_verdict_literal(self) -> None:
        verdict = _verdict(
            key_drivers=["TVL $500M", "APY 99%"],
            evidence=[_cite(1, "no figures here")],
        )
        adjusted, _report = apply_grounding_gate(verdict)
        # The gate scrubs figures + appends a blocker, never rewrites the
        # panel's act/pass/defer decision.
        assert adjusted.verdict == verdict.verdict


class TestSnippetCapDoesNotDrift:
    """S36-#111 regression — the panel truncates a citation snippet to
    ``_CITATION_SNIPPET_LIMIT`` and then constructs a ``Citation``. If the
    field's ``max_length`` validator is below that limit, every snippet in
    the gap raises ``ValidationError`` at construction — which scored the
    #110 N=50 ship-gate as N=0. Both must derive from one constant.
    """

    def test_truncation_limit_equals_validator_max(self) -> None:
        from gecko_core.orchestration.trade_panel import _CITATION_SNIPPET_LIMIT
        from gecko_core.orchestration.trade_panel.models import (
            CITATION_SNIPPET_MAX_LEN,
        )

        # Pattern A — one source of truth, so they cannot drift again.
        assert _CITATION_SNIPPET_LIMIT == CITATION_SNIPPET_MAX_LEN

    def test_citation_accepts_snippet_at_truncation_limit(self) -> None:
        from gecko_core.orchestration.trade_panel import _CITATION_SNIPPET_LIMIT

        # A snippet truncated to the exact limit must construct cleanly.
        cite = _cite(1, "x" * _CITATION_SNIPPET_LIMIT)
        assert len(cite.snippet) == _CITATION_SNIPPET_LIMIT

    def test_citation_accepts_snippet_in_old_dead_zone(self) -> None:
        # 300 chars — inside the old (240, 320] gap that raised
        # ValidationError before #111 unified the cap.
        cite = _cite(2, "y" * 300)
        assert len(cite.snippet) == 300


class TestParsedVerdictRedaction:
    """S37 jito fix — WS1a scrubbed turns[].content + key_drivers but left
    raw figures in turns[].parsed_verdict (the coordinator's `falsifier`,
    the bull/bear debater's `decisive_question`). The judge reads the full
    transcript, so jito-mev-tip-band hard-failed hallucination 0/5 at #122.
    The gate must redact parsed_verdict strings too.
    """

    def test_ungrounded_figure_in_parsed_verdict_is_redacted(self) -> None:
        turn = TradePanelTurn(
            agent="bull_bear_debater",
            content="The tip-floor read is inconclusive.",
            parsed_verdict={
                "decisive_question": "Does the 1.052e-06 SOL tip floor hold 7d?",
                "falsifier": "tip floor climbing past 8.8785e-06 SOL",
            },
        )
        verdict = _verdict(
            key_drivers=["tip band sits near 1.052e-06 SOL"],
            turns=[turn],
            evidence=[_cite(1, "Jito bundle landing telemetry — no tip figures.")],
        )
        adjusted, report = apply_grounding_gate(verdict)
        assert report.abstained
        pv = adjusted.turns[0].parsed_verdict
        assert pv is not None
        # The raw lamport figures must be gone from BOTH parsed_verdict keys.
        assert "1.052e-06" not in pv["decisive_question"]
        assert "8.8785e-06" not in pv["falsifier"]

    def test_grounded_figure_in_parsed_verdict_survives(self) -> None:
        turn = TradePanelTurn(
            agent="coordinator",
            content="P75 tip band is defensible.",
            parsed_verdict={"falsifier": "tip floor drops below 7.42 lamports"},
        )
        verdict = _verdict(
            key_drivers=["P75 band holds at 7.42"],
            turns=[turn],
            evidence=[_cite(1, "Jito tip floor: 75th percentile 7.42 lamports.")],
        )
        adjusted, report = apply_grounding_gate(verdict)
        # Fully grounded — gate does not abstain, parsed_verdict untouched.
        assert not report.abstained
        assert adjusted.turns[0].parsed_verdict == turn.parsed_verdict

    def test_parsed_verdict_none_is_safe(self) -> None:
        turn = TradePanelTurn(agent="strategist", content="APY of 26.12%")
        verdict = _verdict(
            key_drivers=["APY of 26.12%"],
            turns=[turn],
            evidence=[_cite(1, "no figures in this snippet")],
        )
        adjusted, _report = apply_grounding_gate(verdict)
        assert adjusted.turns[0].parsed_verdict is None
