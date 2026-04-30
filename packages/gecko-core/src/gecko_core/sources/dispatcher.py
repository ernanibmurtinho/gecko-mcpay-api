"""URL-pattern → adapter dispatcher (S6-V2-01..03).

Lives in its own submodule (not in `sources/__init__.py`) so callers
can import `from gecko_core.sources.dispatcher import discover_adapter`
without forcing the parent package's __init__ to run. The parent
package's name collides with `gecko_core.workflows.list_sources` (which
is re-exported as `gecko_core.sources` for the public SDK surface), so
any code path that lazy-loads the package is sensitive to import
ordering. A dedicated submodule sidesteps that entirely.

When a user pastes a specific URL we route it to the right specialised
extractor (Reddit thread JSON, GitHub README/discussions, native PDFs)
rather than the generic web scraper. The pipeline asks
``discover_adapter(url)`` and gets back an awaitable extractor or None
(fall through to generic web).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

# Adapter signature: async (url) -> (text, cost_usd).
URLAdapter = Callable[[str], Awaitable[tuple[str, float]]]


def discover_adapter(url: str) -> tuple[str, URLAdapter] | None:
    """Return ``(adapter_name, adapter_fn)`` for ``url`` or None.

    Lazily imports the adapter modules so a missing optional dep
    (e.g. ``pypdfium2``) doesn't break import of unrelated code paths.
    Order matters: PDF first (file extension is unambiguous), then
    Reddit, then GitHub. Generic web is the implicit fallback.
    """
    # PDF — checked first: the .pdf suffix is the strongest signal and
    # avoids wasting an HTTP probe on, say, a Reddit-hosted PDF.
    from gecko_core.sources import pdf as _pdf

    if _pdf.matches(url):
        return ("pdf", _pdf.extract)

    from gecko_core.sources import reddit as _reddit

    if _reddit.matches_thread_url(url):
        return ("reddit", _reddit.extract_from_url)

    from gecko_core.sources import github as _gh

    if _gh.matches(url):
        return ("github", _gh.extract)

    return None


__all__ = ["URLAdapter", "discover_adapter"]
