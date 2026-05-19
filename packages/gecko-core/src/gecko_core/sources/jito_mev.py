"""S31-#50 — Jito MEV-tip-floor-specific source catalog.

The jito-2025Q2-mev-tip-band rubric fixture (citRel 0.15 in V1-FINAL)
asks about MEV tip-band data — P75 vs P90 floor for high-frequency arb
bundles. The corpus's existing Jito chunks (S28-#26) are dominated by
JitoSOL staking / validator-selection / restaking content, none of
which answers the MEV-tip question. This catalog is the MEV-only
slice: tip floor percentiles, bundle landing mechanics, searcher dev
docs, block-engine telemetry. Staking pages are intentionally
excluded — they live in ``protocol_native.py`` and stay there.

Every endpoint here is free + public, no API key. JSON endpoints
return live tip-floor or recent-block telemetry; HTML/docs endpoints
carry the mechanism prose searchers actually need to reason about
tip strategy.

Carries:
  - provider_kind="protocol_native"
  - protocol=("jito",)
  - metadata.subkind="mev_tip_data"

The subkind tag is the disambiguator at retrieval-debug time: it lets
us tell at a glance whether a Jito chunk is MEV-side (what we wrote
here) or staking-side (what S28-#26 wrote).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final


@dataclass(frozen=True)
class JitoMevEndpoint:
    """One Jito MEV-specific endpoint to fetch + chunk + embed."""

    slug: str
    url: str
    description: str
    content_kind: str = "mechanism"  # mechanism | quote | governance


# --- Live tip-floor / bundle telemetry (quote-kind) -----------------------
# These are the lamport-grounded numbers a searcher needs to make a tip
# decision. content_kind="quote" so the panel knows they're snapshots.

_LIVE_ENDPOINTS: Final[tuple[JitoMevEndpoint, ...]] = (
    JitoMevEndpoint(
        slug="jito-mev-tip-floor-live",
        url="https://bundles.jito.wtf/api/v1/bundles/tip_floor",
        description=(
            "Jito tip-floor percentiles (P25/P50/P75/P95/P99) for bundle "
            "landing — current snapshot. Direct lamport figures that "
            "ground 'P75 vs P90' tip-strategy questions in real data."
        ),
        content_kind="quote",
    ),
    JitoMevEndpoint(
        slug="jito-mev-rewards-snapshot",
        url="https://kobe.mainnet.jito.network/api/v1/mev_rewards",
        description=(
            "Jito MEV rewards summary — recent-epoch MEV totals + "
            "distribution to validators + JitoSOL. Caveat: epoch-level "
            "aggregate, not per-bundle. Use alongside tip_floor for "
            "magnitude calibration."
        ),
        content_kind="quote",
    ),
    JitoMevEndpoint(
        slug="jito-mev-validators-telemetry",
        url="https://kobe.mainnet.jito.network/api/v1/validators",
        description=(
            "Jito validator-set telemetry — per-validator MEV share, "
            "commission, stake size, performance scores. Useful for "
            "reasoning about leader-schedule coverage when sizing tips."
        ),
        content_kind="quote",
    ),
)


# S33-#75 endpoint curation — `_DOCS_ENDPOINTS` (4 docs/marketing pages)
# and `_CLIENT_ENDPOINTS` (5 per-language dev READMEs) were deleted. They
# were the same 0.00-citation-relevance class as the drift/jito docs the
# S33-#75 audit flagged: a README documents bundle-submission CODE, it has
# no tradeable number a verdict can cite. `jito-mev-recent-blocks` was
# also dropped — kobe.mainnet.jito.network/api/v1/recent_blocks returns
# HTTP 404. The MEV catalog is now the 3 verified-live data endpoints.

JITO_MEV_ENDPOINTS: Final[tuple[JitoMevEndpoint, ...]] = _LIVE_ENDPOINTS


# S37-WS1b — the citable Jito MEV figures must lead the rendered chunk.
# The prior render_chunk led with a 4-line metadata header (Protocol-native
# API / Endpoint description / Source URL / Content kind) ~280 chars long —
# it exhausted the rubric judge's ~320-char snippet window *before any
# lamport figure appeared*, so even an honestly-cited tip-floor number was
# never visible to the judge → hallucination_score false-negatives (S36-#112
# §Q2 Error 2). This mirrors the number-first fix S36-WS2 applied to the
# protocol_native renderers (_render_tip_floor_payload et al): the salient
# figures LEAD, provenance/metadata TRAIL.

_JITO_TIP_LADDER: Final[tuple[tuple[str, str], ...]] = (
    ("landed_tips_25th_percentile", "25th"),
    ("landed_tips_50th_percentile", "50th"),
    ("landed_tips_75th_percentile", "75th"),
    ("landed_tips_95th_percentile", "95th"),
    ("landed_tips_99th_percentile", "99th"),
)

# Numeric scalars worth surfacing from a non-tip-floor MEV payload
# (mev_rewards summary, validators telemetry). Best-effort: any int/float
# leaf is citable, but these labelled keys read as prose first.
_JITO_NUMERIC_HINT = re.compile(
    r"(tip|mev|reward|rate|stake|commission|share|epoch|amount|sol|lamport)",
    re.IGNORECASE,
)


def _fmt_jito_num(value: Any) -> str:
    """Compact numeric format — lamport-scale tip floors render verbatim."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    n = float(value)
    if n != 0 and abs(n) < 1e-3:
        # Tip-floor figures are ~1e-6 SOL — keep them readable, not 0.0.
        return f"{n:.10f}".rstrip("0").rstrip(".")
    if abs(n) >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if abs(n) >= 1_000:
        return f"{n / 1e3:.1f}K"
    return f"{n:g}"


