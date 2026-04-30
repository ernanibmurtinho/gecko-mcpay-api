"""S8-INGEST-01 regression — pipeline glue end-to-end with mocked I/O.

Verifies the extract → chunk → embed → upsert path runs cleanly on a
realistic HTML document with no OpenAI / Tavily / Supabase calls. The
Sprint 7 dogfood crashed every URL with `APIError` because the chunker
emitted whitespace-only pieces that the embedder forwarded to OpenAI; this
test pins the empty-chunk filter and the structured-logging hooks added to
`_process_one`.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from gecko_core.ingestion import pipeline
from gecko_core.models import SourceCandidate


class GlueStore:
    """In-memory stand-in for SessionStore — covers every method the
    pipeline calls. No persistence, no rate limits, no errors."""

    def __init__(self) -> None:
        self.sources: dict[tuple[str, str], UUID] = {}
        self.chunk_rows: list[tuple[UUID, int, int]] = []
        self.counts: dict[UUID, int] = {}
        self.costs: list[tuple[str, float]] = []
        self.cache: dict[tuple[str, int], tuple[str, list[float]]] = {}

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
    ) -> int:
        # Sanity-check the row shape so a future schema drift is caught here
        # rather than at the live Supabase boundary.
        for idx, text, vec in chunks:
            assert isinstance(idx, int)
            assert isinstance(text, str) and text.strip()
            assert isinstance(vec, list) and len(vec) == 1536
        self.chunk_rows.append((source_id, len(chunks), len(chunks[0][2])))
        return len(chunks)

    async def set_source_chunk_count(self, source_id: UUID, count: int) -> None:
        self.counts[source_id] = count

    async def add_cost(self, session_id: UUID, kind: str, amount_usd: float) -> None:
        self.costs.append((kind, amount_usd))

    async def get_chunk_cache(self, url_hash: str, indices: list[int]) -> dict[int, list[float]]:
        return {i: self.cache[(url_hash, i)][1] for i in indices if (url_hash, i) in self.cache}

    async def put_chunk_cache(
        self, url_hash: str, rows: list[tuple[int, str, list[float]]]
    ) -> None:
        for idx, text, vec in rows:
            self.cache[(url_hash, idx)] = (text, vec)


def _candidate(url: str) -> SourceCandidate:
    return SourceCandidate(url=url, type="web", score=0.5)


@pytest.fixture
def fake_embed() -> Any:
    """Embedder mock that asserts no empty inputs ever reach OpenAI."""

    async def _embed(texts: list[str], **_: Any) -> tuple[list[list[float]], int]:
        assert texts, "embedder called with empty list — should be short-circuited"
        for t in texts:
            assert t and t.strip(), "embedder must never receive whitespace-only input"
        return [[0.01] * 1536 for _ in texts], 42 * len(texts)

    return _embed


@pytest.mark.asyncio
async def test_one_html_doc_ingests_clean(
    fake_embed: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """End-to-end glue: real chunker, mocked I/O — no APIError, no shape drift."""
    candidate = _candidate("https://x402.org/ecosystem")
    # Realistic-ish extracted markdown. Long enough to produce multiple chunks.
    body = ("x402 is an open protocol for paying APIs in stablecoins. " * 200).strip()

    async def fake_web(url: str, **_: Any) -> tuple[str, float]:
        return body, 0.0

    store = GlueStore()
    sid = uuid4()
    caplog.set_level(logging.INFO, logger="gecko_core.ingestion.pipeline")

    with (
        patch.object(pipeline.web_extractor, "extract", side_effect=fake_web),
        patch.object(pipeline, "embed_texts", side_effect=fake_embed),
    ):
        result = await pipeline.ingest(sid, [candidate], store)  # type: ignore[arg-type]

    assert result.failed == 0, [o.reason for o in result.outcomes if o.status == "failed"]
    assert result.indexed == 1
    assert result.total_chunks > 0
    # Structured logging hooks fired on every stage.
    messages = [r.getMessage() for r in caplog.records]
    assert any("ingest.start" in m for m in messages)
    assert any("ingest.extracted" in m for m in messages)
    assert any("ingest.chunked" in m for m in messages)
    assert any("ingest.indexed" in m for m in messages)


@pytest.mark.asyncio
async def test_whitespace_only_chunks_are_filtered(fake_embed: Any) -> None:
    """If chunker yields a whitespace-only piece, it must not hit the embedder."""
    candidate = _candidate("https://example.com/whitespace")
    store = GlueStore()
    sid = uuid4()

    async def fake_web(url: str, **_: Any) -> tuple[str, float]:
        return "real article body " * 50, 0.0

    def chunky(_text: str, **_kwargs: Any) -> list[str]:
        # Mix of real chunks and whitespace-only ones — the latter are the
        # exact shape the live chunker produced on Tavily-extracted markdown
        # with consecutive newlines.
        return ["real content alpha", "   \n\t  ", "real content beta", ""]

    with (
        patch.object(pipeline.web_extractor, "extract", side_effect=fake_web),
        patch.object(pipeline, "chunk_text", side_effect=chunky),
        patch.object(pipeline, "embed_texts", side_effect=fake_embed),
    ):
        result = await pipeline.ingest(sid, [candidate], store)  # type: ignore[arg-type]

    assert result.failed == 0
    assert result.indexed == 1
    assert result.total_chunks == 2  # only the two non-empty chunks


@pytest.mark.asyncio
async def test_second_ingest_same_url_skips_embedder() -> None:
    """S8-INGEST-03 — re-ingesting the same URL in a new session must hit
    the embedding cache, not OpenAI.

    The dogfood matrix re-runs Tavily on similar ideas and gets back the
    same handful of authoritative URLs (x402.org, solanacompass.com, ...).
    Without this dedup, every retry re-embeds identical content.
    """
    candidate = _candidate("https://x402.org/ecosystem")
    store = GlueStore()

    async def fake_web(url: str, **_: Any) -> tuple[str, float]:
        return ("x402 protocol overview body. " * 100), 0.0

    embed_calls = 0

    async def counting_embed(texts: list[str], **_: Any) -> tuple[list[list[float]], int]:
        nonlocal embed_calls
        embed_calls += 1
        return [[0.02] * 1536 for _ in texts], 11 * len(texts)

    with (
        patch.object(pipeline.web_extractor, "extract", side_effect=fake_web),
        patch.object(pipeline, "embed_texts", side_effect=counting_embed),
    ):
        first = await pipeline.ingest(uuid4(), [candidate], store)  # type: ignore[arg-type]
        # Second session, same URL — cache must serve every chunk.
        second = await pipeline.ingest(uuid4(), [candidate], store)  # type: ignore[arg-type]

    assert first.indexed == 1 and first.total_chunks > 0
    assert second.indexed == 1 and second.total_chunks == first.total_chunks
    # Embedder called exactly once across both ingests.
    assert embed_calls == 1, f"expected 1 embed call (cache hit), got {embed_calls}"
    # Cache populated with one entry per chunk.
    assert len(store.cache) == first.total_chunks


@pytest.mark.asyncio
async def test_partial_cache_hit_only_embeds_missing() -> None:
    """If only some chunks are cached, embedder gets ONLY the missing pieces."""
    candidate = _candidate("https://x402.org/partial")
    store = GlueStore()
    uhash = pipeline.url_hash(str(candidate.url))

    # Pre-seed cache for chunk_index=0 only.
    store.cache[(uhash, 0)] = ("seeded text", [0.99] * 1536)

    async def fake_web(url: str, **_: Any) -> tuple[str, float]:
        # Big enough that the chunker yields >=2 pieces (512-token cap).
        return ("partial coverage body for x402 protocol. " * 800), 0.0

    embedded_inputs: list[list[str]] = []

    async def counting_embed(texts: list[str], **_: Any) -> tuple[list[list[float]], int]:
        embedded_inputs.append(list(texts))
        return [[0.03] * 1536 for _ in texts], len(texts)

    with (
        patch.object(pipeline.web_extractor, "extract", side_effect=fake_web),
        patch.object(pipeline, "embed_texts", side_effect=counting_embed),
    ):
        result = await pipeline.ingest(uuid4(), [candidate], store)  # type: ignore[arg-type]

    assert result.indexed == 1
    assert len(embedded_inputs) == 1
    # Embedder saw N-1 inputs (the seeded chunk_index=0 was served from cache).
    assert len(embedded_inputs[0]) == result.total_chunks - 1
