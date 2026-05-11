"""Pyth Hermes SSE client for price feeds.

Wire reference: https://hermes.pyth.network/docs — GET
``/v2/updates/price/stream?ids[]=<feed_id>&parsed=true`` returns a
text/event-stream where each ``data:`` line is JSON shaped like::

    {
      "binary": { ... },
      "parsed": [
        {
          "id": "<feed_id_hex_no_0x>",
          "price": {
            "price": "12345678",   # int as string
            "conf":  "12345",      # confidence interval
            "expo":  -8,           # apply as price * 10**expo
            "publish_time": 1718001234
          },
          "ema_price": { ... }
        }, ...
      ]
    }

We surface only the ``parsed`` block. Binary updates (used to settle
on-chain via the Pyth contracts) aren't on the agent's hot path —
trader-mode decisions read float prices in RAM.

Reconnect behaviour matches the Helius client: full-jitter exponential
backoff capped at 60s, transparent re-subscribe to the same feed-id
set on each fresh stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_RETRY_BASE_S = 1.0
_RETRY_CAP_S = 60.0


def _full_jitter_backoff(
    attempt: int, base: float = _RETRY_BASE_S, cap: float = _RETRY_CAP_S
) -> float:
    ceiling = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0.0, ceiling)


class PriceUpdate(BaseModel):
    """Parsed Pyth price update.

    Float ``price_float`` is precomputed (``price * 10**expo``) for
    trader-mode consumers that don't want to think about the exponent.
    The original ``price``/``conf``/``expo`` triple is preserved so
    consumers needing exact-integer math (basis-point comparisons,
    on-chain replay) don't lose precision.
    """

    feed_id: str
    price: int  # raw price * 10**(-expo)
    conf: int
    expo: int
    publish_time: int
    price_float: float = Field(...)

    @classmethod
    def from_parsed(cls, parsed: dict[str, Any]) -> PriceUpdate:
        """Build from one element of Hermes' ``parsed`` array."""
        price_block = parsed.get("price") or {}
        raw_price = int(price_block["price"])
        expo = int(price_block["expo"])
        return cls(
            feed_id=str(parsed["id"]),
            price=raw_price,
            conf=int(price_block.get("conf", 0)),
            expo=expo,
            publish_time=int(price_block.get("publish_time", 0)),
            price_float=raw_price * (10**expo),
        )


PriceCallback = Callable[[PriceUpdate], Awaitable[None]]


@dataclass
class _PythStats:
    connections: int = 0
    reconnects: int = 0
    updates_received: int = 0
    updates_dropped: int = 0
    last_update_ts: float | None = None
    parse_errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "connections": self.connections,
            "reconnects": self.reconnects,
            "updates_received": self.updates_received,
            "updates_dropped": self.updates_dropped,
            "last_update_ts": self.last_update_ts,
            "parse_errors": self.parse_errors,
        }


