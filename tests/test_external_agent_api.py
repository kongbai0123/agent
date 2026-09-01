from __future__ import annotations

import base64
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from api.routes.external_agent_api import build_external_agent_api_router
from connector_secrets import ConnectorSecretStore
from external_agent_api import (
    ExternalAgentApiError,
    ExternalAgentApiService,
    ExternalAgentApiStore,
)


def _connection_factory(path: Path):
    @contextmanager
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    return connect


def _protect(value: str) -> str:
    return base64.b64encode(f"sealed:{value}".encode()).decode()


def _unprotect(value: str) -> str:
    decoded = base64.b64decode(value).decode()
    assert decoded.startswith("sealed:")
    return decoded.removeprefix("sealed:")


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


@pytest.fixture()
def components(tmp_path: Path):
    db_path = tmp_path / "workbench.db"
    vault_path = tmp_path / "secrets" / "external-api.json"
    clock = MutableClock()
    store = ExternalAgentApiStore(_connection_factory(db_path))
    vault = ConnectorSecretStore(
        vault_path, protect=_protect, unprotect=_unprotect
    )
    service = ExternalAgentApiService(
        store=store,
        secret_store=vault,
        project_exists=lambda project_id: project_id in {"project-a", "project-b"},
        policy_guard=lambda _project_id, _scope: True,
        clock=clock,
        installation_label="測試工作站",
    )
    service.initialize()
    return service, store, vault, clock, db_path, vault_path


def _issue(service: ExternalAgentApiService, **overrides):
    payload = {
        "name": "n8n production",
        "project_id": "project-a",
        "scopes": [
            "runs:create",
            "runs:read",
            "runs:cancel",
            "capabilities:read",
        ],
        "expires_at": None,
        "rate_limit_per_minute": 60,
        "request_limit_daily": 1000,
    }
    payload.update(overrides)
    return service.issue_key(**payload)


def _auth(secret: str) -> str:
    return f"Bearer {secret}"


def test_key_is_installation_bound_and_plaintext_never_enters_sqlite(components):
    service, store, vault, clock, db_path, vault_path = components
    issued = _issue(service)
    secret = issued["secret"]

    assert secret.startswith("wbk_")
    assert issued["notice"].endswith("完整金鑰只會顯示這一次。")
    assert issued["api_key"]["prefix"] in secret
    assert issued["api_key"]["status"] == "active"

    listing = service.list_keys(api_base_url="http://127.0.0.1:8000/api/public/v1")
    assert listing == {
        "success": True,
        "installation": {
            "id": listing["installation"]["id"],
            "label": "測試工作站",
            "api_base_url": "http://127.0.0.1:8000/api/public/v1",
            "created_at": "2026-08-31T09:00:00+00:00",
        },
        "credential_recovery_required": False,
        "api_keys": [listing["api_keys"][0]],
    }
    assert "secret" not in listing["api_keys"][0]
    assert secret.encode() not in db_path.read_bytes()

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT key_prefix, key_digest FROM external_api_keys"
        ).fetchone()
    assert row[0] == issued["api_key"]["prefix"]
    assert len(row[1]) == 64
    assert row[1] != secret

    # Reopening the same installation and DPAPI vault preserves verification.
    reopened = ExternalAgentApiService(
        store=ExternalAgentApiStore(_connection_factory(db_path)),
        secret_store=ConnectorSecretStore(
            vault_path, protect=_protect, unprotect=_unprotect
        ),
        project_exists=lambda project_id: project_id == "project-a",
        policy_guard=lambda _project_id, _scope: True,
        clock=clock,
    )
    reopened.initialize()
    principal = reopened.authenticate(
        _auth(secret), required_scope="runs:read", action="test.read"
    )
    assert principal.project_id == "project-a"


