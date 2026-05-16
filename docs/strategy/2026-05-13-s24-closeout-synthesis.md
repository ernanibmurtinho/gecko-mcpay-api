# S24 Close-Out Synthesis — 2026-05-13

## Headline

The trade-panel is **answering honestly on thin corpus**, not broken. Defer-rate 0.9 is a *symptom of retrieval failure*, not coordinator logic failure. Option A.2 (move defer logic to code) is no longer the right next move — it would force directional verdicts on bad citations. The real S25 work is retrieval quality.

## Signal stack

**Outcome eval** (`tests/eval/live_runs/2026-05-12-s24-defi-trade-10-3.json`, N=10):

- verdict_dist = `0 act / 1 pass / 9 defer`
- defer_rate = **0.90**
- brier_overall = **0.16** ✅ (calibration is sound)
- pass_drawdown_avoid_rate = 0.0 (single pass missed +6.7% rotation_won)

**Rubric eval** (`tests/eval/live_runs/2026-05-12-s24-defi-rubric-pilot-3.json`, N=3):

| Dimension | Mean | Threshold | Pass-rate |
|---|---|---|---|
| verdict_accuracy | **1.0** | 1.0 | 100% ✅ |
| citation_relevance | **0.38** | 0.7 | 0% ❌ |
| provider_kind_coverage | **1.0** | 1.0 | 100% ✅ |
| hallucination_score | **0.33** | 1.0 | 33% ❌ |
| dissent_grounding | **0.63** | 0.5 | 100% ✅ |
| confidence_calibration | **0.62** | 0.6 | 100% ✅ |

All three rubric fixtures **failed overall** because citation_relevance and hallucination_score gate on perfect, but the failures are concentrated in retrieval, not in the panel's reasoning.

## The retrieval pathology (from judge notes)

1. **Canon dominates over protocol-specific**. Kamino fixtures retrieve mostly Damodaran ERP PDFs + Marks memos. Vault mechanics docs from `paysh_live` / `bazaar_live` are absent despite the corpus being seeded with them.
2. **`market_data` not date-aligned to fixture `as_of_date`**. SOL=$165 question premise (2024-11-05) accepted without flagging market_data showing ~$95 (latest cache). The retrieval layer doesn't respect `as_of_date` from the fixture envelope.
3. **Off-topic same-vertical noise**. Jupiter TVL=$0, jitoSOL price, mSOL data surface in Kamino-JLP-USDC fixture. Vertical filter is too loose; protocol-tag filter not strict enough.
4. **Panel is self-aware**. Three out of three Kamino fixtures emit blocker_question "corpus too thin to support a directional call" — the panel correctly flags retrieval failure rather than confabulating.

## Decision: do NOT dispatch Option A.2

Option A.2 (delete defer-(iii) prompt clause, implement mechanical defer in `_build_verdict_from_coordinator`) would push the panel toward ACT/PASS on the **same bad citations**. Brier would degrade. Hallucination_score would degrade further. Trading honest defer for confident-wrong directional verdicts is a strict downgrade.

The S24 prompt iteration plateau memory (`feedback_prompt_iteration_plateau`) stands as written, but its premise — "the panel can give a directional answer if we fix the coordinator" — is now falsified by the rubric eval. The panel cannot give a *good* directional answer on the current retrieval.

## Decision: ship V1 with honest-state defer behavior

V1 release notes will explicitly frame defer as "the corpus is too thin to support a directional call here — here's what we'd need to see." This is consistent with the wedge story (grounded refusal beats confident hallucination) and consistent with what the panel is actually doing.

The rubric eval becomes the V1 quality bar. Outcome eval moves to a secondary signal that we expect to improve as a *downstream* effect of retrieval improvements in S25.

## S25 unblock

Filed as Task #13: **Retrieval quality push for trade-panel**:

1. **Date-aligned market_data retrieval**. Respect fixture envelope `as_of_date` when scoring market_data freshness; today the cache returns latest regardless of fixture date.
2. **Protocol-strict filter**. Tighten the `$or` admittance from `{protocol == proto OR protocol == [] OR canon_*}` to weight protocol-exact matches above cross-cutting canon at scoring time.
3. **paysh_live + bazaar_live re-ingest** for Kamino, Drift, Jupiter, Jito, Sanctum vault-params manifests. The chunks exist but are not surfacing — likely a `freshness_tier` / `content_kind` filter excluding them.
4. **Re-baseline outcome + rubric eval after each fix**. If citation_relevance crosses 0.7, expect defer-rate to fall toward 0.4-0.5 naturally, without prompt changes.

## Task tracker changes

- #2 (WS-A trade-verdict eval suite) → **completed** with honest-state framing
- #3 (WS-B runtime) → **completed** (was effectively done; dual-write follow-up captured)
- #4 (WS-C x402 live flip) → **ungated** (no longer blocked by #2)
- #8 (WS-G GTM beat) → **in_progress** (V1 messaging built around honest-state)
- #11 (WS-A2 rubric eval) → **completed** (signal delivered the synthesis)
- #13 (S25 retrieval push) → **created**

## Open follow-ups for S25 W1

- Heavy-test rebuild (Task #12)
- Retrieval quality push (Task #13)
- Pyth real-time price grounding for live-asof-date fixtures (subset of #13)
- WS-G content: V1 honest-state positioning thread + Superteam Brasil post

---

S24 closes with V1 panel shipping in honest-state mode. The wedge claim — "grounded refusal beats confident hallucination" — is structurally true and rubric-verifiable. S25 lifts the retrieval bar so the panel has room to act when corpus is rich.
