from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import app as app_module
from chat import runtime as chat_runtime


class _ModelResponse:
    status_code = 200
    text = ""

    def iter_lines(self):
        yield json.dumps(
            {"message": {"content": "skill-aware answer"}, "done": False}
        ).encode()
        yield json.dumps({"message": {}, "done": True, "done_reason": "stop"}).encode()

    def close(self):
        return None


def test_basic_prompt_places_project_skill_context_in_the_system_message():
    messages = chat_runtime.build_basic_messages(
        persisted_messages=[],
        request_messages=[],
        user_query="Review this change",
        current_turn_id="turn_skill_prompt",
        project_skill_context="--- BEGIN PROJECT SKILL ---\nUse the release checklist.\n--- END PROJECT SKILL ---",
    )

    assert [item["role"] for item in messages] == ["system", "user"]
    assert "Use the release checklist." in messages[0]["content"]
    assert "Project Skills selected for this session" in messages[0]["content"]
    assert "Use the release checklist." not in messages[-1]["content"]


def test_chat_route_resolves_skill_context_from_the_session(monkeypatch):
    captured = {}

    def fake_build(session_id, user_query, *, run_id, consume_turn):
        captured["resolution"] = {
            "session_id": session_id,
            "user_query": user_query,
            "run_id": run_id,
            "consume_turn": consume_turn,
        }
        return {
            "project_id": None,
            "context": "--- BEGIN PROJECT SKILL ---\nRoute skill context.\n--- END PROJECT SKILL ---",
            "skills": [],
            "truncated": False,
        }

    def fake_provider(settings, payload, **kwargs):
        captured["payload"] = payload
        return _ModelResponse()

    monkeypatch.setattr(
        app_module.project_skill_runtime,
        "build_prompt_context",
        fake_build,
    )
    monkeypatch.setattr(app_module, "loaded_models_snapshot", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(chat_runtime, "provider_post_chat", fake_provider)

    suffix = uuid.uuid4().hex[:12]
    run_id = f"run_skill_{suffix}"
    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        response = client.post(
            "/api/chat",
            json={
                "model": "route-skill-model",
                "message": "Use my project guidance",
                "run_id": run_id,
            },
        )

    assert response.status_code == 200
    assert captured["resolution"]["run_id"] == run_id
    assert captured["resolution"]["consume_turn"] is True
    assert "Route skill context." in captured["payload"]["messages"][0]["content"]
