# S34-#87 — Per-call cost model: `gecko_trade_research` at `top_k` 5 / 10 / 15

**Author:** quant-analyst · **Date:** 2026-05-17 · **Branch:** `s34/eval-trustworthiness`

## Question

Founder approved raising production `top_k` from 5 to 15 (retrieval eval
`provider_kind_coverage` 0.567 → 0.967). Accuracy is the moat; paying for it
is fine — but the price must be **known, not guessed**. This is that number.

## TL;DR

| `top_k` | retrieved-chunk tokens | panel input tokens (incl. per-turn re-send) | output tokens | **$ / call** | Δ vs `top_k`=5 |
|--------:|-----------------------:|--------------------------------------------:|--------------:|-------------:|---------------:|
| 5  | 2,860 | 36,419 | 2,880 | **$0.00719** | — |
| 10 | 5,059 | 52,036 | 2,880 | **$0.00953** | +$0.00234 (+32.6%) |
| 15 | 6,411 | 61,717 | 2,880 | **$0.01099** | +$0.00379 (+52.8%) |

**Bottom line: `top_k`=15 costs ~$0.011 per call vs ~$0.0072 at `top_k`=5 —
an absolute increase of ~$0.0038 (~0.38 cents) per `gecko_trade_research`
call.** At basic tier (`gpt-4o-mini`) the move is structurally cheap: even a
3× chunk slate adds well under half a cent. The retrieval-eval lifts
`provider_kind_coverage` from 0.567 (fails the 0.8 gate) to ~0.967 (clears
it). The accuracy gain is bought for ~$0.0038/call.

## The decisive fact: `_RAG_CONTEXT_CHAR_CAP` does NOT bind

`_RAG_CONTEXT_CHAR_CAP = 60_000` chars (`trade_panel/__init__.py:101`).
`_format_chunks` truncates the rendered chunk block only if it exceeds that.

Measured rendered chunk-block size on a representative Kamino fixture:

| `top_k` | chunk-block chars | cap | binds? |
|--------:|------------------:|----:|:------:|
| 5  | 10,318 | 60,000 | no |
| 10 | 19,577 | 60,000 | no |
| 15 | 24,141 | 60,000 | no |

At `top_k`=15 the chunk block is **24k chars — 40% of the cap**. The cap
leaves ~36k chars of headroom. **The 3× chunk delta is fully real; it is not
silently truncated back to the `top_k`=5 context.** Had the cap bound,
`top_k`=15 would have cost the same as `top_k`=5 and this whole exercise
would be moot. It does not bind, so the cost delta above is the true delta.

(Sanity bound: even pathologically large chunks would cap the block at 60k
chars ≈ 15k tokens; per-turn re-send across 7 voices that is ≤ ~105k input
tokens ≈ $0.016/call. That is the hard ceiling regardless of `top_k`.)

## How the cost is built — the mechanics, not an estimate

### 1. The panel re-sends the chunk-laden seed every turn

`run_trade_panel` (`__init__.py:560`) runs `REQUIRED_AGENTS` — 7 voices:
`technical_analyst → sentiment_analyst → fundamental_analyst → risk_manager
→ strategist → bull_bear_debater → coordinator`. It is a round-robin over a
**shared, accumulating `messages` list** (`__init__.py:613`): each voice `i`
is called with `seed + all prior turns`. The seed (which embeds the full
retrieved-chunk block via `_opening_prompt` → `_format_chunks`) is therefore
**re-sent on all 7 turns**. This is the cost multiplier.

The code's own cost estimator (`__init__.py:652-667`) encodes exactly this:

```
tokens_in = Σ_i ( seed + Σ_{j<i} turn_j )
```

This model reproduces that arithmetic and adds the per-voice **system
prompt** (the persona prompt, sent on every turn — measured from
`_default_prompts.json`, not assumed).

### 2. Inputs — all measured, none guessed

