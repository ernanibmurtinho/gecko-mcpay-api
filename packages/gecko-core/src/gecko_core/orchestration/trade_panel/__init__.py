"""Trade research panel — 7-agent AG2 GroupChat (Phase 8a).

Public surface:

    from gecko_core.orchestration.trade_panel import run_trade_panel

    verdict = await run_trade_panel(
        idea="Should I open a long on JTO around the next FOMC?",
        protocol="jito",
        retrieved_chunks=[{"text": "...", "source": "exa", "ts": "..."}, ...],
        tier="basic",
    )
    assert verdict.verdict in {"act", "pass", "defer"}

The panel does NOT do retrieval — Phase 8b's caller passes pre-fetched
chunks in. The panel does NOT have its own eval harness in v1 — the Pro
calibration block / falsifier infra is intentionally not cloned.

Speaker order is canonical and round-robin. The driver below walks
REQUIRED_AGENTS in order, dispatches each agent's reply, parses the
closing line, and assembles a TradePanelVerdict from the coordinator's
last turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol, cast

from gecko_core.observability import emit_event
from gecko_core.orchestration.trade_panel.agents import build_groupchat
from gecko_core.orchestration.trade_panel.models import (
    Citation,
    TradePanelTurn,
    TradePanelVerdict,
    TradeVerdictLiteral,
)
from gecko_core.orchestration.trade_panel.personas import (
    BULL_BEAR_DEBATER,
    CLOSING_LINE_PATTERNS,
    COORDINATOR,
    FUNDAMENTAL_ANALYST,
    REQUIRED_AGENTS,
    RISK_MANAGER,
    SENTIMENT_ANALYST,
    STRATEGIST,
    TECHNICAL_ANALYST,
)
from gecko_core.orchestration.trade_panel.prompts import (
    TradePanelPromptsConfigError,
    load_prompts,
)
from gecko_core.sources.types import (
    FRESHNESS_TIER_VALUES,
    PROVIDER_KINDS,
    FreshnessTier,
    ProviderKind,
)

_log = logging.getLogger(__name__)

__all__ = [
    "BULL_BEAR_DEBATER",
    "COORDINATOR",
    "FUNDAMENTAL_ANALYST",
    "REQUIRED_AGENTS",
    "RISK_MANAGER",
    "SENTIMENT_ANALYST",
    "STRATEGIST",
    "TECHNICAL_ANALYST",
    "Citation",
    "TradePanelPromptsConfigError",
    "TradePanelTurn",
    "TradePanelVerdict",
    "TradeVerdictLiteral",
    "build_citations_from_chunks",
    "build_groupchat",
    "load_prompts",
    "retrieve_trade_corpus_chunks",
    "run_trade_panel",
    "run_trade_panel_with_retrieval",
]

# Pre-compiled closing-line regexes — case-insensitive, multiline-friendly.
_CLOSING_RE: dict[str, re.Pattern[str]] = {
    name: re.compile(pat, re.IGNORECASE | re.MULTILINE)
    for name, pat in CLOSING_LINE_PATTERNS.items()
}

# Per-voice timeout. Trade-panel v1 keeps the same default as Pro to start;
# tune separately when we have eval data.
_PER_VOICE_TIMEOUT_S: float = 120.0
# 60k chars ~= 15k tokens; gpt-4o-mini context is 128k; raises ceiling without removing the safety
_RAG_CONTEXT_CHAR_CAP: int = 60000


class _LLMReplier(Protocol):
    """Minimal interface tests can satisfy without AG2.

    Real AG2 ConversableAgent implements ``a_generate_reply(messages=...)``.
    Tests inject a fake replier with the same shape — no autogen install
    required.
    """

    async def a_generate_reply(
        self, messages: list[dict[str, Any]]
    ) -> str | dict[str, Any] | None:  # pragma: no cover - protocol
        ...


def _format_chunks(chunks: list[dict[str, Any]]) -> str:
    """Render retrieved chunks as a numbered context block.

    Indexed so the personas can cite by chunk index in their bodies.
    Each chunk is rendered as ``[idx] (source) text``. Truncated to
    ``_RAG_CONTEXT_CHAR_CAP`` total characters to keep round-1 cheap.
    """
    if not chunks:
        return "(no retrieved chunks — corpus empty for this protocol)"
    lines: list[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        source = chunk.get("source") or chunk.get("provider_kind") or "unknown"
        text = (chunk.get("text") or chunk.get("content") or "").strip()
        if not text:
            continue
        lines.append(f"[{idx}] ({source}) {text}")
    block = "\n\n".join(lines)
    if len(block) > _RAG_CONTEXT_CHAR_CAP:
        block = block[:_RAG_CONTEXT_CHAR_CAP] + "\n\n[context truncated for budget]"
    return block


def _opening_prompt(idea: str, protocol: str, chunks: list[dict[str, Any]]) -> str:
    """Seed message for the panel — stable shape across all 7 personas.

    S26 #19: a CITATION BREADTH directive is appended so non-coordinator
    voices spread their inline ``[N]`` markers across the full chunk
    range. Without it, gpt-4o-mini consistently cites 3-5 edge markers
    only (see canary 2026-05-13-canary-postfix). This is a persona-
    structure directive, not a coordinator verdict-shape edit, and is
    safe under the plateau-memory caveat.
    """
    n_chunks = len(chunks)
    # S28 #25 — softened from the S26 #19 "cite-broadly" directive. With
    # Cohere rerank serving 5 high-relevance chunks, breadth pressure was
    # producing citations padded for coverage rather than for grounding.
    # New directive: cite what you reason over, do NOT cite for breadth,
    # and do NOT mistake a chunk's numeric content for the target metric
    # unless the chunk names the target.
    breadth_block = (
        ""
        if n_chunks == 0
        else (
            "\n\nCITATION DISCIPLINE (mandatory for every non-coordinator voice):\n"
            f"  You have {n_chunks} reranked corpus chunks of high semantic "
            "relevance. Cite the chunks you actually reason over in body text "
            "(typically 3-5). Do NOT cite chunks just for breadth or coverage.\n"
            "  If a chunk reports a specific number (TVL, APR, price, volume), "
            "do NOT treat that number as the answer to a question unless the "
            "chunk explicitly identifies the same pool/vault/asset/protocol "
            "the question asks about.\n"
            "  Phantom citation (claiming a chunk you did not read in body "
            "text) is a panel failure mode and disqualifies the turn.\n"
            "\nQUANTITATIVE GROUNDING (mandatory for every non-coordinator voice):\n"
            "  When you write a specific number — TVL, APR/APY, price, volume, "
            "ratio, percentage, dollar figure — you MUST either:\n"
            "    (a) QUOTE the exact substring from the cited chunk that "
            "contains that number, in the same line — "
            "e.g. `TVL is $1,505,498,759 [3: 'tvl: 1505498759']`, OR\n"
            "    (b) Write the claim WITHOUT a citation and prefix it "
            "`unsourced:` — e.g. `unsourced: yields appear competitive vs peers`.\n"
            "  Do NOT cite a chunk number for a figure that does not appear "
            "verbatim in that chunk. Inferring a plausible TVL/APR/price from "
            "field names, related magnitudes, or surrounding context is a "
            "hallucination and disqualifies the turn. When in doubt, write "
            "the qualitative claim with `unsourced:` rather than fabricate a "
            "specific number.\n"
        )
    )
    return (
        f"Research question: {idea}\n\n"
        f"Protocol in scope: {protocol}\n\n"
        f"Retrieved corpus chunks (numbered for citation):\n{_format_chunks(chunks)}"
        f"{breadth_block}\n\n"
        "Each persona contributes once, in this order: "
        "technical_analyst → sentiment_analyst → fundamental_analyst → "
        "risk_manager → strategist → bull_bear_debater → coordinator. "
        "End your turn with the exact closing line specified by your role."
    )


def _reply_text(reply: Any) -> str:
    if isinstance(reply, dict):
        content = reply.get("content")
        return str(content) if content is not None else ""
    if reply is None:
        return ""
    return str(reply)


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _extract_json_block(text: str) -> dict[str, Any] | None:
    """Pull the ```json ... ``` fenced block from the coordinator turn."""
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_closing_line(agent: str, text: str) -> dict[str, Any] | None:
    """Match the agent's closing-line regex against the last non-empty line.

    Returns a structured dict like ``{"trend_verdict": "bullish"}`` keyed by
    a stable name per persona. ``None`` when the line is missing or doesn't
    match — caller should treat as a soft parse failure.
    """
    pattern = _CLOSING_RE.get(agent)
    if pattern is None:
        return None
    line = _last_nonempty_line(text)
    if not line:
        return None
    m = pattern.match(line)
    if not m:
        return None
    captured = m.group(1).strip()
    key_map = {
        TECHNICAL_ANALYST: "trend_verdict",
        SENTIMENT_ANALYST: "sentiment_band",
        FUNDAMENTAL_ANALYST: "protocol_health",
        RISK_MANAGER: "risk_band",
        STRATEGIST: "strategic_intent",
        BULL_BEAR_DEBATER: "decisive_question",
        COORDINATOR: "verdict",
    }
    return {key_map[agent]: captured}


# Maps each non-coordinator persona's parsed verdict value to the
# "directional bias" we compare against the coordinator's act/pass call.
# 'act' aligns with bullish/greed/growing/acceptable/<intent present>;
# 'pass' aligns with bearish/fear/degraded/unacceptable.
# Ambiguous values (mixed/neutral/stable/elevated) count as no-vote.
_VERDICT_ALIGNS_ACT = {
    TECHNICAL_ANALYST: {"bullish"},
    SENTIMENT_ANALYST: {"greed"},
    FUNDAMENTAL_ANALYST: {"growing"},
    RISK_MANAGER: {"acceptable"},
}
_VERDICT_ALIGNS_PASS = {
    TECHNICAL_ANALYST: {"bearish"},
    SENTIMENT_ANALYST: {"fear"},
    FUNDAMENTAL_ANALYST: {"degraded"},
    RISK_MANAGER: {"unacceptable"},
}


def _voice_directional(agent: str, parsed: dict[str, Any] | None) -> str | None:
    """Return 'act', 'pass', or None for a non-coordinator voice."""
    if parsed is None:
        return None
    if agent in _VERDICT_ALIGNS_ACT:
        # Only the four primary analysts vote directionally; strategist and
        # debater outputs are free-text and don't map cleanly.
        key = next(iter(parsed.keys()))
        val = parsed.get(key)
        if isinstance(val, str):
            v = val.strip().lower()
            if v in _VERDICT_ALIGNS_ACT[agent]:
                return "act"
            if v in _VERDICT_ALIGNS_PASS[agent]:
                return "pass"
    return None


def _count_dissent(turns: list[TradePanelTurn], final_verdict: str) -> int:
    """How many primary analysts pointed AGAINST the coordinator's call."""
    if final_verdict not in {"act", "pass"}:
        # 'defer' has no clean opposite — return 0 rather than guess.
        return 0
    opposite = "pass" if final_verdict == "act" else "act"
    count = 0
    for turn in turns:
        if turn.agent == COORDINATOR:
            continue
        directional = _voice_directional(turn.agent, turn.parsed_verdict)
        if directional == opposite:
            count += 1
    return count


