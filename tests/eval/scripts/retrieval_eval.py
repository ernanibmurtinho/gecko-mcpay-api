"""S33-#82 — deterministic retrieval eval (no LLM judge).

Grades whether ``retrieve_trade_corpus_chunks`` surfaces the *right* chunks
for each trade fixture — isolated from "is the panel reasoning well". Pure
set-membership + substring matching: bit-for-bit reproducible, runs in
seconds, costs only the embed + Atlas query (~$0).

This is the gate for the S33-#82 canon-retrieval fix. The diagnosis
(``docs/eval/2026-05-16-s33-retrieval-pipeline-validation.md``) proved
canon investor-literature chunks never enter the ``$vectorSearch`` candidate
slate — ``canon_floor`` is ``false`` for every fixture. This script is the
before/after instrument for that fix.

Ground truth lives in each fixture's ``retrieval_expectations`` block in
``defi_trade_rubric_suite.json``. Ground truth is provider-kind composition
+ content signatures, NOT chunk-ids (ids churn on daily re-ingest).

S34-#86 — eval/prod top_k parity. The headline ``scores`` / ``gate`` are
graded at the PRODUCTION ``top_k`` (``trade_panel._DEFAULT_TRADE_TOP_K``) —
what ``run_trade_panel_with_retrieval`` actually requests. Previously the
eval graded only at the fixture ``k`` (15), a wide slate production never
uses, so a canon-floor bug at the narrow production ``top_k`` (S34-WS2)
was invisible. Each fixture is now graded at BOTH; the gate keys off the
production view, the fixture-``k`` view is kept under ``at_fixture_k`` /
``scores_at_fixture_k`` as a wide-slate diagnostic.

Metrics (all deterministic):
  - provider_kind_coverage : fraction of required_provider_kinds present
  - precision@k            : relevant chunks / k
  - recall@k               : distinct required kinds satisfied / required
  - mrr                    : mean reciprocal rank of first relevant chunk
                             per required kind
  - canon_floor            : bool — >=1 canon_* chunk in the final slate

Also dumps the Stage-4 raw $vectorSearch pk-distribution so an ANN-ranking
regression is visible even when the reranker masks it downstream.

Contract check: every provider_kind named in any retrieval_expectations
block must exist in the live corpus with count > 0 (extends the S33-#76
fixture<->corpus contract pattern). A fixture demanding an un-ingested
provider_kind fails the run loudly.

Usage:
    uv run python -m tests.eval.scripts.retrieval_eval
    uv run python -m tests.eval.scripts.retrieval_eval --limit 3 --tag baseline
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SUITE_PATH = REPO_ROOT / "tests" / "eval" / "suites" / "defi_trade_rubric_suite.json"
LIVE_RUNS_DIR = REPO_ROOT / "tests" / "eval" / "live_runs"

# Gate bars (deferred to CI later; reported here, not enforced as exit code).
CANON_FLOOR_TARGET = 0.8  # >= 8/10 fixtures must have canon_floor=true
PK_COVERAGE_TARGET = 0.8  # mean provider_kind_coverage


# ----------------------------- loading -----------------------------


def _load_fixtures(limit: int | None) -> list[dict[str, Any]]:
    with SUITE_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit(f"suite root is not a list: {SUITE_PATH}")
    fixtures = [f for f in data if "retrieval_expectations" in f]
    skipped = len(data) - len(fixtures)
    if skipped:
        print(f"WARN: {skipped} fixture(s) lack a retrieval_expectations block — skipped")
    if limit is not None:
        fixtures = fixtures[:limit]
    return fixtures


def _is_canon(provider_kind: str) -> bool:
    return provider_kind.startswith("canon_")


# ----------------------------- contract check -----------------------------


async def _corpus_contract_check(fixtures: list[dict[str, Any]]) -> dict[str, int]:
    """Every provider_kind named in any retrieval_expectations block must
    exist in the live corpus with count > 0. Returns the per-kind counts.
    Raises SystemExit on a missing kind — a fixture must not demand a
    provider_kind that was never ingested.
    """
    from gecko_core.db.mongo import chunks_collection

    needed: set[str] = set()
    for fx in fixtures:
        re_block = fx.get("retrieval_expectations", {})
        needed.update(re_block.get("required_provider_kinds", {}).keys())
        for sig in re_block.get("content_signatures", []):
            mk = sig.get("must_match_provider_kind")
            if mk:
                needed.add(mk)

    coll = chunks_collection()
    if coll is None:
        raise SystemExit("contract check: chunks_collection() is None — Mongo not configured")

    counts: dict[str, int] = {}
    pipeline: list[dict[str, Any]] = [
        {"$match": {"provider_kind": {"$in": sorted(needed)}}},
        {"$group": {"_id": "$provider_kind", "n": {"$sum": 1}}},
    ]
    async for doc in coll.aggregate(pipeline):
        counts[str(doc["_id"])] = int(doc["n"])

    missing = sorted(k for k in needed if counts.get(k, 0) == 0)
    if missing:
        raise SystemExit(
            f"contract check FAILED — fixture demands provider_kind(s) absent "
            f"from corpus: {missing}. Live counts: {counts}"
        )
    print(f"contract check OK — all {len(needed)} demanded provider_kinds present: {counts}")
    return counts


# ----------------------------- stage-4 probe -----------------------------


async def _raw_slate_pk_distribution(idea: str, vertical: str, top_k: int) -> dict[str, int]:
    """Issue the literal $vectorSearch Stage-4 pipeline (no $match, no rerank)
    and return the provider_kind distribution of the raw candidate slate.
    Mirrors retrieve_trade_corpus_chunks's Stage-4 exactly so a regression
    in ANN ranking is visible even when the reranker masks it downstream.
    """
    from gecko_core.db.mongo import VECTOR_INDEX_NAME, chunks_collection
    from gecko_core.ingestion.embedder import embed

    coll = chunks_collection()
    if coll is None:
        return {}
    vectors, _ = await embed([idea], input_type="query")
    if not vectors:
        return {}
    pipeline: list[dict[str, Any]] = [
        {
            "$vectorSearch": {
                "index": VECTOR_INDEX_NAME,
                "path": "embedding",
                "queryVector": vectors[0],
                "numCandidates": max(200, top_k * 20),
                "limit": top_k * 5,
                "exact": False,
                "filter": {
                    "vertical": {"$eq": vertical},
                    "metadata.deprecated": {"$ne": True},
                },
            }
        },
        {"$project": {"provider_kind": 1}},
    ]
    dist: dict[str, int] = {}
    async for doc in coll.aggregate(pipeline):
        pk = str(doc.get("provider_kind") or "unknown")
        dist[pk] = dist.get(pk, 0) + 1
    return dist


# ----------------------------- grading -----------------------------


def _chunk_satisfies_signatures(chunk: dict[str, Any], signatures: list[dict[str, Any]]) -> bool:
    """A chunk with a content-signature constraint for its provider_kind
    must match at least one substring of *some* signature scoped to that
    kind. Chunks whose provider_kind has no signature constraint pass
    trivially (kind-membership alone is enough).
    """
    pk = str(chunk.get("provider_kind") or "")
    relevant = [s for s in signatures if s.get("must_match_provider_kind") == pk]
    if not relevant:
        return True
    text = (str(chunk.get("text") or "")).lower()
    for sig in relevant:
        for sub in sig.get("any_substring", []):
            if str(sub).lower() in text:
                return True
    return False


def grade_retrieval(
    slate: list[dict[str, Any]],
    expectations: dict[str, Any],
    *,
    k: int | None = None,
) -> dict[str, Any]:
    """Score a retrieved slate against a fixture's retrieval_expectations.

    A chunk is "relevant" when its provider_kind is in required_provider_kinds
    AND (if its kind carries a content_signature) its text matches a signature.

    ``k`` overrides the fixture's ``k`` — S34-#86 grades at both the fixture
    ``k`` AND the production ``top_k`` so the gate reflects what production
    actually retrieves, not a setting production never uses.
    """
    required: dict[str, int] = expectations.get("required_provider_kinds", {})
    signatures: list[dict[str, Any]] = expectations.get("content_signatures", [])
    if k is None:
        k = int(expectations.get("k", 15))
    required_kinds = set(required.keys())

    # Per-kind: is each required kind present (relevant chunk found)?
    kind_present: dict[str, bool] = {kind: False for kind in required_kinds}
    # First-relevant rank per required kind (1-indexed) for MRR.
    kind_first_rank: dict[str, int] = {}
    relevant_count = 0

    for rank, chunk in enumerate(slate[:k], start=1):
        pk = str(chunk.get("provider_kind") or "")
        if pk not in required_kinds:
            continue
        if not _chunk_satisfies_signatures(chunk, signatures):
            continue
        relevant_count += 1
        if not kind_present[pk]:
            kind_present[pk] = True
            kind_first_rank[pk] = rank

    n_required = len(required_kinds) or 1
    n_satisfied = sum(1 for v in kind_present.values() if v)

    coverage = n_satisfied / n_required
    precision = relevant_count / k if k else 0.0
    recall = n_satisfied / n_required
    # MRR averaged over required kinds — kinds never found contribute 0.
    rr_sum = sum(1.0 / kind_first_rank[kind] for kind in kind_first_rank)
    mrr = rr_sum / n_required

    canon_floor = any(_is_canon(str(c.get("provider_kind") or "")) for c in slate)

    slate_pk_dist: dict[str, int] = {}
    for c in slate:
        pk = str(c.get("provider_kind") or "unknown")
        slate_pk_dist[pk] = slate_pk_dist.get(pk, 0) + 1

    return {
        "provider_kind_coverage": round(coverage, 4),
        "precision_at_k": round(precision, 4),
        "recall_at_k": round(recall, 4),
        "mrr": round(mrr, 4),
        "canon_floor": canon_floor,
        "kinds_present": sorted(kind for kind, v in kind_present.items() if v),
        "kinds_missing": sorted(kind for kind, v in kind_present.items() if not v),
        "relevant_count": relevant_count,
        "slate_size": len(slate),
        "slate_pk_distribution": slate_pk_dist,
    }


# ----------------------------- scoring loop -----------------------------


async def _score_one(fixture: dict[str, Any]) -> dict[str, Any]:
    """S34-#86 — score a fixture at BOTH the fixture ``k`` and the production
    ``top_k``.

    The fixture ``k`` (15) is a wide-slate diagnostic view; the production
    ``top_k`` (``_DEFAULT_TRADE_TOP_K``) is what ``run_trade_panel_with_
    retrieval`` actually requests. S34-WS2 found a canon-floor bug that only
    manifests at the narrower production ``top_k`` — the deterministic eval
    missed it because it ran exclusively at the fixture ``k``. The gate must
    reflect production reality, so we retrieve + grade at both and the
    headline ``scores`` is the production view.
    """
    from gecko_core.orchestration.trade_panel import (
        _DEFAULT_TRADE_TOP_K,
        retrieve_trade_corpus_chunks,
    )

    expectations = fixture["retrieval_expectations"]
    fixture_k = int(expectations.get("k", 15))
    prod_k = int(_DEFAULT_TRADE_TOP_K)
    vertical = fixture.get("vertical", "dex")

    async def _retrieve_and_grade(top_k: int) -> tuple[dict[str, Any], float]:
        t0 = time.perf_counter()
        slate = await retrieve_trade_corpus_chunks(
            idea=fixture["text"],
            protocol=fixture["protocol"],
            vertical=vertical,
            top_k=top_k,
        )
        wall = time.perf_counter() - t0
        return grade_retrieval(slate, expectations, k=top_k), round(wall, 2)

    prod_scores, prod_wall = await _retrieve_and_grade(prod_k)
    if fixture_k == prod_k:
        fixture_scores, fixture_wall = prod_scores, prod_wall
    else:
        fixture_scores, fixture_wall = await _retrieve_and_grade(fixture_k)

    raw_dist = await _raw_slate_pk_distribution(fixture["text"], vertical, prod_k)

    return {
        "id": fixture["id"],
        "protocol": fixture["protocol"],
        "vertical": vertical,
        # `k` and `scores` are the PRODUCTION view — the gate keys off these.
        "k": prod_k,
        "production_top_k": prod_k,
        "fixture_k": fixture_k,
        "required_provider_kinds": expectations.get("required_provider_kinds", {}),
        "scores": prod_scores,
        "scores_at_fixture_k": fixture_scores,
        "stage4_raw_slate_pk_distribution": raw_dist,
        "wall_seconds": round(prod_wall + fixture_wall, 2),
    }


def _aggregate_one_view(rows: list[dict[str, Any]], scores_key: str) -> dict[str, Any]:
    """Aggregate one scoring view (``scores`` = production top_k, or
    ``scores_at_fixture_k`` = fixture k). Returns means + gate verdict.
    """
    n = len(rows)
    dims = ["provider_kind_coverage", "precision_at_k", "recall_at_k", "mrr"]
    means = {d: round(statistics.mean(r[scores_key][d] for r in rows), 4) for d in dims}
    canon_floor_rate = round(sum(1 for r in rows if r[scores_key]["canon_floor"]) / n, 4)
    return {
        "canon_floor_rate": canon_floor_rate,
        "canon_floor_count": sum(1 for r in rows if r[scores_key]["canon_floor"]),
        "per_dimension_means": means,
        "gate": {
            "canon_floor_target": CANON_FLOOR_TARGET,
            "pk_coverage_target": PK_COVERAGE_TARGET,
            "canon_floor_pass": canon_floor_rate >= CANON_FLOOR_TARGET,
            "pk_coverage_pass": means["provider_kind_coverage"] >= PK_COVERAGE_TARGET,
        },
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """S34-#86 — aggregate at the PRODUCTION top_k as the headline gate.

    The top-level ``gate`` / ``canon_floor_rate`` / ``per_dimension_means``
    reflect what production (``run_trade_panel_with_retrieval`` at
    ``_DEFAULT_TRADE_TOP_K``) actually retrieves — the rubric pre-gate keys
    off ``gate``, so it must measure the production setting, not the wide
    fixture-``k`` diagnostic slate. ``at_fixture_k`` keeps the wide-slate
    view for diagnosis (e.g. an ANN regression masked at the narrow top_k).
    """
    n = len(rows)
    if n == 0:
        return {"n": 0}

    prod_view = _aggregate_one_view(rows, "scores")
    fixture_view = _aggregate_one_view(rows, "scores_at_fixture_k")
    production_top_k = rows[0].get("production_top_k")
    fixture_k = rows[0].get("fixture_k")

    return {
        "n": n,
        "production_top_k": production_top_k,
        "fixture_k": fixture_k,
        # --- headline = PRODUCTION view (the rubric pre-gate reads this) ---
        "canon_floor_rate": prod_view["canon_floor_rate"],
        "canon_floor_count": prod_view["canon_floor_count"],
        "per_dimension_means": prod_view["per_dimension_means"],
        "gate": prod_view["gate"],
        # --- diagnostic = wide fixture-k slate ---
        "at_fixture_k": fixture_view,
    }


async def run(*, limit: int | None, tag: str) -> int:
    fixtures = _load_fixtures(limit)
    if not fixtures:
        raise SystemExit("no fixtures with retrieval_expectations — nothing to grade")

    from gecko_core.db.chunk_store import get_chunk_store

    if get_chunk_store() != "mongo":
        raise SystemExit(
            f"retrieval eval needs GECKO_CHUNK_STORE=mongo (got {get_chunk_store()!r})"
        )

    print(f"S33-#82 retrieval eval | n={len(fixtures)} | suite={SUITE_PATH.name}")
    await _corpus_contract_check(fixtures)

    rows: list[dict[str, Any]] = []
    for fx in fixtures:
        print(f"  [{fx['id']}] protocol={fx['protocol']} ... ", end="")
        sys.stdout.flush()
        try:
            row = await _score_one(fx)
        except Exception as exc:
            print(f"FAIL ({type(exc).__name__}: {exc})")
            continue
        s = row["scores"]
        sf = row["scores_at_fixture_k"]
        print(
            f"[prod k={row['production_top_k']}] "
            f"canon_floor={s['canon_floor']!s:<5} "
            f"pkCov={s['provider_kind_coverage']:.2f} "
            f"P@k={s['precision_at_k']:.2f} R@k={s['recall_at_k']:.2f} "
            f"MRR={s['mrr']:.2f} missing={s['kinds_missing']} "
            f"| [fixture k={row['fixture_k']}] canon_floor={sf['canon_floor']!s:<5} "
            f"pkCov={sf['provider_kind_coverage']:.2f}"
        )
        rows.append(row)

    agg = _aggregate(rows)
    print(f"\nAggregate: {json.dumps(agg, indent=2)}")

    LIVE_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    date = time.strftime("%Y-%m-%d")
    base = LIVE_RUNS_DIR / f"{date}-s33-retrieval-eval-{tag}-{len(rows)}.json"
    out_path = base
    i = 2
    while out_path.exists():
        out_path = LIVE_RUNS_DIR / f"{date}-s33-retrieval-eval-{tag}-{len(rows)}-{i}.json"
        i += 1
    payload = {
        "date": date,
        "ticket": "s33-#82",
        "suite": SUITE_PATH.name,
        "n_fixtures_loaded": len(fixtures),
        "n_scored": len(rows),
        "aggregate": agg,
        "rows": rows,
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Saved -> {out_path}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tests.eval.scripts.retrieval_eval",
        description="S33-#82 — deterministic retrieval eval (no LLM judge).",
    )
    p.add_argument("--limit", type=int, default=None, help="cap at first N fixtures")
    p.add_argument("--tag", default="run", help="run tag (e.g. 'baseline' / 'after')")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(run(limit=args.limit, tag=args.tag))


if __name__ == "__main__":
    sys.exit(main())
