# 2026-05-11 Retrieval Wedge Sprint — Design

**Status:** Design approved. Option C (architectural cleanup) selected by founder 2026-05-11. 1-day implementation sprint.

**Goal:** Make ingested chunks globally retrievable. Drop `session_id` from the Mongo `$vectorSearch.filter`. Relax `RagChunk.source_url` to accept provider-namespaced identifiers. Preserve `session_id` as provenance metadata only.

---

## 1. Surface map

| File | Lines | Change |
|---|---|---|
| `packages/gecko-core/src/gecko_core/db/mongo_reads.py` | 91-141 (`match_chunks_mongo`), esp. **108** | Drop `session_id` from `vs_filter`. Keep `session_id: UUID` in signature for caller compatibility (becomes a no-op parameter; document it as deprecated-for-filtering, used only for logging). |
| `packages/gecko-core/src/gecko_core/db/mongo_reads.py` | 216-308 (`match_chunks_hybrid_mongo`), esp. **243** | Same — drop `session_id` from `vs_filter`. |
| `packages/gecko-core/src/gecko_core/db/mongo_reads.py` | 149-208 (`match_chunks_windowed_mongo`) | **No change.** Already uses `project_id` + `captured_at`, never `session_id`. |
| `packages/gecko-core/src/gecko_core/db/mongo_reads.py` | 316-411 (`match_chunks_filterable_mongo`, `build_filterable_pipeline`) | **No change.** Doesn't use `session_id`. |
| `packages/gecko-core/src/gecko_core/models.py` | 54-76 (`_ALLOWED_CITATION_SCHEMES`, `_validate_citation_uri`) | Add `paysh://` scheme plus a `<ns>/<resource>` regex fallback. Single source of truth — `RagChunk`, `Citation`, `SourceInfo` all import this. |
| `packages/gecko-core/src/gecko_core/ingestion/trading_oracle/run_live_ingest.py` | 106 | **Optional follow-up (not blocking):** wrap `call.listing.get("fqn", "")` in `f"paysh://{fqn}"` so new chunks ship a well-formed URI. Existing 64 paysh_live chunks already in Mongo carry the bare `"paysponge/coingecko"` form — validator must accept that shape OR we backfill. See §6. |
| `packages/gecko-core/src/gecko_core/rag/query.py` | 195-251 (`RagChunk`) | No code change — `RagChunk._check_source_url` already routes through `_validate_citation_uri`; relaxing the central validator is sufficient. |

No Atlas index migration. `session_id` remains in `CHUNKS_VECTOR_LEGACY_FILTER_FIELDS` so the index keeps the path indexed. Removing a field from the query-time filter never requires an index change.

## 2. Filter change spec

**Before** (`mongo_reads.py:108`):
```python
vs_filter: dict[str, Any] = {"session_id": str(session_id)}
if extra_filter:
    vs_filter.update(extra_filter)
```

**After:**
```python
vs_filter: dict[str, Any] = {}
if extra_filter:
    vs_filter.update(extra_filter)
# `session_id` retained on chunks as provenance metadata; no longer a
# retrieval gate (see docs/strategy/2026-05-11-retrieval-wedge-sprint.md).
```

Downstream: the `$vectorSearch` stage must still accept the empty-dict case. Atlas treats `filter: {}` as "no filter" — equivalent to omitting the key. To be defensive, mirror the `match_chunks_windowed_mongo` pattern and only set `vector_search["filter"]` when `vs_filter` is non-empty.

**Identical change at line 243** for `match_chunks_hybrid_mongo`.

## 3. Validator change spec

**Before** (`models.py:61-76`):
```python
_ALLOWED_CITATION_SCHEMES: tuple[str, ...] = (
    "https://", "http://", "bazaar://", "twitsh://",
)

def _validate_citation_uri(value: str) -> str:
    s = str(value).strip()
    for scheme in _ALLOWED_CITATION_SCHEMES:
        if s.startswith(scheme):
            return s
    raise ValueError(...)
```

**After:**
```python
_ALLOWED_CITATION_SCHEMES: tuple[str, ...] = (
    "https://", "http://", "bazaar://", "twitsh://", "paysh://",
)
# Provider-namespaced bare identifiers (e.g. "paysponge/coingecko") — minted
# by paysh_live ingester before URI normalisation lands. Shape: <ns>/<resource>
# with no scheme, no whitespace, no leading slash. Tightens the previous
# "any string" failure mode without forcing a re-ingest of the 64 paysh chunks.
_PROVIDER_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*\/[a-z0-9][a-z0-9_\-\/\.]*$", re.I)

def _validate_citation_uri(value: str) -> str:
    s = str(value).strip()
    for scheme in _ALLOWED_CITATION_SCHEMES:
        if s.startswith(scheme):
            return s
    if _PROVIDER_NAMESPACE_RE.match(s):
        return s
    raise ValueError(...)
```

**Decision rationale (not "any string"):**
- Accepting any string would let hallucinated/empty `source_url` through and break the citation-grounding floor.
- `https://`/scheme prefixes + a single regex for `<provider_ns>/<resource>` covers every real source we ship: web, bazaar, twitsh, paysh, future first-class providers, and the existing in-Mongo paysh chunks.
- Pattern A is preserved — one regex, one tuple, one validator function.

## 4. Test changes

