"""S17-INGEST-FALLBACK-01 — `unparseable_pdf` audit bucket.

When every parser in the PDF fallback chain (pdfium → pdfminer → pypdf)
fails on the same payload, ``UnparseablePDFError`` is raised inside the
extractor. The pipeline must:

  1. Catch it as a clean SKIP (status="skipped", reason="unparseable_pdf").
  2. Emit an audit row with ``error_kind="unparseable_pdf"`` so the
     histogram can separate "PDF is genuinely broken" from "real bug".
  3. NOT log it as a warning — it's an info-level structured event.

We reuse the existing _AuditingFakeStore pattern from
``test_audit_pipeline_emit.py``.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from gecko_core.ingestion import pipeline
from gecko_core.models import SourceCandidate
from gecko_core.sources.pdf import UnparseablePDFError


class _AuditingFakeStore:
    def __init__(self) -> None:
        self.audit_rows: list[dict[str, Any]] = []
        self.sources: dict[tuple[str, str], UUID] = {}

    async def insert_source(
        self, session_id: UUID, url: str, url_hash: str, type_: str
    ) -> UUID | None:
        key = (str(session_id), url_hash)
        if key in self.sources:
            return None
        sid = uuid4()
        self.sources[key] = sid
        return sid

    async def insert_chunks(
        self,
        session_id: UUID,
        source_id: UUID,
        chunks: list[tuple[int, str, list[float]]],
    ) -> int:  # pragma: no cover — unparseable PDFs never reach here
        return len(chunks)

    async def set_source_chunk_count(self, source_id: UUID, count: int) -> None:
        pass

    async def add_cost(self, session_id: UUID, kind: str, amount_usd: float) -> None:
        pass

    async def insert_chunks_write_audit(self, **kwargs: Any) -> None:
        self.audit_rows.append(kwargs)


def _pdf_candidate() -> SourceCandidate:
    return SourceCandidate(url="https://example.com/broken.pdf", type="web", score=0.5)


@pytest.mark.asyncio
async def test_unparseable_pdf_records_audit_and_skips_cleanly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _AuditingFakeStore()
    sid = uuid4()

    async def _raises_unparseable(_candidate: Any) -> tuple[str | None, float, float]:
        raise UnparseablePDFError(
            "https://example.com/broken.pdf",
            [
                ("pypdfium2", "PdfiumError"),
                ("pdfminer", "PDFSyntaxError"),
                ("pypdf", "PdfReadError"),
            ],
        )

    caplog.set_level(logging.INFO, logger="gecko_core.ingestion.pipeline")
    with patch.object(pipeline, "_extract", side_effect=_raises_unparseable):
        result = await pipeline.ingest(sid, [_pdf_candidate()], store)  # type: ignore[arg-type]

    # 1) The source is reported skipped (not failed). The verdict layer
    #    treats skipped/failed differently — broken PDFs shouldn't drag
    #    down "ingestion failure rate" health metrics.
    assert result.skipped == 1
    assert result.failed == 0
    assert result.outcomes[0].status == "skipped"
    assert result.outcomes[0].reason == "unparseable_pdf"

    # 2) Exactly one audit row, bucketed as `unparseable_pdf`.
    assert len(store.audit_rows) == 1
    assert store.audit_rows[0]["error_kind"] == "unparseable_pdf"

    # 3) No WARNING-level "ingest.failed" emission for this case — must
    #    be info-level so the dashboard's failure histogram doesn't
    #    flare on broken PDFs in the wild.
    failed_warns = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "ingest.failed" in r.getMessage()
    ]
    assert failed_warns == [], failed_warns

    # And conversely the structured info event IS present.
    skipped_infos = [
        r for r in caplog.records if "ingest.skipped.unparseable_pdf" in r.getMessage()
    ]
    assert skipped_infos, "expected info-level structured skip log"
