"""S17-INGEST-FALLBACK-01 — PDF parser fallback chain unit tests.

Three orthogonal cases:

  1. Good PDF → first parser (pdfium) wins, fallbacks never run.
  2. pdfium-broken / pdfminer-OK → second parser wins.
  3. All three raise → ``UnparseablePDFError`` propagates with the per-
     parser exception class names recorded.

We monkeypatch the three parser callables in ``gecko_core.sources.pdf``
so the test stays hermetic — no real PDF dependencies, no FS, no network.
The chain *order* and *exception accounting* is what we're verifying;
the per-parser implementations are covered by the existing
``tests/sources/test_pdf_adapter.py`` happy path against pypdfium2.
"""

from __future__ import annotations

import pytest
from gecko_core.sources import pdf as pdf_mod


def _bytes() -> bytes:
    # Payload is opaque — fakes never read it.
    return b"%PDF-fake-payload"


def test_first_parser_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def ok_pdfium(_b: bytes) -> str:
        calls.append("pdfium")
        return "pdfium-text"

    def boom_pdfminer(_b: bytes) -> str:  # pragma: no cover — should not run
        calls.append("pdfminer")
        raise RuntimeError("should not be called")

    def boom_pypdf(_b: bytes) -> str:  # pragma: no cover — should not run
        calls.append("pypdf")
        raise RuntimeError("should not be called")

    monkeypatch.setattr(
        pdf_mod,
        "_PARSER_CHAIN",
        (
            ("pypdfium2", ok_pdfium),
            ("pdfminer", boom_pdfminer),
            ("pypdf", boom_pypdf),
        ),
    )

    text = pdf_mod._extract_text_sync(_bytes(), url="https://example.com/x.pdf")
    assert text == "pdfium-text"
    assert calls == ["pdfium"]


def test_second_parser_wins_after_pdfium_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class PdfiumError(Exception):
        pass

    def fail_pdfium(_b: bytes) -> str:
        calls.append("pdfium")
        raise PdfiumError("Failed to load document (PDFium: Data format error).")

    def ok_pdfminer(_b: bytes) -> str:
        calls.append("pdfminer")
        return "pdfminer-text"

    def boom_pypdf(_b: bytes) -> str:  # pragma: no cover — should not run
        calls.append("pypdf")
        raise RuntimeError("should not be called")

    monkeypatch.setattr(
        pdf_mod,
        "_PARSER_CHAIN",
        (
            ("pypdfium2", fail_pdfium),
            ("pdfminer", ok_pdfminer),
            ("pypdf", boom_pypdf),
        ),
    )

    text = pdf_mod._extract_text_sync(_bytes(), url="https://example.com/y.pdf")
    assert text == "pdfminer-text"
    # pdfium was tried first, then pdfminer — pypdf must NOT run.
    assert calls == ["pdfium", "pdfminer"]


def test_all_three_fail_raises_unparseable(monkeypatch: pytest.MonkeyPatch) -> None:
    class PdfiumError(Exception):
        pass

    class PdfMinerError(Exception):
        pass

    class PyPdfError(Exception):
        pass

    def fail_pdfium(_b: bytes) -> str:
        raise PdfiumError("data format error")

    def fail_pdfminer(_b: bytes) -> str:
        raise PdfMinerError("not a pdf")

    def fail_pypdf(_b: bytes) -> str:
        raise PyPdfError("xref broken")

    monkeypatch.setattr(
        pdf_mod,
        "_PARSER_CHAIN",
        (
            ("pypdfium2", fail_pdfium),
            ("pdfminer", fail_pdfminer),
            ("pypdf", fail_pypdf),
        ),
    )

    with pytest.raises(pdf_mod.UnparseablePDFError) as exc_info:
        pdf_mod._extract_text_sync(_bytes(), url="https://example.com/broken.pdf")

    err = exc_info.value
    assert err.url == "https://example.com/broken.pdf"
    # All three parser names are recorded with their exception class names
    # — the audit classifier maps the outer exception to `unparseable_pdf`,
    # but the diagnostic detail lives on `attempted` for postmortem.
    assert ("pypdfium2", "PdfiumError") in err.attempted
    assert ("pdfminer", "PdfMinerError") in err.attempted
    assert ("pypdf", "PyPdfError") in err.attempted


def test_empty_text_from_all_parsers_returns_empty_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every parser succeeds but returns empty (image-only scan
    that survives all three parsers), surface "" so the pipeline marks
    the source as no_content — NOT as unparseable_pdf. The two cases
    have different audit buckets and different operational meanings."""

    def empty(_b: bytes) -> str:
        return ""

    monkeypatch.setattr(
        pdf_mod,
        "_PARSER_CHAIN",
        (
            ("pypdfium2", empty),
            ("pdfminer", empty),
            ("pypdf", empty),
        ),
    )

    text = pdf_mod._extract_text_sync(_bytes(), url="https://example.com/scan.pdf")
    assert text == ""


def test_binary_output_from_first_parser_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S29-#32 — a parser that returns mostly non-printable bytes (the
    Berkshire-1,674-binary-chunks failure mode) must NOT win the chain.
    The fallback should continue to the next parser and prefer readable
    text from a later parser over binary garbage from an earlier one.
    """
    calls: list[str] = []

    def binary_pdfium(_b: bytes) -> str:
        calls.append("pdfium")
        # ~99% non-printable bytes — the failure shape we observed in
        # the Mongo audit.
        return "PDF " + "\x00\x01\x02\x03" * 200

    def good_pdfminer(_b: bytes) -> str:
        calls.append("pdfminer")
        return "Warren Buffett wrote about intrinsic value at length here."

    def boom_pypdf(_b: bytes) -> str:  # pragma: no cover — should not run
        calls.append("pypdf")
        raise RuntimeError("should not be called")

    monkeypatch.setattr(
        pdf_mod,
        "_PARSER_CHAIN",
        (
            ("pypdfium2", binary_pdfium),
            ("pdfminer", good_pdfminer),
            ("pypdf", boom_pypdf),
        ),
    )

    text = pdf_mod._extract_text_sync(_bytes(), url="https://example.com/binary.pdf")
    assert "Buffett" in text
    assert calls == ["pdfium", "pdfminer"]
