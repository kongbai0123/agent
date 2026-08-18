from __future__ import annotations

import asyncio
import hashlib
import sys
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from extension_catalog import settings_manifests
from extension_manifest import manifest_sha256, parse_extension_manifest
from mcp_coordinator import (
    MCPSettingsAdapterError,
    MCPSettingsCoordinator,
    mcp_stdio_settings_from_mapping,
)
from tool_runtime import ToolAccess, ToolDefinition, ToolRegistry, ToolUnavailableError


def _executable_digest() -> str:
    return hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()


def _write_fixture_server(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """
            import json
            import sys

            tools = [
                {
                    "name": "echo",
                    "description": "Echo an integer",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "not_reviewed",
                    "description": "Must stay hidden",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ]
            for line in sys.stdin:
                message = json.loads(line)
                if "id" not in message:
                    continue
                method = message.get("method")
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "coordinator-fixture", "version": "1"},
                    }
                elif method == "tools/list":
                    result = {"tools": tools}
                elif method == "tools/call":
                    value = message["params"]["arguments"].get("value")
                    result = {"content": [{"type": "text", "text": str(value)}]}
                else:
                    response = {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "unknown"},
                    }
                    print(json.dumps(response), flush=True)
                    continue
                print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
            """
        ),
        encoding="utf-8",
    )


def _settings(tmp_path: Path, script: Path) -> dict:
    return {
        "mcp_servers": [
            {
                "id": "echo",
                "transport": "stdio",
                "executable": str(Path(sys.executable).resolve()),
                "expected_executable_sha256": _executable_digest(),
                "argv": ["-u", str(script)],
                "cwd": str(tmp_path.resolve()),
                "allowed_cwd_roots": [str(tmp_path.resolve())],
                "environment_keys": [],
                "secret_aliases": {},
                "tool_policies": {
                    "echo": {
                        "access": "read",
                        "risk_level": "external_read",
                        "requires_connection": False,
                        "requires_resource": False,
                    }
                },
                "startup_timeout_seconds": 5,
                "timeout_seconds": 30,
                "protocol_version": "2025-06-18",
                "enabled": True,
            }
        ]
    }


class FakeExtensionRegistry:
    def __init__(self, settings: dict):
        manifest = settings_manifests(settings)[0]
        self.item = {
            **manifest.model_dump(mode="json"),
            "manifest_sha256": manifest_sha256(manifest),
        }
        self.enabled = {None: True, "project-a": True, "project-b": False}

    def get(self, extension_id, project_id=None, *, synchronize=True):
        assert extension_id == self.item["id"]
        return dict(self.item)

    def is_effectively_enabled(self, extension_id, project_id=None):
        return extension_id == self.item["id"] and bool(self.enabled.get(project_id, False))


def _global_tool() -> ToolDefinition:
    return ToolDefinition(
        name="system.ping",
        description="Global host tool",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        access=ToolAccess.READ,
        handler=lambda _call: {"ok": True},
        extension_id="builtin.host",
        manifest_sha256=hashlib.sha256(b"global host tool").hexdigest(),
    )


def test_settings_adapter_rejects_remote_transport_plaintext_env_and_missing_attestation(
    monkeypatch,
):
    base = {
        "id": "demo",
        "transport": "stdio",
        "executable": str(Path(sys.executable).resolve()),
        "expected_executable_sha256": _executable_digest(),
        "environment_keys": [],
        "tool_policies": {},
    }
    kwargs = {"extension_id": "mcp.demo", "manifest_digest": "a" * 64}

    with pytest.raises(MCPSettingsAdapterError):
        mcp_stdio_settings_from_mapping(
            {**base, "transport": "http"}, **kwargs
        )
    without_digest = dict(base)
    without_digest.pop("expected_executable_sha256")
    with pytest.raises(MCPSettingsAdapterError, match="attestation"):
        mcp_stdio_settings_from_mapping(without_digest, **kwargs)
    with pytest.raises(MCPSettingsAdapterError, match="reference objects"):
        mcp_stdio_settings_from_mapping(
            {
                **base,
                "environment_keys": ["SERVICE_TOKEN"],
                "environment": {"SERVICE_TOKEN": "plaintext-secret"},
            },
            **kwargs,
        )
    spaced = mcp_stdio_settings_from_mapping(
        {**base, "id": "Demo Server"},
        extension_id="mcp.demo-server",
        manifest_digest="b" * 64,
    )
    assert spaced.id == "demo-server"
    assert spaced.extension_id == "mcp.demo-server"

    monkeypatch.setenv("LANG", "zh_TW.UTF-8")
    inherited = mcp_stdio_settings_from_mapping(
        {**base, "environment_keys": ["LANG"]},
        **kwargs,
    )
    assert inherited.environment["LANG"].source == "literal"
    assert inherited.environment["LANG"].value == "zh_TW.UTF-8"

    monkeypatch.delenv("LC_ALL", raising=False)
    absent = mcp_stdio_settings_from_mapping(
        {**base, "environment_keys": ["LC_ALL"]},
        **kwargs,
    )
    assert "LC_ALL" not in absent.environment


