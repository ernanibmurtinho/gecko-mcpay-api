"""x402 client implementations and the mode factory.

Three modes, identical return shape:
- stub:   in-memory; sleeps briefly; always succeeds. Default for dev/tests.
- live:   real on-chain settlement via the frames.ag AgentWallet bridge —
          frames.ag holds the user keypair and signs/submits. We then poll
          Helius (or public Solana RPC) for confirmation.
- frames: explicit alias for live; reserved for the explicit V2 wallet API
          surface (policy controls, x402_fetch). Today aliases to the same
          bridge as ``live``.

`get_client()` reads `X402_MODE` from env (defaulting to 'stub') via
`PaymentSettings`.

S8-X402-01 — `LiveX402Client.charge()` is now wired:

    PaymentIntent ──► frames.ag /actions/transfer-solana (USDC, devnet)
                     └─► returns txHash
                  ──► Helius getSignatureStatuses poll
                     └─► confirmed within 60s ➜ PaymentResult.success

Devnet is the default. Mainnet is gated by ``X402_NETWORK=solana-mainnet``
(or the legacy CAIP-2 form translated by ``resolve_network``).

Errors are typed (`LiveX402Error` subclasses) and surfaced verbatim — the
gate wraps them into `PaymentRequiredError`. We do **not** swallow or
re-phrase frames.ag's own messages.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import uuid
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

import httpx
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from gecko_core.payments.models import PaymentIntent, PaymentResult

# Single source of truth (S12.5-TEST-01). Every consumer — gecko-api
# settings, the session store, the SQL CHECK constraint — derives its
# accepted set from PAYMENT_MODES. Sprint 12 shipped four parallel copies
# of this list; each fix on the live-mainnet smoke (76bf278, 7e97c59,
# a7f7cc5, ...) flipped one of them while leaving the others stale.
# The schema-drift test in tests/test_payment_mode_consistency.py asserts
# every parallel definition stays in sync.
from gecko_core.payments.modes import PAYMENT_MODES, PaymentMode, X402Mode
from gecko_core.payments.networks import NetworkConfig, resolve_network

logger = logging.getLogger(__name__)

# Frames.ag base URL — overridable for staging / tests.
DEFAULT_FRAMES_BASE = "https://frames.ag/api"

# Path that frames.ag's skill writes the apiToken to. We never log or echo
# the contents — only use it as a Bearer credential.
AGENT_WALLET_CONFIG = Path.home() / ".agentwallet" / "config.json"

# Confirmation poll defaults. The 60s budget covers Solana's worst-case
# leader rotation while keeping the CLI responsive.
DEFAULT_CONFIRM_TIMEOUT_S = 60.0
DEFAULT_CONFIRM_INTERVAL_S = 2.0


class _Unset:
    """Sentinel used to distinguish "argument not passed" from "explicitly None".

    Without this, callers that *intentionally* disable the helius key (e.g.
    tests that want to use the public RPC) cannot override env config.
    """


_UNSET = _Unset()


# ---------------------------------------------------------------------------
# Typed errors — surfaced verbatim by the gate.
# ---------------------------------------------------------------------------


class LiveX402Error(RuntimeError):
    """Base for live-mode payment failures."""


class WalletNotConfiguredError(LiveX402Error):
    """`~/.agentwallet/config.json` missing or unreadable."""


class TreasuryNotConfiguredError(LiveX402Error):
    """`GECKO_WALLET_ADDRESS` not set — can't route a payment."""


class NetworkMismatchError(LiveX402Error):
    """Server says network X but wallet is configured for network Y."""


class InsufficientBalanceError(LiveX402Error):
    """frames.ag rejected for `insufficient_funds` / `INSUFFICIENT_FUNDS`."""


class FramesAuthError(LiveX402Error):
    """frames.ag rejected the apiToken (expired, revoked, malformed)."""


class FramesUnavailableError(LiveX402Error):
    """frames.ag returned 5xx or was unreachable after retries."""


class ConfirmationTimeoutError(LiveX402Error):
    """Submitted on-chain but never confirmed within the timeout."""