def test_database_copy_without_machine_dpapi_pepper_cannot_use_key(components, tmp_path):
    service, store, vault, clock, db_path, vault_path = components
    issued = _issue(service)
    copied_db = tmp_path / "copied" / "workbench.db"
    copied_db.parent.mkdir()
    shutil.copy2(db_path, copied_db)

    other_machine = ExternalAgentApiService(
        store=ExternalAgentApiStore(_connection_factory(copied_db)),
        secret_store=ConnectorSecretStore(
            tmp_path / "other-machine" / "vault.json",
            protect=_protect,
            unprotect=_unprotect,
        ),
        project_exists=lambda _project_id: True,
        policy_guard=lambda _project_id, _scope: True,
        clock=clock,
        installation_label="另一台電腦",
    )
    other_machine.initialize()

    with pytest.raises(ExternalAgentApiError) as raised:
        other_machine.authenticate(
            _auth(issued["secret"]),
            required_scope="runs:read",
            action="test.read",
        )
    assert raised.value.code == "EXTERNAL_API_CREDENTIAL_RECOVERY_REQUIRED"
    assert other_machine.credential_recovery_required is True
    assert len(other_machine.list_keys()["api_keys"]) == 1

    reset = other_machine.reset_installation(confirmation="RESET_EXTERNAL_API")
    assert reset["revoked_key_count"] == 1
    assert reset["installation"]["id"] != service.installation()["id"]
    assert other_machine.list_keys()["api_keys"] == []
    replacement = _issue(other_machine)
    assert other_machine.authenticate(
        _auth(replacement["secret"]),
        required_scope="runs:read",
        action="test.recovered",
    ).project_id == "project-a"
    assert issued["secret"].encode() not in copied_db.read_bytes()


def test_restart_marks_unfinished_idempotent_dispatch_unknown(components):
    service, _store, _vault, clock, db_path, vault_path = components
    issued = _issue(service)
    principal = service.authenticate(
        _auth(issued["secret"]),
        required_scope="runs:create",
        action="test.reserve",
    )
    first = service.reserve_idempotent_run(
        principal=principal,
        idempotency_key="restart-safe-0001",
        request_payload={"message": "hello", "model": None, "use_rag": False},
    )
    assert first["state"] == "reserved"

    reopened = ExternalAgentApiService(
        store=ExternalAgentApiStore(_connection_factory(db_path)),
        secret_store=ConnectorSecretStore(
            vault_path, protect=_protect, unprotect=_unprotect
        ),
        project_exists=lambda project_id: project_id == "project-a",
        policy_guard=lambda _project_id, _scope: True,
        clock=clock,
    )
    reopened.initialize()
    replay_principal = reopened.authenticate(
        _auth(issued["secret"]),
        required_scope="runs:create",
        action="test.replay",
    )
    replay = reopened.reserve_idempotent_run(
        principal=replay_principal,
        idempotency_key="restart-safe-0001",
        request_payload={"message": "hello", "model": None, "use_rag": False},
    )

    assert replay["replayed"] is True
    assert replay["state"] == "dispatch_unknown"
    assert replay["response"]["status"] == "failed"
    assert replay["response"]["error"]["code"] == "EXTERNAL_API_DISPATCH_UNKNOWN"


def test_expired_disabled_and_revoked_keys_fail_closed(components):
    service, store, vault, clock, db_path, vault_path = components
    issued = _issue(service, expires_at=clock() + timedelta(minutes=5))
    secret = issued["secret"]
    key = issued["api_key"]

    disabled = service.replace_key_policy(
        key_id=key["id"],
        expected_revision=key["revision"],
        enabled=False,
        scopes=key["scopes"],
        expires_at=key["expires_at"],
        rate_limit_per_minute=key["rate_limit_per_minute"],
        request_limit_daily=key["request_limit_daily"],
    )["api_key"]
    assert disabled["status"] == "disabled"
    with pytest.raises(ExternalAgentApiError) as raised:
        service.authenticate(
            _auth(secret), required_scope="runs:read", action="test.disabled"
        )
    assert raised.value.code == "EXTERNAL_API_KEY_INACTIVE"

    enabled = service.replace_key_policy(
        key_id=key["id"],
        expected_revision=disabled["revision"],
        enabled=True,
        scopes=key["scopes"],
        expires_at=key["expires_at"],
        rate_limit_per_minute=key["rate_limit_per_minute"],
        request_limit_daily=key["request_limit_daily"],
    )["api_key"]
    clock.advance(minutes=6)
    with pytest.raises(ExternalAgentApiError) as raised:
        service.authenticate(
            _auth(secret), required_scope="runs:read", action="test.expired"
        )
    assert raised.value.code == "EXTERNAL_API_KEY_EXPIRED"

    revoked = service.revoke_key(
        key_id=key["id"], expected_revision=enabled["revision"]
    )["api_key"]
    assert revoked["status"] == "revoked"
    with pytest.raises(ExternalAgentApiError) as raised:
        service.authenticate(
            _auth(secret), required_scope="runs:read", action="test.revoked"
        )
    assert raised.value.code == "EXTERNAL_API_KEY_INACTIVE"


