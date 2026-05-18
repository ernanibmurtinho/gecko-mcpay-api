"""Offline oracle fixtures for the backtest harness.

The backtest must NEVER call ``gecko_trade_research`` for real (founder
constraint: zero live $ spent during replay). These fixtures implement
the same call-signature as the production oracle but pull verdicts from
recorded JSON or constant returns.

Three fixtures:

* :class:`RecordedOracleFixture` — Pattern C contract testing pattern.
  Reads a JSON map of ``{idea_hash: verdict_dict}``. Primary backtest
  oracle.
* :class:`OptimisticOracleFixture` — every verdict is ``act`` with
  ``confidence=1.0``. Used to assert ungated baseline parity.
* :class:`PessimisticOracleFixture` — every verdict is ``pass``. Smoke
  test that the gated path correctly suppresses all trades.

All three are async-callable matching :class:`gecko_core.trade_agent.oracle.VerdictCaller`
so the harness can plug them directly into an :class:`OracleWrapper` or
call them inline.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from gecko_core.trade_agent.oracle import idea_hash


class _CountingFixture:
    """Shared book-keeping — call count + cache hits.

    The harness reports ``verdict_call_count`` and ``cache_hit_rate``;
    fixtures track both so the harness doesn't need a side-channel.
    """

    def __init__(self) -> None:
        self.call_count: int = 0
        self.cache_hits: int = 0
        self._seen: Counter[str] = Counter()

    def _record(self, key: str) -> None:
        self.call_count += 1
        if self._seen[key] > 0:
            self.cache_hits += 1
        self._seen[key] += 1

    @property
    def cache_hit_rate(self) -> float:
        if self.call_count == 0:
            return 0.0
        return round(self.cache_hits / self.call_count, 6)


class RecordedOracleFixture(_CountingFixture):
    """Replay verdicts from a recorded JSON map.

    File format: ``{"<idea_hash>": {"verdict": "act"|"pass"|"defer",
    "confidence": float, "verdict_id": str, "evidence_citations": [...],
    "framework_context": [...]}}``

    A missing key falls back to ``default`` (defaults to ``pass`` —
    conservative: an un-recorded idea is not authorised).
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        records: dict[str, dict[str, Any]] | None = None,
        default: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        if records is not None:
            self._records = records
        elif path is not None:
            self._records = json.loads(Path(path).read_text(encoding="utf-8"))
        else:
            self._records = {}
        self._default = default or {
            "verdict": "pass",
            "confidence": 0.0,
            "evidence_citations": [],
            "framework_context": [],
            "verdict_id": "default-pass",
        }

    async def __call__(
        self,
        *,
        idea: str | dict[str, Any],
        tier: str = "basic",
        agent_id: str = "",
    ) -> dict[str, Any]:
        key = idea_hash(idea)
        self._record(key)
        if key in self._records:
            return dict(self._records[key])
        return dict(self._default)


class OptimisticOracleFixture(_CountingFixture):
    """Every idea returns ``act`` with confidence 1.0.

    Used to validate that the gated path behaves identically to the
    ungated path when verdicts never veto.
    """

    async def __call__(
        self,
        *,
        idea: str | dict[str, Any],
        tier: str = "basic",
        agent_id: str = "",
    ) -> dict[str, Any]:
        key = idea_hash(idea)
        self._record(key)
        return {
            "verdict": "act",
            "confidence": 1.0,
            "verdict_id": f"optimistic-{key[:8]}",
            "evidence_citations": [],
            "framework_context": [],
        }


class PessimisticOracleFixture(_CountingFixture):
    """Every idea returns ``pass``. Gated path should make 0 trades."""

    async def __call__(
        self,
        *,
        idea: str | dict[str, Any],
        tier: str = "basic",
        agent_id: str = "",
    ) -> dict[str, Any]:
        key = idea_hash(idea)
        self._record(key)
        return {
            "verdict": "pass",
            "confidence": 1.0,
            "verdict_id": f"pessimistic-{key[:8]}",
            "evidence_citations": [],
            "framework_context": [],
        }


__all__ = [
    "OptimisticOracleFixture",
    "PessimisticOracleFixture",
    "RecordedOracleFixture",
]
