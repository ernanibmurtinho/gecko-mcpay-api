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

__all__ = ["PAYMENT_MODES", "PaymentMode", "X402Mode"]
