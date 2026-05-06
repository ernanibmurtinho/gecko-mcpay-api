"""Downgrade noisy `/sessions/{id}/result` 425 polls from INFO to DEBUG.

Background
----------
The result-polling contract returns HTTP 425 ("Too Early") while the
research workflow is still running. Clients (the CLI, the web app, the
MCP shim) poll every ~3s, so a 60s job emits ~20 access-log lines per
session that all say the same thing: "still working". On a busy box
those lines drown the actually-interesting access entries.

Fix
---
Install a `logging.Filter` on `uvicorn.access` that re-tags
`GET /sessions/<uuid>/result` 425 records as DEBUG. Everything else
(200 completions, 4xx mistakes, 5xx failures) keeps its INFO level so
operators still see the load-bearing signal.

The 425 IS the documented contract — see the docstring on
`gecko_api.main.session_result`. This filter is log hygiene only; it
does NOT change route behavior or the polling protocol.
"""

from __future__ import annotations

import logging
import re

# Uvicorn's default access-log format renders the request line as a
# single string argument: `GET /sessions/<uuid>/result HTTP/1.1`. We
# match the path + a 3-digit status code that includes 425. The UUID
# regex is loose on purpose — case-insensitive hex with dashes is enough
# to distinguish the result-poll path from any other endpoint.
_RESULT_POLL_425 = re.compile(
    r'"GET /sessions/[0-9a-fA-F-]{36}/result HTTP/[\d.]+" 425',
)


class ResultPoll425Downgrader(logging.Filter):
    """Re-tag 425 result-poll access lines as DEBUG.

    Returning ``True`` keeps the record in the pipeline; we only mutate
    its level, so handlers configured at INFO will drop the message
    naturally and DEBUG-attached handlers still see it for
    troubleshooting.
    """

    NAME = "gecko_api.access_log.result_poll_425_downgrader"

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access composes the message lazily — render the args
        # in once so the regex sees the final string.
        try:
            rendered = record.getMessage()
        except Exception:  # pragma: no cover — defensive
            return True
        if _RESULT_POLL_425.search(rendered):
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
        return True


def install() -> None:
    """Attach the downgrader to ``uvicorn.access`` exactly once.

    Idempotent — safe to call from the FastAPI lifespan startup. We
    detect a previously-installed instance by the filter's NAME
    attribute on the logger to avoid stacking duplicates across reloads
    in dev (uvicorn --reload re-imports the module).
    """
    access = logging.getLogger("uvicorn.access")
    for existing in access.filters:
        if getattr(existing, "NAME", None) == ResultPoll425Downgrader.NAME:
            return
    access.addFilter(ResultPoll425Downgrader())
