"""Coinbase Developer Platform (CDP) facilitator integration.

Mainnet x402 settlement runs through CDP at
``https://api.cdp.coinbase.com/platform/v2/x402``. CDP authenticates every
verify/settle request with a short-lived (2-min) JWT bearer token, signed
with the secret API key (Ed25519 or ES256).

We ride the existing ``x402.http.HTTPFacilitatorClient`` — that lib already
hooks an ``AuthProvider`` into request headers via ``FacilitatorConfig``,
which is exactly the seam CDP needs. We supply a CDP ``AuthProvider`` that
mints a fresh JWT for each verify / settle / supported call.

Sentinels treated as unset: any value matching ``__unset__``,
``__dev_change_me__``, or empty string. The ECS task references SSM params
in ``secrets:`` and refuses to start if a referenced param doesn't exist —
so push-ssm-params.sh writes a sentinel rather than skipping the param. We
re-detect those sentinels here to give a clean "not configured" error.

Spec note: CDP's exact JWT claim layout (``uri``, ``nbf``, ``exp``, ``iss``,
``sub``) follows their documented scheme as of April 2026. Until a real
mainnet smoke test lands, byte-for-byte compatibility with their server is
*not* certified — see ``docs/runbooks/mainnet-cutover.md``.
"""

from __future__ import annotations

import logging
import secrets as pysecrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from x402.http.facilitator_client import HTTPFacilitatorClient
from x402.http.facilitator_client_base import (
    AuthHeaders,
    AuthProvider,
    FacilitatorConfig,
)

logger = logging.getLogger(__name__)

# Values push-ssm-params.sh writes when the operator hasn't onboarded yet.
# Treat them as unconfigured so mainnet startup fails loudly rather than
# silently passing a literal "__unset__" upstream.
_SENTINELS: frozenset[str] = frozenset(
    {
        "",
        "__unset__",
        "__dev_change_me__",
    }
)


def is_unconfigured(value: str | None) -> bool:
    """True if `value` is missing or one of the SSM placeholder sentinels."""
    if value is None:
        return True
    return value.strip() in _SENTINELS


@dataclass(frozen=True)
class CDPCredentials:
    """Public key id + secret used to sign CDP JWTs."""

    key_id: str
    key_secret: str

    @staticmethod
    def from_env_values(key_id: str | None, key_secret: str | None) -> CDPCredentials:
        """Build credentials, raising a clear error if either is unconfigured.

        Mentions both env var names so the operator gets a complete fix list.
        """
        missing: list[str] = []
        if is_unconfigured(key_id):
            missing.append("CDP_API_KEY_ID")
        if is_unconfigured(key_secret):
            missing.append("CDP_API_KEY_SECRET")
        if missing:
            raise ValueError(
                "X402_NETWORK=solana-mainnet requires "
                f"{' and '.join(missing)} to be set "
                "(SSM sentinel or empty value detected)."
            )
        assert key_id is not None and key_secret is not None  # for type checker
        return CDPCredentials(key_id=key_id, key_secret=key_secret)


# ---------------------------------------------------------------------------
# JWT signing
# ---------------------------------------------------------------------------
#
# CDP accepts ES256 (P-256) or Ed25519. We support both — `_sign_jwt` sniffs
# the secret. A common mistake is to paste a PEM-encoded private key with
# extra whitespace or a "BEGIN" marker; pyjwt handles those forms natively.
#
# JWT payload (per CDP docs):
#   sub: <key_id>
#   iss: "cdp"
#   nbf: <now>
#   exp: <now + 2min>
#   uri: "<METHOD> <host><path>"   ← canonical form, no query string
# Header includes a `kid` (= key_id) and a unique `nonce`.
# ---------------------------------------------------------------------------


# Short JWT TTL — CDP rejects anything older than ~2 minutes. Refresh per call;
# verify and settle are not on the hot path.
_JWT_TTL_S = 120


def _canonical_uri(method: str, url: str) -> str:
    """`POST api.cdp.coinbase.com/platform/v2/x402/verify`-style claim."""
    parts = urlsplit(url)
    host = parts.netloc
    path = parts.path or "/"
    return f"{method.upper()} {host}{path}"


