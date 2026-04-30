"""PDF URL adapter (S6-V2-03).

Extracts text from native PDFs (arxiv, dropbox, generic). Uses
``pypdfium2`` for the rendering — small, MIT-licensed, no JVM, no
GPL like ``pdfminer.six``.

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
from urllib.parse import urlparse

import httpx

DEFAULT_TIMEOUT_S = 30.0
MAX_PAGES = 100
MIN_TEXT_CHARS = 500
MAX_DOWNLOAD_BYTES = 50_000_000  # 50 MB cap


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


def _extract_text_sync(content: bytes) -> str:
    """Synchronous pdfium extraction. Lives in a thread via asyncio.to_thread.

    Imports pypdfium2 lazily so the module imports cleanly even when the
    optional dep isn't installed (e.g., in slim build envs).
    """
    try:
        import pypdfium2 as pdfium  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pypdfium2 not installed") from exc

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

    text = await asyncio.to_thread(_extract_text_sync, body)
    if len(text.strip()) < MIN_TEXT_CHARS:
        # Likely scanned / image-only. Pipeline marks as no_content.
        return "", 0.0
    return text, 0.0


__all__ = ["MAX_PAGES", "MIN_TEXT_CHARS", "extract", "matches"]
