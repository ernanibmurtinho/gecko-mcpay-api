"""Tests for ParagraphProvider — Sprint 14 S14-PARA-01.

Covers:
- Protocol conformance (structural isinstance check, name + kind).
- ``fetch`` happy-path: stubbed Paragraph MCP fixture returns posts;
  provider normalizes to SourceChunks with ``creator_handle`` populated.
- Auth failure (401/403) surfaces as an empty list + warning rather than
  a halt — the dispatcher then marks the provider in ``degraded_sources``.
- Token store roundtrip + env-precedence.
- Health: missing token → unavailable; auth-rejected → unavailable; OK → available.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from gecko_core.ingestion.providers import (
    ProviderHealth,
    SourceChunk,
    SourceProvider,
)
from gecko_core.ingestion.providers.paragraph_provider import (
    DEFAULT_PER_FETCH_USD,
    ParagraphProvider,
    ParagraphTokenStore,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _ok_search_response(request: httpx.Request) -> httpx.Response:
    """Default mock: returns 3 posts, one of which has both handle + wallet."""
    if request.url.path.endswith("/tools/posts.search"):
        return httpx.Response(
            200,
            json={
                "posts": [
                    {
                        "url": "https://paragraph.com/@alice/post-1",
                        "title": "On x402 economics",
                        "excerpt": "Paid microtransactions change the agent stack.",
                        "author": "@alice",
                        "author_wallet": "0xabc",
                        "score": 0.88,
                        "published_at": "2026-04-15T00:00:00Z",
                    },
                    {
                        "url": "https://paragraph.com/@bob/post-2",
                        "title": "Founder loops",
                        "excerpt": "How dogfooding shortens validation cycles.",
                        "author": "@bob",
                        "score": 0.72,
                    },
                    {
                        # Missing url — should be skipped
                        "title": "no permalink",
                        "author": "@nobody",
                    },
                ]
            },
        )
    if request.url.path.endswith("/.well-known/oauth-protected-resource"):
        return httpx.Response(200, json={"resource": "paragraph"})
    return httpx.Response(404)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_paragraph_provider_satisfies_protocol() -> None:
    # runtime_checkable Protocol — catches drift if a method is renamed
    # or a class attr removed.
    provider = ParagraphProvider(token_store=ParagraphTokenStore(path=Path("/tmp/never")))
    assert isinstance(provider, SourceProvider)


def test_paragraph_provider_has_required_attrs() -> None:
    provider = ParagraphProvider()
    assert provider.name == "paragraph"
    assert provider.kind == "x402-bazaar"


# ---------------------------------------------------------------------------
# Token store
# ---------------------------------------------------------------------------


def test_token_store_env_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "paragraph_token.json"
    p.write_text(json.dumps({"api_key": "from-disk"}))
    monkeypatch.setenv("PARAGRAPH_API_KEY", "from-env")
    store = ParagraphTokenStore(path=p)
    assert store.load() == "from-env"


def test_token_store_reads_from_disk_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PARAGRAPH_API_KEY", raising=False)
    p = tmp_path / "paragraph_token.json"
    p.write_text(json.dumps({"api_key": "disk-key"}))
    assert ParagraphTokenStore(path=p).load() == "disk-key"


def test_token_store_returns_none_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PARAGRAPH_API_KEY", raising=False)
    store = ParagraphTokenStore(path=tmp_path / "missing.json")
    assert store.load() is None


def test_token_store_save_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PARAGRAPH_API_KEY", raising=False)
    p = tmp_path / "nested" / "paragraph_token.json"
    store = ParagraphTokenStore(path=p)
    store.save("new-key")
    assert p.exists()
    assert ParagraphTokenStore(path=p).load() == "new-key"


def test_token_store_rejects_empty(tmp_path: Path) -> None:
    store = ParagraphTokenStore(path=tmp_path / "p.json")
    with pytest.raises(ValueError):
        store.save("")


# ---------------------------------------------------------------------------
# fetch — happy path + degraded paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_normalizes_posts_with_creator_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PARAGRAPH_API_KEY", "test-token")
    transport = _make_transport(_ok_search_response)
    client = httpx.AsyncClient(base_url="https://mcp.paragraph.com/mcp", transport=transport)
    provider = ParagraphProvider(http_client=client)

    chunks = await provider.fetch("agent-economy ideas")
    await provider.aclose()

    # Two valid posts; the third (missing url) is skipped.
    assert len(chunks) == 2
    for c in chunks:
        assert isinstance(c, SourceChunk)
    handles = [c.metadata.get("creator_handle") for c in chunks]
    assert handles == ["@alice", "@bob"]
    # Wallet threads through when present, empty otherwise.
    wallets = [c.metadata.get("creator_wallet") for c in chunks]
    assert wallets == ["0xabc", ""]
    # Title + excerpt populated on the first chunk.
    assert chunks[0].candidate.title == "On x402 economics"
    assert "microtransactions" in chunks[0].text


@pytest.mark.asyncio
async def test_fetch_returns_empty_when_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PARAGRAPH_API_KEY", raising=False)
    provider = ParagraphProvider(token_store=ParagraphTokenStore(path=tmp_path / "absent"))
    assert await provider.fetch("anything") == []


@pytest.mark.asyncio
async def test_fetch_surfaces_auth_failure_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PARAGRAPH_API_KEY", "stale-token")

    def _401(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    transport = _make_transport(_401)
    client = httpx.AsyncClient(base_url="https://mcp.paragraph.com/mcp", transport=transport)
    provider = ParagraphProvider(http_client=client)
    chunks = await provider.fetch("query")
    await provider.aclose()
    # Auth failure does NOT raise — pipeline stays alive; provider is
    # degraded by the dispatcher.
    assert chunks == []


@pytest.mark.asyncio
async def test_fetch_degrades_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PARAGRAPH_API_KEY", "ok-token")

    def _503(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    transport = _make_transport(_503)
    client = httpx.AsyncClient(base_url="https://mcp.paragraph.com/mcp", transport=transport)
    provider = ParagraphProvider(http_client=client)
    chunks = await provider.fetch("query")
    await provider.aclose()
    assert chunks == []


# ---------------------------------------------------------------------------
# health() reports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_unavailable_when_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PARAGRAPH_API_KEY", raising=False)
    provider = ParagraphProvider(token_store=ParagraphTokenStore(path=tmp_path / "absent"))
    health = await provider.health()
    assert isinstance(health, ProviderHealth)
    assert health.available is False
    assert health.last_error is not None
    assert "PARAGRAPH_API_KEY" in health.last_error or "paragraph login" in health.last_error


@pytest.mark.asyncio
async def test_health_unavailable_when_auth_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PARAGRAPH_API_KEY", "bad-token")

    def _403(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    transport = _make_transport(_403)
    client = httpx.AsyncClient(base_url="https://mcp.paragraph.com/mcp", transport=transport)
    provider = ParagraphProvider(http_client=client)
    health = await provider.health()
    await provider.aclose()
    assert health.available is False
    assert "auth rejected" in (health.last_error or "")


@pytest.mark.asyncio
async def test_health_available_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PARAGRAPH_API_KEY", "good-token")
    transport = _make_transport(_ok_search_response)
    client = httpx.AsyncClient(base_url="https://mcp.paragraph.com/mcp", transport=transport)
    provider = ParagraphProvider(http_client=client)
    health = await provider.health()
    await provider.aclose()
    assert health.available is True


# ---------------------------------------------------------------------------
# cost_estimate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_estimate_falls_back_to_default_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PARAGRAPH_API_KEY", raising=False)
    provider = ParagraphProvider(token_store=ParagraphTokenStore(path=tmp_path / "absent"))
    assert await provider.cost_estimate("q") == DEFAULT_PER_FETCH_USD


@pytest.mark.asyncio
async def test_cost_estimate_reads_pricing_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PARAGRAPH_API_KEY", "good")

    def _pricing(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pricing"):
            return httpx.Response(200, json={"posts.search": 0.07})
        return httpx.Response(404)

    transport = _make_transport(_pricing)
    client = httpx.AsyncClient(base_url="https://mcp.paragraph.com/mcp", transport=transport)
    provider = ParagraphProvider(http_client=client)
    cost = await provider.cost_estimate("q")
    await provider.aclose()
    assert cost == 0.07
