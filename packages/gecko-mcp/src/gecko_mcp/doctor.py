"""`gecko-mcp doctor` — pre-flight checks for env, connectivity, and migrations.

Exit code is the contract: 0 means every check passed, 1 means one or more
failed. The output is a human-readable table; the exit code is what scripts
key off.

Security: never echo the *value* of any secret. We only ever print the env
var *name* when it's missing or report a redacted boolean.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

# Thin-client installs (gecko-mcp pointing at a remote gecko-api) require
# ZERO server-side keys.  REQUIRED_ENV_VARS is intentionally empty so that
# `gecko-mcp doctor` passes for a user who only has GECKO_API_URL + payment
# env vars in their .mcp.json.
REQUIRED_ENV_VARS: tuple[str, ...] = ()

# Keys that are ONLY needed when running the full server stack locally
# (gecko-api / bb CLI with GECKO_API_URL unset or pointing at localhost).
# Doctor surfaces these as INFO for remote installs so the user is aware of
# what the server needs — but never fails their install because of them.
SERVER_SIDE_ENV_VARS: tuple[str, ...] = (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "TAVILY_API_KEY",
)

# Optional env vars — surfaced in `doctor` output as INFO; never fail the run.
# - DEEPGRAM_API_KEY: YouTube caption-fallback path
# - OPENAI_API_KEY: only consulted in the v2 fallback (direct OpenAI). v3
#   default is ClawRouter via GECKO_LLM_ENDPOINT and the literal "x402" key.
# - GECKO_LLM_ENDPOINT: defaults to http://localhost:8402/v1 (ClawRouter)
OPTIONAL_ENV_VARS: tuple[str, ...] = (
    "DEEPGRAM_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HELIUS_API_KEY",
    "GECKO_LLM_ENDPOINT",
)

_OPTIONAL_HINTS: dict[str, str] = {
    "DEEPGRAM_API_KEY": "YouTube caption-fallback disabled",
    "OPENAI_API_KEY": "Also required with SUPABASE_URL when EMBED_PROVIDER=voyage "
    "(Postgres ANN: embed_for_postgres_vector). Otherwise v2 fallback / OpenAI routes.",
    "ANTHROPIC_API_KEY": "eval-harness Anthropic calls; not required for verdict path",
    "HELIUS_API_KEY": "trade-agent hotpath (Solana RPC) — required for trader mode (v0.2)",
    "GECKO_LLM_ENDPOINT": "defaults to http://localhost:8402/v1 (ClawRouter)",
}

_SERVER_SIDE_HINTS: dict[str, str] = {
    "SUPABASE_URL": "server-side only; not needed for remote gecko-api installs",
    "SUPABASE_SERVICE_ROLE_KEY": "server-side only; not needed for remote gecko-api installs",
    "TAVILY_API_KEY": "server-side only; not needed for remote gecko-api installs",
}

REQUIRED_TABLES: tuple[str, ...] = ("sessions", "sources", "chunks")
REQUIRED_FUNCTIONS: tuple[str, ...] = ("match_chunks",)
REQUIRED_EXTENSIONS: tuple[str, ...] = ("vector",)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    # `info=True` marks an informational result that renders as `[INFO]` and
    # never affects the exit code. ok must also be True for INFO results.
    info: bool = False


class _SupabaseLike(Protocol):
    """Minimal surface we need from the supabase client (for testability)."""

    def rpc(self, fn: str, params: dict[str, object]) -> object: ...


def check_env(environ: dict[str, str] | None = None) -> list[CheckResult]:
    """Verify every required env var is set (non-empty).

    REQUIRED_ENV_VARS is empty for thin-client installs — no server-side
    keys are needed when gecko-mcp points at a remote gecko-api.
    """
    env = environ if environ is not None else dict(os.environ)
    results: list[CheckResult] = []
    for var in REQUIRED_ENV_VARS:
        present = bool(env.get(var))
        results.append(
            CheckResult(
                name=f"env:{var}",
                ok=present,
                detail="set" if present else f"missing env var: {var}",
            )
        )
    return results


def check_server_side_env(environ: dict[str, str] | None = None) -> list[CheckResult]:
    """Surface server-side env vars as INFO rows.

    These keys (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, TAVILY_API_KEY) are
    only required when running gecko-api / bb CLI locally.  For remote
    thin-client installs they are irrelevant — we surface them as INFO so the
    operator can see the state without the doctor failing their install.

    When the vars ARE set (local-dev or server operator running doctor), we
    promote them to PASS rows so the server-side operator gets green feedback.
    """
    env = environ if environ is not None else dict(os.environ)
    results: list[CheckResult] = []
    for var in SERVER_SIDE_ENV_VARS:
        present = bool(env.get(var))
        hint = _SERVER_SIDE_HINTS.get(var, "server-side only")
        results.append(
            CheckResult(
                name=f"env:{var}",
                ok=True,  # never fails the thin-client doctor
                detail="set" if present else f"unset ({hint})",
                info=not present,  # INFO only when absent; PASS when set
            )
        )
    return results


def check_optional_env(environ: dict[str, str] | None = None) -> list[CheckResult]:
    """Report optional env vars as INFO; never fails the doctor."""
    env = environ if environ is not None else dict(os.environ)
    results: list[CheckResult] = []
    for var in OPTIONAL_ENV_VARS:
        present = bool(env.get(var))
        if present:
            detail = "set"
        else:
            hint = _OPTIONAL_HINTS.get(var, "not configured")
            detail = f"unset ({hint})"
        results.append(CheckResult(name=f"env:{var}", ok=True, detail=detail, info=True))
    return results


def check_wallet(wallet_path: object | None = None) -> list[CheckResult]:
    """Report wallet provider, address, and balance as INFO rows. Never fails.

    Respects GECKO_WALLET_PROVIDER (or ~/.gecko/config.toml): reports frames.ag
    or self-custody accordingly. apiToken is never logged.
    """
    import json
    from pathlib import Path

    from gecko_mcp.wallet import _wallet_provider

    provider = _wallet_provider()
    results: list[CheckResult] = [
        CheckResult(name="payments:provider", ok=True, detail=provider, info=True)
    ]

    if provider == "self":
        from gecko_mcp.wallet_self_custody import (
            DEFAULT_RPC_URL,
            WALLET_PATH,
            _fetch_usdc_balance,
            _resolve_usdc_mint,
        )

        if not WALLET_PATH.exists():
            results.append(
                CheckResult(
                    name="payments:wallet",
                    ok=True,
                    detail=f"no self-custody wallet at {WALLET_PATH} — run `gecko-mcp wallet new`",
                    info=True,
                )
            )
            return results

        try:
            payload = json.loads(WALLET_PATH.read_text())
            pubkey = str(payload.get("public_key", "<missing>"))
        except Exception as exc:  # pragma: no cover
            results.append(
                CheckResult(
                    name="payments:wallet", ok=True, detail=f"parse error: {exc}", info=True
                )
            )
            return results

        results.append(CheckResult(name="payments:address", ok=True, detail=pubkey, info=True))

        rpc_url = os.environ.get("SOLANA_RPC_URL", DEFAULT_RPC_URL)
        try:
            mint = _resolve_usdc_mint(rpc_url)
            balance = _fetch_usdc_balance(pubkey, rpc_url, mint)
            cluster = "mainnet" if "mainnet" in rpc_url.lower() else "devnet"
            results.append(
                CheckResult(
                    name="payments:balance",
                    ok=True,
                    detail=f"{balance:.2f} USDC ({cluster})",
                    info=True,
                )
            )
        except Exception as exc:
            results.append(
                CheckResult(
                    name="payments:balance",
                    ok=True,
                    detail=f"could not fetch balance: {exc}",
                    info=True,
                )
            )
        return results

    # frames.ag path
    if wallet_path is None:
        from gecko_mcp.wallet import CONFIG_PATH as _cp

        path = _cp
    else:
        path = wallet_path if isinstance(wallet_path, Path) else Path(str(wallet_path))

    exists = path.exists()
    results.append(
        CheckResult(
            name="payments:wallet",
            ok=True,
            detail=(
                f"frames.ag config at {path}"
                if exists
                else "no frames.ag config (run `gecko-mcp wallet new` or read https://frames.ag/skill.md)"
            ),
            info=True,
        )
    )
    if exists:
        try:
            payload = json.loads(path.read_text())
            username = str(payload.get("username", "<missing>"))
            sol = str(payload.get("solanaAddress", "<missing>"))
            evm = str(payload.get("evmAddress", "<missing>"))
        except Exception as exc:  # pragma: no cover - filesystem corner case
            results.append(
                CheckResult(
                    name="payments:address",
                    ok=True,
                    detail=f"could not parse config: {exc}",
                    info=True,
                )
            )
        else:
            results.append(
                CheckResult(name="payments:username", ok=True, detail=f"@{username}", info=True)
            )
            results.append(CheckResult(name="payments:solana", ok=True, detail=sol, info=True))
            results.append(CheckResult(name="payments:evm", ok=True, detail=evm, info=True))
    return results


def check_llm_router(environ: dict[str, str] | None = None) -> list[CheckResult]:
    """Surface the active LLM_ROUTER + per-agent model matrix as INFO lines.

    Reads the matrix from ``gecko_core.orchestration.pro.router`` so doctor
    output stays in lockstep with what AG2 actually runs at request time.
    Defaults to ``openai`` when LLM_ROUTER is unset — same behavior as the
    runtime resolver. Never fails the doctor (purely informational).
    """
    env = environ if environ is not None else dict(os.environ)
    raw = (env.get("LLM_ROUTER") or "openrouter").strip().lower()
    results: list[CheckResult] = [CheckResult(name="llm:router", ok=True, detail=raw, info=True)]
    try:
        from gecko_core.orchestration.pro.router import model_matrix

        if raw not in {"openai", "openrouter", "clawrouter"}:
            results.append(
                CheckResult(
                    name="llm:matrix",
                    ok=True,
                    detail=f"unknown router {raw!r}; runtime will raise at startup",
                    info=True,
                )
            )
            return results
        matrix = model_matrix(raw)  # type: ignore[arg-type]
        # Render in stable agent order so the line is grep-able.
        order = ("analyst", "critic", "architect", "scoper", "judge")
        rendered = ", ".join(f"{a}->{matrix[a]}" for a in order if a in matrix)
        results.append(CheckResult(name="llm:matrix", ok=True, detail=rendered, info=True))
    except Exception as exc:  # pragma: no cover — defensive
        results.append(
            CheckResult(
                name="llm:matrix",
                ok=True,
                detail=f"could not load matrix: {exc.__class__.__name__}",
                info=True,
            )
        )
    return results


def check_clawrouter(environ: dict[str, str] | None = None) -> CheckResult:
    """Probe the local ClawRouter proxy. INFO if unreachable (the proxy is
    user-managed and may not be running yet).

    The probe always targets ClawRouter's *own* URL — never the value of
    ``GECKO_LLM_ENDPOINT``. The legacy plane lets an operator point
    ``GECKO_LLM_ENDPOINT`` at OpenAI direct (or any OpenAI-compatible
    endpoint); when that happens, the previous implementation rendered
    ``llm:clawrouter unreachable at https://api.openai.com/v1`` which is
    a misleading row — OpenAI is not clawrouter. Each LLM-router probe
    must check its own configured base URL.
    """
    import httpx

    from gecko_mcp.llm import CLAWROUTER_DEFAULT_URL

    env = environ if environ is not None else dict(os.environ)
    # Honor an explicit ``CLAWROUTER_URL`` override (matches the resolver
    # in ``gecko_core.orchestration.pro.router``). Fall back to the
    # well-known clawrouter localhost. ``GECKO_LLM_ENDPOINT`` is the
    # legacy plane and may point anywhere — never trust it here.
    url = env.get("CLAWROUTER_URL", CLAWROUTER_DEFAULT_URL)
    try:
        response = httpx.get(f"{url}/models", timeout=3.0)
        response.raise_for_status()
    except Exception as exc:
        return CheckResult(
            name="llm:clawrouter",
            ok=True,
            detail=(
                f"unreachable at {url} ({exc.__class__.__name__}); "
                "run `gecko-mcp llm install` then `gecko-mcp llm start`"
            ),
            info=True,
        )
    body = response.json()
    count = len(body.get("data", []))
    return CheckResult(
        name="llm:clawrouter",
        ok=True,
        detail=f"{count} models available at {url}",
        info=True,
    )


_DEFAULT_GECKO_API_URL = "https://api.geckovision.tech"


def check_gecko_api(environ: dict[str, str] | None = None) -> CheckResult:
    """Probe `<GECKO_API_URL>/healthz` (S25-DOC-01).

    - Unset + thin client: probes the production default; FAIL if unreachable.
    - Unset + local dev: INFO — local dev without GECKO_API_URL is expected.
    - Set + reachable: PASS with healthz payload summary.
    - Set + unreachable: FAIL.
    """
    import httpx

    env = environ if environ is not None else dict(os.environ)
    raw = env.get("GECKO_API_URL", "").strip()
    thin = _is_thin_client(env)

    if not raw:
        if not thin:
            # Local dev: GECKO_API_URL unset is normal — just inform.
            return CheckResult(
                name="env:GECKO_API_URL",
                ok=True,
                detail=f"unset, MCP will default to {_DEFAULT_GECKO_API_URL}",
                info=True,
            )
        # Thin client whose GECKO_API_URL is somehow unset — probe the default.
        target = _DEFAULT_GECKO_API_URL
        is_default = True
    else:
        target = raw
        is_default = False

    url = target.rstrip("/") + "/healthz"
    try:
        response = httpx.get(url, timeout=5.0)
    except Exception as exc:
        return CheckResult(
            name="gecko_api:healthz",
            ok=False,
            detail=f"gecko-api unreachable — check network ({target}: {exc.__class__.__name__})",
        )
    if response.status_code != 200:
        return CheckResult(
            name="gecko_api:healthz",
            ok=False,
            detail=f"gecko-api returned status {response.status_code} from {target}",
        )
    try:
        body = response.json()
        detail = f"reachable ({target}) status={body.get('status', 'ok')} payments={body.get('payments', '?')}"
    except Exception:
        detail = f"reachable ({target})"
    if is_default:
        detail += " (default URL — set GECKO_API_URL to override)"
    return CheckResult(name="gecko_api:healthz", ok=True, detail=detail)


def _run_claude_mcp_list() -> str:
    """Run `claude mcp list` and return stdout. Raises on failure."""
    import subprocess

    result = subprocess.run(
        ["claude", "mcp", "list"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout


def check_mcp_registered() -> CheckResult:
    """Check whether gecko is registered with Claude Code (S25-DOC-04).

    Runs `claude mcp list` and greps for 'gecko'. Returns:
    - PASS: registered
    - INFO: claude CLI not found (expected in non-Claude-Code environments)
    - INFO: gecko not in list — actionable hint to register
    """
    import shutil

    if not shutil.which("claude"):
        return CheckResult(
            name="mcp:registered",
            ok=True,
            detail="claude CLI not found — run `claude mcp add gecko -- gecko-mcp serve` after install",
            info=True,
        )
    try:
        stdout = _run_claude_mcp_list()
    except Exception as exc:
        return CheckResult(
            name="mcp:registered",
            ok=True,
            detail=f"could not run `claude mcp list`: {exc.__class__.__name__}",
            info=True,
        )
    if "gecko" in stdout.lower():
        return CheckResult(
            name="mcp:registered", ok=True, detail="gecko registered with Claude Code"
        )
    return CheckResult(
        name="mcp:registered",
        ok=True,
        detail="gecko not registered — run: claude mcp add gecko -- gecko-mcp serve",
        info=True,
    )


def check_x402_mode(environ: dict[str, str] | None = None) -> CheckResult:
    """Validate X402 config — read facilitator + supported networks via the seam.

    Sprint 14 S14-PAY-MIGRATE-03: instead of validating ``X402_MODE``
    against a hardcoded literal here, we instantiate the resolved
    :class:`gecko_core.payments.X402Client` and read
    ``client.facilitator_id`` + ``client.supported_networks`` from the
    Protocol. The MCP tool surface no longer hardcodes the accepted
    mode set — adding a new mode (e.g. ``cdp``) is automatically
    reflected here.
    """
    env = environ if environ is not None else dict(os.environ)
    raw = env.get("X402_MODE")
    mode = raw or "stub"

    try:
        from gecko_core.payments import resolve_client

        client = resolve_client(network_id=env.get("X402_NETWORK"), mode=mode)
    except NotImplementedError as exc:
        # S15 reserved slot — surfaced as a warning (ok=True) with the
        # message verbatim so an operator pre-staging the env sees the
        # pointer instead of a fail.
        return CheckResult(
            name="env:X402_MODE",
            ok=True,
            detail=f"mode={mode} reserved: {exc}",
        )
    except Exception as exc:
        # Unknown mode / network. The factory's error message names the
        # offending value and the accepted set — pass through verbatim.
        return CheckResult(
            name="env:X402_MODE",
            ok=False,
            detail=f"{exc.__class__.__name__}: {exc}",
        )

    detail = f"mode={mode} facilitator={client.facilitator_id}"
    if client.supported_networks:
        detail += f" networks=[{', '.join(client.supported_networks)}]"
    if not raw:
        detail += " (X402_MODE unset, defaulting to 'stub')"

    # S20-X402-VERDICT-SETTLE-01 (#11) — verdict-paywall live flag is
    # gated independently of X402_MODE. Surface the resolved value here
    # so an operator inspecting "is the verdict paywall live?" can read
    # both gates from one doctor line.
    try:
        from gecko_core.payments.verdict_settle import (
            VERDICT_SETTLE_LIVE_ENV,
            is_verdict_settle_live_enabled,
            resolve_verdict_settle_mode,
        )
    except Exception:
        # Optional surface — never fail the doctor on a missing import.
        pass
    else:
        verdict_live = is_verdict_settle_live_enabled(env)
        verdict_mode = resolve_verdict_settle_mode(
            x402_mode=mode,
            live_flag=verdict_live,
        )
        detail += (
            f" verdict_paywall_mode={verdict_mode} "
            f"({VERDICT_SETTLE_LIVE_ENV}={'1' if verdict_live else '0'})"
        )
    return CheckResult(name="env:X402_MODE", ok=True, detail=detail)


def check_credit_signing_key(environ: dict[str, str] | None = None) -> CheckResult:
    """S20-B4 — sanity-check ``GECKO_CREDIT_SIGNING_KEY``.

    * If the credit-pack mint surface is implicitly active
      (``GECKO_SKILLS_DISPATCH_ENABLED`` true) and the signing key is
      UNSET → FAIL: nobody can mint a credit pack.
    * If the key IS set but base64-decodes to anything other than 32
      bytes → FAIL: the env var is corrupt.
    * Otherwise INFO with a redacted prefix.

    The key is never echoed; the detail row only shows the byte length
    and a one-byte fingerprint sourced from the *public* key (safe to
    surface — it's the verifier identity).
    """
    env = environ if environ is not None else dict(os.environ)
    skills_on = env.get("GECKO_SKILLS_DISPATCH_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    raw = env.get("GECKO_CREDIT_SIGNING_KEY", "").strip()

    if not raw:
        if skills_on:
            return CheckResult(
                name="payments:credit_signing_key",
                ok=False,
                detail=(
                    "GECKO_CREDIT_SIGNING_KEY unset but "
                    "GECKO_SKILLS_DISPATCH_ENABLED=true — credit-pack mint "
                    "will fail"
                ),
            )
        return CheckResult(
            name="payments:credit_signing_key",
            ok=True,
            detail="unset (skills dispatch off; not required)",
            info=True,
        )

    # Validate base64 + length without instantiating crypto types — keep
    # doctor light and avoid pulling cryptography on thin clients that
    # don't have it. A clean import path lives behind the gecko_core
    # surface; we only care about shape here.
    import base64

    decoded: bytes | None = None
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            pad = "=" * (-len(raw) % 4)
            decoded = decoder(raw + pad)  # type: ignore[operator]
            break
        except Exception:
            continue
    if decoded is None:
        return CheckResult(
            name="payments:credit_signing_key",
            ok=False,
            detail="GECKO_CREDIT_SIGNING_KEY is not valid base64",
        )
    if len(decoded) != 32:
        return CheckResult(
            name="payments:credit_signing_key",
            ok=False,
            detail=(
                f"GECKO_CREDIT_SIGNING_KEY decoded to {len(decoded)} bytes; "
                "expected 32 (Ed25519 raw seed)"
            ),
        )
    return CheckResult(
        name="payments:credit_signing_key",
        ok=True,
        detail="set (32 bytes, Ed25519)",
    )


def check_supabase(client: _SupabaseLike) -> list[CheckResult]:
    """Probe Supabase: connectivity, required extensions, tables, functions.

    All probes go through a single `gecko_doctor_probe` Postgres function
    that returns a JSON manifest. If that function isn't installed yet we
    fall back to per-check queries. We catch broad exceptions because the
    supabase client raises a wide variety of network/HTTP errors.
    """
    results: list[CheckResult] = []
    try:
        # Lightweight connectivity check via a Postgres `select 1`-equivalent.
        # supabase-py's RPC surface gives us a single round-trip.
        client.rpc("gecko_doctor_ping", {})
        results.append(CheckResult(name="supabase:connect", ok=True, detail="reachable"))
    except Exception as exc:  # pragma: no cover - exercised in integration
        # If the RPC function isn't deployed, fall back to a table existence
        # check, which the underlying PostgREST will short-circuit.
        msg = _redact(str(exc))
        if "gecko_doctor_ping" in msg or "Could not find the function" in msg:
            results.append(CheckResult(name="supabase:connect", ok=True, detail="reachable"))
        else:
            results.append(CheckResult(name="supabase:connect", ok=False, detail=msg))
            return results

    # Schema introspection. We rely on a doctor-RPC that returns a manifest
    # of {extensions:[...], tables:[...], functions:[...]}. If absent, mark
    # those checks as warnings (ok=True) so doctor still passes for the
    # default "env-only" smoke test.
    try:
        manifest_result = client.rpc("gecko_doctor_manifest", {})
        manifest = _coerce_manifest(manifest_result)
    except Exception as exc:
        msg = _redact(str(exc))
        results.append(
            CheckResult(
                name="supabase:schema",
                ok=False,
                detail=f"could not introspect schema: {msg}",
            )
        )
        return results

    extensions = set(manifest.get("extensions", []))
    tables = set(manifest.get("tables", []))
    functions = set(manifest.get("functions", []))

    for ext in REQUIRED_EXTENSIONS:
        ok = ext in extensions
        results.append(
            CheckResult(
                name=f"pg_extension:{ext}",
                ok=ok,
                detail="installed" if ok else f"missing extension: {ext}",
            )
        )
    for tbl in REQUIRED_TABLES:
        ok = tbl in tables
        results.append(
            CheckResult(
                name=f"pg_table:{tbl}",
                ok=ok,
                detail="present" if ok else f"missing table: {tbl}",
            )
        )
    for fn in REQUIRED_FUNCTIONS:
        ok = fn in functions
        results.append(
            CheckResult(
                name=f"pg_function:{fn}",
                ok=ok,
                detail="present" if ok else f"missing function: {fn}",
            )
        )
    return results


def _coerce_manifest(raw: object) -> dict[str, list[str]]:
    """Best-effort: supabase-py `rpc(...).execute()` returns an APIResponse.
    Tests pass a plain dict; the live client returns an object with `.data`.
    """
    data: object = raw
    if hasattr(raw, "data"):
        data = raw.data
    if hasattr(data, "execute"):
        # Some versions return a builder until execute() is called.
        data = data.execute().data
    if isinstance(data, dict):
        out: dict[str, list[str]] = {}
        for key in ("extensions", "tables", "functions"):
            v = data.get(key, [])
            out[key] = [str(x) for x in v] if isinstance(v, list) else []
        return out
    return {"extensions": [], "tables": [], "functions": []}


def _redact(s: str) -> str:
    """Strip anything that looks like a key out of a string. Defensive only —
    we never want a secret in stderr even by accident."""
    out = s
    for var in REQUIRED_ENV_VARS:
        val = os.environ.get(var)
        if val and len(val) > 6 and val in out:
            out = out.replace(val, f"<{var}>")
    return out


def format_results(results: Iterable[CheckResult]) -> str:
    """Render the check table. Stable alignment so it's grep-able."""
    rows = list(results)
    if not rows:
        return "(no checks ran)"
    name_w = max(len(r.name) for r in rows)
    lines: list[str] = []
    for r in rows:
        marker = "INFO" if r.info else ("PASS" if r.ok else "FAIL")
        lines.append(f"  [{marker}] {r.name.ljust(name_w)}  {r.detail}")
    return "\n".join(lines)


def _extract_vector_index_filters(idx_def: dict[str, Any]) -> set[str]:
    """Pull the set of ``filter``-typed paths out of a live Atlas index def.

    Atlas returns either ``latestDefinition.fields`` (newer) or
    ``definition.fields`` (older). Each ``fields`` entry is a dict; a
    filterable entry has ``type=="filter"`` and a ``path`` string.
    Returns empty set when no filter fields are declared.
    """
    for key in ("latestDefinition", "definition"):
        defn = idx_def.get(key)
        if not isinstance(defn, dict):
            continue
        fields = defn.get("fields")
        if not isinstance(fields, list):
            continue
        out: set[str] = set()
        for f in fields:
            if isinstance(f, dict) and f.get("type") == "filter":
                p = f.get("path")
                if isinstance(p, str):
                    out.add(p)
        return out
    return set()


def _extract_vector_index_dim(idx_def: dict[str, Any]) -> tuple[int | None, str | None]:
    """Pull `numDimensions` + `similarity` out of an Atlas Search index def.

    Atlas returns either ``latestDefinition.fields`` (newer) or
    ``definition.fields`` (older). Each `fields` entry is a dict; the
    vector entry has ``type=="vector"`` and a ``numDimensions`` int.
    Returns (None, None) if the shape doesn't match — caller surfaces a
    parse-failure row.
    """
    for key in ("latestDefinition", "definition"):
        defn = idx_def.get(key)
        if not isinstance(defn, dict):
            continue
        fields = defn.get("fields")
        if not isinstance(fields, list):
            continue
        for f in fields:
            if not isinstance(f, dict):
                continue
            if f.get("type") == "vector" and isinstance(f.get("numDimensions"), int):
                sim = f.get("similarity") if isinstance(f.get("similarity"), str) else None
                return int(f["numDimensions"]), sim
    return None, None


def check_chunk_store(environ: dict[str, str] | None = None) -> list[CheckResult]:
    """S18-MONGO-CUTOVER-01 — verify chunk-store backend is reachable.

    When ``GECKO_CHUNK_STORE=mongo``: probe the Atlas connection via the
    motor client, list the ``gecko_rag.chunks`` indexes, and assert
    ``chunks_vector`` + ``chunks_text`` are present and queryable.

    When ``GECKO_CHUNK_STORE=supabase`` (default): emit one INFO row.
    The actual Supabase chunk-table check lives in ``check_supabase``.
    """
    env = environ if environ is not None else dict(os.environ)
    results: list[CheckResult] = []
    kind = env.get("GECKO_CHUNK_STORE", "supabase").lower()
    results.append(CheckResult(name="chunk_store:kind", ok=True, detail=kind, info=True))
    if kind != "mongo":
        return results

    uri = env.get("MONGODB_URI") or env.get("MONGO_URI")
    if not uri or uri == "__unset__":
        results.append(
            CheckResult(
                name="chunk_store:mongo:uri",
                ok=False,
                detail="GECKO_CHUNK_STORE=mongo but MONGODB_URI unset",
            )
        )
        return results

    try:
        from pymongo import MongoClient

        db_name = env.get("MONGODB_CHUNK_DB", "gecko_rag")
        # Sync probe — doctor is run rarely and the seam is simpler.
        client: MongoClient[dict[str, Any]] = MongoClient(uri, serverSelectionTimeoutMS=3000)
        client.admin.command("ping")
        results.append(CheckResult(name="chunk_store:mongo:ping", ok=True, detail=f"db={db_name}"))

        from gecko_core.db.mongo_chunks import MONGO_VECTOR_DIM_EXPECTED

        idx_defs = list(client[db_name]["chunks"].list_search_indexes())
        idx_by_name = {i["name"]: i for i in idx_defs}
        for required in ("chunks_vector", "chunks_text"):
            present = required in idx_by_name
            results.append(
                CheckResult(
                    name=f"chunk_store:mongo:index:{required}",
                    ok=present,
                    detail="ready" if present else f"missing search index: {required}",
                )
            )

        # S19-MONGO-INDEX-DIM-CHECK-01 — verify the live `chunks_vector` index
        # numDimensions matches the dim our chunk validators enforce. If S18
        # left the index at 1536 (the original plan) but ingest writes Voyage
        # 1024, every insert raises dim_mismatch — surfaced here as a red row
        # before any user-facing crash.
        if "chunks_vector" in idx_by_name:
            vec_def = dict(idx_by_name["chunks_vector"])
            dim, sim = _extract_vector_index_dim(vec_def)
            if dim is None:
                results.append(
                    CheckResult(
                        name="chunk_store:mongo:index:chunks_vector:dim",
                        ok=False,
                        detail="could not parse numDimensions from index definition",
                    )
                )
            elif dim != MONGO_VECTOR_DIM_EXPECTED:
                results.append(
                    CheckResult(
                        name="chunk_store:mongo:index:chunks_vector:dim",
                        ok=False,
                        detail=(
                            f"dim={dim} expected={MONGO_VECTOR_DIM_EXPECTED} "
                            "— Voyage embeddings will be rejected by Atlas"
                        ),
                    )
                )
            else:
                detail = f"dim={dim} ok"
                if sim:
                    detail = f"{detail} similarity={sim}"
                results.append(
                    CheckResult(
                        name="chunk_store:mongo:index:chunks_vector:dim",
                        ok=True,
                        detail=detail,
                    )
                )

        # S20-RAG-02 — verify the live `chunks_vector` index advertises the
        # expected filter-typed paths so $vectorSearch can pre-filter past
        # the ANN stage. Source of truth: gecko_core.db.mongo
        # CHUNKS_VECTOR_FILTER_FIELDS (Pattern A — declared in one place).
        if "chunks_vector" in idx_by_name:
            from gecko_core.db.mongo import CHUNKS_VECTOR_FILTER_FIELDS

            live_filters = _extract_vector_index_filters(dict(idx_by_name["chunks_vector"]))
            expected = set(CHUNKS_VECTOR_FILTER_FIELDS)
            missing = expected - live_filters
            if not live_filters:
                results.append(
                    CheckResult(
                        name="chunk_store:mongo:index:chunks_vector:filters",
                        ok=False,
                        detail=(
                            "no filter fields declared — index is not the S20-RAG-02 "
                            "filterable compound shape; run "
                            "scripts/mongo/s20_rag02_filterable_index.py --apply --rebuild"
                        ),
                    )
                )
            elif missing:
                results.append(
                    CheckResult(
                        name="chunk_store:mongo:index:chunks_vector:filters",
                        ok=False,
                        detail="missing filter: " + ", ".join(sorted(missing)),
                    )
                )
            else:
                results.append(
                    CheckResult(
                        name="chunk_store:mongo:index:chunks_vector:filters",
                        ok=True,
                        detail=", ".join(sorted(expected)) + " all filterable",
                    )
                )

        # S20-A7 — surface the pioneer-cell threshold so operators can see
        # the configured behavior. INFO row, never fails the check.
        from gecko_core.knowledge.pioneer import _effective_threshold

        results.append(
            CheckResult(
                name="chunk_store:mongo:pioneer_threshold",
                ok=True,
                detail=str(_effective_threshold()),
                info=True,
            )
        )
    except Exception as exc:
        results.append(
            CheckResult(
                name="chunk_store:mongo:probe",
                ok=False,
                detail=_redact(str(exc)),
            )
        )
    return results


def check_voyage_api_key(environ: dict[str, str] | None = None) -> list[CheckResult]:
    """S19-VOYAGE-API-KEY-01 — verify Voyage AI key when reranker is enabled.

    When ``GECKO_RERANKER=voyage``: ``VOYAGE_API_KEY`` must be set, non-empty,
    and have the expected ``pa-`` prefix per Voyage SDK docs. We never make a
    real Voyage API call (their preview API has no SLA and we don't want to
    hit it on every doctor run); env presence + plausible-prefix is enough.

    When ``GECKO_RERANKER`` is unset or ``none``: emit a single INFO row and
    skip the API-key check entirely.

    Security: the key is never echoed. We surface a sentinel of the form
    ``pa-...<last4>`` (first 3 + last 4 chars) to confirm not-empty without
    leaking the secret. See CLAUDE.md "NEVER log API keys, even in errors".
    """
    env = environ if environ is not None else dict(os.environ)
    results: list[CheckResult] = []
    kind = (env.get("GECKO_RERANKER") or "none").strip().lower()
    results.append(CheckResult(name="reranker:kind", ok=True, detail=kind, info=True))
    if kind != "voyage":
        return results

    key = env.get("VOYAGE_API_KEY", "")
    if not key:
        results.append(
            CheckResult(
                name="voyage:api_key",
                ok=False,
                detail="GECKO_RERANKER=voyage but VOYAGE_API_KEY is unset",
            )
        )
        return results

    if not key.startswith("pa-"):
        # Plausible-prefix check — Voyage SDK keys start with "pa-". We
        # do NOT echo the actual key in the failure detail.
        results.append(
            CheckResult(
                name="voyage:api_key",
                ok=False,
                detail="VOYAGE_API_KEY does not have expected 'pa-' prefix",
            )
        )
        return results

    # Redact: first 3 chars (the "pa-" prefix) + ellipsis + last 4 chars.
    # Never include any middle slice of the secret.
    suffix = key[-4:] if len(key) >= 8 else "????"
    results.append(
        CheckResult(
            name="voyage:api_key",
            ok=True,
            detail=f"prefix=pa-...{suffix}",
        )
    )
    return results


def check_cohere_api_key(environ: dict[str, str] | None = None) -> list[CheckResult]:
    """S28 #25 — verify Cohere API key for the trade-panel rerank wedge.

    ``COHERE_API_KEY`` is optional — the trade-panel retrieval degrades
    gracefully when unset (falls through to the Path D baseline). When set,
    we surface a redacted prefix sentinel to confirm not-empty without
    leaking the secret. Cohere keys do not have a stable public prefix
    pattern so we accept any non-empty value.

    Security: the key is never echoed. We surface ``<first2>...<last4>``
    only when length permits; otherwise ``set`` only.
    """
    env = environ if environ is not None else dict(os.environ)
    results: list[CheckResult] = []
    key = (env.get("COHERE_API_KEY") or "").strip()
    if not key:
        results.append(
            CheckResult(
                name="cohere:api_key",
                ok=True,
                detail="unset (rerank degrades gracefully — Path D baseline)",
                info=True,
            )
        )
        return results
    if len(key) >= 8:
        sentinel = f"{key[:2]}...{key[-4:]}"
    else:
        sentinel = "set"
    results.append(
        CheckResult(
            name="cohere:api_key",
            ok=True,
            detail=f"prefix={sentinel}",
        )
    )
    return results


VOYAGE_LIVE_TIMEOUT_S: float = 5.0


async def check_voyage_live(
    environ: dict[str, str] | None = None,
) -> list[CheckResult]:
    """S19-VOYAGE-DOCTOR-LIVE-01 — real Voyage embed + rerank pings.

    Only runs when ``run_doctor(live=True)`` is called (CLI: ``--live``).
    Each call is hard-timeouted at 5s and returns a single ``CheckResult``;
    failures surface as red rows with redacted error strings (never echo
    the API key, even via the SDK exception's ``__str__``).

    The embed ping uses the configured ``EMBED_MODEL`` (default
    ``voyage-context-3``) with one short string and asserts the response
    shape is 1×1024 (matching ``MONGO_VECTOR_DIM_EXPECTED``); a dim
    mismatch here means the Voyage account was downgraded to a different
    model and would corrupt Atlas inserts. A 404 / "model not found"
    surfaces as a dedicated red row so operators can tell account-level
    model unavailability apart from network errors.

    Cost guard: ≤ ~10 embed tokens + ~5 rerank tokens per run. Negligible.
    """
    env = environ if environ is not None else dict(os.environ)
    results: list[CheckResult] = []

    embed_provider = (env.get("EMBED_PROVIDER") or "voyage").strip().lower()
    embed_model = (env.get("EMBED_MODEL") or "voyage-context-3").strip()
    reranker = (env.get("GECKO_RERANKER") or "none").strip().lower()
    voyage_key = env.get("VOYAGE_API_KEY", "").strip()

    if not voyage_key:
        # Without a key there's nothing to live-ping. Other doctor checks
        # already surface the missing key as a red row.
        results.append(
            CheckResult(
                name="voyage:live",
                ok=True,
                detail="skipped (VOYAGE_API_KEY unset)",
                info=True,
            )
        )
        return results

    # --- embed live ping -----------------------------------------------------
    if embed_provider == "voyage":
        try:
            import voyageai

            client: Any = voyageai.AsyncClient(api_key=voyage_key)  # type: ignore[attr-defined]
            resp: Any = await asyncio.wait_for(
                client.embed(
                    texts=["doctor ping"],
                    model=embed_model,
                    input_type="query",
                ),
                timeout=VOYAGE_LIVE_TIMEOUT_S,
            )
            embeddings = list(resp.embeddings)
            from gecko_core.db.mongo_chunks import MONGO_VECTOR_DIM_EXPECTED

            if len(embeddings) != 1:
                results.append(
                    CheckResult(
                        name="voyage:embed:live",
                        ok=False,
                        detail=f"expected 1 vector, got {len(embeddings)} model={embed_model}",
                    )
                )
            elif len(embeddings[0]) != MONGO_VECTOR_DIM_EXPECTED:
                results.append(
                    CheckResult(
                        name="voyage:embed:live",
                        ok=False,
                        detail=(
                            f"dim={len(embeddings[0])} expected={MONGO_VECTOR_DIM_EXPECTED} "
                            f"model={embed_model}"
                        ),
                    )
                )
            else:
                results.append(
                    CheckResult(
                        name="voyage:embed:live",
                        ok=True,
                        detail=(
                            f"dim={MONGO_VECTOR_DIM_EXPECTED} "
                            f"model={embed_model} "
                            f"tokens={getattr(resp, 'total_tokens', '?')}"
                        ),
                    )
                )
        except TimeoutError:
            results.append(
                CheckResult(
                    name="voyage:embed:live",
                    ok=False,
                    detail=f"timeout after {VOYAGE_LIVE_TIMEOUT_S}s model={embed_model}",
                )
            )
        except Exception as exc:
            # Specific row when Voyage signals the model is not available
            # to this account (404 / "model not found"). Distinguish from
            # generic network / auth errors so operators know to check
            # their Voyage plan instead of chasing connectivity.
            exc_str = str(exc)
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            lowered = exc_str.lower()
            is_model_unavailable = status == 404 or (
                "model" in lowered and ("not found" in lowered or "not available" in lowered)
            )
            if is_model_unavailable:
                results.append(
                    CheckResult(
                        name="voyage:embed:live",
                        ok=False,
                        detail=(
                            f"FAIL model={embed_model} (model not available — check Voyage account)"
                        ),
                    )
                )
            else:
                results.append(
                    CheckResult(
                        name="voyage:embed:live",
                        ok=False,
                        detail=f"{_redact(exc_str)} model={embed_model}",
                    )
                )

    # --- rerank live ping ----------------------------------------------------
    if reranker == "voyage":
        try:
            import voyageai

            client_r: Any = voyageai.AsyncClient(api_key=voyage_key)  # type: ignore[attr-defined]
            resp = await asyncio.wait_for(
                client_r.rerank(
                    query="ping",
                    documents=["alpha", "beta"],
                    model="rerank-2",
                ),
                timeout=VOYAGE_LIVE_TIMEOUT_S,
            )
            rerank_results = list(getattr(resp, "results", []) or [])
            if len(rerank_results) != 2:
                results.append(
                    CheckResult(
                        name="voyage:rerank:live",
                        ok=False,
                        detail=f"expected 2 results, got {len(rerank_results)}",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        name="voyage:rerank:live",
                        ok=True,
                        detail="2 results returned",
                    )
                )
        except TimeoutError:
            results.append(
                CheckResult(
                    name="voyage:rerank:live",
                    ok=False,
                    detail=f"timeout after {VOYAGE_LIVE_TIMEOUT_S}s",
                )
            )
        except Exception as exc:
            results.append(
                CheckResult(
                    name="voyage:rerank:live",
                    ok=False,
                    detail=_redact(str(exc)),
                )
            )

    return results


def check_openai_live(environ: dict[str, str] | None = None) -> CheckResult:
    """S24 WS-E — cheap OpenAI /models probe under ``--live``.

    Hits ``GET https://api.openai.com/v1/models`` with the configured key.
    Cost: 0 tokens (model list is free). Times out at 5s. Returns a single
    PASS / FAIL row with redacted detail — the key is never echoed.
    """
    import httpx

    env = environ if environ is not None else dict(os.environ)
    key = env.get("OPENAI_API_KEY", "").strip()
    if not key:
        return CheckResult(
            name="openai:live",
            ok=True,
            detail="skipped (OPENAI_API_KEY unset)",
            info=True,
        )
    try:
        resp = httpx.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=5.0,
        )
    except Exception as exc:
        return CheckResult(
            name="openai:live",
            ok=False,
            detail=f"OpenAI unreachable — check network ({exc.__class__.__name__})",
        )
    if resp.status_code == 401:
        return CheckResult(
            name="openai:live",
            ok=False,
            detail="OPENAI_API_KEY rejected (401) — rotate the key in your .env",
        )
    if resp.status_code != 200:
        return CheckResult(
            name="openai:live",
            ok=False,
            detail=f"OpenAI returned status {resp.status_code} — retry in a minute",
        )
    try:
        n = len(resp.json().get("data", []))
    except Exception:
        n = 0
    return CheckResult(name="openai:live", ok=True, detail=f"reachable ({n} models)")


