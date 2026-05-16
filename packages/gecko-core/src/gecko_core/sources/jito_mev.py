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

from dataclasses import dataclass
from typing import Final


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


def render_chunk(ep: JitoMevEndpoint, body_text: str, as_of_iso: str) -> str:
    """Render a fetched body as a substantive prose chunk.

    The chunk header tags the content as Jito MEV-tip data so the panel
    + voices know what bucket of evidence they're holding.
    """
    return (
        f"Protocol-native API: jito / {ep.slug} (subkind=mev_tip_data) "
        f"(as of {as_of_iso}).\n"
        f"Endpoint description: {ep.description}\n"
        f"Source URL: {ep.url}\n"
        f"Content kind: {ep.content_kind}\n"
        f"----- body -----\n"
        f"{body_text}\n"
        f"----- end body -----\n"
        f"Provider: protocol_native (Jito MEV-side, distinct from JitoSOL "
        f"staking content)."
    )


__all__ = [
    "JITO_MEV_ENDPOINTS",
    "JitoMevEndpoint",
    "render_chunk",
]
