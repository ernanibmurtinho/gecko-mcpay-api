"""Berkshire Hathaway shareholder letters — investor-canon source.

Public-domain content at https://www.berkshirehathaway.com/letters/letters.html.
Buffett's annual letters 1977-present. HTML for 1977-2003, PDF for
2004-2024. We chunk both into the corpus under
``provider_kind="canon_berkshire"`` so the trade-coach can ground
decisions in Buffett's multi-decade case studies on capital allocation,
cycles, debt, and the cost of overpaying.

Design notes:
  - URL list is the full 1977-2024 set (48 letters), spanning two formats.
    Re-scraping the Berkshire index is a small future ticket; today's
    snapshot covers everything currently published.
  - The 1965-1976 letters are not on letters.html (only summary doc,
    not individual letters); skipped for v1.
  - Idempotent: source_id is UUID5 from the letter URL, same as
    canon_marks pattern.

How to invoke:
    uv run python scripts/canon/ingest_berkshire.py
"""

from __future__ import annotations

from dataclasses import dataclass

BRK_HOST = "https://www.berkshirehathaway.com/letters"

# URL patterns are NOT uniform across years. 1977-1997 follow YYYY.html.
# 1998-1999 publish at YYYYhtm.html (because YYYY.html is a stub page
# telling readers to use the PDF). 2000-2002 are PDF-only at YYYYpdf.pdf.
# 2003+ use the modern YYYYltr.pdf pattern. Probed live 2026-05-11.
_LETTER_LOCATORS: tuple[tuple[int, str, str], ...] = tuple(
    [(y, f"{y}.html", "html") for y in range(1977, 1998)]
    + [
        (1998, "1998htm.html", "html"),
        (1999, "1999htm.html", "html"),
        (2000, "2000pdf.pdf", "pdf"),
        (2001, "2001pdf.pdf", "pdf"),
        (2002, "2002pdf.pdf", "pdf"),
        (2003, "2003ltr.pdf", "pdf"),
    ]
    + [(y, f"{y}ltr.pdf", "pdf") for y in range(2004, 2025)]
)


@dataclass(frozen=True)
class BerkshireLetter:
    """One Buffett annual shareholder letter — year + URL + format."""

    year: int
    url: str
    fmt: str  # "html" or "pdf"

    @property
    def slug(self) -> str:
        return f"brk-{self.year}-{self.fmt}"

    @property
    def title_hint(self) -> str:
        return f"Berkshire Hathaway shareholder letter {self.year}"


def letter_urls() -> list[BerkshireLetter]:
    """Return the full 1977-2024 letter list, ready to fetch."""
    return [
        BerkshireLetter(year=y, url=f"{BRK_HOST}/{path}", fmt=fmt)
        for (y, path, fmt) in _LETTER_LOCATORS
    ]
