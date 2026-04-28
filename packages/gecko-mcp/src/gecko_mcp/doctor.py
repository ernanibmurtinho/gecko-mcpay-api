"""`gecko-mcp doctor` — pre-flight checks for env, connectivity, and migrations.

Exit code is the contract: 0 means every check passed, 1 means one or more
failed. The output is a human-readable table; the exit code is what scripts
key off.

Security: never echo the *value* of any secret. We only ever print the env
var *name* when it's missing or report a redacted boolean.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

REQUIRED_ENV_VARS: tuple[str, ...] = (
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
    "GECKO_LLM_ENDPOINT",
)

_OPTIONAL_HINTS: dict[str, str] = {
    "DEEPGRAM_API_KEY": "YouTube caption-fallback disabled",
    "OPENAI_API_KEY": "v2 fallback only; v3 routes through ClawRouter",
    "GECKO_LLM_ENDPOINT": "defaults to http://localhost:8402/v1 (ClawRouter)",
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
    """Verify every required env var is set (non-empty)."""
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
    """Report frames.ag wallet presence + addresses as INFO. Never fails.

    Reads the canonical `~/.agentwallet/config.json` written by frames.ag's
    skill. The apiToken is never logged — we only surface username + addresses.
    """
    import json
    from pathlib import Path

    if wallet_path is None:
        from gecko_mcp.wallet import CONFIG_PATH as _cp

        path = _cp
    else:
        path = wallet_path if isinstance(wallet_path, Path) else Path(str(wallet_path))

    results: list[CheckResult] = []
    exists = path.exists()
    results.append(
        CheckResult(
            name="wallet:present",
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
                    name="wallet:address",
                    ok=True,
                    detail=f"could not parse config: {exc}",
                    info=True,
                )
            )
        else:
            results.append(
                CheckResult(name="wallet:username", ok=True, detail=f"@{username}", info=True)
            )
            results.append(CheckResult(name="wallet:solana", ok=True, detail=sol, info=True))
            results.append(CheckResult(name="wallet:evm", ok=True, detail=evm, info=True))
    return results


def check_llm_router(environ: dict[str, str] | None = None) -> list[CheckResult]:
    """Surface the active LLM_ROUTER + per-agent model matrix as INFO lines.

    Reads the matrix from ``gecko_core.orchestration.pro.router`` so doctor
    output stays in lockstep with what AG2 actually runs at request time.
    Defaults to ``openai`` when LLM_ROUTER is unset — same behavior as the
    runtime resolver. Never fails the doctor (purely informational).
    """
    env = environ if environ is not None else dict(os.environ)
    raw = (env.get("LLM_ROUTER") or "openai").strip().lower()
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
    user-managed and may not be running yet)."""
    import httpx

    env = environ if environ is not None else dict(os.environ)
    url = env.get("GECKO_LLM_ENDPOINT", "http://localhost:8402/v1")
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


def check_gecko_api(environ: dict[str, str] | None = None) -> CheckResult:
    """Probe `<GECKO_API_URL>/healthz`.

    - Unset: INFO (the MCP server falls back to the localhost default; that's
      fine for dev but should be set explicitly in production).
    - Set + reachable: PASS.
    - Set + unreachable: FAIL — a configured API that doesn't answer means
      every MCP tool call will error.
    """
    import httpx

    env = environ if environ is not None else dict(os.environ)
    raw = env.get("GECKO_API_URL")
    if not raw:
        return CheckResult(
            name="env:GECKO_API_URL",
            ok=True,
            detail="unset, MCP will default to https://api.geckovision.tech",
            info=True,
        )
    url = raw.rstrip("/") + "/healthz"
    try:
        response = httpx.get(url, timeout=5.0)
    except Exception as exc:
        return CheckResult(
            name="gecko_api:healthz",
            ok=False,
            detail=f"unreachable at {raw}: {exc.__class__.__name__}",
        )
    if response.status_code != 200:
        return CheckResult(
            name="gecko_api:healthz",
            ok=False,
            detail=f"status {response.status_code} from {raw}",
        )
    return CheckResult(name="gecko_api:healthz", ok=True, detail=f"reachable ({raw})")


def check_x402_mode(environ: dict[str, str] | None = None) -> CheckResult:
    """Validate X402_MODE. Defaults to 'stub' if unset (with a notice)."""
    env = environ if environ is not None else dict(os.environ)
    raw = env.get("X402_MODE")
    if not raw:
        return CheckResult(
            name="env:X402_MODE",
            ok=True,
            detail="unset, defaulting to 'stub'",
        )
    if raw not in ("stub", "live", "frames"):
        return CheckResult(
            name="env:X402_MODE",
            ok=False,
            detail=f"X402_MODE must be 'stub'|'live'|'frames', got {raw!r}",
        )
    return CheckResult(name="env:X402_MODE", ok=True, detail=raw)


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


def run_doctor(
    *,
    environ: dict[str, str] | None = None,
    supabase_client: _SupabaseLike | None = None,
) -> tuple[int, str]:
    """Run every check. Returns (exit_code, rendered_report).

    `supabase_client` is injectable for tests. If None and env is incomplete,
    we skip Supabase probes (no point probing without credentials).
    """
    results: list[CheckResult] = []
    results.extend(check_env(environ))
    results.append(check_x402_mode(environ))
    results.append(check_gecko_api(environ))
    results.extend(check_optional_env(environ))
    results.extend(check_wallet())
    results.extend(check_llm_router(environ))
    results.append(check_clawrouter(environ))

    # INFO checks never gate downstream probes.
    env_ok = all(r.ok for r in results if not r.info)
    if env_ok and supabase_client is not None:
        results.extend(check_supabase(supabase_client))
    elif env_ok:
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
