from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import database
from n8n_agent_governance import N8nAgentGovernanceService, N8nApiBroker, N8nGovernanceError
from n8n_gmail_crypto import AesGcmContentCipher


class FakeBroker:
    def __init__(self):
        self.calls = []
        self.audit_calls = 0
        self.workflow_lookup_error = False
        self.workflow_active = {"safe": False, "unsafe": False, "protected": False}
        self.workflow_versions = {"safe": "v1", "unsafe": "v1", "protected": "v1"}
        self.workflow_nodes = {"safe": [], "unsafe": [], "protected": []}
        self._api_key_provider = lambda: "x" * 32

    def list_workflows(self):
        return [{"id": "safe", "name": "Safe", "active": False, "protected": False}]

    def get_workflow(self, workflow_id):
        if self.workflow_lookup_error:
            raise RuntimeError("lookup failed")
        names = {
            "safe": "Safe",
            "unsafe": "Unsafe",
            "protected": "Workbench Gmail Inbound v1",
        }
        if workflow_id not in names:
            raise RuntimeError("not found")
        name = names[workflow_id]
        return {
            "id": workflow_id,
            "name": name,
            "active": self.workflow_active[workflow_id],
            "updated_at": self.workflow_versions[workflow_id],
            "facts": {
                "name": name,
                "active": self.workflow_active[workflow_id],
                "nodes": self.workflow_nodes[workflow_id],
                "external_targets": [],
                "credential_aliases": [],
            },
            "protected": name.startswith("Workbench Gmail"),
        }

    def security_audit(self):
        self.audit_calls += 1
        return {"status": "clean", "findings": [], "verified": True}

    def execute(self, operation, payload, *, secret=None):
        self.calls.append((operation, payload, secret))
        return {"id": payload.get("workflow_id", "created"), "name": "Safe", "secret": "must-not-leak"}


@pytest.fixture()
def governed(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "workbench.db"))
    database.init_db()
    database.create_project("project_a", "A", str(tmp_path / "a"))
    database.create_project("project_b", "B", str(tmp_path / "b"))
    database.create_session("session_a", project_id="project_a")
    broker = FakeBroker()
    running = {"value": True}
    runner = {"value": False}
    service = N8nAgentGovernanceService(
        broker=broker,
        cipher=AesGcmContentCipher(lambda: b"k" * 32),
        n8n_running=lambda: running["value"],
        high_risk_runner_ready=lambda: runner["value"],
        integration_permission_check=lambda *_args, **_kwargs: {"decision": "allow"},
        boot_id="boot-a",
        _allow_legacy_raw_workflows_for_tests=True,
    )
    with database.get_db_conn() as conn:
        conn.executemany(
            """
            INSERT INTO n8n_agent_workflow_bindings(
                workflow_id,project_id,workflow_name,created_at,updated_at
            ) VALUES(?,?,?,?,?)
            """,
            [
                ("safe", "project_a", "Safe", "2026-08-13T00:00:00+00:00", "2026-08-13T00:00:00+00:00"),
                ("unsafe", "project_a", "Unsafe", "2026-08-13T00:00:00+00:00", "2026-08-13T00:00:00+00:00"),
            ],
        )
    return service, broker, running, runner


def proposal(**changes):
    value = {
        "project_id": "project_a",
        "session_id": "session_a",
        "operation": "create_draft",
        "payload": {"workflow": {"name": "Safe", "nodes": [{"type": "n8n-nodes-base.set"}]}},
        "diff": {"added": ["Set"]},
    }
    value.update(changes)
    return value


