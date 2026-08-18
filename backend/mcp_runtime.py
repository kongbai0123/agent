"""Minimal secure local stdio MCP Tools runtime.

This client supports only a reviewed local executable and the MCP ``tools``
surface.  It deliberately does not implement HTTP transports, prompts,
resources, sampling, package installation, a shell, or arbitrary environment
inheritance.  The process boundary provides fault isolation, not an OS
sandbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional

from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if __package__:
    from .subprocess_env import (
        agent_subprocess_env,
        is_allowed_subprocess_env_name,
        is_secret_env_name,
    )
    from .tool_runtime import ToolAccess, ToolCall, ToolDefinition
else:  # pragma: no cover - direct backend path imports used by the application
    from subprocess_env import (
        agent_subprocess_env,
        is_allowed_subprocess_env_name,
        is_secret_env_name,
    )
    from tool_runtime import ToolAccess, ToolCall, ToolDefinition


MCP_PROTOCOL_VERSION = "2025-06-18"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_ALIAS = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_EXTENSION_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SHELL_SUFFIXES = {".bat", ".cmd", ".ps1", ".sh"}
_MAX_MESSAGE_BYTES = 1024 * 1024


class MCPRuntimeError(RuntimeError):
    code = "MCP_RUNTIME_ERROR"


class MCPConfigurationError(MCPRuntimeError, ValueError):
    code = "MCP_CONFIGURATION_INVALID"


class MCPProtocolError(MCPRuntimeError):
    code = "MCP_PROTOCOL_ERROR"


class MCPProcessUnavailable(MCPRuntimeError):
    code = "MCP_PROCESS_UNAVAILABLE"
    execution_state_unknown = True


class MCPEnvironmentValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: Literal["literal", "secret_alias"]
    value: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_value(self) -> "MCPEnvironmentValue":
        if "\x00" in self.value or "\r" in self.value or "\n" in self.value:
            raise ValueError("environment values cannot contain control line breaks")
        if self.source == "secret_alias" and not _ALIAS.fullmatch(self.value):
            raise ValueError("secret alias is invalid")
        return self


class MCPToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    access: Literal["read", "write"]
    risk_level: Literal[
        "read",
        "external_read",
        "verify",
        "write",
        "external_write",
        "system",
        "irreversible",
    ]
    requires_connection: bool = False
    requires_resource: bool = False

    @model_validator(mode="after")
    def validate_risk(self) -> "MCPToolPolicy":
        if self.access == "write" and self.risk_level in {
            "read",
            "external_read",
            "verify",
        }:
            raise ValueError("write MCP tools require a write-class risk")
        return self


class MCPStdioSettings(BaseModel):
    """Persistable, strict settings for one trusted local MCP process."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    id: str = Field(min_length=1, max_length=96)
    extension_id: str = Field(min_length=1, max_length=128)
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    transport: Literal["stdio"] = "stdio"
    executable: str = Field(min_length=1, max_length=1024)
    expected_executable_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    arguments: list[str] = Field(default_factory=list, max_length=64)
    cwd: Optional[str] = Field(default=None, max_length=1024)
    environment: dict[str, MCPEnvironmentValue] = Field(default_factory=dict, max_length=64)
    tools: dict[str, MCPToolPolicy] = Field(default_factory=dict, max_length=128)
    startup_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    call_timeout_seconds: float = Field(default=30.0, gt=0, le=60)
    protocol_version: str = Field(default=MCP_PROTOCOL_VERSION, min_length=1, max_length=32)

    @field_validator("id", "extension_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not _EXTENSION_ID.fullmatch(normalized):
            raise ValueError("identifier is invalid")
        return normalized

    @field_validator("arguments")
    @classmethod
    def validate_arguments(cls, value: list[str]) -> list[str]:
        for argument in value:
            if not isinstance(argument, str) or len(argument) > 2048 or "\x00" in argument:
                raise ValueError("MCP argument is invalid")
        return value

    @field_validator("tools")
    @classmethod
    def validate_tool_names(cls, value: dict[str, MCPToolPolicy]) -> dict[str, MCPToolPolicy]:
        for name in value:
            if not str(name).strip() or len(name) > 128 or any(ord(char) < 32 for char in name):
                raise ValueError("MCP tool name is invalid")
        return value

    @model_validator(mode="after")
    def validate_paths_and_environment_shape(self) -> "MCPStdioSettings":
        executable = Path(self.executable).expanduser()
        if not executable.is_absolute():
            raise ValueError("MCP executable must be an absolute path")
        if executable.suffix.casefold() in _SHELL_SUFFIXES:
            raise ValueError("shell scripts are not valid MCP executables")
        if self.cwd is not None and not Path(self.cwd).expanduser().is_absolute():
            raise ValueError("MCP cwd must be an absolute path")
        for name, reference in self.environment.items():
            if not _ENV_NAME.fullmatch(name):
                raise ValueError("MCP environment name is invalid")
            if reference.source == "literal" and (
                not is_allowed_subprocess_env_name(name) or is_secret_env_name(name)
            ):
                raise ValueError(
                    f"literal environment variable {name} is not in the operational allowlist"
                )
            if reference.source == "secret_alias" and not is_secret_env_name(name):
                raise ValueError(
                    f"secret alias may only populate a credential-named variable: {name}"
                )
        return self

    def validate_secure_paths(self, allowed_cwd_roots: tuple[Path, ...] = ()) -> "MCPPathAttestation":
        executable = _secure_regular_file(Path(self.executable).expanduser())
        executable_sha256 = _file_sha256(executable)
        if executable_sha256 != self.expected_executable_sha256:
            raise MCPConfigurationError("MCP executable digest does not match settings")
        roots = tuple(_secure_directory(Path(root)) for root in allowed_cwd_roots)
        if not roots:
            raise MCPConfigurationError("at least one allowed MCP cwd root is required")
        cwd = (
            _secure_directory(Path(self.cwd).expanduser())
            if self.cwd is not None
            else roots[0]
        )
        if not any(_is_within(cwd, root) for root in roots):
            raise MCPConfigurationError("MCP cwd is outside the allowed local roots")
        return MCPPathAttestation(
            executable=executable,
            executable_sha256=executable_sha256,
            cwd=cwd,
        )


class MCPPathAttestation(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    executable: Path
    executable_sha256: str
    cwd: Optional[Path]


class MCPDiscoveredTool(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str = ""
    input_schema: dict[str, Any]


def mcp_settings_sha256(settings: MCPStdioSettings) -> str:
    """Digest the complete non-secret config for Manifest V1 binding."""

    if not isinstance(settings, MCPStdioSettings):
        raise TypeError("settings must be MCPStdioSettings")
    canonical = json.dumps(
        settings.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _is_link_or_reparse(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(info.st_mode) or bool(
        attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


def _reject_link_chain(path: Path) -> None:
    current = path
    while True:
        try:
            info = current.lstat()
        except OSError as error:
            raise MCPConfigurationError(f"MCP path is unavailable: {current}") from error
        if _is_link_or_reparse(info):
            raise MCPConfigurationError("linked or reparse-point MCP paths are not permitted")
        if current.parent == current:
            break
        current = current.parent


def _secure_regular_file(path: Path) -> Path:
    absolute = path.absolute()
    _reject_link_chain(absolute)
    try:
        resolved = absolute.resolve(strict=True)
        info = resolved.stat()
    except OSError as error:
        raise MCPConfigurationError("MCP executable does not exist") from error
    if absolute != resolved or not stat.S_ISREG(info.st_mode):
        raise MCPConfigurationError("MCP executable must be an unlinked regular file")
    return resolved


def _secure_directory(path: Path) -> Path:
    absolute = path.absolute()
    _reject_link_chain(absolute)
    try:
        resolved = absolute.resolve(strict=True)
        info = resolved.stat()
    except OSError as error:
        raise MCPConfigurationError("MCP cwd does not exist") from error
    if absolute != resolved or not stat.S_ISDIR(info.st_mode):
        raise MCPConfigurationError("MCP cwd must be an unlinked directory")
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            [os.path.normcase(str(path)), os.path.normcase(str(root))]
        ) == os.path.normcase(str(root))
    except (OSError, ValueError):
        return False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise MCPConfigurationError("MCP executable could not be attested") from error
    return digest.hexdigest()


def _safe_tool_segment(value: str) -> str:
    segment = re.sub(r"[^a-z0-9_-]+", "_", str(value or "").strip().casefold())
    segment = segment.strip("_-")[:80]
    if not segment:
        raise MCPProtocolError("MCP tool name cannot be normalized safely")
    if not segment[0].isalpha():
        segment = f"tool_{segment}"[:80]
    return segment


SecretResolver = Callable[[str], str]


class MCPStdioClient:
    """One serial JSON-RPC client for one trusted local MCP process."""

    def __init__(
        self,
        settings: MCPStdioSettings,
        *,
        allowed_cwd_roots: tuple[Path, ...] = (),
        secret_resolver: Optional[SecretResolver] = None,
    ) -> None:
        if not isinstance(settings, MCPStdioSettings):
            raise TypeError("settings must be MCPStdioSettings")
        self.settings = settings
        self.allowed_cwd_roots = tuple(Path(root) for root in allowed_cwd_roots)
        self.secret_resolver = secret_resolver
        self.process: Optional[asyncio.subprocess.Process] = None
        self.attestation: Optional[MCPPathAttestation] = None
        self.server_info: Mapping[str, Any] = {}
        self._request_id = 0
        self._rpc_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._discovered: dict[str, MCPDiscoveredTool] = {}

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def _environment(self) -> dict[str, str]:
        environment = agent_subprocess_env()
        for name, reference in self.settings.environment.items():
            if reference.source == "literal":
                environment[name] = reference.value
                continue
            if self.secret_resolver is None:
                raise MCPConfigurationError(
                    f"secret resolver is required for environment variable {name}"
                )
            secret = await asyncio.to_thread(self.secret_resolver, reference.value)
            if not isinstance(secret, str) or not secret or "\x00" in secret:
                raise MCPConfigurationError("secret alias did not resolve to a valid value")
            environment[name] = secret
        return environment

    async def start(self) -> tuple[MCPDiscoveredTool, ...]:
        async with self._lifecycle_lock:
            if self.running:
                return tuple(self._discovered[name] for name in sorted(self._discovered))
            attestation = await asyncio.to_thread(
                self.settings.validate_secure_paths, self.allowed_cwd_roots
            )
            environment = await self._environment()
            # Repeat attestation immediately before process creation to narrow
            # replacement races.  expected_executable_sha256, when persisted,
            # binds the executable across Workbench restarts as well.
            repeated = await asyncio.to_thread(
                self.settings.validate_secure_paths, self.allowed_cwd_roots
            )
            if repeated.executable_sha256 != attestation.executable_sha256:
                raise MCPConfigurationError("MCP executable changed during launch")
            creationflags = (
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
            )
            try:
                self.process = await asyncio.create_subprocess_exec(
                    str(attestation.executable),
                    *self.settings.arguments,
                    cwd=str(attestation.cwd) if attestation.cwd else None,
                    env=environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=creationflags,
                    limit=_MAX_MESSAGE_BYTES,
                )
                self.attestation = attestation
                self._stderr_task = asyncio.create_task(self._drain_stderr())
                initialize = await self._request(
                    "initialize",
                    {
                        "protocolVersion": self.settings.protocol_version,
                        "capabilities": {},
                        "clientInfo": {"name": "LocalAIWorkbench", "version": "1"},
                    },
                    timeout=self.settings.startup_timeout_seconds,
                )
                if not isinstance(initialize, Mapping):
                    raise MCPProtocolError("MCP initialize result must be an object")
                if initialize.get("protocolVersion") != self.settings.protocol_version:
                    raise MCPProtocolError("MCP server negotiated an unexpected protocol version")
                capabilities = initialize.get("capabilities")
                if not isinstance(capabilities, Mapping) or not isinstance(
                    capabilities.get("tools"), Mapping
                ):
                    raise MCPProtocolError("MCP server did not declare the tools capability")
                self.server_info = dict(initialize.get("serverInfo") or {})
                await self._notify("notifications/initialized", {})
                discovered = await self.list_tools()
                return discovered
            except BaseException:
                await self._stop_unlocked()
                raise

    async def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            while await process.stderr.read(8192):
                pass
        except (OSError, asyncio.CancelledError):
            return

    def _require_process(self) -> asyncio.subprocess.Process:
        if not self.running or self.process is None:
            raise MCPProcessUnavailable("MCP process is not running")
        if self.process.stdin is None or self.process.stdout is None:
            raise MCPProcessUnavailable("MCP stdio transport is unavailable")
        return self.process

    async def _write(self, message: Mapping[str, Any]) -> None:
        process = self._require_process()
        encoded = json.dumps(
            dict(message), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(encoded) > _MAX_MESSAGE_BYTES:
            raise MCPProtocolError("MCP request exceeds the message limit")
        process.stdin.write(encoded + b"\n")
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionError) as error:
            raise MCPProcessUnavailable("MCP process closed stdin") from error

    async def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    async def _read_message(self) -> Mapping[str, Any]:
        process = self._require_process()
        try:
            line = await process.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError) as error:
            raise MCPProtocolError("MCP response exceeds the message limit") from error
        if not line:
            raise MCPProcessUnavailable(
                f"MCP process exited unexpectedly with code {process.returncode}"
            )
        if len(line) > _MAX_MESSAGE_BYTES:
            raise MCPProtocolError("MCP response exceeds the message limit")
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MCPProtocolError("MCP process returned invalid JSON") from error
        if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
            raise MCPProtocolError("MCP process returned an invalid JSON-RPC message")
        return message

    async def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        async with self._rpc_lock:
            self._request_id += 1
            request_id = self._request_id
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                }
            )

            async def wait_for_response() -> Any:
                while True:
                    message = await self._read_message()
                    if "method" in message and "id" in message:
                        # Tools-only clients do not grant server-side sampling,
                        # roots or elicitation capabilities.
                        await self._write(
                            {
                                "jsonrpc": "2.0",
                                "id": message["id"],
                                "error": {"code": -32601, "message": "Method not supported"},
                            }
                        )
                        continue
                    if message.get("id") != request_id:
                        # Notifications and stale replies carry no authority.
                        continue
                    if "error" in message:
                        error = message.get("error")
                        safe_message = (
                            str(error.get("message") or "MCP request failed")
                            if isinstance(error, Mapping)
                            else "MCP request failed"
                        )
                        raise MCPProtocolError(safe_message[:500])
                    if "result" not in message:
                        raise MCPProtocolError("MCP response has no result")
                    return message["result"]

            try:
                return await asyncio.wait_for(
                    wait_for_response(),
                    timeout=timeout or self.settings.call_timeout_seconds,
                )
            except asyncio.TimeoutError as error:
                raise MCPProcessUnavailable(f"MCP request {method} timed out") from error

    async def list_tools(self) -> tuple[MCPDiscoveredTool, ...]:
        result = await self._request("tools/list", {})
        if not isinstance(result, Mapping) or not isinstance(result.get("tools"), list):
            raise MCPProtocolError("MCP tools/list returned an invalid result")
        discovered: dict[str, MCPDiscoveredTool] = {}
        for raw in result["tools"]:
            if not isinstance(raw, Mapping):
                raise MCPProtocolError("MCP tool descriptor must be an object")
            name = str(raw.get("name") or "").strip()
            if not name or len(name) > 128 or name in discovered:
                raise MCPProtocolError("MCP tool name is invalid or duplicated")
            schema = raw.get("inputSchema")
            if not isinstance(schema, Mapping) or schema.get("type") != "object":
                raise MCPProtocolError("MCP tool inputSchema must declare an object")
            try:
                Draft202012Validator.check_schema(dict(schema))
            except SchemaError as error:
                raise MCPProtocolError("MCP tool inputSchema is invalid") from error
            discovered[name] = MCPDiscoveredTool(
                name=name,
                description=str(raw.get("description") or "")[:1000],
                input_schema=dict(schema),
            )
        self._discovered = discovered
        return tuple(discovered[name] for name in sorted(discovered))

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        if name not in self._discovered:
            raise MCPProtocolError("MCP tool is not in the attested discovery snapshot")
        result = await self._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
            timeout=self.settings.call_timeout_seconds,
        )
        if not isinstance(result, Mapping):
            raise MCPProtocolError("MCP tools/call returned an invalid result")
        if result.get("isError") is True:
            raise MCPProtocolError("MCP tool reported an execution error")
        return dict(result)

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        """Expose only tools explicitly reviewed in ``settings.tools``."""

        definitions: list[ToolDefinition] = []
        seen_names: set[str] = set()
        for raw_name, policy in sorted(self.settings.tools.items()):
            discovered = self._discovered.get(raw_name)
            if discovered is None:
                continue
            namespace = self.settings.extension_id
            if namespace.startswith("mcp."):
                namespace = namespace[4:]
            public_name = f"mcp.{namespace}.{_safe_tool_segment(raw_name)}"
            if public_name in seen_names:
                raise MCPProtocolError("MCP tool names collide after normalization")
            seen_names.add(public_name)

            async def handler(call: ToolCall, *, _raw_name: str = raw_name) -> Any:
                return await self.call_tool(_raw_name, call.arguments)

            definitions.append(
                ToolDefinition(
                    name=public_name,
                    description=discovered.description or f"Local MCP tool {raw_name}",
                    input_schema=discovered.input_schema,
                    access=ToolAccess(policy.access),
                    handler=handler,
                    extension_id=self.settings.extension_id,
                    manifest_sha256=self.settings.manifest_sha256,
                    risk_level=policy.risk_level,
                    timeout_seconds=self.settings.call_timeout_seconds,
                    requires_connection=policy.requires_connection,
                    requires_resource=policy.requires_resource,
                )
            )
        return tuple(definitions)

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self) -> None:
        process = self.process
        self.process = None
        self._discovered = {}
        if process is not None and process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=2)
            except (ProcessLookupError, asyncio.TimeoutError):
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

    async def __aenter__(self) -> "MCPStdioClient":
        await self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.stop()


