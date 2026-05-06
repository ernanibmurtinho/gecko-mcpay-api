"""S20-FIX-05 — market_landscape post-processor truncation handling.

Production session ``c05ab663-4a45-4db7-998a-369eb6966174`` returned
``market_landscape: null`` because deepseek-v4-flash hit its 2000-token
cap mid-list. The fix:

1. Bump ``max_tokens`` to 4000 (tunable via
   ``GECKO_MARKET_LANDSCAPE_MAX_TOKENS``).
2. Best-effort parse on truncation — recover the valid prefix instead of
   dropping the whole section.
3. Surface the failure mode via ``MarketLandscape.section_flag``
   (``truncated_partial_recovery`` / ``truncated_zero_recovery``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from gecko_core.orchestration.llm_client import LLMTruncationError
from gecko_core.orchestration.pro import post_processors as pp
from gecko_core.orchestration.pro.post_processors import (
    _recover_market_landscape_partial,
)

# ---------------------------------------------------------------------------
# Direct partial-recovery tests (no I/O, no async).
# ---------------------------------------------------------------------------


def _competitor_json(name: str, axis: str = "settlement_layer") -> str:
    return json.dumps(
        {
            "name": name,
            "what_they_do": f"{name} is a thing that does the thing it does.",
            "axis": axis,
            "why_we_are_not_them": (
                "They settle in fiat through banks; we settle USD on-chain with x402 receipts."
            ),
            "flag": None,
        }
    )


def test_partial_recovery_two_complete_then_cut_third() -> None:
    """Truncated mid-third-entry: 2 valid competitors recovered."""
    a = _competitor_json("AlphaCorp")
    b = _competitor_json("BetaCo")
    # Third entry is cut off mid-string.
    partial = '{"competitors": [' + a + "," + b + ',{"name": "ThirdC'
    result = _recover_market_landscape_partial(partial)
    assert result.section_flag == "truncated_partial_recovery"
    assert len(result.competitors) == 2
    assert {c.name for c in result.competitors} == {"AlphaCorp", "BetaCo"}


def test_zero_recovery_cut_after_first_key() -> None:
    """Truncation so early no full competitor parsed → empty + zero flag."""
    partial = '{"compet'
    result = _recover_market_landscape_partial(partial)
    assert result.section_flag == "truncated_zero_recovery"
    assert result.competitors == []


def test_zero_recovery_no_complete_competitor_in_array() -> None:
    """Array opened but first entry cut mid-object → zero recovery."""
    partial = '{"competitors": [{"name": "Alph'
    result = _recover_market_landscape_partial(partial)
    assert result.section_flag == "truncated_zero_recovery"
    assert result.competitors == []


# ---------------------------------------------------------------------------
# End-to-end task tests via mocked _call_json.
# ---------------------------------------------------------------------------


class _FakeClient:
    pass


def _run_landscape_task_sync(
    monkeypatch: pytest.MonkeyPatch,
    *,
    call_json_side_effect: Any,
    caplog: pytest.LogCaptureFixture | None = None,
) -> Any:
    """Drive the inner _landscape_task closure through run_post_processors.

    We monkeypatch ``_call_json`` and the four sibling tasks so only the
    landscape branch exercises real code. Returns the landscape result.
    """
    import asyncio

    from gecko_core.models import (
        NextStepsWithFalsifiers,
        PerVoiceReadout,
        SurvivingDissent,
    )
    from gecko_core.orchestration.pro.transcript import DebateTranscript

    # Stub the prompts loader so we don't need a real prompt file.
    monkeypatch.setattr(
        pp,
        "load_post_processors",
        lambda: {
            "per_voice_extraction": "SYS_PER_VOICE",
            "transcript_summary": "SYS_SUMMARY",
            "market_landscape": "SYS_LANDSCAPE",
            "surviving_dissent": "SYS_DISSENT",
            "next_steps_with_falsifiers": "SYS_STEPS",
            "classification_extraction": "SYS_CLASSIFY",
        },
    )

    # Stub _build_client so we don't try to open a real http connection.
    class _Closeable:
        async def close(self) -> None:
            return None

    monkeypatch.setattr(pp, "_build_client", lambda: _Closeable())

    # Stub the four non-landscape tasks via the underlying _run_section /
    # _call_json paths. Each side_effect dispatches based on the system
    # prompt name (which we set above to a sentinel "x" — instead, we
    # patch _run_section to short-circuit, and _call_json to do the
    # landscape work).
    async def _fake_run_section(
        client: Any, *, system: str, user: str, model_cls: Any, section: str
    ) -> Any:
        if section == "per_voice_extraction":
            return PerVoiceReadout(voices=[])
        if section == "surviving_dissent":
            return SurvivingDissent(dissent_status="no_surviving_dissent")
        if section == "next_steps_with_falsifiers":
            return NextStepsWithFalsifiers(steps=[])
        return None

    monkeypatch.setattr(pp, "_run_section", _fake_run_section)

    call_count = {"n": 0}

    async def _fake_call_json(
        client: Any,
        *,
        system: str,
        user: str,
        model_cls: Any = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        call_count["n"] += 1
        # Route by what the task is asking for. The landscape system
        # prompt is the one we want to drive; everything else returns
        # benign payloads.
        # Heuristic: only the landscape task passes ``max_tokens`` (the
        # FIX-05 cap). All other tasks call _call_json without it.
        if max_tokens is not None:
            # Track the cap for the env-override assertion.
            call_count["landscape_max_tokens"] = max_tokens
            if callable(call_json_side_effect):
                payload = call_json_side_effect()
                assert isinstance(payload, dict)
                return payload
            if isinstance(call_json_side_effect, BaseException):
                raise call_json_side_effect
            assert isinstance(call_json_side_effect, dict)
            return call_json_side_effect
        # transcript_summary
        if "summary" not in call_count:
            call_count["summary"] = True
            return {"summary": "ok"}
        # classification_extraction
        return {"idea_classification": "unclear", "founder_posture": "unclear"}

    monkeypatch.setattr(pp, "_call_json", _fake_call_json)

    transcript = DebateTranscript(turns=[], total_tokens_in=0, total_tokens_out=0)

    async def _go() -> Any:
        return await pp.run_post_processors(transcript, "ctx", "idea")

    result = asyncio.run(_go())
    return result, call_count


def test_happy_path_three_competitors_no_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model returns 3 competitors → 3 propagate, section_flag is None."""
    payload = {
        "competitors": [
            json.loads(_competitor_json("AlphaCorp")),
            json.loads(_competitor_json("BetaCo")),
            json.loads(_competitor_json("GammaInc")),
        ],
        "section_flag": None,
    }
    (_, _, landscape, _, _, _), _ = _run_landscape_task_sync(
        monkeypatch, call_json_side_effect=lambda: payload
    )
    assert landscape is not None
    assert len(landscape.competitors) == 3
    assert landscape.section_flag is None


