from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.responses import StreamingResponse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import app as app_module  # noqa: E402
from external_agent_api import ExternalAgentApiError  # noqa: E402


def test_external_submit_uses_a_fresh_project_bound_session(monkeypatch):
    created = {}
    received = {}
    app_module.external_api_active_runs.clear()
    app_module.external_api_run_sessions.clear()
    app_module.external_api_background_tasks.clear()

    monkeypatch.setattr(
        app_module.database,
        "get_project",
        lambda project_id: {"id": project_id} if project_id == "project-a" else None,
    )
    monkeypatch.setattr(
        app_module.database,
        "create_session",
        lambda session_id, **kwargs: created.update(
            {"session_id": session_id, **kwargs}
        )
        or session_id,
    )
    monkeypatch.setattr(app_module.database, "get_run", lambda _run_id: None)
    monkeypatch.setattr(app_module.database, "get_messages_by_session", lambda _sid: [])
    monkeypatch.setattr(app_module.database, "delete_session", lambda _sid: True)
    monkeypatch.setattr(
        app_module,
        "load_settings",
        lambda: {"default_chat_model": "local-chat"},
    )

    async def fake_chat(request):
        received["request"] = request

        async def stream():
            yield b"event: done\ndata: {}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    monkeypatch.setattr(app_module, "chat", fake_chat)

    async def exercise():
        result = await app_module._external_api_submit_run(
            "run_12345678",
            {"message": "請整理狀態", "model": None, "use_rag": False},
            {"project_id": "project-a", "api_key_id": "wak_12345678"},
        )
        await asyncio.gather(*tuple(app_module.external_api_background_tasks))
        return result

    result = asyncio.run(exercise())

    assert result["project_id"] == "project-a"
    assert result["status"] == "queued"
    assert created["project_id"] == "project-a"
    assert created["title"] == "外部 API 工作"
    assert received["request"].run_id == "run_12345678"
    assert received["request"].session_id == created["session_id"]
    assert not app_module.external_api_active_runs


def test_external_run_projection_returns_only_the_bound_answer(monkeypatch):
    monkeypatch.setattr(
        app_module.database,
        "get_run",
        lambda _run_id: {
            "run_id": "run_12345678",
            "project_id": "project-a",
            "session_id": "sess_12345678",
            "turn_id": "turn_12345678",
            "model": "local-chat",
            "status": "completed",
            "created_at": "2026-08-31T00:00:00+00:00",
            "completed_at": "2026-08-31T00:00:01+00:00",
            "metrics": {},
        },
    )
    monkeypatch.setattr(
        app_module.database,
        "get_messages_by_session",
        lambda _sid: [
            {
                "role": "assistant",
                "turn_id": "turn_other",
                "visible_content": "其他工作",
            },
            {
                "role": "assistant",
                "turn_id": "turn_12345678",
                "visible_content": "安全的最終回答",
                "llm_content": "不應輸出的內部內容",
                "sources": [{"private": "metadata"}],
            },
        ],
    )

    result = app_module._external_api_get_run(
        "run_12345678", {"project_id": "project-a"}
    )

    assert result["answer"] == "安全的最終回答"
    assert "sources" not in result
    assert "llm_content" not in result


def test_external_run_projection_rejects_project_mismatch(monkeypatch):
    monkeypatch.setattr(
        app_module.database,
        "get_run",
        lambda _run_id: {
            "run_id": "run_12345678",
            "project_id": "project-b",
            "status": "running",
        },
    )

    with pytest.raises(ExternalAgentApiError) as raised:
        app_module._external_api_get_run(
            "run_12345678", {"project_id": "project-a"}
        )

    assert raised.value.code == "EXTERNAL_API_RUN_NOT_FOUND"
