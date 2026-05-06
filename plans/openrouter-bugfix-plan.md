OpenRouter Review + Voyage RAG + Wallet-only MCP

Findings (audit)

A. OpenRouter — server-side hardening

OpenRouter is centralized in [packages/gecko-core/.../pro/router.py](packages/gecko-core/src/gecko_core/orchestration/pro/router.py) and [orchestration/settings.py](packages/gecko-core/src/gecko_core/orchestration/settings.py). Eight AsyncOpenAI call sites. Confirmed root causes for "timing out / results cut before they appear":





No streaming anywhere. Every chat.completions.create is non-streaming. OpenRouter only sends : OPENROUTER PROCESSING keep-alive comments on SSE streams; non-streaming requests on slow models (Kimi K2.6, DeepSeek V3.2) get killed by Cloudflare/ALB timeouts before the body lands (OpenRouter streaming docs).



No explicit timeout= on AsyncOpenAI(...) — falls back to httpx default.



finish_reason only checked in [orchestration/basic.py](packages/gecko-core/src/gecko_core/orchestration/basic.py). Post-processors, refine, judge synth, advisor, ask all parse JSON / text without inspecting length → silent partial JSON → ValidationError → dropped section.



AG2 voice timeout hard-wired to 60s at [pro/__init__.py:68](packages/gecko-core/src/gecko_core/orchestration/pro/__init__.py).



ORCH_MAX_TOKENS_AG2 is dead — declared in settings, no AG2 path reads it.



pro/__init__.py:232 defaults LLM_ROUTER to openai, but pro/router.py defaults it to openrouter — matrix vs base_url mismatch when env unset.



Bypass paths in [gecko-api/main.py:2365](packages/gecko-api/src/gecko_api/main.py) and [gecko-mcp/server.py](packages/gecko-mcp/src/gecko_mcp/server.py) (~L1313, the gecko_review live LLM path) build clients from orch.llm_endpoint / orch.llm_api_key directly — bypass resolve_llm_config().

B. Voyage RAG — silent skip surface

The user reports "sometimes not using Voyage." Confirmed root causes, ranked:





GECKO_RERANKER defaults to "none" at [rag/voyage_rerank.py:49](packages/gecko-core/src/gecko_core/rag/voyage_rerank.py) — Voyage rerank only fires when env is explicitly "voyage". Most likely not set on prod (or set on some workers but not others).



Hardcoded RERANK_TIMEOUT_S = 2.5 at [rag/voyage_rerank.py:112](packages/gecko-core/src/gecko_core/rag/voyage_rerank.py) — Voyage cold starts can exceed it; silent fallback to cosine slate (rag.voyage_rerank.fallback err=timeout).



Embedding cache hits in [ingestion/pipeline.py](packages/gecko-core/src/gecko_core/ingestion/pipeline.py) look like "Voyage skipped" in logs — by design, but the log line ingest.embed.skipped is misleading.



EMBED_PROVIDER=openai override silently flips embeddings to OpenAI; defaults to voyage but any env override mid-rollout gives mixed-provider corpora.



Per-replica config drift — one task has VOYAGE_API_KEY + GECKO_RERANKER=voyage, another doesn't. ECS rollout window with stale tasks.



No observability — no per-session log line that says "voyage embed=on, rerank=on, batch_size=20"; you only see fallback warnings, not the success path.

