"""Tests for the curated model catalog (S4-MATRIX-01)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gecko_core.routing.catalog import (
    AgentRole,
    CatalogError,
    ModelEntry,
    TaskProfile,
    Tier,
    all_models,
    load_catalog,
    lookup_model,
    models_for_role,
)


def test_catalog_loads_at_least_15_models() -> None:
    catalog = load_catalog()
    assert len(catalog) >= 15, f"expected at least 15 models, got {len(catalog)}"


def test_catalog_contains_expected_marquee_ids() -> None:
    catalog = load_catalog()
    for model_id in (
        "anthropic/claude-opus-4.7",
        "anthropic/claude-sonnet-4.6",
        "moonshotai/kimi-k2.6",
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
        "openai/gpt-5.5",
        "openai/gpt-4.1-nano",
        "google/gemini-2.5-flash-lite",
    ):
        assert model_id in catalog, f"missing {model_id} from catalog"


def test_lookup_complex_coding_quality_returns_opus() -> None:
    m = lookup_model(TaskProfile.complex_coding, Tier.quality)
    assert m.id == "anthropic/claude-opus-4.7"
    assert m.name == "Claude Opus 4.7"


def test_lookup_complex_coding_balanced_returns_kimi() -> None:
    # complex_coding×balanced still maps to Kimi K2.6: this is an AG2 plain-text
    # voice (architect), not a json_object call site — safe to leave.
    m = lookup_model(TaskProfile.complex_coding, Tier.balanced)
    assert m.id == "moonshotai/kimi-k2.6"
    assert m.score == 84


def test_lookup_file_navigation_budget_returns_deepseek_v4_flash() -> None:
    m = lookup_model(TaskProfile.file_navigation, Tier.budget)
    assert m.id == "deepseek/deepseek-v4-flash"


def test_lookup_complex_coding_free_returns_deepseek_v4_flash() -> None:
    # S8-CATALOG-01: Poolside Laguna M.1 was delisted; the cheapest live
    # coding-capable substitute is DeepSeek V4 Flash.
    m = lookup_model(TaskProfile.complex_coding, Tier.free)
    assert m.id == "deepseek/deepseek-v4-flash"


def test_models_for_role_architect_returns_all_four_tiers() -> None:
    out = models_for_role(AgentRole.architect)
    assert set(out) == {Tier.quality, Tier.balanced, Tier.budget, Tier.free}
    assert all(isinstance(v, ModelEntry) for v in out.values())


def test_models_for_role_ceo_returns_all_four_tiers() -> None:
    out = models_for_role(AgentRole.ceo)
    assert set(out) == {Tier.quality, Tier.balanced, Tier.budget, Tier.free}
    # CEO maps to planning, so quality tier should be Opus per the matrix.
    assert out[Tier.quality].id == "anthropic/claude-opus-4.7"


def test_all_models_returns_full_catalog_sorted() -> None:
    models = all_models()
    ids = [m.id for m in models]
    assert ids == sorted(ids)
    assert len(models) >= 15


def test_role_to_task_matrix_covers_all_ten_roles() -> None:
    # Every AgentRole must have a primary task profile.
    for role in AgentRole:
        out = models_for_role(role)
        assert len(out) == 4


def test_malformed_catalog_raises_clearly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A catalog missing the required `pricing` field on a model must raise."""
    bad_catalog = {
        "metadata": {"version": "test"},
        "models": {
            "broken-model": {
                "id": "broken/x",
                "name": "Broken",
                "provider": "test",
                # NO pricing field
                "context_window": 1000,
                "score": 50,
                "score_per_dollar": 1.0,
                "tier": "budget",
            }
        },
    }
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad_catalog), encoding="utf-8")

    # Patch the module-level path constant + bust the lru_cache.
    from gecko_core.routing import catalog as catalog_mod

    monkeypatch.setattr(catalog_mod, "_CATALOG_PATH", bad_path)
    catalog_mod.load_catalog.cache_clear()

    with pytest.raises(CatalogError, match="pricing"):
        load_catalog()

    # Restore so subsequent tests in this session reload the real catalog.
    catalog_mod.load_catalog.cache_clear()


def test_60_cell_matrix_is_fully_populated() -> None:
    """All 15 task profiles × 4 tiers = 60 cells must resolve to a valid model."""
    from gecko_core.routing.catalog import _TASK_TIER_TO_MODEL_ID

    expected = {(t, tier) for t in TaskProfile for tier in Tier}
    assert set(_TASK_TIER_TO_MODEL_ID.keys()) == expected
    assert len(_TASK_TIER_TO_MODEL_ID) == 60

    catalog = load_catalog()
    for (task, tier), model_id in _TASK_TIER_TO_MODEL_ID.items():
        assert model_id in catalog, (
            f"matrix references {model_id} for ({task.value}, {tier.value}) "
            "but it's not in the catalog"
        )


# ---------------------------------------------------------------------------
# LLM-hygiene Commit B — five new orchestration bypass roles. Each role gets
# an end-to-end (role, tier) → model_id assertion plus an LLM_ROUTER=openai
# fallback assertion so the legacy ``gpt-4o-mini`` hardcodes can't sneak
# back in via a catalog regression.
# ---------------------------------------------------------------------------


