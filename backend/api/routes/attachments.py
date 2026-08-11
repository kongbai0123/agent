"""Temporary user context and image attachment routes for chat."""

from __future__ import annotations

import base64
import os
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from api.schemas.attachments import AttachmentRequest


def build_attachments_router(
    *,
    database: Any,
    error_payload: Callable[..., Dict[str, Any]],
    create_id: Callable[[str], str],
    session_project_id: Callable[[Optional[str]], Optional[str]],
    project_imports_dir: Callable[..., Any],
    project_attachments_dir: Callable[..., Any],
    extract_pdf_text: Callable[[str], str],
    sync_session_archive: Callable[[str], bool],
) -> APIRouter:
    router = APIRouter(tags=["attachments"])

    @router.post("/api/temp-contexts")
    @router.post("/api/documents/parse-temp")
    async def parse_temp_file(
        file: UploadFile = File(...),
        session_id: Optional[str] = Form(None),
    ):
        filename = os.path.basename(file.filename or "temporary_context")
        if session_id and not database.get_session(session_id):
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "SESSION_NOT_FOUND", "Session was not found.", recoverable=False
                ),
            )
        temp_dir = project_imports_dir(
            session_id, session_project_id(session_id)
        ) / "temporary"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = temp_dir / filename
        try:
            with temp_path.open("wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            if filename.lower().endswith(".pdf"):
                text = extract_pdf_text(str(temp_path))
            else:
                try:
                    text = temp_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    text = temp_path.read_text(encoding="gbk", errors="ignore")
            context_id = create_id("tmpctx")
            chunk_count = max(1, (len(text) + 1199) // 1200) if text else 0
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
            database.save_temporary_context(
                context_id, session_id, filename, text, chunk_count, expires_at
            )
            return {
                "success": True,
                "temporary_context_id": context_id,
                "filename": filename,
                "chunk_count": chunk_count,
                "expires_at": expires_at,
                "text": text,
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=error_payload(
                    "DOCUMENT_PARSE_FAILED",
                    "Failed to parse temporary context.",
                    str(exc),
                ),
            ) from exc
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @router.get("/api/temp-contexts/{temporary_context_id}")
    def get_temp_context(temporary_context_id: str):
        context = database.get_temporary_context(temporary_context_id)
        if not context:
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "TEMP_CONTEXT_NOT_FOUND",
                    "Temporary context not found.",
                    recoverable=False,
                ),
            )
        return context

    @router.delete("/api/temp-contexts/{temporary_context_id}")
    def delete_temp_context(temporary_context_id: str):
        return {"success": database.delete_temporary_context(temporary_context_id)}

    @router.post("/api/attachments")
    def create_attachment(request: AttachmentRequest):
        try:
            raw = request.data.split(",", 1)[1] if "," in request.data else request.data
            data = base64.b64decode(raw, validate=True)
            attachment_id = create_id("att")
            extension = (request.mime_type.split("/")[-1] or "bin").replace("jpeg", "jpg")
            filename = request.filename or f"{attachment_id}.{extension}"
            project_id = session_project_id(request.session_id)
            path = project_attachments_dir(request.session_id, project_id) / (
                f"{attachment_id}_{os.path.basename(filename)}"
            )
            path.write_bytes(data)
            database.save_attachment(
                attachment_id,
                request.session_id,
                filename,
                request.mime_type,
                str(path),
                len(data),
                project_id=project_id,
            )
            if request.session_id:
                sync_session_archive(request.session_id)
            return {
                "attachment_id": attachment_id,
                "mime_type": request.mime_type,
                "size_bytes": len(data),
                "width": None,
                "height": None,
            }
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=error_payload(
                    "UNSUPPORTED_ATTACHMENT",
                    "Attachment could not be decoded.",
                    str(exc),
                ),
            ) from exc

    @router.get("/api/attachments/{attachment_id}")
    def get_attachment(attachment_id: str):
        attachment = database.get_attachment(attachment_id)
        if not attachment:
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "ATTACHMENT_NOT_FOUND",
                    "Attachment not found.",
                    recoverable=False,
                ),
            )
        return attachment

    return router
