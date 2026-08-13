from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import database  # noqa: E402
from api.routes.n8n_agent import build_n8n_agent_router  # noqa: E402
from n8n_agent_planner import N8nPlanModelGenerator, N8nPlannerError, N8nPlanningService  # noqa: E402


APP_SOURCE = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")


def generated(operation="create_draft"):
    def choice(choice_id, name, recommended=False):
        return {
            "id": choice_id,
            "label": name,
            "description": f"Prepare {name} for review.",
            "operation": operation,
            "payload": {"workflow": {"name": name, "nodes": [{"type": "n8n-nodes-base.set"}]}},
            "diff": {"added": [name]},
            "expected_result": f"A reviewable {name} workflow proposal.",
            "risks": ["It may write to n8n after approval."],
            "permissions": ["Full management and separate approval are required."],
            "recommended": recommended,
        }
    return {
        "assistant_message": "I can prepare this safely. Nothing has been changed yet.",
        "risk_summary": ["No change occurs during planning."],
        "expected_result": "One selected option becomes an immutable approval request.",
        "permission_requirements": ["The Agent cannot elevate its own permission."],
        "choices": [choice("minimal", "Minimal", True), choice("observable", "Observable")],
    }


class Governance:
    def __init__(self, mode="full_audit", planned=False, *, api_key_configured=True, runtime_ready=True):
        self.mode = mode
        self.api_key_configured = api_key_configured
        self.runtime_ready = runtime_ready
        self.calls = []
        if planned:
            self.create_planned_operation = self._create_planned

    def get_policy(self, project_id, session_id=None):
        return {
            "project_id": project_id, "mode": self.mode, "session_id": session_id,
            "api_key_configured": self.api_key_configured,
            "runtime_ready": self.runtime_ready,
        }

    def create_operation(self, proposal):
        self.calls.append(copy.deepcopy(proposal))
        return {"id": "operation-1", "status": "pending", "digest": "a" * 64}

    def _create_planned(self, proposal):
        self.calls.append(copy.deepcopy(proposal))
        return {"id": "operation-planned", "status": "pending", "digest": "b" * 64}


@pytest.fixture()
def scope(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "workbench.db"))
    database.init_db()
    database.create_project("project-a", "A", str(tmp_path / "a"))
    database.create_project("project-b", "B", str(tmp_path / "b"))
    database.create_session("session-a", project_id="project-a")
    database.create_session("session-b", project_id="project-b")


def test_plan_is_scoped_and_returns_clear_choices_without_payload(scope):
    governance = Governance()
    planner = N8nPlanningService(governance_service=governance, generator=lambda _context: generated())
    plan = planner.start(project_id="project-a", session_id="session-a", message="Build a webhook workflow")

    assert plan["status"] == "planning"
    assert len(plan["choices"]) == 2
    assert plan["risk_summary"] and plan["expected_result"] and plan["permission_requirements"]
    assert "尚未變更 n8n" in plan["assistant_message"]
    assert any("人工核准" in item for item in plan["risk_summary"])
    assert any("Agent 無法自行提升權限" in item for item in plan["permission_requirements"])
    assert all("payload" not in choice for choice in plan["choices"])
    assert plan["choices"][0]["proposal_intent"] == {"added": ["Minimal"]}
    assert plan["choices"][0]["diff"]["source"] == "server"
    assert plan["choices"][0]["diff"] != plan["choices"][0]["proposal_intent"]
    assert governance.calls == []
    with pytest.raises(N8nPlannerError) as mismatch:
        planner.add_message(
            plan["id"], project_id="project-b", session_id="session-a",
            message="continue", expected_digest=plan["digest"],
        )
    assert mismatch.value.code == "N8N_PLAN_SCOPE_MISMATCH"


def test_workflow_inventory_preserves_session_elevation_scope(scope):
    calls = []

    def inventory(project_id, *, session_id):
        calls.append((project_id, session_id))
        return {"workflows": []}

    planner = N8nPlanningService(
        governance_service=Governance(),
        generator=lambda _context: generated(),
        workflow_summary_provider=inventory,
    )
    planner.start(project_id="project-a", session_id="session-a", message="Build a workflow")

    assert calls == [("project-a", "session-a")]


@pytest.mark.parametrize("session_id", ["session-archived", "session-email"])
def test_planner_rejects_archived_and_integration_only_sessions(scope, session_id):
    database.create_session(
        session_id,
        project_id="project-a",
        mode="email" if session_id.endswith("email") else "chat",
    )
    if session_id.endswith("archived"):
        with database.get_db_conn() as conn:
            conn.execute("UPDATE sessions SET archived=1 WHERE id=?", (session_id,))
    planner = N8nPlanningService(
        governance_service=Governance(), generator=lambda _context: generated(),
    )

    with pytest.raises(N8nPlannerError) as rejected:
        planner.start(project_id="project-a", session_id=session_id, message="Build a workflow")

    assert rejected.value.code == "N8N_PLAN_SCOPE_MISMATCH"


