"""Pydantic schemas for the 7-agent trade-research panel.

The contract surface — Phase 8b's MCP tool + REST endpoint serialize these.
Field semantics are stable; adding optional fields is non-breaking, renaming
existing fields is. Keep the Literal sets aligned with the closing-line
patterns in :mod:`gecko_core.orchestration.trade_panel.personas`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from gecko_core.orchestration.trade_panel.backtest.models import BacktestReport
from gecko_core.sources.types import FreshnessTier, ProviderKind

# Final-verdict tokens. Mirrors the coordinator's closing-line regex.
TradeVerdictLiteral = Literal["act", "pass", "defer"]


class TradePanelTurn(BaseModel):
    """A single agent's turn in the panel.

    ``parsed_verdict`` is the structured extraction from the closing line
    (e.g. ``{"trend_verdict": "bullish"}``). ``None`` means the agent did
    not emit a parseable closing line — surface as a soft failure rather
    than coercing a default, so callers can flag the run as degraded.
    """

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(..., description="Persona name (matches REQUIRED_AGENTS).")
    content: str = Field(..., description="Full turn text the agent produced.")
    parsed_verdict: dict[str, Any] | None = Field(
        default=None,
        description="Structured extraction from the closing line, or None if unparsed.",
    )


class Citation(BaseModel):
    """A single retrieved-chunk citation surfaced on the verdict envelope.

    Issue #15: previously, citations were exposed only as inline ``[N]``
    markers inside ``turns[].content``. Skill authors and demo UIs had to
    regex-extract from prose to render or audit. This model is the
    structured wire surface — the ``id`` field links each entry to the
    inline marker (1-indexed, matches ``_format_chunks`` in the panel
    driver). Additive only; the inline markers keep working.

    ``provider_kind`` and ``freshness_tier`` re-export the canonical
    Literals from :mod:`gecko_core.sources.types` (Pattern A — single
    source of truth). When a chunk is missing either column, defaults
    fall through to ``"web"`` / ``"static"`` so the wire shape never
    breaks on partial Mongo rows.
    """

    model_config = ConfigDict(extra="forbid")

    id: int = Field(..., ge=1, description="1-indexed marker that matches inline [N] in turns.")
    source: str = Field(..., description="Catalog name (e.g. 'paysh', 'bazaar', 'tavily').")
    url: str = Field(..., description="Full URL when available, else hash-of-chunk_id fallback.")
    chunk_id: str = Field(..., description="Mongo _id (or empty string when ephemeral).")
    provider_kind: ProviderKind = Field(
        default="web",
        description="Canonical chunks.provider_kind — gecko_core.sources.types.ProviderKind.",
    )
    freshness_tier: FreshnessTier = Field(
        default="static",
        description="Canonical chunks.freshness_tier — gecko_core.sources.types.FreshnessTier.",
    )
    snippet: str = Field(
        default="",
        max_length=240,
        description="First 240 chars of the cited chunk content.",
    )


class TradePanelVerdict(BaseModel):
    """Final aggregated verdict from the coordinator + per-turn audit trail."""

    model_config = ConfigDict(extra="forbid")

    verdict: TradeVerdictLiteral = Field(..., description="Final coordinator decision.")
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Coordinator-reported confidence in [0,1]."
    )
    key_drivers: list[str] = Field(
        default_factory=list,
        description="Short bullet drivers behind the verdict.",
    )
    dissent_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Count of voices pointing the OTHER way from the coordinator's verdict. "
            "Computed from parsed_verdict on each non-coordinator turn."
        ),
    )
    blocker_questions: list[str] = Field(
        default_factory=list,
        description="Open questions that would change the verdict if answered.",
    )
    turns: list[TradePanelTurn] = Field(
        default_factory=list,
        description="Full transcript in canonical agent order.",
    )
    citations: list[Citation] = Field(
        default_factory=list,
        description=(
            "Issue #15: structured cite list — sibling to inline [N] markers "
            "in turns[].content. id field links entry to marker index. "
            "Empty list when the panel ran without retrieval (legacy callers)."
        ),
    )
    backtest: BacktestReport | None = Field(
        default=None,
        description=(
            "Realized-history replay of the Strategist intent. None when "
            "enable_backtest=False (default) or when the panel is rerun "
            "by a caller that doesn't surface backtests."
        ),
    )


__all__ = [
    "BacktestReport",
    "Citation",
    "TradePanelTurn",
    "TradePanelVerdict",
    "TradeVerdictLiteral",
]
