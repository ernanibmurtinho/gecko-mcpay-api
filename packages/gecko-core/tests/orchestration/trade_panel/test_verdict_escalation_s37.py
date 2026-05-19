"""S37-WS2 — coordinator verdict escalation rules.

The #110 N=50 ship-gate came back with `verdict_accuracy` 0.880 — 6 misses.
The #115 diagnosis (docs/eval/2026-05-18-s37-A-verdict-accuracy-diagnosis.md)
traced ALL 6 to coordinator *reasoning* (zero emission bugs, zero parse
bugs) and named two deterministic code rules in
`_build_verdict_from_coordinator`:

  Rule 1 — risk-veto DEFER escalation: risk_manager band `unacceptable` +
           strategist intent says observe/hold/wait + a directional verdict
           → escalate to `defer`.
  Rule 2 — rotation-framing DEFER escalation: rotation / 100% / short
           framing + a `pass` verdict + elevated-or-worse risk → `defer`.

Per repo memory `feedback_prompt_iteration_plateau`, coordinator verdict
logic belongs in CODE, not a persona prompt. These tests are light fakes:
they feed `TradePanelTurn` lists shaped like the four recoverable N=50
misses (recorded `turns[]` parsed_verdict tokens from
tests/eval/live_runs/2026-05-18-s24-defi-rubric-s36-110-shipgate-r{2,5}-10
.json) through `_build_verdict_from_coordinator` directly — zero LLM spend.

The two `jupiter-lst-rotation-msol` rows are an upstream panel-reasoning
gap and are explicitly OUT of scope here; one test pins that they are
NOT mis-escalated by these rules.
"""

from __future__ import annotations

from gecko_core.orchestration.trade_panel import _build_verdict_from_coordinator
from gecko_core.orchestration.trade_panel.models import TradePanelTurn


def _coord_content(verdict: str, frame: str = "") -> str:
    """A minimal coordinator turn: a prose body + a json block + a closing
    line, all agreeing on ``verdict``. ``frame`` lets a test embed the
    research-question framing the coordinator prose restates."""
    body = f"The panel reviewed the proposal. {frame}".strip()
    return (
        f"{body}\n\n"
        "```json\n"
        "{\n"
        f'  "verdict": "{verdict}",\n'
        '  "confidence": 0.65,\n'
        '  "key_drivers": ["panel review"],\n'
        '  "dissent_count": 0,\n'
        '  "blocker_questions": []\n'
        "}\n"
        "```\n\n"
        f"Final verdict: {verdict}"
    )


def _turns(
    *,
    trend: str,
    sentiment: str,
    health: str,
    risk: str,
    strategist_intent: str,
    decisive_q: str,
    coord_verdict: str,
    coord_frame: str = "",
) -> list[TradePanelTurn]:
    """Build a 7-turn panel from parsed-verdict tokens — the recorded shape."""
    return [
        TradePanelTurn(
            agent="technical_analyst",
            content=f"Trend verdict: {trend}",
            parsed_verdict={"trend_verdict": trend},
        ),
        TradePanelTurn(
            agent="sentiment_analyst",
            content=f"Sentiment band: {sentiment}",
            parsed_verdict={"sentiment_band": sentiment},
        ),
        TradePanelTurn(
            agent="fundamental_analyst",
            content=f"Protocol health: {health}",
            parsed_verdict={"protocol_health": health},
        ),
        TradePanelTurn(
            agent="risk_manager",
            content=f"Risk band: {risk}",
            parsed_verdict={"risk_band": risk},
        ),
        TradePanelTurn(
            agent="strategist",
            content=f"Strategic intent: {strategist_intent}",
            parsed_verdict={"strategic_intent": strategist_intent},
        ),
        TradePanelTurn(
            agent="bull_bear_debater",
            content=f"Decisive question: {decisive_q}",
            parsed_verdict={"decisive_question": decisive_q},
        ),
        TradePanelTurn(
            agent="coordinator",
            content=_coord_content(coord_verdict, coord_frame),
            parsed_verdict={"verdict": coord_verdict},
        ),
    ]


