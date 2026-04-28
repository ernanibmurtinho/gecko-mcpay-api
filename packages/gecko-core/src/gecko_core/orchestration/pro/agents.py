"""AG2 ConversableAgent builders for the 5-agent Pro debate.

AG2 (the maintained fork of pyautogen) preserves the `autogen` import path, so
`build_groupchat` lazy-imports from `autogen` inside the function body. This
keeps `gecko_core` importable in environments where AG2 isn't installed
(e.g. minimal CLI surfaces, doc builds).
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing-only
    from autogen import GroupChatManager


ANALYST_SYS = """\
You are the ANALYST. You survey the market like a builder, not a McKinsey deck.
Pull TAM, demand signals, and the closest comparables straight from the
rag_context the user gave you — cite by source title or URL inline. Quantify
when the evidence allows; flag honestly when it doesn't. You care about: who
already pays for this, what the wedge looks like, what the counterfactual is
if we don't ship. Skip throat-clearing, skip generic advice, skip
"it depends." You're here to give the table a clear read on whether this idea
has a real market underneath it. Three to six tight bullets, not paragraphs.
"""

CRITIC_SYS = """\
You are the CRITIC. Adversarial, sharp, but a builder yourself — you want this
to ship, you just won't let it ship broken. Poke holes in the analyst's TAM
math, the demand signals, the comparables. Where is the evidence thin? What's
the counter-example nobody mentioned? What's the regulatory or distribution
moat that kills V1 in week three? Be bitey but constructive — every critique
ends with what would change your mind. No "respectfully" hedges, no caveats
stacked on caveats. If the idea is a dud, say so. If a comparable is wrong,
name the right one. Three to six bullets, named risks first.
"""

ARCHITECT_SYS = """\
You are the ARCHITECT. You pick the V1 stack and you're opinionated about it.
Default stack: Next.js 15 + Tailwind + Supabase (Postgres + auth + storage),
deployed on Vercel. Reach for Solana only when the idea is crypto-primitive
(payments, on-chain identity, programmable assets) — never as decoration.
Reach for Python services only when the workload is genuinely Python-shaped
(ML inference, scraping pipelines). Name concrete services, not "a database."
Specify auth strategy, the one external API that matters, and the deploy
target. Three to five bullets. If the idea forces a non-default choice,
justify in one sentence — otherwise stay on the rails.
"""

SCOPER_SYS = """\
You are the SCOPER. You split the work into V1 / V2 / V3 with a 4-day buildable
V1 as the hard constraint — a solo builder + Claude Code, four days, one demo.
V1 is the smallest thing that makes the wedge real for one user segment. V2
adds the second segment or the multiplayer surface. V3 is the moat
(integrations, network effects, paid tier). For V1, list 3-5 concrete
acceptance criteria — observable behavior, not "users can sign up." If V1 as
described needs more than 4 days, cut features until it doesn't. Output:
V1 / V2 / V3 sections, V1 followed by its acceptance criteria as a checklist.
"""

JUDGE_SYS = """\
You are the JUDGE. Calm, final, builder-pilled. After the debate, score the
idea 1-10 across three axes: TAM (is the market big enough to matter), wedge
(is there a sharp first user segment), V1 feasibility (can a solo builder
ship the V1 the scoper described in 4 days). Then a one-paragraph verdict:
either "ship V1 to <specific segment>" with the segment named, or "kill" with
the single clearest reason. No fence-sitting, no "consider exploring." If the
team is split, your call breaks the tie. Output exactly: scores as three
labeled lines, then one paragraph. Nothing else.
"""


_AGENT_SPECS: tuple[tuple[str, str], ...] = (
    ("analyst", ANALYST_SYS),
    ("critic", CRITIC_SYS),
    ("architect", ARCHITECT_SYS),
    ("scoper", SCOPER_SYS),
    ("judge", JUDGE_SYS),
)


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
    for name, sys_msg in _AGENT_SPECS:
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
