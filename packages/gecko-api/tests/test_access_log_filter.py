"""Unit tests for the 425 result-poll access-log downgrader."""

from __future__ import annotations

import logging

from gecko_api.access_log_filter import (
    ResultPoll425Downgrader,
    install,
)


def _make_record(message: str) -> logging.LogRecord:
    """Build a uvicorn-style access-log record.

    Real uvicorn renders args with `%s` substitution; the filter calls
    `record.getMessage()` so we just hand it a pre-rendered message.
    """
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=message,
        args=None,
        exc_info=None,
    )


def test_filter_downgrades_425_result_poll() -> None:
    f = ResultPoll425Downgrader()
    rec = _make_record(
        '127.0.0.1:0 - "GET /sessions/cae5ab28-4d99-4e9a-8811-b6e6bc1a6177/result HTTP/1.1" 425'
    )
    assert f.filter(rec) is True
    assert rec.levelno == logging.DEBUG
    assert rec.levelname == "DEBUG"


def test_filter_keeps_200_result_at_info() -> None:
    f = ResultPoll425Downgrader()
    rec = _make_record(
        '127.0.0.1:0 - "GET /sessions/cae5ab28-4d99-4e9a-8811-b6e6bc1a6177/result HTTP/1.1" 200'
    )
    assert f.filter(rec) is True
    assert rec.levelno == logging.INFO
    assert rec.levelname == "INFO"


def test_filter_keeps_unrelated_paths_at_info() -> None:
    f = ResultPoll425Downgrader()
    rec = _make_record('127.0.0.1:0 - "POST /research HTTP/1.1" 200')
    assert f.filter(rec) is True
    assert rec.levelno == logging.INFO


def test_filter_keeps_other_methods_at_info() -> None:
    """Only GET on /sessions/{id}/result with 425 is downgraded."""
    f = ResultPoll425Downgrader()
    rec = _make_record(
        '127.0.0.1:0 - "POST /sessions/cae5ab28-4d99-4e9a-8811-b6e6bc1a6177/result HTTP/1.1" 425'
    )
    assert f.filter(rec) is True
    assert rec.levelno == logging.INFO


def test_install_is_idempotent() -> None:
    """Calling install() twice must not stack duplicate filters."""
    access = logging.getLogger("uvicorn.access")
    # Wipe any prior install from another test to keep the count predictable.
    access.filters = [
        f for f in access.filters if getattr(f, "NAME", None) != ResultPoll425Downgrader.NAME
    ]

    install()
    install()
    matching = [
        f for f in access.filters if getattr(f, "NAME", None) == ResultPoll425Downgrader.NAME
    ]
    assert len(matching) == 1
