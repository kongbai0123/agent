"""Trusted catalog records for the local Extension Center.

Manifest V1 remains the only format accepted from local files.  GitHub and
Notion are intentionally described by a separate, server-owned connector
contract: adding connectors must not make the executable Manifest V1 surface
more permissive.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Mapping, Union

from pydantic import BaseModel, ConfigDict, Field

from extension_manifest import (
    EXTENSION_ID_PATTERN,
    ExtensionManifest,
    ExtensionPermission,
    canonical_manifest_bytes,
    parse_extension_manifest,
    safe_settings_identifier,
)


class ConnectorEntrypoint(BaseModel):
    """Reference to one connector adapter compiled into Workbench."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["connector"] = "connector"
    adapter: Literal["github", "notion"]


class ConnectorExtensionDescriptor(BaseModel):
    """Server-owned descriptor kept deliberately separate from Manifest V1."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["connector-v1"] = "connector-v1"
    id: str = Field(pattern=EXTENSION_ID_PATTERN, max_length=96)
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=500)
    publisher: str = Field(min_length=1, max_length=100)
    origin: Literal["builtin"] = "builtin"
    kind: Literal["connector"] = "connector"
    category: str = Field(min_length=1, max_length=64)
    entrypoint: ConnectorEntrypoint
    permissions: list[ExtensionPermission] = Field(min_length=1, max_length=32)
    health_probe: Literal["github", "notion"]
    removable: bool = True
    default_installed: bool = False
    default_enabled: bool = False


CatalogRecord = Union[ExtensionManifest, ConnectorExtensionDescriptor]


def _permission(
    permission_id: str,
    risk: str,
    description: str,
) -> dict[str, Any]:
    return {
        "id": permission_id,
        "risk": risk,
        "description": description,
        "required": True,
    }


def _configuration_sha256(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=repr,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def catalog_record_payload(record: CatalogRecord) -> dict[str, Any]:
    return record.model_dump(mode="json", exclude_none=True)


def canonical_catalog_record_bytes(record: CatalogRecord) -> bytes:
    if isinstance(record, ExtensionManifest):
        return canonical_manifest_bytes(record)
    return json.dumps(
        catalog_record_payload(record),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def catalog_record_sha256(record: CatalogRecord) -> str:
    return hashlib.sha256(canonical_catalog_record_bytes(record)).hexdigest()


def catalog_record_contract(record: CatalogRecord) -> str:
    return "manifest-v1" if isinstance(record, ExtensionManifest) else "connector-v1"


def builtin_manifests() -> tuple[ExtensionManifest, ...]:
    common = {
        "schema_version": 1,
        "version": "1.0.0",
        "publisher": "Local AI Workbench",
        "origin": "builtin",
        "default_installed": True,
        "default_enabled": True,
        "removable": False,
    }
    definitions = (
        {
            **common,
            "id": "builtin.n8n",
            "name": "n8n",
            "description": "Local workflow automation with governed Gmail drafts.",
            "kind": "integration",
            "category": "automation",
            "entrypoint": {"type": "builtin", "adapter": "n8n"},
            "permissions": [
                _permission(
                    "network.n8n",
                    "external_write",
                    "Call the configured managed n8n service.",
                ),
            ],
            "health_probe": "n8n",
        },
        {
            **common,
            "id": "builtin.cursor",
            "name": "Cursor Agent",
            "description": "Cursor adapter is not available in this release.",
            "kind": "integration",
            "category": "development",
            "entrypoint": {"type": "builtin", "adapter": "cursor"},
            "permissions": [
                _permission(
                    "workspace.cursor",
                    "write",
                    "Read or modify the selected project.",
                ),
            ],
            "health_probe": "cursor",
            "default_installed": False,
            "default_enabled": False,
        },
        {
            **common,
            "id": "builtin.excel",
            "name": "Microsoft Excel",
            "description": "Excel adapter is not available in this release.",
            "kind": "desktop",
            "category": "productivity",
            "entrypoint": {"type": "builtin", "adapter": "excel"},
            "permissions": [
                _permission(
                    "desktop.excel",
                    "irreversible",
                    "Read, modify, or save a bound workbook.",
                ),
            ],
            "health_probe": "excel",
            "default_installed": False,
            "default_enabled": False,
        },
        {
            **common,
            "id": "builtin.ollama",
            "name": "Ollama",
            "description": "Use the configured local Ollama model service.",
            "kind": "model_provider",
            "category": "models",
            "entrypoint": {"type": "builtin", "adapter": "ollama"},
            "permissions": [
                _permission(
                    "network.ollama",
                    "external_read",
                    "Call the configured local Ollama API.",
                ),
            ],
            "health_probe": "ollama",
        },
    )
    return tuple(parse_extension_manifest(item) for item in definitions)


def builtin_connector_descriptors() -> tuple[ConnectorExtensionDescriptor, ...]:
    definitions = (
        {
            "id": "connector.github",
            "name": "GitHub",
            "version": "1.0.0",
            "description": (
                "Read repositories, issues, pull requests, and checks; approved "
                "writes can create or update issues and add conversation comments."
            ),
            "publisher": "Local AI Workbench",
            "category": "development",
            "entrypoint": {"adapter": "github"},
            "permissions": [
                _permission(
                    "connector.github.repository.read",
                    "external_read",
                    "Read project-bound repository content and collaboration metadata.",
                ),
                _permission(
                    "connector.github.issue.write",
                    "external_write",
                    "Create or update issues and add approved conversation comments.",
                ),
            ],
            "health_probe": "github",
        },
        {
            "id": "connector.notion",
            "name": "Notion",
            "version": "1.0.0",
            "description": (
                "Read project-bound Notion roots; approved writes can create or "
                "update pages and append blocks."
            ),
            "publisher": "Local AI Workbench",
            "category": "productivity",
            "entrypoint": {"adapter": "notion"},
            "permissions": [
                _permission(
                    "connector.notion.content.read",
                    "external_read",
                    "Read selected page and database roots and their descendants.",
                ),
                _permission(
                    "connector.notion.content.write",
                    "external_write",
                    "Create or update content after per-operation approval.",
                ),
            ],
            "health_probe": "notion",
        },
    )
    return tuple(ConnectorExtensionDescriptor.model_validate(item) for item in definitions)


def builtin_catalog_records() -> tuple[CatalogRecord, ...]:
    return (*builtin_manifests(), *builtin_connector_descriptors())


_BUILTIN_METADATA: dict[str, dict[str, Any]] = {
    "builtin.n8n": {
        "runtime_available": True,
        "connection_required": False,
        "capabilities": ["workflow_automation", "gmail_governed_drafts"],
    },
    "builtin.cursor": {
        "runtime_available": False,
        "availability_reason": "cursor_adapter_not_implemented",
        "connection_required": False,
        "capabilities": [],
    },
    "builtin.excel": {
        "runtime_available": False,
        "availability_reason": "excel_adapter_not_implemented",
        "connection_required": False,
        "capabilities": [],
    },
    "builtin.ollama": {
        "runtime_available": True,
        "connection_required": False,
        "capabilities": ["local_models"],
    },
    "connector.github": {
        "runtime_available": True,
        "connection_required": True,
        "connector_id": "github",
        "capabilities": ["repositories", "issues", "pull_requests", "checks"],
    },
    "connector.notion": {
        "runtime_available": True,
        "connection_required": True,
        "connector_id": "notion",
        "capabilities": ["pages", "databases", "blocks"],
    },
}


def catalog_metadata(extension_id: str) -> dict[str, Any]:
    """Return UI/runtime metadata that is not part of an executable manifest."""

    return {
        "runtime_available": True,
        "availability_reason": None,
        "connection_required": False,
        "capabilities": [],
        **_BUILTIN_METADATA.get(extension_id, {}),
    }


def mcp_configuration_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical, non-secret MCP settings covered by Manifest V1 trust.

    Older settings only contained the original fields below, so optional
    security fields are included only when configured.  Adding an executable
    attestation, environment reference, protocol version or Tool Policy then
    changes the manifest digest and requires an explicit re-trust.
    """

    command = item.get("command") or []
    if isinstance(command, str):
        command = [command]
    environment = item.get("environment") or {}
    environment_keys = set(str(key) for key in (item.get("environment_keys") or []))
    if isinstance(environment, Mapping):
        environment_keys.update(str(key) for key in environment)
    payload: dict[str, Any] = {
        "transport": str(item.get("transport") or "stdio"),
        "executable": str(item.get("executable") or ""),
        "command": [str(part) for part in command],
        "argv": [str(part) for part in item.get("argv") or []],
        "cwd": str(item.get("cwd") or ""),
        "allowed_cwd_roots": [
            str(path) for path in item.get("allowed_cwd_roots") or []
        ],
        "environment_keys": sorted(environment_keys),
        "secret_aliases": dict(item.get("secret_aliases") or {}),
        "timeout_seconds": float(item.get("timeout_seconds") or 30),
    }
    executable_digest = item.get("expected_executable_sha256") or item.get(
        "executable_sha256"
    )
    if executable_digest is not None:
        payload["expected_executable_sha256"] = str(executable_digest)
    if "startup_timeout_seconds" in item:
        payload["startup_timeout_seconds"] = float(item["startup_timeout_seconds"])
    if "protocol_version" in item:
        payload["protocol_version"] = str(item["protocol_version"])
    if isinstance(environment, Mapping) and environment:
        payload["environment"] = {
            str(key): dict(value) if isinstance(value, Mapping) else value
            for key, value in sorted(environment.items(), key=lambda pair: str(pair[0]))
        }
    policies = item.get("tool_policies")
    if policies is None:
        policies = item.get("tools")
    if isinstance(policies, Mapping) and policies:
        payload["tool_policies"] = {
            str(key): dict(value) if isinstance(value, Mapping) else value
            for key, value in sorted(policies.items(), key=lambda pair: str(pair[0]))
        }
    return payload


