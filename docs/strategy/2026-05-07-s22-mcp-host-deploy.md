# S22-MCP-HOST-01 — Hosted MCP Deploy Plan

**Date:** 2026-05-07. **Owner:** staff-engineer. **Effort:** 1.5 days solo.

## RECOMMENDATION

Mount FastMCP's **Streamable HTTP** transport inside the *existing* `gecko-api` FastAPI app, deploy it as a second route prefix (`/mcp`) on the same ECS Fargate service, and front it with a new CNAME `mcp.geckovision.tech`. The existing per-route `PaymentMiddlewareASGI` paywalls are reused — no new x402 plumbing needed. Stdio MCP keeps shipping unchanged for dogfood.

## WHY (key facts from the audit)

- `gecko-mcp` (`packages/gecko-mcp/src/gecko_mcp/server.py:1361`) is already a **thin HTTP client** of `gecko-api` via `GeckoAPIClient`. Every paid tool already round-trips to `api.geckovision.tech`. A hosted MCP is therefore a **transport adapter**, not a re-implementation.
- `gecko-api/main.py:273-470` already declares x402 RouteConfigs for every paid tool surface: `/research`, `/research/pro`, `/route`, `/route/premium`, `/route/upgrade`, `/plan`, `/review`, `/scaffold`, `/advise`, `/ask`, `/classify`, `/report/:session_id`. The paywall wraps the ASGI app at `main.py:753`. **Reuse, don't rebuild.**
- Deploy infra exists: `Dockerfile`, `infra/ecs-stack.yml`, `infra/deploy.sh`, `infra/push-ssm-params.sh`. ALB → uvicorn:8000 with `/healthz`. Adding a host on the same ALB is a CloudFormation parameter change.
- MCP Python SDK 1.0+ exposes `FastMCP.streamable_http_app()` mountable on Starlette/FastAPI (per modelcontextprotocol/python-sdk docs). The 2025-03-26 spec deprecated raw SSE in favor of Streamable HTTP.
- Tool surface confirmed: 16 tools registered at `server.py:192-633`. Paid surface (per memo): `gecko_research`, `gecko_ask` (paid path), `gecko_classify`, `gecko_plan`, `gecko_advise`, `gecko_route`, `gecko_review`, `gecko_scaffold`, `gecko_report`. Free: `gecko_sources`, `gecko_precedents`, `gecko_available_sources`, `gecko_pulse`, `gecko_memory_*`, `gecko_resume`, `gecko_project_economics`. Matches `reference_pay_sh` plus extras already in core.

## REVERSIBILITY

- **One-way:** the public hostname `mcp.geckovision.tech` (skill manifests will pin it; once Manus/Cursor configs reference it, breaking it breaks installs). Pricing per tool is also one-way after first paid call lands.
- **Two-way:** hosting platform, transport choice between Streamable HTTP variants, mount path inside the API.

## REPO(S) AFFECTED

`gecko-mcpay-api` (server + infra), `gecko-mcpay-skills` (manifest update only). `gecko-mcpay-app` is **not** affected.

## DELEGATE TO

- `software-engineer` — transport mount, tool dispatch wiring, infra/ecs-stack changes
- `web3-engineer` — verify x402 challenge headers survive the MCP-over-HTTP envelope (the `WWW-Authenticate: x402` header must reach the MCP client wrapping the call)
- `product-designer` — terse 402-failure error copy that MCP clients will surface

---

## 1. Transport choice: Streamable HTTP (single endpoint)

**Decision:** Streamable HTTP at `https://mcp.geckovision.tech/mcp` (POST + GET on one path). **Not** raw SSE-only.

**Why:**
- The 2025-03-26 MCP spec deprecates the old HTTP+SSE transport in favor of Streamable HTTP. Cursor 0.42+, Claude Desktop, Windsurf, and Manus all support Streamable HTTP. Building on the new transport future-proofs us.
- Streamable HTTP can fall back to a single JSON response (`Content-Type: application/json`) for short tool calls and open SSE only when the call streams progress. Most Gecko tools are single-shot (return JSON once); we don't actually need long-lived SSE for `gecko_classify`, `gecko_ask`, `gecko_sources`, `gecko_memory_*`.
- For the long calls (`gecko_research`, `gecko_plan`) we *do* want SSE so 30-90s LLM debates don't trip ALB idle timeouts (default 60s — bump to 120s).
- `FastMCP(json_response=True, stateless_http=True)` matches our session model: every tool call already carries `session_id` in its payload; we don't need MCP-level session state.

## 2. Hosting choice: existing ECS Fargate, same service

