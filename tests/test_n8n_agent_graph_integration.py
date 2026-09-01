from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest


BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import database  # noqa: E402
from n8n_agent_governance import (  # noqa: E402
    N8nAgentGovernanceService,
    N8nGovernanceError,
)
from n8n_agent_planner import N8nPlannerError, N8nPlanningService  # noqa: E402
from n8n_gmail_crypto import AesGcmContentCipher  # noqa: E402
from n8n_graph_authoring import GraphAuthoringEngine, NodeCatalog  # noqa: E402


def _node(name: str, *, inputs=None, outputs=None, properties=None, credentials=None):
    return {
        "_package": "n8n-nodes-base", "displayName": name, "name": name,
        "group": ["transform"], "version": 1, "description": name,
        "defaults": {"name": name},
        "inputs": ["main"] if inputs is None else inputs,
        "outputs": ["main"] if outputs is None else outputs,
        "properties": properties or [], "credentials": credentials or [],
    }


def _engine():
    catalog = NodeCatalog.from_entries(
        [
            _node("manualTrigger", inputs=[]),
            _node("set"),
            _node(
                "executeWorkflow",
                properties=[{
                    "name": "workflowId", "displayName": "Workflow",
                    "type": "workflowSelector", "required": True, "default": "",
                }],
            ),
            _node(
                "gmail",
                properties=[
                    {
                        "name": "operation", "type": "options", "default": "send",
                        "options": [{"name": "Send", "value": "send"}],
                    },
                    {"name": "sendTo", "type": "string", "required": True, "default": ""},
                ],
                credentials=[{"name": "gmailOAuth2", "required": True}],
            ),
        ],
        fingerprint={"n8n_version": "2.32.5", "n8n_nodes_base_version": "2.32.3"},
    )
    return GraphAuthoringEngine(catalog)


def _semantic_spec(name="Materialized"):
    return {
        "schema": "workflow_spec.v1", "name": name,
        "nodes": [
            {"key": "start", "type": "manualTrigger", "name": "Start"},
            {"key": "edit", "type": "set", "name": "Edit", "parameters": {"value": "private-text"}},
        ],
        "edges": [{"from": "start", "to": "edit"}],
    }


def _generated(spec=None):
    def choice(label, recommended=False):
        return {
            "label": label, "description": "Prepare a graph.",
            "operation": "create_draft", "workflow_id": None, "workflow_name": None,
            "architecture": {
                "schema": "workbench.n8n.architecture.v1", "goal": f"Prepare {label}",
                "steps": [
                    {"key": "start", "capability": "Manual Trigger", "purpose": "Start manually"},
                    {"key": "edit", "capability": "Edit Fields", "purpose": f"Prepare {label}"},
                ],
                "edges": [{"from": "start", "to": "edit"}],
                "required_inputs": [], "assumptions": [label],
            },
            "expected_result": "An inactive draft.", "risks": ["n8n will change after approval."],
            "permissions": ["Human approval is required."], "recommended": recommended,
        }
    return {
        "assistant_message": "Choose an architecture; nothing has changed.",
        "risk_summary": ["Planning is read-only."], "expected_result": "A reviewed graph.",
        "permission_requirements": ["Explicit approval is required."],
        "choices": [choice("One", True), choice("Two")],
    }


def _two_stage_generator(spec=None):
    def generate(context):
        if context.get("phase") in {"materialize", "materialize_repair"}:
            return {"semantic": {"workflow_spec": copy.deepcopy(spec or _semantic_spec())}}
        return _generated(spec)
    return generate


class Broker:
    def __init__(self):
        self._api_key_provider = lambda: "x" * 32
        self.workflows = {
            "unmanaged": {
                "name": "Unmanaged", "nodes": [{
                    "id": "unmanaged-start", "name": "Start", "type": "n8n-nodes-base.manualTrigger",
                    "typeVersion": 1, "position": [240, 180], "parameters": {},
                }], "connections": {}, "settings": {}, "active": False,
            }
        }
        self.calls = []

    def list_workflows(self):
        return []

    def execute(self, operation, payload, *, secret=None):
        self.calls.append((operation, copy.deepcopy(payload)))
        if operation == "create_draft":
            self.workflows["created"] = {**copy.deepcopy(payload["workflow"]), "active": False}
            return {"id": "created", "name": payload["workflow"]["name"], "active": False}
        return {"id": payload.get("workflow_id")}

    def get_workflow(self, workflow_id):
        workflow = self.workflows[workflow_id]
        return {
            "id": workflow_id, "name": workflow["name"], "active": workflow.get("active") is True,
            "protected": False, "updated_at": workflow.get("updated_at", "v1"),
            "workflow": copy.deepcopy(workflow),
            "facts": {"name": workflow["name"], "active": False, "nodes": [], "edges": [],
                      "external_targets": [], "credential_aliases": []},
        }

    def editor_url(self, workflow_id):
        return f"http://127.0.0.1:5678/workflow/{workflow_id}"

    def security_audit(self):
        return {"status": "clean", "findings": [], "verified": True}