def settings_manifests(settings: Mapping[str, Any]) -> list[ExtensionManifest]:
    manifests: list[ExtensionManifest] = []
    for item in settings.get("mcp_servers") or []:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        settings_id = str(item["id"]).strip()
        safe_id = safe_settings_identifier(settings_id)
        config_digest = _configuration_sha256(mcp_configuration_payload(item))
        manifests.append(
            parse_extension_manifest(
                {
                    "schema_version": 1,
                    "id": f"mcp.{safe_id}",
                    "name": str(item.get("label") or settings_id)[:80],
                    "version": "settings-v1",
                    "description": "Trusted local MCP server configured in Workbench settings.",
                    "publisher": "Local configuration",
                    "origin": "local",
                    "kind": "mcp",
                    "category": "tools",
                    "entrypoint": {
                        "type": "mcp_settings",
                        "adapter": "mcp",
                        "settings_id": settings_id,
                        "configuration_sha256": config_digest,
                    },
                    "permissions": [
                        _permission(
                            "process.mcp",
                            "system",
                            "Start the configured MCP process and call its tools.",
                        ),
                    ],
                    "health_probe": "mcp",
                    "removable": True,
                    "default_installed": True,
                    "default_enabled": False,
                }
            )
        )
    manifests.extend(_provider_manifests(settings))
    return manifests


