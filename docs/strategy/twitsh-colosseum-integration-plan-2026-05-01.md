# twit.sh × Colosseum-judges as a research-time source

**Date:** 2026-05-01
**Author:** staff-engineer
**Driver:** user prompt — consume Colosseum hackathon judges' twit.sh posts as a primary research signal for crypto/Solana-flavored ideas.

---

## 1. Why this matters strategically

Per `docs/strategy/bazaar-deeper-thesis-2026-04-30.md`, **Gecko owns the discrimination layer.** Discrimination compounds when it consumes other people's discrimination signal as input.

Colosseum judges are domain-expert humans publicly judging Solana submissions in real time. Their twit.sh posts are exactly the kind of signal Gecko's critic + judge agents would use a $50/hr analyst to harvest manually: "who in the ecosystem is saying this idea is dead, and what did they cite?" That signal directly informs verdicts in the categories Gecko already fires twit.sh against — `crypto`, `defi`, `hackathon-team` (see `_FIRES_FOR` at `packages/gecko-core/src/gecko_core/sources/twit_sh.py:50`).

The compounding loop: Gecko's verdict on a Solana-flavored idea cites Colosseum-judge commentary → the verdict reflects current Solana judgment plus Gecko's adversarial debate → repeat-validators come back to Gecko (S16 pulse repeat-rate gate) because no other surface gives them this synthesis. This is the Vector 5 / "trust artifact" story (`bazaar-deeper-thesis` §2) applied to inputs, not just outputs.

This is also the only twit.sh use case where the $0.05/session cap is *clearly* worth paying. Generic Twitter signal on a regulated-healthcare idea is noise. A specific judge's commentary on a specific submission cluster is high-density evidence.

---

## 2. The right architectural seam

twit.sh today is a one-off `SourceResult`-returning module read directly by the eval gate path. To make it a research-time source it needs three things:

1. **Wrap as `TwitshProvider(SourceProvider)`** per `packages/gecko-core/src/gecko_core/ingestion/providers/__init__.py` (Sprint 12 Track F seam already shipped). `name = "twitsh"`, `kind = "first-party"` (we own the wallet + the cap, not Bazaar). `cost_estimate(query)` returns `ASSUMED_PER_CALL_USD * MAX_RESULTS` capped at `SPEND_CAP_USD = $0.05`. `health()` checks `_is_twitsh_configured()`. `fetch(query)` is the existing dispatcher, normalized to `list[SourceChunk]`.

2. **Wire into `provider_router.py`** (Sprint 13 planned, not yet shipped — explicit dependency). The router consults the classifier's vertical/category output and includes `TwitshProvider` in the provider plan when the idea matches.

3. **Classifier rule for the Colosseum-judge filter.** When `category ∈ {crypto, defi, hackathon-team, agent-economy}` AND idea contains Solana-adjacent keywords (`solana`, `colosseum`, `breakpoint`, `radar`, `cypherpunk`, `breakout`, `renaissance`), the provider plan sets `use_twitsh: True` AND attaches an `author_allowlist: COLOSSEUM_JUDGES` filter. Outside this match, twit.sh either skips or runs unfiltered (depending on category — defi without Solana keywords still uses twit.sh, just without the allowlist).

