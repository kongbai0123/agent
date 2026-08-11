import math
import time
from typing import Any, Callable, Dict, Iterable, Optional, Set

import requests


def _model_name(item: Dict[str, Any]) -> str:
    return str(item.get("name") or item.get("model") or "").strip()


def loaded_models_snapshot(
    ollama_url: str,
    *,
    request_get: Callable[..., Any] = requests.get,
    timeout_seconds: float = 2.0,
) -> Optional[Dict[str, Dict[str, Any]]]:
    try:
        response = request_get(f"{ollama_url.rstrip('/')}/api/ps", timeout=timeout_seconds)
        if response.status_code != 200:
            return None
        models = response.json().get("models", [])
        return {
            name: dict(item)
            for item in models
            if isinstance(item, dict) and (name := _model_name(item))
        }
    except Exception:
        return None


def _safe_resources(sampler: Callable[[], Dict[str, float]]) -> Dict[str, float]:
    try:
        sample = sampler() or {}
        return {
            "ram_free_gb": round(max(0.0, float(sample.get("ram_free_gb") or 0.0)), 2),
            "vram_free_gb": round(max(0.0, float(sample.get("vram_free_gb") or 0.0)), 2),
        }
    except Exception:
        return {"ram_free_gb": 0.0, "vram_free_gb": 0.0}


def monitor_cancel_release(
    *,
    ollama_url: str,
    tracked_models: Iterable[str],
    protected_models: Iterable[str],
    preexisting_snapshot_known: bool,
    resource_sampler: Callable[[], Dict[str, float]],
    grace_seconds: float = 4.0,
    poll_seconds: float = 0.5,
    cleanup_wait_seconds: float = 4.0,
    request_get: Callable[..., Any] = requests.get,
    request_post: Callable[..., Any] = requests.post,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    tracked: Set[str] = {str(model).strip() for model in tracked_models if str(model).strip()}
    protected: Set[str] = {str(model).strip() for model in protected_models if str(model).strip()}
    before = _safe_resources(resource_sampler)
    loaded = loaded_models_snapshot(ollama_url, request_get=request_get)

    base = {
        "grace_seconds": round(max(0.0, float(grace_seconds)), 1),
        "tracked_models": sorted(tracked),
        "protected_models": sorted(tracked & protected),
        "resources_before": before,
        "cleanup_performed": False,
        "models_unloaded": [],
        "timed_out": False,
        "warning": False,
    }
    if not preexisting_snapshot_known or loaded is None:
        return {
            **base,
            "state": "unavailable",
            "warning": True,
            "resources_after": _safe_resources(resource_sampler),
            "message": "無法可靠判定本次 Run 的模型所有權；為避免影響既有 Ollama 工作，未執行強制卸載。",
        }

    cleanup_candidates = (tracked - protected) & set(loaded)
    if not cleanup_candidates:
        protected_loaded = tracked & protected & set(loaded)
        return {
            **base,
            "state": "protected" if protected_loaded else "released",
            "resources_after": _safe_resources(resource_sampler),
            "message": (
                "模型仍由啟動前或其他進行中工作使用，已保留且未強制卸載。"
                if protected_loaded else "Ollama 已在停止寬限時間前釋放本次工作資源。"
            ),
        }

    interval = max(0.1, float(poll_seconds))
    checks = max(1, int(math.ceil(max(0.0, float(grace_seconds)) / interval)))
    for _ in range(checks):
        sleep(interval)
        loaded = loaded_models_snapshot(ollama_url, request_get=request_get)
        if loaded is None:
            return {
                **base,
                "state": "unavailable",
                "warning": True,
                "resources_after": _safe_resources(resource_sampler),
                "message": "停止後無法讀取 Ollama 模型狀態；為避免誤關既有工作，未繼續強制清理。",
            }
        cleanup_candidates &= set(loaded)
        if not cleanup_candidates:
            return {
                **base,
                "state": "released",
                "resources_after": _safe_resources(resource_sampler),
                "message": "Ollama 已在停止寬限時間內釋放本次工作資源。",
            }

    unloaded = []
    cleanup_errors = []
    for model in sorted(cleanup_candidates):
        try:
            response = request_post(
                f"{ollama_url.rstrip('/')}/api/chat",
                json={"model": model, "messages": [], "keep_alive": 0, "stream": False},
                timeout=10,
            )
            if response.status_code == 200:
                unloaded.append(model)
            else:
                cleanup_errors.append(f"{model}: HTTP {response.status_code}")
        except Exception as exc:
            cleanup_errors.append(f"{model}: {str(exc)[:160]}")

    remaining = set(cleanup_candidates)
    cleanup_checks = max(1, int(math.ceil(max(0.0, float(cleanup_wait_seconds)) / interval)))
    for _ in range(cleanup_checks):
        if not remaining:
            break
        sleep(interval)
        loaded = loaded_models_snapshot(ollama_url, request_get=request_get)
        if loaded is None:
            break
        remaining &= set(loaded)

    after = _safe_resources(resource_sampler)
    recovered = {
        "ram_gb": round(after["ram_free_gb"] - before["ram_free_gb"], 2),
        "vram_gb": round(after["vram_free_gb"] - before["vram_free_gb"], 2),
    }
    cleaned = not remaining and not cleanup_errors
    return {
        **base,
        "state": "cleaned" if cleaned else "warning",
        "timed_out": True,
        "warning": True,
        "cleanup_performed": True,
        "models_unloaded": unloaded,
        "models_remaining": sorted(remaining),
        "cleanup_errors": cleanup_errors,
        "resources_after": after,
        "resources_recovered": recovered,
        "message": (
            f"Ollama 未在 {base['grace_seconds']} 秒內釋放資源；已受控卸載本次模型 "
            f"{'、'.join(unloaded)}，未關閉 Ollama 服務。"
            if cleaned else
            "Ollama 停止逾時，且受控卸載後仍有資源未釋放；請查看資源警告，系統未強制終止 Ollama 服務。"
        ),
    }