def _provider_manifests(settings: Mapping[str, Any]) -> list[ExtensionManifest]:
    manifests: list[ExtensionManifest] = []
    provider_items = list(settings.get("model_providers") or [])
    if (
        not provider_items
        and str(settings.get("model_provider") or "ollama").casefold()
        == "openai_compatible"
    ):
        provider_items.append(
            {
                "id": "openai_compatible",
                "label": "OpenAI-compatible provider",
                "base_url": settings.get("openai_compatible_url"),
                "input_cost_per_million": settings.get("model_input_cost_per_million"),
                "output_cost_per_million": settings.get("model_output_cost_per_million"),
                "currency": settings.get("model_cost_currency"),
                "api_key_env": settings.get("openai_api_key_env"),
                "enabled": True,
            }
        )
    for item in provider_items:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        settings_id = str(item["id"]).strip().casefold()
        safe_id = safe_settings_identifier(settings_id)
        config_digest = _configuration_sha256(
            {
                "id": settings_id,
                "provider_type": str(item.get("provider_type") or "openai_compatible"),
                "base_url": str(item.get("base_url") or "").rstrip("/"),
                "selected_model": str(item.get("selected_model") or ""),
                "model_kind": str(item.get("model_kind") or "unknown"),
                "language_pair": str(item.get("language_pair") or ""),
                "supports_tools": bool(item.get("supports_tools", False)),
                "tool_attestation": dict(item.get("tool_attestation") or {}),
                "api_key_env": str(item.get("api_key_env") or ""),
            }
        )
        manifests.append(
            parse_extension_manifest(
                {
                    "schema_version": 1,
                    "id": f"provider.{safe_id}",
                    "name": str(item.get("label") or settings_id)[:80],
                    "version": "settings-v1",
                    "description": "Imported model API configured in Workbench settings.",
                    "publisher": "Local configuration",
                    "origin": "local",
                    "kind": "model_provider",
                    "category": "models",
                    "entrypoint": {
                        "type": "provider_settings",
                        "adapter": "model_provider",
                        "settings_id": settings_id,
                        "configuration_sha256": config_digest,
                    },
                    "permissions": [
                        _permission(
                            "network.model_provider",
                            "external_write",
                            "Send prompts to the configured provider.",
                        ),
                    ],
                    "health_probe": "model_provider",
                    "removable": True,
                    "default_installed": True,
                    "default_enabled": False,
                }
            )
        )
    return manifests


def enabled_settings_extension_ids(settings: Mapping[str, Any]) -> set[str]:
    """Identify pre-platform active settings for one-time compatible import."""

    result: set[str] = set()
    for item in settings.get("mcp_servers") or []:
        if isinstance(item, Mapping) and item.get("id") and item.get("enabled") is True:
            result.add(f"mcp.{safe_settings_identifier(item['id'])}")
    for item in settings.get("model_providers") or []:
        if isinstance(item, Mapping) and item.get("id") and item.get("enabled") is True:
            result.add(f"provider.{safe_settings_identifier(str(item['id']).casefold())}")
    if (
        not settings.get("model_providers")
        and str(settings.get("model_provider") or "ollama").casefold()
        == "openai_compatible"
    ):
        result.add("provider.openai_compatible")
    return result