def _is_thin_client(environ: dict[str, str] | None = None) -> bool:
    """True when gecko-mcp is running as a remote client (not as the server itself).

    Remote clients do not need server-side keys (SUPABASE_URL, VOYAGE_API_KEY, etc.)
    — all embedding, storage, and LLM work happens inside gecko-api.

    Detection: GECKO_API_URL is set and does NOT point at localhost/127.0.0.1.
    Local dev (localhost or unset) = not thin client = full server checks run.
    """
    env = environ or dict(os.environ)
    api_url = env.get("GECKO_API_URL", "").strip()
    if not api_url:
        return False  # unset = local dev mode, run full checks
    lowered = api_url.lower()
    return "localhost" not in lowered and "127.0.0.1" not in lowered


def _is_server_stack(environ: dict[str, str]) -> bool:
    """True when the operator has at least one server-side credential set.

    Used by embed-provider and reranker checks to distinguish a thin-client
    external install (no server keys → skip key validation) from a server
    operator running doctor locally (server keys present → validate them).
    """
    return bool(environ.get("SUPABASE_URL")) or bool(environ.get("TAVILY_API_KEY"))


def check_embed_provider(environ: dict[str, str] | None = None) -> list[CheckResult]:
    """S22-VOYAGE-EMBED — verify embedding provider config is self-consistent.

    EMBED_PROVIDER=voyage (default): VOYAGE_API_KEY must be set and have the
    expected ``pa-`` prefix. When ``SUPABASE_URL`` is also set (local server
    stack), ``OPENAI_API_KEY`` is required too — Postgres ANN RPCs use
    ``text-embedding-3-small`` (1536) via ``embed_for_postgres_vector``.

    EMBED_PROVIDER=openai: OPENAI_API_KEY must be set (non-empty). VOYAGE_API_KEY
    is surfaced as INFO.

    Any other value is a hard FAIL — the embedder factory will raise at runtime.

    Thin-client mode: when no server-side credentials are present (the install
    is a remote gecko-api consumer), embed provider key validation is skipped
    — embedding is done server-side and the keys are irrelevant here.

    Security: keys are never echoed. Voyage key shown as ``pa-...<last4>``;
    OpenAI key shown as ``sk-...<last4>``.
    """
    env = environ if environ is not None else dict(os.environ)
    results: list[CheckResult] = []
    provider = (env.get("EMBED_PROVIDER") or "voyage").strip().lower()
    model = env.get("EMBED_MODEL") or (
        "voyage-context-3" if provider == "voyage" else "text-embedding-3-small"
    )
    results.append(
        CheckResult(name="embed:provider", ok=True, detail=f"{provider} model={model}", info=True)
    )

    # Skip key validation for thin-client installs — embedding happens
    # server-side so the provider key is never needed locally.
    if not _is_server_stack(env):
        results.append(
            CheckResult(
                name="embed:key_check",
                ok=True,
                detail="skipped (thin-client install; embedding is server-side)",
                info=True,
            )
        )
        return results

    if provider == "voyage":
        key = env.get("VOYAGE_API_KEY", "")
        if not key:
            results.append(
                CheckResult(
                    name="embed:voyage_api_key",
                    ok=False,
                    detail="EMBED_PROVIDER=voyage but VOYAGE_API_KEY is unset",
                )
            )
            return results
        if not key.startswith("pa-"):
            results.append(
                CheckResult(
                    name="embed:voyage_api_key",
                    ok=False,
                    detail="VOYAGE_API_KEY does not have expected 'pa-' prefix",
                )
            )
            return results
        suffix = key[-4:] if len(key) >= 8 else "????"
        results.append(
            CheckResult(name="embed:voyage_api_key", ok=True, detail=f"prefix=pa-...{suffix}")
        )
        # Supabase pgvector RPCs (precedent / memory / chunk match) always use
        # OpenAI text-embedding-3-small (1536) — see embed_for_postgres_vector.
        if env.get("SUPABASE_URL"):
            oa = env.get("OPENAI_API_KEY", "")
            if not oa:
                results.append(
                    CheckResult(
                        name="embed:openai_for_postgres_ann",
                        ok=False,
                        detail=(
                            "SUPABASE_URL set but OPENAI_API_KEY unset — "
                            "precedent / memory / chunk ANN expects "
                            "text-embedding-3-small (1536)"
                        ),
                    )
                )
            else:
                oa_suffix = oa[-4:] if len(oa) >= 8 else "????"
                results.append(
                    CheckResult(
                        name="embed:openai_for_postgres_ann",
                        ok=True,
                        detail=f"sk-...{oa_suffix}",
                    )
                )
        return results

    elif provider == "openai":
        key = env.get("OPENAI_API_KEY", "")
        if not key:
            results.append(
                CheckResult(
                    name="embed:openai_api_key",
                    ok=False,
                    detail="EMBED_PROVIDER=openai but OPENAI_API_KEY is unset",
                )
            )
            return results
        suffix = key[-4:] if len(key) >= 8 else "????"
        results.append(CheckResult(name="embed:openai_api_key", ok=True, detail=f"sk-...{suffix}"))

    else:
        results.append(
            CheckResult(
                name="embed:provider",
                ok=False,
                detail=f"unknown EMBED_PROVIDER={provider!r}; accepted: voyage, openai",
            )
        )

    return results


