from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from core import settings as settings_module  # noqa: E402
from api.routes.settings import build_settings_router  # noqa: E402


def test_hermes_settings_are_disabled_and_secret_free_by_default(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("WORKBENCH_SETTINGS_PATH", str(settings_path))

    settings = settings_module.load_settings()

    assert settings["hermes_enabled"] is False
    assert settings["hermes_base_url"] == "http://127.0.0.1:8642"
    assert settings["hermes_rollout_mode"] == "disabled"
    assert settings["hermes_tools_enabled"] is False
    assert "api_key" not in settings


def test_hermes_settings_round_trip_without_persisting_secret(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("WORKBENCH_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("WORKBENCH_HERMES_TEST_KEY", "do-not-persist-this-value")

    canary = settings_module.validate_settings({
        "hermes_rollout_mode": "canary",
        "hermes_canary_session_ids": ["round-trip-canary"],
    })
    settings_module.save_settings(canary)

    validated = settings_module.validate_settings({
        **settings_module.load_settings(),
        "hermes_enabled": True,
        "hermes_base_url": "http://[::1]:8642/",
        "hermes_api_key_env": "WORKBENCH_HERMES_TEST_KEY",
        "hermes_rollout_mode": "percentage",
        "hermes_rollout_percentage": 5,
        "hermes_tools_enabled": False,
        "hermes_allowed_capabilities": ["terminal", "terminal"],
    })
    settings_module.save_settings(validated)
    saved_text = settings_path.read_text(encoding="utf-8")
    saved = json.loads(saved_text)

    assert saved["hermes_base_url"] == "http://[::1]:8642"
    assert saved["hermes_rollout_percentage"] == 5.0
    assert saved["hermes_allowed_capabilities"] == []
    assert saved["hermes_api_key_env"] == "WORKBENCH_HERMES_TEST_KEY"
    assert "do-not-persist-this-value" not in saved_text


@pytest.mark.parametrize(
    "url",
    (
        "http://192.168.1.20:8642",
        "https://example.com:8642",
        "http://test-user@127.0.0.1:8642",
        "http://127.0.0.1:8642/v1",
    ),
)
def test_hermes_settings_reject_non_loopback_or_credentialed_urls(
    monkeypatch, tmp_path, url
):
    monkeypatch.setenv("WORKBENCH_SETTINGS_PATH", str(tmp_path / "settings.json"))
    with pytest.raises(ValueError, match="hermes_base_url"):
        settings_module.validate_settings({"hermes_base_url": url})


def test_hermes_rollout_modes_are_normalized(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("WORKBENCH_SETTINGS_PATH", str(settings_path))

    with pytest.raises(ValueError, match="one stage at a time"):
        settings_module.validate_settings({"hermes_rollout_mode": "all"})

    canary = settings_module.validate_settings({
        "hermes_rollout_mode": "canary",
        "hermes_canary_session_ids": ["sess_a", "sess_a", "sess_b"],
    })
    assert canary["hermes_canary_session_ids"] == ["sess_a", "sess_b"]
    settings_module.save_settings(canary)

    percentage_5 = settings_module.validate_settings({
        "hermes_rollout_mode": "percentage",
    })
    assert percentage_5["hermes_rollout_percentage"] == 5.0
    assert percentage_5["hermes_canary_session_ids"] == []
    settings_module.save_settings(percentage_5)

    with pytest.raises(ValueError, match="exactly 5, 25, or 50"):
        settings_module.validate_settings({
            "hermes_rollout_mode": "percentage",
            "hermes_rollout_percentage": 12.5,
        })
    with pytest.raises(ValueError, match="one stage at a time"):
        settings_module.validate_settings({
            "hermes_rollout_mode": "percentage",
            "hermes_rollout_percentage": 50,
        })

    percentage_25 = settings_module.validate_settings({
        "hermes_rollout_mode": "percentage",
        "hermes_rollout_percentage": 25,
    })
    settings_module.save_settings(percentage_25)
    percentage_50 = settings_module.validate_settings({
        "hermes_rollout_mode": "percentage",
        "hermes_rollout_percentage": 50,
    })
    settings_module.save_settings(percentage_50)
    all_users = settings_module.validate_settings({
        "hermes_rollout_mode": "all",
        "hermes_rollout_percentage": 1,
        "hermes_canary_session_ids": ["ignored"],
    })
    assert all_users["hermes_rollout_percentage"] == 100.0
    assert all_users["hermes_canary_session_ids"] == []
    settings_module.save_settings(all_users)

    immediate_rollback = settings_module.validate_settings({
        "hermes_rollout_mode": "percentage",
        "hermes_rollout_percentage": 5,
    })
    assert immediate_rollback["hermes_rollout_percentage"] == 5.0

    settings_path.unlink()
    with pytest.raises(ValueError, match="canary"):
        settings_module.validate_settings({"hermes_rollout_mode": "canary"})


def test_unrelated_partial_update_preserves_hermes_settings(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("WORKBENCH_SETTINGS_PATH", str(settings_path))
    initial = settings_module.validate_settings({
        "hermes_enabled": True,
        "hermes_rollout_mode": "canary",
        "hermes_canary_session_ids": ["preserve-canary"],
    })
    settings_module.save_settings(initial)
    for percentage in settings_module.HERMES_PERCENTAGE_LADDER:
        initial = settings_module.validate_settings({
            "hermes_rollout_mode": "percentage",
            "hermes_rollout_percentage": percentage,
        })
        settings_module.save_settings(initial)
    initial = settings_module.validate_settings({"hermes_rollout_mode": "all"})
    settings_module.save_settings(initial)

    updated = settings_module.validate_settings({"ui_language": "en-US"})

    assert updated["ui_language"] == "en-US"
    assert updated["hermes_enabled"] is True
    assert updated["hermes_rollout_mode"] == "all"
    assert updated["hermes_rollout_percentage"] == 100.0


def test_hermes_tools_cannot_be_enabled_by_an_environment_flag(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("WORKBENCH_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.delenv("WORKBENCH_HERMES_OS_ISOLATED", raising=False)
    with pytest.raises(ValueError, match="live Docker isolation"):
        settings_module.validate_settings({"hermes_tools_enabled": True})

    monkeypatch.setenv("WORKBENCH_HERMES_OS_ISOLATED", "1")
    with pytest.raises(ValueError, match="live Docker isolation"):
        settings_module.validate_settings({
            "hermes_tools_enabled": True,
            "hermes_allowed_capabilities": ["terminal"],
        })


def test_project_tools_must_be_stopped_and_saved_before_rollout_expands(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("WORKBENCH_SETTINGS_PATH", str(tmp_path / "settings.json"))
    canary = settings_module.validate_settings({
        "hermes_rollout_mode": "canary",
        "hermes_canary_session_ids": ["tool-canary"],
    })
    settings_module.save_settings(canary)
    tools = settings_module.validate_settings({
        "hermes_tools_enabled": True,
        "hermes_allowed_capabilities": ["hermes.project.read"],
        "hermes_readonly_project_id": "project-one",
    })
    settings_module.save_settings(tools)

    with pytest.raises(ValueError, match="Disable Hermes project tools and save"):
        settings_module.validate_settings({
            "hermes_tools_enabled": False,
            "hermes_rollout_mode": "percentage",
            "hermes_rollout_percentage": 5,
        })

    disabled_tools = settings_module.validate_settings({"hermes_tools_enabled": False})
    settings_module.save_settings(disabled_tools)
    promoted = settings_module.validate_settings({
        "hermes_rollout_mode": "percentage",
        "hermes_rollout_percentage": 5,
    })
    assert promoted["hermes_rollout_percentage"] == 5.0


def test_invalid_legacy_rollout_can_only_be_reset_to_disabled(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("WORKBENCH_SETTINGS_PATH", str(settings_path))
    settings_path.write_text(
        json.dumps({
            "hermes_rollout_mode": "percentage",
            "hermes_rollout_percentage": 12.5,
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="persisted Hermes rollout stage is invalid"):
        settings_module.validate_settings({"ui_language": "en-US"})
    reset = settings_module.validate_settings({"hermes_rollout_mode": "disabled"})
    assert reset["hermes_rollout_mode"] == "disabled"
    assert reset["hermes_rollout_percentage"] == 0.0


def test_settings_router_calls_rollout_guard_before_validation_and_redacts_secrets():
    current = {
        "ui_language": "zh-TW",
        "hermes_api_key": "stored-secret",
        "hermes_token": "stored-token",
    }
    events = []

    def load_settings():
        events.append(("load", None))
        return dict(current)

    def guard(current_settings, requested_data):
        events.append(("guard", (dict(current_settings), dict(requested_data))))

    def validate_settings(requested_data):
        events.append(("validate", dict(requested_data)))
        return {**current, **requested_data, "hermes_secret": "validated-secret"}

    def save_settings(settings):
        events.append(("save", dict(settings)))

    def apply_configuration(settings):
        events.append(("apply", dict(settings)))

    app = FastAPI()
    app.include_router(build_settings_router(
        load_settings=load_settings,
        save_settings=save_settings,
        validate_settings=validate_settings,
        effective_config=lambda _settings: {"ready": True},
        normalize_modal_size=lambda _data: {
            "settings_modal_width": 900,
            "settings_modal_height": 650,
        },
        apply_configuration=apply_configuration,
        error_payload=lambda *_args, **_kwargs: {},
        hermes_rollout_guard=guard,
    ))
    client = TestClient(app)

    fetched = client.get("/api/settings")
    assert fetched.status_code == 200
    assert "stored-secret" not in fetched.text
    assert "stored-token" not in fetched.text
    assert "hermes_api_key" not in fetched.json()
    assert "hermes_token" not in fetched.json()["settings"]

    events.clear()
    saved = client.post("/api/settings", json={
        "ui_language": "en-US",
        "hermes_api_key_ref": "submitted-secret-ref",
    })
    assert saved.status_code == 200
    assert [name for name, _value in events] == [
        "load",
        "guard",
        "validate",
        "save",
        "apply",
    ]
    guard_current, guard_requested = events[1][1]
    assert guard_current == current
    assert guard_requested["ui_language"] == "en-US"
    assert "submitted-secret-ref" not in saved.text
    assert "validated-secret" not in saved.text
    for secret_key in (
        "hermes_api_key",
        "hermes_api_key_ref",
        "hermes_secret",
        "hermes_token",
    ):
        assert secret_key not in saved.json()
        assert secret_key not in saved.json()["settings"]
