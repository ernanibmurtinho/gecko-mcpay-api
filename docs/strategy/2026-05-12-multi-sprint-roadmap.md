# Multi-sprint roadmap — S24 → V2

> **Author:** synthesized from staff-engineer + business-manager + product-manager dispatches (2026-05-12 post-S24-Wave-1).
> **Horizon:** S24 (now) → V1 ship → V0.2 trader mode → V1.0 creator attribution → V2 web + multi-vertical.
> **Status:** DRAFT — three convergence points need founder calls (§3) before claiming S25 lane assignments.

---

## 1. What "app working with everything we need" means

A **vibe trader installs the skill cold, deploys an advisor-mode agent on their laptop in under 45 minutes, pays live x402 on Solana for a verdict whose calibration we can defend numerically, and returns within 7 days for a second verdict at their own cost.** That is V1.

V1 success bar (defensible numbers, all three agents agreed within tolerance):

- **100 unique paid installs** from outside founder network
- **≥40% week-over-week retention** on returning users
- **Brier ≤ 0.30** on the 80-fixture eval suite
- **act-verdict 30d-profit rate ≥ 58%** vs ~50% protocol-mean baseline
- **time-to-first-deployed-agent ≤ 45 min** validated in screen-share with ≥3 cold users
- **cost-per-user-month ≤ $0.50 infra**

V0.2 = trader mode (signed execution). V1.0 = creator-attribution on-chain. V2 = web app + general-coach front door + multi-vertical.

Below V1: trader mode, web app, marketplace, licensed canon, public API, multi-vertical beyond DeFi, hot-swap parallel cutover, daemon mode, OKX Skill Quality submission, paid acquisition. Each is deferred with explicit rationale in §5–§7.

---

## 2. The sprint arc at a glance

| Sprint | Window | Theme | Ships | V-tier |
|---|---|---|---|---|
| **S24** | 5/12 → 5/26 | Eval gate + live flip | 40-fixture eval pass · market_data ProviderKind · x402 live on /trade_research · advisor runtime · 10 outside installs | toward V1 |
| **S25** | 5/27 → 6/9 | Retention + ICP broadening | bb digest read-only · general-coach branch (Mateus persona) · streaming verdict · lending/perps corpus gap fill · 5 discovery sessions | toward V1 |
| **S26** | 6/10 → 6/23 | Reliability + scale-safe | doctor v2 · laptop reliability · OpenAPI contract freeze · 25-agent scale test · 80-fixture eval suite v2 | toward V1 |
| **S27** | 6/24 → 7/7 | Calibration re-pass + retention hook | Day-7 actionable digest · verdict permalink · pricing A/B · 50 cumulative paid | toward V1 |
| **S28** | 7/8 → 7/21 | **V1 SHIP** | Verdict diffing on re-run · Day-30 surface · receipts polish · 100 cumulative paid · PH/paid-acquisition gate | **V1** |
| **S29** | 7/22 → 8/4 | V0.2 paper-trade gate | Trader-mode scaffolding · paper-trade 24h gate · backtest harness · trust artifacts in advisor (pre-trade envelope, halt button promoted) | V0.2 |
| **S30** | 8/5 → 8/18 | V0.2 OKX adapter live | exec_adapters/okx.py live · risk-cap enforcement · daily-loss circuit breaker · Pattern C contract test · $25 paper-trade license | V0.2 |
| **S31** | 8/19 → 9/1 | V0.2 SendAI + licensed canon | SendAI adapter live (Backpack stays stub) · O'Reilly/Perlego canon ingest if revenue covers infra | V0.2 |
| **S32-S33** | 9/2 → 9/29 | V1.0 daemon + contributor stub | Multi-agent daemon (one process, N agents) · contributor upload + claim-attribution flow (no payouts yet) | V1.0 prep |
| **S34** | 9/30 → 10/13 | **V1.0 SHIP** | Creator-attribution on-chain settlement live · 70/30 split · bucketed reputation bands (no public leaderboards) | **V1.0** |
| **S35-S36** | 10/14 → 11/10 | V2 web app | Next.js app at app.geckovision.tech · Privy embedded wallet · session sharing · cross-repo to gecko-mcpay-app | V2 |
| **S37** | 11/11 → 11/24 | V2 general-coach at scale | What was S25's branch becomes the primary entry surface · multi-vertical idea routing | V2 |
| **S38** | 11/25 → 12/8 | V2 public categorized API | market_intelligence · investment_signals · dex as paid x402 endpoints for external agents | V2 |
| **S39+** | 12/9 → … | V2 multi-vertical | Beyond DeFi: SaaS GTM canon, consumer-product research, infra/devtools | V2 |