def test_runtime_callbacks_receive_policy_and_workflow_changes(governed):
    _, broker, running, runner = governed
    policy_changes = []
    workflow_changes = []
    service = N8nAgentGovernanceService(
        broker=broker,
        cipher=AesGcmContentCipher(lambda: b"k" * 32),
        n8n_running=lambda: running["value"],
        high_risk_runner_ready=lambda: runner["value"],
        boot_id="boot-callbacks",
        policy_change_callback=lambda project_id, reason: policy_changes.append(
            (project_id, reason)
        ),
        workflow_change_callback=lambda context: workflow_changes.append(dict(context)),
        integration_permission_check=lambda *_args, **_kwargs: {"decision": "allow"},
        _allow_legacy_raw_workflows_for_tests=True,
    )

    service.set_policy(
        "project_a",
        {"mode": "restricted", "elevation_policy": "smart"},
    )
    assert policy_changes == [("project_a", "policy_changed")]

    operation = service.create_operation(proposal())
    assert operation["status"] == "completed"
    assert workflow_changes == [
        {
            "project_id": "project_a",
            "session_id": "session_a",
            "run_id": None,
            "workflow_id": "created",
            "operation": "create_draft",
        }
    ]


def test_broker_normalizes_empty_n8n_audit_as_verified_clean(monkeypatch):
    class Response:
        status_code = 200
        content = b"[]"

        @staticmethod
        def json():
            return []

    monkeypatch.setattr(
        "n8n_agent_governance.requests.request",
        lambda *args, **kwargs: Response(),
    )
    broker = N8nApiBroker(lambda: "x" * 32)
    assert broker.security_audit() == {
        "status": "clean", "findings": [], "verified": True,
    }


def test_default_restricted_executes_only_safe_draft(governed):
    service, broker, _, _ = governed
    result = service.create_operation(proposal())
    assert result["status"] == "completed"
    assert broker.calls[0][0] == "create_draft"
    with pytest.raises(N8nGovernanceError) as error:
        service.create_operation(proposal(payload={"workflow": {"name": "Unsafe", "nodes": [{"type": "n8n-nodes-base.code"}]}}))
    assert error.value.code == "N8N_HIGH_RISK_FORBIDDEN"


def test_unified_integration_policy_is_fail_closed(governed):
    service, broker, _, _ = governed
    service.integration_permission_check = None

    with pytest.raises(N8nGovernanceError) as unavailable:
        service.list_workflows("project_a")

    assert unavailable.value.code == "N8N_INTEGRATION_POLICY_UNAVAILABLE"
    assert unavailable.value.status_code == 503
    assert broker.calls == []


def test_policy_revocation_after_review_blocks_broker_side_effect(governed):
    service, broker, _, _ = governed
    policy = {"decision": "allow"}
    service.integration_permission_check = (
        lambda *_args, **_kwargs: {
            "decision": policy["decision"],
            "policy_revision": 7 if policy["decision"] == "allow" else 8,
        }
    )
    operation = service.create_planned_operation(proposal())
    assert operation["status"] == "pending"
    policy["decision"] = "deny"

    with pytest.raises(N8nGovernanceError) as denied:
        service.decide(
            operation["id"],
            project_id="project_a",
            expected_digest=operation["digest"],
            approved=True,
        )

    assert denied.value.code == "N8N_INTEGRATION_POLICY_DENIED"
    assert broker.calls == []


def test_restricted_operation_binds_the_reviewed_integration_policy_revision(governed):
    service, broker, _, _ = governed
    policy = {"revision": 21}
    service.integration_permission_check = (
        lambda *_args, **_kwargs: {
            "decision": "require_approval",
            "policy_revision": policy["revision"],
        }
    )
    operation = service.create_planned_operation(proposal())
    assert operation["status"] == "pending"
    assert operation["integration_policy_revision"] == 21
    policy["revision"] = 22

    with pytest.raises(N8nGovernanceError) as stale:
        service.decide(
            operation["id"],
            project_id="project_a",
            expected_digest=operation["digest"],
            approved=True,
        )

    assert stale.value.code == "N8N_INTEGRATION_APPROVAL_STALE"
    assert broker.calls == []


@pytest.mark.parametrize(
    "node_type",
    [
        "@n8n/n8n-nodes-langchain.agent",
        "@n8n/n8n-nodes-langchain.agentTool",
        "@n8n/n8n-nodes-langchain.toolExecutor",
        "n8n-nodes-base.messageAnAgent",
    ],
)
def test_restricted_rejects_native_agent_and_hidden_tool_nodes(governed, node_type):
    service, broker, _, _ = governed
    with pytest.raises(N8nGovernanceError) as error:
        service.create_operation(
            proposal(
                payload={
                    "workflow": {
                        "name": "Hidden Tool",
                        "nodes": [{"type": node_type}],
                    }
                }
            )
        )
    assert error.value.code == "N8N_HIGH_RISK_FORBIDDEN"
    assert broker.calls == []


