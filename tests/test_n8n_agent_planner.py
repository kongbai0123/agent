from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import database  # noqa: E402
from api.routes.n8n_agent import build_n8n_agent_router  # noqa: E402
from n8n_agent_planner import (  # noqa: E402
    N8nPlanModelGenerator,
    N8nPlannerError,
    N8nPlanningService,
    _enforce_catalog_choices,
)


APP_SOURCE = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")


def generated(operation="create_draft"):
    def choice(name, recommended=False):
        return {
            "label": name,
            "description": f"Prepare {name} for review.",
            "operation": operation,
            "workflow_id": None if operation == "create_draft" else "workflow-1",
            "workflow_name": None if operation == "create_draft" else name,
            "architecture": {
                "schema": "workbench.n8n.architecture.v1",
                "goal": f"Prepare {name}",
                "steps": [{"key": "edit", "capability": "Edit Fields", "purpose": f"Create {name}"}],
                "edges": [], "required_inputs": [], "assumptions": [f"Use {name}"],
            },
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
        "choices": [choice("Minimal", True), choice("Observable")],
    }


def semantic(*, complete=True, name="Minimal"):
    parameters = {
        "instruction": "Summarize the untrusted input.",
        "model": "model-authored-by-planner",
        "output_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    } if complete else {}
    return {"semantic": {"workflow_spec": {
        "schema": "workflow_spec.v1", "name": name,
        "nodes": [{
                "key": "agent",
                "type": "workbench.agent",
                "name": "Workbench Agent",
                "parameters": copy.deepcopy(parameters),
        }], "edges": [],
    }}}


def two_stage_generator(*, operation="create_draft", complete=True):
    def call(context):
        if context.get("phase") in {"materialize", "materialize_repair"}:
            if operation == "create_draft":
                return semantic(complete=complete)
            return {"semantic": {"workflow_id": "workflow-1"}}
        return generated(operation)
    return call


class Governance:
    def __init__(self, mode="full_audit", planned=False, *, api_key_configured=True, runtime_ready=True):
        self.mode = mode
        self.api_key_configured = api_key_configured
        self.runtime_ready = runtime_ready
        self.calls = []
        self.materialize_calls = []
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

    def materialize_planned_choice(self, **value):
        self.materialize_calls.append(copy.deepcopy(value))
        spec = value["semantic"]["workflow_spec"]
        workflow = {
            "name": spec["name"],
            "nodes": [{
                "id": "node-1", "name": "Edit", "type": "n8n-nodes-base.set",
                "typeVersion": 1, "position": [240, 180], "parameters": {},
            }],
            "connections": {}, "settings": {},
        }
        return {
            "status": "graph_ready", "workflow": workflow,
            "graph_preview": {"name": spec["name"], "node_count": 1, "edge_count": 0},
            "validation_status": "ready", "catalog_digest": "c" * 64,
            "graph_digest": "d" * 64, "base_digest": "e" * 64,
            "issues": [], "questions": [], "diff": {"nodes": {}, "connections": {}},
        }


def materialize(planner, selected):
    return planner.materialize(
        selected["id"], project_id="project-a", session_id="session-a",
        expected_digest=selected["digest"],
    )


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
    planner = N8nPlanningService(governance_service=governance, generator=two_stage_generator())
    plan = planner.start(project_id="project-a", session_id="session-a", message="Build a webhook workflow")

    assert plan["status"] == "architecture_ready"
    assert plan["plan_schema"] == "workbench.n8n.two-stage.v1"
    assert len(plan["choices"]) == 2
    assert plan["risk_summary"] and plan["expected_result"] and plan["permission_requirements"]
    assert "尚未變更 n8n" in plan["assistant_message"]
    assert any("人工核准" in item for item in plan["risk_summary"])
    assert any("Agent 無法自行提升權限" in item for item in plan["permission_requirements"])
    assert all("payload" not in choice for choice in plan["choices"])
    assert plan["choices"][0]["architecture"]["steps"][0]["capability"] == "Edit Fields"
    assert "semantic" not in plan["choices"][0]
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
        generator=two_stage_generator(),
        workflow_summary_provider=inventory,
    )
    planner.start(project_id="project-a", session_id="session-a", message="Build a workflow")

    assert calls == [("project-a", "session-a")]


