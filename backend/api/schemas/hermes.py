"""Request bodies for the local Workbench Hermes control surface."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HermesApprovalDecisionRequest(BaseModel):
    rationale: str = Field(default="", max_length=1000)


class HermesChatApprovalDecisionRequest(BaseModel):
    approval_id: str = Field(min_length=1, max_length=512)
    approved: bool
    decided_by: str = Field(default="local_user", max_length=128)


__all__ = [
    "HermesApprovalDecisionRequest",
    "HermesChatApprovalDecisionRequest",
]