**Decision:** Mount the MCP Streamable HTTP app at `/mcp` inside the existing `gecko-api` FastAPI process. Add `mcp.geckovision.tech` as a second hostname on the same ALB; route it to the same target group with a Listener Rule that strips/prefixes paths.

**Why:**
- Cold starts are unacceptable (LLM panel = 30-90s; serverless 10s-timeouts kill us). Render free tier sleeps. Vercel times out at 60s. **ECS Fargate is the only viable option** of the candidates.
- The API already holds OpenAI / Voyage / Mongo / Tavily / CDP / Solana keys via SSM (`infra/push-ssm-params.sh`). Standing up a *separate* MCP service would force us to either duplicate SSM params or create a cross-service auth path — both worse than just adding a route.
- ALB cost is sunk. Adding a Route 53 CNAME + ACM SAN is ~$0 incremental.
- If we later need to scale MCP independently, peeling it out is mechanical: separate Dockerfile target, second target group. Two-way door.

**Rejected:**
- Render/Railway/Fly: sleep + timeout + secret-management duplication.
- Standalone EC2: more ops than a second route on Fargate.

## 3. x402 paywall middleware

**Already done.** `PaymentMiddlewareASGI` at `gecko-api/main.py:753` wraps the entire app — including the new `/mcp` mount. The piece that needs verification (web3-engineer): the `WWW-Authenticate: x402` and `402 Payment Required` body must propagate through the MCP Streamable HTTP envelope back to the MCP client. Two paths:

- **Tool-level:** every paid MCP tool's handler does an HTTP call to its corresponding `/research`, `/plan`, etc. route via `GeckoAPIClient`, which already speaks x402 (`packages/gecko-mcp/src/gecko_mcp/api_client.py`). The 402 challenge bubbles up and the tool returns it as a structured error. **This is what already happens for stdio MCP today.** Recommended path.
- **Transport-level:** apply x402 directly on `/mcp` so the MCP request itself is paywalled. **Reject** — it forces a flat per-tool-call price and can't differentiate `$0.01 ask` from `$50 pro research`.

**Per-tool pricing table** (already in `gecko-api/settings.py` and surfaced via `/.well-known/x402`):

| Tool | Route | Price |
|---|---|---|
| `gecko_research` (basic) | `POST /research` | $10.00 |
| `gecko_research` (pro) | `POST /research/pro` | $50.00 |
| `gecko_ask` (post-free-quota) | `POST /ask` | $0.01 |
| `gecko_classify` | `POST /classify` | $0.10 |
| `gecko_plan` | `POST /plan` | $0.25 |
| `gecko_advise` | `POST /advise` | $0.05 |
| `gecko_route` | `POST /route{,/premium,/upgrade}` | $0.02 / $0.05 / $0.10 |
| `gecko_review` (live) | `POST /review` | $0.10 |
| `gecko_scaffold` | `POST /scaffold` | $0.05 |
| `gecko_report` | `POST /report/:session_id` | $0.05 |
| free tools | (no API call or free routes) | $0 |

**Idempotency:** x402 settle returns a `tx_signature`; on retry the MCP tool re-includes the same signature header (CDP/Solana facilitator dedupes on signature). Inherits from existing FastAPI surface; no new work.

## 4. Tool surface contract

Lock the hosted MCP to **the exact same 16 tools** declared in `server.py:192-633`. No subset, no superset. The skill manifest at `app.geckovision.tech/skill.md` will pin tool names + input schemas to whatever `tools/list` returns post-deploy. **Add a contract test:** a snapshot of `tools/list` JSON; CI fails if it drifts without an explicit version bump (replicate the `test_payment_mode_consistency.py` pattern from CLAUDE.md project conventions).

Output schemas are already defined by the `gecko_core` Pydantic models (`ResearchResult`, `AskResult`, `AdvisorPanel`, `Verdict`, etc.) — exposed verbatim as JSON.

## 5. Sequencing (1.5 days)

