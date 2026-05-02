"""CDP Bazaar discovery extension declarations for gecko-api routes.

Sprint 12 Track B (S12-BAZAAR-01..03).

CDP Bazaar (`api.cdp.coinbase.com/platform/v2/x402/discovery/*`) catalogs
x402 resources the first time a payment settles through the CDP
Facilitator. To get a route ranked well in semantic search, sellers
register a `discoveryExtension` blob alongside the payment requirements:

    {
      "input": {...example invocation arguments...},
      "schema": {
        "properties": {
          "input":  <JSON Schema for the request body>,
          "output": <JSON Schema for the response body>
        }
      },
      "description": "Semantic-search-friendly description, NOT just the path",
      "tags": [...]
    }

The x402 v2 SDK is JS-flavored; in Python we declare the metadata as
plain dicts here, expose them via `/.well-known/x402` (so a payer-side
client can fetch them), and validate the shape in tests. When Track A's
CDP facilitator client lands, the settle code path will read from
``BAZAAR_EXTENSIONS`` and attach the right blob to its outbound payload.

**Important:** this module does *not* trigger any settlement. It only
declares metadata. Listing happens when the operator actually settles a
payment through CDP — that's S12 Track C.

Conventions (see `docs/runbooks/cdp-bazaar.md`):

1. Descriptions are written for an LLM doing semantic search. Spell out
   *what* the route does and what makes it differentiated, not the URL.
2. Paid routes never carry bare-UUID path segments — Bazaar collapses
   bare UUIDs into a single catalog entry. If a future paid route is
   keyed by ID, prefix the segment (e.g. ``/research/session-{uuid}``).
3. Every declared extension's ``input`` example must validate against
   ``schema.properties.input``. The CI harness in
   ``tests/api/test_bazaar_extensions.py`` enforces this.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class BazaarExtension(BaseModel):
    """Bazaar `discoveryExtension` declaration for a single x402 route.

    Mirrors the JS SDK's ``declareDiscoveryExtension({ input, schema, ... })``
    call. Stored as a plain dict on serialization so it can be embedded
    in the x402 settle response without translation.
    """

    description: str = Field(
        ...,
        min_length=40,
        description=(
            "Semantic-search-friendly description. Should describe what the "
            "route does for an LLM doing discovery, not the URL path."
        ),
    )
    tags: list[str] = Field(default_factory=list)
    input: dict[str, Any] = Field(
        ...,
        description=(
            "Concrete example invocation arguments — must validate against schema.properties.input."
        ),
    )
    schema_: dict[str, Any] = Field(
        ...,
        alias="schema",
        description=(
            "JSON Schema with at least properties.input and properties.output "
            "describing the request and response bodies."
        ),
    )
    output_example: dict[str, Any] | None = Field(
        default=None,
        description="Optional response example for buyers/agents previewing the route.",
    )

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# JSON Schemas — input + output per route
# ---------------------------------------------------------------------------

_TIER_PRESET_ENUM = ["quality", "balanced", "budget", "free"]

_RESEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["idea"],
    "additionalProperties": False,
    "properties": {
        "idea": {
            "type": "string",
            "minLength": 10,
            "maxLength": 500,
            "description": "Founder's product hypothesis to validate.",
        },
        "tier": {"type": "string", "enum": ["basic", "pro"], "default": "basic"},
        "urls": {
            "type": ["array", "null"],
            "items": {"type": "string", "format": "uri"},
            "description": "Optional seed URLs to ingest before adversarial debate.",
        },
        "auto_approve": {"type": "boolean", "default": True},
        "project_id": {"type": ["string", "null"]},
        "frames_username": {"type": ["string", "null"]},
        "tier_preset": {
            "type": "string",
            "enum": _TIER_PRESET_ENUM,
            "default": "balanced",
        },
    },
}

_RESEARCH_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["session_id", "status"],
    "properties": {
        "session_id": {"type": "string", "format": "uuid"},
        "status": {"type": "string", "enum": ["processing", "complete", "failed"]},
        "poll_url": {"type": "string"},
        "events_url": {"type": "string"},
        "events_token": {"type": "string"},
    },
}

_PLAN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["session_id"],
    "additionalProperties": False,
    "properties": {
        "session_id": {
            "type": "string",
            "minLength": 1,
            "description": "UUID of a prior /research session to advise against.",
        },
        "tier_preset": {
            "type": "string",
            "enum": _TIER_PRESET_ENUM,
            "default": "balanced",
        },
        "project_id": {"type": ["string", "null"]},
        "frames_username": {"type": ["string", "null"]},
    },
}

_PLAN_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "AdvisorPanel JSON — 5-voice advisor panel verdict.",
    "properties": {
        "voices": {"type": "array"},
        "synthesis": {"type": "object"},
    },
}

_ROUTE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["prompt"],
    "additionalProperties": False,
    "properties": {
        "prompt": {"type": "string", "minLength": 1, "maxLength": 20_000},
        "task_hint": {
            "type": "string",
            "description": "extraction|summary|reasoning|code|default",
            "default": "default",
        },
        "max_cost_usd": {"type": "number", "minimum": 0, "default": 0.05},
        "prefer_premium": {"type": "boolean", "default": False},
        "tier_preset": {
            "type": "string",
            "enum": _TIER_PRESET_ENUM,
            "default": "balanced",
        },
    },
}

_ROUTE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "model_used": {"type": "string"},
        "completion": {"type": "string"},
        "tier_charged": {"type": "string"},
        "prepay_usd": {"type": "number"},
    },
}


# S13-COMMO-01 — POST /advise schemas.
_ADVISE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["session_id", "voice"],
    "additionalProperties": False,
    "properties": {
        "session_id": {
            "type": "string",
            "minLength": 1,
            "description": "UUID of a prior /research session to advise against.",
        },
        "voice": {
            "type": "string",
            "enum": [
                "ceo",
                "cto",
                "business_manager",
                "product_manager",
                "staff_manager",
                "bm",
                "pm",
                "sm",
            ],
            "description": (
                "Which advisor voice to invoke. Aliases bm/pm/sm map to "
                "business_manager / product_manager / staff_manager."
            ),
        },
        "tier_preset": {
            "type": "string",
            "enum": _TIER_PRESET_ENUM,
            "default": "balanced",
        },
    },
}

_ADVISE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "AdvisorVoiceResult — single voice output_md + tokens metadata.",
    "properties": {
        "role": {"type": "string"},
        "output_md": {"type": "string"},
        "model_used": {"type": "string"},
        "closing_line": {"type": "string"},
        "tokens_in": {"type": "integer"},
        "tokens_out": {"type": "integer"},
        "prepay_usd": {"type": "number"},
    },
}

# S13-COMMO-02 — POST /ask schemas.
_ASK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["session_id", "question"],
    "additionalProperties": False,
    "properties": {
        "session_id": {"type": "string", "minLength": 1},
        "question": {"type": "string", "minLength": 3, "maxLength": 500},
    },
}

_ASK_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "AskResult — grounded answer with citations.",
    "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array"},
        "prepay_usd": {"type": "number"},
    },
}

# S14-PULSE-04 — POST /pulse schemas.
_PULSE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "session_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "UUID of the parent (research) session being re-pulsed. "
                "v14 surface — creates a NEW session row with "
                "phase='during_build' and parent_session_id=<input>."
            ),
        },
        "project_id": {
            "type": ["string", "null"],
            "description": (
                "Project UUID (legacy closing-line delta surface). When "
                "session_id is also given, session_id wins."
            ),
        },
        "tier_preset": {
            "type": "string",
            "enum": _TIER_PRESET_ENUM,
            "default": "balanced",
        },
    },
}

_PULSE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "PulseResult — single-token verdict + gap_classification + "
        "advisor closing lines + fresh windowed citations."
    ),
    "properties": {
        "parent_session_id": {"type": "string", "format": "uuid"},
        "pulse_session_id": {"type": "string", "format": "uuid"},
        "verdict": {"type": "string", "enum": ["PIVOT", "REFINE", "GO"]},
        "gap_classification": {"type": "string"},
        "panel": {"type": "object"},
        "citations": {"type": "array"},
        "summary_bullets": {"type": "array", "items": {"type": "string"}},
    },
}

# S13-COMMO-03 — POST /classify schemas.
_CLASSIFY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["idea"],
    "additionalProperties": False,
    "properties": {
        "idea": {
            "type": "string",
            "minLength": 10,
            "maxLength": 500,
            "description": "Plain-language startup idea to classify.",
        },
    },
}

_CLASSIFY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["categories", "suggested_sources", "priority_weights"],
    "properties": {
        "categories": {"type": "array", "items": {"type": "string"}},
        "scores": {
            "type": "object",
            "additionalProperties": {"type": "number"},
        },
        "suggested_sources": {"type": "array", "items": {"type": "string"}},
        "priority_weights": {
            "type": "object",
            "additionalProperties": {"type": "number"},
        },
        "prepay_usd": {"type": "number"},
    },
}

# ---------------------------------------------------------------------------
# Extension registry — one entry per paid route eligible for Bazaar listing
# ---------------------------------------------------------------------------


BAZAAR_EXTENSIONS: dict[str, BazaarExtension] = {
    "POST /research": BazaarExtension(
        description=(
            "Adversarial multi-agent product validation: emits a "
            "PIVOT/REFINE/GO verdict + cited evidence + 5-voice advisor "
            "panel. Best for founders who want a structured second opinion "
            "on a startup hypothesis before committing build effort."
        ),
        tags=[
            "founder-validation",
            "product-research",
            "adversarial-prd",
            "verdict",
            "multi-agent",
        ],
        input={
            "idea": "An AI agent marketplace for legal contract review",
            "tier": "basic",
            "tier_preset": "balanced",
        },
        schema={
            "type": "object",
            "properties": {
                "input": _RESEARCH_INPUT_SCHEMA,
                "output": _RESEARCH_OUTPUT_SCHEMA,
            },
        },
        output_example={
            "session_id": "00000000-0000-0000-0000-000000000000",
            "status": "processing",
            "poll_url": "/sessions/00000000-0000-0000-0000-000000000000/result",
        },
    ),
    "POST /research/pro": BazaarExtension(
        description=(
            "Pro-tier adversarial validation: 5-agent AutoGen GroupChat "
            "debate (researcher, critic, devil's advocate, synthesist, "
            "judge) streams turn-by-turn over SSE and emits the same "
            "PIVOT/REFINE/GO verdict with deeper citations. Choose this "
            "over basic when the idea is high-stakes or contested."
        ),
        tags=[
            "founder-validation",
            "product-research",
            "adversarial-prd",
            "multi-agent-debate",
            "sse-streaming",
            "pro",
        ],
        input={
            "idea": "An on-chain reputation system for autonomous AI agents",
            "tier": "pro",
            "tier_preset": "quality",
        },
        schema={
            "type": "object",
            "properties": {
                "input": _RESEARCH_INPUT_SCHEMA,
                "output": _RESEARCH_OUTPUT_SCHEMA,
            },
        },
    ),
    "POST /plan": BazaarExtension(
        description=(
            "5-voice advisor panel against an existing research session: "
            "GTM operator, design lead, technical architect, finance, and "
            "skeptic each weigh in. Returns a synthesized recommendation "
            "for next-step priorities. Pairs with /research for a complete "
            "kill/refine/build + actionable plan loop."
        ),
        tags=[
            "advisor-panel",
            "product-strategy",
            "founder-validation",
            "multi-voice",
        ],
        input={
            "session_id": "00000000-0000-0000-0000-000000000000",
            "tier_preset": "balanced",
        },
        schema={
            "type": "object",
            "properties": {
                "input": _PLAN_INPUT_SCHEMA,
                "output": _PLAN_OUTPUT_SCHEMA,
            },
        },
    ),
    "POST /route": BazaarExtension(
        description=(
            "Cost-aware LLM routing for extraction/summary/default tasks: "
            "picks the cheapest model that meets the task's quality bar "
            "and surfaces which tier was charged. Useful for agents that "
            "want one paid surface for low-stakes LLM calls without "
            "managing their own model selection."
        ),
        tags=["llm-routing", "cost-aware", "extraction", "summary"],
        input={
            "prompt": "Summarize this paragraph in one sentence: ...",
            "task_hint": "summary",
            "max_cost_usd": 0.05,
            "tier_preset": "balanced",
        },
        schema={
            "type": "object",
            "properties": {
                "input": _ROUTE_INPUT_SCHEMA,
                "output": _ROUTE_OUTPUT_SCHEMA,
            },
        },
    ),
    "POST /route/premium": BazaarExtension(
        description=(
            "Cost-aware LLM routing for reasoning/code tasks: routes to a "
            "premium-tier model (e.g. GPT-4-class) and returns the "
            "completion plus the tier-charged metadata. Use when the "
            "task_hint is 'reasoning' or 'code' and quality matters more "
            "than cost."
        ),
        tags=["llm-routing", "premium", "reasoning", "code"],
        input={
            "prompt": "Refactor this Python function for readability: ...",
            "task_hint": "code",
            "max_cost_usd": 0.20,
            "tier_preset": "quality",
        },
        schema={
            "type": "object",
            "properties": {
                "input": _ROUTE_INPUT_SCHEMA,
                "output": _ROUTE_OUTPUT_SCHEMA,
            },
        },
    ),
    "POST /advise": BazaarExtension(
        description=(
            "Single advisor voice (CEO/CTO/Business/Product/Staff Manager) "
            "against an existing research session: cheap focused guidance "
            "from one persona instead of the full 5-voice panel. Use when "
            "you want a targeted second opinion on a specific dimension "
            "(go-to-market, technical feasibility, etc.) without paying "
            "for the whole panel."
        ),
        tags=[
            "advisor-voice",
            "founder-validation",
            "single-persona",
            "follow-up",
        ],
        input={
            "session_id": "00000000-0000-0000-0000-000000000000",
            "voice": "cto",
            "tier_preset": "balanced",
        },
        schema={
            "type": "object",
            "properties": {
                "input": _ADVISE_INPUT_SCHEMA,
                "output": _ADVISE_OUTPUT_SCHEMA,
            },
        },
    ),
    "POST /ask": BazaarExtension(
        description=(
            "Grounded follow-up question against an existing research "
            "session's knowledge base: pulls citations from the indexed "
            "corpus, no fresh research run. Use when an agent needs to "
            "interrogate a previously-researched corpus without paying "
            "the full /research price each time."
        ),
        tags=[
            "follow-up",
            "knowledge-base",
            "grounded-qa",
            "citations",
        ],
        input={
            "session_id": "00000000-0000-0000-0000-000000000000",
            "question": "What were the top three competitor risks?",
        },
        schema={
            "type": "object",
            "properties": {
                "input": _ASK_INPUT_SCHEMA,
                "output": _ASK_OUTPUT_SCHEMA,
            },
        },
    ),
    "POST /classify": BazaarExtension(
        description=(
            "Classify an idea into Gecko's category taxonomy and return "
            "suggested sources with priority weights — the pre-flight "
            "decision behind /research source selection. Use when you "
            "want Gecko's source-routing intelligence without running the "
            "full adversarial debate."
        ),
        tags=[
            "classification",
            "category-routing",
            "source-selection",
            "embedding-nn",
        ],
        input={
            "idea": "An on-chain reputation system for autonomous AI agents",
        },
        schema={
            "type": "object",
            "properties": {
                "input": _CLASSIFY_INPUT_SCHEMA,
                "output": _CLASSIFY_OUTPUT_SCHEMA,
            },
        },
        output_example={
            "categories": ["crypto", "defi"],
            "scores": {"crypto": 0.71, "defi": 0.65, "saas": 0.20},
            "suggested_sources": [
                "tavily",
                "hn",
                "reddit",
                "gecko_precedent",
                "colosseum",
                "twit_sh",
            ],
            "priority_weights": {
                "tavily": 1.0,
                "hn": 1.0,
                "reddit": 1.0,
                "gecko_precedent": 1.0,
                "colosseum": 0.71,
                "twit_sh": 0.71,
            },
            "prepay_usd": 0.10,
        },
    ),
    "POST /pulse": BazaarExtension(
        description=(
            "Recurring product validation: re-runs adversarial debate "
            "against evolving evidence to surface verdict shifts as "
            "build progresses. Founders re-pulse weekly — each call "
            "creates a during-build session linked to the original "
            "research and emits a fresh PIVOT/REFINE/GO verdict + "
            "structured gap_classification + advisor closing lines + "
            "citations from the last 14 days. 12-pack prepay available "
            "at a 10% bulk discount ($5.40 / 12 calls)."
        ),
        tags=[
            "pulse",
            "recurring-validation",
            "during-build",
            "lifecycle-monetization",
            "verdict-shift",
            "founder-validation",
        ],
        input={
            "session_id": "00000000-0000-0000-0000-000000000000",
            "tier_preset": "balanced",
        },
        schema={
            "type": "object",
            "properties": {
                "input": _PULSE_INPUT_SCHEMA,
                "output": _PULSE_OUTPUT_SCHEMA,
            },
        },
        output_example={
            "parent_session_id": "00000000-0000-0000-0000-000000000000",
            "pulse_session_id": "00000000-0000-0000-0000-000000000001",
            "verdict": "REFINE",
            "gap_classification": "Partial:segment",
            "summary_bullets": [
                "ceo: refine pricing for the SMB wedge",
                "cto: keep core stack, swap auth provider",
                "product_manager: narrow ICP to design teams",
            ],
        },
    ),
    "POST /route/upgrade": BazaarExtension(
        description=(
            "Highest-tier LLM routing with prefer_premium=True forced on: "
            "always picks the strongest available model regardless of "
            "task_hint. Use sparingly — flat $0.20 charge per call."
        ),
        tags=["llm-routing", "premium", "upgrade", "best-quality"],
        input={
            "prompt": "Draft an investor memo for an AI infrastructure startup",
            "task_hint": "reasoning",
            "max_cost_usd": 0.20,
            "prefer_premium": True,
            "tier_preset": "quality",
        },
        schema={
            "type": "object",
            "properties": {
                "input": _ROUTE_INPUT_SCHEMA,
                "output": _ROUTE_OUTPUT_SCHEMA,
            },
        },
    ),
}


# ---------------------------------------------------------------------------
# Helpers used by /.well-known/x402 + the route-consolidation audit
# ---------------------------------------------------------------------------


# Bare-UUID path segments are exactly ``{name}`` where ``name`` looks like
# an id. Anything prefixed (e.g. ``session-{session_id}``) is fine because
# the constant prefix prevents Bazaar's path-collapsing heuristic from
# treating two paths as the same resource.
_BARE_ID_SEGMENT = re.compile(r"^\{[A-Za-z_][A-Za-z0-9_]*_id\}$")
_BARE_UUID_NAMES = {"id", "uuid", "session_id", "project_id"}


def has_bare_uuid_segment(route_pattern: str) -> bool:
    """Return True if any path segment is a bare UUID/id placeholder.

    A "bare UUID" is a path segment that's *only* a parameter (``{session_id}``)
    rather than a parameter glued to a constant prefix
    (``session-{session_id}``). Bazaar collapses bare-UUID paths; prefixed
    paths stay distinct in the catalog.
    """
    # Strip the leading verb ("POST /research" → "/research").
    if " " in route_pattern:
        _, path = route_pattern.split(" ", 1)
    else:
        path = route_pattern
    for segment in path.strip("/").split("/"):
        if not segment:
            continue
        if _BARE_ID_SEGMENT.match(segment):
            return True
        # Generic ``{id}`` / ``{uuid}`` cases too.
        inner = segment[1:-1] if segment.startswith("{") and segment.endswith("}") else None
        if inner is not None and inner in _BARE_UUID_NAMES:
            return True
    return False


def extension_as_dict(ext: BazaarExtension) -> dict[str, Any]:
    """Serialize a BazaarExtension to the wire shape Bazaar expects.

    Uses ``by_alias=True`` so the ``schema_`` field lands as ``schema`` on
    the wire. Strips ``output_example`` when unset so the JSON stays clean.
    """
    payload = ext.model_dump(by_alias=True, exclude_none=True)
    return payload


__all__ = [
    "BAZAAR_EXTENSIONS",
    "BazaarExtension",
    "extension_as_dict",
    "has_bare_uuid_segment",
]
