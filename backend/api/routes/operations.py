"""Read-only inspection API for the shared operational contracts."""

from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request

from operations_core import OperationsCore


def build_operations_router(*, core: OperationsCore, require_local: Callable[[Request], None],
                            require_project: Callable[[str], Any]) -> APIRouter:
    router = APIRouter(tags=["operations"])

    def project(value: Optional[str]) -> Optional[str]:
        if value and require_project(value) is None:
            raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND", "message": "找不到指定專案。"})
        return value

    @router.get("/api/operations/executions")
    def executions(request: Request, project_id: Optional[str] = None, kind: Optional[str] = None, limit: int = 100):
        require_local(request)
        return {"executions": core.list_executions(project_id=project(project_id), kind=kind, limit=limit)}

    @router.get("/api/operations/executions/{execution_id}")
    def execution(request: Request, execution_id: str):
        require_local(request)
        item = core.get_execution(execution_id)
        if item is None:
            raise HTTPException(status_code=404, detail={"code": "EXECUTION_NOT_FOUND", "message": "找不到執行紀錄。"})
        return {"execution": item, "events": core.list_events(execution_id), "artifacts": core.list_artifacts(execution_id=execution_id)}

    @router.get("/api/operations/policy-decisions")
    def decisions(request: Request, project_id: Optional[str] = None, limit: int = 100):
        require_local(request)
        return {"decisions": core.list_policy_decisions(project_id=project(project_id), limit=limit)}

    @router.get("/api/operations/artifacts")
    def artifacts(request: Request, project_id: Optional[str] = None, limit: int = 100):
        require_local(request)
        return {"artifacts": core.list_artifacts(project_id=project(project_id), limit=limit)}

    @router.get("/api/operations/health")
    def health(request: Request, project_id: Optional[str] = None):
        require_local(request)
        return {"components": core.list_health(project_id=project(project_id))}

    return router
