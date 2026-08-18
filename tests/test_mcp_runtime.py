from __future__ import annotations

import asyncio
import hashlib
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.mcp_runtime import (
    MCPConfigurationError,
    MCPEnvironmentValue,
    MCPStdioClient,
    MCPStdioSettings,
    MCPToolPolicy,
    mcp_settings_sha256,
)


DIGEST = hashlib.sha256(b"local mcp extension").hexdigest()


def executable_digest() -> str:
    return hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()


def settings(tmp_path: Path, script: Path, **changes) -> MCPStdioSettings:
    values = {
        "id": "echo-server",
        "extension_id": "local.echo",
        "manifest_sha256": DIGEST,
        "executable": str(Path(sys.executable).resolve()),
        "expected_executable_sha256": executable_digest(),
        "arguments": ["-u", str(script)],
        "cwd": str(tmp_path.resolve()),
        "tools": {
            "echo": MCPToolPolicy(access="read", risk_level="external_read")
        },
    }
    values.update(changes)
    return MCPStdioSettings(**values)


def write_server(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """
            import json
            import sys

            for line in sys.stdin:
                message = json.loads(line)
                if "id" not in message:
                    continue
                method = message.get("method")
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fixture", "version": "1"},
                    }
                elif method == "tools/list":
                    result = {
                        "tools": [{
                            "name": "echo",
                            "description": "Echo an integer",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"value": {"type": "integer"}},
                                "required": ["value"],
                                "additionalProperties": False,
                            },
                        }]
                    }
                elif method == "tools/call":
                    result = {"content": [{"type": "text", "text": str(message["params"]["arguments"]["value"])}]}
                else:
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "unknown"}}) + "\\n")
                    sys.stdout.flush()
                    continue
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\\n")
                sys.stdout.flush()
            """
        ),
        encoding="utf-8",
    )


def test_stdio_settings_reject_shell_relative_paths_and_plaintext_secret_env(tmp_path):
    with pytest.raises(ValidationError, match="absolute path"):
        MCPStdioSettings(
            id="bad", extension_id="local.bad", manifest_sha256=DIGEST,
            executable="python", expected_executable_sha256="0" * 64, tools={},
        )
    shell = tmp_path / "server.cmd"
    with pytest.raises(ValidationError, match="shell scripts"):
        MCPStdioSettings(
            id="bad", extension_id="local.bad", manifest_sha256=DIGEST,
            executable=str(shell.resolve()), expected_executable_sha256="0" * 64, tools={},
        )
    with pytest.raises(ValidationError, match="literal environment"):
        MCPStdioSettings(
            id="bad", extension_id="local.bad", manifest_sha256=DIGEST,
            executable=str(Path(sys.executable).resolve()),
            expected_executable_sha256=executable_digest(),
            environment={
                "API_TOKEN": MCPEnvironmentValue(source="literal", value="never-store-this")
            },
            tools={},
        )


def test_path_attestation_rejects_cwd_outside_allowed_roots(tmp_path):
    script = tmp_path / "server.py"
    write_server(script)
    configured = settings(tmp_path, script)
    different = tmp_path / "different"
    different.mkdir()

    with pytest.raises(MCPConfigurationError, match="outside"):
        configured.validate_secure_paths((different,))


def test_settings_digest_is_stable_and_contains_no_resolved_secret(tmp_path):
    script = tmp_path / "server.py"
    write_server(script)
    configured = settings(
        tmp_path,
        script,
        environment={
            "SERVICE_TOKEN": MCPEnvironmentValue(source="secret_alias", value="vault.mcp-token")
        },
    )
    assert mcp_settings_sha256(configured) == mcp_settings_sha256(configured)
    assert "secret-value" not in configured.model_dump_json()


def test_local_stdio_client_discovers_only_allowlisted_tools_and_calls_them(tmp_path):
    script = tmp_path / "server.py"
    write_server(script)
    configured = settings(tmp_path, script)

    async def exercise():
        client = MCPStdioClient(configured, allowed_cwd_roots=(tmp_path,))
        try:
            discovered = await client.start()
            assert [tool.name for tool in discovered] == ["echo"]
            assert client.attestation.executable_sha256 == executable_digest()
            definitions = client.tool_definitions()
            assert [tool.name for tool in definitions] == ["mcp.local.echo.echo"]
            result = await definitions[0].handler(
                type("Call", (), {"arguments": {"value": 9}})()
            )
            assert result == {"content": [{"type": "text", "text": "9"}]}
        finally:
            await client.stop()
        assert not client.running

    asyncio.run(exercise())


def test_secret_alias_is_resolved_only_for_child_environment(tmp_path):
    script = tmp_path / "server.py"
    write_server(script)
    configured = settings(
        tmp_path,
        script,
        environment={
            "SERVICE_TOKEN": MCPEnvironmentValue(source="secret_alias", value="vault.mcp-token")
        },
    )
    seen = []
    client = MCPStdioClient(
        configured,
        allowed_cwd_roots=(tmp_path,),
        secret_resolver=lambda alias: seen.append(alias) or "secret-value",
    )

    async def exercise():
        await client.start()
        await client.stop()

    asyncio.run(exercise())
    assert seen == ["vault.mcp-token"]
