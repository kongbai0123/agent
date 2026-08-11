from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from hermes_supervisor import HermesHealthSupervisor  # noqa: E402


class _Manager:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)

    def probe(self):
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_supervisor_health_state_recovers_after_bounded_failures():
    async def scenario():
        manager = _Manager(
            [
                {"success": False, "reason": "sidecar_unavailable"},
                {"success": False, "reason": "sidecar_unavailable"},
                {"success": True, "reason": "probe_ok"},
            ]
        )
        supervisor = HermesHealthSupervisor(
            settings_loader=lambda: {"hermes_enabled": True},
            manager_provider=lambda _settings: manager,
            failure_threshold=2,
        )

        first = await supervisor.probe_once()
        second = await supervisor.probe_once()
        third = await supervisor.probe_once()

        assert first["state"] == "degraded"
        assert second["state"] == "unhealthy"
        assert third["state"] == "healthy"
        status = supervisor.status()
        assert status["probes_total"] == 3
        assert status["successful_probes"] == 1
        assert status["failed_probes"] == 2
        assert status["consecutive_failures"] == 0
        assert status["consecutive_successes"] == 1
        assert "outcomes" not in status

    asyncio.run(scenario())


def test_supervisor_is_fail_closed_and_redacts_probe_exceptions():
    async def scenario():
        manager = _Manager([RuntimeError("Bearer top-secret")])
        supervisor = HermesHealthSupervisor(
            settings_loader=lambda: {"hermes_enabled": True},
            manager_provider=lambda _settings: manager,
        )

        result = await supervisor.probe_once()

        assert result["success"] is False
        assert result["reason"] == "probe_exception"
        assert "top-secret" not in str(supervisor.status())

    asyncio.run(scenario())


def test_supervisor_start_stop_is_idempotent_and_disabled_is_not_a_failure():
    async def scenario():
        supervisor = HermesHealthSupervisor(
            settings_loader=lambda: {"hermes_enabled": False},
            manager_provider=lambda _settings: (_ for _ in ()).throw(
                AssertionError("disabled integration must not build a manager")
            ),
            probe_interval_seconds=1,
        )

        await supervisor.start()
        await supervisor.start()
        await supervisor.probe_once()
        assert supervisor.status()["state"] == "disabled"
        assert supervisor.status()["consecutive_failures"] == 0
        await supervisor.stop()
        await supervisor.stop()
        assert supervisor.status()["running"] is False
        assert supervisor.status()["state"] == "stopped"

    asyncio.run(scenario())
