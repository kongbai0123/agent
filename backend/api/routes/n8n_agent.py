"""Browser and Agent-facing HTTP boundary for governed n8n administration."""

from __future__ import annotations

from typing import Any, Callable, Dict, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from n8n_agent_governance import N8nAgentGovernanceService, N8nGovernanceError
from n8n_agent_planner import N8nPlannerError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PolicyUpdate(_Strict):
    project_id: str = Field(min_length=1, max_length=128)
    mode: Literal["off", "restricted", "full_audit"]
    elevation_policy: Literal["one_hour", "session", "persistent", "smart"] = "smart"
    session_id: Optional[str] = Field(default=None, max_length=128)
    explicit_ack: bool = False


class OperationRequest(_Strict):
    project_id: str = Field(min_length=1, max_length=128)
    session_id: Optional[str] = Field(default=None, max_length=128)
    run_id: Optional[str] = Field(default=None, max_length=128)
    operation: str = Field(min_length=1, max_length=64)
    payload: Dict[str, Any] = Field(default_factory=dict)
    diff: Dict[str, Any] = Field(default_factory=dict)
    base_digest: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class OperationDecision(_Strict):
    project_id: str = Field(min_length=1, max_length=128)
    # Required even when null so the approval is explicitly bound to the
    # operation's Project-only or Project+Session scope.
    session_id: Optional[str] = Field(max_length=128)
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmation: Optional[str] = Field(default=None, max_length=255)


class ApiKeyUpdate(_Strict):
    api_key: SecretStr


class CredentialSecret(_Strict):
    project_id: str = Field(min_length=1, max_length=128)
    fields: Dict[str, Any]


class PlanStart(_Strict):
    project_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=8_000)
    model: Optional[str] = Field(default=None, max_length=255)


class PlanMessage(PlanStart):
    selected_option_id: Optional[str] = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class PlanProposal(_Strict):
    project_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    explicit_confirmation: bool


class PlanMaterialize(_Strict):
    project_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    model: Optional[str] = Field(default=None, max_length=255)


class WorkflowAdoption(_Strict):
    project_id: str = Field(min_length=1, max_length=128)
    session_id: Optional[str] = Field(default=None, max_length=128)
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmation: str = Field(min_length=1, max_length=255)


