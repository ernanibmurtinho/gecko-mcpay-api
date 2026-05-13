"""S29-#32 — unit tests for the chunk-quality invariant.

Five fail-classes + one happy path. Per CLAUDE.md "Tests should not
over-simulate": these are pure-function tests; no fixtures, no mocks.
"""

from __future__ import annotations

import pytest
from gecko_core.ingestion.chunk_quality import (
    ChunkQualityVerdict,
    assess_chunk_quality,
)


def test_happy_path_substantive_prose_passes() -> None:
    text = (
        "Howard Marks writes about the importance of second-level thinking. "
        "First-level thinking is simplistic and superficial; second-level "
        "thinking is deep, complex, and convoluted. It requires considering "
        "many possible outcomes and their relative probabilities. Investors "
        "who succeed long-term consistently apply second-level reasoning to "
        "their decisions."
    )
    verdict = assess_chunk_quality(text)
    assert verdict == ChunkQualityVerdict(True, "ok")


def test_too_short_rejected() -> None:
    verdict = assess_chunk_quality("short.")
    assert verdict.is_substantive is False
    assert verdict.reason == "too_short"


def test_too_short_whitespace_only_rejected() -> None:
    # 300 chars but all whitespace — strip() collapses to empty.
    verdict = assess_chunk_quality(" " * 300)
    assert verdict.is_substantive is False
    assert verdict.reason == "too_short"


def test_binary_content_rejected() -> None:
    # Mimics what pdftotext does to embedded image streams.
    text = "PDF header text " + "\x00\x01\x02\x03\x04\x05" * 100
    verdict = assess_chunk_quality(text)
    assert verdict.is_substantive is False
    assert verdict.reason == "binary"


def test_repeated_chars_rejected() -> None:
    # Horizontal-rule page from a Berkshire annual report scan.
    text = "-" * 400 + " end of page"
    verdict = assess_chunk_quality(text)
    assert verdict.is_substantive is False
    assert verdict.reason == "repeated_chars"


def test_empty_json_stub_rejected() -> None:
    # Empty-JSON stubs are short by nature — the check runs BEFORE the
    # length floor so the audit gets the specific reason, not the
    # generic "too_short".
    verdict = assess_chunk_quality('{"data":[]}')
    assert verdict.is_substantive is False
    assert verdict.reason == "empty_json"


@pytest.mark.parametrize(
    "stub",
    [
        "{}",
        "[]",
        "null",
        '{"results": []}',
        '{"items":[]}',
        '{"data": []}',
    ],
)
def test_empty_json_stub_variants(stub: str) -> None:
    verdict = assess_chunk_quality(stub)
    assert verdict.is_substantive is False
    assert verdict.reason == "empty_json"


def test_empty_json_with_surrounding_whitespace_rejected() -> None:
    # API responses sometimes include a trailing newline; strip() catches it.
    verdict = assess_chunk_quality('\n  {"data":[]}  \n')
    assert verdict.reason == "empty_json"


def test_empty_text_rejected_as_too_short() -> None:
    assert assess_chunk_quality("").reason == "too_short"


def test_min_chars_kwarg_tunable() -> None:
    text = "exactly fifty characters of legitimate prose here!"
    # Default min_chars=200 → too_short.
    assert assess_chunk_quality(text).reason == "too_short"
    # With a lower floor, the same text passes.
    assert assess_chunk_quality(text, min_chars=40).is_substantive is True
