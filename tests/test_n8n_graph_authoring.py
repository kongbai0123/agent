from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from n8n_graph_authoring import (  # noqa: E402
    DEFAULT_N8N_RUNTIME_ROOT,
    GraphAuthoringEngine,
    GraphAuthoringError,
    LazyGraphAuthoringEngine,
    LazyNodeCatalog,
    NodeCatalog,
)


def node(
    name: str,
    *,
    version=1,
    inputs=None,
    outputs=None,
    properties=None,
    credentials=None,
):
    return {
        "_package": "n8n-nodes-base",
        "displayName": name,
        "name": name,
        "group": ["transform"],
        "version": version,
        "description": f"Fixture {name}",
        "defaults": {"name": name},
        "inputs": ["main"] if inputs is None else inputs,
        "outputs": ["main"] if outputs is None else outputs,
        "properties": properties or [],
        "credentials": credentials or [],
    }


@pytest.fixture()
def catalog():
    return NodeCatalog.from_entries(
        [
            node("manualTrigger", inputs=[]),
            node("set", version=[1, 2, 3]),
            node("if", version=[2, 2.3], outputs=["main", "main"]),
            node("switch", version=[3, 3.4], outputs="={{ dynamic }}"),
            node("merge", version=[3, 3.2], inputs="={{ dynamic }}"),
            node("splitInBatches", version=[2, 3], outputs=["main", "main"]),
            node(
                "executeWorkflow",
                version=[1, 1.3],
                properties=[
                    {
                        "name": "workflowId",
                        "displayName": "Workflow",
                        "type": "workflowSelector",
                        "required": True,
                        "default": "",
                        "displayOptions": {"show": {"source": ["database"]}},
                    }
                ],
            ),
            node(
                "gmail",
                version=[2, 2.2],
                properties=[
                    {"name": "resource", "type": "options", "default": "message", "options": [{"name": "Message", "value": "message"}]},
                    {"name": "operation", "type": "options", "default": "send", "options": [{"name": "Send", "value": "send"}]},
                    {
                        "name": "sendTo",
                        "type": "string",
                        "required": True,
                        "default": "",
                        "displayOptions": {"show": {"resource": ["message"], "operation": ["send"]}},
                    },
                ],
                credentials=[
                    {
                        "name": "gmailOAuth2",
                        "required": True,
                        "displayOptions": {"show": {"authentication": ["oAuth2"]}},
                    }
                ],
            ),
            node(
                "slack",
                version=[2, 2.5],
                credentials=[{"name": "slackApi", "required": True}],
            ),
            node("dynamic", inputs="={{ dynamic }}", outputs="={{ dynamic }}"),
        ],
        fingerprint={"n8n_version": "2.32.5", "package_lock_sha256": "fixture"},
    )


@pytest.fixture()
def engine(catalog):
    def credential(alias, credential_type, _context=None):
        if alias == "gmail-main" and credential_type == "gmailOAuth2":
            return {"id": "cred-opaque-1", "name": alias}
        return None

    return GraphAuthoringEngine(
        catalog,
        credential_resolver=credential,
        protected_workflows={
            "workbench.agent": {"workflow_id": "protected-agent", "name": "Workbench Agent Bridge v1"},
            "workbench.approval": {"workflow_id": "protected-approval", "name": "Workbench Approval Bridge v1"},
        },
    )


def basic_spec():
    return {
        "schema": "workflow_spec.v1",
        "name": "Deterministic",
        "nodes": [
            {"key": "start", "type": "manualTrigger", "name": "Start"},
            {"key": "edit", "type": "set", "name": "Edit"},
        ],
        "edges": [{"from": "start", "to": "edit"}],
    }


def test_catalog_search_is_sanitized_and_resolves_short_names(catalog):
    assert catalog.get("set")["type"] == "n8n-nodes-base.set"
    assert catalog.get("set")["selected_version"] == 3
    result = catalog.search("gmail", limit=5)
    assert result == [
        {
            "type": "n8n-nodes-base.gmail",
            "name": "gmail",
            "display_name": "gmail",
            "description": "Fixture gmail",
            "group": ["transform"],
            "versions": [2.0, 2.2],
            "default_version": 2.2,
            "dynamic_inputs": False,
            "dynamic_outputs": False,
            "credential_types": ["gmailOAuth2"],
        }
    ]
    assert "properties" not in result[0]


def test_pinned_runtime_catalog_fingerprints_without_copying_metadata():
    if not DEFAULT_N8N_RUNTIME_ROOT.exists():
        pytest.skip("Pinned local runtime is not installed")
    catalog = NodeCatalog.from_runtime(DEFAULT_N8N_RUNTIME_ROOT)
    assert catalog.fingerprint["n8n_version"] == "2.32.5"
    assert catalog.fingerprint["n8n_nodes_base_version"] == "2.32.3"
    assert len(catalog.fingerprint["package_lock_sha256"]) == 64
    assert len(catalog.digest) == 64
    assert len(catalog) >= 400
    assert catalog.get("n8n-nodes-base.if") is not None


