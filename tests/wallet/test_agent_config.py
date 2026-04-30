"""S9-DOCTOR-01 — shared frames.ag agent-config reader.

Covers the three states bb doctor + gecko-mcp's wallet facade need to
distinguish:

  - file absent           -> read_agent_token() returns None (no raise)
  - file present + valid  -> returns the apiToken string
  - file present + broken -> read_agent_config raises AgentConfigError;
                             read_agent_token swallows it and returns None
                             (doctor must not crash on a malformed file)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gecko_core.wallet.agent_config import (
    AgentConfigError,
    read_agent_config,
    read_agent_token,
)


def test_read_agent_token_returns_none_when_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.json"
    assert read_agent_token(missing) is None
    assert read_agent_config(missing) is None


def test_read_agent_token_returns_token_when_present(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "apiToken": "fr-tok-abc-1234",
                "username": "alice",
                "solanaAddress": "Sol111",
            }
        )
    )
    assert read_agent_token(p) == "fr-tok-abc-1234"
    cfg = read_agent_config(p)
    assert cfg is not None
    assert cfg["username"] == "alice"


def test_read_agent_token_returns_none_when_token_missing(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"username": "alice"}))
    assert read_agent_token(p) is None


def test_read_agent_token_returns_none_when_token_empty(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"apiToken": "", "username": "alice"}))
    assert read_agent_token(p) is None


def test_read_agent_config_raises_on_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text("{not json")
    with pytest.raises(AgentConfigError):
        read_agent_config(p)


def test_read_agent_token_swallows_malformed_file(tmp_path: Path) -> None:
    """doctor must keep running even if the config file is corrupt."""
    p = tmp_path / "config.json"
    p.write_text("{not json")
    assert read_agent_token(p) is None


def test_read_agent_config_raises_when_not_object(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(["not", "an", "object"]))
    with pytest.raises(AgentConfigError):
        read_agent_config(p)