def test_scope_and_project_run_boundaries_are_enforced(components):
    service, store, vault, clock, db_path, vault_path = components
    read_key = _issue(service, scopes=["runs:read"])
    with pytest.raises(ExternalAgentApiError) as raised:
        service.authenticate(
            _auth(read_key["secret"]),
            required_scope="runs:create",
            action="test.create",
        )
    assert raised.value.code == "EXTERNAL_API_SCOPE_DENIED"

    project_a = service.authenticate(
        _auth(read_key["secret"]),
        required_scope="runs:read",
        action="test.read",
    )
    service.bind_run(principal=project_a, run_id="run_project_a_001")
    project_b_key = _issue(
        service,
        name="project b",
        project_id="project-b",
        scopes=["runs:read"],
    )
    project_b = service.authenticate(
        _auth(project_b_key["secret"]),
        required_scope="runs:read",
        action="test.read",
    )
    with pytest.raises(ExternalAgentApiError) as raised:
        service.require_run(principal=project_b, run_id="run_project_a_001")
    assert raised.value.code == "EXTERNAL_API_RUN_NOT_FOUND"


def test_minute_and_daily_request_limits_are_independent(components):
    service, store, vault, clock, db_path, vault_path = components
    issued = _issue(
        service,
        scopes=["runs:read"],
        rate_limit_per_minute=1,
        request_limit_daily=1,
    )
    authorization = _auth(issued["secret"])
    service.authenticate(
        authorization, required_scope="runs:read", action="test.first"
    )
    with pytest.raises(ExternalAgentApiError) as raised:
        service.authenticate(
            authorization, required_scope="runs:read", action="test.minute"
        )
    assert raised.value.code == "EXTERNAL_API_RATE_LIMITED"
    assert raised.value.retry_after == 60

    clock.advance(minutes=1)
    with pytest.raises(ExternalAgentApiError) as raised:
        service.authenticate(
            authorization, required_scope="runs:read", action="test.daily"
        )
    assert raised.value.code == "EXTERNAL_API_DAILY_LIMIT_REACHED"
    assert raised.value.retry_after > 0


def test_unified_policy_guard_denies_before_consuming_request_quota(components):
    service, store, vault, clock, db_path, vault_path = components
    issued = _issue(
        service,
        scopes=["runs:read"],
        rate_limit_per_minute=1,
        request_limit_daily=1,
    )
    decision = {"allowed": False}
    guarded = ExternalAgentApiService(
        store=ExternalAgentApiStore(_connection_factory(db_path)),
        secret_store=ConnectorSecretStore(
            vault_path, protect=_protect, unprotect=_unprotect
        ),
        project_exists=lambda project_id: project_id == "project-a",
        policy_guard=lambda project_id, scope: (
            project_id == "project-a"
            and scope == "runs:read"
            and decision["allowed"]
        ),
        clock=clock,
    )
    guarded.initialize()

    with pytest.raises(ExternalAgentApiError) as raised:
        guarded.authenticate(
            _auth(issued["secret"]),
            required_scope="runs:read",
            action="test.policy",
        )
    assert raised.value.code == "EXTERNAL_API_POLICY_DENIED"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT daily_request_count FROM external_api_keys WHERE key_id = ?",
            (issued["api_key"]["id"],),
        ).fetchone()[0] == 0

    decision["allowed"] = True
    guarded.authenticate(
        _auth(issued["secret"]),
        required_scope="runs:read",
        action="test.policy",
    )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT daily_request_count FROM external_api_keys WHERE key_id = ?",
            (issued["api_key"]["id"],),
        ).fetchone()[0] == 1


