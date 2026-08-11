from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import database  # noqa: E402
from hermes import (  # noqa: E402
    HermesConfig,
    HermesRunSnapshot,
    HermesUnavailableError,
)
from hermes_approval_store import (  # noqa: E402
    HermesApprovalConflictError,
    PersistentHermesApprovalStore,
    approval_event_fingerprint,
)
from hermes_integration import HermesIntegrationManager  # noqa: E402
from hermes_project_skills_bridge import HermesProjectSkillsAttachment  # noqa: E402


def connection_factory(path):
    @contextmanager
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return connect


class FakeHealth:
    def __init__(self):
        self.value = {
            "status": "unknown",
            "reported_status": "unknown",
            "reason": "not_checked",
        }

    def record_probe(self, status, *, latency_ms=None, reason):
        self.value = {
            "status": status.value,
            "reported_status": status.value,
            "reason": reason,
            "latency_ms": latency_ms,
        }

    def snapshot(self):
        return dict(self.value)


class FakeOperations:
    def __init__(self, use_hermes=True):
        self.health = FakeHealth()
        self.use_hermes = use_hermes
        self.completions = []
        self.probe_failures = 0
        self.tool_policy_denials = 0
        self.fallbacks = 0

    def decide(self, _subject):
        return SimpleNamespace(
            use_hermes=self.use_hermes,
            reason="rollout_all" if self.use_hermes else "rollout_disabled",
        )

    def complete(
        self,
        decision,
        *,
        success,
        failure_kind="",
        record_fallback=True,
    ):
        self.completions.append(
            (decision, success, failure_kind, record_fallback)
        )

    def record_fallback(self):
        self.fallbacks += 1

    def record_probe_failure(self):
        self.probe_failures += 1

    def record_tool_policy_denial(self):
        self.tool_policy_denials += 1

    def status(self):
        return {
            "health": self.health.snapshot(),
            "health_gate": {
                "allowed": self.health.snapshot().get("status") == "healthy"
            },
            "rollout": {"mode": "all" if self.use_hermes else "disabled"},
            "metrics": {
                "available": True,
                "totals": {
                    "probe_failure": self.probe_failures,
                    "tool_policy_denial": self.tool_policy_denials,
                    "fallback": self.fallbacks,
                },
            },
            "recent_decisions": [
                {"subject_hash": "correlatable-private-session-hash"}
            ],
            "manifest": {"api_key_ref_configured": True},
        }


class FakeClient:
    def __init__(self, features=None):
        self._features = dict(features or {})

    def health(self):
        return {"status": "ok", "secret": "must-not-escape"}

    def capabilities(self):
        return {
            "features": self._features,
            "credentials": "must-not-escape",
        }


class FakeMappings:
    def __init__(self):
        self.records = {}

    def get_run(self, run_id):
        return self.records.get(run_id)


class FakeRuns:
    def __init__(self, features=None):
        self.client = FakeClient(features)
        self.mappings = FakeMappings()
        self.create_calls = []
        self.approvals = []

    def create_run(self, run_id, session_id, input_text, **kwargs):
        self.create_calls.append((run_id, session_id, input_text, kwargs))
        return HermesRunSnapshot(run_id, session_id, "upstream-1", "started", {})

    def resolve_approval(self, run_id, *, choice):
        self.approvals.append((run_id, choice))
        return HermesRunSnapshot(run_id, "session", "upstream", "running", {})


class FakeSkills:
    def prepare(self, session_id, _query, *, run_id, consume_turn):
        assert consume_turn is True
        session = database.get_session(session_id)
        return attachment(session_id, session.get("project_id"), run_id)


def attachment(session_id, project_id, run_id="run-1"):
    return HermesProjectSkillsAttachment(
        session_id=session_id,
        project_id=project_id,
        workbench_run_id=run_id,
        instructions="scoped instructions",
        sources=(),
        truncated=False,
    )


def config(enabled=True):
    return HermesConfig(
        enabled=enabled,
        api_key="0123456789abcdef0123456789abcdef" if enabled else "",
    )


def manager(
    tmp_path,
    *,
    features=None,
    enabled=True,
    tools_enabled=True,
    tool_project_id=None,
    tool_capability="hermes.tool",
    tool_policy_profile="no-tools-v1",
):
    runs = FakeRuns(features)
    item = HermesIntegrationManager(
        config=config(enabled),
        runs=runs,
        project_skills=FakeSkills(),
        operations=FakeOperations(),
        tools_enabled=tools_enabled,
        tool_project_id=tool_project_id,
        tool_capability=tool_capability,
        deployment_mode="docker" if tool_project_id else "native",
        tool_policy_profile=tool_policy_profile,
        approval_store=PersistentHermesApprovalStore(
            connection_factory(tmp_path / "approvals.db")
        ),
    )
    return item, runs


