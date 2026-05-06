"""S20-INGEST-BLOCKLIST-01 — name-collision blocklist tests.

Guards the ingestion-time URL filter that drops "academic Gecko" /
competitor / operator-specified URLs before embedding. Cost of a
false-positive (a missed legitimate page) is much smaller than letting
``arxiv.org/html/2602.19218`` contaminate the index — the May 5-6 dogfood
sessions burned 3 of 4 verdicts on that exact URL.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from gecko_core.ingestion.blocklist import (
    BLOCKLIST_PATTERNS,
    annotate_collision_risk,
    flag_collision_risk,
    is_blocked,
)
from gecko_core.ingestion.types import ProviderChunk

# ---------------------------------------------------------------------------
# Unit tests — pattern matching
# ---------------------------------------------------------------------------


def test_blocks_explicit_arxiv_gecko_paper() -> None:
    blocked, pattern = is_blocked("https://arxiv.org/html/2602.19218")
    assert blocked is True
    assert pattern == "arxiv-gecko-2602"


def test_blocks_explicit_arxiv_paper_pdf_variant() -> None:
    # /pdf/ and version suffixes (v2, v3) must also match.
    blocked, pattern = is_blocked("https://arxiv.org/pdf/2602.19218v3")
    assert blocked is True
    assert pattern == "arxiv-gecko-2602"


def test_does_not_block_unrelated_product_page() -> None:
    blocked, pattern = is_blocked("https://example.com/gecko-product")
    assert blocked is False
    assert pattern is None


def test_blocks_broad_arxiv_gecko_pattern() -> None:
    # Any other Arxiv paper whose URL slug contains "gecko" — case
    # insensitive, defence-in-depth for collisions we haven't seen yet.
    blocked, pattern = is_blocked("https://arxiv.org/abs/9999.12345/some-paper-about-gecko-lizards")
    assert blocked is True
    assert pattern == "arxiv-gecko-broad"


def test_env_extras_extend_blocklist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GECKO_INGEST_BLOCKLIST_EXTRA", r"competitor-corp\.com/.*")
    blocked, pattern = is_blocked("https://competitor-corp.com/some/post")
    assert blocked is True
    assert pattern == "env-extra-0"


def test_env_extras_empty_falls_back_to_seeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GECKO_INGEST_BLOCKLIST_EXTRA", "")
    # Seeded pattern still works.
    blocked, _ = is_blocked("https://arxiv.org/html/2602.19218")
    assert blocked is True
    # Random domain is NOT blocked when no env extras are set.
    blocked2, _ = is_blocked("https://example.com/blog/post")
    assert blocked2 is False


def test_empty_url_is_not_blocked() -> None:
    # Empty URL is the caller's problem, not the blocklist's; matches
    # the discovery-layer guard which already drops empty URLs.
    blocked, pattern = is_blocked("")
    assert blocked is False
    assert pattern is None


def test_invalid_env_pattern_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A typoed regex must not break ingestion — the bad entry is dropped.
    monkeypatch.setenv("GECKO_INGEST_BLOCKLIST_EXTRA", "[unclosed")
    blocked, _ = is_blocked("https://example.com/anything")
    assert blocked is False  # falls through to seeded patterns only


def test_canonical_tuple_contains_seed_patterns() -> None:
    # CLAUDE.md Pattern A: canonical source. Test asserts the named
    # entries the runbook references actually exist in the tuple.
    names = {p.name for p in BLOCKLIST_PATTERNS}
    assert "arxiv-gecko-2602" in names
    assert "arxiv-gecko-broad" in names


# ---------------------------------------------------------------------------
# Collision-risk linter
# ---------------------------------------------------------------------------


def test_collision_risk_true_for_offdomain_gecko_mention() -> None:
    assert (
        flag_collision_risk(
            "We propose Gecko, a tool-call simulation environment...",
            "https://arxiv.org/abs/2602.19218",
        )
        is True
    )


def test_collision_risk_false_for_canonical_host() -> None:
    assert (
        flag_collision_risk(
            "Gecko is the builder bootstrap platform.",
            "https://app.geckovision.tech/about",
        )
        is False
    )


def test_collision_risk_false_when_text_omits_gecko() -> None:
    assert (
        flag_collision_risk(
            "An unrelated paper about lizards.",
            "https://example.com/post",
        )
        is False
    )


def test_annotate_collision_risk_sets_metadata_field() -> None:
    chunk = ProviderChunk(
        resource_id="abs:2602.19218",
        chunk_index=0,
        text="Gecko is a simulation environment for tool-calls.",
        metadata={"abs_url": "https://arxiv.org/abs/2602.19218"},
    )
    annotated = annotate_collision_risk([chunk], "https://arxiv.org/abs/2602.19218")
    assert annotated[0].metadata["collision_risk"] is True
    # Original metadata is preserved.
    assert annotated[0].metadata["abs_url"] == "https://arxiv.org/abs/2602.19218"


# ---------------------------------------------------------------------------
# Integration — end-to-end through the discovery filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_filters_blocked_arxiv_gecko_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tavily returns the academic-Gecko URL + a clean URL; only the clean
    URL survives the post-fetch blocklist filter."""
    from gecko_core.ingestion import discovery

    monkeypatch.setenv("GECKO_PROVIDER_QUOTA_TAVILY", "5")
    monkeypatch.setenv("TAVILY_API_KEY", "fake-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")

    fake_results: list[dict[str, Any]] = [
        {
            "url": "https://arxiv.org/html/2602.19218",
            "title": "Gecko: a tool-call simulation environment",
            "score": 0.99,
        },
        {
            "url": "https://news.ycombinator.com/item?id=42",
            "title": "Show HN: builder bootstrap platform",
            "score": 0.88,
        },
    ]

    with (
        patch(
            "gecko_core.ingestion.discovery._search_sync",
            return_value=fake_results,
        ),
        patch(
            "gecko_core.ingestion.discovery._safe_classify",
            return_value=set(),
        ),
    ):
        candidates = await discovery.discover("agentic marketplace research")

    urls = [str(c.url) for c in candidates]
    assert not any("2602.19218" in u for u in urls), (
        "academic Gecko URL must be filtered before reaching the candidate list"
    )
    assert any("ycombinator" in u for u in urls)


