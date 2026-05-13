"""S26 Tier 1 — canary eval.

The cheapest, fastest signal that "the panel is healthy". Three
deterministic dimensions, no LLM judge, N=1 fixture, ~$0.05 / run,
<30s wall.

Design doc: docs/strategy/2026-05-13-s26-eval-redesign-plan.md

Dimensions (all deterministic):
  D1 — answered_back:    panel returned a well-formed TradePanelVerdict
  D2 — context_overflow: rendered chunk block did not silently drop chunks
                         at _format_chunks's _RAG_CONTEXT_CHAR_CAP boundary
                         AND estimated prompt tokens are under model limit
  D3 — citations_grounded: every verdict.citations[].chunk_id was in the
                           retrieval input set, AND every inline [N]
                           marker in turns[].content is within bounds

Production-path guarantee (Pattern E / feedback_eval_harness_rag_gap):
this script calls ``run_trade_panel_with_retrieval`` directly, the same
entry point used by the MCP tool + REST endpoint. No canned-context
shortcut, no retrieval bypass.

Exits non-zero on any deterministic failure. Append-only run log at
``tests/eval/live_runs/<date>-canary*.json``.

Usage:
    uv run python -m tests.eval.scripts.canary_eval
    # or
    make test-canary

Hand-wave for later tiers (not implemented in this commit):
  - Tier 2 (D4: rubric judge) — see tests/eval/scripts/score_defi_trade_rubric.py
  - Tier 2 (D5: corpus-presence probe) — file at
    tests/eval/scripts/corpus_presence_probe.py (TODO, S26 W2)
  - Tier 3 (outcome / brier eval) — see
    tests/eval/scripts/score_defi_trade_suite.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SUITE_PATH = REPO_ROOT / "tests" / "eval" / "suites" / "canary_suite.json"
LIVE_RUNS_DIR = REPO_ROOT / "tests" / "eval" / "live_runs"

# Token-estimate ceilings. The canary uses a coarse `len(s) // 4` heuristic;
# upgrade to tiktoken only if these assertions fire spuriously.
TOKEN_SOFT_WARN = 50_000  # warning bound — early signal of cap approach
TOKEN_HARD_LIMIT = 100_000  # hard fail — gpt-4o-mini context is 128k

# Regex for inline [N] markers in persona turns. Conservative — matches
# bracketed integers from 1-99 only, so we don't catch e.g. "[FIX-12]"
# or "[note]" non-citation tokens.
_INLINE_MARKER_RE = re.compile(r"\[(\d{1,2})\]")


# ----------------------------- helpers -----------------------------


def _load_fixtures() -> list[dict[str, Any]]:
    with SUITE_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        raise SystemExit(f"canary suite empty or malformed: {SUITE_PATH}")
    return data


def _build_llm_config(api_key: str) -> dict[str, Any]:
    return {
        "config_list": [{"model": "gpt-4o-mini", "api_key": api_key}],
        "temperature": 0.2,
    }


def _estimate_tokens(s: str) -> int:
    """Coarse char/4 heuristic. Sufficient for the canary tier.

    We are NOT trying to measure exact token count — we are trying to
    catch "the prompt is approaching model limits and chunks are
    silently dropping". The heuristic + a generous TOKEN_HARD_LIMIT
    leaves headroom; upgrade to tiktoken if and only if these fire
    spuriously in practice.
    """
    return max(1, len(s) // 4)


# ----------------------------- dimensions -----------------------------


def _check_d1_answered_back(verdict_obj: Any) -> dict[str, Any]:
    """D1: panel returned a well-formed TradePanelVerdict."""
    failures: list[str] = []

    if verdict_obj is None:
        return {"passed": False, "failures": ["verdict_obj is None"]}

    verdict = getattr(verdict_obj, "verdict", None)
    if verdict not in {"act", "pass", "defer"}:
        failures.append(f"verdict={verdict!r} not in {{act,pass,defer}}")

    confidence = getattr(verdict_obj, "confidence", None)
    if not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
        failures.append(f"confidence={confidence!r} not float in [0,1]")

    turns = list(getattr(verdict_obj, "turns", []) or [])
    if len(turns) != 7:
        failures.append(f"len(turns)={len(turns)}, expected 7 (one per persona)")

    return {
        "passed": not failures,
        "failures": failures,
        "details": {
            "verdict": verdict,
            "confidence": confidence,
            "n_turns": len(turns),
        },
    }


def _check_d2_context_overflow(
    chunks: list[dict[str, Any]],
    rendered_block: str,
    full_prompt: str,
) -> dict[str, Any]:
    """D2: no silent chunk drop, prompt under model limit.

    Inspects `_format_chunks`'s output. The cap fires by truncating the
    block string, not by dropping list elements — so we compare a
    re-rendered (uncapped) chunk-count expectation against the rendered
    block's actual chunk-marker count.
    """
    from gecko_core.orchestration.trade_panel import _RAG_CONTEXT_CHAR_CAP  # type: ignore[attr-defined]

    failures: list[str] = []
    warnings: list[str] = []

    # Count how many [N] markers actually rendered into the block (the
    # cap-truncation mode is "trim the tail of the string", so any chunk
    # whose marker doesn't appear in the rendered block was effectively
    # dropped from the panel's view).
    rendered_markers = set(_INLINE_MARKER_RE.findall(rendered_block))
    n_input = len(chunks)
    n_rendered = len(rendered_markers)

    truncation_marker = "[context truncated for budget]"
    truncated = truncation_marker in rendered_block

    if truncated or n_rendered < n_input:
        failures.append(
            f"chunk dropout: {n_input - n_rendered} of {n_input} chunks not "
            f"reachable in rendered block (cap={_RAG_CONTEXT_CHAR_CAP} chars, "
            f"block_len={len(rendered_block)})"
        )

    est_tokens = _estimate_tokens(full_prompt)
    if est_tokens > TOKEN_HARD_LIMIT:
        failures.append(
            f"estimated_tokens={est_tokens} exceeds hard limit {TOKEN_HARD_LIMIT}"
        )
    elif est_tokens > TOKEN_SOFT_WARN:
        warnings.append(
            f"estimated_tokens={est_tokens} crossed soft-warn {TOKEN_SOFT_WARN} "
            f"— approaching cap"
        )

    return {
        "passed": not failures,
        "failures": failures,
        "warnings": warnings,
        "details": {
            "n_chunks_input": n_input,
            "n_chunks_rendered": n_rendered,
            "rendered_block_chars": len(rendered_block),
            "char_cap": _RAG_CONTEXT_CHAR_CAP,
            "truncation_marker_present": truncated,
            "estimated_prompt_tokens": est_tokens,
        },
    }


def _check_d3_citations_grounded(
    verdict_obj: Any,
    chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    """D3: cite.chunk_id ∈ retrieval set AND every inline [N] is in-range."""
    failures: list[str] = []

    valid_chunk_ids = {str(c.get("id", "")) for c in chunks}
    n_chunks = len(chunks)

    panel_cites = list(getattr(verdict_obj, "citations", []) or [])
    phantom_cites: list[dict[str, Any]] = []
    for cite in panel_cites:
        cid = getattr(cite, "chunk_id", "")
        if cid and cid not in valid_chunk_ids:
            phantom_cites.append(
                {
                    "cite_id": getattr(cite, "id", None),
                    "chunk_id": cid,
                    "source": getattr(cite, "source", "?"),
                }
            )
    if phantom_cites:
        failures.append(
            f"{len(phantom_cites)} citation(s) reference chunk_ids not in retrieval set: "
            f"{phantom_cites[:3]!r}{' ...' if len(phantom_cites) > 3 else ''}"
        )

    # Inline [N] markers across all turns.
    out_of_range: list[dict[str, Any]] = []
    for turn in getattr(verdict_obj, "turns", []) or []:
        agent = getattr(turn, "agent", "?")
        content = getattr(turn, "content", "") or ""
        for m in _INLINE_MARKER_RE.findall(content):
            n = int(m)
            if n < 1 or n > n_chunks:
                out_of_range.append({"agent": agent, "marker": n, "max": n_chunks})
    if out_of_range:
        failures.append(
            f"{len(out_of_range)} inline [N] marker(s) out of range "
            f"(n_chunks={n_chunks}): {out_of_range[:3]!r}"
            f"{' ...' if len(out_of_range) > 3 else ''}"
        )

    return {
        "passed": not failures,
        "failures": failures,
        "details": {
            "n_panel_cites": len(panel_cites),
            "n_valid_chunk_ids": len(valid_chunk_ids),
            "n_phantom_cites": len(phantom_cites),
            "n_inline_markers_out_of_range": len(out_of_range),
        },
    }


# ----------------------------- runner -----------------------------


async def _run_canary(fixture: dict[str, Any], llm_config: dict[str, Any]) -> dict[str, Any]:
    # Import inside the function so `--help` doesn't pay the import cost.
    from gecko_core.orchestration.trade_panel import (
        _format_chunks,  # type: ignore[attr-defined]
        _opening_prompt,  # type: ignore[attr-defined]
        retrieve_trade_corpus_chunks,
        run_trade_panel_with_retrieval,
    )

    protocol = fixture["protocol"]
    vertical = fixture.get("vertical", "dex")
    idea = fixture["text"]

    # Fetch chunks via the SAME helper the panel uses internally. This is
    # not a shortcut — we re-fetch outside the panel only to materialize
    # the inputs the D2/D3 checks need. The panel's own call inside
    # run_trade_panel_with_retrieval will fetch independently; we rely on
    # retrieval being deterministic for the same (idea, protocol, vertical, top_k)
    # signature within a single run.
    t_retrieve = time.perf_counter()
    chunks = await retrieve_trade_corpus_chunks(
        idea=idea, protocol=protocol, vertical=vertical
    )
    retrieve_wall = time.perf_counter() - t_retrieve

    rendered_block = _format_chunks(chunks)
    full_prompt = _opening_prompt(idea, protocol, chunks)

    # The panel call — production path.
    t_panel = time.perf_counter()
    try:
        verdict_obj = await run_trade_panel_with_retrieval(
            idea=idea,
            protocol=protocol,
            vertical=vertical,
            tier="basic",
            llm_config=llm_config,
        )
        panel_error: str | None = None
    except Exception as exc:  # noqa: BLE001 — canary captures all panel failures
        verdict_obj = None
        panel_error = f"{type(exc).__name__}: {exc}"
    panel_wall = time.perf_counter() - t_panel

    # Dimensions.
    d1 = _check_d1_answered_back(verdict_obj)
    if panel_error:
        d1["failures"].insert(0, f"panel raised: {panel_error}")
        d1["passed"] = False

    d2 = _check_d2_context_overflow(chunks, rendered_block, full_prompt)
    d3 = (
        _check_d3_citations_grounded(verdict_obj, chunks)
        if verdict_obj is not None
        else {
            "passed": False,
            "failures": ["panel returned None — cannot evaluate D3"],
            "details": {},
        }
    )

    overall = d1["passed"] and d2["passed"] and d3["passed"]

    return {
        "id": fixture["id"],
        "protocol": protocol,
        "vertical": vertical,
        "passed": overall,
        "dimensions": {
            "d1_answered_back": d1,
            "d2_context_overflow": d2,
            "d3_citations_grounded": d3,
        },
        "panel_error": panel_error,
        "wall_seconds": {
            "retrieve": round(retrieve_wall, 2),
            "panel": round(panel_wall, 2),
        },
        "panel": (
            {
                "verdict": getattr(verdict_obj, "verdict", None),
                "confidence": getattr(verdict_obj, "confidence", None),
                "n_citations": len(getattr(verdict_obj, "citations", []) or []),
                "n_chunks_input": len(chunks),
            }
            if verdict_obj is not None
            else None
        ),
    }


def _print_row(row: dict[str, Any]) -> None:
    print(f"  [{row['id']}] protocol={row['protocol']}")
    for dim_key, label in (
        ("d1_answered_back", "D1 (answered_back)    "),
        ("d2_context_overflow", "D2 (context_overflow) "),
        ("d3_citations_grounded", "D3 (citations_ground) "),
    ):
        dim = row["dimensions"][dim_key]
        status = "PASS" if dim["passed"] else "FAIL"
        print(f"    {label}: {status}")
        for warn in dim.get("warnings", []) or []:
            print(f"      warn: {warn}")
        for fail in dim.get("failures", []) or []:
            print(f"      fail: {fail}")
    if row.get("panel_error"):
        print(f"    panel_error: {row['panel_error']}")
    print(f"    wall: {row['wall_seconds']}")


def _write_run_log(row: dict[str, Any]) -> Path:
    LIVE_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    date = time.strftime("%Y-%m-%d")
    base = LIVE_RUNS_DIR / f"{date}-canary.json"
    out_path = base
    i = 2
    while out_path.exists():
        out_path = LIVE_RUNS_DIR / f"{date}-canary-{i}.json"
        i += 1
    payload = {
        "date": date,
        "suite": SUITE_PATH.name,
        "tier": "canary",
        "row": row,
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out_path


async def _main_async(*, dry_run: bool) -> int:
    fixtures = _load_fixtures()
    fixture = fixtures[0]  # canary is N=1 by design

    print(f"S26 canary | n=1 | suite={SUITE_PATH.name}")

    if dry_run:
        print(f"  [{fixture['id']}] dry-run: scaffold-only, no panel call")
        return 0

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set; canary cannot run the panel.")
        print("Set OPENAI_API_KEY or run with --dry-run.")
        return 2

    llm_config = _build_llm_config(api_key)
    row = await _run_canary(fixture, llm_config)
    _print_row(row)
    out_path = _write_run_log(row)
    print(f"\nSaved -> {out_path}")
    print(f"RESULT: {'PASS' if row['passed'] else 'FAIL'}")
    return 0 if row["passed"] else 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tests.eval.scripts.canary_eval",
        description="S26 Tier-1 canary — D1+D2+D3 on a single fixture.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip the panel call; smoke-test the scaffold only (no API spend).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_main_async(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
