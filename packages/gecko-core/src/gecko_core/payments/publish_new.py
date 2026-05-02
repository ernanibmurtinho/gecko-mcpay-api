"""publish.new artifact publishing — Sprint 14 S14-PUB-01.

Per ``docs/build-plan-sprint-14.md`` Track C: after a ``gecko_research``
run lands a verdict, optionally publish the rendered ResearchResult as a
publish.new artifact at $0.50 (founder-overridable). publish.new shares
the Paragraph backend (verified Sprint 12 chore-probes) — same x402v2
contract, same OAuth Bearer scheme; the wire shape is a single POST to
``/api/artifact/upload``.

Phase 1 (S14): publishes under **Gecko's wallet** (``GECKO_WALLET_ADDRESS_BASE``).
A founder can override the author wallet via the CLI ``--publish-as`` flag,
but the Sprint 14 flow does not bridge frames.ag (Solana) → Base for the
founder. If the founder has no Base address configured, we surface a
clear error pointing at ``bb wallet add publish-new`` and bail out.

Phase 2 (S15+): direct founder publishing under their own wallet via
the CDP wallet bridge.

Settlement path:
  * Stub mode (``X402_MODE=stub``) — short-circuits the POST and returns
    a deterministic ``stub://publish.new/<slug>`` URL. No real artifact
    is created.
  * Live / cdp mode — fires the POST through the publish.new artifact
    upload endpoint; the x402v2 challenge is settled via the X402Client
    factory's resolved client (Base mainnet via CDP).

Failure policy:
  * Auth missing / wallet invalid → typed ``PublishNewError`` raised so
    the CLI can render a clear message. We do NOT swallow these.
  * Network / 5xx from publish.new → typed ``PublishNewError`` raised
    with the upstream status code echoed (verbatim-error policy).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import httpx

from gecko_core.payments.factory import resolve_client
from gecko_core.payments.models import PaymentIntent
from gecko_core.payments.x402_client import _settings as _payment_settings

if TYPE_CHECKING:
    from gecko_core.models import ResearchResult
    from gecko_core.payments.protocol import X402Client

logger = logging.getLogger(__name__)


DEFAULT_PUBLISH_PRICE_USD: Decimal = Decimal("0.50")
"""Default per-artifact price. Founder can override via ``--publish-price``."""

DEFAULT_PUBLISH_NEW_BASE_URL: str = "https://publish.new"
"""Base URL. Override via ``PUBLISH_NEW_BASE_URL`` for staging."""

PUBLISH_NEW_API_KEY_ENV: str = "PARAGRAPH_API_KEY"
"""publish.new shares Paragraph's OAuth token; reuse the same env var.

This matches the S14-PARA-AUTH-01 bootstrap — one OAuth Bearer covers
both surfaces because they share the backend (verified Sprint 12 probes).
"""


_BASE_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class PublishNewError(RuntimeError):
    """Surfaced for any non-recoverable publish.new failure.

    Verbatim-error policy: the message echoes the upstream status code
    or the missing-config detail without rephrasing. The CLI catches
    this and renders it; the workflow caller is expected to treat it
    as non-fatal (publish is opt-in).
    """


@dataclass(frozen=True)
class PublishNewArtifact:
    """The published artifact's coordinates, surfaced on the receipt."""

    url: str
    slug: str
    price_usd: Decimal
    author_address: str
    tx_signature: str | None = None
    is_stub: bool = False


def _slugify(text: str, max_len: int = 60) -> str:
    """ASCII slug for the URL-safe portion of the artifact id.

    Lowercase, non-alnum → ``-``, collapse runs, trim. Mirrors the
    publish.new convention (matches what the platform generates for
    user-supplied titles).
    """
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    if not cleaned:
        cleaned = "verdict"
    return cleaned[:max_len].rstrip("-") or "verdict"


def resolve_publish_price_usd(override: Decimal | None = None) -> Decimal:
    """Apply the override if provided, else env, else default.

    Env: ``PUBLISH_NEW_PRICE_USD`` (any positive Decimal-parseable string).
    """
    if override is not None and override > 0:
        return override
    raw = os.environ.get("PUBLISH_NEW_PRICE_USD", "").strip()
    if raw:
        try:
            value = Decimal(raw)
            if value > 0:
                return value
        except Exception:
            logger.warning(
                "PUBLISH_NEW_PRICE_USD=%r is not a valid decimal; using default $%s",
                raw,
                DEFAULT_PUBLISH_PRICE_USD,
            )
    return DEFAULT_PUBLISH_PRICE_USD


