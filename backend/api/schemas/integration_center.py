"""Strict request contracts for the unified Integration Center."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


PermissionMode = Literal["blocked", "restricted", "open"]


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class IntegrationResourceScope(_StrictRequest):
    resource_type: str = Field(min_length=1, max_length=64)
    resource_id: str = Field(min_length=1, max_length=1024)


class IntegrationGrantRequest(_StrictRequest):
    integration_id: str = Field(min_length=1, max_length=96)
    connection_id: Optional[str] = Field(default=None, min_length=1, max_length=512)
    capabilities: list[str] = Field(default_factory=list, max_length=128)
    resources: list[IntegrationResourceScope] = Field(default_factory=list, max_length=500)


class IntegrationPolicyReplaceRequest(_StrictRequest):
    revision: int = Field(ge=0)
    name: str = Field(default="Project 整合權限", min_length=1, max_length=160)
    permission_mode: PermissionMode = "blocked"
    grants: list[IntegrationGrantRequest] = Field(default_factory=list, max_length=100)
    acknowledge_open_risk: bool = False

    @model_validator(mode="after")
    def validate_open_acknowledgement(self) -> "IntegrationPolicyReplaceRequest":
        if self.permission_mode == "open" and not self.acknowledge_open_risk:
            raise ValueError("open permission requires explicit risk acknowledgement")
        return self


__all__ = [
    "IntegrationGrantRequest",
    "IntegrationPolicyReplaceRequest",
    "IntegrationResourceScope",
    "PermissionMode",
]
