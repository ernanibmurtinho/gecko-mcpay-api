"""Token-counting integration test (S1-03).

Stubs `build_groupchat` with fakes that own a `client.total_usage_summary`
dict — exactly the attribute path AG2 0.12 / autogen 0.7 exposes on each
ConversableAgent's wrapped OpenAIWrapper. Asserts that `pro.generate`
populates `tokens_in` and `tokens_out` on every turn end.
"""

from __future__ import annotations

import pytest


class _FakeOpenAIWrapper:
    """Models AG2's OpenAIWrapper.total_usage_summary growth across calls."""

    def __init__(self, model: str, prompt: int, completion: int) -> None:
        self._model = model
        self._delta = (prompt, completion)
        # Mirror AG2's shape exactly. None until the first call.
        self.total_usage_summary: dict[str, object] | None = None

    def _record(self) -> None:
        if self.total_usage_summary is None:
            self.total_usage_summary = {"total_cost": 0.0}
        # Always carry total_cost as a float — AG2 does the same.
        self.total_usage_summary["total_cost"] = float(
            self.total_usage_summary.get("total_cost", 0.0)  # type: ignore[arg-type]
        )
        per_model = self.total_usage_summary.get(self._model, {})
        if not isinstance(per_model, dict):
            per_model = {}
        per_model = {
            "prompt_tokens": int(per_model.get("prompt_tokens", 0)) + self._delta[0],
            "completion_tokens": int(per_model.get("completion_tokens", 0)) + self._delta[1],
        }
        self.total_usage_summary[self._model] = per_model


class _FakeAgent:
    def __init__(self, name: str, prompt: int, completion: int) -> None:
        self.name = name
        self.client = _FakeOpenAIWrapper(f"model-{name}", prompt, completion)
        self._reply = f"{name} reply"

    async def a_generate_reply(self, messages: object = None) -> str:
        # Production AG2 mutates client state inside this call. We do the
        # same so the before/after diff produces non-zero deltas.
        self.client._record()
        return self._reply


class _FakeChat:
    def __init__(self, agents: list[_FakeAgent]) -> None:
        self.agents = agents
        self.messages: list[dict[str, object]] = []


class _FakeMgr:
    def __init__(self, agents: list[_FakeAgent]) -> None:
        self.groupchat = _FakeChat(agents)


@pytest.fixture
def stub_build(monkeypatch: pytest.MonkeyPatch) -> dict[str, _FakeAgent]:
    """Replace build_groupchat with a fake that records usage per agent."""
    from gecko_core.orchestration import pro as pro_mod

    canned = {
        "analyst": (100, 50),
        "critic": (90, 40),
        "architect": (80, 30),
        "scoper": (70, 25),
        "judge": (200, 80),
    }
    agents = [_FakeAgent(name, p, c) for name, (p, c) in canned.items()]

    def _build(_cfg: object, *, model_matrix: object = None) -> _FakeMgr:
        return _FakeMgr(agents)

    monkeypatch.setattr(pro_mod, "build_groupchat", _build)
    return {a.name: a for a in agents}


async def test_token_counts_populated_from_client_delta(
    stub_build: dict[str, _FakeAgent],
) -> None:
    from gecko_core.orchestration.pro import generate

    transcript = await generate(
        idea="x",
        rag_context="y",
        llm_config={"config_list": [{"model": "m", "api_key": "k", "base_url": "u"}]},
    )

    # Every turn carries non-zero token counts derived from the client delta.
    for turn in transcript.turns:
        assert turn.tokens_in > 0, f"{turn.agent} tokens_in must be > 0"
        assert turn.tokens_out > 0, f"{turn.agent} tokens_out must be > 0"

    # Totals match the canned per-call deltas.
    assert transcript.total_tokens_in == 100 + 90 + 80 + 70 + 200
    assert transcript.total_tokens_out == 50 + 40 + 30 + 25 + 80
