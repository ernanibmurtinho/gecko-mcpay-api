"""Source-discovery quality tests.

Pins the asymmetry between crypto-style ideas (self-disambiguating) and
builder/SaaS ideas (need lexical scaffolding). Mocks Tavily so the test
runs offline and asserts on the *query string* the production code would
ship — no network, no flakiness, no API spend.

Regression context: Sprint 6 dogfood of the Builder Bootstrap Platform
returned dirbuster wordlists + LinkedIn profiles for the idea
"AI co-founder for indie hackers" because the previous query was
literally `f"best sources about: {idea}"` with no domain hints.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from gecko_core.ingestion.discovery import _build_query, discover


def _q(idea: str, cats: set[str]) -> str:
    return _build_query(idea, cats)


class TestBuildQuery:
    """Unit tests for the synchronous query builder — no async, no mocks."""

    def test_crypto_keeps_legacy_form(self) -> None:
        # Crypto ideas already discriminate via "Solana"/"DeFi" keywords.
        # Sprint 2 eval gate measured 1.0 on this; don't regress that.
        q = _q("A Solana lending protocol", {"crypto"})
        assert q == "best sources about: A Solana lending protocol"

    def test_defi_keeps_legacy_form(self) -> None:
        q = _q("A perpetuals exchange on Solana", {"defi"})
        assert q.startswith("best sources about:")

    def test_saas_includes_builder_hints(self) -> None:
        q = _q("A SaaS for managing dental practice billing", {"saas"})
        # Builder-community venues should appear so Tavily ranks
        # founder discussion above SEO-bait pages.
        assert "indie hackers" in q.lower()
        assert "hacker news" in q.lower()
        assert "y combinator" in q.lower()

    def test_devtools_includes_hn(self) -> None:
        q = _q("A test runner that auto-quarantines flaky tests", {"devtools"})
        assert "hacker news" in q.lower()
        assert "indie hackers" in q.lower()

    def test_unknown_falls_back_to_builder_bias(self) -> None:
        # The exact regression: classifier returns ∅ for builder-style
        # ideas → we still bias toward founder venues.
        q = _q("AI co-founder for indie hackers", set())
        low = q.lower()
        assert "indie hackers" in low
        assert "hacker news" in low
        assert "y combinator" in low
        assert "founders" in low

    def test_query_respects_tavily_400_char_cap(self) -> None:
        long_idea = "x " * 300
        q = _q(long_idea, set())
        assert len(q) <= 400


@pytest.mark.asyncio
class TestDiscoverWiring:
    """Integration of classify + query build + Tavily call (Tavily mocked)."""

    async def test_builder_idea_ships_hint_enriched_query(self) -> None:
        """The dogfood regression idea must produce a hint-enriched query."""
        captured: dict[str, Any] = {}

        def fake_search(api_key: str, query: str, max_results: int) -> list[dict[str, Any]]:
            captured["query"] = query
            captured["max_results"] = max_results
            return [
                {
                    "url": "https://www.indiehackers.com/post/ai-cofounder",
                    "title": "AI co-founder discussion",
                    "score": 0.61,
                },
                {
                    "url": "https://news.ycombinator.com/item?id=12345",
                    "title": "Show HN: AI co-founder",
                    "score": 0.55,
                },
            ]

        # Force the unknown-bucket path so this test is robust to seed
        # tweaks — the regression specifically hits ideas the classifier
        # can't place. Patch _safe_classify to return ∅ deterministically.
        with (
            patch(
                "gecko_core.ingestion.discovery._safe_classify",
                return_value=set(),
            ),
            patch(
                "gecko_core.ingestion.discovery._search_sync",
                side_effect=fake_search,
            ),
            patch("gecko_core.ingestion.discovery.get_ingestion_settings") as mock_settings,
        ):
            mock_settings.return_value.tavily_api_key.get_secret_value.return_value = "test-key"
            results = await discover("AI co-founder for indie hackers")

        # The query that actually shipped to Tavily must include builder hints.
        q = captured["query"].lower()
        assert "ai co-founder for indie hackers" in q
        assert "indie hackers" in q
        assert "hacker news" in q

        # Discovery returns sorted candidates with score preserved.
        assert len(results) == 2
        assert results[0].score >= results[1].score
        assert "indiehackers.com" in str(results[0].url) or "ycombinator" in str(results[0].url)

    async def test_crypto_idea_keeps_legacy_query(self) -> None:
        """Don't regress the Sprint 2 baseline (eval gate = 1.0 on crypto)."""
        captured: dict[str, Any] = {}

        def fake_search(api_key: str, query: str, max_results: int) -> list[dict[str, Any]]:
            captured["query"] = query
            return []

        with (
            patch(
                "gecko_core.ingestion.discovery._safe_classify",
                return_value={"crypto"},
            ),
            patch(
                "gecko_core.ingestion.discovery._search_sync",
                side_effect=fake_search,
            ),
            patch("gecko_core.ingestion.discovery.get_ingestion_settings") as mock_settings,
        ):
            mock_settings.return_value.tavily_api_key.get_secret_value.return_value = "test-key"
            await discover("A Solana lending protocol with on-chain credit scores")

        assert captured["query"].startswith("best sources about:")
