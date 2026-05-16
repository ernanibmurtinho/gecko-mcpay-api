"""S24 WS-F #1 — semantic ``idea_hash`` equivalence.

Five+ paraphrase pairs must collapse to the same hash. The synonym table
isn't full NLP; it's dimensional normalisation over (protocol, vertical,
intent, size_bucket, horizon_bucket). A pair drifts apart only when one
side carries a distinguishing dimension (different protocol, different
size band, etc.).
"""

from __future__ import annotations

import pytest
from gecko_core.trade_agent.oracle import _semantic_dimensions, idea_hash

PARAPHRASE_PAIRS: list[tuple[str, str]] = [
    # 1. The canonical example from the task brief.
    (
        "deposit USDC into Kamino reserve right now?",
        "should I put my USDC into Kamino reserve today?",
    ),
    # 2. Jupiter swap intent, two phrasings.
    (
        "swap SOL for USDC on Jupiter now",
        "trade my SOL into USDC via Jup at the moment",
    ),
    # 3. Drift perps long.
    (
        "open a long on Drift perps for a few days",
        "go long on Drift futures, short-term hold",
    ),
    # 4. Marginfi lending — abbreviation collapse.
    (
        "lend USDC on MarginFi this week",
        "supply USDC to MFI, near-term horizon",
    ),
    # 5. Jito LST staking.
    (
        "stake my SOL with Jito long-term",
        "liquid staking SOL via Jito for months",
    ),
    # 6. Bonus pair — Raydium swap with size phrasings.
    (
        "swap a small amount on Raydium today",
        "tiny trade on Ray right now",
    ),
]


@pytest.mark.parametrize("a,b", PARAPHRASE_PAIRS)
def test_paraphrases_hash_equal(a: str, b: str) -> None:
    ha, hb = idea_hash(a), idea_hash(b)
    assert ha == hb, (
        f"paraphrase pair drifted:\n"
        f"  A: {a!r} -> dims={_semantic_dimensions(a)} hash={ha}\n"
        f"  B: {b!r} -> dims={_semantic_dimensions(b)} hash={hb}"
    )


def test_different_protocols_hash_differently() -> None:
    # Sanity check: the normaliser isn't collapsing everything.
    assert idea_hash("deposit USDC into Kamino now") != idea_hash("deposit USDC into MarginFi now")


def test_different_intents_hash_differently() -> None:
    assert idea_hash("open long on Drift today") != idea_hash("open short on Drift today")


def test_different_size_buckets_hash_differently() -> None:
    assert idea_hash("small deposit into Kamino today") != idea_hash(
        "large deposit into Kamino today"
    )


def test_dict_ideas_canonicalised() -> None:
    # Dict-shaped ideas use sorted-key JSON; order shouldn't matter.
    h1 = idea_hash({"a": 1, "b": 2})
    h2 = idea_hash({"b": 2, "a": 1})
    assert h1 == h2
