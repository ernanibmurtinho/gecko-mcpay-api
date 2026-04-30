"""LLM router selection + per-agent model matrix for the Pro debate.

Three routers are supported (selected via `LLM_ROUTER` env):

- ``openai``      — direct api.openai.com (production default)
- ``openrouter``  — https://openrouter.ai/api/v1 (multi-provider passthrough)
- ``clawrouter``  — local x402-paid proxy on http://localhost:8402/v1

The same agent can run on different model strings depending on the router —
OpenAI uses ``gpt-4o-mini``; OpenRouter prefixes with ``openai/``; ClawRouter
uses its own ``blockrun/auto`` route. The matrix lives here so AG2 agent
construction (`agents.build_groupchat`) can keep one source of truth.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from gecko_core.routing.catalog import (
    AgentRole,
    Tier,
    filter_by_provider,
    load_catalog,
    lookup_model,
    task_for_role,
)
from gecko_core.routing.costs import MODEL_PRICING


def _price_per_1k_for(model: str) -> list[float] | None:
    """Return AG2-style ``[input_per_1k, output_per_1k]`` for ``model`` or None.

    Strips known router prefixes (``openai/``, ``anthropic/``) before lookup
    so OpenRouter-style names resolve to the same price as the bare model
    name. ``MODEL_PRICING`` stores USD per 1M tokens; AG2 expects USD per 1K.
    """
    # Try as-is first (catalog ids and legacy bare names both live in
    # MODEL_PRICING after the S4-MATRIX-01 catalog wiring).
    entry = MODEL_PRICING.get(model)
    if entry is None:
        bare = model
        for prefix in ("openai/", "anthropic/", "claude/"):
            if bare.startswith(prefix):
                bare = bare[len(prefix) :]
                break
        entry = MODEL_PRICING.get(bare)
    if entry is None:
        return None
    return [entry["input_per_m"] / 1000.0, entry["output_per_m"] / 1000.0]


Router = Literal["openai", "openrouter", "clawrouter"]
_VALID_ROUTERS: frozenset[str] = frozenset(("openai", "openrouter", "clawrouter"))

# Per-agent model matrix per router.
#
# Cheap models for analyst/proponent/critic/scoper today; larger model for
# judge so verdict calibration doesn't drift on the small model. We promote
# proponent/critic to the larger model later based on eval-harness signal
# (S1-06 — out of scope for this dispatch).
#
# Note on agent names: the production debate uses agent names
# ``analyst, critic, architect, scoper, judge``. We expose the alias
# ``proponent`` -> architect's slot in the matrix so callers using the
# roadmap vocabulary (proponent vs. critic adversarial pair) get the right
# model without renaming the AG2 agent.
_MATRIX: dict[str, dict[str, str]] = {
    "openai": {
        "analyst": "gpt-4o-mini",
        "proponent": "gpt-4o-mini",
        "architect": "gpt-4o-mini",
        "critic": "gpt-4o-mini",
        "scoper": "gpt-4o-mini",
        "judge": "gpt-4o",
    },
    "openrouter": {
        "analyst": "openai/gpt-4o-mini",
        "proponent": "openai/gpt-4o-mini",
        "architect": "openai/gpt-4o-mini",
        "critic": "openai/gpt-4o-mini",
        "scoper": "openai/gpt-4o-mini",
        "judge": "openai/gpt-4o",
    },
    "clawrouter": {
        "analyst": "blockrun/auto",
        "proponent": "blockrun/auto",
        "architect": "blockrun/auto",
        "critic": "blockrun/auto",
        "scoper": "blockrun/auto",
        "judge": "blockrun/auto",
    },
}


@dataclass(frozen=True)
class RouterConfig:
    """Resolved LLM-routing surface used by AG2.

    ``base_url`` and ``api_key`` feed the OpenAI-compatible client; ``extra_headers``
    are required by OpenRouter for usage-attribution analytics. ``tier_preset``
    selects the user-facing cost/quality preset that drives per-agent model
    selection via the curated catalog.
    """

    router: Router
    base_url: str
    api_key: str
    extra_headers: dict[str, str]
    tier_preset: Tier = Tier.balanced

    def llm_config_for_model(self, model: str, *, temperature: float = 0.3) -> dict[str, Any]:
        """Build a single-config-list AG2 ``llm_config`` for one model string."""
        entry: dict[str, Any] = {
            "model": model,
            "api_key": self.api_key,
            "base_url": self.base_url,
        }
        if self.extra_headers:
            # AG2's openai client adapter forwards default_headers into the
            # underlying httpx client.
            entry["default_headers"] = dict(self.extra_headers)
        # AG2's built-in price table doesn't know router-prefixed names like
        # ``openai/gpt-4o-mini`` (OpenRouter format) and silently reports
        # cost as 0 with a WARNING. Inject explicit price [input, output]
        # per 1K tokens so cost attribution stays accurate regardless of
        # router naming.
        price = _price_per_1k_for(model)
        if price is not None:
            entry["price"] = price
        return {"config_list": [entry], "temperature": temperature}


def resolve_router(
    environ: dict[str, str] | None = None,
    *,
    tier_preset: Tier | str = Tier.balanced,
) -> RouterConfig:
    """Resolve LLM routing from the environment.

    ``tier_preset`` flows through to per-agent catalog lookup (see
    ``model_matrix_for_tier``). It is orthogonal to ``LLM_ROUTER``: the router
    controls the BASE URL + auth, the tier controls the MODEL string.

    Raises:
        ValueError: ``LLM_ROUTER`` value is unknown, or the required API key
            for the selected router is not set.
    """
    env = environ if environ is not None else dict(os.environ)
    router = (env.get("LLM_ROUTER") or "openai").strip().lower()
    tier = Tier(tier_preset) if not isinstance(tier_preset, Tier) else tier_preset
    if router not in _VALID_ROUTERS:
        raise ValueError(
            f"LLM_ROUTER={router!r} is not one of {sorted(_VALID_ROUTERS)}; "
            "set LLM_ROUTER=openai|openrouter|clawrouter"
        )

    if router == "openai":
        api_key = env.get("OPENAI_API_KEY") or ""
        if not api_key:
            raise ValueError(
                "LLM_ROUTER=openai requires OPENAI_API_KEY to be set in the environment"
            )
        return RouterConfig(
            router="openai",
            base_url="https://api.openai.com/v1",
            api_key=api_key,
            extra_headers={},
            tier_preset=tier,
        )

    if router == "openrouter":
        api_key = env.get("OPENROUTER_API_KEY") or ""
        # Treat known SSM-placeholder sentinels as unset. The push-ssm script
        # writes "__unset__" so the ECS task definition can resolve the secret
        # ARN even when the operator hasn't provisioned a real key yet.
        if api_key in ("", "__unset__", "__dev_change_me__"):
            # Fail fast at startup, not at first agent reply.
            raise ValueError(
                "LLM_ROUTER=openrouter requires OPENROUTER_API_KEY to be set in the environment"
            )
        # OpenRouter requires (or strongly prefers) referrer + title for
        # provider attribution / leaderboards.
        headers = {
            "HTTP-Referer": env.get("OPENROUTER_REFERER", "https://app.geckovision.tech"),
            "X-Title": env.get("OPENROUTER_TITLE", "Gecko"),
        }
        return RouterConfig(
            router="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            extra_headers=headers,
            tier_preset=tier,
        )

    # clawrouter — no auth, listens on localhost by default.
    return RouterConfig(
        router="clawrouter",
        base_url=env.get("CLAWROUTER_URL", "http://localhost:8402/v1"),
        api_key="noop",
        extra_headers={},
        tier_preset=tier,
    )


def model_for_agent(agent_name: str, router: Router) -> str:
    """Look up the model string for ``(agent, router)``.

    Falls back to ``analyst``'s slot when ``agent_name`` is unknown — the
    matrix is exhaustive for the production agents but stays permissive so
    a future agent rename doesn't crash at runtime.
    """
    matrix = _MATRIX[router]
    return matrix.get(agent_name) or matrix["analyst"]


def model_matrix(router: Router) -> dict[str, str]:
    """Return a copy of the per-agent model map for ``router``."""
    return dict(_MATRIX[router])


# Pro-debate agent names that participate in the catalog-driven matrix. The
# legacy ``_MATRIX`` includes ``proponent`` as an alias for architect — the
# catalog matrix doesn't (the AgentRole enum uses one canonical name per
# slot).
_PRO_DEBATE_ROLES: tuple[AgentRole, ...] = (
    AgentRole.analyst,
    AgentRole.critic,
    AgentRole.architect,
    AgentRole.scoper,
    AgentRole.judge,
)


# Providers reachable directly via OpenAI's API. Other providers must go
# through OpenRouter (or ClawRouter). When ``LLM_ROUTER=openai`` and the
# catalog matrix would pick a non-OpenAI model, we substitute the OpenAI
# fallback for that role rather than 404 at the API.
_OPENAI_FALLBACK_BY_TIER: dict[Tier, str] = {
    Tier.quality: "openai/gpt-5.5",
    Tier.balanced: "openai/gpt-5-mini",
    Tier.budget: "openai/gpt-4.1-nano",
    Tier.free: "openai/gpt-4.1-nano",  # no free OpenAI model — degrade to nano
}


def model_matrix_for_tier(router: Router, tier: Tier) -> dict[str, str]:
    """Catalog-driven per-agent model matrix for the Pro debate.

    Returns a ``{agent_name: model_id}`` map covering the five Pro-debate
    roles plus the ``proponent`` alias (mirrored to ``architect``'s id so
    callers using the roadmap vocabulary still get a valid model).

    Provider filtering: when ``router == "openai"`` the catalog pick is
    substituted with an OpenAI-only fallback for any role whose curated model
    isn't reachable from api.openai.com. Catalog-driven multi-provider
    matrices are OpenRouter-gated.
    """
    catalog = load_catalog()
    openai_only = router == "openai"
    if openai_only:
        catalog = filter_by_provider(catalog, {"openai"})

    matrix: dict[str, str] = {}
    for role in _PRO_DEBATE_ROLES:
        task = task_for_role(role)
        try:
            entry = lookup_model(task, tier)
        except Exception:
            entry = None
        # If lookup succeeded but provider filtering kicked it out, fall back.
        if entry is None or (openai_only and entry.id not in catalog):
            matrix[role.value] = (
                _OPENAI_FALLBACK_BY_TIER[tier]
                if openai_only
                # Cross-provider catalog miss is unusual — bail to a sane default.
                else ("openai/gpt-4.1-nano")
            )
        else:
            matrix[role.value] = entry.id
    # Alias proponent -> architect for legacy callers using the roadmap name.
    matrix["proponent"] = matrix["architect"]
    return matrix


__all__ = [
    "Router",
    "RouterConfig",
    "Tier",
    "model_for_agent",
    "model_matrix",
    "model_matrix_for_tier",
    "resolve_router",
]
