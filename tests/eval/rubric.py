"""5-axis rubric for scoring Pro tier debate transcripts.

Two paths:
  1. Mock mode (default): a deterministic scorer that keys off lexical
     markers. Repeatable, $0, used in CI to validate the rubric itself.
  2. Live mode: Claude Sonnet 4.6 judge, temp=0, JSON output via tool-use.
     Used manually when calibrating prompts after meaningful agent
     changes. Cross-family judge (Claude judging GPT-family debates)
     reduces same-family bias in the rubric scores.

Why a deterministic mock scorer rather than just the live judge with
recorded fixtures: we want CI to fail when the rubric *itself* is broken
(weights wrong, axis mis-keyed). Recorded fixtures would just be replaying
yesterday's noise.

The 5 axes (each 0.0-10.0):
  - agent_voice: each of the 5 agents stays in persona
  - source_grounding: claims are tied to retrieved sources
  - verdict_justification: judge's call follows from the debate
  - cost_predictability: per-turn tokens within ±20% of the cohort median
  - verdict_correctness: judge's verdict matches the suite's
    `expected_verdict` (10 = exact match, 5 = pivot-vs-kill softening,
    0 = wrong). Computed deterministically from `extract_verdict` against
    `expected_verdict`; never asked of the live LLM judge so it doesn't
    bias the other four axes.
"""

from __future__ import annotations

import os
import re
import statistics
from dataclasses import dataclass
from typing import Literal

from tests.eval.mocks import MockTranscript

# Pinned Claude Sonnet 4.6 snapshot for reproducible judging.
# Decision: A,A,A,B,B (Q5=B) — cross-family judge to avoid OpenAI-family bias.
JUDGE_MODEL = "claude-sonnet-4-6"


@dataclass(frozen=True)
class RubricScores:
    agent_voice: float
    source_grounding: float
    verdict_justification: float
    cost_predictability: float
    # Defaults to 0.0 so legacy callers that construct `RubricScores(...)` with
    # only the original four axes keep type-checking. The runner always passes
    # an explicit value derived from `compute_verdict_correctness`.
    verdict_correctness: float = 0.0

    def median(self) -> float:
        return statistics.median(
            [
                self.agent_voice,
                self.source_grounding,
                self.verdict_justification,
                self.cost_predictability,
                self.verdict_correctness,
            ]
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "agent_voice": self.agent_voice,
            "source_grounding": self.source_grounding,
            "verdict_justification": self.verdict_justification,
            "cost_predictability": self.cost_predictability,
            "verdict_correctness": self.verdict_correctness,
        }


Verdict = Literal["kill", "ship", "pivot", "unknown"]
# S12-EVAL-02 / S17-TONE-01 — native single-token taxonomy. Distinct from
# the legacy `Verdict` Literal above (`ship/kill/pivot/unknown`) so v1
# callers keep their type contract while v2 callers get the new bucket
# set. S17-TONE-01 renamed KILL→PIVOT and BUILD→GO; the type now uses the
# new vocabulary as canonical, and the helpers below normalize legacy
# tokens (KILL/BUILD) onto the new buckets so historical fixtures still
# score correctly.
VerdictV2 = Literal["PIVOT", "REFINE", "GO", "UNKNOWN"]


# S12-EVAL-02 / S17-TONE-01 — extract the post-S11 `Final verdict:
# PIVOT|REFINE|GO` line. Mirrors `gecko_core.workflows._JUDGE_VERDICT_RE`
# so we read the same contract the renderer/MCP/journal already speak.
# Accepts the legacy KILL|BUILD tokens for transcripts captured before
# the rename. Falls back to UNKNOWN when the judge prose doesn't carry a
# v2 token (e.g. mock transcripts pre-dating S11).
_V2_VERDICT_RE = re.compile(
    r"(?im)^\s*(?:final\s+)?verdict\s*[:\-]\s*(PIVOT|REFINE|GO|KILL|BUILD)\b"
)