**V1 ship: S28 (2026-07-21).** Picked the middle of the three agent estimates. Staff said S27 (engineering-only), business said S28 (engineering + GTM), PM said S30 (engineering + GTM + Day-30 cohort). S28 keeps engineering rigor + GTM, and accepts that the Day-30 cohort matures *into* V0.2 instead of gating V1.

**V0.2 ship: S31 (2026-09-01).** Trader-mode + OKX live + SendAI + licensed canon arc.

**V1.0 ship: S34 (2026-10-13).** Creator attribution live.

**V2 first surface: S35 (2026-10-14).** Web app + multi-vertical begin.

---

## 3. Three founder decisions before S25

S24 is already running. These three calls land between Day 7 and Day 14 of S24 — they shape S25's lane assignments.

| # | Decision | Three-agent view | My recommendation |
|---|---|---|---|
| **D4** | **General-coach branch — S25 or S26?** | Staff: S25 (per S24 D2). Business: kill from S25 path entirely, defer to V2 S37. PM: S25 top ticket, Mateus is primary cohort from S25 onward. | **S25, gated on S24 discovery sessions.** If first 3 S24 sessions show ≥2 Caios (DeFi-adjacent), proceed. If 2 of 3 sessions show non-DeFi builders bouncing at front door, accelerate. If 0 of 3 are even reachable, defer to S26 and use S25 for trade-vertical depth. |
| **D5** | **Backtest harness — when?** | Staff: skeleton S27, full S28. Business: full V0.2 S29. PM: tied to eval expansion S26-S27. | **Skeleton S27, full S29.** S27 needs minimal backtest to validate eval-suite-v2 claims; S29's paper-trade gate needs full backtest. Splitting is right — but call it explicitly in S27 plan so the team doesn't half-ship. |
| **D6** | **Day-7 retention infra — read-only digest S25 or actionable S27?** | Staff: silent. Business: digest in S26 with re-verdict CTA. PM: read-only S25 → actionable S27 (the seam argument). | **PM's call. Read-only S25, actionable S27.** Reasoning: falsifier dates from S24 first verdicts don't mature until late S25 — a digest in S25 with no signal is spam. Land the surface in S25, turn it on in S27 when falsifiers start firing. |

**Action:** decide D4/D5/D6 by end of S24 Day 7 (2026-05-18). Default to my recs if no counter — staff + business + PM all gave me enough signal to pick.

---

## 4. Per-sprint plan, S24 → V1 ship (high resolution)

### S24 — Eval gate + live flip (covered in detail at `docs/strategy/2026-05-12-s24-plan.md`)

Goal locked. Wave-1 already exposed the headline finding: panel collapsed to 100% defer-rate after the abstain-prompt patch. Market-data ProviderKind is the V1 blocker now.

### S25 — Retention infra + Mateus persona unlock

- **Goal:** Day-7 retention ≥ 20% on the S24 first-10 cohort; broaden front door without diluting wedge.
- **Workstreams:**
  - **WS25-A: General-coach branch** (ai-ml + software) — coach Step 0 routes trade vs. startup idea. Trade keeps existing path; startup hits existing `gecko_research` v1 surface.
  - **WS25-B: `bb digest` read-only** (software) — weekly summary, no actions yet. Open-rate baseline.
  - **WS25-C: Streaming verdict** (ai-ml + cofounder) — "Skeptic engaging…" named-voice progress. Same latency, 10× perceived value.
  - **WS25-D: Lending + perps corpus fill** (data) — addresses Wave-1 finding #2 (2/5 fixtures returned zero citations).
  - **WS25-E: 5 discovery sessions** (cofounder + PM-lite-by-me) — Caio status moves to `n=8 directional`.
  - **WS25-F: pay.sh listing + co-published partner blog** (founder execute) — GTM beat.
