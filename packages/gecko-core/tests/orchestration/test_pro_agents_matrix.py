"""Unit tests for the per-agent model matrix (S1-02)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch


def test_matrix_openai_has_expected_models() -> None:
    from gecko_core.orchestration.pro.router import model_for_agent, model_matrix

    assert model_for_agent("analyst", "openai") == "gpt-4o-mini"
    assert model_for_agent("critic", "openai") == "gpt-4o-mini"
    assert model_for_agent("scoper", "openai") == "gpt-4o-mini"
    assert model_for_agent("judge", "openai") == "gpt-4o"

    matrix = model_matrix("openai")
    # judge is the only larger-model agent in the openai matrix today.
    larger = {k for k, v in matrix.items() if v == "gpt-4o"}
    assert larger == {"judge"}


def test_matrix_openrouter_prefixes_models() -> None:
    from gecko_core.orchestration.pro.router import model_matrix

    matrix = model_matrix("openrouter")
    for v in matrix.values():
        assert v.startswith("openai/")
    assert matrix["analyst"] == "openai/gpt-4o-mini"
    assert matrix["judge"] == "openai/gpt-4o"


def test_matrix_clawrouter_uses_blockrun_auto() -> None:
    from gecko_core.orchestration.pro.router import model_matrix

    matrix = model_matrix("clawrouter")
    assert all(v == "blockrun/auto" for v in matrix.values())


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
