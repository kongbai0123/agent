from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import database
from api.routes.n8n_agent import build_n8n_agent_router
from n8n_agent_governance import N8nAgentGovernanceService
from n8n_gmail_crypto import AesGcmContentCipher


class Broker:
    def __init__(self): self._api_key_provider = lambda: "x" * 32
    def list_workflows(self): return []
    def execute(self, operation, payload, *, secret=None): return {"id": "workflow"}
    def security_audit(self): return {"ok": True}


class Secrets:
    def __init__(self): self.value = None
    def set_api_key(self, value): self.value = value


def test_governance_routes_are_project_scoped_and_secret_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "db.sqlite"))
    database.init_db()
    database.create_project("p1", "P1", str(tmp_path / "p1"))
    database.create_project("p2", "P2", str(tmp_path / "p2"))
    service = N8nAgentGovernanceService(
        broker=Broker(), cipher=AesGcmContentCipher(lambda: b"k" * 32),
        n8n_running=lambda: True,
        integration_permission_check=lambda *_args, **_kwargs: {"decision": "allow"},
        _allow_legacy_raw_workflows_for_tests=True,
    )
    secrets = Secrets()
    app = FastAPI()
    app.include_router(build_n8n_agent_router(
        service=service, secret_store=secrets, require_local=lambda _request: None,
        error_payload=lambda code, message, **kwargs: {"code": code, "message": message, **kwargs},
    ))
    client = TestClient(app)

    policy = client.get("/api/integrations/n8n/agent-policy", params={"project_id": "p1"})
    assert policy.status_code == 200
    assert policy.json()["mode"] == "restricted"
    elevated = client.put("/api/integrations/n8n/agent-policy", json={
        "project_id": "p1", "mode": "full_audit", "elevation_policy": "one_hour",
        "session_id": None, "explicit_ack": True,
    })
    assert elevated.status_code == 200

    created = client.post("/api/integrations/n8n/operation-requests", json={
        "project_id": "p1", "session_id": None, "run_id": None,
        "operation": "create_draft",
        "payload": {"workflow": {"name": "Safe", "nodes": []}},
        "diff": {"added": ["Safe"]}, "base_digest": None,
    })
    assert created.status_code == 202
    operation = created.json()
    hidden = client.get(f"/api/integrations/n8n/operation-requests/{operation['id']}", params={"project_id": "p2"})
    assert hidden.status_code == 404
    missing_scope = client.post(f"/api/integrations/n8n/operation-requests/{operation['id']}/approve", json={
        "project_id": "p1", "expected_digest": operation["digest"], "confirmation": None,
    })
    assert missing_scope.status_code == 422
    stale = client.post(f"/api/integrations/n8n/operation-requests/{operation['id']}/approve", json={
        "project_id": "p1", "session_id": None,
        "expected_digest": "0" * 64, "confirmation": None,
    })
    assert stale.status_code == 409

    saved = client.put("/api/integrations/n8n/agent-api-key", json={"api_key": "private-api-key-value"})
    assert saved.json() == {"configured": True}
    assert secrets.value == "private-api-key-value"
    assert "private-api-key-value" not in saved.text


def test_normal_operation_rejects_embedded_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "db.sqlite"))
    database.init_db(); database.create_project("p1", "P1", str(tmp_path / "p1"))
    service = N8nAgentGovernanceService(
        broker=Broker(), cipher=AesGcmContentCipher(lambda: b"k" * 32),
        n8n_running=lambda: True,
        integration_permission_check=lambda *_args, **_kwargs: {"decision": "allow"},
        _allow_legacy_raw_workflows_for_tests=True,
    )
    app = FastAPI(); app.include_router(build_n8n_agent_router(service=service, secret_store=Secrets(), require_local=lambda _r: None, error_payload=lambda code, message, **kwargs: {"code": code, "message": message}))
    response = TestClient(app).post("/api/integrations/n8n/operation-requests", json={
        "project_id": "p1", "operation": "create_draft", "payload": {"password": "bad"}, "diff": {},
    })
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "N8N_SECRET_IN_PROPOSAL"
