"""S24 WS-E — paid-verdict receipt envelope tests.

Asserts the three new fields (``tx_signature``, ``solscan_url``,
``settlement_mode``) are populated by :func:`gecko_core.payments.receipts.build_receipt`
in both stub-mode and live-mode shapes. Wire-level test for the wedge
claim ("every paid verdict carries an on-chain Solscan link").

Light fakes only — no FastAPI TestClient, no AG2 panel run. The handler
calls ``build_receipt`` against ``(tx_signature, x402_mode)`` extracted
from the request; this test exercises that contract directly.
"""

from __future__ import annotations

from gecko_core.orchestration.trade_panel.models import TradePanelVerdict
from gecko_core.payments.receipts import build_receipt


def _base_verdict_kwargs() -> dict[str, object]:
    return {
        "verdict": "act",
        "confidence": 0.7,
        "key_drivers": ["d1"],
        "blocker_questions": [],
        "turns": [],
        "citations": [],
    }


def test_receipt_stub_mode_has_null_signature() -> None:
    """X402_MODE=stub → tx_signature null, solscan null, mode=stub."""
    receipt = build_receipt(tx_signature=None, x402_mode="stub")
    assert receipt == {
        "tx_signature": None,
        "solscan_url": None,
        "settlement_mode": "stub",
    }


def test_receipt_stub_artifact_is_treated_as_stub() -> None:
    """Even if a stub-* artifact is passed, it must not advertise a Solscan URL."""
    receipt = build_receipt(tx_signature="stub-abc123", x402_mode="live")
    assert receipt["tx_signature"] is None
    assert receipt["solscan_url"] is None
    assert receipt["settlement_mode"] == "stub"


def test_receipt_live_mode_builds_solscan_url() -> None:
    """Real signature + live mode → Solscan deep link."""
    sig = "5J7pRecordedTxHash5J7pRecordedTxHash5J7pRecordedTxHash5J7pRecordedT"
    receipt = build_receipt(tx_signature=sig, x402_mode="live")
    assert receipt["tx_signature"] == sig
    assert receipt["solscan_url"] == f"https://solscan.io/tx/{sig}"
    assert receipt["settlement_mode"] == "live"


def test_receipt_unset_x402_mode_defaults_to_stub() -> None:
    """No X402_MODE in env → defensive stub."""
    receipt = build_receipt(tx_signature=None, x402_mode=None)
    assert receipt["settlement_mode"] == "stub"


def test_verdict_envelope_emits_three_new_fields_in_stub_mode() -> None:
    """Envelope shape: keys exist with stub defaults even when no payment ran."""
    v = TradePanelVerdict(**_base_verdict_kwargs())  # type: ignore[arg-type]
    payload = v.model_dump()
    assert payload["tx_signature"] is None
    assert payload["solscan_url"] is None
    assert payload["settlement_mode"] == "stub"


def test_verdict_envelope_accepts_live_receipt() -> None:
    sig = "F4mEsRecordedTxHashF4mEsRecordedTxHashF4mEsRecordedTxHashF4mEsRec"
    kw = _base_verdict_kwargs()
    receipt = build_receipt(tx_signature=sig, x402_mode="live")
    kw.update(receipt)
    v = TradePanelVerdict(**kw)  # type: ignore[arg-type]
    assert v.tx_signature == sig
    assert v.solscan_url == f"https://solscan.io/tx/{sig}"
    assert v.settlement_mode == "live"
