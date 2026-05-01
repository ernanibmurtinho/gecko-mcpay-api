"""Network-keyed factory for ``X402Client`` instances.

S13-PAY-01 split this out of ``x402_client.py`` so that the facilitator
seam is a clean module: protocol → factory → concrete clients. The
behaviour is unchanged from Sprint 12 Track A; we add an explicit
``http-cloudflare`` slot that raises ``NotImplementedError`` so the S15
Cloudflare integration is a config add, not a refactor.

Routing table (resolution order):

  1. ``mode == 'stub'`` → :class:`StubX402Client` (any network).
  2. ``solana-*`` (or CAIP-2 ``solana:<MINT>``) → frames.ag.
  3. ``base-*`` (or CAIP-2 ``eip155:8453`` / ``eip155:84532``) → CDP.
  4. ``http-cloudflare`` → ``NotImplementedError("Sprint 15: ...")``.
  5. Anything else → ``ValueError`` echoing the offending value.

The legacy callable ``resolve_client_for_network`` lives here too;
``x402_client.py`` re-exports it so existing imports keep working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import SecretStr

from gecko_core.payments.protocol import X402Client

if TYPE_CHECKING:
    from gecko_core.payments.models import PaymentIntent

# ---------------------------------------------------------------------------
# Reserved facilitator slot — S15 will fill this in.
# ---------------------------------------------------------------------------


CLOUDFLARE_NETWORK_ID = "http-cloudflare"
"""Reserved network id for the Cloudflare x402 facilitator (Sprint 15).

The factory raises ``NotImplementedError`` with a Sprint-15 message when
this id is encountered, so any code that accidentally tries to settle on
Cloudflare today fails fast with a clear pointer instead of a generic
``KeyError`` or a silent stub-success.
"""


def resolve_client_for_network(
    network_id: str | None,
    *,
    mode: str | None = None,
) -> X402Client:
    """Network-aware factory. Routes by ``X402_NETWORK`` resolution.

    Resolution table (Sprint 12 S12-CDP-02; Sprint 13 S13-PAY-01 added
    the ``http-cloudflare`` reserved slot + the formal Protocol):

      * ``solana-*`` (or CAIP-2 ``solana:<MINT>``) → frames.ag.
      * ``base-mainnet`` / ``eip155:8453`` → CDP.
      * ``base-sepolia`` / ``eip155:84532`` → CDP.
      * ``http-cloudflare`` → ``NotImplementedError`` (S15).
      * Anything else → ``ValueError`` with the offending value echoed.

    ``mode='stub'`` short-circuits to ``StubX402Client`` regardless of
    network — keeps dev/CI on the no-network path.
    """
    # Imported lazily to avoid an import cycle: x402_client imports from
    # protocol, factory imports from both. Doing the concrete-client
    # imports inside the function keeps module import order trivial.
    from gecko_core.payments.x402_client import (
        FramesX402Client,
        LiveX402Client,
        NetworkKind,
        StubX402Client,
        _resolve_network_kind,
        _settings,
    )

    s = _settings()
    selected_mode = mode if mode is not None else s.mode

    if selected_mode == "stub":
        return StubX402Client()

    # S15 reserved slot — explicit, before the unknown-network fall-through
    # so the error message can name Sprint 15 instead of "unknown network".
    if (network_id or "").strip().lower() == CLOUDFLARE_NETWORK_ID:
        raise NotImplementedError(
            "Sprint 15: Cloudflare x402 integration. "
            f"X402_NETWORK={CLOUDFLARE_NETWORK_ID!r} is reserved but the "
            "CloudflareX402Client is not yet implemented."
        )

    kind = _resolve_network_kind(network_id or "")
    if kind in (NetworkKind.SOLANA_MAINNET, NetworkKind.SOLANA_DEVNET):
        if selected_mode == "frames":
            return FramesX402Client(api_key=s.frames_api_key or SecretStr(""))
        return LiveX402Client(
            facilitator_url=s.facilitator_url or "",
            wallet_secret=s.wallet_secret or SecretStr(""),
        )
    if kind in (NetworkKind.BASE_MAINNET, NetworkKind.BASE_SEPOLIA):
        from gecko_core.payments.cdp_x402_client import (
            BASE_MAINNET_NETWORK_ID,
            BASE_SEPOLIA_NETWORK_ID,
            CDPX402Client,
        )

        evm_network = (
            BASE_MAINNET_NETWORK_ID if kind is NetworkKind.BASE_MAINNET else BASE_SEPOLIA_NETWORK_ID
        )
        return CDPX402Client(
            api_key_id=s.cdp_api_key_id,
            api_key_secret=s.cdp_api_key_secret,
            treasury_address=s.base_treasury_address,
            network=evm_network,
        )

    raise ValueError(
        f"unknown X402_NETWORK={network_id!r}; expected solana-* or base-* "
        "(or a CAIP-2 chain id of the same; "
        f"{CLOUDFLARE_NETWORK_ID!r} is reserved for Sprint 15)."
    )


def facilitator_id_for_network(network_id: str | None) -> str:
    """Report which facilitator settles a given network.

    Used by ``bb doctor`` to render the per-network facilitator row.
    Returns ``'stub'`` when ``X402_MODE=stub``; otherwise
    ``'frames-solana'``, ``'cdp-base'``, ``'http-cloudflare'``, or
    ``'unknown'``.
    """
    from gecko_core.payments.x402_client import (
        NetworkKind,
        _resolve_network_kind,
        _settings,
    )

    s = _settings()
    if s.mode == "stub":
        return "stub"
    if (network_id or "").strip().lower() == CLOUDFLARE_NETWORK_ID:
        return "http-cloudflare"
    kind = _resolve_network_kind(network_id or "")
    if kind in (NetworkKind.SOLANA_MAINNET, NetworkKind.SOLANA_DEVNET):
        return "frames-solana"
    if kind in (NetworkKind.BASE_MAINNET, NetworkKind.BASE_SEPOLIA):
        return "cdp-base"
    return "unknown"


def resolve_client(
    intent: PaymentIntent | None = None,
    *,
    network_id: str | None = None,
    mode: str | None = None,
) -> X402Client:
    """Intent-keyed factory — Sprint 14 S14-PAY-MIGRATE-01.

    The single canonical entry point every consumer should use. Wraps
    :func:`resolve_client_for_network` with an intent-aware signature so
    callers don't have to dig the network out of the intent themselves
    (and so future per-intent dispatch — e.g. routing on tier or amount —
    has a stable seam to grow into).

    ``intent`` is optional to support pre-intent introspection paths
    (``bb doctor``, MCP tool surfacing, ``/.well-known/x402`` rendering)
    where we want to know *which* client would settle without minting an
    intent first. When both ``intent`` and ``network_id`` are passed the
    explicit ``network_id`` wins — useful for the (rare) case where a
    consumer routes one intent across multiple facilitators.
    """
    resolved_network: str | None
    if network_id is not None:
        resolved_network = network_id
    elif intent is not None:
        # PaymentIntent today doesn't carry network — it's resolved from
        # ``X402_NETWORK`` in env via the settings cache. Pass None so the
        # underlying factory reads the configured default; once intent
        # carries a per-call network override, swap to ``intent.network``.
        resolved_network = None
    else:
        resolved_network = None
    return resolve_client_for_network(resolved_network, mode=mode)


__all__ = [
    "CLOUDFLARE_NETWORK_ID",
    "facilitator_id_for_network",
    "resolve_client",
    "resolve_client_for_network",
]