def test_truncated_mid_list_partial_recovery(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """LLM truncates mid-list → 2 valid competitors recovered, WARN logged."""
    a = _competitor_json("AlphaCorp")
    b = _competitor_json("BetaCo")
    partial = '{"competitors": [' + a + "," + b + ',{"name": "ThirdC'
    exc = LLMTruncationError(
        "LLM output truncated [finish_reason=length, completion_tokens=2000, "
        "model=deepseek/deepseek-v4-flash, provider=deepseek, gen_id=abc]",
        partial_content=partial,
    )
    with caplog.at_level(logging.WARNING):
        (_, _, landscape, _, _, _), _ = _run_landscape_task_sync(
            monkeypatch, call_json_side_effect=exc
        )
    assert landscape is not None
    assert landscape.section_flag == "truncated_partial_recovery"
    assert len(landscape.competitors) == 2
    matches = [r for r in caplog.records if r.getMessage() == "pro.market_landscape.truncated"]
    assert len(matches) == 1
    assert getattr(matches[0], "recovered_competitors", None) == 2


def test_total_truncation_zero_recovery(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """LLM truncates after first key → empty + zero-recovery flag, WARN logged."""
    exc = LLMTruncationError(
        "LLM output truncated [finish_reason=length, completion_tokens=2000, "
        "model=deepseek/deepseek-v4-flash, provider=deepseek, gen_id=abc]",
        partial_content='{"compet',
    )
    with caplog.at_level(logging.WARNING):
        (_, _, landscape, _, _, _), _ = _run_landscape_task_sync(
            monkeypatch, call_json_side_effect=exc
        )
    assert landscape is not None
    assert landscape.section_flag == "truncated_zero_recovery"
    assert landscape.competitors == []
    matches = [r for r in caplog.records if r.getMessage() == "pro.market_landscape.truncated"]
    assert len(matches) == 1
    assert getattr(matches[0], "recovered_competitors", None) == 0


def test_max_tokens_default_is_4000(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env unset → landscape task calls LLM with max_tokens=4000."""
    # Clear the lru_cache so the new env state is observed.
    monkeypatch.delenv("GECKO_MARKET_LANDSCAPE_MAX_TOKENS", raising=False)
    from gecko_core.orchestration import settings as orch_settings

    orch_settings.get_orchestration_settings.cache_clear()

    payload: dict[str, Any] = {"competitors": [], "section_flag": None}
    (_, _, _, _, _, _), counters = _run_landscape_task_sync(
        monkeypatch, call_json_side_effect=lambda: payload
    )
    assert counters.get("landscape_max_tokens") == 4000


def test_max_tokens_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """GECKO_MARKET_LANDSCAPE_MAX_TOKENS=8000 → LLM call gets max_tokens=8000."""
    monkeypatch.setenv("GECKO_MARKET_LANDSCAPE_MAX_TOKENS", "8000")
    from gecko_core.orchestration import settings as orch_settings

    orch_settings.get_orchestration_settings.cache_clear()
    try:
        payload: dict[str, Any] = {"competitors": [], "section_flag": None}
        (_, _, _, _, _, _), counters = _run_landscape_task_sync(
            monkeypatch, call_json_side_effect=lambda: payload
        )
        assert counters.get("landscape_max_tokens") == 8000
    finally:
        orch_settings.get_orchestration_settings.cache_clear()
