"""S33-#80 / S36-#106 — tests for protocol_native chunk rendering.

Covers the data-engineer fixes from the retrieval-quality diagnosis:
  §4 — ``render_chunk_pairs`` returns (display_text, embed_text) where the
       embed text has the shared provenance header STRIPPED.
  §3 — the post-render degenerate-chunk guard drops near-empty chunks
       that the ``_fetch`` wire-level empty guard cannot catch.

And the S36-WS2 part-1 number-first invariant:
  S36-#106 — the numeric payload (APY/TVL/price/funding) LEADS each
       chunk; the provenance header now TRAILS it so the rubric judge's
       truncated snippet window always opens on a citable figure.

Pure-function tests: no Mongo, no embedder, no network — light fakes only.
"""

from __future__ import annotations

import json

from gecko_core.sources.protocol_native import (
    ProtocolEndpoint,
    _is_degenerate_body,
    _provenance_header,
    _strip_provenance_header,
    render_chunk_pairs,
)


def _ep(slug: str = "kamino-vaults") -> ProtocolEndpoint:
    return ProtocolEndpoint(
        protocol="kamino",
        slug=slug,
        url="https://api.kamino.finance/kvaults/vaults",
        description="test endpoint",
        content_kind="mechanism",
    )


# ---------------------------------------------------------------------------
# §4 — embed text != display text
# ---------------------------------------------------------------------------


class TestEmbedDisplayDecoupling:
    def test_embed_text_has_header_stripped(self) -> None:
        ep = _ep("kamino-vaults")
        # A list payload renders one prose chunk per entity.
        body = json.dumps([{"name": "USDC Vault", "apy": 0.062, "tvl": 1_200_000}])
        pairs = render_chunk_pairs(ep, body, "2026-05-16")
        assert pairs, "expected at least one chunk pair"
        for display, embed in pairs:
            header = _provenance_header(ep, "2026-05-16")
            # Display still carries the provenance header somewhere — it is
            # the citable provenance — but S36-#106 moved it off the LEAD.
            assert header in display
            assert not display.startswith(header)
            # Embed text drops it entirely — the whole point of §4.
            assert header not in embed
            # The signal content survives in both.
            assert "USDC Vault" in display
            assert "USDC Vault" in embed

    def test_strip_header_returns_unchanged_when_absent(self) -> None:
        header = "Protocol-native API: kamino/kamino-vaults (as of 2026-05-16)."
        # A segment with no header (e.g. a chunker-split docs tail).
        assert _strip_provenance_header("plain body text", header) == "plain body text"

    def test_strip_header_peels_leading_prefix(self) -> None:
        # Legacy / fallback docs chunks still lead with the header.
        header = "Protocol-native API: kamino/kamino-vaults (as of 2026-05-16)."
        chunk = f"{header} Kamino USDC Vault — APY 6.2%."
        assert _strip_provenance_header(chunk, header) == "Kamino USDC Vault — APY 6.2%."

    def test_strip_header_peels_interior_clause(self) -> None:
        # S36-#106 number-first shape: header is interior, before Source:.
        header = "Protocol-native API: kamino/kamino-vaults (as of 2026-05-16)."
        chunk = f"Kamino USDC Vault: APY 6.2%. {header} Source: https://x.test."
        out = _strip_provenance_header(chunk, header)
        assert header not in out
        assert "Kamino USDC Vault: APY 6.2%." in out
        assert "Source: https://x.test." in out


