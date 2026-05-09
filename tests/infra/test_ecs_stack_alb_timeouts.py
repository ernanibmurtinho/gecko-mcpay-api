"""Regression sentinel for issue #16.

The 7-agent panel runs in a 30-60s wall-clock window; the AWS default ALB
idle timeout (60s) was 504-ing roughly 1-in-3 cold basic-tier calls. We
bumped ``idle_timeout.timeout_seconds`` to 120s in ``infra/ecs-stack.yml``.

This test pins the value so a future "tidy-up the LB attributes" PR cannot
silently revert it. If you genuinely need to change the value, update both
the YAML and this expected constant — and re-read issue #16 first to make
sure you're not just papering over a deeper latency regression.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ECS_STACK = REPO_ROOT / "infra" / "ecs-stack.yml"

# Bumped from the AWS default 60s in fix(#16). Anything beyond 120s should be
# treated as a perf bug, not a timeout knob to widen further.
EXPECTED_ALB_IDLE_TIMEOUT_SECONDS = "120"


class _CFNLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation ``!Ref``/``!If`` short tags."""


def _ignore_cfn_tag(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_CFNLoader.add_multi_constructor("!", _ignore_cfn_tag)


@pytest.fixture(scope="module")
def stack() -> dict[str, Any]:
    with ECS_STACK.open() as f:
        return yaml.load(f, Loader=_CFNLoader)  # noqa: S506 — custom loader, not SafeLoader bypass


def test_alb_idle_timeout_set_to_120s(stack: dict[str, Any]) -> None:
    attrs = stack["Resources"]["ALB"]["Properties"]["LoadBalancerAttributes"]
    by_key = {a["Key"]: a["Value"] for a in attrs}
    assert "idle_timeout.timeout_seconds" in by_key, (
        "ALB.LoadBalancerAttributes must set idle_timeout.timeout_seconds "
        "(see issue #16: 7-agent panel cold-starts exceed the AWS default 60s)."
    )
    assert by_key["idle_timeout.timeout_seconds"] == EXPECTED_ALB_IDLE_TIMEOUT_SECONDS


def test_target_group_healthcheck_timeout_is_fast(stack: dict[str, Any]) -> None:
    # Healthchecks should NOT be bumped to the panel's request budget — they
    # need to fail fast so unhealthy tasks rotate out quickly. Pin to <=10s.
    tg = stack["Resources"]["ALBTargetGroup"]["Properties"]
    assert int(tg["HealthCheckTimeoutSeconds"]) <= 10
