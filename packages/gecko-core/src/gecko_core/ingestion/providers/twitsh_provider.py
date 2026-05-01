"""TwitshProvider — paid X/Twitter signal via x402 on Base mainnet.

Sprint 14 S14-TWITSH-01: skeleton wiring of twit.sh under the
`SourceProvider` Protocol. Reuses the existing
`gecko_core.sources.twit_sh.TwitshSource` for the actual x402 transport
+ normalization; the provider just adapts `SourceResult` into the
`SourceChunk` shape the dispatcher (S13+) expects.

This is a SKELETON. The probe (`docs/diagnostics/2026-05-01-twitsh-probe.md`)
confirmed the wire shape; full integration (catalog default fix, URL
synthesis, provenance threading, dispatcher registration) is tracked
under follow-up tickets S14-TWITSH-02..05.

Trust + cost:
- `kind = "x402-bazaar"` — pulled from `ProviderKind` literal so the
  dispatcher's budget enforcement and the verdict renderer's receipt
  anatomy treat it as a paid bazaar source.
- `cost_estimate` returns the listed per-call price ($0.01 USDC) per
  the 402 challenge captured during the S14-TWITSH-01 probe. Real
  spend is bounded server-side by `TwitshSource`'s $0.05 / session
  cap; the dispatcher's pre-check is a coarse-grained gate on top.
- `health` mirrors `is_twitsh_configured()` — if env disables the
  source (`TWITSH_ENABLED=false` or missing key), the provider reports
  unavailable and the dispatcher skips it without spending.
"""

from __future__ import annotations

import logging
import os

from gecko_core.models import SourceCandidate
from gecko_core.sources.twit_sh import TwitshSource, _is_twitsh_configured

from . import ProviderHealth, ProviderKind, SourceChunk

logger = logging.getLogger(__name__)

# Per-call price quoted in the 402 challenge: 10000 base units of USDC
# (6 decimals) = $0.01 / call. See diagnostic doc § Cost.
TWITSH_PER_CALL_USD: float = 0.01


def _synthesize_url(tweet: dict[str, object]) -> str:
    """Build a canonical x.com permalink — twit.sh doesn't return one.

    Falls back to an empty string if either the author handle or tweet id
    is missing; the dispatcher will then drop the candidate rather than
    surface a broken Citation.
    """
    handle = tweet.get("author_handle")
    if isinstance(handle, str) and handle.startswith("@"):
        handle = handle[1:]
    tid = tweet.get("id") or tweet.get("tweet_id")
    if not handle or not tid:
        return ""
    return f"https://x.com/{handle}/status/{tid}"


class TwitshProvider:
    """`SourceProvider` adapter over the existing `TwitshSource`.

    Default category gate is the source's own `applies_to` — crypto /
    defi / hackathon-team ideas only. The dispatcher (S14-TWITSH-03) is
    responsible for passing categories through; this skeleton keeps the
    gate inside `fetch()` for now.
    """

    name: str = "twit_sh"
    kind: ProviderKind = "x402-bazaar"

    def __init__(self, *, source: TwitshSource | None = None) -> None:
        self._source = source or TwitshSource()

    async def cost_estimate(self, query: str) -> float:
        # Single search call per fetch in V1; the source's spend cap
        # ($0.05) bounds the worst case. Coarse-grained gate value.
        return TWITSH_PER_CALL_USD

    async def health(self) -> ProviderHealth:
        if not _is_twitsh_configured():
            return ProviderHealth(
                available=False,
                success_rate=0.0,
                last_error="twitsh: not configured (env disabled or wallet missing)",
            )
        # Wallet balance + per-call success-rate tracking belong in
        # S14-TWITSH-03. For the skeleton, configured == available.
        return ProviderHealth(available=True, success_rate=1.0, last_error=None)

    async def fetch(self, query: str) -> list[SourceChunk]:
        """Run a single x402-paid search and emit one `SourceChunk` per tweet.

        `query` is treated as the idea string; the underlying source
        derives a small keyword set itself. Categories are passed in via
        the env-injected default until the dispatcher contract carries
        them through (S14-TWITSH-03).
        """
        cats_raw = os.environ.get("TWITSH_DEFAULT_CATEGORIES", "crypto,defi")
        categories = {c.strip() for c in cats_raw.split(",") if c.strip()}

        if not await self._source.applies_to(categories=categories):
            return []

        result = await self._source.fetch(idea=query, categories=categories)
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
                # Without a canonical permalink we can't build a usable
                # Citation. Skip rather than emit a broken candidate.
                continue
            try:
                candidate = SourceCandidate(
                    url=url,  # type: ignore[arg-type]
                    title=str(raw_tweet.get("author_handle") or "")[:140],
                    # SourceType is currently `Literal["youtube", "web"]`;
                    # tweets ride under "web" until a dedicated literal lands
                    # (tracked under S14-TWITSH-04 alongside rubric weighting).
                    type="web",
                    score=0.5,  # placeholder; rubric weighting in S14-TWITSH-04
                )
            except Exception as exc:
                logger.warning("twitsh_provider: skipped malformed tweet (%s)", exc)
                continue
            chunks.append(
                SourceChunk(
                    candidate=candidate,
                    text=str(raw_tweet.get("text") or ""),
                    metadata={
                        "author_handle": raw_tweet.get("author_handle", ""),
                        "engagement": raw_tweet.get("engagement", {}),
                        "created_at": raw_tweet.get("created_at", ""),
                        "spend_usd": (result.payload or {}).get("spend_usd", 0.0),
                    },
                )
            )
        return chunks

    async def aclose(self) -> None:
        await self._source.aclose()


__all__ = ["TWITSH_PER_CALL_USD", "TwitshProvider"]