def test_agent_semantic_uses_server_model_and_active_skill_snapshots(scope):
    governance = Governance()

    def context(project_id, *, session_id):
        assert (project_id, session_id) == ("project-a", "session-a")
        return {
            "default_model": "server-approved-model",
            "credential_aliases": [{
                "alias": "gmail-primary", "credential_type": "gmailOAuth2",
                "status": "ready", "credential_id": "must-not-be-forwarded",
            }],
            "project_skills": [
                {
                    "slug": "mail-style", "name": "Mail style", "description": "Tone",
                    "version": "1.0.0", "sha256": "a" * 64, "active": True,
                    "instructions": "must-not-be-forwarded",
                },
                {
                    "slug": "disabled", "name": "Disabled", "description": "",
                    "version": "1.0.0", "sha256": "b" * 64, "active": False,
                },
            ],
        }

    planner = N8nPlanningService(
        governance_service=governance,
        generator=two_stage_generator(),
        planning_context_provider=context,
        protected_workflow_guard=lambda: {"ready": True},
    )
    safe_context = planner._planning_context("project-a", "session-a")
    assert safe_context["credential_aliases"] == [{
        "alias": "gmail-primary", "credential_type": "gmailOAuth2", "status": "ready",
    }]
    assert [item["slug"] for item in safe_context["project_skills"]] == ["mail-style"]
    assert "must-not-be-forwarded" not in json.dumps(safe_context)
    plan = planner.start(
        project_id="project-a", session_id="session-a", message="Use an Agent"
    )
    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="Select minimal", selected_option_id=plan["choices"][0]["id"],
        expected_digest=plan["digest"],
    )
    compiled = materialize(planner, selected)

    assert compiled["status"] == "graph_ready"
    semantic = governance.materialize_calls[0]["semantic"]
    parameters = semantic["workflow_spec"]["nodes"][0]["parameters"]
    assert parameters["model"] == "server-approved-model"
    assert parameters["skills"] == [{"slug": "mail-style", "sha256": "a" * 64}]
    assert "credential_id" not in json.dumps(semantic)
    assert "instructions" not in json.dumps(semantic)


def test_incomplete_agent_semantic_becomes_needs_input_without_runtime_call(scope):
    governance = Governance()
    planner = N8nPlanningService(
        governance_service=governance,
        generator=two_stage_generator(complete=False),
        planning_context_provider=lambda *_args, **_kwargs: {
            "default_model": "server-approved-model",
            "credential_aliases": [], "project_skills": [],
        },
        protected_workflow_guard=lambda: {"ready": True},
    )
    plan = planner.start(
        project_id="project-a", session_id="session-a", message="Use an Agent"
    )
    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="Select minimal", selected_option_id=plan["choices"][0]["id"],
        expected_digest=plan["digest"],
    )
    result = materialize(planner, selected)

    assert result["status"] == "needs_input"
    assert result["validation_status"] == "needs_input"
    assert result["materialization"]["questions"]
    assert governance.materialize_calls == []


def test_bridge_attestation_is_rechecked_for_materialize_and_propose(scope):
    governance = Governance()
    attestation = {"ready": False}
    planner = N8nPlanningService(
        governance_service=governance,
        generator=two_stage_generator(),
        protected_workflow_guard=lambda: dict(attestation),
    )
    plan = planner.start(
        project_id="project-a", session_id="session-a", message="Build a workflow"
    )
    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="Select minimal", selected_option_id=plan["choices"][0]["id"],
        expected_digest=plan["digest"],
    )
    with pytest.raises(N8nPlannerError) as unavailable:
        materialize(planner, selected)
    assert unavailable.value.code == "N8N_AGENT_BRIDGE_NOT_READY"
    assert governance.materialize_calls == []

    attestation["ready"] = True
    compiled = materialize(planner, selected)
    attestation["ready"] = False
    with pytest.raises(N8nPlannerError) as drifted:
        planner.propose(
            plan["id"], project_id="project-a", session_id="session-a",
            expected_digest=compiled["digest"], explicit_confirmation=True,
        )
    assert drifted.value.code == "N8N_AGENT_BRIDGE_NOT_READY"
    assert governance.calls == []


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
        governance_service=Governance(), generator=two_stage_generator(),
    )

    with pytest.raises(N8nPlannerError) as rejected:
        planner.start(project_id="project-a", session_id=session_id, message="Build a workflow")

    assert rejected.value.code == "N8N_PLAN_SCOPE_MISMATCH"


def test_selection_then_digest_confirmation_creates_server_snapshot_only(scope):
    governance = Governance()
    planner = N8nPlanningService(governance_service=governance, generator=two_stage_generator())
    plan = planner.start(project_id="project-a", session_id="session-a", message="Build a workflow")
    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="Use the minimal choice", selected_option_id=plan["choices"][0]["id"],
        expected_digest=plan["digest"],
    )
    assert selected["status"] == "selected"
    assert selected["assistant_message"] == (
        "你已選擇「Minimal」。目前尚未變更 n8n。"
        "請確認上述預期結果、風險及權限需求，再明確確認是否建立核准請求。"
    )
    assert governance.calls == []

    with pytest.raises(N8nPlannerError) as stale:
        planner.propose(
            plan["id"], project_id="project-a", session_id="session-a",
            expected_digest=plan["digest"], explicit_confirmation=True,
        )
    assert stale.value.code == "N8N_PLAN_STALE"

    compiled = materialize(planner, selected)
    result = planner.propose(
        plan["id"], project_id="project-a", session_id="session-a",
        expected_digest=compiled["digest"], explicit_confirmation=True,
    )
    assert result["operation"]["status"] == "pending"
    assert governance.calls[0]["project_id"] == "project-a"
    assert governance.calls[0]["operation"] == "create_draft"
    assert governance.calls[0]["materialization"]["workflow"]["name"] == "Minimal"
    assert governance.calls[0]["plan_digest"] == compiled["digest"]


