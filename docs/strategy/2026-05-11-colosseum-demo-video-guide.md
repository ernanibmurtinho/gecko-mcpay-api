# what colosseum judges actually look for in your demo video

> Based on public-record evaluations from 34 Colosseum judges across 4+ Solana hackathon cycles. Sources: Adam (@adamdelphantom), Billy (@twentyOne2x), Gui Bibeau (@GuiBibeau), Qiao (@QwQiao), plus aggregated mentor threads from Colosseum Arena, Alliance DAO, Solana Incubator. All quotes are verbatim from public X threads.

A demo video is the most-scrutinized artifact you'll ship. The judge isn't reading your README — they have your 90 seconds, your terminal, and your tx hash. Here's what cuts through.

---

## the 4 evaluation axes

The judges aren't scoring against a rubric. They're each running a different mental model. To clear every panelist, hit all four.

### 1. clarity in 5 seconds — gui's lens

> "Who is this for? What does it do? Can I answer both before the second sentence of voice-over?"

Gui Bibeau (Solana products, ex-Vercel, ex-MetaMask) reviews landing pages for Colosseum publicly. His pattern: **if he can't name the user segment and the value prop in the first frame, he marks the video as unclear.**

what lands:
- title card with the 1-liner, on screen, before the demo starts
- specific user named in the first voice-over sentence ("vibe traders with $500 of conviction capital")
- one value-prop sentence, no hedging

what kills it:
- "AI agent platform" / "DeFi infrastructure" / any 4-syllable abstract noun
- "the future of X" framing
- 15+ seconds of context before something visible happens

### 2. evidence of category-specific PMF — adam's lens

> "Greenfield (new market) or iterative (existing analog)? I want different evidence types for each."

Adam (Phantom, SteelCitySolana, Colosseum mentor) publishes his framework on X. He explicitly accepts iterative projects — but **only when they prove organic user feedback loops, not airdrop farmers.**

what lands:
- real numbers from production: "X transactions settled on mainnet, Y dollars spent, Z prod smoke tests passing"
- one specific user moment ("I lost money on my last 3 trades")
- naming where you sit explicitly ("we're iterative — strategy oracle layer above the marketplace") — judges respect founders who don't pretend to be greenfield

what kills it:
- TVL projections, "millions of users" claims, airdrop-farming traction
- vague "we have users" without a number or a name
- claiming greenfield when an obvious analog exists

### 3. brag with context — billy's lens

> "Bullish on founders actively seeking feedback. Strong pitch + GitHub team page wins initial attention."

Billy (attn.markets, prior Colosseum winner) quotes Dave Hsu's framework: **team / market / product / distribution + "brag with context."** Numbers without context are noise; context without numbers is fluff.

what lands:
- every metric tied to a verifiable artifact (Solana tx hash, public smoke test URL, GitHub repo)
- public commit history visible somewhere in the video — the README contributors graph, or a `git log` flash
- a "we shipped X in Y" moment with both numbers

what kills it:
- claims without artifacts
- "Stealth mode" / "DM for details"
- founder team in the credits but not a single line about why the team
- raw self-promotion without an outcome

### 4. force of will — qiao's lens

> "Contrarian thinking, willingness-to-be-wrong, lean-honest mode."

Qiao (Alliance DAO) calibrates on founder posture. **Defaults to "unclear" when no founder context appears.** The signal is willingness to admit a recent failure honestly.

what lands:
- one sentence about a bug you caught + shipped + validated, in the demo voice-over
- a moment where the demo "doesn't lie" — the verdict says `defer` (not always `act`), the dissent survives on screen
- a contrarian framing if it's true ("the marketplaces can't ship this themselves — they'd have to pick a side")

what kills it:
- all-wins narrative
- claims of inevitability
- demo where everything looks too smooth — judges read this as either staged or shallow

---

## the cross-cutting criteria — what every judge checks

From the bulk public-feedback dataset across 34 judges, six patterns appear across every track:

1. **traction / metrics** — visible, verifiable
2. **payment proof** — for anything touching $: a tx hash, a settled receipt, an x402 settle log
3. **1-liner clarity** — gut-test in <5 seconds
4. **demo over deck** — judges want to see it run, not promised
5. **moat** — name it explicitly; don't make them infer it
6. **regulatory awareness** — one slide / one sentence acknowledging the regime, especially for stables / RWA / on-chain finance

---

## anti-patterns judges flag publicly

- **Overdone Web3 aesthetics** — gradient hero, glowing buttons, generic "blockchain" graphics. Gui rejects this on sight.
- **Feature overload** — "social network + Linktree + super-app + on-chain reputation." Pick one. Show one.
- **Hidden technical depth** — if your contracts, docs, GitHub aren't reachable in <2 clicks, judges assume they don't exist.
- **Broken links** — Gui called this out by name on FlowBack. A demo with a broken "read docs" link is a tell.
- **Generic LLM persona claims** — "5 specialist voices debate your idea" without surfacing dissent and citations on screen reads as marketing copy. The dissent has to be visible.

---

## the 90-second structure that lands

| time | beat | what's on screen |
|---|---|---|
| 0–5s | title card | 1-liner, monospace, no animation |
| 5–15s | hook | specific user pain, named, one sentence |
| 15–35s | the wedge in motion | the product doing the thing, on-camera, with verifiable artifact (tx hash, citation, dissent count) |
| 35–55s | the durable surface | "now it runs while I sleep" / local / no API keys |
| 55–70s | the category claim | where you sit relative to incumbents, named |
| 70–85s | the force-of-will moment | one sentence about a bug caught + shipped + validated |
| 85–90s | CTA | URL + one-line install |

---

## one-line close

Build the video for one viewer: a tired judge who has watched 40 submissions before yours, who will mute on first abstract noun, and who will rewind for a real tx hash. Everything else is decoration.
