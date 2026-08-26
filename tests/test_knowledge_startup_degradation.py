from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import app as app_module  # noqa: E402
from api.routes.knowledge import build_knowledge_router  # noqa: E402
from project_knowledge import ProjectKnowledgeError  # noqa: E402


def test_initializer_degrades_when_knowledge_store_cannot_open(tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("database path is unavailable")

    recorded = []
    monkeypatch.setattr(app_module, "ProjectKnowledgeService", fail)
    monkeypatch.setattr(
        app_module,
        "degraded",
        lambda component, action, error, **_kwargs: recorded.append(
            (component, action, type(error).__name__)
        ),
    )

    service, available = app_module._initialize_project_knowledge_service(
        tmp_path / "missing" / "knowledge.sqlite3",
        chunk_chars=600,
        overlap_chars=120,
    )

    assert available is False
    assert isinstance(service, app_module._UnavailableProjectKnowledgeService)
    assert recorded == [
        ("project_knowledge", "initialize project knowledge service", "OSError")
    ]


def test_unavailable_knowledge_api_returns_recoverable_503(tmp_path):
    service = app_module._UnavailableProjectKnowledgeService("test failure")
    api = FastAPI()
    api.include_router(
        build_knowledge_router(
            service=service,
            require_local=lambda _request: None,
            require_project=lambda project_id: {"id": project_id},
            extract_pdf_text=lambda _path: "",
            temporary_root=tmp_path,
            error_payload=app_module.knowledge_error_payload,
        )
    )

    with TestClient(api) as client:
        response = client.get("/api/knowledge/status?project_id=project-one")
        import_response = client.post(
            "/api/knowledge/documents",
            data={"project_id": "project-one"},
            files={"files": ("guide.txt", b"hello", "text/plain")},
        )
        clear_response = client.delete(
            "/api/knowledge?project_id=project-one"
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "KNOWLEDGE_SERVICE_UNAVAILABLE"
    assert response.json()["detail"]["recoverable"] is True
    assert import_response.status_code == 503
    assert import_response.json()["detail"]["code"] == "KNOWLEDGE_SERVICE_UNAVAILABLE"
    assert clear_response.status_code == 503
    assert clear_response.json()["detail"]["code"] == "KNOWLEDGE_SERVICE_UNAVAILABLE"


def test_unavailable_knowledge_cleanup_blocks_project_deletion(monkeypatch):
    class BrokenKnowledgeService:
        def clear_project(self, **_kwargs):
            raise ProjectKnowledgeError(
                "index unavailable",
                code="KNOWLEDGE_SERVICE_UNAVAILABLE",
                status_code=503,
            )

    recorded = []
    monkeypatch.setattr(app_module, "knowledge_service", BrokenKnowledgeService())
    monkeypatch.setattr(
        app_module,
        "degraded",
        lambda component, action, error, **fields: recorded.append(
            (component, action, fields.get("project_id"))
        ),
    )

    with pytest.raises(ProjectKnowledgeError) as raised:
        app_module.clear_project_knowledge_for_delete("project-one")

    assert raised.value.code == "KNOWLEDGE_SERVICE_UNAVAILABLE"
    assert raised.value.status_code == 503
    assert recorded == [
        (
            "project_knowledge",
            "clear project knowledge during project deletion",
            "project-one",
        )
    ]


def test_unavailable_service_raises_stable_recoverable_error_contract():
    service = app_module._UnavailableProjectKnowledgeService("test failure")

    with pytest.raises(ProjectKnowledgeError) as raised:
        service.retrieve(project_id="project-one", query="hello")

    assert raised.value.code == "KNOWLEDGE_SERVICE_UNAVAILABLE"
    assert raised.value.status_code == 503


def test_project_delete_returns_503_and_keeps_metadata_when_knowledge_cleanup_fails(
    tmp_path, monkeypatch
):
    project_id = f"project_{uuid.uuid4().hex}"
    app_module.database.create_project(project_id, "保留專案", str(tmp_path))
    monkeypatch.setattr(
        app_module,
        "knowledge_service",
        app_module._UnavailableProjectKnowledgeService("test failure"),
    )

    try:
        with TestClient(app_module.app) as client:
            assert client.get("/").status_code == 200
            response = client.delete(f"/api/projects/{project_id}")

        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "KNOWLEDGE_SERVICE_UNAVAILABLE"
        assert app_module.database.get_project(project_id) is not None
    finally:
        app_module.database.delete_project(project_id)
