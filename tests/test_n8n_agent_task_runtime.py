from __future__ import annotations

import hashlib
import hmac
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import database
from api.routes.n8n_agent_tasks import build_n8n_agent_tasks_router
from n8n_agent_task_runtime import HMAC_PROFILE, N8nAgentTaskError, N8nAgentTaskRuntime
from n8n_gmail_crypto import AesGcmContentCipher


COMPILED_REVISION = "wbr_compiled_revision_token_001"
ACTIVE_VERSION = "active-version-uuid-001"


def _allow_integration_permission(*_args, **_kwargs):
    return {"decision": "allow", "policy_revision": 1}


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def _setup(
    tmp_path,
    monkeypatch,
    *,
    boot_id="boot-one",
    full_audit=False,
    integration_permission_check=_allow_integration_permission,
    execution_gate=None,
):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "runtime.sqlite"))
    database.init_db()
    database.create_project("project-one", "Project One", str(tmp_path / "p1"))
    database.create_project("project-two", "Project Two", str(tmp_path / "p2"))
    now = datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc).isoformat()
    with database.get_db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS n8n_agent_workflow_bindings (
                workflow_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                workflow_name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO n8n_agent_workflow_bindings VALUES(?,?,?,?,?)",
            ("workflow-one", "project-one", "Managed", now, now),
        )
    clock = Clock()
    clock.live_version = ACTIVE_VERSION
    generated = []
    counters = {}

    def ids(prefix):
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}_{counters[prefix]}"

    def generator(request):
        generated.append(request)
        return {"subject": "Generated", "body": "Safe result"}

    def skill_resolver(project_id, slug, sha256):
        assert project_id == "project-one"
        return {"slug": slug, "sha256": sha256, "instructions": "Use the approved tone."}

    def credential_resolver(credential_id):
        if credential_id != "internal-credential-77":
            raise KeyError(credential_id)
        return {"id": credential_id, "name": "Gmail Work", "type": "gmailOAuth2Api", "status": "ready"}

    runtime = N8nAgentTaskRuntime(
        cipher=AesGcmContentCipher(lambda: b"k" * 32),
        hmac_secret_provider=lambda: b"h" * 32,
        generator=generator,
        skill_resolver=skill_resolver,
        credential_resolver=credential_resolver,
        policy_resolver=(
            lambda _project: {
                "mode": "full_audit", "elevation_policy": "one_hour",
                "runtime_ready": True,
            }
            if full_audit else None
        ),
        execution_gate=execution_gate,
        integration_permission_check=integration_permission_check,
        workflow_revision_resolver=lambda workflow_id: {
            "active": workflow_id == "workflow-one",
            "active_version_id": clock.live_version,
        },
        clock=clock,
        id_factory=ids,
        boot_id=boot_id,
    )
    return runtime, clock, generated


def _binding(runtime):
    skill_sha = "a" * 64
    claim = runtime.binding_resolver(
        "Workbench Agent Bridge",
        {
            "id": "agent-node",
            "agent": {
                "instruction": "Draft a safe structured response.",
                "model": "local-model",
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string", "maxLength": 100},
                        "body": {"type": "string", "maxLength": 1_000},
                    },
                    "required": ["subject", "body"],
                    "additionalProperties": False,
                },
                "skills": [{"slug": "mail-tone", "sha256": skill_sha}],
            },
        },
        {
            "project_id": "project-one",
            "_workbench_revision_token": COMPILED_REVISION,
        },
    )
    assert claim["workflow_revision"] == COMPILED_REVISION
    with database.get_db_conn() as conn:
        provisional = conn.execute(
            "SELECT workflow_revision FROM n8n_agent_binding_claims WHERE claim_id=?",
            (claim["binding_claim_id"],),
        ).fetchone()
    assert provisional["workflow_revision"] == COMPILED_REVISION
    bindings = runtime.finalize_bindings(
        "workflow-one",
        "draft-revision",
        [{"binding_claim_id": claim["binding_claim_id"], "node_id": "agent-node-runtime"}],
        "project-one",
    )
    assert bindings[0]["active"] is False
    assert bindings[0]["workflow_revision"] == COMPILED_REVISION
    activated = runtime.activate_bindings(
        "workflow-one", ACTIVE_VERSION, [claim["agent_binding_id"]], "project-one"
    )
    assert activated[0]["active"] is True
    assert activated[0]["workflow_revision"] == COMPILED_REVISION
    with database.get_db_conn() as conn:
        persisted = conn.execute(
            "SELECT workflow_revision,active_version_id FROM n8n_agent_task_bindings WHERE agent_binding_id=?",
            (claim["agent_binding_id"],),
        ).fetchone()
    assert persisted["workflow_revision"] == COMPILED_REVISION
    assert persisted["active_version_id"] == ACTIVE_VERSION
    return claim["agent_binding_id"]