# ---------------------------------------------------------------------------
# Integration — provider-chunk path (Arxiv real URL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_provider_chunks_drops_blocklisted_synthetic_uri() -> None:
    """`ingest_provider_chunks` must short-circuit on a blocked URL —
    the embedder, source insert, and chunk insert must all be skipped."""
    from gecko_core.ingestion import pipeline

    class _StoreSpy:
        def __init__(self) -> None:
            self.insert_source_calls = 0
            self.insert_chunks_calls = 0

        async def insert_source(self, **_: Any) -> Any:
            self.insert_source_calls += 1
            return uuid4()

        async def insert_chunks(self, *_: Any, **__: Any) -> int:
            self.insert_chunks_calls += 1
            return 0

        async def set_source_chunk_count(self, *_: Any, **__: Any) -> None:
            return None

        async def add_cost(self, *_: Any, **__: Any) -> None:
            return None

    store = _StoreSpy()
    chunks = [
        ProviderChunk(
            resource_id="abs:2602.19218",
            chunk_index=0,
            text="Gecko is a simulation environment for tool-calls.",
            metadata={},
        )
    ]

    inserted = await pipeline.ingest_provider_chunks(
        session_id=uuid4(),
        provider_kind="arxiv",  # type: ignore[arg-type]
        resource_id="2602.19218",
        synthetic_uri="https://arxiv.org/abs/2602.19218",
        chunks=chunks,
        store=store,  # type: ignore[arg-type]
    )

    assert inserted == 0
    assert store.insert_source_calls == 0, "blocklist must short-circuit before insert_source"
    assert store.insert_chunks_calls == 0
