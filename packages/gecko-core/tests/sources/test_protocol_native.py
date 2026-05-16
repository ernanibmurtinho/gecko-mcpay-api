"""S33-#61/#63/#64 — protocol_native renderer tests.

The trade panel cannot cite raw JSON blobs (root cause of
``citation_relevance=0.25``). The contract these tests enforce:

  * no ``{`` / ``}`` in ANY rendered chunk;
  * list payloads split ONE chunk per entity (a 2-element list → ≥2 chunks);
  * every chunk leads with the ``Protocol-native API:`` provenance header.

Sample payloads are small + inline — no live APIs, no Mongo. Sample shapes
were captured from the real read APIs on 2026-05-16.

Note: ``import gecko_core.sources.protocol_native as m`` fails due to a
pre-existing name-shadow in ``gecko_core/__init__.py``; the explicit
``from ... import X`` form below is the supported path.
"""

from __future__ import annotations

import json

from gecko_core.sources.protocol_native import (
    ProtocolEndpoint,
    _render_tip_floor_payload,
    render_chunk,
    render_chunks,
)

_HEADER_PREFIX = "Protocol-native API:"


def _ep(slug: str, protocol: str = "kamino", kind: str = "mechanism") -> ProtocolEndpoint:
    return ProtocolEndpoint(
        protocol=protocol,
        slug=slug,
        url=f"https://api.example.test/{slug}",
        description=f"{protocol} {slug} test endpoint.",
        content_kind=kind,
    )


def _assert_no_braces(chunks: list[str]) -> None:
    for c in chunks:
        assert "{" not in c, f"chunk leaked open brace: {c!r}"
        assert "}" not in c, f"chunk leaked close brace: {c!r}"


def _assert_header(chunks: list[str]) -> None:
    for c in chunks:
        assert c.startswith(_HEADER_PREFIX), f"missing provenance header: {c[:80]!r}"


# ---------------------------------------------------------------------------
# Kamino — markets / vaults list payloads → per-entity prose.
# ---------------------------------------------------------------------------

_KAMINO_MARKETS = [
    {
        "name": "Main Market",
        "isPrimary": True,
        "description": "Primary market on mainnet",
        "lendingMarket": "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF",
    },
    {
        "name": "JLP Market",
        "isPrimary": False,
        "description": "JLP isolated market",
        "lendingMarket": "DxXdAyU3kCjnyggvHmY5nAwg5cRbbmdyX3npfDMjjMek",
    },
]


def test_kamino_list_payload_splits_per_entity() -> None:
    chunks = render_chunks(_ep("kamino-markets"), json.dumps(_KAMINO_MARKETS), "2026-05-16")
    assert len(chunks) >= 2, "a 2-entity list must yield >=2 chunks"
    _assert_no_braces(chunks)
    _assert_header(chunks)
    # Entity names surface in the prose.
    joined = " ".join(chunks)
    assert "Main Market" in joined
    assert "JLP Market" in joined


def test_kamino_vault_fields_render_as_prose() -> None:
    vaults = [
        {"name": "JLP-USDC", "apy": 0.384, "tvl": 1_200_000, "tokenMint": "5oVNBeEE1234"},
        {"name": "SOL-USDC", "apy": 0.0591, "tvl": 5_800_000, "tokenMint": "OtherMint9876"},
    ]
    chunks = render_chunks(_ep("kamino-vaults"), json.dumps(vaults), "2026-05-16")
    assert len(chunks) == 2
    _assert_no_braces(chunks)
    _assert_header(chunks)
    assert "APY" in chunks[0]
    assert "TVL" in chunks[0]


# ---------------------------------------------------------------------------
# Jupiter — token-tag list payload → per-token prose.
# ---------------------------------------------------------------------------

_JUPITER_TOKENS = [
    {
        "id": "So11111111111111111111111111111111111111112",
        "name": "Wrapped SOL",
        "symbol": "SOL",
        "decimals": 9,
        "holderCount": 3820662,
        "usdPrice": 89.135,
        "mcap": 51530701741.78,
    },
    {
        "id": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "name": "USD Coin",
        "symbol": "USDC",
        "decimals": 6,
        "holderCount": 2100000,
        "usdPrice": 1.0,
        "mcap": 60000000000.0,
    },
]


