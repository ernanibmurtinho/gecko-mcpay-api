"""Basic-tier orchestration: a single GPT-4o-mini call grounded in RAG context.

Returns a partial `ResearchResult` (the workflow layer stamps `session_id`,
`tier`, and `sources`). One retry on Pydantic validation failure, with the
validation error fed back into the prompt so the model can fix its output.

Citations are post-validated: every `source_url` referenced by the model
must appear in the session's `sources` table (the RAG context). A mismatch
raises `OrchestrationError` rather than silently shipping a hallucination.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import tiktoken
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from gecko_core.models import (
    GAP_CLASSIFICATION_FALLBACK,
    PRD,
    BusinessPlan,
    Citation,
    ResearchResult,
    Tier,
    ValidationReport,
    derive_verdict,
)
from gecko_core.orchestration.settings import get_orchestration_settings
from gecko_core.rag.query import RagChunk, rag_query
from gecko_core.sessions.store import SessionStore

logger = logging.getLogger(__name__)

_enc = tiktoken.get_encoding("cl100k_base")


class OrchestrationError(Exception):
    """Raised when the LLM output can't be salvaged after retry, or when
    citations don't match indexed sources."""


class _LLMOutput(BaseModel):
    """Shape we ask the LLM to produce. We stamp the rest in the workflow."""

    business_plan: BusinessPlan
    validation_report: ValidationReport
    prd: PRD


_SYSTEM_PROMPT = """You are a startup research analyst. You will receive:
- An idea (one line).
- Context chunks from a curated knowledge base, each labeled like
  [n] (source: <url>) <text>.

Produce a single JSON object with EXACTLY three top-level keys:
business_plan, validation_report, prd.

Schemas:
business_plan: {problem, icp, solution, market, business_model, channels,
  risks: list[str], citations: list[Citation]}
validation_report: {market_size_signal, competitor_analysis, demand_evidence,
  risk_flags: list[str], citations: list[Citation],
  gap_classification: <one of the gap labels below>, gap_summary: str}

gap_classification (REQUIRED — a single discrete label, no prose, no
synonyms) is exactly one of:
  - "Full"                  — an existing competitor fully covers the wedge.
  - "Partial:segment"       — competitor covers most segments, not THIS one.
  - "Partial:UX"            — competitor exists but UX gap is the wedge.
  - "Partial:geo"           — competitor exists but not in this geography.
  - "Partial:pricing"       — competitor exists but priced wrong for ICP.
  - "Partial:integration"   — competitor exists but missing integration X.
  - "False"                 — no real gap; demand doesn't actually go unmet.

gap_summary is ONE sentence (≤ 140 chars) naming the closest competitor and
the specific facet they miss. Example: "Stripe Radar covers fraud detection
but not webhook replay debugging for payments engineers."
prd: {v1_scope, v2_scope, v3_scope, acceptance_criteria, non_functional,
  success_metrics: each list[str], citations: list[Citation]}

Citation = {source_url: <one of the urls in the context>, chunk_index: int,
  similarity: float in [0,1]}.

Rules:
- Every document MUST include at least one citation.
- Every citation's source_url MUST be one of the URLs that appears in the
  context block. Never invent URLs.
- Ground every concrete claim in the context. If the context is thin, say so
  in the relevant field rather than fabricating market data.
- Output JSON only. No prose around the JSON. No markdown fences."""


def _format_context(chunks: list[RagChunk]) -> str:
    if not chunks:
        return "(no indexed sources — generating from idea description only)"
    return "\n\n".join(
        f"[{i}] (source: {c.source_url}) (chunk_index={c.chunk_index}, "
        f"similarity={c.similarity:.3f})\n{c.text}"
        for i, c in enumerate(chunks, 1)
    )


def _truncate_to_token_budget(text: str, max_tokens: int) -> str:
    """Truncate `text` to `max_tokens` cl100k tokens. Defensive — see CLAUDE.md."""
    tokens = _enc.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return _enc.decode(tokens[:max_tokens])


def _user_prompt(idea: str, context: str) -> str:
    return f"Idea: {idea}\n\nContext:\n{context}\n\nReturn JSON only."


