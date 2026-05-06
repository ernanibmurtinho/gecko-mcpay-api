# Manus First-Touch Sequence — simulating a new agent discovering Gecko

**Date:** 2026-05-06
**Persona:** Manus (autonomous AI agent, Solana-native wallet, no Gecko account).
**Goal:** Validate a startup idea end-to-end, paying per-call via x402, with zero sign-up.

This document has **two parts**:

- **Part A — Today (v0.2.12 in production):** what a new agent can actually do right now using the routes that ship.
- **Part B — S20 target:** the catalog-first flow with the agent-skills manifest, per-skill x402 dispatch, and bulk credit pack. **This shape does not exist yet** — it's the work scoped in `docs/strategy/2026-05-06-s20-knowledge-as-commodity.md` Track B.

Treat Part B as a design contract for what we're shipping in S20, not as a runnable demo.

---

## Pre-flight (Manus already has)

- A funded Solana wallet (USDC on the operator devnet/mainnet, per `X402_NETWORK`).
- Either Claude Code with `gecko-mcp` installed (MCP-native flow), OR the ability to make HTTP calls (raw API flow).

> ⚠ **Manus does NOT have:** a Gecko account, a Gecko API key, or any prior session.

---

# Part A — Today (v0.2.12, runs against `https://api.geckovision.tech`)

## Step 0 — There is no public catalog yet

> ❌ **Not shipped:** `https://app.geckovision.tech/.well-known/agent-skills/index.json` does not exist. Discovery is via the static `/pricing` endpoint instead.

```bash
curl -s https://api.geckovision.tech/pricing
```

Returns the per-tier price ladder for `/research`, `/plan`, `/route` (the routes that exist today). No structured `category` field; no `gecko_knowledge_category`; no per-skill descriptions for an agent crawler.

## Step 1 — Make a research call without payment

```bash
curl -s -X POST https://api.geckovision.tech/research \
  -H "Content-Type: application/json" \
  -d '{"idea":"Manus auto-validate: tokenized solar microgrid in São Paulo","tier":"basic","tier_preset":"budget"}'
```

**Response: HTTP 402 Payment Required**

```
HTTP/1.1 402 Payment Required
PAYMENT-REQUIRED: <base64 PaymentRequired>
```

The PAYMENT-REQUIRED header decodes to:
- `scheme: exact`
- `network: solana:<chain_id>`
- `asset: <USDC mint>`
- `amount: 100000` (= $0.10 in 6-decimal USDC)
- `payTo: <Gecko operator wallet>`
- `maxTimeoutSeconds: 300`

Standard x402 challenge. No sign-up triggered. Manus has no account.

## Step 2 — Pay and retry

Manus signs an SPL `transferWithAuthorization` (or the stub equivalent in dev mode), bundles it into the `X-PAYMENT` header, and retries:

```bash
curl -s -X POST https://api.geckovision.tech/research \
  -H "Content-Type: application/json" \
  -H "X-PAYMENT: <base64 signed transfer + accepted block>" \
  -d '{"idea":"Manus auto-validate: tokenized solar microgrid in São Paulo","tier":"basic","tier_preset":"budget"}'
```

**Response: 202 Accepted** with `session_id`. Workflow runs as a background task; payment settled synchronously, research keeps running for 60–90s.

## Step 3 — Poll for completion

```bash
curl -s https://api.geckovision.tech/sessions/<session_id>/result
```

When done, returns the full `ResearchResult` JSON: business plan, validation report, PRD, sources, citations, verdict (KILL / REFINE / BUILD), surviving dissent, falsifier checklist.

## Step 4 — Cheap follow-ups within the same session

The first 100 follow-up `ask` calls per session are **free** (covered by the original x402 settle), via the per-session route:

```bash
curl -s -X POST https://api.geckovision.tech/sessions/<session_id>/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"What 3 things would falsify this in 14 days?"}'
```

After the 100-call quota, the route returns 402 and Manus must pay $0.01/call via the standalone `POST /ask` route.

## Step 5 — High-volume usage today: per-call only

> ❌ **Not shipped:** `POST /skills/credit-pack` does not exist yet. There is no on-rail bulk credit today. Manus pays per call.

The free-quota-per-session is the only "discount" available today.

## Step 6 — Render a shareable report

```bash
curl -s -X POST https://api.geckovision.tech/report/<session_id> \
  -H "Content-Type: application/json" \
  -H "X-PAYMENT: <base64 $0.05 transfer>" \
  -d '{"format":"html"}'
```

Returns a self-contained HTML doc.

## Round-trip cost on a typical first session (TODAY)

| Step | Route | Cost | Cumulative |
|---|---|---:|---:|
| 0 | `GET /pricing` (discovery) | $0.00 | $0.00 |
| 2 | `POST /research` (basic) | $0.10 | $0.10 |
| 4 | 5× `POST /sessions/{id}/ask` | $0.00 | $0.10 |
| 6 | `POST /report/{id}` | $0.05 | $0.15 |
| **Total** | | | **$0.15** |

Manus paid **$0.15 for a full validated decision** with cited evidence + surviving dissent + falsifier checklist. **No account. No key. No subscription.**

---

## MCP-native variant (Claude Code agent, today)