def run_doctor(
    *,
    environ: dict[str, str] | None = None,
    supabase_client: _SupabaseLike | None = None,
    live: bool = False,
) -> tuple[int, str]:
    """Run every check. Returns (exit_code, rendered_report).

    Thin-client mode (GECKO_API_URL points at a remote server): only x402 +
    GECKO_API_URL + wallet checks are required; server-side keys surface as
    INFO and never fail the doctor.

    Local-dev / server-operator mode (GECKO_API_URL unset or localhost):
    server-side env vars are surfaced as PASS/INFO and Supabase probes run
    when credentials are present.

    `supabase_client` is injectable for tests. If None and Supabase creds are
    absent, Supabase probes are skipped.

    `live=True` (CLI: ``--live``) issues real Voyage embed + rerank pings to
    detect revoked keys / outages that env-presence checks can't catch.
    """
    env = environ if environ is not None else dict(os.environ)
    thin = _is_thin_client(env)
    results: list[CheckResult] = []
    results.extend(check_env(environ))
    # Server-side keys are only relevant for local-dev / server-operator mode.
    # Thin clients (remote GECKO_API_URL) skip these entirely — the keys live
    # on the server, not on the user's machine.
    if not thin:
        results.extend(check_server_side_env(environ))
    results.append(check_x402_mode(environ))
    results.append(check_credit_signing_key(environ))
    results.append(check_gecko_api(environ))
    wallet_results = check_wallet()
    results.extend(wallet_results)
    if thin:
        # S25-DOC-02: wallet is required in thin-client mode — x402 payments
        # can't sign without it. Fail if no address was surfaced by check_wallet.
        has_address = any(
            r.name in ("payments:solana", "payments:address", "payments:evm")
            for r in wallet_results
        )
        if not has_address:
            results.append(
                CheckResult(
                    name="payments:wallet_required",
                    ok=False,
                    detail="No wallet configured — run: gecko-mcp wallet new",
                )
            )
        # S25-DOC-04: verify gecko is registered with Claude Code.
        results.append(check_mcp_registered())
    if not thin:
        results.extend(check_optional_env(environ))
        results.extend(check_llm_router(environ))
        results.append(check_clawrouter(environ))
        results.extend(check_chunk_store(environ))
        results.extend(check_embed_provider(environ))
        results.extend(check_voyage_api_key(environ))
        results.extend(check_cohere_api_key(environ))
        if live:
            results.extend(asyncio.run(check_voyage_live(environ)))
            results.append(check_openai_live(environ))

    # Supabase probes only run in local-dev/server-operator mode when creds are present.
    # Thin clients (remote GECKO_API_URL) never run Supabase probes — the DB is
    # server-side and the keys are irrelevant on the client machine.
    env_ok = all(r.ok for r in results if not r.info)
    supabase_creds_present = bool(env.get("SUPABASE_URL")) and bool(
        env.get("SUPABASE_SERVICE_ROLE_KEY")
    )
    if not thin and env_ok and supabase_creds_present:
        if supabase_client is not None:
            results.extend(check_supabase(supabase_client))
        else:
            # Build the default client lazily — avoids importing supabase in
            # all-missing-env tests.
            try:
                from gecko_core.db import create_supabase_client

                client = create_supabase_client()
            except Exception as exc:
                results.append(
                    CheckResult(
                        name="supabase:client",
                        ok=False,
                        detail=f"could not build client: {_redact(str(exc))}",
                    )
                )
            else:
                results.extend(check_supabase(client))

    # Only non-INFO failures break exit 0.
    exit_code = 0 if all(r.ok for r in results if not r.info) else 1
    header = "doctor: OK" if exit_code == 0 else "doctor: FAIL"
    return exit_code, f"{header}\n{format_results(results)}"