- **DoD:** ≥30 cumulative unique paid installs; ≥6 day-7 returners; coach handles non-DeFi without falling to defer; lending/perps fixtures return ≥3 citations; n=8 discovery directional report in `docs/discovery/`.

### S26 — Reliability + scale-safe

- **Goal:** System holds under 25 concurrent agents; cofounder OpenAPI contract freezes for V2 frontend prep.
- **Workstreams:**
  - **WS26-A: bb trade-agent doctor v2** — full preflight with remediation copy per failure path
  - **WS26-B: Laptop reliability** — sleep/wake recovery, websocket reconnect storms, journal corruption guards, systemd/launchd recipes
  - **WS26-C: Oracle cost ceiling + backoff** — per-agent daily spend cap, semaphore tuning under burst
  - **WS26-D: MongoDB Atlas scale test** — 25-agent simulated load, TTL verification, storage cap alert
  - **WS26-E: OpenAPI contract freeze** for gecko-api — V2 cross-repo prep
  - **WS26-F: Eval suite expansion** to 80 fixtures (S27 needs this baseline)
- **DoD:** 25 simulated agents run 72h without operator touch; zero data loss on simulated kill -9; doctor catches 100% of seeded failures; OpenAPI v0.3 spec frozen.

### S27 — Calibration re-pass + retention hook activates

