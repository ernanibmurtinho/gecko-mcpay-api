"""Wallet provisioning for the gecko-mcp CLI.

Handles two paths from skill.md Step 3:
- `gecko-mcp wallet new`    — generate a fresh keypair, store encrypted
- `gecko-mcp wallet import` — import an existing keypair

The wallet file lives at ~/.gecko/wallet.json and is encrypted with a
machine-derived key + optional user passphrase. It is NEVER logged.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import sys
from pathlib import Path

import click
import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from solders.keypair import Keypair

WALLET_PATH = Path.home() / ".gecko" / "wallet.json"

# USDC SPL token mint addresses. The v2 demo runs on devnet for the hackathon;
# mainnet is here for completeness but devnet is the default per the v2 plan.
USDC_MINT_MAINNET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_MINT_DEVNET = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
DEFAULT_RPC_URL = "https://api.devnet.solana.com"


def _derive_key(passphrase: str) -> bytes:
    """Derive a Fernet key from a passphrase.

    Salt is fixed (per-user not per-wallet) — this is a passphrase, not a primary
    secret. The keypair file itself is the secret. Passphrase is just defense-in-depth.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"gecko-wallet-salt-v1",
        iterations=600_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def _save_keypair(kp: Keypair, passphrase: str) -> None:
    WALLET_PATH.parent.mkdir(parents=True, exist_ok=True)
    fernet = Fernet(_derive_key(passphrase))
    encrypted = fernet.encrypt(bytes(kp))
    payload = {
        "version": 1,
        "public_key": str(kp.pubkey()),
        "encrypted_secret": encrypted.decode(),
    }
    WALLET_PATH.write_text(json.dumps(payload, indent=2))
    WALLET_PATH.chmod(0o600)


def _load_keypair(passphrase: str) -> Keypair:
    if not WALLET_PATH.exists():
        raise FileNotFoundError(f"no wallet at {WALLET_PATH}; run `gecko-mcp wallet new`")
    payload = json.loads(WALLET_PATH.read_text())
    fernet = Fernet(_derive_key(passphrase))
    secret = fernet.decrypt(payload["encrypted_secret"].encode())
    return Keypair.from_bytes(secret)


def _resolve_usdc_mint(rpc_url: str) -> str:
    """Pick the USDC mint that matches the RPC cluster.

    Heuristic-only: defaults to devnet unless the URL explicitly references
    mainnet. The v2 demo is devnet-first.
    """
    lowered = rpc_url.lower()
    if "mainnet" in lowered:
        return USDC_MINT_MAINNET
    return USDC_MINT_DEVNET


def _fetch_usdc_balance(pubkey: str, rpc_url: str, mint: str) -> float:
    """Call Solana RPC `getTokenAccountsByOwner` and sum USDC balances.

    Returns the human-readable USDC amount (raw / 10^6). Returns 0.0 when the
    owner has no token account for the mint yet.
    """
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            pubkey,
            {"mint": mint},
            {"encoding": "jsonParsed"},
        ],
    }
    response = httpx.post(rpc_url, json=request, timeout=10.0)
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(f"RPC error: {body['error']}")
    accounts = body.get("result", {}).get("value", [])
    total_raw = 0
    for account in accounts:
        info = account.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        amount = info.get("tokenAmount", {}).get("amount", "0")
        try:
            total_raw += int(amount)
        except (TypeError, ValueError):
            continue
    # USDC has 6 decimals.
    return total_raw / 1_000_000


@click.group()
def wallet() -> None:
    """Manage the agent wallet that pays for Gecko sessions via x402."""


@wallet.command()
@click.option("--passphrase", default="", help="Optional. Adds defense-in-depth encryption.")
def new(passphrase: str) -> None:
    """Generate a fresh Solana keypair and store it encrypted."""
    if WALLET_PATH.exists():
        click.confirm(
            f"A wallet already exists at {WALLET_PATH}. Overwrite?",
            abort=True,
        )

    if not passphrase:
        passphrase = getpass.getpass("Passphrase (optional, press Enter to skip): ") or "default"

    kp = Keypair()
    _save_keypair(kp, passphrase)

    pubkey_str = str(kp.pubkey())
    rpc_url = os.environ.get("SOLANA_RPC_URL", DEFAULT_RPC_URL)
    deep_link = fund_url(pubkey_str, amount_usdc=5.0, rpc_url=rpc_url)

    click.secho("Created agent wallet", fg="green")
    click.echo(f"Public address: {pubkey_str}")
    click.echo(f"Keypair stored encrypted at {WALLET_PATH}")
    click.echo()
    click.echo("To fund this wallet:")
    click.echo(f"  - Send USDC on Solana directly to {pubkey_str}, OR")
    click.echo(f"  - Open in Phantom (mobile): {deep_link}")
    click.echo("  - Scan the QR code below on your phone to open Phantom:")
    click.echo()
    try:
        click.echo(qr_code_ascii(deep_link))
    except Exception:
        click.echo(f"  (QR unavailable — open {deep_link} manually)")
    click.echo()
    click.secho(
        "  Back up ~/.gecko/wallet.json — losing it means losing access to your funds.",
        fg="yellow",
    )
    if not passphrase or passphrase == "default":
        click.secho(
            "  No passphrase set. Add one with GECKO_WALLET_PASSPHRASE env var for extra security.",
            fg="yellow",
        )


