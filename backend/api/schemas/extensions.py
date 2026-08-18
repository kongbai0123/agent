"""Strict request contracts for the local Extension Center API."""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalExtensionInspectRequest(_StrictRequest):
    filename: str = Field(min_length=1, max_length=200)


class ExtensionInstallRequest(_StrictRequest):
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExtensionTrustRequest(_StrictRequest):
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExtensionGlobalStateRequest(_StrictRequest):
    global_enabled: bool
    manifest_sha256: Optional[str] = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )


class ProjectExtensionStateRequest(_StrictRequest):
    mode: Literal["inherit", "enabled", "disabled"]
    manifest_sha256: Optional[str] = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
