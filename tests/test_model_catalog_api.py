from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from api.routes import models as models_route  # noqa: E402
from api.schemas.models import ModelInstallRequest  # noqa: E402


def _models_router(*, installed=(), database=None):
    return models_route.build_models_router(
        database=database or SimpleNamespace(),
        load_settings=lambda: {"ollama_url": "http://127.0.0.1:11434"},
        save_settings=lambda _settings: None,
        error_payload=lambda code, message, detail=None, **_kwargs: {
            "code": code,
            "message": message,
            "detail": detail,
        },
        create_id=lambda prefix: f"{prefix}_test",
        require_local_workbench=lambda _request: None,
        rag_stats=lambda: {},
        ollama_models=lambda: list(installed),
        require_extension=lambda _extension, _project: None,
        app_version="test",
        agent_protocol_version=1,
    )


def _catalog_payload(monkeypatch, *, ram_gb: float, vram_gb: float = 0, installed=()):
    monkeypatch.setattr(
        models_route,
        "_detect_ram",
        lambda: {"ram_total_gb": ram_gb, "ram_free_gb": ram_gb / 2},
    )
    monkeypatch.setattr(
        models_route,
        "_detect_gpus",
        lambda: ([{
            "name": "Test GPU",
            "vram_total_gb": vram_gb,
            "vram_free_gb": vram_gb / 2,
        }] if vram_gb else []),
    )
    monkeypatch.setattr(
        models_route.requests,
        "get",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=503),
    )
    router = _models_router(installed=installed)
    endpoint = next(
        route.endpoint for route in router.routes if route.path == "/api/models/catalog"
    )
    return endpoint()


def test_catalog_uses_current_hardware_shape_and_gpu_fit(monkeypatch):
    payload = _catalog_payload(monkeypatch, ram_gb=16, vram_gb=12)
    assert payload["hardware"]["ram_total_gb"] == 16
    assert payload["hardware"]["gpu"][0]["vram_total_gb"] == 12
    assert "ram" not in payload["hardware"]
    assert "gpus" not in payload["hardware"]
    by_name = {item["name"]: item for item in payload["catalog"]}
    assert by_name["qwen2.5-coder:7b"]["compatibility"]["fit"] == "good"
    assert "qwen2.5-coder:7b" in {item["name"] for item in payload["recommended"]}


def test_catalog_cpu_fallback_and_installed_aliases(monkeypatch):
    payload = _catalog_payload(
        monkeypatch,
        ram_gb=8,
        installed=("gemma4:latest", "qwen2.5-coder:7b"),
    )
    by_name = {item["name"]: item for item in payload["catalog"]}
    assert by_name["qwen2.5-coder:7b"]["compatibility"]["fit"] == "ok"
    assert by_name["qwen2.5-coder:14b"]["compatibility"]["fit"] == "poor"
    assert by_name["qwen2.5-coder:7b"]["installed"] is True
    assert by_name["gemma4:e4b"]["installed"] is True
    assert by_name["gemma4:e4b"]["installed_as"] == "gemma4:latest"
    recommended_names = {item["name"] for item in payload["recommended"]}
    assert "qwen2.5-coder:7b" not in recommended_names
    assert "gemma4:e4b" not in recommended_names


@pytest.mark.parametrize("model", (
    "bge-m3:latest",
    "embeddinggemma:latest",
    "glm-ocr:latest",
    "llama-guard3:8b",
    "starcoder2:3b",
    "vendor/opaque-model:latest",
))
def test_custom_install_rejects_specialized_or_unrecognized_models(model):
    endpoint = next(
        route.endpoint
        for route in _models_router().routes
        if route.path == "/api/models/install" and "POST" in route.methods
    )
    with pytest.raises(HTTPException) as exc_info:
        endpoint(
            ModelInstallRequest(model=model),
            BackgroundTasks(),
            SimpleNamespace(),
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "MODEL_KIND_UNVERIFIED"
