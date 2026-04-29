# Pro Judge + Critic prompt v5.4 — recency-bias flip + quota cap

## Motivation

Four consecutive Judge-only iterations failed to lift `verdict_accuracy` to
the v4 baseline of **0.85** on the general suite:

| Bundle | Live run | accuracy | kill_rate |
|---|---|---|---|
| v5.0 | `tests/eval/live_runs/2026-04-28-general.json` | 0.55 | 0.95 |
| v5.1 | `tests/eval/live_runs/2026-04-28-general-2.json` | 0.65 | 0.85 |
| v5.2 | `tests/eval/live_runs/2026-04-28-general-3.json` | 0.65 | 0.65 |
| v5.3 | `tests/eval/live_runs/2026-04-28-general-4.json` | **0.50** | 0.80 |

v5.3 was the worst of the run. The slimming/structural fixes that v5.1→v5.3
introduced were correct in isolation but compounded into a failure mode the
Judge-only patches could not address.

## Failure modes addressed

1. **Critic over-anchoring.** v5.3's Critic instruction said `must produce >= 2
   named, idea-specific kill criteria`. That quota seeded the downstream
   Architect / Scoper / Judge with kill bullets even on ship-shaped ideas, so
   STEP 4 had something to anchor on regardless of merit.
2. **STEP 4 recency bias.** v5.3's KILL block (STEP 4) was 13 keyword triggers
   immediately before STEP 5 (default SHIP). gpt-4o-mini reads sequentially
   and over-weights the most recent block — so the Judge effectively "default
   killed" once it hit the trigger wall, even when STEP 5 should have fired.
3. **STEP 2 brittleness.** v5.3 had 12 named-entry keyword triggers with
   multi-keyword AND conditions. gpt-4o-mini missed partial matches —
   `Carta-aware cap table diff` failed v5.2's `cap-table-diff` trigger because
   of dash-vs-space, and the v5.3 keyword fix only papered over the worst
   instances; `bad-uber-for-dogwalkers` and `bad-nft-loyalty` flipped to
   false-SHIP because STEP 4 triggers also became unreliable under the
   expanded list.

## The three Judge structural changes

### 1. STEP 2 collapsed to a single named-buyer + named-artifact rule

**Before (v5.3):** 12 entries, each with its own `Trigger: idea text
contains (...)` keyword combo. Brittle, easy to miss partial matches.

**After (v5.4):** one rule —

> If the idea text names a buyer noun (engineers, founders, vets, ...) AND
> names a specific artifact / API / corpus / format (Stripe, Postgres, NIH,
> Carta, Datadog, Helius, x402, Airtable, ClinicalTrials.gov, FAA Part 107,
> SEC EDGAR, PubMed, GitHub, HubSpot, Salesforce, Notion, Twilio, Kafka, ...)
> AND Scoper said V1_FEASIBLE_IN_4_DAYS=yes → SHIP V1 to that buyer, EXIT.

The 12 dash-cased entries (`cap-table-diff`, `stripe-replay`, ...) survive as
a single appendix line, explicitly labeled "illustrative, NOT gates."

### 2. STEP 3 is now the DEFAULT SHIP (was STEP 5 in v5.3)

The pipeline goes STEP 1 (precedent) → STEP 2 (named SHIP) → **STEP 3 (default
SHIP)** → STEP 4 (KILL exception). STEP 4 is now the LAST block the model
reads, but its trigger list is short and unambiguous (see #3) so the recency
bias works *for* the default-builder-pilled stance instead of against it.

### 3. STEP 4 trimmed from 13 triggers to 4 hard-kill triggers

Every retained trigger names the kill class in one sentence:

- (a) Uber/Slack/Airbnb-for-X with no liquidity wedge.
- (b) AI-therapy / AI-tax / AI-legal-advice with no licensed professional in
  loop.
- (c) Generic GPT-wrapper on saturated commodity vertical (todo apps, resume
  builders, meeting summarizers, blog generators, social schedulers, email
  assistants — named).
- (d) Consumer-social-with-token / new-L1 / NFT-loyalty-for-SMB.

If none of (a)-(d) match, STEP 4 explicitly re-emits STEP 3's SHIP. There is
no longer a sub-pipeline of fallback "no buyer noun → KILL" rules.

## Critic cap

> produce 1-3 named risks AND exactly one 'Change my mind by:' clause naming
> the single piece of evidence that would flip you to SHIP. Do NOT generate
> kill criteria for the sake of meeting a quota.

Replaces the v5.3 ">= 2 named, idea-specific kill criteria" quota. The
`FLOOR-LEVEL skepticism` block is dropped entirely; commodity-saturation
patterns are delegated to Judge STEP 4 with a one-liner ("say so once and move
on — Judge STEP 4 handles it").

The regulation-as-moat vs regulation-as-kill distinction is kept verbatim — it
is correct and the live runs do not show it misfiring. The V1 source weighting
list (twit_sh / gecko_precedent / hn / reddit / colosseum) is collapsed from
the v5.3 four-paragraph form into four one-liners.

## Analyst / Architect / Scoper

Inherited verbatim from v5.3. The four live runs show no diagnostic evidence
of failures originating in those three agents — every miss traced back to the
Judge's STEP 4 over-firing or the Critic's quota-seeded kill list.

## Expected eval delta

Qualitative — the user re-runs the live gate after this lands.

- v5.3 false-SHIPs (`bad-uber-for-dogwalkers`, `bad-nft-loyalty`) should flip
  back to KILL because STEP 4 (a) and (d) are now narrow, unambiguous, and
  the only thing standing between the default SHIP and the verdict.
- v5.3 false-KILLs across the named-buyer entries should flip to SHIP because
  STEP 2's collapsed rule no longer requires partial-string keyword matching
  — `engineers + Postgres + V1_FEASIBLE=yes` is enough.
- Net direction: kill_rate drops from 0.80 toward the suite's expected
  ~0.50–0.55 baseline; verdict_accuracy lifts toward >= 0.85.

If the live run regresses again, the most likely cause is the STEP 4 (c)
saturation list being too narrow — extend the named patterns there before
touching the pipeline structure.

## Rollback

```bash
GECKO_PRO_PROMPTS_VERSION=v5.3   # 5-step pipeline + per-entry keyword triggers
GECKO_PRO_PROMPTS_VERSION=v5.2   # numbered execution pipeline (no triggers)
GECKO_PRO_PROMPTS_VERSION=v5.1   # parallel SHIP/KILL sections
GECKO_PRO_PROMPTS_VERSION=v5     # pre-2026-04-28 baseline
GECKO_PRO_PROMPTS_VERSION=v4     # original
```

The v5.3 JSON is intentionally retained on disk so a one-env-var rollback is
always available without a code change.
