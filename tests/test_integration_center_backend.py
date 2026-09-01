from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from api.routes.integration_center import build_integration_center_router
from integration_center_service import IntegrationCenterError, IntegrationCenterService
from integration_center_store import IntegrationCenterStore, IntegrationPolicyConflict
from integration_policy_applier import (
    AuthoritativeIntegrationPolicyApplier,
    IntegrationPolicyApplyReceipt,
    IntegrationPolicyApplyError,
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


class Receipt:
    def __init__(self):
        self.rolled_back = False

    def rollback(self):
        self.rolled_back = True


class FakeConnectorService:
    def __init__(self):
        self.bindings = {
            "connection-gh": {"enabled": False, "mode": "read_write", "revision": 3},
            "connection-notion": {"enabled": True, "mode": "read_only", "revision": 1},
        }
        self.calls = []

    def list_connections(self, *, project_id):
        assert project_id == "project-1"
        return [
            {
                "connection_id": "connection-gh",
                "connector_id": "github",
                "status": "connected",
                "display_name": "Octocat",
                "workspace_id": None,
                "granted_permissions": ["contents:read", "issues:write"],
                "binding": dict(self.bindings["connection-gh"]),
                "validated_at": "2026-08-31T00:00:00+00:00",
            },
            {
                "connection_id": "connection-notion",
                "connector_id": "notion",
                "status": "connected",
                "display_name": "Docs",
                "workspace_id": "workspace-1",
                "granted_permissions": ["content:read"],
                "binding": dict(self.bindings["connection-notion"]),
            },
        ]

    def get_bound_resources(self, *, project_id, connection_id):
        assert project_id == "project-1"
        resources = {
            "connection-gh": [
                {
                    "resource_type": "repository",
                    "resource_id": "openai/example",
                    "display_label": "openai/example",
                }
            ],
            "connection-notion": [
                {
                    "resource_type": "page",
                    "resource_id": "page-1",
                    "display_label": "Project notes",
                }
            ],
        }
        return {"resources": resources[connection_id]}

    def put_project_binding(self, *, project_id, connection_id, enabled, mode):
        self.calls.append((project_id, connection_id, enabled, mode))
        self.bindings[connection_id] = {
            **self.bindings[connection_id],
            "enabled": enabled,
            "mode": mode,
        }
        return dict(self.bindings[connection_id])


class FakeExtensionRegistry:
    def __init__(self):
        self.permissions = {
            "builtin.n8n": {"level": "restricted", "revision": 2},
            "connector.github": {"level": "blocked", "revision": 4},
            "connector.notion": {"level": "restricted", "revision": 1},
            "local.playwright": {"level": "restricted", "revision": 5},
        }
        self.modes = {extension_id: "inherit" for extension_id in self.permissions}
        self.calls = []

    def catalog(self, project_id):
        assert project_id == "project-1"
        return {
            "extensions": [
                {
                    "id": extension_id,
                    "installed": True,
                    "trusted": True,
                    "effective_enabled": True,
                    "global_enabled": True,
                    "project_override": self.modes[extension_id],
                    "project_permission": dict(permission),
                    "entrypoint": (
                        {"type": "mcp_settings", "settings_id": "playwright"}
                        if extension_id == "local.playwright"
                        else {"type": "builtin"}
                    ),
                    "health": {"status": "ready"},
                    "manifest_sha256": "a" * 64,
                }
                for extension_id, permission in self.permissions.items()
            ]
        }

    def set_project_permission(self, extension_id, project_id, level, *, expected_revision, actor):
        assert project_id == "project-1"
        current = self.permissions[extension_id]
        assert current["revision"] == expected_revision
        self.calls.append((extension_id, level, actor))
        self.permissions[extension_id] = {
            "level": level,
            "revision": expected_revision + 1,
        }
        return {"project_permission": dict(self.permissions[extension_id])}

    def set_project_mode(self, extension_id, project_id, mode, *, expected_sha256, actor):
        assert project_id == "project-1"
        if mode == "enabled":
            assert expected_sha256 == "a" * 64
        self.calls.append((extension_id, mode, actor))
        self.modes[extension_id] = mode
        return {"project_override": mode}


@pytest.fixture()
def store(tmp_path: Path) -> IntegrationCenterStore:
    value = IntegrationCenterStore(_connection_factory(tmp_path / "integration-center.db"))
    value.ensure_schema()
    return value


def _service(store: IntegrationCenterStore, **kwargs) -> IntegrationCenterService:
    return IntegrationCenterService(
        store=store,
        project_exists=lambda project_id: project_id == "project-1",
        authoritative_applier=kwargs.pop("authoritative_applier", lambda *_args: Receipt()),
        **kwargs,
    )


def test_policy_replace_revision_decision_and_audit(store: IntegrationCenterStore):
    applied = []

    def applier(project_id, old_policy, new_policy):
        applied.append((project_id, old_policy["revision"], new_policy["revision"]))
        return Receipt()

    service = _service(store, authoritative_applier=applier)
    initial = service.get_policy("project-1")
    assert initial["revision"] == 0
    assert initial["permission_mode"] == "blocked"

    saved = service.put_policy(
        "project-1",
        expected_revision=0,
        policy={
            "name": "Agent API",
            "permission_mode": "restricted",
            "grants": [
                {
                    "integration_id": "external_api",
                    "capabilities": ["run.create", "run.read", "capabilities.read"],
                    "resources": [],
                }
            ],
        },
    )
    assert saved["revision"] == 1
    assert applied == [("project-1", 0, 1)]
    assert service.permission_decision(
        project_id="project-1",
        integration_id="external_api",
        capability="run.read",
    )["decision"] == "allow"
    assert service.external_api_policy_guard("project-1", "runs:create") is True
    assert service.external_api_policy_guard("project-1", "capabilities:read") is True
    assert service.external_api_policy_guard("project-1", "unknown") is False
    assert service.permission_decision(
        project_id="project-1",
        integration_id="external_api",
        capability="run.create",
    )["decision"] == "allow"
    assert service.permission_decision(
        project_id="project-1",
        integration_id="external_api",
        capability="run.cancel",
    )["decision"] == "deny"

    audits = service.audits("project-1")
    assert audits[0]["status"] == "completed"
    assert audits[0]["details"] == {
        "permission_mode": "restricted",
        "integration_count": 1,
        "capability_count": 3,
        "resource_count": 0,
    }
    serialized = json.dumps(audits)
    assert "wb_live" not in serialized
    with pytest.raises(IntegrationPolicyConflict):
        service.put_policy(
            "project-1",
            expected_revision=0,
            policy={"name": "stale", "permission_mode": "blocked", "grants": []},
        )
    assert len(applied) == 1


def test_applier_failure_does_not_advance_policy_revision(store: IntegrationCenterStore):
    def fail(*_args):
        raise RuntimeError("provider included secret=do-not-leak")

    service = _service(store, authoritative_applier=fail)
    with pytest.raises(IntegrationCenterError) as captured:
        service.put_policy(
            "project-1",
            expected_revision=0,
            policy={
                "name": "Rejected",
                "permission_mode": "restricted",
                "grants": [
                    {
                        "integration_id": "external_api",
                        "capabilities": ["run.read"],
                        "resources": [],
                    }
                ],
            },
        )
    assert captured.value.code == "INTEGRATION_POLICY_APPLY_FAILED"
    assert service.get_policy("project-1")["revision"] == 0
    audits = service.audits("project-1")
    assert audits[0]["status"] == "failed"
    assert "do-not-leak" not in json.dumps(audits)


def test_incomplete_compensation_durably_blocks_project(store: IntegrationCenterStore):
    def fail(*_args):
        raise IntegrationPolicyApplyError(
            "partial apply",
            compensation_incomplete=True,
        )

    service = _service(store, authoritative_applier=fail)
    with pytest.raises(IntegrationCenterError) as captured:
        service.put_policy(
            "project-1",
            expected_revision=0,
            policy={
                "name": "Unsafe partial apply",
                "permission_mode": "restricted",
                "grants": [
                    {
                        "integration_id": "external_api",
                        "capabilities": ["run.read"],
                        "resources": [],
                    }
                ],
            },
        )
    assert captured.value.code == "INTEGRATION_POLICY_COMPENSATION_FAILED"
    assert service.get_policy("project-1")["revision"] == 0
    assert store.get_apply_state("project-1")["status"] == "blocked"
    assert service.permission_decision(
        project_id="project-1",
        integration_id="external_api",
        capability="run.read",
    )["reason"] == "policy_apply_not_active"


def test_initialize_blocks_an_apply_interrupted_by_process_restart(
    store: IntegrationCenterStore,
):
    store.begin_apply("project-1", expected_revision=0)
    assert store.get_apply_state("project-1")["status"] == "applying"

    service = _service(store)
    service.initialize()

    state = store.get_apply_state("project-1")
    assert state["status"] == "blocked"
    assert state["error_code"] == "INTEGRATION_POLICY_APPLY_INTERRUPTED"
    assert service.permission_decision(
        project_id="project-1",
        integration_id="external_api",
        capability="run.read",
    )["reason"] == "policy_apply_not_active"
    audit = service.audits("project-1")[0]
    assert audit["action"] == "policy.reconcile"
    assert audit["status"] == "failed"
    assert audit["error_code"] == "INTEGRATION_POLICY_APPLY_INTERRUPTED"


def test_apply_receipt_retries_only_failed_compensation_actions():
    calls = []
    should_fail = {"second": True}
    receipt = IntegrationPolicyApplyReceipt()

    def first():
        calls.append("first")

    def second():
        calls.append("second")
        if should_fail["second"]:
            should_fail["second"] = False
            raise RuntimeError("transient rollback failure")

    receipt.add(first)
    receipt.add(second)
    with pytest.raises(IntegrationPolicyApplyError):
        receipt.rollback()
    assert calls == ["second", "first"]

    receipt.rollback()
    assert calls == ["second", "first", "second"]
    receipt.rollback()
    assert calls == ["second", "first", "second"]


def test_open_policy_requires_acknowledgement_and_exact_resource(store: IntegrationCenterStore):
    service = _service(store)
    policy = {
        "name": "n8n workflows",
        "permission_mode": "open",
        "grants": [
            {
                "integration_id": "n8n",
                "capabilities": ["workflow.execute"],
                "resources": [{"resource_type": "workflow", "resource_id": "daily-report"}],
            }
        ],
    }
    with pytest.raises(IntegrationCenterError) as captured:
        service.put_policy("project-1", expected_revision=0, policy=policy)
    assert captured.value.code == "INTEGRATION_POLICY_OPEN_ACKNOWLEDGEMENT_REQUIRED"
    saved = service.put_policy(
        "project-1",
        expected_revision=0,
        policy=policy,
        acknowledge_open_risk=True,
    )
    assert saved["permission_mode"] == "open"
    allowed = service.permission_decision(
        project_id="project-1",
        integration_id="n8n",
        capability="workflow.execute",
        resource_type="workflow",
        resource_id="daily-report",
    )
    denied = service.permission_decision(
        project_id="project-1",
        integration_id="n8n",
        capability="workflow.execute",
        resource_type="workflow",
        resource_id="unlisted",
    )
    assert allowed["decision"] == "allow"
    assert denied["decision"] == "deny"


def test_gmail_requires_an_explicit_connection_and_mailbox_scope(store: IntegrationCenterStore):
    service = _service(store)
    with pytest.raises(IntegrationCenterError) as missing:
        service.put_policy(
            "project-1",
            expected_revision=0,
            acknowledge_open_risk=True,
            policy={
                "name": "受治理 Gmail",
                "permission_mode": "open",
                "grants": [
                    {
                        "integration_id": "gmail",
                        "capabilities": ["message.read"],
                        "resources": [],
                    }
                ],
            },
        )
    assert missing.value.code == "INTEGRATION_POLICY_CONNECTION_REQUIRED"


def test_connector_grants_cannot_exceed_existing_project_binding(store: IntegrationCenterStore):
    service = _service(store, connector_service=FakeConnectorService())
    valid = {
        "name": "GitHub read",
        "permission_mode": "restricted",
        "grants": [
            {
                "integration_id": "github",
                "connection_id": "connection-gh",
                "capabilities": ["repository.read"],
                "resources": [{"resource_type": "repository", "resource_id": "openai/example"}],
            }
        ],
    }
    assert service.put_policy("project-1", expected_revision=0, policy=valid)["revision"] == 1
    assert service.permission_decision(
        project_id="project-1",
        integration_id="github",
        capability="repository.read",
        connection_id="connection-gh",
        resource_type="repository",
        resource_id="openai/example",
    )["decision"] == "allow"
    assert service.permission_decision(
        project_id="project-1",
        integration_id="github",
        capability="repository.read",
        connection_id="another-connection",
        resource_type="repository",
        resource_id="openai/example",
    )["decision"] == "deny"
    assert service.permission_decision(
        project_id="project-1",
        integration_id="github",
        capability="repository.read",
        connection_id="connection-gh",
        resource_type="repository",
        resource_id="private/unbound",
    )["decision"] == "deny"
    assert service.permission_decision(
        project_id="project-1",
        integration_id="github",
        capability="issue.write",
        connection_id="connection-gh",
        resource_type="repository",
        resource_id="openai/example",
    )["decision"] == "deny"
    invalid = {
        **valid,
        "grants": [
            {
                **valid["grants"][0],
                "resources": [{"resource_type": "repository", "resource_id": "private/unbound"}],
            }
        ],
    }
    with pytest.raises(IntegrationCenterError) as captured:
        service.put_policy("project-1", expected_revision=1, policy=invalid)
    assert captured.value.code == "INTEGRATION_POLICY_RESOURCE_NOT_BOUND"
    assert service.get_policy("project-1")["revision"] == 1


def test_overview_aggregates_safely_without_exposing_credentials(store: IntegrationCenterStore):
    registry = FakeExtensionRegistry()
    connector = FakeConnectorService()
    service = _service(
        store,
        connector_service=connector,
        extension_catalog_provider=registry.catalog,
        n8n_status_provider=lambda: {
            "status": "healthy",
            "running": True,
            "api_key": "n8n-secret",
        },
        gmail_profile_provider=lambda: {
            "configured": True,
            "enabled": True,
            "project_id": "project-1",
            "required_label": "Workbench-Agent",
            "fixed_recipient": "canary@example.test",
            "instruction": "private prompt",
            "access_token": "gmail-token",
            "crypto_ready": True,
            "isolation_ready": True,
        },
        mcp_status_provider=lambda: {"status": "healthy", "running": 1, "secret_alias": "vault-item"},
        external_api_summary_provider=lambda project_id: {
            "enabled": True,
            "active_key_count": 1,
            "sample": "Bearer wb_live_should_not_leak",
            "project_id": project_id,
        },
    )
    overview = service.overview("project-1")
    assert [item["id"] for item in overview["integrations"]] == [
        "gmail",
        "github",
        "notion",
        "n8n",
        "mcp",
        "external_api",
    ]
    serialized = json.dumps(overview, ensure_ascii=False)
    for secret in ("n8n-secret", "gmail-token", "vault-item", "wb_live_should_not_leak", "private prompt"):
        assert secret not in serialized
    assert overview["summary"]["total"] == 6


def test_authoritative_applier_updates_and_can_compensate_live_gates():
    registry = FakeExtensionRegistry()
    connectors = FakeConnectorService()
    callback_state = {
        "connectors": "blocked",
        "n8n": "blocked",
        "mcp": "blocked",
        "external_api": "blocked",
    }

    def setter(name):
        def apply(_project_id, mode, _grants):
            previous = callback_state[name]
            callback_state[name] = mode
            return lambda: callback_state.__setitem__(name, previous)

        return apply

    applier = AuthoritativeIntegrationPolicyApplier(
        extension_registry=registry,
        connector_service=connectors,
        connector_gate_setter=setter("connectors"),
        n8n_gate_setter=setter("n8n"),
        mcp_gate_setter=setter("mcp"),
        external_api_gate_setter=setter("external_api"),
    )
    receipt = applier(
        "project-1",
        {"permission_mode": "blocked", "grants": []},
        {
            "permission_mode": "open",
            "grants": [
                {"integration_id": "github", "connection_id": "connection-gh", "capabilities": [], "resources": []},
                {"integration_id": "n8n", "capabilities": [], "resources": []},
                {"integration_id": "mcp", "connection_id": "local.playwright", "capabilities": [], "resources": []},
                {"integration_id": "external_api", "capabilities": [], "resources": []},
            ],
        },
    )
    assert registry.permissions["connector.github"]["level"] == "open"
    assert registry.permissions["connector.notion"]["level"] == "blocked"
    assert registry.permissions["local.playwright"]["level"] == "open"
    assert connectors.bindings["connection-gh"]["enabled"] is True
    assert connectors.bindings["connection-notion"]["enabled"] is False
    assert callback_state == {
        "connectors": "open",
        "n8n": "open",
        "mcp": "open",
        "external_api": "open",
    }
    assert registry.modes["connector.github"] == "enabled"
    assert registry.modes["connector.notion"] == "disabled"
    assert registry.modes["local.playwright"] == "enabled"

    receipt.rollback()
    assert registry.permissions["connector.github"]["level"] == "blocked"
    assert registry.permissions["connector.notion"]["level"] == "restricted"
    assert registry.permissions["local.playwright"]["level"] == "restricted"
    assert connectors.bindings["connection-gh"]["enabled"] is False
    assert connectors.bindings["connection-notion"]["enabled"] is True
    assert callback_state == {
        "connectors": "blocked",
        "n8n": "blocked",
        "mcp": "blocked",
        "external_api": "blocked",
    }
    assert all(mode == "inherit" for mode in registry.modes.values())


def test_authoritative_applier_rejects_missing_runtime_gate_and_untrusted_selection():
    registry = FakeExtensionRegistry()
    connectors = FakeConnectorService()
    applier = AuthoritativeIntegrationPolicyApplier(
        extension_registry=registry,
        connector_service=connectors,
    )
    with pytest.raises(IntegrationPolicyApplyError):
        applier(
            "project-1",
            {"permission_mode": "blocked", "grants": []},
            {
                "permission_mode": "restricted",
                "grants": [
                    {
                        "integration_id": "github",
                        "connection_id": "connection-gh",
                        "capabilities": ["repository.read"],
                        "resources": [],
                    }
                ],
            },
        )
    assert registry.permissions["connector.github"]["level"] == "blocked"
    assert registry.modes["connector.github"] == "inherit"
    assert connectors.bindings["connection-gh"]["enabled"] is False

    registry.permissions["connector.github"] = {"level": "blocked", "revision": 4}
    original_catalog = registry.catalog

    def untrusted_catalog(project_id):
        payload = original_catalog(project_id)
        for item in payload["extensions"]:
            if item["id"] == "connector.github":
                item["trusted"] = False
        return payload

    registry.catalog = untrusted_catalog
    strict_applier = AuthoritativeIntegrationPolicyApplier(
        extension_registry=registry,
        connector_service=connectors,
        connector_gate_setter=lambda *_args: None,
    )
    with pytest.raises(IntegrationPolicyApplyError):
        strict_applier(
            "project-1",
            {"permission_mode": "blocked", "grants": []},
            {
                "permission_mode": "restricted",
                "grants": [
                    {
                        "integration_id": "github",
                        "connection_id": "connection-gh",
                        "capabilities": ["repository.read"],
                        "resources": [],
                    }
                ],
            },
        )
    assert registry.modes["connector.github"] == "inherit"


def test_migration_imports_only_healthy_existing_authority(store: IntegrationCenterStore):
    registry = FakeExtensionRegistry()
    registry.permissions["connector.github"] = {"level": "restricted", "revision": 4}
    connectors = FakeConnectorService()
    connectors.bindings["connection-gh"]["enabled"] = True
    service = _service(
        store,
        connector_service=connectors,
        extension_catalog_provider=registry.catalog,
        n8n_status_provider=lambda: {"status": "healthy", "running": True},
        gmail_profile_provider=lambda: {
            "configured": True,
            "enabled": True,
            "project_id": "project-1",
            "required_label": "Workbench-Agent",
            "fixed_recipient": "canary@example.test",
            "crypto_ready": True,
            "isolation_ready": True,
        },
        mcp_status_provider=lambda: {"status": "healthy", "running": 1},
        external_api_summary_provider=lambda _project_id: {
            # A generated inbound key is never legacy authority. It remains
            # blocked until the user explicitly adds external_api to policy.
            "enabled": True,
            "active_key_count": 1,
        },
    )
    result = service.import_existing_project_policy("project-1")
    assert result["migrated"] is True
    policy = result["policy"]
    assert policy["permission_mode"] == "restricted"
    assert {grant["integration_id"] for grant in policy["grants"]} == {
        "github",
        "notion",
        "n8n",
        "mcp",
    }
    github = next(item for item in policy["grants"] if item["integration_id"] == "github")
    assert github["connection_id"] == "connection-gh"
    assert github["resources"] == [
        {"resource_type": "repository", "resource_id": "openai/example"}
    ]
    assert service.audits("project-1")[0]["action"] == "migration_existing_integration_state"
    repeated = service.import_existing_project_policy("project-1")
    assert repeated["migrated"] is False
    assert repeated["reason"] == "policy_exists"


def test_routes_require_local_session_and_expose_fixed_contract(store: IntegrationCenterStore):
    guarded = []
    service = _service(store)
    app = FastAPI()
    app.include_router(
        build_integration_center_router(
            service=service,
            require_local=lambda request: guarded.append(request.url.path),
            error_payload=lambda code, message, recoverable=False: {
                "code": code,
                "message": message,
                "recoverable": recoverable,
            },
        )
    )
    client = TestClient(app)
    response = client.get("/api/integration-center/overview", params={"project_id": "project-1"})
    assert response.status_code == 200
    assert response.json()["project_id"] == "project-1"

    response = client.put(
        "/api/integration-center/policies/project-1",
        json={
            "revision": 0,
            "name": "外部 Agent",
            "permission_mode": "restricted",
            "grants": [
                {
                    "integration_id": "external_api",
                    "capabilities": ["run.read"],
                    "resources": [],
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["policy"]["revision"] == 1
    response = client.get("/api/integration-center/audit", params={"project_id": "project-1"})
    assert response.status_code == 200
    assert response.json()["audits"][0]["status"] == "completed"
    assert guarded == [
        "/api/integration-center/overview",
        "/api/integration-center/policies/project-1",
        "/api/integration-center/audit",
    ]
