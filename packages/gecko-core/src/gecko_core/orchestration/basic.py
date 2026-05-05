"""Basic-tier orchestration: a single GPT-4o-mini call grounded in RAG context.

Returns a partial `ResearchResult` (the workflow layer stamps `session_id`,
`tier`, and `sources`). One retry on Pydantic validation failure, with the
validation error fed back into the prompt so the model can fix its output.

Citations are post-validated: every `source_url` referenced by the model
must appear in the session's `sources` table (the RAG context). A mismatch
raises `OrchestrationError` rather than silently shipping a hallucination.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from uuid import UUID

import tiktoken
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from gecko_core.llm_helpers import build_response_format
from gecko_core.models import (
    GAP_CLASSIFICATION_FALLBACK,
    PRD,
    BusinessPlan,
    Citation,
    ResearchResult,
    Tier,
    ValidationReport,
    derive_verdict,
    is_low_grounding,
)
from gecko_core.orchestration.settings import (
    get_orchestration_settings,
    resolve_llm_config,
)
from gecko_core.rag.query import RagChunk, rag_query
from gecko_core.routing.catalog import AgentRole, resolve_model_for_router
from gecko_core.routing.catalog import Tier as CatalogTier
from gecko_core.sessions.store import SessionStore

logger = logging.getLogger(__name__)

_enc = tiktoken.get_encoding("cl100k_base")


# LLM-hygiene Commit B — basic-tier research model resolves through the
# catalog at call time instead of reading ``OrchestrationSettings.chat_model``
# directly. Default tier is balanced (DeepSeek V3.2 on the general_reasoning
# task profile); ``LLM_ROUTER=openai`` substitutes the OpenAI-only fallback.
_BASIC_RESEARCH_TIER = CatalogTier.balanced


def _resolve_basic_research_model() -> tuple[str, str]:
    """Return ``(model_id, router_name)``.

    Router name is needed at the call site so Commit D can decide whether
    to opt into Structured Outputs strict mode (OpenAI direct only today;
    OpenRouter non-OpenAI providers stay on json_object + the f67b211
    Pydantic adapter safety net).
    """
    cfg = resolve_llm_config(settings=get_orchestration_settings())
    router_name = cfg.source.split(":", 1)[1] if cfg.source.startswith("router:") else "openai"
    model_id = resolve_model_for_router(AgentRole.research_basic, _BASIC_RESEARCH_TIER, router_name)
    return model_id, router_name


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
  gap_classification: <one of the gap labels below>, gap_summary: str,
  gap_explanation: str}

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

gap_explanation (REQUIRED) is 2-3 sentences that ground the gap_classification
label in the actual chunks. It MUST do all three of:
  1. Name the dimension explicitly — restate WHICH facet is partial (UX,
     segment, pricing, integration, geo). Don't make the reader decode
     "Partial:UX"; say "the UX is the gap" in plain English.
  2. Cite specific chunks via `[n]` markers — every concrete claim is
     followed by `[1]`, `[2]`, etc., where `n` matches the bracket index
     in the Context block (the same `[n]` your citations refer to).
     Cite at least one chunk. Never invent a chunk index.
  3. Name the CONSEQUENCE for the founder — what they would ship
     differently if this gap were closed. One concrete shipping move,
     not platitudes ("focus on UX" is too vague; "ship a one-click
     replay debugger for Stripe webhooks before competing on dashboards"
     is the bar).
Example: "The UX is the gap, not the category — Stripe Radar covers fraud
detection but its replay-debug flow assumes you already know which
webhook fired [3]. A payments engineer triaging at 2am needs the
opposite onramp [4]. Ship the replay-from-error path before competing
on the dashboard." Keep to 3 sentences max so it fits on a single
terminal screen.
prd: {v1_scope, v2_scope, v3_scope, acceptance_criteria, non_functional,
  success_metrics: each list[str], citations: list[Citation]}

Citation = {source_url: <one of the urls in the context>, chunk_index: int,
  similarity: float in [0,1]}.

Rules:
- Every document MUST include at least one citation.
- Every citation's source_url MUST be one of the URLs that appears in the
  context block. Never invent URLs.
- Citations may use https://, bazaar://, twitsh:// or arxiv URIs. All are
  valid evidence — do not skip a chunk because its URI is not https.
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
    max_tokens: int | None = None,
    response_format: dict[str, Any] | None = None,
) -> tuple[str, float]:
    """Run a chat completion and return (content, estimated_cost_usd).

    Cost resolution order:
    1. `x-clawrouter-cost-usd` response header (preferred — real spend).
    2. Token-based estimate from `usage` * `_MODEL_RATES_USD_PER_1M`.
    3. 0.0 (unknown model, no header) — surfaces as zero on the dashboard.

    Reproducibility: ``seed=42`` is forwarded for best-effort determinism
    on supporting providers. OpenRouter passes ``seed`` per-provider; not
    all providers honor it (best-effort, not bit-identical across runs;
    Anthropic and Google silently ignore the kwarg).

    LLM-hygiene Commit D: ``response_format`` defaults to ``json_object``
    so the existing test surface and any caller that doesn't thread the
    kwarg keeps the legacy behavior. Pass a strict-mode ``json_schema``
    payload when the (model, router) supports it; the wrapper does not
    decide that for you — the caller resolves it via
    ``llm_helpers.build_response_format``. We stay on
    ``with_raw_response.create`` rather than ``client.beta.chat.completions
    .parse`` so the ClawRouter cost header (``x-clawrouter-cost-usd``) is
    still readable; the Beta helper hides response headers.
    """
    create_kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": response_format
        if response_format is not None
        else {"type": "json_object"},
        "temperature": temperature,
        "seed": 42,
    }
    if max_tokens is not None:
        create_kwargs["max_tokens"] = max_tokens
    # Retry up to 2 times on empty content — OpenRouter-routed models
    # occasionally return empty responses on cold-start or transient blips.
    # Exception: finish_reason=length means the token budget was exhausted
    # before any visible output (reasoning models consume budget on internal
    # thinking tokens before emitting visible content). Retrying the same
    # params would hit the same wall — raise immediately instead.
    _MAX_EMPTY_RETRIES = 2
    for _attempt in range(_MAX_EMPTY_RETRIES + 1):
        raw = await client.chat.completions.with_raw_response.create(**create_kwargs)
        resp = raw.parse()
        content = resp.choices[0].message.content
        finish_reason = resp.choices[0].finish_reason if resp.choices else None
        if content:
            break
        if finish_reason == "length":
            logger.warning(
                "llm.empty_content finish_reason=length model=%s — "
                "token budget exhausted before visible output; aborting retries",
                model,
            )
            raise OrchestrationError(
                "LLM returned empty content (finish_reason=length — token budget exhausted)"
            )
        if _attempt < _MAX_EMPTY_RETRIES:
            await asyncio.sleep(2.0 * (_attempt + 1))
    else:
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


# Match `[N]` (1-3 digits) — narrow on purpose: we don't want to strip
# things like `[ref-12]` or `[author 2024]` from prose. The basic prompt
# instructs the model to use bare integer brackets, and the format_context
# emits `[1]` `[2]` ... so the contract is bounded integers.
_INLINE_CHUNK_REF_RE = re.compile(r"\[(\d{1,3})\]")


def _filter_inline_chunk_refs(text: str | None, max_chunk_index: int) -> tuple[str | None, int]:
    """Strip `[n]` markers in ``text`` whose ``n`` is outside ``1..max_chunk_index``.

    `gap_explanation` (and any future inline-cited prose surface) is asked
    to cite chunks via `[n]` markers matching the indices in the Context
    block. This is the consistency guard requested by the 2026-05-03
    dogfood follow-up: align inline `[n]` refs with the chunk list the
    same way `_drop_unknown_citations` aligns Citation objects with the
    indexed sources. Dangling `[99]` refs (where the model invented a
    high index) get stripped; valid `[1]` `[2]` survive. Returns
    ``(filtered_text, dropped_count)``. ``None`` text round-trips as
    ``(None, 0)`` so the call site doesn't have to special-case it.
    """
    if not text:
        return text, 0
    if max_chunk_index < 1:
        # No chunks were retrieved; any [n] is dangling. Strip them all.
        new = _INLINE_CHUNK_REF_RE.sub("", text)
        dropped = len(_INLINE_CHUNK_REF_RE.findall(text))
        return new, dropped

    dropped_count = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal dropped_count
        n = int(match.group(1))
        if 1 <= n <= max_chunk_index:
            return match.group(0)
        dropped_count += 1
        return ""

    new_text = _INLINE_CHUNK_REF_RE.sub(_sub, text)
    return new_text, dropped_count


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
    "sentence ≤ 140 chars naming the closest competitor + the facet they miss, "
    "AND validation_report.gap_explanation as a 2-3 sentence narrative that "
    "(a) restates which dimension is partial in plain English, (b) cites at "
    "least one chunk via `[n]` markers matching the Context block, and "
    "(c) names the concrete shipping consequence for the founder. "
    "Return corrected JSON only."
)

# 2026-05-03 v0.1.10 — narrower retry suffix for the case where
# gap_classification is fine but gap_explanation is missing/empty. We
# tell the model exactly which field it forgot rather than re-prompting
# the entire validation contract; matches the "model self-correction
# prompt, not a fill-in" posture from the dogfood brief.
_GAP_EXPLANATION_RETRY_SUFFIX = (
    "\n\nYour previous response omitted validation_report.gap_explanation "
    "(or returned it as an empty string / null). The field is REQUIRED and "
    "MUST be a 2-3 sentence narrative that (a) restates which dimension is "
    "partial in plain English, (b) cites at least one chunk via `[n]` markers "
    "matching the Context block, and (c) names the concrete shipping "
    "consequence for the founder. Without this field the response is invalid. "
    "Keep gap_classification, gap_summary, business_plan, and prd identical "
    "to your previous response. Return corrected JSON only."
)

# Maximum number of retries for the gap_explanation re-prompt path (separate
# from the gap_classification retry, which already runs once). v0.1.10 caps at
# 2 per the brief — beyond that we accept the model's non-compliance, mark
# ``low_explanation=True`` on the result, and let the renderer surface
# honestly. Bump only with eval-harness evidence.
_GAP_EXPLANATION_MAX_RETRIES: int = 2

# Cost-gate threshold: only retry the gap_explanation re-prompt when the
# first call's cost was below this floor. Prevents 3x'ing the spend on an
# expensive run for a prose-quality field. The basic-tier first call on
# gpt-4o-mini lands ~$0.0015; on Kimi via OpenRouter ~$0.005-0.01; on
# heavier providers we accept the missing field rather than burn budget.
_GAP_EXPLANATION_RETRY_COST_FLOOR_USD: float = 0.05


def _has_gap_explanation(report: ValidationReport) -> bool:
    """True iff ``report.gap_explanation`` is a non-empty string after stripping.

    The Pydantic field is ``str | None``; treat ``None`` and whitespace-only
    strings the same — both surface as "the model didn't comply" to the
    operator. Source-of-truth lives on the parsed model rather than the raw
    JSON because the strict-mode path emits ``"gap_explanation": null``
    exactly and the json_object fallback may emit ``""`` — both should
    trigger the same retry/flag.
    """
    val = report.gap_explanation
    if val is None:
        return False
    if not isinstance(val, str):
        return False
    return bool(val.strip())


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


def _validate_citations(
    out: _LLMOutput, allowed_urls: set[str], *, chunk_count: int = 0
) -> _LLMOutput:
    """Drop citations referencing URLs that didn't make it into the index.

    Replaces the pre-Sprint 8 hard-crash behavior. Logs each dropped
    citation at WARNING so the dogfood operator can see when an agent
    hallucinated past the failed-ingest list, but the session still
    completes with the surviving citations.

    Also strips dangling inline `[n]` markers from ``gap_explanation``
    whose ``n`` doesn't correspond to a real chunk index in the Context
    block (2026-05-03 dogfood follow-up — same consistency posture as
    URL filtering, applied to the prose surface).
    """
    out, dropped = _drop_unknown_citations(out, allowed_urls)
    for where, url in dropped:
        logger.warning(
            "citation.dropped where=%s unknown_url=%s "
            "(source not in indexed set; degrading gracefully)",
            where,
            url,
        )
    # Inline [n] refs in gap_explanation must match real chunk indices.
    # Dangling refs get stripped; readable prose survives.
    cleaned_explanation, refs_dropped = _filter_inline_chunk_refs(
        out.validation_report.gap_explanation, chunk_count
    )
    if refs_dropped:
        logger.warning(
            "gap_explanation.inline_ref.dropped count=%d "
            "(out-of-range [n] markers stripped; chunk_count=%d)",
            refs_dropped,
            chunk_count,
        )
    if cleaned_explanation != out.validation_report.gap_explanation:
        out.validation_report = out.validation_report.model_copy(
            update={"gap_explanation": cleaned_explanation}
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
    model_id, router_name = _resolve_basic_research_model()
    # LLM-hygiene Commit D: opt into Structured Outputs strict mode for the
    # OpenAI plane only; non-OpenAI OpenRouter providers (Kimi K2.6 default)
    # stay on json_object + the f67b211 Pydantic adapters as the safety net.
    research_response_format = build_response_format(_LLMOutput, model_id, router_name)
    raw, cost1 = await _call_llm(
        client=client,
        model=model_id,
        system=_SYSTEM_PROMPT,
        user=user,
        temperature=orch.temperature,
        max_tokens=orch.max_tokens_research_basic,
        response_format=research_response_format,
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
            model=model_id,
            system=_SYSTEM_PROMPT,
            user=retry_user,
            temperature=orch.temperature,
            max_tokens=orch.max_tokens_research_basic,
            response_format=research_response_format,
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
            model=model_id,
            system=_SYSTEM_PROMPT,
            user=retry_user,
            temperature=orch.temperature,
            max_tokens=orch.max_tokens_research_basic,
            response_format=research_response_format,
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

    # 2026-05-03 v0.1.10 — bounded retry loop for the gap_explanation field.
    # The strict-mode path on the OpenAI plane already makes the field
    # PRESENT (it's listed in `required` of the schema produced by
    # `pydantic_to_strict_schema`), but the model is still allowed to emit
    # `null`. The OpenRouter plane (Kimi K2.6, deepseek, etc.) runs on
    # `json_object` and can omit the field entirely. Either way: if we land
    # here with empty/None gap_explanation, re-prompt with a strict
    # self-correction suffix. Cap at `_GAP_EXPLANATION_MAX_RETRIES`; gate on
    # cost so we don't 3x the spend on expensive providers. NO synthesis on
    # exhaustion — `low_explanation` is set so the renderer surfaces the
    # missing field honestly.
    explanation_retries_used = 0
    if not _has_gap_explanation(out.validation_report):
        cost_so_far = cost1
        while (
            not _has_gap_explanation(out.validation_report)
            and explanation_retries_used < _GAP_EXPLANATION_MAX_RETRIES
        ):
            if cost_so_far >= _GAP_EXPLANATION_RETRY_COST_FLOOR_USD:
                logger.warning(
                    "gap_explanation.retry.skipped reason=cost_floor "
                    "cost_so_far=%.4f floor=%.4f — accepting missing field",
                    cost_so_far,
                    _GAP_EXPLANATION_RETRY_COST_FLOOR_USD,
                )
                break
            explanation_retries_used += 1
            logger.info(
                "gap_explanation.retry attempt=%d/%d (model self-correction)",
                explanation_retries_used,
                _GAP_EXPLANATION_MAX_RETRIES,
            )
            retry_user_exp = user + _GAP_EXPLANATION_RETRY_SUFFIX
            try:
                raw_exp, cost_exp = await _call_llm(
                    client=client,
                    model=model_id,
                    system=_SYSTEM_PROMPT,
                    user=retry_user_exp,
                    temperature=orch.temperature,
                    max_tokens=orch.max_tokens_research_basic,
                    response_format=research_response_format,
                )
            except OrchestrationError as exc:
                # Empty content on the retry — accept and surface honestly.
                logger.warning(
                    "gap_explanation.retry.empty_content attempt=%d: %s",
                    explanation_retries_used,
                    exc,
                )
                break
            await store.add_cost(session_id, "llm", cost_exp)
            cost_so_far += cost_exp
            try:
                out_exp = _LLMOutput.model_validate_json(raw_exp)
            except ValidationError as ve:
                # Bad JSON on retry — keep the prior `out` (gap_classification
                # is still valid from the earlier pass) and try again or
                # exit the loop on next iteration.
                logger.warning(
                    "gap_explanation.retry.bad_json attempt=%d: %s",
                    explanation_retries_used,
                    ve,
                )
                continue
            # Lift only the validation_report so plan / prd / citations
            # don't drift between retries — same posture as the
            # gap_classification retry above.
            if _has_gap_explanation(out_exp.validation_report):
                out.validation_report = out_exp.validation_report

    out = _validate_citations(out, allowed_urls, chunk_count=len(chunks))

    # 2026-05-03 v0.1.10 — re-check after the validate_citations pass: an
    # explanation that consisted solely of dangling [n] markers (now
    # stripped) could become effectively empty. Surface honestly.
    low_explanation = not _has_gap_explanation(out.validation_report)
    if low_explanation:
        logger.warning(
            "low_explanation=True (gap_explanation missing/empty after %d retry attempts) "
            "— surfacing as flag, not synthesizing fallback",
            explanation_retries_used,
        )

    # S26-SOURCE-DIVERSITY-01 — stamp provenance.provider_kind on citations
    # from the rag_query chunk lookup. The LLM emits source_url strings that
    # match chunk source_urls (twitsh://, bazaar://, https://…); the
    # Provenance default is "free" which makes every citation look like web.
    # Stamping the real kind here lets audit_provider_mix detect diversity
    # even after the citation is serialized (the audit uses Provenance before
    # falling back to URI-scheme inference).
    _chunk_kind_by_url: dict[str, str] = {c.source_url: c.provider_kind for c in chunks}
    from gecko_core.models import Provenance as _Prov

    def _stamp(citations_list: list[Citation]) -> list[Citation]:
        out_list: list[Citation] = []
        for cit in citations_list:
            kind = _chunk_kind_by_url.get(str(cit.source_url))
            if kind and kind != (cit.provenance.provider_kind if cit.provenance else "free"):
                cit = cit.model_copy(update={"provenance": _Prov(provider_kind=kind)})
            out_list.append(cit)
        return out_list

    out.business_plan.citations = _stamp(out.business_plan.citations)
    out.validation_report.citations = _stamp(out.validation_report.citations)
    out.prd.citations = _stamp(out.prd.citations)

    # S11-VERDICT-01 — stamp the single-token verdict derived from the
    # typed gap_classification. Basic tier has no advisor panel running,
    # so we pass advisor_consensus=None — `derive_verdict` treats that as
    # 0.0 and leans REFINE for the pricing/integration partials. The
    # downstream advisor panel (gecko_advise / gecko_plan) can promote to
    # GO on its own surface; the research output stays conservative.
    #
    # S17-VERDICT-01 — pass the citations so derive_verdict can apply the
    # evidence-strength floor (≥3 chunks AND max similarity ≥ 0.40);
    # below the floor, verdict is forced to REFINE and ``low_grounding``
    # is True for the renderer.
    citations = out.validation_report.citations
    low_grounding = is_low_grounding(citations)
    verdict = derive_verdict(
        out.validation_report.gap_classification,
        citations=citations,
    )
    # S20-PROVIDER-MIX-FLOOR-01 — informational provider-mix audit on the
    # final citation set. Does not affect verdict or verdict_hash.
    from gecko_core.orchestration.provider_mix_audit import audit_provider_mix

    provider_mix_flag = audit_provider_mix(citations)

    return ResearchResult(
        session_id=str(session_id),
        tier="basic",
        business_plan=out.business_plan,
        validation_report=out.validation_report,
        prd=out.prd,
        sources=sources,
        verdict=verdict,
        low_grounding=low_grounding,
        low_explanation=low_explanation,
        provider_mix_flag=provider_mix_flag,
    )


# Re-export typing helpers callers may want (kept here so tier dispatcher stays narrow).
def _ensure_tier(tier: Tier) -> None:
    # Pro tier is now wired in workflows.research(); kept as a no-op so
    # legacy callers that imported this don't break.
    return None


__all__ = ["OrchestrationError", "_ensure_tier", "generate"]