def _approval_scope(runtime, *, target="recipient@example.test"):
    manifest = {
        "schema": "approval_action_manifest.v1",
        "approval_node_id": "approval-node-runtime",
        "downstream_node_id": "gmail-send-node",
        "downstream_node_type": "n8n-nodes-base.gmail",
        "credential_alias": "gmail-work",
        "credential_type": "gmailOAuth2Api",
        "target_kind": "email",
        "target_rule": {"mode": "static", "value": target},
        "action": "send_email",
        "operation": "send",
    }
    claim = runtime.binding_resolver(
        "Workbench Approval Bridge",
        {"key": "approval-node-runtime"},
        {
            "project_id": "project-one",
            "_workbench_revision_token": COMPILED_REVISION,
            "_approval_action_manifests": {
                "approval-node-runtime": manifest,
            },
        },
    )
    finalized = runtime.finalize_bindings(
        "workflow-one",
        "draft-revision",
        [{
            "kind": "workbench.approval",
            "binding_claim_id": claim["binding_claim_id"],
            "node_id": "approval-node-runtime",
            "workflow_revision": COMPILED_REVISION,
            "manifest_digest": claim["manifest_digest"],
        }],
        "project-one",
    )
    assert finalized[0]["active"] is True
    return {
        "node_id": f"{claim['approval_binding_id']}:{claim['manifest_digest']}",
        "approval_binding_id": claim["approval_binding_id"],
        "manifest_digest": claim["manifest_digest"],
    }


def test_provisional_binding_task_is_project_scoped_tool_free_and_encrypted(tmp_path, monkeypatch):
    runtime, _clock, generated = _setup(tmp_path, monkeypatch)
    binding_id = _binding(runtime)

    task = runtime.submit_task(
        {
            "request_id": "execution-1-item-0",
            "workflow_id": "workflow-one",
            "workflow_revision": COMPILED_REVISION,
            "node_id": "agent-node-runtime",
            "agent_binding_id": binding_id,
            "input": {"message": "Untrusted hello"},
        }
    )
    assert task["status"] == "queued"
    assert task["idempotent"] is False

    with database.get_db_conn() as conn:
        row = conn.execute("SELECT * FROM n8n_agent_tasks WHERE task_id=?", (task["task_id"],)).fetchone()
        persisted = " ".join(str(value) for value in tuple(row))
    assert "Untrusted hello" not in persisted
    assert "Draft a safe structured response" not in persisted

    completed = runtime.process_task(task["task_id"])
    assert completed["status"] == "succeeded"
    assert generated[0]["security"] == {
        "tools": [],
        "external_actions": False,
        "input_trust": "untrusted",
        "secrets_allowed": False,
        "project_id": "project-one",
    }
    assert generated[0]["trusted"]["skills"][0]["instructions"] == "Use the approved tone."
    assert generated[0]["untrusted_input"] == {"message": "Untrusted hello"}

    public = runtime.get_task_public(task["task_id"], project_id="project-one")
    assert "result" not in public
    private = runtime.get_task_for_n8n(task["task_id"], workflow_id="workflow-one")
    assert private["result"] == {"subject": "Generated", "body": "Safe result"}
    with pytest.raises(N8nAgentTaskError) as scope:
        runtime.get_task_public(task["task_id"], project_id="project-two")
    assert scope.value.status_code == 404


