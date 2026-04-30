"""On-chain verification for x402 receipts (S6-RECON-01..02).

Given a `tx_signature` written to `sessions.x402_tx_signature` (or a memory
row's `tx_signature`), call Solana RPC (Helius preferred when configured)
and check three things:

  1. The signature confirmed (`status.confirmationStatus == "finalized"` or
     `"confirmed"`).
  2. The total USDC transferred matches the expected `price_usd` within a
     tolerance (Solana represents USDC at 6 decimals).
  3. The destination of the USDC transfer is the configured treasury.

Stub-mode signatures are prefixed `stub_` (see `StubX402Client`); the
verifier short-circuits these — no RPC call, status `skipped_stub`.

This module is intentionally pure-function + dataclass: the CLI wires it
to Click + Rich; the reconcile script wires it to webhook posting; tests
inject a fake `rpc_call` to avoid the network entirely.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Solana / SPL constants.
USDC_DEVNET_MINT = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
USDC_MAINNET_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Tolerance in USD when comparing transferred amount to expected. 0.01 USDC
# (one cent) is enough headroom to absorb the ~5_000 lamport priority fee
# while still catching meaningful underpayment / overcharge.
DEFAULT_AMOUNT_TOLERANCE_USD = 0.01

STUB_SIG_PREFIX = "stub_"

# RPC fetcher signature: (method, params) -> JSON-RPC `result` field.
RpcCall = Callable[[str, list[Any]], Awaitable[Any]]


@dataclass(frozen=True)
class VerifyTarget:
    """Single row to verify. Source-agnostic (sessions OR memory)."""

    source: str  # "session" | "memory" — for the rendered diff
    row_id: str  # session_id or memory entry id
    tx_signature: str
    expected_amount_usd: float
    expected_recipient: str | None  # treasury wallet address
    network: str = "solana-devnet"


@dataclass(frozen=True)
class VerifyResult:
    """Outcome for a single `VerifyTarget`."""

    target: VerifyTarget
    status: str  # "ok" | "mismatch" | "skipped_stub" | "not_found" | "rpc_error"
    confirmation: str | None = None  # "finalized" | "confirmed" | "processed" | None
    actual_amount_usd: float | None = None
    actual_recipient: str | None = None
    mismatches: tuple[str, ...] = ()
    error: str | None = None


def is_stub_signature(tx_signature: str) -> bool:
    """True if the signature is a stub-mode placeholder we should skip."""
    return tx_signature.startswith(STUB_SIG_PREFIX)


def _helius_rpc_url() -> str | None:
    """Build the Helius RPC URL when a key is configured.

    Falls back to None — caller decides whether to use the public Solana
    RPC (rate-limited) or to error out.
    """
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        return None
    cluster = os.environ.get("HELIUS_CLUSTER", "devnet")
    return f"https://{cluster}.helius-rpc.com/?api-key={key}"


def _public_rpc_url(network: str) -> str:
    if network == "solana-mainnet":
        return "https://api.mainnet-beta.solana.com"
    return "https://api.devnet.solana.com"


def make_httpx_rpc(rpc_url: str, *, timeout: float = 10.0) -> RpcCall:
    """Build an `RpcCall` that POSTs JSON-RPC to `rpc_url`."""

    async def _call(method: str, params: list[Any]) -> Any:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(rpc_url, json=body)
            resp.raise_for_status()
            payload = resp.json()
        if "error" in payload:
            raise RuntimeError(f"rpc {method} error: {payload['error']}")
        return payload.get("result")

    return _call


def _extract_usdc_transfer(tx: dict[str, Any], network: str) -> tuple[float | None, str | None]:
    """Pull (amount_usd, destination_owner) from a parsed tx.

    Walks `meta.preTokenBalances` / `meta.postTokenBalances` to find the
    USDC mint delta. Returns (None, None) if no USDC movement detected.
    """
    if not tx:
        return None, None
    meta = tx.get("meta") or {}
    pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances") or []}
    post = {b["accountIndex"]: b for b in meta.get("postTokenBalances") or []}
    usdc_mint = USDC_MAINNET_MINT if network == "solana-mainnet" else USDC_DEVNET_MINT

    best_delta = 0.0
    best_owner: str | None = None
    for idx, post_bal in post.items():
        if post_bal.get("mint") != usdc_mint:
            continue
        post_ui = (post_bal.get("uiTokenAmount") or {}).get("uiAmount") or 0.0
        pre_bal = pre.get(idx)
        pre_ui = 0.0
        if pre_bal is not None:
            pre_ui = (pre_bal.get("uiTokenAmount") or {}).get("uiAmount") or 0.0
        delta = float(post_ui) - float(pre_ui)
        if delta > best_delta:
            best_delta = delta
            best_owner = post_bal.get("owner")
    if best_delta <= 0:
        return None, None
    return best_delta, best_owner


async def verify_target(
    target: VerifyTarget,
    rpc_call: RpcCall,
    *,
    amount_tolerance_usd: float = DEFAULT_AMOUNT_TOLERANCE_USD,
) -> VerifyResult:
    """Verify a single tx_signature against expected values.

    `rpc_call(method, params)` performs the RPC; tests inject a fake.
    """
    if is_stub_signature(target.tx_signature):
        return VerifyResult(target=target, status="skipped_stub")

    try:
        tx = await rpc_call(
            "getTransaction",
            [
                target.tx_signature,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
            ],
        )
    except Exception as exc:
        return VerifyResult(target=target, status="rpc_error", error=str(exc))

    if tx is None:
        return VerifyResult(target=target, status="not_found")

    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return VerifyResult(
            target=target,
            status="mismatch",
            mismatches=(f"on-chain err: {meta.get('err')!r}",),
        )

    confirmation = tx.get("confirmationStatus") or "confirmed"
    actual_amount, actual_recipient = _extract_usdc_transfer(tx, target.network)

    mismatches: list[str] = []
    if confirmation not in {"confirmed", "finalized"}:
        mismatches.append(f"confirmation={confirmation!r} (want confirmed|finalized)")
    if actual_amount is None:
        mismatches.append("no USDC transfer found in tx")
    elif abs(actual_amount - target.expected_amount_usd) > amount_tolerance_usd:
        mismatches.append(f"amount={actual_amount:.6f} expected={target.expected_amount_usd:.6f}")
    if (
        target.expected_recipient is not None
        and actual_recipient is not None
        and actual_recipient != target.expected_recipient
    ):
        mismatches.append(f"recipient={actual_recipient} expected={target.expected_recipient}")

    status = "ok" if not mismatches else "mismatch"
    return VerifyResult(
        target=target,
        status=status,
        confirmation=confirmation,
        actual_amount_usd=actual_amount,
        actual_recipient=actual_recipient,
        mismatches=tuple(mismatches),
    )


async def verify_targets(
    targets: Iterable[VerifyTarget],
    rpc_call: RpcCall,
    *,
    amount_tolerance_usd: float = DEFAULT_AMOUNT_TOLERANCE_USD,
) -> list[VerifyResult]:
    """Verify a batch of targets sequentially (preserves rate-limit safety)."""
    results: list[VerifyResult] = []
    for t in targets:
        results.append(await verify_target(t, rpc_call, amount_tolerance_usd=amount_tolerance_usd))
    return results


def summarize(results: list[VerifyResult]) -> dict[str, int]:
    """Return a {status: count} summary suitable for webhook payloads."""
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def resolve_rpc_url(network: str = "solana-devnet") -> str:
    """Pick Helius (if `HELIUS_API_KEY` set) else public RPC."""
    helius = _helius_rpc_url()
    if helius is not None:
        return helius
    return _public_rpc_url(network)


__all__ = [
    "DEFAULT_AMOUNT_TOLERANCE_USD",
    "STUB_SIG_PREFIX",
    "RpcCall",
    "VerifyResult",
    "VerifyTarget",
    "is_stub_signature",
    "make_httpx_rpc",
    "resolve_rpc_url",
    "summarize",
    "verify_target",
    "verify_targets",
]
