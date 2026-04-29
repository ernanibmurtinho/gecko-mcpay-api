"""S2X-06 — Pro tier consumes Gecko Flywheel precedents.

Asserts:
  - render_precedent_block produces the canonical block (header + bullets,
    or header + "No prior precedents found." when the corpus is empty).
  - `_opening_prompt` injects the rendered block into the analyst's
    kickoff message.
  - The end-to-end `_run_pro_debate` happy path threads precedents into
    `chat.messages[0]` so the agents observe them in their context.
  - Prompts loader honors GECKO_PRO_PROMPTS_VERSION (v4 vs v5 rollback).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


def _precedent(
    *,
    summary: str,
    verdict: str,
    similarity: float | None = 0.84,
) -> Any:
    from gecko_core.sessions.store import GeckoPrecedent

    return GeckoPrecedent(
        id=uuid4(),
        session_id=uuid4(),
        user_id=None,
        idea_summary=summary,
        verdict=verdict,  # type: ignore[arg-type]
        key_comparables=[],
        similarity=similarity,
    )


def test_render_block_with_precedents() -> None:
    from gecko_core.orchestration.pro.precedents import render_precedent_block

    rows = [
        _precedent(summary="MCP server for Postgres explainer", verdict="ship", similarity=0.91),
        _precedent(summary="Generic AI todo app", verdict="kill", similarity=0.79),
    ]
    block = render_precedent_block(rows)
    assert "Prior similar ideas Gecko evaluated:" in block
    assert "[SHIP] MCP server for Postgres explainer (sim=0.91)" in block
    assert "[KILL] Generic AI todo app (sim=0.79)" in block


def test_render_block_empty_says_no_precedent() -> None:
    """Absence is signal — render the section even when retrieval is empty."""
    from gecko_core.orchestration.pro.precedents import render_precedent_block

    block = render_precedent_block([])
    assert "Prior similar ideas Gecko evaluated:" in block
    assert "No prior precedents found." in block


def test_opening_prompt_includes_precedent_block() -> None:
    from gecko_core.orchestration.pro import _opening_prompt

    rows = [_precedent(summary="vet-tele-rx checklist", verdict="ship", similarity=0.86)]
    prompt = _opening_prompt("a new vet workflow tool", "rag chunk", rows)
    assert "Idea to validate: a new vet workflow tool" in prompt
    assert "Prior similar ideas Gecko evaluated:" in prompt
    assert "[SHIP] vet-tele-rx checklist (sim=0.86)" in prompt
    # rag_context must still appear after the precedent block
    assert "Knowledge-base context" in prompt
    assert prompt.index("Prior similar ideas") < prompt.index("Knowledge-base context")


def test_opening_prompt_empty_precedents_renders_section() -> None:
    from gecko_core.orchestration.pro import _opening_prompt

    prompt = _opening_prompt("an idea", "rag chunk", None)
    assert "Prior similar ideas Gecko evaluated:" in prompt
    assert "No prior precedents found." in prompt


async def test_pro_generate_threads_precedents_into_chat_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agents see the precedent block in their context (chat.messages[0])."""
    from gecko_core.orchestration import pro as pro_mod
    from gecko_core.orchestration.pro import generate

    canned = {
        "analyst": "TAM real",
        "critic": "wedge fuzzy",
        "architect": "next.js",
        "scoper": "V1 4d",
        "judge": "ship",
    }

    captured_messages: list[dict[str, Any]] = []

    class _FakeAgent:
        def __init__(self, name: str) -> None:
            self.name = name

        async def a_generate_reply(self, messages: object = None) -> str:
            # Snapshot the messages the agent sees on its first call.
            if isinstance(messages, list) and not captured_messages:
                captured_messages.extend(messages)  # type: ignore[arg-type]
            return canned[self.name]

    class _FakeChat:
        def __init__(self) -> None:
            self.agents = [_FakeAgent(n) for n in canned]
            self.messages: list[dict[str, object]] = []

    class _FakeManager:
        def __init__(self) -> None:
            self.groupchat = _FakeChat()

    monkeypatch.setattr(pro_mod, "build_groupchat", lambda _cfg: _FakeManager())

    rows = [
        _precedent(summary="cap-table diff tool", verdict="ship", similarity=0.88),
        _precedent(summary="generic gpt wrapper", verdict="kill", similarity=0.81),
    ]

    await generate(
        idea="a cap-table workflow",
        rag_context="some chunks",
        llm_config={"config_list": [{"model": "x", "api_key": "y", "base_url": "z"}]},
        precedents=rows,
    )

    assert captured_messages, "agents must have seen the opening message"
    opening = str(captured_messages[0]["content"])
    assert "Prior similar ideas Gecko evaluated:" in opening
    assert "[SHIP] cap-table diff tool (sim=0.88)" in opening
    assert "[KILL] generic gpt wrapper (sim=0.81)" in opening