def test_materialize_lease_rejects_a_second_request_without_model_or_compiler(scope):
    governance = Governance()
    generator_calls = []

    def generator(context):
        generator_calls.append(context.get("phase"))
        return two_stage_generator()(context)

    planner = N8nPlanningService(governance_service=governance, generator=generator)
    plan = planner.start(
        project_id="project-a", session_id="session-a", message="Build a workflow"
    )
    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="Use the minimal choice", selected_option_id=plan["choices"][0]["id"],
        expected_digest=plan["digest"],
    )
    stage_one_calls = list(generator_calls)
    with database.get_db_conn() as conn:
        conn.execute(
            "UPDATE n8n_agent_plans SET status='materializing' WHERE id=?",
            (plan["id"],),
        )

    with pytest.raises(N8nPlannerError) as busy:
        materialize(planner, selected)

    assert busy.value.code == "N8N_PLAN_MATERIALIZING"
    assert generator_calls == stage_one_calls
    assert governance.materialize_calls == []


def test_selected_option_blocker_message_remains_valid_traditional_chinese(scope):
    governance = Governance(api_key_configured=False)
    planner = N8nPlanningService(
        governance_service=governance, generator=two_stage_generator()
    )
    plan = planner.start(
        project_id="project-a", session_id="session-a", message="Build a workflow"
    )

    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="Use the minimal choice", selected_option_id=plan["choices"][0]["id"],
        expected_digest=plan["digest"],
    )

    assert selected["status"] == "blocked"
    assert selected["assistant_message"].endswith(
        "目前仍有安全前置條件未完成，因此尚不能建立核准請求。"
    )


def test_restricted_mode_fails_closed_without_pending_governance_api(scope):
    governance = Governance(mode="restricted")
    planner = N8nPlanningService(governance_service=governance, generator=two_stage_generator())
    plan = planner.start(project_id="project-a", session_id="session-a", message="Build a workflow")
    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="Select minimal", selected_option_id=plan["choices"][0]["id"], expected_digest=plan["digest"],
    )
    compiled = materialize(planner, selected)
    with pytest.raises(N8nPlannerError) as blocked:
        planner.propose(
            plan["id"], project_id="project-a", session_id="session-a",
            expected_digest=compiled["digest"], explicit_confirmation=True,
        )
    assert blocked.value.code == "N8N_PLAN_REVIEW_MODE_REQUIRED"
    assert governance.calls == []


def test_pending_capable_governance_allows_restricted_proposal(scope):
    governance = Governance(mode="restricted", planned=True)
    planner = N8nPlanningService(governance_service=governance, generator=two_stage_generator())
    plan = planner.start(project_id="project-a", session_id="session-a", message="Build a workflow")
    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="Select minimal", selected_option_id=plan["choices"][0]["id"], expected_digest=plan["digest"],
    )
    compiled = materialize(planner, selected)
    result = planner.propose(
        plan["id"], project_id="project-a", session_id="session-a",
        expected_digest=compiled["digest"], explicit_confirmation=True,
    )
    assert result["operation"]["id"] == "operation-planned"


def test_secrets_and_client_option_forgery_are_rejected(scope):
    governance = Governance()
    planner = N8nPlanningService(governance_service=governance, generator=two_stage_generator())
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
    planner = N8nPlanningService(governance_service=governance, generator=two_stage_generator())
    plan = planner.start(project_id="project-a", session_id="session-a", message="Build a workflow")
    with pytest.raises(N8nPlannerError) as stale:
        planner.add_message(
            plan["id"], project_id="project-a", session_id="session-a",
            message="continue", expected_digest="0" * 64,
        )
    assert stale.value.code == "N8N_PLAN_STALE"

    unsupported = N8nPlanningService(
        governance_service=governance, generator=two_stage_generator(operation="execute"),
    )
    with pytest.raises(N8nPlannerError) as invalid:
        unsupported.start(project_id="project-a", session_id="session-a", message="Run it")
    assert invalid.value.code == "N8N_PLAN_MODEL_INVALID"


def test_broker_readiness_allows_planning_but_blocks_selection_and_proposal(scope):
    governance = Governance(api_key_configured=False, runtime_ready=False)
    planner = N8nPlanningService(governance_service=governance, generator=two_stage_generator())
    plan = planner.start(project_id="project-a", session_id="session-a", message="先規劃一個流程")
    assert {item["code"] for item in plan["blockers"]} == {
        "N8N_API_KEY_NOT_CONFIGURED", "N8N_RUNTIME_NOT_READY",
    }
    assert any("API 金鑰" in item for item in plan["risk_summary"])
    assert any("不要把金鑰貼到對話" in item for item in plan["permission_requirements"])

    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="選擇 minimal", selected_option_id=plan["choices"][0]["id"], expected_digest=plan["digest"],
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
    planner = N8nPlanningService(governance_service=governance, generator=two_stage_generator())
    plan = planner.start(project_id="project-a", session_id="session-a", message="建立流程")
    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="選擇 minimal", selected_option_id=plan["choices"][0]["id"], expected_digest=plan["digest"],
    )
    assert selected["status"] == "selected"
    compiled = materialize(planner, selected)
    governance.runtime_ready = False
    with pytest.raises(N8nPlannerError) as unavailable:
        planner.propose(
            plan["id"], project_id="project-a", session_id="session-a",
            expected_digest=compiled["digest"], explicit_confirmation=True,
        )
    assert unavailable.value.code == "N8N_PLAN_BROKER_NOT_READY"
    assert governance.calls == []


