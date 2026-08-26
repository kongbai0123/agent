from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import database
from operations_core import OperationsCore


def _core(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "operations.db"))
    database.init_db()
    core = OperationsCore(database_module=database)
    core.initialize()
    return core


def test_execution_policy_artifact_and_health_contracts(tmp_path, monkeypatch):
    core = _core(tmp_path, monkeypatch)
    execution = core.create_execution(
        execution_id="exec_test",
        kind="model.install",
        owner_type="model",
        owner_id="example/model:latest",
        project_id="project_1",
        metadata={"api_key": "must-not-leak", "source": "test"},
    )
    assert execution["status"] == "queued"
    assert "must-not-leak" not in str(execution)

    updated = core.update_execution("exec_test", status="running", progress=50, expected_revision=1)
    assert updated["revision"] == 2
    assert core.list_events("exec_test")[-1]["payload"]["progress"] == 50

    decision = core.record_policy_decision(
        execution_id="exec_test",
        project_id="project_1",
        policy_id="fixed.local_training",
        subject_type="adapter",
        subject_id="local.adapter",
        action="allow",
        reason_code="bounded_local_adapter",
        inputs={"token": "must-not-leak"},
    )
    assert decision["action"] == "allow"
    assert "must-not-leak" not in str(core.list_policy_decisions(project_id="project_1"))

    artifact = core.register_artifact(
        execution_id="exec_test",
        project_id="project_1",
        artifact_kind="trained_model",
        display_name="測試模型",
        locator={"scheme": "local_mlops", "id": "model_1"},
    )
    assert artifact["reference_id"].startswith("artifact_ref_")
    assert core.list_artifacts(project_id="project_1")[0]["locator"]["scheme"] == "local_mlops"

    health = core.report_health(
        component_type="mcp", component_id="mcp.browser", status="degraded",
        reason_code="process_unavailable", detail={"secret": "must-not-leak"},
    )
    assert health["consecutive_failures"] == 1
    recovered = core.report_health(
        component_type="mcp", component_id="mcp.browser", status="healthy",
        reason_code="probe_succeeded",
    )
    assert recovered["consecutive_failures"] == 0
    assert recovered["last_success_at"]


def test_revision_conflict_is_fail_closed(tmp_path, monkeypatch):
    core = _core(tmp_path, monkeypatch)
    core.create_execution(execution_id="exec_revision", kind="test.run", owner_type="test", owner_id="one")
    try:
        core.update_execution("exec_revision", status="running", expected_revision=9)
    except RuntimeError as exc:
        assert str(exc) == "OPERATION_REVISION_CONFLICT"
    else:
        raise AssertionError("stale update must be rejected")
