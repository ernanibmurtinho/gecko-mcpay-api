"""Helius WS client contract tests.

Pattern C: every wire-protocol client ships with a fixture-shape test
exercising the real wire shape. We spin a tiny in-process
:func:`websockets.serve` instance, speak Helius's JSON-RPC dialect at
the client, then sever the connection to verify backoff + transparent
re-subscribe.

No real network traffic. Marked ``not live``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import websockets
from gecko_core.trade_agent.hotpath.helius import (
    HeliusWebSocketClient,
    _full_jitter_backoff,
)


def test_full_jitter_backoff_envelope() -> None:
    # Should always be in [0, cap].
    for n in range(1, 20):
        v = _full_jitter_backoff(n, base=1.0, cap=60.0)
        assert 0.0 <= v <= 60.0


class _HeliusMockServer:
    """Minimal Helius-shaped JSON-RPC websocket server.

    Tracks every subscribe call so the test can assert re-subscription
    happens after a reconnect.
    """

    def __init__(self) -> None:
        self.subscribe_calls: list[dict[str, Any]] = []
        self._next_sub_id = 1000
        self._server: Any = None
        self.port: int | None = None
        # Set per-connection: if True, the next accepted connection is
        # closed immediately after the first message.
        self.kill_after_first = False
        self.connections_seen = 0
        # Hook: optional async coroutine to push a notification down
        # the wire after a successful subscribe.
        self._notify_after_subscribe: dict[str, Any] | None = None

    async def _handler(self, ws: Any) -> None:
        self.connections_seen += 1
        kill_this_one = self.kill_after_first
        async for raw in ws:
            msg = json.loads(raw)
            method = msg.get("method", "")
            if method.endswith("Subscribe"):
                self.subscribe_calls.append(msg)
                sub_id = self._next_sub_id
                self._next_sub_id += 1
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": msg["id"],
                            "result": sub_id,
                        }
                    )
                )
                if self._notify_after_subscribe is not None:
                    await ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": method.replace("Subscribe", "Notification"),
                                "params": {
                                    "subscription": sub_id,
                                    "result": self._notify_after_subscribe,
                                },
                            }
                        )
                    )
                if kill_this_one:
                    await ws.close()
                    return
            elif method.endswith("Unsubscribe"):
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": True}))

    async def __aenter__(self) -> _HeliusMockServer:
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        # websockets stashes sockets on the server object.
        sock = next(iter(self._server.sockets))
        self.port = sock.getsockname()[1]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._server.close()
        await self._server.wait_closed()


@pytest.mark.asyncio
async def test_subscribe_account_returns_local_id_and_speaks_helius_wire() -> None:
    async with _HeliusMockServer() as srv:
        client = HeliusWebSocketClient(
            api_key="dummy",
            base_ws=f"ws://127.0.0.1:{srv.port}/",
            reconnect_initial_s=0.05,
            reconnect_max_s=0.5,
            polling_fallback_after_s=999.0,
        )
        await client.start()
        try:
            received: list[dict[str, Any]] = []

            async def cb(evt: dict[str, Any]) -> None:
                received.append(evt)

            sub_id = await client.subscribe_account("SomePubkey111", cb)
            assert isinstance(sub_id, int)
            # The mock recorded an accountSubscribe call shaped like
            # the real Helius wire: method + params=[pubkey, opts].
            assert len(srv.subscribe_calls) == 1
            call = srv.subscribe_calls[0]
            assert call["method"] == "accountSubscribe"
            assert call["params"][0] == "SomePubkey111"
            assert "commitment" in call["params"][1]
            stats = client.stats()
            assert stats["active_subscriptions"] == 1
            assert stats["connections"] >= 1
        finally:
            await client.stop()


@pytest.mark.asyncio
async def test_notification_dispatched_to_callback() -> None:
    async with _HeliusMockServer() as srv:
        srv._notify_after_subscribe = {"value": {"lamports": 42}}
        client = HeliusWebSocketClient(
            api_key="dummy",
            base_ws=f"ws://127.0.0.1:{srv.port}/",
            reconnect_initial_s=0.05,
            reconnect_max_s=0.5,
            polling_fallback_after_s=999.0,
        )
        await client.start()
        try:
            seen: list[dict[str, Any]] = []
            done = asyncio.Event()

            async def cb(evt: dict[str, Any]) -> None:
                seen.append(evt)
                done.set()

            await client.subscribe_account("Pubkey1", cb)
            await asyncio.wait_for(done.wait(), timeout=2.0)
            assert seen[0]["result"] == {"value": {"lamports": 42}}
            assert client.stats()["events_received"] == 1
        finally:
            await client.stop()


@pytest.mark.asyncio
async def test_reconnect_resubscribes_transparently() -> None:
    async with _HeliusMockServer() as srv:
        srv.kill_after_first = True
        client = HeliusWebSocketClient(
            api_key="dummy",
            base_ws=f"ws://127.0.0.1:{srv.port}/",
            reconnect_initial_s=0.05,
            reconnect_max_s=0.2,
            polling_fallback_after_s=999.0,
        )
        await client.start()
        try:

            async def cb(_evt: dict[str, Any]) -> None:
                pass

            await client.subscribe_account("Pubkey1", cb)
            # First connection is killed immediately after the
            # subscribe ACK. Allow the reconnect loop to run; after a
            # second successful connection we expect a re-subscribe
            # call recorded by the server.
            for _ in range(40):
                await asyncio.sleep(0.1)
                if len(srv.subscribe_calls) >= 2:
                    # After at least one re-subscribe, stop killing.
                    srv.kill_after_first = False
                    break
            assert len(srv.subscribe_calls) >= 2, (
                f"expected re-subscribe, only saw {len(srv.subscribe_calls)} calls"
            )
            assert client.stats()["reconnects"] >= 1
        finally:
            await client.stop()


@pytest.mark.asyncio
async def test_stop_idempotent_and_clean() -> None:
    async with _HeliusMockServer() as srv:
        client = HeliusWebSocketClient(
            api_key="dummy",
            base_ws=f"ws://127.0.0.1:{srv.port}/",
            reconnect_initial_s=0.05,
            polling_fallback_after_s=999.0,
        )
        await client.start()
        await client.stop()
        await client.stop()  # second stop is a no-op


@pytest.mark.asyncio
async def test_unsubscribe_drops_local_state() -> None:
    async with _HeliusMockServer() as srv:
        client = HeliusWebSocketClient(
            api_key="dummy",
            base_ws=f"ws://127.0.0.1:{srv.port}/",
            reconnect_initial_s=0.05,
            polling_fallback_after_s=999.0,
        )
        await client.start()
        try:

            async def cb(_evt: dict[str, Any]) -> None:
                pass

            sub_id = await client.subscribe_account("Pubkey1", cb)
            assert client.stats()["active_subscriptions"] == 1
            await client.unsubscribe(sub_id)
            assert client.stats()["active_subscriptions"] == 0
            # Idempotent.
            await client.unsubscribe(sub_id)
        finally:
            await client.stop()
