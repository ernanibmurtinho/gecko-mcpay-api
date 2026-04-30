"""Wallet facade for the gecko-mcp CLI — frames.ag AgentWallet (v3).

We do NOT hold private keys. The frames.ag skill (`https://frames.ag/skill.md`)
owns the connect flow and caches credentials at `~/.agentwallet/config.json`.
This module reads that canonical path and proxies wallet operations to
`frames.ag/api/wallets/{username}/...`.

Self-custody fallback (v2) lives in `wallet_self_custody.py` and is unused by
default; switch by setting `GECKO_WALLET_PROVIDER=self`.

Security:
- The apiToken is treated as a password. It is never logged, echoed, or
  included in error messages.
- We read `~/.agentwallet/config.json` once per command invocation.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import click
import httpx
from gecko_core.wallet import (
    DEFAULT_CONFIG_PATH as _SHARED_CONFIG_PATH,
)
from gecko_core.wallet import (
    read_agent_config as _read_agent_config_shared,
)

# Re-export for back-compat with code/tests that import CONFIG_PATH from here.
CONFIG_PATH = _SHARED_CONFIG_PATH
FRAMES_BASE = os.environ.get("FRAMES_AG_BASE_URL", "https://frames.ag/api")
DEFAULT_TIMEOUT = 30.0

# Legacy v2 self-custody symbols re-exported for backwards compatibility
# (demo-agent, doctor.py, tests/mcp/test_wallet.py). v3 default is frames.ag;
# the v2 path activates when GECKO_WALLET_PROVIDER=self.
from gecko_mcp.wallet_self_custody import (  # noqa: E402
    WALLET_PATH,
    get_keypair_for_signing,
)

__all__ = [  # noqa: RUF022 - grouping (v3 facade above, v2 legacy below) is intentional
    "CONFIG_PATH",
    "FRAMES_BASE",
    "FramesAGWallet",
    "WalletNotConfiguredError",
    "wallet",
    # legacy v2 fallback
    "WALLET_PATH",
    "get_keypair_for_signing",
]


class WalletNotConfiguredError(RuntimeError):
    pass


def _read_config() -> dict[str, Any]:
    """Wrap the shared `gecko_core.wallet` parser with the MCP-side error
    semantics (raise WalletNotConfiguredError when missing, with the existing
    user-facing hint). Single source of truth lives in gecko-core so doctor
    and MCP can't drift on what counts as a valid config (S9-DOCTOR-01).
    """
    cfg = _read_agent_config_shared(CONFIG_PATH)
    if cfg is None:
        raise WalletNotConfiguredError(
            f"No frames.ag credentials at {CONFIG_PATH}. "
            "Run `gecko-mcp wallet new` to connect (delegates to frames.ag's skill)."
        )
    return cfg


def _client(config: dict[str, Any]) -> httpx.Client:
    token = config["apiToken"]
    return httpx.Client(
        base_url=FRAMES_BASE,
        headers={"Authorization": f"Bearer {token}"},
        timeout=DEFAULT_TIMEOUT,
    )


def _username(config: dict[str, Any]) -> str:
    return str(config["username"])


def _frames_error_message(exc: httpx.HTTPStatusError) -> str:
    """Map frames.ag error codes to user-actionable messages.

    Codes are listed in https://frames.ag/skill.md and we surface them verbatim
    rather than paraphrasing — agents and users both deserve the real reason.
    """
    try:
        body = exc.response.json()
    except Exception:
        return f"frames.ag returned {exc.response.status_code}"
    code = body.get("code") or body.get("error") or ""
    message = body.get("message") or ""
    hints = {
        "POLICY_DENIED": "Your spending policy denied this payment. Update with `gecko-mcp wallet policy --max-per-tx <USD>`.",
        "WALLET_FROZEN": "Your frames.ag wallet is frozen. Visit https://frames.ag to resolve.",
        "insufficient_funds": "Wallet balance insufficient. Fund at https://frames.ag/u/<username>.",
        "PAYMENT_REJECTED": "Target API rejected the payment (target-side issue).",
        "INVALID_URL": "URL is malformed, points to localhost, or an internal IP.",
        "TARGET_TIMEOUT": "Target API timed out.",
        "TARGET_ERROR": "Target API returned a 5xx error.",
        "NO_PAYMENT_OPTION": "No compatible payment network for this endpoint.",
    }
    hint = hints.get(code, "")
    parts = [code or "frames.ag error", message, hint]
    return " — ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Click commands
# ---------------------------------------------------------------------------


@click.group()
def wallet() -> None:
    """Manage the agent wallet that pays for Gecko sessions via x402.

    Wallet custody is delegated to frames.ag's AgentWallet — server-custodial,
    x402-native, policy-controlled. We don't hold private keys here.
    """


@wallet.command()
def new() -> None:
    """Connect to frames.ag (delegates to frames.ag's skill).

    Setup is owned by frames.ag's skill. This command checks whether
    `~/.agentwallet/config.json` already exists; if not, instructs the user
    (or the agent driving this CLI) to run frames.ag's connect flow.
    """
    if CONFIG_PATH.exists():
        try:
            config = _read_config()
        except Exception as exc:
            click.secho(
                f"Existing config at {CONFIG_PATH} is unreadable: {exc}", fg="red", err=True
            )
            sys.exit(1)
        click.secho("Already connected to frames.ag.", fg="green")
        click.echo(f"  username:        @{config.get('username', '?')}")
        click.echo(f"  solana address:  {config.get('solanaAddress', '?')}")
        click.echo(f"  evm address:     {config.get('evmAddress', '?')}")
        click.echo()
        click.echo(f"Fund at https://frames.ag/u/{config.get('username', '<username>')}")
        return

    click.secho("Not connected to frames.ag.", fg="yellow")
    click.echo()
    click.echo("Wallet provisioning is delegated to frames.ag's skill.")
    click.echo("From your AI agent (Claude Code, Cursor, etc.), run:")
    click.echo()
    click.secho(
        "    Read https://frames.ag/skill.md and follow the instructions to join AgentWallet.",
        bold=True,
    )
    click.echo()
    click.echo("After connection, frames.ag's skill caches credentials at:")
    click.echo(f"    {CONFIG_PATH}")
    click.echo()
    click.echo("Re-run `gecko-mcp wallet new` to verify, then `gecko-mcp wallet balance`.")


@wallet.command()
@click.option("--chain", type=click.Choice(["solana", "evm", "both"]), default="both")
def address(chain: str) -> None:
    """Print the wallet address(es). Safe to share."""
    config = _read_config()
    if chain in ("solana", "both"):
        click.echo(f"solana: {config.get('solanaAddress', '<missing>')}")
    if chain in ("evm", "both"):
        click.echo(f"evm:    {config.get('evmAddress', '<missing>')}")


@wallet.command()
def balance() -> None:
    """Show USDC balance per chain."""
    config = _read_config()
    with _client(config) as http:
        try:
            r = http.get(f"/wallets/{_username(config)}/balances")
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            click.secho(_frames_error_message(exc), fg="red", err=True)
            sys.exit(1)
        except httpx.HTTPError as exc:
            click.secho(f"frames.ag unreachable: {exc}", fg="red", err=True)
            sys.exit(1)
    body = r.json()
    click.echo(json.dumps(body, indent=2))


@wallet.command()
@click.option("--limit", default=20, type=int)
def activity(limit: int) -> None:
    """Recent wallet activity."""
    config = _read_config()
    with _client(config) as http:
        try:
            r = http.get(f"/wallets/{_username(config)}/activity", params={"limit": limit})
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            click.secho(_frames_error_message(exc), fg="red", err=True)
            sys.exit(1)
    click.echo(json.dumps(r.json(), indent=2))


@wallet.command()
def stats() -> None:
    """Aggregate spend, rank, streak."""
    config = _read_config()
    with _client(config) as http:
        try:
            r = http.get(f"/wallets/{_username(config)}/stats")
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            click.secho(_frames_error_message(exc), fg="red", err=True)
            sys.exit(1)
    click.echo(json.dumps(r.json(), indent=2))


@wallet.command()
@click.option("--show", is_flag=True, default=False, help="Show current policy")
@click.option("--max-per-tx", type=float, default=None, help="Update max_per_tx_usd")
@click.option("--allow-chains", type=str, default=None, help="Comma-separated chains")
def policy(show: bool, max_per_tx: float | None, allow_chains: str | None) -> None:
    """Show or update spending policy."""
    config = _read_config()
    path = f"/wallets/{_username(config)}/policy"
    with _client(config) as http:
        try:
            if max_per_tx is None and allow_chains is None:
                r = http.get(path)
            else:
                update: dict[str, Any] = {}
                if max_per_tx is not None:
                    update["max_per_tx_usd"] = max_per_tx
                if allow_chains is not None:
                    update["allow_chains"] = [
                        c.strip() for c in allow_chains.split(",") if c.strip()
                    ]
                r = http.patch(path, json=update)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            click.secho(_frames_error_message(exc), fg="red", err=True)
            sys.exit(1)
    click.echo(json.dumps(r.json(), indent=2))


@wallet.command(name="pay")
@click.argument("url")
@click.option("--method", default="POST")
@click.option("--body", default=None, help="JSON body for the target request")
@click.option("--max-payment", default=None, help="Max USD willing to pay (e.g. '0.05')")
def pay(url: str, method: str, body: str | None, max_payment: str | None) -> None:
    """Generic x402 fetch. Frames.ag handles 402 detection, signing, and retry."""
    config = _read_config()
    payload: dict[str, Any] = {"url": url, "method": method}
    if body:
        try:
            payload["body"] = json.loads(body)
        except json.JSONDecodeError as exc:
            click.secho(f"--body is not valid JSON: {exc}", fg="red", err=True)
            sys.exit(1)
    if max_payment:
        payload["maxPayment"] = max_payment

    with _client(config) as http:
        try:
            r = http.post(
                f"/wallets/{_username(config)}/actions/x402/fetch",
                json=payload,
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            click.secho(_frames_error_message(exc), fg="red", err=True)
            sys.exit(1)
    click.echo(json.dumps(r.json(), indent=2))


@wallet.command(name="doctor")
def wallet_doctor() -> None:
    """Verify credentials cached, frames.ag reachable, balance > 0."""
    if not CONFIG_PATH.exists():
        click.secho(f"❌ no credentials at {CONFIG_PATH}", fg="red", err=True)
        click.echo("   run `gecko-mcp wallet new`")
        sys.exit(1)
    click.secho(f"✅ credentials cached at {CONFIG_PATH}", fg="green")

    config = _read_config()
    with _client(config) as http:
        try:
            r = http.get(f"/wallets/{_username(config)}/balances")
            r.raise_for_status()
        except httpx.HTTPError as exc:
            click.secho(f"❌ frames.ag unreachable: {exc}", fg="red", err=True)
            sys.exit(1)
    click.secho("✅ frames.ag reachable", fg="green")

    body = r.json()
    click.echo(f"   balances: {json.dumps(body)}")


# ---------------------------------------------------------------------------
# Programmatic access (used by gecko-mcp.api_client / server)
# ---------------------------------------------------------------------------


class FramesAGWallet:
    """Async facade used by gecko-mcp's API client when it needs to sign x402 calls."""

    def __init__(self) -> None:
        self.config = _read_config()
        self._http = self._build_client()

    def _build_client(self) -> httpx.AsyncClient:
        """Build a fresh httpx client tuned for frames.ag's edge.

        Why these settings:
        - ``max_keepalive_connections=0`` — gecko-mcp serve is a long-lived
          subprocess. frames.ag is fronted by Vercel; idle pooled sockets
          get silently dropped on their side and surface as 502 Bad Gateway
          on the next reuse. Curl and short-lived python -c calls always
          open fresh TCP and never see this. Disabling keepalive costs
          ~150ms per request — a fair price to avoid phantom 502s.
        - ``Connection: close`` — same reason, belt-and-suspenders. Some
          intermediate proxies honor the header even when the client pool
          would otherwise reuse the socket.
        - ``http2=False`` — explicit HTTP/1.1; httpx defaults to HTTP/1.1
          unless installed with the http2 extra, but pinning makes our
          behavior independent of which extras the install picked up.
        - ``User-Agent`` — identifiable in frames.ag's edge logs if we
          need to escalate; default ``python-httpx/...`` is generic enough
          to be lumped with bots.
        """
        return httpx.AsyncClient(
            base_url=FRAMES_BASE,
            headers={
                "Authorization": f"Bearer {self.config['apiToken']}",
                "Connection": "close",
                "User-Agent": "gecko-mcp/0.1 (+https://geckovision.tech)",
                "Accept": "application/json",
            },
            timeout=120.0,
            limits=httpx.Limits(max_keepalive_connections=0, max_connections=10),
            http2=False,
        )

    @property
    def username(self) -> str:
        return str(self.config["username"])

    @property
    def solana_address(self) -> str:
        return str(self.config.get("solanaAddress", ""))

    @property
    def evm_address(self) -> str:
        return str(self.config.get("evmAddress", ""))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def balance(self) -> dict[str, Any]:
        r = await self._http.get(f"/wallets/{self.username}/balances")
        r.raise_for_status()
        return r.json()  # type: ignore[no-any-return]

    async def x402_fetch(
        self,
        url: str,
        method: str = "POST",
        body: dict[str, Any] | None = None,
        max_payment_usd: str | None = None,
        *,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        """One-step x402 — frames.ag handles 402 detection, signing, retry.

        Retries up to ``max_attempts`` on 502/503/504 from frames.ag's gateway
        with linear backoff (1s, 2s). On each 5xx, the underlying httpx
        client is closed and rebuilt — otherwise a stale-pool 502 just
        reuses the same poisoned socket on the retry. 4xx errors raise
        immediately (don't waste backoff on auth failures).
        """
        import asyncio as _asyncio

        payload: dict[str, Any] = {"url": url, "method": method}
        if body is not None:
            payload["body"] = body
        if max_payment_usd is not None:
            payload["maxPayment"] = max_payment_usd

        last_status: int = 0
        for attempt in range(max_attempts):
            r = await self._http.post(
                f"/wallets/{self.username}/actions/x402/fetch",
                json=payload,
            )
            if r.status_code < 500 or r.status_code >= 600:
                # 2xx/3xx/4xx — raise_for_status will distinguish.
                r.raise_for_status()
                return r.json()  # type: ignore[no-any-return]

            last_status = r.status_code
            if attempt + 1 < max_attempts:
                # Tear down the client so the retry uses a fresh TCP+TLS
                # handshake. Without this, a stale-keepalive 502 cascades
                # across all retries.
                await self._http.aclose()
                self._http = self._build_client()
                await _asyncio.sleep(attempt + 1)

        # Exhausted retries — raise the most recent response so the caller
        # sees the body (helps debug what frames.ag's gateway is saying).
        r.raise_for_status()  # always raises since 5xx
        raise RuntimeError(f"unreachable — frames.ag returned {last_status}")
