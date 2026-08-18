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


def app_for(
    lifecycle,
    *,
    workflow_status=None,
    require_extension=None,
    on_stop=None,
):
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
            require_extension=require_extension,
            on_stop=on_stop,
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


def test_status_remains_readable_without_invoking_extension_gate():
    lifecycle = FakeLifecycle()
    calls = []

    def gate(*args):
        calls.append(args)
        raise AssertionError("read-only status must not invoke the extension gate")

    with TestClient(app_for(lifecycle, require_extension=gate)) as client:
        response = client.get("/api/integrations/n8n/status")

    assert response.status_code == 200
    assert calls == []
    assert lifecycle.started == lifecycle.stopped == 0


def test_lifecycle_mutations_are_denied_before_side_effects():
    lifecycle = FakeLifecycle(state="ready")
    calls = []

    class Disabled(RuntimeError):
        code = "EXTENSION_DISABLED"

    def deny(extension_id, project_id):
        calls.append((extension_id, project_id))
        raise Disabled("n8n is disabled by Extension Center")

    with TestClient(app_for(lifecycle, require_extension=deny)) as client:
        for action in ("start", "stop", "restart"):
            response = client.post(f"/api/integrations/n8n/{action}")
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "EXTENSION_DISABLED"

    assert calls == [("builtin.n8n", None)] * 3
    assert lifecycle.started == lifecycle.stopped == 0


def test_restart_is_gated_once_and_runs_owned_stop_then_start():
    lifecycle = FakeLifecycle(state="ready")
    gate_calls = []
    stop_callbacks = []

    def allow(extension_id, project_id):
        gate_calls.append((extension_id, project_id))
        return {"effective_enabled": True}

    with TestClient(
        app_for(
            lifecycle,
            require_extension=allow,
            on_stop=lambda: stop_callbacks.append("stopped"),
        )
    ) as client:
        response = client.post("/api/integrations/n8n/restart")

    assert response.status_code == 200
    assert response.json()["state"] == "ready"
    assert gate_calls == [("builtin.n8n", None)]
    assert lifecycle.stopped == lifecycle.started == 1
    assert stop_callbacks == ["stopped"]