Same flow, but via tool calls. The `gecko-mcp` server handles the x402 dance.

```python
classify = await mcp.gecko_classify(idea="Manus auto-validate: tokenized solar microgrid in São Paulo")
session = await mcp.gecko_research(
    idea="Manus auto-validate: tokenized solar microgrid in São Paulo",
    tier="basic",
    tier_preset="budget",
)
answer = await mcp.gecko_ask(
    session_id=session["session_id"],
    question="What 3 things would falsify this in 14 days?"
)
report = await mcp.gecko_report(
    session_id=session["session_id"],
    format="html"
)
```

The MCP layer is a thin wrapper over the HTTP routes above; same 402 → pay → 202 mechanic, same JSON shapes.

---

# Part B — S20 target shape (catalog-first, NOT shipped)

> ⚠ **Everything below is design, not running code.** It corresponds to S20 Track B (`docs/strategy/2026-05-06-s20-knowledge-as-commodity.md`). Tickets:
> - `S20-B-SKILL-REGISTRY-01` — canonical skill list + manifest builder
> - `S20-B-MANIFEST-ENDPOINT-01` — publish at `/.well-known/agent-skills/index.json`
> - `S20-B-X402-DISPATCH-01` — single `POST /skills/{skill_name}` route
> - `S20-B-CREDIT-PACK-01` — Ed25519-signed JWT credit token
> - `S20-B-DISPATCH-HANDLERS-01` — per-skill thin handlers
> - `S20-B-CONTRACT-TESTS-01` — pay.sh discovery + facilitator contract tests

## What changes from today

| Surface | Today (v0.2.12) | S20 target |
|---|---|---|
| Discovery | `/pricing` (tier table only) | `/.well-known/agent-skills/index.json` (12 skills, pay.sh-compatible) |
| Routes | `/research`, `/plan`, `/route`, `/advise`, `/ask`, `/report` | Single `POST /skills/{skill_name}` dispatch |
| Catalog skills | 4–5 unstructured | 12: 7 retrieval + 3 team-debate + 1 full + 1 credit |
| Per-category retrieval | ❌ none — full pipeline only | 7 endpoints at $0.01 each |
| Bulk credit | ❌ none | `credit-pack` ($10 → 1.5M tokens, JWT-redeemable) |
| Discovery for pay.sh crawlers | ❌ no manifest | ✅ pay.sh v1.0 schema with `pricing` + `gecko_knowledge_category` extensions |

## S20 cold-start flow (target)

```bash
# Step 0 — Discover via manifest
curl -s https://app.geckovision.tech/.well-known/agent-skills/index.json | jq '.skills | map({name, pricing: .pricing.flat_usd})'

# Step 1 — Cheap categorized retrieval ($0.01)
curl -s -X POST https://api.geckovision.tech/skills/retrieve-market-intelligence \
  -H "X-PAYMENT: <base64 $0.01 transfer>" \
  -d '{"query":"tokenized solar microgrid São Paulo competitors"}'

# Step 2 — Or run a single-team debate ($0.10)
curl -s -X POST https://api.geckovision.tech/skills/research-market \
  -H "X-PAYMENT: <base64 $0.10 transfer>" \
  -d '{"idea":"…"}'

# Step 3 — Full pipeline ($0.50)
curl -s -X POST https://api.geckovision.tech/skills/research-full \
  -H "X-PAYMENT: <base64 $0.50 transfer>" \
  -d '{"idea":"…"}'

# Step 4 — Bulk credit pack ($10, NOT subscription, NOT recurring)
curl -s -X POST https://api.geckovision.tech/skills/credit-pack \
  -H "X-PAYMENT: <base64 $10 transfer>"
# → returns Ed25519-signed JWT
# subsequent calls: Authorization: Bearer <jwt> instead of X-PAYMENT
```

## S20 round-trip economics (target)

| Step | Skill | Cost |
|---|---|---:|
| 0 | manifest GET | $0.00 |
| 1 | `retrieve-market-intelligence` | $0.01 |
| 2 | `retrieve-business-financial` | $0.01 |
| 3 | `research-market` (2-agent debate) | $0.10 |
| 4 | 5× free `ask` follow-ups | $0.00 |
| 5 | `report` | $0.05 |
| **Total** | | **$0.17** |

Cheaper than today *for narrow questions* — categorized retrieval at $0.01 is the new entry point, and Manus gets to mix-and-match retrieval ↔ debate ↔ full-pipeline based on what it actually needs.

---

## What this sequence proves about the thesis

1. **Cold-start works today.** Part A is real. A new agent can validate an idea via x402 in under 60 seconds with no account.
2. **Per-call is the default — no subscription.** x402 is per-call by design. Bulk credit (S20) is a single x402 settle, not recurring billing.
3. **The S20 catalog upgrade is what unlocks the cheap retrieval surface** — today's $0.10 floor is "full pipeline or nothing." S20's $0.01 categorized retrieval is the entry point that lets Manus-class agents iterate cheaply.
4. **Cataloged discoverability** (S20-B2) is what gets Gecko picked up by pay.sh's crawler and any other x402-ecosystem aggregator — same publish-once-be-everywhere pattern.
