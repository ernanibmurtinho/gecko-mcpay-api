"""Wallet provisioning + balance helpers (S2-05).

Currently only Privy v2 embedded wallets. Lazy-instantiated; the rest of
gecko-core never imports this module unless `is_privy_configured()` returns
True at the call site, so devnet flows without Privy keys keep working.
"""

from gecko_core.wallets.privy import (
    PrivyClient,
    PrivyClientError,
    PrivyNotConfiguredError,
    PrivyWallet,
    is_privy_configured,
)

__all__ = [
    "PrivyClient",
    "PrivyClientError",
    "PrivyNotConfiguredError",
    "PrivyWallet",
    "is_privy_configured",
]