def test_jupiter_list_payload_splits_per_token() -> None:
    chunks = render_chunks(
        _ep("jupiter-tokens-verified-list", protocol="jupiter"),
        json.dumps(_JUPITER_TOKENS),
        "2026-05-16",
    )
    assert len(chunks) >= 2
    _assert_no_braces(chunks)
    _assert_header(chunks)
    joined = " ".join(chunks)
    assert "Wrapped SOL" in joined
    assert "USD Coin" in joined


# ---------------------------------------------------------------------------
# Jito — tip-floor percentile ladder → a single flattened prose line.
# ---------------------------------------------------------------------------

_TIP_FLOOR = [
    {
        "time": "2026-05-16T03:35:33+00:00",
        "landed_tips_25th_percentile": 2.7362e-06,
        "landed_tips_50th_percentile": 5.517e-06,
        "landed_tips_75th_percentile": 8.1342e-06,
        "landed_tips_95th_percentile": 6.926e-05,
        "landed_tips_99th_percentile": 0.000162477,
        "ema_landed_tips_50th_percentile": 4.1514e-06,
    }
]


def test_tip_floor_flattens_ladder_to_prose() -> None:
    ep = _ep("jito-tip-floor", protocol="jito", kind="quote")
    chunks = render_chunks(ep, json.dumps(_TIP_FLOOR), "2026-05-16")
    assert len(chunks) == 1
    _assert_no_braces(chunks)
    _assert_header(chunks)
    text = chunks[0]
    # The percentile ladder reads as prose, not JSON keys.
    for rung in ("25th", "50th", "75th", "95th", "99th"):
        assert f"{rung} pct" in text, f"missing {rung} rung in {text!r}"
    assert "SOL" in text
    assert "EMA" in text


def test_render_tip_floor_payload_direct_dict() -> None:
    """The helper also accepts a bare dict snapshot (not list-wrapped)."""
    ep = _ep("jito-tip-floor", protocol="jito", kind="quote")
    chunks = _render_tip_floor_payload(ep, _TIP_FLOOR[0], "2026-05-16")
    assert len(chunks) == 1
    _assert_no_braces(chunks)
    _assert_header(chunks)


# ---------------------------------------------------------------------------
# Fallback — docs HTML prose pass-through.
# ---------------------------------------------------------------------------


def test_docs_html_prose_passes_through_with_header() -> None:
    ep = _ep("drift-funding-rates", protocol="drift")
    prose = "Drift funding rates rebalance long and short open interest every hour."
    chunks = render_chunks(ep, prose, "2026-05-16")
    assert len(chunks) == 1
    _assert_no_braces(chunks)
    _assert_header(chunks)
    assert prose in chunks[0]


def test_fallback_flattens_unstructured_json_no_braces() -> None:
    """A JSON dict on an endpoint without a structured renderer still
    flattens to brace-free prose."""
    ep = _ep("kamino-staking-yields")
    # A dict payload routes through _render_kamino_payload's fallback.
    body = {"jitoSOL": {"apy": 0.072}, "mSOL": {"apy": 0.069}}
    chunks = render_chunks(ep, json.dumps(body), "2026-05-16")
    assert chunks
    _assert_no_braces(chunks)
    _assert_header(chunks)


def test_empty_list_payload_does_not_crash() -> None:
    chunks = render_chunks(_ep("kamino-vaults"), "[]", "2026-05-16")
    assert chunks  # falls back, never empty
    _assert_no_braces(chunks)
    _assert_header(chunks)


def test_malformed_json_falls_back_to_prose() -> None:
    ep = _ep("kamino-markets")
    chunks = render_chunks(ep, "{not valid json", "2026-05-16")
    assert chunks
    _assert_header(chunks)


# ---------------------------------------------------------------------------
# Back-compat single-string renderer.
# ---------------------------------------------------------------------------


def test_render_chunk_back_compat_joins_chunks() -> None:
    out = render_chunk(_ep("kamino-markets"), json.dumps(_KAMINO_MARKETS), "2026-05-16")
    assert isinstance(out, str)
    assert "{" not in out and "}" not in out
    assert out.startswith(_HEADER_PREFIX)


def test_provenance_header_carries_protocol_slug_and_date() -> None:
    chunks = render_chunks(_ep("kamino-vaults"), json.dumps(_KAMINO_MARKETS), "2026-05-16")
    assert chunks[0].startswith("Protocol-native API: kamino/kamino-vaults (as of 2026-05-16).")