@wallet.command(name="import")
@click.option("--from-file", "from_file", type=click.Path(exists=True, path_type=Path))
@click.option("--passphrase", default="")
def import_(from_file: Path | None, passphrase: str) -> None:
    """Import an existing Solana keypair (e.g. exported from Solflare)."""
    if from_file is None:
        click.echo("Paste your base58-encoded keypair (or use --from-file):")
        secret = getpass.getpass("Keypair: ").strip()
        try:
            kp = Keypair.from_base58_string(secret)
        except Exception as e:
            click.secho(f"Invalid keypair: {e}", fg="red", err=True)
            sys.exit(1)
    else:
        # Solana CLI keypair JSON format: a list of 64 ints
        data = json.loads(from_file.read_text())
        kp = Keypair.from_bytes(bytes(data))

    if not passphrase:
        passphrase = getpass.getpass("Passphrase (optional): ") or "default"

    _save_keypair(kp, passphrase)
    click.secho(f"Imported wallet: {kp.pubkey()}", fg="green")


@wallet.command()
def address() -> None:
    """Print the public address of the agent wallet (safe to share)."""
    if not WALLET_PATH.exists():
        click.secho(f"No wallet at {WALLET_PATH}. Run `gecko-mcp wallet new`.", fg="red", err=True)
        sys.exit(1)
    payload = json.loads(WALLET_PATH.read_text())
    click.echo(payload["public_key"])


@wallet.command()
def balance() -> None:
    """Check the USDC balance of the agent wallet on Solana."""
    if not WALLET_PATH.exists():
        click.secho(f"No wallet at {WALLET_PATH}. Run `gecko-mcp wallet new`.", fg="red", err=True)
        sys.exit(1)
    payload = json.loads(WALLET_PATH.read_text())
    pubkey = payload["public_key"]
    rpc_url = os.environ.get("SOLANA_RPC_URL", DEFAULT_RPC_URL)
    mint = _resolve_usdc_mint(rpc_url)
    try:
        amount = _fetch_usdc_balance(pubkey, rpc_url, mint)
    except Exception as exc:
        click.secho(f"Could not fetch balance: {exc}", fg="red", err=True)
        sys.exit(1)
    cluster = "mainnet" if mint == USDC_MINT_MAINNET else "devnet"
    click.echo(f"{amount:.2f} USDC ({cluster})")


def fund_url(pubkey: str, amount_usdc: float = 5.0, rpc_url: str = DEFAULT_RPC_URL) -> str:
    """Return a Phantom deep link pre-filled for a USDC transfer to this wallet.

    Works on mobile Phantom (opens the transfer sheet directly) and displays
    as a plain https:// URL that Phantom's browser extension also understands.
    """
    mint = _resolve_usdc_mint(rpc_url)
    # SPL Token URI scheme: Phantom opens the "Send" sheet pre-filled.
    amount_str = f"{amount_usdc:.6f}".rstrip("0").rstrip(".")
    return (
        f"solana:{pubkey}"
        f"?amount={amount_str}"
        f"&spl-token={mint}"
        f"&label=Gecko+Agent+Wallet"
        f"&message=Fund+your+Gecko+agent+wallet"
    )


def qr_code_ascii(data: str) -> str:
    """Return a terminal-printable ASCII QR code for *data*.

    Uses qrcode's TerminalOutput which produces a dense block-character grid
    suitable for scanning on a phone screen from a terminal window.
    """
    import io

    import qrcode
    from qrcode.image.terminal import TerminalImage

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=1,
    )
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.StringIO()
    img = qr.make_image(image_factory=TerminalImage)
    img.save(buf)
    return buf.getvalue()


def get_keypair_for_signing(passphrase: str | None = None) -> Keypair:
    """Used by the MCP server when it needs to sign x402 payments.

    Loads from ~/.gecko/wallet.json. Passphrase resolution order:
    1. argument
    2. GECKO_WALLET_PASSPHRASE env var
    3. "default" (no passphrase set)
    """
    if passphrase is None:
        passphrase = os.environ.get("GECKO_WALLET_PASSPHRASE", "default")
    return _load_keypair(passphrase)