def test_task_submission_fails_closed_without_unified_policy_gate(tmp_path, monkeypatch):
    runtime, _clock, generated = _setup(
        tmp_path,
        monkeypatch,
        integration_permission_check=None,
    )
    binding_id = _binding(runtime)

    with pytest.raises(N8nAgentTaskError) as unavailable:
        runtime.submit_task(
            {
                "request_id": "policy-missing",
                "workflow_id": "workflow-one",
                "workflow_revision": COMPILED_REVISION,
                "node_id": "agent-node-runtime",
                "agent_binding_id": binding_id,
                "input": {"message": "must not run"},
            }
        )

    assert unavailable.value.code == "N8N_INTEGRATION_POLICY_UNAVAILABLE"
    assert unavailable.value.status_code == 503
    assert generated == []


def test_restricted_project_does_not_treat_workflow_binding_as_task_approval(tmp_path, monkeypatch):
    runtime, _clock, generated = _setup(
        tmp_path,
        monkeypatch,
        integration_permission_check=lambda *_args, **_kwargs: {
            "decision": "require_approval",
            "policy_revision": 4,
        },
    )
    binding_id = _binding(runtime)

    with pytest.raises(N8nAgentTaskError) as approval:
        runtime.submit_task(
            {
                "request_id": "restricted-task",
                "workflow_id": "workflow-one",
                "workflow_revision": COMPILED_REVISION,
                "node_id": "agent-node-runtime",
                "agent_binding_id": binding_id,
                "input": {"message": "must await an exact approval contract"},
            }
        )

    assert approval.value.code == "N8N_INTEGRATION_APPROVAL_REQUIRED"
    assert generated == []
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM n8n_agent_tasks").fetchone()[0] == 0


def test_extension_revocation_after_queue_terminates_task_without_model_call(tmp_path, monkeypatch):
    enabled = {"value": True}
    runtime, _clock, generated = _setup(
        tmp_path,
        monkeypatch,
        execution_gate=lambda _project_id: enabled["value"],
    )
    binding_id = _binding(runtime)
    task = runtime.submit_task(
        {
            "request_id": "extension-revoked",
            "workflow_id": "workflow-one",
            "workflow_revision": COMPILED_REVISION,
            "node_id": "agent-node-runtime",
            "agent_binding_id": binding_id,
            "input": {"message": "must not reach the model"},
        }
    )
    enabled["value"] = False

    result = runtime.process_task(task["task_id"])

    assert result["status"] == "generation_failed"
    assert result["error_code"] == "EXTENSION_DISABLED"
    assert generated == []
    assert runtime.process_next_task() is None


def test_background_worker_rechecks_policy_before_model_execution(tmp_path, monkeypatch):
    checks = []

    def permission(project_id, capability, **scope):
        checks.append((project_id, capability, dict(scope)))
        # submit_task and the worker's pre-claim check pass.  Revocation is
        # observed by the final check immediately before the model call.
        return {"decision": "allow" if len(checks) < 3 else "deny"}

    runtime, _clock, generated = _setup(
        tmp_path,
        monkeypatch,
        integration_permission_check=permission,
    )
    binding_id = _binding(runtime)
    task = runtime.submit_task(
        {
            "request_id": "policy-revoked",
            "workflow_id": "workflow-one",
            "workflow_revision": COMPILED_REVISION,
            "node_id": "agent-node-runtime",
            "agent_binding_id": binding_id,
            "input": {"message": "must not reach the model"},
        }
    )

    result = runtime.process_task(task["task_id"])

    assert result["status"] == "generation_failed"
    assert result["error_code"] == "N8N_INTEGRATION_POLICY_DENIED"
    assert generated == []
    assert [item[1] for item in checks] == [
        "agent.task.submit",
        "agent.task.submit",
        "agent.task.submit",
    ]


