"""Production rollout gates for Hermes text traffic.

Rollout settings remain the source of truth.  This module adds the operational
gate that settings validation cannot provide: a promotion is allowed only one
step at a time and only after the *current cohort* has enough healthy,
persistent evidence.  Rollback is intentionally faster and may move to any
lower stage without waiting for the sidecar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional


REQUIRED_RUN_FEATURES = (
    "run_approval_response",
    "run_events_sse",
    "run_status",
    "run_stop",
    "run_submission",
)


@dataclass(frozen=True)
class RolloutStage:
    name: str
    mode: str
    percentage: float
    minimum_samples: int
    minimum_success_rate: float

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "percentage": self.percentage,
            "minimum_samples": self.minimum_samples,
            "minimum_success_rate": self.minimum_success_rate,
        }


# Thresholds apply to evidence collected while the *current* stage/cohort is
# active.  Hermes metrics uses a configuration-derived cohort key, so evidence
# from a previous percentage cannot accidentally promote the next percentage.
ROLLOUT_STAGES: tuple[RolloutStage, ...] = (
    RolloutStage("disabled", "disabled", 0.0, 0, 1.0),
    RolloutStage("canary", "canary", 0.0, 20, 0.95),
    RolloutStage("percentage_5", "percentage", 5.0, 50, 0.97),
    RolloutStage("percentage_25", "percentage", 25.0, 100, 0.98),
    RolloutStage("percentage_50", "percentage", 50.0, 200, 0.99),
    RolloutStage("all", "all", 100.0, 0, 1.0),
)
class HermesRolloutError(ValueError):
    """A rollout transition was invalid or lacked production evidence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def rollout_stage(settings: Mapping[str, Any]) -> Optional[RolloutStage]:
    mode = str(settings.get("hermes_rollout_mode") or "disabled").strip().casefold()
    percentage = _number(settings.get("hermes_rollout_percentage"), 0.0)
    for stage in ROLLOUT_STAGES:
        if mode == stage.mode and (
            mode != "percentage" or percentage == stage.percentage
        ):
            return stage
    return None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


