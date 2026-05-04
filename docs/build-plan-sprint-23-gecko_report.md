# Sprint 23 ticket — `gecko_report` (productize the dogfood HTML)

**Status:** ready to plan
**Predecessor:** the 2026-05-04 dogfood demo (`/tmp/gecko-demo/gecko-e2e-2026-05-04.html`) — the first hand-rolled HTML report the operator built for the Colosseum weekly update.
**Driver:** every research session today produces structured JSON that's *demo-ready* but not *share-ready*. The operator manually built an HTML report for the weekly update; that's a one-time artifact. Productize it as an MCP tool and a paid HTTP endpoint so any tester (or any agent) can drop a shareable report after `gecko_research` + `gecko_plan` in one call.

**Done = `gecko_report(session_id)` returns a single HTML file equivalent to the 2026-05-04 dogfood report, populated from the session's persisted state, brand-consistent, deployable to `app.geckovision.tech/r/<verdict_hash>` for permanent linking.**

---

## Tracks

### Track A — `gecko_report` MCP tool (S23-REPORT-01)

New tool in `packages/gecko-mcp/src/gecko_mcp/tools.py` (or wherever the existing tool registrations live):

```python
gecko_report(session_id: str, format: Literal["html", "markdown", "pdf"] = "html") -> str
```

- Accepts a `session_id` from a prior `gecko_research` call.
- Reads the session's persisted state (verdict, sources, classification, business_plan, validation_report, prd, voices if `gecko_plan` was called, follow-up Q&A from `gecko_ask`).
- Renders an HTML report matching the structure of `/tmp/gecko-demo/gecko-e2e-2026-05-04.html`: header / verdict / classification / sources / validation / business plan / PRD / Q&A / 5-voice panel / run economics / "what testers should see" footer.
- `format="markdown"` for embedding in PRs / docs.
- `format="pdf"` defers to S24 (HTML-to-PDF via headless chrome adds an infra dep — split).
- FREE at the MCP layer; the paid HTTP route charges $0.05 (per Sprint 13 commoditization pattern).

**Implementation:**

- Renderer lives in `packages/gecko-core/src/gecko_core/reports/html.py` (new). Pure function: `render_html_report(result: ResearchResult, plan: AdvisorPanel | None, asks: list[AskResult] | None) -> str`. No MCP / API plumbing in there — the renderer is core business logic per the CLAUDE.md "thin transport" rule.
- Templates inlined (no Jinja dep) — the dogfood HTML is ~600 lines of CSS + content, well under the threshold where a template engine pays for itself.
- Brand-consistent CSS lifted from the dogfood file — keep the `--bg/--panel/--accent-2` token names so a future `gecko-mcpay-app` styleguide can reuse them.

**Tests:** `packages/gecko-core/tests/reports/test_html.py` — round-trip a fixture `ResearchResult` → HTML, assert the verdict_hash + session_id appear in the output, no `{{` / `}}` template debris, no missing-section errors. ~40 lines.

**Owner:** software-engineer

---

### Track B — `POST /report/{session_id}` HTTP route (S23-REPORT-02)

Paid x402 endpoint in `packages/gecko-api/src/gecko_api/main.py`:

```python
POST /report/{session_id}
```

- $0.05 per report (`REPORT_PRICE` env var, default `'$0.05'`).
- Same `format=html|markdown` query param.
- Cached by `verdict_hash` — re-rendering the same verdict is free (idempotent + content-addressed).
- Add to `_build_routes` with `extra=svm_extra` per the catalog convention from commit `2378bff`.
- Add to `bazaar/extension_as_dict` so the `/.well-known/x402` catalog advertises it.

**Tests:** `tests/api/test_report_route.py` — happy path + 404 on bad session_id + 402 stub-mode flow + idempotent re-render.

**Owner:** software-engineer + web3-engineer (route registration touches x402 wiring)

---

### Track C — Permanent shareable URLs (S23-REPORT-03)

`POST /report/{session_id}` accepts `?publish=true` which:

1. Renders the HTML.
2. Writes to Supabase Storage at `reports/<verdict_hash>.html`.
3. Returns `{"url": "https://app.geckovision.tech/r/<verdict_hash>"}`.

The frontend `gecko-mcpay-app` adds a static route `/r/[verdict_hash]/page.tsx` that fetches the HTML from Supabase and renders it inside a `dangerouslySetInnerHTML` (with CSP nonce — the report HTML is operator-trusted, not user-generated, so XSS surface is bounded).

This is the path that makes the **Colosseum weekly update** workable: operator runs `gecko_research`, then `gecko_report --publish`, gets a `app.geckovision.tech/r/<hash>` URL to drop in the post.

**Owner:** frontend-engineer (gecko-mcpay-app stub; real impl lives in that repo) + software-engineer here for the API side
**Note:** cross-repo coordination — flag in PR description per the CLAUDE.md done-checklist line 9.

---

### Track D — Bundled `gecko_plan` workflow (S23-REPORT-04)

Optional: extend `gecko_plan` to accept `with_report: bool = False` that auto-fires `gecko_report` after the panel returns and includes the URL in the response. Saves the operator a tool call when they're scripting the demo loop.

**Owner:** software-engineer
**Defer if Track C lands later** — the standalone `gecko_report` tool covers the use case.

---

## Out of scope (split to S24)

- HTML → PDF rendering (needs headless chrome / playwright)
- Report templating engine (Jinja / Mako) — no need until 3+ format variants
- Per-tester branding overrides (white-label) — not a launch requirement

---

## Acceptance

1. `gecko_report(session_id="08e0be97-eb66-44ca-ae3b-89e578506b25")` produces an HTML file byte-equivalent (modulo timestamp) to `/tmp/gecko-demo/gecko-e2e-2026-05-04.html` for that session.
2. `POST /report/{id}` returns 402 in stub mode, 200 with HTML body in live mode after x402 settle.
3. `bb doctor` checks the new route is advertised in `/.well-known/x402`.
4. The operator's Colosseum weekly update flow becomes: run `gecko_research` → run `gecko_plan` → run `gecko_report --publish` → drop the URL in the post. Three commands, no manual HTML editing.

---

## Why this lands here, not in Sprint 22

S22 is voyage-embed + judges + calibration. Adding a renderer ticket would dilute focus. The hand-rolled dogfood HTML is the spec — it exists, it's been reviewed, the operator wants to share that shape. S23 is the right home: short, tight, single deliverable, one of the highest-leverage GTM moves on the roadmap (every research session ends with a shareable artifact instead of a JSON dump).

---

## Origin note

This ticket was created on 2026-05-04 after the operator manually built `/tmp/gecko-demo/gecko-e2e-2026-05-04.html` for the Colosseum weekly update video. The HTML's "Sprint flag" footer points readers to this file — keep that link working when the file is renamed for inclusion in S23 planning.