def test_selection_then_digest_confirmation_creates_server_snapshot_only(scope):
    governance = Governance()
    planner = N8nPlanningService(governance_service=governance, generator=lambda _context: generated())
    plan = planner.start(project_id="project-a", session_id="session-a", message="Build a workflow")
    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="Use the minimal choice", selected_option_id="minimal",
        expected_digest=plan["digest"],
    )
    assert selected["status"] == "ready"
    assert "你已選擇" in selected["assistant_message"]
    assert governance.calls == []

    with pytest.raises(N8nPlannerError) as stale:
        planner.propose(
            plan["id"], project_id="project-a", session_id="session-a",
            expected_digest=plan["digest"], explicit_confirmation=True,
        )
    assert stale.value.code == "N8N_PLAN_STALE"

    result = planner.propose(
        plan["id"], project_id="project-a", session_id="session-a",
        expected_digest=selected["digest"], explicit_confirmation=True,
    )
    assert result["operation"]["status"] == "pending"
    assert governance.calls == [{
        "project_id": "project-a", "session_id": "session-a", "run_id": None,
        "operation": "create_draft",
        "payload": {"workflow": {"name": "Minimal", "nodes": [{"type": "n8n-nodes-base.set"}]}},
        "diff": {
            "schema": "workbench.n8n.operation-diff.v1", "source": "server",
            "operation": "create_draft", "effect": "create_workflow_draft",
            "target": {"workflow_id": None, "workflow_name": "Minimal"},
            "after": {"node_count": 1, "node_types": ["n8n-nodes-base.set"]},
            "reversible": True,
        },
        "base_digest": selected["digest"],
    }]


def test_restricted_mode_fails_closed_without_pending_governance_api(scope):
    governance = Governance(mode="restricted")
    planner = N8nPlanningService(governance_service=governance, generator=lambda _context: generated())
    plan = planner.start(project_id="project-a", session_id="session-a", message="Build a workflow")
    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="Select minimal", selected_option_id="minimal", expected_digest=plan["digest"],
    )
    with pytest.raises(N8nPlannerError) as blocked:
        planner.propose(
            plan["id"], project_id="project-a", session_id="session-a",
            expected_digest=selected["digest"], explicit_confirmation=True,
        )
    assert blocked.value.code == "N8N_PLAN_REVIEW_MODE_REQUIRED"
    assert governance.calls == []


def test_pending_capable_governance_allows_restricted_proposal(scope):
    governance = Governance(mode="restricted", planned=True)
    planner = N8nPlanningService(governance_service=governance, generator=lambda _context: generated())
    plan = planner.start(project_id="project-a", session_id="session-a", message="Build a workflow")
    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="Select minimal", selected_option_id="minimal", expected_digest=plan["digest"],
    )
    result = planner.propose(
        plan["id"], project_id="project-a", session_id="session-a",
        expected_digest=selected["digest"], explicit_confirmation=True,
    )
    assert result["operation"]["id"] == "operation-planned"


def test_secrets_and_client_option_forgery_are_rejected(scope):
    governance = Governance()
    planner = N8nPlanningService(governance_service=governance, generator=lambda _context: generated())
    with pytest.raises(N8nPlannerError) as secret:
        planner.start(project_id="project-a", session_id="session-a", message="api_key: private-value")
    assert secret.value.code == "N8N_PLAN_SECRET_REJECTED"

    plan = planner.start(project_id="project-a", session_id="session-a", message="Build a workflow")
    with pytest.raises(N8nPlannerError) as forged:
        planner.add_message(
            plan["id"], project_id="project-a", session_id="session-a",
            message="select", selected_option_id="attacker-choice", expected_digest=plan["digest"],
        )
    assert forged.value.code == "N8N_PLAN_OPTION_INVALID"
    assert governance.calls == []


def test_message_requires_latest_digest_and_unsupported_operations_fail(scope):
    governance = Governance()
    planner = N8nPlanningService(governance_service=governance, generator=lambda _context: generated())
    plan = planner.start(project_id="project-a", session_id="session-a", message="Build a workflow")
    with pytest.raises(N8nPlannerError) as stale:
        planner.add_message(
            plan["id"], project_id="project-a", session_id="session-a",
            message="continue", expected_digest="0" * 64,
        )
    assert stale.value.code == "N8N_PLAN_STALE"

    unsupported = N8nPlanningService(
        governance_service=governance, generator=lambda _context: generated("execute"),
    )
    with pytest.raises(N8nPlannerError) as invalid:
        unsupported.start(project_id="project-a", session_id="session-a", message="Run it")
    assert invalid.value.code == "N8N_PLAN_INVALID"


