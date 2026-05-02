"""Operator script — record a verdict-settle cassette against the real x402 facilitator.

This is the missing piece between `GECKO_VERDICT_X402_LIVE=1` (which only
makes the contract test skip) and a committed cassette JSON the contract
test can replay against. The contract test deliberately skips auto-record
to keep redaction in operator hands; this script handles the redaction and
the cassette JSON shape so the operator just signs the payment.

Cost: ~$2.50 USDC per recording (the verdict-paywall price).

Usage::

    # Pre-flight: pick a facilitator and have a funded buyer wallet ready.
    # CDP/Base needs GECKO_VERDICT_BUYER_PRIVATE_KEY exported.
    # frames.ag/Solana needs the agentwallet config at ~/.agentwallet/config.json.

    GECKO_VERDICT_X402_LIVE=1 \\
    X402_MODE=live X402_VERDICT_SETTLE_LIVE=1 \\
        uv run python scripts/record_verdict_cassette.py \\
            --facilitator cdp-base \\
            --verdict-hash <64-char-sha256-from-a-real-bb-research-run>

    # On success, writes:
    #   tests/payments/cassettes/verdict_settle/<facilitator>_verify_and_settle.json

After the cassette is committed, unset `GECKO_VERDICT_X402_LIVE` and run the
contract test — it should replay the cassette green. Then flip
``X402_VERDICT_SETTLE_LIVE=1`` in production env to enable the live paywall.

Why this script and not auto-record-in-test:
- Per ``tests/payments/_cassette.py:113``: live mode lets real HTTP through
  but does not auto-record. Operator captures + redacts by hand.
- This script is that capture, packaged so the redaction is automatic and
  the JSON shape matches what ``replay_cassette()`` expects.
- Sensitive headers (Authorization, X-API-Key, Cookie, X-Payment) are
  redacted before write — the cassette is safe to commit publicly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
CASSETTE_DIR = REPO_ROOT / "tests" / "payments" / "cassettes" / "verdict_settle"

# Cassette schema mirrors tests/payments/_cassette.py::replay_cassette.
# Sensitive headers are redacted; body is preserved verbatim.
_SENSITIVE_HEADERS = {
    "authorization",
    "x-api-key",
    "x-cdp-api-key",
    "cookie",
    "x-payment",  # The signed payload — replay reconstructs this from the
                  # cassette flow, not the recorded value.
}


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("<redacted>" if k.lower() in _SENSITIVE_HEADERS else v) for k, v in headers.items()}


class CassetteRecorder:
    """httpx event_hooks recorder. Captures every request/response pair."""

    def __init__(self) -> None:
        self.interactions: list[dict[str, Any]] = []

    async def on_request(self, request: httpx.Request) -> None:
        # Don't snapshot here — the response hook has the matched pair.
        pass

    async def on_response(self, response: httpx.Response) -> None:
        request = response.request
        # Read body BEFORE returning so the response stays consumable downstream.
        body_bytes = await response.aread()
        try:
            body_text = body_bytes.decode("utf-8")
        except UnicodeDecodeError:
            body_text = body_bytes.hex()

        self.interactions.append(
            {
                "request": {
                    "method": request.method,
                    "url": str(request.url),
                    "headers": _redact_headers(dict(request.headers)),
                },
                "response": {
                    "status": response.status_code,
                    "headers": _redact_headers(dict(response.headers)),
                    "body": body_text,
                },
            }
        )


async def record(*, facilitator: str, verdict_hash: str, output: Path) -> None:
    if not os.environ.get("GECKO_VERDICT_X402_LIVE"):
        print("ERROR: GECKO_VERDICT_X402_LIVE=1 must be set to authorise live spend.", file=sys.stderr)
        sys.exit(2)
    if os.environ.get("X402_MODE", "stub") == "stub":
        print("ERROR: X402_MODE must be 'live' (currently 'stub').", file=sys.stderr)
        sys.exit(2)
    if not os.environ.get("X402_VERDICT_SETTLE_LIVE"):
        print(
            "ERROR: X402_VERDICT_SETTLE_LIVE=1 required to dispatch a live verdict settlement.",
            file=sys.stderr,
        )
        sys.exit(2)
    if len(verdict_hash) != 64:
        print(f"ERROR: --verdict-hash must be 64 hex chars (got {len(verdict_hash)}).", file=sys.stderr)
        sys.exit(2)

    recorder = CassetteRecorder()

    # Patch httpx's default async client constructors so every facilitator
    # call routes through the event hooks. The verdict_settle code uses
    # httpx.AsyncClient internally; injecting hooks at construct time is
    # the cleanest seam.
    from gecko_core.payments import verdict_settle

    original_async_client = httpx.AsyncClient

    class _RecordingAsyncClient(original_async_client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            existing_hooks = kwargs.get("event_hooks") or {}
            existing_hooks.setdefault("request", []).append(recorder.on_request)
            existing_hooks.setdefault("response", []).append(recorder.on_response)
            kwargs["event_hooks"] = existing_hooks
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = _RecordingAsyncClient  # type: ignore[misc]

    try:
        # The operator's wallet flow has to actually sign a real x402 payment.
        # verify_verdict_payment expects the X-Payment payload as bytes; the
        # operator's wallet integration produces it. For the recorder, we
        # invoke the higher-level flow that also drives the /verify request.
        receipt = await verdict_settle.verify_verdict_payment(
            payload=b"<live-x-payment-from-wallet>",
            verdict_hash=verdict_hash,
            mode="live",
        )
        print(f"settlement OK: facilitator={receipt.facilitator}, tx={receipt.tx_signature}")
    except Exception as exc:
        print(f"settlement failed: {exc!r}", file=sys.stderr)
        # Even on failure, write what we captured — the failure trace is
        # itself diagnostic value worth preserving.
    finally:
        httpx.AsyncClient = original_async_client  # type: ignore[misc]

    if not recorder.interactions:
        print("WARN: no HTTP interactions captured. Settlement code may not have made calls.", file=sys.stderr)
        sys.exit(3)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"interactions": recorder.interactions}, indent=2, sort_keys=True)
    )
    print(f"wrote {len(recorder.interactions)} interactions to {output}")
    print(f"\nNext steps:")
    print(f"  1. Inspect {output.name} for any unredacted sensitive data before committing.")
    print(f"  2. git add {output} && git commit -m 'record verdict-settle cassette ({facilitator})'")
    print(f"  3. unset GECKO_VERDICT_X402_LIVE")
    print(f"  4. uv run pytest tests/payments/test_verdict_settle_contract.py -m live_x402_verdict")
    print(f"  5. flip X402_VERDICT_SETTLE_LIVE=1 in production env to enable the paywall.")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else "")
    p.add_argument(
        "--facilitator",
        choices=("cdp-base", "frames-solana"),
        required=True,
        help="Which facilitator to record against. Cassette filename derives from this.",
    )
    p.add_argument(
        "--verdict-hash",
        required=True,
        help="64-char sha256 verdict_hash to bind the scope to. Pull one from a recent bb research footer.",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    cassette_name = (
        "cdp_base_verify_and_settle.json"
        if args.facilitator == "cdp-base"
        else "frames_solana_verify_and_settle.json"
    )
    output = CASSETTE_DIR / cassette_name
    if output.exists():
        print(
            f"ERROR: cassette {output} already exists. Delete it first if you want to re-record.",
            file=sys.stderr,
        )
        sys.exit(2)
    asyncio.run(record(facilitator=args.facilitator, verdict_hash=args.verdict_hash, output=output))


if __name__ == "__main__":
    main()