def all_run_features():
    return {
        "run_submission": True,
        "run_status": True,
        "run_events_sse": True,
        "run_stop": True,
        "run_approval_response": True,
    }


def test_probe_uses_v0182_approval_feature_and_redacts_status(tmp_path):
    item, _runs = manager(tmp_path, features=all_run_features())
    result = item.probe()
    status = item.status()

    assert result["success"] is True
    assert result["features"]["run_approval_response"] is True
    assert status["tools_enabled"] is True
    assert status["base_url"] == "http://127.0.0.1:8642"
    assert status["api_key_configured"] is True
    assert status["health_gate"]["allowed"] is True
    assert status["metrics"]["available"] is True
    assert "recent_decisions" not in status["operations"]
    encoded = json.dumps({"probe": result, "status": status})
    assert "0123456789abcdef" not in encoded
    assert "must-not-escape" not in encoded
    assert "correlatable-private-session-hash" not in encoded


def test_legacy_wrong_approval_feature_fails_readiness(tmp_path):
    features = all_run_features()
    features.pop("run_approval_response")
    features["run_approval"] = True
    item, _runs = manager(tmp_path, features=features)
    assert item.probe()["success"] is False
    assert item.status()["tools_enabled"] is False
    assert item.operations.probe_failures == 1


def test_start_run_derives_project_or_unscoped_scope_from_session(tmp_path):
    database.init_db()
    project_id = "hermes-project-scope-test"
    session_id = "hermes-session-scope-test"
    database.create_project(project_id, "Hermes", str(tmp_path / "project"))
    database.create_session(session_id, project_id=project_id)
    item, runs = manager(tmp_path, features=all_run_features())

    item.start_run(
        workbench_run_id="run-project",
        workbench_session_id=session_id,
        input_text="hello",
        attachment=attachment(session_id, project_id),
    )
    assert runs.create_calls[-1][3]["session_scope"] == project_id

    unscoped_session = "hermes-session-unscoped-test"
    database.create_session(unscoped_session)
    item.start_run(
        workbench_run_id="run-unscoped",
        workbench_session_id=unscoped_session,
        input_text="hello",
        attachment=attachment(unscoped_session, None),
    )
    assert runs.create_calls[-1][3]["session_scope"] == "unscoped"


def test_mismatched_project_attachment_fails_closed(tmp_path):
    database.init_db()
    session_id = "hermes-session-mismatch-test"
    database.create_session(session_id)
    item, _runs = manager(tmp_path, features=all_run_features())
    with pytest.raises(Exception):
        item.start_run(
            workbench_run_id="run-mismatch",
            workbench_session_id=session_id,
            input_text="hello",
            attachment=attachment(session_id, "another-project"),
        )


def test_readonly_tool_routing_rejects_sessions_from_another_project(tmp_path):
    database.init_db()
    allowed_project = "hermes-readonly-allowed-project"
    other_project = "hermes-readonly-other-project"
    database.create_project(allowed_project, "Allowed", str(tmp_path / "allowed"))
    database.create_project(other_project, "Other", str(tmp_path / "other"))
    allowed_session = "hermes-readonly-allowed-session"
    other_session = "hermes-readonly-other-session"
    database.create_session(allowed_session, project_id=allowed_project)
    database.create_session(other_session, project_id=other_project)
    item, _runs = manager(
        tmp_path,
        features=all_run_features(),
        tool_project_id=allowed_project,
        tool_capability="hermes.project.read",
        tool_policy_profile="project-readonly-v1",
    )

    assert item.decide(other_session).reason == "readonly_project_mismatch"
    assert item.decide(other_session).use_hermes is False
    assert item.decide(allowed_session).use_hermes is True
    public_status = item.status()
    assert public_status["tool_project_scoped"] is True
    assert "tool_project_id" not in public_status
    assert allowed_project not in json.dumps(public_status)


