"""Strict request contracts for local connector administration."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ConnectorAuthProfileUpdate(_StrictModel):
    client_id: str = Field(min_length=1, max_length=512)
    # An omitted/blank secret means "preserve the existing encrypted value".
    # The service still requires a non-empty secret for first-time setup.
    client_secret: Optional[SecretStr] = None
    callback_uri: str = Field(min_length=1, max_length=2048)


class ConnectorOAuthStart(_StrictModel):
    connection_id: Optional[str] = Field(default=None, min_length=1, max_length=512)


class ProjectConnectionUpdate(_StrictModel):
    enabled: bool
    mode: Literal["read_only", "read_write"] = "read_write"


class ConnectorResourceSelection(_StrictModel):
    resource_type: str = Field(min_length=1, max_length=64)
    resource_id: str = Field(min_length=1, max_length=1024)
    parent_id: Optional[str] = Field(default=None, max_length=1024)
    display_label: str = Field(min_length=1, max_length=1024)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConnectorResourceBindingsReplace(_StrictModel):
    revision: int = Field(ge=0)
    resources: list[ConnectorResourceSelection] = Field(max_length=500)


__all__ = [
    "ConnectorAuthProfileUpdate",
    "ConnectorOAuthStart",
    "ConnectorResourceBindingsReplace",
    "ConnectorResourceSelection",
    "ProjectConnectionUpdate",
]
