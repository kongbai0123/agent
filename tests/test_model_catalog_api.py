from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from api.routes import models as models_route  # noqa: E402


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
    router = models_route.build_models_router(
        database=SimpleNamespace(),
        load_settings=lambda: {"ollama_url": "http://127.0.0.1:11434"},
        save_settings=lambda _settings: None,
        error_payload=lambda code, message, *_args, **_kwargs: {"code": code, "message": message},
        create_id=lambda prefix: f"{prefix}_test",
        require_local_workbench=lambda _request: None,
        rag_stats=lambda: {},
        ollama_models=lambda: list(installed),
        require_extension=lambda _extension, _project: None,
        app_version="test",
        agent_protocol_version=1,
    )
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
