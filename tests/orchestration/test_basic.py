"""Unit tests for orchestration.basic.

Mocks: the OpenAI client, the RAG query layer, and the SessionStore. We
exercise the happy path, the validation-retry path, and the
citation-mismatch failure path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from gecko_core.models import SourceInfo
from gecko_core.orchestration import basic as basic_mod
from gecko_core.rag.query import RagChunk


def _make_chunk(url: str, idx: int = 0, sim: float = 0.9) -> RagChunk:
    return RagChunk(
        source_id=uuid4(),
        source_url=url,
        chunk_index=idx,
        text=f"content for {url} chunk {idx}",
        similarity=sim,
    )


def _make_source(url: str) -> SourceInfo:
    return SourceInfo(url=url, type="web", chunk_count=3, indexed_at=datetime.now(UTC))


def _valid_payload(url: str) -> dict[str, Any]:
    cit = [{"source_url": url, "chunk_index": 0, "similarity": 0.9}]
    return {
        "business_plan": {
            "problem": "p",
            "icp": "i",
            "solution": "s",
            "market": "m",
            "business_model": "bm",
            "channels": "c",
            "risks": ["r1"],
            "citations": cit,
        },
        "validation_report": {
            "market_size_signal": "m",
            "competitor_analysis": "c",
            "demand_evidence": "d",
            "risk_flags": ["rf"],
            "citations": cit,
        },
        "prd": {
            "v1_scope": ["a"],
            "v2_scope": ["b"],
            "v3_scope": ["c"],
            "acceptance_criteria": ["ac"],
            "non_functional": ["nf"],
            "success_metrics": ["sm"],
            "citations": cit,
        },
    }


def _mk_openai_client(contents: list[str]) -> MagicMock:
    """Build an AsyncOpenAI-shaped mock for the v3 raw-response surface.

    Orchestration uses `client.chat.completions.with_raw_response.create()` so it
    can read the `x-clawrouter-cost-usd` header. Each call returns a raw object
    whose `.parse()` yields the typical chat-completion shape and whose
    `.headers.get(...)` returns None (no cost header → token-based fallback).
    """
    client = MagicMock()
    completions = MagicMock()

    def _raw(content: str) -> MagicMock:
        parsed = MagicMock(
            choices=[MagicMock(message=MagicMock(content=content))],
            usage=None,
            model="openai/gpt-4o",
        )
        raw = MagicMock()
        raw.parse = MagicMock(return_value=parsed)
        raw.headers = MagicMock()
        raw.headers.get = MagicMock(return_value=None)
        return raw

    raws = [_raw(c) for c in contents]
    with_raw = MagicMock()
    with_raw.create = AsyncMock(side_effect=raws)
    completions.with_raw_response = with_raw
    client.chat = MagicMock(completions=completions)
    return client


def _store_with_sources(urls: list[str]) -> MagicMock:
    store = MagicMock()
    store.list_sources = AsyncMock(return_value=[_make_source(u) for u in urls])
    store.add_cost = AsyncMock(return_value=None)
    return store


@pytest.mark.asyncio
async def test_generate_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    sid: UUID = uuid4()
    url = "https://example.com/a"
    chunks = [_make_chunk(url)]

    async def _fake_rag(*a: Any, **kw: Any) -> list[RagChunk]:
        return chunks

    monkeypatch.setattr(basic_mod, "rag_query", _fake_rag)

    store = _store_with_sources([url])
    client = _mk_openai_client([json.dumps(_valid_payload(url))])

    result = await basic_mod.generate(sid, "an idea", store, openai_client=client)

    assert result.session_id == str(sid)
    assert result.tier == "basic"
    assert result.business_plan.problem == "p"
    assert len(result.sources) == 1


@pytest.mark.asyncio
async def test_generate_retries_on_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid: UUID = uuid4()
    url = "https://example.com/a"

    async def _fake_rag(*a: Any, **kw: Any) -> list[RagChunk]:
        return [_make_chunk(url)]

    monkeypatch.setattr(basic_mod, "rag_query", _fake_rag)

    store = _store_with_sources([url])

    # First response: missing `prd` → validation fails. Second: valid.
    bad = json.dumps({"business_plan": {}, "validation_report": {}})
    good = json.dumps(_valid_payload(url))
    client = _mk_openai_client([bad, good])

    result = await basic_mod.generate(sid, "idea", store, openai_client=client)
    assert result.session_id == str(sid)
    # Both calls happened.
    assert client.chat.completions.with_raw_response.create.await_count == 2


@pytest.mark.asyncio
async def test_generate_drops_unknown_citation_url(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S8-INGEST-02 — unknown citation URLs are dropped + logged, not raised.

    Pre-Sprint 8 this raised `OrchestrationError` and crashed the session.
    The audit (F12) showed agents will cite Tavily-discovered URLs even
    when ingestion failed on them, so the failure mode was deterministic
    on every dogfood run. Graceful degradation is the contract now.
    """
    import logging

    sid: UUID = uuid4()
    indexed_url = "https://example.com/a"
    bogus_url = "https://hallucinated.example.com/x"

    async def _fake_rag(*a: Any, **kw: Any) -> list[RagChunk]:
        return [_make_chunk(indexed_url)]

    monkeypatch.setattr(basic_mod, "rag_query", _fake_rag)

    store = _store_with_sources([indexed_url])
    # Mix one good citation with one hallucinated URL across all three
    # citation lists. The good one survives, the bogus one is dropped.
    payload = _valid_payload(indexed_url)
    for section in ("business_plan", "validation_report", "prd"):
        payload[section]["citations"].append(
            {"source_url": bogus_url, "chunk_index": 0, "similarity": 0.42}
        )
    client = _mk_openai_client([json.dumps(payload)])

    caplog.set_level(logging.WARNING, logger="gecko_core.orchestration.basic")
    result = await basic_mod.generate(sid, "idea", store, openai_client=client)

    # Session completed instead of crashing.
    assert result.session_id == str(sid)
    # Bogus URL filtered out of every citation list.
    for section in (result.business_plan, result.validation_report, result.prd):
        urls = {str(c.source_url) for c in section.citations}
        assert bogus_url not in urls
        assert any(indexed_url in u for u in urls)
    # Each dropped citation logged once with the unknown URL.
    drop_messages = [r.getMessage() for r in caplog.records if "citation.dropped" in r.getMessage()]
    # _valid_payload re-uses the same `cit` list across sections, so the
    # appended bogus URL appears 3x in each section. Assert at least one
    # drop per section + every drop names the bogus URL.
    assert len(drop_messages) >= 3
    assert all(bogus_url in m for m in drop_messages)
    sections_seen = {m.split("where=")[1].split(" ")[0] for m in drop_messages}
    assert sections_seen == {"business_plan", "validation_report", "prd"}


@pytest.mark.asyncio
async def test_generate_drops_all_citations_when_no_sources_indexed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ingestion failed on every URL, the report still ships — without citations."""
    sid: UUID = uuid4()
    cited_url = "https://allium.so/blog/x402-explained"

    async def _fake_rag(*a: Any, **kw: Any) -> list[RagChunk]:
        return []

    monkeypatch.setattr(basic_mod, "rag_query", _fake_rag)

    store = _store_with_sources([])  # no sources successfully indexed
    client = _mk_openai_client([json.dumps(_valid_payload(cited_url))])

    result = await basic_mod.generate(sid, "idea", store, openai_client=client)
    assert result.business_plan.citations == []
    assert result.validation_report.citations == []
    assert result.prd.citations == []


def test_ensure_tier_now_accepts_pro() -> None:
    """Pro tier shipped in Phase 6 — `_ensure_tier` is now a no-op kept for
    backwards compat with any caller that still imports it.
    """
    assert basic_mod._ensure_tier("pro") is None
    assert basic_mod._ensure_tier("basic") is None
