"""S12-HARDEN-01 — unauthenticated rate-limit on /research and /plan.

The 30/min/IP cap applies only to the 402-response path (no X-Payment
header). Authenticated paid traffic bypasses entirely — payment is the
rate gate. This test hits /research 50× from one IP without payment and
asserts that 429s appear well before the 50th call.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    os.environ.setdefault("X402_MODE", "stub")
    os.environ.setdefault("GECKO_WALLET_ADDRESS", "STUB_TEST_WALLET")
    # Purge module cache so the per-process rate state is fresh.
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)

    from gecko_api.main import _unpaid_rate_state, app

    _unpaid_rate_state.clear()
    with TestClient(app) as c:
        yield c
    _unpaid_rate_state.clear()


def test_unauthenticated_research_rate_limited_at_30_per_minute(
    client: TestClient,
) -> None:
    """50 unpaid POSTs to /research from one IP → at least one 429 before 50."""
    statuses = []
    for _ in range(50):
        r = client.post("/research", json={"idea": "valid idea " * 5, "tier": "basic"})
        statuses.append(r.status_code)

    assert 429 in statuses, f"expected 429 in first 50 calls, saw {set(statuses)}"
    first_429 = statuses.index(429)
    # 30/min is the cap; first 429 should land at or shortly after that.
    assert first_429 <= 35, f"first 429 at index {first_429}; rate limit too loose"
    # Before the cap kicks in, unpaid calls are 402 (x402 middleware).
    assert all(s == 402 for s in statuses[:first_429])


def test_paid_calls_bypass_rate_limit(client: TestClient) -> None:
    """A request bearing an X-Payment header is not counted toward the cap.

    We can't fake a fully-valid x402 payload here (the stub still validates
    the `accepted` block matches a registered route), so we send a bogus
    payload — x402 will reject with a 402 error rather than 429. The
    point: the rate limiter should NOT short-circuit before x402 sees it,
    which we verify by hitting the endpoint 50× with a bogus X-Payment
    header and asserting 429 never appears.
    """
    statuses = []
    for _ in range(50):
        r = client.post(
            "/research",
            json={"idea": "valid idea " * 5, "tier": "basic"},
            headers={"X-Payment": "bogus-bypass-marker"},
        )
        statuses.append(r.status_code)
    assert 429 not in statuses, (
        f"paid traffic should bypass rate limit, but saw 429 in {set(statuses)}"
    )


def test_unpaid_plan_rate_limited(client: TestClient) -> None:
    """/plan is also rate-limited on unpaid probes."""
    statuses = []
    for _ in range(50):
        r = client.post(
            "/plan",
            json={"session_id": "11111111-1111-1111-1111-111111111111"},
        )
        statuses.append(r.status_code)
    assert 429 in statuses, f"expected 429 on /plan in first 50, saw {set(statuses)}"


def test_healthz_is_not_rate_limited(client: TestClient) -> None:
    """Status / discovery endpoints stay free."""
    for _ in range(50):
        r = client.get("/healthz")
        assert r.status_code == 200
