"""The payment gate.

Single entry point: `run_payment_gate(session_id, tier, store, client=None)`.
Implements the canonical 5-step sequence from `docs/implementation-plan.md`:

  1. Session row already exists (status='pending') — caller created it.
  2. Generate intent_id, write to sessions.payment_intent_id.
  3. Call client.charge(intent).
  4. On success: status='indexing', return PaymentResult.
  5. On failure: status='failed', raise PaymentRequiredError. NO ingestion.

Idempotent on intent_id — if the session already has one persisted, we reuse
it and short-circuit when its previous result was a success.
"""

from __future__ import annotations

import logging
import time
from uuid import UUID

from gecko_core.models import Tier
from gecko_core.payments.models import (
    PaymentIntent,
    PaymentRequiredError,
    PaymentResult,
)
from gecko_core.payments.pricing import price_for
from gecko_core.payments.x402_client import (
    X402Client,
    get_client,
    new_intent_id,
)
from gecko_core.sessions.store import SessionStore

logger = logging.getLogger(__name__)


async def run_payment_gate(
    session_id: UUID,
    tier: Tier,
    store: SessionStore,
    client: X402Client | None = None,
) -> PaymentResult:
    """Run the payment gate for an existing pending session.

    Returns the PaymentResult on success. Raises PaymentRequiredError on
    failure after marking the session 'failed'. Idempotent on intent_id.
    """
    if client is None:
        client = get_client()

    record = await store.get(session_id)
    if record is None:
        raise ValueError(f"session not found: {session_id}")

    # Idempotency: reuse intent_id if one is already persisted. If the
    # session is already past 'pending', the gate has already run — don't
    # double-charge.
    if record.payment_intent_id is not None:
        intent_id = record.payment_intent_id
        if record.status != "pending":
            logger.info(
                "gate idempotent skip session_id=%s intent_id=%s status=%s",
                session_id,
                intent_id,
                record.status,
            )
            return PaymentResult(
                intent_id=intent_id,
                status="success",
                tx_signature=None,
                error=None,
            )
    else:
        intent_id = new_intent_id()
        await store.set_payment_intent(session_id, intent_id)

    intent = PaymentIntent(
        intent_id=intent_id,
        session_id=session_id,
        tier=tier,
        amount_usd=price_for(tier),
    )

    _t0 = time.monotonic()
    try:
        result = await client.charge(intent)
    except Exception as e:
        # Surface verbatim — but mark failed first so we don't leave a
        # zombie 'pending' row.
        await store.update_status(session_id, "failed")
        logger.warning(
            "gate charge raised session_id=%s intent_id=%s",
            session_id,
            intent_id,
        )
        raise PaymentRequiredError(
            session_id=session_id,
            tier=tier,
            intent_id=intent_id,
            reason=str(e),
        ) from e

    if result.status != "success":
        await store.update_status(session_id, "failed")
        logger.info(
            "gate failed session_id=%s intent_id=%s",
            session_id,
            intent_id,
        )
        raise PaymentRequiredError(
            session_id=session_id,
            tier=tier,
            intent_id=intent_id,
            reason=result.error or "charge declined",
        )

    await store.update_status(session_id, "indexing")
    logger.info(
        "gate ok session_id=%s intent_id=%s",
        session_id,
        intent_id,
    )
    # S24 WS-D — economics ledger. Best-effort, never blocks the gate.
    try:
        from gecko_core.observability import emit_event

        await emit_event(
            "x402.settle",
            {
                "route": f"session:{tier}",
                "intent_id": str(intent_id),
                "session_id": str(session_id),
                "amount_usdc": float(price_for(tier)),
                "signature": result.tx_signature,
                "latency_ms": int((time.monotonic() - _t0) * 1000),
            },
        )
    except Exception:  # pragma: no cover - never block on ledger
        logger.debug("x402.settle ledger write failed", exc_info=True)
    return result


__all__ = ["run_payment_gate"]