def test_pinned_catalog_uses_declared_default_version_and_not_latest():
    if not DEFAULT_N8N_RUNTIME_ROOT.exists():
        pytest.skip("Pinned local runtime is not installed")
    catalog = NodeCatalog.from_runtime(DEFAULT_N8N_RUNTIME_ROOT)
    airtop = catalog.get("n8n-nodes-base.airtop")
    assert airtop is not None
    assert airtop["supported_versions"] == [1.0, 1.1]
    assert airtop["selected_version"] == 1.0


def test_pinned_catalog_resource_locator_defaults_do_not_hide_required_inputs():
    if not DEFAULT_N8N_RUNTIME_ROOT.exists():
        pytest.skip("Pinned local runtime is not installed")
    catalog = NodeCatalog.from_runtime(DEFAULT_N8N_RUNTIME_ROOT)

    def credential(alias, credential_type, _context=None):
        if alias == "teams-main" and credential_type == "microsoftTeamsOAuth2Api":
            return {"id": "credential-opaque", "name": alias, "type": credential_type}
        return None

    result = GraphAuthoringEngine(catalog, credential_resolver=credential).materialize(
        {
            "schema": "workflow_spec.v1",
            "name": "Missing Teams fields",
            "nodes": [
                {
                    "key": "start",
                    "type": "n8n-nodes-base.microsoftTeamsTrigger",
                    "name": "Teams",
                    "parameters": {
                        "event": "newChannelMessage",
                        "authentication": "microsoftTeamsOAuth2Api",
                    },
                    "credential_aliases": {
                        "microsoftTeamsOAuth2Api": "teams-main",
                    },
                }
            ],
            "edges": [],
        },
        context={"project_id": "project-a", "session_id": "session-a"},
    )

    assert result.status == "needs_input"
    missing = {(issue.code, issue.path) for issue in result.issues}
    assert ("PARAMETER_REQUIRED", "parameters.teamId") in missing
    assert ("PARAMETER_REQUIRED", "parameters.channelId") in missing


def test_lazy_catalog_is_non_throwing_at_startup_and_fail_closed(tmp_path):
    lazy = LazyNodeCatalog(tmp_path / "missing")
    assert lazy.status() == {"ready": False, "state": "not_loaded"}
    status = lazy.status(probe=True)
    assert status["ready"] is False
    assert status["error"]["code"] == "N8N_CATALOG_READ_FAILED"
    with pytest.raises(GraphAuthoringError):
        lazy.require()


def test_lazy_engine_constructor_does_not_load_catalog_and_first_use_fails_closed(tmp_path):
    lazy_catalog = LazyNodeCatalog(tmp_path / "missing")
    engine = LazyGraphAuthoringEngine(lazy_catalog)
    assert engine.status() == {"ready": False, "state": "not_loaded"}
    with pytest.raises(GraphAuthoringError) as error:
        engine.materialize(basic_spec())
    assert error.value.code == "N8N_CATALOG_READ_FAILED"


def test_compile_is_deterministic_and_builds_n8n_connections(engine):
    first = engine.materialize(basic_spec())
    second = engine.materialize(basic_spec())
    assert first.status == "graph_ready"
    assert first.validation_status == "ready"
    assert first.graph_digest == second.graph_digest
    assert first.workflow == second.workflow
    assert first.workflow["nodes"][0]["id"] == "d56107f8-7a92-5e6c-8ede-f3ddbb5336c7"
    assert first.workflow["nodes"][1]["position"][0] > first.workflow["nodes"][0]["position"][0]
    assert first.workflow["connections"] == {
        "Start": {"main": [[{"node": "Edit", "type": "main", "index": 0}]]}
    }


def test_duplicate_display_names_are_made_unique(engine):
    spec = basic_spec()
    spec["nodes"][1]["name"] = "Start"
    result = engine.materialize(spec)
    assert [item["name"] for item in result.workflow["nodes"]] == ["Start", "Start 2"]
    assert result.status == "graph_ready"


def test_reserved_workbench_nodes_compile_to_protected_subworkflows(catalog):
    engine = GraphAuthoringEngine(
        catalog,
        credential_resolver=lambda alias, credential_type, _context=None: (
            {"id": "cred-opaque-1", "name": alias}
            if alias == "gmail-main" and credential_type == "gmailOAuth2"
            else None
        ),
        binding_resolver=lambda kind, _raw, _context: (
            {"binding_id": "agent-binding-opaque"}
            if kind == "workbench.agent" else None
        ),
        protected_workflows={
            "workbench.agent": {"workflow_id": "protected-agent", "name": "Workbench Agent Bridge v1"},
            "workbench.approval": {"workflow_id": "protected-approval", "name": "Workbench Approval Bridge v1"},
        },
        revision_token_factory=lambda: "wbr_server_minted_revision_token_001",
    )
    spec = {
        "schema": "workflow_spec.v1",
        "name": "Agent review",
        "nodes": [
            {"key": "start", "type": "manualTrigger"},
            {"key": "agent", "type": "workbench.agent"},
            {"key": "approval", "type": "workbench.approval"},
            {
                "key": "mail",
                "type": "gmail",
                "parameters": {
                    "resource": "message", "operation": "send",
                    "authentication": "oAuth2", "sendTo": "allowed@example.test",
                },
                "credential_aliases": {"gmailOAuth2": "gmail-main"},
            },
        ],
        "edges": [
            {"from": "start", "to": "agent"},
            {"from": "agent", "to": "approval"},
            {"from": "approval", "to": "mail"},
        ],
    }
    result = engine.materialize(spec)
    assert result.status == "graph_ready"
    agent = result.workflow["nodes"][1]
    approval = result.workflow["nodes"][2]
    assert agent["type"] == "n8n-nodes-base.executeWorkflow"
    assert agent["parameters"]["workflowId"]["value"] == "protected-agent"
    agent_inputs = agent["parameters"]["workflowInputs"]["value"]
    assert agent_inputs["agent_binding_id"] == "agent-binding-opaque"
    assert agent_inputs["workflow_id"] == "={{$workflow.id}}"
    assert agent_inputs["workflow_revision"] == "wbr_server_minted_revision_token_001"
    assert "$workflow.activeVersionId" not in json.dumps(result.workflow)
    assert agent_inputs["node_id"] == agent["id"]
    assert agent_inputs["request_id"] == "={{$execution.id + '-' + $itemIndex}}"
    assert agent_inputs["input"] == "={{$json}}"
    assert approval["parameters"]["workflowId"]["value"] == "protected-approval"
    assert approval["parameters"]["workflowInputs"]["value"]["approval_binding_id"].startswith("wba_")
    assert (
        approval["parameters"]["workflowInputs"]["value"]["workflow_revision"]
        == agent_inputs["workflow_revision"]
    )
    assert result.binding_claims == [
        {
            "kind": "workbench.agent",
            "binding_id": "agent-binding-opaque",
            "node_id": agent["id"],
            "node_name": agent["name"],
            "workflow_revision": "wbr_server_minted_revision_token_001",
            "provisional": True,
        },
    ]


