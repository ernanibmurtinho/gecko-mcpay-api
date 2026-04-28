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
    are required by OpenRouter for usage-attribution analytics.
    """

    router: Router
    base_url: str
    api_key: str
    extra_headers: dict[str, str]

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
        return {"config_list": [entry], "temperature": temperature}


def resolve_router(environ: dict[str, str] | None = None) -> RouterConfig:
    """Resolve LLM routing from the environment.

    Raises:
        ValueError: ``LLM_ROUTER`` value is unknown, or the required API key
            for the selected router is not set.
    """
    env = environ if environ is not None else dict(os.environ)
    router = (env.get("LLM_ROUTER") or "openai").strip().lower()
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
        )

    # clawrouter — no auth, listens on localhost by default.
    return RouterConfig(
        router="clawrouter",
        base_url=env.get("CLAWROUTER_URL", "http://localhost:8402/v1"),
        api_key="noop",
        extra_headers={},
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


__all__ = [
    "Router",
    "RouterConfig",
    "model_for_agent",
    "model_matrix",
    "resolve_router",
]
