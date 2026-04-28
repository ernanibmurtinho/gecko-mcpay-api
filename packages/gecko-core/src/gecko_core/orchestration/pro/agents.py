"""AG2 ConversableAgent builders for the 5-agent Pro debate.

AG2 (the maintained fork of pyautogen) preserves the `autogen` import path, so
`build_groupchat` lazy-imports from `autogen` inside the function body. This
keeps `gecko_core` importable in environments where AG2 isn't installed
(e.g. minimal CLI surfaces, doc builds).
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from gecko_core.orchestration.pro.prompts import REQUIRED_AGENTS, load_prompts

if TYPE_CHECKING:  # pragma: no cover - typing-only
    from autogen import GroupChatManager


def _agent_specs() -> tuple[tuple[str, str], ...]:
    """Resolve (agent_name, system_message) pairs in the canonical order.

    Reads from the prompts loader (default JSON or GECKO_PROMPTS_PATH override).
    Order matches REQUIRED_AGENTS so the GroupChat sees a deterministic sequence.
    """
    prompts = load_prompts()
    return tuple((name, prompts[name]) for name in REQUIRED_AGENTS)


def _override_model(base: dict[str, Any], model: str) -> dict[str, Any]:
    """Deep-copy ``base`` and rewrite the first config_list entry's ``model``.

    AG2 mutates ``llm_config`` internally (caching, key rotation) — sharing
    one dict across five agents would let one agent's state leak into the
    next. Deep-copy is cheap; the dict has at most a handful of entries.
    """
    cfg = copy.deepcopy(base)
    config_list = cfg.get("config_list")
    if isinstance(config_list, list) and config_list:
        # Mutate only the first entry; AG2 picks the head of the list when
        # routing a single call.
        first = dict(config_list[0])
        first["model"] = model
        config_list[0] = first
        cfg["config_list"] = config_list
    else:
        # Flat-style llm_config (no config_list) — AG2 supports this too.
        cfg["model"] = model
    return cfg


def build_groupchat(
    llm_config: dict[str, Any],
    *,
    model_matrix: dict[str, str] | None = None,
) -> GroupChatManager:
    """Construct the 5-agent GroupChat for the Pro debate.

    Args:
        llm_config: Base AG2 llm_config (router base_url + api_key + headers).
            The ``model`` field is overridden per-agent when ``model_matrix``
            is provided.
        model_matrix: Optional ``{agent_name: model_string}`` map. Each agent
            gets its own llm_config with that model substituted in.
            Unmapped agents fall back to ``llm_config`` as-is. Pass ``None``
            (default) for the legacy single-model behavior — kept for the
            existing test suite.

    AG2 is imported lazily so `gecko_core.orchestration.pro` stays importable
    in environments without AG2. Calling this function without AG2 installed
    raises ImportError at the import line below.
    """
    from autogen import ConversableAgent, GroupChat, GroupChatManager

    matrix = model_matrix or {}
    # Typed as list[Any] so mypy doesn't complain about list invariance
    # between ConversableAgent and the Agent supertype GroupChat expects.
    agents: list[Any] = []
    for name, sys_msg in _agent_specs():
        agent_cfg = _override_model(llm_config, matrix[name]) if name in matrix else llm_config
        agents.append(
            ConversableAgent(
                name=name,
                system_message=sys_msg,
                llm_config=agent_cfg,
                human_input_mode="NEVER",
            )
        )

    chat = GroupChat(
        agents=agents,
        messages=[],
        max_round=12,
        speaker_selection_method="auto",
    )
    # The manager itself uses the base llm_config (it doesn't generate replies
    # in our fixed-order driver, but AG2 still requires a valid config).
    return GroupChatManager(groupchat=chat, llm_config=llm_config)
