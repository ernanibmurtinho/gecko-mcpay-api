"""S14-PUB-01 — publish.new artifact publishing.

Test surfaces:
  1. Stub mode short-circuit returns a ``stub://publish.new/<slug>`` URL
     and never hits the network.
  2. Author-address resolution prefers the override, then env, and
     raises ``PublishNewError`` when neither is configured or the value
     fails the EIP-55-friendly format check.
  3. Price resolution honors the override + env + default; non-positive
     values fall back to the $0.50 default.
  4. Title + slug build deterministically from idea + verdict.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from gecko_core.models import (
    PRD,
    BusinessPlan,
    ResearchResult,
    ValidationReport,
    Verdict,
)
from gecko_core.payments.publish_new import (
    DEFAULT_PUBLISH_PRICE_USD,
    PublishNewError,
    _slugify,
    build_artifact_title,
    publish_artifact,
    render_artifact_markdown,
    resolve_publish_author_address,
    resolve_publish_price_usd,
)


def _result(verdict: Verdict = Verdict.GO) -> ResearchResult:
    return ResearchResult(
        session_id=str(uuid4()),
        tier="basic",
        business_plan=BusinessPlan(
            problem="p",
            icp="i",
            solution="s",
            market="m",
            business_model="bm",
            channels="c",
            risks=["r1"],
            citations=[],
        ),
        validation_report=ValidationReport(
            market_size_signal="ms",
            competitor_analysis="ca",
            demand_evidence="de",
            risk_flags=["rf1"],
            citations=[],
            gap_classification="Partial:segment",
            gap_summary="some gap",
        ),
        prd=PRD(
            v1_scope=["v1"],
            v2_scope=[],
            v3_scope=[],
            acceptance_criteria=["a"],
            non_functional=[],
            success_metrics=["m"],
            citations=[],
        ),
        sources=[],
        verdict=verdict,
    )


# ---------------------------------------------------------------------------
# Price + address resolution
# ---------------------------------------------------------------------------


def test_resolve_price_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUBLISH_NEW_PRICE_USD", raising=False)
    assert resolve_publish_price_usd() == DEFAULT_PUBLISH_PRICE_USD


def test_resolve_price_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLISH_NEW_PRICE_USD", "0.10")
    assert resolve_publish_price_usd(Decimal("0.99")) == Decimal("0.99")


def test_resolve_price_env_when_no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLISH_NEW_PRICE_USD", "0.25")
    assert resolve_publish_price_usd() == Decimal("0.25")


def test_resolve_price_garbage_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLISH_NEW_PRICE_USD", "not-a-number")
    assert resolve_publish_price_usd() == DEFAULT_PUBLISH_PRICE_USD


def test_resolve_price_zero_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLISH_NEW_PRICE_USD", "0")
    assert resolve_publish_price_usd() == DEFAULT_PUBLISH_PRICE_USD


_VALID_BASE = "0x" + "a" * 40


def test_address_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GECKO_WALLET_ADDRESS_BASE", _VALID_BASE)
    other = "0x" + "b" * 40
    assert resolve_publish_author_address(other) == other


def test_address_env_when_no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GECKO_WALLET_ADDRESS_BASE", _VALID_BASE)
    assert resolve_publish_author_address() == _VALID_BASE


def test_address_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_WALLET_ADDRESS_BASE", raising=False)
    with pytest.raises(PublishNewError, match="bb wallet add publish-new"):
        resolve_publish_author_address()


def test_address_invalid_format_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_WALLET_ADDRESS_BASE", raising=False)
    with pytest.raises(PublishNewError, match="not a valid Base 0x address"):
        resolve_publish_author_address("solana-wallet-1234")


def test_address_solana_wallet_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Founder with frames.ag-only setup gets a clear pointer."""
    monkeypatch.delenv("GECKO_WALLET_ADDRESS_BASE", raising=False)
    # base58 placeholder — not a 0x address.
    with pytest.raises(PublishNewError, match="bb wallet add publish-new"):
        resolve_publish_author_address("9xKpAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAa")


# ---------------------------------------------------------------------------
# Slug + title
# ---------------------------------------------------------------------------


def test_slugify_lowercases_and_dashes() -> None:
    assert _slugify("Hello, World!") == "hello-world"


def test_slugify_empty_falls_back() -> None:
    assert _slugify("") == "verdict"


def test_slugify_truncates() -> None:
    s = _slugify("a" * 100, max_len=10)
    assert len(s) <= 10


def test_build_title_includes_verdict() -> None:
    r = _result(Verdict.PIVOT)
    title = build_artifact_title("test idea about defi", r)
    assert title.startswith("Gecko verdict: ")
    assert title.endswith("PIVOT")


def test_build_title_truncates_long_idea() -> None:
    r = _result()
    title = build_artifact_title("x" * 200, r)
    assert "..." in title


def test_render_markdown_contains_sections() -> None:
    r = _result()
    md = render_artifact_markdown("idea", r)
    assert "# Gecko verdict: idea" in md
    assert "## Business Plan" in md
    assert "## Validation Report" in md
    assert "## PRD" in md


# ---------------------------------------------------------------------------
# Stub-mode publish_artifact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_stub_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """X402_MODE=stub: returns a deterministic stub:// URL, never POSTs."""
    monkeypatch.setenv("X402_MODE", "stub")
    monkeypatch.setenv("GECKO_WALLET_ADDRESS_BASE", _VALID_BASE)
    # Bust the cached settings so the test sees the patched env.
    from gecko_core.payments.x402_client import _settings

    _settings.cache_clear()
    try:
        sid = uuid4()
        artifact = await publish_artifact(
            session_id=sid,
            idea="autonomous defi vault for stable yields",
            result=_result(Verdict.GO),
        )
    finally:
        _settings.cache_clear()

    assert artifact.is_stub is True
    assert artifact.url.startswith("stub://publish.new/")
    assert artifact.tx_signature is None
    assert artifact.author_address == _VALID_BASE
    assert artifact.price_usd == DEFAULT_PUBLISH_PRICE_USD
    # Slug should embed verdict + idea fragment (S17-TONE-01: BUILD → GO).
    assert "go" in artifact.slug.lower()


@pytest.mark.asyncio
async def test_publish_stub_honors_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X402_MODE", "stub")
    monkeypatch.delenv("GECKO_WALLET_ADDRESS_BASE", raising=False)
    from gecko_core.payments.x402_client import _settings

    _settings.cache_clear()
    other = "0x" + "1" * 40
    try:
        artifact = await publish_artifact(
            session_id=uuid4(),
            idea="i",
            result=_result(),
            price_usd=Decimal("0.10"),
            author_address=other,
        )
    finally:
        _settings.cache_clear()

    assert artifact.price_usd == Decimal("0.10")
    assert artifact.author_address == other


@pytest.mark.asyncio
async def test_publish_stub_missing_address_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X402_MODE", "stub")
    monkeypatch.delenv("GECKO_WALLET_ADDRESS_BASE", raising=False)
    from gecko_core.payments.x402_client import _settings

    _settings.cache_clear()
    try:
        with pytest.raises(PublishNewError):
            await publish_artifact(
                session_id=uuid4(),
                idea="i",
                result=_result(),
            )
    finally:
        _settings.cache_clear()