# ---------------------------------------------------------------------------
# Network kind resolver — S10-LIVE-03.
#
# Some 402 challenges identify the settlement network with a CAIP-style id
# of the form ``solana:<USDC_MINT>`` (the SPL token mint, NOT the genesis
# hash used by `networks.py`). When that wire form disagrees with what the
# wallet is configured for we want the error message to spell out *which*
# cluster each side wanted instead of just echoing two opaque base58 blobs.
# ---------------------------------------------------------------------------


class NetworkKind(enum.StrEnum):
    """Coarse network classification derived from a USDC mint / chain id.

    Sprint 12 S12-CDP-02 added ``BASE_MAINNET`` / ``BASE_SEPOLIA`` for the
    CDP facilitator path; the existing Solana variants stay untouched.
    """

    SOLANA_MAINNET = "solana-mainnet"
    SOLANA_DEVNET = "solana-devnet"
    BASE_MAINNET = "base-mainnet"
    BASE_SEPOLIA = "base-sepolia"
    UNKNOWN = "unknown"


# Canonical Circle USDC mints. These are not secrets — they are public on-
# chain addresses. Verify by querying any Solana explorer or running
# `spl-token display <mint>` on the respective cluster.
SOLANA_MAINNET_USDC_MINT: Final = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_DEVNET_USDC_MINT: Final = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"


def _resolve_network_kind(network_id: str) -> NetworkKind:
    """Map a CAIP-style id to a typed :class:`NetworkKind`.

    Recognised forms:
      * ``solana:<USDC_MINT>`` — mainnet/devnet Solana via mint.
      * ``solana-mainnet`` / ``solana-devnet`` — friendly names.
      * ``eip155:8453`` — Base mainnet (Sprint 12 CDP path).
      * ``eip155:84532`` — Base Sepolia.
      * ``base-mainnet`` / ``base-sepolia`` — friendly names.

    Returns ``NetworkKind.UNKNOWN`` for malformed input or unknown namespaces
    — never raises. Callers use the typed result to format clearer
    mismatch errors.
    """
    if not isinstance(network_id, str):
        return NetworkKind.UNKNOWN
    friendly = network_id.strip().lower()
    if friendly == "solana-mainnet":
        return NetworkKind.SOLANA_MAINNET
    if friendly == "solana-devnet":
        return NetworkKind.SOLANA_DEVNET
    if friendly == "base-mainnet":
        return NetworkKind.BASE_MAINNET
    if friendly == "base-sepolia":
        return NetworkKind.BASE_SEPOLIA
    if ":" not in network_id:
        return NetworkKind.UNKNOWN
    namespace, _, suffix = network_id.partition(":")
    namespace = namespace.strip().lower()
    if namespace == "solana" and suffix:
        # Two CAIP-style forms in the wild: the USDC mint (used by some
        # 402 challenges) and the cluster genesis-hash prefix (used by
        # ``networks.py``). Recognise both.
        if suffix == SOLANA_MAINNET_USDC_MINT or suffix.startswith("5eykt4Us"):
            return NetworkKind.SOLANA_MAINNET
        if suffix == SOLANA_DEVNET_USDC_MINT or suffix.startswith("EtWTRABZ"):
            return NetworkKind.SOLANA_DEVNET
        return NetworkKind.UNKNOWN
    if namespace == "eip155" and suffix:
        if suffix == "8453":
            return NetworkKind.BASE_MAINNET
        if suffix == "84532":
            return NetworkKind.BASE_SEPOLIA
        return NetworkKind.UNKNOWN
    return NetworkKind.UNKNOWN


def _wallet_caip_id_for(network: NetworkConfig) -> str:
    """Return the ``solana:<USDC_MINT>`` id for a configured wallet network.

    Used only to build mismatch error messages — the live transfer path
    itself uses frames.ag's ``mainnet`` / ``devnet`` short names.
    """
    if network.cluster == "mainnet-beta":
        return f"solana:{SOLANA_MAINNET_USDC_MINT}"
    if network.cluster == "devnet":
        return f"solana:{SOLANA_DEVNET_USDC_MINT}"
    return f"solana:{network.cluster}"


