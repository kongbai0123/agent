"""Bounded, redacted operational metrics for the Hermes integration.

The store accepts only fixed metric kinds, a bounded rollout-cohort label,
numeric latency, and circuit-breaker state.  Prompts, identifiers, paths, tool
arguments, model content, exception text, and secrets have no representation.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from typing import Any, Callable, ContextManager, Optional, Protocol


METRIC_KINDS = frozenset(
    {
        "success",
        "failure",
        "fallback",
        "tool_policy_denial",
        "probe_failure",
    }
)
CIRCUIT_STATES = frozenset({"closed", "open", "half_open"})
DEFAULT_EVENT_LIMIT = 256
MAX_EVENT_LIMIT = 10_000
MAX_COUNTER = 9_000_000_000_000_000
MAX_LATENCY_MS = 3_600_000
MAX_CONSECUTIVE_FAILURES = 1_000_000
MAX_RETRY_AFTER_SECONDS = 86_400.0
_COHORT_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,95}$")


class HermesMetricsStoreError(RuntimeError):
    """Raised when operational metrics cannot be validated or persisted."""


class HermesMetricsStore(Protocol):
    """Minimal storage seam used by :class:`HermesOperationsController`."""

    def record(
        self,
        kind: str,
        *,
        cohort: str,
        latency_ms: Optional[float] = None,
    ) -> None:
        ...

    def update_circuit(
        self,
        state: str,
        *,
        consecutive_failures: int,
        retry_after_seconds: float,
    ) -> None:
        ...

    def snapshot(self, *, cohort: str) -> dict[str, Any]:
        ...


def _event_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HermesMetricsStoreError("event_limit must be an integer.")
    if not 1 <= value <= MAX_EVENT_LIMIT:
        raise HermesMetricsStoreError(
            f"event_limit must be between 1 and {MAX_EVENT_LIMIT}."
        )
    return value


def _timestamp(clock: Callable[[], float]) -> float:
    try:
        value = float(clock())
    except (TypeError, ValueError) as exc:
        raise HermesMetricsStoreError("metrics clock returned an invalid value.") from exc
    if not math.isfinite(value) or value < 0:
        raise HermesMetricsStoreError("metrics clock returned an invalid value.")
    return value


def _kind(value: Any) -> str:
    if not isinstance(value, str) or value not in METRIC_KINDS:
        raise HermesMetricsStoreError("metric kind is not allowed.")
    return value


def _cohort(value: Any) -> str:
    if not isinstance(value, str) or not _COHORT_RE.fullmatch(value):
        raise HermesMetricsStoreError("metric cohort is invalid.")
    return value


def _latency(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HermesMetricsStoreError("latency_ms must be a number.")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise HermesMetricsStoreError("latency_ms must be finite and non-negative.")
    return min(MAX_LATENCY_MS, int(round(result)))


def _circuit(
    state: Any,
    consecutive_failures: Any,
    retry_after_seconds: Any,
) -> tuple[str, int, float]:
    if not isinstance(state, str) or state not in CIRCUIT_STATES:
        raise HermesMetricsStoreError("circuit state is not allowed.")
    if (
        isinstance(consecutive_failures, bool)
        or not isinstance(consecutive_failures, int)
        or not 0 <= consecutive_failures <= MAX_CONSECUTIVE_FAILURES
    ):
        raise HermesMetricsStoreError("consecutive_failures is invalid.")
    if isinstance(retry_after_seconds, bool) or not isinstance(
        retry_after_seconds, (int, float)
    ):
        raise HermesMetricsStoreError("retry_after_seconds must be a number.")
    retry = float(retry_after_seconds)
    if not math.isfinite(retry) or retry < 0:
        raise HermesMetricsStoreError(
            "retry_after_seconds must be finite and non-negative."
        )
    return state, consecutive_failures, min(retry, MAX_RETRY_AFTER_SECONDS)


def _count(value: Any) -> int:
    if isinstance(value, bool):
        raise HermesMetricsStoreError("metrics data is corrupt.")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise HermesMetricsStoreError("metrics data is corrupt.") from exc
    if not 0 <= result <= MAX_COUNTER:
        raise HermesMetricsStoreError("metrics data is corrupt.")
    return result


def _stored_timestamp(value: Any, label: str) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise HermesMetricsStoreError(f"{label} is corrupt.") from exc
    if not math.isfinite(result) or result < 0:
        raise HermesMetricsStoreError(f"{label} is corrupt.")
    return result


def _percentile_95(values: list[int]) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def _public_snapshot(
    *,
    persistent: bool,
    cohort: str,
    event_limit: int,
    counts: dict[str, int],
    recent: list[tuple[str, str, Optional[int], float]],
    circuit_state: str,
    circuit_consecutive_failures: int,
    circuit_open_until: Optional[float],
    circuit_transition_count: int,
    updated_at: Optional[float],
    now: float,
) -> dict[str, Any]:
    cohort_events = [event for event in recent if event[0] == cohort]
    window_counts = {name: 0 for name in METRIC_KINDS}
    latencies: list[int] = []
    last_event_at: Optional[float] = None
    for _event_cohort, event_kind, latency, created_at in cohort_events:
        window_counts[event_kind] += 1
        if latency is not None:
            latencies.append(latency)
        if last_event_at is None or created_at > last_event_at:
            last_event_at = created_at
    attempts = window_counts["success"] + window_counts["failure"]
    success_rate = (
        round(window_counts["success"] / attempts, 6) if attempts else None
    )
    retry_after = (
        max(0.0, circuit_open_until - now)
        if circuit_state == "open" and circuit_open_until is not None
        else 0.0
    )
    return {
        "schema_version": 1,
        "persistent": persistent,
        "available": True,
        "cohort": cohort,
        "retention_limit": event_limit,
        "retained_events": len(recent),
        "totals": {name: counts[name] for name in sorted(METRIC_KINDS)},
        "window": {
            "sample_count": len(cohort_events),
            "completed_attempts": attempts,
            **{name: window_counts[name] for name in sorted(METRIC_KINDS)},
            "success_rate": success_rate,
            "last_event_at": last_event_at,
        },
        "latency_ms": {
            "count": len(latencies),
            "average": round(sum(latencies) / len(latencies), 3)
            if latencies
            else None,
            "maximum": max(latencies) if latencies else None,
            "p95_recent": _percentile_95(latencies),
        },
        "circuit_breaker": {
            "state": circuit_state,
            "consecutive_failures": circuit_consecutive_failures,
            "retry_after_seconds": retry_after,
            "transition_count": circuit_transition_count,
        },
        "updated_at": updated_at,
    }


def unavailable_metrics_snapshot(
    cohort: str = "unavailable:000000:000000000000",
) -> dict[str, Any]:
    """Return the fixed fail-closed schema without exposing an exception."""

    safe_cohort = _cohort(cohort)
    return {
        "schema_version": 1,
        "persistent": None,
        "available": False,
        "cohort": safe_cohort,
        "retention_limit": 0,
        "retained_events": 0,
        "totals": {name: 0 for name in sorted(METRIC_KINDS)},
        "window": {
            "sample_count": 0,
            "completed_attempts": 0,
            **{name: 0 for name in sorted(METRIC_KINDS)},
            "success_rate": None,
            "last_event_at": None,
        },
        "latency_ms": {
            "count": 0,
            "average": None,
            "maximum": None,
            "p95_recent": None,
        },
        "circuit_breaker": {
            "state": "unknown",
            "consecutive_failures": 0,
            "retry_after_seconds": 0.0,
            "transition_count": 0,
        },
        "updated_at": None,
    }


class InMemoryHermesMetricsStore:
    """Bounded process-local implementation used by focused unit tests."""

    def __init__(
        self,
        *,
        event_limit: int = DEFAULT_EVENT_LIMIT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.event_limit = _event_limit(event_limit)
        self._clock = clock
        self._counts = {name: 0 for name in METRIC_KINDS}
        self._recent: deque[tuple[str, str, Optional[int], float]] = deque(
            maxlen=self.event_limit
        )
        self._circuit_state = "closed"
        self._circuit_consecutive_failures = 0
        self._circuit_open_until: Optional[float] = None
        self._circuit_transition_count = 0
        self._updated_at: Optional[float] = None
        self._lock = threading.RLock()

    def record(
        self,
        kind: str,
        *,
        cohort: str,
        latency_ms: Optional[float] = None,
    ) -> None:
        safe_kind = _kind(kind)
        safe_cohort = _cohort(cohort)
        safe_latency = _latency(latency_ms)
        now = _timestamp(self._clock)
        with self._lock:
            self._counts[safe_kind] = min(
                MAX_COUNTER, self._counts[safe_kind] + 1
            )
            self._recent.append((safe_cohort, safe_kind, safe_latency, now))
            self._updated_at = now

    def update_circuit(
        self,
        state: str,
        *,
        consecutive_failures: int,
        retry_after_seconds: float,
    ) -> None:
        safe_state, failures, retry = _circuit(
            state, consecutive_failures, retry_after_seconds
        )
        now = _timestamp(self._clock)
        with self._lock:
            if safe_state != self._circuit_state:
                self._circuit_transition_count = min(
                    MAX_COUNTER, self._circuit_transition_count + 1
                )
            self._circuit_state = safe_state
            self._circuit_consecutive_failures = failures
            self._circuit_open_until = now + retry if safe_state == "open" else None
            self._updated_at = now

    def snapshot(self, *, cohort: str) -> dict[str, Any]:
        safe_cohort = _cohort(cohort)
        now = _timestamp(self._clock)
        with self._lock:
            return _public_snapshot(
                persistent=False,
                cohort=safe_cohort,
                event_limit=self.event_limit,
                counts=dict(self._counts),
                recent=list(self._recent),
                circuit_state=self._circuit_state,
                circuit_consecutive_failures=self._circuit_consecutive_failures,
                circuit_open_until=self._circuit_open_until,
                circuit_transition_count=self._circuit_transition_count,
                updated_at=self._updated_at,
                now=now,
            )


def _default_connection_factory() -> ContextManager[Any]:
    try:
        from database import get_db_conn
    except ModuleNotFoundError:  # pragma: no cover - package-style importers
        from backend.database import get_db_conn
    return get_db_conn()


class PersistentHermesMetricsStore:
    """SQLite metrics with one aggregate row and a fixed-size event window."""

    def __init__(
        self,
        connection_factory: Optional[
            Callable[[], ContextManager[Any]]
        ] = None,
        *,
        event_limit: int = DEFAULT_EVENT_LIMIT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._connection_factory = connection_factory or _default_connection_factory
        self.event_limit = _event_limit(event_limit)
        self._clock = clock
        self._schema_ready = False
        self._lock = threading.RLock()

    @staticmethod
    def _columns(conn: Any, table: str) -> set[str]:
        return {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _ensure_schema(self, conn: Any) -> None:
        if self._schema_ready:
            return
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hermes_operational_metrics (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                fallback_count INTEGER NOT NULL DEFAULT 0,
                tool_policy_denial_count INTEGER NOT NULL DEFAULT 0,
                probe_failure_count INTEGER NOT NULL DEFAULT 0,
                circuit_state TEXT NOT NULL DEFAULT 'closed',
                circuit_consecutive_failures INTEGER NOT NULL DEFAULT 0,
                circuit_open_until REAL,
                circuit_transition_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL
            )
            """
        )
        aggregate_columns = self._columns(conn, "hermes_operational_metrics")
        if "probe_failure_count" not in aggregate_columns:
            conn.execute(
                """
                ALTER TABLE hermes_operational_metrics
                ADD COLUMN probe_failure_count INTEGER NOT NULL DEFAULT 0
                """
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hermes_operational_metric_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                cohort TEXT NOT NULL,
                kind TEXT NOT NULL,
                latency_ms INTEGER,
                created_at REAL NOT NULL
            )
            """
        )
        event_columns = self._columns(conn, "hermes_operational_metric_events")
        if "cohort" not in event_columns:
            conn.execute(
                """
                ALTER TABLE hermes_operational_metric_events
                ADD COLUMN cohort TEXT NOT NULL DEFAULT 'legacy'
                """
            )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_hermes_metric_events_created
            ON hermes_operational_metric_events(event_id DESC)
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO hermes_operational_metrics(singleton)
            VALUES (1)
            """
        )
        conn.execute(
            """
            DELETE FROM hermes_operational_metric_events
            WHERE event_id NOT IN (
                SELECT event_id
                FROM hermes_operational_metric_events
                ORDER BY event_id DESC
                LIMIT ?
            )
            """,
            (self.event_limit,),
        )
        self._schema_ready = True

    def record(
        self,
        kind: str,
        *,
        cohort: str,
        latency_ms: Optional[float] = None,
    ) -> None:
        safe_kind = _kind(kind)
        safe_cohort = _cohort(cohort)
        safe_latency = _latency(latency_ms)
        now = _timestamp(self._clock)
        column = {
            "success": "success_count",
            "failure": "failure_count",
            "fallback": "fallback_count",
            "tool_policy_denial": "tool_policy_denial_count",
            "probe_failure": "probe_failure_count",
        }[safe_kind]
        with self._lock, self._connection_factory() as conn:
            self._ensure_schema(conn)
            conn.execute(
                f"""
                UPDATE hermes_operational_metrics
                SET {column} = MIN({column} + 1, ?),
                    updated_at = ?
                WHERE singleton = 1
                """,
                (MAX_COUNTER, now),
            )
            conn.execute(
                """
                INSERT INTO hermes_operational_metric_events(
                    cohort, kind, latency_ms, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (safe_cohort, safe_kind, safe_latency, now),
            )
            conn.execute(
                """
                DELETE FROM hermes_operational_metric_events
                WHERE event_id NOT IN (
                    SELECT event_id
                    FROM hermes_operational_metric_events
                    ORDER BY event_id DESC
                    LIMIT ?
                )
                """,
                (self.event_limit,),
            )

    def update_circuit(
        self,
        state: str,
        *,
        consecutive_failures: int,
        retry_after_seconds: float,
    ) -> None:
        safe_state, failures, retry = _circuit(
            state, consecutive_failures, retry_after_seconds
        )
        now = _timestamp(self._clock)
        open_until = now + retry if safe_state == "open" else None
        with self._lock, self._connection_factory() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                UPDATE hermes_operational_metrics
                SET circuit_transition_count = MIN(
                        circuit_transition_count
                        + CASE WHEN circuit_state <> ? THEN 1 ELSE 0 END,
                        ?
                    ),
                    circuit_state = ?,
                    circuit_consecutive_failures = ?,
                    circuit_open_until = ?,
                    updated_at = ?
                WHERE singleton = 1
                """,
                (
                    safe_state,
                    MAX_COUNTER,
                    safe_state,
                    failures,
                    open_until,
                    now,
                ),
            )

    def snapshot(self, *, cohort: str) -> dict[str, Any]:
        safe_cohort = _cohort(cohort)
        now = _timestamp(self._clock)
        with self._lock, self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM hermes_operational_metrics WHERE singleton = 1"
            ).fetchone()
            events = conn.execute(
                """
                SELECT cohort, kind, latency_ms, created_at
                FROM hermes_operational_metric_events
                ORDER BY event_id DESC
                LIMIT ?
                """,
                (self.event_limit,),
            ).fetchall()
        if row is None:
            raise HermesMetricsStoreError("metrics aggregate is unavailable.")
        state = str(row["circuit_state"] or "")
        if state not in CIRCUIT_STATES:
            raise HermesMetricsStoreError("metrics circuit state is corrupt.")
        recent: list[tuple[str, str, Optional[int], float]] = []
        for event in reversed(events):
            event_cohort = _cohort(event["cohort"])
            event_kind = _kind(event["kind"])
            event_latency = _latency(event["latency_ms"])
            created_at = _stored_timestamp(
                event["created_at"], "metrics event timestamp"
            )
            if created_at is None:
                raise HermesMetricsStoreError("metrics event timestamp is corrupt.")
            recent.append(
                (event_cohort, event_kind, event_latency, created_at)
            )
        open_until = _stored_timestamp(
            row["circuit_open_until"], "metrics circuit deadline"
        )
        updated_at = _stored_timestamp(row["updated_at"], "metrics timestamp")
        return _public_snapshot(
            persistent=True,
            cohort=safe_cohort,
            event_limit=self.event_limit,
            counts={
                "success": _count(row["success_count"]),
                "failure": _count(row["failure_count"]),
                "fallback": _count(row["fallback_count"]),
                "tool_policy_denial": _count(row["tool_policy_denial_count"]),
                "probe_failure": _count(row["probe_failure_count"]),
            },
            recent=recent,
            circuit_state=state,
            circuit_consecutive_failures=_count(
                row["circuit_consecutive_failures"]
            ),
            circuit_open_until=open_until,
            circuit_transition_count=_count(row["circuit_transition_count"]),
            updated_at=updated_at,
            now=now,
        )


__all__ = [
    "CIRCUIT_STATES",
    "DEFAULT_EVENT_LIMIT",
    "HermesMetricsStore",
    "HermesMetricsStoreError",
    "InMemoryHermesMetricsStore",
    "MAX_EVENT_LIMIT",
    "MAX_LATENCY_MS",
    "METRIC_KINDS",
    "PersistentHermesMetricsStore",
    "unavailable_metrics_snapshot",
]
