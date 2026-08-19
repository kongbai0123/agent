from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import database  # noqa: E402
from api.routes.extensions import build_extensions_router  # noqa: E402
from extension_catalog import settings_manifests  # noqa: E402
from extension_manifest import parse_extension_manifest  # noqa: E402
from extension_registry import (  # noqa: E402
    ExtensionManifestRejected,
    ExtensionRegistry,
    ExtensionUnavailable,
)


@pytest.fixture
def registry_env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "workbench.db"))
    database.init_db()
    local_dir = tmp_path / "runtime" / "extensions" / "local"
    local_dir.mkdir(parents=True)
    settings: dict = {
        "ollama_url": "http://127.0.0.1:11434",
        "mcp_servers": [],
        "model_providers": [],
    }

    def load_settings():
        return json.loads(json.dumps(settings))

    def save_settings(value):
        settings.clear()
        settings.update(json.loads(json.dumps(value)))

    registry = ExtensionRegistry(
        load_settings,
        save_settings=save_settings,
        require_project=lambda project_id: project_id == "project_1",
        local_dir=local_dir,
    )
    return registry, settings, local_dir


def _local_mcp_manifest(settings: dict) -> dict:
    configured = settings_manifests(settings)[0]
    return {
        "schema_version": 1,
        "id": configured.id,
        "name": "Local Demo",
        "version": "1.0.0",
        "description": "A reviewed local MCP manifest.",
        "publisher": "Test",
        "origin": "local",
        "kind": "mcp",
        "category": "tools",
        "entrypoint": {
            "type": "mcp_settings",
            "adapter": "mcp",
            "settings_id": "demo",
            "configuration_sha256": configured.entrypoint.configuration_sha256,
        },
        "permissions": [
            {
                "id": "process.mcp",
                "risk": "system",
                "description": "Start the reviewed MCP process.",
                "required": True,
            }
        ],
        "health_probe": "mcp",
        "removable": True,
        "default_installed": False,
        "default_enabled": False,
    }


def test_catalog_models_connectors_and_hard_unavailable_adapters(registry_env):
    registry, settings, _local_dir = registry_env
    settings["model_providers"] = [
        {
            "id": "openrouter",
            "label": "OpenRouter",
            "base_url": "https://openrouter.ai/api/v1",
            "enabled": True,
            "selected_model": "test/model",
            "supports_tools": True,
        }
    ]

    catalog = registry.catalog()
    by_id = {item["id"]: item for item in catalog["extensions"]}

    assert {
        "builtin.n8n",
        "builtin.cursor",
        "builtin.excel",
        "builtin.ollama",
        "connector.github",
        "connector.notion",
        "provider.openrouter",
    } <= set(by_id)
    assert by_id["connector.github"]["contract_type"] == "connector-v1"
    assert by_id["connector.github"]["runtime_available"] is True
    assert by_id["connector.github"]["installed"] is False
    assert by_id["connector.github"]["trusted"] is True
    assert by_id["connector.notion"]["connector_id"] == "notion"
    n8n = by_id["builtin.n8n"]
    assert n8n["installed"] is False
    assert n8n["global_enabled"] is False
    assert n8n["effective_enabled"] is False
    assert n8n in catalog["sections"]["available"]
    assert not any(
        audit["action"] == "migration_existing_user_configuration"
        for audit in registry.audits("builtin.n8n")
    )
    assert any(
        audit["action"] == "migration_existing_user_configuration"
        for audit in registry.audits("builtin.ollama")
    )

    for extension_id, reason in (
        ("builtin.cursor", "cursor_adapter_not_implemented"),
        ("builtin.excel", "excel_adapter_not_implemented"),
    ):
        item = by_id[extension_id]
        assert item["runtime_available"] is False
        assert item["availability_reason"] == reason
        assert item["installed"] is False
        assert item["effective_enabled"] is False
        assert item["health"]["status"] == "unavailable"
        assert item in catalog["sections"]["unavailable"]
        with pytest.raises(ExtensionUnavailable):
            registry.set_global(
                extension_id,
                True,
                expected_sha256=item["manifest_sha256"],
            )

    provider = by_id["provider.openrouter"]
    assert provider["installed"] is True
    assert provider["trusted"] is True
    assert provider["effective_enabled"] is True
    assert any(
        audit["action"] == "migration_existing_user_configuration"
        for audit in registry.audits("provider.openrouter")
    )

    settings["model_providers"][0]["enabled"] = False
    disabled = registry.get("provider.openrouter")
    assert disabled["global_enabled"] is True
    assert disabled["configuration_enabled"] is False
    assert disabled["effective_enabled"] is False