def build_n8n_agent_router(
    *, service: N8nAgentGovernanceService, secret_store: Any,
    require_local: Callable[[Request], None], error_payload: Callable[..., Dict[str, Any]],
    planner: Any = None,
) -> APIRouter:
    router = APIRouter(tags=["n8n-agent-governance"])

    def local(request: Request) -> None:
        require_local(request)

    def failure(exc: BaseException) -> HTTPException:
        if isinstance(exc, (N8nGovernanceError, N8nPlannerError)):
            return HTTPException(
                status_code=exc.status_code,
                detail=error_payload(exc.code, exc.message, recoverable=exc.status_code < 500),
            )
        return HTTPException(
            status_code=500,
            detail=error_payload("N8N_GOVERNANCE_ERROR", "The governed n8n request failed.", recoverable=False),
        )

    def planning_service() -> Any:
        if planner is None:
            raise N8nPlannerError(
                "N8N_PLANNER_UNAVAILABLE",
                "The governed n8n planning assistant is unavailable.",
                status_code=503,
            )
        return planner

    @router.get("/api/integrations/n8n/agent-policy")
    def get_policy(request: Request, project_id: str = Query(min_length=1, max_length=128), session_id: Optional[str] = Query(default=None, max_length=128)):
        local(request)
        try: return service.get_policy(project_id, session_id=session_id)
        except Exception as exc: raise failure(exc) from exc

    @router.put("/api/integrations/n8n/agent-policy")
    def put_policy(payload: PolicyUpdate, request: Request):
        local(request)
        try: return service.set_policy(payload.project_id, payload.model_dump())
        except Exception as exc: raise failure(exc) from exc

    @router.get("/api/integrations/n8n/managed-workflows")
    def managed_workflows(
        request: Request,
        project_id: str = Query(min_length=1, max_length=128),
        session_id: Optional[str] = Query(default=None, max_length=128),
    ):
        local(request)
        try: return service.list_workflows(project_id, session_id=session_id)
        except Exception as exc: raise failure(exc) from exc

    @router.get("/api/integrations/n8n/node-catalog")
    def node_catalog(
        request: Request,
        project_id: str = Query(min_length=1, max_length=128),
        session_id: Optional[str] = Query(default=None, max_length=128),
        query: str = Query(default="", max_length=255),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        local(request)
        try: return service.search_node_catalog(project_id, session_id=session_id, query=query, limit=limit)
        except Exception as exc: raise failure(exc) from exc

    @router.post("/api/integrations/n8n/managed-workflows/{workflow_id}/adopt")
    def adopt_workflow(workflow_id: str, payload: WorkflowAdoption, request: Request):
        local(request)
        try:
            return service.adopt_workflow(
                payload.project_id, workflow_id, session_id=payload.session_id,
                expected_digest=payload.expected_digest, confirmation=payload.confirmation,
            )
        except Exception as exc: raise failure(exc) from exc

    @router.get("/api/integrations/n8n/managed-workflows/{workflow_id}/adoption-preview")
    def adoption_preview(
        workflow_id: str, request: Request,
        project_id: str = Query(min_length=1, max_length=128),
        session_id: Optional[str] = Query(default=None, max_length=128),
    ):
        local(request)
        try: return service.preview_adoption(project_id, workflow_id, session_id=session_id)
        except Exception as exc: raise failure(exc) from exc

    @router.get("/api/integrations/n8n/operation-requests")
    def list_operations(request: Request, project_id: str = Query(min_length=1, max_length=128), limit: int = Query(default=100, ge=1, le=250)):
        local(request)
        try: return {"operations": service.list_operations(project_id, limit=limit)}
        except Exception as exc: raise failure(exc) from exc

    @router.post("/api/integrations/n8n/operation-requests", status_code=202)
    def create_operation(payload: OperationRequest, request: Request):
        local(request)
        try: return service.create_operation(payload.model_dump())
        except Exception as exc: raise failure(exc) from exc

    @router.get("/api/integrations/n8n/operation-requests/{operation_id}")
    def get_operation(operation_id: str, request: Request, project_id: str = Query(min_length=1, max_length=128)):
        local(request)
        try: return service.get_operation(operation_id, project_id=project_id)
        except Exception as exc: raise failure(exc) from exc

    @router.post("/api/integrations/n8n/operation-requests/{operation_id}/approve")
    def approve_operation(operation_id: str, payload: OperationDecision, request: Request):
        local(request)
        try: return service.decide(operation_id, project_id=payload.project_id, session_id=payload.session_id, expected_digest=payload.expected_digest, approved=True, confirmation=payload.confirmation)
        except Exception as exc: raise failure(exc) from exc

    @router.post("/api/integrations/n8n/operation-requests/{operation_id}/reject")
    def reject_operation(operation_id: str, payload: OperationDecision, request: Request):
        local(request)
        try: return service.decide(operation_id, project_id=payload.project_id, session_id=payload.session_id, expected_digest=payload.expected_digest, approved=False)
        except Exception as exc: raise failure(exc) from exc

    @router.get("/api/integrations/n8n/audits")
    def audits(request: Request, project_id: str = Query(min_length=1, max_length=128), limit: int = Query(default=100, ge=1, le=250)):
        local(request)
        try: return {"audits": service.list_audits(project_id, limit=limit)}
        except Exception as exc: raise failure(exc) from exc

    @router.put("/api/integrations/n8n/agent-api-key")
    def configure_api_key(payload: ApiKeyUpdate, request: Request):
        local(request)
        try:
            secret_store.set_api_key(payload.api_key.get_secret_value())
            return {"configured": True}
        except Exception as exc: raise failure(exc) from exc

    @router.post("/api/integrations/n8n/credential-secrets", status_code=201)
    def stage_credential_secret(payload: CredentialSecret, request: Request):
        local(request)
        try: return service.stage_secret(payload.project_id, payload.fields)
        except Exception as exc: raise failure(exc) from exc

    @router.post("/api/integrations/n8n/plans", status_code=201)
    def start_plan(payload: PlanStart, request: Request):
        local(request)
        try:
            return planning_service().start(**payload.model_dump())
        except Exception as exc:
            raise failure(exc) from exc

    @router.post("/api/integrations/n8n/plans/{plan_id}/messages")
    def add_plan_message(plan_id: str, payload: PlanMessage, request: Request):
        local(request)
        try:
            return planning_service().add_message(plan_id, **payload.model_dump())
        except Exception as exc:
            raise failure(exc) from exc

    @router.post("/api/integrations/n8n/plans/{plan_id}/propose", status_code=202)
    def propose_plan(plan_id: str, payload: PlanProposal, request: Request):
        local(request)
        try:
            return planning_service().propose(plan_id, **payload.model_dump())
        except Exception as exc:
            raise failure(exc) from exc

    @router.post("/api/integrations/n8n/plans/{plan_id}/materialize")
    def materialize_plan(plan_id: str, payload: PlanMaterialize, request: Request):
        local(request)
        try:
            return planning_service().materialize(plan_id, **payload.model_dump())
        except Exception as exc:
            raise failure(exc) from exc

    return router


__all__ = ["build_n8n_agent_router"]