# Tokens that mean "this voice did not pick a direction" — S24 night-shift.
# Used by both the abstain-count defer rule AND the confidence-penalty
# normalization in _build_verdict_from_coordinator.
_ABSTAIN_TOKENS: dict[str, set[str]] = {
    TECHNICAL_ANALYST: {"mixed"},
    SENTIMENT_ANALYST: {"neutral"},
    FUNDAMENTAL_ANALYST: {"stable"},
    RISK_MANAGER: {"elevated"},
}


def _count_abstains(turns: list[TradePanelTurn]) -> int:
    """How many of the four primary analysts abstained from a direction."""
    count = 0
    for turn in turns:
        if turn.agent not in _ABSTAIN_TOKENS:
            continue
        parsed = turn.parsed_verdict or {}
        if not parsed:
            continue
        val = next(iter(parsed.values()), None)
        if isinstance(val, str) and val.strip().lower() in _ABSTAIN_TOKENS[turn.agent]:
            count += 1
    return count


# S24 night-shift confidence anchor — coordinator prompt instructs the
# model to start from 0.70 and apply penalties. We re-apply the same
# rules in code as a safety net against collapsed-band failure mode.
_CONF_ANCHOR = 0.70
_CONF_PENALTY_PER_DISSENT = 0.10
_CONF_PENALTY_PER_ABSTAIN = 0.05
_CONF_FLOOR = 0.30
_CONF_CEILING = 0.85


def _coerce_verdict_token(raw: Any) -> TradeVerdictLiteral:
    """Squash a free-form verdict string into the Literal — strict whitelist."""
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v in {"act", "pass", "defer"}:
            return cast(TradeVerdictLiteral, v)
    # Fallback: defer is the safest unknown-state.
    return "defer"


def _coerce_confidence(raw: Any) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, val))


# S34-#70 — confidence parity. The coordinator turn carries TWO
# confidence-bearing surfaces: the ```json``` fenced block (a `confidence`
# field) and the prose body (the coordinator prompt asks the model to
# *state* its confidence inline, e.g. "Confidence: 0.70"). gpt-4o-mini
# does not reliably keep the two numbers in sync — a prod smoke returned
# envelope confidence 0.4 while the turn prose said 0.70. Two numbers for
# one verdict is a trust bug. We extract the prose number here so
# `_build_verdict_from_coordinator` can reconcile both surfaces in CODE
# (per repo memory feedback_prompt_iteration_plateau: confidence
# reconciliation belongs in code, not a prompt instruction — gpt-4o-mini
# rounds toward caution on any prompt directive).
_PROSE_CONFIDENCE_RE = re.compile(
    r"confidence[\s:=]*(?:is|of|at|=)?\s*([01](?:\.\d+)?|0?\.\d+|\d{1,3})\s*(%?)",
    re.IGNORECASE,
)
# Two surfaces "agree" within this tolerance — below it, no reconciliation
# (0.70 vs 0.72 is rounding noise, not a disagreement).
_CONF_PARITY_EPSILON = 0.05


def _extract_prose_confidence(text: str) -> float | None:
    """Pull a stated confidence number from the coordinator's prose body.

    Returns the value normalized to [0,1], or ``None`` when the prose
    states no confidence. Tolerates both fraction form ("0.70") and
    percentage form ("70%"). Pure + deterministic — unit-testable with a
    plain string, no panel run required.
    """
    last: float | None = None
    for m in _PROSE_CONFIDENCE_RE.finditer(text):
        num_s, pct = m.group(1), m.group(2)
        try:
            val = float(num_s)
        except ValueError:  # pragma: no cover - regex guarantees numeric
            continue
        if pct == "%" or val > 1.0:
            val = val / 100.0
        if 0.0 <= val <= 1.0:
            last = val  # last stated confidence wins (closing summary)
    return last


def _reconcile_confidence(
    block_confidence: float,
    prose_confidence: float | None,
) -> tuple[float, bool]:
    """S34-#70 — collapse two confidence surfaces into ONE deterministic value.

    Parity rule:
      - No prose confidence stated → trust the JSON block (the structured
        contract surface).
      - Surfaces agree within ``_CONF_PARITY_EPSILON`` → JSON block value
        (the canonical structured field; rounding noise is not a conflict).
      - Surfaces DISAGREE → take ``min(block, prose)``. A disagreement is
        itself evidence the model is not converged on its own conviction;
        a low-conviction verdict must not inherit the higher of two
        self-reports. We pick the lower surface (conservative) rather than
        always-JSON because the bug we are fixing is an *inflated* envelope
        number — the lower value is the safe deterministic choice.

    Returns ``(confidence, disagreed)``. ``disagreed`` lets the caller log
    the parity event without a schema change.
    """
    if prose_confidence is None:
        return block_confidence, False
    if abs(block_confidence - prose_confidence) <= _CONF_PARITY_EPSILON:
        return block_confidence, False
    return min(block_confidence, prose_confidence), True


