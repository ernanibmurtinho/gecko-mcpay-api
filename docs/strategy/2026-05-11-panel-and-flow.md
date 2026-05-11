# Gecko panel + end-to-end flow — quick reference

**For:** product-designer co-founder
**Date:** 2026-05-11
**Companion to:** `2026-05-11-cofounder-roadmap-brief.md`

Two tables. The first is **who's in the room when a verdict is produced**. The second is **what the user goes through to get there and what happens after**. Read in order — the panel is what runs INSIDE step 3 + step 7 of the user flow.

---

## 1. The 7-voice trade panel

Seven specialists run in sequence inside every `gecko_trade_research` call. Each ends with a structured closing line (a single token like `Trend verdict: bullish`) that the next persona reads as input. No one repeats another's job; if they try, the system rejects the turn.

| # | Persona | Reads | Produces | Closing token | Out-of-scope |
|---|---|---|---|---|---|
| 1 | **technical_analyst** | Price/volume snapshots, on-chain metrics | Trend direction, 1–3 named levels, volatility regime, divergences | `Trend verdict: bullish / bearish / mixed` | Fundamentals, sentiment, action |
| 2 | **sentiment_analyst** | News, X/Twitter, governance threads | Dominant narrative, 2–4 named concerns, recent pivots, insider-vs-retail voice | `Sentiment band: fear / neutral / greed` | Charts, mechanics, action |
| 3 | **fundamental_analyst** | Protocol docs, governance, audits, on-chain metrics | Core mechanic in one sentence, TVL/volume/fee trajectory, audit posture, integrations | `Protocol health: degraded / stable / growing` | Price, sentiment, veto |
| 4 | **risk_manager** | All prior turns + corpus | Named risk vectors (severity × likelihood), risks others missed, sizing/horizon constraint | `Risk band: acceptable / elevated / unacceptable` | Targets, strategy, abstract moralizing |
| 5 | **strategist** | All four analyst turns | One-sentence question restate, alignment/conflict map, structured intent (action / direction / size_band / stop_band / horizon), single most-important falsifier | `Strategic intent: <one-sentence-action>` | New analysis, $/leverage specifics, tax/legal |
| 6 | **bull_bear_debater** | All five prior turns | Bull case (3–6 sentences), Bear case (3–6 sentences), one **decisive question** that would break the tie | `Decisive question: <one observable>` | Picking a side (that's the coordinator) |
| 7 | **coordinator** | All six prior turns | 2–4 sentence panel summary, final verdict, confidence ∈ [0.0, 0.85], 2–5 key_drivers, dissent_count, 0–3 blocker_questions | `Final verdict: act / pass / defer` (+ JSON envelope) | New reads, moralizing, >0.85 confidence |

**Roles to remember when designing surfaces:**
- The **bull_bear_debater is a single agent producing both perspectives** — not two voices in a conversation. The design surface for dissent should reflect this: one card, two opposing case blocks, one decisive question.
- The **coordinator never exceeds 0.85 confidence** by design. If a UI ever displays 0.95 confidence, it's not a Gecko verdict. Stretch the bar at 0.85.
- **Dissent count is from the 5 *non-debater* analysts** — technical, sentiment, fundamental, risk, strategist. Bull_bear_debater is structurally adversarial so it isn't counted as dissent.
- Every persona's closing line is the **load-bearing parse target** for the system. Design copy can wrap and contextualize these tokens, but never replace them.

---

## 2. The end-to-end user flow (vibe trader, v0.1)

| # | Step | User action | What we charge | What they see | What they leave with |
|---|---|---|---|---|---|
| 1 | **Discover** | Reads `app.geckovision.tech/skill.md` from a Discord post / OKX skill catalogue | $0 | One-paragraph promise + a terminal screenshot of a verdict | The decision to run one command |
| 2 | **Install** | `Read skill.md and follow the instructions.` in Claude Code, then `curl -fsSL …/install.sh \| bash` | $0 | Skill registers; `bb` and the 3 new skills appear; `gecko-mcp doctor` passes | A wired local env — no login, no signup |
| 3 | **First paid verdict** | Asks the coach a one-line trade question (e.g. "Kamino USDC reserve right now?") | **$0.25 USDC** via x402 | 7-voice panel runs → KILL/REFINE/BUILD verdict + surviving_dissent + 3–5 attributed citations | First proof the wedge is real |
| 4 | **Coach session** | Multi-turn conversation: risk tolerance, capital size, venue preference, horizon | $0 (conversation is free) | Profile being built, citations attached to each suggested rule | A profile + a candidate strategy template |
| 5 | **Strategy spec emit** | Coach calls the oracle per meaningful decision (template, sizing, exit) | **~3–5 × $0.25** during the build (cache hits free) | Schema-validated JSON spec rendered as a human-readable summary | A deployable spec at `~/.gecko/trade-agent/specs/<name>.json` |
| 6 | **Deploy agent (advisor mode)** | "Deploy strategy `<name>`" → `bb trade-agent up --spec <path>` | **$0.75 pro startup verdict** on cache miss, free on cache hit | "Agent `<id>` deployed in advisor mode. Watching N tokens. Will surface opportunities; will not sign." | A long-running Python process on their laptop |
| 7 | **Review opportunities** | Agent surfaces candidates in the journal; user reads, decides whether to execute manually | **~$1.50/day** in scheduled re-verdicts + triggered breakers (cache-first) | Journal tail in `bb trade-agent inspect <id>` + Claude Code summaries | Trades they would not have made on vibes — and trades they avoided because dissent was strong |
| 8 | **Graduate to trader mode** *(v0.2 — NOT v0.1)* | Once advisor telemetry validates trust, flip `--mode trader` | Same oracle math + adapter fees pass-through | "Trader mode armed. 24h paper-trade gate active." | Autonomous execution they trust |
| 9 | **Publish to registry** *(future)* | When partner Attested Alpha Registry ships, one-click "publish my strategy" | $0 to publish; revenue share via the Pioneer model | "Published" badge + future earnings ticker | The vibe trader becomes a Pioneer |

**The critical design moment is step 3.** The $0.25 first verdict must feel like a *qualitatively different answer* than ChatGPT-with-no-grounding. If it doesn't, the journey dies and we never see the user again. Every other step has slack. Step 3 has none.

**Step 6 is the second emotional peak.** The user watches an agent boot on their own machine, with their keys, their cache, their journal. Local-hosted is the moat — design the deploy ceremony to feel earned.

---

## How the panel connects to the flow

The 7-voice panel runs **inside step 3** (the first verdict) and **inside step 7** (every triggered re-verdict the agent fires). It runs again at **step 6** (the pro-tier startup verdict on cache miss). The user never sees seven separate voices — they see one envelope with:

- `verdict` (the coordinator's call)
- `confidence` (≤ 0.85 by design)
- `key_drivers` (which persona said what)
- `surviving_dissent` (the 1–3 voices that disagreed and weren't talked out of it)
- `citations[]` (attributed chunks — `Howard Marks — Oaktree memo (2008)`, not `chunk_8a3f`)
- `backtest` (pro tier only)

**Design implication:** the panel is internal architecture. The user-facing surface is the envelope. Make the envelope feel like dissent-checked judgment from a real room — not "the AI said." Attribute everything; never collapse to a single voice; never hide that 0.62 confidence is "the panel was not certain."
