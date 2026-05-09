"""Task 8 — Tavily falsifier for the trading-oracle.

Goal
----
Prove (with numbers, not vibes) that Gecko's grounded-debate verdict
diverges meaningfully from a one-shot Tavily search on the same Solana
DeFi question. If Gecko's cites are ~entirely a subset of Tavily's top-N
URLs, then "orchestration + adversarial debate" is decorative and the
KaaS-oracle thesis collapses (Pattern D — orchestration alone is table
stakes; the wedge has to live somewhere else).

What this script measures
-------------------------
For N=5 fixed Solana-DeFi questions:

  1. Hit Gecko's POST /trade_research (basic tier) with the stub-mode
     payment dance and capture verdict + cites + drivers + blockers.
  2. Hit Tavily's /search (advanced) directly and capture top-10 URLs
     plus the model-friendly ``answer`` field if present.
  3. Compute, per question:
        - cites_unique_to_gecko: URLs Gecko cited that Tavily's top-10
          did NOT surface (host-level set difference).
        - cites_overlap: URLs both surfaced (host-level intersection).
        - tavily_only_urls: URLs Tavily surfaced that Gecko didn't cite.
        - has_dissent: whether the Gecko verdict carries any
          ``blocker_questions`` (the dissent / "you're missing X"
          surface that a Tavily one-shot CANNOT produce — Tavily has no
          adversarial step).
  4. Print a markdown report to stdout AND write JSON to
     ``docs/superpowers/falsifier-results/trading_oracle_vs_tavily.json``
     (gitignored — manual judging artifact, not a CI input).

Run
---
    GECKO_E2E_BASE_URL=https://api.geckovision.tech \\
    TAVILY_API_KEY=tvly-... \\
    uv run python scripts/falsifier_tavily.py

  Or with --limit to dial back the question count for quick iteration.

Constraints
-----------
- Stub-mode payment header — works against any X402_MODE=stub server.
  A live-mode flip would require a real signer; out of scope here.
- No new dependencies. ``httpx`` is already in gecko-core; we go direct
  to ``api.tavily.com`` rather than pulling in the ``tavily-python``
  client because the SDK's sync-only ``search`` would block the asyncio
  loop and the wire shape we want is trivially small.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

# Five fixed Solana-DeFi questions. Cherry-picked to bias toward
# protocol-specific operational facts where a one-shot search is most
# likely to surface either marketing pages or stale Medium posts — i.e.
# where adversarial debate + paysh/bazaar-fed corpus should dominate.
QUESTIONS: list[tuple[str, str]] = [
    ("kamino", "Should a trader deposit USDC into Kamino's USDC reserve right now?"),
    ("drift", "Has Drift had any oracle-staleness incidents in the last 90 days?"),
    ("jito", "What audit firms signed off on Jito's most recent vault contract changes?"),
    ("orca", "Compare current Orca vs Raydium fee tiers for SOL/USDC pools."),
    ("pyth", "Has Pyth pushed any breaking parameter change to its SOL/USD feed in 2026?"),
]

OUTPUT_PATH = Path("docs/superpowers/falsifier-results/trading_oracle_vs_tavily.json")
TAVILY_ENDPOINT = "https://api.tavily.com/search"


@dataclass
class RowResult:
    protocol: str
    question: str
    gecko_verdict: str | None = None
    gecko_confidence: float | None = None
    gecko_blockers: list[str] = field(default_factory=list)
    gecko_drivers: list[str] = field(default_factory=list)
    gecko_cited_urls: list[str] = field(default_factory=list)
    tavily_top_urls: list[str] = field(default_factory=list)
    tavily_answer: str | None = None
    error: str | None = None

    @property
    def gecko_hosts(self) -> set[str]:
        return {_host(u) for u in self.gecko_cited_urls if u}

    @property
    def tavily_hosts(self) -> set[str]:
        return {_host(u) for u in self.tavily_top_urls if u}

    @property
    def hosts_unique_to_gecko(self) -> set[str]:
        return self.gecko_hosts - self.tavily_hosts

    @property
    def hosts_overlap(self) -> set[str]:
        return self.gecko_hosts & self.tavily_hosts

    @property
    def has_dissent(self) -> bool:
        return bool(self.gecko_blockers)


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower() or url.lower()
    except Exception:
        return url.lower()


def _decode_challenge(header_value: str) -> dict:
    return json.loads(base64.b64decode(header_value).decode("utf-8"))


def _stub_payment_header(accepts_entry: dict) -> str:
    payload_obj = {
        "x402Version": 2,
        "payload": {"signature": "stub-sig", "transaction": "stub-tx"},
        "accepted": accepts_entry,
    }
    return base64.b64encode(json.dumps(payload_obj).encode("utf-8")).decode("utf-8")


def _extract_cited_urls(verdict_body: dict) -> list[str]:
    """Pull citation URLs out of the trade panel verdict envelope.

    The wire shape doesn't carry a flat ``citation_markers`` list at
    /trade_research today — cites live inside per-agent ``turns[*].
    parsed_verdict`` and ``turns[*].content``. We do a defensive scrape
    rather than couple to one shape, because the panel formatter has
    drifted before (Phase 8b -> 10A).
    """
    urls: list[str] = []
    for turn in verdict_body.get("turns", []) or []:
        if not isinstance(turn, dict):
            continue
        # 1. parsed_verdict.cites or .citations or .sources
        parsed = turn.get("parsed_verdict") or {}
        if isinstance(parsed, dict):
            for key in ("cites", "citations", "sources", "urls"):
                v = parsed.get(key)
                if isinstance(v, list):
                    urls.extend(str(x) for x in v if isinstance(x, str))
        # 2. plaintext URLs in content (rough — bare http(s) tokens).
        content = turn.get("content")
        if isinstance(content, str):
            for tok in content.split():
                tok = tok.strip(".,;:()<>[]\"'")
                if tok.startswith("http://") or tok.startswith("https://"):
                    urls.append(tok)
    # de-dupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def _gecko_query(client: httpx.AsyncClient, *, protocol: str, question: str) -> dict:
    body = {"idea": question, "protocol": protocol, "vertical": "defi-trading"}
    r0 = await client.post("/trade_research", json=body)
    if r0.status_code != 402:
        raise RuntimeError(f"expected 402 from /trade_research, got {r0.status_code}: {r0.text}")
    header_value = r0.headers.get("payment-required") or r0.headers.get("PAYMENT-REQUIRED")
    if not header_value:
        raise RuntimeError(f"missing payment-required header; headers={dict(r0.headers)!r}")
    challenge = _decode_challenge(header_value)
    accepts_entry = challenge["accepts"][0]
    payment_header = _stub_payment_header(accepts_entry)
    r = await client.post(
        "/trade_research", json=body, headers={"PAYMENT-SIGNATURE": payment_header}
    )
    r.raise_for_status()
    return r.json()


async def _tavily_query(client: httpx.AsyncClient, *, api_key: str, question: str) -> dict:
    payload = {
        "api_key": api_key,
        "query": question,
        "search_depth": "advanced",
        "max_results": 10,
        "include_answer": True,
    }
    r = await client.post(TAVILY_ENDPOINT, json=payload)
    r.raise_for_status()
    return r.json()


async def _run_one(
    *,
    gecko_client: httpx.AsyncClient,
    tavily_client: httpx.AsyncClient,
    tavily_key: str,
    protocol: str,
    question: str,
) -> RowResult:
    row = RowResult(protocol=protocol, question=question)
    try:
        # Run both in parallel — they're independent.
        gecko_task = asyncio.create_task(
            _gecko_query(gecko_client, protocol=protocol, question=question)
        )
        tavily_task = asyncio.create_task(
            _tavily_query(tavily_client, api_key=tavily_key, question=question)
        )
        gecko_body, tavily_body = await asyncio.gather(gecko_task, tavily_task)

        row.gecko_verdict = gecko_body.get("verdict")
        conf = gecko_body.get("confidence")
        row.gecko_confidence = float(conf) if isinstance(conf, (int, float)) else None
        row.gecko_blockers = list(gecko_body.get("blocker_questions") or [])
        row.gecko_drivers = list(gecko_body.get("key_drivers") or [])
        row.gecko_cited_urls = _extract_cited_urls(gecko_body)

        results = tavily_body.get("results") or []
        row.tavily_top_urls = [
            r["url"] for r in results[:10] if isinstance(r, dict) and r.get("url")
        ]
        ans = tavily_body.get("answer")
        row.tavily_answer = ans if isinstance(ans, str) else None
    except Exception as exc:
        row.error = f"{type(exc).__name__}: {exc}"
    return row


def _markdown_report(rows: list[RowResult]) -> str:
    lines: list[str] = []
    lines.append("# Trading-Oracle vs Tavily — Falsifier Report")
    lines.append("")

    successful = [r for r in rows if r.error is None]
    n = len(successful)
    if n == 0:
        lines.append("No successful rows. See `error` field in JSON output.")
        return "\n".join(lines)

    total_unique = sum(len(r.hosts_unique_to_gecko) for r in successful)
    total_overlap = sum(len(r.hosts_overlap) for r in successful)
    rows_with_dissent = sum(1 for r in successful if r.has_dissent)
    rows_with_any_gecko_cite = sum(1 for r in successful if r.gecko_hosts)

    lines.append("## Aggregate")
    lines.append("")
    lines.append(f"- Questions probed: **{n}**")
    lines.append(f"- Hosts cited by Gecko but absent from Tavily top-10: **{total_unique}**")
    lines.append(f"- Hosts cited by both: **{total_overlap}**")
    lines.append(f"- Rows with Gecko dissent (>=1 blocker_question): **{rows_with_dissent}/{n}**")
    lines.append(f"- Rows where Gecko surfaced any cited host: **{rows_with_any_gecko_cite}/{n}**")
    lines.append("")
    lines.append("## Per-question")
    lines.append("")

    for r in rows:
        lines.append(f"### {r.protocol} — {r.question}")
        if r.error:
            lines.append(f"- ERROR: `{r.error}`")
            lines.append("")
            continue
        lines.append(f"- Gecko verdict: **{r.gecko_verdict}** (confidence={r.gecko_confidence})")
        lines.append(f"- Gecko cited hosts: {sorted(r.gecko_hosts) or '(none)'}")
        lines.append(f"- Tavily top-10 hosts: {sorted(r.tavily_hosts) or '(none)'}")
        lines.append(f"- Unique to Gecko: {sorted(r.hosts_unique_to_gecko) or '(none)'}")
        lines.append(f"- Overlap: {sorted(r.hosts_overlap) or '(none)'}")
        lines.append(f"- Dissent (blockers): {r.gecko_blockers or '(none)'}")
        lines.append("")

    lines.append("## Reading the result")
    lines.append("")
    lines.append(
        "If `Hosts unique to Gecko` is ~0 across all rows AND `Rows with dissent` is "
        "~0/N, the verdict is materially the same as a Tavily one-shot — kill the "
        "KaaS-oracle thesis or find a different wedge (Pattern D)."
    )
    lines.append(
        "If unique-host count is non-trivial OR dissent shows up in >50% of rows, "
        "the adversarial-debate + paid-corpus story is doing real work."
    )
    return "\n".join(lines)


async def _amain(*, limit: int | None) -> int:
    base_url = os.environ.get("GECKO_E2E_BASE_URL")
    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not base_url:
        print("ERROR: GECKO_E2E_BASE_URL not set", file=sys.stderr)
        return 2
    if not tavily_key:
        print("ERROR: TAVILY_API_KEY not set", file=sys.stderr)
        return 2

    questions = QUESTIONS if limit is None else QUESTIONS[:limit]

    timeout = httpx.Timeout(120.0, connect=10.0)
    async with (
        httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout) as gecko_client,
        httpx.AsyncClient(timeout=timeout) as tavily_client,
    ):
        rows: list[RowResult] = []
        for protocol, question in questions:
            print(f"... probing {protocol}: {question}", file=sys.stderr)
            row = await _run_one(
                gecko_client=gecko_client,
                tavily_client=tavily_client,
                tavily_key=tavily_key,
                protocol=protocol,
                question=question,
            )
            rows.append(row)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps([_row_to_jsonable(r) for r in rows], indent=2),
        encoding="utf-8",
    )
    report = _markdown_report(rows)
    print(report)
    print(f"\n[wrote raw JSON to {OUTPUT_PATH}]", file=sys.stderr)
    return 0


def _row_to_jsonable(row: RowResult) -> dict:
    return {
        "protocol": row.protocol,
        "question": row.question,
        "gecko_verdict": row.gecko_verdict,
        "gecko_confidence": row.gecko_confidence,
        "gecko_blockers": row.gecko_blockers,
        "gecko_drivers": row.gecko_drivers,
        "gecko_cited_urls": row.gecko_cited_urls,
        "tavily_top_urls": row.tavily_top_urls,
        "tavily_answer": row.tavily_answer,
        "hosts_unique_to_gecko": sorted(row.hosts_unique_to_gecko),
        "hosts_overlap": sorted(row.hosts_overlap),
        "has_dissent": row.has_dissent,
        "error": row.error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only probe the first N questions (default: all 5).",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(limit=args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