def _coerce_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _build_verdict_from_coordinator(
    turns: list[TradePanelTurn],
) -> TradePanelVerdict:
    """Assemble the final TradePanelVerdict from the coordinator's last turn.

    Prefers the JSON-fenced block (richer structure); falls back to the
    closing-line capture for the verdict token alone.
    """
    coord_turn = next((t for t in turns if t.agent == COORDINATOR), None)
    if coord_turn is None:
        # No coordinator turn at all — defer with empty drivers, dissent 0.
        return TradePanelVerdict(
            verdict="defer",
            confidence=0.0,
            key_drivers=[],
            dissent_count=0,
            blocker_questions=["coordinator turn missing"],
            turns=turns,
        )

    block = _extract_json_block(coord_turn.content) or {}
    closing = coord_turn.parsed_verdict or {}

    verdict = _coerce_verdict_token(block.get("verdict") or closing.get("verdict"))

    # S34-#70 — confidence parity. The JSON block and the prose body can
    # state two different confidence numbers for the same verdict. Collapse
    # them to ONE deterministic value here (in code), then feed the result
    # into the existing S24 band-normalization. min(block, prose) on a
    # disagreement: a split self-report is a low-conviction signal, so the
    # envelope must not inherit the higher number.
    block_confidence = _coerce_confidence(block.get("confidence", 0.5))
    prose_confidence = _extract_prose_confidence(coord_turn.content)
    raw_confidence, _conf_disagreed = _reconcile_confidence(block_confidence, prose_confidence)
    if _conf_disagreed:
        _log.warning(
            "trade_panel.confidence_parity_conflict block=%.2f prose=%.2f resolved=%.2f",
            block_confidence,
            prose_confidence,
            raw_confidence,
        )

    key_drivers = _coerce_str_list(block.get("key_drivers"))
    blocker_questions = _coerce_str_list(block.get("blocker_questions"))

    # S24 night-shift: always re-derive dissent + abstain from analyst turns.
    # The coordinator's self-report is advisory but not authoritative — we've
    # seen the model under-count both in practice (jupiter-2025Q1 fixture
    # reported dissent=3 but coordinator still passed at 0.75).
    derived_dissent = _count_dissent(turns, verdict)
    derived_abstains = _count_abstains(turns)
    raw_dissent = block.get("dissent_count")
    if isinstance(raw_dissent, int) and raw_dissent >= 0:
        dissent_count = max(raw_dissent, derived_dissent)
    else:
        dissent_count = derived_dissent

    # S24 night-shift defer override (rule iii in coordinator prompt):
    # high-conviction dissent against a 'pass' call → flip to defer and
    # surface the disagreement as a blocker. Mirror for 'act' for
    # symmetry. Three voices opposing a directional call is structural
    # disagreement, not noise.
    if verdict in {"act", "pass"} and dissent_count >= 3:
        blocker_questions = [
            *blocker_questions,
            f"3+ analysts dissent against the '{verdict}' call — resolve which voice's "
            "lens is wrong before committing.",
        ]
        verdict = "defer"

    # S24 night-shift abstain-floor defer rule: 3+ genuine abstains across
    # the four primary analysts means the panel lacks evidence to call.
    # This catches the case where the coordinator forces a direction despite
    # most voices declining to call. Skipped when verdict is already defer.
    if verdict in {"act", "pass"} and derived_abstains >= 3:
        blocker_questions = [
            *blocker_questions,
            f"{derived_abstains} of 4 primary analysts abstained — corpus too thin "
            "to support a directional call.",
        ]
        verdict = "defer"

    # S24 night-shift confidence-band normalization. Coordinator prompt
    # asks the model to start at 0.70 and subtract penalties; we enforce
    # the same shape in code as a safety net (the model collapsed to
    # 0.75 across all 10 fixtures on 2026-05-12 despite the prompt).
    # We take the MIN of (model's self-report, anchor - penalties) so the
    # model can lower confidence further on its own judgment but cannot
    # silently inflate past the penalty-adjusted ceiling.
    penalty = (
        _CONF_PENALTY_PER_DISSENT * dissent_count + _CONF_PENALTY_PER_ABSTAIN * derived_abstains
    )
    anchored = _CONF_ANCHOR - penalty
    if verdict == "defer":
        # Defer carries its own meaning; keep model's self-report but
        # clamp to ceiling. No anchoring penalty applied — a defer
        # IS the response to weak signal.
        confidence = min(raw_confidence, _CONF_CEILING)
    else:
        # Take the model's confidence but cap it at the anchored ceiling.
        # If the model under-reports relative to penalties, trust the
        # model (it may see a clean signal the heuristic missed).
        confidence = min(raw_confidence, anchored)
        confidence = max(_CONF_FLOOR, min(_CONF_CEILING, confidence))

    return TradePanelVerdict(
        verdict=verdict,
        confidence=round(confidence, 2),
        key_drivers=key_drivers,
        dissent_count=dissent_count,
        blocker_questions=blocker_questions,
        turns=turns,
    )


# Type alias for the agent-factory callback tests inject. Production
# callers don't pass this — build_groupchat is used by default.
AgentFactory = Callable[[dict[str, Any]], dict[str, _LLMReplier]]


async def run_trade_panel(
    idea: str,
    protocol: str,
    retrieved_chunks: list[dict[str, Any]],
    *,
    tier: str = "basic",
    llm_config: dict[str, Any] | None = None,
    agent_factory: AgentFactory | None = None,
    agent_id: str | None = None,
    wallet: str | None = None,
) -> TradePanelVerdict:
    """Run the 7-agent trade research panel.

    Args:
        idea: The user's research question.
        protocol: Protocol in scope (e.g. ``"kamino"``, ``"jito"``).
        retrieved_chunks: Pre-fetched corpus chunks. Phase 8b's caller does
            the retrieval; this function does NOT touch the vector store.
        tier: ``"basic"`` (default) or ``"pro"``. Currently only used by
            Phase 8b's caller for routing/cost; v1 panel logic is identical
            across tiers.
        llm_config: AG2 llm_config. Required for production paths. Tests
            pass ``agent_factory`` and may omit this.
        agent_factory: Test-only hook. Given the llm_config, returns a
            ``{persona_name: replier}`` mapping. When provided, the AG2
            GroupChat is bypassed entirely — useful for fakes that don't
            require autogen installed.

    Returns:
        :class:`TradePanelVerdict` with all 7 turns + the coordinator's
        final verdict shape.
    """
    if not agent_factory and llm_config is None:
        raise ValueError(
            "run_trade_panel requires either llm_config (production) or "
            "agent_factory (tests). Got neither."
        )

    seed = _opening_prompt(idea, protocol, retrieved_chunks)

    # Resolve the per-agent replier map.
    if agent_factory is not None:
        repliers = agent_factory(llm_config or {})
        missing = [n for n in REQUIRED_AGENTS if n not in repliers]
        if missing:
            raise ValueError(f"agent_factory returned no replier for required personas: {missing}")
    else:
        manager = build_groupchat(llm_config or {})
        repliers = {a.name: cast(_LLMReplier, a) for a in manager.groupchat.agents}

    # Round-robin: each agent sees the seed + all prior turns. We append
    # turns into a shared message list as we go so the coordinator (last)
    # gets the full panel context.
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": seed},
    ]
    turns: list[TradePanelTurn] = []

    for agent_name in REQUIRED_AGENTS:
        replier = repliers[agent_name]
        try:
            reply = await asyncio.wait_for(
                replier.a_generate_reply(messages=list(messages)),
                timeout=_PER_VOICE_TIMEOUT_S,
            )
        except TimeoutError:
            content = f"(voice failed: timeout after {int(_PER_VOICE_TIMEOUT_S)}s)"
            _log.warning("trade_panel.voice_timeout agent=%s", agent_name)
            turns.append(TradePanelTurn(agent=agent_name, content=content, parsed_verdict=None))
            messages.append({"role": "assistant", "content": content})
            continue
        except Exception as exc:  # pragma: no cover - defensive
            content = f"(voice failed: {type(exc).__name__})"
            _log.warning("trade_panel.voice_error agent=%s err=%s", agent_name, exc)
            turns.append(TradePanelTurn(agent=agent_name, content=content, parsed_verdict=None))
            messages.append({"role": "assistant", "content": content})
            continue

        text = _reply_text(reply)
        parsed = _parse_closing_line(agent_name, text)
        turns.append(TradePanelTurn(agent=agent_name, content=text, parsed_verdict=parsed))
        messages.append({"role": "assistant", "content": text})

    # S24 WS-F #6 — emit oracle.cost_usd. AG2 ConversableAgents don't
    # surface the OpenAI usage object cleanly; we estimate from tiktoken
    # over the seed prompt (shared by all 7 voices) and each turn's
    # content. tokens_in = seed_tokens * len(voices) (each voice sees the
    # accumulating message log; approximate as seed * N since per-voice
    # context grows but is dominated by the seed + previous turns). To
    # avoid over-estimating, count once and add accumulating prior-turn
    # tokens explicitly. embed_tokens = idea tokens (the retrieval embed
    # call is upstream but we attribute its cost to the panel run).
    try:
        from gecko_core.routing.costs import estimate_cost_usd, estimate_tokens

        seed_tokens = estimate_tokens(seed)
        turn_tokens = [estimate_tokens(t.content) for t in turns]
        # Each voice sees seed + prior turns => sum_i (seed + sum_{j<i} turn_j)
        tokens_in = 0
        accum = seed_tokens
        for tt in turn_tokens:
            tokens_in += accum
            accum += tt
        tokens_out = sum(turn_tokens)
        embed_tokens = estimate_tokens(idea)
        # Default model — basic = gpt-4o-mini, pro = gpt-4o.
        model_id = "gpt-4o" if tier == "pro" else "gpt-4o-mini"
        llm_cost = estimate_cost_usd(model_id, tokens_in=tokens_in, tokens_out=tokens_out)
        # text-embedding-3-small is ~$0.02 per 1M tokens.
        embed_cost = embed_tokens * 0.02 / 1_000_000
        total_usd = round(llm_cost + embed_cost, 6)
        await emit_event(
            "oracle.cost_usd",
            {
                "agent_id": agent_id,
                "tier": tier,
                "model": model_id,
                "llm_tokens_in": tokens_in,
                "llm_tokens_out": tokens_out,
                "embed_tokens": embed_tokens,
                "total_usd": total_usd,
                "protocol": protocol,
            },
            wallet=wallet,
        )
    except Exception as exc:  # never block verdict synthesis on cost-emit
        _log.warning("trade_panel.cost_emit_failed err=%s", exc)

    return _build_verdict_from_coordinator(turns)


