# S35-WS1 — citation_relevance diagnosis (why retrieval is good but the judge rates cites ~47%)

**Date:** 2026-05-17
**Ticket:** s35-#90
**Branch:** `s35/citation-relevance`
**Scope:** DIAGNOSIS ONLY. Read the #89 N=30 ship-gate artifacts; explain `citation_relevance mean 0.468, CI [0.422, 0.512]` — a confirmed fail — despite `provider_kind_coverage` locked at 1.00 and the S34-#85 grounding fix. No code changes, no rubric re-run.

**Artifacts read:** the 4 pooled rubric runs behind N=30 (`s34-shipgate-r1/r2/r3/r3-topup`), `tests/eval/scripts/score_defi_trade_rubric.py`, `docs/eval/2026-05-17-s34-shipgate-N15-vs-N30.md`.

---

## TL;DR

The judge is not wrong and retrieval is not broken. **The panel cites all 15 retrieved chunks indiscriminately, and 6 of those 15 are cross-cutting investor-canon chunks that are tangential by construction to any specific Solana DeFi trade question.** The judge — correctly, per its own rubric ("0.5 = roughly half are tangential canon boilerplate") — scores that mix at ~0.40-0.47. The number is honest. The fix is to stop citing tangential canon, not to re-tune the judge.

The snippet cap is **NOT** the culprit (quantified in §4 — only ~3% of citations lose load-bearing content off-screen). The canon-mix problem is. There is also a real data-quality bug riding alongside it (§ cause 2: 9.3% of cited snippets are unreadable mojibake) but it is not the primary driver of the 0.47.

---

## The numbers behind the diagnosis

**Per-fixture citation_relevance (3 draws each, judge temp=0):**

| fixture | protocol | n=3 mean | values |
|---|---|---|---|
| drift-jto-perp-short | drift | **0.200** | 0.20, 0.20, 0.20 |
| jupiter-jlp-vs-jitosol | jupiter | 0.367 | 0.35, 0.40, 0.35 |
| kamino-jitosol-vault | kamino | 0.400 | 0.35, 0.40, 0.45 |
| kamino-usdc-pyusd-roll | kamino | 0.400 | 0.40, 0.40, 0.40 |
| kamino-jlpusdc-entry | kamino | 0.433 | 0.40, 0.40, 0.50 |
| jupiter-lst-rotation-msol | jupiter | 0.550 | 0.55, 0.55, 0.55 |
| kamino-sol-leverage-entry | kamino | 0.550 | 0.55, 0.55, 0.55 |
| sanctum-unstake-vs-hold | sanctum | 0.550 | 0.55, 0.55, 0.55 |
| jito-mev-tip-band | jito | 0.583 | 0.65, 0.55, 0.55 |
| drift-sol-perp-long | drift | 0.650 | 0.65, 0.65, 0.65 |

It is **not** all 10 evenly and it is **not** per-protocol — both drift fixtures and both jupiter fixtures straddle the spread. The variance is *per-question*, driven by how on-topic the protocol_native chunks happen to be (drift-jto retrieved 6 ETH-PERP funding chunks for a JTO question — see cause 3) and whether the judge spots a canon chunk it considers "loosely relevant."

**Citation composition — identical across all 30 rows:**

- Every row cites **exactly 15 chunks** (min=15, max=15). The panel cites the *entire* `top_k=15` retrieval set — zero selection.
- Every row's 15 split **9 protocol_native + 6 canon** (`canon_berkshire` ×2, plus one each of `canon_marks / canon_macro / canon_damodaran / canon_mauboussin`).
- Aggregate: 270/450 protocol_native (60%), 180/450 canon (40%).

**The judge's own rubric** (`_JUDGE_SYSTEM`, score_defi_trade_rubric.py L191-194):
> "1.0 = every cite is on-topic; **0.5 = roughly half are tangential canon boilerplate**; 0.0 = citations are completely unrelated."

With a fixed 40% canon fraction and canon being genuinely cross-cutting (not question-specific), the judge's rubric *mathematically anchors* the score near 0.5 and below. The measured 0.468 is exactly what this rubric returns for a "9 on-topic + 6 tangential" mix. **The eval is telling the truth.**

---

## Ranked diagnosis

### Cause 1 — PRIMARY — the panel cites all 15 chunks; 6/15 (40%) are tangential canon by construction

**Evidence.** All 30 rows: `n_citations=15`, composition `9 protocol_native + 6 canon`, invariant. The panel does not select — it cites the whole retrieved set. Canon chunks are, by the trade-vertical design (CLAUDE.md: "General-knowledge chunks (canon) carry `protocol=[]` … surfaces for ALL protocols"), deliberately cross-cutting. They reach the panel correctly (that is what `provider_kind_coverage=1.00` certifies) — but a Berkshire 1987 shareholder letter or a Damodaran tracking-stock passage is *tangential to every specific trade question by design*.

