"""ArxivSource — free, structured paper-abstract provider.

Backed by the public Arxiv export API:
    http://export.arxiv.org/api/query?search_query=<q>&max_results=<n>

Why a dedicated provider (and not "just a Tavily query"):
    - Tavily returns raw HTML pages we then have to fetch + parse, often
      losing the Atom abstract entirely. Arxiv's Atom feed is parseable
      first-class — title, abstract, authors, arxiv_id, pdf_url all come
      back in a single <entry/>.
    - No key, no rate-limit pain at our session volumes.
    - The abstract is the citation. Each entry is a self-contained chunk
      already, so there is nothing to chunk client-side.

Gating policy:
    - Always fires when classify_idea() returns any of {crypto, defi,
      devtools, hackathon-team} — Arxiv has strong CS / cryptography /
      protocol-paper coverage there.
    - Also fires when the idea text matches one of the technical-paper
      signal patterns ("agent", "agentic", "protocol", "x402", "rag",
      "embedding", "verifiable", "zk", "consensus", "research", ...) —
      this catches ideas like the founder's "Gecko: makes judgment
      tradeable" run where the classifier returns ∅ but the topic is
      research-adjacent.

The provider returns a ``SourceResult`` whose ``payload["chunks"]`` is a
list of dicts shaped like a ``BazaarChunk`` so the downstream rendering
+ economics ledger code paths don't fork on provider type. Cost is
always 0.0 (free).
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote_plus

import httpx

from gecko_core.sources import SourceResult

logger = logging.getLogger(__name__)


ARXIV_API_BASE: str = "http://export.arxiv.org/api/query"

# Atom namespaces in the Arxiv response.
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"

# Default per-call abstract cap. Tunable via env in the workflows
# rebalance step (GECKO_PROVIDER_QUOTA_ARXIV).
DEFAULT_MAX_RESULTS: int = 5

# HTTP timeout: Arxiv's mirror is generally fast (<1s) but can lag during
# their nightly index rebuild. 8s is plenty without holding up dispatch.
DEFAULT_TIMEOUT_SECONDS: float = 8.0

# Categories where Arxiv is reliably useful. Other classifications still
# run via the keyword-signal fallback below.
_CATEGORY_FIRES_FOR: frozenset[str] = frozenset({"crypto", "defi", "devtools", "hackathon-team"})

# Idea-text signals that flag a research-adjacent topic when the
# classifier returned ∅. Lowercased substring match; conservative so we
# don't fire on every SaaS idea that mentions "AI".
_TECHNICAL_SIGNALS: tuple[str, ...] = (
    "agent",
    "agentic",
    "protocol",
    "x402",
    "rag",
    "retrieval-augmented",
    "embedding",
    "verifiable",
    "zk",
    "zero-knowledge",
    "consensus",
    "research",
    "paper",
    "benchmark",
    "transformer",
    "diffusion",
    "llm",
    "mcp",
    "judgment",
    "judge",
    "marketplace",
    "tradeable",
    "tradable",
)


def _idea_has_technical_signal(idea: str) -> bool:
    text = (idea or "").lower()
    return any(sig in text for sig in _TECHNICAL_SIGNALS)


# Same stopword list shape as twit_sh — keep the keyword extraction
# behaviour predictable across providers so debugging tools aren't
# guessing which stop-list ran.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "for",
        "to",
        "of",
        "in",
        "on",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "by",
        "at",
        "from",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "as",
        "into",
        "than",
        "you",
        "your",
        "we",
        "our",
        "they",
        "their",
        "i",
        "me",
        "my",
        "use",
        "uses",
        "using",
        "make",
        "makes",
        "making",
        "build",
        "builds",
    }
)


def _build_query(idea: str, *, max_keywords: int = 5) -> str:
    """Extract a small keyword set from the idea for arxiv search_query.

    Arxiv's search_query is a Lucene-style expression; we use ``all:term``
    AND'd together so each keyword has to appear somewhere in the
    title/abstract/comments of returned papers. Conservative match width
    — broad enough to surface candidates, narrow enough to avoid the
    "popular phrase that returns 10000 hits" failure mode.
    """
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", (idea or "").lower())
    seen: set[str] = set()
    kept: list[str] = []
    # Prefer longer tokens — they discriminate better than 3-letter words.
    for t in sorted(tokens, key=len, reverse=True):
        if t in _STOPWORDS or t in seen:
            continue
        seen.add(t)
        kept.append(t)
        if len(kept) >= max_keywords:
            break
    if not kept:
        # Degrade to the raw idea (clamped) so we still get *something*.
        return quote_plus((idea or "").strip()[:120] or "research")
    return "+AND+".join(f"all:{quote_plus(t)}" for t in kept)


def _parse_atom(xml_text: str) -> list[dict[str, Any]]:
    """Parse an Arxiv Atom feed into a list of normalized entry dicts.

    Returns an empty list on parse error — Arxiv occasionally serves a
    truncated body during their index rebuild and we'd rather degrade
    silently than fail the dispatch.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("arxiv: parse error: %s", exc)
        return []

    out: list[dict[str, Any]] = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        title_el = entry.find(f"{_ATOM_NS}title")
        summary_el = entry.find(f"{_ATOM_NS}summary")
        id_el = entry.find(f"{_ATOM_NS}id")
        published_el = entry.find(f"{_ATOM_NS}published")

        title = (title_el.text or "").strip() if title_el is not None else ""
        summary = (summary_el.text or "").strip() if summary_el is not None else ""
        full_id = (id_el.text or "").strip() if id_el is not None else ""
        published = (published_el.text or "").strip() if published_el is not None else ""

        # ``id`` is the abs-page URL; the arxiv_id is its trailing segment.
        arxiv_id = full_id.rsplit("/", 1)[-1] if full_id else ""

        authors: list[str] = []
        for author in entry.findall(f"{_ATOM_NS}author"):
            name_el = author.find(f"{_ATOM_NS}name")
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip())

        pdf_url = ""
        for link in entry.findall(f"{_ATOM_NS}link"):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                href = link.get("href") or ""
                if href:
                    pdf_url = href
                    break

        primary_category_el = entry.find(f"{_ARXIV_NS}primary_category")
        primary_category = (
            primary_category_el.get("term") if primary_category_el is not None else ""
        )

        if not (title or summary):
            continue

        out.append(
            {
                "title": title,
                "abstract": summary,
                "authors": authors,
                "arxiv_id": arxiv_id,
                "abs_url": full_id,
                "pdf_url": pdf_url,
                "published_date": published,
                "primary_category": primary_category,
            }
        )
    return out


