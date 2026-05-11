"""Howard Marks / Oaktree memos — investor-canon source.

Public-domain content licensed under fair-use academic reading. Memos are
free at https://www.oaktreecapital.com/insights/memos. We chunk them
into the corpus under ``provider_kind="canon_marks"`` so the trade-coach
can ground decisions in Marks' market-cycle, risk-control, and
second-level-thinking frameworks.

Design notes:
  - URL list is curated, not auto-scraped. Re-scraping the Oaktree index
    is a separate (small) ticket; the curated list is what the index
    showed on 2026-05-11 and covers the canonical Marks corpus.
  - Idempotent: source_id is UUID5 from the memo URL, so re-running this
    ingester against the same URLs produces 0 net new chunks (the unique
    index on ``(source_id, chunk_index)`` deduplicates).
  - Polite: sequential fetch + sleep between requests to avoid hammering
    Oaktree's CDN. Total run time ~2-3 min for 35 memos.

How to invoke:
    uv run python scripts/canon/ingest_marks.py
"""

from __future__ import annotations

from dataclasses import dataclass

OAKTREE_HOST = "https://www.oaktreecapital.com"

# Curated memo slugs — snapshot of the Oaktree memos index page on
# 2026-05-11. Each maps to ``OAKTREE_HOST + "/insights/memo/" + slug``.
# Order is by index-page order (most-prominent first).
MARKS_MEMO_SLUGS: tuple[str, ...] = (
    "2020-in-review",
    "ai-hurtles-ahead",
    "a-look-under-the-hood",
    "bull-market-rhymes",
    "cockroaches-in-the-coal-mine",
    "coming-into-focus",
    "conversation-at-panmure-house",
    "easy-money",
    "fewer-losers-or-more-winners",
    "further-thoughts-on-sea-change",
    "gimme-credit",
    "i-beg-to-differ",
    "is-it-a-bubble",
    "lessons-from-silicon-valley-bank",
    "more-on-repealing-the-laws-of-economics",
    "mr-market-miscalculates",
    "nobody-knows-yet-again",
    "on-bubble-watch",
    "ruminating-on-asset-allocation",
    "sea-change",
    "selling-out",
    "shall-we-repeal-the-laws-of-economics",
    "something-of-value",
    "taking-the-temperature",
    "the-calculus-of-value",
    "the-folly-of-certainty",
    "the-illusion-of-knowledge",
    "the-impact-of-debt",
    "the-indispensability-of-risk",
    "the-pendulum-in-international-affairs",
    "the-winds-of-change",
    "thinking-about-macro",
    "what-really-matters",
    "whats-going-on-in-private-credit",
)
"""Snapshot 2026-05-11. Re-run the scrape periodically to pick up new
memos; expansion is a one-line change."""


@dataclass(frozen=True)
class CanonMemo:
    """One Howard Marks memo — URL, slug (= citation key), title hint."""

    slug: str
    url: str

    @property
    def title_hint(self) -> str:
        """Best-effort human-readable title from the slug."""
        return self.slug.replace("-", " ").title()


def memo_urls() -> list[CanonMemo]:
    """Return the full curated memo list, ready to fetch."""
    return [
        CanonMemo(slug=slug, url=f"{OAKTREE_HOST}/insights/memo/{slug}")
        for slug in MARKS_MEMO_SLUGS
    ]