def test_submission_unknown_never_allows_basic_fallback(tmp_path):
    item, runs = manager(tmp_path, features=all_run_features())
    error = HermesUnavailableError("timeout")
    assert item.fallback_allowed("run-new", error, token_emitted=False) is True
    assert item.operations.fallbacks == 1
    runs.mappings.records["run-unknown"] = SimpleNamespace(
        status="submission_unknown", hermes_run_id=""
    )
    assert item.fallback_allowed("run-unknown", error, token_emitted=False) is False
    runs.mappings.records["run-bound"] = SimpleNamespace(
        status="running", hermes_run_id="upstream"
    )
    assert item.fallback_allowed("run-bound", error, token_emitted=False) is False
    assert item.operations.fallbacks == 1


def test_failure_metric_does_not_claim_fallback_before_replay_is_allowed(tmp_path):
    item, _runs = manager(tmp_path, features=all_run_features())
    decision = item.decide("fallback-evidence-session")

    item.complete(decision, success=False, failure_kind="unavailable")

    assert item.operations.completions[-1][1:] == (
        False,
        "unavailable",
        False,
    )
    assert item.operations.fallbacks == 0


def test_approval_request_is_durable_deduplicated_and_once_only(tmp_path):
    database.init_db()
    session_id = "hermes-session-approval-test"
    database.create_session(session_id)
    item, runs = manager(tmp_path, features=all_run_features())
    event = {
        "event": "approval.request",
        "timestamp": 123.0,
        "command": "echo 0123456789abcdef0123456789abcdef",
        "choices": ["once", "session", "always", "deny"],
    }
    first = item.register_approval(
        workbench_run_id="approval-run",
        workbench_session_id=session_id,
        project_id=None,
        event=event,
    )
    repeated = item.register_approval(
        workbench_run_id="approval-run",
        workbench_session_id=session_id,
        project_id=None,
        event=event,
    )

    assert first == repeated
    assert first.status == "pending"
    assert first.choices == ("once", "deny")
    assert "0123456789abcdef" not in first.summary
    restored = PersistentHermesApprovalStore(
        connection_factory(tmp_path / "approvals.db")
    ).get(first.approval_id)
    assert restored == first

    decided = item.resolve_approval(
        first.approval_id,
        choice="once",
        rationale="Approved for this call only.",
    )
    assert decided.status == "approved_once"
    assert runs.approvals == [("approval-run", "once")]
    with pytest.raises(HermesApprovalConflictError):
        item.resolve_approval(
            first.approval_id,
            choice="once",
            rationale="Do it again.",
        )


def test_approval_after_restart_is_explicitly_denied_without_live_nonce(tmp_path):
    database.init_db()
    session_id = "hermes-session-restart-approval-test"
    database.create_session(session_id)
    first_manager, _first_runs = manager(tmp_path, features=all_run_features())
    pending = first_manager.register_approval(
        workbench_run_id="restart-approval-run",
        workbench_session_id=session_id,
        project_id=None,
        event={
            "event": "approval.request",
            "timestamp": 456.0,
            "tool": "project_read_file",
            "choices": ["once", "deny"],
        },
    )

    restarted, restarted_runs = manager(tmp_path, features=all_run_features())
    decided = restarted.resolve_approval(
        pending.approval_id,
        choice="once",
        rationale="The durable record survived, but the nonce did not.",
    )

    assert decided.status == "denied_missing_live_grant"
    assert restarted_runs.approvals == [("restart-approval-run", "deny")]


def test_policy_denied_approval_records_only_a_redacted_operational_counter(tmp_path):
    database.init_db()
    session_id = "hermes-session-policy-metric-test"
    database.create_session(session_id)
    item, runs = manager(
        tmp_path,
        features=all_run_features(),
        tools_enabled=False,
    )

    record = item.register_approval(
        workbench_run_id="policy-metric-run",
        workbench_session_id=session_id,
        project_id=None,
        event={
            "event": "approval.request",
            "tool": "terminal",
            "command": "cat C:/private/secret.txt",
            "choices": ["once", "deny"],
        },
    )
    status = item.status()

    assert record.status == "denied_policy"
    assert runs.approvals == [("policy-metric-run", "deny")]
    assert item.operations.tool_policy_denials == 1
    assert status["metrics"]["totals"]["tool_policy_denial"] == 1
    encoded = json.dumps(status, sort_keys=True)
    assert "private/secret" not in encoded


def test_approval_fingerprint_is_stable_for_sse_replay():
    event = {"timestamp": 100, "command": "safe command", "tool": "terminal"}
    assert approval_event_fingerprint("run-1", event) == approval_event_fingerprint(
        "run-1", dict(event)
    )
