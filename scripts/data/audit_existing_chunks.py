"""S29-#32 Pass 2 — audit existing Mongo chunks against the chunk-quality gate.

Iterates the entire ``chunks`` collection, scores each chunk with
``assess_chunk_quality``, and reports a per-``(provider_kind, reason)``
breakdown plus the top-N actual garbage samples (truncated chunk_id +
first 80 chars of text) so the founder can decide what to purge.

This script does NOT mutate the database. It is read-only. Purge
decisions are explicit follow-ups (Pass 3 onward).

Run:

    uv run python scripts/data/audit_existing_chunks.py \\
        --mongodb-uri "$MONGODB_URI" \\
        --db gecko --collection chunks \\
        --top-samples 10

Per ``feedback_no_secret_access``: pass the URI via env. Never echo it.

Output is a structured JSON summary printed to stdout plus a human-
readable report to stderr.

REACHABILITY:
    scripts/data/audit_existing_chunks.py
      -> chunks_collection().find({}, projection={text:1, provider_kind:1, source:1, source_url:1})
      -> assess_chunk_quality(doc['text']) per chunk
      -> Counter[(provider_kind, reason)]
      -> json dump to stdout

The script is the canonical reachability probe for Pattern E ("wired ≠
reaches the model"). Run it before AND after every corpus mutation.
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

from gecko_core.ingestion.chunk_quality import assess_chunk_quality

logger = logging.getLogger("audit_existing_chunks")


async def run_audit(
    *,
    mongodb_uri: str,
    db_name: str,
    collection_name: str,
    top_samples: int,
    batch_size: int,
) -> dict[str, Any]:
    """Iterate all chunks and produce a quality breakdown.

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
    # Lazy import so importers that only want the type/signature don't
    # need motor installed.
    from motor.motor_asyncio import AsyncIOMotorClient

    client: Any = AsyncIOMotorClient(mongodb_uri)
    try:
        coll = client[db_name][collection_name]

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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mongodb-uri",
        default=os.environ.get("MONGODB_URI"),
        help="Mongo connection string (defaults to MONGODB_URI env)",
    )
    parser.add_argument("--db", default=os.environ.get("MONGO_DB", "gecko"))
    parser.add_argument("--collection", default="chunks")
    parser.add_argument("--top-samples", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument(
        "--out",
        help="Optional path to write the JSON report. stdout always gets it too.",
    )
    args = parser.parse_args()

    if not args.mongodb_uri:
        print(
            "error: pass --mongodb-uri or set MONGODB_URI env",
            file=sys.stderr,
        )
        return 2

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    report = asyncio.run(
        run_audit(
            mongodb_uri=args.mongodb_uri,
            db_name=args.db,
            collection_name=args.collection,
            top_samples=args.top_samples,
            batch_size=args.batch_size,
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
