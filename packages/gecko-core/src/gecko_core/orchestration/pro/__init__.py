"""Pro tier orchestration — 5-agent AG2 GroupChat debate.

A2 shipped the surface (events, transcript, budget guard, agent builders).
A4 fills in the AG2 invocation inside `generate`. The `on_event` callback
decouples persistence and SSE plumbing from orchestration.

We deliberately drive the 5 agents in fixed order (analyst → critic →
architect → scoper → judge) rather than relying on AG2's `auto`
speaker selection. Reasons:
  - Deterministic ordering makes the SSE stream legible to the user.
  - Budget enforcement is straightforward: one `record_turn` per agent.
  - Avoids an extra "speaker selector" LLM call per round (cost + latency).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from gecko_core.orchestration.pro.agents import build_groupchat
from gecko_core.orchestration.pro.budget import BudgetExceeded, BudgetGuard
from gecko_core.orchestration.pro.events import AgentEvent
from gecko_core.orchestration.pro.transcript import (
    AgentTurn,
    DebateTranscript,
    transcript_from_events,
)

__all__ = [
    "AgentEvent",
    "AgentTurn",
    "BudgetExceeded",
    "BudgetGuard",
    "DebateTranscript",
    "build_groupchat",
    "generate",
    "transcript_from_events",
]


# Order matters — the analyst opens with TAM, the critic pokes holes, the
# architect picks the stack, the scoper carves V1/V2/V3, the judge calls it.
_AGENT_ORDER: tuple[str, ...] = ("analyst", "critic", "architect", "scoper", "judge")

_RAG_CONTEXT_CHAR_CAP = 8000


def _opening_prompt(idea: str, rag_context: str) -> str:
    """Templated kickoff for the analyst.

    rag_context is sliced to keep round-1 cheap. Subsequent speakers see the
    full chat history (their own + prior turns) but not the original context
    again — AG2 prepends the system message per-agent.
    """
    sliced = rag_context[:_RAG_CONTEXT_CHAR_CAP]
    if len(rag_context) > _RAG_CONTEXT_CHAR_CAP:
        sliced += "\n\n[context truncated for budget]"
    return (
        f"Idea to validate: {idea}\n\n"
        f"Knowledge-base context (sources curated by the user):\n{sliced}\n\n"
        "Analyst — start. Then pass to critic, architect, scoper, and judge "
        "in that order. Each speaker contributes once."
    )


def _extract_token_counts(reply: Any) -> tuple[int, int]:
    """Best-effort token extraction from an AG2 reply *return value*.

    AG2's `a_generate_reply` mostly returns a plain string — usage rides on
    the wrapped client (see ``_client_usage_delta``). We still inspect the
    return value first because some downstream adapters round-trip a dict
    with ``usage``/``token_usage`` (notably stubbed test fixtures).
    """
    if isinstance(reply, dict):
        usage = reply.get("usage") or reply.get("token_usage") or {}
        if isinstance(usage, dict):
            return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    return 0, 0


def _sum_summary(summary: Any) -> tuple[int, int]:
    """Sum prompt/completion tokens across all model keys in an AG2 usage summary.

    AG2 stores per-model rollups under ``client.total_usage_summary`` shaped
    like ``{"total_cost": float, "<model>": {"prompt_tokens": int, ...}, ...}``.
    Summing across model keys handles the case where an agent rotates models
    mid-debate (rare, but the schema permits it).
    """
    if not isinstance(summary, dict):
        return 0, 0
    p = 0
    c = 0
    for key, val in summary.items():
        if key == "total_cost" or not isinstance(val, dict):
            continue
        p += int(val.get("prompt_tokens") or 0)
        c += int(val.get("completion_tokens") or 0)
    return p, c


def _client_usage_delta(agent: Any, before: tuple[int, int]) -> tuple[int, int]:
    """Return ``(tokens_in_delta, tokens_out_delta)`` since the ``before`` snapshot.

    Pinned attribute path (AG2 0.12 / autogen-core 0.7): each ``ConversableAgent``
    holds an ``OpenAIWrapper`` at ``agent.client``; that wrapper accumulates
    usage at ``agent.client.total_usage_summary`` (a per-model dict). We
    snapshot before each ``a_generate_reply`` and diff after — that gives
    per-call deltas without reaching into private state.

    Returns ``(0, 0)`` when ``client`` is None (e.g. fakes in unit tests),
    in which case the caller falls back to the reply-shape extractor or
    zero-token attribution.
    """
    client = getattr(agent, "client", None)
    if client is None:
        return 0, 0
    after = _sum_summary(getattr(client, "total_usage_summary", None))
    return max(after[0] - before[0], 0), max(after[1] - before[1], 0)


def _client_usage_snapshot(agent: Any) -> tuple[int, int]:
    client = getattr(agent, "client", None)
    if client is None:
        return 0, 0
    return _sum_summary(getattr(client, "total_usage_summary", None))


def _reply_text(reply: Any) -> str:
    if isinstance(reply, dict):
        content = reply.get("content")
        return str(content) if content is not None else ""
    if reply is None:
        return ""
    return str(reply)


async def generate(
    *,
    idea: str,
    rag_context: str,
    llm_config: dict[str, Any] | None = None,
    on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
    budget: BudgetGuard | None = None,
    model_matrix: dict[str, str] | None = None,
) -> DebateTranscript:
    """Run the 5-agent debate.

    Drives agents in fixed order (analyst → critic → architect → scoper →
    judge). Emits `turn_start` and `turn_end` AgentEvents per agent. On
    BudgetExceeded the transcript's `budget_halt_reason` is populated and
    the partial result is returned (no exception).

    Raises:
        ImportError: AG2 (`autogen`) isn't installed.
        ValueError: llm_config is None.
    """
    if llm_config is None:
        raise ValueError("llm_config is required for pro.generate")
    budget = budget or BudgetGuard()

    # Only forward `model_matrix` when supplied so legacy callers that
    # monkeypatch `build_groupchat` with a single-arg signature still work.
    if model_matrix is None:
        manager = build_groupchat(llm_config)
    else:
        manager = build_groupchat(llm_config, model_matrix=model_matrix)
    chat = manager.groupchat
    agents_by_name = {a.name: a for a in chat.agents}

    seq = 0
    collected: list[AgentEvent] = []
    halt_reason: str | None = None

    async def _emit(event: AgentEvent) -> None:
        collected.append(event)
        if on_event is not None:
            await on_event(event)

    # The opening message seeds the transcript so each agent has something
    # to react to. Recorded as the analyst's "user message" — not a turn.
    opening = _opening_prompt(idea, rag_context)
    chat.messages = [{"role": "user", "name": "user", "content": opening}]

    budget.start()

    for agent_name in _AGENT_ORDER:
        agent = agents_by_name.get(agent_name)
        if agent is None:  # pragma: no cover — build_groupchat invariant
            continue

        seq += 1
        await _emit(
            AgentEvent(
                type="turn_start",
                agent=agent_name,
                content="",
                ts=time.time(),
                tokens_in=0,
                tokens_out=0,
                seq=seq,
            )
        )

        try:
            # Snapshot client usage BEFORE the call so we can diff after.
            usage_before = _client_usage_snapshot(agent)
            # Each agent sees the running transcript and replies once.
            reply = await agent.a_generate_reply(messages=list(chat.messages))
        except Exception as exc:
            seq += 1
            await _emit(
                AgentEvent(
                    type="error",
                    agent=agent_name,
                    content=f"{type(exc).__name__}: {exc}",
                    ts=time.time(),
                    tokens_in=0,
                    tokens_out=0,
                    seq=seq,
                )
            )
            halt_reason = None
            break

        text = _reply_text(reply)
        # Token attribution priority:
        #   1. Reply-shape (covers test fakes that hand back {"usage": ...})
        #   2. Wrapped-client usage delta (the production AG2 path)
        # We never sum (1) and (2) — only one source is authoritative per
        # turn, and AG2 production never returns dict-shape usage on the
        # reply, so the client delta wins in real runs.
        tokens_in, tokens_out = _extract_token_counts(reply)
        if tokens_in == 0 and tokens_out == 0:
            tokens_in, tokens_out = _client_usage_delta(agent, usage_before)

        seq += 1
        await _emit(
            AgentEvent(
                type="turn_end",
                agent=agent_name,
                content=text,
                ts=time.time(),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                seq=seq,
            )
        )

        # Append the agent's reply to the running transcript.
        chat.messages.append({"role": "assistant", "name": agent_name, "content": text})

        # Budget enforcement happens AFTER the turn so we always commit the
        # event we just emitted. BudgetExceeded breaks the loop with the
        # halt reason recorded on the transcript.
        try:
            budget.record_turn(tokens_in, tokens_out)
        except BudgetExceeded as exc:
            halt_reason = exc.reason
            break

    # Final event closes the SSE stream. Carries the judge's verdict (last
    # `turn_end` content) so consumers can stop reading without a poll.
    final_summary = ""
    for ev in reversed(collected):
        if ev.type == "turn_end" and ev.agent == "judge":
            final_summary = ev.content
            break

    seq += 1
    await _emit(
        AgentEvent(
            type="final",
            agent=None,
            content=final_summary,
            ts=time.time(),
            tokens_in=0,
            tokens_out=0,
            seq=seq,
        )
    )

    transcript = transcript_from_events(collected)
    if halt_reason is not None:
        transcript = transcript.model_copy(update={"budget_halt_reason": halt_reason})
    return transcript
