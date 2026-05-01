"""S12-PROVIDER-01 — every Citation gets a default Provenance(provider_kind="free").

Existing call sites (LLM-emitted JSON in basic.py, hand-built test
citations) construct a Citation without specifying provenance. The
Pydantic default factory must fire and emit a free/tavily Provenance —
that's how the verdict-renderer surfaces evidence source today, and how
S13+ paid providers will distinguish themselves on the same shape.
"""

from __future__ import annotations

import json

from gecko_core.ingestion.types import IngestionResult
from gecko_core.models import Citation, Provenance


def test_citation_has_default_provenance() -> None:
    c = Citation(
        source_url="https://example.com/post",  # type: ignore[arg-type]
        chunk_index=3,
        similarity=0.7,
    )
    assert isinstance(c.provenance, Provenance)
    assert c.provenance.provider_name == "tavily"
    assert c.provenance.provider_kind == "free"
    assert c.provenance.payment is None


def test_citation_default_provenance_is_independent() -> None:
    """Each Citation gets a fresh Provenance — no shared mutable default."""
    a = Citation(
        source_url="https://example.com/a",  # type: ignore[arg-type]
        chunk_index=0,
        similarity=0.5,
    )
    b = Citation(
        source_url="https://example.com/b",  # type: ignore[arg-type]
        chunk_index=1,
        similarity=0.5,
    )
    assert a.provenance is not b.provenance


def test_citation_from_llm_json_gets_default_provenance() -> None:
    """LLM-emitted citations don't include provenance; default fills in."""
    payload = json.loads(
        '{"source_url": "https://example.com/x", "chunk_index": 2, "similarity": 0.8}'
    )
    c = Citation.model_validate(payload)
    assert c.provenance.provider_kind == "free"
    assert c.provenance.provider_name == "tavily"


def test_citation_can_carry_paid_provenance() -> None:
    c = Citation(
        source_url="https://amadeus.example.com/route",  # type: ignore[arg-type]
        chunk_index=0,
        similarity=0.9,
        provenance=Provenance(
            provider_name="amadeus",
            provider_kind="x402-bazaar",
            payment={"intent_id": "abc", "tx_signature": "stub_sig", "status": "success"},
        ),
    )
    assert c.provenance.provider_kind == "x402-bazaar"
    assert c.provenance.payment is not None
    assert c.provenance.payment["status"] == "success"


def test_ingestion_result_has_empty_degraded_sources_by_default() -> None:
    r = IngestionResult(session_id="00000000-0000-0000-0000-000000000000")
    assert r.degraded_sources == []
