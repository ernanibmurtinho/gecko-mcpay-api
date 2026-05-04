"""Unit tests for the per-agent model matrix.

The legacy ``_MATRIX`` / ``model_matrix(router)`` / ``model_for_agent`` surface
was removed in the LLM-hygiene Commit A — it was a hardcoded gpt-4o-mini map
that ignored ``tier_preset`` entirely. The catalog-driven
``model_matrix_for_tier(router, tier)`` is now the single source of truth.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch


def test_matrix_for_tier_balanced_openai_substitutes_fallback() -> None:
    """LLM_ROUTER=openai forces an OpenAI-only fallback when the catalog
    pick is non-OpenAI (e.g. balanced/general_reasoning -> Kimi K2.6)."""
    from gecko_core.orchestration.pro.router import model_matrix_for_tier
    from gecko_core.routing.catalog import Tier

    matrix = model_matrix_for_tier("openai", Tier.balanced)
    # All five Pro-debate roles plus the proponent alias resolve to a model id.
    for role in ("analyst", "critic", "architect", "scoper", "judge", "proponent"):
        assert role in matrix, f"missing role {role!r}"
    # OpenAI router can't reach Anthropic/Moonshot/etc., so every entry must
    # be an openai/* model.
    for v in matrix.values():
        assert v.startswith("openai/"), f"openai router leaked non-openai model: {v!r}"


def test_matrix_for_tier_balanced_openrouter_uses_catalog() -> None:
    """OpenRouter can reach any provider; the catalog picks should land
    intact (no fallback substitution)."""
    from gecko_core.orchestration.pro.router import model_matrix_for_tier
    from gecko_core.routing.catalog import AgentRole, Tier, lookup_model, task_for_role

    matrix = model_matrix_for_tier("openrouter", Tier.balanced)
    # Each role's resolved model id matches the catalog cell directly.
    for role_name in ("analyst", "critic", "architect", "scoper", "judge"):
        role = AgentRole(role_name)
        expected = lookup_model(task_for_role(role), Tier.balanced).id
        assert matrix[role_name] == expected, (
            f"{role_name}: catalog={expected!r} vs resolved={matrix[role_name]!r}"
        )


def test_build_groupchat_overrides_per_agent_model() -> None:
    """Each agent's llm_config gets its own ``model`` from the matrix.

    AG2's `ConversableAgent` is patched out so the test doesn't require any
    network or autogen runtime — we capture the kwargs it was called with.
    """
    captured: list[dict[str, Any]] = []

    class _FakeAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured.append(kwargs)
            self.name = kwargs["name"]

    class _FakeGroupChat:
        def __init__(self, **kwargs: Any) -> None:
            self.agents = kwargs["agents"]

    class _FakeMgr:
        def __init__(self, **kwargs: Any) -> None:
            self.groupchat = kwargs["groupchat"]

    fake_autogen = type(
        "fake_autogen",
        (),
        {
            "ConversableAgent": _FakeAgent,
            "GroupChat": _FakeGroupChat,
            "GroupChatManager": _FakeMgr,
        },
    )

    base_cfg: dict[str, Any] = {
        "config_list": [{"model": "PLACEHOLDER", "api_key": "k", "base_url": "https://x/v1"}],
        "temperature": 0.3,
    }
    matrix = {
        "analyst": "gpt-4o-mini",
        "critic": "gpt-4o-mini",
        "architect": "gpt-4o-mini",
        "scoper": "gpt-4o-mini",
        "judge": "gpt-4o",
    }

    import sys

    with patch.dict(sys.modules, {"autogen": fake_autogen}):
        from gecko_core.orchestration.pro.agents import build_groupchat

        build_groupchat(base_cfg, model_matrix=matrix)

    by_name = {kw["name"]: kw for kw in captured}
    assert by_name["analyst"]["llm_config"]["config_list"][0]["model"] == "gpt-4o-mini"
    assert by_name["judge"]["llm_config"]["config_list"][0]["model"] == "gpt-4o"
    # Sanity: per-agent llm_configs are independent dicts (no aliasing).
    by_name["analyst"]["llm_config"]["config_list"][0]["model"] = "MUTATED"
    assert by_name["judge"]["llm_config"]["config_list"][0]["model"] == "gpt-4o", (
        "per-agent llm_configs must not share state"
    )
    # Base config_list[0] was not mutated either.
    assert base_cfg["config_list"][0]["model"] == "PLACEHOLDER"


def test_build_groupchat_without_matrix_uses_base_config() -> None:
    """Backward compat: legacy callers pass no matrix and get the old behavior."""
    captured: list[dict[str, Any]] = []

    class _FakeAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured.append(kwargs)

    class _FakeGroupChat:
        def __init__(self, **kwargs: Any) -> None:
            self.agents = kwargs["agents"]

    class _FakeMgr:
        def __init__(self, **kwargs: Any) -> None:
            self.groupchat = kwargs["groupchat"]

    fake_autogen = type(
        "fake_autogen",
        (),
        {
            "ConversableAgent": _FakeAgent,
            "GroupChat": _FakeGroupChat,
            "GroupChatManager": _FakeMgr,
        },
    )
    base_cfg: dict[str, Any] = {
        "config_list": [{"model": "gpt-4o-mini", "api_key": "k", "base_url": "u"}]
    }
    import sys

    with patch.dict(sys.modules, {"autogen": fake_autogen}):
        from gecko_core.orchestration.pro.agents import build_groupchat

        build_groupchat(base_cfg)
    # All five agents share the base config's model.
    for kw in captured:
        assert kw["llm_config"]["config_list"][0]["model"] == "gpt-4o-mini"
