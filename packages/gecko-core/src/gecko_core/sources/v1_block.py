"""V1 Source Signal block — dispatch + render for the Pro debate (S4-TWITSH-01).

Sits between `_run_pro_debate` and the four V1 sources (twit.sh, HN, Reddit,
gecko_precedent). One entrypoint, one render, one cap.

Why a separate module rather than inlining in `workflows.py`:
- The render logic is pure-string and gets >50 lines once you handle the
  empty-state-per-source contract correctly. Inlining would push
  `_run_pro_debate` past readable.
- The `$0.10` cap is a workflow-level invariant (twit.sh has its own $0.05
  cap; this is the *cross-source* cap). Keeping it adjacent to the renderer
  means the cap and the rendered `spend_usd` numbers can't drift.
- Lets `tests/test_twitsh_active.py` exercise dispatch + render directly
  without spinning up the whole AG2 stack.

The block ALWAYS renders all four headings, even when a source returned
nothing. Absence-of-signal is signal: if the agents don't see a "no twit.sh
results" line they can't reason about why a crypto idea has no X chatter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from gecko_core.sources import Source, SourceResult, dispatch_sources
from gecko_core.sources.gecko_precedent import GeckoPrecedentSource
from gecko_core.sources.hn import HackerNewsSource
from gecko_core.sources.reddit import RedditSource
from gecko_core.sources.twit_sh import TwitshSource
from gecko_core.sources.v1_source_ids import V1_SOURCE_IDS, V1SourceId

logger = logging.getLogger(__name__)

# Cross-source cap. Twit.sh enforces its own $0.05 cap internally; this is
# the *workflow-level* ceiling so when V1.5 paid sources land we don't
# accidentally double up. Tracked against the session ledger via
# `cost_v1_sources_usd`.
V1_SOURCES_SPEND_CAP_USD: float = 0.10

# Caps per-result rendering so a chatty source can't blow rag_context past
# the pro round-1 char cap.
_RENDER_LIMIT_PER_SOURCE: int = 5


@dataclass(frozen=True)
class V1Block:
    """The rendered string + per-source spend ledger for telemetry.

    `rag_block` is what gets prepended to the existing RAG corpus.
    `spend_by_source` is what `_run_pro_debate` rolls into the session
    cost ledger via `store.add_cost(...)`.
    """

    rag_block: str
    spend_by_source: dict[str, float]
    results: dict[str, SourceResult]

    @property
    def total_spend_usd(self) -> float:
        return sum(self.spend_by_source.values())


def build_default_sources(
    *,
    embedding: list[float] | None,
    store: Any,
) -> list[Source]:
    """Construct the canonical V1 source list.

    `embedding` and `store` only feed the precedent source — the other three
    are stateless. We pass them as keyword args so the caller can drop the
    precedent source by passing `embedding=None` when they don't have one
    (e.g. cold flywheel during ingestion-only flows).
    """
    sources: list[Source] = [
        TwitshSource(),
        HackerNewsSource(),
        RedditSource(),
    ]
    if embedding is not None and store is not None:
        sources.append(GeckoPrecedentSource(embedding=embedding, store=store))
    return sources


def _render_twitsh(result: SourceResult | None) -> str:
    if result is None or not result.fired:
        return "No data found."
    tweets = result.payload.get("tweets") or []
    if not tweets:
        return "No data found."
    lines: list[str] = []
    for t in tweets[:_RENDER_LIMIT_PER_SOURCE]:
        handle = t.get("author_handle") or "@unknown"
        text = (t.get("text") or "").replace("\n", " ").strip()
        # Cap excerpt at ~160 chars — agents don't need the full tweet,
        # the citation gestures at the signal.
        if len(text) > 160:
            text = text[:157] + "..."
        eng = t.get("engagement") or {}
        likes = eng.get("likes", 0)
        replies = eng.get("replies", 0)
        lines.append(f'- {handle}: "{text}" (likes={likes}, replies={replies})')
    return "\n".join(lines)


def _render_hn(result: SourceResult | None) -> str:
    if result is None or not result.fired:
        return "No data found."
    hits = result.payload.get("hits") or []
    if not hits:
        return "No data found."
    lines: list[str] = []
    for h in hits[:_RENDER_LIMIT_PER_SOURCE]:
        title = (h.get("title") or "").strip()
        url = h.get("url") or ""
        points = h.get("points") or 0
        comments = h.get("comments") or 0
        lines.append(f"- {title} — {url} (points={points}, comments={comments})")
    return "\n".join(lines)


def _render_reddit(result: SourceResult | None) -> str:
    if result is None or not result.fired:
        return "No data found."
    posts = result.payload.get("posts") or []
    if not posts:
        return "No data found."
    lines: list[str] = []
    for p in posts[:_RENDER_LIMIT_PER_SOURCE]:
        sub = p.get("subreddit") or "?"
        title = (p.get("title") or "").strip()
        url = p.get("url") or ""
        score = p.get("score") or 0
        lines.append(f"- r/{sub} · {title} — {url} (score={score})")
    return "\n".join(lines)


def _render_precedents(result: SourceResult | None) -> str:
    if result is None or not result.fired:
        return "No data found."
    precedents = result.payload.get("precedents") or []
    if not precedents:
        return "No data found."
    lines: list[str] = []
    for p in precedents[:_RENDER_LIMIT_PER_SOURCE]:
        verdict = (p.get("verdict") or "?").upper()
        summary = (p.get("idea_summary") or "").strip()
        sim = p.get("similarity")
        sim_str = f"{float(sim):.2f}" if sim is not None else "—"
        lines.append(f"- [{verdict}] similar idea: {summary} (sim={sim_str})")
    return "\n".join(lines)


def render_block(results: dict[str, SourceResult]) -> str:
    """Render the four-heading V1 source signal block.

    Order is fixed (twit.sh, HN, Reddit, precedents) so the agents can rely
    on positional reasoning ("the prior verdict block told me X"). Even an
    empty source gets its heading + "No data found." so absence of signal
    is visible to the agents.
    """
    # Source ids referenced here are the canonical V1SourceId values from
    # `gecko_core.sources.v1_source_ids`. The dict-key strings stay
    # literal because the four renderers are heterogeneous, but the
    # consistency test in tests/test_v1_source_ids_consistency.py asserts
    # every id used in any prompt JSON is in V1_SOURCE_IDS.
    twit_sh_id: V1SourceId = "twit_sh"
    hn_id: V1SourceId = "hn"
    reddit_id: V1SourceId = "reddit"
    gecko_precedent_id: V1SourceId = "gecko_precedent"
    parts = [
        "## V1 Source Signal",
        "",
        "### Twitter / X (twit.sh)",
        _render_twitsh(results.get(twit_sh_id)),
        "",
        "### Hacker News",
        _render_hn(results.get(hn_id)),
        "",
        "### Reddit",
        _render_reddit(results.get(reddit_id)),
        "",
        "### Prior Gecko Verdicts (gecko_precedent)",
        _render_precedents(results.get(gecko_precedent_id)),
    ]
    return "\n".join(parts)


async def dispatch_and_render(
    *,
    idea: str,
    categories: set[str],
    sources: list[Source],
    spend_cap_usd: float = V1_SOURCES_SPEND_CAP_USD,
    timeout_seconds: float = 15.0,
) -> V1Block:
    """Run dispatch_sources, enforce the cross-source cap, render the block.

    The cap is enforced *post-hoc* per source: we let twit.sh run (it has
    its own internal $0.05 cap) and only short-circuit additional paid
    sources if we've already exceeded $0.10. Today twit.sh is the only paid
    V1 source, so the cap effectively just verifies twit.sh stayed under
    its own budget. The structure is in place for V1.5 paid sources.
    """
    results = await dispatch_sources(
        idea=idea,
        categories=categories,
        sources=sources,
        timeout_seconds=timeout_seconds,
    )

    spend_by_source: dict[str, float] = {}
    running_total = 0.0
    for name, res in results.items():
        cost = float(res.cost_usd or 0.0)
        if cost <= 0:
            continue
        if running_total + cost > spend_cap_usd:
            # Cap hit. Record the actual debit (twit.sh already paid) but
            # log so the next paid source we add knows it was clipped.
            logger.warning(
                "v1_sources: spend cap hit at $%.4f after %s ($%.4f); future "
                "paid sources should short-circuit",
                running_total + cost,
                name,
                cost,
            )
        spend_by_source[name] = cost
        running_total += cost

    return V1Block(
        rag_block=render_block(results),
        spend_by_source=spend_by_source,
        results=results,
    )


__all__ = [
    "V1_SOURCES_SPEND_CAP_USD",
    "V1_SOURCE_IDS",
    "V1Block",
    "V1SourceId",
    "build_default_sources",
    "dispatch_and_render",
    "render_block",
]


# Keep the import light; UUID kept only for typing hints in adjacent modules.
_ = UUID
