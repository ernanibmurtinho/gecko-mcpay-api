"""Arxiv free SourceProvider.

Public Arxiv export API — no key, no per-IP rate caps for our volumes.
S17-DISCOVERY-01 introduces this as the *structured* counterweight to
Tavily for technical / research / web3-protocol / agentic-economy ideas.
"""

from __future__ import annotations

from gecko_core.sources.arxiv.provider import ArxivSource, make_arxiv_source

__all__ = ["ArxivSource", "make_arxiv_source"]
