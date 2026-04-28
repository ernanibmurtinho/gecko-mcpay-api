"""HMAC-signed session-scoped tokens.

Used for two related but distinct flows:

1. **events tokens** (prefix ``"events"``, 10-minute TTL) — required to
   subscribe to GET /research/pro/{session_id}/events.
2. **retry tokens** (prefix ``"retry"``, 24-hour TTL) — issued at the moment
   a Pro session fails; redeemable once at POST /research/pro/{session_id}/retry
   to re-run the debate without paying x402 again.

Format (urlsafe base64, no padding):
    base64(prefix "." session_id "." expiry_unix "." hex_hmac_sha256)

Why HMAC and not JWT: zero deps, ~80-90 bytes, scoped to one resource. The
prefix prevents the two token types from being confused at the verification
boundary — an `events:` token cannot redeem a retry, and vice versa. The
secret lives in `Settings.events_secret`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from uuid import UUID

logger = logging.getLogger(__name__)


_DEFAULT_EVENTS_TTL_SECONDS = 600  # 10 minutes — Pro debate runs in <2 min
_DEFAULT_RETRY_TTL_SECONDS = 24 * 60 * 60  # 24 hours

EVENTS_PREFIX = "events"
RETRY_PREFIX = "retry"


class EventsTokenError(Exception):
    """Raised by `verify_token` for any rejection reason. Maps to 401."""


def issue_token(
    session_id: UUID,
    secret: str,
    *,
    ttl_seconds: int = _DEFAULT_EVENTS_TTL_SECONDS,
    prefix: str = EVENTS_PREFIX,
    now: float | None = None,
) -> str:
    """Mint a token for `session_id` valid for `ttl_seconds`.

    `prefix` partitions the token namespace; verifiers MUST require the
    expected prefix so an events-token can never be replayed against the
    retry endpoint and vice versa.
    """
    if "." in prefix:
        raise ValueError("prefix must not contain '.'")
    issued = int(now if now is not None else time.time())
    expiry = issued + ttl_seconds
    payload = f"{prefix}.{session_id}.{expiry}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{payload}.{sig}".encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def issue_retry_token(
    session_id: UUID,
    secret: str,
    *,
    ttl_seconds: int = _DEFAULT_RETRY_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Convenience wrapper — mint a retry-prefixed token with a 24h TTL."""
    return issue_token(
        session_id,
        secret,
        ttl_seconds=ttl_seconds,
        prefix=RETRY_PREFIX,
        now=now,
    )


def verify_token(
    token: str,
    secret: str,
    expected_session_id: UUID,
    *,
    expected_prefix: str = EVENTS_PREFIX,
    now: float | None = None,
) -> None:
    """Raise EventsTokenError on any failure. Returns None on success.

    Validates: shape, prefix, signature, expiry, session-id binding.
    """
    if not token:
        raise EventsTokenError("missing token")
    # Re-pad for base64 decode.
    padding = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + padding).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise EventsTokenError("malformed token") from exc

    parts = raw.split(".")
    # Backwards compat: legacy events tokens (pre-prefix) had 3 parts:
    # session_id.expiry.sig. Treat them as having an implicit "events" prefix.
    if len(parts) == 3:
        sid_str, expiry_str, sig = parts
        actual_prefix = EVENTS_PREFIX
        signed = f"{sid_str}.{expiry_str}"
    elif len(parts) == 4:
        actual_prefix, sid_str, expiry_str, sig = parts
        signed = f"{actual_prefix}.{sid_str}.{expiry_str}"
    else:
        raise EventsTokenError("malformed token")

    expected_sig = hmac.new(
        secret.encode("utf-8"),
        signed.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise EventsTokenError("invalid token signature")

    if actual_prefix != expected_prefix:
        # Constant-time compare not required — this is a categorical mismatch,
        # not a secret. The wrong-prefix rejection is the whole point of the
        # namespace partition.
        raise EventsTokenError(
            f"token prefix mismatch (expected {expected_prefix!r}, got {actual_prefix!r})"
        )

    try:
        expiry = int(expiry_str)
    except ValueError as exc:
        raise EventsTokenError("malformed expiry") from exc

    current = int(now if now is not None else time.time())
    if current >= expiry:
        raise EventsTokenError("token expired")

    if sid_str != str(expected_session_id):
        raise EventsTokenError("token does not bind to this session")


__all__ = [
    "EVENTS_PREFIX",
    "RETRY_PREFIX",
    "EventsTokenError",
    "issue_retry_token",
    "issue_token",
    "verify_token",
]
