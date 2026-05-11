"""Helius WebSocket subscription manager for trade-agent hot path.

Wire reference: https://docs.helius.dev/ — ``accountSubscribe`` /
``programSubscribe`` / ``transactionSubscribe`` over the Helius JSON-RPC
WebSocket endpoint. The endpoint is shaped as
``wss://mainnet.helius-rpc.com/?api-key=<key>`` (mainnet) — caller may
override for devnet / staging.

Responsibilities (W3-1 in
`docs/strategy/2026-05-11-trade-vertical-expansion.md` §7):

* Open a single websocket; multiplex multiple subscriptions over it.
* Full-jitter exponential backoff on disconnect, capped at 60s — same
  shape as :func:`gecko_core.ingestion.embedder._full_jitter_backoff`.
* Transparently re-subscribe each registered callback on every fresh
  connection. From the caller's POV, ``subscribe_account`` is a
  promise — events keep flowing across reconnects.
* If the websocket fails to come back inside ``polling_fallback_after``
  seconds, switch the affected subscriptions to a 5s HTTP polling loop
  (Helius ``getAccountInfo``) and log a warning. Polling stops as soon
  as the websocket recovers.
* Counters exposed via :meth:`stats` — surfaced through ``bb trade-agent
  inspect`` and the runtime journal.

We do **not** sign or settle transactions here. This file is read-only
chain state ingest.
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
import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

# Same backoff envelope as ingestion/embedder.py:119 — keep them in
# lockstep so operators only have to memorise one curve.
_RETRY_BASE_S = 1.0
_RETRY_CAP_S = 60.0

# Pyth/Helius "polling fallback" cadence per design spec §6 risk row.
_POLL_INTERVAL_S = 5.0

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _full_jitter_backoff(
    attempt: int, base: float = _RETRY_BASE_S, cap: float = _RETRY_CAP_S
) -> float:
    """AWS-style full-jitter exponential. Mirrors ``embedder._full_jitter_backoff``.

    ``attempt`` is 1-indexed. Returns a sleep drawn uniformly from
    ``[0, min(cap, base * 2 ** (n-1))]``.
    """
    ceiling = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0.0, ceiling)


@dataclass
class _Subscription:
    sub_id: int
    method: str  # "accountSubscribe" | "programSubscribe"
    params: list[Any]
    callback: EventCallback
    # Helius assigns a server-side subscription id distinct from our
    # local sub_id. We track both so we can map incoming notifications
    # back to the caller.
    server_sub_id: int | None = None


@dataclass
class _Stats:
    connections: int = 0
    reconnects: int = 0
    events_received: int = 0
    events_dropped: int = 0
    polling_fallbacks: int = 0
    last_event_ts: float | None = None
    rpc_errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "connections": self.connections,
            "reconnects": self.reconnects,
            "events_received": self.events_received,
            "events_dropped": self.events_dropped,
            "polling_fallbacks": self.polling_fallbacks,
            "last_event_ts": self.last_event_ts,
            "rpc_errors": self.rpc_errors,
        }


class HeliusWebSocketClient:
    """Multiplexed websocket client for Helius account/program subs.

    Lifecycle:

        client = HeliusWebSocketClient(api_key=os.environ["HELIUS_API_KEY"])
        await client.start()
        sub_id = await client.subscribe_account(mint, on_event)
        ...
        await client.unsubscribe(sub_id)
        await client.stop()

    The instance is single-use after :meth:`stop`. Re-start by
    constructing a fresh client.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_ws: str | None = None,
        http_base: str = "https://mainnet.helius-rpc.com",
        reconnect_initial_s: float = _RETRY_BASE_S,
        reconnect_max_s: float = _RETRY_CAP_S,
        polling_fallback_after_s: float = 60.0,
        polling_interval_s: float = _POLL_INTERVAL_S,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        # Never log or echo the key — these strings shouldn't appear in
        # error messages either. The URL carries the key as a query
        # param, which is the wire shape Helius supports.
        self._base_ws = base_ws or f"wss://mainnet.helius-rpc.com/?api-key={api_key}"
        self._http_base = http_base
        self._reconnect_initial_s = reconnect_initial_s
        self._reconnect_max_s = reconnect_max_s
        self._polling_fallback_after_s = polling_fallback_after_s
        self._polling_interval_s = polling_interval_s

        self._ws: Any = None
        self._next_local_id = 1
        self._next_rpc_id = 1
        self._subscriptions: dict[int, _Subscription] = {}
        # Map server-side sub id -> local sub id for routing inbound
        # notifications.
        self._server_to_local: dict[int, int] = {}
        # Pending RPC responses keyed by JSON-RPC id.
        self._pending: dict[int, asyncio.Future[Any]] = {}
        # When a subscribe RPC is in flight we track the local sub id
        # it belongs to so the read loop can install the
        # server_sub_id → local_sub_id mapping atomically with ACK
        # parsing. Otherwise a notification arriving on the heels of
        # the ACK races the caller's "sub.server_sub_id = ..." line.
        self._pending_subscribe: dict[int, int] = {}
        self._running = False
        self._reader_task: asyncio.Task[None] | None = None
        self._polling_task: asyncio.Task[None] | None = None
        self._ws_lock = asyncio.Lock()
        self._connected_event = asyncio.Event()
        self._stats = _Stats()

    # ----- public API -----

    async def start(self) -> None:
        """Spin the reconnect loop. Returns once the first ws is up
        (or the first reconnect attempt is in flight — we don't block
        forever)."""
        if self._running:
            return
        self._running = True
        self._reader_task = asyncio.create_task(self._connect_loop(), name="helius-ws-connect")
        # Best-effort wait for first connect; bail after the initial
        # backoff envelope so callers aren't blocked indefinitely if
        # Helius is unreachable.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                self._connected_event.wait(),
                timeout=max(self._reconnect_initial_s * 2, 2.0),
            )

    async def stop(self) -> None:
        """Cancel reader + polling tasks and close the websocket."""
        self._running = False
        for task in (self._reader_task, self._polling_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reader_task = None
        self._polling_task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

    async def subscribe_account(
        self,
        pubkey: str,
        callback: EventCallback,
        *,
        commitment: str = "confirmed",
        encoding: str = "jsonParsed",
    ) -> int:
        """Subscribe to account updates for ``pubkey``.

        Returns a local subscription id usable with :meth:`unsubscribe`.
        The callback is invoked with the raw ``params`` payload from
        the Helius notification (``{"result": ..., "subscription": ...}``).
        """
        params: list[Any] = [pubkey, {"commitment": commitment, "encoding": encoding}]
        return await self._register("accountSubscribe", params, callback)

    async def subscribe_program(
        self,
        program_id: str,
        callback: EventCallback,
        *,
        commitment: str = "confirmed",
        encoding: str = "jsonParsed",
    ) -> int:
        """Subscribe to a program's account changes (e.g. SPL Token)."""
        params: list[Any] = [program_id, {"commitment": commitment, "encoding": encoding}]
        return await self._register("programSubscribe", params, callback)

    async def unsubscribe(self, sub_id: int) -> None:
        """Drop a subscription. Idempotent — re-calling with the same
        id is a no-op."""
        sub = self._subscriptions.pop(sub_id, None)
        if sub is None:
            return
        # Drop the server-side mapping if known.
        if sub.server_sub_id is not None:
            self._server_to_local.pop(sub.server_sub_id, None)
        # Best-effort: tell Helius to drop the server-side sub. If the
        # ws is down right now we just let the connection-recycle GC
        # the upstream sub for us.
        if self._ws is not None and sub.server_sub_id is not None:
            unsub_method = sub.method.replace("Subscribe", "Unsubscribe")
            with contextlib.suppress(Exception):
                await self._rpc_call(unsub_method, [sub.server_sub_id])

    def stats(self) -> dict[str, Any]:
        d = self._stats.as_dict()
        d["active_subscriptions"] = len(self._subscriptions)
        d["connected"] = self._connected_event.is_set()
        return d

    # ----- internals -----

    async def _register(
        self,
        method: str,
        params: list[Any],
        callback: EventCallback,
    ) -> int:
        local_id = self._next_local_id
        self._next_local_id += 1
        sub = _Subscription(sub_id=local_id, method=method, params=params, callback=callback)
        self._subscriptions[local_id] = sub
        if self._ws is not None:
            # Connection is up — register immediately. If it's down,
            # the connect loop will pick the sub up on reconnect.
            try:
                await self._send_subscribe(sub)
            except Exception as exc:
                logger.warning(
                    "helius.subscribe_register_deferred method=%s err=%s",
                    method,
                    type(exc).__name__,
                )
        return local_id

    async def _send_subscribe(self, sub: _Subscription) -> None:
        if self._ws is None:
            raise RuntimeError("websocket not connected")
        rpc_id = self._next_rpc_id
        self._next_rpc_id += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rpc_id] = fut
        # Mark this rpc_id as belonging to a subscribe call so _dispatch
        # can install the server_sub_id → local_sub_id mapping the
        # moment the ACK arrives, before any notification routes through.
        self._pending_subscribe[rpc_id] = sub.sub_id
        payload = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": sub.method,
            "params": sub.params,
        }
        await self._ws.send(json.dumps(payload))
        try:
            server_id = await asyncio.wait_for(fut, timeout=15.0)
        finally:
            self._pending.pop(rpc_id, None)
            self._pending_subscribe.pop(rpc_id, None)
        if not isinstance(server_id, int):
            raise RuntimeError(
                f"helius {sub.method} returned non-int subscription id: {server_id!r}"
            )
        # Mapping was already installed in _dispatch; just record on sub.
        sub.server_sub_id = server_id

    async def _rpc_call(self, method: str, params: list[Any]) -> Any:
        if self._ws is None:
            raise RuntimeError("websocket not connected")
        rpc_id = self._next_rpc_id
        self._next_rpc_id += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rpc_id] = fut
        payload = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": method,
            "params": params,
        }
        await self._ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout=15.0)
        finally:
            self._pending.pop(rpc_id, None)

    async def _connect_loop(self) -> None:
        attempt = 0
        last_attempt_ts = time.monotonic()
        while self._running:
            try:
                async with websockets.connect(self._base_ws) as ws:
                    self._ws = ws
                    self._stats.connections += 1
                    if attempt > 0:
                        self._stats.reconnects += 1
                    attempt = 0  # success — reset backoff
                    self._connected_event.set()
                    # Stop the polling fallback if it was running.
                    await self._cancel_polling()
                    # Re-subscribe everything we had registered.
                    await self._resubscribe_all()
                    logger.info("helius.ws_connected subs=%d", len(self._subscriptions))
                    await self._read_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "helius.ws_disconnect err_type=%s msg=%s",
                    type(exc).__name__,
                    # str() on websockets exceptions is safe; key is in
                    # the URL, not in the error body.
                    str(exc)[:200],
                )
            finally:
                self._connected_event.clear()
                self._ws = None
                # Fail every in-flight RPC so callers see the disconnect
                # instead of hanging on the 15s timeout.
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionClosed(None, None))
                self._pending.clear()

            if not self._running:
                return

            attempt += 1
            elapsed_since_first_failure = time.monotonic() - last_attempt_ts
            if (
                elapsed_since_first_failure > self._polling_fallback_after_s
                and self._polling_task is None
                and self._subscriptions
            ):
                logger.warning(
                    "helius.polling_fallback_engaged elapsed_s=%.1f subs=%d",
                    elapsed_since_first_failure,
                    len(self._subscriptions),
                )
                self._stats.polling_fallbacks += 1
                self._polling_task = asyncio.create_task(
                    self._poll_loop(), name="helius-poll-fallback"
                )
            sleep_s = _full_jitter_backoff(
                attempt,
                base=self._reconnect_initial_s,
                cap=self._reconnect_max_s,
            )
            await asyncio.sleep(sleep_s)

    async def _resubscribe_all(self) -> None:
        for sub in list(self._subscriptions.values()):
            sub.server_sub_id = None
            try:
                await self._send_subscribe(sub)
            except Exception as exc:
                self._stats.rpc_errors += 1
                logger.warning(
                    "helius.resubscribe_failed method=%s err=%s",
                    sub.method,
                    type(exc).__name__,
                )

    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                self._stats.events_dropped += 1
                logger.warning("helius.bad_json bytes=%d", len(raw))
                continue
            await self._dispatch(msg)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        # RPC response (subscribe ACK / unsubscribe ACK).
        if "id" in msg and ("result" in msg or "error" in msg):
            rpc_id = msg.get("id")
            fut = self._pending.get(rpc_id) if isinstance(rpc_id, int) else None
            if fut is None or fut.done():
                return
            if "error" in msg:
                self._stats.rpc_errors += 1
                fut.set_exception(RuntimeError(f"helius rpc error: {msg.get('error')!r}"))
            else:
                result = msg.get("result")
                # If this ACK was for a subscribe call, install the
                # server_sub_id → local_sub_id mapping NOW, before the
                # caller's await on the future returns. Any notification
                # arriving on the next read tick will route correctly.
                local_sub_id = self._pending_subscribe.get(rpc_id)
                if local_sub_id is not None and isinstance(result, int):
                    self._server_to_local[result] = local_sub_id
                fut.set_result(result)
            return

        # Notification.
        method = msg.get("method")
        if not method or not method.endswith("Notification"):
            return
        params = msg.get("params") or {}
        server_sub_id = params.get("subscription")
        if not isinstance(server_sub_id, int):
            self._stats.events_dropped += 1
            return
        local_id = self._server_to_local.get(server_sub_id)
        if local_id is None:
            # Notification for a sub we don't know about — caller may
            # have unsubscribed but the wire is still draining.
            self._stats.events_dropped += 1
            logger.info(
                "helius.event_dropped reason=unknown_sub server_sub_id=%s",
                server_sub_id,
            )
            return
        sub = self._subscriptions.get(local_id)
        if sub is None:
            self._stats.events_dropped += 1
            return
        self._stats.events_received += 1
        self._stats.last_event_ts = time.time()
        try:
            await sub.callback(params)
        except Exception as exc:
            # Callback failures must not kill the read loop. Surface
            # verbatim per CLAUDE.md ("propagate, don't catch-and-rephrase")
            # via the logger, then keep going.
            logger.exception(
                "helius.callback_error method=%s err=%s",
                sub.method,
                type(exc).__name__,
            )

    async def _cancel_polling(self) -> None:
        if self._polling_task is None:
            return
        self._polling_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._polling_task
        self._polling_task = None

    async def _poll_loop(self) -> None:
        """HTTP polling fallback when the ws stays down > threshold.

        Only handles ``accountSubscribe``-style subs. Program subs are
        not polled — the volume of getProgramAccounts polling would
        crater Helius credits. We log and skip them.
        """
        async with httpx.AsyncClient(timeout=10.0) as http:
            while self._running:
                for sub in list(self._subscriptions.values()):
                    if sub.method != "accountSubscribe":
                        continue
                    pubkey = sub.params[0] if sub.params else None
                    if not pubkey:
                        continue
                    try:
                        resp = await http.post(
                            f"{self._http_base}/?api-key={self._api_key}",
                            json={
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "getAccountInfo",
                                "params": [
                                    pubkey,
                                    {"encoding": "jsonParsed", "commitment": "confirmed"},
                                ],
                            },
                        )
                        body = resp.json()
                    except Exception as exc:
                        self._stats.rpc_errors += 1
                        logger.warning(
                            "helius.poll_failed pubkey=%s err=%s",
                            pubkey,
                            type(exc).__name__,
                        )
                        continue
                    result = body.get("result")
                    if result is None:
                        continue
                    # Shape the payload to look like a ws notification
                    # so callbacks don't need a polling-aware branch.
                    synthetic = {
                        "subscription": sub.server_sub_id or -1,
                        "result": result,
                        "_source": "poll_fallback",
                    }
                    self._stats.events_received += 1
                    self._stats.last_event_ts = time.time()
                    try:
                        await sub.callback(synthetic)
                    except Exception as exc:
                        logger.exception(
                            "helius.poll_callback_error err=%s",
                            type(exc).__name__,
                        )
                await asyncio.sleep(self._polling_interval_s)
