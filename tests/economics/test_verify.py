"""S6-RECON-01 verifier tests — stub-skip + mismatch detection.

Mocks the RPC fetcher so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest
from gecko_core.payments.verifier import (
    USDC_DEVNET_MINT,
    VerifyTarget,
    is_stub_signature,
    summarize,
    verify_target,
    verify_targets,
)

TREASURY = "TREASURYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
PAYER = "PAYERxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def _ok_tx(amount: float, owner: str = TREASURY) -> dict[str, Any]:
    """Build a minimally-valid getTransaction response."""
    return {
        "confirmationStatus": "confirmed",
        "meta": {
            "err": None,
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": USDC_DEVNET_MINT,
                    "owner": owner,
                    "uiTokenAmount": {"uiAmount": 0.0},
                },
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": USDC_DEVNET_MINT,
                    "owner": owner,
                    "uiTokenAmount": {"uiAmount": amount},
                },
            ],
        },
    }


def _make_rpc(response: Any):
    async def _call(method: str, params: list[Any]) -> Any:
        return response

    return _call


def test_is_stub_signature() -> None:
    assert is_stub_signature("stub_abc123")
    assert not is_stub_signature("4fdh11111111111111111111111111111111F1Ee")


@pytest.mark.asyncio
async def test_stub_sig_skipped_no_rpc_call() -> None:
    called: list[str] = []

    async def _rpc(method: str, params: list[Any]) -> Any:
        called.append(method)
        return None

    target = VerifyTarget(
        source="session",
        row_id="s1",
        tx_signature="stub_deadbeef",
        expected_amount_usd=0.10,
        expected_recipient=TREASURY,
    )
    result = await verify_target(target, _rpc)
    assert result.status == "skipped_stub"
    assert called == [], "rpc must not be called for stub sigs"


@pytest.mark.asyncio
async def test_ok_when_amount_and_recipient_match() -> None:
    target = VerifyTarget(
        source="session",
        row_id="s2",
        tx_signature="realsig000000",
        expected_amount_usd=0.10,
        expected_recipient=TREASURY,
    )
    result = await verify_target(target, _make_rpc(_ok_tx(0.10)))
    assert result.status == "ok", result.mismatches
    assert result.actual_amount_usd == pytest.approx(0.10)
    assert result.actual_recipient == TREASURY
    assert result.confirmation == "confirmed"


@pytest.mark.asyncio
async def test_mismatch_on_wrong_amount() -> None:
    target = VerifyTarget(
        source="session",
        row_id="s3",
        tx_signature="realsig111",
        expected_amount_usd=0.10,
        expected_recipient=TREASURY,
    )
    # Charged 0.05 instead of 0.10 — way past tolerance.
    result = await verify_target(target, _make_rpc(_ok_tx(0.05)))
    assert result.status == "mismatch"
    assert any("amount=" in m for m in result.mismatches)


@pytest.mark.asyncio
async def test_mismatch_on_wrong_recipient() -> None:
    target = VerifyTarget(
        source="session",
        row_id="s4",
        tx_signature="realsig222",
        expected_amount_usd=0.10,
        expected_recipient=TREASURY,
    )
    result = await verify_target(target, _make_rpc(_ok_tx(0.10, owner=PAYER)))
    assert result.status == "mismatch"
    assert any("recipient=" in m for m in result.mismatches)


@pytest.mark.asyncio
async def test_not_found_when_rpc_returns_none() -> None:
    target = VerifyTarget(
        source="session",
        row_id="s5",
        tx_signature="realsig333",
        expected_amount_usd=0.10,
        expected_recipient=TREASURY,
    )
    result = await verify_target(target, _make_rpc(None))
    assert result.status == "not_found"


@pytest.mark.asyncio
async def test_rpc_error_surfaced_verbatim() -> None:
    async def _bad(method: str, params: list[Any]) -> Any:
        raise RuntimeError("helius 429: rate limited")

    target = VerifyTarget(
        source="session",
        row_id="s6",
        tx_signature="realsig444",
        expected_amount_usd=0.10,
        expected_recipient=TREASURY,
    )
    result = await verify_target(target, _bad)
    assert result.status == "rpc_error"
    assert "rate limited" in (result.error or "")


@pytest.mark.asyncio
async def test_batch_summarize_mixes_stub_and_real() -> None:
    targets = [
        VerifyTarget(
            source="session",
            row_id="a",
            tx_signature="stub_a",
            expected_amount_usd=0.10,
            expected_recipient=TREASURY,
        ),
        VerifyTarget(
            source="memory",
            row_id="b",
            tx_signature="realsig_b",
            expected_amount_usd=0.10,
            expected_recipient=TREASURY,
        ),
    ]

    async def _rpc(method: str, params: list[Any]) -> Any:
        return _ok_tx(0.10)

    results = await verify_targets(targets, _rpc)
    counts = summarize(results)
    assert counts.get("skipped_stub") == 1
    assert counts.get("ok") == 1


@pytest.mark.asyncio
async def test_onchain_err_marks_mismatch() -> None:
    tx = _ok_tx(0.10)
    tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    target = VerifyTarget(
        source="session",
        row_id="s7",
        tx_signature="realsig555",
        expected_amount_usd=0.10,
        expected_recipient=TREASURY,
    )
    result = await verify_target(target, _make_rpc(tx))
    assert result.status == "mismatch"
    assert any("on-chain err" in m for m in result.mismatches)
