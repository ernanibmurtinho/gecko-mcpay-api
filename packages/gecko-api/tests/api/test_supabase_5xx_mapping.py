"""S14-HARDEN-02 — gecko-api maps Supabase 5xx → BACKEND_STORE_UNAVAILABLE.

The dogfood findings F-S14-1 + F-S14-2 surfaced repeated Supabase outages
where a Cloudflare 522 / 525 / 526 (or a postgrest 502/503) bubbled up to
the client as a generic 500. The frames.ag bridge maps generic 500s to
``UPSTREAM_ERROR`` — we want session-store outages to surface as a
distinct ``BACKEND_STORE_UNAVAILABLE`` 503 with a 60s retry hint so the
bridge can apply the right SLO.

Acceptance: a postgrest ``APIError`` with code in {502, 503, 522, 525,
526} produces a 503 from gecko-api with ``error =
BACKEND_STORE_UNAVAILABLE``, ``retry_after = 60``, and a clean message.

Anything else (e.g. PGRST106 — a SQL-shape bug) MUST fall through to the
default 500 path so we don't accidentally mask real bugs as transient.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Boot gecko-api with stub mode + a known wallet so startup succeeds."""
    monkeypatch.setenv("X402_MODE", "stub")
    monkeypatch.setenv(
        "GECKO_WALLET_ADDRESS",
        "GeckoSoanaWaetAddressBase58Sampe1234567xyz9",
    )
    monkeypatch.setenv(
        "GECKO_WALLET_ADDRESS_BASE",
        "0x1234567890abcdef1234567890abcdef12345678",
    )
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)
    from gecko_api.main import app as _app

    # Inject a synthetic raise-only route so we can drive the handler from
    # a TestClient without hitting any real Supabase or business logic.
    @_app.get("/_test/raise_supabase")
    async def _raise_supabase(code: str = "522") -> dict[str, str]:
        raise APIError({"code": code, "message": f"upstream {code}"})

    @_app.get("/_test/raise_pgrst")
    async def _raise_pgrst() -> dict[str, str]:
        # PGRST106 is a SQL-shape bug — must NOT be mapped to BACKEND_STORE.
        raise APIError({"code": "PGRST106", "message": "schema mismatch"})

    yield TestClient(_app, raise_server_exceptions=False)


@pytest.mark.parametrize("code", ["502", "503", "522", "525", "526"])
def test_supabase_5xx_mapped_to_backend_store_unavailable(
    api_client: TestClient, code: str
) -> None:
    response = api_client.get(f"/_test/raise_supabase?code={code}")
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"] == "BACKEND_STORE_UNAVAILABLE"
    assert body["code"] == code
    assert body["retry_after"] == 60
    assert "session store temporarily unavailable" in body["message"]
    # The Retry-After header should mirror the body field so HTTP-aware
    # clients (browsers, curl) honor it without parsing the JSON.
    assert response.headers.get("Retry-After") == "60"


def test_non_backend_store_apierror_falls_through(api_client: TestClient) -> None:
    """A SQL-shape bug must NOT be masked as a transient outage."""
    response = api_client.get("/_test/raise_pgrst")
    # The default FastAPI 500 path with raise_server_exceptions=False
    # produces a 500. The body must NOT carry our BACKEND_STORE token.
    assert response.status_code == 500, response.text
    # If the body is JSON, it should not advertise BACKEND_STORE_UNAVAILABLE.
    try:
        body = response.json()
        assert body.get("error") != "BACKEND_STORE_UNAVAILABLE"
    except ValueError:
        # Non-JSON body is fine — the contract is "not BACKEND_STORE".
        pass


def test_backend_store_handler_is_distinct_from_upstream_error(
    api_client: TestClient,
) -> None:
    """The frames.ag bridge keys off ``error``; assert the token is
    BACKEND_STORE_UNAVAILABLE and not UPSTREAM_ERROR."""
    response = api_client.get("/_test/raise_supabase?code=522")
    body = response.json()
    assert body["error"] != "UPSTREAM_ERROR"
    assert body["error"] == "BACKEND_STORE_UNAVAILABLE"