async def test_run_pro_debate_retrieves_and_injects_precedents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: `_run_pro_debate` calls retrieve_gecko_precedent and the
    block lands in the agents' context.
    """
    from types import SimpleNamespace

    from gecko_core.models import ResearchResult, SourceInfo
    from gecko_core.orchestration import pro as pro_mod
    from gecko_core.workflows import _run_pro_debate

    # Stub rag_query so we don't talk to Supabase.
    async def _fake_rag_query(*_a: object, **_kw: object) -> list[Any]:
        return [
            SimpleNamespace(
                source_url="https://example.com/a",
                chunk_index=0,
                similarity=0.9,
                text="Hosts care about local recommendations.",
            )
        ]

    monkeypatch.setattr("gecko_core.workflows.rag_query", _fake_rag_query, raising=False)
    monkeypatch.setattr("gecko_core.rag.query.rag_query", _fake_rag_query, raising=False)

    # Stub the embedder — return a fixed 1536-dim vector.
    async def _fake_embed(texts: list[str], **_kw: object) -> tuple[list[list[float]], int]:
        return [[0.0] * 1536 for _ in texts], 5

    monkeypatch.setattr("gecko_core.ingestion.embedder.embed", _fake_embed, raising=True)

    # Stub the groupchat — capture the messages the analyst sees.
    canned = {
        "analyst": "TAM real",
        "critic": "wedge fuzzy",
        "architect": "next.js",
        "scoper": "V1 4d",
        "judge": "Verdict: SHIP V1 to vet practices.",
    }

    captured_messages: list[dict[str, Any]] = []

    class _FakeAgent:
        def __init__(self, name: str) -> None:
            self.name = name

        async def a_generate_reply(self, messages: object = None) -> str:
            if isinstance(messages, list) and not captured_messages:
                captured_messages.extend(messages)  # type: ignore[arg-type]
            return canned[self.name]

    class _FakeChat:
        def __init__(self) -> None:
            self.agents = [_FakeAgent(n) for n in canned]
            self.messages: list[dict[str, object]] = []

    class _FakeManager:
        def __init__(self) -> None:
            self.groupchat = _FakeChat()

    monkeypatch.setattr(pro_mod, "build_groupchat", lambda _cfg: _FakeManager())

    # Fake store — records appended events/costs and returns 2 precedents
    # from retrieve_gecko_precedent.
    store = AsyncMock()
    store.appended_events = []  # type: ignore[attr-defined]
    store.appended_costs = []  # type: ignore[attr-defined]

    async def _append_pro_event(**kw: Any) -> int:
        store.appended_events.append(kw)  # type: ignore[attr-defined]
        return len(store.appended_events)  # type: ignore[attr-defined]

    async def _append_session_cost(**kw: Any) -> int:
        store.appended_costs.append(kw)  # type: ignore[attr-defined]
        return len(store.appended_costs)  # type: ignore[attr-defined]

    rows = [
        _precedent(summary="vet-tele-rx checklist", verdict="ship", similarity=0.88),
        _precedent(summary="generic gpt wrapper", verdict="kill", similarity=0.79),
    ]

    async def _retrieve(**_kw: object) -> list[Any]:
        return rows

    store.append_pro_event = AsyncMock(side_effect=_append_pro_event)
    store.append_session_cost = AsyncMock(side_effect=_append_session_cost)
    store.retrieve_gecko_precedent = AsyncMock(side_effect=_retrieve)

    sid = uuid4()
    base_result = ResearchResult(
        session_id=str(sid),
        tier="basic",
        business_plan={  # type: ignore[arg-type]
            "problem": "p",
            "icp": "i",
            "solution": "s",
            "market": "m",
            "business_model": "bm",
            "channels": "c",
            "risks": ["r"],
            "citations": [],
        },
        validation_report={  # type: ignore[arg-type]
            "market_size_signal": "x",
            "competitor_analysis": "y",
            "demand_evidence": "z",
            "risk_flags": [],
            "citations": [],
        },
        prd={  # type: ignore[arg-type]
            "v1_scope": ["a"],
            "v2_scope": [],
            "v3_scope": [],
            "acceptance_criteria": ["b"],
            "non_functional": [],
            "success_metrics": [],
            "citations": [],
        },
        sources=[
            SourceInfo(
                url="https://example.com/a",  # type: ignore[arg-type]
                type="web",
                chunk_count=1,
                indexed_at=datetime.now(UTC),
            )
        ],
    )

    result = await _run_pro_debate(sid, "a vet workflow tool", base_result, store)

    # Retrieval was called.
    store.retrieve_gecko_precedent.assert_awaited()

    # Agents saw the precedent block in their first message.
    assert captured_messages, "agents must have seen the opening message"
    opening = str(captured_messages[0]["content"])
    assert "Prior similar ideas Gecko evaluated:" in opening
    assert "[SHIP] vet-tele-rx checklist (sim=0.88)" in opening
    assert "[KILL] generic gpt wrapper (sim=0.79)" in opening

    # Tier flipped, transcript populated.
    assert result.tier == "pro"
    assert result.transcript is not None


def test_prompts_version_default_is_v5(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default load resolves to a bundle that carries the v5 analyst guidance.

    Note: v5.1 is the current default (Judge-only fix for the 2026-04-28
    regression); it inherits the v5 analyst prompt verbatim, so the
    `gecko_precedent` marker is still present. See
    `test_prompts_version_default_is_v5_1` for the v5.1-specific assertion.
    """
    from gecko_core.orchestration.pro import prompts as prompts_mod

    monkeypatch.delenv("GECKO_PROMPTS_PATH", raising=False)
    monkeypatch.delenv("GECKO_PRO_PROMPTS_VERSION", raising=False)
    prompts_mod.load_prompts.cache_clear()
    p = prompts_mod.load_prompts()
    # v5/v5.1 mention gecko_precedent in the analyst — sanity check we did
    # not regress to v4.
    assert "gecko_precedent" in p["analyst"].lower()


