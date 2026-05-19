"""S37-WS1b — number-first invariant for the Jito MEV renderer.

S36-WS2 made the ``protocol_native`` renderers number-first but missed
``jito_mev.render_chunk`` (S36-#112 §Q2 Error 2). The prior renderer led
each chunk with a ~4-line metadata header (Protocol-native API / Endpoint
description / Source URL / Content kind) ~280 chars long — it exhausted
the rubric judge's ~320-char snippet window before any lamport figure
appeared, so an honestly-cited tip-floor number was never visible to the
judge.

These tests assert the rendered chunk LEADS with a numeric figure within
the first ~120 chars and that the provenance metadata TRAILS. Pure-function
tests: no Mongo, no embedder, no network — light fakes only.
"""

from __future__ import annotations

import json

from gecko_core.sources.jito_mev import JitoMevEndpoint, render_chunk


def _ep(slug: str = "jito-mev-tip-floor-live") -> JitoMevEndpoint:
    return JitoMevEndpoint(
        slug=slug,
        url="https://bundles.jito.wtf/api/v1/bundles/tip_floor",
        description=(
            "Jito tip-floor percentiles (P25/P50/P75/P95/P99) for bundle "
            "landing — current snapshot. Direct lamport figures."
        ),
        content_kind="quote",
    )


# A real-shaped tip_floor payload — a list with one percentile snapshot.
_TIP_FLOOR = [
    {
        "time": "2026-05-18T03:35:33+00:00",
        "landed_tips_25th_percentile": 2.7362e-06,
        "landed_tips_50th_percentile": 5.517e-06,
        "landed_tips_75th_percentile": 8.1342e-06,
        "landed_tips_95th_percentile": 6.926e-05,
        "landed_tips_99th_percentile": 0.000162477,
        "ema_landed_tips_50th_percentile": 4.1514e-06,
    }
]


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


class TestJitoNumberFirst:
    """The citable lamport figure must lead the chunk text."""

    def test_tip_floor_chunk_leads_with_a_figure(self) -> None:
        chunk = render_chunk(_ep(), json.dumps(_TIP_FLOOR), "2026-05-18")
        head = chunk[:120]
        assert _has_digit(head), f"no numeric figure in chunk head: {head!r}"
        # The percentile ladder is the lead — provenance is pushed to the tail.
        assert chunk.startswith("Jito bundle tip floor"), head

    def test_tip_floor_figure_survives_snippet_window(self) -> None:
        # The rubric judge verifies against a truncated ~320-char snippet.
        chunk = render_chunk(_ep(), json.dumps(_TIP_FLOOR), "2026-05-18")
        snippet = chunk[:320]
        # A real P75 lamport figure lands inside the snippet window — the
        # sub-1e-3 tip floor renders as a verbatim decimal (0.0000081342).
        assert "75th pct" in snippet
        assert "0.0000081342" in snippet, "P75 tip-floor figure must be in-window"
        assert "SOL" in snippet

    def test_provenance_metadata_trails_the_figure(self) -> None:
        chunk = render_chunk(_ep(), json.dumps(_TIP_FLOOR), "2026-05-18")
        figure_pos = chunk.find("0.0000081342")
        header_pos = chunk.find("Protocol-native API:")
        assert figure_pos != -1 and header_pos != -1
        assert figure_pos < header_pos, "numeric payload must precede provenance"

    def test_generic_mev_payload_leads_with_a_figure(self) -> None:
        # mev_rewards / validators payloads are dicts of labelled numbers —
        # the renderer surfaces the first few numeric leaves.
        ep = _ep("jito-mev-rewards-snapshot")
        body = json.dumps(
            {
                "epoch": 812,
                "total_mev_rewards": 4321.55,
                "validator_count": 1900,
                "mev_to_jitosol_sol": 1200.7,
            }
        )
        chunk = render_chunk(ep, body, "2026-05-18")
        head = chunk[:120]
        assert _has_digit(head), f"no numeric figure in chunk head: {head!r}"
        assert chunk.startswith("Jito MEV snapshot"), head

    def test_non_json_body_still_renders(self) -> None:
        # A docs/telemetry body with no parseable numeric payload must not
        # be lost — the raw body leads instead.
        ep = _ep("jito-mev-validators-telemetry")
        body = "Bundle landing mechanics overview — searcher dev notes."
        chunk = render_chunk(ep, body, "2026-05-18")
        assert chunk.startswith(body)
        # Provenance still trails.
        assert "Protocol-native API:" in chunk
        assert chunk.find(body) < chunk.find("Protocol-native API:")

    def test_full_metadata_block_still_present_in_tail(self) -> None:
        # Number-first reorders the chunk; it does not drop provenance.
        chunk = render_chunk(_ep(), json.dumps(_TIP_FLOOR), "2026-05-18")
        for marker in (
            "Protocol-native API: jito / jito-mev-tip-floor-live",
            "Endpoint description:",
            "Source URL: https://bundles.jito.wtf",
            "Content kind: quote",
        ):
            assert marker in chunk, f"missing provenance marker: {marker!r}"