def test_manifest_digest_binds_executable_attestation_environment_and_tool_policy(tmp_path):
    script = tmp_path / "fixture_mcp.py"
    _write_fixture_server(script)
    original = _settings(tmp_path, script)
    original_digest = manifest_sha256(settings_manifests(original)[0])

    changed_policy = {"mcp_servers": [{**original["mcp_servers"][0]}]}
    changed_policy["mcp_servers"][0]["tool_policies"] = {
        "echo": {"access": "write", "risk_level": "external_write"}
    }
    changed_executable = {"mcp_servers": [{**original["mcp_servers"][0]}]}
    changed_executable["mcp_servers"][0]["expected_executable_sha256"] = "0" * 64

    assert manifest_sha256(settings_manifests(changed_policy)[0]) != original_digest
    assert manifest_sha256(settings_manifests(changed_executable)[0]) != original_digest


def test_coordinator_runs_only_effective_project_and_exposes_only_reviewed_tools(tmp_path):
    script = tmp_path / "fixture_mcp.py"
    _write_fixture_server(script)
    settings = _settings(tmp_path, script)
    extensions = FakeExtensionRegistry(settings)
    tools = ToolRegistry((_global_tool(),))
    coordinator = MCPSettingsCoordinator(
        extension_registry=extensions,
        tool_registry=tools,
        allowed_cwd_roots=(tmp_path,),
        project_ids_provider=lambda: ("project-a", "project-b"),
    )

    async def scenario():
        try:
            result = await coordinator.sync_from_settings(settings)
            assert result["status"] == "healthy"
            assert result["running"] == 1
            assert [item.name for item in tools.for_project("project-a")] == [
                "mcp.echo.echo",
                "system.ping",
            ]
            assert [item.name for item in tools.for_project("project-b")] == [
                "system.ping"
            ]
            with pytest.raises(ToolUnavailableError):
                tools.get("project-a", "mcp.echo.not_reviewed")
            definition = tools.get("project-a", "mcp.echo.echo")
            response = await definition.handler(
                type("Call", (), {"arguments": {"value": 11}})()
            )
            assert response == {"content": [{"type": "text", "text": "11"}]}
            assert coordinator.health("mcp.echo")["projects"] == ["project-a"]

            first_client = coordinator._active["mcp.echo"].client
            await coordinator.sync_from_settings(settings)
            assert coordinator._active["mcp.echo"].client is first_client

            extensions.enabled["project-a"] = False
            extensions.enabled["project-b"] = True
            moved = await coordinator.sync_from_settings(settings)
            assert moved["running"] == 1
            assert coordinator._active["mcp.echo"].client is first_client
            with pytest.raises(ToolUnavailableError):
                tools.get("project-a", "mcp.echo.echo")
            assert tools.get("project-b", "mcp.echo.echo").name == "mcp.echo.echo"

            extensions.enabled["project-b"] = False
            disabled = await coordinator.sync_from_settings(settings)
            assert disabled["running"] == 0
            assert coordinator.health("mcp.echo")["status"] == "disabled"
            with pytest.raises(ToolUnavailableError):
                tools.get("project-a", "mcp.echo.echo")
            assert tools.get("project-a", "system.ping").name == "system.ping"
        finally:
            await coordinator.stop_all()

    asyncio.run(scenario())


