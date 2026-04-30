# Landing vs. research — positioning delta

**Date:** 2026-04-30
**Author:** product-designer (S10-POSITION-02)
**Inputs:**
- Landing copy: `gecko-mcpay-landing/components/marketing/{hero,icp-buyer,anti-positioning,pricing}.tsx` (read 2026-04-30)
- Research output: `docs/positioning/2026-04-30-gecko-self-research.md` (skeleton; awaiting first matrix run)
- Persona reference: `docs/PRD.md`, `docs/product-story.md`

> **Status:** v0 of the delta. The `2026-04-30-gecko-self-research.md`
> file is a skeleton until `scripts/positioning_check.sh` runs end-to-end
> in an env with API keys (the agent shell that authored this doc has no
> Bash tool — the human operator runs the script and re-renders this
> delta against the populated matrix). The structural deltas below hold
> regardless: they're driven by landing copy vs. PRD persona, and the
> matrix run will sharpen the specific quotes per idea.

---

## Snapshot — landing copy (current)

| Field | Copy |
|---|---|
| **Eyebrow** | Agentic product validation · MCP |
| **Hero headline** | Stop building things nobody wants. |
| **Subhead** | Gecko is an AI founder review board inside Claude Code. It stress-tests your idea, sharpens your ICP, and turns the surviving concept into a fundable PRD — before you write code. You fund a workspace budget; agents pay per task under the hood. |
| **Top value bullets** | Verdict: build · narrow · pivot · kill · Adversarial panel in ~3 min · Claude Code MCP · workspace budget |
| **ICP** | Claude Code / Cursor power users who can build — but fear building the wrong thing. Primary: technical founders, senior engineers, AI-native builders. Secondary: MVP studios, hackathon teams. |
| **Pricing pitch** | Pay for judgment — then optional routed execution. Validation $9–19/run · PRD+ship $29–49/run · Workspace $99/mo. |
| **Anti-positioning** | Not an LLM router · Not crypto-first software · Not competing with frames.ag. |

## Snapshot — research persona (PRD + product-story)

| Field | What the pipeline says |
|---|---|
| **One-liner** | Plain-language idea → searchable knowledge base + business plan + validation report + PRD in <30 min. Session billed via x402 on Solana. |
| **Primary persona (V1)** | Technical developer building on Solana or adjacent stacks. CLI-first. Wants session ID, chunk count, structured output. |
| **Pricing model** | Basic $10–20/session, Pro $50–100/session. Session-based, never per-query. |
| **Verdict shape** | Structured `gap_classification` (Full / Partial:* / False) + 5-voice advisor closing lines. No single-token "build/narrow/pivot/kill" emitted today. |
| **Output shape** | Three documents (Business Plan, Validation Report, PRD), every claim cited. |

---

## 1. Claims on landing the research disagrees with

### 1a. "Adversarial panel in ~3 min" vs. measured pipeline latency

Landing promises ~3 min. The PRD non-functional targets are:
- Indexing: <3 min (5 sources)
- Basic generation: <60 s
- Pro generation (AutoGen GroupChat): <5 min

A real run including discovery + ingestion + 5-voice plan is closer
to 5–10 min on a cold session. The "~3 min" claim is true for
**plan-only re-runs** against an already-indexed session, not for the
first reveal a new user sees.

**Recommend copy edit:** "Adversarial panel in minutes, not weeks."
Drop the false-precise number.

### 1b. "Verdict: build · narrow · pivot · kill" vs. actual emitted verdict

The pipeline emits `gap_classification` (`Full`, `Partial:segment`,
`Partial:UX`, `Partial:geo`, `Partial:pricing`, `Partial:integration`,
`False`) plus 5 closing lines — not a single-token "build/narrow/
pivot/kill" verdict. The landing word "verdict" sets an expectation
the renderer doesn't currently meet.

**Recommend (pick one):**
- (a) Land the verdict mapping in the renderer: `False/Full → KILL`,
  `Partial:* → REFINE`, advisor consensus → `BUILD` upgrade. Then the
  landing copy is honest. (Track this as an S11 follow-up.)
- (b) Soften landing to "Verdict: kill · refine · ship" and drop
  "narrow/pivot" until the underlying taxonomy supports it.

### 1c. "Workspace budget · agents pay per task under the hood"

