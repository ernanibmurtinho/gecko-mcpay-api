"""PDF URL adapter (S6-V2-03) — fixture-based.

The PDF fixture is generated at module-load time by writing primitive
PDF syntax (no external PDF-writing dependency). We verify pypdfium2
can read it before the tests run, so a fixture regression fails loudly
rather than appearing as an empty extraction.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from gecko_core.sources.dispatcher import discover_adapter
from gecko_core.sources.pdf import MIN_TEXT_CHARS, extract, matches


def _build_text_pdf(text: str) -> bytes:
    """Build a minimal single-page PDF with ``text`` as a Tj content stream.

    Just enough PDF structure (catalog → pages → page → content +
    Helvetica font) for pypdfium2 to extract the literal string.
    """
    stream = b"BT /F1 10 Tf 50 750 Td (" + text.encode("ascii") + b") Tj ET"
    parts: list[bytes] = [b"%PDF-1.4\n"]

    def offset() -> int:
        return sum(len(p) for p in parts)

    xref_positions: list[int] = []
    xref_positions.append(offset())
    parts.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    xref_positions.append(offset())
    parts.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
    xref_positions.append(offset())
    parts.append(
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
    )
    xref_positions.append(offset())
    parts.append(
        b"4 0 obj\n<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream\nendobj\n"
    )
    xref_positions.append(offset())
    parts.append(b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")
    xref_offset = offset()
    xref = b"xref\n0 6\n0000000000 65535 f \n"
    for p in xref_positions:
        xref += f"{p:010d} 00000 n \n".encode()
    parts.append(xref)
    parts.append(
        b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode()
        + b"\n%%EOF\n"
    )
    return b"".join(parts)


# ~810 chars of extractable body — well above MIN_TEXT_CHARS.
PDF_FIXTURE_TEXT = ("Gecko adapter test corpus. " * 30).strip()
PDF_FIXTURE = _build_text_pdf(PDF_FIXTURE_TEXT)
# Tiny fixture — used to assert MIN_TEXT_CHARS rejection.
PDF_TINY_FIXTURE = _build_text_pdf("short.")


def test_matches_recognises_extensions_and_arxiv() -> None:
    assert matches("https://example.com/paper.pdf")
    assert matches("https://arxiv.org/abs/2301.00001")
    assert matches("https://arxiv.org/pdf/2301.00001.pdf")
    assert not matches("https://example.com/paper.html")
    assert not matches("ftp://example.com/paper.pdf")


@pytest.mark.asyncio
async def test_extract_returns_text_for_native_pdf() -> None:
    url = "https://example.com/papers/test.pdf"
    with respx.mock(assert_all_called=True) as router:
        router.get(url).mock(
            return_value=httpx.Response(
                200, content=PDF_FIXTURE, headers={"Content-Type": "application/pdf"}
            )
        )
        async with httpx.AsyncClient() as client:
            text, cost = await extract(url, client=client)

    assert cost == 0.0
    assert "Gecko adapter test corpus" in text
    assert len(text.strip()) >= MIN_TEXT_CHARS


@pytest.mark.asyncio
async def test_extract_rejects_short_text_as_scanned() -> None:
    """Tiny PDF (< MIN_TEXT_CHARS extractable) is treated as scanned."""
    url = "https://example.com/scanned.pdf"
    with respx.mock(assert_all_called=True) as router:
        router.get(url).mock(return_value=httpx.Response(200, content=PDF_TINY_FIXTURE))
        async with httpx.AsyncClient() as client:
            text, cost = await extract(url, client=client)

    assert text == ""
    assert cost == 0.0


@pytest.mark.asyncio
async def test_extract_rewrites_arxiv_abs_to_pdf() -> None:
    """``arxiv.org/abs/<id>`` should fetch ``arxiv.org/pdf/<id>.pdf``."""
    abs_url = "https://arxiv.org/abs/2301.99999"
    pdf_url = "https://arxiv.org/pdf/2301.99999.pdf"
    with respx.mock(assert_all_called=True) as router:
        router.get(pdf_url).mock(return_value=httpx.Response(200, content=PDF_FIXTURE))
        async with httpx.AsyncClient() as client:
            text, _ = await extract(abs_url, client=client)
    assert "Gecko adapter test corpus" in text


def test_discover_adapter_picks_pdf() -> None:
    pick = discover_adapter("https://example.com/foo.pdf")
    assert pick is not None
    name, _fn = pick
    assert name == "pdf"
