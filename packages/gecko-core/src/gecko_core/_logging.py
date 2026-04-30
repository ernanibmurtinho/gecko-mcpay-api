"""Logging hygiene primitives (S8-LOG-01).

When ``LOG_LEVEL=DEBUG`` is set, ``httpcore`` and ``hpack`` emit raw header
dumps that contain Supabase JWT, OpenAI/OpenRouter Bearer tokens, and
Tavily ``X-API-Key`` values. Forward those to stdout in production and
you've leaked secrets in CloudWatch.

This module ships a single ``RedactingFilter`` plus an ``install()`` that
wires the filter into the root logger. Each app entry point (gecko-api,
gecko-mcp, ``bb`` CLI) calls ``install()`` once at startup. The filter is
attached at the root so it covers every existing AND future logger
without per-module bookkeeping.

Redaction strategy: rewrite the rendered ``LogRecord.msg`` and any
``args`` string values, replacing matched tokens with ``<redacted>``. We
do NOT attempt to parse structured logging payloads — too many shapes,
too many false negatives. Instead we redact the canonical token-shaped
substrings that ``httpcore``/``hpack``/``httpx`` emit:

  - ``authorization: <value>``   (case-insensitive header dump)
  - ``apikey: <value>``
  - ``x-api-key: <value>``
  - ``cookie: <value>`` / ``set-cookie: <value>``
  - any header whose name contains ``key``/``token``/``secret``
  - bare ``Bearer <token>`` substrings (httpx debug logs prefix this way)
  - long JWT-shaped substrings (3 base64url segments separated by dots)

The filter is intentionally over-aggressive: a false-positive redaction
of a non-secret token is fine; a false-negative leak is not.
"""

from __future__ import annotations

import logging
import re
from typing import Any

REDACTED = "<redacted>"

# Canonical sensitive header names we always redact. Lower-cased; matched
# case-insensitively at the regex level.
_ALWAYS_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "apikey",
        "x-api-key",
        "cookie",
        "set-cookie",
        "proxy-authorization",
    }
)

# Substring matchers for header NAMES that are sensitive even when we
# haven't seen the literal name before. ``contains``-style matching keeps
# the filter robust against vendor-specific prefixes (``x-supabase-key``,
# ``X-OpenAI-Token``, etc.).
_HEADER_NAME_FRAGMENTS: tuple[str, ...] = ("key", "token", "secret")

# httpcore's debug formatter renders headers as ``b'name', b'value'`` or
# ``('name', 'value')`` tuples. Match the value in either form.
#
# Example log line from httpcore=0.x at DEBUG:
#   send_request_headers.complete return_value=Headers([(b'host', b'...'),
#       (b'authorization', b'Bearer eyJhbGciOi...'), ...])
_HEADER_TUPLE_RE = re.compile(
    r"""
    (?P<prefix>[\(\[])                       # opening tuple/list
    \s*
    (?P<name_q>b?['"])                       # opening quote (optional b prefix)
    (?P<name>[A-Za-z0-9_-]+)                 # header name
    (?P=name_q)                              # closing same-style quote
    \s*,\s*
    (?P<val_q>b?['"])                        # opening value quote
    (?P<value>[^'"]*)                        # value contents
    (?P=val_q)                               # closing value quote
    """,
    re.VERBOSE,
)

# hpack debug logs render decoded headers as `header_name: value` lines.
# Example:
#   decoded header: authorization: Bearer eyJhbGciOi...
#
# Match every ``name: value`` pair on a line and let ``_redact_colon_match``
# decide whether the name is sensitive. The trailing-comma boundary keeps
# us from gobbling whole multi-header dumps in one match.
_HEADER_COLON_RE = re.compile(
    r"""
    (?P<name>[A-Za-z][A-Za-z0-9_-]*)         # header name token
    \s*:\s*
    (?P<value>[^\r\n,]+?)                    # value (stops at newline / comma)
    (?=$|[\r\n,])
    """,
    re.VERBOSE | re.MULTILINE,
)

# Bare `Bearer <token>` substrings. httpx logs URLs with auth params in
# DEBUG sometimes embed these without a `name: value` shape.
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-+/=]+", re.IGNORECASE)

# JWT-shaped bare tokens — three dot-separated base64url segments, each at
# least 8 chars to avoid matching version strings or UUIDs.
_JWT_RE = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")


def _is_sensitive_header(name: str) -> bool:
    """True if ``name`` (case-insensitive) is a header we always redact."""
    lowered = name.lower()
    if lowered in _ALWAYS_HEADERS:
        return True
    return any(frag in lowered for frag in _HEADER_NAME_FRAGMENTS)