class TestNumberFirstInvariant:
    """S36-#106 — the citable figure must lead the chunk text."""

    def test_kamino_vault_leads_with_apy(self) -> None:
        ep = _ep("kamino-vaults")
        body = json.dumps([{"name": "JitoSOL-SOL Vault", "apy": 0.0742, "tvl": 12_500_000}])
        pairs = render_chunk_pairs(ep, body, "2026-05-16")
        assert pairs
        display, _embed = pairs[0]
        header = _provenance_header(ep, "2026-05-16")
        # The APY figure appears BEFORE the provenance header.
        apy_pos = display.find("APY")
        header_pos = display.find(header)
        assert apy_pos != -1 and header_pos != -1
        assert apy_pos < header_pos, "numeric payload must precede provenance header"

    def test_number_survives_truncation_window(self) -> None:
        # The judge sees a truncated snippet. With number-first rendering a
        # leading figure survives even a tight 200-char window.
        ep = _ep("kamino-vaults")
        body = json.dumps([{"name": "JitoSOL-SOL Vault", "apy": 0.0742, "tvl": 12_500_000}])
        display, _embed = render_chunk_pairs(ep, body, "2026-05-16")[0]
        truncated = display[:200]
        # The APY value (rendered verbatim by _fmt_num) lands in-window.
        assert "0.0742" in truncated, "APY figure must land inside the snippet window"
        assert "12.50M" in truncated, "TVL figure must land inside the snippet window"

    def test_drift_funding_record_leads_with_rate(self) -> None:
        from gecko_core.sources.protocol_native import render_chunks

        ep = ProtocolEndpoint(
            protocol="drift",
            slug="drift-funding-sol-perp",
            url="https://data.api.drift.trade/market/SOL-PERP/fundingRates",
            description="drift funding",
            content_kind="quote",
        )
        body = json.dumps(
            {"success": True, "records": [{"fundingRate": "0.000575916", "ts": 1700000000}]}
        )
        chunk = render_chunks(ep, body, "2026-05-16")[0]
        truncated = chunk[:200]
        # The raw funding rate lands inside the truncation window — the
        # interpretation note no longer pushes it out.
        assert "0.000575916" in truncated

    def test_jupiter_price_payload_leads_with_price(self) -> None:
        """S37-WS1b — the jupiter price/v3 endpoints return a dict keyed by
        mint address, not a list. Before WS1b that shape fell through to
        ``_render_fallback`` (header-first), so the price figure sat past
        the snippet window. The mint-keyed dict is now number-first."""
        from gecko_core.sources.protocol_native import render_chunks

        ep = ProtocolEndpoint(
            protocol="jupiter",
            slug="jupiter-lst-prices",
            url="https://lite-api.jup.ag/price/v3?ids=mSoL",
            description="Jupiter Lite-API LST snapshot for LST rotation analysis.",
            content_kind="quote",
        )
        # price/v3 — dict keyed by mint, value carries usdPrice/priceChange24h.
        body = json.dumps(
            {
                "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": {
                    "usdPrice": 211.47,
                    "priceChange24h": -1.83,
                    "decimals": 9,
                }
            }
        )
        chunks = render_chunks(ep, body, "2026-05-16")
        assert chunks, "mint-keyed price dict must render at least one chunk"
        chunk = chunks[0]
        assert "{" not in chunk and "}" not in chunk, "price chunk leaked a brace"
        truncated = chunk[:200]
        # The price figure lands inside the truncation window — the
        # provenance header no longer leads.
        assert "211.47" in truncated, "price figure must land in the snippet window"
        header = _provenance_header(ep, "2026-05-16")
        assert not chunk.startswith(header), "price chunk must be number-first"
        assert header in chunk, "provenance must still trail the figure"


# ---------------------------------------------------------------------------
# §3 — degenerate-chunk guard
# ---------------------------------------------------------------------------


class TestDegenerateChunkGuard:
    def test_short_body_is_degenerate(self) -> None:
        # The flagged empty chunk — protocol name + slug, nothing else.
        assert _is_degenerate_body("Kamino kamino-vaults.")

    def test_source_clause_does_not_mask_empty_signal(self) -> None:
        # An entity chunk always carries a trailing "Source: <url>." — it
        # must be stripped before the length check or the guard never fires.
        assert _is_degenerate_body(
            "Kamino kamino-vaults. Source: https://api.kamino.finance/kvaults/vaults."
        )

    def test_substantive_body_is_kept(self) -> None:
        assert not _is_degenerate_body("Kamino USDC Vault — APY 6.2%, TVL 1.20M.")

    def test_long_prose_without_digits_is_kept(self) -> None:
        # Docs-prose mechanism chunks (e.g. Sanctum) are citable even with
        # no digits — the guard is length-only by design.
        assert not _is_degenerate_body(
            "Sanctum unifies Solana liquid staking tokens into a single "
            "liquid layer via the router and infinity pool."
        )

    def test_render_drops_degenerate_entity(self) -> None:
        ep = _ep("kamino-vaults")
        # An entity payload whose entities lack the expected name/field
        # keys — _render_entity_list emits a bare "Kamino kamino-vaults."
        # which the post-render guard must drop.
        body = json.dumps([{"unexpectedKey": "x"}])
        pairs = render_chunk_pairs(ep, body, "2026-05-16")
        assert pairs == [], "near-empty rendered chunk should be dropped"