def test_connector_lifecycle_and_project_scope_are_persistent(registry_env):
    registry, _settings, local_dir = registry_env
    github = next(
        item
        for item in registry.catalog("project_1")["extensions"]
        if item["id"] == "connector.github"
    )
    assert github["available"] is True

    installed = registry.install("connector.github", github["manifest_sha256"])
    assert installed["installed"] and installed["trusted"]
    enabled = registry.set_global(
        "connector.github",
        True,
        expected_sha256=github["manifest_sha256"],
        project_id="project_1",
    )
    assert enabled["effective_enabled"] is True

    project_events = []
    registry.project_state_change_handler = (
        lambda extension_id, project_id, mode, item: project_events.append(
            (extension_id, project_id, mode, item["effective_enabled"])
        )
    )
    registry.set_project_mode("connector.github", "project_1", "disabled")
    assert project_events == [
        ("connector.github", "project_1", "disabled", False)
    ]
    assert registry.require_enabled("connector.github", None)["effective_enabled"] is True
    assert registry.is_effectively_enabled("connector.github", "project_1") is False

    reopened = ExtensionRegistry(
        registry.load_settings,
        save_settings=registry.save_settings,
        require_project=lambda project_id: project_id == "project_1",
        local_dir=local_dir,
    )
    reopened.initialize()
    item = reopened.get("connector.github", "project_1", synchronize=False)
    assert item["installed"] and item["global_enabled"]
    assert item["project_override"] == "disabled"

    reopened.remove("connector.github")
    removed = reopened.get("connector.github")
    assert removed["installed"] is False
    assert removed["available"] is True


def test_global_handler_failure_restores_db_settings_and_reconciles_old_state(
    registry_env,
):
    registry, settings, _local_dir = registry_env
    github = next(
        item
        for item in registry.catalog()["extensions"]
        if item["id"] == "connector.github"
    )
    registry.install("connector.github", github["manifest_sha256"])
    settings["lifecycle_marker"] = "before"
    events: list[tuple[str, bool]] = []

    def fail_after_side_effect(extension_id, enabled, _item):
        events.append((extension_id, enabled))
        settings["lifecycle_marker"] = "after" if enabled else "before"
        if enabled:
            raise RuntimeError("injected handler failure")

    registry.state_change_handler = fail_after_side_effect

    with pytest.raises(RuntimeError, match="injected handler failure"):
        registry.set_global(
            "connector.github",
            True,
            expected_sha256=github["manifest_sha256"],
        )

    restored = registry.get("connector.github", synchronize=False)
    assert restored["global_enabled"] is False
    assert registry.store.get("connector.github")[
        "global_approved_manifest_sha256"
    ] is None
    assert settings["lifecycle_marker"] == "before"
    assert events == [
        ("connector.github", True),
        ("connector.github", False),
    ]


