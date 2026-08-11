from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import app as app_module
from chat import runtime as chat_runtime


class RouteModelResponse:
    status_code = 200
    text = ""

    def __init__(self):
        self.closed = False

    def iter_lines(self):
        yield json.dumps({"message": {"content": "route answer"}, "done": False}).encode()
        yield json.dumps({
            "message": {}, "done": True, "prompt_eval_count": 4,
            "eval_count": 2, "eval_duration": 1_000_000_000,
            "done_reason": "stop",
        }).encode()

    def close(self):
        self.closed = True


def _events(stream_text):
    result = []
    for block in stream_text.strip().split("\n\n"):
        lines = block.splitlines()
        result.append((lines[0].split(":", 1)[1].strip(), json.loads(lines[1][6:])))
    return result


def test_post_chat_route_runs_basic_stream_end_to_end(monkeypatch):
    captured = {}
    model_response = RouteModelResponse()

    def fake_provider(settings, payload, **kwargs):
        captured.update(settings=settings, payload=payload, kwargs=kwargs)
        return model_response

    monkeypatch.setattr(app_module, "loaded_models_snapshot", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(chat_runtime, "provider_post_chat", fake_provider)
    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        response = client.post("/api/chat", json={
            "model": "route-test-model",
            "messages": [{"role": "user", "content": "route hello"}],
            "use_rag": True,
            "skill_ids": [],
            "skill_auto": True,
            "run_id": "run_route1234",
        })

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    names = [name for name, _ in events]
    assert names == ["meta", "token", "metrics", "done"]
    assert not set(names) & {"plan", "tool_start", "agent_spawned", "safir", "validation"}
    assert captured["payload"]["messages"][-1]["content"] == "route hello"
    assert "tools" not in captured["payload"]
    assert captured["payload"]["keep_alive"] == 0
    session_id = events[-1][1]["session_id"]
    messages = app_module.database.get_messages_by_session(session_id)
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "route hello"),
        ("assistant", "route answer"),
    ]
    assert messages[-1]["process_events"] == []
    assert model_response.closed


def test_models_route_returns_flat_model_names(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "model_inventory",
        lambda: [{"name": "local-model"}, {"name": "provider::remote-model"}],
    )

    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        response = client.get("/api/models")

    assert response.status_code == 200
    assert response.json()["models"] == ["local-model", "provider::remote-model"]
