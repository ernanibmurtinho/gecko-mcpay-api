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


def test_general_suite_has_20_ideas() -> None:
    ideas = runner._load_ideas(filter_id=None, suite="general")
    assert len(ideas) == 20
    kills = [i for i in ideas if i["expected_verdict"] == "kill"]
    ships = [i for i in ideas if i["expected_verdict"] == "ship"]
    assert len(kills) == 10
    assert len(ships) == 10


def test_crypto_suite_has_15_ideas_balanced() -> None:
    ideas = runner._load_ideas(filter_id=None, suite="crypto")
    assert len(ideas) == 15
    kills = [i for i in ideas if i["expected_verdict"] == "kill"]
    ships = [i for i in ideas if i["expected_verdict"] == "ship"]
    assert len(kills) == 8
    assert len(ships) == 7


def test_saas_suite_has_15_ideas_balanced() -> None:
    ideas = runner._load_ideas(filter_id=None, suite="saas")
    assert len(ideas) == 15
    kills = [i for i in ideas if i["expected_verdict"] == "kill"]
    ships = [i for i in ideas if i["expected_verdict"] == "ship"]
    assert len(kills) == 8
    assert len(ships) == 7


def test_all_suites_total_50_ideas() -> None:
    ideas = runner._load_ideas(filter_id=None)
    assert len(ideas) == 50


def test_every_idea_has_a_mock_transcript() -> None:
    ideas = runner._load_ideas(filter_id=None)
    for idea in ideas:
        assert idea["id"] in MOCK_TRANSCRIPTS, f"missing mock for {idea['id']}"


def test_every_idea_has_required_new_fields() -> None:
    ideas = runner._load_ideas(filter_id=None)
    for idea in ideas:
        assert "expected_categories" in idea, f"{idea['id']} missing expected_categories"
        assert isinstance(idea["expected_categories"], list)
        assert "must_cite_sources" in idea, f"{idea['id']} missing must_cite_sources"
        assert isinstance(idea["must_cite_sources"], list)


def test_idea_text_under_300_chars() -> None:
    """Pydantic on /research enforces this; keep the suite ideas under the
    same limit so live mode doesn't 422 on its own fixtures."""
    ideas = runner._load_ideas(filter_id=None)
    for idea in ideas:
        assert len(idea["text"]) <= 300, (
            f"{idea['id']} text exceeds 300 chars ({len(idea['text'])})"
        )


def test_mock_run_produces_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Redirect baselines into tmp so we don't pollute the repo.
    monkeypatch.setattr(runner, "BASELINES_DIR", tmp_path)
    rc = runner.main(["--no-save", "--suite", "general"])
    assert rc == 0


def test_mock_run_writes_per_suite_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "BASELINES_DIR", tmp_path)
    rc = runner.main(["--suite", "general"])
    assert rc == 0
    out = tmp_path / "general_baseline.json"
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["mode"] == "mock"
    assert payload["suite"] == "general"
    assert len(payload["ideas"]) == 20
    assert "kill_rate" in payload["aggregate"]
    assert "verdict_accuracy" in payload["aggregate"]
    # General suite is hand-curated; mock rubric should clear 0.7 here.
    assert payload["aggregate"]["verdict_accuracy"] >= 0.7


def test_mock_run_all_suites_emits_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "BASELINES_DIR", tmp_path)
    rc = runner.main(["--suite", "all"])
    assert rc == 0
    out = tmp_path / "all_baseline.json"
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["suite"] == "all"
    assert "suites" in payload
    assert set(payload["suites"].keys()) == {"general", "crypto", "saas"}
    # 50 ideas across all three suites.
    total = sum(s["aggregate"]["n"] for s in payload["suites"].values())
    assert total == 50
    assert payload["aggregate"]["n"] == 50


def test_baseline_diff_passes_against_self(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running mock mode twice and diffing run2 vs run1 must produce zero
    regressions — the deterministic scorer is reproducible."""
    monkeypatch.setattr(runner, "BASELINES_DIR", tmp_path)
    runner.main(["--suite", "general"])
    baseline_path = tmp_path / "general_baseline.json"
    assert baseline_path.exists()

    rc = runner.main(["--suite", "general", "--baseline", str(baseline_path), "--no-save"])
    assert rc == 0


def test_filter_to_single_idea(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "BASELINES_DIR", tmp_path)
    rc = runner.main(["--suite", "general", "--idea", "good-devbrief"])
    assert rc == 0
    payload = json.loads((tmp_path / "general_baseline.json").read_text())
    assert len(payload["ideas"]) == 1
    assert payload["ideas"][0]["id"] == "good-devbrief"


def test_unknown_idea_id_errors() -> None:
    with pytest.raises(SystemExit):
        runner._load_ideas(filter_id="nonexistent")


def test_unknown_suite_errors() -> None:
    with pytest.raises(SystemExit):
        runner._load_suite("does-not-exist")


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


def test_live_rag_flag_is_wired() -> None:
    """S4-TWITSH-03: argparse must accept `--live-rag` and surface it as
    `args.live_rag`. The flag is a no-op without `--live`, but must parse."""
    parser = runner._build_parser()
    args = parser.parse_args(["--live", "--live-rag", "--suite", "holdout_live"])
    assert args.live_rag is True
    assert args.live is True
    assert args.suite == "holdout_live"

    # Default (omitted) is False so existing baselines aren't affected.
    default_args = parser.parse_args([])
    assert default_args.live_rag is False


def test_holdout_live_suite_has_10_balanced_ideas() -> None:
    """The new live-rag gate suite: 10 ideas, 5 ship + 5 kill, NO precedents."""
    ideas = runner._load_suite("holdout_live")
    assert len(ideas) == 10
    kills = [i for i in ideas if i["expected_verdict"] == "kill"]
    ships = [i for i in ideas if i["expected_verdict"] == "ship"]
    assert len(kills) == 5
    assert len(ships) == 5
    # Strip-down check: no canned signals so the harness must use --live-rag.
    for idea in ideas:
        assert "mock_precedents" not in idea, f"{idea['id']} must not pre-cache precedents"
        assert "rag_context" not in idea, f"{idea['id']} must not pre-cache rag_context"


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


def test_phase_dir_pre_product_aliases_suites() -> None:
    """S13-PHASE-03 — pre_product resolves to the legacy suites/ directory."""
    assert runner._phase_dir("pre_product") == runner.SUITES_DIR


def test_phase_dir_during_build_resolves_to_fixtures() -> None:
    """S13-PHASE-03 — during_build resolves under fixtures/."""
    p = runner._phase_dir("during_build")
    assert p == runner.FIXTURES_DIR / "during_build"
    assert p.is_dir(), f"expected {p} to exist as a directory"


def test_phase_dir_ongoing_resolves_to_fixtures() -> None:
    """S13-PHASE-03 — ongoing resolves under fixtures/."""
    p = runner._phase_dir("ongoing")
    assert p == runner.FIXTURES_DIR / "ongoing"
    assert p.is_dir()


def test_phase_dir_unknown_phase_raises() -> None:
    """Unknown phase short-circuits with a clear error rather than silently no-op."""
    with pytest.raises(SystemExit):
        runner._phase_dir("launching")


def test_load_ideas_default_phase_is_pre_product() -> None:
    """Loading with the default phase returns suites/ ideas with `_phase=pre_product`."""
    ideas = runner._load_ideas(filter_id=None, suite="general")
    assert ideas, "expected the general suite to be non-empty"
    assert all(i.get("_phase") == "pre_product" for i in ideas)
