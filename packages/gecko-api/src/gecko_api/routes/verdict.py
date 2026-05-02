"""Public verdict-URL surface — ``GET /v1/verdict/{hash}``.

S20-VERDICT-URL-IMPL-01 (#10). This is the public, embeddable, curl-able
teaser for a verdict captured in ``judge_transcripts``. The full
prose / citations / advisor voices stay gated behind ``?detail=full``,
which returns 402 today (stub) and lights up x402 settlement in S20 #11.

Lookup strategy:

* 64-char full hex sha256 → single ``find_one`` on ``verdict_hash``.
* 12-char short hash → prefix match. 0 hits → 404. 1 hit → 302 to the
  canonical full-hash URL. >1 hits → 409 with the candidate set so the
  client can disambiguate.

CORS: ``Access-Control-Allow-Origin: *`` on both the teaser and the
402 stub (per the contract doc). The 402 stub will be tightened to
``https://app.geckovision.tech`` in #11 once the paywall ships.

Rate limits (slowapi, the limiter that gecko-api already uses):

* teaser: 60/min/IP (per-IP bucket — the verdict URL is unauthenticated)
* ``?detail=full``: 10/min/IP (so #11's facilitator dispatch can't be
  hammered before settlement)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from starlette.responses import JSONResponse, RedirectResponse

from gecko_api.rate_limit import limiter

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/v1/verdict", tags=["verdict"])


# Per-call price displayed in the 402 stub. #11 will replace this with a
# tier-aware price; the response shape (``price_usdc`` as a string) is the
# contract surface so frontend can render dynamic CTA copy.
_DEFAULT_DETAIL_PRICE_USDC = "2.50"

# Excerpt cap for the teaser surface. The judge_prose can run several
# paragraphs in pro-tier; only the lead is exposed publicly.
_PROSE_EXCERPT_MAX_CHARS = 280

_FULL_HASH_LEN = 64
_SHORT_HASH_LEN = 12

# Wide-open CORS for the public teaser. See module docstring on why this
# differs from the restrictive app-wide CORS middleware.
_TEASER_CORS_HEADERS: dict[str, str] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
}


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


def _judge_prose_excerpt(judge_prose: str | None) -> str:
    """Truncate the judge prose to the teaser-safe lead sentence(s)."""
    if not isinstance(judge_prose, str) or not judge_prose:
        return ""
    text = judge_prose.strip()
    if len(text) <= _PROSE_EXCERPT_MAX_CHARS:
        return text
    # Truncate on a word boundary so we don't surface a half-token.
    cut = text[:_PROSE_EXCERPT_MAX_CHARS].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + "..."


def _format_created_at(value: Any) -> str | None:
    if isinstance(value, datetime):
        # Use Z suffix so the surface matches the OpenAPI / contract doc.
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str):
        return value
    return None


def _short_hash_token(full_hash: str) -> str:
    return f"verdict@{full_hash[:_SHORT_HASH_LEN]}"


def _teaser_payload(doc: dict[str, Any]) -> dict[str, Any]:
    full_hash = str(doc.get("verdict_hash") or "")
    return {
        "verdict_hash": full_hash,
        "verdict_hash_short": _short_hash_token(full_hash) if full_hash else None,
        "idea_text": doc.get("idea_text"),
        "verdict": doc.get("actual_verdict_v2"),
        "judge_prose_excerpt": _judge_prose_excerpt(doc.get("judge_prose")),
        "gap_classification": doc.get("gap_classification"),
        "created_at": _format_created_at(doc.get("created_at")),
        "tier": doc.get("tier"),
        "provider_mix_flag": doc.get("provider_mix_flag"),
        "is_paywalled": True,
        "preview_only": True,
    }


def _json_response(
    *,
    status_code: int,
    content: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    headers = dict(_TEASER_CORS_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    return JSONResponse(status_code=status_code, content=content, headers=headers)


def _not_found(echo: str) -> JSONResponse:
    # Single 404 path for "never existed" AND "expired" so we don't leak a
    # timing / shape side channel about which one applies.
    return _json_response(
        status_code=404,
        content={"error": "verdict_not_found", "hash": echo},
    )


def _get_collection() -> Any | None:
    """Return the ``judge_transcripts`` Mongo collection, or None when Mongo
    isn't reachable. Imports lazily so the route module stays import-light
    in environments that don't have pymongo wired (CI, smoke tests)."""
    from gecko_core.orchestration import transcripts as t

    return t._mongo_collection_unsafe()


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


