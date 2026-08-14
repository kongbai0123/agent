from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from local_session import install_local_session_guard  # noqa: E402


def error_payload(code, message, **_kwargs):
    return {"success": False, "code": code, "message": message}


def test_n8n_gmail_v1_prefix_uses_route_auth_not_browser_session_token():
    app = FastAPI()
    install_local_session_guard(app, error_payload)

    @app.post("/api/integrations/n8n/v1/gmail/probe")
    def probe(x_integration_signature: str = Header(default="")):
        if x_integration_signature != "valid-route-proof":
            raise HTTPException(status_code=401, detail="integration auth required")
        return {"success": True}

    with TestClient(app) as client:
        denied = client.post("/api/integrations/n8n/v1/gmail/probe")
        allowed = client.post(
            "/api/integrations/n8n/v1/gmail/probe",
            headers={"X-Integration-Signature": "valid-route-proof"},
        )

    assert denied.status_code == 401
    assert allowed.status_code == 200


def test_n8n_agent_v1_prefix_uses_route_auth_not_browser_session_token():
    app = FastAPI()
    install_local_session_guard(app, error_payload)

    @app.post("/api/integrations/n8n/v1/agent/tasks")
    def probe(x_n8n_signature: str = Header(default="")):
        if x_n8n_signature != "valid-route-proof":
            raise HTTPException(status_code=401, detail="integration auth required")
        return {"success": True}

    with TestClient(app) as client:
        denied = client.post("/api/integrations/n8n/v1/agent/tasks")
        allowed = client.post(
            "/api/integrations/n8n/v1/agent/tasks",
            headers={"X-N8n-Signature": "valid-route-proof"},
        )

    assert denied.status_code == 401
    assert allowed.status_code == 200


def test_other_n8n_v1_paths_do_not_bypass_browser_auth():
    app = FastAPI()
    install_local_session_guard(app, error_payload)

    @app.post("/api/integrations/n8n/v1/not-gmail")
    def probe():
        return {"success": True}

    with TestClient(app) as client:
        response = client.post("/api/integrations/n8n/v1/not-gmail")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"


def test_browser_n8n_routes_are_not_in_the_service_bypass():
    app = FastAPI()
    install_local_session_guard(app, error_payload)

    @app.get("/api/integrations/n8n/status")
    def status():
        return {"success": True}

    with TestClient(app) as client:
        response = client.get("/api/integrations/n8n/status")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"