| # | Ticket | Effort | Owner |
|---|---|---|---|
| 1 | `S22-MCP-HOST-02` Refactor `gecko_mcp.server` to a `FastMCP` instance (port the 16 `@server.list_tools` + `@server.call_tool` branches into `@mcp.tool()` decorators). Keep `serve()` stdio entrypoint working by calling `mcp.run("stdio")`. | 4h | software-engineer |
| 2 | `S22-MCP-HOST-03` In `gecko_api/main.py`, mount `mcp.streamable_http_app()` at `/mcp` and wire `mcp.session_manager.run()` into the existing `lifespan` context. | 2h | software-engineer |
| 3 | `S22-MCP-HOST-04` Tools-list snapshot test in `packages/gecko-mcp/tests/test_tools_contract.py`. | 30m | software-engineer |
| 4 | `S22-MCP-HOST-05` ALB Listener Rule: add `mcp.geckovision.tech` as SAN on existing ACM cert (or new cert), Route 53 CNAME, ECS task `Origin` validation env-var. Bump ALB idle timeout to 120s. | 2h | software-engineer |
| 5 | `S22-MCP-HOST-06` web3-engineer audit: 402 challenge round-trip via Streamable HTTP using `pay.sh` MCP client. Recorded fixture per Pattern C. | 2h | web3-engineer |
| 6 | `S22-MCP-HOST-07` Smoke from Claude Desktop, Cursor, Manus configs (just paste `{"url": "https://mcp.geckovision.tech/mcp"}`). | 1h | staff-engineer |
| 7 | `S22-MCP-HOST-08` Update `gecko-mcpay-skills/skill.md` to lead with the hosted endpoint; keep the stdio fallback section for dev. | 1h | product-designer |

**Total:** ~12.5h. Two work-days max.

## 6. Falsifiers (kill switches)

1. **Streamable-HTTP support gap** — if Cursor/Manus haven't yet shipped the new transport client (they may still be on legacy SSE), the user base shrinks to Claude Desktop only. Mitigation: keep `FastMCP` backward-compat layer that also speaks the deprecated `/sse` + `/messages` pair. ~2h add-on if needed.
2. **x402 challenge does not propagate through Streamable HTTP** — if the MCP client wraps tool errors in a way that loses `WWW-Authenticate`, the wallet can't satisfy the challenge. This is the web3-engineer audit (#5 above). If it fails, fallback is **out-of-band quote**: tool returns `{error: "402", price: 0.10, route: "/classify"}` and the client makes a direct HTTP call. Adds ~3h.
3. **ALB 60s idle timeout kills `gecko_research` (pro)** — verified concern. Mitigation in #4.
4. **Tool-payload size** — `/research` returns large JSON (sources + chunks + plan). Existing body-size cap test `test_body_size_limit.py` already enforces a ceiling; confirm it doesn't trip the MCP envelope.
5. **Origin/DNS-rebinding hardening** — the MCP spec mandates `Origin` validation. FastMCP enforces this; allowlist must include the MCP-client `Origin` strings (`null` for desktop apps). Misconfig blocks all calls.

## 7. Backwards compat

- Stdio MCP **must** keep working. `serve()` (`server.py:1347`) is what `bb` and `gecko-mcp` CLI use locally. Refactor in ticket #1 keeps both transports in one `FastMCP` instance — `mcp.run("stdio")` for `gecko-mcp` script entry, `mcp.streamable_http_app()` mounted for hosted. This is the SDK's intended pattern.
- `.mcp.json` (current points at `api.geckovision.tech`) keeps working for power users.
- `GECKO_API_URL` env behavior unchanged for local dev.

## 8. Out of scope

- Multi-tenancy beyond per-wallet (no per-org accounts, quotas, or admin)
- Credit pre-funding / subscription billing (x402 is per-call only for V1)
- Rate limiting beyond what x402 + existing slowapi already do
- Observability dashboard (CloudWatch + access log filter is what we have)
- Voyage/Mongo cutover or any retrieval-layer change (S19 territory)
- A separate `mcp-api` service with its own deploy pipeline

---

## SUMMARY (5 lines)

1. The hosted MCP is a transport adapter, not a new service — `gecko-mcp` already round-trips to `api.geckovision.tech` for every paid tool.
2. Mount FastMCP's Streamable HTTP app at `/mcp` inside the existing `gecko-api` FastAPI process; same Fargate service, same x402 middleware.
3. Add `mcp.geckovision.tech` CNAME + ACM SAN on the existing ALB; bump idle timeout to 120s for pro debates.
4. Stdio MCP keeps shipping unchanged via `mcp.run("stdio")` from the same FastMCP instance — one tool registry, two transports.
5. ~12.5h total across 7 tickets; web3-engineer audit of the x402 challenge round-trip is the gating risk.

## BLOCKER REQUIRING FOUNDER DECISION

**Pricing for `gecko_research` over hosted MCP** — the existing `/research` charges $10 (basic) / $50 (pro), set when buyers were CLI-using developers. If Manus/Cursor users are the new ABP, $10 may be a hard wall for first-call discovery; consider a "first call free per wallet" intro tier, or drop basic to $1-2 to convert. This is a `business-manager` call before the manifest at `app.geckovision.tech/skill.md` pins prices into install instructions (one-way).
