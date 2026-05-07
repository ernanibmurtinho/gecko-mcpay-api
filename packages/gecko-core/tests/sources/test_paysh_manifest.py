"""S22-N2 — PayshManifestSource contract tests.

Pattern C: recorded-fixture style. The default test run never touches
the live pay.sh endpoint; the canned manifest pinned 2026-05-07 is the
contract. The ``@pytest.mark.live`` test below is the drift detector,
opted into manually or by a weekly job.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from gecko_core.sources.paysh_manifest import (
    PAYSH_CATALOG_URL,
    PAYSH_HOST,
    PAYSH_MANIFEST_URL,
    PayshCatalog,
    PayshCatalogProvider,
    PayshManifest,
    PayshSkillEntry,
    _entry_to_chunk,
    _format_price_band,
    _idea_has_paysh_signal,
    _is_paysh_url,
    applies_to,
    fetch_catalog,
    fetch_manifest,
    filter_neobank_relevant,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "paysh_manifest_2026_05_07.json"
_CATALOG_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "paysh_catalog_2026_05_07.json"


def _load_fixture_text() -> str:
    return _FIXTURE_PATH.read_text(encoding="utf-8")


def _load_fixture_dict() -> dict:
    return json.loads(_load_fixture_text())


# ---------------------------------------------------------------------------
# Pure-helper unit tests — no orchestrator firing, no http.
# ---------------------------------------------------------------------------


def test_is_paysh_url_accepts_apex_https() -> None:
    assert _is_paysh_url(PAYSH_MANIFEST_URL) is True
    assert _is_paysh_url("https://pay.sh/anything") is True


def test_is_paysh_url_rejects_other_hosts() -> None:
    assert _is_paysh_url("https://evil.example.com/x") is False
    assert _is_paysh_url("https://pay.sh.evil.com/") is False
    assert _is_paysh_url("http://pay.sh/x") is False  # http, not https
    assert _is_paysh_url("file:///etc/passwd") is False
    assert _is_paysh_url("") is False


def test_idea_has_paysh_signal_positive_negative() -> None:
    assert _idea_has_paysh_signal("an x402 marketplace for AI agents")
    assert _idea_has_paysh_signal("integrate Pay MCP into our app")
    assert _idea_has_paysh_signal("micropayment paid api")
    assert not _idea_has_paysh_signal("hotel booking app for nomads")


def test_applies_to_vertical_match() -> None:
    assert applies_to(categories={"neobank"}) is True
    assert applies_to(categories={"dex"}) is True
    assert applies_to(categories={"marketplace"}) is True
    assert applies_to(categories={"fintech"}) is True


def test_applies_to_idea_signal_fallback() -> None:
    assert applies_to(categories=set(), idea="x402 routing layer") is True
    assert applies_to(categories=set(), idea="sandwich shop loyalty app") is False


def test_entry_to_chunk_uses_pydantic_model_construct() -> None:
    """Light test per CLAUDE.md ``feedback_lighter_tests.md`` —
    use ``model_construct`` to skip validation; we're testing the
    normalizer in isolation."""
    entry = PayshSkillEntry.model_construct(
        name="discover-provider",
        title="Discover a provider",
        description="Search the registry.",
        url="https://pay.sh/docs/x.md",
    )
    chunk = _entry_to_chunk(entry)
    assert chunk is not None
    assert chunk["provider_kind"] == "free:paysh_manifest"
    assert chunk["cost_usd"] == "0"
    assert chunk["text"] == "Discover a provider: Search the registry."
    assert chunk["metadata"]["citation_id"] == "paysh_manifest:discover-provider"
    assert chunk["metadata"]["source_url"].endswith("#discover-provider")


def test_entry_to_chunk_returns_none_for_empty_entry() -> None:
    empty = PayshSkillEntry.model_construct(name="", title="", description="", url="")
    assert _entry_to_chunk(empty) is None
    nameless = PayshSkillEntry.model_construct(name="x", title="", description="", url="")
    # ``name`` alone is enough to render a citation+text.
    chunk = _entry_to_chunk(nameless)
    assert chunk is not None
    assert chunk["text"] == "x"


def test_pinned_manifest_validates_against_pydantic_model() -> None:
    """Schema-drift guard: the pinned fixture parses cleanly.

    If pay.sh changes the manifest in a non-backwards-compatible way
    and we re-pin the fixture, this is the test that fails first.
    """
    raw = _load_fixture_dict()
    manifest = PayshManifest.model_validate(raw)
    assert manifest.version == "1.0"
    assert len(manifest.skills) == 5
    # Every entry has at least a name (the URL anchor / citation key).
    assert all(s.name for s in manifest.skills)


# ---------------------------------------------------------------------------
# Pattern C contract test — fixture replay through fetch_manifest.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fixture_normalizes_into_chunks() -> None:
    fixture_body = _load_fixture_text()
    fixture = _load_fixture_dict()

    async def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == PAYSH_HOST
        return httpx.Response(
            200,
            text=fixture_body,
            headers={"content-type": "application/json"},
        )

    transport = httpx.MockTransport(_handler)
    client = httpx.AsyncClient(transport=transport)

    result = await fetch_manifest(client=client)
    await client.aclose()

    assert result.fired is True
    assert result.cost_usd == 0.0
    chunks = result.payload["chunks"]
    assert len(chunks) == len(fixture["skills"]) == 5
    for chunk in chunks:
        assert chunk["provider_kind"] == "free:paysh_manifest"
        assert chunk["text"]
        assert chunk["metadata"]["source_url"].startswith(PAYSH_MANIFEST_URL + "#")
    assert result.payload["skill_count"] == 5
    assert result.payload["manifest_version"] == "1.0"


# ---------------------------------------------------------------------------
# SSRF guard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_manifest_rejects_non_paysh_url() -> None:
    """SSRF guard — passing a non-pay.sh URL must short-circuit before
    any HTTP round-trip. We assert the error string and that the
    handler was never called."""
    handler_called = {"v": False}

    async def _handler(request: httpx.Request) -> httpx.Response:
        handler_called["v"] = True
        return httpx.Response(200, text="{}")

    transport = httpx.MockTransport(_handler)
    client = httpx.AsyncClient(transport=transport)

    result = await fetch_manifest(
        client=client, manifest_url="https://evil.example.com/manifest.json"
    )
    await client.aclose()

    assert result.fired is False
    assert result.error is not None
    assert "SSRF" in result.error
    assert handler_called["v"] is False


# ---------------------------------------------------------------------------
# Live drift detector — skipped by default; opt in with `-m live`.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# S22-N2-EXT — catalog (`/api/catalog`) tests.
# ---------------------------------------------------------------------------


def test_format_price_band_renders_expected_strings() -> None:
    """Pure helper test — light per ``feedback_lighter_tests.md``."""
    free = PayshCatalogProvider.model_construct(
        fqn="x/y",
        endpoint_count=16,
        has_free_tier=True,
        min_price_usd=0.0,
        max_price_usd=0.0,
    )
    assert _format_price_band(free) == ("Pricing: free across 16 endpoints, free-tier available.")

    flat = PayshCatalogProvider.model_construct(
        fqn="x/y",
        endpoint_count=105,
        has_free_tier=False,
        min_price_usd=0.01,
        max_price_usd=0.01,
    )
    assert _format_price_band(flat) == "Pricing: $0.01 across 105 endpoints."

    band = PayshCatalogProvider.model_construct(
        fqn="x/y",
        endpoint_count=141,
        has_free_tier=True,
        min_price_usd=0.001,
        max_price_usd=1.0,
    )
    assert band.endpoint_count == 141
    assert _format_price_band(band) == (
        "Pricing: $0.001 – $1 across 141 endpoints, free-tier available."  # noqa: RUF001
    )


def test_filter_neobank_relevant_pure_function() -> None:
    """Category AND keyword must both match; one alone is insufficient."""
    yes = PayshCatalogProvider.model_construct(
        fqn="a/b",
        category="finance",
        description="market prices and treasury balances",
        use_case="",
    )
    no_category = PayshCatalogProvider.model_construct(
        fqn="c/d",
        category="shopping",
        description="market prices",
        use_case="",
    )
    no_keyword = PayshCatalogProvider.model_construct(
        fqn="e/f",
        category="finance",
        description="hello world",
        use_case="",
    )
    use_case_match = PayshCatalogProvider.model_construct(
        fqn="g/h",
        category="data",
        description="generic",
        use_case="kyc verification flows",
    )
    out = filter_neobank_relevant([yes, no_category, no_keyword, use_case_match])
    fqns = [p.fqn for p in out]
    assert fqns == ["a/b", "g/h"]


@pytest.mark.asyncio
async def test_catalog_fixture_normalizes_into_chunks() -> None:
    fixture_body = _CATALOG_FIXTURE_PATH.read_text(encoding="utf-8")
    fixture = json.loads(fixture_body)

    async def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == PAYSH_HOST
        assert request.url.path == "/api/catalog"
        return httpx.Response(
            200,
            text=fixture_body,
            headers={"content-type": "application/json"},
        )

    transport = httpx.MockTransport(_handler)
    client = httpx.AsyncClient(transport=transport)

    result = await fetch_catalog(client=client)
    await client.aclose()

    assert result.fired is True
    assert result.cost_usd == 0.0
    chunks = result.payload["chunks"]
    assert len(chunks) == fixture["provider_count"] == 72
    for chunk in chunks:
        assert chunk["provider_kind"] == "free:paysh_manifest"
        assert chunk["text"]
        assert chunk["metadata"]["source_url"].startswith(PAYSH_CATALOG_URL + "#")
        # service_url must be available to N4 in metadata.
        assert "service_url" in chunk["metadata"]
    assert result.payload["provider_count"] == 72


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_catalog_matches_pinned_shape() -> None:
    """Drift detector for ``/api/catalog`` — opt-in via ``-m live``."""
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
        resp = await client.get(PAYSH_CATALOG_URL)
    assert resp.status_code == 200
    catalog = PayshCatalog.model_validate(resp.json())
    assert catalog.providers, "live catalog reported zero providers"
    assert all(p.fqn for p in catalog.providers)


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_manifest_matches_pinned_shape() -> None:
    """Hit the live pay.sh well-known endpoint and assert the shape
    still parses against the pinned pydantic model. This is the
    contract-drift detector per Pattern C — run by hand or in a
    weekly job, not in default CI."""
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
        resp = await client.get(PAYSH_MANIFEST_URL)
    assert resp.status_code == 200
    manifest = PayshManifest.model_validate(resp.json())
    assert manifest.skills, "live manifest reported zero skills"
    assert all(s.name for s in manifest.skills)