# --- Rule 1 — risk-veto DEFER escalation (2 of 4 target misses) ----------


def test_rule1_recovers_kamino_sol_leverage_entry() -> None:
    """r2 `kamino-sol-leverage-entry`: got PASS, expected {ACT, DEFER}.
    risk=unacceptable + strategist 'observe ... pending improvements in
    risk profile' — Rule 1 escalates the lone-veto PASS to DEFER."""
    turns = _turns(
        trend="bullish",
        sentiment="neutral",
        health="stable",
        risk="unacceptable",
        strategist_intent=(
            "observe the market for Kamino and SOL, pending improvements in risk profile."
        ),
        decisive_q=("Does Kamino's protocol undergo a successful audit within the next 30 days?"),
        coord_verdict="pass",
    )
    verdict = _build_verdict_from_coordinator(turns)
    assert verdict.verdict == "defer"
    assert any("unacceptable" in b for b in verdict.blocker_questions)


def test_rule1_recovers_drift_jto_perp_short() -> None:
    """r2 `drift-jto-perp-short`: got PASS, expected {ACT, DEFER}.
    risk=unacceptable + strategist 'observe ... for 21 days' — Rule 1
    escalates. (Rule 2's short framing would also fire; Rule 1 runs
    first and the result is the same DEFER.)"""
    turns = _turns(
        trend="bearish",
        sentiment="neutral",
        health="stable",
        risk="unacceptable",
        strategist_intent=(
            "observe JTO-PERP on Drift for 21 days due to unacceptable risk factors."
        ),
        decisive_q="Does the price of JTO-PERP decline by more than 10%?",
        coord_verdict="pass",
        coord_frame="The proposal is to short JTO-PERP on Drift.",
    )
    verdict = _build_verdict_from_coordinator(turns)
    assert verdict.verdict == "defer"


# --- Rule 2 — rotation-framing DEFER escalation (2 of 4 target misses) ---


def test_rule2_recovers_jupiter_jlp_vs_jitosol_r2() -> None:
    """r2 `jupiter-jlp-vs-jitosol`: got PASS, expected {ACT, DEFER}.
    Strategist 'hold JitoSOL for now, monitoring ... for potential future
    rotation' + risk=elevated — Rule 2 escalates the silently-rejected
    rotation to DEFER (flag the concentration call)."""
    turns = _turns(
        trend="bearish",
        sentiment="fear",
        health="stable",
        risk="elevated",
        strategist_intent=(
            "hold JitoSOL for now, monitoring liquidity and market "
            "conditions for potential future rotation."
        ),
        decisive_q="Does JLP demonstrate sufficient liquidity?",
        coord_verdict="pass",
        coord_frame=(
            "The question is whether to rotate 100% of a JitoSOL stack into JLP for 30 days."
        ),
    )
    verdict = _build_verdict_from_coordinator(turns)
    assert verdict.verdict == "defer"
    assert any("oncentration" in b for b in verdict.blocker_questions)


def test_rule2_recovers_jupiter_jlp_vs_jitosol_r5() -> None:
    """r5 `jupiter-jlp-vs-jitosol`: got PASS, expected {ACT, DEFER}.
    Strategist intent itself names 'rotation'; risk=elevated — Rule 2
    fires off the strategist token even before the coordinator prose."""
    turns = _turns(
        trend="bearish",
        sentiment="fear",
        health="stable",
        risk="elevated",
        strategist_intent=(
            "observe JitoSOL and JLP for potential rotation, days "
            "horizon — falsifier: JitoSOL price recovers significantly."
        ),
        decisive_q="Does JitoSOL's price recover above $110?",
        coord_verdict="pass",
        coord_frame="Proposal: rotate 100% of a JitoSOL stack into JLP.",
    )
    verdict = _build_verdict_from_coordinator(turns)
    assert verdict.verdict == "defer"


# --- Out-of-scope: jupiter-lst-rotation-msol must NOT be mis-escalated ---