# ---------------------------------------------------------------------------
# Phase 8b — retrieval glue
#
# The panel itself stays retrieval-agnostic (Phase 8a contract). This
# convenience wrapper is the public entry point the MCP tool + REST endpoint
# call: it embeds the question once, reads top-K chunks scoped to
# (vertical, protocol) from the trading-oracle corpus, and forwards into
# run_trade_panel.
#
# Why filter shape: the `vertical` field IS declared as a filterable path on
# the chunks_vector index (see CHUNKS_VECTOR_FILTER_FIELDS); `protocol` is
# NOT. We push `vertical` into $vectorSearch.filter (Atlas pre-filters before
# the ANN graph traversal) and post-$match on `protocol` after the kNN. This
# keeps round trips at one and avoids a noisy index migration just for the
# trade-research surface.
# ---------------------------------------------------------------------------

_DEFAULT_TRADE_TOP_K: int = 15  # S34 #87 — raised 5→15. The S34-#86 retrieval eval found provider_kind_coverage=0.567 at top_k=5 (fails the 0.8 gate) vs 0.967 at top_k=15; the canon-floor quota starves protocol_native at the narrow k. Per-call cost delta is ~$0.0038 (gpt-4o-mini basic tier) — _RAG_CONTEXT_CHAR_CAP (60k chars) does NOT bind at k=15 (~24k chars), so the 3× chunk slate is real. See docs/eval/2026-05-17-s34-topk-cost-model.md.

# S25 #13 — scoring boost terms. Empirical: Atlas vectorSearchScore on the
# corpus sits in [0.69, 0.78] for typical queries; a +0.15 boost pushes a
# protocol-exact chunk past a tangentially-relevant canon chunk without
# crowding genuinely top-scored content. Date demotion is bounded so that
# market_data outside the fixture window doesn't fall off the result —
# the panel still sees it but pays attention to protocol manifest first.
_BOOST_PROTOCOL_EXACT: float = 0.15
_BOOST_PROVIDER_SPECIFIC: float = 0.10  # paysh_live / bazaar_live with protocol match
_BOOST_DATE_ALIGNED: float = 0.05
_DEMOTE_DATE_MISALIGNED: float = -0.10
_DATE_WINDOW_DAYS: int = 7


def _parse_chunk_date(chunk: dict[str, Any]) -> str | None:
    """Best-effort extract of an ISO date string (YYYY-MM-DD) from a chunk.

    Looks at, in order:
      1. ``metadata.timestamp`` (ingest-set; canonical when present)
      2. ``metadata.as_of_iso`` (ingest-set on some market_data writes)
      3. ``captured_at`` (Mongo write-time timestamp; falls back to ingest
         time if no domain-level date is available)
      4. ``source_url`` suffix ``#<iso>`` (the market_data plan hashes the
         hour bucket into the URL; we strip the time portion)
      5. ``text`` regex for ``as of YYYY-MM-DDTHH:MM`` or ``YYYY-MM-DD``

    Returns None when no parseable date is found. Caller treats None as
    "do not apply date scoring" (neutral).
    """
    md = chunk.get("metadata") or {}
    for key in ("timestamp", "as_of_iso"):
        val = md.get(key) if isinstance(md, dict) else None
        if isinstance(val, str) and len(val) >= 10:
            return val[:10]
        # Pymongo returns BSON datetimes as native datetime objects;
        # support both shapes so the date demotion fires regardless.
        if isinstance(val, datetime):
            try:
                return val.strftime("%Y-%m-%d")
            except Exception:
                pass
    captured = chunk.get("captured_at")
    # Only use captured_at for hot/live chunks — for static canon the
    # ingest date is not a content date.
    if (
        isinstance(captured, str)
        and len(captured) >= 10
        and chunk.get("freshness_tier") in {"hot", "live_only", "daily"}
    ):
        return captured[:10]
    url = str(chunk.get("source_url") or "")
    if "#" in url:
        tail = url.rsplit("#", 1)[-1]
        if len(tail) >= 10 and tail[4] == "-" and tail[7] == "-":
            return tail[:10]
    text = chunk.get("text") or ""
    m = re.search(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)", text)
    if m:
        return m.group(1)
    return None


def _days_between(a: str, b: str) -> int | None:
    """Absolute days between two YYYY-MM-DD strings. None on parse failure."""
    from datetime import date

    try:
        da = date.fromisoformat(a)
        db = date.fromisoformat(b)
    except ValueError:
        return None
    return abs((da - db).days)


def _apply_retrieval_boosts(
    rows: list[dict[str, Any]],
    *,
    protocol: str,
    as_of_date: str | None,
) -> list[dict[str, Any]]:
    """Re-score retrieved chunks with protocol/provider/date boosts.

    Pure function — input rows carry an ``Atlas vectorSearchScore`` in the
    ``score`` field; this writes back the boosted score and preserves the
    original under ``raw_score`` for diagnostics. Sorted descending by the
    boosted score on return.

    Boost terms:
      - +0.15 when ``protocol`` appears in the chunk's protocol tag list
        (treats array-typed and scalar-typed protocol fields uniformly).
      - +0.10 additional when provider_kind is paysh_live/bazaar_live AND
        the protocol match also fires (these are protocol-manifest sources
        whose semantic content is often sparse but on-topic and structurally
        what the panel needs to ground a directional verdict).
      - +0.05 when chunk's parseable date is within the date window of
        ``as_of_date``; -0.10 when outside. Skipped entirely when
        ``as_of_date`` is None or the chunk is not freshness=hot/live/daily.

    Boosts compose additively. A protocol-exact paysh_live chunk dated
    in-window stacks to +0.30 over the raw Atlas score.
    """
    proto_norm = (protocol or "").strip().lower()
    PROVIDER_SPECIFIC_KINDS = {"paysh_live", "bazaar_live", "protocol_native"}

    for row in rows:
        raw = float(row.get("score") or 0.0)
        row["raw_score"] = raw
        boost = 0.0
        chunk_protos = row.get("protocol")
        proto_match = False
        if isinstance(chunk_protos, list):
            proto_match = proto_norm in {str(p).strip().lower() for p in chunk_protos}
        elif isinstance(chunk_protos, str):
            proto_match = chunk_protos.strip().lower() == proto_norm
        if proto_match and proto_norm:
            boost += _BOOST_PROTOCOL_EXACT
            if row.get("provider_kind") in PROVIDER_SPECIFIC_KINDS:
                boost += _BOOST_PROVIDER_SPECIFIC
        # Date alignment — only for hot/live/daily chunks where the date
        # carries information. Static canon (Marks, Damodaran, Berkshire)
        # is intentionally date-agnostic.
        if as_of_date and row.get("freshness_tier") in {"hot", "live_only", "daily"}:
            chunk_date = _parse_chunk_date(row)
            if chunk_date is not None:
                days = _days_between(as_of_date, chunk_date)
                if days is not None:
                    if days <= _DATE_WINDOW_DAYS:
                        boost += _BOOST_DATE_ALIGNED
                    else:
                        boost += _DEMOTE_DATE_MISALIGNED
        row["score"] = raw + boost
        row["score_boost"] = boost

    rows.sort(key=lambda r: float(r.get("score") or 0.0), reverse=True)
    return rows


