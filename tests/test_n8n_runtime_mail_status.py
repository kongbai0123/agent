from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from api.routes.n8n_runtime import build_n8n_runtime_router


class _Lifecycle:
    def status(self, *, probe_node=False):
        return {
            "state": "ready", "reason": "ready", "installation": {"valid": True},
            "version": "2.32.5", "node_version": "22.22.0",
            "isolation_ready": True, "isolation_blockers": [],
            "checked_at": "2026-08-13T00:00:00Z",
        }


def test_runtime_status_whitelists_content_free_mail_snapshot():
    app = FastAPI()
    app.include_router(build_n8n_runtime_router(
        lifecycle=_Lifecycle(),
        require_local=lambda request: None,
        error_payload=lambda *args, **kwargs: {},
        workflow_ready=lambda: True,
        mail_status=lambda: {
            "type": "mail_runs_changed", "pending_approvals": 2,
            "counts": {"awaiting_approval": 2, "failed": 1},
            "latest_updated_at": "2026-08-13T01:00:00Z", "fingerprint": "a" * 64,
            "body_text": "MUST_NOT_LEAK", "instruction": "MUST_NOT_LEAK",
        },
    ))
    payload = TestClient(app).get("/api/integrations/n8n/status").json()

    assert payload["mail"] == {
        "type": "mail_runs_changed", "pending_approvals": 2,
        "counts": {"awaiting_approval": 2, "failed": 1},
        "latest_updated_at": "2026-08-13T01:00:00Z", "revision": "a" * 64,
    }
    assert "MUST_NOT_LEAK" not in str(payload)
