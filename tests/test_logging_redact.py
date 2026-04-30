"""S8-LOG-01 — verify the redacting log filter scrubs secrets.

Covers the three shapes our HTTP stack emits at DEBUG:
  1. httpcore tuple-form headers: ``(b'authorization', b'Bearer ...')``
  2. hpack/httpx colon-form headers: ``authorization: Bearer ...``
  3. Bare ``Bearer <token>`` substrings + JWT-shaped tokens.
"""

from __future__ import annotations

import logging

from gecko_core._logging import REDACTED, RedactingFilter, install, redact


def test_redact_authorization_tuple_form() -> None:
    raw = "Headers([(b'host', b'api.openai.com'), (b'authorization', b'Bearer sk-abc123XYZ')])"
    out = redact(raw)
    assert "sk-abc123XYZ" not in out
    assert REDACTED in out
    # Non-sensitive headers stay untouched.
    assert "api.openai.com" in out


def test_redact_apikey_colon_form() -> None:
    raw = "decoded header: apikey: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    out = redact(raw)
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in out
    assert REDACTED in out


def test_redact_x_api_key_colon_form() -> None:
    raw = "x-api-key: tvly-1234567890abcdef"
    out = redact(raw)
    assert "tvly-1234567890abcdef" not in out


def test_redact_supabase_token_header() -> None:
    """Header NAMES containing 'token' get redacted even when not in the always-list."""
    raw = "x-supabase-auth-token: eyJhbGc.payload.sig"
    out = redact(raw)
    assert "eyJhbGc.payload.sig" not in out


def test_redact_bare_bearer() -> None:
    raw = "calling https://api.openai.com with Bearer sk-proj-VeryLongSecret"
    out = redact(raw)
    assert "sk-proj-VeryLongSecret" not in out
    assert "Bearer <redacted>" in out


def test_redact_jwt_shape() -> None:
    raw = "session jwt: aaaaaaaaa.bbbbbbbbb.ccccccccc"
    out = redact(raw)
    assert "aaaaaaaaa.bbbbbbbbb.ccccccccc" not in out


def test_redact_idempotent() -> None:
    raw = "authorization: Bearer foo123XYZ"
    once = redact(raw)
    twice = redact(once)
    assert once == twice


def test_filter_scrubs_log_record_msg() -> None:
    f = RedactingFilter()
    rec = logging.LogRecord(
        name="httpcore",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg="authorization: Bearer sk-leakySecret",
        args=None,
        exc_info=None,
    )
    assert f.filter(rec) is True
    assert "sk-leakySecret" not in str(rec.msg)
    assert REDACTED in str(rec.msg)


def test_filter_scrubs_log_record_args_tuple() -> None:
    f = RedactingFilter()
    rec = logging.LogRecord(
        name="httpx",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg="sending %s with %s",
        args=("GET /v1/chat", "Bearer sk-leakSecret"),
        exc_info=None,
    )
    assert f.filter(rec) is True
    assert isinstance(rec.args, tuple)
    assert all("sk-leakSecret" not in str(a) for a in rec.args)


def test_install_is_idempotent() -> None:
    """Re-running install() must not stack filters on the root logger."""
    root = logging.getLogger()
    install()
    install()
    install()
    redactors = [x for x in root.filters if isinstance(x, RedactingFilter)]
    assert len(redactors) == 1
    # Cleanup so the test doesn't leak into other tests.
    for x in redactors:
        root.removeFilter(x)
