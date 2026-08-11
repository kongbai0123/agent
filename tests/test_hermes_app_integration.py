from __future__ import annotations

import json
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import app as app_module  # noqa: E402
from chat import runtime as chat_runtime  # noqa: E402
from hermes import HermesRunSnapshot, SSEEvent  # noqa: E402
from hermes_integration import HermesIntegrationDecision  # noqa: E402
from hermes_project_skills_bridge import HermesProjectSkillsAttachment  # noqa: E402


def _events(stream_text: str):
    parsed = []
    for block in stream_text.strip().split("\n\n"):
        lines = block.splitlines()
        parsed.append(
            (lines[0].split(":", 1)[1].strip(), json.loads(lines[1][6:]))
        )
    return parsed


class _ApprovalStore:
    def __init__(self) -> None:
        self.expired = []

    def expire_run(self, run_id: str) -> None:
        self.expired.append(run_id)

    def list_pending(self, **_kwargs):
        return []


class _Runs:
    @contextmanager
    def open_events(self, _run_id: str):
        yield iter(
            [
                SSEEvent(
                    "message",
                    json.dumps(
                        {
                            "event": "run.completed",
                            "output": "Hermes route answer",
                            "usage": {
                                "input_tokens": 3,
                                "output_tokens": 4,
                                "total_tokens": 7,
                            },
                        }
                    ),
                )
            ]
        )


class _Manager:
    def __init__(self) -> None:
        self.config = SimpleNamespace(default_model="gemma4-hermes:latest")
        self.runs = _Runs()
        self.approval_store = _ApprovalStore()
        self.prepare_calls = []
        self.completions = []

    def probe(self):
        return {"success": True}

    def status(self):
        return {
            "enabled": True,
            "configured": True,
            "model": self.config.default_model,
            "base_url": "http://127.0.0.1:8642",
            "api_key_configured": True,
            "health": {"status": "healthy"},
            "rollout": {"mode": "all"},
            "features": {},
            "tools_enabled": False,
        }

    def prepare_project_skills(
        self, session_id, query, *, run_id, consume_turn
    ):
        self.prepare_calls.append((session_id, query, run_id, consume_turn))
        return HermesProjectSkillsAttachment(
            session_id=session_id,
            project_id=None,
            workbench_run_id=run_id,
            instructions="scoped Project Skill",
            sources=(),
            truncated=False,
        )

    def decide(self, _session_id):
        return HermesIntegrationDecision(
            True, "selected_all", "", SimpleNamespace()
        )

    def start_run(self, **kwargs):
        return HermesRunSnapshot(
            kwargs["workbench_run_id"],
            kwargs["workbench_session_id"],
            "upstream-1",
            "running",
            {},
        )

    def complete(self, decision, *, success, failure_kind=""):
        self.completions.append((decision, success, failure_kind))

    def abandon(self, _decision, *, reason):
        raise AssertionError(f"successful run must not be abandoned: {reason}")

    def fallback_allowed(self, *_args, **_kwargs):
        return False


class _Cache:
    def __init__(self, manager: _Manager) -> None:
        self.manager = manager
        self.closed = False

    def try_get(self, _settings):
        return self.manager

    def status(self, _settings):
        return self.manager.status()

    def close(self):
        self.closed = True


class _UnavailableCache:
    def try_get(self, _settings):
        return None

    def status(self, _settings):
        return {
            "enabled": True,
            "configured": False,
            "health": {"status": "unhealthy", "reason": "installation_unavailable"},
        }

    def close(self):
        return None


class _BasicResponse:
    status_code = 200
    text = ""

    def iter_lines(self):
        yield json.dumps(
            {"message": {"content": "basic fallback answer"}, "done": False}
        ).encode()
        yield json.dumps({"message": {}, "done": True, "done_reason": "stop"}).encode()

    def close(self):
        return None