# Map either-vocabulary-token → canonical S17 bucket.
_V2_NORMALIZE: dict[str, VerdictV2] = {
    "PIVOT": "PIVOT",
    "REFINE": "REFINE",
    "GO": "GO",
    "KILL": "PIVOT",  # legacy → S17 rename
    "BUILD": "GO",  # legacy → S17 rename
}


def extract_verdict_v2(judge_text: str) -> VerdictV2:
    """Parse the judge's `Final verdict: PIVOT|REFINE|GO` line.

    Returns "UNKNOWN" when no token can be inferred. Distinct from
    `extract_verdict` because v1 collapses GO→ship and has no slot for
    REFINE; v2 grading needs all three buckets distinguishable.

    Fallback ladder (in order):
      1. Post-S11/S17 contract: `Final verdict: PIVOT|REFINE|GO` line
         (legacy KILL/BUILD tokens accepted and normalized).
      2. Inline v2 token mention (`\\bPIVOT\\b` / `\\bGO\\b` / `\\bREFINE\\b`,
         plus legacy `\\bKILL\\b` / `\\bBUILD\\b`).
      3. Legacy v1 bridge: parse `extract_verdict` and map SHIP→GO,
         KILL→PIVOT, PIVOT→REFINE. This keeps the deterministic mock-mode
         scorer meaningful for the v2 axis without forcing a regen of all
         canned mock transcripts in `tests/eval/mocks.py`.
    """
    if not judge_text:
        return "UNKNOWN"
    match = _V2_VERDICT_RE.search(judge_text)
    if match is not None:
        return _V2_NORMALIZE[match.group(1).upper()]
    upper = judge_text.upper()
    for token in ("PIVOT", "REFINE", "GO", "KILL", "BUILD"):
        if re.search(rf"\b{token}\b", upper):
            return _V2_NORMALIZE[token]
    # Legacy bridge — map v1 token to its closest v2 bucket. SHIP→GO
    # because the legacy v1 SHIP token maps to the new GO slot per the
    # S11/S17 verdict-surface migration; PIVOT (legacy) → REFINE because
    # PIVOT was historically the soft-kill / soft-ship bucket and REFINE
    # inherits that role. NOTE: legacy v1 PIVOT differs in semantics from
    # the post-S17 PIVOT (which means "don't build as-is" = old KILL).
    legacy = extract_verdict(judge_text)
    if legacy == "ship":
        return "GO"
    if legacy == "kill":
        return "PIVOT"
    if legacy == "pivot":
        return "REFINE"
    return "UNKNOWN"


def compute_verdict_correctness_v2(actual: str, expected: str | None) -> float:
    """Map (actual, expected) v2 verdicts to a 0-10 correctness score.

    Three-bucket scoring per `docs/diagnostics/2026-04-30-rubric-native-verdict-proposal.md`:
      - exact match → 10.0
      - {PIVOT, REFINE} near-match → 5.0 (right intuition, softer framing)
      - {GO, REFINE} near-match → 5.0 (right intuition, harder framing)
      - {PIVOT, GO} mismatch → 0.0 (full disagreement)
      - UNKNOWN → 0.0
      - expected=None → 5.0 (neutral)

    S17-TONE-01: legacy KILL/BUILD inputs are normalized to PIVOT/GO so
    historical eval expectations grade against post-rename runs.
    """
    if expected is None:
        return 5.0
    a = _V2_NORMALIZE.get(actual.strip().upper(), actual.strip().upper())
    e = _V2_NORMALIZE.get(expected.strip().upper(), expected.strip().upper())
    if a == e:
        return 10.0
    if a == "UNKNOWN":
        return 0.0
    pair = {a, e}
    if pair == {"PIVOT", "REFINE"} or pair == {"GO", "REFINE"}:
        return 5.0
    return 0.0


def compute_verdict_correctness(actual: str, expected: str | None) -> float:
    """Map (actual, expected) verdicts to a 0-10 correctness score.

    - Exact match → 10.0.
    - `pivot` predicted when expected is `kill` (or vice versa) → 5.0,
      mirroring the runner's `_normalize_verdict` rule that treats pivot
      as kill-equivalent. The half-credit captures "right intuition,
      softened framing."
    - `unknown` predicted (judge text was unparseable) → 0.0.
    - Anything else → 0.0.

    `expected=None` returns 5.0 (neutral) so callers that don't know
    the expected verdict don't get penalized.
    """
    if expected is None:
        return 5.0
    a = actual.strip().lower()
    e = expected.strip().lower()
    if a == e:
        return 10.0
    if a == "unknown":
        return 0.0
    if {a, e} == {"pivot", "kill"}:
        return 5.0
    return 0.0


