"""Anti-leakage assertions for eval-suite fixtures.

These tests are the structural invariants that make `verdict_accuracy`
measure Judge quality rather than fixture quality. They are deliberately
trivial — if any of them fails, the gate is no longer trustworthy and
must be repaired before merging.

Background: staff-engineer's Apr-2026 review found that every idea in
`general_suite.json` had at least one mock_precedent at similarity ≥ 0.80
matching `expected_verdict`, plus rag_context bullets containing literal
`gecko_precedent: ... KILLED YYYY-MM-DD` answer-key strings. v5.4 hit
verdict_accuracy 1.0 on a trivial baseline ("majority vote of mock_precedents")
because of the leak, not because v5.4 was correct. See
`docs/runbooks/eval-bias-fix.md` for the repair record.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

SUITES_DIR = Path(__file__).parent / "suites"

# All four suite files we own. Holdout is included because the same anti-
# leakage rules apply — the whole point of the holdout is to be a clean
# generalization check.
_SUITE_FILES = (
    "general_suite.json",
    "crypto_suite.json",
    "saas_suite.json",
    "general_holdout_suite.json",
)

# Verdict labels the Judge emits and that, when literally embedded in a
# rag_context bullet, telegraph the answer key. We match these only when
# they appear after `gecko_precedent:` (the historical leak shape).
_VERDICT_LABELS = ("KILLED", "SHIPPED", "SHIP", "KILL", "PIVOT", "PIVOTED")
_LEAKY_PRECEDENT_RE = re.compile(
    r"gecko_precedent\s*:\s*[^.\n]*?\b(?:" + "|".join(_VERDICT_LABELS) + r")\b",
    re.IGNORECASE,
)

_MAX_PRECEDENT_SIMILARITY = 0.75
_MIN_VERDICT_DIVERSITY_RATIO = 0.30


def _load(name: str) -> list[dict[str, object]]:
    path = SUITES_DIR / name
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, list), f"{name} root must be a list"
    return data


@pytest.mark.parametrize("suite_file", _SUITE_FILES)
def test_no_gecko_precedent_verdict_leak_in_rag_context(suite_file: str) -> None:
    """rag_context must not contain `gecko_precedent: ... <VERDICT>` strings."""
    ideas = _load(suite_file)
    offenders: list[str] = []
    for idea in ideas:
        rag = str(idea.get("rag_context", ""))
        if _LEAKY_PRECEDENT_RE.search(rag):
            offenders.append(str(idea.get("id", "<no id>")))
    assert not offenders, (
        f"{suite_file}: rag_context for these ideas leaks the answer key via "
        f"`gecko_precedent: ... <VERDICT>`: {offenders}"
    )


@pytest.mark.parametrize("suite_file", _SUITE_FILES)
def test_mock_precedent_similarity_capped(suite_file: str) -> None:
    """No mock_precedent may exceed similarity 0.75 (was 0.81-0.94 pre-fix)."""
    ideas = _load(suite_file)
    offenders: list[tuple[str, float]] = []
    for idea in ideas:
        for p in idea.get("mock_precedents", []) or []:
            sim = p.get("similarity")
            if sim is None:
                continue
            if float(sim) > _MAX_PRECEDENT_SIMILARITY:
                offenders.append((str(idea.get("id", "<no id>")), float(sim)))
    assert not offenders, (
        f"{suite_file}: mock_precedents above similarity {_MAX_PRECEDENT_SIMILARITY}: {offenders}"
    )


@pytest.mark.parametrize("suite_file", _SUITE_FILES)
def test_mock_precedent_verdict_diversity(suite_file: str) -> None:
    """At least 30% of ideas must have one precedent verdict ≠ expected_verdict.

    This forces the Judge to weight precedents against other signal rather
    than echoing them. Without this, even a similarity-capped suite can
    still be trivially scored by 'majority of precedents.'
    """
    ideas = _load(suite_file)
    if not ideas:
        pytest.skip(f"{suite_file} is empty")

    diverse = 0
    for idea in ideas:
        expected = idea.get("expected_verdict")
        precedents = idea.get("mock_precedents", []) or []
        if any(p.get("verdict") != expected for p in precedents):
            diverse += 1

    ratio = diverse / len(ideas)
    assert ratio >= _MIN_VERDICT_DIVERSITY_RATIO, (
        f"{suite_file}: only {diverse}/{len(ideas)} ideas "
        f"({ratio:.0%}) have a precedent with verdict ≠ expected_verdict; "
        f"required ≥ {_MIN_VERDICT_DIVERSITY_RATIO:.0%}"
    )


def test_holdout_no_proper_noun_overlap_with_gate_suites() -> None:
    """Holdout ideas must not reference proper nouns from the gate suites.

    We use idea `id` and `text` as the canonical surface form. The check
    is conservative — substring match on a curated list of distinctive
    proper nouns from the original brief. If a future suite update wants
    to reuse one of these names, update both the suite and this list.
    """
    holdout_ids = {str(i["id"]) for i in _load("general_holdout_suite.json")}
    holdout_texts = " ".join(str(i.get("text", "")) for i in _load("general_holdout_suite.json"))

    forbidden = (
        # From general_suite (originals)
        "Carta",
        "Vet-tele-Rx",
        "Devbrief",
        "Part 107",
        "Stripe",
        "Postgres",
        "Airtable",
        "ClinicalTrials.gov",
        "NIH",
        # From saas_suite (originals)
        "LaunchDarkly",
        "Datadog",
        # From crypto_suite (originals)
        "Echo",
        "Helius",
        "Kamino",
    )
    hits = [
        n
        for n in forbidden
        if n.lower() in holdout_texts.lower() or any(n.lower() in i.lower() for i in holdout_ids)
    ]
    assert not hits, (
        f"holdout suite text/ids reference proper nouns from gate suites "
        f"(must be isomorphic but distinct): {hits}"
    )
