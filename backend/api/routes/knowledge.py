"""Project-scoped knowledge ingestion and retrieval API."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from project_knowledge import (
    MAX_DOCUMENT_BYTES,
    ProjectKnowledgeError,
    ProjectKnowledgeService,
)


_TEXT_SUFFIXES = frozenset({".txt", ".md", ".py", ".js", ".html", ".css", ".json", ".csv"})
_ALLOWED_SUFFIXES = _TEXT_SUFFIXES | {".pdf"}
_MAX_FILES_PER_IMPORT = 10
MAX_PDF_PAGES = 200
MAX_EXTRACTED_TEXT_BYTES = MAX_DOCUMENT_BYTES
PDF_EXTRACTION_TIMEOUT_SECONDS = 30.0


def _default_pdf_page_count(file_path: str) -> int:
    # Keep the optional PDF dependency lazy so text-only knowledge features
    # remain available in minimal/test installations.
    from pypdf import PdfReader

    reader = PdfReader(file_path)
    if bool(getattr(reader, "is_encrypted", False)):
        raise ValueError("Encrypted PDFs are not accepted.")
    return len(reader.pages)


class KnowledgeQuery(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=8192)
    top_k: int = Field(default=5, ge=1, le=20)
    candidate_limit: int = Field(default=40, ge=1, le=100)
    run_id: str = Field(default="", max_length=160)
    consent_proposal_id: str = Field(default="", max_length=160)
    requested_model: str = Field(default="", max_length=240)
    budget_override_id: str = Field(default="", max_length=160)


def build_knowledge_router(
    *,
    service: ProjectKnowledgeService,
    require_local: Callable[[Request], None],
    require_project: Callable[[str], Any],
    extract_pdf_text: Callable[[str], str],
    temporary_root: Path,
    error_payload: Callable[..., Dict[str, Any]],
    pdf_page_counter: Callable[[str], int] | None = None,
    max_pdf_pages: int = MAX_PDF_PAGES,
    max_pdf_text_bytes: int = MAX_EXTRACTED_TEXT_BYTES,
    pdf_extraction_timeout_seconds: float = PDF_EXTRACTION_TIMEOUT_SECONDS,
    settings_loader: Callable[[], Dict[str, Any]] | None = None,
    semantic_consent_proposal_factory: Callable[..., Dict[str, Any] | None]
    | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
    temporary_root = Path(temporary_root).resolve()
    temporary_root.mkdir(parents=True, exist_ok=True)
    page_counter = pdf_page_counter or _default_pdf_page_count
    max_pdf_pages = int(max_pdf_pages)
    max_pdf_text_bytes = int(max_pdf_text_bytes)
    pdf_extraction_timeout_seconds = float(pdf_extraction_timeout_seconds)
    import_semaphore = asyncio.Semaphore(2)
    pdf_semaphore = asyncio.Semaphore(2)
    if max_pdf_pages < 1 or max_pdf_text_bytes < 1 or pdf_extraction_timeout_seconds <= 0:
        raise ValueError("PDF extraction limits must be positive.")

    def checked_project(project_id: str) -> str:
        value = str(project_id or "").strip()
        if not value or require_project(value) is None:
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "PROJECT_NOT_FOUND", "找不到指定的專案。", recoverable=False
                ),
            )
        return value

    def translated_error(
        exc: ProjectKnowledgeError,
        *,
        project_id: str = "",
        run_id: str = "",
        requested_model: str = "",
    ) -> HTTPException:
        messages = {
            "INVALID_KNOWLEDGE_SCOPE": "知識庫的專案範圍無效。",
            "INVALID_KNOWLEDGE_DOCUMENT": "文件內容無效或格式不受支援。",
            "KNOWLEDGE_INPUT_TOO_LARGE": "文件超過知識庫允許的大小。",
            "INVALID_KNOWLEDGE_QUERY": "檢索問題或數量設定無效。",
            "KNOWLEDGE_DOCUMENT_NOT_FOUND": "找不到指定的知識庫文件。",
            "KNOWLEDGE_ADAPTER_FAILED": "嵌入或重排處理失敗。",
            "SEMANTIC_DATA_CONSENT_REQUIRED": "將專案文件送往外部語意模型前，需要先取得資料傳送同意。",
            "SEMANTIC_PROVIDER_DISABLED": "語意模型尚未在目前專案啟用。",
            "SEMANTIC_PROVIDER_NOT_VERIFIED": "語意模型連線尚未完成驗證。",
            "SEMANTIC_PROVIDER_CREDENTIAL_MISSING": "語意模型憑證不存在或已失效。",
            "SEMANTIC_PROVIDER_UNREACHABLE": "目前無法連線至語意模型。",
            "SEMANTIC_GOVERNANCE_UNAVAILABLE": "語意模型治理服務暫時無法使用。",
            "KNOWLEDGE_INDEX_TOO_LARGE": "知識庫索引超過本機安全掃描上限。",
            "KNOWLEDGE_PROJECT_CHUNK_LIMIT": "匯入後會超過此專案的知識片段上限，請先移除不需要的文件。",
            "KNOWLEDGE_BATCH_CONFLICT": "同一批文件含有重複的來源或文件識別碼。",
            "KNOWLEDGE_SOURCE_CONFLICT": "文件來源已由同一專案中的其他文件使用。",
            "KNOWLEDGE_IMPORT_CONFLICT": "匯入期間文件已被更新，請重新整理後再試一次。",
            "KNOWLEDGE_IMPORT_FAILED": "知識文件未能完整寫入；本批資料均未提交。",
            "KNOWLEDGE_PDF_PAGE_LIMIT": "PDF 頁數超過知識庫允許的上限。",
            "KNOWLEDGE_PDF_TEXT_LIMIT": "PDF 解壓後的文字超過知識庫允許的上限。",
            "KNOWLEDGE_PDF_EXTRACTION_TIMEOUT": "PDF 解析逾時，未匯入任何文件。",
            "KNOWLEDGE_PDF_EXTRACTION_FAILED": "PDF 無法解析；請確認檔案未損毀或受密碼保護。",
        }
        code = str(getattr(exc, "code", "PROJECT_KNOWLEDGE_ERROR"))
        detail: Any = None
        if (
            code == "SEMANTIC_DATA_CONSENT_REQUIRED"
            and semantic_consent_proposal_factory is not None
            and project_id
            and run_id
            and requested_model
        ):
            detail = semantic_consent_proposal_factory(
                project_id=project_id,
                run_id=run_id,
                requested_model=requested_model,
                error=exc,
            )
            if detail:
                code = "MODEL_DATA_CONSENT_REQUIRED"
                messages[code] = (
                    "將專案文件片段傳送到雲端語意模型前，需要先取得明確同意。"
                )
        return HTTPException(
            status_code=int(getattr(exc, "status_code", 400)),
            detail=error_payload(
                code,
                messages.get(code, "知識庫作業失敗。"),
                detail=detail,
                recoverable=(
                    int(getattr(exc, "status_code", 400)) < 500
                    or str(getattr(exc, "code", ""))
                    in {"KNOWLEDGE_ADAPTER_FAILED", "KNOWLEDGE_IMPORT_FAILED", "KNOWLEDGE_PDF_EXTRACTION_TIMEOUT"}
                ),
            ),
        )

    def extract_pdf_bounded(temporary_path: Path) -> str:
        """Run inside a worker thread and always reclaim its temporary file."""

        try:
            page_count = int(page_counter(str(temporary_path)))
            if page_count < 1:
                raise ProjectKnowledgeError(
                    "PDF contains no pages.",
                    code="KNOWLEDGE_PDF_EXTRACTION_FAILED",
                    status_code=422,
                )
            if page_count > max_pdf_pages:
                raise ProjectKnowledgeError(
                    "PDF exceeds the page limit.",
                    code="KNOWLEDGE_PDF_PAGE_LIMIT",
                    status_code=413,
                )
            content = extract_pdf_text(str(temporary_path))
            if not isinstance(content, str) or not content.strip():
                raise ProjectKnowledgeError(
                    "PDF did not contain extractable text.",
                    code="KNOWLEDGE_PDF_EXTRACTION_FAILED",
                    status_code=422,
                )
            if len(content.encode("utf-8")) > max_pdf_text_bytes:
                raise ProjectKnowledgeError(
                    "Extracted PDF text exceeds the byte limit.",
                    code="KNOWLEDGE_PDF_TEXT_LIMIT",
                    status_code=413,
                )
            return content
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                # A timed-out parser may still own a Windows file handle.  Its
                # worker executes this same cleanup again when it eventually exits.
                pass

    async def extract_pdf_with_bounded_worker(temporary_path: Path) -> str:
        """Bound parser workers even after a request-level timeout fires."""

        await pdf_semaphore.acquire()
        loop = asyncio.get_running_loop()
        worker = loop.run_in_executor(None, extract_pdf_bounded, temporary_path)

        def release_worker_slot(_future: Any) -> None:
            try:
                loop.call_soon_threadsafe(pdf_semaphore.release)
            except RuntimeError:
                # The application event loop may already be closing. The
                # worker still owns temporary-file cleanup in its finally.
                pass

        worker.add_done_callback(release_worker_slot)
        return await asyncio.wait_for(
            asyncio.shield(worker),
            timeout=pdf_extraction_timeout_seconds,
        )

    @router.get("/status")
    def status(project_id: str, request: Request):
        require_local(request)
        try:
            return service.status(project_id=checked_project(project_id))
        except ProjectKnowledgeError as exc:
            raise translated_error(exc) from exc

    @router.get("/documents")
    def documents(project_id: str, request: Request):
        require_local(request)
        try:
            return {
                "success": True,
                "documents": service.list_documents(
                    project_id=checked_project(project_id)
                ),
            }
        except ProjectKnowledgeError as exc:
            raise translated_error(exc) from exc

    @router.post("/documents")
    async def import_documents(
        request: Request,
        project_id: str = Form(...),
        files: List[UploadFile] = File(...),
        run_id: str = Form(""),
        consent_proposal_id: str = Form(""),
        requested_model: str = Form(""),
        budget_override_id: str = Form(""),
    ):
        require_local(request)
        project = checked_project(project_id)
        if not files or len(files) > _MAX_FILES_PER_IMPORT:
            raise HTTPException(
                status_code=400,
                detail=error_payload(
                    "INVALID_KNOWLEDGE_DOCUMENT",
                    f"一次只能匯入 1 到 {_MAX_FILES_PER_IMPORT} 份文件。",
                    recoverable=True,
                ),
            )
        prepared_documents: list[dict[str, Any]] = []
        for upload in files:
            filename = os.path.basename(str(upload.filename or "")).strip()
            suffix = Path(filename).suffix.casefold()
            if not filename or suffix not in _ALLOWED_SUFFIXES:
                raise HTTPException(
                    status_code=415,
                    detail=error_payload(
                        "UNSUPPORTED_KNOWLEDGE_DOCUMENT",
                        f"不支援文件「{filename or '未命名文件'}」的格式。",
                        recoverable=True,
                    ),
                )
            raw = await upload.read(MAX_DOCUMENT_BYTES + 1)
            if not raw or len(raw) > MAX_DOCUMENT_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=error_payload(
                        "KNOWLEDGE_INPUT_TOO_LARGE",
                        f"文件「{filename}」為空白或超過 8 MiB。",
                        recoverable=True,
                    ),
                )
            if suffix == ".pdf":
                temporary_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        prefix="knowledge-", suffix=".pdf", dir=temporary_root, delete=False
                    ) as handle:
                        handle.write(raw)
                        temporary_path = Path(handle.name)
                    content = await extract_pdf_with_bounded_worker(temporary_path)
                except asyncio.TimeoutError as exc:
                    raise translated_error(
                        ProjectKnowledgeError(
                            "PDF extraction timed out.",
                            code="KNOWLEDGE_PDF_EXTRACTION_TIMEOUT",
                            status_code=504,
                        )
                    ) from exc
                except ProjectKnowledgeError as exc:
                    raise translated_error(exc) from exc
                except Exception as exc:
                    raise translated_error(
                        ProjectKnowledgeError(
                            "PDF extraction failed.",
                            code="KNOWLEDGE_PDF_EXTRACTION_FAILED",
                            status_code=422,
                        )
                    ) from exc
                finally:
                    if temporary_path is not None:
                        try:
                            temporary_path.unlink(missing_ok=True)
                        except OSError:
                            pass
            else:
                try:
                    content = raw.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise HTTPException(
                        status_code=415,
                        detail=error_payload(
                            "UNSUPPORTED_KNOWLEDGE_ENCODING",
                            f"文件「{filename}」不是 UTF-8 文字。",
                            recoverable=True,
                        ),
                    ) from exc
            prepared_documents.append(
                {
                    "source_id": filename,
                    "title": filename,
                    "content": content,
                    "metadata": {
                        "filename": filename,
                        "media_type": upload.content_type or "",
                    },
                }
            )
        try:
            async with import_semaphore:
                imported = await asyncio.to_thread(
                    service.import_documents,
                    project_id=project,
                    documents=prepared_documents,
                    run_id=run_id,
                    consent_proposal_id=consent_proposal_id,
                    requested_model=requested_model,
                    budget_override_id=budget_override_id,
                )
        except ProjectKnowledgeError as exc:
            raise translated_error(
                exc,
                project_id=project,
                run_id=run_id,
                requested_model=requested_model,
            ) from exc
        return {"success": True, "documents": imported}

    @router.get("/documents/{document_id}/chunks")
    def chunks(document_id: str, project_id: str, request: Request):
        require_local(request)
        try:
            project = checked_project(project_id)
            items = service.document_chunks(
                project_id=project, document_id=document_id
            )
            total = service.document_chunk_count(
                project_id=project, document_id=document_id
            )
            return {
                "success": True,
                "chunks": items,
                "total_chunks": total,
                "truncated": total > len(items),
            }
        except ProjectKnowledgeError as exc:
            raise translated_error(exc) from exc

    @router.delete("/documents/{document_id}")
    def delete_document(document_id: str, project_id: str, request: Request):
        require_local(request)
        try:
            deleted = service.delete_document(
                project_id=checked_project(project_id), document_id=document_id
            )
        except ProjectKnowledgeError as exc:
            raise translated_error(exc) from exc
        if not deleted:
            raise translated_error(
                ProjectKnowledgeError(
                    "Document was not found.",
                    code="KNOWLEDGE_DOCUMENT_NOT_FOUND",
                    status_code=404,
                )
            )
        return {"success": True, "document_id": document_id}

    @router.delete("")
    def clear(project_id: str, request: Request):
        require_local(request)
        try:
            removed = service.clear_project(project_id=checked_project(project_id))
        except ProjectKnowledgeError as exc:
            raise translated_error(exc) from exc
        return {"success": True, "removed": removed}

    @router.post("/retrieve")
    def retrieve(payload: KnowledgeQuery, request: Request):
        require_local(request)
        project = checked_project(payload.project_id)
        try:
            hits = service.retrieve(
                project_id=project,
                query=payload.query,
                top_k=payload.top_k,
                candidate_limit=max(payload.top_k, payload.candidate_limit),
                run_id=payload.run_id,
                consent_proposal_id=payload.consent_proposal_id,
                requested_model=payload.requested_model,
                budget_override_id=payload.budget_override_id,
            )
        except ProjectKnowledgeError as exc:
            raise translated_error(
                exc,
                project_id=project,
                run_id=payload.run_id,
                requested_model=payload.requested_model,
            ) from exc
        threshold = 0.0
        if settings_loader is not None:
            try:
                threshold = max(
                    0.0,
                    min(
                        1.0,
                        float(
                            (settings_loader() or {}).get(
                                "rag_rerank_threshold", 0.0
                            )
                            or 0.0
                        ),
                    ),
                )
            except (TypeError, ValueError):
                threshold = 0.0
        if threshold > 0:
            hits = [
                hit
                for hit in hits
                if float(hit.get("score") or 0.0) >= threshold
            ]
        return {
            "success": True,
            "project_id": project,
            "embedding_adapter": service.embedding_adapter_id,
            "minimum_score": threshold,
            "results": [
                {
                    "source": hit["citation"]["title"],
                    "content": hit["text"],
                    "score": hit["score"],
                    "document_id": hit["citation"]["document_id"],
                    "chunk_id": hit["citation"]["chunk_id"],
                    "citation": hit["citation"],
                    "retrieval_mode": hit.get("retrieval_mode", "embedding"),
                    "degradation_code": hit.get("degradation_code"),
                }
                for hit in hits
            ],
        }

    return router


__all__ = ["KnowledgeQuery", "build_knowledge_router"]
