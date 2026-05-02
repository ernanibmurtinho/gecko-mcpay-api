"""Single source of truth for the X402 payment-mode enum (S12.5-TEST-01).

This module is intentionally a leaf: zero intra-package imports. Sprint 12
shipped four parallel definitions of the same Literal — gecko-core
`x402_client.X402Mode`, gecko-core `sessions.store.PaymentMode`, gecko-api
`settings.X402Mode`, and the SQL CHECK on `sessions.payment_mode`. Each
fix during the live-mainnet smoke (76bf278, 7e97c59, a7f7cc5) flipped one
copy and missed the others.

Now every consumer imports `PAYMENT_MODES` (and the matching `PaymentMode`
/ `X402Mode` Literal) from here. The schema-drift test in
`tests/test_payment_mode_consistency.py` parses the active SQL migration
and asserts the CHECK constraint matches `PAYMENT_MODES` exactly.

Why a separate module instead of `x402_client`: importing a submodule of
`gecko_core.payments` triggers `payments/__init__.py`, which pulls in
`gate.py`, which imports `SessionStore` — a circular import the moment the
session store wants its own `PaymentMode`. Putting the constants in a leaf
module (no `gecko_core.*` imports) breaks the cycle without TYPE_CHECKING
hacks.
"""

from __future__ import annotations

from typing import Final, Literal

PAYMENT_MODES: Final[tuple[str, ...]] = ("stub", "live", "frames", "cdp")
"""Runtime tuple — used by env validation and schema-drift assertions."""

PaymentMode = Literal["stub", "live", "frames", "cdp"]
"""Static type alias. Keep in sync with PAYMENT_MODES manually — the
schema-drift test verifies they match (typing.get_args(PaymentMode))."""

# Historical name. Both refer to the exact same Literal type.
X402Mode = PaymentMode

# ---------------------------------------------------------------------------
# Buyer-side (S16 Track B) — outbound x402 consumer modes.
#
# Sibling Literal to PaymentMode. Shapes differ:
#   * No "frames" yet on the buyer path (Solana outbound lands later).
#   * Otherwise mirrors the seller-side modes 1:1.
#
# Per Pattern A: declared once, here. Consumers import from this module —
# never redeclare. ``X402_CONSUMER_MODE`` env var defaults to ``X402_MODE``
# but is logged separately so buyer vs seller failures stay decoupled.
# ---------------------------------------------------------------------------

CONSUMER_MODES: Final[tuple[str, ...]] = ("stub", "live", "cdp")
"""Runtime tuple — used by env validation."""

ConsumerMode = Literal["stub", "live", "cdp"]
"""Static type alias for the buyer-side X402Consumer mode. Keep in sync
with CONSUMER_MODES manually."""

__all__ = [
    "CONSUMER_MODES",
    "PAYMENT_MODES",
    "ConsumerMode",
    "PaymentMode",
    "X402Mode",
]