def test_manual_revision_mismatch_and_secret_input_fail_closed(tmp_path, monkeypatch):
    runtime, _clock, _generated = _setup(tmp_path, monkeypatch)
    binding_id = _binding(runtime)
    base = {
        "request_id": "request-one",
        "workflow_id": "workflow-one",
        "workflow_revision": "manually-edited-version",
        "node_id": "agent-node-runtime",
        "agent_binding_id": binding_id,
        "input": {"message": "hello"},
    }
    with pytest.raises(N8nAgentTaskError) as stale:
        runtime.submit_task(base)
    assert stale.value.code == "N8N_AGENT_BINDING_SCOPE_MISMATCH"
    base["workflow_revision"] = COMPILED_REVISION
    base["input"] = {"api_key": "must-never-enter-model"}
    with pytest.raises(N8nAgentTaskError) as secret:
        runtime.submit_task(base)
    assert secret.value.code == "N8N_AGENT_SECRET_FIELD_FORBIDDEN"


def test_live_active_version_drift_revokes_and_disables_binding(tmp_path, monkeypatch):
    runtime, clock, _generated = _setup(tmp_path, monkeypatch)
    binding_id = _binding(runtime)
    approval_scope = _approval_scope(runtime)
    runtime.adopt_credential_alias(
        {
            "project_id": "project-one",
            "alias": "gmail-work",
            "credential_id": "internal-credential-77",
        }
    )
    pending = runtime.request_runtime_approval(
        {
            "request_id": "before-manual-publish",
            "workflow_id": "workflow-one",
            "workflow_revision": COMPILED_REVISION,
            "node_id": approval_scope["node_id"],
            "credential_alias": "gmail-work",
            "target_kind": "email",
            "target": "recipient@example.test",
            "action": "send_email",
            "run_key": "execution-before-publish",
            "task_id": None,
        }
    )
    clock.live_version = "manually-published-version-002"
    with pytest.raises(N8nAgentTaskError) as stale:
        runtime.submit_task(
            {
                "request_id": "after-manual-publish",
                "workflow_id": "workflow-one",
                "workflow_revision": COMPILED_REVISION,
                "node_id": "agent-node-runtime",
                "agent_binding_id": binding_id,
                "input": {"message": "must fail closed"},
            }
        )
    assert stale.value.code == "N8N_WORKFLOW_REVISION_CHANGED"
    assert runtime.get_binding(binding_id, project_id="project-one")["active"] is False
    assert runtime.list_runtime_approvals("project-one")[0]["status"] == "revoked"
    with pytest.raises(N8nAgentTaskError) as old_status:
        runtime.get_runtime_approval_for_n8n(
            pending["approval_id"], workflow_id="workflow-one"
        )
    assert old_status.value.code == "N8N_APPROVAL_MANIFEST_SCOPE_MISMATCH"


def test_runtime_action_status_rechecks_live_active_version(tmp_path, monkeypatch):
    runtime, clock, _generated = _setup(tmp_path, monkeypatch)
    binding_id = _binding(runtime)
    approval_scope = _approval_scope(runtime)
    runtime.adopt_credential_alias(
        {
            "project_id": "project-one",
            "alias": "gmail-work",
            "credential_id": "internal-credential-77",
        }
    )
    pending = runtime.request_runtime_approval(
        {
            "request_id": "runtime-status-drift",
            "workflow_id": "workflow-one",
            "workflow_revision": COMPILED_REVISION,
            "node_id": approval_scope["node_id"],
            "credential_alias": "gmail-work",
            "target_kind": "email",
            "target": "recipient@example.test",
            "action": "send_email",
            "run_key": "runtime-status-run",
            "task_id": None,
        }
    )
    runtime.decide_runtime_approval(
        pending["approval_id"],
        project_id="project-one",
        expected_digest=pending["request_digest"],
        approved=True,
    )
    clock.live_version = "manually-published-version-003"
    with pytest.raises(N8nAgentTaskError) as stale:
        runtime.get_runtime_approval_for_n8n(
            pending["approval_id"], workflow_id="workflow-one"
        )
    assert stale.value.code == "N8N_WORKFLOW_REVISION_CHANGED"
    assert runtime.get_binding(binding_id, project_id="project-one")["active"] is False
    assert runtime.list_runtime_approvals("project-one")[0]["status"] == "revoked"