def _redact_tuple_match(m: re.Match[str]) -> str:
    name = m.group("name")
    if not _is_sensitive_header(name):
        return m.group(0)
    return (
        f"{m.group('prefix')}{m.group('name_q')}{name}{m.group('name_q')}, "
        f"{m.group('val_q')}{REDACTED}{m.group('val_q')}"
    )


def _redact_colon_match(m: re.Match[str]) -> str:
    name = m.group("name")
    if not _is_sensitive_header(name):
        return m.group(0)
    return f"{name}: {REDACTED}"


def _redact_colon_form(text: str) -> str:
    """Walk colon-form ``name: value`` pairs, redacting only sensitive names.

    Why not a plain ``re.sub``? When a benign pair appears before a
    sensitive pair on the same line (``decoded header: apikey: <secret>``),
    re.sub's leftmost-non-overlapping behavior consumes the benign value
    (which happens to be the next header name) and the cursor lands past
    it, skipping the real secret. This walker advances past the ``name:``
    prefix of benign matches but leaves the value text in place so the
    next iteration can reparse it as a potential header.
    """
    pos = 0
    out: list[str] = []
    while pos < len(text):
        m = _HEADER_COLON_RE.search(text, pos)
        if m is None:
            out.append(text[pos:])
            break
        # Append everything before the match unchanged.
        out.append(text[pos : m.start()])
        name = m.group("name")
        if _is_sensitive_header(name):
            out.append(f"{name}: {REDACTED}")
            pos = m.end()
        else:
            # Keep the literal name + colon, but back the cursor up so the
            # value text is re-scanned for nested ``name: value`` pairs.
            value_start = m.start("value")
            out.append(text[m.start() : value_start])
            pos = value_start
    return "".join(out)


def redact(text: str) -> str:
    """Return ``text`` with sensitive header values + bare bearer/JWT tokens redacted.

    Pure function — exported so tests can verify the regex set without
    constructing LogRecords. Idempotent.
    """
    if not text:
        return text
    # Order matters: tuple-form first (httpcore), then colon-form
    # (hpack/httpx), then bare bearer/JWT. Each pass operates on the prior
    # output so a string with both shapes ends up fully scrubbed.
    out = _HEADER_TUPLE_RE.sub(_redact_tuple_match, text)
    out = _redact_colon_form(out)
    out = _BEARER_RE.sub(f"Bearer {REDACTED}", out)
    out = _JWT_RE.sub(REDACTED, out)
    return out


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs secrets from every record.

    Mutates ``record.msg`` and any string entries in ``record.args`` in
    place. Returns True so the record still propagates — this is a
    redactor, not a dropper.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            # ``record.args`` is None, a tuple, or a single mapping. Touch
            # only string members; numbers/bools/etc are uninteresting.
            args = record.args
            if isinstance(args, tuple) and args:
                new_args: list[Any] = []
                for a in args:
                    new_args.append(redact(a) if isinstance(a, str) else a)
                record.args = tuple(new_args)
            elif isinstance(args, dict):
                # Mappings used by `%(name)s`-style format strings. Redact
                # values only; keys are usually plain identifiers.
                record.args = {k: (redact(v) if isinstance(v, str) else v) for k, v in args.items()}
        except Exception:  # pragma: no cover — never let logging crash the app
            return True
        return True


def install(*, level: int | str | None = None) -> RedactingFilter:
    """Attach a ``RedactingFilter`` to the root logger.

    Idempotent: re-running ``install()`` does not stack filters. Returns
    the filter instance for tests/inspection.

    Args:
        level: Optional root log level (e.g. ``logging.DEBUG`` or "INFO").
            When None, the existing root level is left alone — call sites
            that want to honor ``LOG_LEVEL`` env should resolve it before
            invoking ``install``.
    """
    root = logging.getLogger()
    # Drop any prior instance so re-install doesn't stack filters.
    for existing in list(root.filters):
        if isinstance(existing, RedactingFilter):
            root.removeFilter(existing)
    f = RedactingFilter()
    root.addFilter(f)

    # Filters on the root logger only fire for records actually handled by
    # root's handlers. Library loggers like ``httpcore`` propagate up to
    # root, but if a handler is attached directly to them OR to any
    # specific logger, those handlers see the un-filtered record. Defend
    # by also attaching the filter to every existing handler on root.
    for h in root.handlers:
        # Avoid duplicate filter attachments.
        if not any(isinstance(x, RedactingFilter) for x in h.filters):
            h.addFilter(f)

    if level is not None:
        root.setLevel(level)
    return f


__all__ = ["REDACTED", "RedactingFilter", "install", "redact"]