def resolve_publish_author_address(override: str | None = None) -> str:
    """Resolve the publish.new author wallet (Base 0x address).

    Resolution order:
      1. ``override`` (the CLI ``--publish-as`` flag).
      2. ``GECKO_WALLET_ADDRESS_BASE`` env var (Phase 1 default).

    Raises :class:`PublishNewError` when neither is configured or the
    candidate fails the EIP-55-friendly format check. The error message
    points the founder at ``bb wallet add publish-new`` so they know
    which lever to pull.
    """
    candidate = override or os.environ.get("GECKO_WALLET_ADDRESS_BASE", "")
    candidate = candidate.strip()
    if not candidate:
        raise PublishNewError(
            "publish.new requires a Base 0x author address — set "
            "GECKO_WALLET_ADDRESS_BASE or pass --publish-as 0x..., or run "
            "`bb wallet add publish-new` to bootstrap one. (frames.ag/"
            "Solana wallets cannot publish on publish.new in V1; the Base "
            "bridge ships in S15.)"
        )
    if not _BASE_ADDRESS_RE.match(candidate):
        raise PublishNewError(
            f"publish.new author address {candidate!r} is not a valid Base "
            "0x address (expected 0x + 40 hex chars). Run "
            "`bb wallet add publish-new` to wire a correct one."
        )
    return candidate


def _verdict_token(result: ResearchResult) -> str:
    """Single-token verdict (PIVOT / REFINE / GO) for the title."""
    verdict = getattr(result, "verdict", None)
    return getattr(verdict, "value", "REFINE") or "REFINE"


def build_artifact_title(idea: str, result: ResearchResult) -> str:
    """``Gecko verdict: <idea_summary> — <PIVOT|REFINE|GO>``.

    Matches the S14-PUB-01 acceptance copy. Idea is truncated to keep
    the title under publish.new's display limit.
    """
    idea_summary = (idea or "").strip()
    if len(idea_summary) > 80:
        idea_summary = idea_summary[:77].rstrip() + "..."
    return f"Gecko verdict: {idea_summary} — {_verdict_token(result)}"


def render_artifact_markdown(idea: str, result: ResearchResult) -> str:
    """Render the ResearchResult as the artifact body (markdown).

    Compact: title block + verdict line + the three documents'
    structured fields rendered as headed sections. The CLI's Rich
    renderer is for terminal; this is for the publish.new artifact.
    The body intentionally avoids citation hyperlinks rendered as Rich
    markup so the markdown is portable.
    """
    bp = result.business_plan
    vr = result.validation_report
    prd = result.prd

    def _bullets(items: list[str]) -> str:
        if not items:
            return "_(none)_\n"
        return "\n".join(f"- {i}" for i in items) + "\n"

    return (
        f"# Gecko verdict: {idea}\n\n"
        f"**Verdict:** {_verdict_token(result)}\n\n"
        f"**Tier:** {result.tier}\n"
        f"**Sources:** {len(result.sources)}\n\n"
        "---\n\n"
        "## Business Plan\n\n"
        f"**Problem.** {bp.problem}\n\n"
        f"**ICP.** {bp.icp}\n\n"
        f"**Solution.** {bp.solution}\n\n"
        f"**Market.** {bp.market}\n\n"
        f"**Business model.** {bp.business_model}\n\n"
        f"**Channels.** {bp.channels}\n\n"
        "**Risks.**\n" + _bullets(bp.risks) + "\n"
        "## Validation Report\n\n"
        f"**Gap.** {vr.gap_classification} — {vr.gap_summary}\n\n"
        f"**Market size signal.** {vr.market_size_signal}\n\n"
        f"**Competitor analysis.** {vr.competitor_analysis}\n\n"
        f"**Demand evidence.** {vr.demand_evidence}\n\n"
        "**Risk flags.**\n" + _bullets(vr.risk_flags) + "\n"
        "## PRD\n\n"
        "**V1 scope.**\n" + _bullets(prd.v1_scope) + "\n"
        "**V2 scope.**\n" + _bullets(prd.v2_scope) + "\n"
        "**V3 scope.**\n" + _bullets(prd.v3_scope) + "\n"
        "**Acceptance criteria.**\n" + _bullets(prd.acceptance_criteria) + "\n"
        "**Success metrics.**\n" + _bullets(prd.success_metrics) + "\n"
    )


