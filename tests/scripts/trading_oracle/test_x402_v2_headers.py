"""Tests for the Coinbase x402 v2 buyer wire (header-based protocol).

Modern Bazaar listings (Exa, Zerion, paysponge-wrapped Perplexity, Bankr,
BlockRun) implement Coinbase x402 v2: the 402 challenge ships in a
base64 ``PAYMENT-REQUIRED`` response header (not the JSON body), the
signed payload travels in a ``PAYMENT-SIGNATURE`` request header (not
``X-PAYMENT``), and the settlement receipt comes back in a
``PAYMENT-RESPONSE`` response header.

Light fakes only — see feedback_lighter_tests.md. We exercise the pure
helpers (``_extract_payment_required``, ``_extract_settled_amount``,
``_build_paid_request_headers``) directly. No httpx, no signing, no
network.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

# Load run.py by file path — scripts/ isn't on sys.path by default. Same
# pattern as test_per_call_limit.py / test_service_call_specs.py.
_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "trading_oracle" / "run.py"
_spec = importlib.util.spec_from_file_location("trading_oracle_run_v2", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
run_mod = importlib.util.module_from_spec(_spec)
sys.modules["trading_oracle_run_v2"] = run_mod
_spec.loader.exec_module(run_mod)

_extract_payment_required = run_mod._extract_payment_required
_extract_settled_amount = run_mod._extract_settled_amount
_build_paid_request_headers = run_mod._build_paid_request_headers
_encode_payment_signature_canonical = run_mod._encode_payment_signature_canonical


class _FakeResponse:
    """Light fake of an httpx.Response — only the surface we use."""

    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        body: object | None = None,
        raise_on_json: bool = False,
    ) -> None:
        self.headers = headers or {}
        self._body = body
        self._raise_on_json = raise_on_json

    def json(self) -> object:
        if self._raise_on_json:
            raise ValueError("not json")
        return self._body


def _b64_json(payload: dict[str, object]) -> str:
    """Helper: base64-encode a JSON dict the way Coinbase x402 v2 does."""
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# A. _extract_payment_required: v2 PAYMENT-REQUIRED header path
# ---------------------------------------------------------------------------


def test_extract_payment_required_from_header_base64() -> None:
    """v2: PAYMENT-REQUIRED header carries base64 JSON with accepts[]."""
    challenge = {
        "accepts": [
            {
                "maxAmountRequired": "50000",
                "payTo": "0xseller",
                "asset": "USDC",
                "network": "base-mainnet",
            }
        ],
        "x402Version": 1,
    }
    response = _FakeResponse(
        headers={"PAYMENT-REQUIRED": _b64_json(challenge)},
        # Body is a non-x402 error blob — v2 servers don't put the
        # challenge in the body. Verifies header takes priority.
        body={"error": "payment required"},
    )

    parsed = _extract_payment_required(response)

    assert isinstance(parsed, dict)
    accepts = parsed.get("accepts")
    assert isinstance(accepts, list) and len(accepts) == 1
    assert accepts[0]["payTo"] == "0xseller"
    assert accepts[0]["maxAmountRequired"] == "50000"


# ---------------------------------------------------------------------------
# B. _extract_payment_required: v1 body fallback
# ---------------------------------------------------------------------------


def test_extract_payment_required_falls_back_to_body() -> None:
    """v1: no header, accepts[] lives in the JSON body."""
    body = {
        "accepts": [
            {
                "maxAmountRequired": "10000",
                "payTo": "0xseller-v1",
                "asset": "USDC",
                "network": "base-mainnet",
            }
        ]
    }
    response = _FakeResponse(headers={}, body=body)

    parsed = _extract_payment_required(response)

    assert parsed == body
    accepts = parsed["accepts"]
    assert accepts[0]["payTo"] == "0xseller-v1"


# ---------------------------------------------------------------------------
# C. _extract_settled_amount: v2 PAYMENT-RESPONSE header path
# ---------------------------------------------------------------------------


def test_extract_settled_amount_from_payment_response_header() -> None:
    """v2: PAYMENT-RESPONSE base64 JSON receipt with settledAmount=7000 atomic."""
    receipt = {
        "transactionHash": "0xabc123",
        "settledAmount": "7000",  # 0.007 USDC at 6 decimals
        "network": "base-mainnet",
    }
    headers = {"PAYMENT-RESPONSE": _b64_json(receipt)}

    settled = _extract_settled_amount(headers, fallback_usd=Decimal("0.05"))

    # 7000 atomic / 1e6 = 0.007 USDC. v2 header MUST override fallback.
    assert settled == Decimal("0.007")


# ---------------------------------------------------------------------------
# D. _build_paid_request_headers: emits both v1 X-PAYMENT and v2 PAYMENT-SIGNATURE
# ---------------------------------------------------------------------------


def test_send_both_payment_headers() -> None:
    """Both PAYMENT-SIGNATURE (v2) and X-PAYMENT (v1) carry the same payload."""
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "base-mainnet",
        "payload": {"signature": "0xsig", "authorization": {"value": "50000"}},
    }

    headers = _build_paid_request_headers(payload)

    # Both v2 and v1 headers MUST be present.
    assert "PAYMENT-SIGNATURE" in headers
    assert "X-PAYMENT" in headers
    # Same encoded value (signed bytes are identical — only header name differs).
    assert headers["PAYMENT-SIGNATURE"] == headers["X-PAYMENT"]
    # Decodes back to the original payload.
    decoded = json.loads(base64.b64decode(headers["X-PAYMENT"]).decode("utf-8"))
    assert decoded == payload
    # Accept header for JSON responses.
    assert headers.get("Accept") == "application/json"


# ---------------------------------------------------------------------------
# E. Canonical encoding strips ``null`` cells (Exa 400 X402_INVALID_SIGNATURE fix)
# ---------------------------------------------------------------------------


def test_canonical_encoding_excludes_null_optional_fields() -> None:
    """A signed Pydantic ``PaymentPayload`` must encode without ``null`` cells.

    Exa's strict v2 parser rejects payloads where optional fields
    (``resource.description``, ``resource.mimeType``, top-level
    ``extensions``) are serialized as ``null`` — returns HTTP 400
    ``X402_INVALID_SIGNATURE``. The canonical encoder uses
    ``model_dump_json(by_alias=True, exclude_none=True)`` to drop those
    cells. This test asserts that contract directly: build a real
    ``PaymentPayload`` with sparse optional fields, encode it, decode
    the base64, and verify zero ``null`` substrings in the wire JSON.

    Also asserts the canonical encoding matches the x402 SDK's own
    ``encode_payment_signature_header`` (the bytes the strict server
    expects).
    """
    from x402.http.utils import encode_payment_signature_header
    from x402.schemas import PaymentPayload, PaymentRequirements, ResourceInfo

    requirements = PaymentRequirements(
        scheme="exact",
        network="eip155:8453",
        asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        amount="7000",
        pay_to="0x" + "11" * 20,
        max_timeout_seconds=60,
        extra={
            "name": "USD Coin",
            "version": "2",
            "verifyingContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "assetTransferMethod": "eip3009",
        },
    )
    payload = PaymentPayload(
        x402_version=2,
        accepted=requirements,
        # Intentionally NOT signing — just testing the encoding shape. A
        # placeholder inner ``payload`` dict mirrors the post-sign shape.
        payload={
            "authorization": {
                "from": "0x" + "aa" * 20,
                "to": "0x" + "11" * 20,
                "value": "7000",
                "validAfter": "0",
                "validBefore": "0",
                "nonce": "0x" + "bb" * 32,
            },
            "signature": "0x" + "cc" * 65,
        },
        # ResourceInfo with sparse optionals — these MUST be stripped.
        resource=ResourceInfo(url="https://api.exa.ai/search"),
    )

    encoded = _encode_payment_signature_canonical(payload)
    decoded_json = base64.b64decode(encoded).decode("utf-8")

    # No ``null`` substrings — the strict-zod gate that Exa enforces.
    assert "null" not in decoded_json, f"canonical encoding leaked ``null`` cells: {decoded_json}"

    # The resource URL is preserved verbatim — no query-string suffix, no
    # body hash appended. The signed payload binds to the URL the server
    # advertised in its 402 challenge (``https://api.exa.ai/search``).
    parsed = json.loads(decoded_json)
    assert parsed["resource"]["url"] == "https://api.exa.ai/search"
    # description / mimeType MUST be absent (not ``null``).
    assert "description" not in parsed["resource"]
    assert "mimeType" not in parsed["resource"]
    # ``extensions`` (optional top-level field) MUST be absent.
    assert "extensions" not in parsed

    # EIP-712 domain version threads through ``accepted.extra.version`` and
    # MUST equal Exa's advertised ``"2"`` (USDC EIP-2612 domain v2). If
    # this drifts to ``"1"`` the facilitator would derive the wrong domain
    # separator and the signature would NOT verify.
    assert parsed["accepted"]["extra"]["version"] == "2"

    # Bytes-equal to the x402 SDK's canonical encoder. If this ever diverges
    # we'd be back to producing a payload Exa rejects.
    assert encoded == encode_payment_signature_header(payload)


def test_build_paid_request_headers_uses_canonical_for_pydantic_input() -> None:
    """Passing a Pydantic ``PaymentPayload`` routes through the canonical encoder.

    Regression guard: if someone refactors ``_build_paid_request_headers``
    and accidentally drops the ``hasattr(model_dump_json)`` branch, this
    catches it because the legacy ``json.dumps(dict(...))`` path WOULD
    serialize ``None`` as ``null``.
    """
    from x402.schemas import PaymentPayload, PaymentRequirements, ResourceInfo

    requirements = PaymentRequirements(
        scheme="exact",
        network="eip155:8453",
        asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        amount="7000",
        pay_to="0x" + "11" * 20,
        max_timeout_seconds=60,
        extra={"name": "USD Coin", "version": "2"},
    )
    payload = PaymentPayload(
        x402_version=2,
        accepted=requirements,
        payload={"authorization": {}, "signature": "0x"},
        resource=ResourceInfo(url="https://api.exa.ai/search"),
    )

    headers = _build_paid_request_headers(payload)

    decoded = base64.b64decode(headers["PAYMENT-SIGNATURE"]).decode("utf-8")
    assert "null" not in decoded
    # v1 and v2 headers carry the same bytes.
    assert headers["PAYMENT-SIGNATURE"] == headers["X-PAYMENT"]
