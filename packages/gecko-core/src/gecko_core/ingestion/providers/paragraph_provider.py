"""ParagraphProvider — Paragraph MCP wrapped as a `SourceProvider`.

Sprint 14 S14-PARA-01. Per ``docs/strategy/paragraph-publish-new-expansion-2026-04-30.md``
Surface A: Paragraph posts are ingested as a `SourceProvider` instance via
the S12-PROVIDER-01 seam, surfacing creator attribution into the citation
footer that S13-CITE-01 pre-paid.

Wire shape (probed Sprint 12, ``sprint-12-chore-probes-2026-04-30.md``):
- ``mcp.paragraph.com`` is the hosted Paragraph MCP, OAuth-Bearer-gated.
- Tool call: ``posts.search`` (or equivalent) returns post records with
  ``author`` (handle), ``url`` (permalink), ``title``, and ``excerpt``.
- Per-fetch cost is read from the MCP's pricing schema if advertised;
  otherwise we fall back to the ``DEFAULT_PER_FETCH_USD`` placeholder so
  the dispatcher's budget enforcement has a value to work with.

OAuth bootstrap (S14-PARA-AUTH-01):
- ``ParagraphTokenStore`` reads/writes a JSON blob at
  ``~/.gecko/paragraph_token.json`` so the CLI ``paragraph login --token
  <api-key>`` flow can persist credentials between runs without leaking
  through env vars.
- Two-tier resolution: env (``PARAGRAPH_API_KEY``) wins if set, else the
  on-disk file. Missing-token paths surface as ``ProviderHealth(available
  =False)`` and an empty ``fetch()`` rather than a hard pipeline halt
  (matches the `degraded_sources` pattern from the FreeProvider).

Settle hop:
- The actual creator-payout settle is **S14-PARA-02** (web3-engineer's
  lane, firing in parallel). This module emits ``creator_handle`` in the
  chunk metadata and lets the citation-renderer aggregate; it does NOT
  fire on-chain payouts itself.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from gecko_core.models import SourceCandidate

from . import ProviderHealth, ProviderKind, SourceChunk

logger = logging.getLogger(__name__)


# Per-fetch cost when the MCP doesn't advertise its own pricing schema.
# Conservative placeholder — real pricing is whatever Paragraph's
# x402v2 challenge returns at fetch time (S14-PARA-02 reads it from the
# challenge directly). $0.05 keeps the dispatcher's budget pre-check
# reasonable without blowing past the per-session creator-payout cap.
DEFAULT_PER_FETCH_USD: float = 0.05

# Default Paragraph MCP base URL. Operator can override via env when
# routing through a staging facilitator.
DEFAULT_PARAGRAPH_MCP_URL: str = "https://mcp.paragraph.com/mcp"

# Persistent token cache. Lives under the user's home rather than CWD so
# multiple Gecko CLI invocations from different directories share a
# single credential. Permissions are 0600 — see ``ParagraphTokenStore``.
DEFAULT_TOKEN_PATH: Path = Path.home() / ".gecko" / "paragraph_token.json"


class ParagraphTokenStore:
    """Persistent storage for the Paragraph OAuth Bearer token.

    File format is a small JSON blob — easier to extend later (refresh
    tokens, expiry timestamps) than a bare text file. We deliberately
    don't carry an expiry today: Paragraph's API keys are long-lived,
    and a wrong key surfaces as a 401 on the first ``fetch`` call.

    Resolution order on read:
      1. ``PARAGRAPH_API_KEY`` env var (highest precedence so CI / ECS
         can inject the token without writing to disk).
      2. The on-disk JSON blob at ``DEFAULT_TOKEN_PATH``.
      3. ``None`` — the provider reports unavailable and the dispatcher
         skips it cleanly via ``degraded_sources``.
    """

    def __init__(self, path: Path = DEFAULT_TOKEN_PATH) -> None:
        self._path = path

    def load(self) -> str | None:
        env = os.environ.get("PARAGRAPH_API_KEY")
        if env:
            return env
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("paragraph_token: failed to read %s: %s", self._path, exc)
            return None
        token = data.get("api_key") or data.get("token")
        if not isinstance(token, str) or not token.strip():
            return None
        return token

    def save(self, token: str) -> None:
        """Write the token to disk with restrictive permissions.

        Creates ``~/.gecko/`` if missing. The restrictive 0600 mode is
        belt-and-suspenders — anyone with shell access can already read
        the file, but we don't make it world-readable just because the
        parent dir was created with the umask default.
        """
        if not token or not token.strip():
            raise ValueError("paragraph_token: refusing to persist an empty token")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"api_key": token}))
        # Belt-and-suspenders 0600. Non-POSIX filesystems (Windows
        # without ACL support) silently ignore chmod — that's fine; the
        # token is still gated by the parent dir's umask.
        with contextlib.suppress(OSError):  # pragma: no cover — non-POSIX
            self._path.chmod(0o600)


class ParagraphAuthError(RuntimeError):
    """Surfaced when the MCP rejects our OAuth Bearer (401/403)."""


class ParagraphProvider:
    """`SourceProvider` adapter over ``mcp.paragraph.com``.

    Conforms structurally to the S12-PROVIDER-01 Protocol. The actual
    Paragraph MCP wire calls are encapsulated in private methods so a
    test can swap the underlying ``httpx.AsyncClient`` for a stub (see
    ``tests/ingestion/test_paragraph_provider.py``).

    Class attrs match the Protocol:
      * ``name = "paragraph"`` — surfaced on Provenance.provider_name.
      * ``kind = "x402-bazaar"`` — paid bazaar source; the dispatcher's
        budget enforcement and the verdict renderer treat it as such.
    """

    name: str = "paragraph"
    kind: ProviderKind = "x402-bazaar"

    def __init__(
        self,
        *,
        token_store: ParagraphTokenStore | None = None,
        base_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        per_fetch_usd: float | None = None,
    ) -> None:
        self._token_store = token_store or ParagraphTokenStore()
        self._base_url = (
            base_url or os.environ.get("PARAGRAPH_MCP_URL") or DEFAULT_PARAGRAPH_MCP_URL
        )
        self._http = http_client
        # Operator can override the placeholder via env — useful when the
        # MCP's pricing schema isn't yet readable but the operator knows
        # the real per-call price from a manual probe.
        env_price = os.environ.get("PARAGRAPH_PER_FETCH_USD")
        if per_fetch_usd is not None:
            self._per_fetch_usd = per_fetch_usd
        elif env_price:
            try:
                self._per_fetch_usd = float(env_price)
            except ValueError:
                self._per_fetch_usd = DEFAULT_PER_FETCH_USD
        else:
            self._per_fetch_usd = DEFAULT_PER_FETCH_USD

    # ------------------------------------------------------------------
    # SourceProvider Protocol surface
    # ------------------------------------------------------------------

    async def cost_estimate(self, query: str) -> float:
        """Return the per-fetch USD cost.

        First tries the MCP's pricing schema (``GET /pricing``); falls
        back to the ctor's ``per_fetch_usd`` placeholder on any failure.
        We **never** raise here — the dispatcher uses this purely for
        budget pre-checks and a missing pricing endpoint shouldn't break
        the discover phase.
        """
        token = self._token_store.load()
        if not token:
            return self._per_fetch_usd
        try:
            client = self._build_http()
            owns = self._http is None
            try:
                resp = await client.get(
                    "/pricing",
                    headers=self._auth_headers(token),
                )
            finally:
                if owns:
                    await client.aclose()
        except Exception:
            return self._per_fetch_usd
        if resp.status_code != 200:
            return self._per_fetch_usd
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            return self._per_fetch_usd
        # Accept a few common pricing-schema shapes; the MCP catalog
        # isn't standardised and we don't want to brittle-couple to one.
        if isinstance(body, dict):
            for key in ("posts.search", "search", "fetch", "per_call_usd"):
                value = body.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    return float(value)
        return self._per_fetch_usd

    async def health(self) -> ProviderHealth:
        """Reachability + auth check via the MCP's metadata endpoint.

        We probe ``GET /.well-known/oauth-protected-resource`` — the
        OAuth metadata surface is mandated by the spec and returns
        without consuming a paid call. A 401/403 surfaces as auth
        failure (token missing or rejected); 5xx as transient
        unreachability; anything else as available.
        """
        token = self._token_store.load()
        if not token:
            return ProviderHealth(
                available=False,
                success_rate=0.0,
                last_error=(
                    "paragraph: no API key (set PARAGRAPH_API_KEY or run "
                    "`paragraph login --token <key>`)"
                ),
            )
        try:
            client = self._build_http()
            owns = self._http is None
            try:
                resp = await client.get(
                    "/.well-known/oauth-protected-resource",
                    headers=self._auth_headers(token),
                )
            finally:
                if owns:
                    await client.aclose()
        except Exception as exc:
            return ProviderHealth(
                available=False,
                success_rate=0.0,
                last_error=f"paragraph: unreachable ({exc.__class__.__name__})",
            )
        if resp.status_code in (401, 403):
            return ProviderHealth(
                available=False,
                success_rate=0.0,
                last_error=f"paragraph: auth rejected (HTTP {resp.status_code})",
            )
        if resp.status_code >= 500:
            return ProviderHealth(
                available=False,
                success_rate=0.0,
                last_error=f"paragraph: 5xx ({resp.status_code})",
            )
        return ProviderHealth(available=True, success_rate=1.0, last_error=None)

    async def fetch(self, query: str) -> list[SourceChunk]:
        """Run ``posts.search`` and emit one `SourceChunk` per post.

        Failures are NOT raised — they are logged and an empty list is
        returned. The dispatcher (S13+) is responsible for adding the
        provider name to ``IngestionResult.degraded_sources``.

        Each emitted chunk carries ``creator_handle`` in
        ``SourceChunk.metadata`` so the downstream Citation builder can
        populate the ``Citation.creator_handle`` field that S13-CITE-01
        pre-paid (the citation-renderer surfaces handles + the payout
        footer block once S14-PARA-02 fires the on-chain leg).
        """
        token = self._token_store.load()
        if not token:
            logger.info("paragraph_provider: skipped (no API key configured)")
            return []

        try:
            posts = await self._search_posts(query=query, token=token)
        except ParagraphAuthError as exc:
            logger.warning("paragraph_provider: auth rejected — %s", exc)
            return []
        except Exception as exc:
            logger.warning(
                "paragraph_provider: search failed (%s)",
                exc.__class__.__name__,
            )
            return []

        chunks: list[SourceChunk] = []
        for raw in posts:
            if not isinstance(raw, dict):
                continue
            url = self._post_url(raw)
            if not url:
                continue
            try:
                candidate = SourceCandidate(
                    url=url,  # type: ignore[arg-type]
                    title=str(raw.get("title") or "")[:280],
                    type="web",
                    score=float(raw.get("score") or 0.5),
                )
            except Exception as exc:
                logger.warning("paragraph_provider: skipped malformed post (%s)", exc)
                continue
            chunks.append(
                SourceChunk(
                    candidate=candidate,
                    text=str(raw.get("excerpt") or raw.get("text") or ""),
                    metadata={
                        # creator_handle threads through to Citation via
                        # the pipeline's chunk-to-citation builder. The
                        # actual on-chain payout is S14-PARA-02.
                        "creator_handle": raw.get("author") or raw.get("author_handle") or "",
                        "creator_wallet": raw.get("author_wallet") or "",
                        "published_at": raw.get("published_at", ""),
                        "provider": self.name,
                    },
                )
            )
        return chunks

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_http(self) -> httpx.AsyncClient:
        if self._http is not None:
            return self._http
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=20.0,
            headers={
                "Accept": "application/json",
                "User-Agent": "gecko-core/0.1 (+https://geckovision.tech)",
            },
        )

    @staticmethod
    def _auth_headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _post_url(raw: dict[str, Any]) -> str:
        """Pull a canonical permalink out of a Paragraph post record.

        Paragraph's MCP returns ``url`` directly; older schemas expose
        ``permalink`` instead. Empty / missing → empty string (caller
        skips the post rather than emit a broken citation).
        """
        url = raw.get("url") or raw.get("permalink") or ""
        return str(url) if url else ""

    async def _search_posts(self, *, query: str, token: str) -> list[dict[str, Any]]:
        """POST the MCP ``posts.search`` tool. Raises typed errors on auth fail."""
        client = self._build_http()
        owns = self._http is None
        try:
            resp = await client.post(
                "/tools/posts.search",
                json={"query": query, "limit": 10},
                headers=self._auth_headers(token),
            )
        finally:
            if owns:
                await client.aclose()

        if resp.status_code in (401, 403):
            raise ParagraphAuthError(
                f"paragraph MCP rejected the OAuth Bearer (HTTP {resp.status_code})"
            )
        if resp.status_code != 200:
            # Non-2xx, non-auth — surface as a generic failure; the
            # caller drops to ``degraded_sources``.
            raise RuntimeError(f"paragraph MCP returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"paragraph MCP returned non-JSON: {exc}") from exc

        # Accept a few result-envelope shapes — keeps us resilient to
        # minor schema drift on Paragraph's side.
        if isinstance(body, dict):
            for key in ("posts", "results", "items"):
                value = body.get(key)
                if isinstance(value, list):
                    return [v for v in value if isinstance(v, dict)]
        if isinstance(body, list):
            return [v for v in body if isinstance(v, dict)]
        return []

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()


__all__ = [
    "DEFAULT_PARAGRAPH_MCP_URL",
    "DEFAULT_PER_FETCH_USD",
    "DEFAULT_TOKEN_PATH",
    "ParagraphAuthError",
    "ParagraphProvider",
    "ParagraphTokenStore",
]
