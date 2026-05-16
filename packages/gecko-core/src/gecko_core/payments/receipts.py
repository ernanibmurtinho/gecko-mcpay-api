"""Receipt-shape helpers for paid verdict envelopes (S24 WS-E).

A receipt is the user-visible proof that a paid verdict was settled on chain.
The envelope carries three fields:

  * ``tx_signature``   — on-chain Solana signature (None when stub).
  * ``solscan_url``    — Solscan deep link (None when stub).
  * ``settlement_mode``— ``stub`` | ``live`` (see gecko_core.types).

This module owns the *one* place that builds the Solscan URL so the format
never drifts across handlers. Anything else that needs to render a receipt
imports :func:`build_receipt` — no string templates copied at call sites.
"""

from __future__ import annotations

from typing import TypedDict

from gecko_core.types import SettlementMode

# Mainnet Solscan. Devnet/testnet links would need a `?cluster=` suffix; we
# only ship mainnet today so a single template is enough. Add a cluster
# parameter here when devnet/testnet settlement lands.
_SOLSCAN_TX_TEMPLATE = "https://solscan.io/tx/{sig}"


class VerdictReceipt(TypedDict):
    """Wire shape carried on every paid verdict envelope."""

    tx_signature: str | None
    solscan_url: str | None
    settlement_mode: SettlementMode


def build_receipt(
    *,
    tx_signature: str | None,
    x402_mode: str | None,
) -> VerdictReceipt:
    """Build the receipt dict from the settle event + resolved x402 mode.

    Decision table:

      x402_mode == "stub"  → ``settlement_mode="stub"``, sig/url null.
      tx_signature is None → ``settlement_mode="stub"`` (defensive — no on-chain
                              evidence means we don't claim live settlement).
      tx_signature is a stub artifact (``stub-<hex>``) → settlement_mode="stub".
      otherwise            → ``settlement_mode="live"`` with solscan URL.

    The stub-artifact branch defends against handlers that always populate
    ``tx_signature`` (e.g. from ``sessions.x402_tx_signature``) even when the
    middleware ran in stub mode — without it, a stub-mode envelope would
    proudly claim a Solscan URL for a fake hash.
    """
    mode_norm = (x402_mode or "stub").strip().lower()
    if mode_norm == "stub" or tx_signature is None or tx_signature.startswith("stub-"):
        return VerdictReceipt(
            tx_signature=None,
            solscan_url=None,
            settlement_mode="stub",
        )
    return VerdictReceipt(
        tx_signature=tx_signature,
        solscan_url=_SOLSCAN_TX_TEMPLATE.format(sig=tx_signature),
        settlement_mode="live",
    )


__all__ = ["VerdictReceipt", "build_receipt"]