async def publish_artifact(
    *,
    session_id: UUID,
    idea: str,
    result: ResearchResult,
    price_usd: Decimal | None = None,
    author_address: str | None = None,
    base_url: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    client: X402Client | None = None,
) -> PublishNewArtifact:
    """POST the rendered ResearchResult to publish.new at ``price_usd``.

    Phase 1 author resolution: ``author_address`` overrides;
    otherwise ``GECKO_WALLET_ADDRESS_BASE`` is the Phase-1 default.

    Stub mode short-circuits and returns a ``stub://publish.new/<slug>``
    URL. The X402Client factory honors ``X402_MODE=stub`` globally; we
    detect that path early and skip the network entirely so dev/CI runs
    never touch publish.new.

    Errors propagate verbatim via :class:`PublishNewError`. The caller
    (workflow + CLI) decides whether to halt or render-and-continue.
    """
    resolved_price = resolve_publish_price_usd(price_usd)
    resolved_address = resolve_publish_author_address(author_address)
    title = build_artifact_title(idea, result)
    body_markdown = render_artifact_markdown(idea, result)
    slug = _slugify(f"{_verdict_token(result)}-{idea}-{session_id.hex[:8]}")

    mode = _payment_settings().mode
    if mode == "stub":
        # Stub short-circuit — mirrors the live shape so the CLI receipt
        # block carries an identical structure.
        return PublishNewArtifact(
            url=f"stub://publish.new/{slug}",
            slug=slug,
            price_usd=resolved_price,
            author_address=resolved_address,
            tx_signature=None,
            is_stub=True,
        )

    # Live / cdp / frames — pre-mint a payment intent so the receipt
    # carries an idempotency key, then settle through the X402Client
    # seam. publish.new's x402v2 challenge requires the settlement to
    # land before the artifact is materialized.
    intent = PaymentIntent(
        intent_id=f"publish-new-{session_id.hex}-{uuid4().hex[:8]}",
        session_id=session_id,
        tier=result.tier,
        amount_usd=resolved_price,
    )
    resolved_client = client if client is not None else resolve_client(intent)

    try:
        payment = await resolved_client.charge(intent)
    except Exception as exc:
        # Verbatim policy — surface the class + message; do not re-wrap
        # into a generic message that hides which facilitator failed.
        raise PublishNewError(
            f"publish.new payment settle failed: {type(exc).__name__}: {exc}"
        ) from exc

    if payment.status != "success":
        raise PublishNewError(
            f"publish.new payment did not settle (status={payment.status}, "
            f"error={payment.error or 'unknown'})"
        )

    token = os.environ.get(PUBLISH_NEW_API_KEY_ENV, "").strip()
    if not token:
        raise PublishNewError(
            f"publish.new requires {PUBLISH_NEW_API_KEY_ENV} to be set "
            "(the OAuth Bearer is shared with Paragraph)."
        )

    base = base_url or os.environ.get("PUBLISH_NEW_BASE_URL") or DEFAULT_PUBLISH_NEW_BASE_URL
    payload = {
        "title": title,
        "body": body_markdown,
        "slug": slug,
        "price_usd": float(resolved_price),
        "author_address": resolved_address,
        "tx_signature": payment.tx_signature,
        "session_id": str(session_id),
    }

    owns_http = http_client is None
    http = http_client or httpx.AsyncClient(
        base_url=base,
        timeout=20.0,
        headers={
            "Accept": "application/json",
            "User-Agent": "gecko-core/0.1 (+https://geckovision.tech)",
        },
    )
    try:
        try:
            resp = await http.post(
                "/api/artifact/upload",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise PublishNewError(
                f"publish.new upload failed: {type(exc).__name__}: {exc}"
            ) from exc
    finally:
        if owns_http:
            await http.aclose()

    if resp.status_code >= 400:
        # Echo the upstream code + body snippet verbatim — never rephrase.
        raise PublishNewError(
            f"publish.new upload returned HTTP {resp.status_code}: {resp.text[:300]}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise PublishNewError(f"publish.new upload returned non-JSON: {exc}") from exc

    artifact_url = data.get("url") if isinstance(data, dict) else None
    if not isinstance(artifact_url, str) or not artifact_url:
        # The wire shape can drift; fall back to the canonical
        # ``<base>/<slug>`` form so we always have *some* URL on the
        # receipt rather than blanking it.
        artifact_url = f"{base.rstrip('/')}/{slug}"
    returned_slug = (
        data.get("slug") if isinstance(data, dict) and isinstance(data.get("slug"), str) else slug
    )

    return PublishNewArtifact(
        url=str(artifact_url),
        slug=str(returned_slug),
        price_usd=resolved_price,
        author_address=resolved_address,
        tx_signature=payment.tx_signature,
        is_stub=False,
    )


__all__ = [
    "DEFAULT_PUBLISH_NEW_BASE_URL",
    "DEFAULT_PUBLISH_PRICE_USD",
    "PUBLISH_NEW_API_KEY_ENV",
    "PublishNewArtifact",
    "PublishNewError",
    "build_artifact_title",
    "publish_artifact",
    "render_artifact_markdown",
    "resolve_publish_author_address",
    "resolve_publish_price_usd",
]
