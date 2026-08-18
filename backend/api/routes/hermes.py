"""Local-only status, probe, approval, and cancellation routes for Hermes."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from api.schemas.hermes import (
    HermesApprovalDecisionRequest,
    HermesChatApprovalDecisionRequest,
)
from hermes import HermesAuthenticationError, HermesError, HermesUnavailableError
from hermes_approval_store import (
    HermesApprovalConflictError,
    HermesApprovalStoreError,
)
from hermes_integration import HermesIntegrationManager


def _http_failure(
    exc: BaseException,
    error_payload: Callable[..., Dict[str, Any]],
) -> HTTPException:
    if isinstance(exc, KeyError):
        status_code, code, message = 404, "HERMES_RECORD_NOT_FOUND", "Hermes record was not found."
    elif isinstance(exc, HermesApprovalConflictError):
        status_code, code, message = 409, "HERMES_APPROVAL_CONFLICT", str(exc)
    elif isinstance(exc, HermesApprovalStoreError):
        status_code, code, message = 400, "HERMES_APPROVAL_INVALID", str(exc)
    elif isinstance(exc, HermesAuthenticationError):
        status_code, code, message = 502, exc.code, str(exc)
    elif isinstance(exc, HermesUnavailableError):
        status_code, code, message = 503, exc.code, str(exc)
    elif isinstance(exc, HermesError):
        status_code, code, message = 502, exc.code, str(exc)
    elif isinstance(exc, ValueError):
        status_code, code, message = 400, "HERMES_REQUEST_INVALID", str(exc)
    else:
        status_code, code, message = 500, "HERMES_CONTROL_ERROR", "Hermes control request failed."
    return HTTPException(
        status_code=status_code,
        detail=error_payload(
            code,
            message,
            recoverable=status_code in {400, 404, 409, 503},
        ),
    )


def build_hermes_router(
    *,
    manager: Optional[HermesIntegrationManager] = None,
    manager_provider: Optional[
        Callable[[], Optional[HermesIntegrationManager]]
    ] = None,
    status_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    require_local: Callable[[Request], None],
    error_payload: Callable[..., Dict[str, Any]],
    cancel_local_run: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    rollback_handler: Optional[Callable[[], Dict[str, Any]]] = None,
    generic_approval_resolver: Optional[
        Callable[[str, str, bool], Optional[Dict[str, Any]]]
    ] = None,
) -> APIRouter:
    if manager is None and manager_provider is None:
        raise ValueError("Hermes router requires a manager or manager provider.")

    router = APIRouter(tags=["hermes"])

    def current_manager() -> HermesIntegrationManager:
        current = manager_provider() if manager_provider is not None else manager
        if current is None:
            raise HermesUnavailableError(
                "Hermes is not installed or its runtime configuration is unavailable."
            )
        return current

    def current_status() -> Dict[str, Any]:
        if status_provider is not None:
            return dict(status_provider())
        return current_manager().status()

    @router.get("/api/hermes/status")
    def status():
        return {"success": True, **current_status()}

    @router.post("/api/hermes/probe")
    def probe(request: Request):
        require_local(request)
        try:
            current = current_manager()
            result = current.probe()
            return {
                **current.status(),
                "success": bool(result.get("success")),
                "probe": result,
            }
        except HermesUnavailableError:
            return {"success": False, **current_status()}

    @router.post("/api/hermes/rollout/rollback")
    def rollback(request: Request):
        """Atomically return all chat traffic to the Workbench basic runtime."""

        require_local(request)
        if rollback_handler is None:
            raise HTTPException(
                status_code=503,
                detail=error_payload(
                    "HERMES_ROLLBACK_UNAVAILABLE",
                    "Hermes rollback control is unavailable.",
                    recoverable=True,
                ),
            )
        try:
            result = rollback_handler()
        except HTTPException:
            raise
        except Exception as exc:
            raise _http_failure(exc, error_payload) from exc
        return {"success": True, **result}

    @router.get("/api/hermes/approvals")
    def pending_approvals(
        session_id: Optional[str] = Query(default=None, max_length=256),
        run_id: Optional[str] = Query(default=None, max_length=256),
    ):
        try:
            records = current_manager().approval_store.list_pending(
                session_id=session_id,
                run_id=run_id,
            )
            return {
                "success": True,
                "approvals": [record.public_dict() for record in records],
            }
        except Exception as exc:
            raise _http_failure(exc, error_payload) from exc

    def decide_approval(
        approval_id: str,
        choice: str,
        body: HermesApprovalDecisionRequest,
        request: Request,
    ):
        require_local(request)
        try:
            record = current_manager().resolve_approval(
                approval_id,
                choice=choice,
                rationale=body.rationale,
            )
            return {"success": True, "approval": record.public_dict()}
        except Exception as exc:
            raise _http_failure(exc, error_payload) from exc

    @router.post("/api/hermes/approvals/{approval_id}/once")
    def approve_once(
        approval_id: str,
        body: HermesApprovalDecisionRequest,
        request: Request,
    ):
        return decide_approval(approval_id, "once", body, request)

    @router.post("/api/hermes/approvals/{approval_id}/deny")
    def deny(
        approval_id: str,
        body: HermesApprovalDecisionRequest,
        request: Request,
    ):
        return decide_approval(approval_id, "deny", body, request)

    @router.post("/api/chat/runs/{run_id}/approval")
    def chat_approval_compatibility(
        run_id: str,
        body: HermesChatApprovalDecisionRequest,
        request: Request,
    ):
        require_local(request)
        if generic_approval_resolver is not None:
            try:
                generic = generic_approval_resolver(
                    run_id,
                    body.approval_id,
                    bool(body.approved),
                )
            except HTTPException:
                raise
            if generic is not None:
                return {
                    "success": True,
                    "approved": body.approved,
                    "approval": generic,
                }
        try:
            active_manager = current_manager()
            current = active_manager.approval_store.get(body.approval_id)
            if current is None:
                raise KeyError(body.approval_id)
            if current.workbench_run_id != run_id:
                raise HermesApprovalConflictError(
                    "Approval belongs to another Workbench run."
                )
            record = active_manager.resolve_approval(
                body.approval_id,
                choice="once" if body.approved else "deny",
                rationale=(
                    "Approved once by the local Workbench user."
                    if body.approved
                    else "Denied by the local Workbench user."
                ),
            )
            return {
                "success": True,
                "approved": body.approved,
                "approval": record.public_dict(),
            }
        except Exception as exc:
            raise _http_failure(exc, error_payload) from exc

    @router.get("/api/hermes/runs/{run_id}")
    def run_status(run_id: str):
        try:
            return {"success": True, **current_manager().run_status(run_id)}
        except Exception as exc:
            raise _http_failure(exc, error_payload) from exc

    @router.post("/api/hermes/runs/{run_id}/cancel")
    def cancel_run(run_id: str, request: Request):
        require_local(request)
        local_result = cancel_local_run(run_id) if cancel_local_run else None
        if local_result is not None:
            # An active Workbench ChatRunControl owns a HermesUpstreamCancellation
            # attachment. Its synchronous close already sends the one upstream
            # /stop request. Calling manager.cancel() again can race the first
            # stop and turn a successful cancellation into a Hermes 404/502.
            return {
                "success": True,
                "run_id": run_id,
                "status": "stopping",
                "cancelled": True,
                "upstream": "delegated_to_chat_control",
                "local": local_result,
            }
        try:
            upstream = current_manager().cancel(run_id)
        except KeyError:
            if local_result is None:
                raise _http_failure(KeyError(run_id), error_payload)
            upstream = {"run_id": run_id, "cancelled": True, "upstream": "not_bound"}
        except HermesUnavailableError:
            if local_result is None:
                raise _http_failure(
                    HermesUnavailableError("Hermes is unavailable."), error_payload
                )
            upstream = {"run_id": run_id, "cancelled": True, "upstream": "unavailable"}
        except Exception as exc:
            raise _http_failure(exc, error_payload) from exc
        return {
            "success": True,
            **upstream,
            "local": local_result,
        }

    return router


__all__ = ["build_hermes_router"]
