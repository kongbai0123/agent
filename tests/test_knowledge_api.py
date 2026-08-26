from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from api.routes.knowledge import build_knowledge_router  # noqa: E402
from project_knowledge import ProjectKnowledgeService  # noqa: E402
from project_knowledge import KnowledgeAdapterError  # noqa: E402
from semantic_retrieval import SemanticConsentRequired  # noqa: E402


def _error_payload(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def _client(
    tmp_path: Path,
    *,
    service=None,
    extract_pdf_text=None,
    pdf_page_counter=None,
    max_pdf_pages: int = 200,
    max_pdf_text_bytes: int = 8 * 1024 * 1024,
    pdf_extraction_timeout_seconds: float = 30.0,
    settings_loader=None,
    semantic_consent_proposal_factory=None,
) -> TestClient:
    service = service or ProjectKnowledgeService(
        tmp_path / "knowledge.sqlite3", chunk_chars=128, overlap_chars=16
    )
    projects = {"project-a": {"id": "project-a"}, "project-b": {"id": "project-b"}}
    app = FastAPI()
    app.include_router(
        build_knowledge_router(
            service=service,
            require_local=lambda _request: None,
            require_project=projects.get,
            extract_pdf_text=extract_pdf_text or (lambda _path: "PDF 專案操作手冊內容"),
            pdf_page_counter=pdf_page_counter or (lambda _path: 1),
            max_pdf_pages=max_pdf_pages,
            max_pdf_text_bytes=max_pdf_text_bytes,
            pdf_extraction_timeout_seconds=pdf_extraction_timeout_seconds,
            temporary_root=tmp_path / "temporary",
            error_payload=_error_payload,
            settings_loader=settings_loader,
            semantic_consent_proposal_factory=semantic_consent_proposal_factory,
        )
    )
    return TestClient(app)


class _RecordingKnowledgeService(ProjectKnowledgeService):
    def __init__(self, path: Path) -> None:
        super().__init__(path, chunk_chars=128, overlap_chars=16)
        self.import_authority: dict[str, Any] = {}
        self.retrieve_authority: dict[str, Any] = {}

    def import_documents(self, **kwargs):
        self.import_authority = {
            key: kwargs.get(key)
            for key in (
                "run_id",
                "consent_proposal_id",
                "requested_model",
                "budget_override_id",
            )
        }
        return super().import_documents(**kwargs)

    def retrieve(self, **kwargs):
        self.retrieve_authority = {
            key: kwargs.get(key)
            for key in (
                "run_id",
                "consent_proposal_id",
                "requested_model",
                "budget_override_id",
            )
        }
        return super().retrieve(**kwargs)


class _ConsentRequiredKnowledgeService(ProjectKnowledgeService):
    @staticmethod
    def _raise_consent():
        cause = SemanticConsentRequired(
            "approval required",
            provider_id="semantic",
            model_reference="semantic::embed-model",
        )
        wrapped = KnowledgeAdapterError(
            "approval required",
            code=cause.code,
            status_code=cause.status_code,
        )
        raise wrapped from cause

    def retrieve(self, **_kwargs):
        self._raise_consent()


def test_knowledge_api_returns_model_bound_semantic_consent_proposal(tmp_path: Path) -> None:
    service = _ConsentRequiredKnowledgeService(tmp_path / "consent.sqlite3")
    proposals: list[dict[str, Any]] = []

    def create_proposal(**kwargs):
        proposals.append(dict(kwargs))
        return {
            "proposal_id": "mrp_" + "a" * 32,
            "provider": "semantic",
            "selected_model": "semantic::embed-model",
            "project_id": kwargs["project_id"],
            "run_id": kwargs["run_id"],
        }

    client = _client(
        tmp_path,
        service=service,
        semantic_consent_proposal_factory=create_proposal,
    )
    response = client.post(
        "/api/knowledge/retrieve",
        json={
            "project_id": "project-a",
            "query": "部署方式",
            "run_id": "run_" + "b" * 32,
            "requested_model": "knowledge-workspace",
        },
    )

    assert response.status_code == 409
    payload = response.json()["detail"]
    assert payload["code"] == "MODEL_DATA_CONSENT_REQUIRED"
    assert payload["detail"]["proposal_id"].startswith("mrp_")
    assert proposals[0]["project_id"] == "project-a"
    assert proposals[0]["run_id"].startswith("run_")
    assert isinstance(proposals[0]["error"], KnowledgeAdapterError)


def test_project_knowledge_api_import_retrieve_preview_and_delete(tmp_path: Path) -> None:
    client = _client(tmp_path)

    imported = client.post(
        "/api/knowledge/documents",
        data={"project_id": "project-a"},
        files={"files": ("guide.md", "n8n 工作流程需要先通過權限核准。" * 20, "text/markdown")},
    )
    assert imported.status_code == 200
    document = imported.json()["documents"][0]
    document_id = document["document_id"]

    status = client.get("/api/knowledge/status", params={"project_id": "project-a"})
    assert status.status_code == 200
    assert status.json()["document_count"] == 1
    assert status.json()["chunk_count"] >= 1
    assert status.json()["current_adapter_chunk_count"] == status.json()["chunk_count"]
    assert status.json()["reindex_required"] is False
    assert status.json()["embedding_adapter"].startswith("local-hash-embedding-")

    listed = client.get("/api/knowledge/documents", params={"project_id": "project-a"})
    assert listed.status_code == 200
    assert listed.json()["documents"][0]["document_id"] == document_id

    preview = client.get(
        f"/api/knowledge/documents/{document_id}/chunks",
        params={"project_id": "project-a"},
    )
    assert preview.status_code == 200
    assert preview.json()["chunks"][0]["text"]
    assert preview.json()["total_chunks"] == len(preview.json()["chunks"])
    assert preview.json()["truncated"] is False

    retrieved = client.post(
        "/api/knowledge/retrieve",
        json={"project_id": "project-a", "query": "權限核准", "top_k": 3},
    )
    assert retrieved.status_code == 200
    assert retrieved.json()["results"]
    assert all(
        item["citation"]["project_id"] == "project-a"
        for item in retrieved.json()["results"]
    )

    deleted = client.delete(
        f"/api/knowledge/documents/{document_id}",
        params={"project_id": "project-a"},
    )
    assert deleted.status_code == 200
    assert client.get(
        "/api/knowledge/status", params={"project_id": "project-a"}
    ).json()["document_count"] == 0


def test_knowledge_api_forwards_governance_authority_to_semantic_adapters(
    tmp_path: Path,
) -> None:
    service = _RecordingKnowledgeService(tmp_path / "knowledge.sqlite3")
    client = _client(tmp_path, service=service)
    authority = {
        "run_id": "run_api_semantic_12345678",
        "consent_proposal_id": "mrp_" + ("a" * 32),
        "requested_model": "provider::semantic-model",
        "budget_override_id": "mbo_" + ("b" * 32),
    }

    imported = client.post(
        "/api/knowledge/documents",
        data={"project_id": "project-a", **authority},
        files={"files": ("guide.md", "語意治理測試內容", "text/markdown")},
    )
    retrieved = client.post(
        "/api/knowledge/retrieve",
        json={
            "project_id": "project-a",
            "query": "語意治理",
            **authority,
        },
    )

    assert imported.status_code == 200
    assert retrieved.status_code == 200
    assert service.import_authority == authority
    assert service.retrieve_authority == authority


def test_retrieval_test_applies_the_same_minimum_score_policy_as_chat(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path,
        settings_loader=lambda: {"rag_rerank_threshold": 1.0},
    )
    imported = client.post(
        "/api/knowledge/documents",
        data={"project_id": "project-a"},
        files={
            "files": (
                "guide.txt",
                "這是一份內容很長的權限核准與工作流程說明。" * 12,
                "text/plain",
            )
        },
    )
    assert imported.status_code == 200

    retrieved = client.post(
        "/api/knowledge/retrieve",
        json={"project_id": "project-a", "query": "權限核准", "top_k": 3},
    )

    assert retrieved.status_code == 200
    assert retrieved.json()["minimum_score"] == 1.0
    assert retrieved.json()["results"] == []

def test_project_knowledge_api_enforces_project_scope_and_upload_limits(tmp_path: Path) -> None:
    client = _client(tmp_path)
    imported = client.post(
        "/api/knowledge/documents",
        data={"project_id": "project-a"},
        files={"files": ("private.txt", "僅供 A 專案使用", "text/plain")},
    )
    document_id = imported.json()["documents"][0]["document_id"]

    cross_project = client.get(
        f"/api/knowledge/documents/{document_id}/chunks",
        params={"project_id": "project-b"},
    )
    assert cross_project.status_code == 404
    assert cross_project.json()["detail"]["code"] == "KNOWLEDGE_DOCUMENT_NOT_FOUND"

    unknown_project = client.get(
        "/api/knowledge/status", params={"project_id": "missing"}
    )
    assert unknown_project.status_code == 404
    assert unknown_project.json()["detail"]["code"] == "PROJECT_NOT_FOUND"

    unsupported = client.post(
        "/api/knowledge/documents",
        data={"project_id": "project-a"},
        files={"files": ("program.exe", b"MZ", "application/octet-stream")},
    )
    assert unsupported.status_code == 415
    assert unsupported.json()["detail"]["code"] == "UNSUPPORTED_KNOWLEDGE_DOCUMENT"


def test_project_knowledge_api_pdf_uses_bounded_temporary_extraction(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/api/knowledge/documents",
        data={"project_id": "project-a"},
        files={"files": ("manual.pdf", b"not-a-real-pdf", "application/pdf")},
    )
    assert response.status_code == 200
    assert response.json()["documents"][0]["source_id"] == "manual.pdf"
    temporary = tmp_path / "temporary"
    assert temporary.exists()
    assert not list(temporary.iterdir())


def test_project_knowledge_api_validates_entire_batch_before_writing(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/knowledge/documents",
        data={"project_id": "project-a"},
        files=[
            ("files", ("valid.md", "這份文件本來可以匯入", "text/markdown")),
            ("files", ("blocked.exe", b"MZ", "application/octet-stream")),
        ],
    )

    assert response.status_code == 415
    listed = client.get("/api/knowledge/documents", params={"project_id": "project-a"})
    assert listed.json()["documents"] == []


def test_project_knowledge_api_pdf_page_and_extracted_text_limits_are_atomic(
    tmp_path: Path,
) -> None:
    extraction_calls: list[str] = []

    def should_not_extract(path: str) -> str:
        extraction_calls.append(path)
        return "unreachable"

    page_limited = _client(
        tmp_path / "pages",
        extract_pdf_text=should_not_extract,
        pdf_page_counter=lambda _path: 201,
        max_pdf_pages=200,
    )
    page_response = page_limited.post(
        "/api/knowledge/documents",
        data={"project_id": "project-a"},
        files=[
            ("files", ("valid.md", "先解析但不可提交", "text/markdown")),
            ("files", ("too-many-pages.pdf", b"pdf", "application/pdf")),
        ],
    )
    assert page_response.status_code == 413
    assert page_response.json()["detail"]["code"] == "KNOWLEDGE_PDF_PAGE_LIMIT"
    assert extraction_calls == []
    assert page_limited.get(
        "/api/knowledge/documents", params={"project_id": "project-a"}
    ).json()["documents"] == []

    text_limited = _client(
        tmp_path / "text",
        extract_pdf_text=lambda _path: "字" * 20,
        max_pdf_text_bytes=32,
    )
    text_response = text_limited.post(
        "/api/knowledge/documents",
        data={"project_id": "project-a"},
        files={"files": ("expanded.pdf", b"pdf", "application/pdf")},
    )
    assert text_response.status_code == 413
    assert text_response.json()["detail"]["code"] == "KNOWLEDGE_PDF_TEXT_LIMIT"
    assert text_limited.get(
        "/api/knowledge/documents", params={"project_id": "project-a"}
    ).json()["documents"] == []


def test_project_knowledge_api_pdf_extraction_timeout_is_bounded_and_cleans_up(
    tmp_path: Path,
) -> None:
    def slow_extract(_path: str) -> str:
        time.sleep(0.08)
        return "too late"

    client = _client(
        tmp_path,
        extract_pdf_text=slow_extract,
        pdf_extraction_timeout_seconds=0.01,
    )
    started = time.monotonic()
    response = client.post(
        "/api/knowledge/documents",
        data={"project_id": "project-a"},
        files={"files": ("slow.pdf", b"pdf", "application/pdf")},
    )

    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "KNOWLEDGE_PDF_EXTRACTION_TIMEOUT"
    assert time.monotonic() - started < 0.5
    time.sleep(0.1)
    assert not list((tmp_path / "temporary").iterdir())
    assert client.get(
        "/api/knowledge/documents", params={"project_id": "project-a"}
    ).json()["documents"] == []
