"""GitHub URL adapter (S6-V2-02).

Two URL shapes:

  * **Repo root** (``github.com/<owner>/<repo>`` or ``.../tree/<branch>``)
    → fetch README from ``raw.githubusercontent.com``. Tries common
    branches (``main`` then ``master``) and common file names.
  * **Discussion** (``github.com/<owner>/<repo>/discussions/<n>``) →
    REST API ``/repos/{owner}/{repo}/discussions/{n}``. Returns
    discussion body + top-level comments. Token via ``GITHUB_TOKEN`` env
    if available — falls back to anonymous (lower rate limit).

Plain-text return; pipeline handles chunking + embedding.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

import httpx

DEFAULT_TIMEOUT_S = 15.0
README_CANDIDATES = ("README.md", "README.MD", "Readme.md", "readme.md", "README")
README_BRANCHES = ("main", "master")

_REPO_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/?$")
_REPO_TREE_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/tree/(?P<branch>[^/]+)/?$")
_DISCUSSION_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/discussions/(?P<num>\d+)/?$")


def _is_github_host(host: str) -> bool:
    return host.lower() in {"github.com", "www.github.com"}


def matches(url: str) -> bool:
    """True iff ``url`` is a supported GitHub repo or discussion URL."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if not _is_github_host(parsed.hostname or ""):
        return False
    path = parsed.path or ""
    return bool(_REPO_RE.match(path) or _REPO_TREE_RE.match(path) or _DISCUSSION_RE.match(path))


def _classify(url: str) -> tuple[str, dict[str, str]]:
    parsed = urlparse(url)
    path = parsed.path or ""
    if (m := _DISCUSSION_RE.match(path)) is not None:
        return "discussion", m.groupdict()
    if (m := _REPO_TREE_RE.match(path)) is not None:
        return "repo", m.groupdict()
    if (m := _REPO_RE.match(path)) is not None:
        d = m.groupdict()
        d["branch"] = ""
        return "repo", d
    raise ValueError(f"unsupported github url: {url}")


def _gh_headers(*, json_api: bool) -> dict[str, str]:
    headers: dict[str, str] = {"User-Agent": "Gecko/0.1 (gecko-mcpay-api)"}
    if json_api:
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _fetch_readme(client: httpx.AsyncClient, owner: str, repo: str, branch_hint: str) -> str:
    """Try branch_hint then README_BRANCHES, README_CANDIDATES, return body or ''."""
    branches: tuple[str, ...] = (branch_hint, *README_BRANCHES) if branch_hint else README_BRANCHES
    seen: set[str] = set()
    for branch in branches:
        if branch in seen:
            continue
        seen.add(branch)
        for fname in README_CANDIDATES:
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{fname}"
            try:
                resp = await client.get(url, headers=_gh_headers(json_api=False))
            except httpx.HTTPError:
                continue
            if resp.status_code == 200 and resp.text.strip():
                return resp.text
    return ""


async def _fetch_discussion(client: httpx.AsyncClient, owner: str, repo: str, num: str) -> str:
    """REST: discussion body + top-level comments. Empty string on failure."""
    base = f"https://api.github.com/repos/{owner}/{repo}/discussions/{num}"
    try:
        d_resp = await client.get(base, headers=_gh_headers(json_api=True))
        d_resp.raise_for_status()
        d = d_resp.json()
    except (httpx.HTTPError, ValueError):
        return ""

    parts: list[str] = []
    title = d.get("title") if isinstance(d, dict) else None
    body = d.get("body") if isinstance(d, dict) else None
    user = (d.get("user") or {}).get("login") if isinstance(d, dict) else None
    state = d.get("state") if isinstance(d, dict) else None
    if title:
        parts.append(f"# {title}")
    if user or state:
        parts.append(f"opened by {user or 'unknown'} ({state or 'unknown'})")
    if isinstance(body, str) and body.strip():
        parts.append(body.strip())

    try:
        c_resp = await client.get(f"{base}/comments", headers=_gh_headers(json_api=True))
        c_resp.raise_for_status()
        comments_raw = c_resp.json()
    except (httpx.HTTPError, ValueError):
        comments_raw = []

    if isinstance(comments_raw, list) and comments_raw:
        parts.append("## Comments")
        for c in comments_raw:
            if not isinstance(c, dict):
                continue
            cu = (c.get("user") or {}).get("login") or "unknown"
            cb = c.get("body")
            if isinstance(cb, str) and cb.strip():
                parts.append(f"@{cu}: {cb.strip()}")
    return "\n\n".join(parts)


async def extract(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> tuple[str, float]:
    """Fetch a GitHub repo README or discussion thread.

    Returns (text, cost_usd). Cost is always 0 — GitHub raw + REST anon
    are free. SSRF-validated. Raises ``ValueError`` on unsupported URLs.
    """
    if not matches(url):
        raise ValueError(f"not a github url: {url}")
    # Lazy import — see reddit.py for the same pipeline ↔ sources cycle
    # rationale.
    from gecko_core.ingestion.web import validate_url

    validate_url(url)
    kind, parts = _classify(url)

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    assert client is not None
    try:
        if kind == "repo":
            text = await _fetch_readme(
                client, parts["owner"], parts["repo"], parts.get("branch", "")
            )
        else:
            text = await _fetch_discussion(client, parts["owner"], parts["repo"], parts["num"])
    finally:
        if owns_client:
            await client.aclose()
    return text, 0.0


__all__ = ["extract", "matches"]