def extract_verdict(judge_text: str) -> Verdict:
    """Parse the judge's verdict from free-form text.

    Looks for "Verdict:" or a leading verb. Falls back to lexical markers.
    Order matters — `pivot` is checked first because the analyst sometimes
    says "kill" inside a pivot recommendation.

    Returns the LEGACY v1 bucket (ship/kill/pivot/unknown). S17-TONE-01
    renamed the v2 single-token surface to PIVOT/REFINE/GO; this helper
    keeps speaking v1 for the rubric's mock-mode bridge. The mapping is:
        SHIP/BUILD/GO  -> "ship"
        KILL           -> "kill"  (post-S17 PIVOT also maps here, since
                                   S17 PIVOT == legacy KILL semantically)
        PIVOT (legacy soft-kill bucket) -> "pivot"
    Disambiguation between legacy-PIVOT (soft-kill) and S17-PIVOT
    (hard-redirect) happens at the v2 layer (`extract_verdict_v2`); v1
    callers don't make that distinction.
    """
    text = judge_text.lower()
    m = re.search(r"verdict\s*:\s*(\w+)", text)
    if m:
        word = m.group(1)
        if word.startswith("kill"):
            return "kill"
        if word.startswith("ship") or word.startswith("build") or word == "go":
            return "ship"
        if word.startswith("pivot"):
            return "pivot"
    if "pivot" in text:
        return "pivot"
    if "kill" in text or "do not build" in text or "don't build" in text:
        return "kill"
    if "ship" in text or "build it" in text:
        return "ship"
    return "unknown"


# --- Mock-mode deterministic scorer ---


_AGENT_VOICE_MARKERS: dict[str, tuple[str, ...]] = {
    # Persona-correctness signals. Hand-tuned against the canned transcripts;
    # if you change the mocks you may need to retune these.
    "analyst": ("source", "tam", "market", "demand", "$"),
    "critic": ("kill", "risk", "why", "concern", "wedge"),
    "architect": ("stack", "build", "weekend", "v1", "api", "sdk"),
    "scoper": ("v1", "v2", "v3", "ship", "month"),
    "judge": ("verdict", "ship", "kill", "pivot"),
}


def _score_agent_voice(transcript: MockTranscript) -> float:
    """For each agent, count how many persona markers appear in their turn.

    Score = mean(per_agent_marker_hit_rate) * 10. An agent that hits all
    its markers scores 10; missing all scores 0. Lightly capped at 10.
    """
    per_agent: list[float] = []
    for agent, markers in _AGENT_VOICE_MARKERS.items():
        turn = transcript.get(agent)
        if turn is None:
            per_agent.append(0.0)
            continue
        text = turn["text"].lower()
        hits = sum(1 for m in markers if m in text)
        per_agent.append(min(hits / len(markers), 1.0))
    return round(min(10.0, statistics.mean(per_agent) * 10.0), 2)


_SOURCE_PATTERNS = (
    re.compile(r"\(source[^)]*\)", re.I),
    re.compile(r"https?://"),
    re.compile(r"\bs-?1\b", re.I),
    re.compile(r"\b\d{4}\b"),  # e.g. "2024"
)


def _score_source_grounding(transcript: MockTranscript) -> float:
    """Count source-like markers across all 5 turns.

    >=4 hits ~ 10. 0 hits = 0. Linear in between.
    """
    blob = " ".join(turn["text"] for turn in transcript.values())
    hits = sum(len(p.findall(blob)) for p in _SOURCE_PATTERNS)
    return round(min(10.0, hits * 2.5), 2)


