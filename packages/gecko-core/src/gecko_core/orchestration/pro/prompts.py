"""Pro tier system-prompt loader.

Overview
--------

The 5 system prompts the AG2 GroupChat uses are loaded here, not hardcoded in
``agents.py``. This decouples the prompt content from the orchestration code so
the public OSS repo can ship working defaults while production runs a
privately-tuned set without code changes.

Resolution order:

1. ``GECKO_PROMPTS_PATH`` env var → JSON file at that path. Used in production
   to point at a privately-tuned prompts file (mounted via SSM, downloaded at
   container boot, etc.).
2. The bundled ``_default_prompts.json`` next to this module. Used in dev,
   tests, and the OSS install path. These are the prompts that public users
   get; they're real and tuned, not stubs.

The file format is::

    {
      "version": "v1",
      "agents": {
        "analyst":  "...",
        "critic":   "...",
        "architect":"...",
        "scoper":   "...",
        "judge":    "..."
      }
    }

Schema is enforced at load time — missing keys, empty strings, or wrong types
raise loudly so a bad override fails fast at boot rather than mid-debate.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

REQUIRED_AGENTS = ("analyst", "critic", "architect", "scoper", "judge")

_DEFAULT_PROMPTS_PATH = Path(__file__).parent / "_default_prompts.json"


class PromptsConfigError(ValueError):
    """Raised when prompts JSON is missing keys, empty, or malformed."""


def _validate(data: dict[str, object]) -> dict[str, str]:
    agents = data.get("agents")
    if not isinstance(agents, dict):
        raise PromptsConfigError("prompts JSON must have a top-level 'agents' object")
    out: dict[str, str] = {}
    for name in REQUIRED_AGENTS:
        val = agents.get(name)
        if not isinstance(val, str) or not val.strip():
            raise PromptsConfigError(
                f"prompts JSON is missing or empty for required agent '{name}'"
            )
        out[name] = val.strip()
    return out


@lru_cache(maxsize=1)
def load_prompts() -> dict[str, str]:
    """Resolve and validate the system prompts.

    Returns a ``{agent_name: system_message}`` dict containing exactly the 5
    required entries. Caches the result so re-imports don't re-parse the file.
    """
    override = os.environ.get("GECKO_PROMPTS_PATH")
    path = Path(override).expanduser() if override else _DEFAULT_PROMPTS_PATH

    if not path.is_file():
        if override:
            raise PromptsConfigError(
                f"GECKO_PROMPTS_PATH={override} does not point to a readable file"
            )
        # The bundled default should always exist; the package would be malformed otherwise.
        raise PromptsConfigError(f"bundled prompts file is missing: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PromptsConfigError(f"prompts JSON at {path} is not valid JSON: {exc}") from exc

    return _validate(data)


__all__ = ["REQUIRED_AGENTS", "PromptsConfigError", "load_prompts"]
