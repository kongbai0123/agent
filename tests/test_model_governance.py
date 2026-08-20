from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import database
from model_governance import GovernanceError, ModelGovernanceService, parse_retry_after
from api.routes.model_governance import build_model_governance_router


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "governance.db"))
    database.init_db()
    value = ModelGovernanceService(database_module=database)
    value.initialize()
    return value


def test_credential_metadata_is_versioned_without_storing_secret(service):
    result = service.rotate_credential("nvidia", "cred_0123456789abcdef")
    assert result["credential_version_id"] == "cred_0123456789abcdef"
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    result = service.set_credential_metadata(
        "nvidia", expires_at=expires, expiry_source="user_declared", never_expires=False
    )
    assert 6 <= result["remaining_days"] <= 7
    with database.get_db_conn() as conn:
        payload = " ".join(str(value) for row in conn.execute("SELECT * FROM provider_credential_metadata") for value in row)
    assert "nvapi" not in payload


@pytest.mark.parametrize(
    ("status", "state", "model_scope"),
    [
        (401, "auth_required", ""),
        (402, "quota_exhausted", ""),
        (403, "permission_denied", "m"),
        (404, "model_unavailable", "m"),
        (429, "rate_limited", "m"),
    ],
)
def test_provider_failure_state_mapping(service, status, state, model_scope):
    service.rotate_credential("nvidia")
    result = service.observe_failure(
        "nvidia", model_id="m", endpoint="https://example.test/v1", status_code=status
    )
    assert result["state"] == state
    assert result["model_id"] == model_scope
    assert not service.operational_decision(
        "nvidia", model_id="m", endpoint="https://example.test/v1"
    ).allowed


def test_transport_opens_circuit_after_three_failures_and_success_recovers(service):
    service.rotate_credential("nvidia")
    for _ in range(3):
        state = service.observe_failure(
            "nvidia", model_id="m", endpoint="https://example.test/v1", transport_error=True
        )
    assert state["state"] == "unreachable"
    assert state["retry_at"]
    recovered = service.observe_success("nvidia", model_id="m", endpoint="https://example.test/v1")
    assert recovered["state"] == "healthy"
    assert recovered["failure_streak"] == 0


def test_cooldown_allows_only_one_half_open_probe(service):
    current = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    service.clock = lambda: current[0]
    service.rotate_credential("nvidia")
    service.observe_failure(
        "nvidia",
        model_id="m",
        endpoint="https://example.test/v1",
        status_code=429,
        retry_after="1",
    )
    current[0] += timedelta(seconds=2)
    assert service.operational_decision(
        "nvidia", model_id="m", endpoint="https://example.test/v1"
    ).allowed
    second = service.operational_decision(
        "nvidia", model_id="m", endpoint="https://example.test/v1"
    )
    assert not second.allowed
    assert second.code == "PROVIDER_HALF_OPEN"


def test_removing_credential_clears_live_state_but_keeps_usage(service):
    service.rotate_credential("nvidia")
    service.observe_failure("nvidia", status_code=401, endpoint="https://example.test/v1")
    service.record_usage(
        call_id="c1", provider_id="nvidia", model_id="m", capability="chat"
    )
    service.clear_credential("nvidia")
    assert service.credential_metadata("nvidia")["credential_version_id"] == ""
    assert service.state("nvidia", endpoint="https://example.test/v1")["state"] == "unknown"
    assert service.usage()["totals"]["requests"] == 1


def test_retry_after_supports_seconds_and_caps_http_date():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert parse_retry_after("30", now=now) == now + timedelta(seconds=30)
    assert parse_retry_after("99999", now=now) == now + timedelta(hours=1)


def test_budget_warns_and_blocks_at_project_or_global_limit(service):
    budget = service.put_budget(
        "global", "global", revision=0, timezone_name="Asia/Taipei",
        policy={"daily": {"requests": 2}},
    )
    assert budget["revision"] == 1
    first = service.budget_decision(project_id="p1", run_id="r1", call_id="c1")
    assert first.allowed
    service.record_usage(call_id="c1", provider_id="nvidia", model_id="m", capability="chat", project_id="p1", run_id="r1")
    blocked = service.budget_decision(project_id="p1", run_id="r2", call_id="c2")
    assert not blocked.allowed
    assert blocked.code == "MODEL_BUDGET_EXCEEDED"
    with pytest.raises(GovernanceError) as conflict:
        service.put_budget("global", "global", revision=0, timezone_name="Asia/Taipei", policy={})
    assert conflict.value.code == "BUDGET_REVISION_CONFLICT"


