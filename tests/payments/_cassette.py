"""Tiny VCR-shaped JSON cassette helper for the Bazaar consumer contract test.

S16-BAZAAR-CONSUMER-04. Pattern C from CLAUDE.md — every X402-touching
client conformer ships with a recorded-fixture contract test before
any live wire ships. This is the consumer-side template.

Why not vcrpy: vcrpy has heavy filesystem/serializer machinery and a
sticky asyncio-transport story under httpx>=0.27. Our needs are
narrow enough that a 60-line JSON recorder is clearer and hooks
respx (which the suite already uses) to replay.

Cassette shape (JSON, human-readable diff-friendly)::

    {
      "interactions": [
        {
          "request": {"method": "GET", "url": "...", "headers": {...}},
          "response": {"status": 402, "headers": {...}, "body": "..."}
        },
        ...
      ]
    }

Re-record with ``GECKO_BAZAAR_LIVE=1``. Without that env var, the
helper replays from disk; CI runs in replay mode.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import respx

LIVE_ENV: str = "GECKO_BAZAAR_LIVE"


def is_live_record_mode() -> bool:
    return os.environ.get(LIVE_ENV) == "1"


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip auth / API-key headers before persisting a cassette."""
    redacted: dict[str, str] = {}
    sensitive = ("authorization", "x-api-key", "x-cdp-api-key", "cookie")
    for k, v in headers.items():
        if k.lower() in sensitive:
            redacted[k] = "<redacted>"
        else:
            redacted[k] = v
    return redacted


def load_cassette(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(
            f"cassette {path} missing — re-record with {LIVE_ENV}=1 and a funded buyer wallet"
        )
    data = json.loads(path.read_text())
    interactions = data.get("interactions", [])
    if not isinstance(interactions, list):
        raise ValueError(f"cassette {path} corrupt: interactions not a list")
    return interactions


def save_cassette(path: Path, interactions: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"interactions": interactions}, indent=2, sort_keys=True))


@contextmanager
def replay_cassette(path: Path) -> Iterator[respx.MockRouter]:
    """Replay every recorded interaction in order through respx.

    Mocks all incoming HTTP regardless of host. Each cassette interaction
    is bound to its URL+method; subsequent identical requests reuse the
    same response (so a retry hitting the same URL twice is fine).
    """
    interactions = load_cassette(path)
    with respx.mock(assert_all_called=False) as router:
        # Group responses per (method, url) — respx replays in declaration
        # order when the same route is hit multiple times.
        routes: dict[tuple[str, str], respx.Route] = {}
        for inter in interactions:
            req = inter["request"]
            res = inter["response"]
            method = req["method"].upper()
            url = req["url"]
            key = (method, url)
            response = httpx.Response(
                status_code=int(res["status"]),
                headers=res.get("headers") or {},
                content=res.get("body", "").encode("utf-8"),
            )
            if key not in routes:
                routes[key] = router.request(method, url=url)
                routes[key].mock(side_effect=[response])
            else:
                # Append to the side-effect list.
                existing = routes[key].side_effect or []
                if not isinstance(existing, list):
                    existing = [existing]
                routes[key].mock(side_effect=[*existing, response])
        yield router


@contextmanager
def record_or_replay(
    path: Path,
    *,
    record_callable: Callable[[], Any] | None = None,
) -> Iterator[None]:
    """Replay if cassette exists; in live mode, callers do real HTTP and
    the test author commits the resulting cassette by hand from logs.

    For S16 we keep this minimal: live mode does not auto-record (we
    don't trust an auto-recorder to redact correctly first time). The
    operator runs the test under ``GECKO_BAZAAR_LIVE=1``, captures the
    interactions via httpx event hooks (or `mitmproxy`), and commits a
    redacted JSON file. Replay is the steady state.
    """
    if is_live_record_mode():
        # Live mode — do nothing here, real network goes through.
        yield
        return
    with replay_cassette(path):
        yield
