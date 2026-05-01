"""TwitshProvider — paid X/Twitter signal via x402 on Base mainnet.

Sprint 14 S14-TWITSH-01: full ``SourceProvider`` Protocol conformer
promoted from the Sprint 13 skeleton. Wraps the existing
``gecko_core.sources.twit_sh.TwitshSource`` for the actual x402 transport
+ normalization; this provider adapts ``SourceResult`` into the
``SourceChunk`` shape the dispatcher expects, and adds two S14
behaviours on top of the source:

1. **Author allowlist** — when constructed with ``author_allowlist``
   (e.g. the Colosseum-judge handles loaded from
   ``colosseum_judges.json``), the underlying ``TwitshSource.fetch``
   call filters tweets by author and includes a hash of the allowlist
   in the Mongo cache key so filtered/unfiltered runs do not collide.

2. **Research-time feature flag** — ``TWITSH_RESEARCH_ENABLED`` gates
   the provider from the research-time path. The legacy
   ``TWITSH_ENABLED`` flag stays in place for the eval-gate use only.
   Off by default in V1; the dispatcher reports the provider as
   unavailable via ``ProviderHealth(available=False)``.

Trust + cost:
- ``kind = "x402-bazaar"`` — paid bazaar source; the dispatcher's
  budget enforcement and the verdict renderer's receipt anatomy treat
  it accordingly.
- ``cost_estimate`` returns the live $0.01/call price captured during
  the Sprint 13 probe (S14-TWITSH-02 corrected this from $0.005). Real
  spend is bounded server-side by ``TwitshSource``'s $0.05 / session
  cap; the dispatcher's pre-check is a coarse-grained gate on top.
- ``health`` mirrors ``_is_twitsh_configured()`` plus the research
  feature flag.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gecko_core.models import SourceCandidate
from gecko_core.sources.twit_sh import (
    ASSUMED_PER_CALL_USD,
    TwitshSource,
    _is_twitsh_configured,
)
from gecko_core.sources.twitsh_circuit import reserve_spend

from . import ProviderHealth, ProviderKind, SourceChunk

logger = logging.getLogger(__name__)

# Per-call price: the $0.01 USDC quote captured by the S14-TWITSH-01
# probe and pinned in S14-TWITSH-02. Re-exported here so callers that
# only import the provider (not the lower-level source module) can read
# the per-call cost without reaching across the package.
TWITSH_PER_CALL_USD: float = ASSUMED_PER_CALL_USD

# Path to the static allowlist JSON. Lives next to the source module so
# the package data ships with the install.
COLOSSEUM_JUDGES_PATH: Path = (
    Path(__file__).resolve().parents[2] / "sources" / "colosseum_judges.json"
)

# Allowlist staleness warning threshold. Per the integration plan
# (twitsh-colosseum-integration-plan-2026-05-01.md § 5 risk #2) judges
# rotate quarterly; warn at 180d so business-manager has a clear refresh
# trigger before the list silently drops good signal.
_ALLOWLIST_STALENESS_DAYS: int = 180


def _is_research_enabled() -> bool:
    """Research-time flag — independent of the eval-gate `TWITSH_ENABLED`.

    Off by default in V1 so the provider is dark-launched. Operator flips
    on per the integration plan once judges are verified for the active
    Colosseum cycle.
    """
    raw = os.environ.get("TWITSH_RESEARCH_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def load_colosseum_judges(
    path: Path | None = None,
    *,
    cycles: list[str] | None = None,
) -> frozenset[str]:
    """Read the Colosseum-judge allowlist JSON. Empty on any failure.

    ``cycles`` selects which cycle keys to union; ``None`` means every
    cycle present (the integration plan's "active cycles" model). Tests
    inject ``path`` directly to avoid reading the shipped data file.

    Logs (but does not raise) when the file's ``updated_at`` is older
    than ``_ALLOWLIST_STALENESS_DAYS`` — see the integration plan's
    refresh runbook obligation owned by business-manager.
    """
    target = path or COLOSSEUM_JUDGES_PATH
    if not target.exists():
        logger.info("colosseum_judges: %s missing — empty allowlist", target)
        return frozenset()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("colosseum_judges: failed to read %s: %s", target, exc)
        return frozenset()

    if not isinstance(data, dict):
        return frozenset()

    # Staleness probe — informational only.
    updated = data.get("updated_at")
    if isinstance(updated, str):
        try:
            ts = datetime.fromisoformat(updated).replace(tzinfo=UTC)
            if datetime.now(UTC) - ts > timedelta(days=_ALLOWLIST_STALENESS_DAYS):
                logger.warning(
                    "colosseum_judges: allowlist updated_at=%s is >%dd old; "
                    "business-manager should refresh per "
                    "docs/strategy/twitsh-colosseum-integration-plan-2026-05-01.md",
                    updated,
                    _ALLOWLIST_STALENESS_DAYS,
                )
        except ValueError:
            pass

    cycles_blob = data.get("cycles")
    if not isinstance(cycles_blob, dict):
        return frozenset()

    selected_cycles = cycles or list(cycles_blob.keys())
    handles: set[str] = set()
    for cycle_name in selected_cycles:
        entries = cycles_blob.get(cycle_name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str) and entry.strip():
                handles.add(entry.strip())
    return frozenset(handles)


def _synthesize_url(tweet: dict[str, object]) -> str:
    """Build a canonical x.com permalink — twit.sh doesn't always return one.

    Falls back to whatever ``url`` the upstream emitted, then to a synthesized
    permalink from ``handle`` + ``id``. Empty string when neither is available
    so the caller drops the candidate rather than emit a broken citation.
    """
    existing = tweet.get("url")
    if isinstance(existing, str) and existing.startswith("http"):
        return existing
    handle = tweet.get("author_handle")
    if isinstance(handle, str) and handle.startswith("@"):
        handle = handle[1:]
    tid = tweet.get("id") or tweet.get("tweet_id")
    if not handle or not tid:
        return ""
    return f"https://x.com/{handle}/status/{tid}"


class TwitshProvider:
    """`SourceProvider` adapter over the existing `TwitshSource`.

    Conforms structurally to the S12-PROVIDER-01 Protocol. Two S14
    additions: optional ``author_allowlist`` for the Colosseum-judge
    filter, and the research-time feature flag gate via
    ``TWITSH_RESEARCH_ENABLED``.

    The default category gate is the source's own ``applies_to`` —
    crypto / defi / hackathon-team ideas only. The dispatcher passes
    categories through via the env-injected ``TWITSH_DEFAULT_CATEGORIES``
    until the dispatcher contract carries them in directly.
    """

    name: str = "twitsh"
    kind: ProviderKind = "x402-bazaar"

    def __init__(
        self,
        *,
        source: TwitshSource | None = None,
        author_allowlist: frozenset[str] | None = None,
    ) -> None:
        self._source = source or TwitshSource()
        self._allowlist = author_allowlist

    async def cost_estimate(self, query: str) -> float:
        # Single search call per fetch in V1; the source's spend cap
        # ($0.05) bounds the worst case.
        return TWITSH_PER_CALL_USD

    async def health(self) -> ProviderHealth:
        if not _is_research_enabled():
            return ProviderHealth(
                available=False,
                success_rate=0.0,
                last_error=("twitsh: research-time disabled (set TWITSH_RESEARCH_ENABLED=1)"),
            )
        if not _is_twitsh_configured():
            return ProviderHealth(
                available=False,
                success_rate=0.0,
                last_error="twitsh: not configured (env disabled or wallet missing)",
            )
        return ProviderHealth(available=True, success_rate=1.0, last_error=None)

    async def fetch(self, query: str) -> list[SourceChunk]:
        """Run a single x402-paid search and emit one `SourceChunk` per tweet.

        ``query`` is treated as the idea string. Categories are passed in
        via the env-injected default until the dispatcher contract carries
        them through directly. Failures (auth, http, cap-blocked) surface
        as an empty list — the dispatcher handles ``degraded_sources``.
        """
        if not _is_research_enabled():
            return []

        cats_raw = os.environ.get("TWITSH_DEFAULT_CATEGORIES", "crypto,defi")
        categories = {c.strip() for c in cats_raw.split(",") if c.strip()}

        if not await self._source.applies_to(categories=categories):
            return []

        # S14-TWITSH-05: daily aggregate circuit breaker. Reserve the
        # per-call cost (the eventual real spend may be slightly higher
        # if the source fires multiple endpoints; reserving the planner
        # estimate is the conservative call). Breaker tripped → caller
        # surfaces "twitsh" in degraded_sources via fanout_fetch.
        if not reserve_spend(TWITSH_PER_CALL_USD):
            logger.info("twitsh_provider: daily cap reached; skipping fetch")
            return []

        result = await self._source.fetch(
            idea=query,
            categories=categories,
            author_allowlist=self._allowlist,
        )
        if not result.fired:
            if result.error:
                logger.info("twitsh_provider: skipped (%s)", result.error)
            return []

        tweets = (result.payload or {}).get("tweets") or []
        chunks: list[SourceChunk] = []
        for raw_tweet in tweets:
            if not isinstance(raw_tweet, dict):
                continue
            url = _synthesize_url(raw_tweet)
            if not url:
                continue
            try:
                candidate = SourceCandidate(
                    url=url,  # type: ignore[arg-type]
                    title=str(raw_tweet.get("author_handle") or "")[:140],
                    type="web",
                    score=0.5,
                )
            except Exception as exc:
                logger.warning("twitsh_provider: skipped malformed tweet (%s)", exc)
                continue
            chunks.append(
                SourceChunk(
                    candidate=candidate,
                    text=str(raw_tweet.get("text") or ""),
                    metadata={
                        # Threads through to Citation.creator_handle via the
                        # pipeline's chunk-to-citation builder. Allowlist
                        # filtering already happened in TwitshSource — this
                        # is purely the surfacing layer.
                        "creator_handle": raw_tweet.get("author_handle", ""),
                        "engagement": raw_tweet.get("engagement", {}),
                        "created_at": raw_tweet.get("created_at", ""),
                        "spend_usd": (result.payload or {}).get("spend_usd", 0.0),
                        "provider": self.name,
                    },
                )
            )
        return chunks

    async def aclose(self) -> None:
        await self._source.aclose()


__all__ = [
    "COLOSSEUM_JUDGES_PATH",
    "TWITSH_PER_CALL_USD",
    "TwitshProvider",
    "load_colosseum_judges",
]