@pytest.mark.parametrize("failed_target", [True, False])
def test_apply_configuration_failure_rolls_back_grant_and_keeps_revoke_closed(
    registry_env,
    failed_target,
):
    registry, settings, _local_dir = registry_env
    github = next(
        item
        for item in registry.catalog()["extensions"]
        if item["id"] == "connector.github"
    )
    digest = github["manifest_sha256"]
    registry.install("connector.github", digest)
    settings["connector_enabled"] = False
    runtime = {"connector_enabled": False}
    handler_events: list[bool] = []
    apply_events: list[bool] = []

    def persist_desired_state(_extension_id, enabled, _item):
        handler_events.append(enabled)
        settings["connector_enabled"] = enabled

    failure_armed = False

    def apply_configuration(value):
        nonlocal failure_armed
        desired = bool(value["connector_enabled"])
        apply_events.append(desired)
        runtime["connector_enabled"] = desired
        if failure_armed and desired is failed_target:
            failure_armed = False
            raise RuntimeError("injected apply failure")

    registry.state_change_handler = persist_desired_state
    registry.apply_configuration = apply_configuration

    if failed_target is False:
        registry.set_global(
            "connector.github",
            True,
            expected_sha256=digest,
        )
        assert registry.get(
            "connector.github", synchronize=False
        )["global_enabled"] is True
        assert settings["connector_enabled"] is True
        assert runtime["connector_enabled"] is True

    previous = registry.get("connector.github", synchronize=False)
    previous_enabled = bool(previous["global_enabled"])
    previous_digest = registry.store.get("connector.github")[
        "global_approved_manifest_sha256"
    ]
    failure_armed = True

    with pytest.raises(RuntimeError, match="injected apply failure"):
        registry.set_global(
            "connector.github",
            failed_target,
            expected_sha256=digest if failed_target else None,
        )

    expected_enabled = previous_enabled if failed_target else False
    expected_digest = previous_digest if expected_enabled else None
    restored = registry.get("connector.github", synchronize=False)
    assert restored["global_enabled"] is expected_enabled
    assert registry.store.get("connector.github")[
        "global_approved_manifest_sha256"
    ] == expected_digest
    assert settings["connector_enabled"] is expected_enabled
    assert runtime["connector_enabled"] is expected_enabled
    assert handler_events[-2:] == [failed_target, expected_enabled]
    assert apply_events[-2:] == [failed_target, expected_enabled]


def test_project_disable_handler_failure_keeps_fail_closed_override(
    registry_env,
):
    registry, _settings, _local_dir = registry_env
    github = next(
        item
        for item in registry.catalog("project_1")["extensions"]
        if item["id"] == "connector.github"
    )
    digest = github["manifest_sha256"]
    registry.install("connector.github", digest)
    registry.set_global(
        "connector.github",
        True,
        expected_sha256=digest,
    )
    registry.set_project_mode(
        "connector.github",
        "project_1",
        "enabled",
        expected_sha256=digest,
    )
    events: list[tuple[str, bool]] = []
    failure_armed = True

    def fail_after_schedule(_extension_id, _project_id, mode, item):
        nonlocal failure_armed
        events.append((mode, bool(item["effective_enabled"])))
        if mode == "disabled" and failure_armed:
            failure_armed = False
            raise RuntimeError("injected project handler failure")

    registry.project_state_change_handler = fail_after_schedule

    with pytest.raises(RuntimeError, match="injected project handler failure"):
        registry.set_project_mode(
            "connector.github",
            "project_1",
            "disabled",
        )

    assert registry.store.project_state("connector.github", "project_1") == {
        "mode": "disabled",
        "approved_manifest_sha256": None,
    }
    restored = registry.get(
        "connector.github", "project_1", synchronize=False
    )
    assert restored["project_override"] == "disabled"
    assert restored["effective_enabled"] is False
    assert events == [("disabled", False), ("disabled", False)]


def test_project_grant_handler_failure_restores_previous_disabled_override(
    registry_env,
):
    registry, _settings, _local_dir = registry_env
    github = next(
        item
        for item in registry.catalog("project_1")["extensions"]
        if item["id"] == "connector.github"
    )
    digest = github["manifest_sha256"]
    registry.install("connector.github", digest)
    registry.set_global(
        "connector.github",
        True,
        expected_sha256=digest,
    )
    registry.set_project_mode("connector.github", "project_1", "disabled")
    events: list[str] = []

    def fail_grant(_extension_id, _project_id, mode, _item):
        events.append(mode)
        if mode == "enabled":
            raise RuntimeError("injected project grant failure")

    registry.project_state_change_handler = fail_grant
    with pytest.raises(RuntimeError, match="injected project grant failure"):
        registry.set_project_mode(
            "connector.github",
            "project_1",
            "enabled",
            expected_sha256=digest,
        )

    assert registry.store.project_state("connector.github", "project_1") == {
        "mode": "disabled",
        "approved_manifest_sha256": None,
    }
    assert events == ["enabled", "disabled"]