C. MCP wallet-only architecture (the user's correction)

Per [README.md:49](README.md) and [scripts/install.sh:109](scripts/install.sh) the contract is "No API keys, just a wallet" via gecko-mcp wallet new (frames.ag email+OTP) → ~/.agentwallet/config.json → x402-paid calls to api.geckovision.tech.

Today this contract is broken for 7 MCP tools because they call gecko_core directly and need SUPABASE_* + (sometimes) OPENROUTER_API_KEY on the user's machine:







MCP tool



Today



Server endpoint



Action





gecko_research



REMOTE (paid)



POST /research



OK





gecko_ask



REMOTE



POST /sessions/{id}/ask



OK





gecko_sources



REMOTE



GET /sessions/{id}/sources



OK





gecko_report



REMOTE (paid)



POST /report/{id}



OK





gecko_project_economics



REMOTE



GET /projects/{id}/economics



OK





gecko_classify



HYBRID



POST /classify



flip to remote-only





gecko_precedents



HYBRID



POST /precedents



flip to remote-only





gecko_route



HYBRID



POST /route



flip to remote-only





gecko_plan



HYBRID



POST /plan



flip to remote-only





gecko_advise



ALWAYS-LOCAL



POST /advise (exists ~L1734)



migrate





gecko_scaffold



ALWAYS-LOCAL



POST /scaffold (exists ~L2152)



migrate





gecko_pulse



ALWAYS-LOCAL



POST /pulse (exists ~L2203)



migrate





gecko_review



ALWAYS-LOCAL (+ live LLM call locally if X402_MODE=live)



POST /review (exists ~L2286)



migrate





gecko_resume



ALWAYS-LOCAL



missing



add endpoint + migrate





gecko_memory_save / recall / search



ALWAYS-LOCAL



only /memory/query exists (~L2709)



add 3 endpoints + migrate





gecko_memory_query



ALWAYS-LOCAL



exists



migrate





gecko_available_sources



ALWAYS-LOCAL (static catalog, no keys needed)



n/a — keep local



OK

gecko-mcp wallet new today is frames.ag email+OTP only — no --network devnet flag. Devnet selection is server-side via X402_NETWORK (default already solana-devnet per [gecko_api/settings.py:47](packages/gecko-api/src/gecko_api/settings.py)).

Strategy

flowchart TD
    A["Track 1: OpenRouter hardening<br>(server-side, gecko-core + gecko-api)"]
    A --> A1["1.1 Central AsyncOpenAI factory + timeouts"]
    A1 --> A2["1.2 Stream long calls with finish_reason"]
    A2 --> A3["1.3 finish_reason guards"]
    A3 --> A4["1.4 AG2 max_tokens + voice timeout env-tunable"]
    A4 --> A5["1.5 Kill direct AsyncOpenAI bypass paths"]

    B["Track 2: Voyage RAG fix"]
    B --> B1["2.1 Default GECKO_RERANKER=voyage"]
    B1 --> B2["2.2 Env-tune RERANK_TIMEOUT_S (default 5s)"]
    B2 --> B3["2.3 Per-session log: provider, rerank_used, fallback_reason"]
    B3 --> B4["2.4 Doctor: warn on EMBED_PROVIDER vs RERANKER mismatch"]

    C["Track 3: Wallet-only MCP migration"]
    C --> C1["3.1 Add /memory/save, /memory/recall, /memory/search, /resume to gecko-api"]
    C1 --> C2["3.2 Flip 7 ALWAYS-LOCAL tools to use GeckoAPIClient"]
    C2 --> C3["3.3 Remove direct gecko_core / SessionStore / OpenAI imports from server.py"]

    D["Track 4: Cursor MCP UX"]
    D --> D1["4.1 .cursor/mcp.json: only {GECKO_API_URL}"]
    D1 --> D2["4.2 README Cursor section: wallet new → doctor → use"]
    D2 --> D3["4.3 Devnet-first: confirm api.geckovision.tech runs solana-devnet"]

    A5 --> C
    B4 --> C
    C3 --> D

Track 1 — OpenRouter hardening (server-side)

Same as before — these fixes affect gecko-core (used by gecko-api in production and by bb research for maintainers).

1.1 Single LLM client factory

New module packages/gecko-core/src/gecko_core/orchestration/llm_client.py:





build_async_client(cfg: LLMClientConfig) -> AsyncOpenAI with explicit timeout=httpx.Timeout(connect=10, read=180, write=30, pool=10) and max_retries=2.



Knobs (server-side env): OPENROUTER_TIMEOUT_S (default 180), OPENROUTER_MAX_RETRIES (default 2), OPENROUTER_STALL_S (default 60).



Replace every AsyncOpenAI(...) site: [basic.py](packages/gecko-core/src/gecko_core/orchestration/basic.py), [post_processors.py:_build_client](packages/gecko-core/src/gecko_core/orchestration/pro/post_processors.py), [refine.py](packages/gecko-core/src/gecko_core/refine.py), [judges/synth.py](packages/gecko-core/src/gecko_core/judges/synth.py), [workflows.py:ask](packages/gecko-core/src/gecko_core/workflows.py), [advisor/agents.py](packages/gecko-core/src/gecko_core/orchestration/advisor/agents.py), [routing/__init__.py:_call_model](packages/gecko-core/src/gecko_core/routing/__init__.py).

1.2 Stream long calls

stream_chat_completion(client, **kwargs) -> StreamedCompletion returning (content, finish_reason, usage, model, provider, gen_id). Raises LLMTruncationError on finish_reason="length". Switch basic, post-processor, refine, judge synth, advisor, ask, routing._call_model to streaming. Stall watchdog: if no chunk for OPENROUTER_STALL_S, abort and surface LLMStalledError.

1.3 finish_reason guards

Add the same guard pattern as [basic.py:323](packages/gecko-core/src/gecko_core/orchestration/basic.py) to: post_processors._call_json, refine, judges/synth, workflows.ask, advisor.agents._call_once.

1.4 AG2 (Pro debate)

In [pro/router.py:llm_config_for_model](packages/gecko-core/src/gecko_core/orchestration/pro/router.py): thread max_tokens=get_orchestration_settings().max_tokens_ag2 and timeout=int(env.get("OPENROUTER_TIMEOUT_S", "180")) into the entry. In [pro/__init__.py](packages/gecko-core/src/gecko_core/orchestration/pro/__init__.py): _VOICE_TIMEOUT_SECONDS = float(os.environ.get("ORCH_VOICE_TIMEOUT_S", "120")); fix LLM_ROUTER default mismatch on L232.

1.5 Kill bypass paths





[gecko-api/main.py:2365](packages/gecko-api/src/gecko_api/main.py): use build_async_client(resolve_llm_config()).



[gecko-mcp/server.py](packages/gecko-mcp/src/gecko_mcp/server.py) ~L1313 (gecko_review live LLM path): same swap — but this whole path goes away in Track 3.

Track 2 — Voyage RAG fix

2.1 Flip the rerank default

[packages/gecko-core/src/gecko_core/rag/voyage_rerank.py:49](packages/gecko-core/src/gecko_core/rag/voyage_rerank.py):

def _flag_enabled() -> bool:
    return (os.environ.get("GECKO_RERANKER") or "voyage").strip().lower() == "voyage"

Changes the default from "none" to "voyage". Operators that explicitly want it off set GECKO_RERANKER=none. Also push GECKO_RERANKER=voyage into [infra/ecs-stack.yml](infra/ecs-stack.yml) SSM list and [.env.example](.env.example).

2.2 Env-tune the rerank timeout

[voyage_rerank.py](packages/gecko-core/src/gecko_core/rag/voyage_rerank.py): replace hardcoded RERANK_TIMEOUT_S = 2.5 with RERANK_TIMEOUT_S = float(os.environ.get("VOYAGE_RERANK_TIMEOUT_S", "5.0")). 5s is a safe default for Voyage rerank-2 cold starts; ops can tune.

2.3 Observability

In [rag/query.py:rag_query](packages/gecko-core/src/gecko_core/rag/query.py), emit one structured log line per call:

rag.query session=<id> embed_provider=voyage embed_tokens=… chunks_in=20 chunks_out=12 rerank_enabled=true rerank_used=true rerank_fallback_reason=none latency_ms=…

rerank_fallback_reason is one of: none, flag_off, no_key, import_failed, timeout, api_error, empty_results. This single line lets ops grep for rerank_used=false and immediately see why.

2.4 Doctor mismatch warning

[packages/gecko-mcp/src/gecko_mcp/doctor.py:check_voyage_api_key](packages/gecko-mcp/src/gecko_mcp/doctor.py): WARN (not FAIL) when EMBED_PROVIDER=voyage but GECKO_RERANKER != voyage (or vice versa) — operator most likely intended both. Already runs in non-thin doctor; thin-client doctor unaffected (the user doesn't see this).

Track 3 — Wallet-only MCP migration

3.1 New gecko-api endpoints (parity)

Add to [packages/gecko-api/src/gecko_api/main.py](packages/gecko-api/src/gecko_api/main.py):





POST /memory/save — wraps gecko_core.memory.save. Free.



POST /memory/recall — wraps MemoryStore.recall. Free.



POST /memory/search — wraps semantic search. Free or x402-low ($0.005).



POST /resume — wraps gecko_core.resume.build_resume. Free.

All four follow the existing pattern of bearer-auth via frames apiToken (api_client.py:_paid_post for paid endpoints; free endpoints use the same auth header via _get with Authorization: Bearer {apiToken}).

3.2 Flip ALWAYS-LOCAL tools to remote

In [packages/gecko-mcp/src/gecko_mcp/server.py](packages/gecko-mcp/src/gecko_mcp/server.py), rewrite these tool dispatchers to call GeckoAPIClient exclusively:





gecko_advise (L711–717 → use existing client.advise)



gecko_scaffold (L703–708 → call new client.scaffold against existing POST /scaffold)



gecko_pulse (L726–734 → client.pulse)



gecko_review (L783–790 → client.review; delete the local _build_review_llm_caller at L1295–1326 — that's the one that pulls OPENAI_API_KEY onto the user's machine)



gecko_resume (L776–781 → new client.resume)



gecko_memory_save / recall / search / query (L1083–1243 → corresponding client.memory_*)

In [packages/gecko-mcp/src/gecko_mcp/api_client.py](packages/gecko-mcp/src/gecko_mcp/api_client.py): add scaffold, advise, pulse, review, resume, memory_save, memory_recall, memory_search methods (matching the existing route / plan shape).

Drop hybrid behavior entirely. Delete _route_uses_local_fallback from [gecko-mcp/server.py:861](packages/gecko-mcp/src/gecko_mcp/server.py). Every tool except gecko_available_sources (static catalog) goes through GeckoAPIClient. Maintainers iterate via bb CLI when they need direct gecko_core access; MCP is always remote.

3.3 Remove local-mode imports

After 3.2, delete these imports from tool dispatchers in [gecko-mcp/server.py](packages/gecko-mcp/src/gecko_mcp/server.py):





from gecko_core.orchestration.scaffold import generate_scaffold



from gecko_core.orchestration.advisor import generate_voice / generate_panel



from gecko_core.memory ...



from gecko_core.resume ...



from gecko_core.review.builder ...



from openai import AsyncOpenAI (the live-LLM caller branch in gecko_review)

Net: a fresh uvx gecko-mcp@latest serve with only GECKO_API_URL and ~/.agentwallet/config.json works for every tool. Plug-and-play for Claude Code, Cursor, Codex, etc.

Track 4 — Cursor MCP UX (devnet first)

4.1 Workspace .cursor/mcp.json

Exactly the user's snippet, no env keys:

{
  "mcpServers": {
    "gecko": {
      "command": "uvx",
      "args": ["gecko-mcp@latest", "serve"],
      "env": {
        "GECKO_API_URL": "https://api.geckovision.tech"
      }
    }
  }
}

Ship as [.cursor/mcp.json](.cursor/mcp.json) (workspace) for committers and document the same JSON for ~/.cursor/mcp.json (global) for end users.

4.2 README — Cursor section

Add a "Path A — Cursor" subsection mirroring the Claude Code section in [README.md](README.md):

# 1. Install
curl -fsSL https://app.geckovision.tech/install.sh | bash

# 2. Wallet (Email + OTP, ~30 seconds)
gecko-mcp wallet new
#    → email: ernanibmurtinho@gmail.com
#    → check inbox, paste 6-digit OTP
#    → writes ~/.agentwallet/config.json (chmod 600)

# 3. Verify
gecko-mcp doctor   # expect "doctor: OK"

# 4. Add to Cursor (~/.cursor/mcp.json), restart, then in chat:
#    Use gecko_research to validate: a hotel guide for Brazil

Note in the README: same email always resolves to the same wallet (frames.ag deterministic), so re-running gecko-mcp wallet new rotates the apiToken without losing funds.

4.3 Devnet-first

Confirm api.geckovision.tech is running with X402_NETWORK=solana-devnet ([gecko_api/settings.py:47](packages/gecko-api/src/gecko_api/settings.py) default is already devnet). Action items:





Verify SSM parameter /X402_NETWORK on the prod ECS task is solana-devnet (or solana-mainnet, whichever you intend for now); document in [infra/push-ssm-params.sh](infra/push-ssm-params.sh).



Add a "fund your devnet wallet" callout to README pointing at a Solana devnet USDC faucet (e.g. https://spl-token-faucet.com or solana airdrop for SOL + USDC mint helper script).



gecko-mcp doctor already shows the resolved network in the x402 row — no code change needed; verify the message reads "solana-devnet" cleanly.

Verify

# Server-side fixes
LLM_ROUTER=openrouter OPENROUTER_API_KEY=… GECKO_RERANKER=voyage VOYAGE_API_KEY=… \
  uv run bb research --idea "a hotel guide for Brazil" --tier basic --yes
LLM_ROUTER=openrouter OPENROUTER_API_KEY=… GECKO_RERANKER=voyage VOYAGE_API_KEY=… \
  uv run bb research --idea "a hotel guide for Brazil" --tier pro  --yes --tier-preset budget
uv run pytest packages/gecko-core/tests packages/gecko-api/tests packages/gecko-mcp/tests

# Wallet-only MCP smoke (no env keys whatsoever)
unset OPENROUTER_API_KEY VOYAGE_API_KEY SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY
gecko-mcp wallet new --email ernanibmurtinho@gmail.com   # OTP
gecko-mcp doctor                                          # expect "doctor: OK"
# Then in Cursor:
#   Use gecko_research to validate: a hotel guide for Brazil
#   Use gecko_advise on session <id> for voice market
#   Use gecko_memory_save scope=project key=hello content=world
# All three must work with ZERO env keys on the client.

Add regression tests:





tests/orchestration/test_streaming_truncation.py — fakes partial JSON stream + finish_reason=length and asserts LLMTruncationError bubbles with gen_id.



tests/mcp/test_wallet_only.py — boots MCP with only GECKO_API_URL set, monkeypatches GeckoAPIClient to record outbound calls, runs each tool, asserts no direct gecko_core / openai / supabase imports execute.



tests/rag/test_voyage_default_on.py — GECKO_RERANKER unset → _flag_enabled() returns True.

Out of scope





Frames.ag self-custody flow (wallet_self_custody.py) — separate sprint.



A gecko-mcp wallet new --network devnet flag (server-side env already controls network).



Eval harness migration off OPENAI_API_KEY in tests/eval/runner.py.



Price tuning for the new memory / resume endpoints (default to free; revisit when usage data shows).


