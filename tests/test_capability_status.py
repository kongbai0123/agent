from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from capability_status import (
    CapabilityStatusError,
    CapabilityStatusService,
    build_capability_status_tool_definitions,
)


def _integration_overview(project_id: str):
    assert project_id == "project-one"
    return {
        "project_id": project_id,
        "apply_state": {"status": "active", "active_revision": 2},
        "integrations": [
            {
                "id": "gmail",
                "name": "Gmail",
                "kind": "oauth_connector",
                "description": "搜尋與閱讀郵件。",
                "requires_connection": True,
                "capabilities": [
                    {"id": "message.read", "label": "讀取郵件", "risk": "external_read"}
                ],
                "state": {
                    "healthy": True,
                    "connections": [
                        {
                            "connection_id": "gmail-one",
                            "status": "connected",
                            "binding": {"enabled": True, "revision": 3},
                            "resources": [],
                            "access_token": "must-never-escape",
                        }
                    ],
                },
                "policy": {
                    "permission_mode": "restricted",
                    "revision": 2,
                    "grants": [
                        {
                            "integration_id": "gmail",
                            "connection_id": "gmail-one",
                            "capabilities": ["message.read"],
                        }
                    ],
                },
            },
            {
                "id": "n8n",
                "name": "n8n",
                "kind": "managed_runtime",
                "description": "本機工作流程。",
                "requires_connection": False,
                "capabilities": [],
                "state": {"healthy": True, "status": "ready"},
                "policy": {
                    "permission_mode": "restricted",
                    "revision": 2,
                    "grants": [{"integration_id": "n8n", "capabilities": ["workflow.read"]}],
                },
            },
        ],
    }


def _extension_catalog(project_id: str):
    assert project_id == "project-one"
    return {
        "extensions": [
            {
                "id": "connector.gmail",
                "name": "Gmail",
                "description": "Gmail connector",
                "installed": True,
                "trusted": True,
                "effective_enabled": True,
                "project_permission": {"level": "restricted"},
                "health": {"status": "healthy", "checked_at": "2026-09-01T00:00:00Z"},
                "entrypoint": {"type": "connector"},
            },
            {
                "id": "builtin.n8n",
                "name": "n8n",
                "installed": True,
                "trusted": True,
                "effective_enabled": True,
                "project_permission": {"level": "restricted"},
                "health": {"status": "healthy"},
                "entrypoint": {"type": "builtin"},
            },
            {
                "id": "builtin.playwright-browser",
                "name": "Playwright Browser",
                "description": "隔離瀏覽器工具",
                "installed": True,
                "trusted": True,
                "effective_enabled": False,
                "project_permission": {"level": "restricted"},
                "health": {"status": "healthy"},
                "entrypoint": {"type": "builtin"},
            },
        ]
    }


def _model_overview(project_id: str):
    assert project_id == "project-one"
    return {
        "providers": [
            {
                "provider_id": "nvidia",
                "model_id": "nvidia/example",
                "enabled": True,
                "credential": {"expires_at": "2027-01-01", "access_token": "secret"},
                "operational": {"state": "healthy", "updated_at": "2026-09-01T00:00:00Z"},
            }
        ]
    }


def _service():
    return CapabilityStatusService(
        project_exists=lambda project_id: project_id == "project-one",
        integration_overview_provider=_integration_overview,
        extension_catalog_provider=_extension_catalog,
        model_overview_provider=_model_overview,
    )


def test_unified_status_reports_authoritative_block_reason_without_secrets():
    payload = asyncio.run(_service().query("project-one", "Gmail 是否已啟用？"))
    assert payload["project_id"] == "project-one"
    assert payload["summary"] == {"total": 1, "available": 0, "blocked": 1}
    gmail = payload["items"][0]
    assert gmail["id"] == "gmail"
    assert gmail["installed"] is True
    assert gmail["connected"] is True
    assert gmail["resource_allowed"] is False
    assert gmail["reason_code"] == "resource_binding_required"
    assert gmail["repair"]["workspace"] == "integrations"
    serialized = str(payload).casefold()
    assert "must-never-escape" not in serialized
    assert "access_token" not in serialized
    assert "secret" not in serialized