class HermesRolloutGate:
    """Evaluate and enforce one-way promotion plus immediate rollback."""

    def __init__(
        self,
        *,
        status_provider: Callable[[], Mapping[str, Any]],
    ) -> None:
        self._status_provider = status_provider

    def _status(self) -> Mapping[str, Any]:
        try:
            value = self._status_provider()
        except Exception:
            return {}
        return value if isinstance(value, Mapping) else {}

    def readiness(
        self,
        settings: Mapping[str, Any],
        *,
        status: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        current = rollout_stage(settings)
        evaluated_at = datetime.now(timezone.utc).isoformat()
        if current is None:
            return {
                "current_stage": "invalid",
                "next_stage": None,
                "can_promote": False,
                "blockers": [
                    {
                        "code": "rollout_stage_invalid",
                        "message": "The persisted Hermes rollout stage is invalid.",
                    }
                ],
                "evaluated_at": evaluated_at,
            }
        current_index = ROLLOUT_STAGES.index(current)
        next_stage = (
            ROLLOUT_STAGES[current_index + 1]
            if current_index + 1 < len(ROLLOUT_STAGES)
            else None
        )
        if next_stage is None:
            return {
                "current_stage": current.name,
                "next_stage": None,
                "can_promote": False,
                "blockers": [],
                "reason": "fully_enabled",
                "requirements": {},
                "observed": {},
                "evaluated_at": evaluated_at,
            }

        live = self._status() if status is None else status
        operations = _mapping(live.get("operations"))
        health = _mapping(live.get("health"))
        circuit = _mapping(operations.get("circuit_breaker"))
        metrics = _mapping(operations.get("metrics"))
        window = _mapping(metrics.get("window"))
        health_gate = _mapping(operations.get("health_gate"))
        supervisor = _mapping(live.get("supervisor"))

        successes = _count(window.get("success"))
        failures = _count(window.get("failure"))
        # Only completed Hermes attempts count toward promotion.  The bounded
        # event window also contains fallback/probe/policy events, which must
        # never inflate rollout sample evidence.
        samples = successes + failures
        retained_cohort_events = _count(window.get("sample_count"))
        policy_denials = _count(window.get("tool_policy_denial"))
        success_rate = _number(window.get("success_rate"), 0.0)
        if "success_rate" not in window and successes + failures:
            success_rate = successes / (successes + failures)

        blockers: list[dict[str, str]] = []

        def block(code: str, message: str) -> None:
            blockers.append({"code": code, "message": message})

        if settings.get("hermes_enabled") is not True or live.get("enabled") is not True:
            block("integration_disabled", "Hermes must be enabled before rollout promotion.")
        if live.get("configured") is not True:
            block("configuration_unavailable", "Hermes production configuration is unavailable.")
        if str(live.get("deployment_mode") or "").casefold() != "docker":
            block("docker_required", "Production rollout requires the pinned Docker deployment.")
        if settings.get("hermes_fallback_enabled") is not True or live.get("fallback_enabled") is not True:
            block("fallback_required", "Basic-chat fallback must remain enabled during rollout.")
        if health.get("status") != "healthy":
            block("health_not_healthy", "Hermes must pass its current health probe.")
        if any(_mapping(live.get("features")).get(name) is not True for name in REQUIRED_RUN_FEATURES):
            block("runs_capabilities_incomplete", "All required Hermes Runs capabilities must be verified.")
        if circuit.get("state") != "closed":
            block("circuit_not_closed", "The Hermes circuit breaker must be closed.")
        if health_gate and health_gate.get("allowed") is not True:
            block("health_gate_closed", "The Hermes operational health gate is closed.")
        if supervisor and (
            supervisor.get("state") != "healthy"
            or _count(supervisor.get("consecutive_successes")) < 2
        ):
            block(
                "supervisor_not_healthy",
                "Background Hermes monitoring requires two consecutive healthy probes.",
            )
        if metrics.get("available") is not True or metrics.get("persistent") is not True:
            block("persistent_metrics_required", "Persistent Hermes rollout metrics are unavailable.")
        if current.name != "disabled" and samples < current.minimum_samples:
            block(
                "insufficient_samples",
                f"Collect at least {current.minimum_samples} completed samples in the current stage.",
            )
        if current.name != "disabled" and success_rate < current.minimum_success_rate:
            block(
                "success_rate_below_threshold",
                f"Current-stage success rate must reach {current.minimum_success_rate:.0%}.",
            )
        if policy_denials:
            block("policy_denial_observed", "A policy denial was observed in the current rollout cohort.")
        if current.name == "canary" and settings.get("hermes_tools_enabled") is True:
            block("project_tools_must_be_disabled", "Disable project tools before expanding text rollout.")
        if next_stage.name not in {"canary"} and live.get("tool_policy_profile") != "no-tools-v1":
            block("no_tools_profile_required", "Expanded text rollout requires the no-tools policy profile.")

        return {
            "current_stage": current.name,
            "next_stage": next_stage.name,
            "can_promote": not blockers,
            "blockers": blockers,
            "requirements": {
                "minimum_samples": current.minimum_samples,
                "minimum_success_rate": current.minimum_success_rate,
                "maximum_policy_denials": 0,
                "deployment_mode": "docker",
                "fallback_required": True,
            },
            "observed": {
                "sample_count": samples,
                "retained_cohort_events": retained_cohort_events,
                "success": successes,
                "failure": failures,
                "success_rate": success_rate,
                "tool_policy_denial": policy_denials,
                "metrics_cohort": metrics.get("cohort"),
                "health_status": health.get("status"),
                "circuit_state": circuit.get("state"),
                "supervisor_state": supervisor.get("state"),
                "supervisor_consecutive_successes": _count(
                    supervisor.get("consecutive_successes")
                ),
            },
            "evaluated_at": evaluated_at,
        }

    def guard(
        self,
        current_settings: Mapping[str, Any],
        requested_settings: Mapping[str, Any],
    ) -> None:
        rollout_fields = {
            "hermes_rollout_mode",
            "hermes_rollout_percentage",
            "hermes_canary_session_ids",
        }
        if not rollout_fields.intersection(requested_settings):
            return
        merged = dict(current_settings)
        merged.update({key: requested_settings[key] for key in rollout_fields if key in requested_settings})
        current = rollout_stage(current_settings)
        target = rollout_stage(merged)
        if current is None or target is None:
            raise HermesRolloutError(
                "HERMES_ROLLOUT_STAGE_INVALID",
                "Hermes rollout must use disabled, canary, 5%, 25%, 50%, or all.",
            )
        current_index = ROLLOUT_STAGES.index(current)
        target_index = ROLLOUT_STAGES.index(target)
        if target_index <= current_index:
            return
        if target_index != current_index + 1:
            raise HermesRolloutError(
                "HERMES_ROLLOUT_STAGE_SKIPPED",
                "Hermes rollout promotion must advance exactly one stage.",
            )
        readiness = self.readiness(current_settings)
        if readiness.get("can_promote") is not True:
            codes = ", ".join(
                str(item.get("code"))
                for item in readiness.get("blockers", ())
                if isinstance(item, Mapping)
            )
            raise HermesRolloutError(
                "HERMES_ROLLOUT_NOT_READY",
                f"Hermes rollout promotion is not ready: {codes or 'production gate closed'}.",
            )


__all__ = [
    "HermesRolloutError",
    "HermesRolloutGate",
    "ROLLOUT_STAGES",
    "RolloutStage",
    "rollout_stage",
]