def test_approved_runtime_action_is_not_released_after_project_policy_revocation(tmp_path, monkeypatch):
    policy = {"decision": "allow"}

    def permission(*_args, **_kwargs):
        return {"decision": policy["decision"], "policy_revision": 11}

    runtime, _clock, _generated = _setup(
        tmp_path,
        monkeypatch,
        integration_permission_check=permission,
    )
    _binding(runtime)
    approval_scope = _approval_scope(runtime)
    runtime.adopt_credential_alias(
        {
            "project_id": "project-one",
            "alias": "gmail-work",
            "credential_id": "internal-credential-77",
        }
    )
    pending = runtime.request_runtime_approval(
        {
            "request_id": "policy-revoked-after-approval",
            "workflow_id": "workflow-one",
            "workflow_revision": COMPILED_REVISION,
            "node_id": approval_scope["node_id"],
            "credential_alias": "gmail-work",
            "target_kind": "email",
            "target": "recipient@example.test",
            "action": "send_email",
            "run_key": "policy-revoked-run",
            "task_id": None,
        }
    )
    runtime.decide_runtime_approval(
        pending["approval_id"],
        project_id="project-one",
        expected_digest=pending["request_digest"],
        approved=True,
    )
    policy["decision"] = "deny"

    with pytest.raises(N8nAgentTaskError) as denied:
        runtime.get_runtime_approval_for_n8n(
            pending["approval_id"],
            workflow_id="workflow-one",
        )

    assert denied.value.code == "N8N_INTEGRATION_POLICY_DENIED"


def test_runtime_approval_binds_integration_policy_revision(tmp_path, monkeypatch):
    policy = {"revision": 17}

    def permission(*_args, **_kwargs):
        return {
            "decision": "require_approval",
            "policy_revision": policy["revision"],
        }

    runtime, _clock, _generated = _setup(
        tmp_path,
        monkeypatch,
        integration_permission_check=permission,
    )
    _binding(runtime)
    approval_scope = _approval_scope(runtime)
    runtime.adopt_credential_alias(
        {
            "project_id": "project-one",
            "alias": "gmail-work",
            "credential_id": "internal-credential-77",
        }
    )
    pending = runtime.request_runtime_approval(
        {
            "request_id": "policy-revision-action",
            "workflow_id": "workflow-one",
            "workflow_revision": COMPILED_REVISION,
            "node_id": approval_scope["node_id"],
            "credential_alias": "gmail-work",
            "target_kind": "email",
            "target": "recipient@example.test",
            "action": "send_email",
            "run_key": "policy-revision-run",
            "task_id": None,
        }
    )
    policy["revision"] = 18

    with pytest.raises(N8nAgentTaskError) as stale:
        runtime.decide_runtime_approval(
            pending["approval_id"],
            project_id="project-one",
            expected_digest=pending["request_digest"],
            approved=True,
        )

    assert stale.value.code == "N8N_INTEGRATION_APPROVAL_STALE"
    assert runtime.list_runtime_approvals("project-one")[0]["status"] == "pending"


def test_runtime_approval_is_released_exactly_once(tmp_path, monkeypatch):
    runtime, _clock, _generated = _setup(tmp_path, monkeypatch)
    _binding(runtime)
    approval_scope = _approval_scope(runtime)
    runtime.adopt_credential_alias(
        {
            "project_id": "project-one",
            "alias": "gmail-work",
            "credential_id": "internal-credential-77",
        }
    )
    pending = runtime.request_runtime_approval(
        {
            "request_id": "one-shot-action",
            "workflow_id": "workflow-one",
            "workflow_revision": COMPILED_REVISION,
            "node_id": approval_scope["node_id"],
            "credential_alias": "gmail-work",
            "target_kind": "email",
            "target": "recipient@example.test",
            "action": "send_email",
            "run_key": "one-shot-run",
            "task_id": None,
        }
    )
    runtime.decide_runtime_approval(
        pending["approval_id"],
        project_id="project-one",
        expected_digest=pending["request_digest"],
        approved=True,
    )

    first = runtime.get_runtime_approval_for_n8n(
        pending["approval_id"], workflow_id="workflow-one"
    )
    second = runtime.get_runtime_approval_for_n8n(
        pending["approval_id"], workflow_id="workflow-one"
    )

    assert first["status"] == "approved"
    assert second["status"] == "consumed"