def _score_verdict_justification(transcript: MockTranscript) -> float:
    """Judge's verdict should align with critic's stance.

    If critic says "kill" and judge says "kill", score high. If they
    contradict, score low. The mock is calibrated so this fires for the
    20 canned ideas in a way that makes the test reproducible.
    """
    critic_text = transcript.get("critic", {"text": ""})["text"].lower()
    judge_text = transcript.get("judge", {"text": ""})["text"].lower()
    judge_verdict = extract_verdict(judge_text)

    critic_kill = "kill" in critic_text
    critic_ship = "ship" in critic_text or "plausible" in critic_text or "doable" in critic_text

    if judge_verdict == "kill" and critic_kill:
        return 9.0
    if judge_verdict == "ship" and critic_ship:
        return 9.0
    if judge_verdict == "pivot":
        return 6.5
    if judge_verdict == "unknown":
        return 3.0
    # Mismatch
    return 4.5


def _score_cost_predictability(transcript: MockTranscript, cohort_median_tokens: float) -> float:
    """Score = 10 if total tokens within ±20% of cohort median; degrades linearly.

    cohort_median_tokens is the median total (in+out) across all 20 transcripts
    in the run. A transcript at exactly the median scores 10; at 2x or 0.5x
    scores ~0.
    """
    total = sum(t["tokens_in"] + t["tokens_out"] for t in transcript.values())
    if cohort_median_tokens <= 0:
        return 5.0
    deviation = abs(total - cohort_median_tokens) / cohort_median_tokens
    if deviation <= 0.20:
        return 10.0
    # Beyond ±20%, decay to 0 at ±100%.
    score = max(0.0, 10.0 - (deviation - 0.20) * 12.5)
    return round(score, 2)


def score_transcript_mock(
    transcript: MockTranscript,
    cohort_median_tokens: float,
    *,
    expected_verdict: str | None = None,
    expected_verdict_v2: str | None = None,
) -> RubricScores:
    """Deterministic mock scorer — no LLM, no network.

    `expected_verdict` is optional so existing single-axis tests in
    `tests/eval/test_rubric.py` keep their two-arg call signature. When
    omitted, `verdict_correctness` is the neutral 5.0; the runner always
    passes the suite's expected_verdict.

    S12-EVAL-02 — when `expected_verdict_v2` is set, the verdict_correctness
    axis grades against the native KILL/REFINE/BUILD taxonomy. Otherwise it
    falls back to the legacy ship/kill/pivot path. The runner threads v2
    through whenever a fixture has the new field populated.
    """
    judge_text = transcript.get("judge", {"text": ""})["text"]
    if expected_verdict_v2 is not None:
        actual_v2 = extract_verdict_v2(judge_text)
        correctness = compute_verdict_correctness_v2(actual_v2, expected_verdict_v2)
    else:
        actual = extract_verdict(judge_text)
        correctness = compute_verdict_correctness(actual, expected_verdict)
    return RubricScores(
        agent_voice=_score_agent_voice(transcript),
        source_grounding=_score_source_grounding(transcript),
        verdict_justification=_score_verdict_justification(transcript),
        cost_predictability=_score_cost_predictability(transcript, cohort_median_tokens),
        verdict_correctness=correctness,
    )


# --- Live-mode GPT-4o judge ---


_JUDGE_SYSTEM = """\
You are scoring a 5-agent debate transcript for a startup-idea evaluation.
The agents are: analyst, critic, architect, scoper, judge — in that order.

Score on 4 axes, each 0.0-10.0:
  1. agent_voice — does each agent stay in their persona?
     (analyst surveys market, critic is adversarial, architect is concrete,
      scoper carves V1/V2/V3, judge calls it)
  2. source_grounding — are claims tied to retrieved sources, or hand-wavy?
  3. verdict_justification — does the judge's verdict follow from the debate?
  4. cost_predictability — is the per-turn token usage balanced across agents
     (no agent dominates >40% of tokens)?

Use the `submit_scores` tool to return your evaluation. Do not include any
prose outside the tool call.
"""

