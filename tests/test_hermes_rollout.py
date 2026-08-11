from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from hermes_rollout import (  # noqa: E402
    HermesRolloutError,
    HermesRolloutGate,
    rollout_stage,
)


def _settings(mode="canary", percentage=0.0, *, tools=False):
    return {
        "hermes_enabled": True,
        "hermes_rollout_mode": mode,
        "hermes_rollout_percentage": percentage,
        "hermes_canary_session_ids": ["sess-canary"] if mode == "canary" else [],
        "hermes_tools_enabled": tools,
        "hermes_fallback_enabled": True,
    }


def _status(*, samples=20, success=20, failure=0, policy_denial=0):
    total = success + failure
    return {
        "enabled": True,
        "configured": True,
        "deployment_mode": "docker",
        "tool_policy_profile": "no-tools-v1",
        "fallback_enabled": True,
        "tools_enabled": False,
        "health": {"status": "healthy"},
        "features": {
            "run_approval_response": True,
            "run_events_sse": True,
            "run_status": True,
            "run_stop": True,
            "run_submission": True,
        },
        "supervisor": {"state": "healthy", "consecutive_successes": 2},
        "operations": {
            "circuit_breaker": {"state": "closed"},
            "health_gate": {"allowed": True},
            "metrics": {
                "available": True,
                "persistent": True,
                "cohort": "safe-hash",
                "window": {
                    "sample_count": samples,
                    "success": success,
                    "failure": failure,
                    "success_rate": success / total if total else 0.0,
                    "tool_policy_denial": policy_denial,
                },
            },
        },
    }


def test_rollout_stage_ladder_is_exact():
    expected = [
        ("disabled", 0, "disabled"),
        ("canary", 0, "canary"),
        ("percentage", 5, "percentage_5"),
        ("percentage", 25, "percentage_25"),
        ("percentage", 50, "percentage_50"),
        ("all", 100, "all"),
    ]
    for mode, percentage, name in expected:
        assert rollout_stage(_settings(mode, percentage)).name == name
    assert rollout_stage(_settings("percentage", 10)) is None


def test_canary_promotes_only_with_persistent_cohort_evidence():
    status = _status(samples=20, success=19, failure=1)
    gate = HermesRolloutGate(status_provider=lambda: status)
    current = _settings()

    readiness = gate.readiness(current)
    assert readiness["can_promote"] is True
    assert readiness["next_stage"] == "percentage_5"
    gate.guard(
        current,
        {
            "hermes_rollout_mode": "percentage",
            "hermes_rollout_percentage": 5,
        },
    )


def test_promotion_fails_closed_for_samples_health_policy_or_tools():
    cases = [
        (_status(samples=19, success=19), _settings(), "insufficient_samples"),
        (_status(samples=20, success=18, failure=2), _settings(), "success_rate_below_threshold"),
        (_status(samples=20, success=20, policy_denial=1), _settings(), "policy_denial_observed"),
        (_status(samples=20, success=20), _settings(tools=True), "project_tools_must_be_disabled"),
    ]
    cases[0][0]["health"]["status"] = "healthy"
    for status, current, expected in cases:
        gate = HermesRolloutGate(status_provider=lambda status=status: status)
        readiness = gate.readiness(current)
        assert readiness["can_promote"] is False
        assert expected in {item["code"] for item in readiness["blockers"]}
        with pytest.raises(HermesRolloutError, match="not ready"):
            gate.guard(
                current,
                {
                    "hermes_rollout_mode": "percentage",
                    "hermes_rollout_percentage": 5,
                },
            )


def test_promotion_cannot_skip_but_rollback_can_jump_to_disabled():
    gate = HermesRolloutGate(status_provider=lambda: _status())
    current = _settings("percentage", 25)

    with pytest.raises(HermesRolloutError, match="exactly one"):
        gate.guard(
            current,
            {"hermes_rollout_mode": "all", "hermes_rollout_percentage": 100},
        )

    gate.guard(
        current,
        {"hermes_rollout_mode": "disabled", "hermes_rollout_percentage": 0},
    )


def test_rollout_status_never_exposes_canary_or_project_identity():
    gate = HermesRolloutGate(status_provider=lambda: _status())
    readiness = gate.readiness(
        {
            **_settings(),
            "hermes_canary_session_ids": ["sensitive-session"],
            "hermes_readonly_project_id": "sensitive-project",
        }
    )
    rendered = str(readiness)
    assert "sensitive-session" not in rendered
    assert "sensitive-project" not in rendered