def test_runtime_approval_cannot_expire_between_read_and_atomic_consume(tmp_path, monkeypatch):
    advance = {"value": False}
    clock_holder = {}

    def permission(*_args, **_kwargs):
        if advance["value"]:
            clock_holder["clock"].value += timedelta(seconds=2)
            advance["value"] = False
        return {"decision": "allow", "policy_revision": 1}

    runtime, clock, _generated = _setup(
        tmp_path,
        monkeypatch,
        integration_permission_check=permission,
    )
    clock_holder["clock"] = clock
    _binding(runtime)
    approval_scope = _approval_scope(runtime)
    runtime.adopt_credential_alias(
        {
            "project_id": "project-one",
            "alias": "gmail-work",
            "credential_id": "internal-credential-77",
        }
    )
    pending = runtime.request_runtime_approval(
        {
            "request_id": "expiry-boundary-action",
            "workflow_id": "workflow-one",
            "workflow_revision": COMPILED_REVISION,
            "node_id": approval_scope["node_id"],
            "credential_alias": "gmail-work",
            "target_kind": "email",
            "target": "recipient@example.test",
            "action": "send_email",
            "run_key": "expiry-boundary-run",
            "task_id": None,
        }
    )
    runtime.decide_runtime_approval(
        pending["approval_id"],
        project_id="project-one",
        expected_digest=pending["request_digest"],
        approved=True,
    )
    clock.value = datetime.fromisoformat(pending["expires_at"]) - timedelta(seconds=1)
    advance["value"] = True

    result = runtime.get_runtime_approval_for_n8n(
        pending["approval_id"], workflow_id="workflow-one"
    )

    assert result["status"] == "expired"


def test_credential_alias_and_exact_runtime_approval_grants(tmp_path, monkeypatch):
    runtime, clock, _generated = _setup(tmp_path, monkeypatch)
    _binding(runtime)
    approval_scope = _approval_scope(runtime)
    credential = runtime.adopt_credential_alias(
        {"project_id": "project-one", "alias": "gmail-work", "credential_id": "internal-credential-77"}
    )
    assert credential["status"] == "ready"
    assert "credential_id" not in credential
    assert runtime.credential_alias_resolver("project-one", "gmail-work", "gmailOAuth2Api")["id"] == "internal-credential-77"
    with database.get_db_conn() as conn:
        stored = conn.execute("SELECT credential_ref_envelope FROM n8n_agent_credential_aliases").fetchone()[0]
    assert "internal-credential-77" not in stored

    action = {
        "request_id": "action-one",
        "workflow_id": "workflow-one",
        "workflow_revision": COMPILED_REVISION,
        "node_id": approval_scope["node_id"],
        "credential_alias": "gmail-work",
        "target_kind": "email",
        "target": "recipient@example.test",
        "action": "send_email",
        "run_key": "execution-one",
        "task_id": None,
    }
    pending = runtime.request_runtime_approval(action)
    assert pending["status"] == "pending"
    approved = runtime.decide_runtime_approval(
        pending["approval_id"], project_id="project-one",
        expected_digest=pending["request_digest"], approved=True,
    )
    assert approved["status"] == "approved"

    same_run = runtime.request_runtime_approval({**action, "request_id": "action-two"})
    assert same_run["status"] == "pending"
    other_run = runtime.request_runtime_approval(
        {**action, "request_id": "action-three", "run_key": "execution-two"}
    )
    assert other_run["status"] == "pending"
    with pytest.raises(N8nAgentTaskError):
        runtime.decide_runtime_approval(
            other_run["approval_id"], project_id="project-one",
            expected_digest=other_run["request_digest"], approved=True, duration_minutes=61,
        )

    runtime.notify_policy_changed("project-one")
    assert runtime.get_runtime_approval_for_n8n(
        approved["approval_id"], workflow_id="workflow-one"
    )["status"] == "revoked"
    clock.value += timedelta(hours=2)
    assert runtime.get_runtime_approval_for_n8n(
        other_run["approval_id"], workflow_id="workflow-one"
    )["status"] == "revoked"


