"""Self-tests for the eval harness in mock mode.

These run in default `uv run pytest`. They MUST NOT make any network
calls — `--live` is the manual escape hatch.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from tests.eval import runner
from tests.eval.mocks import MOCK_TRANSCRIPTS


def test_ideas_yaml_has_20_ideas() -> None:
    ideas = runner._load_ideas(filter_id=None)
    assert len(ideas) == 20
    kills = [i for i in ideas if i["expected_verdict"] == "kill"]
    ships = [i for i in ideas if i["expected_verdict"] == "ship"]
    assert len(kills) == 10
    assert len(ships) == 10


def test_every_idea_has_a_mock_transcript() -> None:
    ideas = runner._load_ideas(filter_id=None)
    for idea in ideas:
        assert idea["id"] in MOCK_TRANSCRIPTS, f"missing mock for {idea['id']}"


def test_mock_run_produces_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Redirect baselines into tmp so we don't pollute the repo.
    monkeypatch.setattr(runner, "BASELINES_DIR", tmp_path)
    rc = runner.main(["--no-save"])
    assert rc == 0


def test_mock_run_writes_json_with_expected_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "BASELINES_DIR", tmp_path)
    rc = runner.main([])
    assert rc == 0
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["mode"] == "mock"
    assert len(payload["ideas"]) == 20
    assert "kill_rate" in payload["aggregate"]
    assert "verdict_accuracy" in payload["aggregate"]
    # Sanity: rubric is calibrated such that the canned transcripts agree
    # with their `expected_verdict` at >70% accuracy. If this drops, the
    # mocks or the rubric drifted.
    assert payload["aggregate"]["verdict_accuracy"] >= 0.7


def test_baseline_diff_passes_against_self(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running mock mode twice and diffing run2 vs run1 must produce zero
    regressions — the deterministic scorer is reproducible."""
    monkeypatch.setattr(runner, "BASELINES_DIR", tmp_path)
    runner.main([])
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    baseline_path = files[0]

    rc = runner.main(["--baseline", str(baseline_path), "--no-save"])
    assert rc == 0


def test_filter_to_single_idea(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "BASELINES_DIR", tmp_path)
    rc = runner.main(["--idea", "good-devbrief"])
    assert rc == 0
    files = list(tmp_path.glob("*.json"))
    payload = json.loads(files[0].read_text())
    assert len(payload["ideas"]) == 1
    assert payload["ideas"][0]["id"] == "good-devbrief"


def test_unknown_idea_id_errors() -> None:
    with pytest.raises(SystemExit):
        runner._load_ideas(filter_id="nonexistent")


def test_live_without_openai_key_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """The --live path must fail loudly if OPENAI_API_KEY is missing,
    rather than silently degrading or making a half-call."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-anthropic")
    import asyncio

    with pytest.raises(SystemExit, match="OPENAI_API_KEY"):
        asyncio.run(runner._run_live("any idea"))


def test_live_without_anthropic_key_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """If OPENAI is set but neither ANTHROPIC nor CLAUDE key is set,
    fail before spending on agents."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-openai")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    import asyncio

    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        asyncio.run(runner._run_live("any idea"))


def test_live_accepts_claude_api_key_alias() -> None:
    """CLAUDE_API_KEY should satisfy the Anthropic-key check.

    Asserted against the rubric module directly (not via _run_live) so we
    don't depend on network behavior with a fake OpenAI key.
    """
    from tests.eval import rubric

    # Inspect the source — both names must appear in the live-mode env check.
    src = inspect.getsource(rubric.score_transcript_live)
    assert "ANTHROPIC_API_KEY" in src
    assert "CLAUDE_API_KEY" in src