@pytest.fixture()
def services(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "workbench.db"))
    database.init_db()
    database.create_project("project", "Project", str(tmp_path / "project"))
    database.create_session("session", project_id="project")
    broker = Broker()
    governance = N8nAgentGovernanceService(
        broker=broker, cipher=AesGcmContentCipher(lambda: b"k" * 32),
        n8n_running=lambda: True, graph_authoring=_engine(),
        integration_permission_check=lambda *_args, **_kwargs: {"decision": "allow"},
    )
    planner = N8nPlanningService(
        governance_service=governance, generator=_two_stage_generator(),
        graph_authoring=governance.graph_authoring,
    )
    return governance, planner, broker


def test_semantic_choice_materializes_then_creates_exact_inactive_draft(services):
    governance, planner, broker = services
    plan = planner.start(project_id="project", session_id="session", message="Build it")
    assert all("semantic" not in choice and "workflow" not in choice for choice in plan["choices"])
    selected = planner.add_message(
        plan["id"], project_id="project", session_id="session", message="Choose one",
        selected_option_id=plan["choices"][0]["id"], expected_digest=plan["digest"],
    )
    assert selected["status"] == "selected"
    with pytest.raises(N8nPlannerError) as early:
        planner.propose(
            plan["id"], project_id="project", session_id="session",
            expected_digest=selected["digest"], explicit_confirmation=True,
        )
    assert early.value.code == "N8N_PLAN_GRAPH_REQUIRED"

    materialized = planner.materialize(
        plan["id"], project_id="project", session_id="session",
        expected_digest=selected["digest"],
    )
    assert materialized["status"] == "graph_ready"
    assert materialized["graph_preview"]["node_count"] == 2
    assert len(materialized["graph_digest"]) == 64
    assert "private-text" not in str(materialized["materialization"]["diff"])

    proposed = planner.propose(
        plan["id"], project_id="project", session_id="session",
        expected_digest=materialized["digest"], explicit_confirmation=True,
    )["operation"]
    assert proposed["status"] == "pending"
    assert proposed["graph_digest"] == materialized["graph_digest"]
    completed = governance.decide(
        proposed["id"], project_id="project", expected_digest=proposed["digest"], approved=True,
    )
    assert completed["status"] == "completed"
    assert completed["result"]["editor_url"] == "http://127.0.0.1:5678/workflow/created"
    assert broker.workflows["created"]["active"] is False


def test_missing_semantic_input_does_not_create_operation(services):
    governance, _planner, broker = services
    result = governance.materialize_planned_choice(
        project_id="project", session_id="session", operation="create_draft",
        semantic={"workflow_spec": {**_semantic_spec(), "name": ""}},
    )
    assert result["status"] == "needs_input"
    assert result["questions"]
    assert broker.calls == []


def test_materializer_receives_authoritative_project_session_context(services):
    governance, _planner, _broker = services
    inner = governance.graph_authoring
    captured = []

    class Spy:
        catalog = inner.catalog

        def materialize(self, spec, **kwargs):
            captured.append(copy.deepcopy(kwargs.get("context")))
            return inner.materialize(spec, **kwargs)

        def __getattr__(self, name):
            return getattr(inner, name)

    governance.graph_authoring = Spy()
    result = governance.materialize_planned_choice(
        project_id="project", session_id="session", operation="create_draft",
        semantic={"workflow_spec": _semantic_spec()},
    )
    assert result["status"] == "graph_ready"
    assert captured == [{
        "project_id": "project", "session_id": "session",
        "operation": "create_draft", "source": "planner",
    }]