# Anthropic tool-use forces structured output without prompt-side JSON
# parsing risk — much more reliable than asking for JSON in text.
_SUBMIT_SCORES_TOOL = {
    "name": "submit_scores",
    "description": "Submit the 4-axis rubric scores for the debate transcript.",
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_voice": {"type": "number", "minimum": 0.0, "maximum": 10.0},
            "source_grounding": {"type": "number", "minimum": 0.0, "maximum": 10.0},
            "verdict_justification": {"type": "number", "minimum": 0.0, "maximum": 10.0},
            "cost_predictability": {"type": "number", "minimum": 0.0, "maximum": 10.0},
        },
        "required": [
            "agent_voice",
            "source_grounding",
            "verdict_justification",
            "cost_predictability",
        ],
    },
}


def _format_transcript_for_judge(transcript: MockTranscript) -> str:
    parts = []
    for agent in ("analyst", "critic", "architect", "scoper", "judge"):
        turn = transcript.get(agent)
        if turn is None:
            continue
        parts.append(
            f"--- {agent} (in={turn['tokens_in']}, out={turn['tokens_out']}) ---\n{turn['text']}"
        )
    return "\n\n".join(parts)


def score_transcript_live(
    transcript: MockTranscript,
    *,
    expected_verdict: str | None = None,
    expected_verdict_v2: str | None = None,
) -> RubricScores:
    """Call Claude Sonnet 4.6 as the judge. Requires ANTHROPIC_API_KEY.

    Note: cohort_median_tokens isn't passed because the live judge is
    asked to assess intra-transcript balance, not cohort-relative cost.
    This is a known divergence between mock and live axis-4 semantics —
    documented in tests/eval/README.md.

    `verdict_correctness` is computed deterministically (not asked of the
    Anthropic judge) so telling it the expected answer doesn't bias the
    other four axes.
    """
    # Anthropic SDK reads ANTHROPIC_API_KEY by default. Accept CLAUDE_API_KEY
    # as a fallback alias because some local .env conventions use the
    # provider-neutral "CLAUDE" name.
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY (or CLAUDE_API_KEY) is not set; live rubric requires it. "
            "Run without --live to use the deterministic mock scorer."
        )

    # Lazy import — keep mock-mode path zero-dep on the anthropic SDK at import.
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    transcript_body = _format_transcript_for_judge(transcript)
    # Anthropic SDK uses TypedDicts for tools / tool_choice / messages — plain
    # dict literals are runtime-correct but mypy can't infer them as the
    # matching TypedDicts. Ignoring on this one line is cleaner than scattering
    # casts through every literal.
    resp = client.messages.create(  # type: ignore[call-overload]
        model=JUDGE_MODEL,
        max_tokens=512,
        temperature=0.0,
        system=_JUDGE_SYSTEM,
        tools=[_SUBMIT_SCORES_TOOL],
        tool_choice={"type": "tool", "name": "submit_scores"},
        messages=[{"role": "user", "content": transcript_body}],
    )

    # Forced tool_choice means the response always contains a tool_use block.
    tool_use = next(
        (block for block in resp.content if getattr(block, "type", None) == "tool_use"),
        None,
    )
    if tool_use is None:
        raise RuntimeError(
            "Anthropic judge response missing tool_use block; got: "
            f"{[getattr(b, 'type', '?') for b in resp.content]}"
        )
    data = tool_use.input
    if not isinstance(data, dict):
        raise RuntimeError(f"Anthropic judge tool input is not a dict: {type(data).__name__}")
    judge_text = transcript.get("judge", {"text": ""})["text"]
    # S12-EVAL-02 — v2 grading takes precedence when the fixture supplies it.
    if expected_verdict_v2 is not None:
        actual_v2 = extract_verdict_v2(judge_text)
        correctness = compute_verdict_correctness_v2(actual_v2, expected_verdict_v2)
    else:
        actual = extract_verdict(judge_text)
        correctness = compute_verdict_correctness(actual, expected_verdict)
    return RubricScores(
        agent_voice=float(data.get("agent_voice", 0.0)),
        source_grounding=float(data.get("source_grounding", 0.0)),
        verdict_justification=float(data.get("verdict_justification", 0.0)),
        cost_predictability=float(data.get("cost_predictability", 0.0)),
        verdict_correctness=correctness,
    )