class MCPRuntimeManager:
    """Lifecycle owner ensuring one isolated process per enabled extension."""

    def __init__(
        self,
        *,
        allowed_cwd_roots: tuple[Path, ...] = (),
        secret_resolver: Optional[SecretResolver] = None,
    ) -> None:
        self.allowed_cwd_roots = allowed_cwd_roots
        self.secret_resolver = secret_resolver
        self._clients: dict[str, MCPStdioClient] = {}
        self._lock = asyncio.Lock()

    async def enable(self, settings: MCPStdioSettings) -> MCPStdioClient:
        async with self._lock:
            existing = self._clients.get(settings.extension_id)
            if existing is not None:
                if existing.settings == settings and existing.running:
                    return existing
                await existing.stop()
            client = MCPStdioClient(
                settings,
                allowed_cwd_roots=self.allowed_cwd_roots,
                secret_resolver=self.secret_resolver,
            )
            await client.start()
            self._clients[settings.extension_id] = client
            return client

    async def disable(self, extension_id: str) -> bool:
        async with self._lock:
            client = self._clients.pop(str(extension_id or "").strip().casefold(), None)
            if client is None:
                return False
            await client.stop()
            return True

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        definitions = [
            definition
            for extension_id in sorted(self._clients)
            for definition in self._clients[extension_id].tool_definitions()
        ]
        return tuple(definitions)

    async def shutdown(self) -> None:
        async with self._lock:
            clients = tuple(self._clients.values())
            self._clients.clear()
        await asyncio.gather(*(client.stop() for client in clients), return_exceptions=True)


__all__ = [
    "MCPConfigurationError",
    "MCPDiscoveredTool",
    "MCPEnvironmentValue",
    "MCPPathAttestation",
    "MCPProcessUnavailable",
    "MCPProtocolError",
    "MCPRuntimeError",
    "MCPRuntimeManager",
    "MCPStdioClient",
    "MCPStdioSettings",
    "MCPToolPolicy",
    "mcp_settings_sha256",
]