def test_n8n_disable_handler_failure_never_reopens_global_gate(registry_env):
    registry, _settings, _local_dir = registry_env
    n8n = registry.get("builtin.n8n")
    registry.install("builtin.n8n", n8n["manifest_sha256"])
    n8n = registry.get("builtin.n8n")
    registry.set_global(
        "builtin.n8n",
        True,
        expected_sha256=n8n["manifest_sha256"],
    )
    assert registry.get("builtin.n8n")["global_enabled"] is True
    events: list[bool] = []

    def fail_stop(_extension_id, enabled, _item):
        events.append(enabled)
        raise RuntimeError("injected n8n stop failure")

    registry.state_change_handler = fail_stop
    with pytest.raises(RuntimeError, match="injected n8n stop failure"):
        registry.set_global("builtin.n8n", False)

    assert registry.get(
        "builtin.n8n", synchronize=False
    )["global_enabled"] is False
    assert events == [False, False]


def _configure_settings_mcp(registry, settings, *, enabled: bool):
    settings["mcp_servers"] = [
        {
            "id": "demo",
            "label": "Demo",
            "transport": "stdio",
            "executable": "D:/tools/demo.exe",
            "argv": ["--stdio"],
            "timeout_seconds": 30,
            "enabled": enabled,
        }
    ]
    item = registry.get("mcp.demo")
    if not item["installed"]:
        registry.install("mcp.demo", item["manifest_sha256"])
    item = registry.get("mcp.demo")
    if not item["trusted"]:
        registry.trust("mcp.demo", item["manifest_sha256"])
    return registry.get("mcp.demo")


def _mcp_desired_state_handler(registry, settings, schedules, *, fail_once=False):
    failure = {"armed": fail_once}

    def handler(extension_id, enabled, _item):
        cfg = json.loads(json.dumps(settings))
        settings_id = extension_id.removeprefix("mcp.")
        for server in cfg["mcp_servers"]:
            if server["id"].casefold() == settings_id.casefold():
                server["enabled"] = enabled
                break
        registry.save_settings(cfg)
        schedules.append(enabled)
        if failure["armed"]:
            failure["armed"] = False
            raise RuntimeError("injected MCP handler failure")

    return handler


def test_mcp_enable_failure_restores_only_bound_settings_mirror(registry_env):
    registry, settings, _local_dir = registry_env
    item = _configure_settings_mcp(registry, settings, enabled=False)
    schedules: list[bool] = []
    registry.state_change_handler = _mcp_desired_state_handler(
        registry,
        settings,
        schedules,
    )
    runtime = {"enabled": False}
    failure_armed = True

    def fail_apply_after_unrelated_settings_write(value):
        nonlocal failure_armed
        desired = bool(value["mcp_servers"][0]["enabled"])
        runtime["enabled"] = desired
        if desired and failure_armed:
            failure_armed = False
            # Represents an unrelated settings save concurrent with a failed
            # runtime apply.  Rollback must not replace the entire snapshot.
            settings["ui_language"] = "zh-TW"
            raise RuntimeError("injected MCP apply failure")

    registry.apply_configuration = fail_apply_after_unrelated_settings_write
    with pytest.raises(RuntimeError, match="injected MCP apply failure"):
        registry.set_global(
            "mcp.demo",
            True,
            expected_sha256=item["manifest_sha256"],
        )

    assert registry.get("mcp.demo", synchronize=False)["global_enabled"] is False
    assert settings["mcp_servers"][0]["enabled"] is False
    assert settings["ui_language"] == "zh-TW"
    assert runtime["enabled"] is False
    assert schedules == [True, False]