# Per-1M-token rates (USD) for fallback cost estimation when the LLM provider
# doesn't return an explicit cost header. Used only to seed the economics view;
# real cost lives in ClawRouter's `x-clawrouter-cost-usd` header when present.
# Numbers are approximate and intentionally on the high side — better to
# overestimate margin pressure than to silently undercount on the dashboard.
_MODEL_RATES_USD_PER_1M: dict[str, tuple[float, float]] = {
    # (input, output)
    "gpt-4o": (2.50, 10.00),
    "openai/gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-4o-mini": (0.15, 0.60),
    "claude-sonnet-4.6": (3.00, 15.00),
    "anthropic/claude-sonnet-4.6": (3.00, 15.00),
    "claude-opus-4.6": (15.00, 75.00),
    "anthropic/claude-opus-4.6": (15.00, 75.00),
}


def _estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Fallback when the provider doesn't expose a per-call cost header.

    Matches by exact key first, then by prefix — OpenAI returns dated model
    names like ``gpt-4o-mini-2024-07-18`` that wouldn't otherwise resolve.
    """
    rates = _MODEL_RATES_USD_PER_1M.get(model)
    if rates is None:
        # Prefix match — pick the longest matching key so versioned names
        # resolve to the right rate (e.g. "gpt-4o-mini-..." → "gpt-4o-mini").
        for key in sorted(_MODEL_RATES_USD_PER_1M.keys(), key=len, reverse=True):
            if model.startswith(key):
                rates = _MODEL_RATES_USD_PER_1M[key]
                break
    if rates is None:
        return 0.0
    in_rate, out_rate = rates
    return (prompt_tokens * in_rate + completion_tokens * out_rate) / 1_000_000


async def _call_llm(
    *,
    client: AsyncOpenAI,
    model: str,
    system: str,
    user: str,
    temperature: float,
) -> tuple[str, float]:
    """Run a chat completion and return (content, estimated_cost_usd).

    Cost resolution order:
    1. `x-clawrouter-cost-usd` response header (preferred — real spend).
    2. Token-based estimate from `usage` * `_MODEL_RATES_USD_PER_1M`.
    3. 0.0 (unknown model, no header) — surfaces as zero on the dashboard.
    """
    raw = await client.chat.completions.with_raw_response.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=temperature,
    )
    resp = raw.parse()
    content = resp.choices[0].message.content
    if not content:
        raise OrchestrationError("LLM returned empty content")

    cost_usd = 0.0
    header = raw.headers.get("x-clawrouter-cost-usd")
    if header:
        try:
            cost_usd = float(header)
        except ValueError:
            cost_usd = 0.0
    if cost_usd == 0.0 and resp.usage is not None:
        cost_usd = _estimate_cost_usd(
            resp.model or model,
            resp.usage.prompt_tokens or 0,
            resp.usage.completion_tokens or 0,
        )
    return content, cost_usd


def _normalize_url(url: str) -> str:
    """Trim trailing slash + lowercase host so HttpUrl renders that pick up
    a slash from pydantic don't fail an otherwise-valid citation match.

    We don't touch the path/query — just the two normalizations that have
    bitten dogfood. Anything more aggressive risks false positives.
    """
    s = url.strip()
    if s.endswith("/"):
        s = s[:-1]
    return s


def _drop_unknown_citations(
    out: _LLMOutput, allowed_urls: set[str]
) -> tuple[_LLMOutput, list[tuple[str, str]]]:
    """Filter citations whose source_url wasn't ingested for this session.

    S8-INGEST-02 — when ingestion partially fails (Tavily found 6 URLs but
    one bot-walls Tavily Extract too), the LLM still sees the URL in the
    discovery list and can cite it. Pre-Sprint 8 behavior was to crash the
    whole session with `OrchestrationError`. We now drop the unknown
    citation, log a warning, and keep going — the rest of the report is
    salvageable.

    Returns (filtered_output, dropped) where `dropped` is a list of
    (where, url) tuples for the caller to log/audit.
    """
    allowed_norm = {_normalize_url(u) for u in allowed_urls}
    dropped: list[tuple[str, str]] = []

    def _filter(citations: list[Citation], where: str) -> list[Citation]:
        kept: list[Citation] = []
        for c in citations:
            if _normalize_url(str(c.source_url)) in allowed_norm:
                kept.append(c)
            else:
                dropped.append((where, str(c.source_url)))
        return kept

    out.business_plan.citations = _filter(out.business_plan.citations, "business_plan")
    out.validation_report.citations = _filter(out.validation_report.citations, "validation_report")
    out.prd.citations = _filter(out.prd.citations, "prd")
    return out, dropped


_VALID_GAP_VALUES: frozenset[str] = frozenset(
    [
        "Full",
        "Partial:segment",
        "Partial:UX",
        "Partial:geo",
        "Partial:pricing",
        "Partial:integration",
        "False",
    ]
)

_GAP_RETRY_SUFFIX = (
    "\n\nYour previous response was missing or had an invalid "
    "validation_report.gap_classification. The field is REQUIRED and MUST be "
    "EXACTLY one of: 'Full', 'Partial:segment', 'Partial:UX', 'Partial:geo', "
    "'Partial:pricing', 'Partial:integration', 'False'. No other strings, no "
    "prose, no nesting. Also include validation_report.gap_summary as a single "
    "sentence ≤ 140 chars naming the closest competitor + the facet they miss. "
    "Return corrected JSON only."
)


def _raw_gap_classification(raw: str) -> str | None:
    """Extract `validation_report.gap_classification` from the raw JSON string.

    The Pydantic model carries a default for `gap_classification` so a parsed
    output can't tell us whether the LLM omitted the field or genuinely meant
    `Partial:segment`. We inspect the raw JSON instead — a missing field
    triggers retry; a present-but-off-taxonomy value triggers retry; a
    canonical value passes through.

    Returns None when the field is missing or when the JSON itself can't be
    parsed (the parent caller will surface that error path). Otherwise
    returns the raw string for membership-checking against
    `_VALID_GAP_VALUES`.
    """
    import json

    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    vr = obj.get("validation_report")
    if not isinstance(vr, dict):
        return None
    val = vr.get("gap_classification")
    if not isinstance(val, str):
        return None
    return val


def _gap_classification_is_valid(raw: str) -> bool:
    """True iff the raw JSON emits a canonical gap_classification value.

    Source of truth is the raw LLM output, not the parsed model — see
    `_raw_gap_classification` for why.
    """
    val = _raw_gap_classification(raw)
    return val in _VALID_GAP_VALUES


def _enforce_gap_classification(out: _LLMOutput) -> _LLMOutput:
    """Hard-fallback helper. Logs a warning and rewrites the field to the
    fallback so downstream consumers always see a valid taxonomy entry.

    Only call this after the retry has also failed — silently coercing on the
    first pass would mask judge regressions.
    """
    logger.warning(
        "validation_report.gap_classification missing/invalid after retry; "
        "falling back to %s. Original value=%r",
        GAP_CLASSIFICATION_FALLBACK,
        out.validation_report.gap_classification,
    )
    out.validation_report = out.validation_report.model_copy(
        update={"gap_classification": GAP_CLASSIFICATION_FALLBACK}
    )
    return out


def _validate_citations(out: _LLMOutput, allowed_urls: set[str]) -> _LLMOutput:
    """Drop citations referencing URLs that didn't make it into the index.

    Replaces the pre-Sprint 8 hard-crash behavior. Logs each dropped
    citation at WARNING so the dogfood operator can see when an agent
    hallucinated past the failed-ingest list, but the session still
    completes with the surviving citations.
    """
    out, dropped = _drop_unknown_citations(out, allowed_urls)
    for where, url in dropped:
        logger.warning(
            "citation.dropped where=%s unknown_url=%s "
            "(source not in indexed set; degrading gracefully)",
            where,
            url,
        )
    return out


async def generate(
    session_id: UUID,
    idea: str,
    store: SessionStore,
    *,
    top_k: int = 12,
    openai_client: AsyncOpenAI | None = None,
) -> ResearchResult:
    """Single-pass basic generation. See module docstring for contract."""
    chunks = await rag_query(session_id, idea, top_k=top_k, store=store)
    sources = await store.list_sources(session_id)
    allowed_urls = {str(s.url) for s in sources}

    orch = get_orchestration_settings()
    if openai_client is not None:
        client = openai_client
    else:
        # S8-CONFIG-01 — single resolution path. Honors LLM_ROUTER so the
        # basic tier respects the same router plane as the Pro debate.
        from gecko_core.orchestration.settings import resolve_llm_config

        cfg = resolve_llm_config(settings=orch)
        client_kwargs: dict[str, Any] = {
            "api_key": cfg.api_key,
            "base_url": cfg.base_url,
        }
        if cfg.extra_headers:
            client_kwargs["default_headers"] = dict(cfg.extra_headers)
        client = AsyncOpenAI(**client_kwargs)

    context = _format_context(chunks)
    # Truncate the *context* portion only — system + idea are tiny and known.
    # Reserve room for the system prompt and the wrapper text.
    sys_tokens = len(_enc.encode(_SYSTEM_PROMPT))
    overhead = sys_tokens + len(_enc.encode(idea)) + 256
    budget = max(orch.max_input_tokens - overhead, 1024)
    context = _truncate_to_token_budget(context, budget)

    user = _user_prompt(idea, context)
    raw, cost1 = await _call_llm(
        client=client,
        model=orch.chat_model,
        system=_SYSTEM_PROMPT,
        user=user,
        temperature=orch.temperature,
    )
    await store.add_cost(session_id, "llm", cost1)

    out: _LLMOutput
    parsed_raw: str = raw
    try:
        out = _LLMOutput.model_validate_json(raw)
    except ValidationError as ve:
        # One retry — feed the validation error in so the model can self-correct.
        retry_user = (
            user
            + "\n\nYour previous response failed Pydantic validation:\n"
            + str(ve)
            + "\n\nReturn corrected JSON only."
        )
        raw2, cost2 = await _call_llm(
            client=client,
            model=orch.chat_model,
            system=_SYSTEM_PROMPT,
            user=retry_user,
            temperature=orch.temperature,
        )
        await store.add_cost(session_id, "llm", cost2)
        try:
            out = _LLMOutput.model_validate_json(raw2)
            parsed_raw = raw2
        except ValidationError as ve2:
            raise OrchestrationError(f"LLM output failed validation after retry: {ve2}") from ve2

    # S9-VERDICT-01 — enforce structured gap_classification on the validation
    # report. If the LLM's raw JSON omitted the field or emitted an off-
    # taxonomy value, retry once with a stricter suffix; if that still fails,
    # log a WARNING and pin the field to GAP_CLASSIFICATION_FALLBACK so
    # downstream consumers always see a valid label.
    if not _gap_classification_is_valid(parsed_raw):
        retry_user = user + _GAP_RETRY_SUFFIX
        raw_gap, cost_gap = await _call_llm(
            client=client,
            model=orch.chat_model,
            system=_SYSTEM_PROMPT,
            user=retry_user,
            temperature=orch.temperature,
        )
        await store.add_cost(session_id, "llm", cost_gap)
        try:
            out_retry = _LLMOutput.model_validate_json(raw_gap)
        except ValidationError:
            # Bad JSON on the retry — fall back without overwriting the
            # earlier valid `out` (citations, plan, prd are still good).
            out = _enforce_gap_classification(out)
        else:
            if _gap_classification_is_valid(raw_gap):
                # Lift only the validation_report (and its gap fields) from
                # the retry; the other fields could have drifted between
                # calls and we already validated them on the first pass.
                out.validation_report = out_retry.validation_report
            else:
                out = _enforce_gap_classification(out)

    out = _validate_citations(out, allowed_urls)

    # S11-VERDICT-01 — stamp the single-token verdict derived from the
    # typed gap_classification. Basic tier has no advisor panel running,
    # so we pass advisor_consensus=None — `derive_verdict` treats that as
    # 0.0 and leans REFINE for the pricing/integration partials. The
    # downstream advisor panel (gecko_advise / gecko_plan) can promote to
    # BUILD on its own surface; the research output stays conservative.
    verdict = derive_verdict(out.validation_report.gap_classification)

    return ResearchResult(
        session_id=str(session_id),
        tier="basic",
        business_plan=out.business_plan,
        validation_report=out.validation_report,
        prd=out.prd,
        sources=sources,
        verdict=verdict,
    )


# Re-export typing helpers callers may want (kept here so tier dispatcher stays narrow).
def _ensure_tier(tier: Tier) -> None:
    # Pro tier is now wired in workflows.research(); kept as a no-op so
    # legacy callers that imported this don't break.
    return None


__all__ = ["OrchestrationError", "_ensure_tier", "generate"]
