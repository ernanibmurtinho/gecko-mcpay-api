# Bazaar-as-composer — design review

**Date:** 2026-04-30
**Author:** product-designer
**Inputs:** `docs/research/bazaar-as-composer-2026-04-30.md`, `docs/marketing/landing-copy-v2.md`, `docs/positioning/landing-vs-research-delta.md`
**Status:** Strategy memo. Not a copy spec. Landing v2 is frozen for Sprint 11.

The composer reframe is the strongest positioning move on the table. This memo is the UX commitment list that translates it into surfaces.

---

## 1. The reveal moment

**Recommendation: name the providers, but only after the verdict.**

The reveal magic today is: spinner → debate → VERDICT → citations. Provider names belong in the citations layer, not the spinner layer. Two reasons:

- The spinner is suspense. Listing "Querying Amadeus, FlightAware, Skyscanner..." turns suspense into a procurement log. That's the Zapier failure mode.
- Citations are where trust is paid. A founder who reads "demand signal: 412 routes/day (Amadeus)" trusts the verdict more than one who read "Amadeus" five minutes earlier on a progress bar.

**Concrete spec:**

- Progress phase labels stay vertical-themed, not provider-themed. `"Pulling travel market signals"` not `"Querying Amadeus"`. One label per logical step, not per HTTP call.
- Each cited claim in the Validation Report carries the provider name as dim metadata next to the URL: `[3] 412 daily routes JFK→LHR · Amadeus · $0.05`.
- A new collapsed footer block per document: `SOURCES PAID — Amadeus, FlightAware, Skyscanner · 4 calls · $0.20`. Expand-on-request, not default-open.

**Cost transparency: single total in the spinner, line items in the receipt.** The receipt is where founders forgive cost; the spinner is where they get nervous. Don't itemize mid-flight.

---

## 2. Vertical suite naming + discovery

**Auto-detect, with an override flag.** The idea classifier already exists. Making the founder pick `--vertical travel` puts the cognitive load back on them — exactly what the composer reframe removes. Auto-detect is the product. The flag is the escape hatch when classification is wrong.

```
bb research --idea "..."                  # auto, picks vertical
bb research --idea "..." --vertical travel  # override
bb research --idea "..." --vertical none     # force generic Pro
```

- **Landing page:** vertical suites are a **Pro+ proof block**, not a separate section. One row of vertical chips under the Pro+ card: `travel · fintech · saas · defi`. Clicking a chip swaps the receipt's line items to that vertical's bundle. Same card, four states. Avoids a "configure your stack" page.
- **Skill repo:** one skill that branches on vertical. Separate skill files fragment the bootstrap moment (`Read app.geckovision.tech/skill.md` is the brand). The skill stays singular; the orchestrator branches inside.

---

## 3. Pro+ receipt anatomy

Same monospace block, third card. Travel-vertical example:

```
GECKO PRO+ ─────────────── $1.50 USDC
─────────────────────────────────────────
LLM debate (5 agents) .... $0.0950
Embeddings + flywheel .... $0.0010
Free sources (HN/RDT/GH) . $0.0023
twit.sh judge threads .... $0.0500
Bazaar-routed sources .... $0.2000
  └─ vertical: travel (4 providers)
─────────────────────────────────────────
Cost of goods ............ $0.3483
Margin ................... 77%
─────────────────────────────────────────
PAID ON-CHAIN — Solana mainnet · x402
SETTLED VIA — CDP Bazaar · 4 routed calls
```

**Signal sent by `Bazaar-routed sources ........ $0.20`:**

1. *Gecko didn't scrape this.* Legitimacy. The data has a paid origin.
2. *Gecko isn't reselling Amadeus to you raw.* The markup is on judgment, not data.
3. *This is a category, not a config.* Founder reads "Bazaar-routed" the way they read "embeddings" — a black-box capability they don't tune.

The indented `└─ vertical: travel (4 providers)` is the disclosure. Provider names live one keystroke away (`bb research --idea "..." --show-providers`), not on the receipt by default. If a founder cares, they can ask. Most won't, and that's the whole point.

---

## 4. Anti-pattern check — biggest UX risk

**(c) Latency that breaks the reveal magic.** By a wide margin.

(a) and (b) are copy/layout problems we already solve well — the design above buries provider names. (c) is a physics problem. 4 serial Bazaar calls at 500–2000ms each, plus debate, plus synthesis, pushes Pro+ from "minutes, not weeks" to a 90-second blank stare. The reveal-as-product principle dies at 90 seconds.

**Design against it:**

1. **Parallel by default.** `asyncio.gather` with per-provider 3s timeout. If a provider misses the window, the critic agent flags the gap as a real critique — degradation becomes content. (This is the move the research doc already gestures at; making it a UX commitment, not a fallback.)
2. **Progressive reveal, not block reveal.** Render the Business Plan panel the moment its inputs land. Validation Report panel renders next. PRD last. The founder reads while the pipeline runs. Today's "everything appears at once" is wrong for Pro+ even if it works for Basic.
3. **Latency budget per tier, hard-capped.** Basic: 60s. Pro: 5min. Pro+: 5min. Pro+ is *not* allowed to be slower than Pro on the box. If Bazaar adds latency, we cut elsewhere (smaller debate context, fewer free sources) — we don't ship a slower premium tier.

(b) is real but secondary: as long as we never ship a "pick your providers" panel, the Zapier framing has nowhere to land.

---

## 5. The sub-fold above frames.ag

**Don't broaden. Sharpen.**

"Validation layer above frames.ag" is concrete because frames.ag is concrete. "Validation layer above any x402 facilitator" is mush — founders don't know what a facilitator is, and "any" reads as "none in particular."

**Recommendation for Sprint 13:** keep the frames.ag sub-fold. *Add* a parallel sub-fold one section down:

> **Above the Bazaar, too.** Gecko routes Amadeus, FlightAware, and four other paid services into one verdict. You pay one price. We pay them.

Two sub-folds, same shape, different facilitators. The thesis becomes legible by repetition, not by abstraction. When a third facilitator shows up in Sprint 16, we add a third sub-fold. The pattern *is* the message: Gecko is the judgment layer above whichever execution rail the founder's idea needs.

The unifying headline only earns its abstraction once three concrete sub-folds are stacked under it. Don't write it before then.

---

## UX commitments (the short list)

1. Provider names live in citations and the receipt's disclosure line, never in the spinner.
2. Vertical is auto-detected; `--vertical` is an escape hatch, not a setup step.
3. Pro+ receipt uses one Bazaar line item with an indented vertical disclosure. No per-provider rows by default.
4. Pro+ latency budget = Pro latency budget. Parallel calls, hard timeouts, progressive panel reveal.
5. Sub-fold strategy is additive (frames.ag + Bazaar) until Sprint 16, not unified.

---

## Escalations

- Verdict taxonomy still unresolved (landing-vs-research §1b). Pro+ receipt assumes KILL/REFINE/BUILD lands first. If it doesn't, Pro+ ships with `gap_classification` strings and the receipt copy reads weirder. Flag for `business-manager`.
- Per-call settlement reconciliation across N providers is `web3-engineer` territory once Vector 4 enters scope.
- The "show providers" CLI flag and progressive panel reveal are `software-engineer` work — spec'd here, not built.