def test_trusted_local_manifest_metadata_keeps_registry_digest_as_runtime_binding(tmp_path):
    script = tmp_path / "fixture_mcp.py"
    _write_fixture_server(script)
    settings = _settings(tmp_path, script)
    derived = settings_manifests(settings)[0]
    local_payload = derived.model_dump(mode="json")
    local_payload.update(
        {
            "name": "Reviewed Echo MCP",
            "version": "1.0.0",
            "description": "A manually reviewed local Manifest.",
            "publisher": "Local reviewer",
            "default_installed": False,
            "default_enabled": False,
        }
    )
    local_manifest = parse_extension_manifest(local_payload)
    local_digest = manifest_sha256(local_manifest)
    assert local_digest != manifest_sha256(derived)
    assert (
        local_manifest.entrypoint.configuration_sha256
        == derived.entrypoint.configuration_sha256
    )

    extensions = FakeExtensionRegistry(settings)
    extensions.item = {
        **local_manifest.model_dump(mode="json"),
        "manifest_sha256": local_digest,
    }
    tools = ToolRegistry()
    coordinator = MCPSettingsCoordinator(
        extension_registry=extensions,
        tool_registry=tools,
        allowed_cwd_roots=(tmp_path,),
        project_ids_provider=lambda: ("project-a",),
    )

    async def scenario():
        try:
            result = await coordinator.sync_from_settings(settings)
            assert result["status"] == "healthy"
            assert result["running"] == 1
            definition = tools.get("project-a", "mcp.echo.echo")
            assert definition.manifest_sha256 == local_digest
            assert (
                coordinator._active["mcp.echo"].settings.manifest_sha256
                == local_digest
            )
        finally:
            await coordinator.stop_all()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("type", "builtin"),
        ("adapter", "different-adapter"),
        ("settings_id", "different-settings"),
        ("configuration_sha256", "0" * 64),
    ],
)
def test_registry_entrypoint_must_match_every_settings_binding_field(
    tmp_path,
    field,
    replacement,
):
    script = tmp_path / "fixture_mcp.py"
    _write_fixture_server(script)
    settings = _settings(tmp_path, script)
    extensions = FakeExtensionRegistry(settings)
    extensions.item["entrypoint"] = {
        **extensions.item["entrypoint"],
        field: replacement,
    }
    coordinator = MCPSettingsCoordinator(
        extension_registry=extensions,
        tool_registry=ToolRegistry(),
        allowed_cwd_roots=(tmp_path,),
        project_ids_provider=lambda: ("project-a",),
    )

    result = asyncio.run(coordinator.sync_from_settings(settings))

    assert result["status"] == "degraded"
    assert result["running"] == 0
    health = coordinator.health("mcp.echo")
    assert health["error_code"] == "MCP_MANIFEST_MISMATCH"


def test_manifest_change_stops_old_process_and_requires_registry_retrust(tmp_path):
    script = tmp_path / "fixture_mcp.py"
    _write_fixture_server(script)
    settings = _settings(tmp_path, script)
    extensions = FakeExtensionRegistry(settings)
    tools = ToolRegistry()
    coordinator = MCPSettingsCoordinator(
        extension_registry=extensions,
        tool_registry=tools,
        allowed_cwd_roots=(tmp_path,),
        project_ids_provider=lambda: ("project-a",),
    )

    async def scenario():
        await coordinator.sync_from_settings(settings)
        first_client = coordinator._active["mcp.echo"].client
        changed = {"mcp_servers": [{**settings["mcp_servers"][0]}]}
        changed["mcp_servers"][0]["argv"] = [
            *changed["mcp_servers"][0]["argv"],
            "--configuration-changed",
        ]
        result = await coordinator.sync_from_settings(changed)
        assert result["status"] == "degraded"
        assert result["running"] == 0
        assert not first_client.running
        health = coordinator.health("mcp.echo")
        assert health["status"] == "error"
        assert health["error_code"] == "MCP_MANIFEST_MISMATCH"
        with pytest.raises(ToolUnavailableError):
            tools.get("project-a", "mcp.echo.echo")
        await coordinator.stop_all()

    asyncio.run(scenario())


def test_stop_all_preserves_global_tools_and_is_idempotent(tmp_path):
    script = tmp_path / "fixture_mcp.py"
    _write_fixture_server(script)
    settings = _settings(tmp_path, script)
    tools = ToolRegistry((_global_tool(),))
    coordinator = MCPSettingsCoordinator(
        extension_registry=FakeExtensionRegistry(settings),
        tool_registry=tools,
        allowed_cwd_roots=(tmp_path,),
        project_ids_provider=lambda: ("project-a",),
    )

    async def scenario():
        await coordinator.sync_from_settings(settings)
        await coordinator.stop_all()
        await coordinator.stop_all()
        assert coordinator.health()["running"] == 0
        assert tools.get("project-a", "system.ping").name == "system.ping"
        with pytest.raises(ToolUnavailableError):
            tools.get("project-a", "mcp.echo.echo")

    asyncio.run(scenario())