def test_all_credential_shaped_state_is_absent_from_public_contract():
    def poisoned_overview(_project_id):
        payload = _integration_overview("project-one")
        payload["integrations"][0]["state"].update(
            {
                "api_key": "api-key-raw",
                "oauth_token": "oauth-token-raw",
                "client_secret": "client-secret-raw",
                "pkce_verifier": "pkce-verifier-raw",
                "provider_error": "Bearer unmasked-provider-error",
            }
        )
        return payload

    service = CapabilityStatusService(
        project_exists=lambda project_id: project_id == "project-one",
        integration_overview_provider=poisoned_overview,
        extension_catalog_provider=_extension_catalog,
        model_overview_provider=_model_overview,
    )
    serialized = str(asyncio.run(service.list_capabilities("project-one"))).casefold()
    for forbidden in (
        "api-key-raw",
        "oauth-token-raw",
        "client-secret-raw",
        "pkce-verifier-raw",
        "unmasked-provider-error",
    ):
        assert forbidden not in serialized


def test_generic_backend_capability_question_returns_all_project_items():
    payload = asyncio.run(_service().query("project-one", "有哪些後台功能目前可以使用？"))
    ids = {item["id"] for item in payload["items"]}
    assert {"gmail", "n8n", "builtin.playwright-browser", "provider.nvidia"} <= ids


def test_status_query_is_project_scoped_and_fails_closed():
    with pytest.raises(CapabilityStatusError) as error:
        asyncio.run(_service().query("project-two", "Gmail"))
    assert error.value.code == "PROJECT_NOT_FOUND"

    with pytest.raises(CapabilityStatusError) as independent:
        asyncio.run(_service().query("__independent_chat__", "Gmail"))
    assert independent.value.code == "PROJECT_REQUIRED"


def test_read_only_tool_contract_uses_call_project_and_has_no_write_surface():
    definitions = build_capability_status_tool_definitions(_service())
    assert [item.name for item in definitions] == [
        "workbench.list_capabilities",
        "workbench.get_capability_status",
    ]
    assert all(item.access.value == "read" for item in definitions)
    assert all(item.risk_level == "read" for item in definitions)
    assert all(item.requires_connection is False for item in definitions)
    assert all(item.requires_resource is False for item in definitions)

    result = asyncio.run(
        definitions[1].handler(
            SimpleNamespace(
                project_id="project-one",
                arguments={"capability": "Playwright 是否啟用？"},
            )
        )
    )
    assert result["items"][0]["id"] == "builtin.playwright-browser"
    assert result["items"][0]["reason_code"] == "not_enabled"


def test_status_is_recomputed_after_authoritative_state_changes():
    enabled = {"value": False}

    def mutable_extensions(project_id):
        payload = _extension_catalog(project_id)
        payload["extensions"][2]["effective_enabled"] = enabled["value"]
        return payload

    service = CapabilityStatusService(
        project_exists=lambda project_id: project_id == "project-one",
        integration_overview_provider=_integration_overview,
        extension_catalog_provider=mutable_extensions,
        model_overview_provider=_model_overview,
    )
    first = asyncio.run(service.query("project-one", "Playwright 是否啟用？"))
    assert first["items"][0]["reason_code"] == "not_enabled"
    enabled["value"] = True
    second = asyncio.run(service.query("project-one", "Playwright 是否啟用？"))
    assert second["items"][0]["reason_code"] == "ready"


@pytest.mark.parametrize(
    ("state", "reason_code", "repair_section"),
    [
        ("auth_required", "auth_required", "connections"),
        ("permission_denied", "permission_denied", "connections"),
        ("quota_exhausted", "quota_exhausted", "health"),
        ("rate_limited", "rate_limited", "health"),
        ("model_unavailable", "model_unavailable", "connections"),
        ("degraded", "degraded", "health"),
        ("unreachable", "unreachable", "connections"),
    ],
)
def test_provider_failure_classification_is_specific(state, reason_code, repair_section):
    def models(_project_id):
        return {
            "providers": [
                {
                    "provider_id": "nvidia",
                    "model_id": "nvidia/example",
                    "enabled": True,
                    "credential": {
                        "expires_at": "2027-01-01T00:00:00Z",
                        "expiry_source": "user_declared",
                        "remaining_days": 122,
                        "access_token": "never-public",
                    },
                    "operational": {
                        "state": state,
                        "reason": "Bearer raw-upstream-message",
                    },
                }
            ]
        }

    service = CapabilityStatusService(
        project_exists=lambda project_id: project_id == "project-one",
        integration_overview_provider=_integration_overview,
        extension_catalog_provider=_extension_catalog,
        model_overview_provider=models,
    )
    payload = asyncio.run(service.query("project-one", "NVIDIA 模型狀態"))
    provider = payload["items"][0]
    assert provider["reason_code"] == reason_code
    assert provider["repair"]["section"] == repair_section
    assert provider["available"] is False
    assert provider["key_lifecycle"]["remaining_days"] == 122
    assert "raw-upstream-message" not in str(payload)
    assert "never-public" not in str(payload)