def test_timed_grant_requires_live_full_audit_and_is_exact(tmp_path, monkeypatch):
    runtime, _clock, _generated = _setup(tmp_path, monkeypatch, full_audit=True)
    _binding(runtime)
    approval_scope = _approval_scope(runtime)
    runtime.adopt_credential_alias(
        {"project_id": "project-one", "alias": "gmail-work", "credential_id": "internal-credential-77"}
    )
    action = {
        "request_id": "timed-one", "workflow_id": "workflow-one",
        "workflow_revision": COMPILED_REVISION, "node_id": approval_scope["node_id"],
        "credential_alias": "gmail-work", "target_kind": "email",
        "target": "recipient@example.test", "action": "send_email",
        "run_key": "execution-one", "task_id": None,
    }
    pending = runtime.request_runtime_approval(action)
    approved = runtime.decide_runtime_approval(
        pending["approval_id"], project_id="project-one",
        expected_digest=pending["request_digest"], approved=True, duration_minutes=15,
    )
    assert approved["status"] == "approved"
    automatic = runtime.request_runtime_approval({**action, "request_id": "timed-two"})
    assert automatic["status"] == "approved_by_grant"


def test_runtime_action_cannot_self_assert_manifest_alias_action_or_target(tmp_path, monkeypatch):
    runtime, _clock, _generated = _setup(tmp_path, monkeypatch)
    _binding(runtime)
    runtime.adopt_credential_alias(
        {"project_id": "project-one", "alias": "gmail-work", "credential_id": "internal-credential-77"}
    )
    base = {
        "request_id": "manifest-required", "workflow_id": "workflow-one",
        "workflow_revision": COMPILED_REVISION, "node_id": "approval-node-runtime",
        "credential_alias": "gmail-work", "target_kind": "email",
        "target": "recipient@example.test", "action": "send_email",
        "run_key": "execution-one", "task_id": None,
    }
    with pytest.raises(N8nAgentTaskError) as missing:
        runtime.request_runtime_approval(base)
    assert missing.value.code == "N8N_APPROVAL_MANIFEST_REQUIRED"

    scope = _approval_scope(runtime)
    valid = {**base, "request_id": "manifest-valid", "node_id": scope["node_id"]}
    assert runtime.request_runtime_approval(valid)["status"] == "pending"
    for request_id, change in (
        ("manifest-alias", {"credential_alias": "other-alias"}),
        ("manifest-action", {"action": "http_write"}),
        ("manifest-target", {"target": "other@example.test"}),
    ):
        with pytest.raises(N8nAgentTaskError) as spoofed:
            runtime.request_runtime_approval({**valid, "request_id": request_id, **change})
        assert spoofed.value.code == "N8N_RUNTIME_ACTION_SCOPE_MISMATCH"


def test_protected_approval_template_uses_server_token_and_returns_only_payload():
    template = json.loads(
        (BACKEND.parent / "config" / "n8n-workflows" / "workbench-approval-gate-v1.json")
        .read_text(encoding="utf-8")
    )
    serialized = json.dumps(template, ensure_ascii=False)
    assert "node_id:String($json.approval_token" in serialized
    assert "$('When Called').item.json.input.payload" in serialized


def _signed(path, payload, now, nonce):
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = int(now.timestamp())
    canonical = f"POST\n{path}\n{timestamp}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode()
    signature = hmac.new(b"h" * 32, canonical, hashlib.sha256).hexdigest()
    return body, {
        "content-type": "application/json",
        "X-N8N-Profile": HMAC_PROFILE,
        "X-N8N-Timestamp": str(timestamp),
        "X-N8N-Nonce": nonce,
        "X-N8N-Signature": f"sha256={signature}",
    }


