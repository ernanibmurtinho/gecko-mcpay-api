# Sprint Plan: gecko-mcp Plug-and-Play (S22–S28)

**Goal:** A user who has never heard of Gecko runs `curl -fsSL https://app.geckovision.tech/install.sh | bash`, follows 5 steps, and is running `gecko_research` within 5 minutes. Zero API keys, zero local services, zero env configuration — just a wallet.

**Target UX:**
1. Paste one line into Claude Code → `Read https://app.geckovision.tech/skill.md and follow the instructions`
2. Wallet appears (Email + 6-digit OTP, ~30 seconds, powered by frames.ag, no browser detour)
3. Fund $5 USDC (Coinbase Onramp, PIX, card, bank — covers ~50 sessions)
4. Validate any idea: `Use gecko_research to validate: <your idea>` → cited business plan, validation report, V1/V2/V3 PRD
5. Build it: Five sub-agents take the PRD and scaffold the V1 with `npx create-next-app`. You ship.

---

## Architectural Decisions

### ClawRouter: Option C — dev-only, never required for production

`gecko-mcp` is a pure HTTP client of `gecko-api`. All LLM calls execute server-side.
`warm_clawrouter()` currently runs on the user's machine but the user never makes LLM calls — gecko-api does.

**Decision:** Remove `clawrouter_supervisor` from the `gecko-mcp` server startup path entirely. It stays in the codebase as an internal dev tool for gecko-api developers. The MCP client has zero LLM configuration.

### Client vs Server separation (current violations)

`gecko-mcp doctor` currently checks for server-side env vars that external users will never have:
- `SUPABASE_URL` — server-side, should never be on client
- `SUPABASE_SERVICE_ROLE_KEY` — server-side secret, definitely not on client
- `TAVILY_API_KEY` — server-side
- `OPENAI_API_KEY` — server-side
- `VOYAGE_API_KEY` — server-side

**The doctor must only check:**
- `gecko-api` reachable at `GECKO_API_URL`
- Wallet present and funded
- MCP registered with Claude Code

Everything else is gecko-api's concern, not the user's.

**Other violations in `server.py`:**
- `_run_classify` and `_run_precedents` import `gecko_core` directly → must go behind gecko-api endpoints
- `_run_available_sources` calls a gecko-core function directly
- Local-dev fallback paths import gecko-core (must be gated on `GECKO_API_URL=localhost` only)

These make `gecko-mcp` pull OpenAI SDK, numpy, etc. on the user's machine and inflate install time.

### PyPI publish strategy

Publish `gecko-core` and `gecko-mcp` to PyPI, version-locked. Once gecko-core dependency is trimmed (violations above fixed), mark it as optional `[dev]` extra so production install stays light.

---

## Sprint 22 — Architectural cleanup: zero client-side LLM

**Goal:** `gecko-mcp serve` never touches ClawRouter, Node.js, or any LLM configuration. Doctor only checks client-relevant items.

| ID | Title | Owner | Effort | Acceptance Criteria |
|----|-------|-------|--------|---------------------|
| S22-MCP-01 | Remove `warm_clawrouter` from MCP serve path | software-engineer | S | `server.py` no longer imports or calls `clawrouter_supervisor`. `serve()` entrypoint has no Node check. `gecko-mcp serve` starts in under 2s with no Node.js on the machine. |
| S22-MCP-02 | Move `gecko_classify` behind gecko-api | software-engineer | M | `_run_classify` deleted from `server.py`. `GeckoAPIClient.classify()` added. Calls `POST /classify` on gecko-api. Test: mock API call succeeds. |
| S22-MCP-03 | Move `gecko_precedents` behind gecko-api | software-engineer | M | Same pattern as S22-MCP-02. `_run_precedents` removed. `GeckoAPIClient.precedents()` added. Embed + pgvector runs server-side. |
| S22-MCP-04 | Audit and trim gecko-mcp's `gecko-core` dependency | software-engineer | S | After S22-MCP-02/03, enumerate remaining gecko-core imports. Mark as optional `[dev]` extra if only local-dev paths remain. `uv tool install gecko-mcp` completes under 10s on fresh machine. |
| S22-MCP-05 | Remove ClawRouter from `install.sh` prereq check | software-engineer | S | Lines checking for Node/ClawRouter removed. Prerequisites become: Python 3.11+, uv, Claude Code CLI. Script never mentions ClawRouter in user-facing output. |
| S22-MCP-06 | Rewrite `gecko-mcp doctor` for client-only checks | software-engineer | M | Doctor checks: (1) gecko-api reachable, (2) wallet present, (3) balance, (4) MCP registered. ALL server-side env checks (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `TAVILY_API_KEY`, `OPENAI_API_KEY`, `VOYAGE_API_KEY`, `DEEPGRAM_API_KEY`) removed from doctor. ClawRouter unreachable is no longer shown. `llm:matrix` ImportError no longer shown. Acceptance: on a fresh machine with only `GECKO_API_URL` set, doctor passes. |
| S22-CORE-01 | gecko-api: set `GECKO_LLM_ENDPOINT` for production | software-engineer | S | gecko-api deployment env sets `GECKO_LLM_ENDPOINT=https://openrouter.ai/api/v1`. The `http://localhost:8402/v1` default only applies in local gecko-core dev. gecko-mcp doctor does not mention LLM endpoint at all. |