4. **Citation rendering piggybacks on Sprint 13 Track D** (creator attribution: `creator_handle`, `creator_payout_usd`, `creator_wallet` on `Citation`). For twit.sh citations, `creator_handle = @author`, `creator_payout_usd = $0.005` (per-call assumption), `creator_wallet = None` (we don't pay the judge — we pay twit.sh; the handle is attribution only). The footer block PD designed renders cleanly.

---

## 3. Implementation sketch

```python
# packages/gecko-core/src/gecko_core/ingestion/providers/twitsh_provider.py
class TwitshProvider:
    name = "twitsh"
    kind: ProviderKind = "first-party"

    def __init__(self, author_allowlist: frozenset[str] | None = None):
        self._allowlist = author_allowlist  # e.g. COLOSSEUM_JUDGES

    async def cost_estimate(self, query: str) -> float:
        return min(ASSUMED_PER_CALL_USD * MAX_RESULTS, SPEND_CAP_USD)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(available=_is_twitsh_configured())

    async def fetch(self, query: str) -> list[SourceChunk]:
        # delegate to existing twit_sh.fetch(); post-filter by author if set
        results = await _fetch_existing(query)
        if self._allowlist:
            results = [r for r in results if r.author_handle in self._allowlist]
        return [_to_chunk(r) for r in results]
```

```python
# packages/gecko-core/src/gecko_core/orchestration/provider_router.py (S13 planned)
def build_provider_plan(idea: str, category: str, vertical: str | None) -> ProviderPlan:
    plan = ProviderPlan(providers=[FreeProvider()])
    if _is_solana_adjacent(idea) and category in TWITSH_CATEGORIES:
        plan.providers.append(TwitshProvider(author_allowlist=COLOSSEUM_JUDGES))
    elif category in TWITSH_CATEGORIES:
        plan.providers.append(TwitshProvider())
    return plan
```

**Cost gate.** Surface to the CLI before firing, per the `bb economics` pattern: `Estimated x402 spend: $0.05 (twitsh, ≤10 reads, Colosseum-judge filter)`. User confirmation only required if total session estimate exceeds the tier price — for Pro at $0.75, twit.sh's $0.05 is rounding error and never blocks.

**Cache reuse.** The existing 6h Mongo cache in `twit_sh.py` (`CACHE_TTL_SECONDS`) handles dedup automatically. Cache key already incorporates idea + categories. Adding the allowlist to the cache key is a one-line change so Colosseum-filtered runs don't poison the unfiltered cache and vice versa.

**Allowlist source.** Static JSON at `packages/gecko-core/src/gecko_core/sources/colosseum_judges.json`, refreshed manually per hackathon cycle (~quarterly). Format: `{"renaissance_2026": ["@judge1", "@judge2", ...], "radar_2026": [...]}`. Provider reads the union of active cycles. Refreshing is a `business-manager` task tied to each Colosseum announcement.

---

## 4. Sprint sequencing

**Recommendation: slot in S14 alongside ParagraphProvider, +1 day add. Not a separate sprint.**

Argument: S14 already plans `ParagraphProvider` as the first paid `SourceProvider` instance per `roadmap-sprint-13-to-17-synthesis` § "S14". Both are paid providers under the same Protocol. The orchestration cost (provider router wiring, classifier extension, citation rendering) is *one-time* — Paragraph pays it; twit.sh slipstreams. The only twit.sh-specific work is (a) the wrapper class, (b) the allowlist JSON + classifier rule, (c) cache-key augmentation. Three discrete tasks, ~3 days for one engineer.

If S14 is scope-stressed — and it might be, given Paragraph + pulse v1 + creator citation surfacing all land that sprint — defer to S15. Don't go later than S15 because by then Cloudflare consumer-side ships and the provider count gets crowded; landing twit.sh in the same sprint as Cloudflare risks a 2-provider regression hunt.

---

## 5. Risks

1. **twit.sh API stability for non-eval traffic.** Today twit.sh fires once per eval-gate run. Research-time traffic is 1-3 orders of magnitude higher. The in-flight probe agent tonight is testing exactly this; their findings gate this work. *Mitigation:* twit.sh provider goes behind a feature flag (`TWITSH_RESEARCH_ENABLED`, separate from the eval-gate `TWITSH_ENABLED`) so we can dark-launch.

2. **Author allowlist staleness.** Colosseum judges change between hackathons. A stale list silently drops good signal. *Mitigation:* allowlist JSON has an `updated_at` field; provider logs WARN when allowlist is >180 days old. business-manager owns the refresh.

3. **Cost variance.** Judges may post in bursts (during demo days especially). The $0.05/session cap is hard, but if 50 sessions/day all hit it, that's $2.50/day on twit.sh alone — not catastrophic but worth tracking. *Mitigation:* `bb economics --provider twitsh` rolls up daily spend; circuit-breaker (S12 Track I-04 pattern) at $10/day twit.sh aggregate.

4. **Brand risk.** A judge says something controversial → Gecko's cited evidence echoes it → Gecko looks like it's endorsing the take. *Mitigation:* citation rendering shows `via @judge on twit.sh` clearly; the critic agent's prompt already instructs it to attribute opinions, not adopt them. Audit one live run before public launch.

5. **Compounding signal becomes echo chamber.** If Gecko cites Solana judges and the verdicts shape Solana-builder behavior, we may be reinforcing existing ecosystem priors rather than discriminating against them. *Mitigation:* this is real but V3-level concern; flag in `docs/strategy/option-set.md` and revisit at S17.

---

## 6. Concrete S14 ticket

### S14-TWITSH-01 — `TwitshProvider` + Colosseum-judge allowlist

**Owner:** software-engineer (provider class + classifier extension) + business-manager (allowlist JSON + refresh runbook)

**Estimate:** 3-5 days

**Dependencies:**
- S13 Track F (provider_router.py) shipped
- S13 Track D (Citation creator fields) shipped
- S14 ParagraphProvider scaffold shipped first (twit.sh slipstreams the same shape)

**Acceptance:**
- [ ] `TwitshProvider` class lands at `packages/gecko-core/src/gecko_core/ingestion/providers/twitsh_provider.py`, conforms to `SourceProvider` Protocol, passes a stub-mode unit test with a fake author allowlist
- [ ] `colosseum_judges.json` lands at `packages/gecko-core/src/gecko_core/sources/colosseum_judges.json` with at least 5 verified judge handles for the current Colosseum cycle, plus a `updated_at` field and refresh runbook at `docs/runbooks/colosseum-judges-refresh.md`
- [ ] `provider_router.py` extended: when `category ∈ {crypto, defi, hackathon-team}` AND idea matches Solana-keyword regex, plan includes `TwitshProvider(author_allowlist=COLOSSEUM_JUDGES)`; outside that branch, plan uses unfiltered `TwitshProvider` if category matches
- [ ] Cache key in `twit_sh.py` includes `allowlist_hash` so filtered/unfiltered runs don't collide
- [ ] `TWITSH_RESEARCH_ENABLED` feature flag gates the provider; off by default in V1
- [ ] Citation rendering shows `creator_handle = @judge` inline; `creator_payout_usd = $0.005` rendered in footer (reuses S13 Track D surface, no new render code)
- [ ] One stub-mode test: `bb research --idea "Solana DEX with adversarial sandwich-protection" --tier basic` with `TWITSH_RESEARCH_ENABLED=1 X402_MODE=stub` produces a citation with `creator_handle` set
- [ ] One live-mode smoke (gated on `TWITSH_RESEARCH_ENABLED=1` + funded TWITSH wallet) settles ≤$0.05 against twit.sh and surfaces ≥1 judge-authored citation
- [ ] Eval gate (`bash scripts/run_eval_gate.sh`) still passes ≥0.80 with provider on AND off

**Out of scope:**
- Twit.sh listing in CDP Bazaar (provider is consumer-side only; we read judges, we don't re-list them)
- Pulse-time twit.sh refresh (S15+: pulse delta on Solana ideas could re-fire twit.sh, but plumbing lives with S15 pulse delta work)
- Paying judges directly (we pay twit.sh; judge attribution is render-only, no on-chain payout)

**Reversibility:** two-way. Provider behind a flag, allowlist is data, no schema migration.
