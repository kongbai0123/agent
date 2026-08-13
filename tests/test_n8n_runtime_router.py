from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from api.routes.n8n_runtime import build_n8n_runtime_router  # noqa: E402
from n8n_lifecycle import N8nConfigurationError  # noqa: E402


class FakeLifecycle:
    def __init__(self, *, state="stopped", isolation=True):
        self.state = state
        self.isolation = isolation
        self.started = 0
        self.stopped = 0

    def status(self, *, probe_node=False):
        return {
            "state": self.state,
            "reason": "ready" if self.state == "ready" else "no_listener",
            "version": "2.32.5",
            "node_version": "24.15.0",
            "installation": {"valid": True},
            "isolation_ready": self.isolation,
            "isolation_blockers": [] if self.isolation else ["service_account_missing"],
            "checked_at": "2026-08-13T00:00:00+00:00",
        }

    def start(self):
        if not self.isolation:
            raise N8nConfigurationError(
                "not ready", details={"blockers": ["service_account_missing"]}
            )
        self.started += 1
        self.state = "ready"

    def stop(self):
        self.stopped += 1
        self.state = "stopped"


def app_for(lifecycle, *, workflow_status=None):
    app = FastAPI()
    app.include_router(
        build_n8n_runtime_router(
            lifecycle=lifecycle,
            require_local=lambda _request: None,
            error_payload=lambda code, message, detail=None, recoverable=True: {
                "code": code,
                "message": message,
                "detail": detail,
                "recoverable": recoverable,
            },
            workflow_ready=lambda: True,
            workflow_status=workflow_status,
        )
    )
    return app


def test_status_and_owned_start_stop_projection():
    lifecycle = FakeLifecycle()
    with TestClient(app_for(lifecycle)) as client:
        status = client.get("/api/integrations/n8n/status").json()
        assert status["editor_url"] is None
        assert status["installed"] is True
        started = client.post("/api/integrations/n8n/start").json()
        assert started["editor_url"] == "http://127.0.0.1:5678/"
        assert started["workflow_ready"] is True
        stopped = client.post("/api/integrations/n8n/stop").json()
        assert stopped["running"] is False
    assert lifecycle.started == lifecycle.stopped == 1


def test_status_reports_gmail_oauth_readiness_separately():
    lifecycle = FakeLifecycle(state="ready")
    app = app_for(
        lifecycle,
        workflow_status=lambda: {
            "ready": False,
            "credentials": {"gmail_oauth_bound": True},
        },
    )
    with TestClient(app) as client:
        status = client.get("/api/integrations/n8n/status").json()
    assert status["gmail_oauth_ready"] is True
    assert status["workflow_ready"] is False


def test_start_fails_closed_when_isolation_is_missing():
    lifecycle = FakeLifecycle(isolation=False)
    with TestClient(app_for(lifecycle)) as client:
        response = client.post("/api/integrations/n8n/start")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "N8N_CONFIGURATION_INVALID"
    assert response.json()["detail"]["detail"] == "service_account_missing"
    assert lifecycle.started == 0
