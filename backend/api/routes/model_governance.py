"""Local-only model governance and routing APIs."""

from __future__ import annotations

from typing import Any, Callable, Dict, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from model_governance import GovernanceError, ModelGovernanceService


class CredentialMetadataRequest(BaseModel):
    expires_at: Optional[str] = Field(default=None, max_length=64)
    expiry_source: Literal["user_declared", "provider_verified", "unknown"] = "unknown"
    never_expires: bool = False


class BudgetPolicyRequest(BaseModel):
    revision: int = Field(ge=0)
    timezone: str = Field(default="Asia/Taipei", min_length=1, max_length=64)
    policy: Dict[str, Any] = Field(default_factory=dict)


class RoutingPolicyRequest(BaseModel):
    revision: int = Field(ge=0)
    mode: Literal["off", "ask", "auto_within_policy"] = "ask"
    allowed_providers: list[str] = Field(default_factory=list, max_length=32)
    data_consent: Dict[str, bool] = Field(default_factory=dict)
    preferred_models: list[str] = Field(default_factory=list, max_length=64)


class RouteResolveRequest(BaseModel):
    project_id: Optional[str] = Field(default=None, max_length=128)
    run_id: Optional[str] = Field(default=None, max_length=128)
    requested_model: str = Field(min_length=1, max_length=240)
    requirements: Dict[str, Any] = Field(default_factory=dict)


class RouteApprovalRequest(BaseModel):
    remember_project: bool = False


class BudgetOverrideRequest(BaseModel):
    project_id: Optional[str] = Field(default=None, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)


def build_model_governance_router(
    *,
    service: ModelGovernanceService,
    load_settings: Callable[[], Dict[str, Any]],
    model_inventory: Callable[[], list[Dict[str, Any]]],
    require_local: Optional[Callable[[Request], None]],
    require_project: Callable[[str], Any],
    error_payload: Callable[..., Dict[str, Any]],
) -> APIRouter:
    router = APIRouter(tags=["model-governance"])

    def local(request: Request) -> None:
        if require_local is not None:
            require_local(request)

    def fail(exc: GovernanceError) -> HTTPException:
        return HTTPException(
            status_code=exc.status_code,
            detail=error_payload(
                exc.code,
                str(exc),
                exc.details,
                recoverable=exc.status_code < 500,
            ),
        )

    def project(project_id: Optional[str]) -> Optional[str]:
        if not project_id:
            return None
        if require_project(project_id) is None:
            raise HTTPException(
                status_code=404,
                detail=error_payload("PROJECT_NOT_FOUND", "Project was not found.", recoverable=False),
            )
        return project_id

    @router.get("/api/model-governance/overview")
    def overview(request: Request, project_id: Optional[str] = None):
        local(request)
        try:
            selected_project = project(project_id)
            return {
                "success": True,
                **service.overview(
                    project_id=selected_project,
                    providers=load_settings().get("model_providers") or [],
                ),
            }
        except GovernanceError as exc:
            raise fail(exc) from exc

    @router.put("/api/model-governance/providers/{provider_id}/credential-metadata")
    def credential_metadata(provider_id: str, body: CredentialMetadataRequest, request: Request):
        local(request)
        try:
            configured = {
                str(item.get("id") or "").casefold()
                for item in load_settings().get("model_providers") or []
                if isinstance(item, dict)
            }
            if provider_id.casefold() not in configured:
                raise GovernanceError("MODEL_PROVIDER_NOT_FOUND", "Model provider is not configured.", status_code=404)
            return {
                "success": True,
                "credential": service.set_credential_metadata(
                    provider_id,
                    expires_at=body.expires_at,
                    expiry_source=body.expiry_source,
                    never_expires=body.never_expires,
                ),
            }
        except (GovernanceError, ValueError) as exc:
            if isinstance(exc, GovernanceError):
                raise fail(exc) from exc
            raise HTTPException(status_code=422, detail=error_payload("INVALID_PROVIDER_ID", str(exc), recoverable=False)) from exc

    @router.post("/api/model-governance/providers/{provider_id}/verify")
    def verify_status(provider_id: str, request: Request):
        local(request)
        metadata = service.credential_metadata(provider_id)
        if not metadata.get("last_verified_at"):
            raise HTTPException(
                status_code=409,
                detail=error_payload(
                    "PROVIDER_VERIFICATION_REQUIRED",
                    "請先完成連線測試與指定模型能力測試。",
                    recoverable=True,
                ),
            )
        return {"success": True, "credential": metadata}

    @router.get("/api/model-governance/usage")
    def usage(request: Request, project_id: Optional[str] = None, since: Optional[str] = None):
        local(request)
        return {"success": True, **service.usage(project_id=project(project_id), since=since)}

    @router.get("/api/model-governance/budgets/{scope_kind}/{scope_id}")
    def get_budget(scope_kind: str, scope_id: str, request: Request):
        local(request)
        try:
            if scope_kind == "project":
                project(scope_id)
            return {"success": True, "budget": service.get_budget(scope_kind, scope_id)}
        except GovernanceError as exc:
            raise fail(exc) from exc

    @router.put("/api/model-governance/budgets/{scope_kind}/{scope_id}")
    def put_budget(scope_kind: str, scope_id: str, body: BudgetPolicyRequest, request: Request):
        local(request)
        try:
            if scope_kind == "project":
                project(scope_id)
            return {
                "success": True,
                "budget": service.put_budget(
                    scope_kind,
                    scope_id,
                    revision=body.revision,
                    timezone_name=body.timezone,
                    policy=body.policy,
                ),
            }
        except GovernanceError as exc:
            raise fail(exc) from exc

    @router.get("/api/projects/{project_id}/model-routing-policy")
    def get_routing_policy(project_id: str, request: Request):
        local(request); project(project_id)
        return {"success": True, "policy": service.get_routing_policy(project_id)}

    @router.put("/api/projects/{project_id}/model-routing-policy")
    def put_routing_policy(project_id: str, body: RoutingPolicyRequest, request: Request):
        local(request); project(project_id)
        try:
            return {
                "success": True,
                "policy": service.put_routing_policy(
                    project_id,
                    revision=body.revision,
                    mode=body.mode,
                    allowed_providers=body.allowed_providers,
                    data_consent=body.data_consent,
                    preferred_models=body.preferred_models,
                ),
            }
        except GovernanceError as exc:
            raise fail(exc) from exc

    @router.post("/api/model-routing/resolve")
    def resolve_route(body: RouteResolveRequest, request: Request):
        local(request)
        try:
            return {
                "success": True,
                **service.resolve_route(
                    project_id=project(body.project_id),
                    run_id=body.run_id,
                    requested_model=body.requested_model,
                    requirements=body.requirements,
                    candidates=model_inventory(),
                ),
            }
        except GovernanceError as exc:
            raise fail(exc) from exc

    @router.post("/api/model-routing/proposals/{proposal_id}/approve")
    def approve_route(proposal_id: str, body: RouteApprovalRequest, request: Request):
        local(request)
        try:
            return {"success": True, "proposal": service.approve_proposal(proposal_id, remember_project=body.remember_project)}
        except GovernanceError as exc:
            raise fail(exc) from exc

    @router.post("/api/model-governance/budget-overrides")
    def create_override(body: BudgetOverrideRequest, request: Request):
        local(request)
        return {"success": True, "override": service.create_budget_override(project_id=project(body.project_id), run_id=body.run_id)}

    return router


__all__ = ["build_model_governance_router"]
