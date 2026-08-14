"""Standalone HTTP boundary for protected n8n Workbench-Agent tasks."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from n8n_agent_task_runtime import N8nAgentTaskError, N8nAgentTaskRuntime


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SkillBinding(_Strict):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class AgentBindingCreate(_Strict):
    project_id: str = Field(min_length=1, max_length=128)
    workflow_id: str = Field(min_length=1, max_length=128)
    workflow_revision: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    instruction: str = Field(min_length=1, max_length=20_000)
    model: str = Field(min_length=1, max_length=255)
    output_schema: Dict[str, Any]
    skills: List[SkillBinding] = Field(default_factory=list, max_length=8)


class AgentBindingUpdate(_Strict):
    project_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
    workflow_revision: str = Field(min_length=1, max_length=128)
    instruction: str = Field(min_length=1, max_length=20_000)
    model: str = Field(min_length=1, max_length=255)
    output_schema: Dict[str, Any]
    skills: List[SkillBinding] = Field(default_factory=list, max_length=8)


class CredentialAdopt(_Strict):
    project_id: str = Field(min_length=1, max_length=128)
    alias: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,62}$")
    credential_id: str = Field(min_length=1, max_length=255)


class RuntimeDecision(_Strict):
    project_id: str = Field(min_length=1, max_length=128)
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    duration_minutes: int = Field(default=0, ge=0, le=60)


class TaskSubmit(_Strict):
    request_id: str = Field(min_length=1, max_length=128)
    workflow_id: str = Field(min_length=1, max_length=128)
    workflow_revision: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    agent_binding_id: str = Field(min_length=1, max_length=128)
    input: Any


class WorkflowScope(_Strict):
    workflow_id: str = Field(min_length=1, max_length=128)


class RuntimeAction(_Strict):
    request_id: str = Field(min_length=1, max_length=128)
    workflow_id: str = Field(min_length=1, max_length=128)
    workflow_revision: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    credential_alias: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,62}$")
    target_kind: Literal["email", "url", "service"]
    target: str = Field(min_length=1, max_length=2_048)
    action: Literal[
        "send_email", "http_write", "database_write", "delete", "publish", "external_write"
    ]
    run_key: str = Field(min_length=1, max_length=128)
    task_id: Optional[str] = Field(default=None, max_length=128)


def build_n8n_agent_tasks_router(
    *,
    runtime: N8nAgentTaskRuntime,
    require_local: Callable[[Request], None],
    error_payload: Callable[..., Dict[str, Any]],
) -> APIRouter:
    """Build the router without mutating app.py or global application state."""

    router = APIRouter(tags=["n8n-agent-tasks"])

    def local(request: Request) -> None:
        require_local(request)

    def failure(exc: BaseException) -> HTTPException:
        if isinstance(exc, N8nAgentTaskError):
            return HTTPException(
                status_code=exc.status_code,
                detail=error_payload(
                    exc.code, exc.message,
                    recoverable=exc.recoverable or (400 <= exc.status_code < 500),
                ),
            )
        if isinstance(exc, ValidationError):
            return HTTPException(
                status_code=422,
                detail=error_payload(
                    "N8N_AGENT_REQUEST_INVALID", "The Agent runtime request is invalid.", recoverable=True
                ),
            )
        return HTTPException(
            status_code=500,
            detail=error_payload(
                "N8N_AGENT_RUNTIME_ERROR", "The protected Agent runtime request failed.", recoverable=False
            ),
        )

    async def signed(request: Request, model: type[_Strict]) -> _Strict:
        body = await request.body()
        try:
            runtime.authenticate_request(
                method=request.method, path=request.url.path, headers=request.headers, body=body
            )
            return model.model_validate_json(body, strict=True)
        except Exception as exc:
            raise failure(exc) from exc

    def process_in_background(task_id: str) -> None:
        try:
            runtime.process_task(task_id)
        except Exception:
            # The runtime persists a safe failure state and never logs model
            # output, task input or credential material here.
            return

    # Local browser APIs.  None of these responses contains instructions,
    # model input/output, n8n credential ids, OAuth material or HMAC secrets.
    @router.post("/api/integrations/n8n/agent-bindings", status_code=201)
    def create_binding(payload: AgentBindingCreate, request: Request):
        local(request)
        try:
            return runtime.create_binding(payload.model_dump())
        except Exception as exc:
            raise failure(exc) from exc

    @router.get("/api/integrations/n8n/agent-bindings")
    def list_bindings(
        request: Request,
        project_id: str = Query(min_length=1, max_length=128),
    ):
        local(request)
        try:
            return {"bindings": runtime.list_bindings(project_id)}
        except Exception as exc:
            raise failure(exc) from exc

    @router.put("/api/integrations/n8n/agent-bindings/{binding_id}")
    def update_binding(binding_id: str, payload: AgentBindingUpdate, request: Request):
        local(request)
        try:
            return runtime.update_binding(binding_id, payload.model_dump())
        except Exception as exc:
            raise failure(exc) from exc

    @router.delete("/api/integrations/n8n/agent-bindings/{binding_id}")
    def deactivate_binding(
        binding_id: str,
        request: Request,
        project_id: str = Query(min_length=1, max_length=128),
    ):
        local(request)
        try:
            return runtime.deactivate_binding(binding_id, project_id=project_id)
        except Exception as exc:
            raise failure(exc) from exc

    @router.post("/api/integrations/n8n/credential-aliases", status_code=201)
    def adopt_credential(payload: CredentialAdopt, request: Request):
        local(request)
        try:
            return runtime.adopt_credential_alias(payload.model_dump())
        except Exception as exc:
            raise failure(exc) from exc

    @router.get("/api/integrations/n8n/credential-aliases")
    def list_credentials(
        request: Request,
        project_id: str = Query(min_length=1, max_length=128),
    ):
        local(request)
        try:
            return {"credentials": runtime.list_credential_aliases(project_id)}
        except Exception as exc:
            raise failure(exc) from exc

    @router.post("/api/integrations/n8n/credential-aliases/{alias}/refresh")
    def refresh_credential(
        alias: str,
        request: Request,
        project_id: str = Query(min_length=1, max_length=128),
    ):
        local(request)
        try:
            return runtime.refresh_credential_alias(project_id, alias)
        except Exception as exc:
            raise failure(exc) from exc

    @router.delete("/api/integrations/n8n/credential-aliases/{alias}")
    def revoke_credential(
        alias: str,
        request: Request,
        project_id: str = Query(min_length=1, max_length=128),
    ):
        local(request)
        try:
            return runtime.revoke_credential_alias(project_id, alias)
        except Exception as exc:
            raise failure(exc) from exc

    @router.get("/api/integrations/n8n/agent-tasks")
    def list_tasks(
        request: Request,
        project_id: str = Query(min_length=1, max_length=128),
        limit: int = Query(default=100, ge=1, le=250),
    ):
        local(request)
        try:
            return {"tasks": runtime.list_tasks(project_id, limit=limit)}
        except Exception as exc:
            raise failure(exc) from exc

    @router.get("/api/integrations/n8n/agent-tasks/{task_id}")
    def get_task(
        task_id: str,
        request: Request,
        project_id: str = Query(min_length=1, max_length=128),
    ):
        local(request)
        try:
            return runtime.get_task_public(task_id, project_id=project_id)
        except Exception as exc:
            raise failure(exc) from exc

    @router.get("/api/integrations/n8n/runtime-approvals")
    def list_approvals(
        request: Request,
        project_id: str = Query(min_length=1, max_length=128),
        status: Optional[str] = Query(default=None, max_length=32),
        limit: int = Query(default=100, ge=1, le=250),
    ):
        local(request)
        try:
            return {"approvals": runtime.list_runtime_approvals(project_id, status=status, limit=limit)}
        except Exception as exc:
            raise failure(exc) from exc

    @router.post("/api/integrations/n8n/runtime-approvals/{approval_id}/approve")
    def approve_runtime(approval_id: str, payload: RuntimeDecision, request: Request):
        local(request)
        try:
            return runtime.decide_runtime_approval(
                approval_id,
                project_id=payload.project_id,
                expected_digest=payload.expected_digest,
                approved=True,
                duration_minutes=payload.duration_minutes,
            )
        except Exception as exc:
            raise failure(exc) from exc

    @router.post("/api/integrations/n8n/runtime-approvals/{approval_id}/reject")
    def reject_runtime(approval_id: str, payload: RuntimeDecision, request: Request):
        local(request)
        try:
            return runtime.decide_runtime_approval(
                approval_id,
                project_id=payload.project_id,
                expected_digest=payload.expected_digest,
                approved=False,
            )
        except Exception as exc:
            raise failure(exc) from exc

    # n8n-only APIs.  Project selection is deliberately absent; the runtime
    # derives it from the server-owned managed-workflow binding.
    @router.post("/api/integrations/n8n/v1/agent/tasks", status_code=202)
    async def submit_task(request: Request, background: BackgroundTasks):
        payload = await signed(request, TaskSubmit)
        try:
            result = runtime.submit_task(payload.model_dump())
            # Scheduling an idempotent queued request is safe because the
            # durable claim allows only one worker to move it to generating.
            if result["status"] == "queued":
                background.add_task(process_in_background, result["task_id"])
            return result
        except Exception as exc:
            raise failure(exc) from exc

    @router.post("/api/integrations/n8n/v1/agent/tasks/{task_id}/status")
    async def task_status(task_id: str, request: Request):
        payload = await signed(request, WorkflowScope)
        try:
            return runtime.get_task_for_n8n(task_id, workflow_id=payload.workflow_id)
        except Exception as exc:
            raise failure(exc) from exc

    @router.post("/api/integrations/n8n/v1/agent/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str, request: Request):
        payload = await signed(request, WorkflowScope)
        try:
            return runtime.cancel_task(task_id, workflow_id=payload.workflow_id)
        except Exception as exc:
            raise failure(exc) from exc

    @router.post("/api/integrations/n8n/v1/agent/runtime-actions", status_code=202)
    async def request_runtime_action(request: Request):
        payload = await signed(request, RuntimeAction)
        try:
            result = runtime.request_runtime_approval(payload.model_dump())
            # n8n receives authorization state, never Workbench Project identity.
            result.pop("project_id", None)
            return result
        except Exception as exc:
            raise failure(exc) from exc

    @router.post("/api/integrations/n8n/v1/agent/runtime-actions/{approval_id}/status")
    async def runtime_action_status(approval_id: str, request: Request):
        payload = await signed(request, WorkflowScope)
        try:
            result = runtime.get_runtime_approval_for_n8n(
                approval_id, workflow_id=payload.workflow_id
            )
            result.pop("project_id", None)
            return result
        except Exception as exc:
            raise failure(exc) from exc

    return router


__all__ = ["build_n8n_agent_tasks_router"]
