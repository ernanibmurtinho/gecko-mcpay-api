"""Per-service request-shape registry for x402 calls.

Most Bazaar/paysh endpoints follow one of a small number of families:
  - chat_completions: POST with {"model": ..., "messages": [...]}
  - rest_query:       GET with ?q=... (today's default)
  - exa_search:       POST with {"query": ..., "numResults": ...}
  - graphql:          POST with {"query": "...", "variables": {...}}

A spec maps a service_id (or pattern) to:
  - which endpoint index (or selector) to use
  - HTTP method
  - body template (callable that takes the prompt + ctx and returns dict | None)
  - content type (default application/json for POST)

If a service has no spec, fall back to the legacy GET-with-?q= behavior.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

HttpMethod = Literal["GET", "POST"]

# A body builder takes (prompt, context) and returns a dict to JSON-encode,
# or None for no body (GET).
BodyBuilder = Callable[[str, Mapping[str, Any]], dict[str, Any] | None]


@dataclass(frozen=True)
class CallSpec:
    """How to call one specific service via x402."""

    service_id_pattern: str  # exact id like "exa-ai" or wildcard "*chat_completions*"
    endpoint_predicate: Callable[[Mapping[str, Any]], bool]  # filter endpoints to pick one
    method: HttpMethod
    body_builder: BodyBuilder | None  # None means no body (GET)
    content_type: str = "application/json"


# Registry. Order matters — first matching pattern wins.
_REGISTRY: tuple[CallSpec, ...] = (
    # Exa search — POST /search with {"query": ...}
    CallSpec(
        service_id_pattern="exa-ai",
        endpoint_predicate=lambda ep: ep.get("method") == "POST" and "/search" in ep.get("url", ""),
        method="POST",
        body_builder=lambda prompt, ctx: {"query": prompt, "numResults": 5, "type": "auto"},
    ),
    # OpenAI-compatible chat completions (Venice, Bankr, BlockRun, OpenAI itself).
    # Matched primarily on the endpoint URL substring "chat/completions".
    CallSpec(
        service_id_pattern="*chat/completions*",
        endpoint_predicate=lambda ep: (
            ep.get("method") == "POST" and "chat/completions" in ep.get("url", "")
        ),
        method="POST",
        body_builder=lambda prompt, ctx: {
            "model": ctx.get("model", "gpt-4o-mini"),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1500,
        },
    ),
    # Anthropic-compatible messages (Bankr's /v1/messages).
    CallSpec(
        service_id_pattern="*messages*",
        endpoint_predicate=lambda ep: (
            ep.get("method") == "POST" and "/messages" in ep.get("url", "")
        ),
        method="POST",
        body_builder=lambda prompt, ctx: {
            "model": ctx.get("model", "claude-3-5-sonnet-latest"),
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        },
    ),
)


def _matches(pattern: str, value: str) -> bool:
    """Simple glob match: '*' = any, exact otherwise."""
    if pattern == "*":
        return True
    if pattern.startswith("*") and pattern.endswith("*"):
        return pattern.strip("*") in value
    if pattern.startswith("*"):
        return value.endswith(pattern.lstrip("*"))
    if pattern.endswith("*"):
        return value.startswith(pattern.rstrip("*"))
    return pattern == value


def find_spec_for(
    service_id: str, endpoints: list[Mapping[str, Any]]
) -> tuple[CallSpec | None, Mapping[str, Any] | None]:
    """Return (spec, chosen_endpoint) or (None, None) if no spec matches.

    Tries each spec in order; for the first whose service_id_pattern matches
    AND for which at least one endpoint passes endpoint_predicate, returns
    that spec + that endpoint.
    """
    for spec in _REGISTRY:
        if not (
            _matches(spec.service_id_pattern, service_id)
            or any(_matches(spec.service_id_pattern, str(ep.get("url", ""))) for ep in endpoints)
        ):
            continue
        for ep in endpoints:
            if spec.endpoint_predicate(ep):
                return spec, ep
    return None, None


def registry_size() -> int:
    """Count of registered specs (used for diagnostics / status reporting)."""
    return len(_REGISTRY)
