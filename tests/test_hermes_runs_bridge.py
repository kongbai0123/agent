from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from hermes import (  # noqa: E402
    HermesConflictError,
    HermesContextBudgetError,
    HermesProtocolError,
    HermesRunMappingStore,
    HermesRunsBridge,
    HermesUnavailableError,
    SSEEvent,
)


def connection_factory(path):
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


class StubClient:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []
        self.sse_calls = []

    def request_json(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.error:
            raise self.error
        return self.responses.pop(0)

    @contextmanager
    def open_sse(self, path, **kwargs):
        self.sse_calls.append((path, kwargs))
        yield iter([SSEEvent("delta", '{"text":"hello"}', "evt-1")])


def test_session_and_run_mappings_survive_store_recreation(tmp_path):
    factory = connection_factory(tmp_path / "mapping.db")
    first = HermesRunMappingStore(factory)
    session = first.get_or_create_session("session-1")
    reserved = first.reserve_run("run-1", "session-1")
    assert reserved.status == "creating"
    first.bind_run("run-1", "upstream-1", status="queued")

    second = HermesRunMappingStore(factory)
    assert second.get_or_create_session("session-1") == session
    assert second.get_session_by_hermes_id(session.hermes_session_id) == session
    restored = second.get_run("run-1")
    assert restored is not None
    assert restored.hermes_run_id == "upstream-1"
    assert second.get_run_by_hermes_id("upstream-1") == restored


def test_project_scope_change_rotates_transcript_and_memory_identifiers(tmp_path):
    factory = connection_factory(tmp_path / "mapping.db")
    store = HermesRunMappingStore(factory)
    first = store.get_or_create_session("session-1", workbench_scope="project-a")
    same = store.get_or_create_session("session-1", workbench_scope="project-a")
    moved = store.get_or_create_session("session-1", workbench_scope="project-b")

    assert same == first
    assert moved.workbench_scope == "project-b"
    assert moved.hermes_session_id != first.hermes_session_id
    assert moved.hermes_session_key != first.hermes_session_key


def test_workbench_session_delete_cleans_mapping_rows(tmp_path, monkeypatch):
    import database

    original_db = database.DB_PATH
    database.DB_PATH = str(tmp_path / "workbench.db")
    try:
        database.init_db()
        database.create_session("session-delete")
        store = HermesRunMappingStore(database.get_db_conn)
        store.get_or_create_session("session-delete", workbench_scope="unscoped")
        store.reserve_run("run-delete", "session-delete")
        store.bind_run("run-delete", "upstream-delete", status="queued")

        assert database.delete_session("session-delete") is True
        assert store.get_session("session-delete") is None
        assert store.get_run("run-delete") is None
    finally:
        database.DB_PATH = original_db


def test_mapping_prevents_cross_session_and_upstream_id_collisions(tmp_path):
    store = HermesRunMappingStore(connection_factory(tmp_path / "mapping.db"))
    store.reserve_run("same-run", "session-a")
    with pytest.raises(HermesConflictError):
        store.reserve_run("same-run", "session-b")
    store.bind_run("same-run", "same-upstream", status="queued")
    store.reserve_run("run-b", "session-b")
    with pytest.raises(HermesConflictError):
        store.bind_run("run-b", "same-upstream", status="queued")


def test_create_run_uses_opaque_session_mapping_and_is_locally_idempotent(tmp_path):
    store = HermesRunMappingStore(connection_factory(tmp_path / "mapping.db"))
    client = StubClient([{"id": "hermes-run-1", "status": "queued"}])
    bridge = HermesRunsBridge(client, store)

    created = bridge.create_run(
        "workbench-run-1",
        "workbench-session-1",
        "do the work",
        instructions="project-scoped instructions",
        history=[{"role": "user", "content": "earlier"}],
        session_scope="project-one",
    )
    repeated = bridge.create_run(
        "workbench-run-1", "workbench-session-1", "do the work"
    )

    assert created.hermes_run_id == "hermes-run-1"
    assert repeated.hermes_run_id == "hermes-run-1"
    assert len(client.calls) == 1
    _method, _path, kwargs = client.calls[0]
    assert kwargs["payload"]["session_id"].startswith("wb-session-")
    assert kwargs["payload"]["session_id"] != "workbench-session-1"
    assert kwargs["payload"]["conversation_history"] == [
        {"role": "user", "content": "earlier"}
    ]
    assert "history" not in kwargs["payload"]
    assert kwargs["headers"]["X-Hermes-Session-Key"].startswith("wb-memory-")
    assert kwargs["headers"]["Idempotency-Key"] == "workbench-run-1"
    assert store.get_session("workbench-session-1").workbench_scope == "project-one"


def test_submission_failure_is_not_automatically_resubmitted(tmp_path):
    store = HermesRunMappingStore(connection_factory(tmp_path / "mapping.db"))
    client = StubClient(error=HermesUnavailableError("offline"))
    bridge = HermesRunsBridge(client, store)

    with pytest.raises(HermesUnavailableError):
        bridge.create_run("run-unknown", "session-1", "input")
    assert store.get_run("run-unknown").status == "submission_unknown"

    with pytest.raises(HermesConflictError):
        bridge.create_run("run-unknown", "session-1", "input")
    assert len(client.calls) == 1


def test_status_stop_and_resumable_events_use_persisted_upstream_id(tmp_path):
    store = HermesRunMappingStore(connection_factory(tmp_path / "mapping.db"))
    store.get_or_create_session("session-1")
    store.reserve_run("run/with space", "session-1")
    store.bind_run("run/with space", "upstream/with space", status="running")
    client = StubClient(
        [
            {"id": "upstream/with space", "status": "completed"},
            {"id": "upstream/with space", "status": "stopped"},
        ]
    )
    bridge = HermesRunsBridge(client, store)

    assert bridge.status("run/with space").status == "completed"
    assert bridge.stop("run/with space").status == "stopped"
    events = list(bridge.events("run/with space", after_event_id="evt-0"))

    assert events[0].json() == {"text": "hello"}
    expected = "/v1/runs/upstream%2Fwith%20space"
    assert client.calls[0][1] == expected
    assert client.calls[1][1] == expected + "/stop"
    assert client.sse_calls == [
        (expected + "/events", {"headers": {"Last-Event-ID": "evt-0"}})
    ]


def test_approval_resolution_allows_only_once_or_deny(tmp_path):
    store = HermesRunMappingStore(connection_factory(tmp_path / "mapping.db"))
    store.reserve_run("run-approval", "session-1")
    store.bind_run("run-approval", "upstream-approval", status="waiting_for_approval")
    client = StubClient([{"choice": "once", "resolved": 1}])
    bridge = HermesRunsBridge(client, store)

    result = bridge.resolve_approval("run-approval", choice="once")
    assert result.status == "running"
    assert client.calls[0][1] == "/v1/runs/upstream-approval/approval"
    assert client.calls[0][2]["payload"] == {
        "choice": "once",
        "resolve_all": False,
    }
    with pytest.raises(ValueError):
        bridge.resolve_approval("run-approval", choice="always")


def test_missing_upstream_run_id_marks_mapping_as_protocol_error(tmp_path):
    store = HermesRunMappingStore(connection_factory(tmp_path / "mapping.db"))
    bridge = HermesRunsBridge(StubClient([{"status": "queued"}]), store)
    with pytest.raises(HermesProtocolError):
        bridge.create_run("run-1", "session-1", "input")
    assert store.get_run("run-1").status == "protocol_error"


def test_aggregate_context_guard_runs_before_http_or_mapping_writes(tmp_path):
    store = HermesRunMappingStore(connection_factory(tmp_path / "mapping.db"))
    client = StubClient([])
    bridge = HermesRunsBridge(client, store)

    with pytest.raises(HermesContextBudgetError):
        bridge.create_run(
            "oversized-run",
            "oversized-session",
            "x" * 300_000,
            instructions="each individual field is otherwise valid",
        )

    assert client.calls == []
    assert store.get_session("oversized-session") is None
    assert store.get_run("oversized-run") is None
