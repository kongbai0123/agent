from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from hermes_metrics import (  # noqa: E402
    HermesMetricsStoreError,
    MAX_LATENCY_MS,
    PersistentHermesMetricsStore,
)


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def connection_factory(path: Path):
    @contextmanager
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return connect


def test_metrics_survive_restart_and_keep_rollout_cohorts_separate(tmp_path) -> None:
    clock = FakeClock()
    connect = connection_factory(tmp_path / "metrics.db")
    first = PersistentHermesMetricsStore(
        connect,
        event_limit=8,
        clock=clock,
    )
    canary = "canary:000000:aaaaaaaaaaaa"
    percentage = "percentage:025000:bbbbbbbbbbbb"

    first.record("success", cohort=canary, latency_ms=10)
    clock.advance(1)
    first.record("failure", cohort=canary, latency_ms=30)
    first.record("fallback", cohort=canary)
    first.record("tool_policy_denial", cohort=canary)
    first.record("probe_failure", cohort=canary)

    restarted = PersistentHermesMetricsStore(
        connect,
        event_limit=8,
        clock=clock,
    )
    canary_snapshot = restarted.snapshot(cohort=canary)
    fresh_stage = restarted.snapshot(cohort=percentage)

    assert canary_snapshot["persistent"] is True
    assert canary_snapshot["totals"] == {
        "failure": 1,
        "fallback": 1,
        "probe_failure": 1,
        "success": 1,
        "tool_policy_denial": 1,
    }
    assert canary_snapshot["window"]["sample_count"] == 5
    assert canary_snapshot["window"]["success_rate"] == 0.5
    assert canary_snapshot["window"]["last_event_at"] == clock.value
    assert canary_snapshot["latency_ms"] == {
        "count": 2,
        "average": 20.0,
        "maximum": 30,
        "p95_recent": 30,
    }
    assert fresh_stage["totals"] == canary_snapshot["totals"]
    assert fresh_stage["window"]["sample_count"] == 0
    assert fresh_stage["window"]["success_rate"] is None
    assert fresh_stage["latency_ms"]["count"] == 0


def test_event_storage_and_latency_are_bounded_and_payload_free(tmp_path) -> None:
    clock = FakeClock()
    path = tmp_path / "metrics.db"
    connect = connection_factory(path)
    store = PersistentHermesMetricsStore(connect, event_limit=3, clock=clock)
    cohort = "all:100000:cccccccccccc"

    for index in range(7):
        store.record(
            "success" if index % 2 == 0 else "failure",
            cohort=cohort,
            latency_ms=MAX_LATENCY_MS + index + 1,
        )
        clock.advance(1)

    snapshot = store.snapshot(cohort=cohort)
    with connect() as conn:
        rows = conn.execute(
            "SELECT cohort, kind, latency_ms, created_at "
            "FROM hermes_operational_metric_events"
        ).fetchall()
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(hermes_operational_metric_events)"
            ).fetchall()
        }

    assert len(rows) == 3
    assert snapshot["retained_events"] == 3
    assert snapshot["latency_ms"]["maximum"] == MAX_LATENCY_MS
    assert columns == {"event_id", "cohort", "kind", "latency_ms", "created_at"}
    encoded = json.dumps(snapshot, sort_keys=True)
    for forbidden in (
        "prompt",
        "content",
        "path",
        "query",
        "secret",
        "session_id",
        "run_id",
    ):
        assert forbidden not in encoded.casefold()


def test_circuit_state_and_retry_deadline_survive_store_recreation(tmp_path) -> None:
    clock = FakeClock()
    connect = connection_factory(tmp_path / "metrics.db")
    first = PersistentHermesMetricsStore(connect, clock=clock)
    cohort = "all:100000:dddddddddddd"

    first.update_circuit(
        "open",
        consecutive_failures=3,
        retry_after_seconds=10,
    )
    restarted = PersistentHermesMetricsStore(connect, clock=clock)
    initial = restarted.snapshot(cohort=cohort)["circuit_breaker"]
    clock.advance(4)
    later = restarted.snapshot(cohort=cohort)["circuit_breaker"]

    assert initial["state"] == "open"
    assert initial["consecutive_failures"] == 3
    assert initial["retry_after_seconds"] == 10
    assert initial["transition_count"] == 1
    assert later["retry_after_seconds"] == 6


@pytest.mark.parametrize(
    ("kind", "cohort", "latency"),
    [
        ("prompt", "all:100000:eeeeeeeeeeee", None),
        ("success", "C:/private/project", None),
        ("failure", "all:100000:eeeeeeeeeeee", float("inf")),
        ("failure", "all:100000:eeeeeeeeeeee", -1),
    ],
)
def test_store_rejects_unbounded_or_sensitive_shaped_fields(
    tmp_path,
    kind,
    cohort,
    latency,
) -> None:
    store = PersistentHermesMetricsStore(
        connection_factory(tmp_path / "metrics.db")
    )
    with pytest.raises(HermesMetricsStoreError):
        store.record(kind, cohort=cohort, latency_ms=latency)
