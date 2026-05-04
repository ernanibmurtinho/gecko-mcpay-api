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
    "GECKO_LLM_ENDPOINT",
)

_OPTIONAL_HINTS: dict[str, str] = {
    "DEEPGRAM_API_KEY": "YouTube caption-fallback disabled",
    "OPENAI_API_KEY": "v2 fallback only; v3 routes through ClawRouter",
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

        idx_names = {i["name"] for i in client[db_name]["chunks"].list_search_indexes()}
        for required in ("chunks_vector", "chunks_text"):
            ok = required in idx_names
            results.append(
                CheckResult(
                    name=f"chunk_store:mongo:index:{required}",
                    ok=ok,
                    detail="ready" if ok else f"missing search index: {required}",
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
    expected ``pa-`` prefix. OPENAI_API_KEY is surfaced as INFO (not required).

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
        "voyage-3" if provider == "voyage" else "text-embedding-3-small"
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
    """
    env = environ if environ is not None else dict(os.environ)
    results: list[CheckResult] = []
    results.extend(check_env(environ))
    # Server-side keys: surface as PASS when set, INFO when absent.
    # Never fail the thin-client doctor.
    results.extend(check_server_side_env(environ))
    results.append(check_x402_mode(environ))
    results.append(check_gecko_api(environ))
    results.extend(check_optional_env(environ))
    results.extend(check_wallet())
    results.extend(check_llm_router(environ))
    results.append(check_clawrouter(environ))
    results.extend(check_chunk_store(environ))
    results.extend(check_embed_provider(environ))
    results.extend(check_voyage_api_key(environ))

    # Supabase probes only run when SUPABASE_URL + key are both present.
    # For remote thin-client installs these will be absent — that is normal.
    env_ok = all(r.ok for r in results if not r.info)
    supabase_creds_present = bool(env.get("SUPABASE_URL")) and bool(
        env.get("SUPABASE_SERVICE_ROLE_KEY")
    )
    if env_ok and supabase_creds_present:
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