def _lead_figures(body_text: str) -> str:
    """Extract a number-first salient-figure clause from a Jito MEV body.

    Returns a prose clause leading with the citable figures, or ``""`` when
    the body carries no parseable numeric payload (the caller then leads
    with the raw body so nothing is lost).
    """
    stripped = body_text.lstrip()
    if stripped[:1] not in ("{", "["):
        return ""
    try:
        body: Any = json.loads(body_text)
    except (json.JSONDecodeError, ValueError):
        return ""

    snapshot: dict[str, Any] | None = None
    if isinstance(body, list) and body and isinstance(body[0], dict):
        snapshot = body[0]
    elif isinstance(body, dict):
        snapshot = body

    if snapshot is None:
        return ""

    # Tip-floor percentile ladder — the highest-signal MEV payload.
    rungs = [
        f"{label} pct {_fmt_jito_num(snapshot[key])} SOL"
        for key, label in _JITO_TIP_LADDER
        if snapshot.get(key) is not None
    ]
    if rungs:
        clause = "Jito bundle tip floor — " + "; ".join(rungs) + "."
        ema = snapshot.get("ema_landed_tips_50th_percentile")
        if ema is not None:
            clause += f" EMA 50th pct {_fmt_jito_num(ema)} SOL."
        return clause

    # Generic MEV payload (mev_rewards / validators) — surface the first
    # few labelled numeric leaves so a figure lands inside the snippet.
    figures: list[str] = []
    for key, val in snapshot.items():
        if len(figures) >= 5:
            break
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        if _JITO_NUMERIC_HINT.search(str(key)):
            figures.append(f"{key} {_fmt_jito_num(val)}")
    if figures:
        return "Jito MEV snapshot — " + ", ".join(figures) + "."
    return ""


def render_chunk(ep: JitoMevEndpoint, body_text: str, as_of_iso: str) -> str:
    """Render a fetched body as a substantive, number-first prose chunk.

    S37-WS1b — the salient lamport/MEV figures LEAD the chunk; the
    provenance metadata (protocol/slug, endpoint description, source URL,
    content kind) TRAILS it. This guarantees the rubric judge's truncated
    snippet window opens on a citable figure rather than a 4-line header.
    When the body carries no parseable numeric payload the raw body still
    leads so docs/telemetry prose is never lost.
    """
    lead = _lead_figures(body_text)
    body_block = lead if lead else body_text.strip()
    return (
        f"{body_block}\n"
        f"----- source body -----\n"
        f"{body_text}\n"
        f"----- end body -----\n"
        f"Protocol-native API: jito / {ep.slug} (subkind=mev_tip_data) "
        f"(as of {as_of_iso}).\n"
        f"Endpoint description: {ep.description}\n"
        f"Source URL: {ep.url}\n"
        f"Content kind: {ep.content_kind}\n"
        f"Provider: protocol_native (Jito MEV-side, distinct from JitoSOL "
        f"staking content)."
    )


__all__ = [
    "JITO_MEV_ENDPOINTS",
    "JitoMevEndpoint",
    "render_chunk",
]