def test_binding_resolver_uses_only_authoritative_context_and_runs_once(catalog):
    calls = []

    def resolve(kind, raw, context):
        calls.append((kind, raw, context))
        return {
            "binding_id": f"binding-{context['project_id']}",
            "output_schema": {"type": "object", "properties": {"body": {"type": "string"}}},
        }

    engine = GraphAuthoringEngine(
        catalog,
        binding_resolver=resolve,
        protected_workflows={"workbench.agent": {"workflow_id": "protected-agent"}},
        revision_token_factory=lambda: "wbr_authoritative_revision_token_001",
    )
    spec = {
        "schema": "workflow_spec.v1",
        "project_id": "model-controlled-project",
        "name": "Context",
        "nodes": [
            {"key": "start", "type": "manualTrigger"},
            {"key": "agent", "type": "workbench.agent", "binding_id": "model-controlled-binding"},
        ],
        "edges": [{"from": "start", "to": "agent"}],
    }
    result = engine.materialize(spec, context={"project_id": "project-authoritative", "session_id": "session-a"})
    assert result.status == "graph_ready"
    assert len(calls) == 1
    assert calls[0][1].get("binding_id") is None
    assert calls[0][2]["project_id"] == "project-authoritative"
    assert result.binding_claims[0]["binding_id"] == "binding-project-authoritative"


def test_task_runtime_shaped_binding_keeps_claim_private_and_compiles_opaque_id(catalog):
    def resolve(_kind, _raw, _context):
        return {
            "binding_claim_id": "claim-server-only",
            "agent_binding_id": "agent-binding-opaque",
            "output_schema": {"type": "object", "properties": {}},
        }

    engine = GraphAuthoringEngine(
        catalog,
        binding_resolver=resolve,
        protected_workflows={"workbench.agent": {"workflow_id": "protected-agent"}},
        revision_token_factory=lambda: "wbr_private_claim_revision_token_001",
    )
    result = engine.materialize(
        {
            "schema": "workflow_spec.v1",
            "name": "Protected binding",
            "nodes": [
                {"key": "start", "type": "manualTrigger"},
                {"key": "agent", "type": "workbench.agent"},
            ],
            "edges": [{"from": "start", "to": "agent"}],
        },
        context={"project_id": "project-authoritative"},
    )
    assert result.status == "graph_ready"
    parameters = result.workflow["nodes"][1]["parameters"]
    assert parameters["workflowInputs"]["value"]["agent_binding_id"] == "agent-binding-opaque"
    assert "claim-server-only" not in json.dumps(result.workflow)
    assert result.binding_claims == [
        {
            "kind": "workbench.agent",
            "binding_claim_id": "claim-server-only",
            "binding_id": "agent-binding-opaque",
            "node_id": result.workflow["nodes"][1]["id"],
            "workflow_revision": "wbr_private_claim_revision_token_001",
            "provisional": True,
        }
    ]


def test_missing_protected_binding_needs_input(engine):
    spec = {
        "schema": "workflow_spec.v1",
        "name": "Missing binding",
        "nodes": [
            {"key": "start", "type": "manualTrigger"},
            {"key": "agent", "type": "workbench.agent"},
        ],
        "edges": [{"from": "start", "to": "agent"}],
    }
    result = engine.materialize(spec)
    assert result.status == "needs_input"
    assert "AGENT_BINDING_REQUIRED" in {item.code for item in result.issues}
    assert result.graph_digest is None