def test_post_processor_role_resolves_to_classification_budget() -> None:
    """Post-processor: classification × budget → DeepSeek V4 Flash (catalog)
    or openai/gpt-4.1-nano (openai fallback)."""
    from gecko_core.routing.catalog import (
        AgentRole,
        Tier,
        model_id_for_role,
        resolve_model_for_router,
    )

    catalog_id = model_id_for_role(AgentRole.post_processor, Tier.budget)
    assert catalog_id == "deepseek/deepseek-v4-flash"
    assert (
        resolve_model_for_router(AgentRole.post_processor, Tier.budget, "openrouter") == catalog_id
    )
    # openai router substitutes the fallback (DeepSeek isn't reachable from
    # api.openai.com).
    assert (
        resolve_model_for_router(AgentRole.post_processor, Tier.budget, "openai")
        == "openai/gpt-4.1-nano"
    )


def test_refiner_role_resolves_to_creative_writing_balanced() -> None:
    """Refiner: creative_writing × balanced → DeepSeek V3.2 (S22-KIMI-AUDIT fix).

    Kimi K2.6 was the pre-fix value. It is a reasoning model: when called with
    response_format=json_object via OpenRouter its internal thinking trace
    exhausts max_tokens before emitting visible output → content=null →
    OrchestrationError. Replaced with DeepSeek V3.2 (non-reasoning, proven
    json_object compat). OpenAI router falls back to gpt-5-mini (deepseek is
    not reachable from api.openai.com).
    """
    from gecko_core.routing.catalog import (
        AgentRole,
        Tier,
        model_id_for_role,
        resolve_model_for_router,
    )

    catalog_id = model_id_for_role(AgentRole.refiner, Tier.balanced)
    assert catalog_id == "deepseek/deepseek-v3.2"
    assert resolve_model_for_router(AgentRole.refiner, Tier.balanced, "openrouter") == catalog_id
    assert (
        resolve_model_for_router(AgentRole.refiner, Tier.balanced, "openai") == "openai/gpt-5-mini"
    )


def test_judge_synth_role_resolves_to_creative_writing_quality() -> None:
    """Judge synth: creative_writing × quality → Claude Sonnet 4.6 / openai fallback."""
    from gecko_core.routing.catalog import (
        AgentRole,
        Tier,
        model_id_for_role,
        resolve_model_for_router,
    )

    catalog_id = model_id_for_role(AgentRole.judge_synth, Tier.quality)
    assert catalog_id == "anthropic/claude-sonnet-4.6"
    assert resolve_model_for_router(AgentRole.judge_synth, Tier.quality, "openrouter") == catalog_id
    # openai router can't reach Anthropic; ladder to gpt-5.5.
    assert (
        resolve_model_for_router(AgentRole.judge_synth, Tier.quality, "openai") == "openai/gpt-5.5"
    )


def test_research_basic_role_resolves_to_general_reasoning_balanced() -> None:
    """Basic research: general_reasoning × balanced → DeepSeek V3.2.

    This cell was the first Kimi K2.6 fix in 0.2.7 (catalog.py line 246).
    DeepSeek V3.2 is a non-reasoning model, json_object compatible via
    OpenRouter. OpenAI router falls back to gpt-5-mini (deepseek not on
    api.openai.com).
    """
    from gecko_core.routing.catalog import (
        AgentRole,
        Tier,
        model_id_for_role,
        resolve_model_for_router,
    )

    catalog_id = model_id_for_role(AgentRole.research_basic, Tier.balanced)
    assert catalog_id == "deepseek/deepseek-v3.2"
    assert (
        resolve_model_for_router(AgentRole.research_basic, Tier.balanced, "openrouter")
        == catalog_id
    )
    assert (
        resolve_model_for_router(AgentRole.research_basic, Tier.balanced, "openai")
        == "openai/gpt-5-mini"
    )


def test_ask_role_resolves_to_summarization_budget() -> None:
    """Ask: summarization × budget → openai/gpt-4.1-nano (already openai-native)."""
    from gecko_core.routing.catalog import (
        AgentRole,
        Tier,
        model_id_for_role,
        resolve_model_for_router,
    )

    catalog_id = model_id_for_role(AgentRole.ask, Tier.budget)
    # summarization × budget is already an openai/* id, so all routers agree.
    assert catalog_id == "openai/gpt-4.1-nano"
    for router in ("openai", "openrouter", "clawrouter"):
        assert resolve_model_for_router(AgentRole.ask, Tier.budget, router) == catalog_id


def test_resolve_model_for_router_openai_does_not_substitute_when_native() -> None:
    """When the catalog pick is already an openai/* model the openai router
    should pass it through unchanged (no spurious fallback substitution)."""
    from gecko_core.routing.catalog import (
        AgentRole,
        Tier,
        resolve_model_for_router,
    )

    # ask × budget = openai/gpt-4.1-nano — native, no fallback.
    assert resolve_model_for_router(AgentRole.ask, Tier.budget, "openai") == "openai/gpt-4.1-nano"