def test_prompts_version_default_is_v5_4(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default load resolves to the v5.4 bundle (Judge + Critic rewrite that
    flips recency bias and caps quota-driven kill seeding).

    Live evidence chain: tests/eval/live_runs/2026-04-28-general*.json showed
    accuracy 0.55 (v5.0) → 0.65 (v5.1) → 0.65 (v5.2) → 0.50 (v5.3, WORST).
    v5.4 collapses the brittle 12-entry STEP 2 keyword-trigger list to a single
    named-buyer + named-artifact + V1_FEASIBLE_IN_4_DAYS=yes rule, reorders the
    pipeline so STEP 3 is the DEFAULT SHIP (KILL becomes the LAST block read by
    gpt-4o-mini), trims STEP 4 from 13 triggers to 4 hard-kill triggers, and
    caps the Critic at 1-3 risks plus one Change-my-mind clause.
    """
    from gecko_core.orchestration.pro import prompts as prompts_mod

    monkeypatch.delenv("GECKO_PROMPTS_PATH", raising=False)
    monkeypatch.delenv("GECKO_PRO_PROMPTS_VERSION", raising=False)
    prompts_mod.load_prompts.cache_clear()
    p = prompts_mod.load_prompts()
    judge = p["judge"]
    critic = p["critic"]

    # 4-step pipeline (v5.4 dropped the v5.3 STEP 5 — STEP 3 is now default).
    assert "DECISION PIPELINE" in judge
    assert "STEP 1" in judge
    assert "STEP 2" in judge
    assert "STEP 3" in judge
    assert "STEP 4" in judge
    assert "STEP 5" not in judge

    # STEP 3 is the DEFAULT SHIP and comes BEFORE STEP 4 KILL — that's the
    # recency-bias flip that v5.4 exists to enable.
    assert "DEFAULT SHIP" in judge
    assert judge.index("STEP 3") < judge.index("STEP 4")

    # NAMED-ICP sanity check still present (inherited from v5.2/v5.3).
    assert "NAMED-ICP SANITY CHECK" in judge
    assert "No named ICP after the full debate: KILL." not in judge

    # STEP 2 collapsed: per-entry "Trigger: idea text contains" lines from v5.3
    # are gone; the named entries survive only as an illustrative appendix.
    assert 'Trigger: idea text contains "cap table"' not in judge
    assert 'Trigger: idea text contains "MCP"' not in judge
    assert 'Trigger: idea text contains "Part 107"' not in judge
    assert "cap-table-diff" in judge  # appendix is still there
    assert "stripe-replay" in judge
    assert "grant-budget-rewriter" in judge

    # STEP 4 trimmed to 4 hard-kill triggers (a)-(d). The Uber liquidity-wedge
    # trigger is the canonical one we want to keep firing.
    assert "(a) Uber" in judge
    assert "(b) AI-therapy" in judge
    assert "(c) Generic GPT-wrapper" in judge
    assert "(d) Consumer-social-with-token" in judge

    # Critic cap: the v5.3 ">= 2 named, idea-specific kill criteria" quota is
    # gone, replaced by the 1-3 risks + Change-my-mind clause.
    assert ">= 2 named" not in critic
    assert "FLOOR-LEVEL skepticism" not in critic
    assert "1-3 named" in critic
    assert "Change my mind by:" in critic
    assert "do NOT generate kill criteria" in critic.lower() or (
        "do not generate kill criteria" in critic.lower()
    )


def test_prompts_version_v5_3_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting GECKO_PRO_PROMPTS_VERSION=v5.3 loads the prior bundle unmodified
    (5-step pipeline with per-entry keyword Triggers in STEP 2 and STEP 4)."""
    from gecko_core.orchestration.pro import prompts as prompts_mod

    monkeypatch.delenv("GECKO_PROMPTS_PATH", raising=False)
    monkeypatch.setenv("GECKO_PRO_PROMPTS_VERSION", "v5.3")
    prompts_mod.load_prompts.cache_clear()
    p = prompts_mod.load_prompts()
    judge = p["judge"]
    # v5.3 carries the 5-step pipeline and the per-entry keyword triggers.
    assert "STEP 5" in judge
    assert 'Trigger: idea text contains "cap table"' in judge
    assert 'Trigger: idea text contains ("Uber"' in judge
    prompts_mod.load_prompts.cache_clear()


def test_prompts_version_v5_2_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting GECKO_PRO_PROMPTS_VERSION=v5.2 loads the prior bundle unmodified
    (DECISION PIPELINE present, but STEP 2 entries lack per-entry keyword
    Triggers and STEP 4 is the prose saturation list rather than triggers).
    """
    from gecko_core.orchestration.pro import prompts as prompts_mod

    monkeypatch.delenv("GECKO_PROMPTS_PATH", raising=False)
    monkeypatch.setenv("GECKO_PRO_PROMPTS_VERSION", "v5.2")
    prompts_mod.load_prompts.cache_clear()
    p = prompts_mod.load_prompts()
    judge = p["judge"]
    assert "DECISION PIPELINE" in judge
    # v5.2 has no per-entry keyword Triggers; that's the v5.3 addition.
    assert 'Trigger: idea text contains "cap table"' not in judge
    assert 'Trigger: idea text contains ("Uber"' not in judge
    prompts_mod.load_prompts.cache_clear()


def test_prompts_version_v5_1_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting GECKO_PRO_PROMPTS_VERSION=v5.1 loads the prior bundle unmodified
    (parallel MANDATORY SHIP / MANDATORY KILL sections, no DECISION PIPELINE).
    """
    from gecko_core.orchestration.pro import prompts as prompts_mod

    monkeypatch.delenv("GECKO_PROMPTS_PATH", raising=False)
    monkeypatch.setenv("GECKO_PRO_PROMPTS_VERSION", "v5.1")
    prompts_mod.load_prompts.cache_clear()
    p = prompts_mod.load_prompts()
    judge = p["judge"]
    # v5.1 has parallel sections, not a pipeline.
    assert "MANDATORY SHIP RULES" in judge
    assert "MANDATORY KILL RULES" in judge
    assert "DECISION PIPELINE" not in judge
    prompts_mod.load_prompts.cache_clear()


def test_prompts_version_v5_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting GECKO_PRO_PROMPTS_VERSION=v5 loads the prior bundle unmodified
    (no v5.1 sanity-check section, no narrowed kill condition).
    """
    from gecko_core.orchestration.pro import prompts as prompts_mod

    monkeypatch.delenv("GECKO_PROMPTS_PATH", raising=False)
    monkeypatch.setenv("GECKO_PRO_PROMPTS_VERSION", "v5")
    prompts_mod.load_prompts.cache_clear()
    p = prompts_mod.load_prompts()
    judge = p["judge"]
    # v5 still has the blunt kill line.
    assert "No named ICP after the full debate: KILL." in judge
    # v5 has no sanity-check section.
    assert "NAMED-ICP SANITY CHECK" not in judge
    prompts_mod.load_prompts.cache_clear()


def test_prompts_version_v4_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting GECKO_PRO_PROMPTS_VERSION=v4 loads the prior bundle unmodified."""
    from gecko_core.orchestration.pro import prompts as prompts_mod

    monkeypatch.delenv("GECKO_PROMPTS_PATH", raising=False)
    monkeypatch.setenv("GECKO_PRO_PROMPTS_VERSION", "v4")
    prompts_mod.load_prompts.cache_clear()
    p = prompts_mod.load_prompts()
    # v4 must NOT mention twit_sh in the critic — that's a v5 addition.
    assert "twit_sh" not in p["critic"]
    prompts_mod.load_prompts.cache_clear()


def test_prompts_version_unknown_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from gecko_core.orchestration.pro import prompts as prompts_mod

    monkeypatch.delenv("GECKO_PROMPTS_PATH", raising=False)
    monkeypatch.setenv("GECKO_PRO_PROMPTS_VERSION", "v99")
    prompts_mod.load_prompts.cache_clear()
    with pytest.raises(prompts_mod.PromptsConfigError, match="not a known bundled version"):
        prompts_mod.load_prompts()
    prompts_mod.load_prompts.cache_clear()
