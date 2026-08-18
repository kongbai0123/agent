"""Session and conversation-history routes."""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from api.schemas.sessions import (
    CreateSessionRequest,
    PatchSessionRequest,
    ReorderSessionsRequest,
)


def build_sessions_router(
    *,
    database: Any,
    create_id: Callable[[str], str],
    now_iso: Callable[[], str],
    error_payload: Callable[..., Dict[str, Any]],
    ensure_session_folder: Callable[..., Any],
    sync_session_archive: Callable[[str], bool],
    move_session_storage: Callable[[str, Optional[str], Optional[str]], Any],
    archive_session: Callable[[str], Any],
    export_session_zip: Callable[[str], Optional[bytes]],
    has_active_chat_run: Callable[[str], bool],
    session_change_guard: Optional[
        Callable[[str, str, Optional[Dict[str, Any]]], None]
    ] = None,
    session_lifecycle_observer: Optional[
        Callable[[str, Dict[str, Any]], None]
    ] = None,
) -> APIRouter:
    router = APIRouter(tags=["sessions"])
    @router.post("/api/sessions")
    def api_create_session(req: CreateSessionRequest):
        if str(req.mode or "chat").strip().casefold() == "email":
            raise HTTPException(
                status_code=403,
                detail=error_payload(
                    "INTEGRATION_SESSION_RESERVED",
                    "Email integration sessions can only be created by the integration service.",
                    recoverable=False,
                ),
            )
        sid = req.session_id or create_id("sess")
        existed = database.get_session(sid) is not None
        if req.project_id and not database.get_project(req.project_id):
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "PROJECT_NOT_FOUND",
                    "Project was not found.",
                    recoverable=False,
                ),
            )
        database.create_session(
            sid,
            req.title or "New chat",
            req.mode,
            req.model,
            req.project_id,
        )
        ensure_session_folder(sid, req.title or "New chat", req.mode, req.model)
        sync_session_archive(sid)
        if not existed and session_lifecycle_observer is not None:
            try:
                session_lifecycle_observer(
                    "created",
                    {"session_id": sid, "project_id": req.project_id, "model": req.model},
                )
            except Exception:
                pass
        return {
            "session_id": sid,
            "id": sid,
            "title": req.title or "New chat",
            "mode": req.mode,
            "model": req.model,
            "project_id": req.project_id,
            "created_at": now_iso(),
        }
    @router.get("/api/sessions")
    def api_get_sessions(search: Optional[str] = None):
        return {"sessions": database.get_all_sessions(search)}
    @router.post("/api/sessions/reorder")
    def api_reorder_sessions(req: ReorderSessionsRequest):
        if req.project_id and not database.get_project(req.project_id):
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "PROJECT_NOT_FOUND",
                    "Project was not found.",
                    recoverable=False,
                ),
            )
        for session_id in req.session_ids:
            if session_change_guard is not None:
                session_change_guard(
                    session_id,
                    "reorder",
                    {"project_id": req.project_id},
                )
            current = database.get_session(session_id)
            if (
                current
                and current.get("project_id") != req.project_id
                and has_active_chat_run(session_id)
            ):
                raise HTTPException(
                    status_code=409,
                    detail=error_payload(
                        "SESSION_RUN_ACTIVE",
                        "A session cannot move projects while a run is active.",
                        recoverable=True,
                    ),
                )
        if not database.reorder_sessions(req.session_ids, req.project_id):
            raise HTTPException(
                status_code=400,
                detail=error_payload(
                    "INVALID_SESSION_ORDER",
                    "Session order contains duplicate or unknown IDs.",
                ),
            )
        return {
            "success": True,
            "session_ids": req.session_ids,
            "project_id": req.project_id,
        }

    @router.get("/api/sessions/{session_id}/messages")
    def api_get_messages(session_id: str):
        if session_change_guard is not None:
            session_change_guard(session_id, "read_messages", None)
        return {"messages": database.get_messages_by_session(session_id)}

    @router.patch("/api/sessions/{session_id}")
    def api_patch_session(session_id: str, req: PatchSessionRequest):
        existing = database.get_session(session_id)
        if not existing:
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "SESSION_NOT_FOUND",
                    "Session was not found.",
                    recoverable=False,
                ),
            )
        changes = req.model_dump(exclude_unset=True)
        if session_change_guard is not None:
            session_change_guard(session_id, "patch", changes)
        if changes.get("project_id") and not database.get_project(changes["project_id"]):
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "PROJECT_NOT_FOUND",
                    "Project was not found.",
                    recoverable=False,
                ),
            )
        if (
            "project_id" in changes
            and changes["project_id"] != existing.get("project_id")
            and has_active_chat_run(session_id)
        ):
            raise HTTPException(
                status_code=409,
                detail=error_payload(
                    "SESSION_RUN_ACTIVE",
                    "A session cannot move projects while a run is active.",
                    recoverable=True,
                ),
            )
        if not database.update_session_metadata(session_id, **changes):
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "SESSION_NOT_FOUND",
                    "Session was not found.",
                    recoverable=False,
                ),
            )
        if "project_id" in changes and changes["project_id"] != existing.get("project_id"):
            move_session_storage(
                session_id,
                existing.get("project_id"),
                changes["project_id"],
            )
        sync_session_archive(session_id)
        return {"success": True, "session_id": session_id}

    @router.delete("/api/sessions/{session_id}")
    def api_delete_session(session_id: str):
        existing = database.get_session(session_id)
        if session_change_guard is not None:
            session_change_guard(session_id, "delete", None)
        sync_session_archive(session_id)
        archived_to = archive_session(session_id)
        deleted = database.delete_session(session_id)
        if deleted and existing and session_lifecycle_observer is not None:
            try:
                session_lifecycle_observer(
                    "deleted",
                    {
                        "session_id": session_id,
                        "project_id": existing.get("project_id"),
                        "model": existing.get("model"),
                    },
                )
            except Exception:
                pass
        return {
            "success": deleted,
            "archived_to": str(archived_to) if archived_to else None,
        }

    @router.get("/api/sessions/{session_id}/export.zip")
    def api_export_session(session_id: str):
        if session_change_guard is not None:
            session_change_guard(session_id, "export", None)
        content = export_session_zip(session_id)
        if content is None:
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "SESSION_NOT_FOUND",
                    "Session was not found.",
                    recoverable=False,
                ),
            )
        safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
        return Response(
            content,
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="conversation-{safe_name}.zip"'
                )
            },
        )

    return router
