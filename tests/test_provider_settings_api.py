import os
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import app as workbench_app
import local_session


def test_provider_secret_write_only_api_uses_dpapi(tmp_path):
    settings_path = tmp_path / "settings.json"
    secret_path = tmp_path / "provider-secrets.json"
    headers = {
        "Origin": "http://127.0.0.1:8080",
        "X-Workbench-Token": local_session.session_token(),
    }
    provider = {
        "id": "openrouter",
        "provider_type": "openai_compatible",
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "enabled": True,
        "input_cost_per_million": 1.25,
        "output_cost_per_million": 4.5,
        "currency": "USD",
    }
    with patch.dict(
        os.environ,
        {
            "WORKBENCH_SETTINGS_PATH": str(settings_path),
            "WORKBENCH_SECRET_STORE_PATH": str(secret_path),
        },
        clear=False,
    ), TestClient(workbench_app.app) as client:
        saved = client.post(
            "/api/settings",
            headers=headers,
            json={"model_providers": [provider]},
        )
        assert saved.status_code == 200
        saved_provider = saved.json()["model_providers"][0]
        assert saved_provider["enabled"] is True
        assert saved_provider["supports_tools"] is False

        secret = "test-api-route-secret-9876"
        written = client.post(
            "/api/settings/secrets",
            headers=headers,
            json={"provider_id": "openrouter", "api_key": secret},
        )
        assert written.status_code == 200
        assert written.json()["last4"] == "9876"
        assert secret not in written.text

        public = client.get("/api/settings/secrets", headers=headers)
        assert public.status_code == 200
        assert public.json()["providers"] == [{
            "provider_id": "openrouter",
            "configured": True,
            "last4": "9876",
        }]
        assert secret not in public.text
        assert secret not in settings_path.read_text(encoding="utf-8")
        assert secret not in secret_path.read_text(encoding="utf-8")


def test_provider_type_is_persisted_and_official_endpoint_cannot_be_replaced(tmp_path):
    settings_path = tmp_path / "settings.json"
    headers = {
        "Origin": "http://127.0.0.1:8080",
        "X-Workbench-Token": local_session.session_token(),
    }
    with patch.dict(
        os.environ,
        {"WORKBENCH_SETTINGS_PATH": str(settings_path)},
        clear=False,
    ), TestClient(workbench_app.app) as client:
        saved = client.post(
            "/api/settings",
            headers=headers,
            json={
                "model_providers": [{
                    "id": "nvidia",
                    "provider_type": "nvidia",
                    "label": "NVIDIA 免費端點",
                    "base_url": "https://integrate.api.nvidia.com/v1",
                }],
            },
        )
        assert saved.status_code == 200
        provider = saved.json()["model_providers"][0]
        assert provider["provider_type"] == "nvidia"

        rejected = client.post(
            "/api/settings",
            headers=headers,
            json={
                "model_providers": [{
                    "id": "nvidia",
                    "provider_type": "nvidia",
                    "label": "Fake NVIDIA",
                    "base_url": "https://attacker.example/v1",
                }],
            },
        )
        assert rejected.status_code == 400
        assert "cannot be changed" in rejected.text
