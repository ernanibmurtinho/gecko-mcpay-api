"""PDF URL adapter (S6-V2-03, S17-INGEST-FALLBACK-01).

Extracts text from native PDFs (arxiv, dropbox, generic). Uses a three-step
parser fallback chain: ``pypdfium2`` → ``pdfminer.six`` → ``pypdf``. Each
parser has a different bug surface, so 3-of-3 failure means the PDF is
genuinely broken (image-only scan, encrypted, corrupt header, etc.) and
the adapter raises :class:`UnparseablePDFError` — the pipeline catches that
and records a clean info-level skip with audit ``error_kind=unparseable_pdf``.

Limits:

  * 100-page hard cap. Beyond that the marginal text is rarely worth
    the embedding spend.
  * Reject if extracted text < 500 chars: that's almost always a
    scanned PDF (image-only pages) which would need OCR — out of scope
    for V2. Returns empty string so the pipeline marks the source as
    no_content rather than failing.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Callable
from urllib.parse import urlparse

import httpx

# Single-argument PDF parser signature. Used by the fallback chain.
_Parser = Callable[[bytes], str]

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0
MAX_PAGES = 100
MIN_TEXT_CHARS = 500
MAX_DOWNLOAD_BYTES = 50_000_000  # 50 MB cap


class UnparseablePDFError(RuntimeError):
    """Every parser in the fallback chain failed on the same payload.

    Carries the per-parser exception class names for diagnostics; the
    audit classifier (``gecko_core.ingestion.audit.classify_exception``)
    maps this to the ``unparseable_pdf`` error_kind bucket.
    """

    def __init__(self, url: str, attempted: list[tuple[str, str]]):
        self.url = url
        # list of (parser_name, exception_class_name)
        self.attempted = attempted
        detail = ", ".join(f"{p}={e}" for p, e in attempted)
        super().__init__(f"all PDF parsers failed for {url}: {detail}")


def matches(url: str) -> bool:
    """Heuristic: ends with .pdf, or is an arxiv abs/pdf URL."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if path.endswith(".pdf"):
        return True
    return host.endswith("arxiv.org") and (path.startswith("/abs/") or path.startswith("/pdf/"))


def _normalize_arxiv(url: str) -> str:
    """Rewrite arxiv abs/ → pdf/ so we always download the PDF."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if host.endswith("arxiv.org") and path.startswith("/abs/"):
        new_path = "/pdf/" + path[len("/abs/") :]
        if not new_path.endswith(".pdf"):
            new_path += ".pdf"
        return parsed._replace(path=new_path).geturl()
    return url


def _extract_with_pdfium(content: bytes) -> str:
    """Primary parser. Fastest, native, MIT-licensed. Fails on lightly
    malformed headers with ``PdfiumError``."""
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    pdf = pdfium.PdfDocument(content)
    try:
        n_pages = min(len(pdf), MAX_PAGES)
        pieces: list[str] = []
        for i in range(n_pages):
            page = pdf[i]
            try:
                tp = page.get_textpage()
                try:
                    text = tp.get_text_range()
                finally:
                    tp.close()
            finally:
                page.close()
            if text:
                pieces.append(text)
        return "\n\n".join(pieces)
    finally:
        pdf.close()


def _extract_with_pdfminer(content: bytes) -> str:
    """Pure-Python, more permissive on malformed PDFs. Slower than pdfium.

    We pass MAX_PAGES via ``maxpages`` to keep the wall-clock bounded —
    pdfminer is roughly 5-10x slower than pdfium per page, so a 200-page
    PDF without the cap can take tens of seconds.
    """
    from pdfminer.high_level import extract_text

    return extract_text(io.BytesIO(content), maxpages=MAX_PAGES) or ""


def _extract_with_pypdf(content: bytes) -> str:
    """Third parser — different parsing implementation than pdfminer, so
    failure modes don't fully overlap. Fallback for the small set of
    PDFs that defeat both pdfium and pdfminer."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    pieces: list[str] = []
    n_pages = min(len(reader.pages), MAX_PAGES)
    for i in range(n_pages):
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:
            # pypdf raises a grab-bag of exceptions on per-page issues;
            # skip the page rather than abort the whole document.
            continue
        if text:
            pieces.append(text)
    return "\n\n".join(pieces)


# (parser_name, callable). Order is the fallback chain — first success wins.
_PARSER_CHAIN: tuple[tuple[str, _Parser], ...] = (
    ("pypdfium2", _extract_with_pdfium),
    ("pdfminer", _extract_with_pdfminer),
    ("pypdf", _extract_with_pypdf),
)


def _extract_text_sync(content: bytes, *, url: str) -> str:
    """Run the parser fallback chain. Raises UnparseablePDFError on 3-of-3.

    Empty-string returns from a parser are treated as "this parser
    succeeded but found no extractable text" — we still try the next
    parser in case it can do better. Only the *last* parser's empty
    return is propagated up (caller decides scanned-vs-OK via
    MIN_TEXT_CHARS).
    """
    attempted: list[tuple[str, str]] = []
    last_text = ""
    for name, fn in _PARSER_CHAIN:
        try:
            text = fn(content)
        except Exception as exc:
            attempted.append((name, exc.__class__.__name__))
            logger.info(
                "pdf.parser.failed url=%s parser=%s exc=%s",
                url,
                name,
                exc.__class__.__name__,
            )
            continue
        if text and text.strip():
            if attempted:
                logger.info(
                    "pdf.parser.fallback_succeeded url=%s parser=%s prior=%s",
                    url,
                    name,
                    attempted,
                )
            return text
        # parser returned empty but did not raise — record and move on.
        attempted.append((name, "EmptyText"))
        last_text = text
    # Every parser raised, OR every parser returned empty. If at least
    # one returned empty cleanly, surface that empty string so the caller
    # can mark the source as no_content (image-only scan). Only when
    # *every* parser raised do we treat it as unparseable.
    if all(reason != "EmptyText" for _name, reason in attempted):
        raise UnparseablePDFError(url, attempted)
    return last_text


async def extract(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> tuple[str, float]:
    """Download + extract text from a PDF URL.

    Returns (text, cost_usd). Cost is 0 — direct fetch only. Returns
    ("", 0.0) if extracted text is below the scanned-PDF threshold so
    the pipeline can record a graceful skip.

    Raises :class:`UnparseablePDFError` only when *every* parser in the
    fallback chain failed on the payload — the pipeline maps that to a
    clean ``unparseable_pdf`` audit bucket.
    """
    if not matches(url):
        raise ValueError(f"not a pdf url: {url}")
    # Lazy import — see reddit.py for the same pipeline ↔ sources cycle
    # rationale.
    from gecko_core.ingestion.web import validate_url

    safe = validate_url(_normalize_arxiv(url))

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    assert client is not None
    try:
        resp = await client.get(safe, headers={"User-Agent": "Gecko/0.1 (gecko-mcpay-api)"})
        resp.raise_for_status()
        body = resp.content[:MAX_DOWNLOAD_BYTES]
    finally:
        if owns_client:
            await client.aclose()

    text = await asyncio.to_thread(_extract_text_sync, body, url=safe)
    if len(text.strip()) < MIN_TEXT_CHARS:
        # Likely scanned / image-only. Pipeline marks as no_content.
        return "", 0.0
    return text, 0.0


__all__ = [
    "MAX_PAGES",
    "MIN_TEXT_CHARS",
    "UnparseablePDFError",
    "extract",
    "matches",
]