**Dependencies:** None. First sprint, unblocked.

---

## Sprint 23 — PyPI publish + zero-install path

**Goal:** `uvx gecko-mcp@latest serve` resolves from PyPI and starts successfully.

| ID | Title | Owner | Effort | Acceptance Criteria |
|----|-------|-------|--------|---------------------|
| S23-PUB-01 | Confirm PyPI name availability | software-engineer | S | Verify `gecko-mcp` and `gecko-core` are not taken on PyPI. If taken, decide on alternative names before proceeding. One-way decision. |
| S23-PUB-02 | Publish `gecko-core` to PyPI | software-engineer | M | `gecko-core` published under `gecko-core`. Version matches repo. `uv add gecko-core` resolves without git URL. (May be deferred if gecko-core dep becomes optional in S22-MCP-04.) |
| S23-PUB-03 | Publish `gecko-mcp` to PyPI | software-engineer | M | `gecko-mcp` published. `uvx gecko-mcp@latest serve` resolves, installs, starts on fresh machine. Version: `0.2.0` (breaking: removes ClawRouter startup from prior `0.1.11`). |
| S23-PUB-04 | CI: automated PyPI publish on tag | software-engineer | M | GitHub Actions: on push of `gecko-mcp/v*` tag, `uv build` + publish. Secret `PYPI_TOKEN` in repo. No manual publish step. |
| S23-PUB-05 | Update `install.sh` to use PyPI | software-engineer | S | `UV_PACKAGE` changes from `git+${GECKO_MCP_REPO}@...` to `gecko-mcp`. Pre-PyPI warning comment removed. Acceptance: `install.sh` on fresh Ubuntu 24.04 installs from PyPI. |
| S23-PUB-06 | Remove PyPI caveat from `skill.md` Step 2 | software-engineer | S | The `(Until published to PyPI, use: ...)` parenthetical removed. `uv tool install gecko-mcp` is the single canonical command. |

**Dependencies:** S22-MCP-01 through S22-MCP-04 must complete before publish — do not publish with ClawRouter startup call.

---

## Sprint 24 — gecko_report diagnosis + MCP tool registry audit

**Goal:** All tools in `server.py` are confirmed visible in Claude Code; gecko_report is callable end-to-end.