# ---------------------------------------------------------------------------
# S27 #21 — provider_kind quota allocator (fixed-slot, post-boost).
#
# Problem: S25 #13's protocol-exact + provider-specific boosts stack to +0.25
# on protocol_native chunks. A pure top_k-by-boosted-score slice then routinely
# fills all 15 slots with protocol_native, even though the rubric judge grades
# against `must_cite_provider_kinds = {paysh_live, market_data, canon_marks,
# canon_damodaran, canon_berkshire}`. S26 lost-in-middle handoff documents
# `provider_kinds_present=['protocol_native']` on 2/3 fixtures, giving
# provider_kind_coverage=0.33 against threshold 1.00.
#
# Fix: after _apply_retrieval_boosts has scored the candidate pool, partition
# by provider_kind and fill per-bucket quotas first. Unfilled slots overflow
# to other buckets by global boosted score. Boost still informs ranking
# within each bucket — the allocator only decides who gets into top_k.
#
# Universal V1 quota (question-class-blind). Sum (19) intentionally exceeds
# top_k (15) so higher-quota buckets win when supply is plentiful and lower-
# quota buckets get squeezed. Question-class-aware quotas are S28+ work.
_PROVIDER_QUOTAS: dict[str, int] = {
    "protocol_native": 5,
    "canon_marks": 3,
    "canon_damodaran": 3,
    "canon_berkshire": 2,
    "market_data": 2,
    "bazaar_live": 1,
    "paysh_live": 1,
    "canon_youtube": 1,
    "canon_macro": 1,
}

# Minimum text length for a chunk to count toward its provider_kind quota.
# paysh_live chunks for many protocols are empty `{"data":[]}` API dumps
# (Mongo audit 2026-05-13: paysh_live kamino total=7, substantive=0).
# Forcing a quota slot on those is worse than leaving the slot empty for
# overflow into a substantive bucket.
_QUOTA_MIN_CHUNK_CHARS: int = 200


def _provider_quota_floor(
    rows: list[dict[str, Any]],
    *,
    top_k: int,
    quotas: dict[str, int] | None = None,
    min_chars: int = _QUOTA_MIN_CHUNK_CHARS,
) -> list[dict[str, Any]]:
    """Allocate ``top_k`` slots across provider_kinds via fixed quotas.

    Algorithm:
      1. Drop candidates whose text is shorter than ``min_chars`` from
         quota eligibility (they can still surface via the last-resort
         filler if no eligible chunk is available).
      2. Partition the eligible pool by ``provider_kind``; within each
         bucket sort by boosted score desc.
      3. For each provider_kind in ``quotas``, take up to its quota
         from the bucket. Buckets shorter than their quota leave
         unfilled slots.
      4. Overflow: fill any remaining slots from the global eligible
         pool by boosted score, skipping anything already picked.
      5. Last-resort: ineligible (short-text) chunks fill out top_k
         when the corpus is genuinely thin.
      6. Final sort by boosted score desc so the panel sees strongest first.

    Pure function — returns a new list; input rows are not mutated.
    """
    if top_k <= 0 or not rows:
        return []
    quotas = quotas if quotas is not None else _PROVIDER_QUOTAS

    def _eligible(row: dict[str, Any]) -> bool:
        text = row.get("text") or ""
        return len(text) >= min_chars

    by_kind: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        pk = str(row.get("provider_kind") or "web")
        by_kind.setdefault(pk, []).append(row)
    for bucket in by_kind.values():
        bucket.sort(key=lambda r: float(r.get("score") or 0.0), reverse=True)

    picked: list[dict[str, Any]] = []
    picked_ids: set[int] = set()

    # Pass 1 — per-bucket quotas over eligible chunks.
    for pk, quota in quotas.items():
        bucket = by_kind.get(pk, [])
        taken = 0
        for row in bucket:
            if taken >= quota:
                break
            if not _eligible(row):
                continue
            rid = id(row)
            if rid in picked_ids:
                continue
            picked.append(row)
            picked_ids.add(rid)
            taken += 1
            if len(picked) >= top_k:
                break
        if len(picked) >= top_k:
            break

    # Pass 2 — overflow. Fill remaining slots from the global eligible
    # pool by boosted score desc.
    if len(picked) < top_k:
        remaining = sorted(
            (r for r in rows if id(r) not in picked_ids),
            key=lambda r: float(r.get("score") or 0.0),
            reverse=True,
        )
        for row in remaining:
            if not _eligible(row):
                continue
            picked.append(row)
            picked_ids.add(id(row))
            if len(picked) >= top_k:
                break
        # Last-resort: ineligible chunks. Keeps top_k honest when the
        # corpus is thin. Rare in practice.
        if len(picked) < top_k:
            for row in remaining:
                if id(row) in picked_ids:
                    continue
                picked.append(row)
                picked_ids.add(id(row))
                if len(picked) >= top_k:
                    break

    picked.sort(key=lambda r: float(r.get("score") or 0.0), reverse=True)
    return picked


# S33-#82 — canon retrieval floor. The trade-idea query embeds ~0.55 cosine
# to protocol_native API text and only ~0.38-0.41 to canon investor-canon
# prose, so canon loses the single-pool ANN race outright (0/75 every
# fixture — see docs/eval/2026-05-16-s33-retrieval-pipeline-validation.md).
# A single $vectorSearch can never surface canon for these queries. The fix
# is a structural floor: a SECOND $vectorSearch leg pre-filtered to canon
# provider_kinds (an indexed filterable path) guarantees N canon chunks
# reach the panel alongside protocol_native. The two slates are merged
# BEFORE the Voyage reranker so the cross-encoder reorders a slate that
# actually contains canon. This mirrors rag_query's _rerank_by_provider
# per-kind quota rescue, which the trade-panel path previously lacked.
_CANON_PROVIDER_KINDS: tuple[str, ...] = tuple(
    pk for pk in PROVIDER_KINDS if pk.startswith("canon_")
)
# Floor of canon chunks guaranteed in the final top_k slate. The canon leg
# fetches a per-kind-balanced pool (see _retrieve_canon_floor) and the
# post-rerank quota reserves this many slots for it. 6 of top_k=15 keeps
# protocol_native the majority voice while guaranteeing the panel sees a
# diverse canon mix.
_CANON_FLOOR_COUNT: int = 6
# Per-canon-kind fetch cap for the canon leg. A single canon $vectorSearch
# is monopolised by whichever canon kind sits closest in embedding space —
# measured: canon_macro (Fed/BIS papers) wins the canon ANN race for trade
# queries and a single pooled leg returns 6/6 canon_macro, starving the
# canon_marks / canon_damodaran the fixtures actually demand. So the canon
# leg issues ONE $vectorSearch PER canon kind, capped at this many each,
# then round-robin merges — guaranteeing kind diversity by construction.
_CANON_PER_KIND_CAP: int = 4


async def _retrieve_canon_floor(
    *,
    query_vector: list[float],
    vertical: str,
    floor: int,
) -> list[dict[str, Any]]:
    """Second $vectorSearch leg, pre-filtered to canon ``provider_kind``s.

    S33-#82. The main leg's slate is monoculture ``protocol_native`` because
    a trade-idea query is far closer to API text than to investor-canon
    prose. This leg filters the ANN search to the canon kinds via the
    ``provider_kind`` indexed filterable path, so canon chunks are ranked
    only against *each other* and a guaranteed ``floor`` of them survives.

    Returns up to ``floor`` plain dicts shaped identically to the main
    leg's rows (same ``$project`` keys). Canon chunks carry ``protocol=[]``
    so they need no protocol ``$match`` — they are cross-cutting frameworks
    valid for every protocol. Degrades to ``[]`` on any error; a canon-leg
    failure must never break retrieval.
    """
    if floor <= 0 or not _CANON_PROVIDER_KINDS:
        return []

    from gecko_core.db.mongo import VECTOR_INDEX_NAME, chunks_collection

    coll = chunks_collection()
    if coll is None:
        return []

    def _row_from_doc(doc: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(doc.get("_id", "")),
            "text": doc.get("text") or "",
            "source_url": doc.get("source_url") or "",
            "source": (doc.get("source") or doc.get("provider_kind") or "unknown"),
            "provider_kind": doc.get("provider_kind") or "web",
            "freshness_tier": doc.get("freshness_tier") or "static",
            "protocol": doc.get("protocol") or [],
            "vertical": doc.get("vertical") or vertical,
            "score": float(doc.get("score") or 0.0),
        }

    project_stage: dict[str, Any] = {
        "$project": {
            "_id": 1,
            "source_url": 1,
            "text": 1,
            "vertical": 1,
            "protocol": 1,
            "provider_kind": 1,
            "freshness_tier": 1,
            "source": 1,
            "metadata": 1,
            "score": {"$meta": "vectorSearchScore"},
        }
    }

    # One $vectorSearch PER canon kind, capped at _CANON_PER_KIND_CAP each.
    # A single pooled canon leg is monopolised by the canon kind closest in
    # embedding space (canon_macro for trade queries); per-kind legs
    # guarantee canon_marks / canon_damodaran candidates exist before the
    # round-robin merge. Each leg is small (cap ~4) so the extra Atlas round
    # trips are cheap. A per-kind leg failure degrades that kind only.
    per_kind: dict[str, list[dict[str, Any]]] = {}
    for kind in _CANON_PROVIDER_KINDS:
        pipeline: list[dict[str, Any]] = [
            {
                "$vectorSearch": {
                    "index": VECTOR_INDEX_NAME,
                    "path": "embedding",
                    "queryVector": query_vector,
                    "numCandidates": max(200, _CANON_PER_KIND_CAP * 40),
                    "limit": _CANON_PER_KIND_CAP,
                    "exact": False,
                    "filter": {
                        "vertical": {"$eq": vertical},
                        "provider_kind": {"$eq": kind},
                        "metadata.deprecated": {"$ne": True},
                    },
                }
            },
            project_stage,
        ]
        kind_rows: list[dict[str, Any]] = []
        try:
            async for doc in coll.aggregate(pipeline):
                kind_rows.append(_row_from_doc(doc))
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("trade_panel.retrieve.canon_leg_error kind=%s err=%s", kind, exc)
            continue
        if kind_rows:
            per_kind[kind] = kind_rows

    if not per_kind:
        return []

    # Round-robin across canon kinds so the returned pool is kind-balanced:
    # take the top chunk of each kind, then the 2nd of each, etc. The
    # post-rerank quota downstream re-scores this pool, but a balanced pool
    # in means a balanced (canon_marks + canon_damodaran + ...) set survives.
    merged: list[dict[str, Any]] = []
    rank = 0
    while True:
        added_this_round = False
        for kind in _CANON_PROVIDER_KINDS:
            rows_for_kind = per_kind.get(kind, [])
            if rank < len(rows_for_kind):
                merged.append(rows_for_kind[rank])
                added_this_round = True
        if not added_this_round:
            break
        rank += 1
    return merged


