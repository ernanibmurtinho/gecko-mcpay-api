"""S6-RECON-02 — nightly x402 reconcile.

Pulls the last 24h of `pulse_runs` + `memory` rows that carry a
`tx_signature`, runs the on-chain verifier, and posts a summary to
`GECKO_RECONCILE_WEBHOOK_URL` (Slack/Discord-compatible).

Defaults are deliberately conservative:

    GECKO_X402_MODE=stub            (default) → dry-run; no RPC calls,
                                     no webhook POST, just print the plan.
    GECKO_X402_MODE=live            → fetch + verify + post.

Where any required config is missing we DRY-RUN — never crash. The intent
is that this script can be wired into a cron / GitHub Action without
fearing that an unset `HELIUS_API_KEY` will page someone at 3am.

Usage:
    uv run python scripts/reconcile_economics.py
    uv run python scripts/reconcile_economics.py --hours 6 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from gecko_core.payments.verifier import (
    VerifyResult,
    VerifyTarget,
    is_stub_signature,
    make_httpx_rpc,
    resolve_rpc_url,
    summarize,
    verify_targets,
)

logger = logging.getLogger("reconcile_economics")

DEFAULT_LOOKBACK_HOURS = 24


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _is_live() -> bool:
    return os.environ.get("GECKO_X402_MODE", "stub").strip().lower() == "live"


async def _fetch_targets(hours: int) -> list[VerifyTarget]:
    """Pull recent rows with `tx_signature` from sessions (pulse_runs) + memory.

    Returns [] (with a log line) when Supabase isn't configured — keeps
    the script dry-run-safe in CI.
    """
    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY")):
        logger.warning("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set; skipping fetch")
        return []

    try:
        from gecko_core.db import create_supabase_client
    except Exception as exc:
        logger.warning("supabase client unavailable (%s); dry-run", exc)
        return []

    treasury = os.environ.get("GECKO_TREASURY_ADDRESS")
    cutoff = (datetime.now(tz=UTC) - timedelta(hours=hours)).isoformat()
    client = create_supabase_client()
    targets: list[VerifyTarget] = []

    # Sessions / pulse_runs join: pulse_runs reference sessions which carry
    # the tx_signature. We pull from sessions directly — every paid pulse
    # writes a row.
    try:
        sess_resp = await asyncio.to_thread(
            lambda: (
                client.table("sessions")
                .select("id,price_usd,x402_tx_signature,network,created_at")
                .not_.is_("x402_tx_signature", None)
                .gte("created_at", cutoff)
                .execute()
            )
        )
        for raw_sess in sess_resp.data or []:
            row: dict[str, Any] = raw_sess if isinstance(raw_sess, dict) else {}
            sig = row.get("x402_tx_signature")
            if not isinstance(sig, str) or not sig:
                continue
            targets.append(
                VerifyTarget(
                    source="session",
                    row_id=str(row.get("id")),
                    tx_signature=sig,
                    expected_amount_usd=float(row.get("price_usd") or 0.0),
                    expected_recipient=treasury,
                    network=str(row.get("network") or "solana-devnet"),
                )
            )
    except Exception as exc:
        logger.warning("sessions fetch failed: %s", exc)

    # Memory rows (auto-journal entries also stamp tx_signature for paid
    # operations). Schema: memory(id, tx_signature, value->price_usd?).
    try:
        mem_resp = await asyncio.to_thread(
            lambda: (
                client.table("memory")
                .select("id,tx_signature,value,created_at")
                .not_.is_("tx_signature", None)
                .gte("created_at", cutoff)
                .execute()
            )
        )
        for raw_mem in mem_resp.data or []:
            row = raw_mem if isinstance(raw_mem, dict) else {}
            sig = row.get("tx_signature")
            if not isinstance(sig, str) or not sig:
                continue
            value = row.get("value") or {}
            price = 0.0
            if isinstance(value, dict):
                price = float(value.get("price_usd") or value.get("paid_usd") or 0.0)
            targets.append(
                VerifyTarget(
                    source="memory",
                    row_id=str(row.get("id")),
                    tx_signature=sig,
                    expected_amount_usd=price,
                    expected_recipient=treasury,
                    network="solana-devnet",
                )
            )
    except Exception as exc:
        logger.warning("memory fetch failed: %s", exc)

    return targets


def _format_summary(
    results: list[VerifyResult],
    *,
    hours: int,
    live: bool,
) -> str:
    counts = summarize(results)
    total = len(results)
    mode = "live" if live else "stub/dry-run"
    lines = [
        f"*Gecko x402 reconcile* ({mode}, last {hours}h)",
        f"  total: {total}",
    ]
    for status, n in sorted(counts.items()):
        lines.append(f"  {status}: {n}")
    bad = [r for r in results if r.status == "mismatch"]
    if bad:
        lines.append("")
        lines.append("*mismatches:*")
        for r in bad[:10]:
            lines.append(
                f"  {r.target.source}:{r.target.row_id} "
                f"sig={r.target.tx_signature[:12]}… "
                f"→ {', '.join(r.mismatches)}"
            )
        if len(bad) > 10:
            lines.append(f"  …and {len(bad) - 10} more")
    return "\n".join(lines)


async def _post_webhook(text: str) -> None:
    url = os.environ.get("GECKO_RECONCILE_WEBHOOK_URL")
    if not url:
        logger.info("no webhook configured; would have posted:\n%s", text)
        return
    payload = {"text": text}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("webhook post failed: %s", exc)


async def _run(hours: int, dry_run: bool) -> int:
    live = _is_live() and not dry_run
    logger.info(
        "reconcile starting: hours=%d mode=%s dry_run=%s",
        hours,
        "live" if live else "stub",
        dry_run,
    )

    targets = await _fetch_targets(hours)
    logger.info("fetched %d candidate target(s)", len(targets))

    # Drop stub sigs early so the printed plan matches what we'd actually
    # send to RPC in live mode.
    real = [t for t in targets if not is_stub_signature(t.tx_signature)]
    skipped = len(targets) - len(real)

    if not live:
        # Dry-run: print the plan, never make RPC or webhook calls.
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "hours": hours,
                    "would_verify": len(real),
                    "stub_skipped": skipped,
                    "sample": [asdict(t) for t in real[:3]],
                },
                indent=2,
            )
        )
        return 0

    network = (
        "solana-mainnet" if any(t.network == "solana-mainnet" for t in real) else "solana-devnet"
    )
    rpc_url = resolve_rpc_url(network)
    rpc = make_httpx_rpc(rpc_url)
    results = await verify_targets(real, rpc)

    text = _format_summary(results, hours=hours, live=True)
    print(text)
    await _post_webhook(text)

    mismatches = sum(1 for r in results if r.status == "mismatch")
    return 0 if mismatches == 0 else 4


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    p = argparse.ArgumentParser(description="Reconcile x402 receipts vs on-chain.")
    p.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_LOOKBACK_HOURS,
        help="Lookback window in hours (default: 24).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run regardless of GECKO_X402_MODE.",
    )
    args = p.parse_args(argv)
    return asyncio.run(_run(hours=args.hours, dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
