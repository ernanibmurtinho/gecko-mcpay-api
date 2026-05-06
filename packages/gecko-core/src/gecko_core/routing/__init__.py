"""gecko_route — cost-aware LLM routing surface (S3-05).

Public entry point: ``route(prompt, task_hint, max_cost_usd, prefer_premium)``.

Picks a model from the routing matrix, charges the caller's x402 wallet,
calls the chosen model via the existing OpenAI-compatible transport
(`gecko_core.orchestration.pro.router`), and returns a `RouteResult` with
cost + savings observability.

Design notes:
- Matrix lives in ``matrix.py`` as data, not branches — pricing changes
  without code changes.
- Prices live in ``costs.py`` (per-model $/M input + $/M output).
- We never silently truncate. Over-budget on the cheapest model is an error.
- x402 charge happens BEFORE the LLM call so a crash mid-call still leaves a
  paid intent (the Pro tier follows the same persist-before-work rule).
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from gecko_core.orchestration.settings import LLMClientConfig
from gecko_core.payments.x402_client import get_client, new_intent_id
from gecko_core.routing.costs import estimate_cost_usd, estimate_tokens, price_for
from gecko_core.routing.matrix import (
    DEFAULT_TASK_HINT,
    TaskHint,
    candidate_models,
    pick_model,
)
from gecko_core.routing.models import RouteResult

if TYPE_CHECKING:
    from gecko_core.payments.models import PaymentIntent

logger = logging.getLogger(__name__)


class RouteBudgetError(Exception):
    """Raised when even the cheapest candidate model exceeds ``max_cost_usd``."""


class RoutePaymentError(Exception):
    """Raised when the x402 charge for a routed call fails. No retry."""


def _build_intent(amount_usd: Decimal) -> PaymentIntent:
    """Construct a stub-friendly PaymentIntent for a routed call.

    The Pro debate uses a real session UUID. Routed calls are session-less
    by design (one-shot), so we mint a fresh UUID4 per call as the
    session_id. Tier is "basic" — gecko_route does not use the Pro debate
    machinery.
    """
    from gecko_core.payments.models import PaymentIntent

    return PaymentIntent(
        intent_id=new_intent_id(),
        session_id=uuid.uuid4(),
        tier="basic",
        amount_usd=amount_usd,
    )


def _select_model(
    *,
    task_hint: TaskHint,
    prefer_premium: bool,
    estimated_in: int,
    estimated_out: int,
    max_cost_usd: float,
) -> tuple[str, float]:
    """Return (chosen_model, estimated_cost_usd) honoring the budget cap.

    Walks the candidate list cheapest-first when the preferred choice would
    exceed ``max_cost_usd``. Raises ``RouteBudgetError`` if no candidate fits.
    """
    candidates = candidate_models(task_hint=task_hint, prefer_premium=prefer_premium)
    # First pass: try the preferred choice.
    preferred = candidates[0]
    pref_cost = estimate_cost_usd(preferred, tokens_in=estimated_in, tokens_out=estimated_out)
    if pref_cost <= max_cost_usd:
        return preferred, pref_cost

    # Downshift: find the cheapest candidate that fits the budget.
    by_cost = sorted(
        (
            (m, estimate_cost_usd(m, tokens_in=estimated_in, tokens_out=estimated_out))
            for m in candidates
        ),
        key=lambda kv: kv[1],
    )
    for model, cost in by_cost:
        if cost <= max_cost_usd:
            return model, cost
    cheapest_model, cheapest_cost = by_cost[0]
    raise RouteBudgetError(
        f"all candidates exceed max_cost_usd={max_cost_usd}: "
        f"cheapest is {cheapest_model} at ${cheapest_cost:.4f}"
    )


def _premium_equivalent(task_hint: TaskHint) -> str:
    """The premium-tier model for a given task_hint, used for savings calc."""
    return pick_model(task_hint=task_hint, prefer_premium=True)


class _CallOutcome:
    """Internal carrier for a single LLM call's results.

    Plain class (not dataclass / Pydantic) so monkeypatch-based tests can
    construct arbitrary subclasses or mocks without buying into a schema.
    """

    __slots__ = (
        "model_used",
        "text",
        "tokens_in",
        "tokens_out",
        "upstream_cost_usd",
        "usage_cost_usd",
    )

    def __init__(
        self,
        *,
        text: str,
        tokens_in: int,
        tokens_out: int,
        usage_cost_usd: float | None,
        upstream_cost_usd: float | None,
        model_used: str,
    ) -> None:
        self.text = text
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.usage_cost_usd = usage_cost_usd
        self.upstream_cost_usd = upstream_cost_usd
        self.model_used = model_used


# Status codes / exception types that trigger a single fallback attempt
# (S4-ROUTE-03). We retry once and only once — cost discipline.
_FALLBACK_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503})


def _is_retryable_error(exc: BaseException) -> bool:
    """True if `exc` indicates a transient upstream failure worth one retry."""
    try:
        import httpx
    except Exception:  # pragma: no cover — httpx is a hard dep
        httpx = None  # type: ignore[assignment]

    if httpx is not None and isinstance(exc, httpx.RemoteProtocolError):
        return True

    # OpenAI SDK raises APIStatusError subclasses with `.status_code`. Avoid
    # importing the symbol so the fallback path doesn't bind a tight version.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _FALLBACK_STATUS_CODES:
        return True
    # Some SDK variants expose status via .response.status_code instead.
    response = getattr(exc, "response", None)
    if response is not None:
        rstatus = getattr(response, "status_code", None)
        if isinstance(rstatus, int) and rstatus in _FALLBACK_STATUS_CODES:
            return True
    return False


def _fallback_for(model: str, *, task_hint: TaskHint, prefer_premium: bool) -> str | None:
    """Return the next-best candidate model for `model` in the same tier.

    v1 implementation walks the existing routing matrix's candidate list and
    returns the first entry that isn't ``model`` itself. Returns None when no
    alternative exists. Lives here (not in matrix.py) because the catalog has
    its own richer fallback metadata that a Sprint-5 refactor will surface.
    """
    candidates = candidate_models(task_hint=task_hint, prefer_premium=prefer_premium)
    for candidate in candidates:
        if candidate != model:
            return candidate
    return None


def _extract_usage_cost(usage: object) -> tuple[float | None, float | None]:
    """Pull `(usage.cost, usage.cost_details.upstream_inference_cost)` if present.

    Returns ``(None, None)`` when the upstream didn't include those fields
    (direct OpenAI API, mocked transports without cost surfacing). Tolerates
    Pydantic, dataclass, or plain-attribute usage objects.
    """
    if usage is None:
        return None, None

    def _get(obj: object, attr: str) -> object | None:
        return getattr(obj, attr, None) if not isinstance(obj, dict) else obj.get(attr)

    raw_cost = _get(usage, "cost")
    cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else None

    details = _get(usage, "cost_details")
    upstream: float | None = None
    if details is not None:
        raw_upstream = _get(details, "upstream_inference_cost")
        if isinstance(raw_upstream, (int, float)):
            upstream = float(raw_upstream)
    return cost, upstream


async def _call_model(
    *,
    model: str,
    prompt: str,
    fallback_model: str | None = None,
) -> _CallOutcome:
    """Call the chosen model via the existing OpenAI-compatible client.

    When ``fallback_model`` is provided we wire OpenRouter's server-side
    fallback chain via ``extra_body={"models": [primary, fallback]}``. The
    upstream surfaces the model that actually answered as ``response.model``,
    which we surface as ``model_used`` (S4-ROUTE-03).

    Reuses the AG2 router config so this honors LLM_ROUTER (openai |
    openrouter | clawrouter) the same way the Pro debate does.
    """
    # Lazy import — avoid pulling the OpenAI SDK into modules that just want
    # routing decisions (matrix tests, CLI argument parsing).
    #
    # 2026-05-05 — built via build_async_client (explicit timeouts) and
    # streamed via stream_chat_completion (OpenRouter SSE keep-alives) so
    # the routed call inherits the same hardening as the basic / pro paths.
    from gecko_core.orchestration.llm_client import (
        LLMStalledError,
        LLMTruncationError,
        build_async_client,
        stream_chat_completion,
    )
    from gecko_core.orchestration.pro.router import resolve_router

    cfg = resolve_router()
    client = build_async_client(
        LLMClientConfig(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            extra_headers=dict(cfg.extra_headers),
            chat_model=model,
            temperature=0.3,
            max_input_tokens=0,
            source=f"router:{cfg.router}",
        )
    )
    extra_body: dict[str, object] | None = None
    if fallback_model:
        extra_body = {"models": [model, fallback_model]}

    try:
        completion = await stream_chat_completion(
            client,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            extra_body=extra_body,
        )
    except (LLMTruncationError, LLMStalledError):
        # `route()` already retries once with the fallback model; surface
        # truncation/stall as transient failures upstream.
        raise

    text = completion.content
    tokens_in = int(completion.prompt_tokens or 0)
    tokens_out = int(completion.completion_tokens or 0)
    usage_cost = completion.usage_cost_usd
    upstream_cost = completion.upstream_cost_usd
    model_used = completion.model
    return _CallOutcome(
        text=text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        usage_cost_usd=usage_cost,
        upstream_cost_usd=upstream_cost,
        model_used=model_used,
    )


def _check_cost_drift(*, model: str, estimate: float, truth: float | None) -> None:
    """Warn at WARNING level when the catalog estimate diverges from billed truth.

    Catches stale catalog prices before they surprise users. Threshold is 10% —
    below that we tolerate float / partial-token noise. Skipped when ``truth``
    is None (direct provider didn't report `usage.cost`).
    """
    if truth is None:
        return
    if estimate <= 0.0:
        return
    delta = abs(truth - estimate) / estimate
    if delta > 0.10:
        pct = delta * 100.0
        logger.warning(
            "cost drift on %s: estimate=%.6f truth=%.6f delta=%.1f%%",
            model,
            estimate,
            truth,
            pct,
        )


def _emit_demo_log(result: RouteResult, task_hint: TaskHint) -> None:
    """One-line demo log on stdout when invoked from the CLI.

    Gated on ``GECKO_ROUTE_LOG=1`` (the CLI sets this). Importing modules
    pay nothing for this — the env check is cheap.
    """
    if os.environ.get("GECKO_ROUTE_LOG") != "1":
        return
    premium = _premium_equivalent(task_hint)
    line = (
        f"[gecko_route] task={task_hint} -> {result.model_used} - "
        f"${result.cost_usd:.4f} (saved ${result.savings_vs_premium:.4f} vs {premium})"
    )
    print(line, file=sys.stdout, flush=True)


async def route(
    prompt: str,
    task_hint: TaskHint = DEFAULT_TASK_HINT,
    max_cost_usd: float = 0.05,
    prefer_premium: bool = False,
) -> RouteResult:
    """Route an LLM call through Gecko's cost-aware router.

    Args:
        prompt: The user prompt. Token-counted to bound spend before calling.
        task_hint: Bias the matrix toward a model class.
        max_cost_usd: Per-call hard cap. Downshifts; raises if even the
            cheapest fit exceeds the cap.
        prefer_premium: If True, take the premium column of the matrix as
            the preferred choice.

    Raises:
        RouteBudgetError: ``max_cost_usd`` cannot be honored by any candidate.
        RoutePaymentError: x402 charge failed (no retry — fail loudly).
    """
    estimated_in = estimate_tokens(prompt)
    # Conservative output estimate: cap at 1024 tokens unless the prompt
    # itself is enormous; matches the typical Pro-debate per-turn ceiling.
    estimated_out = min(1024, max(256, estimated_in // 2))

    chosen, estimated_cost = _select_model(
        task_hint=task_hint,
        prefer_premium=prefer_premium,
        estimated_in=estimated_in,
        estimated_out=estimated_out,
        max_cost_usd=max_cost_usd,
    )

    # Charge before the model call. The Pro tier follows the same rule —
    # persist-before-work means a crash mid-LLM-call doesn't lose the intent.
    intent = _build_intent(Decimal(str(round(estimated_cost, 6))))
    client = get_client()
    payment = await client.charge(intent)
    if payment.status != "success":
        raise RoutePaymentError(
            f"x402 charge failed for intent {intent.intent_id}: {payment.error or 'unknown'}"
        )

    # S4-ROUTE-03: wire OpenRouter's server-side fallback chain. We retry once
    # locally on transient failures (429 / 5xx / RemoteProtocolError) using the
    # next-best candidate. OpenRouter handles in-call fallback via extra_body
    # transparently; the local retry guards against full-call failures the
    # upstream couldn't recover from on its own.
    fallback = _fallback_for(chosen, task_hint=task_hint, prefer_premium=prefer_premium)
    try:
        outcome = await _call_model(model=chosen, prompt=prompt, fallback_model=fallback)
    except Exception as exc:
        if not _is_retryable_error(exc) or fallback is None:
            raise
        logger.warning(
            "primary model %s failed (%s); retrying with fallback %s",
            chosen,
            type(exc).__name__,
            fallback,
        )
        # ONE fallback per call — pass no fallback_model on the retry so we
        # don't accidentally chain a third attempt server-side.
        outcome = await _call_model(model=fallback, prompt=prompt, fallback_model=None)

    tokens_in = outcome.tokens_in
    tokens_out = outcome.tokens_out
    text = outcome.text
    # Recompute actual cost from real token usage (estimate was for budget
    # gating; the catalog-based cost surfaced to the caller should reflect
    # whatever model actually answered).
    actual_model = outcome.model_used
    try:
        actual_cost = estimate_cost_usd(actual_model, tokens_in=tokens_in, tokens_out=tokens_out)
    except KeyError:
        # Fallback model not in our catalog (rare — OpenRouter routed
        # somewhere unexpected). Use the requested model's price as the
        # next-best estimate; the OpenRouter-billed truth still surfaces
        # via usage_cost_usd.
        actual_cost = estimate_cost_usd(chosen, tokens_in=tokens_in, tokens_out=tokens_out)
    premium = _premium_equivalent(task_hint)
    premium_cost = estimate_cost_usd(premium, tokens_in=tokens_in, tokens_out=tokens_out)
    savings = max(0.0, premium_cost - actual_cost)

    # S4-ROUTE-01: emit a WARNING when our catalog estimate drifts from the
    # OpenRouter-billed truth by more than 10%. Catches stale pricing before
    # users notice the divergence.
    _check_cost_drift(model=actual_model, estimate=actual_cost, truth=outcome.usage_cost_usd)

    result = RouteResult(
        response=text,
        model_used=actual_model,
        model_requested=chosen,
        cost_usd=actual_cost,
        usage_cost_usd=outcome.usage_cost_usd,
        upstream_cost_usd=outcome.upstream_cost_usd,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        savings_vs_premium=savings,
    )
    _emit_demo_log(result, task_hint)
    return result


__all__ = [
    "RouteBudgetError",
    "RoutePaymentError",
    "RouteResult",
    "TaskHint",
    "candidate_models",
    "estimate_cost_usd",
    "estimate_tokens",
    "pick_model",
    "price_for",
    "route",
]