- **Goal:** Second eval pass confirms calibration didn't drift; Day-7 digest becomes actionable.
- **Workstreams:**
  - **WS27-A: Eval suite v2 run** on 80 fixtures — Brier ≤ 0.28 (tighter than S24's 0.30)
  - **WS27-B: Backtest skeleton** (D5 split) — enough to validate eval claims, not a user-facing surface
  - **WS27-C: Actionable digest** — re-verdict CTA when falsifier date passed
  - **WS27-D: Verdict permalink** — shareable link for each verdict
  - **WS27-E: Pricing A/B** — $0.25 vs $0.50 basic on cohort split; measure conversion delta
  - **WS27-F: Tier-shape fix** (issue #16) — calibrate basic vs pro signal differentiation
  - **WS27-G: Colosseum follow-up + data-led Twitter thread** — "30 verdicts settled live, here's what they got right and wrong"
- **DoD:** Brier ≤ 0.28; ≥50 cumulative paid; ≥10 day-7 returners (vs S25's 6); pricing experiment conclusive.

### S28 — V1 ship 🚢

- **Goal:** V1 declared production-ready.
- **Workstreams:**
  - **WS28-A: Verdict diffing on re-run** — show what changed between runs (which voice flipped, which falsifier resolved). PM's retention wedge.
  - **WS28-B: Day-30 surface** — first Day-30 returners exist now, design for them
  - **WS28-C: Receipts polish** — Solscan signature + tx-amount + protocol target in every verdict envelope footer
  - **WS28-D: Session bundle pricing** ($5 flat for coach-built spec, includes oracle calls during build)
  - **WS28-E: Daily free tier** (3 cache-hits/day per wallet)
  - **WS28-F: V1 launch comms** + cofounder design polish on `app.geckovision.tech` hero
  - **WS28-G: PH/paid acquisition gate** — PH only if S27 hit ≥25 weekly-active wallets; paid only if organic CAC < $15
- **DoD:** ≥100 cumulative paid; ≥40 day-7 returners; ≥10 Day-30 returners; cost-per-user-month ≤ $0.50.

---

## 5. V0.2 sprint arc — Trader mode (S29 → S31)

### S29 — Paper-trade gate

- Trader runtime forks from advisor; every entry gated through 24h paper window with simulated PnL journal
- OKX adapter goes from stub to live behind `--mode trader --confirm` double-opt-in
- Full backtest harness (D5 right half)
- **Trust artifacts ship in advisor first** (pre-trade envelope with `human_required: bool`, halt button promoted to top of `bb trade-agent inspect`)
- **Contributor onboarding stub** (upload form, claim-attribution flow, no payouts yet)
- DoD: 24h paper trade completes 10 simulated trades per agent without circuit-breaker trip on test fixtures

### S30 — OKX execution live

- Live signed execution through OnchainOS
- Risk-cap enforcement, daily-loss circuit breaker, per-trade size validation
- Pattern C contract test gates the live flip
- **$25 paper-trade license per spec** introduced (one-time, not subscription)
- DoD: 5 real trader-mode wallets, zero custody incidents, recorded fixtures for contract test

### S31 — SendAI + licensed canon

- SendAI adapter live (second venue; Backpack stays stub — Pattern B)
- Wallet/facilitator neutrality validated by real swap
- **Licensed canon (O'Reilly + Perlego)** ingest **gated on V1 revenue covering infra** (memory `feedback_okx_no_funding_pressure`)
- DoD: ≥25 trader-mode wallets, ≥2 wallets running on SendAI, licensed-canon ingest live if budget unlocks

---

## 6. V1.0 sprint arc — Daemon + creator attribution (S32 → S34)

### S32 — Multi-agent daemon

One process supervises N agents (today: one process per agent). Resource pooling for Helius/Pyth subs, shared cache. Required before any user runs >3 agents.

### S33 — Hot-swap + contributor stub matures

New-version-parallel → drain old → atomic switch. Mongo transaction on `agent_state.spec_version`. Migration test matrix for open positions on old spec, new spec changes risk caps mid-position.

Contributor upload + attribution preview ("your chunk would have been cited in 12 of the last 100 verdicts"). Still no payouts.

### S34 — V1.0 ship 🚢

- Creator-attribution on-chain settlement live
- 70/30 split, batch-settled at $15 threshold per creator wallet
- **Bucketed reputation bands** (`emerging` / `established` / `senior`) per CLAUDE.md anti-gaming rule
- **No public leaderboards** — contributors see their own band, nobody else's
- Lucia persona (canon contributor) enters product
- DoD: ≥$2K/month flowing to cited creators; ≥10 contributors with claimed attribution

---

## 7. V2 sprint arc — Web + multi-vertical (S35+)

Gated on V1 + V1.0 hitting: ≥$10K/month MRR-equivalent + ≥3 unprompted non-trade vertical asks + V1.0 creator settlement clearing ≥$2K/month.

### S35-S36 — Web app at `app.geckovision.tech`

Next.js + Privy embedded wallet. Cross-repo to `gecko-mcpay-app`. Two sprints because UX-heavy: creator OAuth claim, GUI session, document reveal.

### S37 — General-coach at scale

What was S25's branch becomes the primary entry surface. Multi-vertical idea routing (trade / SaaS / consumer / infra) all hitting the same oracle.

### S38 — Public categorized retrieval API

`market_intelligence`, `investment_signals`, `dex` as paid x402 endpoints. External-agent integrations (frames.ag, Bazaar, Apify-style). KaaS positioning fully realized.

### S39+ — Multi-vertical corpus expansion

Beyond DeFi: SaaS GTM canon, consumer-product research literature, infra/devtools market data. Each new vertical = new ProviderKind family + new eval suite.

---

## 8. Five critical-path crossings

Five places where a slip cascades two sprints downstream:

1. **S24 WS-A eval pass → S24 WS-C live flip → S25 retention.** Quality miss in S24 means no live flip, means S25 retention is measured on stub-mode users (vanity).
2. **S24 WS-D events ledger → S26 reliability tuning → V1 cost-per-user claim.** Without real metrics in S24, S26 has nothing to tune and V1 cost claim is a guess.
3. **S26 OpenAPI contract freeze → S35 web app → S38 public API.** Two future sprints depend on the contract being stable.
4. **S28 backtest skeleton → S29 paper-trade gate AND S27 eval rigor.** Backtest is currently scheduled for V0.2 but needs to start in S27 for eval to be defensible. Tension resolved by D5.
5. **S25 discovery sessions → general-coach in S25/S26 (D4) → S37 multi-vertical scope.** Three sprints downstream of one ICP-validation call. Treat S25 sessions as a hard gate with written go/no-go before S26 starts.

---

## 9. Consensus kill list — what we are NOT building

All three agents agreed. Each item: removed from sprint plans through V1.

- **OKX Skill Quality Award submission** — per `feedback_okx_no_funding_pressure`, off the table
- **Web app at app.geckovision.tech before S35** — V2, not earlier (D3 already chose UX work over web app)
- **Voyage AI reranker swap** — ship only if Mongo Atlas Search caps measurably degrade retrieval
- **Streaming verdict UI ("Skeptic engaging…") before S25** — perceived-value, ship inside coach-branch sprint
- **Backtest harness as user-facing surface before V1.0** — internal-only through V1 + V0.2
- **SendAI/Backpack live adapters before S31** — stub through V0.2 (Pattern B)
- **Multi-language (PT/ES) UI** — V3, not earlier
- **Per-query micropayment exposure** — session bundles are the right unit (PRD out-of-scope)
- **Discovery-session productization** — research, not shippable surface
- **Mauboussin / Boyle / Felix canon expansion** — marginal canon dollar goes to market-data or contributor pipeline
- **Paid acquisition before S28 PH gate** — organic CAC must prove first
- **Pricing experiments before n=20 validated Caios** — defer A/B to S27

---

## 10. Opinionated resequence calls from staff-engineer (re-affirmed)

1. **Market-data ProviderKind is a V1 blocker, not v0.2 polish.** Wave-1 dry-run proved 3/7 voices structurally bluff without it. Fix the trade-vertical-expansion doc: ships S24 WS-A, not v0.2.
2. **Backtest harness mis-sequenced in cofounder brief.** Brief puts it v1.0 (3 months); it belongs in S27 skeleton + S29 full. Pull left.
3. **Pioneer journey + partner registry have no V1 design surface.** Already correctly excluded in S24 plan. Reaffirm: pay.sh listing is GTM coordination, not a sprint workstream.
4. **Licensed canon (O'Reilly/Perlego) sequencing right, rationale wrong.** Defer not on cash but because S27 eval v2 will prove whether canon quality is the binding constraint. Wave-1 said market-data is the gap, not "more canon."

---

## 11. What needs to happen next

**To start S25 lane assignments on 2026-05-27, decide:**

- D4: general-coach branch in S25 (recommended), S26 (gated on discovery), or V2 (business-manager push)
- D5: backtest skeleton S27 + full S29 (recommended) or full S29 only
- D6: read-only digest S25 + actionable S27 (recommended) or skip-to-actionable S26

**Tasks remaining open from S24 Wave 1:**

- Task #2 (WS-A eval) — in progress; needs market-data ingest live + prompt-patch re-run
- Task #3 (WS-B runtime) — schema landed; software-engineer can now claim
- Task #4 (WS-C live flip) — scaffolding done; gated on Task #2
- Task #5 (WS-D observability) — pending
- Task #6 (WS-E install polish) — pending
- Task #7 (WS-F panel safety) — pending
- Task #8 (WS-G GTM beat) — pending Day 7

S25 tasks will be created once D4/D5/D6 land. S26+ will be planned in a single batch at end of S24 retro.

---

**References:**
- `docs/strategy/2026-05-12-s24-plan.md` — current sprint
- `docs/strategy/2026-05-11-trade-vertical-expansion.md` — V0.2 design (corrections in §10 above)
- `docs/strategy/2026-05-11-cofounder-roadmap-brief.md` — outcome metrics (corrections in §10 above)
- `docs/PRD.md` — needs V1 metric refresh + V0.2 + V1.0 + V2 sections inserted post-founder D4/D5/D6
- `memory/MEMORY.md` — kill list rationale grounded here
