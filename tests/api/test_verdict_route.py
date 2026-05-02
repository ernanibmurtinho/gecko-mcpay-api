"""S20-VERDICT-URL-IMPL-01 — public ``GET /v1/verdict/{hash}`` route.

The Mongo collection is patched with mongomock (the same fake the
transcript-store tests use) so the assertions stay hermetic. Each test
seeds whatever shape it needs; we never share state across tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

_FULL_HASH = "a" * 12 + "b" * 52  # 12-char prefix shared with the ambiguous test
_OTHER_HASH = "a" * 12 + "c" * 52  # same 12-char prefix → ambiguous case
_UNIQUE_HASH = "f" * 12 + "9" * 52  # disjoint 12-char prefix → redirect case


def _seed_doc(
    *,
    verdict_hash: str,
    idea_text: str = "a hotel guide for Brazil",
    judge_prose: str = "Final verdict: GO\nThe wedge is real.",
    actual_verdict_v2: str = "GO",
    gap: str = "Partial:UX",
    tier: str = "basic",
    created_at: datetime | None = None,
    provider_mix_flag: str | None = "balanced",
) -> dict[str, Any]:
    return {
        "session_id": f"sess-{verdict_hash[:8]}",
        "idea_text": idea_text,
        "judge_prose": judge_prose,
        "parsed_verdict": actual_verdict_v2,
        "actual_verdict": "ship" if actual_verdict_v2 == "GO" else "pivot",
        "actual_verdict_v2": actual_verdict_v2,
        "gap_classification": gap,
        "gap_summary": "",
        "tier": tier,
        "agent_turns": None,
        "verdict_hash": verdict_hash,
        "provider_mix_flag": provider_mix_flag,
        "created_at": created_at or datetime.now(UTC),
    }


@pytest.fixture
def patched_mongo(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch ``_mongo_collection_unsafe`` to a mongomock-backed collection."""
    import mongomock
    from gecko_api.routes import verdict as v

    fake = mongomock.MongoClient()
    coll = fake["gecko_test"]["judge_transcripts"]

    monkeypatch.setattr(v, "_get_collection", lambda: coll)
    return coll


@pytest.fixture
def client() -> TestClient:
    from gecko_api.main import app

    return TestClient(app)


def test_teaser_happy_path(patched_mongo: Any, client: TestClient) -> None:
    """Seed a transcript with a known hash → GET returns the teaser shape."""
    patched_mongo.insert_one(
        _seed_doc(
            verdict_hash=_UNIQUE_HASH,
            idea_text="a hotel guide for Brazil",
            judge_prose="Final verdict: GO\nThe wedge is grounded in real demand signals.",
            actual_verdict_v2="GO",
        )
    )

    resp = client.get(f"/v1/verdict/{_UNIQUE_HASH}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["verdict_hash"] == _UNIQUE_HASH
    assert body["verdict_hash_short"] == f"verdict@{_UNIQUE_HASH[:12]}"
    assert body["idea_text"] == "a hotel guide for Brazil"
    assert body["verdict"] == "GO"
    assert "wedge is grounded" in body["judge_prose_excerpt"]
    assert body["gap_classification"] == "Partial:UX"
    assert body["tier"] == "basic"
    assert body["provider_mix_flag"] == "balanced"
    assert body["is_paywalled"] is True
    assert body["preview_only"] is True
    # Teaser CORS is wide open (the public app-wide CORS is restricted).
    assert resp.headers.get("access-control-allow-origin") == "*"


def test_404_on_missing_hash(patched_mongo: Any, client: TestClient) -> None:
    """Random 64-char hex hash → 404 with the verdict_not_found shape."""
    missing = "0" * 64
    resp = client.get(f"/v1/verdict/{missing}")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "verdict_not_found"
    assert body["hash"] == missing


def test_402_on_detail_full(patched_mongo: Any, client: TestClient) -> None:
    """``?detail=full`` returns the 402 stub regardless of whether the hash
    exists — paywall echoes the hash and surfaces ``price_usdc``."""
    patched_mongo.insert_one(_seed_doc(verdict_hash=_UNIQUE_HASH))

    resp = client.get(f"/v1/verdict/{_UNIQUE_HASH}?detail=full")
    assert resp.status_code == 402
    body = resp.json()
    assert body["error"] == "payment_required"
    assert body["verdict_hash"] == _UNIQUE_HASH
    # Surfaced as a string so the frontend can render dynamic CTA copy.
    assert body["price_usdc"] == "2.50"


def test_short_hash_redirect(patched_mongo: Any, client: TestClient) -> None:
    """12-char prefix → 302 to the canonical full-hash URL."""
    patched_mongo.insert_one(_seed_doc(verdict_hash=_UNIQUE_HASH))

    resp = client.get(
        f"/v1/verdict/{_UNIQUE_HASH[:12]}",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/v1/verdict/{_UNIQUE_HASH}"


def test_short_hash_ambiguous(patched_mongo: Any, client: TestClient) -> None:
    """Two transcripts with identical 12-char prefix → 409 with candidates."""
    now = datetime.now(UTC)
    patched_mongo.insert_one(
        _seed_doc(verdict_hash=_FULL_HASH, created_at=now - timedelta(seconds=10))
    )
    patched_mongo.insert_one(_seed_doc(verdict_hash=_OTHER_HASH, created_at=now))

    short = _FULL_HASH[:12]
    assert short == _OTHER_HASH[:12]  # sanity: shared prefix

    resp = client.get(f"/v1/verdict/{short}")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "verdict_hash_ambiguous"
    assert body["short"] == short
    assert set(body["candidates"]) == {_FULL_HASH, _OTHER_HASH}


def test_404_on_malformed_hash(patched_mongo: Any, client: TestClient) -> None:
    """A clearly malformed hash (wrong length, non-hex) → 404."""
    resp = client.get("/v1/verdict/not-a-hex-hash-at-all")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "verdict_not_found"


def test_rate_limit_teaser(patched_mongo: Any, client: TestClient) -> None:
    """Exceeding 60/min on the teaser surface returns 429.

    slowapi's in-memory backend persists between requests within the same
    process; we patch it to a fresh storage so prior tests don't poison
    the budget. If the limiter isn't wired (refactor regression) the test
    will spot it via the absence of a 429 across 75 calls.
    """
    from gecko_api.rate_limit import limiter

    limiter.reset()
    patched_mongo.insert_one(_seed_doc(verdict_hash=_UNIQUE_HASH))

    saw_429 = False
    for _ in range(75):
        resp = client.get(f"/v1/verdict/{_UNIQUE_HASH}")
        if resp.status_code == 429:
            saw_429 = True
            break
    assert saw_429, "expected 60/min teaser limit to fire within 75 requests"

    # Reset so the rest of the suite isn't poisoned.
    limiter.reset()
