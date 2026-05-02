"""Gecko Flywheel — write-path hook (S2X-04) + privacy guardrail helpers (S2X-05).

After every successful Pro session we persist a `gecko_precedent` row so
future sessions can retrieve precedent context. Three hard rules:

1. ``idea_summary`` is LLM-generated, never the verbatim user idea. CI test
   ``tests/test_flywheel_privacy.py`` enforces ≤30% character overlap.
2. The write is best-effort: failure is logged + swallowed. We never refund
   or fail a Pro session because the flywheel write hiccupped.
3. We only persist verdicts in the canonical {ship, kill, pivot} set. An
   "unknown" verdict (judge text didn't parse) skips the write entirely.

Public surface:
    - ``write_precedent`` — top-level orchestrator wired from workflows.py
    - ``summarize_idea_for_flywheel`` — privacy-safe LLM summary
    - ``extract_comparables`` — heuristic comparable-products extractor
    - ``extract_verdict`` — re-export so workflows doesn't reach into tests/
    - ``longest_common_substring_ratio`` — used by the CI privacy test
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Literal, cast
from uuid import UUID

from openai import AsyncOpenAI

from gecko_core.classify import classify_idea
from gecko_core.ingestion.embedder import embed
from gecko_core.orchestration.settings import get_orchestration_settings
from gecko_core.sessions.store import SessionStore, Verdict

logger = logging.getLogger(__name__)


_CANONICAL_VERDICTS: frozenset[str] = frozenset(("ship", "kill", "pivot"))

# Privacy-safe summarization. Asks the model for a category-flavored
# abstraction. Temperature is intentionally non-zero so the phrasing
# diverges from the user's exact text — the privacy guardrail is statistical
# (≤30% LCS overlap), not formal, so a tiny temperature buys margin.
_SUMMARY_SYSTEM = (
    "You are a startup-idea categorizer. Given a builder's idea text, "
    "produce a single short sentence (≤ 25 words) that describes the "
    "CATEGORY OF THING the idea is — not the idea itself. "
    "Examples:\n"
    "  Idea: 'I want to build a Solana-native validator monitoring tool with "
    "slashing alerts so operators don't lose stake'\n"
    "  Summary: 'Solana validator monitoring tool with slashing alerts'\n\n"
    "  Idea: 'AI-powered changelog writer that turns merged PRs into a "
    "weekly customer-facing release-notes email for B2B SaaS teams'\n"
    "  Summary: 'AI changelog generator for B2B SaaS'\n\n"
    "Hard rules:\n"
    "- DO NOT echo phrases longer than 4 words from the original idea.\n"
    "- DO NOT mention the user, their company, or any first-person framing.\n"
    '- Output ONLY the JSON object {"idea_summary": "..."}. No prose.'
)


def _normalize_idea(idea: str) -> str:
    return " ".join(idea.lower().split())


def _idea_hash(idea: str) -> str:
    return hashlib.sha256(_normalize_idea(idea).encode("utf-8")).hexdigest()


def extract_verdict(judge_text: str) -> Verdict | Literal["unknown"]:
    """Parse the judge's verdict from free-form text.

    Mirrors ``tests/eval/rubric.extract_verdict`` but lives here so the
    write-path doesn't import from tests/. Returns "unknown" when the
    judge text doesn't parse — caller must skip the precedent write.
    """
    text = judge_text.lower()
    m = re.search(r"verdict\s*:\s*(\w+)", text)
    if m:
        word = m.group(1)
        if word.startswith("kill"):
            return "kill"
        if word.startswith("ship") or word.startswith("build"):
            return "ship"
        if word.startswith("pivot"):
            return "pivot"
    if "pivot" in text:
        return "pivot"
    if "kill" in text or "do not build" in text or "don't build" in text:
        return "kill"
    if "ship" in text or "build it" in text:
        return "ship"
    return "unknown"


# Comparable-extraction heuristics. The Pro debate prompts encourage agents
# to name existing products as comparables ("Stripe Atlas", "Helius",
# "Linear", etc.); we don't get a structured field from AG2, so we lift
# capitalized multi-word phrases plus a small allowlist of single-word
# product names. Good enough for the dashboard / future retrieval prompts;
# the flywheel write tolerates an empty list.

# Single-word product/company names that show up frequently in debates and
# wouldn't pass the Title-Case multi-word filter on their own.
_KNOWN_COMPARABLES: frozenset[str] = frozenset(
    {
        "Stripe",
        "Linear",
        "Notion",
        "Helius",
        "Solana",
        "Phantom",
        "Privy",
        "Ethereum",
        "Vercel",
        "Supabase",
        "OpenAI",
        "Anthropic",
        "GitHub",
        "Tavily",
        "Datadog",
        "Sentry",
        "Twilio",
        "Plaid",
        "Coinbase",
    }
)

# Title-Case phrases of 1-4 words. Disallow leading-of-sentence single
# capitalized words by requiring either ≥2 words OR membership in the
# allowlist.
_COMPARABLE_PHRASE = re.compile(r"\b([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,3})\b")
_STOPWORDS_PHRASE: frozenset[str] = frozenset(
    {
        # debate-grammar noise that the Title-Case regex catches
        "Verdict",
        "Ship",
        "Kill",
        "Pivot",
        "TAM",
        "WEDGE",
        "ICP",
        "V1",
        "V2",
        "V3",
        "Year",
        "Analyst",
        "Critic",
        "Architect",
        "Scoper",
        "Judge",
        "I",
        "You",
        "We",
        "The",
    }
)


def extract_comparables(transcript: dict[str, Any] | None, *, limit: int = 8) -> list[str]:
    """Pull comparable products/companies the agents named in the debate.

    Walks the transcript's ``turns[*].content`` and returns up to ``limit``
    distinct candidate names in first-mention order. Best-effort — the
    flywheel row tolerates an empty list (`key_comparables jsonb default '[]'`).
    """
    if not transcript:
        return []
    turns = transcript.get("turns") if isinstance(transcript, dict) else None
    if not isinstance(turns, list):
        return []

    seen: dict[str, None] = {}  # ordered set
    for turn in turns:
        content = turn.get("content") if isinstance(turn, dict) else None
        if not isinstance(content, str):
            continue
        for match in _COMPARABLE_PHRASE.finditer(content):
            phrase = match.group(1).strip()
            words = phrase.split()
            # Single-word: must be in allowlist (case-sensitive on first char).
            if len(words) == 1 and phrase not in _KNOWN_COMPARABLES:
                continue
            if phrase in _STOPWORDS_PHRASE:
                continue
            # Reject phrases whose first word is a debate-grammar stopword
            # ("SHIP V1", "Verdict PIVOT", etc. — false positives for products).
            if words[0] in _STOPWORDS_PHRASE:
                continue
            seen.setdefault(phrase, None)
            if len(seen) >= limit:
                break
        if len(seen) >= limit:
            break
    return list(seen.keys())


async def summarize_idea_for_flywheel(
    idea: str,
    categories: list[str],
    *,
    client: AsyncOpenAI | None = None,
) -> str:
    """Privacy-safe LLM summary of `idea`.

    Returns a 1-sentence category abstraction. Uses JSON mode so we can
    validate the shape before writing; falls back to a category-only string
    on parse failure (still privacy-safe — never the raw idea).
    """
    if client is None:
        # Use the orchestration endpoint (ClawRouter / OpenAI), not the
        # ingestion endpoint — the ingestion key is scoped to embeddings.
        orch = get_orchestration_settings()
        client = AsyncOpenAI(api_key=orch.llm_api_key, base_url=orch.llm_endpoint)

    orch = get_orchestration_settings()
    user_msg = (
        f"Categories detected: {', '.join(categories) if categories else 'general'}\nIdea: {idea}"
    )
    try:
        resp = await client.chat.completions.create(
            model=orch.chat_model,
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0.4,
        )
        raw = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        summary = parsed.get("idea_summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    except Exception as exc:
        logger.warning("flywheel summary LLM call failed: %s", type(exc).__name__)

    # Defensive fallback — privacy-safe by construction (no idea text).
    if categories:
        return f"{categories[0]} project (auto-generated category summary)"
    return "general builder project"


async def write_precedent(
    *,
    session_id: UUID,
    idea: str,
    transcript: dict[str, Any] | None,
    user_id: UUID | None,
    store: SessionStore,
    openai_client: AsyncOpenAI | None = None,
) -> UUID | None:
    """Best-effort flywheel write. Returns the new precedent UUID or None.

    Never raises. Logs and swallows everything — the Pro session response
    must not be blocked or refunded by a flywheel hiccup.
    """
    try:
        # Verdict — must be ship/kill/pivot. Unknown skips the write so we
        # don't pollute the precedent corpus with junk rows.
        judge_text = ""
        if transcript and isinstance(transcript.get("turns"), list):
            for turn in reversed(transcript["turns"]):
                if isinstance(turn, dict) and turn.get("agent") == "judge":
                    judge_text = str(turn.get("content") or "")
                    break
        verdict = extract_verdict(judge_text)
        if verdict not in _CANONICAL_VERDICTS:
            logger.info(
                "flywheel: skipping precedent write for session %s (verdict=%s)",
                session_id,
                verdict,
            )
            return None

        # Categories. Empty list is fine — the column has a default.
        try:
            cats = sorted(await classify_idea(idea))
        except Exception as exc:
            logger.warning("flywheel: classify_idea failed: %s", exc)
            cats = []

        # Privacy-safe summary. Always returns a non-empty string.
        idea_summary = await summarize_idea_for_flywheel(idea, cats, client=openai_client)

        # Comparables from transcript text.
        comparables = extract_comparables(transcript)

        # Embed the SUMMARY (not the idea) so retrieval surfaces categorically
        # similar precedent without leaking the original phrasing.
        vectors, _tokens = await embed([idea_summary], client=openai_client)
        if not vectors:
            logger.warning("flywheel: empty embedding result for session %s", session_id)
            return None

        precedent_id = await store.append_gecko_precedent(
            session_id=session_id,
            user_id=user_id,
            idea_summary=idea_summary,
            idea_hash=_idea_hash(idea),
            category_tags=cats,
            verdict=cast(Verdict, verdict),
            key_comparables=cast(list[Any], comparables),
            embedding=vectors[0],
        )
        logger.info(
            "flywheel: wrote precedent %s for session %s (verdict=%s, cats=%s, comparables=%d)",
            precedent_id,
            session_id,
            verdict,
            cats,
            len(comparables),
        )
        return precedent_id
    except Exception as exc:
        # Never let flywheel failures escape — the spec is explicit.
        logger.warning(
            "flywheel: write_precedent best-effort failure for session %s: %s: %s",
            session_id,
            type(exc).__name__,
            exc,
        )
        return None


def longest_common_substring_ratio(a: str, b: str) -> float:
    """Length of LCS(a, b) divided by len(a). Character-level, tokenization-free.

    Used by the CI privacy test (S2X-05) to enforce ≤30% verbatim overlap
    between the user's idea and the LLM-generated summary. ``a`` is the
    idea (denominator); ``b`` is the summary.
    """
    if not a:
        return 0.0
    a_low = a.lower()
    b_low = b.lower()
    n = len(a_low)
    m = len(b_low)
    if m == 0:
        return 0.0
    # Rolling 2-row DP — O(n*m) time, O(min(n,m)) space.
    if m > n:
        a_low, b_low = b_low, a_low
        n, m = m, n
    prev = [0] * (m + 1)
    curr = [0] * (m + 1)
    best = 0
    for i in range(1, n + 1):
        ai = a_low[i - 1]
        for j in range(1, m + 1):
            if ai == b_low[j - 1]:
                curr[j] = prev[j - 1] + 1
                if curr[j] > best:
                    best = curr[j]
            else:
                curr[j] = 0
        prev, curr = curr, prev
    return best / len(a)


__all__ = [
    "extract_comparables",
    "extract_verdict",
    "longest_common_substring_ratio",
    "summarize_idea_for_flywheel",
    "write_precedent",
]
