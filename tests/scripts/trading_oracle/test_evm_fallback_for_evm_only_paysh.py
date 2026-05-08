"""Phase 10A — EVM-buyer fallback tests for EVM-only paysh 402 challenges.

paysponge/perplexity-style providers advertise Base (eip155:8453) only
in their 402; the Solana buyer correctly raises (since paysh-route
presumes Solana settlement). The fallback re-issues the call via the
existing EVM ``_LiveX402PaidRequester`` when ``TWITSH_WALLET_*`` is
configured.

Light fakes only — no real x402, no real RPC, no real httpx requests.
Per ``feedback_lighter_tests``: exercise the typed exception + fallback
helpers directly rather than full request flows.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts" / "trading_oracle"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Force-load the paysh_live module BEFORE run.py imports gecko_core. The
# package exposes ``sources`` as a function (back-compat shim) and only
# binds a hard-coded subset of submodules onto it; ``paysh_live`` isn't on
# that list, so ``import gecko_core.sources.paysh_live`` only works once
# the submodule has been loaded by some other path. See
# ``gecko_core/__init__.py`` S17-WEDGE-WIRE-02 comment.
import gecko_core.sources.paysh_live  # noqa: F401, E402  - load-bearing import order

# Bootstrap helpers — loaded lazily so we always pick up the current
# sys.modules entries. Other test files in this directory reload their
# own copies of run.py and solana_buyer.py at collection time, so a
# module-level cache here would point at a stale module instance and
# trip ``isinstance`` against the wrong NoSolanaAcceptsError class.
_RUN_PATH = _SCRIPTS_DIR / "run.py"
_BUYER_PATH = _SCRIPTS_DIR / "solana_buyer.py"


def _load_module(mod_name: str, path: Path) -> object:
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run_mod() -> object:
    """Return whichever ``trading_oracle_run`` is current in sys.modules."""
    return sys.modules.get("trading_oracle_run") or _load_module("trading_oracle_run", _RUN_PATH)


def _buyer_mod() -> object:
    """Return whichever ``solana_buyer`` is current in sys.modules."""
    return sys.modules.get("solana_buyer") or _load_module("solana_buyer", _BUYER_PATH)


# Force-load once so subsequent tests have a baseline.
_run_mod()
_buyer_mod()


# ---------------------------------------------------------------------------
# A3.1 — typed exception carries the advertised networks list.
# ---------------------------------------------------------------------------


def test_solana_buyer_raises_typed_exception_on_evm_only_accepts() -> None:
    """``pick_solana_accepts_entry`` raises ``NoSolanaAcceptsError`` with the
    list of advertised networks when no Solana entry is present.

    Subclasses ``RuntimeError`` so pre-Phase 10A `except RuntimeError`
    callers stay compatible.
    """
    evm_only = [
        {"network": "eip155:8453", "asset": "USDC", "amount": "1000"},
        {"network": "eip155:43114", "asset": "USDC", "amount": "1000"},
    ]
    with pytest.raises(_buyer_mod().NoSolanaAcceptsError) as ei:
        _buyer_mod().pick_solana_accepts_entry(evm_only)
    assert isinstance(ei.value, RuntimeError)
    assert ei.value.advertised_networks == ["eip155:8453", "eip155:43114"]


# ---------------------------------------------------------------------------
# A3.2 — _is_no_solana_accepts walks the cause chain through paysh_live's
# ProviderUnreachableError wrapper.
# ---------------------------------------------------------------------------


def test_is_no_solana_accepts_unwraps_provider_unreachable_error() -> None:
    """The check unwinds ``__cause__`` so paysh_live's wrap doesn't hide it."""
    inner = _buyer_mod().NoSolanaAcceptsError(["eip155:8453"])
    try:
        try:
            raise inner
        except _buyer_mod().NoSolanaAcceptsError as e:
            raise RuntimeError("paysh_live: requester error") from e
    except RuntimeError as wrapped:
        assert _run_mod()._is_no_solana_accepts(wrapped) is True
        assert _run_mod()._advertised_from_cause(wrapped) == ["eip155:8453"]


def test_is_no_solana_accepts_returns_false_for_unrelated_error() -> None:
    """Random RuntimeError must not trigger the EVM fallback path."""
    err = RuntimeError("totally unrelated")
    assert _run_mod()._is_no_solana_accepts(err) is False


# ---------------------------------------------------------------------------
# A3.3 — clear error message when TWITSH_WALLET_* env not set.
# ---------------------------------------------------------------------------


def test_no_evm_env_clear_error_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """No EVM wallet env → ``_build_evm_requester_for_fallback`` raises with
    the env var names mentioned so the operator knows what to set.
    """
    for var in (
        "TWITSH_WALLET_PRIVATE_KEY",
        "TWITSH_WALLET_ADDRESS",
        "GECKO_BUYER_WALLET_PRIVATE_KEY",
        "GECKO_BUYER_WALLET_ADDRESS",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="TWITSH_WALLET_"):
        _run_mod()._build_evm_requester_for_fallback(advertised=["eip155:8453"])


# ---------------------------------------------------------------------------
# A3.4 — _charge_and_fetch routes to the EVM buyer when the Solana buyer
# refused the EVM-only accepts. Exercises the dispatch only — both paysh
# fetch_paid invocations are stubbed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_charge_and_fetch_routes_to_evm_buyer_when_solana_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the Solana paysh call raises NoSolanaAcceptsError (wrapped),
    ``_charge_and_fetch`` retries the same paysh_fetch_paid call with an
    EVM requester built fresh from TWITSH_WALLET_* env.
    """
    # Wallet env so the fallback can build an EVM requester.
    monkeypatch.setenv(
        "TWITSH_WALLET_PRIVATE_KEY",
        "0x" + "11" * 32,
    )
    monkeypatch.setenv("TWITSH_WALLET_ADDRESS", "0x000000000000000000000000000000000000dEaD")

    # Fake a Solana requester so _build_paid_requester returns it without
    # touching solders.
    class _FakeSolanaRequester:
        def __init__(self) -> None:
            self.network = "solana-mainnet"

        def set_listing_context(self, **kwargs: object) -> None:
            return None

    fake_solana = _FakeSolanaRequester()
    monkeypatch.setattr(_run_mod(), "_build_paid_requester", lambda: fake_solana)

    captured: dict[str, object] = {"calls": []}

    class _FakeResult:
        def __init__(self, body: str) -> None:
            self.payload = {"chunks": [{"text": body}]}

    async def _fake_paysh_fetch_paid(
        fqn: str, query: str, *, x402_client: object, catalog_providers: object
    ) -> _FakeResult:
        # First invocation: simulate the wrapped NoSolanaAcceptsError that
        # paysh_live re-raises. Second invocation: success.
        calls = captured["calls"]
        assert isinstance(calls, list)
        calls.append(x402_client)
        if len(calls) == 1:
            inner = _buyer_mod().NoSolanaAcceptsError(["eip155:8453"])
            try:
                raise inner
            except _buyer_mod().NoSolanaAcceptsError as e:
                raise RuntimeError("paysh_live: wrapped") from e
        return _FakeResult("ok body")

    # Patch the paysh import target — `from gecko_core.sources.paysh_live
    # import fetch_paid` in _charge_and_fetch resolves at call time. The
    # parent ``gecko_core.sources`` is shadowed by a function (back-compat),
    # so ``import gecko_core.sources.paysh_live`` only works through the
    # already-loaded sys.modules entry. Use that handle directly.
    paysh_mod = sys.modules["gecko_core.sources.paysh_live"]
    monkeypatch.setattr(paysh_mod, "fetch_paid", _fake_paysh_fetch_paid)

    # Avoid going through _LiveX402PaidRequester's __init__ (which in
    # principle could reach into web3 libs — keep this light).
    class _FakeEvmRequester:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def set_listing_context(self, **kwargs: object) -> None:
            return None

    monkeypatch.setattr(_run_mod(), "_LiveX402PaidRequester", _FakeEvmRequester)

    # Build a synthetic PlannedCall (frozen dataclass: name + price_usd + listing).
    from decimal import Decimal as _Decimal

    planned_call = _run_mod().PlannedCall(
        name="paysponge/perplexity",
        price_usd=_Decimal("0.01"),
        listing={
            "fqn": "paysponge/perplexity",
            "name": "Perplexity",
            "description": "",
            "category": "search",
            "price_usd": 0.01,
            "service_url": "https://x402.example/sonar",
            "endpoints": [
                {"url": "https://x402.example/sonar", "method": "POST"},
            ],
            "provider_kind": "paysh_live",
        },
    )

    out = await _run_mod()._charge_and_fetch(planned_call)
    assert out["body"] == "ok body"
    assert out["fqn"] == "paysponge/perplexity"

    calls = captured["calls"]
    assert isinstance(calls, list)
    assert len(calls) == 2, "expected one Solana attempt + one EVM retry"
    # First call used the Solana fake; second call used the EVM fake.
    assert calls[0] is fake_solana
    assert isinstance(calls[1], _FakeEvmRequester)
