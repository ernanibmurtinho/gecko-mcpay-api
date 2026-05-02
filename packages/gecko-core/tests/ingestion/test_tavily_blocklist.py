"""S17-DISCOVERY-01 — Tavily dictionary-noise blocklist tests.

These guard the post-fetch URL filter that drops wordlist / dictionary
dumps before they reach the candidate list. The 2026-04-30 e2e burned
4-of-8 ingestion attempts on these URLs; the cost of a false-positive
here (a missed legitimate page) is much smaller than letting `dict.dat`
into the index.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from gecko_core.ingestion.discovery import (
    _is_blocked_dictionary_url,
    discover,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        # Curated host blocklist
        ("https://snap.berkeley.edu/some/page.html", True),
        ("https://stat.wharton.upenn.edu/~buja/STAT-961/dict.dat", True),
        ("https://people.brandeis.edu/~ifinkel/wordlist.txt", True),
        ("https://april.eecs.umich.edu/courses/eecs281_w20/wiki/", True),
        # Extension blocklist
        ("https://example.com/data/words.txt", True),
        ("https://example.com/dump.dat", True),
        ("https://example.com/file.csv", True),
        ("https://example.com/index.json", True),
        # Path-fragment blocklist
        ("https://example.com/dictionary/english.html", True),
        ("https://example.com/path/wordlist/main.html", True),
        ("https://example.com/4letter-words/index", True),
        # Should NOT match — legitimate URLs
        ("https://news.ycombinator.com/item?id=123", False),
        ("https://example.com/blog/post.html", False),
        ("https://arxiv.org/abs/2403.12345", False),
        ("https://github.com/some/repo/blob/main/words.md", False),
        # PDF — explicitly NOT blocked (S17-INGEST-FALLBACK-01 parses these).
        ("https://example.com/paper.pdf", False),
    ],
)
def test_is_blocked_dictionary_url(url: str, expected: bool) -> None:
    assert _is_blocked_dictionary_url(url) is expected


def test_blocklist_handles_empty_url() -> None:
    # Empty URL is considered "blocked" (drop it from results).
    assert _is_blocked_dictionary_url("") is True


@pytest.mark.asyncio
async def test_discover_filters_blocked_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dictionary URLs returned by Tavily must not appear in candidates."""
    # Force a known quota so over-fetch math is predictable.
    monkeypatch.setenv("GECKO_PROVIDER_QUOTA_TAVILY", "3")
    monkeypatch.setenv("TAVILY_API_KEY", "fake-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")

    fake_results: list[dict[str, Any]] = [
        {"url": "https://stat.wharton.upenn.edu/~buja/dict.dat", "title": "dict", "score": 0.99},
        {"url": "https://snap.berkeley.edu/wordlist.html", "title": "snap", "score": 0.95},
        {"url": "https://news.ycombinator.com/item?id=1", "title": "hn", "score": 0.80},
        {"url": "https://example.com/blog.html", "title": "blog", "score": 0.70},
        {"url": "https://example.com/words.txt", "title": "words", "score": 0.60},
        {"url": "https://founder-blog.com/post", "title": "founder", "score": 0.55},
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
        candidates = await discover("agentic marketplace research")

    urls = [str(c.url) for c in candidates]
    # Blocked URLs must be gone.
    assert not any("dict.dat" in u for u in urls)
    assert not any("snap.berkeley.edu" in u for u in urls)
    assert not any("words.txt" in u for u in urls)
    # Legitimate URLs must remain (and respect the quota=3 cap).
    assert len(candidates) <= 3
    assert any("ycombinator" in u for u in urls)


@pytest.mark.asyncio
async def test_discover_respects_tavily_quota_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GECKO_PROVIDER_QUOTA_TAVILY", "2")
    monkeypatch.setenv("TAVILY_API_KEY", "fake-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")

    fake_results = [
        {"url": f"https://example.com/post-{i}.html", "title": f"post {i}", "score": 0.9 - i * 0.01}
        for i in range(8)
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
        candidates = await discover("some idea")
    assert len(candidates) == 2