def test_lst_rotation_msol_not_escalated_act_is_left_alone() -> None:
    """r1/r5 `jupiter-lst-rotation-msol`: panel emitted ACT. This is an
    upstream panel-reasoning gap (points-narrative ≠ cash) explicitly OUT
    of scope for WS2. risk=elevated + strategist 'open small long' — no
    observe/hold token, verdict is ACT not PASS — so neither rule fires
    and ACT is left untouched (no over-escalation)."""
    turns = _turns(
        trend="bullish",
        sentiment="greed",
        health="stable",
        risk="elevated",
        strategist_intent=(
            "open small long, tight stop, weeks horizon — falsifier: bSOL price drops below $100."
        ),
        decisive_q="Does bSOL's price remain above $100?",
        coord_verdict="act",
    )
    verdict = _build_verdict_from_coordinator(turns)
    # Rule 1 needs risk=unacceptable (this is elevated); Rule 2 needs a
    # `pass` verdict (this is act). Neither fires — ACT stands.
    assert verdict.verdict == "act"


# --- Over-escalation guards — the rules must NOT touch legitimate calls --


def test_rule1_does_not_fire_when_strategist_proposes_a_concrete_entry() -> None:
    """A genuine PASS where risk=unacceptable but the strategist did NOT
    say observe/hold/wait must stay PASS — Rule 1 is gated on the
    strategist's own non-action token, not on the risk band alone."""
    turns = _turns(
        trend="bearish",
        sentiment="fear",
        health="degraded",
        risk="unacceptable",
        strategist_intent="close the position and exit immediately.",
        decisive_q="Does the protocol survive the audit?",
        coord_verdict="pass",
    )
    verdict = _build_verdict_from_coordinator(turns)
    # No observe/hold/wait/monitor token in the strategist intent — Rule 1
    # holds; the PASS is the calibrated reject.
    assert verdict.verdict == "pass"


def test_rule2_does_not_fire_on_a_legitimate_act() -> None:
    """A rotation-framed question the panel affirmatively endorses (ACT)
    is NOT escalated — Rule 2 is gated on `pass` only. An ACT is the
    panel endorsing the rotation; it must be left untouched."""
    turns = _turns(
        trend="bullish",
        sentiment="greed",
        health="growing",
        risk="elevated",
        strategist_intent="rotate into JLP now, normal stop, weeks horizon.",
        decisive_q="Does JLP liquidity hold?",
        coord_verdict="act",
        coord_frame="Proposal: rotate 100% of the stack into JLP.",
    )
    verdict = _build_verdict_from_coordinator(turns)
    assert verdict.verdict == "act"


def test_rule2_does_not_fire_on_low_risk_rotation() -> None:
    """A rotation-framed PASS where risk is only `acceptable` stays PASS —
    Rule 2's elevated-risk gate is what keeps it from blanket-converting
    every declined rotation into a DEFER."""
    turns = _turns(
        trend="bearish",
        sentiment="fear",
        health="stable",
        risk="acceptable",
        strategist_intent="hold the current stack; no rotation warranted.",
        decisive_q="Does momentum return?",
        coord_verdict="pass",
        coord_frame="Proposal: rotate 100% into JLP.",
    )
    verdict = _build_verdict_from_coordinator(turns)
    assert verdict.verdict == "pass"


def test_non_rotation_pass_with_elevated_risk_stays_pass() -> None:
    """A plain long-entry PASS with elevated risk and no rotation/short
    framing is untouched — Rule 2 requires the rotation framing token."""
    turns = _turns(
        trend="bearish",
        sentiment="fear",
        health="stable",
        risk="elevated",
        strategist_intent="decline the entry; wait for a cleaner setup.",
        decisive_q="Does the trend reverse?",
        coord_verdict="pass",
        coord_frame="Proposal: open a small SOL long on Kamino.",
    )
    verdict = _build_verdict_from_coordinator(turns)
    # 'wait' is a non-action token but Rule 1 needs risk=unacceptable, and
    # Rule 2 needs rotation framing — neither is present. PASS stands.
    assert verdict.verdict == "pass"