def test_broker_readiness_allows_planning_but_blocks_selection_and_proposal(scope):
    governance = Governance(api_key_configured=False, runtime_ready=False)
    planner = N8nPlanningService(governance_service=governance, generator=lambda _context: generated())
    plan = planner.start(project_id="project-a", session_id="session-a", message="先規劃一個流程")
    assert {item["code"] for item in plan["blockers"]} == {
        "N8N_API_KEY_NOT_CONFIGURED", "N8N_RUNTIME_NOT_READY",
    }
    assert any("API 金鑰" in item for item in plan["risk_summary"])
    assert any("不要把金鑰貼到對話" in item for item in plan["permission_requirements"])

    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="選擇 minimal", selected_option_id="minimal", expected_digest=plan["digest"],
    )
    assert selected["status"] == "blocked"
    assert selected["blockers"]
    with pytest.raises(N8nPlannerError) as unavailable:
        planner.propose(
            plan["id"], project_id="project-a", session_id="session-a",
            expected_digest=selected["digest"], explicit_confirmation=True,
        )
    assert unavailable.value.code == "N8N_PLAN_BROKER_NOT_READY"
    assert governance.calls == []


def test_propose_rechecks_live_broker_readiness(scope):
    governance = Governance()
    planner = N8nPlanningService(governance_service=governance, generator=lambda _context: generated())
    plan = planner.start(project_id="project-a", session_id="session-a", message="建立流程")
    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="選擇 minimal", selected_option_id="minimal", expected_digest=plan["digest"],
    )
    assert selected["status"] == "ready"
    governance.runtime_ready = False
    with pytest.raises(N8nPlannerError) as unavailable:
        planner.propose(
            plan["id"], project_id="project-a", session_id="session-a",
            expected_digest=selected["digest"], explicit_confirmation=True,
        )
    assert unavailable.value.code == "N8N_PLAN_BROKER_NOT_READY"
    assert governance.calls == []


def test_planner_routes_match_frontend_contract(scope):
    governance = Governance()
    planner = N8nPlanningService(governance_service=governance, generator=lambda _context: generated())
    app = FastAPI()
    app.include_router(build_n8n_agent_router(
        service=governance, secret_store=object(), planner=planner,
        require_local=lambda _request: None,
        error_payload=lambda code, message, **kwargs: {"code": code, "message": message, **kwargs},
    ))
    client = TestClient(app)
    started = client.post("/api/integrations/n8n/plans", json={
        "project_id": "project-a", "session_id": "session-a", "message": "Build a workflow",
    })
    assert started.status_code == 201
    plan = started.json()
    missing_digest = client.post(f"/api/integrations/n8n/plans/{plan['id']}/messages", json={
        "project_id": "project-a", "session_id": "session-a", "message": "Select minimal",
        "selected_option_id": "minimal",
    })
    assert missing_digest.status_code == 422
    selected = client.post(f"/api/integrations/n8n/plans/{plan['id']}/messages", json={
        "project_id": "project-a", "session_id": "session-a", "message": "Select minimal",
        "selected_option_id": "minimal", "expected_digest": plan["digest"],
    })
    assert selected.status_code == 200
    proposed = client.post(f"/api/integrations/n8n/plans/{plan['id']}/propose", json={
        "project_id": "project-a", "session_id": "session-a",
        "expected_digest": selected.json()["digest"], "explicit_confirmation": True,
    })
    assert proposed.status_code == 202
    assert proposed.json()["operation"]["status"] == "pending"
    assert "private-value" not in json.dumps(proposed.json())


def test_model_generator_is_tool_free_and_accepts_secret_status_metadata():
    captured = {}

    class Response:
        status_code = 200

        def __init__(self):
            self.closed = False

        def json(self):
            return {"message": {"content": json.dumps(generated())}}

        def close(self):
            self.closed = True

    response = Response()

    def post(_settings, payload, **kwargs):
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return response

    generator = N8nPlanModelGenerator(
        lambda: {"default_chat_model": "model-a"}, post_chat=post,
    )
    result = generator({
        "project_id": "project-a", "session_id": "session-a",
        "policy": {"mode": "restricted", "api_key_configured": False},
        "workflow_inventory": {"status": "ready", "workflows": []},
        "conversation": [{"role": "user", "content": "Build a workflow"}],
    })
    assert len(result["choices"]) == 2
    assert "tools" not in captured["payload"]
    assert "user's primary language" in captured["payload"]["messages"][0]["content"]
    assert captured["kwargs"]["project_id"] == "project-a"
    assert response.closed is True


def test_workbench_composes_planner_with_governance_and_router():
    assert "from n8n_agent_planner import N8nPlanModelGenerator, N8nPlanningService" in APP_SOURCE
    assert "n8n_agent_planner = N8nPlanningService(" in APP_SOURCE
    assert "governance_service=n8n_agent_governance" in APP_SOURCE
    assert "generator=N8nPlanModelGenerator(settings_loader=load_settings)" in APP_SOURCE
    assert "planner=n8n_agent_planner" in APP_SOURCE