| ID | Title | Owner | Effort | Acceptance Criteria |
|----|-------|-------|--------|---------------------|
| S24-MCP-01 | Diagnose gecko_report absence in Claude Code tool list | software-engineer | S | Root cause documented. `gecko_report` appears in Claude Code tool list after fresh `gecko-mcp serve` start. (Note: tool exists at server.py line 595-617 and 797-805 — issue is likely stale installed binary vs local source.) |
| S24-MCP-02 | Fix `api_client.report()` format query param bug | software-engineer | S | `format` sent as `?format=...` query param, not JSON body. `_parse_json_object` handles `text/html` content-type without crashing. (Fix already applied in current session — ensure it's in the published version.) |
| S24-MCP-03 | Full tool-registry smoke test | software-engineer | S | Automated test starts `gecko-mcp serve` subprocess, sends MCP `tools/list` over stdio, asserts all expected tool names present. Runs in CI. |

**Dependencies:** S22 complete.

---

## Sprint 25 — gecko-mcp doctor production hardening

**Goal:** `gecko-mcp doctor` gives a green pass on fresh install against production, with actionable failure messages for client-relevant issues only.

| ID | Title | Owner | Effort | Acceptance Criteria |
|----|-------|-------|--------|---------------------|
| S25-DOC-01 | Doctor check: gecko-api reachable | software-engineer | S | `GET https://api.geckovision.tech/healthz`. Pass: API version. Fail: "gecko-api unreachable — check network". Timeout 5s. |
| S25-DOC-02 | Doctor check: wallet present | software-engineer | S | Reads frames.ag config or `~/.gecko/wallet.json`. Pass: truncated address. Fail: "No wallet — run `gecko-mcp wallet new`". |
| S25-DOC-03 | Doctor check: balance sufficient | web3-engineer | S | Queries USDC balance for wallet address. Pass: shows balance. Warn if < $1 (under one basic session). Fail: "Balance 0 — fund at https://app.geckovision.tech/onramp". |
| S25-DOC-04 | Doctor check: MCP registered | software-engineer | S | `claude mcp list | grep gecko`. Pass: registered. Warn if Claude CLI not found. |
| S25-DOC-05 | Clean VM test for doctor | software-engineer | M | GitHub Actions on Ubuntu 24.04 (no pre-installed tooling): `install.sh` → `gecko-mcp doctor` exits 0. Uses funded test wallet in CI secrets. Gates all future releases. |

**Dependencies:** S23-PUB-03 (must be on PyPI for clean VM test).

---

## Sprint 26 — frames.ag wallet onboarding

**Goal:** skill.md and install.sh describe a single wallet path matching product vision (Email + OTP, no seed phrase risk).

| ID | Title | Owner | Effort | Acceptance Criteria |
|----|-------|-------|--------|---------------------|
| S26-W3-01 | Audit frames.ag Email+OTP API availability | web3-engineer | S | Document what `gecko-mcp wallet new` does today vs. product vision. Gap analysis: does frames.ag expose Email+OTP programmatically? |
| S26-W3-02 | Implement frames.ag Email+OTP onboarding | web3-engineer | L | `gecko-mcp wallet new` prompts email → OTP → wallet ready. No browser detour. Under 60s on fresh machine. Doctor shows funded address. |
| S26-W3-03 | Update skill.md Step 3: single path | software-engineer | S | Remove Path A / Path B split. Single flow: `gecko-mcp wallet new` → email → OTP → done. Import is a secondary note. |
| S26-W3-04 | Update install.sh next-steps | software-engineer | S | Remove `Read https://frames.ag/skill.md`. Next steps: `1. gecko-mcp wallet new  2. (Claude Code example)`. |

**Dependencies:** S26-W3-01 before S26-W3-02. S26-W3-03/04 after S26-W3-02.

---

## Sprint 27 — skill.md + install.sh deployed publicly

**Goal:** `https://app.geckovision.tech/skill.md` and `https://app.geckovision.tech/install.sh` return correct files.

| ID | Title | Owner | Effort | Acceptance Criteria |
|----|-------|-------|--------|---------------------|
| S27-FE-01 | Serve `skill.md` at `app.geckovision.tech/skill.md` | frontend-engineer | S | Next.js route returns `instructions/skill.md` with `Content-Type: text/plain`. Stable URL — one-way public commitment. |
| S27-FE-02 | Serve `install.sh` at `app.geckovision.tech/install.sh` | frontend-engineer | S | `curl -fsSL https://app.geckovision.tech/install.sh \| bash` works. |
| S27-FE-03 | CD: auto-update skill.md/install.sh on repo changes | staff-engineer (coordinate) | M | gecko-mcpay-app fetches raw files from `gecko-mcpay-api` main at build time. Merge → rebuild → new content within 10 minutes. |
| S27-FE-04 | Onramp page at `app.geckovision.tech/onramp` | frontend-engineer | M | Coinbase Onramp embedded. Accepts card/bank/PIX. Destination address pre-filled from `?address=<wallet>` query param. |

**Dependencies:** S26-W3-03 (final skill.md content) before S27-FE-01.

---

## Sprint 28 — End-to-end validation

**Goal:** Fresh machine → running `gecko_research` in under 5 minutes.

| ID | Title | Owner | Effort | Acceptance Criteria |
|----|-------|-------|--------|---------------------|
| S28-E2E-01 | CI: install.sh → doctor passes | software-engineer | M | GitHub Actions: no pre-installed tooling → `install.sh` → `gecko-mcp doctor` exits 0. Every push to main. |
| S28-E2E-02 | CI: gecko_research stub run | software-engineer | M | After doctor: calls production gecko-api in stub mode, asserts 200 + `session_id` + `verdict`. No real USDC spent. |
| S28-E2E-03 | Human tester: 5-step flow timed | staff-engineer (coordinate) | S | Team member on fresh machine. Paste skill.md URL → wallet → fund → gecko_research → receive output. Under 5 minutes. Any blocker = P0 before sprint closes. |
| S28-DOC-01 | Update README quickstart | software-engineer | S | README matches final flow. No ClawRouter, no Node, no git+URL. Single path. |

**Dependencies:** All prior sprints complete.

---

## Sprint dependency chain

```
S22 (architecture cleanup + doctor rewrite)
  └── S23 (PyPI publish)
        ├── S24 (tool registry audit) ← can start parallel with S23
        ├── S25 (doctor hardening) ← S25-DOC-05 needs S23-PUB-03
        └── S26 (wallet onboarding)
              └── S27 (public URLs) ← S27-FE-01/02 need S26-W3-03
                    └── S28 (E2E validation)
```

---

## Key risks

| Risk | Mitigation |
|------|-----------|
| PyPI name collision for `gecko-mcp` or `gecko-core` | Check NOW before S23 starts — name reservation is one-way |
| gecko-core dep tree inflates client install | S22-MCP-04 audit must verify wheel size < 10s install |
| frames.ag Email+OTP not yet a stable API | S26-W3-01 audit first; fallback to local keypair if needed |
| `/classify` and `/precedents` routes missing from gecko-api | Verify before S22-MCP-02/03 — implement if stubs |

---

*Opened: 2026-05-04 | Owner: staff-engineer | Tickets: 34 across S22–S28*
