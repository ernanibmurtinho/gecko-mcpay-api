"""Ingestion-time URL blocklist + name-collision linter.

Why this exists
---------------
Tavily and other discovery providers regularly surface pages that share a
*name* with our product but describe a completely different artefact. The
canonical offender as of S20 is ``arxiv.org/html/2602.19218`` — an academic
paper about a tool-call simulation environment also called "Gecko" — which
contaminated 3 of the last 4 dogfood sessions. Even with a correct wedge
sentence, the corpus pulled in the wrong "Gecko" and the panel scored us
against an unrelated paper.

Two layers ship here:

1. **Hard blocklist** (`is_blocked`). Compiled regex patterns matched
   against URL strings (and optionally page titles). Any match drops the
   URL *before* embedding so we don't burn OpenAI credit on contamination.
   Patterns extend at runtime via ``GECKO_INGEST_BLOCKLIST_EXTRA``
   (comma-separated regex sources).

2. **Soft collision flag** (`flag_collision_risk`). For chunks that mention
   "Gecko" but live on a domain that is *not* one of our canonical hosts,
   a metadata field ``collision_risk: true`` is attached. This is an
   advisory signal only — retrieval does not yet use it. Lets us keep
   ambiguous content in the base while warning consumers (next ticket
   wires it into scoring).

Conventions (CLAUDE.md, Pattern A)
----------------------------------
- ``BLOCKLIST_PATTERNS`` is the canonical tuple. Tests, ingestion, and the
  operator runbook all reference this constant. Adding an entry = touch
  exactly this file (or set the env var for a hot-fix).
- Patterns are **compiled once at module import**. Never per-URL.
- Each pattern carries a stable ``name`` so structured logs and tests can
  assert the *reason* a URL was filtered, not just the boolean outcome.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlocklistPattern:
    """A single compiled blocklist entry.

    ``name`` is the stable identifier surfaced in logs + tests.
    ``regex`` is matched against the *full* URL (case-insensitive). When
    ``match_title`` is True the pattern is also tested against the page
    title — used for narrow cases where the URL is generic but the title
    is unambiguous.
    """

    name: str
    regex: re.Pattern[str]
    match_title: bool = False


# Canonical seed list. Order is significant: the *first* matching pattern
# is the one returned. Keep the most specific entries first so logs name
# the precise offender rather than the broad fallback.
BLOCKLIST_PATTERNS: tuple[BlocklistPattern, ...] = (
    # The exact academic-Gecko paper that contaminated the May 5-6 dogfood
    # sessions. Matches /abs/, /html/, /pdf/ variants under any version
    # suffix (vN) — Arxiv assigns those silently on revisions.
    BlocklistPattern(
        name="arxiv-gecko-2602",
        regex=re.compile(
            r"^https?://(?:www\.)?arxiv\.org/(?:abs|html|pdf)/2602\.19218",
            re.IGNORECASE,
        ),
    ),
    # Defensive: any other Arxiv paper whose URL slug contains "gecko".
    # Lower-priority than the explicit 2602.19218 entry above so the
    # logged ``pattern`` field stays meaningful when the explicit one
    # would have matched. The path-only check avoids domain-name false
    # positives (geckovision.tech.arxiv.org would never be a real URL,
    # but defence-in-depth costs nothing here).
    BlocklistPattern(
        name="arxiv-gecko-broad",
        regex=re.compile(
            r"^https?://(?:www\.)?arxiv\.org/[^?#]*gecko",
            re.IGNORECASE,
        ),
    ),
)


# Hosts that we consider canonical "us". Chunks that mention "Gecko" while
# living under one of these domains are NOT collision candidates — the
# usage is unambiguously about the product. Compared via suffix match on
# the parsed hostname (case-insensitive) so subdomains (app., api., docs.)
# are covered without per-host enumeration.
_CANONICAL_GECKO_HOSTS: tuple[str, ...] = (
    "geckovision.tech",
    "gecko-mcpay-api",  # github / artifact paths
    "gecko-mcpay-app",
    "gecko-mcpay-skills",
)


_ENV_EXTRA = "GECKO_INGEST_BLOCKLIST_EXTRA"


def _compile_extra_from_env() -> tuple[BlocklistPattern, ...]:
    """Compile any operator-supplied patterns from the env var.

    Comma-separated regex *sources*. Each entry gets a synthesised
    ``name`` of ``env-extra-<index>`` so the structured log line can still
    name the matching pattern. Invalid regex sources are dropped with a
    warning — we never want a typoed env var to take ingestion offline.
    """
    raw = os.environ.get(_ENV_EXTRA, "").strip()
    if not raw:
        return ()
    out: list[BlocklistPattern] = []
    for idx, entry in enumerate(raw.split(",")):
        src = entry.strip()
        if not src:
            continue
        try:
            compiled = re.compile(src, re.IGNORECASE)
        except re.error as exc:
            logger.warning(
                "ingest.blocklist.bad_extra_pattern index=%d err=%s",
                idx,
                exc,
            )
            continue
        out.append(BlocklistPattern(name=f"env-extra-{idx}", regex=compiled))
    return tuple(out)


def _all_patterns() -> tuple[BlocklistPattern, ...]:
    """Seed patterns + any env-supplied extras.

    Re-reads the env on every call so tests can monkeypatch the variable
    without forcing a module reload. The cost is one ``os.environ.get`` +
    a small split per ``is_blocked`` call — negligible against the
    network round-trip the result gates.
    """
    return BLOCKLIST_PATTERNS + _compile_extra_from_env()


def is_blocked(
    url: str,
    title: str | None = None,
    *,
    source_provider: str | None = None,
) -> tuple[bool, str | None]:
    """Decide whether ``url`` (or ``title``) should be skipped at ingest.

    Args:
        url:             Candidate URL. Empty / falsy → not blocked
                         (caller already drops empty URLs upstream).
        title:           Optional page title. Tested against patterns
                         where ``match_title=True``.
        source_provider: Provider name (``tavily``, ``arxiv``, ``bazaar``,
                         ``twitsh``, ...). Surfaced in the structured log
                         line so the next dogfood report can attribute
                         filtered URLs to their origin.

    Returns:
        ``(blocked, pattern_name)``. ``pattern_name`` is the stable
        identifier of the first matching pattern (or an
        ``env-extra-<index>`` synthesised name) or ``None`` when nothing
        matched.
    """
    if not url:
        return False, None
    haystack = url
    title_haystack = title or ""
    for pat in _all_patterns():
        if pat.regex.search(haystack):
            logger.info(
                "ingest.blocklist.match url=%s pattern=%s source_provider=%s",
                url,
                pat.name,
                source_provider or "unknown",
            )
            return True, pat.name
        if pat.match_title and title_haystack and pat.regex.search(title_haystack):
            logger.info(
                "ingest.blocklist.match url=%s pattern=%s source_provider=%s match_field=title",
                url,
                pat.name,
                source_provider or "unknown",
            )
            return True, pat.name
    return False, None


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except (ValueError, TypeError):
        return ""


def _is_canonical_gecko_host(url: str) -> bool:
    host = _hostname(url)
    if not host:
        return False
    return any(
        host == h or host.endswith("." + h) or h in url.lower() for h in _CANONICAL_GECKO_HOSTS
    )


def flag_collision_risk(text: str, url: str) -> bool:
    """Return True when ``text`` mentions "Gecko" but ``url`` is off-domain.

    Soft signal — the retrieval layer does not consume this yet. Callers
    that build chunks may attach the result to ``metadata.collision_risk``
    so a future scoring pass can down-weight (or filter) ambiguous chunks.

    "Gecko" is matched case-insensitively as a whole word so we don't
    flag every chunk that happens to contain the substring (``geckos``,
    ``gecko-lizard``, etc. are still caught — that's intentional, the
    flag is advisory).
    """
    if not text:
        return False
    if _is_canonical_gecko_host(url):
        return False
    return bool(re.search(r"\bgecko\w*\b", text, re.IGNORECASE))


def annotate_collision_risk(chunks: list[Any], url: str) -> list[Any]:
    """Return new ProviderChunk records with ``metadata.collision_risk`` set.

    Imported lazily to avoid pulling ``ingestion.types`` (and its pydantic
    dep) into this module at import time. Pure function — input is not
    mutated. The returned list is a fresh sequence of ``ProviderChunk``
    dataclasses with merged metadata.

    The flag is advisory: callers may forward it to downstream consumers,
    but retrieval scoring does NOT consume ``collision_risk`` today.
    """
    if not chunks:
        return list(chunks)
    from .types import ProviderChunk  # local import — see module docstring

    out: list[Any] = []
    for c in chunks:
        if not isinstance(c, ProviderChunk):
            out.append(c)
            continue
        risk = flag_collision_risk(c.text, url)
        merged = dict(c.metadata or {})
        merged["collision_risk"] = bool(risk)
        out.append(
            ProviderChunk(
                resource_id=c.resource_id,
                chunk_index=c.chunk_index,
                text=c.text,
                metadata=merged,
            )
        )
    return out


__all__ = [
    "BLOCKLIST_PATTERNS",
    "BlocklistPattern",
    "annotate_collision_risk",
    "flag_collision_risk",
    "is_blocked",
]