def test_planner_safe_draft_requires_approval_before_broker_execution(governed):
    service, broker, _, _ = governed
    operation = service.create_planned_operation(proposal(origin="browser-forgery-is-ignored"))
    assert operation["status"] == "pending"
    assert operation["origin"] == "planner"
    assert broker.calls == []

    completed = service.decide(
        operation["id"],
        project_id="project_a",
        expected_digest=operation["digest"],
        approved=True,
    )
    assert completed["status"] == "completed"
    assert broker.calls[0][0] == "create_draft"


def test_execute_atomically_claims_operation_at_most_once(governed):
    service, broker, _, _ = governed
    operation = service.create_planned_operation(proposal())
    with database.get_db_conn() as conn:
        conn.execute("UPDATE n8n_agent_operations SET status='approved' WHERE id=?", (operation["id"],))
    start = threading.Barrier(3)
    completed = []
    failures = []

    def execute_once():
        start.wait()
        try:
            completed.append(service._execute(operation["id"]))
        except N8nGovernanceError as exc:
            failures.append(exc)

    workers = [threading.Thread(target=execute_once) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert len(completed) == 1
    assert completed[0]["status"] == "completed"
    assert len(failures) == 1
    assert failures[0].code == "N8N_EXECUTION_ALREADY_CLAIMED"
    assert failures[0].status_code == 409
    assert len(broker.calls) == 1


def test_post_broker_reconciliation_failure_becomes_execution_unknown(governed, monkeypatch):
    service, broker, _, _ = governed
    operation = service.create_planned_operation(proposal())
    monkeypatch.setattr(
        service, "_bind_created_workflow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("local write failed")),
    )

    with pytest.raises(N8nGovernanceError) as unknown:
        service.decide(
            operation["id"], project_id="project_a",
            expected_digest=operation["digest"], approved=True,
        )

    assert unknown.value.code == "N8N_EXECUTION_OUTCOME_UNKNOWN"
    stored = service.get_operation(operation["id"], project_id="project_a")
    assert stored["status"] == "execution_unknown"
    assert stored["error_code"] == "N8N_EXECUTION_OUTCOME_UNKNOWN"
    assert stored["result"]["reconciliation_required"] is True
    assert stored["result"]["remote_result"]["id"] == "created"
    assert len(broker.calls) == 1
    with pytest.raises(N8nGovernanceError) as no_retry:
        service.decide(
            operation["id"], project_id="project_a",
            expected_digest=operation["digest"], approved=True,
        )
    assert no_retry.value.code == "N8N_APPROVAL_CONFLICT"


def test_delete_success_removes_project_workflow_binding(governed):
    service, broker, _, _ = governed
    operation = service.create_planned_operation(proposal(
        operation="delete",
        payload={"workflow_id": "safe", "workflow_name": "Safe"},
        diff={"removed": ["Safe"]},
    ))
    completed = service.decide(
        operation["id"],
        project_id="project_a",
        expected_digest=operation["digest"],
        approved=True,
        confirmation="Safe",
    )
    assert completed["status"] == "completed"
    assert broker.calls[-1][0] == "delete"
    assert service.list_workflows("project_a")["workflows"] == []


def test_operation_creation_requires_runtime_and_api_key(governed):
    service, broker, running, _ = governed
    assert service.get_policy("project_a")["runtime_ready"] is True
    running["value"] = False
    assert service.get_policy("project_a")["runtime_ready"] is False
    with pytest.raises(N8nGovernanceError) as stopped:
        service.create_planned_operation(proposal())
    assert stopped.value.code == "N8N_RUNTIME_NOT_READY"
    assert broker.calls == []

    running["value"] = True
    broker._api_key_provider = lambda: ""
    with pytest.raises(N8nGovernanceError) as missing_key:
        service.create_planned_operation(proposal())
    assert missing_key.value.code == "N8N_API_KEY_NOT_CONFIGURED"
    assert broker.calls == []


def test_update_draft_requires_exact_inactive_workflow(governed):
    service, broker, _, _ = governed
    broker.workflow_active["safe"] = True
    with pytest.raises(N8nGovernanceError) as active:
        service.create_planned_operation(proposal(
            operation="update_draft",
            payload={"workflow_id": "safe", "workflow": {"name": "Safe", "nodes": []}},
        ))
    assert active.value.code == "N8N_ACTIVE_WORKFLOW_UPDATE_FORBIDDEN"
    assert broker.calls == []


def test_diff_is_server_canonical_and_ignores_model_diff(governed):
    service, _, _, _ = governed
    operation = service.create_planned_operation(proposal(
        payload={
            "workflow": {
                "name": "Canonical",
                "active": True,
                "id": "caller-controlled",
                "nodes": [{
                    "id": "http-1",
                    "name": "Call API",
                    "type": "n8n-nodes-base.httpRequest",
                    "parameters": {"url": "https://api.example.com/private/path?token=hidden"},
                    "credentials": {"httpHeaderAuth": {"id": "cred-id", "name": "API Alias"}},
                }],
                "connections": {},
            }
        },
        diff={"forged": True, "warnings": []},
    ))
    assert operation["diff"]["source"] == "server"
    assert "forged" not in operation["diff"]
    added = operation["diff"]["nodes"]["added"]
    assert [(item["id"], item["name"], item["type"]) for item in added] == [
        ("http-1", "Call API", "n8n-nodes-base.httpRequest"),
    ]
    assert added[0]["parameter_keys"] == ["url"]
    assert len(added[0]["parameter_digest"]) == 64
    assert operation["diff"]["external_targets"]["after"] == [
        "https://api.example.com", "service:httprequest",
    ]
    assert operation["diff"]["credential_aliases"]["after"] == ["API Alias"]
    assert operation["risk"]["credential_aliases"] == ["API Alias"]
    with database.get_db_conn() as conn:
        payload = json.loads(conn.execute(
            "SELECT payload_json FROM n8n_agent_operations WHERE id=?", (operation["id"],)
        ).fetchone()["payload_json"])
    assert "active" not in payload["workflow"]
    assert "id" not in payload["workflow"]


def test_workflow_snapshot_change_invalidates_approval_and_execution(governed):
    service, broker, _, _ = governed
    operation = service.create_planned_operation(proposal(
        operation="update_draft",
        payload={"workflow_id": "safe", "workflow": {"name": "Safe", "nodes": []}},
    ))
    assert len(operation["base_digest"]) == 64
    broker.workflow_versions["safe"] = "v2"
    with pytest.raises(N8nGovernanceError) as stale:
        service.decide(
            operation["id"], project_id="project_a",
            expected_digest=operation["digest"], approved=True,
        )
    assert stale.value.code == "N8N_WORKFLOW_STALE"
    assert broker.calls == []

    # Even an internal execution attempt cannot bypass the target snapshot.
    with pytest.raises(N8nGovernanceError) as stale_execute:
        service._execute(operation["id"])
    assert stale_execute.value.code == "N8N_WORKFLOW_STALE"
    assert broker.calls == []


def test_update_diff_uses_exact_before_and_sanitized_after(governed):
    service, broker, _, _ = governed
    broker.workflow_nodes["safe"] = [{
        "id": "old", "name": "Old", "type": "n8n-nodes-base.set",
    }]
    operation = service.create_planned_operation(proposal(
        operation="update_draft",
        payload={
            "workflow_id": "safe",
            "workflow": {
                "name": "Safe Updated",
                "nodes": [{"id": "new", "name": "New", "type": "n8n-nodes-base.if"}],
                "connections": {},
            },
        },
        diff={"removed": [], "added": []},
    ))
    assert operation["diff"]["before"]["nodes"] == [{
        "id": "old", "name": "Old", "type": "n8n-nodes-base.set",
    }]
    after = operation["diff"]["after"]["nodes"]
    assert [(item["id"], item["name"], item["type"]) for item in after] == [
        ("new", "New", "n8n-nodes-base.if"),
    ]
    assert after[0]["parameter_keys"] == []
    assert [item["id"] for item in operation["diff"]["nodes"]["removed"]] == ["old"]
    assert [item["id"] for item in operation["diff"]["nodes"]["added"]] == ["new"]

def test_full_audit_requires_ack_and_every_change_approval(governed):
    service, broker, _, _ = governed
    with pytest.raises(N8nGovernanceError) as error:
        service.set_policy("project_a", {"mode": "full_audit", "elevation_policy": "one_hour"})
    assert error.value.code == "N8N_ELEVATION_ACK_REQUIRED"
    service.set_policy("project_a", {"mode": "full_audit", "elevation_policy": "one_hour", "explicit_ack": True})
    operation = service.create_operation(proposal())
    assert operation["status"] == "pending"
    assert not broker.calls
    with pytest.raises(N8nGovernanceError) as stale:
        service.decide(operation["id"], project_id="project_a", expected_digest="0" * 64, approved=True)
    assert stale.value.code == "N8N_OPERATION_STALE"
    completed = service.decide(operation["id"], project_id="project_a", expected_digest=operation["digest"], approved=True)
    assert completed["status"] == "completed"


def test_session_elevation_requires_bound_session(governed):
    service, _, _, _ = governed
    with pytest.raises(N8nGovernanceError) as missing:
        service.set_policy("project_a", {"mode": "full_audit", "elevation_policy": "session", "explicit_ack": True})
    assert missing.value.code == "N8N_SESSION_REQUIRED"
    active = service.set_policy("project_a", {"mode": "full_audit", "elevation_policy": "session", "session_id": "session_a", "explicit_ack": True})
    assert active["mode"] == "full_audit"
    assert service.list_workflows("project_a", session_id="session_a")["project_id"] == "project_a"
    assert service.get_policy("project_a", session_id="session_a")["mode"] == "full_audit"
    assert service.get_policy("project_a", session_id=None)["mode"] == "restricted"


def test_one_hour_expiry_persistent_and_smart_restart(governed):
    service, broker, running, runner = governed
    service.set_policy("project_a", {"mode": "full_audit", "elevation_policy": "one_hour", "explicit_ack": True})
    with database.get_db_conn() as conn:
        conn.execute("UPDATE n8n_agent_policies SET expires_at='2000-01-01T00:00:00+00:00' WHERE project_id='project_a'")
    assert service.get_policy("project_a")["mode"] == "restricted"

    service.set_policy("project_a", {"mode": "full_audit", "elevation_policy": "persistent", "explicit_ack": True})
    restarted = N8nAgentGovernanceService(
        broker=broker, cipher=AesGcmContentCipher(lambda: b"k" * 32),
        n8n_running=lambda: False, high_risk_runner_ready=lambda: runner["value"], boot_id="boot-b",
    )
    assert restarted.get_policy("project_a")["mode"] == "full_audit"

    restarted.set_policy("project_a", {"mode": "full_audit", "elevation_policy": "smart", "explicit_ack": True})
    second_restart = N8nAgentGovernanceService(
        broker=broker, cipher=AesGcmContentCipher(lambda: b"k" * 32),
        n8n_running=lambda: running["value"], high_risk_runner_ready=lambda: runner["value"], boot_id="boot-c",
    )
    assert second_restart.get_policy("project_a")["mode"] == "restricted"


def test_project_and_session_scope_are_fail_closed(governed):
    service, _, _, _ = governed
    with pytest.raises(N8nGovernanceError) as mismatch:
        service.create_operation(proposal(project_id="project_b"))
    assert mismatch.value.code == "SESSION_SCOPE_MISMATCH"
    operation = service.create_operation(proposal())
    with pytest.raises(N8nGovernanceError) as hidden:
        service.get_operation(operation["id"], project_id="project_b")
    assert hidden.value.status_code == 404

    database.create_session("integration_email", mode="email", project_id="project_a")
    with pytest.raises(N8nGovernanceError) as integration_only:
        service.create_planned_operation(proposal(session_id="integration_email"))
    assert integration_only.value.code == "SESSION_SCOPE_MISMATCH"


def test_archived_project_or_session_cannot_approve_or_execute(governed):
    service, broker, _, _ = governed
    session_operation = service.create_planned_operation(proposal())
    with database.get_db_conn() as conn:
        conn.execute("UPDATE sessions SET archived=1 WHERE id='session_a'")
    with pytest.raises(N8nGovernanceError) as archived_session:
        service.decide(
            session_operation["id"], project_id="project_a",
            expected_digest=session_operation["digest"], approved=True,
        )
    assert archived_session.value.code == "SESSION_ARCHIVED"
    with pytest.raises(N8nGovernanceError) as archived_session_execute:
        service._execute(session_operation["id"])
    assert archived_session_execute.value.code == "SESSION_ARCHIVED"
    assert broker.calls == []

    with database.get_db_conn() as conn:
        conn.execute("UPDATE sessions SET archived=0 WHERE id='session_a'")
    project_operation = service.create_planned_operation(proposal())
    with database.get_db_conn() as conn:
        conn.execute("UPDATE projects SET archived=1 WHERE id='project_a'")
    with pytest.raises(N8nGovernanceError) as archived_project:
        service.decide(
            project_operation["id"], project_id="project_a",
            expected_digest=project_operation["digest"], approved=True,
        )
    assert archived_project.value.code == "PROJECT_NOT_FOUND"
    with pytest.raises(N8nGovernanceError) as archived_project_execute:
        service._execute(project_operation["id"])
    assert archived_project_execute.value.code == "PROJECT_NOT_FOUND"
    assert broker.calls == []


def test_workflow_listing_and_operations_are_project_scoped(governed):
    service, broker, _, _ = governed
    broker.list_workflows = lambda: [
        {"id": "safe", "name": "Safe", "active": False, "protected": False},
        {"id": "other", "name": "Other", "active": False, "protected": False},
    ]
    with database.get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO n8n_agent_workflow_bindings(
                workflow_id,project_id,workflow_name,created_at,updated_at
            ) VALUES('other','project_b','Other','2026-08-13T00:00:00+00:00','2026-08-13T00:00:00+00:00')
            """
        )
    listed = service.list_workflows("project_a")
    assert [item["id"] for item in listed["workflows"]] == ["safe"]

    broker.get_workflow = lambda workflow_id: {
        "id": workflow_id, "name": "Other", "active": False, "protected": False,
    }
    with pytest.raises(N8nGovernanceError) as cross_project:
        service.create_planned_operation(proposal(
            operation="update_draft",
            payload={"workflow_id": "other", "workflow": {"name": "Other", "nodes": []}},
        ))
    assert cross_project.value.code == "N8N_WORKFLOW_SCOPE_MISMATCH"
    assert broker.calls == []

    with pytest.raises(N8nGovernanceError) as unmanaged:
        service.create_planned_operation(proposal(
            operation="update_draft",
            payload={"workflow_id": "unmanaged", "workflow": {"name": "Unmanaged", "nodes": []}},
        ))
    assert unmanaged.value.code == "N8N_WORKFLOW_NOT_MANAGED"


def test_secret_is_encrypted_single_use_and_never_public(governed):
    service, broker, _, _ = governed
    service.set_policy("project_a", {"mode": "full_audit", "elevation_policy": "one_hour", "explicit_ack": True})
    staged = service.stage_secret("project_a", {"clientSecret": "private-value"})
    with pytest.raises(N8nGovernanceError) as unavailable:
        service.create_operation(proposal(
            operation="credential_create",
            payload={"credential": {"name": "Gmail Alias", "type": "gmailOAuth2"}, "secret_handle": staged["secret_handle"]},
            diff={"credential": "Gmail Alias"},
        ))
    assert unavailable.value.code == "N8N_CREDENTIAL_GOVERNANCE_UNAVAILABLE"
    assert broker.calls == []
    with database.get_db_conn() as conn:
        row = conn.execute(
            "SELECT envelope,consumed_at FROM n8n_agent_secret_handles WHERE id=?",
            (staged["secret_handle"],),
        ).fetchone()
    assert row is not None
    assert row["consumed_at"] is None
    assert "private-value" not in row["envelope"]
    assert "private-value" not in json.dumps(service.list_audits("project_a"))


def test_smart_policy_downgrades_and_revokes_pending(governed):
    service, _, running, _ = governed
    service.set_policy("project_a", {"mode": "full_audit", "elevation_policy": "smart", "explicit_ack": True})
    operation = service.create_operation(proposal(operation="activate", payload={"workflow_id": "safe", "workflow_name": "Safe"}))
    running["value"] = False
    policy = service.get_policy("project_a", session_id="session_a")
    assert policy["mode"] == "restricted"
    assert service.get_operation(operation["id"])["status"] == "revoked"


def test_restricted_to_off_revokes_planner_approval(governed):
    service, broker, _, _ = governed
    operation = service.create_planned_operation(proposal())
    service.set_policy("project_a", {"mode": "off", "elevation_policy": "smart"})
    assert service.get_operation(operation["id"])["status"] == "revoked"
    with pytest.raises(N8nGovernanceError) as conflict:
        service.decide(
            operation["id"],
            project_id="project_a",
            expected_digest=operation["digest"],
            approved=True,
        )
    assert conflict.value.code == "N8N_APPROVAL_CONFLICT"
    assert broker.calls == []


def test_high_risk_publish_needs_runner_audit_and_second_approval(governed):
    service, broker, _, runner = governed
    service.set_policy("project_a", {"mode": "full_audit", "elevation_policy": "one_hour", "explicit_ack": True})
    operation = service.create_operation(proposal(
        operation="publish",
        payload={"workflow_id": "unsafe", "workflow_name": "Unsafe", "high_risk": True},
    ))
    with pytest.raises(N8nGovernanceError) as unavailable:
        service.decide(operation["id"], project_id="project_a", expected_digest=operation["digest"], approved=True)
    assert unavailable.value.code == "N8N_HIGH_RISK_RUNNER_UNAVAILABLE"
    runner["value"] = True
    second = service.decide(operation["id"], project_id="project_a", expected_digest=operation["digest"], approved=True)
    assert second["status"] == "pending_second_approval"
    assert broker.audit_calls == 1
    completed = service.decide(operation["id"], project_id="project_a", expected_digest=operation["digest"], approved=True)
    assert completed["status"] == "completed"


def test_existing_high_risk_nodes_are_derived_from_target_snapshot(governed):
    service, broker, _, runner = governed
    service.set_policy("project_a", {
        "mode": "full_audit", "elevation_policy": "one_hour", "explicit_ack": True,
    })
    broker.workflow_nodes["unsafe"] = [{
        "id": "code-1", "name": "Code", "type": "n8n-nodes-base.code",
    }]
    operation = service.create_planned_operation(proposal(
        operation="activate",
        payload={"workflow_id": "unsafe", "workflow_name": "Unsafe"},
    ))
    assert operation["high_risk"] is True
    assert operation["risk"]["high_risk_nodes"] is True
    with pytest.raises(N8nGovernanceError) as unavailable:
        service.decide(
            operation["id"], project_id="project_a",
            expected_digest=operation["digest"], approved=True,
        )
    assert unavailable.value.code == "N8N_HIGH_RISK_RUNNER_UNAVAILABLE"
    assert broker.calls == []
    runner["value"] = True
    second = service.decide(
        operation["id"], project_id="project_a",
        expected_digest=operation["digest"], approved=True,
    )
    assert second["status"] == "pending_second_approval"


@pytest.mark.parametrize(
    ("report", "expected_code"),
    [
        ({"status": "critical", "findings": [{"severity": "critical"}]}, "N8N_SECURITY_AUDIT_CRITICAL"),
        ({"success": False, "errors": ["audit failed"]}, "N8N_SECURITY_AUDIT_FAILED"),
        ({"unexpected": "shape"}, "N8N_SECURITY_AUDIT_UNVERIFIABLE"),
    ],
)
def test_security_audit_result_is_semantically_gated(governed, report, expected_code):
    service, broker, _, _ = governed
    broker.security_audit = lambda: report
    operation = service.create_planned_operation(proposal(
        operation="activate", payload={"workflow_id": "safe", "workflow_name": "Safe"},
    ))
    with pytest.raises(N8nGovernanceError) as rejected:
        service.decide(
            operation["id"], project_id="project_a",
            expected_digest=operation["digest"], approved=True,
        )
    assert rejected.value.code == expected_code
    assert broker.calls == []


@pytest.mark.parametrize("category", ["nodes", "filesystem", "database", "instance"])
def test_official_n8n_security_audit_findings_block_execution(governed, category):
    service, broker, _, _ = governed
    broker.security_audit = lambda: {
        f"{category.title()} Risk Report": {
            "risk": category,
            "sections": [{
                "title": "Reviewed finding",
                "description": "A security finding exists.",
                "recommendation": "Review before publishing.",
                "location": [],
            }],
        },
    }
    operation = service.create_planned_operation(proposal(
        operation="activate", payload={"workflow_id": "safe", "workflow_name": "Safe"},
    ))
    with pytest.raises(N8nGovernanceError) as rejected:
        service.decide(
            operation["id"], project_id="project_a",
            expected_digest=operation["digest"], approved=True,
        )
    assert rejected.value.code == "N8N_SECURITY_AUDIT_FINDINGS"
    assert broker.calls == []


def test_official_credential_hygiene_report_is_digestible(governed):
    service, broker, _, _ = governed
    broker.security_audit = lambda: {
        "Credentials Risk Report": {
            "risk": "credentials",
            "sections": [{
                "title": "Unused credential",
                "description": "Credential hygiene only.",
                "recommendation": "Remove it separately.",
                "location": [],
            }],
        },
    }
    operation = service.create_planned_operation(proposal(
        operation="activate", payload={"workflow_id": "safe", "workflow_name": "Safe"},
    ))
    completed = service.decide(
        operation["id"], project_id="project_a",
        expected_digest=operation["digest"], approved=True,
    )
    assert completed["status"] == "completed"


def test_low_risk_publish_still_requires_security_audit(governed):
    service, broker, _, _ = governed
    operation = service.create_operation(proposal(operation="activate", payload={"workflow_id": "safe", "workflow_name": "Safe"}))
    completed = service.decide(operation["id"], project_id="project_a", expected_digest=operation["digest"], approved=True)
    assert completed["status"] == "completed"
    assert broker.audit_calls == 1


def test_protected_workflow_and_secret_in_normal_payload_are_rejected(governed):
    service, broker, _, _ = governed
    with pytest.raises(N8nGovernanceError) as protected:
        service.create_operation(proposal(payload={"workflow_name": "workbench-gmail-inbound-v1"}))
    assert protected.value.code == "N8N_WORKFLOW_PROTECTED"
    for protected_name in ("Workbench Agent Bridge v1", "Workbench Approval Gate v1"):
        with pytest.raises(N8nGovernanceError) as protected_bridge:
            service.create_operation(proposal(payload={"workflow_name": protected_name}))
        assert protected_bridge.value.code == "N8N_WORKFLOW_PROTECTED"
    with pytest.raises(N8nGovernanceError) as secret:
        service.create_operation(proposal(payload={"api_key": "do-not-store"}))
    assert secret.value.code == "N8N_SECRET_IN_PROPOSAL"
    with pytest.raises(N8nGovernanceError) as community:
        service.create_operation(proposal(payload={"workflow": {"name": "Community", "nodes": [{"type": "n8n-nodes-example.action"}]}}))
    assert community.value.code == "N8N_HIGH_RISK_FORBIDDEN"

    with pytest.raises(N8nGovernanceError) as protected_by_exact_lookup:
        service.create_operation(proposal(
            operation="update_draft",
            payload={
                "workflow_id": "protected",
                "workflow": {"name": "Innocent display name", "nodes": []},
            },
        ))
    assert protected_by_exact_lookup.value.code == "N8N_WORKFLOW_PROTECTED"

    broker.workflow_lookup_error = True
    with pytest.raises(N8nGovernanceError) as lookup_failed:
        service.create_operation(proposal(
            operation="update_draft",
            payload={"workflow_id": "safe", "workflow": {"name": "Safe", "nodes": []}},
        ))
    assert lookup_failed.value.code == "N8N_PROTECTED_WORKFLOW_LOOKUP_FAILED"
    assert broker.calls == []