def test_missing_unified_policy_guard_fails_closed_without_consuming_quota(components):
    service, store, vault, clock, db_path, vault_path = components
    issued = _issue(service, scopes=["runs:read"])
    service.policy_guard = None
    with pytest.raises(ExternalAgentApiError) as raised:
        service.authenticate(
            _auth(issued["secret"]),
            required_scope="runs:read",
            action="test.no-policy",
        )
    assert raised.value.code == "EXTERNAL_API_POLICY_UNAVAILABLE"
    assert raised.value.status_code == 503
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT daily_request_count FROM external_api_keys WHERE key_id = ?",
            (issued["api_key"]["id"],),
        ).fetchone()[0] == 0


def test_rotation_revokes_old_secret_and_returns_new_secret_once(components):
    service, store, vault, clock, db_path, vault_path = components
    issued = _issue(service, scopes=["runs:read"])
    rotated = service.rotate_key(
        key_id=issued["api_key"]["id"],
        expected_revision=issued["api_key"]["revision"],
    )
    assert rotated["secret"] != issued["secret"]
    assert rotated["api_key"]["project_id"] == "project-a"
    with pytest.raises(ExternalAgentApiError) as raised:
        service.authenticate(
            _auth(issued["secret"]),
            required_scope="runs:read",
            action="test.old",
        )
    assert raised.value.code == "EXTERNAL_API_KEY_INACTIVE"
    assert service.authenticate(
        _auth(rotated["secret"]),
        required_scope="runs:read",
        action="test.new",
    ).project_id == "project-a"
    assert issued["secret"].encode() not in db_path.read_bytes()
    assert rotated["secret"].encode() not in db_path.read_bytes()


def _error_payload(code, message, detail=None, recoverable=True, suggestions=None):
    return {
        "success": False,
        "code": code,
        "message": message,
        "recoverable": recoverable,
    }


