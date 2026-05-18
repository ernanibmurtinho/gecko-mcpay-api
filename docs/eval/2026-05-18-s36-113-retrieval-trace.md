# S36-#113 — Retrieval-Lane Trace for the #110 Ship-Gate Hallucination Failure

Date: 2026-05-18 · Branch: `s36/prompt-grounding` · Lane: retrieval (paired with #114 panel lane)
Status: read-only diagnostic. No commit by this doc's author — parent commits #113 + #114 together.

## Scope

The S36 N=50 ship-gate (#110) returned 3/6 with `hallucination_score` stuck at 0.34.
This doc traces the FRONT of the chain: generated query -> chunks retrieved -> what the
panel was handed. The panel-decision half is #114.

## Fixture selection (deterministic, shared with #114)

Ranked all 10 #110 fixtures by **mean `hallucination_score` across the 5 runs**
(`2026-05-18-s24-defi-rubric-s36-110-shipgate-r{1..5}-10.json`). Note: in this rubric
`hallucination_score = 1.0` is GOOD (no hallucination), `0.0` is worst.

| rank | fixture | protocol/vertical | per-run scores | mean |
|---|---|---|---|---|
| 1 | kamino-jitosol-vault | kamino/dex | 0,0,0,0,0 | 0.000 |
| 2 | jupiter-jlp-vs-jitosol | jupiter/dex | 0,0,0,0,0 | 0.000 |
| 3 | jupiter-lst-rotation-msol | jupiter/dex | 0,0,0,0,0 | 0.000 |
| 4 | jito-mev-tip-band | jito/dex | 0,0,0,0,0 | 0.000 |
| 5 | sanctum-unstake-vs-hold | sanctum/dex | 0,0,0,0,0 | 0.000 |

Five fixtures tie at exactly 0.000 — they ARE the bottom 5. #114 must converge on this
same set. (Remaining six: drift-jto-perp-short 0.50, kamino-sol-leverage-entry 0.60,
kamino-usdc-pyusd-roll 0.60, drift-sol-perp-long 0.70, kamino-jlpusdc-entry 1.00.)

## Retrieval path traced

`retrieve_trade_corpus_chunks` (`orchestration/trade_panel/__init__.py:1141`).
For each fixture: embed the fixture `text` with `embed(input_type=None)`, run the
`$vectorSearch` + `$match` pipeline (`numCandidates=600`, `top_k=15`), Voyage rerank.
Env sourced for Mongo + Voyage. No panel LLM run — embeddings + Mongo only.

Note: the Voyage reranker **timed out (2.5s) on every fixture** and fell back to
vector order. This does not change slate *membership* (same 15 chunks) but it does
mean the cross-encoder re-rank that S33-#79 shipped was not exercised in this trace.
Flagged for follow-up but not a hallucination root cause.

## Per-fixture trace

Every fixture returned a **full 15-chunk slate** — no empty, no degenerate, no
near-blank retrieval. The provider_kind mix was identical in shape across all five:
**9x `protocol_native` + 6x canon** (`canon_marks/damodaran/berkshire/macro/mauboussin`).

### 1. kamino-jitosol-vault — "Park idle SOL into the Kamino JitoSOL-SOL vault for 30d. Outperform straight JitoSOL holding?"
- 15 chunks: 9 protocol_native + 6 canon. Top score 1.065.
- protocol_native = Kamino `kamino-market` (Jito 10x leverage pool pubkey) + 8x
  `kamino-staking-yields` rows (per-mint APY: JitoSOL 0.0543, mSOL 0.0592, etc).
- On-topic: YES. The vault exists in the corpus; per-LST staking APYs are present.
- Gap: NO chunk states a vault-vs-straight-hold *differential*. The fixture
  `must_not_hallucinate` explicitly demands a `paysh_live`/`market_data` source for
  any APY-differential figure. **Zero `market_data` and zero `paysh_live` chunks in
  the slate.** The differential the question hinges on simply is not in any corpus.
- #110 panel cited 5x protocol_native, 0 canon as evidence. Judge flagged the
  bull-bear voice misreading `0.0594` as "0.0594%". That figure IS in chunk [3] —
  this is a panel arithmetic/units error, not bad retrieval.

### 2. jupiter-jlp-vs-jitosol — "Rotate 100% of a JitoSOL stack into JLP for 30d? Or stay in JitoSOL?"
- 15 chunks: 9 protocol_native + 6 canon. Top score 1.114.
- protocol_native = Jupiter LST price snapshot, `tokens/v2/tag` JitoSOL price/mcap,
  plus 6x Jupiter **Lend** docs (unwind / multiply / oracles).
- Half the protocol_native slate is Jupiter Lend leverage-loop docs — tangential to a
  JLP-vs-JitoSOL rotation (judge independently flagged this; citation_relevance ~0.55).
- The figures the #110 judge flagged as hallucinated — `$173M` liquidity, `-2.33%`
  price change — **ARE present in the retrieved corpus**: chunk [0]
  (`jupiter-lst-prices`) literally contains `...VusJm.liquidity: 173030991.85` and a
  `priceChange24h` field. So this is NOT confabulation-from-nothing.
- The real defect: the panel cited those figures to `[1]` (a `tokens/v2/tag` price
  chunk) instead of to chunk `[0]` where they actually live. Mis-attribution, not
  invention. Retrieval supplied the number; the panel pointed at the wrong source id.

### 3. jupiter-lst-rotation-msol — "Rotate 50% of an mSOL position into bSOL via Jupiter for 30d? bSOL yield+points narrative active."
- 15 chunks: 9 protocol_native + 6 canon. Top score 1.090.
- protocol_native = LST price snapshot + bSOL/mSOL `tokens/v2/tag` price/mcap + 6x
  Jupiter Lend docs + a perps-root chunk.
- bSOL and mSOL price + market cap are present and on-topic.
- Gap: there is NO bSOL *yield* or *points-program* data anywhere in the slate. The
  `tokens/v2/tag` chunks carry price/mcap only. The #110 judge flagged the coordinator
  claiming "active yield narrative supporting bSOL [4]" where [4] is a bare price
  snippet. That is a panel over-claim — but it is enabled by retrieval handing over a
  slate with no yield/points chunk to ground (or refute) the narrative.

### 4. jito-mev-tip-band — "P75 tip band reasonable, or is P90 needed for the next 7d?"
- 15 chunks: 9 protocol_native + 6 canon. Top score 1.104.
- protocol_native = 5x Jito `tip_floor` API chunks + 4x Jito low-latency docs.
- STRONGEST retrieval of the five. Chunk [4] gives clean percentile figures
  (25th 0.000001 / 50th 0.00000208 / 75th 0.0000053 / 95th 0.0001 / 99th 0.00025913
  SOL). Chunks [0],[1],[5] carry the raw `landed_tips_75th_percentile` JSON incl.
  `5.04575e-06` and `8.8785e-06`.
- The lamport figures the #110 judge flagged as ungrounded (`8.8785e-06`,
  `5.04575e-06`) **ARE in the retrieved corpus** — chunk [1] and chunk [0]
  respectively. Retrieval did its job here.
- The defect is downstream: multiple Jito tip-floor snapshots from *different
  as-of dates* (2026-05-13, -05-14, -05-16) are in the same slate with conflicting
  percentile values. The panel mixed figures across snapshots and could not name a
  single grounded source -> its own grounding gate flagged them as unverified.
  That is a panel disambiguation failure, not empty retrieval.

### 5. sanctum-unstake-vs-hold — "Holding 200 INF. Unstake now via the Sanctum router, or hold 30d?"
- 15 chunks: 9 protocol_native + 6 canon. Top score 1.133.
- protocol_native = 9x Sanctum docs (infinity technical/non-technical, router,
  reserve, optimal-LST-state, native-to-liquid). All on-topic, all Sanctum.
- The `26.12% APY` figure the #110 judge flagged as "ungrounded, no such citation in
  the evidence list" **IS in the retrieved corpus** — chunk [5]
  (`sanctum-infinity-technical`): "...during the market flash-crash on October 10,
  2025, INF earned a 26.12% APY epoch return."
- The defect: that is a one-off historical crash-epoch figure, NOT a forward 30d
  yield. The panel lifted it as if it were a current yield estimate. Retrieval
  surfaced a real but contextually wrong number; the panel failed to read the
  "October 10 flash-crash" qualifier. The chunk was also not in the panel's final
  `evidence_citations[]` — so the panel cited a figure from a chunk it dropped.

## Founder's named failure: empty / too-few / degenerate / off-topic retrieval

- **Empty / near-blank retrieval: NONE.** All 5 fixtures returned a full 15 chunks
  with healthy scores (1.0-1.13). No fixture was starved.
- **Too-few protocol_native: NONE.** 9 protocol_native per fixture — well above any
  quota floor.
- **Off-topic retrieval: PARTIAL, two fixtures.** jupiter-jlp-vs-jitosol and
  jupiter-lst-rotation-msol both pulled 6x Jupiter *Lend* leverage-loop docs into a
  *rotation/LST* question. Tangential, dilutes the slate, but not the cause of the
  hallucinated *figures*.
- **The real structural gap: a whole provider_kind class is missing from every
  slate.** Zero `market_data` and zero `paysh_live` chunks reached the panel on any
  of the 5 fixtures. Every fixture's `must_not_hallucinate` rule explicitly says
  specific APY / peg / yield / spread figures must be grounded in a
  `paysh_live`/`market_data` source. The panel is structurally unable to satisfy that
  rule because that corpus class never arrives. This is the S24 WS-A
  `provider_kind: market_data` admit-clause firing but matching **no documents** —
  the market_data corpus is either un-ingested or empty for these protocols.

## Cross-ref: #110 evidence_citations vs what retrieval offered

| fixture | retrieval offered | panel cited (#110) | mismatch |
|---|---|---|---|
| kamino-jitosol-vault | 9 protocol_native + 6 canon | 5 protocol_native, 0 canon | panel ignored all 6 canon chunks it was handed |
| jupiter-jlp-vs-jitosol | 9 PN (3 on-topic, 6 Lend) + 6 canon | 6 protocol_native | panel cited figures to wrong chunk id |
| jupiter-lst-rotation-msol | 9 PN + 6 canon | 2 protocol_native | panel narrowed to 2 of 15; claimed yield from a price-only chunk |
| jito-mev-tip-band | 9 PN (5 tip-floor) + 6 canon | 2 protocol_native | panel mixed figures across 3 date-stamped snapshots |
| sanctum-unstake-vs-hold | 9 PN + 6 canon | 4 protocol_native, 0 canon | panel cited 26.12% from chunk [5] which it then dropped from evidence |

Pattern: in every case retrieval handed the panel MORE and BETTER context than the
panel used. The panel consistently (a) ignored the canon chunks, (b) narrowed to
2-6 chunks, (c) mis-attributed or mis-contextualized real figures.

## Headline

**Bad retrieval is NOT the root cause of the #110 hallucination failure.**

For all 5 worst fixtures, retrieval returned a full, healthy, on-topic 15-chunk
slate. Every figure the judge flagged as "hallucinated" was traced back into the
retrieved corpus text — `$173M`, `-2.33%`, `8.8785e-06`, `5.04575e-06`, `26.12%` all
exist in chunks that retrieval surfaced. The panel did not invent numbers from
nothing. The failures are downstream, in the panel lane (#114):

1. **Mis-attribution** — citing a real figure to the wrong chunk id (jupiter-jlp).
2. **Mis-contextualization** — lifting a historical crash-epoch APY as a forward
   yield (sanctum); reading `0.0594` as `0.0594%` (kamino).
3. **Snapshot confusion** — merging conflicting figures across 3 date-stamped Jito
   tip-floor snapshots and failing to name a single grounded source.
4. **Ignoring supplied context** — dropping all 6 canon chunks; claiming a yield
   narrative from a price-only chunk.

## Two retrieval-lane defects worth a ticket (contributing, not root cause)

- **D1 — `market_data`/`paysh_live` corpus is empty for these protocols.** Every
  `must_not_hallucinate` rule names `paysh_live`/`market_data` as the required ground
  for specific figures; zero such chunks reach the panel. The panel is set up to fail
  the rubric's own grounding requirement. Either ingest that corpus or relax the
  rubric to accept `protocol_native` figures. Cross-lane call — escalate to
  `staff-engineer` + `ai-ml-engineer`.
- **D2 — same-endpoint multi-date duplicates in one slate.** Jito tip-floor and
  Jupiter Lend chunks appear 3-5x with different `as_of` dates and conflicting
  numbers. Retrieval should de-dup-by-endpoint to the freshest, or the panel must be
  told which snapshot is canonical. Currently the panel picks arbitrarily.
- **D3 (minor) — Voyage reranker timed out (2.5s) on all 5 fixtures.** Slate fell
  back to vector order. Membership unchanged so not a hallucination cause, but the
  S33-#79 cross-encoder is silently not running under this latency. Ops follow-up.

## Verdict for the founder

Do not spend more eval on retrieval tuning. The retrieval lane is sound for these 5
fixtures — full slates, on-topic, figures present. The `hallucination_score=0.34`
plateau is a panel-grounding / citation-discipline problem (#114), with one genuine
cross-lane contributor: the missing `market_data`/`paysh_live` corpus class (D1),
which makes the rubric's grounding clause unsatisfiable as written.