def test_planner_routes_match_frontend_contract(scope):
    governance = Governance()
    planner = N8nPlanningService(governance_service=governance, generator=two_stage_generator())
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
        "selected_option_id": plan["choices"][0]["id"],
    })
    assert missing_digest.status_code == 422
    selected = client.post(f"/api/integrations/n8n/plans/{plan['id']}/messages", json={
        "project_id": "project-a", "session_id": "session-a", "message": "Select minimal",
        "selected_option_id": plan["choices"][0]["id"], "expected_digest": plan["digest"],
    })
    assert selected.status_code == 200
    compiled = client.post(f"/api/integrations/n8n/plans/{plan['id']}/materialize", json={
        "project_id": "project-a", "session_id": "session-a",
        "expected_digest": selected.json()["digest"],
    })
    assert compiled.status_code == 200
    assert compiled.json()["status"] == "graph_ready"
    proposed = client.post(f"/api/integrations/n8n/plans/{plan['id']}/propose", json={
        "project_id": "project-a", "session_id": "session-a",
        "expected_digest": compiled.json()["digest"], "explicit_confirmation": True,
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


def test_model_generator_uses_bounded_tool_free_catalog_prepass():
    payloads = []
    searches = []

    class Response:
        status_code = 200

        def __init__(self, content):
            self.content = content
            self.closed = False

        def json(self):
            return {"message": {"content": json.dumps(self.content)}}

        def close(self):
            self.closed = True

    def post(_settings, payload, **_kwargs):
        payloads.append(payload)
        if len(payloads) == 1:
            return Response(generated())
        if len(payloads) == 2:
            return Response({"terms": ["Webhook", "Email"]})
        return Response({"semantic": {"workflow_spec": {
            "schema": "workflow_spec.v1", "name": "Webhook email",
            "nodes": [{"key": "edit", "type": "n8n-nodes-base.set", "name": "Edit"}],
            "edges": [],
        }}})

    def search(project_id, *, session_id, query, limit):
        searches.append((project_id, session_id, query, limit))
        return {
            "catalog_digest": "c" * 64,
            "nodes": [
                {
                    "type": f"n8n-nodes-base.{query.casefold()}", "display_name": query,
                    "description": "safe", "group": ["trigger"], "versions": [1],
                    "default_version": 1, "dynamic_inputs": False, "dynamic_outputs": False,
                    "credential_types": [],
                },
                {
                    "type": "n8n-nodes-base.set", "display_name": "Edit Fields",
                    "description": "safe", "group": ["transform"], "versions": [1],
                    "default_version": 1, "dynamic_inputs": False, "dynamic_outputs": False,
                    "credential_types": [],
                },
            ],
        }

    generator = N8nPlanModelGenerator(
        lambda: {"default_chat_model": "model-a"}, post_chat=post, catalog_search=search,
    )
    context = {
        "project_id": "project-a", "session_id": "session-a",
        "policy": {"mode": "restricted"}, "workflow_inventory": {"workflows": []},
        "conversation": [{"role": "user", "content": "When a webhook arrives send email"}],
        "model": "model-a",
    }
    result = generator({**context, "phase": "architecture"})
    prepared = generator.prepare_materialization(context)
    semantic_result = generator({
        **context, **prepared, "phase": "materialize", "operation": "create_draft",
        "selected_architecture": result["choices"][0]["architecture"],
    })

    assert len(result["choices"]) == 2
    assert "semantic" in semantic_result
    assert payloads[0]["options"]["num_predict"] == 1200
    assert "copy existing step.key values verbatim" in payloads[0]["messages"][0]["content"]
    assert searches == [
        ("project-a", "session-a", "Webhook", 10),
        ("project-a", "session-a", "Email", 10),
    ]
    assert payloads[1]["options"]["num_predict"] == 640
    assert payloads[1]["format"] == "json"
    formal_source = payloads[2]["messages"][1]["content"]
    assert "n8n-nodes-base.webhook" in formal_source
    assert "n8n-nodes-base.email" in formal_source
    assert "exact node type values" in payloads[2]["messages"][0]["content"]
    assert "workbench.agent" in payloads[2]["messages"][0]["content"]
    assert "workbench.approval" in payloads[2]["messages"][0]["content"]
    assert payloads[2]["options"]["num_predict"] == 2400
    assert payloads[2]["format"] == "json"


@pytest.mark.parametrize("requested,expected", [
    ("manual_trigger", "n8n-nodes-base.manualTrigger"),
    ("Manual Trigger", "n8n-nodes-base.manualTrigger"),
    ("editFields", "n8n-nodes-base.set"),
    ("Edit Fields", "n8n-nodes-base.set"),
    ("IF", "n8n-nodes-base.if"),
])
def test_catalog_type_aliases_resolve_only_to_unique_server_entries(requested, expected):
    entries = [
        {"type": "n8n-nodes-base.manualTrigger", "name": "manualTrigger", "display_name": "Manual Trigger"},
        {"type": "n8n-nodes-base.set", "name": "set", "display_name": "Edit Fields"},
        {"type": "n8n-nodes-base.if", "name": "if", "display_name": "If"},
    ]
    checked = _enforce_catalog_choices({"choices": [{"semantic": {"workflow_spec": {
        "schema": "workflow_spec.v1", "name": "Canary",
        "nodes": [{"key": "node", "type": requested}], "edges": [],
    }}}]}, {"status": "ready", "entries": entries})
    assert checked["choices"][0]["semantic"]["workflow_spec"]["nodes"][0]["type"] == expected


def test_catalog_type_aliases_fail_closed_for_ambiguous_unknown_and_community_nodes():
    ambiguous = [
        {"type": "n8n-nodes-base.alpha", "name": "alpha", "display_name": "Same Node"},
        {"type": "n8n-nodes-base.beta", "name": "beta", "display_name": "Same Node"},
    ]

    def check(node_type, entries):
        return _enforce_catalog_choices({"choices": [{"semantic": {"workflow_spec": {
            "schema": "workflow_spec.v1", "name": "Blocked",
            "nodes": [{"key": "node", "type": node_type}], "edges": [],
        }}}]}, {"status": "ready", "entries": entries})

    with pytest.raises(N8nPlannerError) as duplicate:
        check("Same Node", ambiguous)
    assert duplicate.value.code == "N8N_PLAN_NODE_TYPE_AMBIGUOUS"
    with pytest.raises(N8nPlannerError) as unknown:
        check("Unknown Node", ambiguous)
    assert unknown.value.code == "N8N_PLAN_NODE_NOT_IN_CATALOG"
    with pytest.raises(N8nPlannerError) as community:
        check("community.customNode", ambiguous)
    assert community.value.code == "N8N_PLAN_NODE_NOT_IN_CATALOG"


def test_catalog_candidates_union_architecture_conversation_and_model_terms():
    searches = []

    class Response:
        status_code = 200

        def json(self):
            return {"message": {"content": json.dumps({"terms": ["Conditional"]})}}

        def close(self):
            return None

    catalog_by_term = {
        "manual trigger": {"type": "n8n-nodes-base.manualTrigger", "name": "manualTrigger", "display_name": "Manual Trigger"},
        "if": {"type": "n8n-nodes-base.if", "name": "if", "display_name": "If"},
        "edit fields": {"type": "n8n-nodes-base.set", "name": "set", "display_name": "Edit Fields"},
        "conditional": {"type": "n8n-nodes-base.switch", "name": "switch", "display_name": "Switch"},
    }

    def search(_project, *, session_id, query, limit):
        searches.append((session_id, query, limit))
        value = catalog_by_term.get(query.casefold())
        return {"catalog_digest": "c" * 64, "nodes": [value] if value else []}

    generator = N8nPlanModelGenerator(
        lambda: {"default_chat_model": "model-a"},
        post_chat=lambda *_args, **_kwargs: Response(), catalog_search=search,
    )
    result = generator.prepare_materialization({
        "project_id": "project-a", "session_id": "session-a", "model": "model-a",
        "selected_architecture": {"steps": [
            {"key": "start", "capability": "manual_trigger"},
            {"key": "condition", "capability": "if"},
        ]},
        "conversation": [{"role": "user", "content": "建立 Manual Trigger → Edit Fields → IF"}],
    })["node_catalog"]

    assert [item[1] for item in searches] == ["manual trigger", "if", "Edit Fields", "Conditional"]
    assert {item["type"] for item in result["entries"]} == {
        "n8n-nodes-base.manualTrigger", "n8n-nodes-base.if",
        "n8n-nodes-base.set", "n8n-nodes-base.switch",
    }
    assert result["catalog_digest"] == "c" * 64


def test_catalog_digest_change_during_candidate_union_fails_closed():
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"message": {"content": json.dumps({"terms": ["Edit Fields"]})}}

        def close(self):
            return None

    def search(_project, *, session_id, query, limit):
        calls.append(query)
        return {"catalog_digest": ("c" if len(calls) == 1 else "d") * 64, "nodes": []}

    generator = N8nPlanModelGenerator(
        lambda: {"default_chat_model": "model-a"},
        post_chat=lambda *_args, **_kwargs: Response(), catalog_search=search,
    )
    with pytest.raises(N8nPlannerError) as stale:
        generator.prepare_materialization({
            "project_id": "project-a", "session_id": "session-a", "model": "model-a",
            "selected_architecture": {"steps": [{"key": "start", "capability": "manual_trigger"}]},
            "conversation": [{"role": "user", "content": "Edit Fields"}],
        })
    assert stale.value.code == "N8N_NODE_CATALOG_STALE"


def test_architecture_retry_budget_is_bounded_and_length_is_reported_safely():
    payloads = []

    class Response:
        status_code = 200

        def json(self):
            return {
                "done": True, "done_reason": "length",
                "message": {"content": "{\"assistant_message\":\"truncated"},
            }

        def close(self):
            return None

    def post(_settings, payload, **_kwargs):
        payloads.append(payload)
        return Response()

    generator = N8nPlanModelGenerator(
        lambda: {"default_chat_model": "model-a"}, post_chat=post,
    )
    context = {
        "phase": "architecture", "attempt": 2,
        "project_id": "project-a", "session_id": "session-a", "model": "model-a",
        "policy": {"mode": "restricted"},
        "workflow_inventory": {"workflows": []},
        "conversation": [{"role": "user", "content": "Build a safe workflow"}],
    }

    with pytest.raises(N8nPlannerError) as truncated:
        generator(context)

    assert truncated.value.code == "N8N_PLAN_MODEL_INVALID"
    assert "truncated" in truncated.value.message
    assert payloads[0]["options"]["num_predict"] == 2400


def test_nvidia_stage_one_uses_strict_json_schema_and_capability_fallback(scope):
    payloads = []

    class Response:
        def __init__(self, status, content=None, text=""):
            self.status_code = status
            self.content = content
            self.text = text

        def json(self):
            return {"message": {"content": json.dumps(self.content)}}

        def close(self):
            return None

    def post(_settings, payload, **_kwargs):
        payloads.append(payload)
        if len(payloads) == 1:
            return Response(400, text="response_format json_schema is unsupported")
        return Response(200, generated())

    settings = {
        "default_chat_model": "connection::nvidia/nemotron-test",
        "model_providers": [{
            "id": "connection", "enabled": True,
            "base_url": "http://127.0.0.1:9999/v1",
        }],
    }
    planner = N8nPlanningService(
        governance_service=Governance(),
        generator=N8nPlanModelGenerator(lambda: settings, post_chat=post),
    )
    plan = planner.start(project_id="project-a", session_id="session-a", message="Build it")

    schema = payloads[0]["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["choices"]["minItems"] == 2
    assert schema["properties"]["choices"]["maxItems"] == 3
    assert schema["properties"]["risk_summary"]["type"] == "array"
    assert "nodes" not in json.dumps(schema["properties"]["choices"])
    assert "nvext" in payloads[1] and "guided_json" in payloads[1]["nvext"]
    assert plan["generation_provenance"]["structured_mode"] == "guided_json"
    assert plan["generation_provenance"]["format_repaired"] is False


@pytest.mark.parametrize("status", [401, 429, 500])
def test_structured_mode_does_not_fallback_for_auth_limit_or_server_errors(scope, status):
    payloads = []

    class Response:
        status_code = status
        text = "response_format json_schema is unsupported"

        def close(self):
            return None

    def post(_settings, payload, **_kwargs):
        payloads.append(payload)
        return Response()

    settings = {
        "default_chat_model": "connection::nvidia/nemotron-test",
        "model_providers": [{
            "id": "connection", "enabled": True,
            "base_url": "http://127.0.0.1:9999/v1",
        }],
    }
    planner = N8nPlanningService(
        governance_service=Governance(),
        generator=N8nPlanModelGenerator(lambda: settings, post_chat=post),
    )

    with pytest.raises(N8nPlannerError) as rejected:
        planner.start(project_id="project-a", session_id="session-a", message="Build it")

    assert rejected.value.code == "N8N_PLAN_MODEL_REJECTED"
    assert len(payloads) == 1
    assert "response_format" in payloads[0]


def test_stage_one_uses_local_gemma_only_for_format_repair(scope):
    payloads = []
    invalid = generated()
    invalid["risk_summary"] = invalid["risk_summary"][0]
    repaired = generated()

    class Response:
        status_code = 200
        text = ""

        def __init__(self, content):
            self.content = content

        def json(self):
            return {"message": {"content": json.dumps(self.content)}, "done": True}

        def close(self):
            return None

    def post(_settings, payload, **_kwargs):
        payloads.append(payload)
        return Response(invalid if len(payloads) < 3 else repaired)

    settings = {
        "default_chat_model": "connection::nvidia/nemotron-test",
        "ollama_url": "http://127.0.0.1:11434",
        "model_providers": [{
            "id": "connection", "enabled": True,
            "base_url": "http://127.0.0.1:9999/v1",
        }],
    }
    planner = N8nPlanningService(
        governance_service=Governance(),
        generator=N8nPlanModelGenerator(lambda: settings, post_chat=post),
    )
    plan = planner.start(project_id="project-a", session_id="session-a", message="Build it")

    assert len(payloads) == 3
    assert payloads[2]["model"] == "ollama::gemma4-hermes:latest"
    assert "format" in payloads[2]
    repair_source = payloads[2]["messages"][1]["content"]
    assert "conversation" not in repair_source
    assert "workflow_inventory" not in repair_source
    assert plan["generation_provenance"] == {
        "primary_model": "connection::nvidia/nemotron-test",
        "structured_mode": "json_schema",
        "format_repaired": True,
        "repair_model": "ollama::gemma4-hermes:latest",
        "repair_count": 1,
    }
    assert "_generation_integrity" not in plan


def test_format_repair_rejects_semantic_drift():
    candidate = generated()
    candidate["risk_summary"] = candidate["risk_summary"][0]
    drifted = generated()
    drifted["choices"][0]["operation"] = "delete"

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {"message": {"content": json.dumps(drifted)}, "done": True}

        def close(self):
            return None

    generator = N8nPlanModelGenerator(
        lambda: {"default_chat_model": "model-a", "ollama_url": "http://127.0.0.1:11434"},
        post_chat=lambda *_args, **_kwargs: Response(),
    )
    with pytest.raises(N8nPlannerError) as rejected:
        generator.repair_architecture_format({
            "project_id": "project-a", "candidate": candidate,
            "validation_issue": "risk summary must be a list",
            "structured_mode": "json_schema",
        })
    assert rejected.value.code == "N8N_PLAN_REPAIR_SEMANTIC_DRIFT"


@pytest.mark.parametrize("mutation", ["text", "order", "capability", "edge", "external_target"])
def test_format_repair_rejects_all_semantic_changes(mutation):
    candidate = generated()
    candidate["risk_summary"] = candidate["risk_summary"][0]
    repaired = generated()
    if mutation == "text":
        repaired["choices"][0]["description"] = "Rewritten meaning"
    elif mutation == "order":
        repaired["choices"].reverse()
    elif mutation == "capability":
        repaired["choices"][0]["architecture"]["steps"][0]["capability"] = "HTTP Request"
    elif mutation == "edge":
        repaired["choices"][0]["architecture"]["edges"] = [{"from": "edit", "to": "edit"}]
    else:
        repaired["choices"][0]["architecture"]["required_inputs"] = ["https://external.example"]

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {"message": {"content": json.dumps(repaired)}, "done": True}

        def close(self):
            return None

    generator = N8nPlanModelGenerator(
        lambda: {"default_chat_model": "model-a", "ollama_url": "http://127.0.0.1:11434"},
        post_chat=lambda *_args, **_kwargs: Response(),
    )
    with pytest.raises(N8nPlannerError) as rejected:
        generator.repair_architecture_format({
            "project_id": "project-a", "candidate": candidate,
            "validation_issue": "format only", "structured_mode": "json_schema",
        })
    assert rejected.value.code == "N8N_PLAN_REPAIR_SEMANTIC_DRIFT"


def test_format_repair_rejects_secret_before_model_call():
    calls = []
    candidate = generated()
    candidate["risk_summary"] = candidate["risk_summary"][0]
    candidate["choices"][0]["architecture"]["required_inputs"] = ["api_key=sk-secret-value"]
    generator = N8nPlanModelGenerator(
        lambda: {"default_chat_model": "model-a", "ollama_url": "http://127.0.0.1:11434"},
        post_chat=lambda *_args, **_kwargs: calls.append(True),
    )
    with pytest.raises(N8nPlannerError) as rejected:
        generator.repair_architecture_format({
            "project_id": "project-a", "candidate": candidate,
            "validation_issue": "format only", "structured_mode": "json_schema",
        })
    assert rejected.value.code == "N8N_PLAN_SECRET_REJECTED"
    assert calls == []


@pytest.mark.parametrize("settings", [
    {
        "default_chat_model": "connection::provider/model",
        "model_providers": [{
            "id": "connection", "enabled": True,
            "base_url": "http://127.0.0.1:9999/v1",
        }],
    },
    {
        "default_chat_model": "legacy-model", "model_provider": "openai_compatible",
        "openai_compatible_url": "http://127.0.0.1:9999/v1", "openai_api_key_env": "",
    },
])
def test_non_ollama_provider_does_not_receive_ollama_json_format(settings):
    payloads = []

    class Response:
        status_code = 200

        def __init__(self, content):
            self.content = content

        def json(self):
            return {"message": {"content": json.dumps(self.content)}}

        def close(self):
            return None

    def post(_settings, payload, **_kwargs):
        payloads.append(payload)
        return Response({"terms": ["Edit Fields"]} if len(payloads) == 1 else generated())

    def search(*_args, **_kwargs):
        return {"catalog_digest": "c" * 64, "nodes": [{
            "type": "n8n-nodes-base.set", "display_name": "Edit Fields",
            "description": "safe", "group": ["transform"], "versions": [1],
            "default_version": 1, "dynamic_inputs": False, "dynamic_outputs": False,
            "credential_types": [],
        }]}

    N8nPlanModelGenerator(
        lambda: settings,
        post_chat=post, catalog_search=search,
    )({
        "project_id": "project-a", "session_id": "session-a",
        "policy": {"mode": "restricted"}, "workflow_inventory": {"workflows": []},
        "conversation": [{"role": "user", "content": "Build a workflow"}],
    })

    assert all("format" not in payload for payload in payloads)


def test_model_generator_reports_only_safe_structural_validation_issue(scope):
    class Response:
        status_code = 200

        def json(self):
            return {"message": {"content": json.dumps({"assistant_message": "Need details"})}}

        def close(self):
            return None

    generator = N8nPlanModelGenerator(
        lambda: {"default_chat_model": "model-a"}, post_chat=lambda *_args, **_kwargs: Response(),
    )
    planner = N8nPlanningService(governance_service=Governance(), generator=generator)
    with pytest.raises(N8nPlannerError) as exc_info:
        planner.start(project_id="project-a", session_id="session-a", message="Build a workflow")

    assert exc_info.value.code == "N8N_PLAN_MODEL_INVALID"
    assert "two or three safe architectures" in exc_info.value.message
    assert "Need details" not in exc_info.value.message


@pytest.mark.parametrize("choice_count", [1, 4])
def test_stage_one_rejects_incomplete_choice_sets_without_persisting(scope, choice_count):
    calls = []

    def invalid(context):
        calls.append(context["attempt"])
        value = generated()
        value["choices"] = (value["choices"] * 2)[:choice_count]
        return value

    planner = N8nPlanningService(governance_service=Governance(), generator=invalid)
    with pytest.raises(N8nPlannerError) as rejected:
        planner.start(project_id="project-a", session_id="session-a", message="Build it")
    assert rejected.value.code == "N8N_PLAN_MODEL_INVALID"
    assert calls == [0, 1, 2]
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS total FROM n8n_agent_plans").fetchone()["total"] == 0


def test_stage_one_rejects_embedded_workflow_spec_and_mints_opaque_ids(scope):
    clean = generated()
    planner = N8nPlanningService(
        governance_service=Governance(), generator=two_stage_generator()
    )
    plan = planner.start(project_id="project-a", session_id="session-a", message="Build it")
    assert all(re.fullmatch(r"n8nchoice_[a-f0-9]{32}", item["id"]) for item in plan["choices"])
    assert sum(item["recommended"] is True for item in plan["choices"]) == 1
    assert "workflow_spec" not in json.dumps(plan["choices"])

    clean["choices"][0]["workflow_spec"] = {"nodes": []}
    invalid = N8nPlanningService(
        governance_service=Governance(), generator=lambda _context: copy.deepcopy(clean)
    )
    with pytest.raises(N8nPlannerError) as rejected:
        invalid.start(project_id="project-a", session_id="session-a", message="Build it")
    assert rejected.value.code == "N8N_PLAN_MODEL_INVALID"


def test_selected_followup_preserves_architecture_without_rerunning_stage_one(scope):
    phases = []
    base = two_stage_generator()

    def tracked(context):
        phases.append(context.get("phase"))
        return base(context)

    planner = N8nPlanningService(governance_service=Governance(), generator=tracked)
    plan = planner.start(project_id="project-a", session_id="session-a", message="Build it")
    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a", message="Use minimal",
        selected_option_id=plan["choices"][0]["id"], expected_digest=plan["digest"],
    )
    clarified = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a",
        message="The value should be reviewed", expected_digest=selected["digest"],
    )
    assert clarified["status"] == "selected"
    assert clarified["selected_option_id"] == selected["selected_option_id"]
    assert phases == ["architecture"]