def test_required_parameter_and_credential_alias_are_resolved(engine):
    spec = {
        "schema": "workflow_spec.v1",
        "name": "Mail",
        "nodes": [
            {"key": "start", "type": "manualTrigger"},
            {"key": "approval", "type": "workbench.approval"},
            {
                "key": "mail",
                "type": "gmail",
                "parameters": {
                    "resource": "message",
                    "operation": "send",
                    "authentication": "oAuth2",
                    "sendTo": "allowed@example.test",
                },
                "credential_aliases": {"gmailOAuth2": "gmail-main"},
            },
        ],
        "edges": [
            {"from": "start", "to": "approval"},
            {"from": "approval", "to": "mail"},
        ],
    }
    result = engine.materialize(spec)
    assert result.status == "graph_ready"
    assert result.workflow["nodes"][2]["credentials"] == {
        "gmailOAuth2": {"id": "cred-opaque-1", "name": "gmail-main"}
    }


def test_missing_required_parameter_and_credential_fail_before_draft(engine):
    spec = {
        "schema": "workflow_spec.v1",
        "name": "Mail",
        "nodes": [
            {"key": "start", "type": "manualTrigger"},
            {"key": "approval", "type": "workbench.approval"},
            {
                "key": "mail",
                "type": "gmail",
                "parameters": {"resource": "message", "operation": "send", "authentication": "oAuth2"},
            },
        ],
        "edges": [
            {"from": "start", "to": "approval"},
            {"from": "approval", "to": "mail"},
        ],
    }
    result = engine.materialize(spec)
    assert result.status == "needs_input"
    assert {"PARAMETER_REQUIRED", "CREDENTIAL_REQUIRED"} <= {
        item.code for item in result.issues
    }