def test_raw_browser_graph_is_not_a_bypass_when_authoring_is_enabled(services):
    governance, _planner, broker = services
    with pytest.raises(N8nGovernanceError) as rejected:
        governance.create_operation({
            "project_id": "project", "session_id": "session", "operation": "create_draft",
            "payload": {"workflow": {"name": "Raw", "nodes": [], "connections": {}}},
        })
    assert rejected.value.code == "N8N_WORKFLOW_SPEC_REQUIRED"
    assert broker.calls == []


def test_unmanaged_workflow_requires_digest_and_exact_name_before_adoption(services):
    governance, _planner, _broker = services
    preview = governance.preview_adoption("project", "unmanaged", session_id="session")
    with pytest.raises(N8nGovernanceError) as wrong:
        governance.adopt_workflow(
            "project", "unmanaged", session_id="session",
            expected_digest=preview["expected_digest"], confirmation="wrong",
        )
    assert wrong.value.code == "N8N_ADOPTION_CONFIRMATION_REQUIRED"
    adopted = governance.adopt_workflow(
        "project", "unmanaged", session_id="session",
        expected_digest=preview["expected_digest"], confirmation="Unmanaged",
    )
    assert adopted["managed"] is True


def _bind_managed_workflow(workflow_id: str, *, name: str = "Managed"):
    with database.get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO n8n_agent_workflow_bindings(
                workflow_id,project_id,workflow_name,created_at,updated_at
            ) VALUES(?,?,?,?,?)
            """,
            (workflow_id, "project", name, "2026-08-14T00:00:00+00:00", "2026-08-14T00:00:00+00:00"),
        )


@pytest.mark.parametrize("operation", ["activate", "publish"])
def test_publish_and_activate_revalidate_exact_existing_graph_and_block_generic_subworkflow(
    services, operation,
):
    governance, _planner, broker = services
    broker.workflows["hidden-call"] = {
        "name": "Hidden call", "active": False, "updated_at": "v1", "settings": {},
        "nodes": [
            {
                "id": "start", "name": "Start", "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1, "position": [240, 180], "parameters": {},
            },
            {
                "id": "call", "name": "Hidden subworkflow",
                "type": "n8n-nodes-base.executeWorkflow", "typeVersion": 1,
                "position": [500, 180],
                "parameters": {"workflowId": {"value": "arbitrary-external-workflow"}},
            },
        ],
        "connections": {
            "Start": {"main": [[{
                "node": "Hidden subworkflow", "type": "main", "index": 0,
            }]]},
        },
    }
    _bind_managed_workflow("hidden-call", name="Hidden call")

    direct = governance.graph_authoring.validate(broker.workflows["hidden-call"])
    assert direct.status == "needs_input"
    assert "EXTERNAL_ACTION_CLASSIFICATION_REQUIRED" in {item.code for item in direct.issues}

    materialized = governance.materialize_planned_choice(
        project_id="project", session_id="session", operation=operation,
        semantic={"workflow_id": "hidden-call"},
    )
    assert materialized["status"] == "needs_input"
    assert "EXTERNAL_ACTION_CLASSIFICATION_REQUIRED" in {
        item["code"] for item in materialized["issues"]
    }
    assert broker.calls == []


def test_activate_revalidates_manual_enable_of_disabled_external_write(services):
    governance, _planner, broker = services
    broker.workflows["manual-toggle"] = {
        "name": "Manual toggle", "active": False, "updated_at": "v1", "settings": {},
        "nodes": [
            {
                "id": "start", "name": "Start", "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1, "position": [240, 180], "parameters": {},
            },
            {
                "id": "mail", "name": "Send mail", "type": "n8n-nodes-base.gmail",
                "typeVersion": 1, "position": [500, 180], "disabled": True,
                "parameters": {"operation": "send", "sendTo": "reviewed@example.test"},
                "credentials": {"gmailOAuth2": {"id": "opaque", "name": "gmail-main"}},
            },
        ],
        "connections": {
            "Start": {"main": [[{"node": "Send mail", "type": "main", "index": 0}]]},
        },
    }
    _bind_managed_workflow("manual-toggle", name="Manual toggle")

    disabled = governance.materialize_planned_choice(
        project_id="project", session_id="session", operation="activate",
        semantic={"workflow_id": "manual-toggle"},
    )
    assert disabled["status"] == "graph_ready"

    broker.workflows["manual-toggle"]["nodes"][1].pop("disabled")
    broker.workflows["manual-toggle"]["updated_at"] = "v2"
    enabled = governance.materialize_planned_choice(
        project_id="project", session_id="session", operation="activate",
        semantic={"workflow_id": "manual-toggle"},
    )
    assert enabled["status"] != "graph_ready"
    assert "EXTERNAL_WRITE_APPROVAL_BYPASS" in {item["code"] for item in enabled["issues"]}
    assert broker.calls == []


def test_activate_proposal_rejects_manual_graph_drift_before_execution(services):
    governance, _planner, broker = services
    preview = governance.preview_adoption("project", "unmanaged", session_id="session")
    governance.adopt_workflow(
        "project", "unmanaged", session_id="session",
        expected_digest=preview["expected_digest"], confirmation="Unmanaged",
    )
    operation = governance.create_operation({
        "project_id": "project", "session_id": "session", "operation": "activate",
        "payload": {"workflow_id": "unmanaged"},
    })
    assert operation["status"] == "pending"

    broker.workflows["unmanaged"]["updated_at"] = "v2"
    broker.workflows["unmanaged"]["nodes"].append({
        "id": "call", "name": "Hidden subworkflow",
        "type": "n8n-nodes-base.executeWorkflow", "typeVersion": 1,
        "position": [500, 180],
        "parameters": {"workflowId": {"value": "arbitrary-external-workflow"}},
    })
    broker.workflows["unmanaged"]["connections"] = {
        "Start": {"main": [[{
            "node": "Hidden subworkflow", "type": "main", "index": 0,
        }]]},
    }
    with pytest.raises(N8nGovernanceError) as stale:
        governance.decide(
            operation["id"], project_id="project", session_id="session",
            expected_digest=operation["digest"], approved=True,
        )
    assert stale.value.code == "N8N_WORKFLOW_STALE"
    assert broker.calls == []


def test_binding_claims_finalize_only_after_exact_remote_graph_reconciliation(services):
    governance, _planner, broker = services
    authoring = governance.graph_authoring
    compiled = authoring.materialize(_semantic_spec()).to_dict()
    compiled["base_digest"] = __import__("hashlib").sha256(
        b'{"target":"new-workflow"}'
    ).hexdigest()
    compiled["binding_claims"] = [{
        "binding_claim_id": "claim-1", "binding_id": "binding-1",
        "node_id": compiled["workflow"]["nodes"][1]["id"], "provisional": True,
    }]
    finalized = []

    def finalize(claims, context):
        finalized.append((copy.deepcopy(claims), copy.deepcopy(context)))
        return [{"binding_id": "binding-1", "status": "inactive"}]

    governance.graph_binding_finalizer = finalize
    operation = governance.create_planned_operation({
        "project_id": "project", "session_id": "session", "operation": "create_draft",
        "materialization": compiled, "plan_digest": "f" * 64,
    })
    assert finalized == []
    completed = governance.decide(
        operation["id"], project_id="project", session_id="session",
        expected_digest=operation["digest"], approved=True,
    )
    assert completed["status"] == "completed"
    assert completed["result"]["binding_count"] == 1
    assert finalized[0][0][0]["binding_claim_id"] == "claim-1"
    assert finalized[0][1]["workflow_id"] == "created"
    assert finalized[0][1]["graph_digest"] == completed["graph_digest"]
    assert broker.calls


def test_pending_operation_cannot_be_executed_or_approved_from_another_session(services):
    governance, planner, broker = services
    plan = planner.start(project_id="project", session_id="session", message="Build it")
    selected = planner.add_message(
        plan["id"], project_id="project", session_id="session", message="one",
        selected_option_id=plan["choices"][0]["id"], expected_digest=plan["digest"],
    )
    materialized = planner.materialize(
        plan["id"], project_id="project", session_id="session", expected_digest=selected["digest"],
    )
    operation = planner.propose(
        plan["id"], project_id="project", session_id="session",
        expected_digest=materialized["digest"], explicit_confirmation=True,
    )["operation"]
    with pytest.raises(N8nGovernanceError) as direct:
        governance._execute(operation["id"])
    assert direct.value.code == "N8N_EXECUTION_ALREADY_CLAIMED"
    with pytest.raises(N8nGovernanceError) as mismatch:
        governance.decide(
            operation["id"], project_id="project", session_id="different-session",
            expected_digest=operation["digest"], approved=True,
        )
    assert mismatch.value.code == "N8N_APPROVAL_SCOPE_MISMATCH"
    assert broker.calls == []