def test_stage_two_retries_invalid_semantic_at_most_twice(scope):
    attempts = []

    def generator(context):
        if context.get("phase") == "architecture":
            return generated()
        attempts.append(context.get("attempt"))
        if len(attempts) < 3:
            return {"not_semantic": True}
        return semantic()

    planner = N8nPlanningService(governance_service=Governance(), generator=generator)
    plan = planner.start(project_id="project-a", session_id="session-a", message="Build it")
    selected = planner.add_message(
        plan["id"], project_id="project-a", session_id="session-a", message="Use minimal",
        selected_option_id=plan["choices"][0]["id"], expected_digest=plan["digest"],
    )
    result = materialize(planner, selected)
    assert result["status"] == "graph_ready"
    assert attempts == [0, 1, 2]


def test_old_plan_schema_and_model_drift_fail_closed(scope):
    planner = N8nPlanningService(governance_service=Governance(), generator=two_stage_generator())
    plan = planner.start(
        project_id="project-a", session_id="session-a", message="Build it", model="model-a"
    )
    with pytest.raises(N8nPlannerError) as model_drift:
        planner.add_message(
            plan["id"], project_id="project-a", session_id="session-a", message="continue",
            expected_digest=plan["digest"], model="model-b",
        )
    assert model_drift.value.code == "N8N_PLAN_MODEL_STALE"

    with database.get_db_conn() as conn:
        conn.execute("UPDATE n8n_agent_plans SET plan_schema=NULL WHERE id=?", (plan["id"],))
    with pytest.raises(N8nPlannerError) as schema_stale:
        planner.add_message(
            plan["id"], project_id="project-a", session_id="session-a", message="continue",
            expected_digest=plan["digest"],
        )
    assert schema_stale.value.code == "N8N_PLAN_SCHEMA_STALE"


def test_workbench_composes_planner_with_governance_and_router():
    assert "from n8n_agent_planner import N8nPlanModelGenerator, N8nPlanningService" in APP_SOURCE
    assert "n8n_agent_planner = N8nPlanningService(" in APP_SOURCE
    assert "governance_service=n8n_agent_governance" in APP_SOURCE
    assert "generator=N8nPlanModelGenerator(" in APP_SOURCE
    assert "settings_loader=load_settings" in APP_SOURCE
    assert "catalog_search=n8n_agent_governance.search_node_catalog" in APP_SOURCE
    assert "protected_workflow_guard=_inspect_configured_n8n_agent_bridges" in APP_SOURCE
    assert "planning_context_provider=_n8n_agent_planning_context" in APP_SOURCE
    assert "planner=n8n_agent_planner" in APP_SOURCE
