"""Local HTTP boundary for the unified Integration Center."""

from __future__ import annotations

from typing import Any, Callable, Dict

from fastapi import APIRouter, HTTPException, Query, Request

from api.schemas.integration_center import IntegrationPolicyReplaceRequest
from integration_center_service import IntegrationCenterError, IntegrationCenterService
from integration_center_store import IntegrationCenterStoreError


def _failure(exc: BaseException, error_payload: Callable[..., Dict[str, Any]]) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, (IntegrationCenterError, IntegrationCenterStoreError)):
        return HTTPException(
            status_code=exc.status_code,
            detail=error_payload(
                exc.code,
                exc.message,
                recoverable=getattr(exc, "recoverable", exc.status_code < 500),
            ),
        )
    return HTTPException(
        status_code=500,
        detail=error_payload(
            "INTEGRATION_CENTER_INTERNAL_ERROR",
            "整合中心暫時無法處理要求。",
            recoverable=True,
        ),
    )


def build_integration_center_router(
    *,
    service: IntegrationCenterService,
    require_local: Callable[[Request], None],
    error_payload: Callable[..., Dict[str, Any]],
) -> APIRouter:
    router = APIRouter(tags=["integration-center"])

    @router.get("/api/integration-center/overview")
    def get_overview(request: Request, project_id: str = Query(min_length=1, max_length=512)):
        require_local(request)
        try:
            return service.overview(project_id)
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.get("/api/integration-center/policies/{project_id}")
    def get_policy(project_id: str, request: Request):
        require_local(request)
        try:
            return {"success": True, "policy": service.get_policy(project_id)}
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.put("/api/integration-center/policies/{project_id}")
    def put_policy(
        project_id: str,
        payload: IntegrationPolicyReplaceRequest,
        request: Request,
    ):
        require_local(request)
        try:
            policy = service.put_policy(
                project_id,
                expected_revision=payload.revision,
                policy={
                    "name": payload.name,
                    "permission_mode": payload.permission_mode,
                    "grants": [item.model_dump(mode="json") for item in payload.grants],
                },
                acknowledge_open_risk=payload.acknowledge_open_risk,
            )
            return {"success": True, "policy": policy}
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.get("/api/integration-center/audit")
    def get_audit(
        request: Request,
        project_id: str = Query(min_length=1, max_length=512),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        require_local(request)
        try:
            return {
                "success": True,
                "project_id": project_id,
                "audits": service.audits(project_id, limit=limit),
            }
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    return router


__all__ = ["build_integration_center_router"]