async def retrieve_trade_corpus_chunks(
    *,
    idea: str,
    protocol: str,
    vertical: str = "dex",
    top_k: int = _DEFAULT_TRADE_TOP_K,
    as_of_date: str | None = None,
) -> list[dict[str, Any]]:
    """Embed ``idea`` and read top-K chunks scoped to ``(vertical, protocol)``.

    Returns a list of plain dicts shaped for ``run_trade_panel`` (``text`` +
    ``source`` keys are the only contract). Returns ``[]`` when the chunk
    store is not configured for Mongo, when the embedder yields no vector,
    or when the corpus has no matching rows. Production-only — no Supabase
    fallback because the trading-oracle corpus is Mongo-native.
    """
    if top_k <= 0 or not idea.strip():
        return []
    proto_norm = protocol.strip().lower()
    if not proto_norm:
        return []

    # Issue #12 — diagnostic instrumentation. Log entry to retrieval so we
    # can disambiguate "handler never called retrieval" from "retrieval was
    # called but Atlas returned 0 hits". Protocol/vertical are echoed
    # verbatim so we can spot casing / whitespace / vertical drift between
    # ingest tagging and read-side filter.
    _log.info(
        "trade_panel.retrieve.entry protocol=%s vertical=%s top_k=%d question_len=%d",
        proto_norm,
        vertical,
        top_k,
        len(idea),
    )

    # Lazy imports keep gecko_core's startup cost off the trade_panel package
    # import path (the in-process MCP server imports this module at boot).
    from gecko_core.db import get_chunk_store
    from gecko_core.db.mongo import VECTOR_INDEX_NAME, chunks_collection
    from gecko_core.ingestion.embedder import embed
    from gecko_core.rag.voyage_rerank import voyage_rerank_dicts

    if get_chunk_store() != "mongo":
        _log.warning(
            "trade_panel.retrieve.skip reason=non_mongo_store store=%s",
            get_chunk_store(),
        )
        return []
    coll = chunks_collection()
    if coll is None:
        _log.warning("trade_panel.retrieve.skip reason=no_collection")
        return []

    # S33-#82 — query embed input_type. S33-#79 set input_type="query" on
    # the asymmetric-retrieval assumption. The S33-#81 diagnosis measured
    # the opposite: vs a fixed canon chunk, query-side "query" embedding
    # gives cos 0.38 while symmetric None gives 0.41 and "document" 0.53 —
    # "query" *widens* the query<->canon gap. The live $vectorSearch band
    # (~0.55 true cosine) matches the document-style pairing, not the
    # query-style one. Reverted to symmetric None: it ranks canon strictly
    # higher than "query" and needs no corpus re-embed (the corpus was
    # re-embedded "document" in #80; None query vs document corpus is the
    # closest available pairing without a re-embed). This is a query-side
    # code-only change. NOTE: the structural canon fix is the canon-floor
    # leg below — input_type alone moves cosine ~0.03 but canon still loses
    # the single-pool ANN race outright (0/75 at every input_type).
    vectors, _tokens = await embed([idea], input_type=None)
    if not vectors:
        _log.warning("trade_panel.retrieve.skip reason=empty_embed_vector")
        return []
    query_vector = vectors[0]

    # numCandidates oversized vs. top_k — Atlas's ANN graph needs slack to
    # land good rows after the post-$match on protocol filters out chunks
    # that survive the vertical pre-filter but are tagged for a different
    # protocol. 20x is the same shape `build_filterable_pipeline` uses.
    pipeline: list[dict[str, Any]] = [
        {
            "$vectorSearch": {
                "index": VECTOR_INDEX_NAME,
                "path": "embedding",
                "queryVector": query_vector,
                # S27 Path D — bumped numCandidates from 20x to 40x so the
                # ANN graph has enough slack for the wider 12x post-filter
                # pool (below). limit raised from 10x to 15x for the same
                # reason. Atlas charges by candidate scan; this is one
                # wider scan, not 4x cost.
                "numCandidates": max(400, top_k * 40),
                "limit": top_k * 15,
                "exact": False,
                # S24 WS-A Pattern F — the vertical pre-filter must admit
                # cross-cutting chunks (canon literature, market_data macro
                # feeds) regardless of the requested vertical, otherwise
                # perps/lending/infra/lst requests see zero canon citations
                # because canon is currently tagged vertical="dex". Same
                # spirit as the post-$match: cross-cutting chunks reach the
                # panel for every protocol.
                #
                # S25 #13 — paysh_live + bazaar_live chunks also admitted
                # across verticals (they carry a specific protocol tag but
                # protocol-manifest content is useful regardless of which
                # vertical bucket the caller asked for; e.g. Kamino
                # paysh_live chunks tagged vertical=dex must still reach
                # a vertical=lending request because the protocol manifest
                # documents both vault types).
                "filter": {
                    "$or": [
                        {"vertical": {"$eq": vertical}},
                        {
                            "provider_kind": {
                                "$in": [
                                    "market_data",
                                    "canon_marks",
                                    "canon_damodaran",
                                    "canon_berkshire",
                                    "paysh_live",
                                    "bazaar_live",
                                ]
                            }
                        },
                    ],
                    "metadata.deprecated": {"$ne": True},
                },
            }
        },
        # Protocol-tagged chunks (paysh_live, bazaar_live) match exact;
        # general investor-canon chunks (canon_marks/berkshire/damodaran,
        # tagged protocol=[]) surface for ALL protocols since they're
        # cross-cutting frameworks. See docs/strategy/2026-05-11-
        # retrieval-wedge-sprint.md — this is the wedge: canon corpus
        # must reach the panel regardless of named protocol.
        #
        # S24 WS-A — market_data chunks (Pyth candles + DefiLlama TVL)
        # surface alongside canon. They carry a specific protocol tag for
        # provenance but cross-protocol macro feeds (SOL/USD, BTC/USD)
        # are useful to every voice, so the provider_kind clause admits
        # them regardless of protocol match. Pattern F.
        {
            "$match": {
                "$or": [
                    {"protocol": proto_norm},
                    {"protocol": {"$size": 0}},
                    {"protocol": {"$exists": False}},
                    # S24 WS-A — market_data chunks carry a specific
                    # protocol tag for provenance; admit them post-$match
                    # regardless of the requested protocol (Pattern F).
                    {"provider_kind": "market_data"},
                ]
            }
        },
        # S33-#79 — keep the full over-fetch slate (top_k * 5) here so the
        # Voyage cross-encoder reranker downstream has a wide candidate set
        # to re-score. The true top_k truncation happens *after* rerank
        # (see voyage_rerank_dicts below); on the legacy / flag-off path the
        # reranker no-ops and returns the vector-order slate[:top_k].
        #
        # S25 #13 protocol-exact / date-alignment scoring boosts
        # (_apply_retrieval_boosts) still run on this slate in Python
        # below — they re-rank the protocol pool *before* the cross-encoder
        # sees it. The S27 12x over-fetch was sized for the now-superseded
        # _provider_quota_floor allocator; the S33 canon-floor leg solves
        # canon coverage structurally (a dedicated canon $vectorSearch),
        # so the protocol slate only needs the 5x rerank window.
        {"$limit": top_k * 5},
        {
            "$project": {
                "_id": 1,
                "source_url": 1,
                "text": 1,
                "vertical": 1,
                "protocol": 1,
                "provider_kind": 1,
                "freshness_tier": 1,
                "source": 1,
                "metadata": 1,
                "score": {"$meta": "vectorSearchScore"},
            }
        },
    ]

    rows: list[dict[str, Any]] = []
    try:
        async for doc in coll.aggregate(pipeline):
            rows.append(
                {
                    "id": str(doc.get("_id", "")),
                    "text": doc.get("text") or "",
                    "source_url": doc.get("source_url") or "",
                    # Prefer the catalog-named `source` (e.g. "exa", "zerion",
                    # "paysh"); fall back to the legacy provider_kind tag.
                    "source": (doc.get("source") or doc.get("provider_kind") or "unknown"),
                    "provider_kind": doc.get("provider_kind") or "web",
                    "freshness_tier": doc.get("freshness_tier") or "static",
                    # Preserve raw ingest-side protocol tag (incl. empty
                    # list for canon). Do NOT fall back to proto_norm —
                    # the boost step relies on truthful tagging to avoid
                    # crediting canon chunks with a protocol-exact match.
                    "protocol": doc.get("protocol"),
                    "vertical": doc.get("vertical") or vertical,
                    "score": float(doc.get("score") or 0.0),
                }
            )
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("trade_panel.retrieve.error err=%s", exc)
        return []

    # S25 #13 — apply protocol-exact + provider-specific + date-alignment
    # scoring boosts in Python over the oversampled protocol pool. Atlas raw
    # vectorSearchScore alone buries sparse protocol-manifest chunks beneath
    # tangentially-on-topic prose; the boost re-orders the protocol slate so
    # the cross-encoder reranker (below) sees protocol-grounded chunks first.
    # The boost terms live in _apply_retrieval_boosts.
    #
    # S33 note: _provider_quota_floor (the S27 provider-kind allocator) is
    # intentionally NOT called here. The S33-#81 diagnosis proved canon is
    # 0/75 in the single-pool $vectorSearch slate — the allocator had no
    # canon candidates to allocate. The dedicated canon-floor leg below
    # solves canon coverage structurally; the boost still tunes intra-
    # protocol ranking, which remains useful.
    rows = _apply_retrieval_boosts(rows, protocol=proto_norm, as_of_date=as_of_date)

    # S33-#82 — canon retrieval floor with a POST-rerank quota.
    #
    # An earlier shape merged canon into a single pre-rerank slate, but the
    # cross-encoder scores protocol_native API text above canon prose for a
    # trade-idea query, so a single rerank still truncated canon down to ~1
    # of top_k and, worse, kept whichever canon chunk happened to score
    # highest (often canon_macro) rather than a balanced canon mix. The
    # structural fix is a quota that bites AFTER rerank: rerank the two
    # legs INDEPENDENTLY, then assemble the final top_k as
    # `(top_k - quota)` protocol rows + `quota` canon rows. Each canon
    # slot is still the most query-relevant canon chunk per the
    # cross-encoder — the rerank does the ordering, the quota does the
    # structural guarantee. Mirrors rag_query's _rerank_by_provider
    # reserve_quota, which the trade-panel path previously lacked.
    pre_rerank_count = len(rows)
    canon_rows = await _retrieve_canon_floor(
        query_vector=query_vector,
        vertical=vertical,
        floor=_CANON_FLOOR_COUNT,
    )
    seen_ids = {r["id"] for r in rows if r.get("id")}
    canon_rows = [r for r in canon_rows if r.get("id") not in seen_ids]

    # S33-#79 — semantic rerank. $vectorSearch returns a flat cosine band;
    # cosine alone cannot separate on-target from loosely-related chunks at
    # that resolution. A Voyage rerank-2 cross-encoder re-scores each leg by
    # true query relevance. Flag-gated on GECKO_RERANKER=voyage; graceful-
    # degrades to the vector-order slate on flag-off, missing key, timeout,
    # or any API error — retrieval never breaks on a rerank failure.
    # S34-#85 — canon quota must never starve the protocol head.
    # _CANON_FLOOR_COUNT (6) was sized for the eval's top_k=15 (6/15 = a
    # 40% canon floor, protocol_native keeps the majority).
    # S34-#87 raised production top_k (_DEFAULT_TRADE_TOP_K) 5 -> 15, so
    # production and the eval now share top_k=15 -> canon_quota=6,
    # protocol_slots=9: the 40%-canon split the floor was designed for.
    # The `min(_CANON_FLOOR_COUNT, top_k // 2)` clamp below is kept anyway
    # — it is the load-bearing guard against the S34-#85 regression. At the
    # old top_k=5 the naive `min(6, top_k)` yielded canon_quota=5,
    # protocol_slots=0 — the canon FLOOR silently became a CEILING that
    # consumed the entire slate, and the panel saw 0 protocol_native
    # chunks for a protocol-specific question. That was the 2026-05-16 live
    # finding: 5/5 canon citations on a Kamino vault question, the
    # fundamental_analyst fabricating a "$150M TVL" figure and misciting a
    # BIS canon_macro paper because no protocol chunk was in scope.
    # Fix: clamp canon to at most half the slate so protocol_native always
    # retains a >=ceil(top_k/2) majority. At top_k=5 -> canon_quota=2,
    # protocol_slots=3; at top_k=15 -> canon_quota=6, protocol_slots=9.
    canon_quota = min(_CANON_FLOOR_COUNT, top_k // 2) if canon_rows else 0
    protocol_slots = max(0, top_k - canon_quota)
    protocol_reranked = await voyage_rerank_dicts(idea, rows, top_n=top_k)
    canon_reranked: list[dict[str, Any]] = []
    if canon_rows:
        # Rerank the whole balanced canon pool, then pick the quota by
        # round-robin across canon kinds so the cross-encoder cannot
        # re-collapse the slate onto one canon kind. The rerank still
        # orders WITHIN each kind by query relevance; the round-robin
        # preserves the kind diversity the canon leg built in.
        canon_pool = await voyage_rerank_dicts(idea, canon_rows, top_n=len(canon_rows))
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for r in canon_pool:
            by_kind.setdefault(str(r.get("provider_kind") or ""), []).append(r)
        rr = 0
        while len(canon_reranked) < canon_quota:
            added = False
            for kind_rows in by_kind.values():
                if rr < len(kind_rows):
                    canon_reranked.append(kind_rows[rr])
                    added = True
                    if len(canon_reranked) >= canon_quota:
                        break
            if not added:
                break
            rr += 1

    # Assemble: protocol head fills the non-quota slots, canon fills the
    # quota, then any spare capacity (canon leg returned fewer than quota)
    # is back-filled from the protocol tail. dedup by id throughout.
    final_rows: list[dict[str, Any]] = []
    final_ids: set[str] = set()
    for r in protocol_reranked[:protocol_slots]:
        rid = str(r.get("id") or "")
        if rid and rid in final_ids:
            continue
        final_rows.append(r)
        final_ids.add(rid)
    for r in canon_reranked[:canon_quota]:
        rid = str(r.get("id") or "")
        if rid and rid in final_ids:
            continue
        final_rows.append(r)
        final_ids.add(rid)
    for r in protocol_reranked:
        if len(final_rows) >= top_k:
            break
        rid = str(r.get("id") or "")
        if rid and rid in final_ids:
            continue
        final_rows.append(r)
        final_ids.add(rid)
    rows = final_rows[:top_k]

    reranked = bool(rows) and any(r.get("rerank_score") is not None for r in rows)
    canon_in_slate = sum(1 for r in rows if str(r.get("provider_kind") or "").startswith("canon_"))
    _log.info(
        "trade_panel.retrieve.rerank candidates=%d returned=%d reranked=%s "
        "canon_leg=%d canon_in_slate=%d canon_quota=%d",
        pre_rerank_count,
        len(rows),
        reranked,
        len(canon_rows),
        canon_in_slate,
        canon_quota,
    )

    # Issue #12 — exit log. hit_count + top_score disambiguate "Atlas returned
    # nothing" (likely filter-shape / ingest-tag drift) from "Atlas returned
    # rows but the post-$match on protocol filtered them out". The mongo_filter
    # echo lets the founder grep prod logs and replay the exact pipeline shape
    # against Atlas Compass.
    top_score = rows[0]["score"] if rows else 0.0
    _log.info(
        "trade_panel.retrieve.exit protocol=%s vertical=%s hit_count=%d "
        "top_score=%.4f mongo_filter=vertical=%s,protocol=%s",
        proto_norm,
        vertical,
        len(rows),
        top_score,
        vertical,
        proto_norm,
    )
    return rows


# Strategist closing-line: "Strategic intent: open small long, normal stop,
# weeks horizon — falsifier: ...". v1 best-effort regex over the free-form
# sentence. When the panel adopts the structured Phase 9 prompt addendum
# (entry_window/exit_horizon/direction/size_band) we'll prefer those keys
# directly on the parsed_verdict.
_STRATEGIST_DIRECTION_RE = re.compile(r"\b(long|short|neutral)\b", re.IGNORECASE)
_STRATEGIST_HORIZON_RE = re.compile(r"\b(intraday|days|weeks|months|\d+\s*[dwhm])\b", re.IGNORECASE)
_STRATEGIST_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _strategist_intent_from_turn(turn: TradePanelTurn | None, protocol: str) -> dict[str, Any]:
    """Best-effort extraction of a backtestable intent from the strategist turn.

    Pulls direction (long/short/neutral), a horizon hint, and an optional
    size band out of the closing line. Returns a dict shaped for
    :func:`backtest_intent`. Always populates ``protocol`` so the caller
    has a routable key even when the rest is thin.
    """
    intent: dict[str, Any] = {"protocol": protocol.strip().lower()}
    if turn is None:
        return intent
    line = (turn.parsed_verdict or {}).get("strategic_intent") or ""
    if not line:
        return intent
    if dm := _STRATEGIST_DIRECTION_RE.search(line):
        intent["direction"] = dm.group(1).lower()
    if hm := _STRATEGIST_HORIZON_RE.search(line):
        token = hm.group(1).lower()
        # Map qualitative tokens to representative day counts.
        mapped = {
            "intraday": "1d",
            "days": "3d",
            "weeks": "14d",
            "months": "60d",
        }.get(token, token)
        intent["exit_horizon"] = mapped
    if sm := _STRATEGIST_SIZE_RE.search(line):
        with contextlib.suppress(ValueError):
            intent["size_pct"] = float(sm.group(1))
    return intent


_CITATION_SNIPPET_LIMIT: int = 240


def _coerce_provider_kind(raw: Any) -> ProviderKind:
    """Whitelist a chunk's provider_kind to the canonical Literal.

    Pattern A: anything not in PROVIDER_KINDS falls back to ``"web"`` so
    we don't leak adapter-internal tags (e.g. ``"bazaar:dataset"``) onto
    the wire. The ingest path is responsible for translating those at
    write time; this is the read-side defensive backstop.
    """
    if isinstance(raw, str) and raw in PROVIDER_KINDS:
        return cast(ProviderKind, raw)
    return "web"


def _coerce_freshness_tier(raw: Any) -> FreshnessTier:
    if isinstance(raw, str) and raw in FRESHNESS_TIER_VALUES:
        return raw
    return "static"


def build_citations_from_chunks(chunks: list[dict[str, Any]]) -> list[Citation]:
    """Project retrieved chunks into the wire-shape :class:`Citation` list.

    Issue #15. The 1-indexed ``id`` matches the inline ``[N]`` markers
    that ``_format_chunks`` injects into the opening prompt — that's the
    contract callers rely on to link prose to the cite array.

    URL fallback: when ``source_url`` is empty (e.g. live-only chunks
    with no canonical URL), we synthesize ``gecko://chunk/<sha256[:16]>``
    keyed off ``chunk_id``. This keeps the wire field non-empty and
    deterministic without inventing a fake http URL.
    """
    out: list[Citation] = []
    for idx, chunk in enumerate(chunks, start=1):
        chunk_id = str(chunk.get("id") or "")
        url = str(chunk.get("source_url") or "").strip()
        if not url:
            digest = hashlib.sha256(chunk_id.encode("utf-8")).hexdigest()[:16]
            url = f"gecko://chunk/{digest}"
        snippet = (chunk.get("text") or chunk.get("content") or "").strip()
        if len(snippet) > _CITATION_SNIPPET_LIMIT:
            snippet = snippet[:_CITATION_SNIPPET_LIMIT]
        out.append(
            Citation(
                id=idx,
                source=str(chunk.get("source") or chunk.get("provider_kind") or "unknown"),
                url=url,
                chunk_id=chunk_id,
                provider_kind=_coerce_provider_kind(chunk.get("provider_kind")),
                freshness_tier=_coerce_freshness_tier(chunk.get("freshness_tier")),
                snippet=snippet,
            )
        )
    return out


async def run_trade_panel_with_retrieval(
    idea: str,
    protocol: str,
    *,
    vertical: str = "dex",
    tier: str = "basic",
    top_k: int = _DEFAULT_TRADE_TOP_K,
    llm_config: dict[str, Any] | None = None,
    agent_factory: AgentFactory | None = None,
    enable_backtest: bool = False,
    history_source: Any | None = None,
    agent_id: str | None = None,
    wallet: str | None = None,
    as_of_date: str | None = None,
) -> TradePanelVerdict:
    """Convenience wrapper — fetch corpus chunks, then run the 7-agent panel.

    Phase 8b's public entry point for the MCP tool + REST endpoint. The panel
    itself does NOT touch the vector store (that contract is locked by Phase
    8a). This wrapper does the retrieval up front and forwards into
    :func:`run_trade_panel`.

    Phase 9 addendum: when ``enable_backtest=True``, the strategist turn's
    intent is extracted and replayed against historical price data via
    :func:`backtest_intent`. The resulting :class:`BacktestReport` is
    attached to the returned verdict. Default False keeps existing callers
    on the Phase 8 contract.
    """
    chunks = await retrieve_trade_corpus_chunks(
        idea=idea,
        protocol=protocol,
        vertical=vertical,
        top_k=top_k,
        as_of_date=as_of_date,
    )

    # Issue #12 — panel kickoff log. Truthy chunks here but empty
    # `citations` on the response would point at hypothesis 3 (prompt-drop):
    # retrieval landed rows but the panel's _format_chunks / opening prompt
    # isn't injecting them. chunk_ids are bounded to 15 by _DEFAULT_TRADE_TOP_K
    # so this stays cheap.
    chunk_ids = [c.get("id", "") for c in chunks]
    _log.info(
        "trade_panel.kickoff protocol=%s vertical=%s tier=%s "
        "chunks_passed_to_panel=%d chunk_ids=%s",
        protocol.strip().lower(),
        vertical,
        tier,
        len(chunks),
        chunk_ids,
    )

    verdict = await run_trade_panel(
        idea=idea,
        protocol=protocol,
        retrieved_chunks=chunks,
        tier=tier,
        llm_config=llm_config,
        agent_factory=agent_factory,
        agent_id=agent_id,
        wallet=wallet,
    )

    # Issue #15: attach the structured citation list sourced from the same
    # chunks the panel saw. The 1-indexed ids match the inline [N] markers
    # the personas cite in their turns, so consumers can render cite chips
    # without regex-extracting from prose.
    citations = build_citations_from_chunks(chunks)
    if citations:
        verdict = verdict.model_copy(update={"citations": citations})

    if not enable_backtest:
        return verdict

    # Lazy import keeps the backtest sub-package off the trade_panel hot path
    # when callers leave the flag at its default.
    from gecko_core.orchestration.trade_panel.backtest import (
        backtest_intent as _backtest_intent,
    )

    # Phase 9.5: default history source flipped from Pyth to CoinGecko.
    # Pyth Hermes does not expose OHLCV; CoinGecko's free `/coins/{id}/ohlc`
    # does. Callers that explicitly pass `history_source=PythHermesHistorySource()`
    # still get the cache-only path — only the implicit default changed.
    from gecko_core.orchestration.trade_panel.backtest.history_source import (
        CoinGeckoOhlcHistorySource,
    )

    strategist_turn = next((t for t in verdict.turns if t.agent == STRATEGIST), None)
    intent_dict = _strategist_intent_from_turn(strategist_turn, protocol)
    source = history_source if history_source is not None else CoinGeckoOhlcHistorySource()
    try:
        report = await _backtest_intent(intent_dict, source)
    except Exception as exc:  # pragma: no cover - defensive; backtest never raises
        _log.warning("trade_panel.backtest.error err=%s", exc)
        return verdict
    return verdict.model_copy(update={"backtest": report})