def test_agent_output_fields_are_verified_before_mapping_to_gmail(catalog):
    binding_schema = {
        "type": "object",
        "properties": {
            "recipient": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["recipient", "subject", "body"],
    }

    def binding(_kind, _raw=None):
        return {"binding_id": "agent-mail-binding", "output_schema": binding_schema}

    engine = GraphAuthoringEngine(
        catalog,
        credential_resolver=lambda alias, credential_type, _context=None: (
            {"id": "cred-opaque-1", "name": alias}
            if alias == "gmail-main" and credential_type == "gmailOAuth2"
            else None
        ),
        binding_resolver=binding,
        protected_workflows={
            "workbench.agent": {"workflow_id": "protected-agent"},
            "workbench.approval": {"workflow_id": "protected-approval"},
        },
    )
    target_schema = copy_schema = json.loads(json.dumps(binding_schema))
    spec = {
        "schema": "workflow_spec.v1",
        "name": "Agent mail mapping",
        "nodes": [
            {"key": "start", "type": "manualTrigger"},
            {"key": "agent", "type": "workbench.agent"},
            {
                "key": "approval",
                "type": "workbench.approval",
                "input_schema": binding_schema,
                "output_schema": binding_schema,
            },
            {
                "key": "mail",
                "type": "gmail",
                "input_schema": target_schema,
                "parameters": {
                    "resource": "message",
                    "operation": "send",
                    "authentication": "oAuth2",
                    "sendTo": "={{ $json.recipient }}",
                    "subject": "={{ $json.subject }}",
                    "message": "={{ $json['body'] }}",
                },
                "credential_aliases": {"gmailOAuth2": "gmail-main"},
            },
        ],
        "edges": [
            {"from": "start", "to": "agent"},
            {
                "from": "agent",
                "to": "approval",
                "field_mappings": [
                    {"from": "recipient", "to": "recipient"},
                    {"from": "subject", "to": "subject"},
                    {"from": "body", "to": "body"},
                ],
            },
            {
                "from": "approval",
                "to": "mail",
                "field_mappings": [
                    {"from": "recipient", "to": "recipient"},
                    {"from": "subject", "to": "subject"},
                    {"from": "body", "to": "body"},
                ],
            },
        ],
    }
    result = engine.materialize(spec)
    assert result.status == "graph_ready"
    assert result.graph_preview["data_contracts"]["nodes"][1]["output_fields"] == ["body", "recipient", "subject"]
    assert result.graph_preview["data_contracts"]["edges"][1]["field_mappings"][0] == {
        "from": "recipient",
        "to": "recipient",
    }
    invalid = json.loads(json.dumps(spec))
    invalid["edges"][1]["field_mappings"][0]["from"] = "unknown_recipient"
    rejected = engine.materialize(invalid)
    assert rejected.status == "needs_input"
    assert "SOURCE_FIELD_UNAVAILABLE" in {item.code for item in rejected.issues}


def test_external_write_expression_without_mapping_needs_input(engine):
    spec = {
        "schema": "workflow_spec.v1",
        "name": "Unverified mail mapping",
        "nodes": [
            {"key": "start", "type": "manualTrigger"},
            {"key": "approval", "type": "workbench.approval"},
            {
                "key": "mail",
                "type": "gmail",
                "parameters": {
                    "resource": "message",
                    "operation": "send",
                    "authentication": "oAuth2",
                    "sendTo": "={{ $json.recipient }}",
                },
                "credential_aliases": {"gmailOAuth2": "gmail-main"},
            },
        ],
        "edges": [
            {"from": "start", "to": "approval"},
            {"from": "approval", "to": "mail"},
        ],
    }
    result = engine.materialize(spec)
    assert result.status == "needs_input"
    assert "DATA_FIELD_MAPPING_REQUIRED" in {item.code for item in result.issues}


def test_mapping_rejects_missing_source_and_target_fields(engine):
    spec = {
        "schema": "workflow_spec.v1",
        "name": "Bad mapping",
        "nodes": [
            {
                "key": "source",
                "type": "manualTrigger",
                "output_schema": {"type": "object", "properties": {"known": {"type": "string"}}},
            },
            {
                "key": "target",
                "type": "set",
                "input_schema": {"type": "object", "properties": {"accepted": {"type": "string"}}},
            },
        ],
        "edges": [
            {
                "from": "source",
                "to": "target",
                "field_mappings": [{"from": "missing", "to": "also_missing"}],
            }
        ],
    }
    result = engine.materialize(spec)
    assert result.status == "needs_input"
    assert {"SOURCE_FIELD_UNAVAILABLE", "TARGET_FIELD_UNAVAILABLE"} <= {item.code for item in result.issues}


def test_if_and_switch_port_adapters_validate_branches(engine):
    spec = {
        "schema": "workflow_spec.v1",
        "name": "Branches",
        "nodes": [
            {"key": "start", "type": "manualTrigger"},
            {"key": "if", "type": "if"},
            {"key": "yes", "type": "set"},
            {"key": "no", "type": "set"},
        ],
        "edges": [
            {"from": "start", "to": "if"},
            {"from": "if", "output_index": 0, "to": "yes"},
            {"from": "if", "output_index": 1, "to": "no"},
        ],
    }
    result = engine.materialize(spec)
    assert result.status == "graph_ready"
    spec["edges"][-1]["output_index"] = 2
    invalid = engine.materialize(spec)
    assert invalid.status == "blocked"
    assert "EDGE_OUTPUT_PORT_INVALID" in {item.code for item in invalid.issues}


def test_dynamic_unknown_ports_require_confirmation(engine):
    spec = {
        "schema": "workflow_spec.v1",
        "name": "Dynamic",
        "nodes": [{"key": "a", "type": "dynamic"}, {"key": "b", "type": "set"}],
        "edges": [{"from": "a", "to": "b"}],
    }
    result = engine.materialize(spec)
    assert result.status == "needs_input"
    assert "DYNAMIC_PORTS_UNRESOLVED" in {item.code for item in result.issues}


def test_unreviewed_cycle_is_blocked_but_loop_adapter_pattern_is_allowed(engine):
    invalid = {
        "schema": "workflow_spec.v1",
        "name": "Cycle",
        "nodes": [{"key": "a", "type": "set"}, {"key": "b", "type": "set"}],
        "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
    }
    blocked = engine.materialize(invalid)
    assert blocked.status == "blocked"
    assert "GRAPH_CYCLE_UNREVIEWED" in {item.code for item in blocked.issues}

    reviewed = {
        "schema": "workflow_spec.v1",
        "name": "Loop",
        "nodes": [
            {"key": "start", "type": "manualTrigger"},
            {"key": "loop", "type": "splitInBatches"},
            {"key": "body", "type": "set"},
            {"key": "done", "type": "set"},
        ],
        "edges": [
            {"from": "start", "to": "loop"},
            {"from": "loop", "output_index": 1, "to": "body"},
            {"from": "body", "to": "loop"},
            {"from": "loop", "output_index": 0, "to": "done"},
        ],
    }
    assert engine.materialize(reviewed).status == "graph_ready"


def test_orphan_duplicate_and_dangling_graphs_are_blocked(engine):
    spec = basic_spec()
    spec["nodes"].append({"key": "orphan", "type": "set"})
    result = engine.materialize(spec)
    assert result.status == "blocked"
    assert {"NODE_ORPHAN", "GRAPH_DISCONNECTED"} <= {item.code for item in result.issues}


def test_dangerous_expressions_are_blocked(engine):
    spec = basic_spec()
    spec["nodes"][1]["parameters"] = {"value": "={{ $env.WORKBENCH_SECRET }}"}
    result = engine.materialize(spec)
    assert result.status == "blocked"
    assert "EXPRESSION_ENV_ACCESS" in {item.code for item in result.issues}


def test_trigger_and_all_path_runtime_approval_are_mandatory(engine):
    no_trigger = engine.materialize(
        {
            "schema": "workflow_spec.v1",
            "name": "No trigger",
            "nodes": [{"key": "edit", "type": "set"}],
            "edges": [],
        }
    )
    assert no_trigger.status == "needs_input"
    assert "TRIGGER_REQUIRED" in {item.code for item in no_trigger.issues}

    bypass = engine.materialize(
        {
            "schema": "workflow_spec.v1",
            "name": "Approval bypass",
            "nodes": [
                {"key": "start", "type": "manualTrigger"},
                {"key": "approval", "type": "workbench.approval"},
                {
                    "key": "mail",
                    "type": "gmail",
                    "parameters": {
                        "resource": "message", "operation": "send",
                        "authentication": "oAuth2", "sendTo": "allowed@example.test",
                    },
                    "credential_aliases": {"gmailOAuth2": "gmail-main"},
                },
            ],
            "edges": [
                {"from": "start", "to": "approval"},
                {"from": "approval", "to": "mail"},
                {"from": "start", "to": "mail"},
            ],
        }
    )
    assert bypass.status == "blocked"
    assert "EXTERNAL_WRITE_APPROVAL_BYPASS" in {item.code for item in bypass.issues}


def test_slack_write_without_reviewed_adapter_fails_closed(catalog):
    engine = GraphAuthoringEngine(
        catalog,
        credential_resolver=lambda alias, credential_type, _context=None: (
            {"id": "cred-slack", "name": alias}
            if alias == "slack-main" and credential_type == "slackApi"
            else None
        ),
    )
    result = engine.materialize(
        {
            "schema": "workflow_spec.v1",
            "name": "Slack write bypass",
            "nodes": [
                {"key": "start", "name": "Start", "type": "manualTrigger"},
                {
                    "key": "send",
                    "name": "Send Slack",
                    "type": "slack",
                    "parameters": {
                        "resource": "message",
                        "operation": "post",
                        "text": "hello",
                    },
                    "credential_aliases": {"slackApi": "slack-main"},
                },
            ],
            "edges": [{"from": "start", "to": "send"}],
        }
    )
    assert result.status != "graph_ready"
    assert "EXTERNAL_ACTION_CLASSIFICATION_REQUIRED" in {
        issue.code for issue in result.issues
    }


@pytest.mark.parametrize(
    ("node_type", "operation"),
    [
        ("slack", "delete"),
        ("microsoftOutlook", "send"),
        ("microsoftOutlook", "reply"),
        ("microsoftOutlook", "delete"),
        ("googleSheets", "append"),
        ("googleSheets", "update"),
        ("googleSheets", "delete"),
        ("postgres", "insert"),
        ("postgres", "update"),
        ("postgres", "upsert"),
        ("postgres", "delete"),
    ],
)
def test_unadapted_service_writes_are_classified_external(node_type, operation):
    raw = {
        "type": node_type,
        "parameters": {"operation": operation},
        "credential_aliases": {"credential": "service-main"},
    }
    assert GraphAuthoringEngine._is_external_write(raw) is True
    assert (
        GraphAuthoringEngine._external_action_classification(raw)
        == "unadapted_write"
    )


def test_reviewed_read_only_service_and_credential_trigger_still_compile(catalog):
    resolver = lambda alias, credential_type, _context=None: {
        "id": f"cred-{credential_type}", "name": alias
    }
    service_engine = GraphAuthoringEngine(catalog, credential_resolver=resolver)
    read_result = service_engine.materialize(
        {
            "schema": "workflow_spec.v1",
            "name": "Read Slack",
            "nodes": [
                {"key": "start", "type": "manualTrigger"},
                {
                    "key": "read",
                    "type": "slack",
                    "parameters": {"resource": "message", "operation": "search"},
                    "credential_aliases": {"slackApi": "slack-main"},
                },
            ],
            "edges": [{"from": "start", "to": "read"}],
        }
    )
    assert read_result.status == "graph_ready"

    trigger_catalog = NodeCatalog.from_entries(
        [
            node(
                "serviceTrigger",
                inputs=[],
                credentials=[{"name": "serviceOAuth2", "required": True}],
            )
        ],
        fingerprint={"n8n_version": "2.32.5", "package_lock_sha256": "fixture"},
    )
    trigger_result = GraphAuthoringEngine(
        trigger_catalog, credential_resolver=resolver
    ).materialize(
        {
            "schema": "workflow_spec.v1",
            "name": "Credential trigger",
            "nodes": [
                {
                    "key": "trigger",
                    "type": "serviceTrigger",
                    "credential_aliases": {"serviceOAuth2": "service-main"},
                }
            ],
            "edges": [],
        }
    )
    assert trigger_result.status == "graph_ready"


def test_obvious_write_without_credentials_fails_closed(catalog):
    result = GraphAuthoringEngine(catalog).materialize(
        {
            "schema": "workflow_spec.v1",
            "name": "Uncredentialed write",
            "nodes": [
                {"key": "start", "type": "manualTrigger"},
                {
                    "key": "write",
                    "type": "set",
                    "parameters": {"operation": "delete"},
                },
            ],
            "edges": [{"from": "start", "to": "write"}],
        }
    )
    assert result.status != "graph_ready"
    assert "EXTERNAL_ACTION_CLASSIFICATION_REQUIRED" in {
        issue.code for issue in result.issues
    }


def test_generic_execute_subworkflow_fails_closed_but_protected_pipeline_remains_ready(catalog):
    generic = GraphAuthoringEngine(catalog).materialize(
        {
            "schema": "workflow_spec.v1",
            "name": "Hidden subworkflow",
            "nodes": [
                {"key": "start", "type": "manualTrigger"},
                {
                    "key": "hidden",
                    "type": "executeWorkflow",
                    "parameters": {
                        "source": "database",
                        "workflowId": {
                            "__rl": True,
                            "value": "unreviewed-workflow",
                            "mode": "list",
                        },
                    },
                },
            ],
            "edges": [{"from": "start", "to": "hidden"}],
        }
    )
    assert generic.status != "graph_ready"
    assert "EXTERNAL_ACTION_CLASSIFICATION_REQUIRED" in {
        issue.code for issue in generic.issues
    }

    def credential(alias, credential_type, _context=None):
        if alias == "gmail-main" and credential_type == "gmailOAuth2":
            return {"id": "cred-gmail", "name": alias}
        return None

    def binding(kind, _raw, context):
        if kind == "workbench.agent":
            return {
                "binding_claim_id": "agent-claim",
                "agent_binding_id": "agent-binding",
                "output_schema": {"type": "object", "properties": {}},
            }
        manifest = next(iter(context["_approval_action_manifests"].values()))
        return {
            "binding_claim_id": "approval-claim",
            "approval_binding_id": "approval-binding",
            "manifest_digest": hashlib.sha256(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        }

    protected = GraphAuthoringEngine(
        catalog,
        credential_resolver=credential,
        binding_resolver=binding,
        protected_workflows={
            "workbench.agent": {"workflow_id": "protected-agent"},
            "workbench.approval": {"workflow_id": "protected-approval"},
        },
    ).materialize(
        {
            "schema": "workflow_spec.v1",
            "name": "Protected pipeline",
            "nodes": [
                {"key": "start", "type": "manualTrigger"},
                {"key": "agent", "type": "workbench.agent"},
                {"key": "approval", "type": "workbench.approval"},
                {
                    "key": "mail",
                    "type": "gmail",
                    "parameters": {
                        "resource": "message",
                        "operation": "send",
                        "authentication": "oAuth2",
                        "sendTo": "allowed@example.test",
                    },
                    "credential_aliases": {"gmailOAuth2": "gmail-main"},
                },
            ],
            "edges": [
                {"from": "start", "to": "agent"},
                {"from": "agent", "to": "approval"},
                {"from": "approval", "to": "mail"},
            ],
        },
        context={"project_id": "project-one"},
    )
    assert protected.status == "graph_ready"


def test_approval_manifest_binds_exact_downstream_and_unreviewed_expression_is_blocked(engine):
    spec = {
        "schema": "workflow_spec.v1",
        "name": "Manifest",
        "nodes": [
            {"key": "start", "type": "manualTrigger"},
            {"key": "approval", "type": "workbench.approval"},
            {
                "key": "mail", "type": "gmail",
                "parameters": {
                    "resource": "message", "operation": "send",
                    "authentication": "oAuth2", "sendTo": "allowed@example.test",
                },
                "credential_aliases": {"gmailOAuth2": "gmail-main"},
            },
        ],
        "edges": [
            {"from": "start", "to": "approval"},
            {"from": "approval", "to": "mail"},
        ],
    }
    result = engine.materialize(spec)
    assert result.status == "graph_ready"
    approval = result.workflow["nodes"][1]
    values = approval["parameters"]["workflowInputs"]["value"]
    assert values["approval_token"] == (
        f"{values['approval_binding_id']}:{values['manifest_digest']}"
    )
    assert "payload:$json" in values["input"]

    tampered = json.loads(json.dumps(result.workflow))
    tampered["nodes"][2]["parameters"]["sendTo"] = "other@example.test"
    validation = engine.validate(tampered)
    assert validation.status == "blocked"
    assert "APPROVAL_ACTION_MANIFEST_MISMATCH" in {
        item.code for item in validation.issues
    }

    injection = json.loads(json.dumps(spec))
    injection["nodes"][2]["parameters"]["sendTo"] = (
        "={{ $('Agent').item.json.recipient }}"
    )
    rejected = engine.materialize(injection)
    assert rejected.status == "blocked"
    assert "EXTERNAL_EXPRESSION_UNREVIEWED" in {
        item.code for item in rejected.issues
    }


def test_protected_identity_patch_is_immutable_and_multi_add_binds_after_final_graph(catalog):
    def credential(alias, credential_type, _context=None):
        if alias == "gmail-main" and credential_type == "gmailOAuth2":
            return {"id": "cred-opaque-1", "name": alias}
        return None

    def binding(kind, _raw, context):
        if kind == "workbench.agent":
            return {"binding_id": "agent-existing"}
        manifest = next(iter(context["_approval_action_manifests"].values()))
        digest = hashlib.sha256(
            json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        return {
            "binding_claim_id": "approval-claim-private",
            "approval_binding_id": "approval-binding-private",
            "manifest_digest": digest,
            "node_id": manifest["approval_node_id"],
        }

    authored = GraphAuthoringEngine(
        catalog,
        credential_resolver=credential,
        binding_resolver=binding,
        protected_workflows={
            "workbench.agent": {"workflow_id": "protected-agent"},
            "workbench.approval": {"workflow_id": "protected-approval"},
        },
    )
    base = authored.materialize(
        {
            "schema": "workflow_spec.v1", "name": "Patch secure",
            "nodes": [
                {"key": "start", "type": "manualTrigger", "name": "Start"},
                {"key": "agent", "type": "workbench.agent", "name": "Agent"},
            ],
            "edges": [{"from": "start", "to": "agent"}],
        }
    ).workflow
    agent = base["nodes"][1]
    immutable = authored.apply_patch(
        base,
        {
            "schema": "workflow_patch.v1",
            "operations": [{
                "op": "update", "target": agent["id"],
                "changes": {"parameters": {"workflowId": {"value": "evil"}}},
            }],
        },
    )
    assert immutable.status == "blocked"
    assert immutable.workflow["nodes"][1]["parameters"] == agent["parameters"]
    assert "PATCH_PROTECTED_IDENTITY_IMMUTABLE" in {
        item.code for item in immutable.issues
    }

    patched = authored.apply_patch(
        base,
        {
            "schema": "workflow_patch.v1",
            "operations": [
                {"op": "add", "value": {
                    "key": "approval", "name": "Approval",
                    "type": "workbench.approval",
                }},
                {"op": "add", "value": {
                    "key": "mail", "name": "Mail", "type": "gmail",
                    "parameters": {
                        "resource": "message", "operation": "send",
                        "authentication": "oAuth2", "sendTo": "allowed@example.test",
                    },
                    "credential_aliases": {"gmailOAuth2": "gmail-main"},
                }},
                {"op": "connect", "from": "Agent", "to": "Approval"},
                {"op": "connect", "from": "Approval", "to": "Mail"},
            ],
        },
        context={"project_id": "project-one"},
    )
    assert patched.status == "graph_ready"
    assert patched.binding_claims[0]["kind"] == "workbench.approval"
    assert len(patched.binding_claims[0]["manifest_digest"]) == 64


def test_patch_preserves_untouched_nodes_and_reports_parameter_and_edge_diff(engine):
    base = engine.materialize(basic_spec()).workflow
    untouched = json.loads(json.dumps(base["nodes"][0]))
    edit_id = base["nodes"][1]["id"]
    patch = {
        "schema": "workflow_patch.v1",
        "operations": [
            {"op": "update", "target": edit_id, "changes": {"parameters": {"mode": "manual"}}},
            {
                "op": "add",
                "value": {
                    "key": "final",
                    "name": "Final",
                    "type": "set",
                    "parameters": {},
                    "position": [800, 180],
                },
            },
            {"op": "connect", "from": "Edit", "to": "Final"},
        ],
    }
    result = engine.apply_patch(base, patch)
    assert result.status == "graph_ready"
    assert result.workflow["nodes"][0] == untouched
    changed = result.diff["nodes"]["changed"]
    parameter_change = changed[0]["changes"]["parameters"][0]
    assert parameter_change["path"] == "mode"
    assert parameter_change["before_present"] is False
    assert parameter_change["after_present"] is True
    assert parameter_change["before_digest"] is None
    assert len(parameter_change["after_digest"]) == 64
    assert "manual" not in json.dumps(result.diff)
    assert result.diff["connections"]["added"][0]["to"] == "Final"


def test_patch_add_agent_uses_server_revision_token_and_private_claim(catalog):
    revision_tokens = iter(
        ["wbr_base_revision_token_0001", "wbr_patch_revision_token_0002"]
    )
    engine = GraphAuthoringEngine(
        catalog,
        binding_resolver=lambda _kind, _raw, _context: {
            "binding_claim_id": "claim-for-patched-agent",
            "agent_binding_id": "binding-for-patched-agent",
            "output_schema": {"type": "object", "properties": {}},
        },
        protected_workflows={
            "workbench.agent": {"workflow_id": "protected-agent"}
        },
        revision_token_factory=lambda: next(revision_tokens),
    )
    base = engine.materialize(
        {
            "schema": "workflow_spec.v1",
            "name": "Patch Agent",
            "nodes": [{"key": "start", "name": "Start", "type": "manualTrigger"}],
            "edges": [],
        }
    ).workflow
    result = engine.apply_patch(
        base,
        {
            "schema": "workflow_patch.v1",
            "operations": [
                {
                    "op": "add",
                    "value": {
                        "key": "agent",
                        "name": "Agent",
                        "type": "workbench.agent",
                    },
                },
                {"op": "connect", "from": "Start", "to": "Agent"},
            ],
        },
        context={"project_id": "project-one"},
    )
    assert result.status == "graph_ready"
    inputs = result.workflow["nodes"][1]["parameters"]["workflowInputs"]["value"]
    assert inputs["workflow_revision"] == "wbr_patch_revision_token_0002"
    assert "$workflow.activeVersionId" not in json.dumps(result.workflow)
    assert result.binding_claims == [
        {
            "kind": "workbench.agent",
            "binding_claim_id": "claim-for-patched-agent",
            "binding_id": "binding-for-patched-agent",
            "node_id": result.workflow["nodes"][1]["id"],
            "workflow_revision": "wbr_patch_revision_token_0002",
            "provisional": True,
        }
    ]


def test_rename_and_remove_patch_rewrites_connections(engine):
    base = engine.materialize(basic_spec()).workflow
    renamed = engine.apply_patch(
        base,
        {"schema": "workflow_patch.v1", "operations": [{"op": "rename", "target": "Edit", "name": "Changed"}]},
    )
    assert renamed.status == "graph_ready"
    assert renamed.workflow["connections"]["Start"]["main"][0][0]["node"] == "Changed"
    removed = engine.apply_patch(
        renamed.workflow,
        {"schema": "workflow_patch.v1", "operations": [{"op": "remove", "target": "Changed"}]},
    )
    assert removed.status == "graph_ready"
    assert removed.workflow["connections"]["Start"]["main"][0] == []


def test_size_and_schema_fail_closed(engine):
    with pytest.raises(GraphAuthoringError) as error:
        engine.materialize({"schema": "wrong", "nodes": [], "edges": []})
    assert error.value.code == "N8N_SPEC_SCHEMA_INVALID"
    oversized = {"name": "x", "nodes": [], "connections": {}, "settings": {"padding": "x" * 251_000}}
    assert engine.validate(oversized).status == "blocked"