def test_pending_reservation_prevents_concurrent_budget_overshoot(service):
    service.put_budget(
        "global",
        "global",
        revision=0,
        timezone_name="Asia/Taipei",
        policy={"daily": {"requests": 2}},
    )
    assert service.budget_decision(
        project_id="p1", run_id="r1", call_id="pending-1"
    ).allowed
    blocked = service.budget_decision(
        project_id="p1", run_id="r2", call_id="pending-2"
    )
    assert not blocked.allowed
    assert blocked.code == "MODEL_BUDGET_EXCEEDED"


def test_budget_override_is_single_use_and_bound_to_run(service):
    override = service.create_budget_override(project_id="p1", run_id="r1")
    assert not service.consume_budget_override(override["override_id"], project_id="p1", run_id="wrong")
    assert service.consume_budget_override(override["override_id"], project_id="p1", run_id="r1")
    assert not service.consume_budget_override(override["override_id"], project_id="p1", run_id="r1")


def test_routing_defaults_to_ask_and_remembered_consent_becomes_project_policy(service):
    candidates = [
        {"name": "nvidia::chat-model", "provider": "nvidia", "profile": {"supports_chat": True, "supports_tools": True}},
    ]
    result = service.resolve_route(
        project_id="p1", run_id="r1", requested_model="nvidia::rerank-model",
        requirements={"kind": "chat", "tools": True, "text": True, "documents": True},
        candidates=candidates,
    )
    assert result["status"] == "approval_required"
    approved = service.approve_proposal(result["proposal_id"], remember_project=True)
    assert approved["status"] == "approved"
    policy = service.get_routing_policy("p1")
    assert policy["mode"] == "auto_within_policy"
    assert "nvidia" in policy["allowed_providers"]
    assert policy["data_consent"]["documents"] is True
    assert service.consume_proposal(result["proposal_id"], project_id="p1", requested_model="nvidia::rerank-model") == "nvidia::chat-model"
    assert service.consume_proposal(result["proposal_id"], project_id="p1", requested_model="nvidia::rerank-model") is None


def test_initialize_invalidates_unfinished_ephemeral_authority(service):
    proposal = service.resolve_route(
        project_id="p1", run_id="r1", requested_model="bad",
        requirements={"kind": "chat", "text": True},
        candidates=[{"name": "good", "provider": "ollama", "profile": {"supports_chat": True}}],
    )
    service.approve_proposal(proposal["proposal_id"], remember_project=False)
    override = service.create_budget_override(project_id="p1", run_id="r1")
    counts = service.initialize()
    assert counts["routing_proposals"] >= 1
    assert counts["budget_overrides"] >= 1
    assert service.consume_proposal(proposal["proposal_id"], project_id="p1", requested_model="bad") is None
    assert not service.consume_budget_override(override["override_id"], project_id="p1", run_id="r1")


def test_governance_api_uses_revision_and_returns_local_observation(service):
    app = FastAPI()
    app.include_router(build_model_governance_router(
        service=service,
        load_settings=lambda: {"model_providers": [{"id": "nvidia", "enabled": True, "selected_model": "m", "base_url": "https://example.test/v1"}]},
        model_inventory=lambda: [],
        require_local=None,
        require_project=lambda project_id: {"id": project_id} if project_id == "p1" else None,
        error_payload=lambda code, message, detail=None, recoverable=False: {"code": code, "message": message, "detail": detail, "recoverable": recoverable},
    ))
    with TestClient(app) as client:
        overview = client.get("/api/model-governance/overview?project_id=p1")
        assert overview.status_code == 200
        assert overview.json()["usage"]["source"] == "local_observation"
        assert overview.json()["usage"]["historical_runs"]["budget_eligible"] is False
        saved = client.put("/api/model-governance/budgets/project/p1", json={"revision": 0, "timezone": "Asia/Taipei", "policy": {"daily": {"requests": 10}}})
        assert saved.status_code == 200
        stale = client.put("/api/model-governance/budgets/project/p1", json={"revision": 0, "timezone": "Asia/Taipei", "policy": {}})
        assert stale.status_code == 409
