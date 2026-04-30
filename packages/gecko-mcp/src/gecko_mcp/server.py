"""Gecko MCP server.

Exposes three tools (the contract for the Claude Code skill bootstrap):

    gecko_research — full discover -> index -> pay -> generate workflow
    gecko_ask      — follow-up question against an existing session
    gecko_sources  — list indexed sources for a session

Thin transport layer. v2: the server no longer imports `gecko_core` directly.
It is just another HTTP client of `gecko-api`, paying through the same x402
gate as any other agent. All real work happens server-side; this module
parses MCP arguments, calls `GeckoAPIClient`, and JSON-serializes the result.

Environment:
    GECKO_API_URL — base URL for `gecko-api`. Default:
                    ``https://api.geckovision.tech`` (production). Override
                    to ``http://localhost:8000`` for local dev.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from gecko_mcp.api_client import GeckoAPIClient
from gecko_mcp.clawrouter_supervisor import warm_clawrouter

server: Server = Server("gecko-mcp")

# Lazy module-level client — built on first tool call so server startup
# succeeds even if the API is down or env is incomplete (Claude Code's MCP
# bootstrap must not fail because of transient network issues).
_client: GeckoAPIClient | None = None


def _get_client() -> GeckoAPIClient:
    global _client
    if _client is None:
        _client = GeckoAPIClient()
    return _client


_RESEARCH_DESCRIPTION = (
    "Run the full Builder Bootstrap workflow for a startup idea. Discovers "
    "sources (or uses the URLs you provide), indexes them into a session "
    "knowledge base, runs the x402 payment gate (stub mode by default), and "
    "generates a one-page business plan, validation report, and PRD with "
    "inline citations. Returns a ResearchResult JSON payload including the "
    "session_id needed for follow-up `gecko_ask` calls."
)

_ASK_DESCRIPTION = (
    "Ask a follow-up question grounded in an existing session's knowledge "
    "base. Cites the chunks used inline. Requires the session_id returned "
    "by a prior `gecko_research` call."
)

_SOURCES_DESCRIPTION = (
    "List every source indexed into a session's knowledge base (URL, type, "
    "chunk count, and indexed_at timestamp). Useful for transparency before "
    "running `gecko_ask`."
)

_CLASSIFY_DESCRIPTION = (
    "Classify a startup idea into Gecko's category taxonomy (crypto, defi, "
    "devtools, saas, regulated, hackathon-team) using embedding nearest-"
    "neighbor cosine similarity. Returns the selected categories (top-2 "
    "above threshold) plus the full score map so callers can see why. "
    "Free — no x402 payment required."
)

_PRECEDENTS_DESCRIPTION = (
    "Look up prior Gecko verdicts on similar ideas (the flywheel). Embeds "
    "the idea, runs an internal cosine search over `gecko_precedent`, and "
    "returns the top-K precedent rows with verdict + key_comparables. Free "
    "— no x402 payment required."
)

_AVAILABLE_SOURCES_DESCRIPTION = (
    "List the catalog of signal sources Gecko queries (Tavily, HN, Reddit, "
    "twit.sh, Colosseum, gecko_precedent flywheel, …) with description, "
    "gating rule, and per-call cost. Distinct from `gecko_sources`, which "
    "lists indexed sources for a specific session. Free."
)

_ROUTE_DESCRIPTION = (
    "Route an LLM call through Gecko's cost-aware router. Pays via x402 "
    "wallet. Use task_hint to bias model selection (reasoning, code, "
    "extraction, summary, default). Returns response + cost breakdown "
    "including savings_vs_premium so subagents can show 'you saved $X by "
    "routing through Gecko'."
)

_SCAFFOLD_DESCRIPTION = (
    "Generate a 3-file project starter bundle (PRD.md, business-plan.md, "
    "BUILDING.md) from a completed Pro tier debate. Refuses on verdict=kill. "
    "Files land under <output_dir>/.gecko/scaffolds/<session_id>/. The "
    "BUILDING.md file is a ready-to-use Claude Code prompt for V1. Free — "
    "the user already paid for the Pro debate."
)

_ADVISE_DESCRIPTION = (
    "Run a single advisor voice (CEO, CTO, business_manager, "
    "product_manager, or staff_manager) over an existing session. Cheaper "
    "than `gecko_plan` (1 LLM call vs 5). FREE in Sprint 4 — pricing "
    "decision deferred to Sprint 5 once economics are confirmed."
)

_PLAN_DESCRIPTION = (
    "Run the full 5-voice Advisor Panel over an existing session. Voices "
    "are independent and EXPECTED to disagree — the staff_manager output "
    "synthesizes the others into a sprint plan. Reads scaffold + flywheel "
    "+ V1 source signal. Works on kill verdicts too (pivot advice). FREE "
    "in Sprint 4; the API charge ($0.25 via x402) is wired in Sprint 5."
)

_PULSE_DESCRIPTION = (
    "Re-run the advisor panel with fresh context and surface what CHANGED "
    "since the last advise. v1 detects deltas via closing-line equality; "
    "embedding-based deltas land in Sprint 5. FREE."
)

_MEMORY_SAVE_DESCRIPTION = (
    "Append a typed entry to Gecko's native decision-memory layer. Free. "
    "Scope is `project|session|user`. Entry types are the 5 paid loop steps "
    "plus `feature_shipped` and `user_note`."
)

_MEMORY_RECALL_DESCRIPTION = (
    "Recall recent memory entries for a scope, newest first. Indexed lookup; "
    "no embedding work. Filter by entry_type or key to narrow."
)

_MEMORY_SEARCH_DESCRIPTION = (
    "Cosine-similarity search over a scope's memory entries. Returns the "
    "top_k matches with similarity score. Server-side scope filter prevents "
    "cross-scope leakage."
)

_MEMORY_QUERY_DESCRIPTION = (
    "Query memory by structured filters (S6-MINE-01). Combines scope, "
    "entry_type, since (ISO-8601 timestamp), and k into one indexed lookup. "
    "When a `query` string is provided, falls back to cosine similarity over "
    "the same scope. Free; no embedding work unless `query` is set."
)

_RESUME_DESCRIPTION = (
    "Recap a project's recent loop activity (S5-MEM-05). Walks memory "
    "entries scoped to the project for the last `days` (default 30), groups "
    "by entry_type, and returns a structured payload with last advisor "
    "panel + last pulse deltas. Sub-second; no LLM call."
)

_PROJECT_ECONOMICS_DESCRIPTION = (
    "Per-project economics snapshot (S2-09): privy wallet address, live USDC "
    "balance, budget cap + spend, and the 5 most recent paid sessions. Use "
    "this to answer 'how much have I spent on project X?' and 'does my "
    "project wallet have enough balance for the next run?'. Distinct from "
    "`session_id`-scoped economics — pass a project UUID."
)


@server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="gecko_research",
            description=_RESEARCH_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "idea": {
                        "type": "string",
                        "description": "Plain-language startup idea (1-2 sentences).",
                    },
                    "tier": {
                        "type": "string",
                        "enum": ["basic", "pro"],
                        "default": "basic",
                        "description": (
                            "'basic' = single-pass generation. 'pro' = multi-agent "
                            "AutoGen GroupChat (slower, deeper)."
                        ),
                    },
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional seed URLs (web articles or YouTube). When "
                            "omitted, sources are auto-discovered via Tavily."
                        ),
                    },
                    "auto_approve": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "If True, skip the source-approval prompt and proceed "
                            "directly to the payment gate. MCP clients should "
                            "leave this True — there is no interactive prompt."
                        ),
                    },
                    "project_id": {
                        "type": "string",
                        "description": (
                            "Optional project UUID to attach this session to. "
                            "Phase B5 v1: enables client-side budget enforcement "
                            "and audit-trail (paid_from_wallet_address)."
                        ),
                    },
                    "tier_preset": {
                        "type": "string",
                        "enum": ["quality", "balanced", "budget", "free"],
                        "default": "balanced",
                        "description": (
                            "User-facing cost/quality preset (S4-MATRIX-01). Maps "
                            "to per-agent model selection via the curated catalog. "
                            "Default 'balanced' picks Kimi K2.6 / DeepSeek for the "
                            "sweet spot. Orthogonal to 'tier' (basic/pro)."
                        ),
                    },
                },
                "required": ["idea"],
            },
        ),
        Tool(
            name="gecko_ask",
            description=_ASK_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session UUID returned by `gecko_research`.",
                    },
                    "question": {
                        "type": "string",
                        "description": "Plain-language follow-up question.",
                    },
                },
                "required": ["session_id", "question"],
            },
        ),
        Tool(
            name="gecko_sources",
            description=_SOURCES_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session UUID returned by `gecko_research`.",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="gecko_classify",
            description=_CLASSIFY_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "idea": {
                        "type": "string",
                        "description": "Plain-language startup idea (1-2 sentences).",
                    },
                },
                "required": ["idea"],
            },
        ),
        Tool(
            name="gecko_precedents",
            description=_PRECEDENTS_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "idea": {
                        "type": "string",
                        "description": "Plain-language startup idea to look up.",
                    },
                    "top_k": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 25,
                        "description": "Max number of precedent rows to return.",
                    },
                },
                "required": ["idea"],
            },
        ),
        Tool(
            name="gecko_available_sources",
            description=_AVAILABLE_SOURCES_DESCRIPTION,
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="gecko_route",
            description=_ROUTE_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "task_hint": {
                        "type": "string",
                        "enum": [
                            "reasoning",
                            "code",
                            "extraction",
                            "summary",
                            "default",
                        ],
                    },
                    "max_cost_usd": {"type": "number"},
                    "prefer_premium": {"type": "boolean"},
                    "tier_preset": {
                        "type": "string",
                        "enum": ["quality", "balanced", "budget", "free"],
                        "default": "balanced",
                    },
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="gecko_scaffold",
            description=_SCAFFOLD_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Session UUID returned by `gecko_research` (Pro tier). "
                            "Verdict must be 'ship' or 'pivot'."
                        ),
                    },
                    "output_dir": {
                        "type": "string",
                        "description": (
                            "Workspace root. Files land under "
                            "<output_dir>/.gecko/scaffolds/<session_id>/. "
                            "Defaults to the server's current working directory."
                        ),
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="gecko_advise",
            description=_ADVISE_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session UUID returned by `gecko_research`.",
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
                            "Which advisor voice to invoke. Aliases bm/pm/sm "
                            "map to business_manager / product_manager / "
                            "staff_manager."
                        ),
                    },
                    "tier_preset": {
                        "type": "string",
                        "enum": ["quality", "balanced", "budget", "free"],
                        "default": "balanced",
                    },
                },
                "required": ["session_id", "voice"],
            },
        ),
        Tool(
            name="gecko_plan",
            description=_PLAN_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session UUID. Any verdict — kills get pivot advice too.",
                    },
                    "tier_preset": {
                        "type": "string",
                        "enum": ["quality", "balanced", "budget", "free"],
                        "default": "balanced",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="gecko_pulse",
            description=_PULSE_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Session UUID to re-pulse. Either session_id or "
                            "project_id is required; when both are given, "
                            "project_id wins."
                        ),
                    },
                    "project_id": {
                        "type": "string",
                        "description": (
                            "Project UUID. Walks pulse history across the "
                            "project's sessions for delta detection (S5-API-02)."
                        ),
                    },
                    "tier_preset": {
                        "type": "string",
                        "enum": ["quality", "balanced", "budget", "free"],
                        "default": "balanced",
                    },
                },
            },
        ),
        Tool(
            name="gecko_memory_save",
            description=_MEMORY_SAVE_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "scope_type": {
                        "type": "string",
                        "enum": ["project", "session", "user"],
                    },
                    "scope_id": {"type": "string"},
                    "entry_type": {
                        "type": "string",
                        "enum": [
                            "verdict_received",
                            "scaffold_generated",
                            "plan_advised",
                            "advisor_voiced",
                            "pulse_run",
                            "feature_shipped",
                            "user_note",
                        ],
                    },
                    "value": {"type": "object"},
                    "key": {"type": "string"},
                },
                "required": ["scope_type", "scope_id", "entry_type", "value"],
            },
        ),
        Tool(
            name="gecko_memory_recall",
            description=_MEMORY_RECALL_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "scope_type": {
                        "type": "string",
                        "enum": ["project", "session", "user"],
                    },
                    "scope_id": {"type": "string"},
                    "entry_type": {"type": "string"},
                    "key": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["scope_type", "scope_id"],
            },
        ),
        Tool(
            name="gecko_memory_search",
            description=_MEMORY_SEARCH_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "scope_type": {
                        "type": "string",
                        "enum": ["project", "session", "user"],
                    },
                    "scope_id": {"type": "string"},
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "default": 5},
                },
                "required": ["scope_type", "scope_id", "query"],
            },
        ),
        Tool(
            name="gecko_memory_query",
            description=_MEMORY_QUERY_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "scope_type": {
                        "type": "string",
                        "enum": ["project", "session", "user"],
                    },
                    "scope_id": {"type": "string"},
                    "entry_type": {"type": "string"},
                    "since": {
                        "type": "string",
                        "description": "ISO-8601 timestamp; only entries created at/after this value.",
                    },
                    "k": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional cosine-similarity query. When set, returns "
                            "the top-k semantic matches instead of a chronological list."
                        ),
                    },
                },
                "required": ["scope_type", "scope_id"],
            },
        ),
        Tool(
            name="gecko_resume",
            description=_RESUME_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "days": {"type": "integer", "default": 30},
                },
                "required": ["project_id"],
            },
        ),
        Tool(
            name="gecko_project_economics",
            description=_PROJECT_ECONOMICS_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": (
                            "Project UUID. Get one from `gecko project list` "
                            "or the V2 web app at app.geckovision.tech."
                        ),
                    },
                },
                "required": ["project_id"],
            },
        ),
    ]


@server.call_tool()  # type: ignore[untyped-decorator]
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    client = _get_client()

    if name == "gecko_research":
        idea = str(arguments["idea"])
        tier_raw = arguments.get("tier", "basic")
        if tier_raw not in ("basic", "pro"):
            raise ValueError(f"tier must be 'basic' or 'pro', got {tier_raw!r}")
        urls_raw = arguments.get("urls")
        urls: list[str] | None = [str(u) for u in urls_raw] if isinstance(urls_raw, list) else None
        # `auto_approve` is accepted in the schema for forward-compat but
        # the API always runs auto-approved; there is no MCP prompt surface.
        project_id_raw = arguments.get("project_id")
        project_id = str(project_id_raw) if project_id_raw else None
        tier_preset_raw = arguments.get("tier_preset", "balanced")
        if tier_preset_raw not in ("quality", "balanced", "budget", "free"):
            raise ValueError(
                f"tier_preset must be quality|balanced|budget|free, got {tier_preset_raw!r}"
            )

        result = await client.research(
            idea,
            tier=tier_raw,
            urls=urls,
            project_id=project_id,
            tier_preset=str(tier_preset_raw),
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "gecko_ask":
        result = await client.ask(
            session_id=str(arguments["session_id"]),
            question=str(arguments["question"]),
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "gecko_sources":
        sources = await client.list_sources(session_id=str(arguments["session_id"]))
        return [TextContent(type="text", text=json.dumps(sources, indent=2))]

    if name == "gecko_classify":
        result = await _run_classify(idea=str(arguments["idea"]))
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "gecko_precedents":
        top_k_raw = arguments.get("top_k", 5)
        top_k = int(top_k_raw) if top_k_raw is not None else 5
        result_list = await _run_precedents(idea=str(arguments["idea"]), top_k=top_k)
        return [TextContent(type="text", text=json.dumps(result_list, indent=2))]

    if name == "gecko_available_sources":
        result_list = _run_available_sources()
        return [TextContent(type="text", text=json.dumps(result_list, indent=2))]

    if name == "gecko_route":
        result = await _run_route(
            client=client,
            prompt=str(arguments["prompt"]),
            task_hint=str(arguments.get("task_hint", "default")),
            max_cost_usd=float(arguments.get("max_cost_usd", 0.05)),
            prefer_premium=bool(arguments.get("prefer_premium", False)),
            tier_preset=str(arguments.get("tier_preset", "balanced")),
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "gecko_scaffold":
        output_dir_raw = arguments.get("output_dir")
        result = await _run_scaffold(
            session_id=str(arguments["session_id"]),
            output_dir=str(output_dir_raw) if output_dir_raw else None,
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "gecko_advise":
        result = await _run_advise(
            session_id=str(arguments["session_id"]),
            voice=str(arguments["voice"]),
            tier_preset=str(arguments.get("tier_preset", "balanced")),
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "gecko_plan":
        result = await _run_plan(
            session_id=str(arguments["session_id"]),
            tier_preset=str(arguments.get("tier_preset", "balanced")),
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "gecko_pulse":
        sid_raw = arguments.get("session_id")
        pid_raw = arguments.get("project_id")
        result = await _run_pulse(
            session_id=str(sid_raw) if sid_raw else None,
            project_id=str(pid_raw) if pid_raw else None,
            tier_preset=str(arguments.get("tier_preset", "balanced")),
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "gecko_memory_save":
        result = await _run_memory_save(
            scope_type=str(arguments["scope_type"]),
            scope_id=str(arguments["scope_id"]),
            entry_type=str(arguments["entry_type"]),
            value=dict(arguments.get("value") or {}),
            key=arguments.get("key"),
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "gecko_memory_recall":
        rows = await _run_memory_recall(
            scope_type=str(arguments["scope_type"]),
            scope_id=str(arguments["scope_id"]),
            entry_type=arguments.get("entry_type"),
            key=arguments.get("key"),
            limit=int(arguments.get("limit", 20) or 20),
        )
        return [TextContent(type="text", text=json.dumps(rows, indent=2))]

    if name == "gecko_memory_search":
        rows = await _run_memory_search(
            scope_type=str(arguments["scope_type"]),
            scope_id=str(arguments["scope_id"]),
            query=str(arguments["query"]),
            top_k=int(arguments.get("top_k", 5) or 5),
        )
        return [TextContent(type="text", text=json.dumps(rows, indent=2))]

    if name == "gecko_memory_query":
        rows = await _run_memory_query(
            scope_type=str(arguments["scope_type"]),
            scope_id=str(arguments["scope_id"]),
            entry_type=arguments.get("entry_type"),
            since=arguments.get("since"),
            k=int(arguments.get("k", 20) or 20),
            query=arguments.get("query"),
        )
        return [TextContent(type="text", text=json.dumps(rows, indent=2))]

    if name == "gecko_resume":
        result = await _run_resume(
            project_id=str(arguments["project_id"]),
            days=int(arguments.get("days", 30) or 30),
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "gecko_project_economics":
        result = await client.get_project_economics(project_id=str(arguments["project_id"]))
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    raise ValueError(f"unknown tool: {name}")


async def _run_classify(*, idea: str) -> dict[str, Any]:
    """Call `gecko_core.classify.classify_idea_with_scores`.

    Free path — bypasses GeckoAPIClient / x402. We import lazily so the
    embedder + numpy aren't pulled into the MCP startup path for users
    who never invoke the classifier.
    """
    from gecko_core.classify import classify_idea_with_scores

    selected, scores = await classify_idea_with_scores(idea)
    return {"categories": selected, "scores": scores}


async def _run_precedents(*, idea: str, top_k: int) -> list[dict[str, Any]]:
    """Embed `idea` and retrieve top-K Gecko flywheel precedents."""
    from gecko_core.ingestion.embedder import embed
    from gecko_core.sessions.store import SessionStore

    vecs, _tokens = await embed([idea])
    if not vecs:
        return []
    store = SessionStore.from_env()
    rows = await store.retrieve_gecko_precedent(embedding=vecs[0], limit=top_k)
    return [r.model_dump(mode="json") for r in rows]


def _route_uses_local_fallback(api_url: str | None) -> bool:
    """True when the MCP should bypass the API and call gecko_core directly.

    Localhost / 127.0.0.1 / unset → local-dev mode (call core directly so a
    developer without a running gecko-api still gets answers). Anything else
    forwards through the API so production gets x402-paid routing.
    """
    if not api_url:
        return True
    lowered = api_url.lower()
    return "localhost" in lowered or "127.0.0.1" in lowered


async def _run_route(
    *,
    client: GeckoAPIClient,
    prompt: str,
    task_hint: str,
    max_cost_usd: float,
    prefer_premium: bool,
    tier_preset: str = "balanced",
) -> dict[str, Any]:
    """Forward to gecko-api `/route`, or call the core directly in local dev.

    S4-ROUTE-02: production MCP forwards to gecko-api so x402 payment goes
    through frames.ag wallet. Local dev (no GECKO_API_URL or pointed at
    localhost) falls back to a direct `gecko_core.routing.route` call so a
    developer can iterate without booting the whole API.

    Validates the task_hint against the matrix's enum so we surface a clean
    ValueError instead of an upstream KeyError or a network round-trip.
    """
    import os

    from gecko_core.routing.matrix import ROUTING_MATRIX

    if task_hint not in ROUTING_MATRIX:
        raise ValueError(f"task_hint must be one of {sorted(ROUTING_MATRIX)}; got {task_hint!r}")

    api_url = os.environ.get("GECKO_API_URL")
    if _route_uses_local_fallback(api_url):
        from gecko_core.routing import route

        result = await route(
            prompt,
            task_hint=task_hint,
            max_cost_usd=max_cost_usd,
            prefer_premium=prefer_premium,
        )
        return result.model_dump(mode="json")

    return await client.route(
        prompt,
        task_hint=task_hint,
        max_cost_usd=max_cost_usd,
        prefer_premium=prefer_premium,
        tier_preset=tier_preset,
    )


async def _run_scaffold(*, session_id: str, output_dir: str | None) -> dict[str, Any]:
    """Generate a 3-file scaffold bundle from a Pro tier session.

    Free path — bypasses GeckoAPIClient / x402 (the user already paid for
    the Pro debate). Lazy import keeps OpenAI / supabase out of the MCP
    startup path for users who never invoke this tool.
    """
    from pathlib import Path
    from uuid import UUID

    from gecko_core.orchestration.scaffold import (
        KillVerdictError,
        ScaffoldError,
        SessionNotFoundError,
        SessionNotReadyError,
        generate_scaffold,
    )

    out_dir = Path(output_dir) if output_dir else Path.cwd()
    try:
        result = await generate_scaffold(UUID(session_id), out_dir)
    except KillVerdictError as exc:
        return {"error": "kill_verdict", "message": str(exc)}
    except SessionNotFoundError as exc:
        return {"error": "session_not_found", "message": str(exc)}
    except SessionNotReadyError as exc:
        return {"error": "session_not_ready", "message": str(exc)}
    except ScaffoldError as exc:
        return {"error": "scaffold_failed", "message": str(exc)}

    return {
        "session_id": str(result.session_id),
        "paths": [str(p) for p in result.paths],
        "tokens_used": result.tokens_used,
        "summary": result.summary,
    }


async def _run_advise(*, session_id: str, voice: str, tier_preset: str) -> dict[str, Any]:
    """Single-voice advisor call (FREE in Sprint 4).

    Errors are surfaced as ``{error, message}`` payloads (matching the
    scaffold tool's pattern) so MCP clients don't need to parse stack
    traces from a tool exception.
    """
    from uuid import UUID

    from gecko_core.orchestration.advisor import (
        AdvisorSessionNotFoundError,
        generate_voice,
    )
    from gecko_core.routing.catalog import Tier

    try:
        tier = Tier(tier_preset)
    except ValueError:
        return {"error": "bad_tier", "message": f"unknown tier_preset {tier_preset!r}"}
    try:
        result = await generate_voice(UUID(session_id), voice, tier_preset=tier)
    except AdvisorSessionNotFoundError as exc:
        return {"error": "session_not_found", "message": str(exc)}
    except ValueError as exc:
        return {"error": "bad_voice", "message": str(exc)}
    return result.model_dump(mode="json")


async def _run_plan(*, session_id: str, tier_preset: str) -> dict[str, Any]:
    """Full 5-voice panel.

    S5-API-01: forwards to gecko-api `/plan` (paid, $0.25 via x402) so
    settle goes through the user's frames.ag wallet. Local dev (no
    GECKO_API_URL or pointed at localhost) calls gecko_core directly.
    """
    import os
    from uuid import UUID

    from gecko_core.orchestration.advisor import (
        AdvisorSessionNotFoundError,
        generate_panel,
    )
    from gecko_core.routing.catalog import Tier

    try:
        tier = Tier(tier_preset)
    except ValueError:
        return {"error": "bad_tier", "message": f"unknown tier_preset {tier_preset!r}"}

    api_url = os.environ.get("GECKO_API_URL")
    if _route_uses_local_fallback(api_url):
        try:
            result = await generate_panel(UUID(session_id), tier_preset=tier)
        except AdvisorSessionNotFoundError as exc:
            return {"error": "session_not_found", "message": str(exc)}
        return result.model_dump(mode="json")

    client = _get_client()
    try:
        return await client.plan(session_id=session_id, tier_preset=tier_preset)
    except Exception as exc:
        return {"error": "api_call_failed", "message": f"{type(exc).__name__}: {exc}"}


async def _run_pulse(
    *,
    session_id: str | None = None,
    project_id: str | None = None,
    tier_preset: str = "balanced",
) -> dict[str, Any]:
    """Re-run the panel and surface deltas vs prior pulse.

    Either ``session_id`` or ``project_id`` is required (S5-API-02).
    project_id wins when both are given. Migration 019 persists each
    pulse so subsequent runs compute real deltas across history.
    """
    from uuid import UUID

    from gecko_core.orchestration.advisor import (
        AdvisorSessionNotFoundError,
        run_pulse,
    )
    from gecko_core.routing.catalog import Tier

    if not session_id and not project_id:
        return {
            "error": "missing_id",
            "message": "either session_id or project_id is required",
        }
    try:
        tier = Tier(tier_preset)
    except ValueError:
        return {"error": "bad_tier", "message": f"unknown tier_preset {tier_preset!r}"}
    try:
        result = await run_pulse(
            session_id=UUID(session_id) if session_id else None,
            project_id=UUID(project_id) if project_id else None,
            tier_preset=tier,
        )
    except AdvisorSessionNotFoundError as exc:
        return {"error": "session_not_found", "message": str(exc)}
    except ValueError as exc:
        return {"error": "bad_argument", "message": str(exc)}
    return result.model_dump(mode="json")


async def _run_memory_save(
    *,
    scope_type: str,
    scope_id: str,
    entry_type: str,
    value: dict[str, Any],
    key: Any,
) -> dict[str, Any]:
    """Save a memory entry. Free."""
    from gecko_core.memory import MemoryEntryType, MemoryScope, save

    try:
        et = MemoryEntryType(entry_type)
    except ValueError as exc:
        return {"error": "bad_entry_type", "message": str(exc)}
    if scope_type not in ("project", "session", "user"):
        return {"error": "bad_scope_type", "message": f"unknown scope {scope_type!r}"}
    new_id = await save(
        MemoryScope(type=scope_type, id=scope_id),  # type: ignore[arg-type]
        et,
        value,
        key=str(key) if isinstance(key, str) else None,
    )
    return {"id": str(new_id)}


async def _run_memory_recall(
    *,
    scope_type: str,
    scope_id: str,
    entry_type: Any,
    key: Any,
    limit: int,
) -> list[dict[str, Any]]:
    from gecko_core.memory import MemoryEntryType, MemoryScope, recall

    et: MemoryEntryType | None = None
    if entry_type:
        try:
            et = MemoryEntryType(str(entry_type))
        except ValueError:
            return []
    if scope_type not in ("project", "session", "user"):
        return []
    rows = await recall(
        MemoryScope(type=scope_type, id=scope_id),  # type: ignore[arg-type]
        entry_type=et,
        key=str(key) if isinstance(key, str) else None,
        limit=limit,
    )
    return [
        {
            "id": str(r.id),
            "entry_type": r.entry_type.value,
            "key": r.key,
            "value": r.value,
            "tx_signature": r.tx_signature,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


async def _run_memory_search(
    *,
    scope_type: str,
    scope_id: str,
    query: str,
    top_k: int,
) -> list[dict[str, Any]]:
    from gecko_core.memory import MemoryScope, search

    if scope_type not in ("project", "session", "user"):
        return []
    matches = await search(
        MemoryScope(type=scope_type, id=scope_id),  # type: ignore[arg-type]
        query,
        top_k=top_k,
    )
    return [
        {
            "id": str(entry.id),
            "entry_type": entry.entry_type.value,
            "key": entry.key,
            "value": entry.value,
            "similarity": sim,
            "created_at": entry.created_at.isoformat(),
        }
        for entry, sim in matches
    ]


async def _run_memory_query(
    *,
    scope_type: str,
    scope_id: str,
    entry_type: Any,
    since: Any,
    k: int,
    query: Any,
) -> list[dict[str, Any]]:
    """S6-MINE-01 — structured filter over the memory layer.

    Goes through `MemoryStore.list_by_scope` for the cheap chronological
    path, or `search` when a query string is provided. Returns a flat list
    of JSON-safe dicts.
    """
    from datetime import datetime

    from gecko_core.memory import MemoryEntryType, MemoryScope, recall, search

    if scope_type not in ("project", "session", "user"):
        return []
    et: MemoryEntryType | None = None
    if entry_type:
        try:
            et = MemoryEntryType(str(entry_type))
        except ValueError:
            return []
    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
        except ValueError:
            return []
    scope = MemoryScope(type=scope_type, id=scope_id)  # type: ignore[arg-type]

    if isinstance(query, str) and query.strip():
        matches = await search(scope, query, top_k=k)
        # Filter the post-search results client-side by entry_type / since
        # since the RPC doesn't accept those args. Cheap — we have ≤k rows.
        out: list[dict[str, Any]] = []
        for entry, sim in matches:
            if et is not None and entry.entry_type != et:
                continue
            if since_dt is not None and entry.created_at < since_dt:
                continue
            out.append(
                {
                    "id": str(entry.id),
                    "entry_type": entry.entry_type.value,
                    "key": entry.key,
                    "value": entry.value,
                    "similarity": sim,
                    "created_at": entry.created_at.isoformat(),
                }
            )
        return out

    rows = await recall(scope, entry_type=et, limit=k, since=since_dt)
    return [
        {
            "id": str(r.id),
            "entry_type": r.entry_type.value,
            "key": r.key,
            "value": r.value,
            "tx_signature": r.tx_signature,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


async def _run_resume(*, project_id: str, days: int) -> dict[str, Any]:
    from gecko_core.memory.resume import build_resume

    resume = await build_resume(project_id, days=days)
    return resume.model_dump(mode="json")


def _run_available_sources() -> list[dict[str, Any]]:
    """Return the static source catalog as JSON-safe dicts."""
    from dataclasses import asdict

    from gecko_core.sources import available_sources

    return [asdict(e) for e in available_sources()]


async def serve() -> None:
    """Run the MCP server over stdio.

    Self-warming: brings up ClawRouter on demand if it isn't already
    reachable (and the user didn't override GECKO_LLM_ENDPOINT). The proxy
    is torn down when stdio closes.
    """
    async with warm_clawrouter(), stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
