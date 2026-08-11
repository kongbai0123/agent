"""Strict V1 manifests for built-in and explicitly trusted local extensions.

The manifest is descriptive.  It can select one of the adapters compiled into
Workbench, but it cannot import Python, invoke a shell, install a package, or
name a remote marketplace.  Runtime configuration (MCP argv, provider URL and
credentials) remains in the existing settings/secret stores.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


MANIFEST_SCHEMA_VERSION = 1
EXTENSION_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
ADAPTER_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
ENTRYPOINT_ADAPTERS = {
    "mcp_settings": "mcp",
    "provider_settings": "model_provider",
}
BUILTIN_CONTRACTS = {
    "n8n": ("integration", "n8n", "network.n8n"),
    "cursor": ("integration", "cursor", "workspace.cursor"),
    "excel": ("desktop", "excel", "desktop.excel"),
    "ollama": ("model_provider", "ollama", "network.ollama"),
}
SETTINGS_CONTRACTS = {
    "mcp_settings": ("mcp", "mcp", "process.mcp"),
    "provider_settings": (
        "model_provider",
        "model_provider",
        "network.model_provider",
    ),
}
REQUIRED_PERMISSION_RISKS = {
    "network.n8n": "external_write",
    "workspace.cursor": "write",
    "desktop.excel": "irreversible",
    "network.ollama": "external_read",
    "process.mcp": "system",
    "network.model_provider": "external_write",
}
RiskLevel = Literal[
    "read",
    "external_read",
    "verify",
    "write",
    "external_write",
    "system",
    "irreversible",
]


class ExtensionPermission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=EXTENSION_ID_PATTERN, max_length=96)
    risk: RiskLevel
    description: str = Field(min_length=1, max_length=300)
    required: bool = True


class ExtensionEntrypoint(BaseModel):
    """A reference to reviewed Workbench code, never executable source text."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["builtin", "mcp_settings", "provider_settings"]
    adapter: str = Field(pattern=ADAPTER_ID_PATTERN, max_length=64)
    settings_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    configuration_sha256: Optional[str] = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )

    @model_validator(mode="after")
    def validate_adapter_reference(self) -> "ExtensionEntrypoint":
        if self.type == "builtin":
            if self.settings_id is not None or self.configuration_sha256 is not None:
                raise ValueError("builtin entrypoints cannot reference settings")
            if self.adapter not in BUILTIN_CONTRACTS:
                raise ValueError("builtin entrypoint adapter is not compiled into Workbench")
        else:
            if self.adapter != ENTRYPOINT_ADAPTERS[self.type]:
                raise ValueError(f"{self.type} requires adapter={ENTRYPOINT_ADAPTERS[self.type]}")
            if not self.settings_id or not self.configuration_sha256:
                raise ValueError(
                    f"{self.type} entrypoints require settings_id and configuration_sha256"
                )
        return self


class ExtensionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = MANIFEST_SCHEMA_VERSION
    id: str = Field(pattern=EXTENSION_ID_PATTERN, max_length=96)
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=500)
    publisher: str = Field(min_length=1, max_length=100)
    origin: Literal["builtin", "local"]
    kind: Literal["integration", "mcp", "desktop", "model_provider"]
    category: str = Field(default="other", pattern=ADAPTER_ID_PATTERN, max_length=64)
    entrypoint: ExtensionEntrypoint
    permissions: list[ExtensionPermission] = Field(default_factory=list, max_length=32)
    health_probe: Literal[
        "n8n",
        "cursor",
        "excel",
        "ollama",
        "mcp",
        "model_provider",
        "static",
    ] = "static"
    removable: bool = False
    default_installed: bool = True
    default_enabled: bool = False

    @model_validator(mode="after")
    def validate_manifest_contract(self) -> "ExtensionManifest":
        permission_ids = [permission.id for permission in self.permissions]
        if len(permission_ids) != len(set(permission_ids)):
            raise ValueError("permission IDs must be unique")
        if not self.permissions or any(not permission.required for permission in self.permissions):
            raise ValueError("V1 extensions require at least one non-optional permission")
        if self.origin == "builtin":
            if self.entrypoint.type != "builtin":
                raise ValueError("builtin extensions require a builtin entrypoint")
            if self.removable:
                raise ValueError("builtin extensions cannot be removable")
        elif self.entrypoint.type == "builtin":
            raise ValueError("local extensions cannot claim a builtin entrypoint")
        contract = (
            BUILTIN_CONTRACTS[self.entrypoint.adapter]
            if self.entrypoint.type == "builtin"
            else SETTINGS_CONTRACTS[self.entrypoint.type]
        )
        expected_kind, expected_probe, required_permission = contract
        if self.kind != expected_kind or self.health_probe != expected_probe:
            raise ValueError(
                f"{self.entrypoint.adapter} requires kind={expected_kind} "
                f"and health_probe={expected_probe}"
            )
        permission_risks = {
            permission.id: permission.risk for permission in self.permissions
        }
        if (
            permission_risks.get(required_permission)
            != REQUIRED_PERMISSION_RISKS[required_permission]
        ):
            raise ValueError(
                f"{self.entrypoint.adapter} requires permission={required_permission} "
                f"at risk={REQUIRED_PERMISSION_RISKS[required_permission]}"
            )
        if not self.default_installed and self.default_enabled:
            raise ValueError("an uninstalled extension cannot be enabled")
        return self


def parse_extension_manifest(payload: object) -> ExtensionManifest:
    """Validate an object without accepting aliases or legacy loose fields."""
    return ExtensionManifest.model_validate(payload)


def canonical_manifest_bytes(manifest: ExtensionManifest) -> bytes:
    payload = manifest.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def manifest_sha256(manifest: ExtensionManifest) -> str:
    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


def safe_settings_identifier(value: object) -> str:
    """Normalize an existing settings ID for a namespaced extension ID."""
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().casefold())
    normalized = normalized.strip("-_")[:64]
    if not normalized:
        raise ValueError("settings extension IDs cannot be empty")
    if not normalized[0].isalpha():
        normalized = f"id-{normalized}"[:64]
    return normalized