| Test file | Status after change | Action |
|---|---|---|
| `tests/rag/test_query_legacy_filter.py` | Passes unchanged. Asserts `extra_filter` shape, not `session_id`. | None |
| `tests/db/test_insert_chunks_freshness.py` | Passes unchanged. Write-side. | None |
| `tests/db/test_chunk_protocol_content_kind.py` | Passes unchanged. Write-side. | None |
| Orchestration tests calling `rag_query` | Passes unchanged. Mocked or stub stores. | None |
| Models/citation validator tests | Add positive case for `paysh://...` and `paysponge/coingecko`; negative for whitespace / empty / single-token. | New parametrize block |

## 5. Migration risk

- **`rag_query`** — session_id was only used for the filter; downstream `add_cost` etc. continue to work as session-row writes.
- **`match_chunks_windowed_mongo`** — uses `project_id` + `captured_at`. Not session-scoped.
- **Fresh-per-request ingest in `gecko_trade_research`** — wrote chunks tagged with the request's session_id; after the change those still surface alongside canon. **Intended behavior** per knowledge-as-commodity pivot.
- **Ephemeral / private chunks** — no such concept exists today. If introduced, scope via `metadata.private=true` not session_id.

**Conclusion:** no production path requires session-scoped retrieval. The wedge wire was the only consumer and it was structurally broken.

## 6. Backfill / re-ingest decision

**No re-ingest required.**

- Mongo writes are unchanged.
- 4,733 canon chunks have valid `https://` URLs — pass validator unchanged.
- 64 paysh_live chunks carry bare `paysponge/coingecko`-style identifiers — relaxed validator (§3) accepts them as-is.
- Atlas index unchanged.

Founder's "re-ingest if necessary" is a safety valve, not exercised here.

## 7. Ticket breakdown

| # | Title | Size | Owner | Depends on |
|---|---|---|---|---|
| **#52** | data-engineer: drop `session_id` from `$vectorSearch.filter` in `match_chunks_mongo` + `match_chunks_hybrid_mongo`; preserve write-side and signature. Add empty-filter guard. | S | data-engineer | — |
| **#53** | software-engineer: extend `_validate_citation_uri` to accept `paysh://` + `<ns>/<resource>` namespaced form; add unit tests. | S | software-engineer | — |
| **#53b** | software-engineer: paysh ingester writes `paysh://<fqn>` going forward (`run_live_ingest.py:106`). Non-blocking. | XS | software-engineer | #53 |
| **#54** | ai-ml-engineer: verification — direct rag_query for the Kamino question, assert ≥1 chunk per provider_kind across canon_marks/berkshire/damodaran/paysh_live. | M | ai-ml-engineer | #52, #53 |
| **#55** | post-deploy production smoke against `api.geckovision.tech`; confirm citations span ≥3 provider_kinds. | XS | staff-engineer | #52, #53 deployed |
| **#56** (optional) | mongosh one-shot to normalise existing 64 paysh chunks' `source_url`. Only if #54 reveals downstream consumers prefer scheme-prefixed form. | XS | data-engineer | #54 |

Total: 4 required + 2 conditional. One implementation day.

## 8. Verification protocol

**Local / staging command:**

```bash
GECKO_CHUNK_STORE=mongo \
GECKO_RETRIEVAL_HYBRID=true \
GECKO_RERANKER=none \
uv run python -c "
import asyncio
from uuid import uuid4
from gecko_core.rag.query import rag_query
chunks = asyncio.run(rag_query(uuid4(), 'Should USDC be deposited into Kamino given current liquidity conditions?', top_k=12))
from collections import Counter
print(Counter(c.provider_kind for c in chunks))
for c in chunks: print(c.provider_kind, c.similarity, c.source_url[:80])
"
```

**Expected:** ≥1 chunk each from `paysh_live`, plus coverage from canon-tagged chunks. Total ≥8 chunks. No `ValidationError`. Cosine top score ≥0.55.

**Production smoke:**

```bash
./test.sh
```

**Expected:** `citations[]` length >0; ≥2 distinct provider_kind values represented.

**Failure modes to watch:**
- `Atlas error: Path X needs to be indexed as filter` → an `extra_filter` is reaching the query without index support.
- 0 chunks returned → embedding-space mismatch (Voyage 1024 vs OpenAI 1536); verify `GECKO_CHUNK_STORE=mongo`.
- `ValidationError` on a paysh chunk → tighten or loosen the namespace regex.

## 9. Risk register

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| 1 | Per-kind quota under-fetches wedge providers when web prose floods | Medium | Env-tunable `GECKO_RETRIEVAL_PER_KIND_QUOTA`; bump if #54 measurement shows starvation |
| 2 | Cross-session citation pollution | Low (by design) | This *is* the wedge. Tenancy concerns handled via `metadata.private=true` if needed later. |
| 3 | Validator change ripples to `Citation` / `SourceInfo` | Low | Namespace regex requires `<ns>/<rest>` with alphanumerics; bare phrases still fail. Add 2 negative tests. |

## 10. Open questions for founder

1. **Tenancy long-term:** Introduce `tenant_id` filterable path now? Founder pick: defer until needed.
2. **paysh URI normalisation (#56):** Cosmetic. Founder pick: leave bare; validator accepts both.
3. **`session_id` parameter signature:** Keep as no-op + documented vs remove entirely. Founder pick: keep + document.