class PythHermesClient:
    """SSE client subscribing to a fixed set of price feeds.

    The current model is "one client = one feed-id set". If the caller
    wants to add or drop feeds at runtime, build a new client; the
    Hermes stream URL is shaped by the feed list and re-keying mid-flight
    would just be a reconnect anyway.
    """

    def __init__(
        self,
        base_url: str = "https://hermes.pyth.network",
        *,
        reconnect_initial_s: float = _RETRY_BASE_S,
        reconnect_max_s: float = _RETRY_CAP_S,
        request_timeout_s: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._reconnect_initial_s = reconnect_initial_s
        self._reconnect_max_s = reconnect_max_s
        self._request_timeout_s = request_timeout_s
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._feed_ids: list[str] = []
        self._callback: PriceCallback | None = None
        self._stats = _PythStats()

    async def start(self) -> None:
        """Hermes is request-driven — there's no idle connection to
        warm up. We initialise state and return; the actual stream
        opens on the first :meth:`subscribe_prices` call."""
        self._running = True

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def subscribe_prices(
        self,
        price_feed_ids: list[str],
        callback: PriceCallback,
    ) -> None:
        """Begin streaming updates for ``price_feed_ids``.

        Only one active subscription at a time — calling this twice
        replaces the previous subscription (and cancels the old stream).
        """
        if not price_feed_ids:
            raise ValueError("price_feed_ids must be non-empty")
        if not self._running:
            await self.start()
        # Cancel any pre-existing stream.
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._feed_ids = list(price_feed_ids)
        self._callback = callback
        self._task = asyncio.create_task(self._stream_loop(), name="pyth-hermes-stream")

    def stats(self) -> dict[str, Any]:
        d = self._stats.as_dict()
        d["feed_count"] = len(self._feed_ids)
        return d

    # ----- internals -----

    def _build_params(self) -> list[tuple[str, str]]:
        # Hermes accepts ?ids[]=<id>&ids[]=<id>&parsed=true. httpx
        # serialises a list of tuples in the order given.
        params: list[tuple[str, str]] = [("ids[]", fid) for fid in self._feed_ids]
        params.append(("parsed", "true"))
        return params

    async def _stream_loop(self) -> None:
        attempt = 0
        url = f"{self._base_url}/v2/updates/price/stream"
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._request_timeout_s, read=None)
        ) as http:
            while self._running:
                try:
                    async with http.stream(
                        "GET",
                        url,
                        params=self._build_params(),
                        headers={"Accept": "text/event-stream"},
                    ) as resp:
                        resp.raise_for_status()
                        self._stats.connections += 1
                        if attempt > 0:
                            self._stats.reconnects += 1
                        attempt = 0
                        logger.info(
                            "pyth.hermes_connected feeds=%d",
                            len(self._feed_ids),
                        )
                        await self._consume(resp)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "pyth.hermes_disconnect err_type=%s msg=%s",
                        type(exc).__name__,
                        str(exc)[:200],
                    )
                if not self._running:
                    return
                attempt += 1
                sleep_s = _full_jitter_backoff(
                    attempt,
                    base=self._reconnect_initial_s,
                    cap=self._reconnect_max_s,
                )
                await asyncio.sleep(sleep_s)

    async def _consume(self, resp: httpx.Response) -> None:
        """Walk the SSE stream, parse ``data:`` events, dispatch.

        We hand-parse rather than pull in httpx-sse so the parser stays
        under our control for the recorded-fixture contract test.
        """
        buffer: list[str] = []
        async for line in resp.aiter_lines():
            if line == "":
                # End of event. Concatenate accumulated data lines.
                if not buffer:
                    continue
                payload = "\n".join(buffer)
                buffer.clear()
                await self._handle_event_payload(payload)
                continue
            if line.startswith(":"):
                # SSE comment / keepalive.
                continue
            if line.startswith("data:"):
                buffer.append(line[len("data:") :].lstrip())
            # Other SSE fields (event:, id:, retry:) are ignored — Hermes
            # currently sends bare data events.

    async def _handle_event_payload(self, payload: str) -> None:
        try:
            doc = json.loads(payload)
        except json.JSONDecodeError:
            self._stats.parse_errors += 1
            logger.warning("pyth.bad_json bytes=%d", len(payload))
            return
        parsed_list = doc.get("parsed") or []
        if not isinstance(parsed_list, list):
            self._stats.parse_errors += 1
            return
        for parsed in parsed_list:
            try:
                update = PriceUpdate.from_parsed(parsed)
            except Exception as exc:
                self._stats.parse_errors += 1
                logger.warning(
                    "pyth.parse_error err=%s",
                    type(exc).__name__,
                )
                continue
            self._stats.updates_received += 1
            self._stats.last_update_ts = time.time()
            if self._callback is None:
                self._stats.updates_dropped += 1
                continue
            try:
                await self._callback(update)
            except Exception as exc:
                logger.exception(
                    "pyth.callback_error feed_id=%s err=%s",
                    update.feed_id,
                    type(exc).__name__,
                )
