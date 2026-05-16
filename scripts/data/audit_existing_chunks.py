"""S29-#32 Pass 2 — audit existing Mongo chunks against the chunk-quality gate.

Iterates the entire ``chunks`` collection, scores each chunk with
``assess_chunk_quality``, and reports a per-``(provider_kind, reason)``
breakdown plus the top-N actual garbage samples (truncated chunk_id +
first 80 chars of text) so the founder can decide what to purge.

This script does NOT mutate the database. It is read-only. Purge
decisions are explicit follow-ups (Pass 3 onward).

Run (no flags — picks up the prod DB via ``MONGODB_CHUNK_DB``):

    uv run python scripts/data/audit_existing_chunks.py --top-samples 10

Optional overrides for staging/test databases:

    uv run python scripts/data/audit_existing_chunks.py \\
        --db gecko_rag_staging --collection chunks --mongodb-uri "$MONGODB_URI"

Per ``feedback_no_secret_access``: pass the URI via env. Never echo it.

Output is a structured JSON summary printed to stdout plus a human-
readable report to stderr.

REACHABILITY:
    scripts/data/audit_existing_chunks.py
      -> gecko_core.db.mongo.chunks_collection() (single source of truth
         for ``MONGODB_CHUNK_DB`` + ``CHUNKS_COLLECTION``; Pattern A)
      -> .find({}, projection={text, provider_kind, source, source_url})
      -> assess_chunk_quality(doc['text']) per chunk
      -> Counter[(provider_kind, reason)]
      -> json dump to stdout

The script is the canonical reachability probe for Pattern E ("wired ≠
reaches the model"). Run it before AND after every corpus mutation.

S30-#38 — defaults route through ``gecko_core.db.mongo.chunks_collection()``
so the script targets the same DB+collection that production reads/writes.
Previously this script defaulted to ``--db gecko`` (wrong) and respected
``MONGO_DB`` (wrong env var); production uses ``gecko_rag`` and
``MONGODB_CHUNK_DB``. That divergence is exactly the Pattern A violation
the project conventions guard against.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from typing import Any

from gecko_core.db.mongo import (
    CHUNKS_COLLECTION,
    chunk_db_name,
    chunks_collection,
)
from gecko_core.ingestion.chunk_quality import assess_chunk_quality

logger = logging.getLogger("audit_existing_chunks")


async def run_audit(
    *,
    top_samples: int,
    batch_size: int,
    mongodb_uri: str | None = None,
    db_name: str | None = None,
    collection_name: str | None = None,
) -> dict[str, Any]:
    """Iterate all chunks and produce a quality breakdown.

    Defaults route through ``gecko_core.db.mongo.chunks_collection()`` —
    the single source of truth for ``MONGODB_CHUNK_DB`` +
    ``CHUNKS_COLLECTION``. Pass explicit ``db_name`` / ``collection_name``
    / ``mongodb_uri`` only for staging or test databases.

    Returns a dict-shaped report:
        {
            "total_chunks": int,
            "by_reason_global": {reason: count, ...},
            "by_provider_kind": {
                pk: {"total": int, "ok": int, "garbage": int,
                     "reasons": {reason: count, ...}},
                ...
            },
            "samples": [
                {"chunk_id": "...", "provider_kind": "...", "reason": "...",
                 "text_preview": "..."},
                ...
            ],
        }
    """
    override_uri = mongodb_uri is not None
    override_db = db_name is not None
    override_coll = collection_name is not None

    client: Any = None
    if override_uri or override_db or override_coll:
        # Explicit override path — open a one-off client targeted at the
        # caller-specified database. Lazy import keeps motor optional.
        from motor.motor_asyncio import AsyncIOMotorClient

        uri = mongodb_uri or os.environ.get("MONGODB_URI")
        if not uri:
            raise RuntimeError("override path requires --mongodb-uri or MONGODB_URI env")
        client = AsyncIOMotorClient(uri)
        resolved_db = db_name or chunk_db_name()
        resolved_coll = collection_name or CHUNKS_COLLECTION
        coll: Any = client[resolved_db][resolved_coll]
    else:
        # Canonical path — production wiring via gecko_core.db.mongo.
        coll = chunks_collection()
        if coll is None:
            raise RuntimeError(
                "chunks_collection() returned None — set MONGODB_URI and "
                "GECKO_CHUNK_STORE=mongo, or pass explicit --mongodb-uri / "
                "--db / --collection overrides for a staging database."
            )

    try:
        total = 0
        by_reason: Counter[str] = Counter()
        by_pk: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "total": 0,
                "ok": 0,
                "garbage": 0,
                "reasons": Counter(),
            }
        )
        samples_per_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)

        cursor = coll.find(
            {},
            projection={
                "text": 1,
                "provider_kind": 1,
                "source": 1,
                "source_url": 1,
            },
            batch_size=batch_size,
        )
        async for doc in cursor:
            total += 1
            text = doc.get("text") or ""
            pk = doc.get("provider_kind") or doc.get("source") or "unknown"
            verdict = assess_chunk_quality(text)
            by_pk[pk]["total"] += 1
            if verdict.is_substantive:
                by_pk[pk]["ok"] += 1
                by_reason["ok"] += 1
                continue
            by_pk[pk]["garbage"] += 1
            by_pk[pk]["reasons"][verdict.reason] += 1
            by_reason[verdict.reason] += 1
            if len(samples_per_reason[verdict.reason]) < top_samples:
                samples_per_reason[verdict.reason].append(
                    {
                        "chunk_id": str(doc.get("_id")),
                        "provider_kind": pk,
                        "reason": verdict.reason,
                        "source_url": doc.get("source_url"),
                        "text_preview": text[:80].replace("\n", " "),
                    }
                )

            if total % 5000 == 0:
                print(f"  ...scanned {total} chunks", file=sys.stderr)

        # Flatten Counter sub-dicts for JSON serialization.
        report: dict[str, Any] = {
            "total_chunks": total,
            "by_reason_global": dict(by_reason),
            "by_provider_kind": {
                pk: {
                    "total": v["total"],
                    "ok": v["ok"],
                    "garbage": v["garbage"],
                    "reasons": dict(v["reasons"]),
                }
                for pk, v in sorted(by_pk.items())
            },
            "samples": [s for ss in samples_per_reason.values() for s in ss],
        }
        return report
    finally:
        if client is not None:
            client.close()


def _render_human(report: dict[str, Any]) -> str:
    lines: list[str] = []
    total = report["total_chunks"]
    lines.append("=" * 72)
    lines.append(f"CHUNK QUALITY AUDIT — total chunks: {total}")
    lines.append("=" * 72)
    lines.append("")
    lines.append("Per-reason totals (global):")
    for reason, count in sorted(
        report["by_reason_global"].items(),
        key=lambda kv: kv[1],
        reverse=True,
    ):
        pct = (count / total * 100) if total else 0.0
        lines.append(f"  {reason:18s} {count:>8d}  ({pct:5.1f}%)")
    lines.append("")
    lines.append("Per-provider-kind breakdown:")
    lines.append(f"  {'provider_kind':25s} {'total':>8s} {'ok':>8s} {'garbage':>8s}  reasons")
    for pk, stats in report["by_provider_kind"].items():
        gpct = (stats["garbage"] / stats["total"] * 100) if stats["total"] else 0.0
        rstr = ", ".join(f"{r}={n}" for r, n in stats["reasons"].items()) or "-"
        lines.append(
            f"  {pk:25s} {stats['total']:>8d} {stats['ok']:>8d} "
            f"{stats['garbage']:>8d} ({gpct:5.1f}%)  {rstr}"
        )
    lines.append("")
    lines.append("Sample garbage chunks (top per reason):")
    for s in report["samples"]:
        lines.append(
            f"  [{s['reason']:14s}] {s['provider_kind']:20s} {s['chunk_id'][:24]}  "
            f"{s['text_preview']!r}"
        )
    lines.append("")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    # Defaults intentionally None — when omitted, the script routes through
    # gecko_core.db.mongo (single source of truth for MONGODB_CHUNK_DB +
    # CHUNKS_COLLECTION). Pass overrides only for staging/test databases.
    parser.add_argument(
        "--mongodb-uri",
        default=None,
        help=(
            "Mongo connection string override. Default: route through "
            "gecko_core.db.mongo (respects MONGODB_URI env)."
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        help=(
            "Database override. Default: gecko_core.db.mongo.chunk_db_name() "
            "(respects MONGODB_CHUNK_DB env; falls back to 'gecko_rag')."
        ),
    )
    parser.add_argument(
        "--collection",
        default=None,
        help=(
            f"Collection override. Default: {CHUNKS_COLLECTION!r} "
            "(gecko_core.db.mongo.CHUNKS_COLLECTION)."
        ),
    )
    parser.add_argument("--top-samples", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument(
        "--out",
        help="Optional path to write the JSON report. stdout always gets it too.",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    report = asyncio.run(
        run_audit(
            top_samples=args.top_samples,
            batch_size=args.batch_size,
            mongodb_uri=args.mongodb_uri,
            db_name=args.db,
            collection_name=args.collection,
        )
    )

    print(_render_human(report), file=sys.stderr)
    payload = json.dumps(report, indent=2, default=str)
    print(payload)
    if args.out:
        with open(args.out, "w") as f:
            f.write(payload)
        print(f"wrote report to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
