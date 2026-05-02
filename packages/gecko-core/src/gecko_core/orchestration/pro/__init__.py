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

S12-LATENCY-01 — analyst and critic are independent of each other (both
read the same RAG context, neither sees the other's reply). We dispatch
them concurrently via ``asyncio.gather`` and then run architect → scoper
→ judge serially. Transcript order remains canonical (analyst → critic →
architect → scoper → judge) regardless of completion order, so replay /
debugging stays deterministic.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from gecko_core.orchestration.pro.agents import build_groupchat
from gecko_core.orchestration.pro.budget import BudgetExceeded, BudgetGuard
from gecko_core.orchestration.pro.events import AgentEvent
from gecko_core.orchestration.pro.precedents import render_precedent_block
from gecko_core.orchestration.pro.transcript import (
    AgentTurn,
    DebateTranscript,
    transcript_from_events,
)
from gecko_core.sessions.store import GeckoPrecedent

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

# S12-LATENCY-01: analyst + critic are mutually independent (both read the
# same RAG context, neither needs the other's reply). Architect waits on
# both; scoper waits on architect; judge waits on scoper.
_PARALLEL_STAGE: tuple[str, ...] = ("analyst", "critic")
_SERIAL_STAGE: tuple[str, ...] = ("architect", "scoper", "judge")

# Per-voice wall-clock cap. A single hung voice cannot block the rest of
# the debate; on timeout we emit a placeholder turn (voice failed:
# timeout after Ns) so the transcript stays well-formed.
_VOICE_TIMEOUT_SECONDS: float = 15.0

_RAG_CONTEXT_CHAR_CAP = 8000


@dataclass(slots=True)
class _VoiceOutcome:
    """Result of a single voice turn, dispatched in `_run_voice`.

    Buffered until the caller is ready to emit events in canonical order —
    which matters for the parallel analyst/critic stage where completion
    order is non-deterministic but transcript order must stay stable.
    """

    agent: str
    kind: Literal["ok", "timeout", "error", "skipped"]
    content: str
    tokens_in: int
    tokens_out: int
    error_text: str | None


def _opening_prompt(
    idea: str,
    rag_context: str,
    precedents: list[GeckoPrecedent] | None = None,
) -> str:
    """Templated kickoff for the analyst.

    rag_context is sliced to keep round-1 cheap. Subsequent speakers see the
    full chat history (their own + prior turns) but not the original context
    again — AG2 prepends the system message per-agent.

    The Gecko precedent block (S2X-06) is rendered ABOVE the rag_context so
    the analyst sees prior verdicts before market-survey context. We always
    render the section — even when retrieval is empty — because absence of
    precedent is itself signal (new category for Gecko).
    """
    sliced = rag_context[:_RAG_CONTEXT_CHAR_CAP]
    if len(rag_context) > _RAG_CONTEXT_CHAR_CAP:
        sliced += "\n\n[context truncated for budget]"
    precedent_block = render_precedent_block(precedents or [])
    return (
        f"Idea to validate: {idea}\n\n"
        f"{precedent_block}\n\n"
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
    temperature_matrix: dict[str, float] | None = None,
    precedents: list[GeckoPrecedent] | None = None,
    tier_preset: str | None = None,
    gap_classification: str | None = None,
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

    # When the caller specifies a tier_preset and no explicit model_matrix,
    # resolve the per-agent matrix from the curated catalog. This is the
    # S4-MATRIX-01 wiring path; callers passing an explicit model_matrix
    # take precedence (used by tests + the legacy LLM_ROUTER path).
    if tier_preset is not None and model_matrix is None:
        import os as _os

        from gecko_core.orchestration.pro.router import model_matrix_for_tier
        from gecko_core.routing.catalog import Tier as _Tier

        # We can't import RouterConfig.router off llm_config — derive it from
        # LLM_ROUTER directly. Default 'openai' matches resolve_router().
        _router_name = (_os.environ.get("LLM_ROUTER") or "openai").strip().lower()
        if _router_name not in ("openai", "openrouter", "clawrouter"):
            _router_name = "openai"
        model_matrix = model_matrix_for_tier(_router_name, _Tier(tier_preset))  # type: ignore[arg-type]

    # Only forward optional kwargs when supplied so legacy callers that
    # monkeypatch `build_groupchat` with a narrower signature still work.
    bg_kwargs: dict[str, Any] = {}
    if model_matrix is not None:
        bg_kwargs["model_matrix"] = model_matrix
    if temperature_matrix is not None:
        bg_kwargs["temperature_matrix"] = temperature_matrix
    # S20-FEATURE-NOT-PRODUCT-CRITIC-01: only forward gap_classification when
    # supplied so legacy test fakes that monkeypatch build_groupchat with a
    # narrower signature continue to work (same pattern as model_matrix).
    if gap_classification is not None:
        bg_kwargs["gap_classification"] = gap_classification
    manager = build_groupchat(llm_config, **bg_kwargs) if bg_kwargs else build_groupchat(llm_config)
    chat = manager.groupchat
    agents_by_name = {a.name: a for a in chat.agents}

    collected: list[AgentEvent] = []
    halt_reason: str | None = None

    async def _emit(event: AgentEvent) -> None:
        collected.append(event)
        if on_event is not None:
            await on_event(event)

    # The opening message seeds the transcript so each agent has something
    # to react to. Recorded as the analyst's "user message" — not a turn.
    opening = _opening_prompt(idea, rag_context, precedents)
    chat.messages = [{"role": "user", "name": "user", "content": opening}]

    budget.start()

    # `seq` is mutable across nested helpers; keep it as a 1-element list to
    # avoid `nonlocal` declarations in async helpers (mypy-friendly).
    seq_box = [0]

    def _next_seq() -> int:
        seq_box[0] += 1
        return seq_box[0]

    async def _run_voice(agent_name: str, messages: list[dict[str, Any]]) -> _VoiceOutcome:
        """Execute a single voice turn, returning its outcome.

        Does NOT emit events directly — the caller is responsible for
        emitting in canonical order so the transcript stays deterministic
        regardless of completion order in the parallel stage.
        """
        agent = agents_by_name.get(agent_name)
        if agent is None:  # pragma: no cover — build_groupchat invariant
            return _VoiceOutcome(
                agent=agent_name,
                kind="skipped",
                content="",
                tokens_in=0,
                tokens_out=0,
                error_text=None,
            )

        usage_before = _client_usage_snapshot(agent)
        try:
            reply = await asyncio.wait_for(
                agent.a_generate_reply(messages=messages),
                timeout=_VOICE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            # S12-LATENCY-01: timeout surfaces as a placeholder turn (per
            # S9-ADVISOR-01 "voice failed" pattern) — debate continues so
            # the rest of the chain is unblocked.
            return _VoiceOutcome(
                agent=agent_name,
                kind="timeout",
                content=f"(voice failed: timeout after {int(_VOICE_TIMEOUT_SECONDS)}s)",
                tokens_in=0,
                tokens_out=0,
                error_text=f"TimeoutError: voice exceeded {int(_VOICE_TIMEOUT_SECONDS)}s",
            )
        except Exception as exc:
            return _VoiceOutcome(
                agent=agent_name,
                kind="error",
                content="",
                tokens_in=0,
                tokens_out=0,
                error_text=f"{type(exc).__name__}: {exc}",
            )

        text = _reply_text(reply)
        # Token attribution priority:
        #   1. Reply-shape (covers test fakes that hand back {"usage": ...})
        #   2. Wrapped-client usage delta (the production AG2 path)
        tokens_in, tokens_out = _extract_token_counts(reply)
        if tokens_in == 0 and tokens_out == 0:
            tokens_in, tokens_out = _client_usage_delta(agent, usage_before)

        return _VoiceOutcome(
            agent=agent_name,
            kind="ok",
            content=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            error_text=None,
        )

    async def _commit_outcome(outcome: _VoiceOutcome) -> str | None:
        """Emit the canonical events for a completed voice and apply budget.

        Returns ``"halt"`` if the caller should stop the debate (hard error
        or budget exceeded), or ``None`` to continue.
        """
        nonlocal halt_reason

        # turn_start is emitted just before the turn lands so consumers see
        # the canonical (start, end) pair even when the underlying call
        # completed earlier in real time.
        await _emit(
            AgentEvent(
                type="turn_start",
                agent=outcome.agent,
                content="",
                ts=time.time(),
                tokens_in=0,
                tokens_out=0,
                seq=_next_seq(),
            )
        )

        if outcome.kind == "error":
            await _emit(
                AgentEvent(
                    type="error",
                    agent=outcome.agent,
                    content=outcome.error_text or "",
                    ts=time.time(),
                    tokens_in=0,
                    tokens_out=0,
                    seq=_next_seq(),
                )
            )
            return "halt"

        if outcome.kind == "timeout":
            # Surface the timeout via an `error` event for observability,
            # then a `turn_end` with the placeholder content so the
            # transcript reducer still sees a turn in canonical order.
            await _emit(
                AgentEvent(
                    type="error",
                    agent=outcome.agent,
                    content=outcome.error_text or "",
                    ts=time.time(),
                    tokens_in=0,
                    tokens_out=0,
                    seq=_next_seq(),
                )
            )

        await _emit(
            AgentEvent(
                type="turn_end",
                agent=outcome.agent,
                content=outcome.content,
                ts=time.time(),
                tokens_in=outcome.tokens_in,
                tokens_out=outcome.tokens_out,
                seq=_next_seq(),
            )
        )

        # Append to the running chat transcript so subsequent agents see
        # this voice's contribution. Even on timeout we append the
        # placeholder so downstream voices know the channel was lost.
        chat.messages.append(
            {"role": "assistant", "name": outcome.agent, "content": outcome.content}
        )

        # Budget enforcement happens AFTER the events so we always commit
        # what we just emitted. BudgetExceeded halts the chain with the
        # halt reason recorded on the transcript.
        try:
            budget.record_turn(outcome.tokens_in, outcome.tokens_out)
        except BudgetExceeded as exc:
            halt_reason = exc.reason
            return "halt"
        return None

    # ---- Stage 1: analyst + critic in parallel ----
    # Both voices read the same chat.messages snapshot (the opening only).
    # We dispatch concurrently and then commit outcomes in canonical order
    # regardless of completion order — preserving replay determinism.
    parallel_messages = list(chat.messages)
    parallel_outcomes = await asyncio.gather(
        *(_run_voice(name, parallel_messages) for name in _PARALLEL_STAGE),
    )

    halted = False
    for outcome in parallel_outcomes:  # canonical order (analyst, critic)
        if await _commit_outcome(outcome) == "halt":
            halted = True
            break

    # ---- Stage 2: architect → scoper → judge serially ----
    if not halted:
        for agent_name in _SERIAL_STAGE:
            outcome = await _run_voice(agent_name, list(chat.messages))
            if await _commit_outcome(outcome) == "halt":
                break

    # Final event closes the SSE stream. Carries the judge's verdict (last
    # `turn_end` content) so consumers can stop reading without a poll.
    final_summary = ""
    for ev in reversed(collected):
        if ev.type == "turn_end" and ev.agent == "judge":
            final_summary = ev.content
            break

    await _emit(
        AgentEvent(
            type="final",
            agent=None,
            content=final_summary,
            ts=time.time(),
            tokens_in=0,
            tokens_out=0,
            seq=_next_seq(),
        )
    )

    transcript = transcript_from_events(collected)
    if halt_reason is not None:
        transcript = transcript.model_copy(update={"budget_halt_reason": halt_reason})
    return transcript