The current x402 surface is **session-priced**, not per-task. The
product-story explicitly killed per-query micropayments ("friction
creates avoidance"). Landing copy implies a per-task meter that
doesn't exist in V1.

**Recommend copy edit:** "Fund a workspace; pay per validation run."
Reserve "agents pay per task" for V3 when `gecko_route` per-call
billing actually flows.

---

## 2. Research surfaces not on landing

These are claims the research persona is structured to surface that
the landing page underuses or omits.

### 2a. Citation-backed everything

Every line of the validation report cites a source URL with chunk
index and similarity score. The landing page mentions "cited research"
once (anti-positioning pill) but doesn't make citations a hero
artifact. The reveal moment in the CLI is **citations as trust
mechanism**.

**Recommend section:** A "Sources, not vibes" block — show a real
numbered citation list from a sample run, with clickable URLs. Lifts
the trust signal that distinguishes Gecko from chat-bot validation.

### 2b. Knowledge base persists post-session

The PRD V1 spec retains session knowledge bases for 90 days; Pro
agents stay alive 72h post-session for follow-up `bb ask`. Landing
copy treats the run as one-shot. The persistence is a moat layer
(product-story Layer 3 — "context as moat").

**Recommend bullet:** Add to the value strip — "Knowledge base stays
queryable for 90 days. Ask follow-ups, don't re-index."

### 2c. The `gap_classification` taxonomy itself

The fact that Gecko's verdict is **structured** (one of 7 enum
values) — not a vibes-y prose paragraph — is a differentiator vs.
ChatGPT-style validation. Landing doesn't surface this.

**Recommend microcopy:** Under the verdict pills, "Verdicts are typed
labels — not opinions you can re-prompt your way out of."

### 2d. The 5 specific advisor voices

CEO / CTO / BM / PM / SM (per `apps/cli/src/gecko_cli/commands/plan.py`
table). Landing says "founder review board" but doesn't name the
voices. Naming them concretizes the abstract claim.

**Recommend block:** Add the 5 voice icons + one-line role summaries
to the SubAgents section. Cross-reference what each voice grades.

---

## 3. Vocabulary drift

| Landing says | Research / PRD says | Recommendation |
|---|---|---|
| "AI founder review board" | "5-voice Advisor Panel (CEO/CTO/BM/PM/SM)" | Pick one. The PRD/CLI uses "Advisor Panel"; landing should match. "Founder review board" is warmer for marketing — keep as the headline framing but use "Advisor Panel" as the technical noun in body copy + section headers. |
| "fundable PRD" | "PRD with V1/V2/V3 scope, acceptance criteria, success metrics" | Add the structure. "Fundable" is unfalsifiable; the structured scope is the proof. |
| "stress-tests your idea" | "scores demand signals, maps risks, delivers Go/No-Go" (product-story) | Tighten landing to "scores demand, maps risk, signs off Go or No-Go." |
| "agents pay per task" | "session-priced; per-call routing in V3" | See 1c. Drop until V3 lands. |
| "build · narrow · pivot · kill" | `Full / Partial:* / False` + advisor consensus | See 1b. |
| "workspace budget" | x402 session charge (per-call only today) | Either ship the workspace abstraction in `gecko-api` or rename to "session credits". |
| ICP: "technical founders, senior engineers, AI-native builders" | PRD: "solo developer or small team building on Solana or adjacent stacks" | Landing's ICP is broader than the PRD's V1 persona. Either narrow landing to match V1 (Solana-adjacent builders) or update PRD to reflect the broader Claude Code / Cursor power-user pivot. **Recommend updating PRD** — the broader ICP is the post-Sprint-9 reality. |

---

## 4. Action list (ranked)

1. **Resolve the verdict mismatch (1b).** Either the renderer emits
   KILL/REFINE/BUILD or the landing drops "verdict" framing. Pick one
   this sprint — don't ship the demo with the gap.
2. **Drop "agents pay per task" (1c).** It misrepresents V1 pricing
   and re-opens the per-query debate the product-story already closed.
3. **Update the PRD ICP (3, last row).** Landing already pivoted to
   "Claude Code / Cursor power users". The PRD is stale.
4. **Add a citations-first section to landing (2a).** It's the
   strongest trust signal and the cheapest copy edit.
5. **Soften "~3 min" (1a).** Replace with "minutes, not weeks" until
   we have measured cold-session latency under 3 minutes consistently.

## 5. Prompt-persona updates

If the matrix run shows the advisor panel saying things landing
contradicts (e.g. closing lines flag "session pricing too low" while
landing leads with $9–19/run), the fix is in the persona prompts in
`packages/gecko-core/src/gecko_core/orchestration/advisor/` not in
the landing copy. Re-run the matrix after any persona prompt change
and diff this doc.

---

_Re-render this doc after every fresh run of_
`scripts/positioning_check.sh` _— the structural deltas above are
durable, but the per-idea quotes need to track the latest matrix
output._
