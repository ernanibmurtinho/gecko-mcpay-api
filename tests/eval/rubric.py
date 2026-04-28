"""4-axis rubric for scoring Pro tier debate transcripts.

Two paths:
  1. Mock mode (default): a deterministic scorer that keys off lexical
     markers. Repeatable, $0, used in CI to validate the rubric itself.
  2. Live mode: GPT-4o judge, temp=0, JSON-mode, pinned snapshot. Used
     manually when calibrating prompts after meaningful agent changes.

Why a deterministic mock scorer rather than just the live judge with
recorded fixtures: we want CI to fail when the rubric *itself* is broken
(weights wrong, axis mis-keyed). Recorded fixtures would just be replaying
yesterday's noise.

The 4 axes (each 0.0-10.0):
  - agent_voice: each of the 5 agents stays in persona
  - source_grounding: claims are tied to retrieved sources
  - verdict_justification: judge's call follows from the debate
  - cost_predictability: per-turn tokens within ±20% of the cohort median
"""

from __future__ import annotations

import json
import os
import re
import statistics
from dataclasses import dataclass
from typing import Literal

from tests.eval.mocks import MockTranscript

JUDGE_MODEL = "gpt-4o-2024-11-20"  # pinned snapshot for reproducibility


@dataclass(frozen=True)
class RubricScores:
    agent_voice: float
    source_grounding: float
    verdict_justification: float
    cost_predictability: float

    def median(self) -> float:
        return statistics.median(
            [
                self.agent_voice,
                self.source_grounding,
                self.verdict_justification,
                self.cost_predictability,
            ]
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "agent_voice": self.agent_voice,
            "source_grounding": self.source_grounding,
            "verdict_justification": self.verdict_justification,
            "cost_predictability": self.cost_predictability,
        }


Verdict = Literal["kill", "ship", "pivot", "unknown"]


def extract_verdict(judge_text: str) -> Verdict:
    """Parse the judge's verdict from free-form text.

    Looks for "Verdict:" or a leading verb. Falls back to lexical markers.
    Order matters — `pivot` is checked first because the analyst sometimes
    says "kill" inside a pivot recommendation.
    """
    text = judge_text.lower()
    m = re.search(r"verdict\s*:\s*(\w+)", text)
    if m:
        word = m.group(1)
        if word.startswith("kill"):
            return "kill"
        if word.startswith("ship") or word.startswith("build"):
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


def score_transcript_mock(transcript: MockTranscript, cohort_median_tokens: float) -> RubricScores:
    """Deterministic mock scorer — no LLM, no network."""
    return RubricScores(
        agent_voice=_score_agent_voice(transcript),
        source_grounding=_score_source_grounding(transcript),
        verdict_justification=_score_verdict_justification(transcript),
        cost_predictability=_score_cost_predictability(transcript, cohort_median_tokens),
    )


# --- Live-mode GPT-4o judge ---


_JUDGE_PROMPT = """\
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

Return ONLY JSON with these exact keys:
{"agent_voice": 0.0, "source_grounding": 0.0, "verdict_justification": 0.0, "cost_predictability": 0.0}

Transcript:
"""


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


def score_transcript_live(transcript: MockTranscript) -> RubricScores:
    """Call GPT-4o as the judge. Requires OPENAI_API_KEY.

    Note: cohort_median_tokens isn't passed because the live judge is
    asked to assess intra-transcript balance, not cohort-relative cost.
    This is a known divergence between mock and live axis-4 semantics —
    documented in tests/eval/README.md.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set; live rubric requires it. "
            "Run without --live to use the deterministic mock scorer."
        )

    # Lazy import — keep mock-mode path zero-dep on the openai SDK at import.
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    body = _JUDGE_PROMPT + _format_transcript_for_judge(transcript)
    resp = client.chat.completions.create(
        model=JUDGE_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": body}],
    )
    raw = resp.choices[0].message.content or "{}"
    data = json.loads(raw)
    return RubricScores(
        agent_voice=float(data.get("agent_voice", 0.0)),
        source_grounding=float(data.get("source_grounding", 0.0)),
        verdict_justification=float(data.get("verdict_justification", 0.0)),
        cost_predictability=float(data.get("cost_predictability", 0.0)),
    )
