"""Regression test for the LLM-hygiene Commit A bug.

Before the fix, ``workflows.research()`` (Pro tier) called the legacy
``model_matrix(cfg.router)`` from ``pro/router.py`` which returned a hardcoded
``{analyst: gpt-4o-mini, ..., judge: gpt-4o}`` map regardless of the user's
``tier_preset``. The catalog branch in ``pro/__init__.py:222`` is gated on
``model_matrix is None``, so the catalog never fired for the AG2 debate.

This test asserts the new contract: when ``tier_preset`` is set and no
explicit ``model_matrix`` is forwarded, ``pro.generate`` resolves the
per-agent matrix from ``model_matrix_for_tier(router, Tier(tier_preset))``,
not the deleted legacy gpt-4o-mini map.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_tier_preset_resolves_catalog_matrix_when_model_matrix_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalog branch fires when ``model_matrix=None`` and ``tier_preset`` is set."""
    captured_kwargs: dict[str, Any] = {}

    class _FakeAgent:
        name = "analyst"

        def __init__(self, **kwargs: Any) -> None:
            self.name = kwargs["name"]

        async def a_generate_reply(self, *args: Any, **kwargs: Any) -> str:
            return "stub"

    class _FakeGroupChat:
        def __init__(self, **kwargs: Any) -> None:
            self.agents: list[_FakeAgent] = []
            self.messages: list[dict[str, Any]] = []

    class _FakeMgr:
        def __init__(self, **kwargs: Any) -> None:
            self.groupchat = _FakeGroupChat()

    def _fake_build_groupchat(_cfg: object, **kwargs: Any) -> _FakeMgr:
        captured_kwargs.update(kwargs)
        return _FakeMgr()

    # Force LLM_ROUTER=openai so the catalog matrix resolves with provider
    # filtering (deterministic across CI envs).
    monkeypatch.setenv("LLM_ROUTER", "openai")

    from gecko_core.orchestration.pro import generate as pro_generate

    # build_groupchat is called inside generate; it'll raise before we get to
    # turn execution because the FakeMgr.groupchat has no agents. We just
    # need to confirm the model_matrix kwarg got resolved from the catalog.
    # Down-stream debate execution explodes against the fake — that's fine;
    # we only care about the kwargs build_groupchat was called with.
    with (
        patch(
            "gecko_core.orchestration.pro.build_groupchat",
            side_effect=_fake_build_groupchat,
        ),
        contextlib.suppress(Exception),
    ):
        await pro_generate(
            idea="test",
            rag_context="",
            llm_config={"config_list": [{"model": "x"}]},
            tier_preset="balanced",
            # critically: no model_matrix=
        )

    matrix = captured_kwargs.get("model_matrix")
    assert matrix is not None, "tier_preset should have triggered catalog resolution"

    from gecko_core.orchestration.pro.router import model_matrix_for_tier
    from gecko_core.routing.catalog import Tier

    expected = model_matrix_for_tier("openai", Tier.balanced)
    assert matrix == expected, (
        f"AG2 debate matrix drifted from catalog; got={matrix!r} expected={expected!r}"
    )

    # Sanity: the legacy gpt-4o-mini map is GONE (the bug we're guarding against).
    legacy_mini_map = {
        "analyst": "gpt-4o-mini",
        "critic": "gpt-4o-mini",
        "architect": "gpt-4o-mini",
        "scoper": "gpt-4o-mini",
        "judge": "gpt-4o",
    }
    assert matrix != legacy_mini_map, (
        "Resolved matrix matches the deleted legacy _MATRIX — tier_preset is being ignored again."
    )


def test_legacy_model_matrix_symbol_is_gone() -> None:
    """The legacy ``model_matrix(router)`` and ``model_for_agent(...)`` were
    removed in Commit A. Verify they don't sneak back in via re-export."""
    from gecko_core.orchestration.pro import router as pro_router

    assert not hasattr(pro_router, "model_matrix"), (
        "model_matrix(router) was deleted; do not re-add it. "
        "Use model_matrix_for_tier(router, tier) instead."
    )
    assert not hasattr(pro_router, "model_for_agent"), (
        "model_for_agent(name, router) was deleted; do not re-add it. "
        "Resolve via model_matrix_for_tier instead."
    )
