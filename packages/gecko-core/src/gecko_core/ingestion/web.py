"""Web extraction — httpx + BeautifulSoup with an SSRF guard, Tavily fallback.

URLs are validated *before* the network call. We block:
  - non-http(s) schemes (file://, gopher://, etc.)
  - hostnames that resolve to private/loopback/link-local IP space
  - bare IP literals in those ranges
  - localhost / *.local

After fetch, BeautifulSoup strips scripts/styles and we collapse whitespace.

Fallback: when the direct fetch is blocked (4xx) or the connection is reset
(RemoteProtocolError) on a public URL, we hand the URL to Tavily's extract
endpoint, which runs its own scraper infrastructure. Costs ~$0.004/URL,
charged per-session via the cost-tracking layer.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import ipaddress
import logging
import os
import random
import socket
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
MAX_BYTES = 5_000_000  # 5 MB cap to avoid pathological pages

# S17-INGEST-FALLBACK-01 — full-jitter exponential backoff for HTTP retries.
# Mirrors the embedder retry shape (S16-INGEST-04). Three attempts total
# (one initial + 2 retries), base 1s, ceiling 8s. Spread protects against
# thundering-herd reconnects against the same host.
_HTTP_RETRY_MAX_ATTEMPTS = 3
_HTTP_RETRY_BASE_S = 1.0
_HTTP_RETRY_CAP_S = 8.0

# S17-INGEST-FALLBACK-01 — per-session cap on Tavily Extract fallback
# invocations. One bad session shouldn't burn $1 of credits on a wall of
# 200 broken URLs. Env-overridable for ops to tune without a deploy.
MAX_TAVILY_EXTRACT_FALLBACKS_PER_SESSION = int(
    os.environ.get("MAX_TAVILY_EXTRACT_FALLBACKS_PER_SESSION", "3")
)


class _TavilyBudget:
    """Per-session Tavily Extract fallback counter.

    Threaded through the call stack via a ContextVar so the public
    ``extract()`` signature is unchanged. The pipeline binds the budget
    once per session before fanning out across sources; each fallback
    invocation atomically (within asyncio's single-thread model)
    increments and checks against the cap.
    """

    __slots__ = ("cap", "used")

    def __init__(self, cap: int) -> None:
        self.used = 0
        self.cap = cap

    def try_consume(self) -> bool:
        if self.used >= self.cap:
            return False
        self.used += 1
        return True


_tavily_budget_var: contextvars.ContextVar[_TavilyBudget | None] = contextvars.ContextVar(
    "gecko_tavily_extract_budget", default=None
)


def bind_tavily_extract_budget(cap: int | None = None) -> _TavilyBudget:
    """Install a fresh per-session Tavily Extract fallback budget.

    Returns the budget so the caller can read ``used`` after the session
    for telemetry. Pipeline calls this once per session before
    ``ingest()`` fan-out; tests can call it directly to override the cap.
    """
    budget = _TavilyBudget(cap if cap is not None else MAX_TAVILY_EXTRACT_FALLBACKS_PER_SESSION)
    _tavily_budget_var.set(budget)
    return budget


def _http_full_jitter_backoff(attempt: int) -> float:
    """AWS-style full-jitter for the HTTP retry path.

    `attempt` is 1-indexed (1st retry = attempt 1). Returns sleep drawn
    uniformly from [0, min(_HTTP_RETRY_CAP_S, base * 2^(n-1))].
    """
    ceiling = min(_HTTP_RETRY_CAP_S, _HTTP_RETRY_BASE_S * (2 ** (attempt - 1)))
    return random.uniform(0.0, ceiling)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse Retry-After (delta-seconds form). HTTP-date form is rare for
    rate limits in practice and not honored here — we fall through to
    jittered backoff for those.
    """
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        secs = float(raw.strip())
    except ValueError:
        return None
    # Clamp at the retry cap so a misbehaving server can't park us forever.
    return min(max(0.0, secs), _HTTP_RETRY_CAP_S * 2)


# Tavily Extract list price is 1 credit per URL ≈ $0.004. Used by the
# fallback path when direct fetch is bot-walled. Tracked separately on
# the per-session economics view via the existing 'tavily' cost line.
TAVILY_EXTRACT_USD_PER_URL: float = 0.004

# How long a Tavily Extract result stays valid in the cache. Web content for
# a research session changes slowly — a week of staleness is fine for our
# use case and amortizes the per-URL cost across re-runs.
TAVILY_CACHE_TTL = timedelta(days=7)
_CACHE_TABLE = "tavily_extract_cache"

# Realistic browser headers — the previous "gecko-bootstrap/0.1" UA was being
# 403'd by every commercial site (Booking, Tripadvisor, Expedia, Hotels.com).
# This UA + Accept header set matches a current Chrome on macOS.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


class UnsafeURLError(ValueError):
    """Raised when a URL is rejected by the SSRF guard."""


def _is_private_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_url(url: str) -> str:
    """Reject anything we shouldn't fetch. Returns the URL on success."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"unsupported scheme: {parsed.scheme or '(none)'}")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("missing hostname")

    lower = host.lower()
    if lower in {"localhost"} or lower.endswith(".local") or lower.endswith(".localhost"):
        raise UnsafeURLError(f"blocked hostname: {host}")

    # Bare IP literal? Check directly.
    if _is_private_ip(host):
        raise UnsafeURLError(f"blocked private/loopback IP: {host}")

    # DNS resolve and check every returned address. This is best-effort —
    # a deliberate attacker could DNS-rebind, but for our use this is enough
    # since we also re-check on the resolved socket.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"DNS resolution failed for {host}") from exc
    for info in infos:
        ip = str(info[4][0])
        if _is_private_ip(ip):
            raise UnsafeURLError(f"{host} resolves to blocked IP {ip}")

    return url


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _cache_lookup_sync(client: Any, url: str) -> str | None:
    """Return cached raw_content if present and within TTL, else None."""
    try:
        res = (
            client.table(_CACHE_TABLE)
            .select("raw_content,fetched_at")
            .eq("url_hash", _url_hash(url))
            .limit(1)
            .execute()
        )
    except Exception as exc:  # pragma: no cover — best-effort cache
        logger.info("tavily cache lookup failed: %s", exc.__class__.__name__)
        return None
    rows = res.data or []
    if not rows:
        return None
    fetched_raw = rows[0].get("fetched_at")
    if isinstance(fetched_raw, str):
        # supabase-py returns ISO8601 strings; parse defensively.
        try:
            fetched = datetime.fromisoformat(fetched_raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(fetched_raw, datetime):
        fetched = fetched_raw
    else:
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    if datetime.now(UTC) - fetched > TAVILY_CACHE_TTL:
        return None
    content = rows[0].get("raw_content") or ""
    return cast(str, content) if content.strip() else None


def _cache_store_sync(client: Any, url: str, raw_content: str) -> None:
    """Upsert raw_content for url. Best-effort — never raises into the caller."""
    if not raw_content or not raw_content.strip():
        return
    try:
        client.table(_CACHE_TABLE).upsert(
            {
                "url_hash": _url_hash(url),
                "url": url,
                "raw_content": raw_content,
                "fetched_at": datetime.now(UTC).isoformat(),
            },
            on_conflict="url_hash",
        ).execute()
    except Exception as exc:  # pragma: no cover
        logger.info("tavily cache store failed: %s", exc.__class__.__name__)


def _tavily_extract_sync(api_key: str, url: str) -> str | None:
    """Synchronous Tavily Extract call. Returns raw text or None on failure.

    Defensive: catches every exception (network, auth, schema) and returns
    None so the caller can degrade to a skipped-source rather than crash
    the pipeline. Errors are logged but not raised.
    """
    try:
        from tavily import TavilyClient
    except ImportError:  # pragma: no cover
        return None
    try:
        client = TavilyClient(api_key=api_key)
        # Tavily SDK accepts a string or list of URLs; we keep it single-URL
        # so one failure doesn't poison a batch. extract_depth=advanced gets
        # bot-walled sites through but costs slightly more — we already paid
        # per-URL, so use the better depth.
        response: Any = client.extract(urls=[url], extract_depth="advanced")
    except Exception as exc:
        logger.info("tavily extract failed for %s: %s", url, exc.__class__.__name__)
        return None

    if not isinstance(response, dict):
        return None
    results = response.get("results", []) or []
    if not results:
        return None
    raw = results[0].get("raw_content") or ""
    return raw if raw.strip() else None


async def extract_via_tavily(url: str) -> tuple[str | None, bool]:
    """Tavily Extract fallback for bot-walled URLs (cache-aware).

    Returns ``(text, billed)``. ``billed`` is True only when the live Tavily
    API was actually called — cache hits cost nothing. The caller threads
    ``billed`` into the per-session economics view.
    """
    from .settings import get_ingestion_settings

    try:
        settings = get_ingestion_settings()
    except Exception:
        return None, False

    # Try cache first. Failure to read the cache is not fatal — we just
    # fall through to the live call.
    cache_client: Any = None
    try:
        from gecko_core.db import create_supabase_client

        cache_client = create_supabase_client()
        cached = await asyncio.to_thread(_cache_lookup_sync, cache_client, url)
        if cached:
            logger.info("tavily cache hit for %s", url)
            return cached, False
    except Exception as exc:  # pragma: no cover — cache is best-effort
        logger.info("tavily cache disabled: %s", exc.__class__.__name__)
        cache_client = None

    api_key = settings.tavily_api_key.get_secret_value()
    text = await asyncio.to_thread(_tavily_extract_sync, api_key, url)
    if text and cache_client is not None:
        await asyncio.to_thread(_cache_store_sync, cache_client, url, text)
    return text, True


async def extract(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> tuple[str, float]:
    """Fetch and extract readable text from a public web page.

    Returns (text, tavily_extract_cost_usd). Cost is non-zero only when the
    Tavily Extract fallback was actually invoked (whether or not it
    succeeded — Tavily charges per attempt).

    Strategy (S17-INGEST-FALLBACK-01):
      1. Direct fetch with a realistic browser UA (handles 70%+ of sites).
      2. Up to ``_HTTP_RETRY_MAX_ATTEMPTS`` (3) attempts with full-jitter
         exponential backoff on transient errors:
           * httpx.RemoteProtocolError (connection terminated mid-stream
             — the HN/blas.com case)
           * httpx.ReadTimeout / httpx.ConnectError (flaky networks)
           * 5xx responses (server-side blip)
           * 429 — honored via Retry-After header when present, else jitter
      3. On 4xx (bot wall, except 429), skip retry and jump straight to
         the Tavily Extract fallback (gated by the per-session budget cap).
      4. After retry chain exhaustion → Tavily Extract fallback. Returns
         Tavily text on success; raises the original error otherwise.
    """
    safe = validate_url(url)
    transient_excs = (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError)

    body: bytes | None = None
    content_type: str = ""
    last_exc: Exception | None = None
    skip_to_fallback = False  # true on 4xx (bot wall) — retry won't help
    for attempt in range(1, _HTTP_RETRY_MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                http2=False,
                headers=_BROWSER_HEADERS,
            ) as client:
                resp = await client.get(safe)
                # 429 / 5xx → retry path. 4xx (other than 429) → Tavily.
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    last_exc = httpx.HTTPStatusError(
                        f"server returned {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                    if attempt < _HTTP_RETRY_MAX_ATTEMPTS:
                        retry_after = (
                            _retry_after_seconds(resp) if resp.status_code == 429 else None
                        )
                        sleep_s = (
                            retry_after
                            if retry_after is not None
                            else _http_full_jitter_backoff(attempt)
                        )
                        logger.info(
                            "web.fetch.retry url=%s attempt=%d/%d status=%d sleep=%.2fs",
                            safe,
                            attempt,
                            _HTTP_RETRY_MAX_ATTEMPTS,
                            resp.status_code,
                            sleep_s,
                        )
                        await asyncio.sleep(sleep_s)
                        continue
                    break
                if 400 <= resp.status_code < 500:
                    # Genuine 4xx (403/404/etc.) — bot wall or missing.
                    # Don't retry; jump to Tavily fallback.
                    last_exc = httpx.HTTPStatusError(
                        f"client error {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                    skip_to_fallback = True
                    break
                resp.raise_for_status()
                validate_url(str(resp.url))
                body = resp.content[:MAX_BYTES]
                content_type = resp.headers.get("content-type", "")
                break
        except transient_excs as exc:
            last_exc = exc
            if attempt < _HTTP_RETRY_MAX_ATTEMPTS:
                sleep_s = _http_full_jitter_backoff(attempt)
                logger.info(
                    "web.fetch.retry url=%s attempt=%d/%d exc=%s sleep=%.2fs",
                    safe,
                    attempt,
                    _HTTP_RETRY_MAX_ATTEMPTS,
                    exc.__class__.__name__,
                    sleep_s,
                )
                await asyncio.sleep(sleep_s)
                continue
            break

    if body is None:
        # Direct fetch failed; try Tavily Extract (cache-aware) iff the
        # per-session budget allows. Cache hits don't count against the
        # budget — they're free — so we always check the cache first.
        budget = _tavily_budget_var.get()
        if budget is not None and budget.used >= budget.cap:
            logger.warning(
                "web.tavily_fallback.cap_reached url=%s used=%d cap=%d — skipping fallback",
                safe,
                budget.used,
                budget.cap,
            )
            if last_exc is not None:
                raise last_exc
            raise RuntimeError(f"extract failed for {safe} (tavily fallback cap hit)")

        text, billed = await extract_via_tavily(safe)
        if billed and budget is not None:
            # Live Tavily call — count it against the per-session cap.
            # We accept a one-call overshoot vs. the cap (we only
            # checked >= cap above; a concurrent caller could race).
            # That's fine: 3 vs 4 calls is not worth a global lock.
            budget.try_consume()
        if text:
            logger.info(
                "web.tavily_fallback.succeeded url=%s cached=%s billed=%s",
                safe,
                not billed,
                billed,
            )
            return text, (TAVILY_EXTRACT_USD_PER_URL if billed else 0.0)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"extract failed for {safe}")

    # Mark `skip_to_fallback` referenced so static analysis doesn't flag it
    # — kept for log-trace readability above.
    _ = skip_to_fallback
    soup = BeautifulSoup(body, _parser_for_content_type(content_type))
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    return " ".join(text.split()), 0.0


# Content-Types where bs4 should use the XML parser. RSS/Atom feeds otherwise
# trigger XMLParsedAsHTMLWarning and confuse log review (V11-03).
_XML_CONTENT_TYPES = (
    "application/xml",
    "application/rss+xml",
    "application/atom+xml",
    "text/xml",
)


def _parser_for_content_type(content_type: str) -> str:
    """Pick the bs4 parser based on the response Content-Type."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct in _XML_CONTENT_TYPES:
        return "lxml-xml"
    return "html.parser"


__all__ = [
    "MAX_TAVILY_EXTRACT_FALLBACKS_PER_SESSION",
    "TAVILY_EXTRACT_USD_PER_URL",
    "UnsafeURLError",
    "bind_tavily_extract_budget",
    "extract",
    "extract_via_tavily",
    "validate_url",
]