def test_mcp_disable_handler_failure_keeps_db_and_settings_mirror_closed(
    registry_env,
):
    registry, settings, _local_dir = registry_env
    item = _configure_settings_mcp(registry, settings, enabled=True)
    assert item["global_enabled"] is True
    schedules: list[bool] = []
    registry.state_change_handler = _mcp_desired_state_handler(
        registry,
        settings,
        schedules,
        fail_once=True,
    )

    with pytest.raises(RuntimeError, match="injected MCP handler failure"):
        registry.set_global("mcp.demo", False)

    assert registry.get("mcp.demo", synchronize=False)["global_enabled"] is False
    assert settings["mcp_servers"][0]["enabled"] is False
    assert schedules == [False, False]


def test_runtime_gate_does_not_rescan_or_write(registry_env, monkeypatch):
    registry, _settings, _local_dir = registry_env
    registry.initialize()
    ollama = registry.get("builtin.ollama", synchronize=False)
    assert ollama["effective_enabled"] is True

    def forbidden_sync():
        raise AssertionError("runtime gate attempted a catalog sync")

    monkeypatch.setattr(registry, "sync", forbidden_sync)
    assert registry.require_enabled("builtin.ollama")["id"] == "builtin.ollama"
    assert registry.is_effectively_enabled("builtin.ollama") is True


def test_settings_digest_change_revokes_migrated_provider_trust(registry_env):
    registry, settings, _local_dir = registry_env
    settings["model_providers"] = [
        {
            "id": "nvidia",
            "label": "NVIDIA",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "enabled": True,
            "selected_model": "vendor/first",
            "supports_tools": False,
        }
    ]
    first = registry.get("provider.nvidia")
    assert first["effective_enabled"] is True

    settings["model_providers"][0]["selected_model"] = "vendor/replacement"
    changed = registry.get("provider.nvidia")
    assert changed["manifest_sha256"] != first["manifest_sha256"]
    assert changed["trusted"] is False
    assert changed["global_enabled"] is False
    assert changed["effective_enabled"] is False


def test_provider_cleanup_failure_leaves_runtime_gate_disabled(
    registry_env,
    monkeypatch,
):
    registry, settings, _local_dir = registry_env
    settings["model_providers"] = [
        {
            "id": "remote",
            "base_url": "https://models.example/v1",
            "enabled": True,
        }
    ]
    assert registry.get("provider.remote")["effective_enabled"] is True

    def fail_cleanup(_provider_id):
        raise RuntimeError("secret cleanup failed")

    monkeypatch.setattr("secret_store.delete_provider_secret", fail_cleanup)
    with pytest.raises(RuntimeError, match="secret cleanup failed"):
        registry.remove("provider.remote")

    assert registry.is_effectively_enabled("provider.remote") is False
    assert any(
        item["action"] == "remove" and item["status"] == "failed"
        for item in registry.audits("provider.remote")
    )


def test_local_manifest_requires_exact_settings_binding_and_lifecycle(registry_env):
    registry, settings, local_dir = registry_env
    settings["mcp_servers"] = [
        {
            "id": "demo",
            "transport": "stdio",
            "executable": "D:/tools/demo.exe",
            "argv": ["--stdio"],
            "timeout_seconds": 20,
            "enabled": True,
        }
    ]
    payload = _local_mcp_manifest(settings)
    (local_dir / "demo.json").write_text(json.dumps(payload), encoding="utf-8")

    candidate = registry.inspect_local("demo.json", "project_1")
    assert candidate["installed"] is False
    registry.install("mcp.demo", candidate["manifest_sha256"])
    registry.trust("mcp.demo", candidate["manifest_sha256"])
    registry.set_global(
        "mcp.demo",
        True,
        expected_sha256=candidate["manifest_sha256"],
    )
    assert registry.is_effectively_enabled("mcp.demo", "project_1") is True

    settings["mcp_servers"][0]["argv"] = ["--different"]
    assert registry.get("mcp.demo")["effective_enabled"] is False


