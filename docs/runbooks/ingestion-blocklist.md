# Ingestion blocklist runbook

Owner: data-engineer.
Module: `packages/gecko-core/src/gecko_core/ingestion/blocklist.py`.
Tests: `packages/gecko-core/tests/ingestion/test_blocklist.py`.

## What this is

Two layers that protect the index from name-collision contamination:

1. **Hard blocklist** — `is_blocked(url, title)`. Compiled regex patterns
   matched against URL strings (and optionally page titles). A match drops
   the URL before embedding. Wired into:
     - `ingestion/discovery.py` (Tavily post-fetch filter)
     - `ingestion/pipeline.py::_process_one` (web/youtube/adapter path)
     - `ingestion/pipeline.py::ingest_provider_chunks` (Arxiv / Bazaar /
       twit.sh provider path)

2. **Soft collision flag** — `flag_collision_risk(text, url)` /
   `annotate_collision_risk(chunks, url)`. Tags chunks whose text mentions
   "Gecko" but whose URL is off our canonical hosts with
   `metadata.collision_risk: true`. Advisory only — retrieval scoring
   does not consume this field today.

The canonical pattern tuple is `BLOCKLIST_PATTERNS` in `blocklist.py`. Per
CLAUDE.md Pattern A, this is the single source of truth: the seed regexes,
the named identifiers logged on a match, and the entries the tests assert
against all reference this constant.

## When to use which

| Situation | Use |
| --- | --- |
| A specific URL is contaminating verdicts (academic-Gecko paper, competitor blog) | Hard blocklist |
| A whole domain is low-quality / irrelevant for our research surface | Hard blocklist (env var or seed entry) |
| Chunks mention "Gecko" off-domain but the source might still be useful (e.g. a HN comment debating us vs. another product) | Collision-risk flag (keep, but tag) |

## Add a new pattern

### Option A — code-level (preferred for permanent additions)

Add an entry to `BLOCKLIST_PATTERNS` in
`packages/gecko-core/src/gecko_core/ingestion/blocklist.py`:

```python
BlocklistPattern(
    name="some-unique-id",
    regex=re.compile(r"^https?://(?:www\.)?bad-host\.com/", re.IGNORECASE),
),
```

Names must be unique across the tuple. Order matters: more-specific
patterns first so the logged `pattern=` field names the precise offender.

Then add a unit test in `tests/ingestion/test_blocklist.py` asserting the
pattern blocks the offending URL and does **not** block a similar-but-clean
URL.

### Option B — hot-fix via env var

Set `GECKO_INGEST_BLOCKLIST_EXTRA` to a comma-separated list of regex
*sources*. Each entry is compiled with `re.IGNORECASE`. Synthesised names
are `env-extra-0`, `env-extra-1`, ...

```bash
export GECKO_INGEST_BLOCKLIST_EXTRA='competitor-corp\.com/.*,bad-host\.io/.*'
```

Invalid regex sources are dropped with a warning — a typo will not take
ingestion offline.

## Verify a URL is blocked

One-liner from the repo root:

```bash
uv run python -c "from gecko_core.ingestion.blocklist import is_blocked; \
print(is_blocked('https://arxiv.org/html/2602.19218'))"
```

Expected: `(True, 'arxiv-gecko-2602')`.

For env-extra patterns:

```bash
GECKO_INGEST_BLOCKLIST_EXTRA='bad-host\.com/.*' uv run python -c \
"from gecko_core.ingestion.blocklist import is_blocked; \
print(is_blocked('https://bad-host.com/page'))"
```

Expected: `(True, 'env-extra-0')`.

## Verify the wire-in is reaching ingestion

A blocked URL emits a structured INFO log line:

```
ingest.blocklist.match url=<URL> pattern=<NAME> source_provider=<PROVIDER>
```

After deploying, run a smoke ingestion and grep the logs:

```bash
bb research --idea "smoke test" 2>&1 | grep ingest.blocklist.match
```

Per CLAUDE.md, "wired" ≠ "reaches the model" — confirm the log line is
emitted at the actual call site (not just unit-tested in isolation).

## Post-deploy: clean up already-indexed offenders

The blocklist filters at *ingestion* time. URLs that already landed in the
index before this shipped will keep coming back from retrieval. Operator
cleanup is a separate one-shot script. Minimal form (Mongo path,
post-S18):

```python
# scripts/evict_blocklisted_chunks.py
from gecko_core.ingestion.blocklist import is_blocked
from gecko_core.db.mongo_chunks import get_chunks_collection

coll = get_chunks_collection()
to_delete: list[str] = []
for doc in coll.find({}, {"_id": 1, "source_url": 1, "title": 1}):
    blocked, _ = is_blocked(doc.get("source_url") or "", doc.get("title"))
    if blocked:
        to_delete.append(doc["_id"])

if to_delete:
    coll.delete_many({"_id": {"$in": to_delete}})
print(f"evicted {len(to_delete)} chunks")
```

Run once after deploying a new pattern. Re-run only when adding new
patterns that target already-indexed URLs.

## When this is NOT the right tool

- **Down-weighting collision-risk chunks at retrieval time** — separate
  ticket. The flag is shipped as advisory metadata; retrieval scoring is
  unchanged today.
- **Full content-similarity dedup** — not in scope. The blocklist is URL +
  title pattern matching; semantic dedup is a different ticket.
- **Re-ingesting the corpus** — operator step, see the eviction script
  above.