def test_app_routes_text_turn_through_hermes_with_one_skill_resolution(monkeypatch):
    real_settings = app_module.load_settings()
    settings = {
        **real_settings,
        "hermes_enabled": True,
        "hermes_rollout_mode": "all",
    }
    manager = _Manager()
    cache = _Cache(manager)

    monkeypatch.setattr(app_module, "load_settings", lambda: dict(settings))
    monkeypatch.setattr(app_module, "hermes_manager_cache", cache)
    monkeypatch.setattr(
        app_module,
        "loaded_models_snapshot",
        lambda *_args, **_kwargs: set(),
    )
    monkeypatch.setattr(
        app_module.project_skill_runtime,
        "build_prompt_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Project Skills must be resolved only through the shared attachment")
        ),
    )

    suffix = uuid.uuid4().hex[:12]
    run_id = f"run_hermes_app_{suffix}"
    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        response = client.post(
            "/api/chat",
            json={
                "model": "route-test-model",
                "message": "Use Hermes",
                "run_id": run_id,
            },
        )

    assert response.status_code == 200
    events = _events(response.text)
    assert [name for name, _ in events] == ["meta", "token", "metrics", "done"]
    assert events[0][1]["runtime"] == "hermes"
    assert events[1][1]["content"] == "Hermes route answer"
    assert len(manager.prepare_calls) == 1
    assert manager.prepare_calls[0][2:] == (run_id, True)
    assert manager.completions[-1][1] is True
    assert cache.closed is True


def test_hermes_status_stays_redacted_when_installation_is_unavailable(monkeypatch):
    real_settings = app_module.load_settings()
    settings = {**real_settings, "hermes_enabled": True}
    monkeypatch.setattr(app_module, "load_settings", lambda: dict(settings))

    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        response = client.get("/api/hermes/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["enabled"] is True
    assert payload["configured"] is False
    assert payload["health"]["status"] == "unhealthy"
    assert payload["health"]["reason"] in {
        "configuration_invalid",
        "installation_unavailable",
    }
    assert "Bearer " not in response.text


def test_enabled_but_unavailable_sidecar_keeps_basic_chat_working(monkeypatch):
    real_settings = app_module.load_settings()
    settings = {**real_settings, "hermes_enabled": True}
    unavailable = _UnavailableCache()

    monkeypatch.setattr(app_module, "load_settings", lambda: dict(settings))
    monkeypatch.setattr(app_module, "hermes_manager_cache", unavailable)
    monkeypatch.setattr(
        app_module,
        "loaded_models_snapshot",
        lambda *_args, **_kwargs: set(),
    )
    monkeypatch.setattr(
        chat_runtime,
        "provider_post_chat",
        lambda *_args, **_kwargs: _BasicResponse(),
    )

    run_id = f"run_hermes_fallback_{uuid.uuid4().hex[:12]}"
    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        response = client.post(
            "/api/chat",
            json={
                "model": "route-test-model",
                "message": "Keep basic chat available",
                "run_id": run_id,
            },
        )

    assert response.status_code == 200
    events = _events(response.text)
    assert [name for name, _ in events] == ["meta", "token", "metrics", "done"]
    assert events[0][1]["runtime"] == "chat"
    assert events[1][1]["content"] == "basic fallback answer"


def test_emergency_rollback_atomically_disables_rollout_and_tools(monkeypatch):
    current = {
        **app_module.load_settings(),
        "hermes_enabled": True,
        "hermes_rollout_mode": "canary",
        "hermes_canary_session_ids": ["sensitive-canary"],
        "hermes_tools_enabled": True,
        "hermes_allowed_capabilities": ["hermes.project.read"],
        "hermes_readonly_project_id": "sensitive-project",
    }
    saved = []
    applied = []

    def validate(patch):
        assert patch["hermes_rollout_mode"] == "disabled"
        assert patch["hermes_tools_enabled"] is False
        return {**current, **patch}

    monkeypatch.setattr(app_module, "validate_settings", validate)
    monkeypatch.setattr(app_module, "save_settings", lambda cfg: saved.append(dict(cfg)))
    monkeypatch.setattr(
        app_module,
        "apply_runtime_configuration",
        lambda cfg: applied.append(dict(cfg)),
    )

    result = app_module.rollback_hermes_rollout()

    assert result == {
        "rolled_back": True,
        "rollout": {"mode": "disabled", "percentage": 0.0},
        "tools_enabled": False,
        "preserved_runtime_data": True,
    }
    assert saved == applied
    assert saved[0]["hermes_rollout_mode"] == "disabled"
    assert saved[0]["hermes_canary_session_ids"] == []
    assert saved[0]["hermes_tools_enabled"] is False
    assert saved[0]["hermes_allowed_capabilities"] == []
    assert saved[0]["hermes_readonly_project_id"] == ""
