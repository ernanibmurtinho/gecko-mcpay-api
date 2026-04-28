"""Project-wallet provisioning glue between gecko-api and gecko-core (S2-05).

Two responsibilities:

1. ``ensure_project_wallet`` — eager-or-lazy provisioning. Returns the
   project's existing wallet OR creates one via Privy and persists it.
   Called from ``POST /projects`` (eager, at project creation) and from the
   ``/research`` + ``/research/pro`` handlers when a ``project_id`` is
   provided (lazy, on first paid call).

2. ``check_project_budget`` — cheap server-side pre-flight that rejects a
   paid call when ``spent_usd >= budget_cap_usd``. The cryptographic
   per-wallet enforcement is a separate (deferred) ticket — until then, this
   column-driven check is the sole gate.

Race-window note: two concurrent first-time paid calls on the same project
can both observe ``privy_wallet_id IS NULL`` and try to create a wallet.
We mitigate by:
  * the unique partial index on ``projects.privy_wallet_id`` (migration 013)
    — second writer's UPDATE will fail at the DB layer, and
  * re-reading the project row after the failure, returning whichever wallet
    won the race.
We accept a single orphaned Privy wallet in the worst case; that's far
preferable to a per-project advisory lock that would block the hot path.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException
from gecko_core.sessions.store import ProjectWallet, SessionStore
from gecko_core.wallets.privy import (
    PrivyClient,
    PrivyClientError,
    PrivyNotConfiguredError,
)
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from gecko_api.settings import Settings

logger = logging.getLogger(__name__)


async def ensure_project_wallet(
    *,
    settings: Settings,
    store: SessionStore,
    project_id: UUID,
) -> ProjectWallet | None:
    """Return the project's wallet snapshot, lazy-creating if missing.

    Returns None when Privy is unconfigured (sentinel state) — callers must
    treat that as "no wallet yet, fall back to user wallet" rather than an
    error. Devnet without Privy keys still serves the user.
    """
    snapshot = await store.get_project_wallet(project_id)
    if snapshot is None:
        # Project doesn't exist or is soft-deleted; bubble up so the caller
        # surfaces 404 from its existing path.
        return None
    if snapshot.privy_wallet_id and snapshot.privy_wallet_address:
        return snapshot

    if not settings.is_privy_configured():
        # No Privy creds — leave the wallet columns NULL and let the caller
        # proceed with the user's main wallet. We log once per call rather
        # than spamming because this is the documented dev path.
        logger.info(
            "ensure_project_wallet: Privy unconfigured, skipping for project %s",
            project_id,
        )
        return snapshot

    try:
        client = PrivyClient(
            app_id=settings.privy_app_id,
            app_secret=settings.privy_app_secret,
        )
    except PrivyNotConfiguredError:
        # Defensive: settings flag said configured but constructor disagreed.
        # Treat as soft-skip rather than 5xx the user's paid call.
        logger.warning("ensure_project_wallet: PrivyClient refused to start; skipping")
        return snapshot

    try:
        async with client:
            wallet = await client.create_solana_wallet(owner_label=str(project_id))
    except PrivyClientError as exc:
        # If Privy says the external_id already exists, another concurrent
        # request beat us. Re-read the row and return whatever was persisted.
        msg = str(exc).lower()
        if "external_id" in msg or "already" in msg or "duplicate" in msg or "409" in msg:
            logger.info(
                "ensure_project_wallet: race lost for project %s, re-reading",
                project_id,
            )
            again = await store.get_project_wallet(project_id)
            if again and again.privy_wallet_id and again.privy_wallet_address:
                return again
        logger.exception("ensure_project_wallet: Privy create failed")
        # Don't 5xx the paid call — fall back to the user's main wallet.
        return snapshot

    try:
        await store.set_project_wallet(
            project_id,
            privy_wallet_id=wallet.wallet_id,
            privy_wallet_address=wallet.address,
        )
    except Exception as exc:
        # Unique-index collision = race lost. Re-read.
        if "duplicate" in str(exc).lower() or "23505" in str(exc):
            again = await store.get_project_wallet(project_id)
            if again and again.privy_wallet_id and again.privy_wallet_address:
                return again
        logger.exception(
            "ensure_project_wallet: set_project_wallet failed for %s",
            project_id,
        )
        return snapshot

    return ProjectWallet(
        privy_wallet_id=wallet.wallet_id,
        privy_wallet_address=wallet.address,
        budget_cap_usd=snapshot.budget_cap_usd,
        spent_usd=snapshot.spent_usd,
    )


def project_budget_exceeded_response(
    *,
    project_id: UUID,
    spent_usd: Decimal,
    budget_cap_usd: Decimal,
    project_name: str | None = None,
) -> JSONResponse:
    """Build the canonical 402 body for a project that has hit its cap.

    Centralized so /research and /research/pro return the exact same shape.
    Returned as 402 (Payment Required) to mirror x402's wire format — clients
    that already handle 402 from the middleware will surface this cleanly.
    """
    label = project_name or str(project_id)
    return JSONResponse(
        status_code=402,
        content={
            "error": "project_budget_exceeded",
            "message": (
                f"Project '{label}' has reached its "
                f"${budget_cap_usd:.2f} budget cap. "
                "Increase via `gecko project budget set <name> <amount>` "
                "or create a new project."
            ),
            "project_id": str(project_id),
            "spent_usd": f"{spent_usd:.2f}",
            "budget_cap_usd": f"{budget_cap_usd:.2f}",
        },
    )


async def check_project_budget(
    *,
    store: SessionStore,
    project_id: UUID,
) -> JSONResponse | None:
    """Return a 402 response if the project is at-or-over cap; None otherwise.

    Soft-fails on store errors — never block a paid call because of a
    transient Supabase blip. The wallet's own balance is the real ceiling
    once mainnet + Privy enforcement land.
    """
    try:
        snapshot = await store.get_project_wallet(project_id)
    except Exception:
        logger.exception("check_project_budget: get_project_wallet failed")
        return None
    if snapshot is None:
        # Project missing — let the existing handler surface 404.
        return None
    if not isinstance(snapshot, ProjectWallet):
        # Defensive: a misconfigured store returning a non-ProjectWallet
        # shouldn't 5xx the call. Skip the gate.
        return None
    try:
        over = snapshot.spent_usd >= snapshot.budget_cap_usd
    except (TypeError, ValueError):
        logger.exception("check_project_budget: comparison failed")
        return None
    if over:
        return project_budget_exceeded_response(
            project_id=project_id,
            spent_usd=snapshot.spent_usd,
            budget_cap_usd=snapshot.budget_cap_usd,
        )
    return None


async def increment_project_spent_safe(
    *,
    store: SessionStore,
    project_id: UUID,
    delta_usd: Decimal,
) -> None:
    """Best-effort wrapper around the atomic RPC.

    Failure to record spend never fails the user-facing response — the cap
    check is best-effort, and at mainnet time the wallet's own USDC balance
    is the cryptographic ceiling.
    """
    try:
        await store.increment_project_spent(project_id, delta_usd)
    except Exception:
        logger.exception(
            "increment_project_spent_safe: failed for project %s, delta=%s",
            project_id,
            delta_usd,
        )


def _ensure_uuid(value: str) -> UUID:
    """Parse a project_id, raising 400 instead of 5xx for malformed input."""
    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid project_id") from exc


__all__ = [
    "_ensure_uuid",
    "check_project_budget",
    "ensure_project_wallet",
    "increment_project_spent_safe",
    "project_budget_exceeded_response",
]
