"""Tests for the GET /.well-known/agent-skills/index.json endpoint (S20-B2).

Asserts:
    1. Default (flag unset / "false") returns 503 + DRAFT marker.
    2. Flag = "true" serves the manifest with pay.sh v1.0 shape + headers.
    3. Manifest body matches build_manifest() exactly.
    4. Conditional GET (If-None-Match) yields 304 with no body.
    5. Body is < 64 KB.
    6. Schema-drift smoke: research-market is present.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

# Force stub mode BEFORE importing the app — settings are frozen at import.
os.environ.setdefault("X402_MODE", "stub")
os.environ.setdefault("GECKO_WALLET_ADDRESS", "STUB_WALLET_ADDRESS_NOT_FOR_LIVE")


@pytest.fixture
def client() -> Iterator[TestClient]:
    # Drop any previously-imported gecko_api modules so the app picks up
    # current env on each test (the route reads the flag at request time,
    # but settings frozen at import still need a clean slate).
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)

    from gecko_api.main import app

    with TestClient(app) as c:
        yield c


def test_default_flag_returns_503_draft(client: TestClient) -> None:
    os.environ.pop("GECKO_MANIFEST_PUBLIC", None)
    r = client.get("/.well-known/agent-skills/index.json")
    assert r.status_code == 503
    assert r.headers.get("X-Gecko-Manifest-Status") == "draft"
    assert "DRAFT" in r.json()["detail"] or "draft" in r.json()["detail"]


def test_flag_false_returns_503_draft(client: TestClient) -> None:
    os.environ["GECKO_MANIFEST_PUBLIC"] = "false"
    try:
        r = client.get("/.well-known/agent-skills/index.json")
        assert r.status_code == 503
        assert r.headers.get("X-Gecko-Manifest-Status") == "draft"
    finally:
        os.environ.pop("GECKO_MANIFEST_PUBLIC", None)


def test_flag_true_serves_manifest(client: TestClient) -> None:
    os.environ["GECKO_MANIFEST_PUBLIC"] = "true"
    try:
        from gecko_core.skills.manifest import build_manifest

        r = client.get("/.well-known/agent-skills/index.json")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert r.headers["cache-control"] == "public, max-age=300"
        assert r.headers.get("etag")

        body = r.json()
        assert body == build_manifest()
        assert body["version"] == "1.0"
        assert len(body["skills"]) == 12
        for skill in body["skills"]:
            assert {"name", "title", "description", "url"} <= skill.keys()
    finally:
        os.environ.pop("GECKO_MANIFEST_PUBLIC", None)


def test_conditional_get_returns_304(client: TestClient) -> None:
    os.environ["GECKO_MANIFEST_PUBLIC"] = "true"
    try:
        first = client.get("/.well-known/agent-skills/index.json")
        assert first.status_code == 200
        etag = first.headers["etag"]

        second = client.get(
            "/.well-known/agent-skills/index.json",
            headers={"If-None-Match": etag},
        )
        assert second.status_code == 304
        # Body must be empty on 304.
        assert second.content == b""
        assert second.headers.get("etag") == etag
    finally:
        os.environ.pop("GECKO_MANIFEST_PUBLIC", None)


def test_response_under_64kb(client: TestClient) -> None:
    os.environ["GECKO_MANIFEST_PUBLIC"] = "true"
    try:
        r = client.get("/.well-known/agent-skills/index.json")
        assert r.status_code == 200
        assert len(r.content) < 64 * 1024
    finally:
        os.environ.pop("GECKO_MANIFEST_PUBLIC", None)


def test_research_market_skill_present(client: TestClient) -> None:
    os.environ["GECKO_MANIFEST_PUBLIC"] = "true"
    try:
        r = client.get("/.well-known/agent-skills/index.json")
        assert r.status_code == 200
        body_text = r.content.decode("utf-8")
        assert "research-market" in body_text
        # Also verify via parsed structure.
        names = {s["name"] for s in json.loads(body_text)["skills"]}
        assert "research-market" in names
    finally:
        os.environ.pop("GECKO_MANIFEST_PUBLIC", None)