def test_signed_routes_have_no_project_selector_and_replay_is_rejected(tmp_path, monkeypatch):
    runtime, clock, _generated = _setup(tmp_path, monkeypatch)
    binding_id = _binding(runtime)
    app = FastAPI()
    app.include_router(
        build_n8n_agent_tasks_router(
            runtime=runtime,
            require_local=lambda _request: None,
            error_payload=lambda code, message, **kwargs: {"code": code, "message": message, **kwargs},
        )
    )
    client = TestClient(app)
    path = "/api/integrations/n8n/v1/agent/tasks"
    payload = {
        "request_id": "signed-execution-one",
        "workflow_id": "workflow-one",
        "workflow_revision": COMPILED_REVISION,
        "node_id": "agent-node-runtime",
        "agent_binding_id": binding_id,
        "input": {"message": "route input"},
    }
    body, headers = _signed(path, payload, clock(), "nonce_for_agent_task_01")
    response = client.post(path, content=body, headers=headers)
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    task_id = response.json()["task_id"]
    status_path = f"/api/integrations/n8n/v1/agent/tasks/{task_id}/status"
    status_body, status_headers = _signed(
        status_path, {"workflow_id": "workflow-one"}, clock(), "nonce_for_agent_status1"
    )
    status = client.post(status_path, content=status_body, headers=status_headers)
    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    assert status.json()["result"]["body"] == "Safe result"

    replay = client.post(path, content=body, headers=headers)
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "N8N_AGENT_REPLAY_DETECTED"

    injected = {**payload, "request_id": "signed-two", "project_id": "project-two"}
    body, headers = _signed(path, injected, clock(), "nonce_for_agent_task_02")
    rejected = client.post(path, content=body, headers=headers)
    assert rejected.status_code == 422

    browser = client.get(
        f"/api/integrations/n8n/agent-tasks/{task_id}", params={"project_id": "project-one"}
    )
    assert "result" not in browser.json()
    assert "route input" not in browser.text


def test_generating_task_is_requeued_after_restart_and_old_grant_is_revoked(tmp_path, monkeypatch):
    runtime, _clock, _generated = _setup(
        tmp_path, monkeypatch, boot_id="boot-one", full_audit=True
    )
    binding_id = _binding(runtime)
    approval_scope = _approval_scope(runtime)
    runtime.adopt_credential_alias(
        {"project_id": "project-one", "alias": "gmail-work", "credential_id": "internal-credential-77"}
    )
    pending = runtime.request_runtime_approval(
        {
            "request_id": "restart-action", "workflow_id": "workflow-one",
            "workflow_revision": COMPILED_REVISION, "node_id": approval_scope["node_id"],
            "credential_alias": "gmail-work", "target_kind": "email",
            "target": "recipient@example.test", "action": "send_email",
            "run_key": "restart-run", "task_id": None,
        }
    )
    runtime.decide_runtime_approval(
        pending["approval_id"], project_id="project-one",
        expected_digest=pending["request_digest"], approved=True, duration_minutes=60,
    )
    task = runtime.submit_task(
        {
            "request_id": "recover-one", "workflow_id": "workflow-one",
            "workflow_revision": COMPILED_REVISION, "node_id": "agent-node-runtime",
            "agent_binding_id": binding_id, "input": {"message": "recover"},
        }
    )
    with database.get_db_conn() as conn:
        conn.execute("UPDATE n8n_agent_tasks SET status='generating' WHERE task_id=?", (task["task_id"],))
    restarted = N8nAgentTaskRuntime(
        cipher=AesGcmContentCipher(lambda: b"k" * 32),
        hmac_secret_provider=lambda: b"h" * 32,
        generator=lambda _request: {"subject": "Recovered", "body": "Recovered"},
        skill_resolver=lambda _project, slug, sha: {
            "slug": slug, "sha256": sha, "instructions": "Use the approved tone."
        },
        credential_resolver=lambda _id: {},
        integration_permission_check=lambda *_args, **_kwargs: {"decision": "allow"},
        workflow_revision_resolver=lambda _workflow: {
            "active": True,
            "active_version_id": ACTIVE_VERSION,
        },
        boot_id="boot-two",
    )
    assert restarted.get_task_public(task["task_id"], project_id="project-one")["status"] == "queued"
    assert restarted.process_task(task["task_id"])["status"] == "succeeded"
    assert restarted.get_runtime_approval_for_n8n(
        pending["approval_id"], workflow_id="workflow-one"
    )["status"] == "revoked"
