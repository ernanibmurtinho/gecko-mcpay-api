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
    derive_verdict,
    is_low_grounding,
)
from gecko_core.orchestration.basic import generate as basic_generate
from gecko_core.orchestration.pro import generate as pro_generate
from gecko_core.orchestration.settings import get_orchestration_settings
from gecko_core.payments import run_payment_gate
from gecko_core.payments.x402_client import _settings as _payment_settings
from gecko_core.rag.query import rag_query
from gecko_core.sessions.store import CostKind, PaymentMode, SessionStore

logger = logging.getLogger(__name__)


# S17-WEDGE-WIRE-02 — Feature flag for the Path B chunk-ingest behavior.
# When False, ``_dispatch_stub_integration_providers`` falls back to the
# pre-WIRE-02 behavior (cost ledger only, no chunk insert) — that's the
# demo fallback if anything regresses Wednesday. Read once at module
# import; flipping requires a process restart.
# See: docs/strategy/2026-05-02-wedge-wire-path-b-design.md §6.1
_WEDGE_WIRE_ENABLED: bool = os.getenv("GECKO_WEDGE_WIRE_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


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
    calibration: str | None = None,
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

    # Step 6a — S16-INTEGRATE-01. Stub-mode integration dispatch for the
    # Sprint 14 (twit.sh) and Sprint 16 (Bazaar) consumer surfaces. The
    # Wave 2 commits registered both providers but no caller dispatched
    # them on the basic-tier hot path; the 2026-05-01 stub smoke
    # surfaced the gap. This call exercises both in stub mode so the
    # economics ledger carries non-zero `twitsh` + `v1_sources` (Bazaar)
    # lines and the wedge claim is on the path. Live mode is a no-op
    # here for now — live wiring follows in S17 alongside the budget
    # gate refactor.
    try:
        dispatch_summary = await _dispatch_stub_integration_providers(
            session_id=session_id,
            idea=idea,
            store=store,
            payment_mode=payment_mode,
        )
    except Exception as exc:  # pragma: no cover — defensive, never fail the run
        logger.warning("integrate-01 dispatch failed: %s", exc)
        dispatch_summary = None
    if dispatch_summary is not None:
        _emit(progress_callback, dispatch_summary)

    # S21-CALIBRATION-01 — load the calibration corpus once per run when
    # the caller opted in. We pull the entire dataset (42 chunks for the
    # Colosseum set) — small enough to skip similarity-search ranking. The
    # rendered block is prepended to every agent's system prompt by
    # ``build_groupchat``; the corpus_id is stamped on the ResearchResult
    # for the footer surface.
    calibration_block: str | None = None
    calibration_corpus_id: str | None = None
    if calibration:
        from gecko_core.judges import (
            COLOSSEUM_DATASET,
            WEB3_ACCELERATORS_DATASET,
            load_calibration_chunks,
            render_calibration_block,
        )
        from gecko_core.judges import (
            calibration_corpus_id as _build_cal_id,
        )

        if calibration not in ("colosseum", "web3", "all"):
            raise ValueError(
                f"unknown calibration corpus {calibration!r}; supported: 'colosseum', 'web3', 'all'"
            )
        cal_rows: list[dict[str, Any]] = []
        if calibration in ("colosseum", "web3", "all"):
            # 'web3' loads colosseum + web3 (web3 builds *on top of* the
            # core judge set per the dataset coordination memo);
            # 'colosseum' loads colosseum only.
            cal_rows.extend(await load_calibration_chunks(COLOSSEUM_DATASET))
        if calibration in ("web3", "all"):
            cal_rows.extend(await load_calibration_chunks(WEB3_ACCELERATORS_DATASET))
        if not cal_rows:
            logger.warning(
                "calibration='%s' requested but corpus is empty — run "
                "`bb judges ingest-colosseum` first.",
                calibration,
            )
        else:
            calibration_block = render_calibration_block(cal_rows)
            calibration_corpus_id = _build_cal_id(cal_rows)
            _emit(
                progress_callback,
                f"Calibration corpus loaded: {calibration_corpus_id}",
            )

    # Step 6b — pro tier: layer the 5-agent debate on top of the basic result.
    # The basic pass produces the structured 3 docs (business plan, validation
    # report, PRD); pro adds the transcript + session summary. We reuse the
    # same RAG context the basic pass built so we don't re-query.
    if tier == "pro":
        _emit(progress_callback, "Running pro debate")
        result = await _run_pro_debate(
            session_id,
            idea,
            result,
            store,
            tier_preset=tier_preset,
            calibration_block=calibration_block,
        )

    # Stamp the corpus identifier on the result regardless of tier — the
    # footer surfaces it so basic-tier callers can also see they ran
    # calibrated. The Block itself only matters for pro-tier (where the
    # 5-voice debate consumes it); on basic-tier the value is informational.
    if calibration_corpus_id is not None:
        result = result.model_copy(update={"calibration_corpus": calibration_corpus_id})

    # S20-VERDICT-URL-IMPL-01 — stamp the deterministic verdict_hash AFTER
    # both ``provider_mix_flag`` and the pro debate (whose finaliser already
    # stamps provider_mix_flag for the pro path). The hash inputs are locked
    # by ``gecko_core.verdict_hash`` (idea + retrieved sources + structural
    # verdict shape); reruns under the same retrieval reproduce the digest.
    # provider_mix_flag is intentionally NOT a hash input (see the docstring
    # in verdict_hash._verdict_payload) so the audit flag can flip without
    # invalidating cached verdict-URLs.
    from gecko_core.verdict_hash import verdict_hash as _compute_verdict_hash

    result = result.model_copy(update={"verdict_hash": _compute_verdict_hash(idea, result)})

    # Step 7 — persist the final result JSON, then mark complete and return.
    # S17-PERSIST-01: the CLI path calls `workflows.research()` directly and
    # had no `set_result` write, so `bb scaffold` / `gecko_advise` later
    # failed with "no persisted result yet". The API background task in
    # gecko-api/main.py also calls `set_result` (defense in depth — its own
    # lifecycle still owns the API path), but persisting here means the CLI
    # / MCP / direct-SDK callers all get persistence for free. Runs whether
    # tier == "basic" or "pro" (the pro path mutates `result` above via
    # `_run_pro_debate`, so the post-debate version is what we persist).
    try:
        await store.set_result(session_id, result.model_dump(mode="json"))
    except Exception as exc:  # pragma: no cover — defensive
        # Best-effort: a Supabase write failure here shouldn't kill an
        # otherwise-successful run. The API-side write_result still runs
        # for API-driven sessions; CLI sessions degrade to "completed but
        # unscaffoldable" with a clear log line operators can grep.
        logger.warning("workflows.research: set_result failed: %s", exc)
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

    # Step 7b2 — production judge transcript capture (S12-HARDEN-03). Every
    # public verdict that lands in CDP Bazaar needs an audit trail. Disk
    # write is best-effort; never fail the user response on it. Disabled
    # in tests via GECKO_TRANSCRIPT_CAPTURE=false.
    try:
        from gecko_core.orchestration.transcripts import capture as _capture_transcript

        _capture_transcript(session_id=session_id, idea=idea, result=result)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("transcript capture failed: %s", exc)

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

    Prefers the structured ``ResearchResult.verdict`` (S11-VERDICT-01 +
    S17-TONE-01) — PIVOT / REFINE / GO — and lower-cases for the legacy
    journal shape. Falls back to legacy parsing of the judge's prose
    verdict line for pro tiers that ran before S11 stamped the field.
    """
    structured = getattr(result, "verdict", None)
    if isinstance(structured, Verdict):
        # Map the S11/S17 single-token surface back to the legacy
        # ship/kill/pivot taxonomy the journal + flywheel + scaffold
        # consumers still key off. GO → ship, PIVOT → kill (legacy bucket
        # name predates S17 rename and remains the "don't build as-is"
        # signal in the journal+precedent stores), REFINE → pivot (the
        # "needs work / not greenlit" bucket).
        # S20-COHERENCE-VERDICT-LABEL-01 — the new KILL (premise
        # incoherence) maps to the legacy "kill" journal bucket too;
        # both PIVOT and KILL are "don't build as-is" from the journal /
        # flywheel consumer's perspective, the distinction lives in the
        # founder-facing single-token surface.
        _LEGACY = {
            Verdict.GO: "ship",
            Verdict.PIVOT: "kill",
            Verdict.KILL: "kill",
            Verdict.REFINE: "pivot",
        }
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
    calibration_block: str | None = None,
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
    #
    # NOTE: we deliberately do NOT pre-resolve a per-agent matrix here. The
    # legacy ``model_matrix(cfg.router)`` returned a hardcoded gpt-4o-mini
    # map (judge=gpt-4o) that ignored ``tier_preset`` entirely. Per Commit A
    # of the LLM-hygiene sprint we leave ``agent_matrix=None`` so the
    # catalog branch in ``pro/__init__.py`` (gated on ``model_matrix is None``
    # + non-None ``tier_preset``) resolves the matrix from ``model_matrix_for_tier``.
    from gecko_core.orchestration.pro.pricing import compute_cost_usd
    from gecko_core.orchestration.pro.router import (
        model_matrix_for_tier,
        resolve_router,
    )
    from gecko_core.routing.catalog import Tier as _CatalogTier

    if os.environ.get("LLM_ROUTER"):
        cfg = resolve_router()
        llm_config: dict[str, Any] = cfg.llm_config_for_model(
            orch.chat_model, temperature=orch.temperature
        )
        # Catalog is the single source of truth: leave ``agent_matrix=None``
        # so pro.generate's tier_preset-driven branch resolves the per-agent
        # matrix from ``model_matrix_for_tier``. The cost-attribution lookup
        # below still needs to know which model each agent is going to use,
        # so we resolve a parallel copy here purely for the on_event closure.
        agent_matrix: dict[str, str] | None = None
        try:
            _resolved_for_costs = model_matrix_for_tier(
                cfg.router, _CatalogTier(tier_preset or "balanced")
            )
        except Exception:
            _resolved_for_costs = {}
        _model_for: dict[str, str] = dict(_resolved_for_costs)
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

        # S20-FEATURE-NOT-PRODUCT-CRITIC-01 — forward the basic-tier gap
        # classification so the critic's system message gets the conditional
        # "is this a feature, or a defensible product" fragment when the
        # idea sits in a crowded/mature space (Full or Partial:UX/pricing/
        # integration). Pure-prompt change; no verdict-semantics impact.
        transcript = await pro_generate(
            idea=idea,
            rag_context=rag_context,
            llm_config=llm_config,
            on_event=_on_event,
            budget=BudgetGuard(),
            model_matrix=agent_matrix,
            precedents=precedents,
            tier_preset=tier_preset,
            gap_classification=base_result.validation_report.gap_classification,
            # S20-DISTRIBUTION-CRITIC-01 — pass the basic-tier ICP so the
            # critic gets the distribution/GTM fragment when the idea + ICP
            # trip the B2B / 2-sided heuristic detector.
            icp=base_result.business_plan.icp,
            # S21-CALIBRATION-01 — when the run is calibrated (e.g. against
            # the Colosseum-judges corpus), prepend the named-judge lens to
            # every agent's system message.
            calibration_block=calibration_block,
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
    # S17-VERDICT-01 — the verdict is ALWAYS a function of the final
    # gap_classification + citations, never an independent LLM emission.
    # Previously this path read the judge's "Final verdict: ..." line and
    # piped it through, which let the judge emit (e.g.) a PIVOT verdict
    # while the validation report carried Partial:UX — a contradiction.
    # The fix: re-derive from the final gap (basic-tier already enforced
    # the structured gap_classification) and the validation citations,
    # which also fires the evidence-strength floor when grounding is thin.
    # The judge's ``_parse_judge_verdict`` is retained for transcript
    # capture/diagnostics but is no longer the source of truth.
    final_gap = base_result.validation_report.gap_classification
    final_citations = base_result.validation_report.citations
    low_grounding = is_low_grounding(final_citations)
    # S20-COHERENCE-VERDICT-LABEL-01 — count distinct voices that flagged
    # ``incoherent_premise`` in their turn output. ≥2 → Verdict.KILL,
    # overriding gap/grounding signals. The dog-emotional-AI-blockchain
    # class of idea routes here, not through PIVOT/REFINE.
    from gecko_core.orchestration.pro.coherence import (
        count_incoherent_premise_flags,
    )

    incoherence_flags = count_incoherent_premise_flags(transcript.turns)
    final_verdict = derive_verdict(
        final_gap,
        advisor_consensus=None,
        citations=final_citations,
        incoherence_flag_count=incoherence_flags,
    )

    # S20-PROVIDER-MIX-FLOOR-01 — informational provider-mix audit. The
    # citation set here is the post-debate finalisation set; we re-audit
    # at the pro entry point so the flag tracks pro-specific reranks if
    # they ever diverge from the basic path.
    from gecko_core.orchestration.provider_mix_audit import audit_provider_mix

    provider_mix_flag = audit_provider_mix(final_citations)

    # S21-CALIBRATION-CLASSIFY-01 — sentinel extraction from the judge's
    # prose. No new LLM call: the judge prompt instructs the agent to
    # emit `idea_classification: <label>` as the FIRST line of its
    # output under any calibration regime, so we regex it the same way
    # we extract gap_classification / INCOHERENT_PREMISE. Returns None
    # when the sentinel is absent (legacy v5.5 transcripts, mock-mode
    # runs that bypass the judge agent, or judge prose drift).
    from gecko_core.orchestration.pro.coherence import (
        extract_founder_posture,
        extract_idea_classification,
    )

    idea_classification = extract_idea_classification(transcript.turns)
    # S21-CALIBRATION-FOUNDER-POSTURE-01 — sibling sentinel for the
    # founder lens. Same regex contract; the post-processor classification
    # batch refines both fields when the judge drops the sentinels under
    # prompt-load drift.
    founder_posture = extract_founder_posture(transcript.turns)

    # v5.5 demo post-processors — re-read the transcript to produce the
    # structured readouts the renderer expects. Pure post-hoc, never
    # changes the verdict (and excluded from verdict_hash).
    per_voice = None
    transcript_summary_text = None
    market_landscape = None
    surviving_dissent = None
    next_steps_with_falsifiers = None
    try:
        from gecko_core.orchestration.pro.post_processors import run_post_processors

        (
            per_voice,
            transcript_summary_text,
            market_landscape,
            surviving_dissent,
            next_steps_with_falsifiers,
            pp_meta,
        ) = await run_post_processors(transcript, rag_context, idea)
        if pp_meta.get("_dropped_step_count"):
            logger.info(
                "post-processor coherence dropped %d next-step(s)",
                pp_meta["_dropped_step_count"],
            )
        # S21-CALIBRATION-FOUNDER-POSTURE-01 — fallback fill from the
        # post-processor JSON. Sentinel-from-transcript wins when present
        # (deterministic + free); the post-processor only fires when the
        # regex extraction returned None. Two independent fields so a
        # partial fallback (one label populated, the other not) is fine.
        pp_idea = pp_meta.get("idea_classification")
        pp_founder = pp_meta.get("founder_posture")
        if idea_classification is None and isinstance(pp_idea, str):
            idea_classification = pp_idea
        if founder_posture is None and isinstance(pp_founder, str):
            founder_posture = pp_founder
    except Exception as exc:  # pragma: no cover — defense in depth
        logger.warning("v5.5 post-processors failed: %s", exc)

    return base_result.model_copy(
        update={
            "tier": "pro",
            "transcript": transcript.model_dump(mode="json"),
            "pro_session_summary": summary,
            "verdict": final_verdict,
            "low_grounding": low_grounding,
            "provider_mix_flag": provider_mix_flag,
            "per_voice": per_voice,
            "transcript_summary": transcript_summary_text,
            "market_landscape": market_landscape,
            "surviving_dissent": surviving_dissent,
            "next_steps_with_falsifiers": next_steps_with_falsifiers,
            "idea_classification": idea_classification,
            "founder_posture": founder_posture,
        }
    )


# S17-VERDICT-01 — the judge's "Final verdict: ..." line is no longer the
# source of truth for ResearchResult.verdict. The verdict is always
# ``derive_verdict(final_gap, advisor_consensus, citations)`` so the headline
# can never contradict the typed gap_classification. The judge prose stays
# captured in ``pro_session_summary`` for diagnostics (the eval harness in
# ``tests/eval/`` keeps parsing it for parity scoring), but the parsing
# helper that previously fed verdict authority has been removed.


def _quota(env_name: str, default: int) -> int:
    """Read an integer provider quota from env. Falls back on parse error.

    S17-DISCOVERY-01 — every per-provider quota is overridable via env so
    operators can tune the rebalance without a redeploy. Defaults are
    documented in `docs/build-plan-sprint-17.md`.
    """
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


async def _dispatch_stub_integration_providers(
    *,
    session_id: UUID,
    idea: str,
    store: SessionStore,
    payment_mode: PaymentMode,
) -> str | None:
    """S16-INTEGRATE-01 / S17-DISCOVERY-01 — multi-provider rebalance dispatch.

    History:
      - S16-INTEGRATE-01 introduced this as the stub-mode dispatch for
        Bazaar + twit.sh after the 2026-05-01 stub-smoke review found
        both providers were registered but never fired.
      - S17-DISCOVERY-01 added Arxiv (free, structured) and turned the
        per-provider counts into env-tunable quotas. The structural
        intent: lean harder on structured providers (Bazaar JSON,
        Arxiv abstracts, tweets) than on raw-URL Tavily fetches.

    Quotas (env / default):
      GECKO_PROVIDER_QUOTA_BAZAAR  = 3 services
      GECKO_PROVIDER_QUOTA_TWITSH  = 5 tweets   (provider-side cap)
      GECKO_PROVIDER_QUOTA_ARXIV   = 5 abstracts
      GECKO_PROVIDER_QUOTA_TAVILY  = 3 URLs     (read in discovery.py)

    Stub mode runs all providers; live mode short-circuits and the
    paid providers (Bazaar, twit.sh) are still gated on their own
    recorded-fixture wiring tickets. Free providers (Arxiv) run in
    *both* modes — there's no payment risk.

    Failures degrade silently — the user has already received their
    verdict; this is pure attribution + structured-context telemetry.
    """
    # Free providers (Arxiv) can run in any mode; paid providers stay
    # behind the stub-mode gate until the live wiring ticket lands.
    if payment_mode != "stub":
        return None

    # Force the consumer + discovery resolvers to stub at the boundary.
    # ``X402_CONSUMER_MODE`` may be unset in some test runners; default
    # to stub so the Bazaar buyer path doesn't reach for live signing.
    os.environ.setdefault("X402_CONSUMER_MODE", "stub")
    os.environ.setdefault("GECKO_BAZAAR_DISCOVERY_MODE", "stub")

    from gecko_core.classify import classify_idea
    from gecko_core.sources import dispatch_sources
    from gecko_core.sources.arxiv import make_arxiv_source
    from gecko_core.sources.bazaar import make_bazaar_provider
    from gecko_core.sources.twit_sh import TwitshSource

    try:
        categories = await classify_idea(idea)
    except Exception as exc:  # pragma: no cover — degrade
        logger.warning("integrate-01: classify failed (%s); using empty set", exc)
        categories = set()

    # Resolve per-provider quotas. Tavily quota is read inside
    # discovery.py — recorded here only for the structured log line.
    arxiv_quota = _quota("GECKO_PROVIDER_QUOTA_ARXIV", 5)
    _bazaar_quota = _quota("GECKO_PROVIDER_QUOTA_BAZAAR", 3)
    _twitsh_quota = _quota("GECKO_PROVIDER_QUOTA_TWITSH", 5)
    tavily_quota = _quota("GECKO_PROVIDER_QUOTA_TAVILY", 3)
    # Bazaar and twit.sh per-call quotas are enforced inside their own
    # provider classes (TOP_K and MAX_RESULTS respectively); the env
    # values above are read for telemetry parity. Adjusting the
    # in-provider constants is a follow-up coordinated with web3-eng
    # (Bazaar's TOP_K bump is tracked under S17-BAZAAR-FANOUT-01).

    # Skip the optional Supabase-backed daily-cap ledger: the table
    # (``bazaar_spend_ledger``) ships in migration ``20260501150000`` and
    # may not be applied on every dev DB. Stub mode never spends real
    # USDC so the daily-cap gate adds no value here. Live wiring brings
    # the ledger back via the live S17 wiring ticket.
    saved_url = os.environ.pop("SUPABASE_URL", None)
    saved_key = os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)
    try:
        bazaar = make_bazaar_provider(session_id=session_id)
    finally:
        if saved_url is not None:
            os.environ["SUPABASE_URL"] = saved_url
        if saved_key is not None:
            os.environ["SUPABASE_SERVICE_ROLE_KEY"] = saved_key
    twitsh = TwitshSource()
    arxiv = make_arxiv_source(max_results=arxiv_quota)

    # Arxiv's `applies_to` accepts an optional `idea` kwarg (signature
    # extension, see ArxivSource.applies_to). The dispatch layer only
    # passes `categories`, so the keyword-signal fallback won't fire
    # there — pre-compute applicability and skip enqueueing when the
    # idea is clearly off-topic for Arxiv (saves a wasted HTTP probe).
    arxiv_applies = await arxiv.applies_to(categories=categories, idea=idea)
    sources_list: list[Any] = [bazaar, twitsh]
    if arxiv_applies:
        sources_list.append(arxiv)

    results = await dispatch_sources(
        idea=idea,
        categories=categories,
        sources=sources_list,
        timeout_seconds=15.0,
    )

    bazaar_result = results.get("bazaar")
    twitsh_result = results.get("twit_sh")
    arxiv_result = results.get("arxiv")

    # S17-WEDGE-WIRE-02 — chunk-ingest counters. ``*_chunks`` is the
    # number of payloads dispatched (legacy attribution); ``*_indexed``
    # is the number of chunks now durable in the ``chunks`` table for
    # the session, ready for ``match_chunks`` retrieval. Identical when
    # the flag is on, indexed=0 when off (rollback hatch).
    from gecko_core.ingestion.pipeline import ingest_provider_chunks
    from gecko_core.sources.arxiv.embed_adapter import (
        _abs_url_for as _arxiv_abs_url_for,
    )
    from gecko_core.sources.arxiv.embed_adapter import (
        to_chunks as _arxiv_to_chunks,
    )
    from gecko_core.sources.bazaar.embed_adapter import to_chunks as _bazaar_to_chunks
    from gecko_core.sources.twit_sh.embed_adapter import to_chunks as _twitsh_to_chunks

    bazaar_chunks = 0
    bazaar_indexed = 0
    bazaar_cost = 0.0
    if bazaar_result is not None and bazaar_result.fired:
        bazaar_payload_chunks = bazaar_result.payload.get("chunks") or []
        bazaar_chunks = len(bazaar_payload_chunks)
        bazaar_cost = float(bazaar_result.cost_usd or 0.0)
        if bazaar_cost > 0:
            try:
                # No `cost_bazaar_usd` column yet (data-eng owns the
                # migration). Roll up under `v1_sources` so the spend
                # appears in the existing economics surface; the
                # provider-level ledger row in `bazaar_spend_ledger`
                # carries the per-resource breakdown.
                await store.add_cost(session_id, "v1_sources", bazaar_cost)
            except Exception as exc:  # pragma: no cover
                logger.warning("integrate-01: bazaar add_cost failed: %s", exc)

        if _WEDGE_WIRE_ENABLED and bazaar_payload_chunks:
            # The bazaar provider returns chunk *dicts* shaped like
            # ``BazaarChunk._asdict()``; coerce back into the dataclass
            # the embed adapter expects so to_chunks() can read the
            # structured metadata fields.
            from decimal import Decimal as _Decimal

            from gecko_core.sources.bazaar.types import BazaarChunk as _BazaarChunk

            coerced: list[_BazaarChunk] = []
            for raw in bazaar_payload_chunks:
                if not isinstance(raw, dict):
                    continue
                try:
                    cost_raw = raw.get("cost_usd", "0")
                    cost = _Decimal(str(cost_raw)) if cost_raw is not None else _Decimal("0")
                except Exception:
                    cost = _Decimal("0")
                coerced.append(
                    _BazaarChunk(
                        text=str(raw.get("text") or ""),
                        provider_kind=str(raw.get("provider_kind") or "bazaar"),
                        cost_usd=cost,
                        metadata=dict(raw.get("metadata") or {}),
                        creator_handle=raw.get("creator_handle"),
                    )
                )
            adapted = _bazaar_to_chunks(coerced)
            # Group by resource_id so each Bazaar service lands as its
            # own synthetic source row — matches §1.4 of the design memo.
            grouped: dict[str, list[Any]] = {}
            for ch in adapted:
                grouped.setdefault(ch.resource_id, []).append(ch)
            for resource_id, group in grouped.items():
                synthetic_uri = f"bazaar://{resource_id}"
                try:
                    n = await ingest_provider_chunks(
                        session_id=session_id,
                        provider_kind="bazaar",
                        resource_id=resource_id,
                        synthetic_uri=synthetic_uri,
                        chunks=group,
                        store=store,
                    )
                    bazaar_indexed += int(n)
                except Exception as exc:  # pragma: no cover — degrade
                    logger.warning(
                        "wedge-wire: bazaar ingest failed (resource=%s): %s",
                        resource_id,
                        exc,
                    )

    twitsh_chunks = 0
    twitsh_indexed = 0
    twitsh_cost = 0.0
    if twitsh_result is not None and twitsh_result.fired:
        twitsh_tweets = twitsh_result.payload.get("tweets") or []
        twitsh_chunks = len(twitsh_tweets)
        twitsh_cost = float(twitsh_result.cost_usd or 0.0)
        if twitsh_cost > 0:
            try:
                await store.add_cost(session_id, "twitsh", twitsh_cost)
            except Exception as exc:  # pragma: no cover
                logger.warning("integrate-01: twitsh add_cost failed: %s", exc)

        if _WEDGE_WIRE_ENABLED and twitsh_tweets:
            twitsh_adapted = _twitsh_to_chunks(twitsh_tweets)
            if twitsh_adapted:
                synthetic_uri = f"twitsh://session/{session_id}"
                try:
                    twitsh_indexed = await ingest_provider_chunks(
                        session_id=session_id,
                        provider_kind="twitsh",
                        resource_id=str(session_id),
                        synthetic_uri=synthetic_uri,
                        chunks=twitsh_adapted,
                        store=store,
                    )
                except Exception as exc:  # pragma: no cover — degrade
                    logger.warning("wedge-wire: twitsh ingest failed: %s", exc)

    # Arxiv: free, no spend to debit. Surface chunk count so the
    # dashboard can answer "did Arxiv contribute on this run?".
    arxiv_chunks = 0
    arxiv_indexed = 0
    if arxiv_result is not None and arxiv_result.fired:
        arxiv_payload_chunks = arxiv_result.payload.get("chunks") or []
        arxiv_chunks = len(arxiv_payload_chunks)
        if _WEDGE_WIRE_ENABLED and arxiv_payload_chunks:
            arxiv_adapted = _arxiv_to_chunks(arxiv_payload_chunks)
            # One synthetic source row per arxiv entry — they have real
            # abstract URLs (memo §1.4), grouping is by abs_url.
            for ch in arxiv_adapted:
                synthetic_uri = _arxiv_abs_url_for({"metadata": ch.metadata})
                try:
                    n = await ingest_provider_chunks(
                        session_id=session_id,
                        provider_kind="arxiv",
                        resource_id=ch.resource_id,
                        synthetic_uri=synthetic_uri,
                        chunks=[ch],
                        store=store,
                    )
                    arxiv_indexed += int(n)
                except Exception as exc:  # pragma: no cover — degrade
                    logger.warning(
                        "wedge-wire: arxiv ingest failed (resource=%s): %s",
                        ch.resource_id,
                        exc,
                    )

    # Single structured log line — feeds the post-Wave-2 dashboard that
    # answers "did the wedge fire on this run?". Stable key=value form
    # so log-aggregators can pivot without regex contortions.
    logger.info(
        "integrate01_dispatch session_id=%s payment_mode=%s wedge_wire=%s "
        "bazaar_chunks=%d bazaar_indexed=%d bazaar_cost_usd=%.6f "
        "twitsh_chunks=%d twitsh_indexed=%d twitsh_cost_usd=%.6f "
        "arxiv_chunks=%d arxiv_indexed=%d "
        "tavily_quota=%d arxiv_quota=%d",
        session_id,
        payment_mode,
        "on" if _WEDGE_WIRE_ENABLED else "off",
        bazaar_chunks,
        bazaar_indexed,
        bazaar_cost,
        twitsh_chunks,
        twitsh_indexed,
        twitsh_cost,
        arxiv_chunks,
        arxiv_indexed,
        tavily_quota,
        arxiv_quota,
    )

    return (
        f"dispatch: bazaar {bazaar_chunks} chunks (${bazaar_cost:.4f}, indexed {bazaar_indexed}) · "
        f"twitsh {twitsh_chunks} tweets (${twitsh_cost:.4f}, indexed {twitsh_indexed}) · "
        f"arxiv {arxiv_chunks} abstracts (indexed {arxiv_indexed})"
    )


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

    # LLM-hygiene Commit B — resolve through the catalog instead of reading
    # ``orch.chat_model``. ``ask`` is a follow-up Q&A, summarization-shaped;
    # budget tier is appropriate (gpt-4.1-nano on the openai router by
    # default). Plain-text answer with [n] citations — no
    # response_format=json_object on purpose (the route returns prose, not
    # structured JSON; the prior audit was wrong about that).
    from gecko_core.orchestration.settings import resolve_llm_config as _resolve_cfg
    from gecko_core.routing.catalog import (
        AgentRole as _AgentRole,
    )
    from gecko_core.routing.catalog import (
        Tier as _AskTier,
    )
    from gecko_core.routing.catalog import (
        resolve_model_for_router as _resolve_model,
    )

    _ask_cfg = _resolve_cfg(settings=orch)
    _ask_router = (
        _ask_cfg.source.split(":", 1)[1] if _ask_cfg.source.startswith("router:") else "openai"
    )
    _ask_model = _resolve_model(_AgentRole.ask, _AskTier.budget, _ask_router)

    resp = await client.chat.completions.create(
        model=_ask_model,
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


async def degraded_research(
    idea: str,
    *,
    progress_callback: ProgressCallback | None = None,
) -> ResearchResult:
    """S14-HARDEN-01: cache-only research path for backend-down scenarios.

    Reads chunks for the most recent matching session out of the local
    SQLite cache (``~/.gecko/cache.db``) and synthesizes a minimal
    ``ResearchResult`` with the validation report carrying a
    ``mode: degraded`` warning. No fresh sources, no payment gate, no
    Supabase write — purely a fallback so a founder has *something*
    actionable when Supabase is unreachable.

    Raises ``RuntimeError`` when the cache is empty (operator should
    surface this clearly: "no recent runs to fall back to").
    """
    from uuid import uuid4

    from gecko_core.models import (
        PRD,
        BusinessPlan,
        Citation,
        Provenance,
        ResearchResult,
        SourceInfo,
        ValidationReport,
        Verdict,
    )
    from gecko_core.sessions.local_cache import read_recent_chunks

    _emit(progress_callback, "Reading local cache (degraded mode)")
    chunks = read_recent_chunks(idea_substring=idea[:60], limit=20)
    if not chunks:
        raise RuntimeError(
            "degraded mode: local cache is empty; no recent sessions to fall "
            "back to. Run `bb research` once successfully to populate the "
            "cache, then re-run with --degraded when Supabase is down."
        )

    # Build a single-paragraph synthesis from the cached chunk text.
    joined = " ".join(str(c.get("text") or "") for c in chunks)[:2000]
    urls = [str(c.get("source_url") or "") for c in chunks if c.get("source_url")]
    # De-dup URLs while preserving order.
    seen: set[str] = set()
    unique_urls: list[str] = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            unique_urls.append(u)

    # Synthesize Citations off the cached chunks. Provenance carries a
    # synthetic 'local-cache' provider so the renderer can flag the
    # degraded origin in the receipt footer.
    citations: list[Citation] = []
    for idx, c in enumerate(chunks[:10]):
        url_str = str(c.get("source_url") or "")
        if not url_str:
            continue
        try:
            citations.append(
                Citation(
                    source_url=url_str,
                    chunk_index=int(c.get("chunk_index") or idx),
                    similarity=0.5,
                    provenance=Provenance(
                        provider_name="local-cache",
                        provider_kind="free",
                    ),
                )
            )
        except Exception:
            continue

    warning = (
        "DEGRADED MODE — Supabase unreachable; this report was synthesized "
        "from local cache only. No fresh sources fetched. Re-run without "
        "--degraded once the backend recovers."
    )
    validation = ValidationReport(
        market_size_signal="(unavailable in degraded mode)",
        competitor_analysis=joined[:600] or "(no cached evidence)",
        demand_evidence=joined[600:1200] or "(no cached evidence)",
        risk_flags=[warning],
        citations=citations,
        gap_classification="Partial:segment",
        gap_summary="degraded mode — gap classification unavailable",
    )
    business = BusinessPlan(
        problem="(degraded mode — see validation report)",
        icp="(unavailable)",
        solution="(unavailable)",
        market="(unavailable)",
        business_model="(unavailable)",
        channels="(unavailable)",
        risks=[warning],
        citations=citations,
    )
    prd = PRD(
        v1_scope=["(degraded mode — re-run when backend is up)"],
        v2_scope=[],
        v3_scope=[],
        acceptance_criteria=[],
        non_functional=[],
        success_metrics=[],
        citations=citations,
    )
    sources_out: list[SourceInfo] = []
    for u in unique_urls[:5]:
        try:
            sources_out.append(
                SourceInfo(
                    url=u,
                    type="web",
                    chunk_count=sum(1 for c in chunks if c.get("source_url") == u),
                    indexed_at=__import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ),
                )
            )
        except Exception:
            continue

    _emit(progress_callback, "Degraded report ready")
    return ResearchResult(
        session_id=str(uuid4()),
        tier="basic",
        business_plan=business,
        validation_report=validation,
        prd=prd,
        sources=sources_out,
        verdict=Verdict.REFINE,
    )


# Backward-compatible name used by `gecko_core.__init__` re-export.
sources = list_sources


__all__ = ["ask", "degraded_research", "list_sources", "research", "sources"]
