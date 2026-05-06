"""S20-RAG-04 — embed-cost rate-table assertions.

Locks the published Voyage list prices into a unit test so a typo in
`_EMBED_RATES_USD_PER_1M` (or an accidental drop of `voyage-3` while
swapping defaults to `voyage-3-large`) is caught at PR time, not when
the economics ledger silently zeroes a session out.

Note: `voyage-context-3` is supported only via MongoDB's hosted Voyage
endpoint (`ai.mongodb.com/v1/embeddings`). Direct Voyage SDK rejects
the model name. Production default is `voyage-3-large` — the RAG-optimal
1024-dim model on Voyage's official catalog.
"""

from __future__ import annotations

from gecko_core.ingestion.embedder import (
    _EMBED_RATES_USD_PER_1M,
    estimate_embed_cost_usd,
)


def test_voyage_3_large_default_present_and_priced() -> None:
    assert "voyage-3-large" in _EMBED_RATES_USD_PER_1M
    assert _EMBED_RATES_USD_PER_1M["voyage-3-large"] == 0.18


def test_voyage_context_3_priced_for_mongodb_hosted_endpoint() -> None:
    # Kept in the rate table so calls via Atlas's hosted Voyage endpoint
    # attribute cost correctly. Not the default — Voyage's standalone API
    # does not recognize this model name.
    assert "voyage-context-3" in _EMBED_RATES_USD_PER_1M
    assert _EMBED_RATES_USD_PER_1M["voyage-context-3"] == 0.18


def test_voyage_3_legacy_still_priced() -> None:
    # voyage-3 must remain pricable so legacy chunks ingested under it
    # still attribute cost correctly. S20-RAG-04 is a default swap, not
    # a removal.
    assert "voyage-3" in _EMBED_RATES_USD_PER_1M
    assert _EMBED_RATES_USD_PER_1M["voyage-3"] == 0.06


def test_estimate_embed_cost_voyage_3_large_one_million_tokens() -> None:
    assert estimate_embed_cost_usd("voyage-3-large", 1_000_000) == 0.18


def test_estimate_embed_cost_unknown_model_returns_zero() -> None:
    assert estimate_embed_cost_usd("not-a-real-model", 1_000_000) == 0.0
