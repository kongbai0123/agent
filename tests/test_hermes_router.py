from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from api.routes.hermes import build_hermes_router  # noqa: E402


class FakeRecord:
    def __init__(self, approval_id="approval-1", run_id="run-1"):
        self.approval_id = approval_id
        self.workbench_run_id = run_id

    def public_dict(self):
        return {
            "approval_id": self.approval_id,
            "run_id": self.workbench_run_id,
            "status": "pending",
        }


class FakeStore:
    def __init__(self):
        self.record = FakeRecord()

    def list_pending(self, **_kwargs):
        return [self.record]

    def get(self, approval_id):
        return self.record if approval_id == self.record.approval_id else None


class FakeManager:
    def __init__(self):
        self.approval_store = FakeStore()
        self.decisions = []
        self.cancelled = []

    def status(self):
        return {
            "enabled": True,
            "health": {"status": "healthy"},
            "rollout": {"mode": "all"},
            "base_url": "http://127.0.0.1:8642",
            "api_key_configured": True,
            "tools_enabled": True,
        }

    def probe(self):
        return {"success": True, "health": {"status": "healthy"}}

    def resolve_approval(self, approval_id, *, choice, rationale):
        self.decisions.append((approval_id, choice, rationale))
        return self.approval_store.record

    def run_status(self, run_id):
        return {"run_id": run_id, "status": "running"}

    def cancel(self, run_id):
        self.cancelled.append(run_id)
        return {"run_id": run_id, "status": "stopping", "cancelled": True}


def error_payload(code, message, **kwargs):
    return {"code": code, "message": message, **kwargs}


def test_router_exposes_redacted_status_probe_and_frontend_approval_contract():
    manager = FakeManager()
    local_calls = []
    app = FastAPI()
    app.include_router(
        build_hermes_router(
            manager=manager,
            require_local=lambda request: local_calls.append(request.url.path),
            error_payload=error_payload,
            cancel_local_run=lambda run_id: {"run_id": run_id, "cancelled": True},
        )
    )
    with TestClient(app) as client:
        status = client.get("/api/hermes/status")
        probe = client.post("/api/hermes/probe")
        pending = client.get("/api/hermes/approvals")
        approval = client.post(
            "/api/chat/runs/run-1/approval",
            json={
                "approval_id": "approval-1",
                "approved": True,
                "decided_by": "local_user",
            },
        )
        cancelled = client.post("/api/hermes/runs/run-1/cancel")

    assert status.status_code == 200
    assert status.json()["api_key_configured"] is True
    assert "Bearer " not in status.text
    assert "0123456789abcdef" not in status.text
    assert probe.json()["success"] is True
    assert pending.json()["approvals"][0]["approval_id"] == "approval-1"
    assert approval.status_code == 200
    assert manager.decisions[0][:2] == ("approval-1", "once")
    assert cancelled.json()["status"] == "stopping"
    assert cancelled.json()["upstream"] == "delegated_to_chat_control"
    assert manager.cancelled == []
    assert set(local_calls) == {
        "/api/hermes/probe",
        "/api/chat/runs/run-1/approval",
        "/api/hermes/runs/run-1/cancel",
    }


def test_approval_cannot_cross_workbench_runs():
    manager = FakeManager()
    app = FastAPI()
    app.include_router(
        build_hermes_router(
            manager=manager,
            require_local=lambda _request: None,
            error_payload=error_payload,
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/chat/runs/another-run/approval",
            json={"approval_id": "approval-1", "approved": False},
        )
    assert response.status_code == 409
    assert manager.decisions == []


def test_hermes_cancel_calls_upstream_once_when_no_chat_control_exists():
    manager = FakeManager()
    app = FastAPI()
    app.include_router(
        build_hermes_router(
            manager=manager,
            require_local=lambda _request: None,
            error_payload=error_payload,
            cancel_local_run=lambda _run_id: None,
        )
    )

    with TestClient(app) as client:
        response = client.post("/api/hermes/runs/run-1/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "stopping"
    assert manager.cancelled == ["run-1"]


def test_emergency_rollout_rollback_is_local_and_preserves_runtime_data():
    manager = FakeManager()
    local_calls = []
    rollback_calls = []
    app = FastAPI()
    app.include_router(
        build_hermes_router(
            manager=manager,
            require_local=lambda request: local_calls.append(request.url.path),
            error_payload=error_payload,
            rollback_handler=lambda: rollback_calls.append(True) or {
                "rolled_back": True,
                "rollout": {"mode": "disabled", "percentage": 0.0},
                "tools_enabled": False,
                "preserved_runtime_data": True,
            },
        )
    )

    with TestClient(app) as client:
        response = client.post("/api/hermes/rollout/rollback")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "rolled_back": True,
        "rollout": {"mode": "disabled", "percentage": 0.0},
        "tools_enabled": False,
        "preserved_runtime_data": True,
    }
    assert rollback_calls == [True]
    assert local_calls == ["/api/hermes/rollout/rollback"]


def test_emergency_rollback_fails_closed_when_handler_is_not_wired():
    app = FastAPI()
    app.include_router(
        build_hermes_router(
            manager=FakeManager(),
            require_local=lambda _request: None,
            error_payload=error_payload,
        )
    )

    with TestClient(app) as client:
        response = client.post("/api/hermes/rollout/rollback")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "HERMES_ROLLBACK_UNAVAILABLE"
