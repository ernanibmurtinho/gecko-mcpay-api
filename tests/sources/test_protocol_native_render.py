"""S33-#80 — tests for protocol_native embed/display decoupling + guards.

Covers the two data-engineer fixes from the retrieval-quality diagnosis:
  §4 — ``render_chunk_pairs`` returns (display_text, embed_text) where the
       embed text has the shared provenance header STRIPPED.
  §3 — the post-render degenerate-chunk guard drops near-empty chunks
       that the ``_fetch`` wire-level empty guard cannot catch.

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
            # Display keeps the provenance header for citation.
            assert display.startswith(header)
            # Embed text drops it — the whole point of §4.
            assert not embed.startswith(header)
            assert header not in embed
            # The signal content survives in both.
            assert "USDC Vault" in display
            assert "USDC Vault" in embed

    def test_strip_header_returns_unchanged_when_absent(self) -> None:
        header = "Protocol-native API: kamino/kamino-vaults (as of 2026-05-16)."
        # A segment with no header (e.g. a chunker-split docs tail).
        assert _strip_provenance_header("plain body text", header) == "plain body text"

    def test_strip_header_peels_exact_prefix(self) -> None:
        header = "Protocol-native API: kamino/kamino-vaults (as of 2026-05-16)."
        chunk = f"{header} Kamino USDC Vault — APY 6.2%."
        assert _strip_provenance_header(chunk, header) == "Kamino USDC Vault — APY 6.2%."


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
