"""High-level workflows. CLI, MCP, and API call exactly these three functions.

The 7-step user flow (see `docs/implementation-plan.md`):

  1. User describes idea
  2. discover() OR validate provided URLs
  3. surface candidate sources for approval
  4. (after approval) payment gate
  5. ingest sources → chunks + embeddings
  6. orchestrate (basic | pro) → 3 documents
  7. return ResearchResult; KB stays alive for follow-up `ask`

Persistence happens BEFORE expensive work — the session row is inserted up
front so a crash mid-pipeline doesn't lose state. The payment gate runs
BEFORE ingestion so a payment failure can't burn OpenAI/Tavily credits.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import UUID

from openai import AsyncOpenAI

from gecko_core.ingestion import discover, ingest
from gecko_core.ingestion.settings import get_ingestion_settings
from gecko_core.models import (
    AskResult,
    Citation,
    ResearchResult,
    SourceCandidate,
    SourceInfo,
    SourceType,
    Tier,
    Verdict,
)
from gecko_core.orchestration.basic import generate as basic_generate
from gecko_core.orchestration.pro import generate as pro_generate
from gecko_core.orchestration.settings import get_orchestration_settings
from gecko_core.payments import run_payment_gate
from gecko_core.payments.x402_client import _settings as _payment_settings
from gecko_core.rag.query import rag_query
from gecko_core.sessions.store import CostKind, PaymentMode, SessionStore

logger = logging.getLogger(__name__)


ApprovalCallback = Callable[[list[SourceCandidate]], Awaitable[bool]]
ProgressCallback = Callable[[str], None]


def _classify(url: str) -> SourceType:
    return "youtube" if ("youtube.com" in url or "youtu.be" in url) else "web"


def _candidates_from_urls(urls: list[str]) -> list[SourceCandidate]:
    # Pydantic coerces str → HttpUrl at validation; mypy sees the field as
    # HttpUrl-strict, hence the cast.
    return [
        SourceCandidate.model_validate({"url": u, "title": "", "type": _classify(u), "score": 1.0})
        for u in urls
        if u
    ]


def _emit(progress: ProgressCallback | None, msg: str) -> None:
    if progress is not None:
        progress(msg)


async def research(
    idea: str,
    *,
    tier: Tier = "basic",
    urls: list[str] | None = None,
    auto_approve: bool = True,
    approval_callback: ApprovalCallback | None = None,
    store: SessionStore | None = None,
    progress_callback: ProgressCallback | None = None,
    skip_payment_gate: bool = False,
    session_id: UUID | None = None,
    tier_preset: str | None = None,
    journal: bool = True,
) -> ResearchResult:
    """Run the full discover → approve → pay → index → generate workflow.

    Args:
        idea: Plain-language startup idea.
        tier: "basic" (single LLM pass) or "pro" (Phase 6, NotImplemented).
        urls: Optional seed URLs. When omitted, sources auto-discovered via Tavily.
        auto_approve: When True, skip the approval prompt. The CLI's `--yes`
            flag wires through here. When False, `approval_callback` is invoked.
        approval_callback: Async callable that receives the candidate list and
            returns True to proceed. Required when `auto_approve` is False.
        store: Inject a SessionStore (for tests). Defaults to env-built.
        progress_callback: Optional sync callable for UI progress strings.

    Returns:
        ResearchResult with business_plan, validation_report, prd, and sources.

    Raises:
        PaymentRequiredError: x402 gate failed.
        NotImplementedError: tier == "pro".
        OrchestrationError: LLM output failed validation after retry.
        RuntimeError: user declined approval.
    """
    store = store or SessionStore.from_env()
    payment_mode: PaymentMode = _payment_settings().mode

    # Step 1 — persist session row before any expensive work, OR reuse one
    # the API already created (async pattern: API returns 202 with session_id
    # immediately, then runs this workflow under that pre-existing row).
    if session_id is None:
        session_id = await store.create(idea=idea, tier=tier, payment_mode=payment_mode)
    _emit(progress_callback, f"Session {session_id} created")

    # Step 2 — discover or validate.
    if urls:
        candidates = _candidates_from_urls(urls)
        _emit(progress_callback, f"Validating {len(candidates)} provided URLs")
    else:
        _emit(progress_callback, "Discovering sources via Tavily")
        candidates = await discover(idea)
        # Single advanced-search call per discovery; charge it to the session.
        from gecko_core.ingestion.discovery import TAVILY_ADVANCED_SEARCH_USD

        await store.add_cost(session_id, "tavily", TAVILY_ADVANCED_SEARCH_USD)

    if not candidates:
        await store.update_status(session_id, "failed")
        raise RuntimeError("no candidate sources found")

    # Step 3 — approval.
    if not auto_approve:
        if approval_callback is None:
            raise ValueError("approval_callback required when auto_approve=False")
        approved = await approval_callback(candidates)
        if not approved:
            await store.update_status(session_id, "failed")
            raise RuntimeError("user declined source approval")

    # Step 4 — payment gate. BEFORE ingestion. Stub passes; live can fail.
    # In v3, gecko-api's x402 middleware handles payment before this runs,
    # so the API passes skip_payment_gate=True to avoid double-charging.
    if not skip_payment_gate:
        _emit(progress_callback, "Running payment gate")
        await run_payment_gate(session_id, tier, store)
    else:
        _emit(progress_callback, "Payment already verified by API middleware")

    # Step 5 — ingest.
    _emit(progress_callback, f"Indexing {len(candidates)} sources")
    ingestion_result = await ingest(session_id, candidates, store)
    logger.info(
        "ingestion done session_id=%s indexed=%d skipped=%d failed=%d",
        session_id,
        ingestion_result.indexed,
        ingestion_result.skipped,
        ingestion_result.failed,
    )

    if ingestion_result.indexed == 0:
        await store.update_status(session_id, "failed")
        raise RuntimeError("ingestion produced zero indexed sources")

    # Step 6 — orchestrate.
    await store.update_status(session_id, "generating")
    _emit(progress_callback, "Generating documents")
    result = await basic_generate(session_id, idea, store)

    # Step 6b — pro tier: layer the 5-agent debate on top of the basic result.
    # The basic pass produces the structured 3 docs (business plan, validation
    # report, PRD); pro adds the transcript + session summary. We reuse the
    # same RAG context the basic pass built so we don't re-query.
    if tier == "pro":
        _emit(progress_callback, "Running pro debate")
        result = await _run_pro_debate(session_id, idea, result, store, tier_preset=tier_preset)

    # Step 7 — mark complete and return.
    await store.update_status(session_id, "complete")
    _emit(progress_callback, "Done")

    # Step 7b — flywheel write (S2X-04). Pro-only; best-effort. The user
    # response is already determined; this only writes to gecko_precedent
    # for future-session retrieval. Errors are logged + swallowed inside
    # write_precedent — never refund or fail a Pro session for this.
    if tier == "pro":
        from gecko_core.flywheel import write_precedent

        await write_precedent(
            session_id=session_id,
            idea=idea,
            transcript=cast("dict[str, Any] | None", result.transcript),
            user_id=None,  # frames.ag user provisioning hasn't landed yet
            store=store,
        )

    # Step 7c — auto-journal verdict_received (S5-MEM-04). Best-effort;
    # never fail the user response if the memory write fails.
    try:
        from gecko_core.memory.auto_journal import journal_verdict

        record = await store.get(session_id)
        project_id = record.project_id if record is not None else None
        verdict = _detect_research_verdict(result)
        await journal_verdict(
            session_id=session_id,
            project_id=project_id,
            idea=idea,
            verdict=verdict,
            scores=_extract_scores(result),
            sources_count=len(result.sources or []),
            tx_signature=record.x402_tx_signature if record is not None else None,
            journal=journal,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("auto-journal verdict failed: %s", exc)

    return result


def _detect_research_verdict(result: ResearchResult) -> str:
    """Best-effort verdict label for the journal entry.

    Prefers the structured ``ResearchResult.verdict`` (S11-VERDICT-01) —
    KILL / REFINE / BUILD — and lower-cases for the legacy journal shape.
    Falls back to legacy parsing of the judge's prose verdict line for
    pro tiers that ran before S11 stamped the field.
    """
    structured = getattr(result, "verdict", None)
    if isinstance(structured, Verdict):
        # Map the S11 single-token surface back to the legacy
        # ship/kill/pivot taxonomy the journal + flywheel + scaffold
        # consumers still key off. BUILD → ship, KILL → kill, REFINE →
        # pivot (REFINE is the "needs work / not greenlit" bucket, which
        # maps cleanly to the existing pivot label).
        _LEGACY = {Verdict.BUILD: "ship", Verdict.KILL: "kill", Verdict.REFINE: "pivot"}
        return _LEGACY[structured]
    summary = getattr(result, "pro_session_summary", None)
    transcript = getattr(result, "transcript", None)
    if isinstance(summary, str) and summary:
        for label in ("ship", "kill", "pivot"):
            if f"verdict: {label}" in summary.lower():
                return label
    if isinstance(transcript, dict):
        turns = transcript.get("turns") or []
        for t in reversed(turns if isinstance(turns, list) else []):
            if isinstance(t, dict) and t.get("agent") == "judge":
                content = str(t.get("content") or "").lower()
                for label in ("ship", "kill", "pivot"):
                    if f"verdict: {label}" in content:
                        return label
                break
    return "unknown"


def _extract_scores(result: ResearchResult) -> dict[str, Any]:
    """Pull the (tam, wedge, v1_feasibility) scores from the validation report.

    Best-effort: the validation_report shape is markdown; we don't parse
    it here. Return an empty dict so the journal entry is still well-formed.
    """
    return {}


async def _run_pro_debate(
    session_id: UUID,
    idea: str,
    base_result: ResearchResult,
    store: SessionStore,
    *,
    tier_preset: str | None = None,
) -> ResearchResult:
    """Run the 5-agent pro debate and merge its transcript into the result.

    Persists each AgentEvent to `pro_events` (for SSE tail) AND a per-agent
    cost row to `session_costs` (with `agent` populated for the per-agent
    rollup). On AG2 import failure we surface a clean error rather than
    crashing — the API handler maps it to a 500 with a helpful message.
    """
    from decimal import Decimal

    from gecko_core.rag.query import rag_query

    # Rebuild a compact rag_context from top chunks. We cap at the same
    # 8000 chars the pro module truncates to anyway — anything more is
    # wasted budget on round 1.
    chunks = await rag_query(session_id, idea, top_k=12, store=store)
    tavily_rag = "\n\n".join(
        f"[{i}] (source: {c.source_url}) {c.text}" for i, c in enumerate(chunks, 1)
    )

    # S4-TWITSH-01: classify, dispatch the V1 source set, and prepend the
    # rendered signal block ABOVE the Tavily RAG corpus. Failures are
    # non-fatal — a flaky V1 source must never block the pro debate.
    v1_rag, v1_spend_by_source = await _dispatch_v1_sources(session_id, idea, store)
    if v1_rag and tavily_rag:
        rag_context = f"{v1_rag}\n\n{tavily_rag}"
    elif v1_rag:
        rag_context = v1_rag
    else:
        rag_context = tavily_rag

    # S2X-06: fetch flywheel precedents BEFORE the debate so the agents see
    # prior verdicts in their opening prompt (separate from the V1 block —
    # this populates pro.generate's typed `precedents` arg, which the
    # opening-prompt builder uses for structured rendering). Failures are
    # non-fatal.
    precedents = await _retrieve_pro_precedents(idea, store)
    _ = v1_spend_by_source  # already debited inside _dispatch_v1_sources

    orch = get_orchestration_settings()

    # S1-01/S1-02: route LLM traffic via the LLM_ROUTER env switch. When the
    # env var is unset we keep the legacy llm_endpoint/llm_api_key/chat_model
    # path so existing deployments are unaffected.
    from gecko_core.orchestration.pro.pricing import compute_cost_usd
    from gecko_core.orchestration.pro.router import (
        model_matrix as _matrix_for,
    )
    from gecko_core.orchestration.pro.router import (
        resolve_router,
    )

    if os.environ.get("LLM_ROUTER"):
        cfg = resolve_router()
        llm_config: dict[str, Any] = cfg.llm_config_for_model(
            orch.chat_model, temperature=orch.temperature
        )
        # The matrix-driven path overrides the model per-agent inside
        # build_groupchat. The base llm_config still needs a model field for
        # the GroupChatManager + the auto-selector fallback path.
        resolved_matrix: dict[str, str] = _matrix_for(cfg.router)
        agent_matrix: dict[str, str] | None = resolved_matrix
        # Cost-attribution lookup for the on_event closure.
        _model_for: dict[str, str] = dict(resolved_matrix)
    else:
        llm_config = {
            "config_list": [
                {
                    "model": orch.chat_model,
                    "api_key": orch.llm_api_key,
                    "base_url": orch.llm_endpoint,
                }
            ],
            "temperature": orch.temperature,
        }
        agent_matrix = None
        _model_for = {}

    async def _on_event(event: object) -> None:
        # event is gecko_core.orchestration.pro.AgentEvent — typed as object
        # here to keep the workflows module decoupled from pro's surface in
        # the closure signature.
        ev = event
        try:
            await store.append_pro_event(
                session_id=session_id,
                seq=ev.seq,  # type: ignore[attr-defined]
                event_type=ev.type,  # type: ignore[attr-defined]
                agent=ev.agent,  # type: ignore[attr-defined]
                content=ev.content,  # type: ignore[attr-defined]
                tokens_in=ev.tokens_in,  # type: ignore[attr-defined]
                tokens_out=ev.tokens_out,  # type: ignore[attr-defined]
                ts=ev.ts,  # type: ignore[attr-defined]
            )
        except Exception as exc:  # pragma: no cover — best effort, don't halt
            logger.warning("append_pro_event failed: %s", exc)

        # Per-agent cost rollup. We don't have per-call provider headers in
        # AG2's `a_generate_reply` return shape, so we record a stub cost
        # row that scales loosely with token counts. Real cost reconciliation
        # happens at the ClawRouter layer; this row is for the per-agent
        # attribution view in the Pro UI.
        if ev.type == "turn_end" and ev.agent is not None:  # type: ignore[attr-defined]
            agent_name: str = ev.agent  # type: ignore[attr-defined]
            tokens_in_val: int = ev.tokens_in  # type: ignore[attr-defined]
            tokens_out_val: int = ev.tokens_out  # type: ignore[attr-defined]
            # Prefer the per-model price table when we know which model the
            # agent ran on (router-driven path); fall back to the generic
            # estimate when LLM_ROUTER is unset and the matrix is empty.
            model_str = _model_for.get(agent_name)
            if model_str is not None:
                cost = compute_cost_usd(model_str, tokens_in_val, tokens_out_val)
            else:
                cost = _estimate_agent_cost(tokens_in_val, tokens_out_val)
            try:
                await store.append_session_cost(
                    session_id=session_id,
                    line_item="llm",
                    agent=ev.agent,  # type: ignore[attr-defined]
                    cost_usd=cost,
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("append_session_cost failed: %s", exc)

    # S6-MINE-02 — prepend project priors block to the rag context (when
    # GECKO_MEMORY_PRIORS=1 and the session has a project_id). Best-effort.
    try:
        from gecko_core.memory.priors import build_priors_block

        sess_record = await store.get(session_id)
        project_id_str = (
            str(sess_record.project_id) if sess_record and sess_record.project_id else None
        )
        priors_block = await build_priors_block(project_id_str)
        if priors_block:
            rag_context = f"{priors_block}\n\n{rag_context}"
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("pro debate: priors injection failed: %s", exc)

    try:
        from gecko_core.orchestration.pro import BudgetGuard

        transcript = await pro_generate(
            idea=idea,
            rag_context=rag_context,
            llm_config=llm_config,
            on_event=_on_event,
            budget=BudgetGuard(),
            model_matrix=agent_matrix,
            precedents=precedents,
            tier_preset=tier_preset,
        )
    except ImportError as exc:
        logger.warning("AG2 not installed; pro tier degraded to basic: %s", exc)
        # Surface as a basic result with no transcript rather than failing.
        return base_result

    # Pull judge's final paragraph for the summary surface.
    summary: str | None = None
    for turn in reversed(transcript.turns):
        if turn.agent == "judge":
            summary = turn.content
            break

    _ = Decimal  # silence unused-import; Decimal is used inside _on_event
    # S11-VERDICT-01 — prefer the judge's explicit single-token verdict
    # ("Final verdict: KILL|REFINE|BUILD") when the pro debate emits one;
    # otherwise keep the verdict the basic pass already derived from the
    # typed gap. This lets the judge override on tight consensus while
    # keeping the safe basic-tier mapping when the judge stays silent.
    judge_verdict = _parse_judge_verdict(summary)
    final_verdict = judge_verdict if judge_verdict is not None else base_result.verdict

    return base_result.model_copy(
        update={
            "tier": "pro",
            "transcript": transcript.model_dump(mode="json"),
            "pro_session_summary": summary,
            "verdict": final_verdict,
        }
    )


_JUDGE_VERDICT_RE = re.compile(r"(?im)^\s*(?:final\s+)?verdict\s*[:\-]\s*(KILL|REFINE|BUILD)\b")


def _parse_judge_verdict(summary: str | None) -> Verdict | None:
    """Pull a ``Final verdict: KILL|REFINE|BUILD`` line out of the judge's
    closing paragraph. Returns None when no such line is present (e.g.
    legacy ``Verdict: SHIP V1 to ...`` shape) — callers fall back to the
    basic-tier derived verdict in that case.
    """
    if not summary:
        return None
    match = _JUDGE_VERDICT_RE.search(summary)
    if match is None:
        return None
    token = match.group(1).upper()
    try:
        return Verdict(token)
    except ValueError:  # pragma: no cover — regex guards the alphabet
        return None


async def _dispatch_v1_sources(
    session_id: UUID,
    idea: str,
    store: SessionStore,
) -> tuple[str, dict[str, float]]:
    """Classify the idea, dispatch V1 sources, debit per-source costs.

    Returns `(rendered_block, spend_by_source)`. The rendered block always
    has the four headings (twit.sh / HN / Reddit / precedents) so the
    agents can rely on its structure even when sources returned nothing.

    Wrapped end-to-end in try/except: any failure in classify or dispatch
    degrades to "no V1 block" rather than failing the pro debate. We pay
    the price of slightly less context for the resilience guarantee
    web3-engineer's CLAUDE.md demands ("never store private keys, never let
    a flaky third-party take down the session").
    """
    try:
        from gecko_core.classify import classify_idea
        from gecko_core.ingestion.embedder import embed
        from gecko_core.sources.v1_block import (
            build_default_sources,
            dispatch_and_render,
        )

        categories = await classify_idea(idea)

        # Embed once for the precedent source. Failure is fine — we just
        # skip the precedent source in that case (the V1 block still
        # renders three headings + "No data found." for precedents).
        embedding: list[float] | None = None
        try:
            vectors, _ = await embed([idea])
            if vectors:
                embedding = vectors[0]
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("v1_sources: embed failed (%s); skipping precedents", exc)

        sources = build_default_sources(embedding=embedding, store=store)
        block = await dispatch_and_render(idea=idea, categories=categories, sources=sources)

        # Debit per-source spend to the session ledger. twit.sh is the only
        # paid source today; HN/Reddit/precedents are always 0.
        for source_name, cost in block.spend_by_source.items():
            if cost <= 0:
                continue
            kind: CostKind = "twitsh" if source_name == "twit_sh" else "v1_sources"
            try:
                await store.add_cost(session_id, kind, cost)
            except Exception as exc:  # pragma: no cover — best effort
                logger.warning(
                    "v1_sources: add_cost failed for %s ($%.4f): %s",
                    source_name,
                    cost,
                    exc,
                )

        # Structured log line for CloudWatch — per-source telemetry that
        # the cache hit-rate dashboard (S4-TWITSH-02) consumes. Keep it on
        # one line so log-aggregation tools can parse without recombining.
        try:
            from_cache_map: dict[str, bool] = {}
            for name, result in block.results.items():
                if not result.fired:
                    continue
                from_cache_map[name] = bool(result.payload.get("cached")) or bool(
                    result.payload.get("from_cache")
                )
            logger.info(
                "v1_sources.telemetry session_id=%s categories=%s "
                "fired=%s spend_by_source=%s cache_hits=%s",
                session_id,
                sorted(categories),
                sorted(n for n, r in block.results.items() if r.fired),
                block.spend_by_source,
                from_cache_map,
            )
        except Exception:  # pragma: no cover — telemetry must never crash the run
            pass

        return block.rag_block, block.spend_by_source
    except Exception as exc:  # pragma: no cover — full degrade
        logger.warning("v1_sources: dispatch failed (%s); degrading to no block", exc)
        return "", {}


async def _retrieve_pro_precedents(
    idea: str,
    store: SessionStore,
) -> list[Any]:
    """Embed `idea` once and dispatch the GeckoPrecedentSource for retrieval.

    Returns a list of `GeckoPrecedent` (or empty on failure / cold corpus).
    Embedding cost is small (1 input, ~10-50 tokens for an idea sentence) so
    we don't bother attributing it to the session ledger here — the basic
    pass already paid for embeddings during ingestion. Wrapped in a try/except
    so a transient flywheel outage degrades to "no prior precedents found"
    rather than killing the pro debate.
    """
    try:
        from gecko_core.ingestion.embedder import embed
        from gecko_core.sources import dispatch_sources
        from gecko_core.sources.gecko_precedent import GeckoPrecedentSource

        vectors, _ = await embed([idea])
        if not vectors:
            return []
        source = GeckoPrecedentSource(embedding=vectors[0], store=store)
        results = await dispatch_sources(
            idea=idea,
            categories=set(),
            sources=[source],
            timeout_seconds=10.0,
        )
        result = results.get(source.name)
        if result is None or not result.fired:
            return []
        from gecko_core.sessions.store import GeckoPrecedent

        rows = result.payload.get("precedents", [])
        return [GeckoPrecedent.model_validate(r) for r in rows]
    except Exception as exc:  # pragma: no cover — degrade gracefully
        logger.warning("flywheel precedent retrieval failed: %s", exc)
        return []


# Token-pricing fallback for pro per-agent cost attribution. Numbers match
# basic-tier's `_MODEL_RATES_USD_PER_1M` in spirit but we hardcode a single
# generic rate here — pro routes through ClawRouter which may pick any
# upstream model. The real cost rolls up from ClawRouter headers via the
# basic-tier code path; this row exists for per-agent attribution in the UI.
def _estimate_agent_cost(tokens_in: int, tokens_out: int) -> Decimal:  # type: ignore[name-defined]  # noqa: F821
    from decimal import Decimal

    # 0.15/0.60 per 1M (gpt-4o-mini equivalent) — intentionally a stub.
    rate_in = Decimal("0.15") / Decimal(1_000_000)
    rate_out = Decimal("0.60") / Decimal(1_000_000)
    return (rate_in * tokens_in + rate_out * tokens_out).quantize(Decimal("0.000001"))


async def ask(
    session_id: UUID | str,
    question: str,
    store: SessionStore | None = None,
) -> AskResult:
    """Answer a follow-up question grounded in a session's knowledge base."""
    sid = session_id if isinstance(session_id, UUID) else UUID(str(session_id))
    store = store or SessionStore.from_env()

    chunks = await rag_query(sid, question, top_k=8, store=store)
    if not chunks:
        return AskResult(
            session_id=str(sid),
            answer=(
                "No relevant context was found in this session's knowledge "
                "base. Try a different question or re-run research with more "
                "sources."
            ),
            citations=[],
        )

    orch = get_orchestration_settings()
    ingest_s = get_ingestion_settings()
    client = AsyncOpenAI(api_key=ingest_s.openai_api_key.get_secret_value())

    context = "\n\n".join(
        f"[{i}] (source: {c.source_url}) {c.text}" for i, c in enumerate(chunks, 1)
    )

    resp = await client.chat.completions.create(
        model=orch.chat_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You answer questions strictly from the provided context. "
                    "Cite chunk numbers like [1], [2] inline. If the context "
                    "is insufficient, say so."
                ),
            },
            {
                "role": "user",
                "content": f"Context:\n{context}\n\nQuestion: {question}",
            },
        ],
        temperature=orch.temperature,
    )
    answer = resp.choices[0].message.content or ""

    citations = [
        Citation(
            source_url=c.source_url,
            chunk_index=c.chunk_index,
            similarity=c.similarity,
        )
        for c in chunks
    ]
    return AskResult(session_id=str(sid), answer=answer, citations=citations)


async def list_sources(
    session_id: UUID | str,
    store: SessionStore | None = None,
) -> list[SourceInfo]:
    """List all indexed sources for a session."""
    sid = session_id if isinstance(session_id, UUID) else UUID(str(session_id))
    store = store or SessionStore.from_env()
    return await store.list_sources(sid)


# Backward-compatible name used by `gecko_core.__init__` re-export.
sources = list_sources


__all__ = ["ask", "list_sources", "research", "sources"]
