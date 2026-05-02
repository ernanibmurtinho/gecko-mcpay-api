"""S17-DISCOVERY-01 — ArxivSource contract tests.

Pattern C: recorded-fixture style. We don't go near the real Arxiv
mirror in unit tests; the canned Atom payload below mirrors the live
shape closely enough that a regression in `_parse_atom` or
`_entry_to_chunk` will trip these tests.
"""

from __future__ import annotations

import httpx
import pytest
from gecko_core.sources.arxiv.provider import (
    ArxivSource,
    _build_query,
    _entry_to_chunk,
    _idea_has_technical_signal,
    _parse_atom,
    make_arxiv_source,
)

# Minimal Atom payload — two entries, one with a pdf link, one without,
# one with multiple authors. Whitespace mimics the live mirror's
# pretty-printed output.
_FIXTURE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2403.12345v1</id>
    <updated>2026-04-30T00:00:00Z</updated>
    <published>2024-03-18T00:00:00Z</published>
    <title>Agentic Marketplaces and Verifiable Judgment</title>
    <summary>We propose a protocol where autonomous agents trade verdicts.</summary>
    <author><name>Alice Researcher</name></author>
    <author><name>Bob Coauthor</name></author>
    <link href="http://arxiv.org/abs/2403.12345v1" rel="alternate" type="text/html"/>
    <link href="http://arxiv.org/pdf/2403.12345v1" rel="related" type="application/pdf" title="pdf"/>
    <arxiv:primary_category term="cs.MA" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.00001v2</id>
    <updated>2026-04-30T00:00:00Z</updated>
    <published>2024-01-01T00:00:00Z</published>
    <title>Retrieval-Augmented Reasoning with x402 Micropayments</title>
    <summary>An empirical study of paid-evidence orchestration.</summary>
    <author><name>Carol Solo</name></author>
    <link href="http://arxiv.org/abs/2401.00001v2" rel="alternate" type="text/html"/>
    <arxiv:primary_category term="cs.IR" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
</feed>
"""


def test_idea_has_technical_signal_positive() -> None:
    assert _idea_has_technical_signal("Gecko: makes judgment tradeable.")
    assert _idea_has_technical_signal("agentic marketplace for x402 services")
    assert _idea_has_technical_signal("MCP server with RAG")


def test_idea_has_technical_signal_negative() -> None:
    assert not _idea_has_technical_signal("hotel booking app for nomads")
    assert not _idea_has_technical_signal("crm for dentists")


def test_build_query_extracts_long_keywords() -> None:
    query = _build_query("Gecko: makes judgment tradeable. Use judgmentes as a commodity.")
    # Stopwords ("makes", "use", "as", "a") dropped; long discriminators kept.
    assert "judgment" in query
    assert "tradeable" in query
    # AND-chained for arxiv search syntax.
    assert "+AND+" in query


def test_parse_atom_normalizes_entries() -> None:
    entries = _parse_atom(_FIXTURE_ATOM)
    assert len(entries) == 2

    first = entries[0]
    assert first["title"] == "Agentic Marketplaces and Verifiable Judgment"
    assert first["arxiv_id"] == "2403.12345v1"
    assert first["pdf_url"] == "http://arxiv.org/pdf/2403.12345v1"
    assert first["authors"] == ["Alice Researcher", "Bob Coauthor"]
    assert first["primary_category"] == "cs.MA"

    second = entries[1]
    assert second["arxiv_id"] == "2401.00001v2"
    # No pdf link in the second entry — should be empty, not crash.
    assert second["pdf_url"] == ""


def test_parse_atom_handles_malformed() -> None:
    assert _parse_atom("not xml at all") == []
    assert _parse_atom("") == []


def test_entry_to_chunk_shape() -> None:
    entries = _parse_atom(_FIXTURE_ATOM)
    chunk = _entry_to_chunk(entries[0])
    assert chunk["provider_kind"] == "free:arxiv"
    assert chunk["cost_usd"] == "0"
    assert "Agentic Marketplaces" in chunk["text"]
    assert chunk["metadata"]["citation_id"] == "arxiv:2403.12345v1"
    assert chunk["metadata"]["arxiv_id"] == "2403.12345v1"


@pytest.mark.asyncio
async def test_applies_to_categorical() -> None:
    src = ArxivSource()
    assert await src.applies_to(categories={"crypto"}) is True
    assert await src.applies_to(categories={"defi"}) is True
    # Unknown category, no idea text → false.
    assert await src.applies_to(categories=set()) is False


@pytest.mark.asyncio
async def test_applies_to_idea_signal_fallback() -> None:
    src = ArxivSource()
    # No category, but idea-text signal present.
    assert await src.applies_to(categories=set(), idea="agentic marketplace") is True
    assert await src.applies_to(categories=set(), idea="hotel booking for nomads") is False


@pytest.mark.asyncio
async def test_fetch_returns_chunks_from_recorded_atom() -> None:
    """Recorded Atom payload → SourceResult with two chunks."""

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_FIXTURE_ATOM)

    transport = httpx.MockTransport(_handler)
    client = httpx.AsyncClient(transport=transport)

    src = make_arxiv_source(max_results=5, http_client=client)
    result = await src.fetch(idea="agentic marketplace x402 protocol", categories=set())
    await src.aclose()
    await client.aclose()

    assert result.fired is True
    chunks = result.payload["chunks"]
    assert len(chunks) == 2
    assert all(c["provider_kind"] == "free:arxiv" for c in chunks)
    assert result.cost_usd == 0.0


@pytest.mark.asyncio
async def test_fetch_gates_off_for_unrelated_idea() -> None:
    src = ArxivSource()
    result = await src.fetch(idea="sandwich shop loyalty app", categories=set())
    assert result.fired is False


@pytest.mark.asyncio
async def test_fetch_handles_http_error_silently() -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(_handler)
    client = httpx.AsyncClient(transport=transport)
    src = make_arxiv_source(http_client=client)
    result = await src.fetch(idea="agentic protocol research", categories=set())
    await client.aclose()

    assert result.fired is False
    assert result.error is not None
    assert "500" in result.error