def _entry_to_chunk(entry: dict[str, Any]) -> dict[str, Any]:
    """Render an Arxiv entry as a chunk-shaped dict for the dispatcher.

    ``provider_kind="free:arxiv"`` mirrors the Bazaar adapter convention
    (``bazaar:<adapter>``). Citation reads as ``arxiv:<id>``; the full
    abs_url stays in metadata so the renderer can surface it as a link.
    """
    arxiv_id = entry.get("arxiv_id", "")
    title = entry.get("title", "")
    abstract = entry.get("abstract", "")
    citation_id = f"arxiv:{arxiv_id}" if arxiv_id else "arxiv:unknown"

    text = f"{title}\n\n{abstract}".strip() if title or abstract else ""

    return {
        "text": text,
        "provider_kind": "free:arxiv",
        "cost_usd": "0",
        "metadata": {
            "citation_id": citation_id,
            "title": title,
            "authors": entry.get("authors", []),
            "arxiv_id": arxiv_id,
            "abs_url": entry.get("abs_url", ""),
            "pdf_url": entry.get("pdf_url", ""),
            "published_date": entry.get("published_date", ""),
            "primary_category": entry.get("primary_category", ""),
        },
        "creator_handle": None,
    }


class ArxivSource:
    """Free Arxiv provider conforming to the ``Source`` Protocol."""

    name: str = "arxiv"

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        max_results: int = DEFAULT_MAX_RESULTS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        api_base: str = ARXIV_API_BASE,
    ) -> None:
        self._http = http_client
        self._owns_http = http_client is None
        self._max_results = int(max_results)
        self._timeout = float(timeout_seconds)
        self._api_base = api_base

    async def applies_to(self, *, categories: set[str], idea: str = "") -> bool:
        """True for technical/research-adjacent ideas.

        The ``Source`` Protocol's ``applies_to`` is keyword-only and
        canonical signature is ``(*, categories)``. ``idea`` is an
        optional extension we accept for the keyword-signal fallback;
        the dispatcher passes ``categories`` only, so when called via
        ``dispatch_sources`` the keyword fallback is *not* exercised.
        Callers wiring Arxiv in want to pass ``idea`` directly via
        ``fetch`` or via this method on a typed reference.
        """
        if categories & _CATEGORY_FIRES_FOR:
            return True
        return _idea_has_technical_signal(idea)

    async def fetch(self, *, idea: str, categories: set[str]) -> SourceResult:
        # Categorical OR keyword-signal — re-check so a direct caller that
        # bypassed ``applies_to`` still gets the right gating + a clean
        # ``fired=False`` instead of a wasted HTTP round-trip.
        if not (categories & _CATEGORY_FIRES_FOR or _idea_has_technical_signal(idea)):
            return SourceResult(
                source_name=self.name,
                payload={"chunks": []},
                fired=False,
            )

        query = _build_query(idea)
        url = (
            f"{self._api_base}?search_query={query}"
            f"&start=0&max_results={self._max_results}"
            "&sortBy=relevance&sortOrder=descending"
        )

        owns_local = False
        client = self._http
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            owns_local = True

        try:
            try:
                resp = await client.get(url)
            except httpx.HTTPError as exc:
                return SourceResult(
                    source_name=self.name,
                    payload={"chunks": []},
                    fired=False,
                    error=f"arxiv: http error: {type(exc).__name__}: {exc}",
                )

            if resp.status_code >= 400:
                return SourceResult(
                    source_name=self.name,
                    payload={"chunks": []},
                    fired=False,
                    error=f"arxiv: HTTP {resp.status_code}",
                )

            entries = _parse_atom(resp.text)
        finally:
            if owns_local:
                await client.aclose()

        if not entries:
            return SourceResult(
                source_name=self.name,
                payload={"chunks": []},
                fired=False,
                error="arxiv: empty result set",
            )

        chunks = [_entry_to_chunk(e) for e in entries[: self._max_results]]
        return SourceResult(
            source_name=self.name,
            payload={
                "chunks": chunks,
                "query": query,
                "abstract_count": len(chunks),
            },
            cost_usd=0.0,
            fired=True,
        )

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None


def make_arxiv_source(
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    http_client: httpx.AsyncClient | None = None,
) -> ArxivSource:
    """Factory consistent with ``make_bazaar_provider`` + ``TwitshSource``."""
    return ArxivSource(http_client=http_client, max_results=max_results)


__all__ = [
    "ARXIV_API_BASE",
    "DEFAULT_MAX_RESULTS",
    "ArxivSource",
    "_build_query",
    "_idea_has_technical_signal",
    "_parse_atom",
    "make_arxiv_source",
]
