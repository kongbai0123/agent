"""Strict request contracts for the local Extension Center API."""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class ProjectExtensionPermissionRequest(_StrictRequest):
    level: Literal["blocked", "restricted", "open"]
    revision: int = Field(ge=0)
    acknowledge_risk: bool = False

    @model_validator(mode="after")
    def require_open_risk_acknowledgement(self):
        if self.level == "open" and self.acknowledge_risk is not True:
            raise ValueError("open permission requires explicit risk acknowledgement")
        return self
