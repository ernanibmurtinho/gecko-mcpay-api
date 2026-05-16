# S29-#39 Phase 2 Cleanup — Handoff

**Date:** 2026-05-13
**Branch:** `s29/canon-purge-and-reingest`
**Scope executed:** Phase 2 only (purge). Phase 3 (re-ingest) is deferred to a separate dispatch.
**Spend:** $0 (Mongo deletes are free, no embedding calls)

---

## Outcome (one-line)

Deleted 3,660 garbage chunks across 7 classes. Garbage rate: 11.5% → 0.018%. Substantive corpus preserved at 28,134 chunks (within 1 of the 28,135 baseline — single twitsh chunk was a fixture that passed the substantive gate but matched the stub URL pattern).

## Before / After

| Metric | Before | After | Delta |
| --- | --- | --- | --- |
| Total chunks | 31,799 | 28,139 | -3,660 |
| Substantive (ok) | 28,135 | 28,134 | -1 |
| `repeated_chars` (binary) | 3,378 | 0 | -3,378 |
| `too_short` | 286 | 5 | -281 |
| Garbage rate | 11.5% | 0.018% | -11.48 pp |

## Per-class deletion log

| Class | Label | Planned | Deleted | Drift |
| --- | --- | --- | --- | --- |
| A | web + binary | 3,229 | 3,229 | 0 |
| B | canon_berkshire + binary | 149 | 149 | 0 |
| C | twitsh stub fixtures | 118 | 118 | 0 |
| D | paysh_live (whole-kind) | 64 | 64 | 0 |
| E | bazaar_manifest thin | 46 | 46 | 0 |
| F | web test fixtures | 10 | 10 | 0 |
| G | assorted thin (incl. web-thin Option B) | 44 | 44 | 0 |
| **Total** | | **3,660** | **3,660** | **0** |

Zero drift across all seven classes. Re-audit ran after every class.

## Per-provider-kind before/after

| provider_kind | before total | after total | delta |
| --- | --- | --- | --- |
| web | 25,634 | 22,361 | -3,273 |
| canon_damodaran | 2,410 | 2,408 | -2 |
| canon_berkshire | 1,674 | 1,525 | -149 |
| protocol_native | 958 | 958 | 0 |
| canon_marks | 649 | 649 | 0 |
| twitsh | 118 | 0 | -118 |
| market_data | 84 | 81 | -3 |
| paysh_manifest | 77 | 72 | -5 |
| paysh_live | 64 | 0 | -64 (whole-kind) |
| bazaar | 54 | 54 | 0 |
| bazaar_manifest | 50 | 4 | -46 |
| bazaar_live | 27 | 27 | 0 |

## Class G Option B extension

Founder accepted Option B at checkpoint-1: Class G now also matches `web` provider_kind chunks with text length < 200 that are NOT already captured by Class A (binary/repeated) or Class F (fixture regex). Net effect was +34 web-thin chunks added to Class G, bringing the Class G planned count from 10 to 44 and the total from 3,626 to 3,660 — within ~6 of the original 3,664 audit prediction.

The per-doc fine-match enforces the exclusion to avoid double-counting:

```python
if code == "G":
    if not _doc_is_thin(text):
        return False
    if provider_kind == "web":
        if _doc_is_binary_or_repeated(text):
            return False
        if _re.match(WEB_FIXTURE_RE, text or ""):
            return False
    return True
```

## Verification

- Dry-run re-run against the committed `2026-05-13-purge-plan.json` matched exactly (3,626 total before Class G extension). Per-class drift was 0 across all seven classes.
- Audit after every class confirmed the planned reduction exactly. Substantive count dropped only by 1 (the twitsh fixture chunk), which is expected because Class C is URL-pattern-based, not quality-gate-based.

## Residual garbage (5 chunks)

After all seven classes, the audit reports 5 `too_short` chunks remaining, all in `canon_berkshire`. These were not in any of the seven planned classes. They are not blocking — 0.018% garbage rate is well under the <1% target. Suggest sweeping in a follow-up dispatch if the team wants to hit a perfect 0%.

## Commits on `s29/canon-purge-and-reingest`

```
4e7c002 chore(s29-#39): purge class G — 44 assorted thin chunks deleted
c432afb chore(s29-#39): purge class F — 10 web test fixture chunks deleted
0ae1e52 chore(s29-#39): purge class E — 46 bazaar_manifest thin chunks deleted
827949a chore(s29-#39): purge class D — 64 paysh_live empty chunks deleted
97b77b9 chore(s29-#39): purge class C — 118 twitsh stub fixture chunks deleted
8e4d43f chore(s29-#39): purge class B — 149 canon_berkshire+binary chunks deleted
531a393 chore(s29-#39): purge class A — 3,229 web+binary chunks deleted
ecbcb46 fix(s29-#39): extend Class G to cover web thin chunks (Option B)
f61ad87 feat(s29-#39): purge_garbage_chunks.py — dry-run + per-URL breakdown
```

## Recommended next move for Phase 3

The corpus is now clean. Phase 3 (canon re-ingest) is the natural next step:

1. **Berkshire 1980-2024 re-ingest** — the 149 binary chunks deleted came from PDF letters that hit the binary-output extractor before the s29-#32 reject gate. The PDF rejection guard now blocks the binary path; re-running `scripts/canon/ingest_berkshire.py` should land HTML chunks for the 1977-1997 letters and clean PDF text for 1998-2024.
2. **Damodaran ERP papers** — 2 chunks from `ERP2011.pdf` / `ERP2012.pdf` came through with table-fragments. Confirm extractor handles these or skip them by URL.
3. **Bazaar manifests** — 46 short-blurb chunks deleted; these were truncated by chunk size, not content quality. Re-ingest with a `min_chars=200` extractor guard so the manifests get the surrounding context bundled.
4. **Pattern A check (already verified):** `canon_mauboussin` and `canon_macro` are already declared as ProviderKind values in `packages/gecko-core/src/gecko_core/sources/types.py`; no drift-test work needed.
5. **Reachability probe per Pattern E:** any re-ingest must be followed by a live `gecko_trade_research` call asserting ≥1 citation per re-ingested provider_kind.

No need to flip `X402_MODE=live` for Phase 3 — re-ingest is OpenAI-embedding spend only.

## Audit artifacts (all on branch)

- `audit_runs/2026-05-13-audit-before.json` — baseline
- `audit_runs/2026-05-13-audit-after-{A..G}.json` — per-class audits
- `audit_runs/2026-05-13-plan-class{A..G}.json` — per-class dry-run plans
- `audit_runs/2026-05-13-purge-log.json` — per-class execution log (planned/deleted/drift)
- `audit_runs/2026-05-13-purge-plan.json` — original combined plan (pre-Option B)
- `audit_runs/2026-05-13-purge-plan-classg-ext.json` — combined plan post-Option B

## Spend

$0. Mongo deletes are free. No embedding calls were made.