Judge notes confirm this verbatim, every fixture:
- kamino-jlpusdc-entry: *"Citations 10-15 are canon boilerplate … only the Marks memo (14) is loosely relevant … Roughly 60% of citations are off-topic or only loosely relevant."*
- jupiter-jlp-vs-jitosol: *"citations 10-15 are static canon boilerplate (Berkshire letters, Oaktree memo, Damodaran, BIS, Mauboussin) with **zero relevance** to the JitoSOL/JLP rotation question, dragging the score down significantly."*
- kamino-jitosol-vault: *"citations 10-15 are generic canon boilerplate … roughly 40% of citations are tangential."*

The canon chunks are retrieved and cited *correctly* — the wedge wants canon in the panel's *reasoning context*. The bug is that the **panel promotes its entire context window into the `citations[]` envelope** with no relevance gate. Reasoning context ≠ citation. A citation list is a claim of "these sources support this verdict"; 6 of 15 do not.

**Fix (lane: ai-ml).** Decouple "context the panel reads" from "citations the panel emits." The panel/coordinator should emit a citation **only for a chunk it actually drew a claim from**, not the whole retrieved set. Two viable shapes:
- (a) Cheapest: cap emitted citations at the protocol_native subset for protocol-specific trade questions, OR rank-and-trim to top ~6-8 by retrieval score; canon stays in context but is not echoed into `citations[]` unless a turn explicitly references it.
- (b) Better: a post-panel citation-attribution pass — keep a chunk in `citations[]` only if its content is referenced by ≥1 agent turn. The persisted `turns[]` (S34-#69) already make this checkable offline.

This is the **single highest-ROI fix** — see §5.

### Cause 2 — SECONDARY (real data bug) — 9.3% of cited snippets are unreadable mojibake; all `canon_berkshire`

**Evidence.** 42/450 cited snippets (9.3%) are binary garble — e.g. `cit10` in drift-jto-perp-short: `"J��q�}Fy�%����{wA..."`. **Every single garbled snippet is `provider_kind=canon_berkshire`** (42/42). The Berkshire letters ingested as PDFs that did not text-extract cleanly — likely scanned/encrypted-stream pages stored as raw bytes. The judge flags this directly: drift-sol-perp-long notes *"canon boilerplate … with garbled/binary content."* A garbled chunk cannot be relevant to anything; it can only drag the score.

This compounds Cause 1: not only is canon 40% of citations, ~1 in 4 of the *canon* citations (≈2 per row, both Berkshire) is literal noise. Fixing this alone lifts citation_relevance modestly but does not solve the 0.47 — it removes the worst ~2 of the 6 bad cites, not all 6.

**Fix (lane: data).** Re-ingest the Berkshire canon corpus with a working PDF text extractor; add an ingest-time UTF-8/printable-ratio guard that rejects any chunk whose non-printable character ratio exceeds a threshold (the diagnostic used >15%). This is a `data-engineer` corpus-quality ticket. Until fixed, these chunks should not be eligible for retrieval at all.

### Cause 3 — TERTIARY — protocol_native retrieval pulls wrong-instrument market data for some questions

**Evidence.** drift-jto-perp-short (citRel 0.20, the suite floor): the JTO-PERP question retrieved **6 of its 9 protocol_native chunks as ETH-PERP funding records** (`cit4-9`), plus INJ-PERP and JUP-PERP price chunks (`cit2-3`). Judge: *"citations 4-9 (ETH-PERP funding rates, not JTO-PERP) … citations 2-3 are other perp markets … No tokenomics/unlock schedule source is cited at all, which is the core of the thesis."* So even the *protocol_native* half — assumed on-topic — is only ~3/9 on-topic for this fixture. That is why drift-jto sits 0.25-0.45 below every other fixture.

This is a retrieval-ranking miss inside `protocol_native`: market-data chunks for the wrong instrument outrank the right one (or the right instrument has no chunk). It is narrow — drift-jto is the only fixture this severe — but it explains the single worst score and confirms Cause 1's fix must rank-trim, not just drop canon (a blind "keep all 9 protocol_native" still keeps 6 wrong-instrument chunks here).

**Fix (lane: data, with ai-ml on the trim filter).** Instrument-aware retrieval filtering for protocol_native market-data chunks — match the instrument/market token in the question to the chunk's `market` field. Short-term, Cause 1's rank-and-trim with a relevance threshold partially mitigates it.

### Cause 4 — NOT a cause — judge snippet cap is not the culprit (see §4)

### Cause 5 — NOT a cause — the judge rubric is calibrated reasonably

The rubric definition (0.5 = half tangential) is a defensible BEIR-style mid-band calibration. The judge applies it consistently (per-fixture sd ≈ 0.02-0.05 across 3 temp-0 draws) and its prose rationale matches its scores. It is *strict but correct*. Re-tuning the rubric to score the current citation mix as a pass would be vanity-tuning — it would hide Cause 1, not fix it. Leave the rubric alone.

---

## §4 — Is the judge snippet cap the culprit? Decisive: NO.

The judge sees `(c.snippet or "")[:200]` — a **200-character cap** (score_defi_trade_rubric.py L264).

Snippet length across all 450 cited chunks: **min 83, median 240, mean 235, max 240.** The citation snippet is *itself* already capped at ~240 chars upstream (chunk→citation path). So the judge's [:200] removes at most **40 trailing characters** from a 240-char snippet.

What is in those last 40 chars? For protocol_native chunks (60% of all cites), characters 200-240 are almost entirely the **trailing source URL** — e.g. `"…Source: https://api.kamino.fina"` (cut mid-URL) or `"…rift.trade/stats/markets/prices."`. The load-bearing content — the API name, the metric, the figure — sits in chars 0-150. For canon chunks the last 40 chars are mid-sentence boilerplate continuation, no more relevant than the visible part.

**Quantified:** of 450 citations, ~435 (96.7%) exceed 200 chars, but the off-screen 40 chars are **the URL tail or mid-sentence filler in ~100% of inspected cases** — zero citations were found where a *relevant, score-changing* fact lives only in chars 200-240. The cap costs the judge nothing it needs.

The deeper point: the upstream **240-char snippet cap is the real (mild) information limit**, not the judge's 200. But even at 240 chars the protocol_native chunks fully convey their content (API + metric + figure), and the canon chunks are tangential regardless of length. Widening either cap would not move citation_relevance — a tangential Berkshire chunk is tangential at 240 chars and at 2400. **Snippet cap: ruled out.** Do not spend the fix budget here.

---

## §5 — Single highest-ROI fix

**Stop emitting tangential canon into `citations[]`. Lane: ai-ml.**

Rationale:
- It is the *largest* lever: canon is a **fixed 40% of every citation list** and the judge's rubric pins the score near 0.5 for a "half tangential" mix. Removing the 6 canon cites from the *emitted* list (they stay in panel context) takes each row from "9 on-topic + 6 tangential" to "≈9 on-topic" — the rubric's 1.0 anchor. Projected citation_relevance moves from ~0.47 toward ~0.75-0.85, clearing the 0.50 bar with real margin.
- It is **cheap and self-contained**: a coordinator-side filter on the citation envelope, no re-ingest, no model swap, no rubric change. No new spend to ship — only to verify.
- It does not regress the wedge: canon still reaches the panel's *reasoning* context (that is `provider_kind_coverage`, which stays 1.00 — coverage measures retrieval reach, not the emitted list). We are fixing what we *claim as evidence*, not what the panel *reads*.
- Causes 2 and 3 are real but smaller and slower (corpus re-ingest, instrument-aware retrieval — both `data-engineer`). They should be filed, but Cause 1's fix alone is projected to flip citation_relevance green.

**Recommended sequencing:** ai-ml ships the citation-emit filter (Cause 1) → data files Berkshire re-ingest (Cause 2) + instrument-aware market-data retrieval (Cause 3) as parallel `data-engineer` tickets. Re-run the N=30 ship-gate only after Cause 1 lands (one rubric run, ~$3) — do not burn a run per cause.

**Caveat to verify on the next run:** trimming citations could touch `dissent_grounding` / `confidence_calibration` if those judge axes lean on the citation list. They should not (the judge reads `turns[]` for dissent), but confirm on the post-fix N=30.

---

## Lane summary

| Cause | Fix | Lane | Priority |
|---|---|---|---|
| 1. Panel cites all 15, 40% tangential canon | Citation-emit relevance filter / attribution pass — emit only chunks a turn used | **ai-ml** | **HIGHEST ROI** |
| 2. 9.3% of cited snippets are mojibake (all canon_berkshire) | Re-ingest Berkshire with working PDF extractor + ingest-time printable-ratio guard | data | high (real bug, smaller lever) |
| 3. protocol_native pulls wrong-instrument market data (drift-jto) | Instrument-aware retrieval filter for market-data chunks | data | medium (narrow — 1 fixture) |
| 4. Judge 200-char snippet cap | None — ruled out (off-screen 40 chars = URL tail / filler) | n/a | not a cause |
| 5. Judge rubric calibration | None — leave it; re-tuning would hide Cause 1 | n/a | not a cause |