def test_local_manifest_reader_rejects_traversal_and_links(registry_env):
    registry, settings, local_dir = registry_env
    settings["mcp_servers"] = [
        {
            "id": "demo",
            "executable": "D:/tools/demo.exe",
            "enabled": False,
        }
    ]
    target = local_dir.parent / "outside.json"
    target.write_text(json.dumps(_local_mcp_manifest(settings)), encoding="utf-8")

    with pytest.raises(ExtensionManifestRejected):
        registry.inspect_local("../outside.json")

    link = local_dir / "linked.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available")
    with pytest.raises(ExtensionManifestRejected):
        registry.inspect_local("linked.json")


def test_manifest_v1_still_rejects_connector_and_executable_extensions():
    base = {
        "schema_version": 1,
        "id": "connector.demo",
        "name": "Unsafe connector",
        "version": "1",
        "publisher": "Test",
        "origin": "builtin",
        "kind": "integration",
        "entrypoint": {"type": "builtin", "adapter": "github"},
        "permissions": [
            {
                "id": "network.github",
                "risk": "external_read",
                "description": "Read GitHub.",
                "required": True,
            }
        ],
        "health_probe": "static",
    }
    with pytest.raises(ValidationError):
        parse_extension_manifest(base)
    for entrypoint_type in ("python", "shell"):
        changed = json.loads(json.dumps(base))
        changed["entrypoint"] = {"type": entrypoint_type, "adapter": "github"}
        with pytest.raises(ValidationError):
            parse_extension_manifest(changed)


def test_extension_api_contract_and_mutation_guard(registry_env):
    registry, _settings, _local_dir = registry_env
    guard_calls = []

    def require_local(request):
        guard_calls.append((request.method, request.url.path))

    def error_payload(code, message, recoverable=True, **_kwargs):
        return {"code": code, "message": message, "recoverable": recoverable}

    app = FastAPI()
    app.include_router(
        build_extensions_router(
            registry=registry,
            require_local=require_local,
            error_payload=error_payload,
        )
    )
    client = TestClient(app)

    response = client.get("/api/extensions?project_id=project_1")
    assert response.status_code == 200
    assert {"installed", "available", "local", "connectors", "unavailable"} == set(
        response.json()["sections"]
    )
    github = next(
        item
        for item in response.json()["extensions"]
        if item["id"] == "connector.github"
    )
    digest = github["manifest_sha256"]
    assert client.post(
        "/api/extensions/connector.github/install",
        json={"manifest_sha256": digest},
    ).status_code == 200
    assert client.patch(
        "/api/extensions/connector.github/state",
        json={"global_enabled": True, "manifest_sha256": digest},
    ).status_code == 200
    assert client.put(
        "/api/projects/project_1/extensions/connector.github",
        json={"mode": "disabled"},
    ).status_code == 200
    assert client.post("/api/extensions/connector.github/health").status_code == 200
    assert client.get("/api/extensions/connector.github/audits").status_code == 200
    assert client.delete("/api/extensions/connector.github").status_code == 200

    unavailable = next(
        item
        for item in response.json()["extensions"]
        if item["id"] == "builtin.cursor"
    )
    rejected = client.patch(
        "/api/extensions/builtin.cursor/state",
        json={
            "global_enabled": True,
            "manifest_sha256": unavailable["manifest_sha256"],
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "EXTENSION_UNAVAILABLE"

    assert [method for method, _path in guard_calls] == [
        "POST",
        "PATCH",
        "PUT",
        "POST",
        "DELETE",
        "PATCH",
    ]


def test_extension_audit_redacts_secret_values(registry_env):
    registry, _settings, _local_dir = registry_env
    registry.initialize()
    credential_shaped_marker = "sk-" + "ABCDEFGHIJKLMNOPQRSTUV"
    registry.store.record_failure(
        "builtin.n8n",
        "configure",
        {"api_key": "do-not-store-this", "note": credential_shaped_marker},
    )
    serialized = json.dumps(registry.audits("builtin.n8n"), ensure_ascii=False)
    assert "do-not-store-this" not in serialized
    assert credential_shaped_marker not in serialized
    assert "[redacted]" in serialized
