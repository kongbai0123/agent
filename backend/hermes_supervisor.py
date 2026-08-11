"""Bounded background health supervision for the optional Hermes sidecar.

The launcher owns process/container restart.  This supervisor deliberately has
no process-control authority: it only performs authenticated application-level
probes so routing can fail closed between chat turns and exposes a redacted
status snapshot for the local Workbench UI.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HermesHealthSupervisor:
    """Periodically probe Hermes without owning or restarting the sidecar."""

    def __init__(
        self,
        *,
        settings_loader: Callable[[], Mapping[str, Any]],
        manager_provider: Callable[[Mapping[str, Any]], Optional[Any]],
        probe_interval_seconds: float = 10.0,
        failure_threshold: int = 3,
    ) -> None:
        interval = float(probe_interval_seconds)
        if not 1.0 <= interval <= 300.0:
            raise ValueError("probe_interval_seconds must be between 1 and 300")
        if isinstance(failure_threshold, bool) or not 1 <= int(failure_threshold) <= 100:
            raise ValueError("failure_threshold must be between 1 and 100")
        self._settings_loader = settings_loader
        self._manager_provider = manager_provider
        self._interval = interval
        self._failure_threshold = int(failure_threshold)
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._lock = threading.RLock()
        self._running = False
        self._state = "stopped"
        self._started_at: Optional[str] = None
        self._last_probe_at: Optional[str] = None
        self._last_probe_success: Optional[bool] = None
        self._last_reason = "not_started"
        self._probes_total = 0
        self._successful_probes = 0
        self._failed_probes = 0
        self._consecutive_failures = 0
        self._consecutive_successes = 0

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        with self._lock:
            self._running = True
            self._state = "starting"
            self._started_at = _utc_now()
            self._last_reason = "awaiting_probe"
            self._consecutive_failures = 0
            self._consecutive_successes = 0
        self._task = asyncio.create_task(
            self._run(), name="hermes-health-supervisor"
        )

    async def stop(self) -> None:
        task = self._task
        event = self._stop_event
        if event is not None:
            event.set()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=min(2.0, self._interval + 0.5))
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._task = None
        self._stop_event = None
        with self._lock:
            self._running = False
            self._state = "stopped"

    async def probe_once(self) -> dict[str, Any]:
        """Run one probe; exception types are reduced to stable reason codes."""

        def perform() -> tuple[bool, str]:
            try:
                settings = dict(self._settings_loader())
            except Exception:
                return False, "settings_unavailable"
            if settings.get("hermes_enabled") is not True:
                return False, "integration_disabled"
            try:
                manager = self._manager_provider(settings)
            except Exception:
                return False, "manager_unavailable"
            if manager is None:
                return False, "manager_unavailable"
            try:
                result = manager.probe()
            except Exception:
                return False, "probe_exception"
            if not isinstance(result, Mapping):
                return False, "probe_invalid"
            success = result.get("success") is True
            reason = str(result.get("reason") or ("probe_ok" if success else "probe_failed"))
            if not reason or len(reason) > 128 or any(ord(char) < 32 for char in reason):
                reason = "probe_invalid_reason"
            return success, reason.casefold()

        started = time.monotonic()
        success, reason = await asyncio.to_thread(perform)
        elapsed_ms = round(max(0.0, time.monotonic() - started) * 1000.0, 3)
        with self._lock:
            self._probes_total += 1
            self._last_probe_at = _utc_now()
            self._last_probe_success = success
            self._last_reason = reason
            if success:
                self._successful_probes += 1
                self._consecutive_failures = 0
                self._consecutive_successes += 1
                self._state = "healthy"
            else:
                self._failed_probes += 1
                self._consecutive_successes = 0
                if reason == "integration_disabled":
                    self._consecutive_failures = 0
                    self._state = "disabled"
                else:
                    self._consecutive_failures += 1
                    self._state = (
                        "unhealthy"
                        if self._consecutive_failures >= self._failure_threshold
                        else "degraded"
                    )
            return {
                "success": success,
                "reason": reason,
                "elapsed_ms": elapsed_ms,
                "state": self._state,
            }

    async def _run(self) -> None:
        assert self._stop_event is not None
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._interval
                    )
                except asyncio.TimeoutError:
                    await self.probe_once()
        finally:
            with self._lock:
                self._running = False

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "state": self._state,
                "probe_interval_seconds": self._interval,
                "failure_threshold": self._failure_threshold,
                "started_at": self._started_at,
                "last_probe_at": self._last_probe_at,
                "last_probe_success": self._last_probe_success,
                "last_reason": self._last_reason,
                "probes_total": self._probes_total,
                "successful_probes": self._successful_probes,
                "failed_probes": self._failed_probes,
                "consecutive_failures": self._consecutive_failures,
                "consecutive_successes": self._consecutive_successes,
            }


__all__ = ["HermesHealthSupervisor"]
