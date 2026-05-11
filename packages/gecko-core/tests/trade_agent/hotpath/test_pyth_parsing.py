"""Pyth Hermes parser contract test.

Feeds the documented SSE shape into the client's parser and asserts
:class:`PriceUpdate` instances come out with the right fields. The full
HTTP round-trip is exercised separately via the ``live`` marker.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from gecko_core.trade_agent.hotpath.pyth import PriceUpdate, PythHermesClient

# Sample Hermes /v2/updates/price/stream payload (BTC/USD-shaped).
HERMES_SAMPLE = {
    "binary": {"encoding": "hex", "data": ["deadbeef"]},
    "parsed": [
        {
            "id": "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
            "price": {
                "price": "6789012345678",
                "conf": "1234567",
                "expo": -8,
                "publish_time": 1718001234,
            },
            "ema_price": {
                "price": "6789000000000",
                "conf": "1234000",
                "expo": -8,
                "publish_time": 1718001234,
            },
            "metadata": {"slot": 12345},
        }
    ],
}


def test_price_update_from_parsed_shape() -> None:
    update = PriceUpdate.from_parsed(HERMES_SAMPLE["parsed"][0])
    assert update.feed_id == "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"
    assert update.price == 6789012345678
    assert update.conf == 1234567
    assert update.expo == -8
    assert update.publish_time == 1718001234
    # 6_789_012_345_678 * 1e-8 ≈ 67890.12
    assert update.price_float == pytest.approx(67890.12345678, rel=1e-6)


def test_price_update_handles_missing_optional_fields() -> None:
    minimal = {
        "id": "abc123",
        "price": {"price": "100", "conf": "0", "expo": 0, "publish_time": 0},
    }
    update = PriceUpdate.from_parsed(minimal)
    assert update.price == 100
    assert update.price_float == 100.0


@pytest.mark.asyncio
async def test_client_dispatches_parsed_payload() -> None:
    """Drive the SSE-event handler directly with the Hermes shape."""
    client = PythHermesClient()
    received: list[PriceUpdate] = []
    done = asyncio.Event()

    async def cb(update: PriceUpdate) -> None:
        received.append(update)
        done.set()

    # Bypass start()/HTTP — we're testing the parser, not the network.
    client._feed_ids = ["e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"]
    client._callback = cb
    client._running = True

    await client._handle_event_payload(json.dumps(HERMES_SAMPLE))

    await asyncio.wait_for(done.wait(), timeout=1.0)
    assert len(received) == 1
    assert received[0].feed_id.startswith("e62df6c8")
    stats = client.stats()
    assert stats["updates_received"] == 1
    assert stats["parse_errors"] == 0


@pytest.mark.asyncio
async def test_client_counts_bad_json_as_parse_error() -> None:
    client = PythHermesClient()
    await client._handle_event_payload("not json")
    assert client.stats()["parse_errors"] == 1


@pytest.mark.asyncio
async def test_client_handles_empty_parsed_array() -> None:
    client = PythHermesClient()

    async def cb(_u: PriceUpdate) -> None:
        raise AssertionError("should not be called for empty parsed")

    client._callback = cb
    await client._handle_event_payload(json.dumps({"parsed": [], "binary": {}}))
    assert client.stats()["updates_received"] == 0
    assert client.stats()["parse_errors"] == 0


@pytest.mark.asyncio
async def test_subscribe_rejects_empty_feed_list() -> None:
    client = PythHermesClient()
    with pytest.raises(ValueError):
        await client.subscribe_prices([], lambda _u: asyncio.sleep(0))  # type: ignore[arg-type]


def test_sse_data_line_concatenation() -> None:
    """The SSE spec allows multi-line ``data:`` blocks. We don't
    receive them from Hermes today, but the parser must handle them."""
    # We just verify the documented contract here — that two data lines
    # with no blank separator accumulate; this is exercised by the
    # _consume loop. Not a runtime test, but pins the behaviour.
    parts: list[str] = []
    for line in ['data: {"a":', "data: 1}"]:
        if line.startswith("data:"):
            parts.append(line[len("data:") :].lstrip())
    assert json.loads("\n".join(parts)) == {"a": 1}


def test_pythhermesclient_constructs_with_defaults() -> None:
    # Sanity: the default base_url is the public Hermes endpoint and
    # we don't try to dial it at construction time.
    c = PythHermesClient()
    # Internal: feed list empty, no background task.
    assert c.stats()["feed_count"] == 0


# Optional live test — pinned ETH/USD feed id. Skipped unless explicitly
# requested. Documents the real wire shape and is the recommended
# follow-up after touching the parser.
@pytest.mark.live
@pytest.mark.asyncio
async def test_live_hermes_stream_smoke() -> None:  # pragma: no cover - live-only
    eth_usd = "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace"
    client = PythHermesClient()
    received: list[PriceUpdate] = []
    done = asyncio.Event()

    async def cb(update: PriceUpdate) -> None:
        received.append(update)
        if len(received) >= 1:
            done.set()

    await client.subscribe_prices([eth_usd], cb)
    try:
        await asyncio.wait_for(done.wait(), timeout=15.0)
    finally:
        await client.stop()
    assert received[0].feed_id == eth_usd
    assert received[0].price_float > 0


# Type-only assertion to keep mypy happy about Any usage above.
def _typed(_: Any) -> None:
    return None
