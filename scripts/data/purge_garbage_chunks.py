r"""S29-#39 — Targeted purge of garbage chunks (dry-run by default).

Per the 2026-05-13 founder-run audit (31,799 chunks, 11.5% garbage), this
script removes the seven classification buckets confirmed as not-useful
for retrieval. **Default mode is dry-run; deletes require `--confirm`.**

The seven classes (per founder approval):
    A. web              + binary    (PDF extraction failures)
    B. canon_berkshire  + binary    (149 chunks; PDF-bytes leaked through)
    C. twitsh           stub URIs   (provider_kind=twitsh, twitsh://session/* URLs)
    D. paysh_live       all         (64 empty-body chunks; whole-kind purge)
    E. bazaar_manifest  thin        (text length < 200)
    F. web              test fixtures (text exactly matches ^chunk text \d+$)
    G. assorted         too_short   (paysh_manifest + market_data + canon_damodaran
                                     + remaining canon_berkshire + remaining web
                                     too_short not already caught by F)

Pattern A: routes through ``chunks_collection()`` — never hardcodes db or
collection name.

Pattern E ("wired ≠ reaches the model"): after every purge, re-run
``audit_existing_chunks.py`` to measure post-state.

Run examples:

    # dry-run (default) — produces audit_runs/<date>-purge-plan.json
    uv run python scripts/data/purge_garbage_chunks.py

    # execute one class
    uv run python scripts/data/purge_garbage_chunks.py --confirm --class A

    # execute all classes (one by one with per-class log)
    uv run python scripts/data/purge_garbage_chunks.py --confirm --class all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("purge_garbage_chunks")

# --------------------------------------------------------------------------
# Class definitions — each entry is the explicit Mongo filter plus a label.
#
# We keep these here (declarative) so the dry-run + execute paths share the
# exact same filter. Tweaking thresholds = edit in one place.
# --------------------------------------------------------------------------

# regex to detect web test-fixture chunks like "chunk text 0", "chunk text 12"
WEB_FIXTURE_RE = r"^chunk text \d+$"


@dataclass(frozen=True)
class PurgeClass:
    code: str
    label: str
    # Mongo filter (motor-compatible).
    filter: dict[str, Any]
    # Expected count from the 2026-05-13 audit (sanity check during dry-run).
    expected_approx: int


# We will refine some classes (e.g. binary detection) by streaming and using
# the quality gate verdict rather than a single Mongo predicate, because
# "binary" / "repeated_chars" are not directly indexable. The filter below
# is the *pre-filter* sent to Mongo; the *fine* match runs per-doc.


@dataclass
class ClassPlan:
    code: str
    label: str
    expected_approx: int
    matched_count: int
    sample_urls: list[tuple[str, int]]  # (source_url, count)
    sample_chunks: list[dict[str, Any]]
    # ids slated for deletion (collected during dry-run for stable replay).
    ids_to_delete: list[Any]


def _build_classes() -> list[PurgeClass]:
    return [
        PurgeClass(
            code="A",
            label="web + binary (PDF extraction failures)",
            filter={"provider_kind": "web"},
            expected_approx=3229,
        ),
        PurgeClass(
            code="B",
            label="canon_berkshire + binary",
            filter={"provider_kind": "canon_berkshire"},
            expected_approx=149,
        ),
        PurgeClass(
            code="C",
            label="twitsh stub fixtures (twitsh://session/*)",
            filter={
                "provider_kind": "twitsh",
                "source_url": {"$regex": "^twitsh://session/"},
            },
            expected_approx=117,
        ),
        PurgeClass(
            code="D",
            label="paysh_live (whole-kind purge; all 64 empty)",
            filter={"provider_kind": "paysh_live"},
            expected_approx=64,
        ),
        PurgeClass(
            code="E",
            label="bazaar_manifest thin (text length < 200)",
            filter={"provider_kind": "bazaar_manifest"},
            expected_approx=46,
        ),
        PurgeClass(
            code="F",
            label="web test fixtures (^chunk text \\d+$)",
            filter={
                "provider_kind": "web",
                "text": {"$regex": WEB_FIXTURE_RE},
            },
            expected_approx=44,
        ),
        PurgeClass(
            code="G",
            label="assorted too_short (paysh_manifest+market_data+canon_damodaran+leftovers)",
            filter={
                "provider_kind": {
                    "$in": [
                        "paysh_manifest",
                        "market_data",
                        "canon_damodaran",
                    ]
                }
            },
            expected_approx=10,
        ),
    ]


def _doc_is_binary_or_repeated(text: str) -> bool:
    """Identify class A/B binary garbage using the same gate as ingest."""
    from gecko_core.ingestion.chunk_quality import assess_chunk_quality

    verdict = assess_chunk_quality(text)
    if verdict.is_substantive:
        return False
    return verdict.reason in {"binary", "repeated_chars"}


def _doc_is_thin(text: str, *, min_chars: int = 200) -> bool:
    return not text or len(text.strip()) < min_chars


def _doc_matches_class(code: str, text: str) -> bool:
    """Per-class fine-match logic applied per doc after Mongo pre-filter.

    A/B: keep only chunks the quality gate flags binary OR repeated_chars.
    C:   already fully expressed via Mongo filter (regex on source_url).
    D:   whole-kind purge — accept all.
    E:   thin text (<200 chars).
    F:   already fully expressed via Mongo regex on text.
    G:   thin text (<200 chars) — too_short bucket.
    """
    if code in {"A", "B"}:
        return _doc_is_binary_or_repeated(text)
    if code in {"E", "G"}:
        return _doc_is_thin(text)
    return True  # C, D, F


async def _plan_class(
    coll: Any,
    pc: PurgeClass,
    *,
    sample_limit: int = 5,
    top_urls: int = 10,
) -> ClassPlan:
    matched = 0
    url_counter: Counter[str] = Counter()
    sample_chunks: list[dict[str, Any]] = []
    ids: list[Any] = []

    cursor = coll.find(
        pc.filter,
        projection={"_id": 1, "text": 1, "source_url": 1, "provider_kind": 1},
        batch_size=200,
    )
    async for doc in cursor:
        text = doc.get("text") or ""
        if not _doc_matches_class(pc.code, text):
            continue
        matched += 1
        ids.append(doc["_id"])
        url = doc.get("source_url") or "<no-url>"
        url_counter[url] += 1
        if len(sample_chunks) < sample_limit:
            sample_chunks.append(
                {
                    "_id": str(doc["_id"]),
                    "source_url": url,
                    "provider_kind": doc.get("provider_kind"),
                    "text_preview": text[:120].replace("\n", " "),
                }
            )

    return ClassPlan(
        code=pc.code,
        label=pc.label,
        expected_approx=pc.expected_approx,
        matched_count=matched,
        sample_urls=url_counter.most_common(top_urls),
        sample_chunks=sample_chunks,
        ids_to_delete=ids,
    )


async def _execute_class(coll: Any, plan: ClassPlan, *, batch: int = 500) -> int:
    """Delete plan.ids_to_delete in batches. Returns actual deleted count."""
    total = 0
    ids = list(plan.ids_to_delete)
    for i in range(0, len(ids), batch):
        chunk = ids[i : i + batch]
        result = await coll.delete_many({"_id": {"$in": chunk}})
        total += result.deleted_count
        print(
            f"    class {plan.code}: deleted {total}/{len(ids)} so far",
            file=sys.stderr,
        )
    return total


def _render_plan(plans: list[ClassPlan]) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("PURGE PLAN — DRY RUN (no deletes performed)")
    lines.append("=" * 72)
    total = 0
    for p in plans:
        total += p.matched_count
        lines.append("")
        lines.append(f"[CLASS {p.code}] {p.label}")
        lines.append(f"  matched: {p.matched_count}   (audit predicted ~{p.expected_approx})")
        if p.sample_urls:
            lines.append("  top source_urls:")
            for url, n in p.sample_urls:
                lines.append(f"    {n:>5d}  {url[:100]}")
        if p.sample_chunks:
            lines.append("  sample chunks:")
            for s in p.sample_chunks[:3]:
                lines.append(
                    f"    {s['_id'][:24]}  {s['source_url'][:60]:60s}  {s['text_preview']!r}"
                )
    lines.append("")
    lines.append("-" * 72)
    lines.append(f"TOTAL would-delete: {total}  (audit predicted ~3,664)")
    lines.append("-" * 72)
    return "\n".join(lines)


async def run_main(args: argparse.Namespace) -> int:
    from gecko_core.db.mongo import (
        chunk_db_name,
        mongo_uri,
    )
    from gecko_core.db.mongo import (
        chunks_collection as _chunks_coll,
    )

    # The canonical accessor honors GECKO_CHUNK_STORE/MONGODB_URI. The
    # founder may be running with chunk-store=supabase env locally; force
    # mongo when the URI is present.
    if not mongo_uri():
        print("error: MONGODB_URI not set (or sentinel)", file=sys.stderr)
        return 2

    os.environ.setdefault("GECKO_CHUNK_STORE", "mongo")
    coll = _chunks_coll()
    if coll is None:
        print("error: chunks_collection() returned None", file=sys.stderr)
        return 2

    print(f"db={chunk_db_name()}  collection=chunks", file=sys.stderr)

    classes = _build_classes()
    if args.class_code and args.class_code != "all":
        classes = [c for c in classes if c.code == args.class_code]
        if not classes:
            print(f"error: unknown --class {args.class_code}", file=sys.stderr)
            return 2

    plans: list[ClassPlan] = []
    for pc in classes:
        print(f"\n>>> planning class {pc.code}: {pc.label}", file=sys.stderr)
        plan = await _plan_class(coll, pc)
        plans.append(plan)
        print(
            f"    matched={plan.matched_count}  (expected ~{plan.expected_approx})",
            file=sys.stderr,
        )

    print(_render_plan(plans), file=sys.stderr)

    # Persist plan JSON.
    date_tag = datetime.now(UTC).strftime("%Y-%m-%d")
    plan_path = args.plan_out or f"audit_runs/{date_tag}-purge-plan.json"
    os.makedirs(os.path.dirname(plan_path) or ".", exist_ok=True)
    with open(plan_path, "w") as f:
        json.dump(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "mode": "confirm" if args.confirm else "dry-run",
                "classes": [
                    {
                        "code": p.code,
                        "label": p.label,
                        "expected_approx": p.expected_approx,
                        "matched_count": p.matched_count,
                        "sample_urls": p.sample_urls,
                        "sample_chunks": p.sample_chunks,
                        "ids_count": len(p.ids_to_delete),
                    }
                    for p in plans
                ],
                "total_would_delete": sum(p.matched_count for p in plans),
            },
            f,
            indent=2,
            default=str,
        )
    print(f"\nwrote plan to {plan_path}", file=sys.stderr)

    if not args.confirm:
        print(
            "\nDRY RUN — no deletes performed. Re-run with --confirm --class <code> to execute.",
            file=sys.stderr,
        )
        return 0

    # Execute path: one class at a time, per-class log.
    log_path = args.log_out or f"audit_runs/{date_tag}-purge-log.json"
    log_entries: list[dict[str, Any]] = []
    if os.path.exists(log_path):
        try:
            with open(log_path) as f:
                log_entries = json.load(f)
        except Exception:
            log_entries = []

    for plan in plans:
        print(f"\n>>> EXECUTING class {plan.code}: {plan.label}", file=sys.stderr)
        deleted = await _execute_class(coll, plan)
        log_entries.append(
            {
                "executed_at": datetime.now(UTC).isoformat(),
                "class": plan.code,
                "label": plan.label,
                "planned": plan.matched_count,
                "deleted": deleted,
                "drift": plan.matched_count - deleted,
                "sample_deleted_ids": [str(x) for x in plan.ids_to_delete[:5]],
            }
        )
        with open(log_path, "w") as f:
            json.dump(log_entries, f, indent=2, default=str)
        print(
            f"    class {plan.code}: planned={plan.matched_count} deleted={deleted}",
            file=sys.stderr,
        )

    print(f"\nwrote execution log to {log_path}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="EXECUTE deletes. Without this flag, the script is dry-run only.",
    )
    parser.add_argument(
        "--class",
        dest="class_code",
        default=None,
        choices=["A", "B", "C", "D", "E", "F", "G", "all"],
        help="Restrict to one class. Default: plan all classes (dry-run only).",
    )
    parser.add_argument("--plan-out", default=None)
    parser.add_argument("--log-out", default=None)
    args = parser.parse_args()

    if args.confirm and not args.class_code:
        print(
            "error: --confirm requires --class <code|all> "
            "(refusing to bulk-delete without explicit class selection)",
            file=sys.stderr,
        )
        return 2

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(run_main(args))


# Re-export regex for tests.
__all__ = ["WEB_FIXTURE_RE", "ClassPlan", "PurgeClass", "main"]


if __name__ == "__main__":
    sys.exit(main())