def _sign_jwt(
    *,
    creds: CDPCredentials,
    method: str,
    url: str,
    now: float | None = None,
) -> str:
    """Mint a CDP-compatible JWT for one verify/settle/supported call."""
    import jwt as pyjwt

    iat = int(now if now is not None else time.time())
    payload: dict[str, Any] = {
        "sub": creds.key_id,
        "iss": "cdp",
        "nbf": iat,
        "exp": iat + _JWT_TTL_S,
        "uri": _canonical_uri(method, url),
    }
    headers: dict[str, Any] = {
        "kid": creds.key_id,
        # Unique nonce per token; CDP rejects replays within the validity
        # window. Ample entropy for our verify/settle volume.
        "nonce": pysecrets.token_hex(16),
    }
    secret = creds.key_secret.strip()

    # Algorithm sniff: PEM EC keys → ES256; everything else → Ed25519. We
    # default to Ed25519 because CDP's recommended onboarding mints Ed25519.
    if "BEGIN EC PRIVATE KEY" in secret or ("BEGIN PRIVATE KEY" in secret and "EC " in secret):
        algorithm = "ES256"
    else:
        algorithm = "EdDSA"

    return pyjwt.encode(payload, secret, algorithm=algorithm, headers=headers)


# ---------------------------------------------------------------------------
# AuthProvider implementation
# ---------------------------------------------------------------------------


class CDPAuthProvider:
    """Mint per-endpoint Authorization headers for CDP facilitator calls.

    Implements the `AuthProvider` Protocol from x402.http.facilitator_client_base.
    Each `get_auth_headers()` call signs three fresh JWTs (verify, settle,
    supported) with their own canonical-URI claim. We tolerate the small
    overhead — facilitate calls are infrequent (one per paid request) and
    JWT signing is microseconds.
    """

    def __init__(
        self,
        credentials: CDPCredentials,
        base_url: str,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._creds = credentials
        # Trim trailing slash so `_canonical_uri` produces a clean path.
        self._base_url = base_url.rstrip("/")
        self._clock = clock or time.time

    def _bearer(self, method: str, path: str) -> dict[str, str]:
        token = _sign_jwt(
            creds=self._creds,
            method=method,
            url=f"{self._base_url}{path}",
            now=self._clock(),
        )
        return {"Authorization": f"Bearer {token}"}

    def get_auth_headers(self) -> AuthHeaders:
        return AuthHeaders(
            verify=self._bearer("POST", "/verify"),
            settle=self._bearer("POST", "/settle"),
            supported=self._bearer("GET", "/supported"),
        )


# Sanity check: `CDPAuthProvider` actually satisfies the protocol. Failing
# this would be caught at type-check time, but we want the runtime guarantee
# too — if x402 ever extends `AuthProvider`, this dies on import.
_provider_check: AuthProvider = CDPAuthProvider(
    CDPCredentials(key_id="placeholder", key_secret="placeholder"),
    base_url="https://example.invalid",
)
del _provider_check


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def build_cdp_facilitator_client(
    credentials: CDPCredentials,
    *,
    base_url: str = "https://api.cdp.coinbase.com/platform/v2/x402",
    identifier: str = "cdp-mainnet",
) -> HTTPFacilitatorClient:
    """Build the production CDP facilitator client.

    Returned client is shape-compatible with the existing devnet
    `HTTPFacilitatorClient` — same verify/settle/get_supported protocol —
    so the rest of the FastAPI wiring is network-agnostic.
    """
    auth_provider = CDPAuthProvider(credentials, base_url=base_url)
    config = FacilitatorConfig(
        url=base_url,
        auth_provider=auth_provider,
        identifier=identifier,
    )
    return HTTPFacilitatorClient(config)


# Per-spec request: a unified `CDPFacilitatorClient` symbol that mirrors the
# existing devnet client class. We expose it as a thin builder-returned alias
# rather than a class so we keep the exact same protocol surface (verify /
# settle / get_supported) without re-implementing those methods. Tests that
# `isinstance`-check should match the underlying `HTTPFacilitatorClient`.
def CDPFacilitatorClient(
    key_id: str,
    key_secret: str,
    *,
    base_url: str = "https://api.cdp.coinbase.com/platform/v2/x402",
) -> HTTPFacilitatorClient:
    """Construct a CDP-authenticated facilitator client.

    `key_id` / `key_secret` are accepted as plain strings — call sites are
    expected to unwrap a `pydantic.SecretStr` via `.get_secret_value()` so
    we don't accidentally print secrets in tracebacks.
    """
    creds = CDPCredentials.from_env_values(key_id, key_secret)
    return build_cdp_facilitator_client(creds, base_url=base_url)


def new_cdp_request_id() -> str:
    """Idempotency / trace key for CDP facilitator calls."""
    return f"cdp-{uuid.uuid4()}"


__all__ = [
    "CDPAuthProvider",
    "CDPCredentials",
    "CDPFacilitatorClient",
    "build_cdp_facilitator_client",
    "is_unconfigured",
    "new_cdp_request_id",
]