def test_public_routes_inject_existing_runtime_and_preserve_project_scope(components):
    service, store, vault, clock, db_path, vault_path = components
    calls = []

    async def submit(run_id, payload, auth_context):
        # The public boundary must bind its server-generated ID before dispatch.
        assert store.require_run_project(
            run_id=run_id, project_id=auth_context["project_id"]
        )["run_id"] == run_id
        calls.append(("submit", run_id, payload, auth_context))
        return {
            "run_id": run_id,
            "project_id": auth_context["project_id"],
            "session_id": "session_server_created",
            "status": "queued",
        }

    def read(run_id, auth_context):
        calls.append(("read", run_id, auth_context))
        return {
            "run_id": run_id,
            "project_id": auth_context["project_id"],
            "status": "completed",
            "answer": "完成",
        }

    def cancel(run_id, auth_context):
        calls.append(("cancel", run_id, auth_context))
        return {
            "run_id": run_id,
            "project_id": auth_context["project_id"],
            "status": "cancelled",
        }

    def capabilities(auth_context):
        calls.append(("capabilities", auth_context))
        return {
            "project_id": auth_context["project_id"],
            "chat": True,
            "streaming": False,
        }

    app = FastAPI()
    app.include_router(
        build_external_agent_api_router(
            service=service,
            require_local=lambda _request: None,
            error_payload=_error_payload,
            submit_run=submit,
            get_run=read,
            cancel_run=cancel,
            capabilities=capabilities,
        )
    )
    client = TestClient(app)
    created_key = client.post(
        "/api/integration-center/api-keys",
        json={
            "name": "external n8n",
            "project_id": "project-a",
            "scopes": [
                "runs:create",
                "runs:read",
                "runs:cancel",
                "capabilities:read",
            ],
            "expires_at": "2035-09-01T23:59:59+08:00",
            "rate_limit_per_minute": 60,
            "request_limit_daily": 1000,
        },
    )
    assert created_key.status_code == 201
    assert created_key.headers["cache-control"] == "no-store"
    issued = created_key.json()

    rotated_key = client.post(
        f"/api/integration-center/api-keys/{issued['api_key']['id']}/rotate",
        json={"revision": issued["api_key"]["revision"]},
    )
    assert rotated_key.status_code == 201
    assert rotated_key.headers["cache-control"] == "no-store"
    issued = rotated_key.json()
    headers = {
        "Authorization": _auth(issued["secret"]),
        "Idempotency-Key": "external-run-request-0001",
    }

    catalog = client.get("/api/public/v1/capabilities", headers=headers)
    assert catalog.status_code == 200
    assert catalog.json()["project_id"] == "project-a"
    assert catalog.json()["capabilities"]["chat"] is True

    created = client.post(
        "/api/public/v1/runs",
        headers=headers,
        json={"message": "請整理專案", "use_rag": True},
    )
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    assert run_id.startswith("run_")
    assert created.json()["idempotency_replayed"] is False
    assert calls[1][3]["project_id"] == "project-a"
    assert "authorization" not in calls[1][3]
    assert "session_id" not in calls[1][2]
    assert "metadata" not in calls[1][2]

    replayed = client.post(
        "/api/public/v1/runs",
        headers=headers,
        json={"message": "請整理專案", "use_rag": True},
    )
    assert replayed.status_code == 202
    assert replayed.json()["run_id"] == run_id
    assert replayed.json()["idempotency_replayed"] is True
    assert len([call for call in calls if call[0] == "submit"]) == 1

    conflict = client.post(
        "/api/public/v1/runs",
        headers=headers,
        json={"message": "不同內容", "use_rag": True},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "EXTERNAL_API_IDEMPOTENCY_CONFLICT"
    assert b"external-run-request-0001" not in db_path.read_bytes()

    forbidden_input = client.post(
        "/api/public/v1/runs",
        headers={**headers, "Idempotency-Key": "external-run-request-0002"},
        json={
            "message": "不應接受外部 session",
            "session_id": "session_attacker_selected",
            "metadata": {"arbitrary": "data"},
        },
    )
    assert forbidden_input.status_code == 422

    status = client.get(
        f"/api/public/v1/runs/{run_id}", headers=headers
    )
    assert status.status_code == 200
    assert status.json()["answer"] == "完成"

    cancelled = client.post(
        f"/api/public/v1/runs/{run_id}/cancel", headers=headers
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    missing = client.get("/api/public/v1/runs/run_other_12345678", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "EXTERNAL_API_RUN_NOT_FOUND"

    listing = client.get("/api/integration-center/api-keys").json()
    assert listing["installation"]["api_base_url"].endswith("/api/public/v1")
    active_key = next(
        item for item in listing["api_keys"] if item["id"] == issued["api_key"]["id"]
    )
    assert active_key["last_used_at"] is not None
    assert "secret" not in active_key

    reset = client.post(
        "/api/integration-center/installation/reset",
        json={"confirmation": "RESET_EXTERNAL_API"},
    )
    assert reset.status_code == 200
    assert reset.json()["revoked_key_count"] == 2
    assert client.get("/api/integration-center/api-keys").json()["api_keys"] == []


def test_runtime_output_dto_rejects_extra_fields_and_project_mismatch(components):
    service, store, vault, clock, db_path, vault_path = components
    issued = _issue(service, scopes=["runs:create", "capabilities:read"])
    submit_calls = []

    def unsafe_submit(run_id, payload, auth_context):
        submit_calls.append(run_id)
        return {
            "run_id": run_id,
            "project_id": auth_context["project_id"],
            "status": "queued",
            "raw_provider_response": {"unbounded": True},
        }

    app = FastAPI()
    app.include_router(
        build_external_agent_api_router(
            service=service,
            require_local=lambda _request: None,
            error_payload=_error_payload,
            submit_run=unsafe_submit,
            get_run=lambda *_args: {},
            cancel_run=lambda *_args: {},
            capabilities=lambda _auth: {
                "project_id": "project-b",
                "chat": True,
            },
        )
    )
    client = TestClient(app)
    authorization = {"Authorization": _auth(issued["secret"])}
    wrong_project = client.get("/api/public/v1/capabilities", headers=authorization)
    assert wrong_project.status_code == 502
    assert wrong_project.json()["detail"]["code"] == "EXTERNAL_API_RUNTIME_SCOPE_MISMATCH"

    rejected = client.post(
        "/api/public/v1/runs",
        headers={**authorization, "Idempotency-Key": "strict-output-0001"},
        json={"message": "測試嚴格輸出"},
    )
    assert rejected.status_code == 502
    assert rejected.json()["detail"]["code"] == "EXTERNAL_API_RUNTIME_CONTRACT_INVALID"
    assert len(submit_calls) == 1
    assert store.require_run_project(
        run_id=submit_calls[0], project_id="project-a"
    )["run_id"] == submit_calls[0]


def test_invalid_auth_failures_are_aggregated_without_raw_credentials(components):
    service, store, vault, clock, db_path, vault_path = components
    for index in range(25):
        with pytest.raises(ExternalAgentApiError):
            service.authenticate(
                f"Bearer invalid-{index}-credential",
                required_scope="runs:read",
                action="public.run.read",
            )
    failures = store.list_auth_failures(limit=100)
    assert len(failures) == 1
    assert failures[0]["failure_count"] == 25
    assert failures[0]["action"] == "public.run.read"
    raw_db = db_path.read_bytes()
    assert b"invalid-0-credential" not in raw_db
    assert b"invalid-24-credential" not in raw_db


def test_dpapi_decryption_failure_degrades_to_recovery_state(components):
    service, store, vault, clock, db_path, vault_path = components
    _issue(service)

    def broken_unprotect(_value: str) -> str:
        raise RuntimeError("different Windows DPAPI identity")

    degraded = ExternalAgentApiService(
        store=ExternalAgentApiStore(_connection_factory(db_path)),
        secret_store=ConnectorSecretStore(
            vault_path, protect=_protect, unprotect=broken_unprotect
        ),
        project_exists=lambda _project_id: True,
        policy_guard=lambda _project_id, _scope: True,
        clock=clock,
    )
    # Initialization stays isolated instead of taking down the whole app.
    installation = degraded.initialize()
    assert installation["id"] == service.installation()["id"]
    assert degraded.credential_recovery_required is True
    with pytest.raises(ExternalAgentApiError) as raised:
        degraded.authenticate(
            "Bearer wbk_000000000000_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            required_scope="runs:read",
            action="test.degraded",
        )
    assert raised.value.code == "EXTERNAL_API_CREDENTIAL_RECOVERY_REQUIRED"


def test_public_route_returns_retry_after_without_leaking_key(components):
    service, store, vault, clock, db_path, vault_path = components
    issued = _issue(
        service,
        scopes=["capabilities:read"],
        rate_limit_per_minute=1,
        request_limit_daily=10,
    )
    app = FastAPI()
    app.include_router(
        build_external_agent_api_router(
            service=service,
            require_local=lambda _request: None,
            error_payload=_error_payload,
            submit_run=lambda *_args: {},
            get_run=lambda *_args: {},
            cancel_run=lambda *_args: {},
            capabilities=lambda auth: {
                "project_id": auth["project_id"],
                "chat": True,
            },
        )
    )
    client = TestClient(app)
    headers = {"Authorization": _auth(issued["secret"])}
    assert client.get("/api/public/v1/capabilities", headers=headers).status_code == 200
    limited = client.get("/api/public/v1/capabilities", headers=headers)
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert issued["secret"] not in limited.text
    assert issued["secret"].encode() not in db_path.read_bytes()
