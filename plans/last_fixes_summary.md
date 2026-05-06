Report: LLM client, routing, and embedding-dimension work (2026-05-05)
1. Context
Work started from a laptop-freeze during a full pytest run, then refocused on advisor/Pro reliability and empty CEO output. Along the way we migrated high-traffic call sites to a streaming llm_client, adjusted the model catalog (Kimi-related failures), and fixed Supabase pgvector vs default embedder dimension mismatch (1536 vs 1024).

2. Root causes we proved
2.1 Kimi K2.6 + streamed advisor path
Observed in logs:

finish=length, completion_tokens=4000, content_len=0
Not a generic “15s timeout”: wall time ~41s; OPENROUTER_TIMEOUT_S was not the limiter.
Interpretation: reasoning-style routing can consume the visible budget in streams where delta.content stays empty while tokens still accrue — aggregation correctly ignores hidden reasoning; result reads as “null / empty answer.”

2.2 Precedent + memory RPC failures
Errors:

different vector dimensions 1536 and 1024 (precedent RPC)
expected 1536 dimensions, not 1024 (memory insert)
Cause: Default embedder (EMBED_PROVIDER=voyage, 1024) did not match Supabase migrations (vector(1536) for precedent/memory/chunk RPCs).

2.3 RAG / pulse after Postgres-only embed fix
Using only OpenAI 1536 query embeddings would break Mongo chunk stores: ingest typically stores Voyage 1024 on GECKO_CHUNK_STORE=mongo. Query vectors must match ingest space.

3. What we implemented (summary)
3.1 Central LLM client (llm_client.py)
Streaming completions with explicit timeouts, stall guard, LLMTruncationError / LLMStalledError.
build_async_client with httpx timeouts (OPENROUTER_* env knobs).
Wired through basic, post_processors, refine, judges/synth, workflows.ask, advisor/agents, routing as discussed in the migration thread.
3.2 Model catalog (option A — no GPT‑4.1 Mini everywhere)
To avoid Kimi’s invisible-token burn on plain-text advisor paths:

Cell	Change
planning × balanced
Kimi → Gemini 3 Flash (google/gemini-3-flash-preview)
complex_coding × balanced
Kimi → DeepSeek V4 Pro
code_review × budget
Kimi → DeepSeek V4 Flash
Tests updated (routing/test_catalog.py, advisor matrix comments, etc.).

3.3 Advisor tests vs streaming API
_FakeOpenAI in test_advisor.py was updated so chat.completions.create returns an async chunk stream (MagicMocks), delegating to the same with_raw_response-style canned content so existing assertions on with_raw_response.calls still work.

3.4 Postgres-aligned embeddings
Added embed_for_postgres_vector in gecko_core.ingestion.embedder:

Always text-embedding-3-small / 1536 via OPENAI_API_KEY, independent of EMBED_PROVIDER.
Used for:

Advisor load_context (flywheel precedents)
workflows V1 precedent embedding + _retrieve_pro_precedents
memory/embedder.embed_text (journal memory)
flywheel.write_precedent (no longer incorrectly depended on embed(..., client=openai_client) while EMBED_PROVIDER=voyage)
Supabase-only RAG question embedding where chunks live in Postgres
Memory tests updated 1024 → 1536 in stubs.

3.5 Chunk-store–aware RAG and pulse
rag_query: mongo → embed([question]); supabase → embed_for_postgres_vector. Cost uses embed_model vs POSTGRES_EMBED_MODEL accordingly.
pulse_engine._fresh_citations: same branching for match_chunks_windowed.
3.6 Doctor (gecko-mcp doctor)
When EMBED_PROVIDER=voyage and SUPABASE_URL is set:

New check embed:openai_for_postgres_ann: OPENAI_API_KEY required for Postgres ANN paths.
Docs / OPTIONAL_HINTS updated.
tests/mcp/test_doctor.py extended (including voyage + Supabase without OpenAI → fail).
4. What we executed (verification)
Step	Result
uv run pytest packages/gecko-core/tests/orchestration/
92 passed (after advisor stub fix)
pytest memory + test_doctor + advisor
47 passed (memory), doctor suite green after OPENAI merges
bb advise on session 0a00b4a7-… (CEO, balanced)
Success: Gemini Flash, finish=stop, content_len ~2825, ~11s
Live env
set -a; source .env; set +a — keys not pasted into chat
Pre-fix logs showed llm.stream with finish=length, content_len=0 for Kimi; post-catalog + embed fixes, CEO produced full markdown.

5. Residual / operator notes
OPENAI_API_KEY is required for precedent / memory / Supabase chunk ANN whenever SUPABASE_URL is set and default ingest is Voyage — doctor encodes this.
Full pytest on the whole repo can still stress the machine (tiktoken first download, many imports); prefer targeted suites or prime tiktoken once.
uv run mypy on touched packages before merge is still the repo checklist; not fully run in the last turn.
6. Suggested quick re-check (you)
set -a; source .env; set +a
LOG_LEVEL=INFO uv run bb advise <SESSION_ID> --voice ceo --tier-preset balanced
Confirm: no 1536/1024 warnings; journal_voice succeeds if journaling is on.

If you want this as a committed doc (e.g. docs/diagnostics/2026-05-05-embedding-and-advisor-fixes.md), say so — you previously preferred not adding markdown unless asked.