# slowapi's per-handler decorator only supports a single static (or
# request-agnostic) rate. The contract calls for 60/min on the teaser and
# 10/min on ``?detail=full``; we install the 60/min ceiling here so the
# public teaser surface is bounded today. The stricter 10/min on
# ``?detail=full`` will be enforced in S20 #11 when the detail surface is
# split into its own (paid) route — at which point each FastAPI handler
# carries its own slowapi decorator and the buckets are clean.
@router.get("/{hash}", response_model=None)
@limiter.limit("60/minute")
async def get_verdict(
    request: Request,
    hash: str,
    detail: str | None = Query(default=None),
) -> JSONResponse | RedirectResponse:
    """Return the teaser for a verdict identified by full or short hash.

    The slowapi limiter has to bucket the two surfaces differently — teaser
    at 60/min, ``?detail=full`` at 10/min — but slowapi's decorator only
    accepts a single rate per handler. We dispatch to two inner helpers,
    each decorated, so the limiter sees them as distinct routes for
    bucketing purposes.
    """
    if detail == "full":
        return await _detail_full_stub(request=request, hash_=hash)
    return await _teaser(request=request, hash_=hash)


async def _teaser(*, request: Request, hash_: str) -> JSONResponse | RedirectResponse:
    # Reject obvious junk early so the Mongo round-trip isn't on the abuse path.
    if not hash_ or not _is_hex(hash_):
        return _not_found(echo=hash_)

    coll = _get_collection()
    if coll is None:
        # Mongo not configured — there's nothing to look up. Same shape as
        # a missing hash so we don't leak deployment posture.
        return _not_found(echo=hash_)

    if len(hash_) == _FULL_HASH_LEN:
        # Full canonical hash — return most-recent on a tie (same idea +
        # same retrieved set produces identical hash across reruns; the
        # ``verdict_hash`` index is non-unique by design).
        doc = _find_most_recent(coll, {"verdict_hash": hash_})
        if doc is None:
            return _not_found(echo=hash_)
        return _json_response(status_code=200, content=_teaser_payload(doc))

    if len(hash_) == _SHORT_HASH_LEN:
        return _short_hash_dispatch(coll=coll, short=hash_, request=request)

    # Any other length is malformed.
    return _not_found(echo=hash_)


async def _detail_full_stub(*, request: Request, hash_: str) -> JSONResponse:
    # 402 stub for ``?detail=full``. #11 will replace this with the x402
    # facilitator handshake; the response body shape (``price_usdc``) is
    # already the contract surface so the frontend renders dynamic copy.
    return _json_response(
        status_code=402,
        content={
            "error": "payment_required",
            "message": "x402 settlement coming in S20 #11",
            "verdict_hash": hash_,
            "price_usdc": _DEFAULT_DETAIL_PRICE_USDC,
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_most_recent(coll: Any, query: dict[str, Any]) -> dict[str, Any] | None:
    """Return the most-recent document matching ``query`` by ``created_at``."""
    cursor = coll.find(query).sort("created_at", -1).limit(1)
    for doc in cursor:
        return doc  # type: ignore[no-any-return]
    return None


def _short_hash_dispatch(
    *, coll: Any, short: str, request: Request
) -> JSONResponse | RedirectResponse:
    """Resolve a 12-char prefix to either a 302 (single match), 409
    (multiple), or 404 (none). We use a regex prefix match keyed off the
    indexed ``verdict_hash`` field — ``^abc123…`` is a covered scan on
    that index in MongoDB."""
    # Distinct full-hash candidates within the prefix space.
    candidates_seen: list[str] = []
    cursor = coll.find(
        {"verdict_hash": {"$regex": f"^{short}"}},
        projection={"verdict_hash": 1, "created_at": 1},
    ).sort("created_at", -1)
    for doc in cursor:
        full = str(doc.get("verdict_hash") or "")
        if not full:
            continue
        if full not in candidates_seen:
            candidates_seen.append(full)
        # Bound the work — anything past 5 distinct collisions is already
        # unambiguously "ambiguous" for the response.
        if len(candidates_seen) >= 5:
            break

    if not candidates_seen:
        return _not_found(echo=short)

    if len(candidates_seen) == 1:
        canonical = f"/v1/verdict/{candidates_seen[0]}"
        # 302: client should re-issue against the canonical full hash. The
        # response inherits the wide-open CORS so browser clients can read
        # the redirect target.
        return RedirectResponse(url=canonical, status_code=302, headers=_TEASER_CORS_HEADERS)

    return _json_response(
        status_code=409,
        content={
            "error": "verdict_hash_ambiguous",
            "short": short,
            "candidates": candidates_seen,
        },
    )


__all__ = ["router"]