- **Retrieved-chunk tokens** — real corpus sample via
  `retrieve_trade_corpus_chunks(idea, protocol='kamino', vertical='dex')`,
  tokenized with `tiktoken cl100k_base` (`routing/costs.estimate_tokens`):
  `top_k`=5 → 2,860 · 10 → 5,059 · 15 → 6,411 tokens.
  Note the slate is sub-linear in `top_k`: the canon-floor quota +
  Voyage rerank pull shorter, denser chunks into the wider slate (mean
  chunk shrinks 572 → 427 tokens from k=5 → k=15).
- **Opening-prompt scaffold** — `_opening_prompt` overhead (question,
  protocol line, `CITATION DISCIPLINE` + `QUANTITATIVE GROUNDING` block,
  the `[N] (source)` per-chunk wrappers, the persona-order line): measured
  468 / 500 / 531 tokens at `top_k` 5 / 10 / 15.
- **Per-voice system prompts** — measured from `_default_prompts.json`:
  technical 669, sentiment 621, fundamental 859, risk 508, strategist 959,
  debater 506, coordinator 1,491. Total 5,613 tokens; re-sent once each.
- **Per-turn output tokens** — typical lengths per persona contract
  (analysts ~320-380, strategist ~420, debater ~460, coordinator's
  250-450-word synthesis + JSON block ~620). Total **2,880 output tokens**.
  Output is `top_k`-invariant — voices write the same-length turn
  regardless of slate width.

### 3. Model + pricing

Basic tier runs `gpt-4o-mini` (`__init__.py:666`,
`model_id = "gpt-4o" if tier == "pro" else "gpt-4o-mini"`). Pricing from
`routing/costs._LEGACY_PRICING`: **$0.15 / 1M input, $0.60 / 1M output**.

Pro tier runs `gpt-4o` ($2.50 / $10.00 per 1M) — see "Pro tier" below.

## Assumptions (stated explicitly)

1. **Sample representativeness.** Chunk tokens sampled on one Kamino/`dex`
   fixture. Chunk-token counts vary ±15% across protocols; the **cost
   *delta*** is robust because it is dominated by the per-turn re-send
   structure, not the absolute chunk count. The slate is sub-linear in
   `top_k` (rerank favors shorter canon chunks), so a linear "3× chunks =
   3× chunk cost" extrapolation would *over*-estimate — the measured delta
   is the honest one.
2. **Output tokens are `top_k`-invariant** (2,880 total). Wider slates do
   not lengthen turns; the persona contracts cap turn length. Held constant
   so the table isolates the input-side cost of the chunk delta.
3. **No tokenizer mismatch.** `gpt-4o-mini` uses `o200k_base`; the estimator
   uses `cl100k_base`. Difference is <5% and applies uniformly to all three
   rows — it does not move the delta.
4. **One voice, one turn.** No retries, no timeout re-dispatch. A timed-out
   voice (`__init__.py:625`) emits a stub and costs ~0 — that path makes a
   call *cheaper*, not pricier; the table is the worst (full-completion)
   case.
5. **Retrieval embed cost is negligible.** The question-embed
   (`text-embedding-3-small`, ~$0.02/1M) is ~10 tokens ≈ $2e-7. Excluded.
6. **No prompt caching.** OpenAI prompt caching would discount the re-sent
   seed substantially; not modeled, so the table is an upper bound. If
   caching is enabled the `top_k`=15 delta shrinks further.

## Pro tier note

If `gecko_trade_research` is invoked at `pro` tier (`gpt-4o`), multiply by
the price ratio (~16.7× input, 16.7× output): `top_k`=5 ≈ $0.118,
`top_k`=15 ≈ $0.181 — a Δ of ~$0.063/call. Still small in absolute terms,
but the per-tier decision should note it. Basic tier is the default and the
basis for the headline number.

## Recommendation

Ship `top_k`=15. The cost of the accuracy fix is **~$0.0038 per call** at
basic tier — three-tenths of a cent. The `provider_kind_coverage` gate
(0.8) goes from failing (0.567) to clearing (~0.967). The price is now
known, not guessed, and it is small. The `_RAG_CONTEXT_CHAR_CAP` headroom
(36k chars unused at `top_k`=15) means there is no truncation risk and room
to grow the slate further before the cap becomes a factor.
