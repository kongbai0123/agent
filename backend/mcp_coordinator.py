"""Extension-gated coordination for trusted local stdio MCP servers.

This module is the adapter between validated ``settings['mcp_servers']``, the
Extension Registry, :class:`mcp_runtime.MCPStdioClient`, and the project-safe
host ToolRegistry.  It intentionally has no HTTP transport, downloader,
installer, package-manager, shell, or FastAPI dependency.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from pydantic import ValidationError

if __package__:
    from .extension_catalog import settings_manifests
    from .extension_manifest import safe_settings_identifier
    from .mcp_runtime import (
        MCPEnvironmentValue,
        MCPStdioClient,
        MCPStdioSettings,
        MCPToolPolicy,
        mcp_settings_sha256,
    )
    from .structured_log import redact
    from .tool_runtime import ToolDefinition, ToolRegistry
else:  # pragma: no cover - direct backend path imports used by the application
    from extension_catalog import settings_manifests
    from extension_manifest import safe_settings_identifier
    from mcp_runtime import (
        MCPEnvironmentValue,
        MCPStdioClient,
        MCPStdioSettings,
        MCPToolPolicy,
        mcp_settings_sha256,
    )
    from structured_log import redact
    from tool_runtime import ToolDefinition, ToolRegistry


_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class MCPCoordinatorError(RuntimeError):
    code = "MCP_COORDINATOR_ERROR"


class MCPSettingsAdapterError(MCPCoordinatorError, ValueError):
    code = "MCP_SETTINGS_INVALID"


class MCPManifestMismatch(MCPCoordinatorError):
    code = "MCP_MANIFEST_MISMATCH"


class MCPRegistrationError(MCPCoordinatorError):
    code = "MCP_TOOL_REGISTRATION_FAILED"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: Any, field_name: str, *, maximum: int = 512) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise MCPSettingsAdapterError(f"{field_name} is invalid")
    return text


def _string_list(value: Any, field_name: str, *, maximum: int = 64) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise MCPSettingsAdapterError(f"{field_name} must be a bounded list")
    result: list[str] = []
    for item in value:
        text = str(item) if isinstance(item, (str, int, float)) else ""
        if not text or len(text) > 2048 or "\x00" in text:
            raise MCPSettingsAdapterError(f"{field_name} contains an invalid argument")
        result.append(text)
    return result


def _environment(
    value: Any,
    declared_keys: Any,
    secret_aliases: Any,
) -> dict[str, MCPEnvironmentValue]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise MCPSettingsAdapterError("MCP environment must be an object of safe references")
    result: dict[str, MCPEnvironmentValue] = {}
    for raw_name, raw_reference in value.items():
        name = _bounded_text(raw_name, "environment name", maximum=128)
        if not isinstance(raw_reference, Mapping):
            raise MCPSettingsAdapterError(
                "MCP environment values must use literal or secret_alias reference objects"
            )
        try:
            result[name] = MCPEnvironmentValue.model_validate(dict(raw_reference))
        except ValidationError as error:
            raise MCPSettingsAdapterError(
                f"MCP environment reference is invalid: {name}"
            ) from error
    declared = set(_string_list(declared_keys, "environment_keys"))
    for name in sorted(declared):
        if name in result:
            raise MCPSettingsAdapterError(
                "MCP environment keys and explicit references cannot overlap"
            )
        # ``environment_keys`` is an allowlist of operational variables, not
        # persisted values.  Resolve it only at the final process boundary;
        # MCPStdioSettings re-validates that the name is non-secret and in the
        # shared subprocess allowlist.  An absent variable remains absent.
        inherited = os.environ.get(name)
        if inherited is not None:
            try:
                result[name] = MCPEnvironmentValue(
                    source="literal",
                    value=inherited,
                )
            except ValidationError as error:
                raise MCPSettingsAdapterError(
                    f"MCP operational environment value is invalid: {name}"
                ) from error
    if secret_aliases is None:
        secret_aliases = {}
    if not isinstance(secret_aliases, Mapping):
        raise MCPSettingsAdapterError("MCP secret_aliases must be an object")
    for raw_name, raw_alias in secret_aliases.items():
        name = _bounded_text(raw_name, "secret environment name", maximum=128)
        if name in declared or name in result:
            raise MCPSettingsAdapterError(
                "MCP environment keys and secret aliases cannot overlap"
            )
        try:
            result[name] = MCPEnvironmentValue(
                source="secret_alias",
                value=_bounded_text(raw_alias, "secret alias", maximum=256),
            )
        except ValidationError as error:
            raise MCPSettingsAdapterError(
                f"MCP secret alias is invalid: {name}"
            ) from error
    return result


def _tool_policies(value: Any) -> dict[str, MCPToolPolicy]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > 128:
        raise MCPSettingsAdapterError("MCP tool_policies must be an object")
    policies: dict[str, MCPToolPolicy] = {}
    for raw_name, raw_policy in value.items():
        name = _bounded_text(raw_name, "MCP tool name", maximum=128)
        if not isinstance(raw_policy, Mapping):
            raise MCPSettingsAdapterError(f"MCP Tool Policy is invalid: {name}")
        try:
            policies[name] = MCPToolPolicy.model_validate(dict(raw_policy))
        except ValidationError as error:
            raise MCPSettingsAdapterError(f"MCP Tool Policy is invalid: {name}") from error
    return policies


def mcp_stdio_settings_from_mapping(
    item: Mapping[str, Any],
    *,
    extension_id: str,
    manifest_digest: str,
) -> MCPStdioSettings:
    """Convert one already-validated settings record to strict runtime data.

    The adapter remains defensive because this is the final boundary before a
    process launch.  Raw environment strings, missing executable attestation,
    non-stdio transports and unreviewed discovered tools are rejected.
    """

    if not isinstance(item, Mapping):
        raise MCPSettingsAdapterError("MCP server settings must be an object")
    settings_id = _bounded_text(item.get("id"), "MCP settings ID", maximum=96)
    runtime_id = safe_settings_identifier(settings_id)
    expected_extension_id = f"mcp.{runtime_id}"
    if str(extension_id or "").strip().casefold() != expected_extension_id:
        raise MCPSettingsAdapterError("MCP extension ID does not match its settings ID")
    digest = str(manifest_digest or "").strip().casefold()
    if not _SHA256.fullmatch(digest):
        raise MCPSettingsAdapterError("MCP manifest digest is invalid")
    executable_digest = item.get("expected_executable_sha256") or item.get(
        "executable_sha256"
    )
    if not executable_digest:
        raise MCPSettingsAdapterError("MCP executable SHA-256 attestation is required")
    command = _string_list(item.get("command"), "command")
    argv = _string_list(item.get("argv"), "argv")
    policies = item.get("tool_policies")
    if policies is None:
        policies = item.get("tools")
    try:
        return MCPStdioSettings(
            id=runtime_id,
            extension_id=expected_extension_id,
            manifest_sha256=digest,
            transport=str(item.get("transport") or "stdio"),
            executable=_bounded_text(item.get("executable"), "MCP executable", maximum=1024),
            expected_executable_sha256=str(executable_digest).strip().casefold(),
            arguments=[*command, *argv],
            cwd=(str(item.get("cwd")).strip() if item.get("cwd") else None),
            environment=_environment(
                item.get("environment"),
                item.get("environment_keys") or [],
                item.get("secret_aliases") or {},
            ),
            tools=_tool_policies(policies),
            startup_timeout_seconds=float(item.get("startup_timeout_seconds") or 10),
            call_timeout_seconds=float(item.get("timeout_seconds") or 30),
            protocol_version=str(item.get("protocol_version") or "2025-06-18"),
        )
    except MCPSettingsAdapterError:
        raise
    except (ValidationError, TypeError, ValueError) as error:
        raise MCPSettingsAdapterError(
            f"MCP stdio settings are invalid for {settings_id}"
        ) from error


@dataclass
class _ActiveMCP:
    settings: MCPStdioSettings
    settings_sha256: str
    client: Any
    projects: frozenset[str]
    definitions: tuple[ToolDefinition, ...]


ClientFactory = Callable[[MCPStdioSettings], Any]
ProjectIDsProvider = Callable[[], Iterable[str]]


class MCPSettingsCoordinator:
    """Reconcile settings, effective extension state, processes and tools."""

    def __init__(
        self,
        *,
        extension_registry: Any,
        tool_registry: ToolRegistry,
        allowed_cwd_roots: Iterable[Path],
        project_ids_provider: Optional[ProjectIDsProvider] = None,
        secret_resolver: Optional[Callable[[str], str]] = None,
        client_factory: Optional[ClientFactory] = None,
    ) -> None:
        if extension_registry is None:
            raise TypeError("extension_registry is required")
        if not isinstance(tool_registry, ToolRegistry):
            raise TypeError("tool_registry must be ToolRegistry")
        roots = tuple(Path(root).expanduser() for root in allowed_cwd_roots)
        if not roots:
            raise ValueError("at least one allowed MCP cwd root is required")
        if any(not root.is_absolute() for root in roots):
            raise ValueError("coordinator MCP cwd roots must be absolute")
        self.extension_registry = extension_registry
        self.tool_registry = tool_registry
        self.allowed_cwd_roots = tuple(root.absolute() for root in roots)
        self.project_ids_provider = project_ids_provider or (lambda: ())
        self.secret_resolver = secret_resolver
        self.client_factory = client_factory
        self._active: dict[str, _ActiveMCP] = {}
        self._registered: dict[str, dict[str, str]] = {}
        self._health: dict[str, dict[str, Any]] = {}
        self._sync_lock = asyncio.Lock()
        self._state_lock = threading.RLock()

    def _create_client(
        self,
        settings: MCPStdioSettings,
        allowed_cwd_roots: tuple[Path, ...],
    ) -> Any:
        if self.client_factory is not None:
            return self.client_factory(settings)
        return MCPStdioClient(
            settings,
            allowed_cwd_roots=allowed_cwd_roots,
            secret_resolver=self.secret_resolver,
        )

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            return os.path.commonpath(
                [os.path.normcase(str(path)), os.path.normcase(str(root))]
            ) == os.path.normcase(str(root))
        except (OSError, ValueError):
            return False

    def _runtime_roots(self, item: Mapping[str, Any]) -> tuple[Path, ...]:
        raw_roots = item.get("allowed_cwd_roots") or self.allowed_cwd_roots
        if isinstance(raw_roots, (str, bytes)) or not isinstance(
            raw_roots, (list, tuple)
        ):
            raise MCPSettingsAdapterError("allowed_cwd_roots must be a list")
        roots: list[Path] = []
        for raw_root in raw_roots:
            root = Path(_bounded_text(raw_root, "allowed cwd root", maximum=1024)).expanduser()
            if not root.is_absolute():
                raise MCPSettingsAdapterError("allowed cwd roots must be absolute local paths")
            absolute = root.absolute()
            if not any(
                self._is_within(absolute, allowed.absolute())
                for allowed in self.allowed_cwd_roots
            ):
                raise MCPSettingsAdapterError(
                    "configured MCP cwd root exceeds the coordinator boundary"
                )
            if absolute not in roots:
                roots.append(absolute)
        if not roots:
            raise MCPSettingsAdapterError("at least one MCP cwd root is required")
        return tuple(roots)

    def _set_health(
        self,
        extension_id: str,
        status: str,
        *,
        projects: Iterable[str] = (),
        tool_count: int = 0,
        manifest_digest: str = "",
        error: Optional[BaseException] = None,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> None:
        with self._state_lock:
            record = {
                "extension_id": extension_id,
                "status": status,
                "running": bool(
                    extension_id in self._active
                    and getattr(self._active[extension_id].client, "running", False)
                ),
                "projects": sorted(set(projects)),
                "tool_count": max(0, int(tool_count)),
                "manifest_sha256": manifest_digest,
                "checked_at": _now_iso(),
                "detail": dict(redact(dict(detail or {}))),
            }
            if error is not None:
                record["error_code"] = str(
                    getattr(error, "code", "MCP_COORDINATOR_ERROR")
                )[:128]
                record["error_type"] = type(error).__name__
                record["error"] = str(redact(str(error)))[:1000]
            self._health[extension_id] = record

    @staticmethod
    async def _call(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    def _project_ids(self, explicit: Optional[Iterable[str]]) -> tuple[str, ...]:
        raw = explicit if explicit is not None else self.project_ids_provider()
        if isinstance(raw, (str, bytes)):
            raise MCPSettingsAdapterError("project IDs must be an iterable of identifiers")
        projects: set[str] = set()
        for value in raw or ():
            projects.add(_bounded_text(value, "project_id", maximum=128))
        return tuple(sorted(projects))

    def _is_available(self, extension_id: str, project_id: str) -> bool:
        with self._state_lock:
            active = self._active.get(extension_id)
        return bool(
            active is not None
            and getattr(active.client, "running", False)
            and project_id in active.projects
            and self.extension_registry.is_effectively_enabled(extension_id, project_id)
        )

    def _safe_unregister(self, project_id: str, tool_name: str, owner: str) -> None:
        try:
            current = self.tool_registry.get(project_id, tool_name)
        except Exception:
            return
        if current.extension_id == owner:
            self.tool_registry.unregister(tool_name, project_id=project_id)

    async def _stop_extension(
        self,
        extension_id: str,
        *,
        status: str,
        preserve_health: bool = False,
    ) -> None:
        with self._state_lock:
            active = self._active.pop(extension_id, None)
        for project, registrations in tuple(self._registered.items()):
            for tool_name, owner in tuple(registrations.items()):
                if owner == extension_id:
                    self._safe_unregister(project, tool_name, owner)
                    registrations.pop(tool_name, None)
            if not registrations:
                self._registered.pop(project, None)
        if active is not None:
            try:
                await self._call(active.client.stop())
            except Exception as error:
                self._set_health(
                    extension_id,
                    "error",
                    projects=active.projects,
                    manifest_digest=active.settings.manifest_sha256,
                    error=error,
                    detail={"action": "stop"},
                )
                return
            if not preserve_health:
                self._set_health(
                    extension_id,
                    status,
                    projects=(),
                    manifest_digest=active.settings.manifest_sha256,
                )
            else:
                with self._state_lock:
                    preserved = dict(self._health.get(extension_id, {}))
                    preserved.update(
                        {
                            "running": False,
                            "projects": [],
                            "tool_count": 0,
                            "checked_at": _now_iso(),
                        }
                    )
                    self._health[extension_id] = preserved

    @staticmethod
    def _settings_items(settings: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        if not isinstance(settings, Mapping):
            raise MCPSettingsAdapterError("settings must be an object")
        raw = settings.get("mcp_servers") or []
        if not isinstance(raw, (list, tuple)) or len(raw) > 64:
            raise MCPSettingsAdapterError("mcp_servers must be a bounded list")
        if any(not isinstance(item, Mapping) for item in raw):
            raise MCPSettingsAdapterError("every MCP server setting must be an object")
        return tuple(raw)

    async def sync_from_settings(
        self,
        settings: Mapping[str, Any],
        *,
        project_ids: Optional[Iterable[str]] = None,
    ) -> dict[str, Any]:
        """Reconcile all local MCP processes and project tool registrations."""

        async with self._sync_lock:
            projects = self._project_ids(project_ids)
            try:
                items = self._settings_items(settings)
            except Exception:
                await self._stop_all_unlocked(status="configuration_error")
                raise

            indexed: dict[str, Mapping[str, Any]] = {}
            invalid_ids: set[str] = set()
            for index, item in enumerate(items):
                try:
                    settings_id = _bounded_text(item.get("id"), "MCP settings ID", maximum=96)
                    extension_id = f"mcp.{safe_settings_identifier(settings_id)}"
                except Exception as error:
                    self._set_health(f"settings[{index}]", "error", error=error)
                    continue
                if extension_id in indexed:
                    invalid_ids.add(extension_id)
                    self._set_health(
                        extension_id,
                        "error",
                        error=MCPSettingsAdapterError("duplicate MCP settings ID"),
                    )
                    continue
                indexed[extension_id] = item
            for extension_id in invalid_ids:
                indexed.pop(extension_id, None)

            expected_manifests: dict[str, Any] = {}
            for extension_id, item in indexed.items():
                try:
                    manifests = settings_manifests({"mcp_servers": [item]})
                    expected = next(
                        manifest for manifest in manifests if manifest.id == extension_id
                    )
                    expected_manifests[extension_id] = expected
                except Exception as error:
                    self._set_health(extension_id, "error", error=error)

            desired: dict[
                str,
                tuple[MCPStdioSettings, frozenset[str], tuple[Path, ...]],
            ] = {}
            for extension_id, item in indexed.items():
                expected = expected_manifests.get(extension_id)
                if expected is None:
                    continue
                try:
                    registry_item = self.extension_registry.get(
                        extension_id, None, synchronize=False
                    )
                    current_digest = str(registry_item.get("manifest_sha256") or "")
                    entrypoint = registry_item.get("entrypoint") or {}
                    expected_entrypoint = expected.entrypoint.model_dump(
                        mode="json",
                        exclude_none=True,
                    )
                    if (
                        entrypoint.get("type") != expected_entrypoint.get("type")
                        or entrypoint.get("adapter")
                        != expected_entrypoint.get("adapter")
                        or str(entrypoint.get("settings_id") or "")
                        != str(expected_entrypoint.get("settings_id") or "")
                        or entrypoint.get("configuration_sha256")
                        != expected_entrypoint.get("configuration_sha256")
                    ):
                        raise MCPManifestMismatch(
                            "MCP settings no longer match the trusted extension manifest"
                        )
                    runtime_settings = mcp_stdio_settings_from_mapping(
                        item,
                        extension_id=extension_id,
                        manifest_digest=current_digest,
                    )
                    runtime_roots = self._runtime_roots(item)
                    enabled_projects = frozenset(
                        project
                        for project in projects
                        if self.extension_registry.is_effectively_enabled(
                            extension_id, project
                        )
                    )
                    globally_enabled = self.extension_registry.is_effectively_enabled(
                        extension_id, None
                    )
                    should_run = bool(enabled_projects) if projects else globally_enabled
                    if should_run:
                        desired[extension_id] = (
                            runtime_settings,
                            enabled_projects,
                            runtime_roots,
                        )
                    else:
                        self._set_health(
                            extension_id,
                            "disabled",
                            projects=(),
                            manifest_digest=current_digest,
                        )
                except Exception as error:
                    self._set_health(extension_id, "error", error=error)

            with self._state_lock:
                active_extension_ids = tuple(self._active)
            for extension_id in active_extension_ids:
                if extension_id not in desired:
                    with self._state_lock:
                        preserve_health = (
                            self._health.get(extension_id, {}).get("status") == "error"
                        )
                    await self._stop_extension(
                        extension_id,
                        status="disabled",
                        preserve_health=preserve_health,
                    )

            for extension_id, (
                runtime_settings,
                enabled_projects,
                runtime_roots,
            ) in desired.items():
                fingerprint = mcp_settings_sha256(runtime_settings)
                with self._state_lock:
                    current = self._active.get(extension_id)
                if current is not None and (
                    current.settings_sha256 != fingerprint
                    or not getattr(current.client, "running", False)
                ):
                    await self._stop_extension(extension_id, status="restarting")
                    current = None
                if current is None:
                    client = None
                    try:
                        client = self._create_client(runtime_settings, runtime_roots)
                        await self._call(client.start())
                        definitions = tuple(client.tool_definitions())
                        if any(
                            not isinstance(definition, ToolDefinition)
                            or definition.extension_id != extension_id
                            or definition.manifest_sha256 != runtime_settings.manifest_sha256
                            for definition in definitions
                        ):
                            raise MCPRegistrationError(
                                "MCP discovery returned an invalid tool definition"
                            )
                        current = _ActiveMCP(
                            settings=runtime_settings,
                            settings_sha256=fingerprint,
                            client=client,
                            projects=enabled_projects,
                            definitions=definitions,
                        )
                        with self._state_lock:
                            self._active[extension_id] = current
                    except Exception as error:
                        if client is not None:
                            try:
                                await self._call(client.stop())
                            except Exception:
                                pass
                        with self._state_lock:
                            self._active.pop(extension_id, None)
                        self._set_health(
                            extension_id,
                            "error",
                            manifest_digest=runtime_settings.manifest_sha256,
                            error=error,
                            detail={"action": "start_or_discover"},
                        )
                        continue
                else:
                    current.projects = enabled_projects

                try:
                    governed_definitions = tuple(
                        replace(
                            definition,
                            availability=(
                                lambda project_id, _extension_id=extension_id: self._is_available(
                                    _extension_id, project_id
                                )
                            ),
                        )
                        for definition in current.definitions
                    )
                    for project in enabled_projects:
                        for definition in governed_definitions:
                            try:
                                existing = self.tool_registry.get(project, definition.name)
                            except Exception:
                                existing = None
                            if (
                                existing is not None
                                and existing.extension_id != extension_id
                            ):
                                raise MCPRegistrationError(
                                    f"MCP tool conflicts with another owner: {definition.name}"
                                )
                    for project in enabled_projects:
                        for definition in governed_definitions:
                            self.tool_registry.register(
                                definition,
                                project_ids=(project,),
                                replace_existing=True,
                            )
                    current.definitions = governed_definitions
                    self._set_health(
                        extension_id,
                        "healthy",
                        projects=enabled_projects,
                        tool_count=len(governed_definitions),
                        manifest_digest=runtime_settings.manifest_sha256,
                    )
                except Exception as error:
                    for project in enabled_projects:
                        for definition in governed_definitions:
                            self._safe_unregister(
                                project, definition.name, extension_id
                            )
                    await self._stop_extension(extension_id, status="error")
                    self._set_health(
                        extension_id,
                        "error",
                        manifest_digest=runtime_settings.manifest_sha256,
                        error=error,
                        detail={"action": "register_tools"},
                    )

            desired_registrations: dict[str, dict[str, str]] = {}
            with self._state_lock:
                active_snapshot = tuple(self._active.items())
            for extension_id, active in active_snapshot:
                for project in active.projects:
                    project_tools = desired_registrations.setdefault(project, {})
                    for definition in active.definitions:
                        project_tools[definition.name] = extension_id
            for project, registrations in tuple(self._registered.items()):
                desired_project = desired_registrations.get(project, {})
                for tool_name, owner in tuple(registrations.items()):
                    if desired_project.get(tool_name) != owner:
                        self._safe_unregister(project, tool_name, owner)
            self._registered = desired_registrations

            health = self.health()
            return {
                "status": health["status"],
                "running": health["running"],
                "configured": len(indexed),
                "extensions": health["extensions"],
            }

    async def _stop_all_unlocked(self, *, status: str) -> None:
        with self._state_lock:
            extension_ids = tuple(self._active)
        for extension_id in extension_ids:
            await self._stop_extension(extension_id, status=status)
        for project, registrations in tuple(self._registered.items()):
            for tool_name, owner in tuple(registrations.items()):
                self._safe_unregister(project, tool_name, owner)
        self._registered.clear()

    async def stop_all(self) -> None:
        async with self._sync_lock:
            await self._stop_all_unlocked(status="stopped")

    def health(self, extension_id: Optional[str] = None) -> dict[str, Any]:
        with self._state_lock:
            records = {key: dict(value) for key, value in self._health.items()}
            active_snapshot = tuple(self._active.items())
        for key, active in active_snapshot:
            record = records.setdefault(key, {})
            running = bool(getattr(active.client, "running", False))
            record.update(
                {
                    "extension_id": key,
                    "running": running,
                    "status": "healthy" if running else "error",
                    "projects": sorted(active.projects),
                    "tool_count": len(active.definitions),
                    "manifest_sha256": active.settings.manifest_sha256,
                }
            )
            if not running:
                record.update(
                    {
                        "error_code": "MCP_PROCESS_UNAVAILABLE",
                        "error": "MCP process is not running",
                    }
                )
        if extension_id is not None:
            normalized = str(extension_id or "").strip().casefold()
            return records.get(
                normalized,
                {
                    "extension_id": normalized,
                    "status": "unknown",
                    "running": False,
                    "projects": [],
                    "tool_count": 0,
                    "detail": {},
                },
            )
        statuses = [record.get("status") for record in records.values()]
        status = "healthy"
        if any(item == "error" for item in statuses):
            status = "degraded"
        elif not any(record.get("running") for record in records.values()):
            status = "stopped"
        return {
            "status": status,
            "running": sum(1 for record in records.values() if record.get("running")),
            "extensions": records,
        }


__all__ = [
    "MCPCoordinatorError",
    "MCPManifestMismatch",
    "MCPRegistrationError",
    "MCPSettingsAdapterError",
    "MCPSettingsCoordinator",
    "mcp_stdio_settings_from_mapping",
]