def _format_network_mismatch(server_network_id: str, wallet_network_id: str) -> str:
    """Build a human-friendly mismatch description.

    Falls back to bare CAIP ids if either side resolves to ``UNKNOWN`` so
    operators can still copy-paste them into a block explorer.
    """
    server_kind = _resolve_network_kind(server_network_id)
    wallet_kind = _resolve_network_kind(wallet_network_id)

    def _describe(kind: NetworkKind, network_id: str) -> str:
        if kind is NetworkKind.SOLANA_MAINNET:
            return f"mainnet (mint={SOLANA_MAINNET_USDC_MINT})"
        if kind is NetworkKind.SOLANA_DEVNET:
            return f"devnet (mint={SOLANA_DEVNET_USDC_MINT})"
        return f"unknown network ({network_id!r})"

    return (
        f"you're on {_describe(wallet_kind, wallet_network_id)}, "
        f"server expected {_describe(server_kind, server_network_id)}"
    )


# ---------------------------------------------------------------------------
# Settings.
# ---------------------------------------------------------------------------


class PaymentSettings(BaseSettings):
    """Payment-related env config. Defaults are dev-safe."""

    mode: X402Mode = Field(default="stub", alias="X402_MODE")
    facilitator_url: str | None = Field(default=None, alias="X402_FACILITATOR_URL")
    wallet_secret: SecretStr | None = Field(default=None, alias="X402_WALLET_SECRET")
    frames_api_key: SecretStr | None = Field(default=None, alias="FRAMES_API_KEY")
    frames_base_url: str = Field(default=DEFAULT_FRAMES_BASE, alias="FRAMES_AG_BASE_URL")
    network: str | None = Field(default=None, alias="X402_NETWORK")
    treasury_address: str | None = Field(default=None, alias="GECKO_WALLET_ADDRESS")
    helius_api_key: SecretStr | None = Field(default=None, alias="HELIUS_API_KEY")
    # CDP / Base — Sprint 12 S12-CDP-02. Independent from the Solana
    # treasury so an operator can run both legs simultaneously.
    cdp_api_key_id: str | None = Field(default=None, alias="CDP_API_KEY_ID")
    cdp_api_key_secret: SecretStr | None = Field(default=None, alias="CDP_API_KEY_SECRET")
    base_treasury_address: str | None = Field(default=None, alias="GECKO_WALLET_ADDRESS_BASE")
    # Buyer-side EIP-3009 signer for the CDP/Base path. V1 reuses TWITSH
    # (server-managed Base EOA, already wired for twit.sh) so pay-yourself
    # smoke flows. Independent from CDP_API_KEY_SECRET (which signs CDP
    # JWTs, not on-chain transfers).
    twitsh_wallet_private_key: SecretStr | None = Field(
        default=None, alias="TWITSH_WALLET_PRIVATE_KEY"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache(maxsize=1)
def _settings() -> PaymentSettings:
    return PaymentSettings()


@runtime_checkable
class X402Client(Protocol):
    """The contract every mode honors. Same return shape, same error shape."""

    async def charge(self, intent: PaymentIntent) -> PaymentResult: ...


class StubX402Client:
    """No-op client for dev/tests. Always succeeds. Never hits the network."""

    async def charge(self, intent: PaymentIntent) -> PaymentResult:
        await asyncio.sleep(0.1)
        logger.debug("stub charge ok intent_id=%s", intent.intent_id)
        return PaymentResult(
            intent_id=intent.intent_id,
            status="success",
            tx_signature=None,
            error=None,
        )


# ---------------------------------------------------------------------------
# Helpers — wallet config + Helius RPC.
# ---------------------------------------------------------------------------


def _load_agent_wallet_config(path: Path = AGENT_WALLET_CONFIG) -> dict[str, Any]:
    """Read frames.ag's cached credentials. Never log the token."""
    if not path.exists():
        raise WalletNotConfiguredError(
            f"frames.ag credentials not found at {path}. "
            "Run frames.ag's connect skill: "
            "Read https://frames.ag/skill.md and follow the OTP flow."
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise WalletNotConfiguredError(
            f"frames.ag config at {path} is unreadable: {exc}. Re-run the frames.ag connect flow."
        ) from exc
    if not data.get("apiToken") or not data.get("username"):
        raise WalletNotConfiguredError(
            f"frames.ag config at {path} is missing apiToken/username. "
            "Re-run the frames.ag connect flow."
        )
    return data  # type: ignore[no-any-return]


def _helius_rpc_url(cluster: str, key: str | None) -> str:
    """Pick Helius if a key is configured, else the public Solana RPC."""
    if key:
        # cluster: "devnet" | "mainnet-beta" → Helius hostname
        helius_host = "devnet" if cluster == "devnet" else "mainnet"
        return f"https://{helius_host}.helius-rpc.com/?api-key={key}"
    if cluster == "mainnet-beta":
        return "https://api.mainnet-beta.solana.com"
    return "https://api.devnet.solana.com"


def _network_for_frames(network: NetworkConfig) -> str:
    """frames.ag wants `mainnet` | `devnet` (not the friendly name)."""
    return "devnet" if network.cluster == "devnet" else "mainnet"


def _usdc_smallest_units(amount_usd: Decimal) -> int:
    """USDC has 6 decimals on Solana. Round to the nearest unit."""
    # Quantize defensively — never pass a fractional smallest-unit.
    return int((amount_usd * Decimal(1_000_000)).quantize(Decimal(1)))


# ---------------------------------------------------------------------------
# Live client — frames.ag bridge.
# ---------------------------------------------------------------------------


class LiveX402Client:
    """Real on-chain settlement. Bridges to frames.ag for signing+submission.

    Flow per `charge(intent)`:
      1. Resolve network from env (`X402_NETWORK`).
      2. Resolve treasury from env (`GECKO_WALLET_ADDRESS`).
      3. Read frames.ag apiToken from `~/.agentwallet/config.json`.
      4. POST `/wallets/{username}/actions/transfer-solana` with USDC amount.
      5. Poll Helius `getSignatureStatuses` until confirmed (≤60s).

    The frames.ag wallet handles network-mismatch detection on its side
    (returns 400 + clear code). We mirror that to a typed error here so the
    CLI / gate can show a fix.
    """

    def __init__(
        self,
        facilitator_url: str,
        wallet_secret: SecretStr,
        *,
        frames_base_url: str | None = None,
        network: NetworkConfig | None = None,
        treasury_address: str | None | _Unset = _UNSET,
        helius_api_key: str | None | _Unset = _UNSET,
        config_path: Path = AGENT_WALLET_CONFIG,
        confirm_timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S,
        confirm_interval_s: float = DEFAULT_CONFIRM_INTERVAL_S,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        # Kept for backward-compat with the old skeleton signature.
        self._facilitator_url = facilitator_url
        self._wallet_secret = wallet_secret

        s = _settings()
        self._frames_base = frames_base_url or s.frames_base_url
        self._network = network or resolve_network(s.network)
        if isinstance(treasury_address, _Unset):
            self._treasury = s.treasury_address
        else:
            self._treasury = treasury_address
        if isinstance(helius_api_key, _Unset):
            self._helius_key = (
                s.helius_api_key.get_secret_value() if s.helius_api_key is not None else None
            ) or None
        else:
            self._helius_key = helius_api_key
        self._config_path = config_path
        self._confirm_timeout_s = confirm_timeout_s
        self._confirm_interval_s = confirm_interval_s
        self._http = http_client  # injected for tests; otherwise built per-call

    # --- public --------------------------------------------------------

    async def charge(self, intent: PaymentIntent) -> PaymentResult:
        if not self._treasury:
            raise TreasuryNotConfiguredError(
                "GECKO_WALLET_ADDRESS is not set. Set it to a Solana pubkey you control "
                "(the recipient for x402 settlements). See .env.example."
            )

        config = _load_agent_wallet_config(self._config_path)
        api_token = config["apiToken"]
        username = config["username"]
        wallet_address = config.get("solanaAddress")

        amount_units = _usdc_smallest_units(intent.amount_usd)
        if amount_units <= 0:
            raise LiveX402Error(
                f"intent amount_usd={intent.amount_usd} rounds to 0 USDC smallest units"
            )

        frames_network = _network_for_frames(self._network)
        payload: dict[str, Any] = {
            "to": self._treasury,
            "amount": str(amount_units),
            "asset": "usdc",
            "network": frames_network,
        }
        if wallet_address:
            payload["walletAddress"] = wallet_address

        tx_signature = await self._submit_via_frames(
            api_token=api_token,
            username=username,
            payload=payload,
            intent=intent,
        )

        await self._wait_for_confirmation(tx_signature)

        logger.info(
            "live charge confirmed intent_id=%s tx=%s network=%s",
            intent.intent_id,
            tx_signature,
            self._network.name,
        )
        return PaymentResult(
            intent_id=intent.intent_id,
            status="success",
            tx_signature=tx_signature,
            error=None,
        )

    # --- internals -----------------------------------------------------

    def _build_http(self) -> httpx.AsyncClient:
        if self._http is not None:
            return self._http
        return httpx.AsyncClient(
            base_url=self._frames_base,
            timeout=120.0,
            limits=httpx.Limits(max_keepalive_connections=0, max_connections=10),
            http2=False,
            headers={
                "Connection": "close",
                "User-Agent": "gecko-core/0.1 (+https://geckovision.tech)",
                "Accept": "application/json",
            },
        )

    async def _submit_via_frames(
        self,
        *,
        api_token: str,
        username: str,
        payload: dict[str, Any],
        intent: PaymentIntent,
    ) -> str:
        """POST transfer-solana, return txHash. Raises typed errors on failure."""
        url_path = f"/wallets/{username}/actions/transfer-solana"
        headers = {"Authorization": f"Bearer {api_token}"}

        client = self._build_http()
        owns_client = self._http is None
        try:
            try:
                resp = await client.post(url_path, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise FramesUnavailableError(
                    f"frames.ag unreachable for transfer-solana: {exc}"
                ) from exc
        finally:
            if owns_client:
                await client.aclose()

        return self._parse_frames_response(resp, intent=intent)

    def _parse_frames_response(self, resp: httpx.Response, *, intent: PaymentIntent) -> str:
        try:
            body = resp.json()
        except Exception:
            body = {}

        if resp.is_success:
            tx_hash = body.get("txHash") or body.get("signature")
            if not tx_hash:
                raise FramesUnavailableError(
                    f"frames.ag returned 2xx without txHash for intent {intent.intent_id}: {body!r}"
                )
            return str(tx_hash)

        # Error path — map verbatim where possible.
        code = (body.get("code") or body.get("error") or "").upper()
        message = body.get("message") or resp.text or ""

        if resp.status_code in (401, 403) and code in ("", "UNAUTHORIZED", "FORBIDDEN"):
            raise FramesAuthError(
                "frames.ag rejected the apiToken (likely expired or revoked). "
                "Re-run the frames.ag connect flow: "
                "Read https://frames.ag/skill.md."
            )
        if code in ("INSUFFICIENT_FUNDS",) or "insufficient" in message.lower():
            raise InsufficientBalanceError(
                f"insufficient USDC balance for {intent.amount_usd} USDC on "
                f"{self._network.name}: {message or code}. "
                f"Fund at https://frames.ag/u/<username>."
            )
        if code in ("NETWORK_MISMATCH", "INVALID_NETWORK") or (
            "network" in message.lower()
            and ("mismatch" in message.lower() or "invalid" in message.lower())
        ):
            # Best-effort decode of any `solana:<MINT>` ids the server echoed
            # so the operator sees plain "devnet" / "mainnet" labels.
            server_id = (
                body.get("expectedNetwork")
                or body.get("expected_network")
                or body.get("serverNetwork")
                or ""
            )
            wallet_id = (
                body.get("walletNetwork")
                or body.get("wallet_network")
                or _wallet_caip_id_for(self._network)
            )
            if server_id:
                friendly = _format_network_mismatch(server_id, wallet_id)
            else:
                wallet_kind = _resolve_network_kind(wallet_id)
                friendly = (
                    f"wallet is on {wallet_kind.value} ({wallet_id!r}); "
                    f"server-side network unspecified"
                )
            raise NetworkMismatchError(
                f"network mismatch — {friendly}: {message or code}. "
                "Check X402_NETWORK matches the wallet's chain."
            )
        if code == "POLICY_DENIED":
            raise LiveX402Error(
                f"frames.ag spending policy denied this charge: {message}. "
                "Adjust with `gecko-mcp wallet policy --max-per-tx <USD>`."
            )
        if code == "WALLET_FROZEN":
            raise FramesAuthError(
                f"frames.ag wallet is frozen: {message}. Visit https://frames.ag to resolve."
            )
        if 500 <= resp.status_code < 600:
            raise FramesUnavailableError(
                f"frames.ag returned {resp.status_code}: {message or body or '<empty>'}"
            )

        raise LiveX402Error(
            f"frames.ag error {resp.status_code} {code or '<no code>'}: {message or body!r}"
        )

    async def _wait_for_confirmation(self, tx_signature: str) -> None:
        """Poll `getSignatureStatuses` until confirmed/finalized or timeout.

        Retries the RPC once on transient failure before giving up.
        """
        rpc_url = _helius_rpc_url(self._network.cluster, self._helius_key)
        deadline = asyncio.get_event_loop().time() + self._confirm_timeout_s
        last_error: str | None = None
        rpc_failures = 0

        while asyncio.get_event_loop().time() < deadline:
            try:
                status = await self._fetch_signature_status(rpc_url, tx_signature)
            except Exception as exc:
                rpc_failures += 1
                last_error = str(exc)
                if rpc_failures >= 2:
                    raise ConfirmationTimeoutError(
                        f"Solana RPC failed twice while polling tx={tx_signature}: {last_error}"
                    ) from exc
                await asyncio.sleep(self._confirm_interval_s)
                continue

            if status is None:
                # tx not seen yet — keep polling
                await asyncio.sleep(self._confirm_interval_s)
                continue

            err = status.get("err")
            if err:
                raise LiveX402Error(
                    f"on-chain tx {tx_signature} failed during confirmation: err={err!r}"
                )
            confirmation = status.get("confirmationStatus")
            if confirmation in ("confirmed", "finalized"):
                return
            await asyncio.sleep(self._confirm_interval_s)

        raise ConfirmationTimeoutError(
            f"tx {tx_signature} not confirmed within {self._confirm_timeout_s}s "
            f"on {self._network.name}. Last error: {last_error or 'none'}."
        )

    async def _fetch_signature_status(
        self, rpc_url: str, tx_signature: str
    ) -> dict[str, Any] | None:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignatureStatuses",
            "params": [[tx_signature], {"searchTransactionHistory": True}],
        }
        async with httpx.AsyncClient(timeout=10.0) as rpc:
            resp = await rpc.post(rpc_url, json=body)
            resp.raise_for_status()
            payload = resp.json()
        if "error" in payload:
            raise RuntimeError(f"rpc getSignatureStatuses error: {payload['error']}")
        result = payload.get("result") or {}
        values = result.get("value") or []
        if not values:
            return None
        first = values[0]
        if first is None:
            return None
        return first  # type: ignore[no-any-return]


class FramesX402Client(LiveX402Client):
    """frames.ag wallet API — V2 alias for the live bridge.

    Today this is the same code path as ``LiveX402Client``; we keep the
    distinct class so V2 can layer on policy controls (`max_per_session`,
    `daily_limit`) and route via `/x402/fetch` for x402-protected URLs.
    """

    def __init__(self, api_key: SecretStr) -> None:
        # `api_key` is honored if explicitly passed — otherwise we fall back
        # to the agentwallet config (the default frames.ag flow).
        self._explicit_api_key = api_key
        super().__init__(facilitator_url="", wallet_secret=SecretStr(""))


def get_client(mode: str | None = None) -> X402Client:
    """Build the client for the requested mode (or env default).

    `mode=None` reads X402_MODE from env, defaulting to 'stub'. Sprint 12
    (S12-CDP-02) added ``mode='cdp'`` for the CDP facilitator on Base.

    For network-keyed dispatch, prefer :func:`resolve_client_for_network`.
    """
    s = _settings()
    selected = mode if mode is not None else s.mode

    if selected == "stub":
        return StubX402Client()
    if selected == "live":
        # Constructor must succeed even without facilitator/secret env so the
        # factory tests stay green. charge() will raise typed errors if the
        # treasury or wallet config is missing.
        return LiveX402Client(
            facilitator_url=s.facilitator_url or "",
            wallet_secret=s.wallet_secret or SecretStr(""),
        )
    if selected == "frames":
        return FramesX402Client(api_key=s.frames_api_key or SecretStr(""))
    if selected == "cdp":
        # Lazy import — keep cdp_x402_client out of the import path for
        # callers that never touch CDP, so x402-lib import failures on
        # exotic platforms don't break Solana-only flows.
        from gecko_core.payments.cdp_x402_client import CDPX402Client

        return CDPX402Client(
            api_key_id=s.cdp_api_key_id,
            api_key_secret=s.cdp_api_key_secret,
            treasury_address=s.base_treasury_address,
            payer_private_key=s.twitsh_wallet_private_key,
        )
    raise ValueError(f"unknown X402_MODE: {selected!r}")


def resolve_client_for_network(
    network_id: str | None,
    *,
    mode: str | None = None,
) -> X402Client:
    """Network-aware factory. Routes by ``X402_NETWORK`` resolution.

    Resolution table (Sprint 12 S12-CDP-02; Sprint 13 S13-PAY-01 will
    formalize this on the ``X402Client`` Protocol with ``supported_networks``
    and ``facilitator_id`` class attrs):

      * ``solana-*`` (or CAIP-2 ``solana:<MINT>``) → frames.ag.
      * ``base-mainnet`` / ``eip155:8453`` → CDP.
      * ``base-sepolia`` / ``eip155:84532`` → CDP.
      * Anything else → ``ValueError`` with the offending value echoed.

    ``mode='stub'`` short-circuits to ``StubX402Client`` regardless of
    network — keeps dev/CI on the no-network path.
    """
    s = _settings()
    selected_mode = mode if mode is not None else s.mode

    if selected_mode == "stub":
        return StubX402Client()

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
        "(or a CAIP-2 chain id of the same)."
    )


def facilitator_id_for_network(network_id: str | None) -> str:
    """Report which facilitator settles a given network.

    Used by ``bb doctor`` to render the per-network facilitator row.
    Returns ``'stub'`` when ``X402_MODE=stub``; otherwise ``'frames-solana'``,
    ``'cdp-base'``, or ``'unknown'``.
    """
    s = _settings()
    if s.mode == "stub":
        return "stub"
    kind = _resolve_network_kind(network_id or "")
    if kind in (NetworkKind.SOLANA_MAINNET, NetworkKind.SOLANA_DEVNET):
        return "frames-solana"
    if kind in (NetworkKind.BASE_MAINNET, NetworkKind.BASE_SEPOLIA):
        return "cdp-base"
    return "unknown"


def new_intent_id() -> str:
    """Mint a fresh idempotency key. Client-side uuid4."""
    return str(uuid.uuid4())


def _reset_settings_cache() -> None:
    """Test helper — clears the `lru_cache` on `_settings`."""
    _settings.cache_clear()


__all__ = [
    "AGENT_WALLET_CONFIG",
    "PAYMENT_MODES",
    "SOLANA_DEVNET_USDC_MINT",
    "SOLANA_MAINNET_USDC_MINT",
    "ConfirmationTimeoutError",
    "FramesAuthError",
    "FramesUnavailableError",
    "FramesX402Client",
    "InsufficientBalanceError",
    "LiveX402Client",
    "LiveX402Error",
    "NetworkKind",
    "NetworkMismatchError",
    "PaymentMode",
    "PaymentSettings",
    "StubX402Client",
    "TreasuryNotConfiguredError",
    "WalletNotConfiguredError",
    "X402Client",
    "X402Mode",
    "facilitator_id_for_network",
    "get_client",
    "new_intent_id",
    "resolve_client_for_network",
]
# Re-export the env path for callers that look it up dynamically.
_ = os  # keep ``os`` import used (env reads happen via pydantic-settings)
