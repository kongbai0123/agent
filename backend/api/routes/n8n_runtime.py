"""Browser-only lifecycle and status routes for managed n8n."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, Mapping, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from n8n_lifecycle import N8N_BASE_URL, N8nLifecycleError


def _public_status(raw: Mapping[str, Any], *, workflow_ready: bool) -> Dict[str, Any]:
    state = str(raw.get("state") or "failed")
    installation = raw.get("installation") if isinstance(raw.get("installation"), Mapping) else {}
    running = state in {"ready", "starting", "degraded"}
    ready = state == "ready"
    reason = str(raw.get("reason") or "status_unavailable")
    return {
        "installed": bool(installation.get("valid")),
        "running": running,
        "reachable": ready,
        "starting": state == "starting",
        "state": state,
        "reason": reason,
        "message": {
            "ready": "n8n 已在受控本機環境中執行。",
            "stopped": "n8n 已停止；Workbench 關閉時不會執行排程。",
            "upgrade_required": "n8n 或 Node 版本與固定版本不符。",
            "port_conflict": "連接埠 5678 被未受 Workbench 管理的程序占用。",
            "degraded": "n8n 程序存在，但健康檢查尚未通過。",
            "starting": "n8n 正在啟動。",
        }.get(state, "n8n 尚未通過受控執行檢查。"),
        "version": str(raw.get("version") or ""),
        "node_version": str(raw.get("node_version") or ""),
        "editor_url": f"{N8N_BASE_URL}/" if ready else None,
        "workflow_ready": bool(workflow_ready and ready),
        "isolation_ready": raw.get("isolation_ready") is True,
        "isolation_blockers": [
            str(item) for item in list(raw.get("isolation_blockers") or [])[:32]
        ],
        "checked_at": raw.get("checked_at"),
    }


def build_n8n_runtime_router(
    *,
    lifecycle: Any,
    require_local: Callable[[Request], None],
    error_payload: Callable[..., Dict[str, Any]],
    workflow_ready: Optional[Callable[[], bool]] = None,
    workflow_status: Optional[Callable[[], Mapping[str, Any]]] = None,
    mail_status: Optional[Callable[[], Mapping[str, Any]]] = None,
    on_stop: Optional[Callable[[], None]] = None,
    require_extension: Optional[Callable[[str, Optional[str]], Any]] = None,
) -> APIRouter:
    router = APIRouter(tags=["n8n-runtime"])

    def require_n8n_extension() -> None:
        """Gate lifecycle mutations while preserving the legacy no-gate default."""

        if require_extension is None:
            return
        try:
            decision = require_extension("builtin.n8n", None)
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail=error_payload(
                    str(getattr(exc, "code", "EXTENSION_DISABLED")),
                    str(exc) or "The n8n extension is disabled.",
                    recoverable=True,
                ),
            ) from exc
        if decision is False:
            raise HTTPException(
                status_code=409,
                detail=error_payload(
                    "EXTENSION_DISABLED",
                    "The n8n extension is disabled.",
                    recoverable=True,
                ),
            )

    def readiness() -> Dict[str, Any]:
        try:
            if workflow_status is not None:
                raw = workflow_status()
                return dict(raw) if isinstance(raw, Mapping) else {"ready": False}
            return {"ready": bool(workflow_ready and workflow_ready())}
        except Exception:
            return {"ready": False}

    def status_payload(*, probe_node: bool = False) -> Dict[str, Any]:
        workflow = readiness()
        payload = _public_status(
            lifecycle.status(probe_node=probe_node),
            workflow_ready=bool(workflow.get("ready")),
        )
        credentials = (
            workflow.get("credentials")
            if isinstance(workflow.get("credentials"), Mapping)
            else {}
        )
        payload["gmail_oauth_ready"] = credentials.get("gmail_oauth_bound") is True
        if mail_status is not None:
            try:
                snapshot = dict(mail_status() or {})
                # This callback is content-free by contract.  Whitelist again
                # here so a future service change cannot leak mail via SSE.
                payload["mail"] = {
                    "type": str(snapshot.get("type") or "mail_runs_changed"),
                    "pending_approvals": max(0, int(snapshot.get("pending_approvals") or 0)),
                    "counts": {
                        str(key)[:64]: max(0, int(value or 0))
                        for key, value in dict(snapshot.get("counts") or {}).items()
                    },
                    "latest_updated_at": snapshot.get("latest_updated_at"),
                    "revision": str(snapshot.get("fingerprint") or "")[:64],
                }
            except Exception:
                payload["mail"] = {
                    "type": "mail_events_unavailable",
                    "pending_approvals": 0,
                    "counts": {},
                    "latest_updated_at": None,
                    "revision": "",
                }
        return payload

    def lifecycle_failure(exc: BaseException) -> HTTPException:
        if isinstance(exc, N8nLifecycleError):
            details = getattr(exc, "details", {})
            blockers = [str(item) for item in list(details.get("blockers") or [])[:32]]
            return HTTPException(
                status_code=409,
                detail=error_payload(
                    getattr(exc, "code", "N8N_LIFECYCLE_ERROR"),
                    "n8n 無法在通過隔離與擁有權驗證前執行。",
                    detail=",".join(blockers) if blockers else None,
                    recoverable=True,
                ),
            )
        return HTTPException(
            status_code=500,
            detail=error_payload(
                "N8N_LIFECYCLE_ERROR",
                "n8n 生命週期操作失敗。",
                recoverable=True,
            ),
        )

    @router.get("/api/integrations/n8n/status")
    def get_status(request: Request):
        require_local(request)
        return status_payload(probe_node=True)

    @router.post("/api/integrations/n8n/start")
    def start(request: Request):
        require_local(request)
        require_n8n_extension()
        try:
            lifecycle.start()
            return status_payload(probe_node=False)
        except Exception as exc:
            raise lifecycle_failure(exc) from exc

    @router.post("/api/integrations/n8n/stop")
    def stop(request: Request):
        require_local(request)
        require_n8n_extension()
        try:
            lifecycle.stop()
            if on_stop is not None:
                on_stop()
            return status_payload(probe_node=False)
        except Exception as exc:
            raise lifecycle_failure(exc) from exc

    @router.post("/api/integrations/n8n/restart")
    def restart(request: Request):
        require_local(request)
        require_n8n_extension()
        try:
            lifecycle.stop()
            if on_stop is not None:
                on_stop()
            lifecycle.start()
            return status_payload(probe_node=False)
        except Exception as exc:
            raise lifecycle_failure(exc) from exc

    @router.get("/api/integrations/n8n/events")
    async def events(request: Request):
        require_local(request)

        async def stream():
            previous = ""
            while not await request.is_disconnected():
                try:
                    payload = status_payload(probe_node=False)
                    encoded = json.dumps(
                        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    if encoded != previous:
                        previous = encoded
                        yield f"event: status\ndata: {encoded}\n\n"
                    else:
                        yield ": keepalive\n\n"
                except Exception:
                    safe = json.dumps(
                        {"state": "failed", "reason": "status_unavailable"},
                        separators=(",", ":"),
                    )
                    yield f"event: status\ndata: {safe}\n\n"
                await asyncio.sleep(5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
            },
        )

    return router


__all__ = ["build_n8n_runtime_router"]
