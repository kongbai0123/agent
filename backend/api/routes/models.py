"""Environment, model catalog, installation, and run-inspection routes."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse

from api.schemas.models import BenchmarkRequest, ModelInstallRequest, SelectModelRequest
from chat.events import encode_sse
from model_catalog import MODEL_CATALOG
from system_resources import detect_gpus as _detect_gpus, detect_ram as _detect_ram


def _run_model_benchmark(
    req: BenchmarkRequest,
    *,
    load_settings: Callable[[], Dict[str, Any]],
    error_payload: Callable[..., Dict[str, Any]],
    require_ollama: Callable[[], None],
) -> Dict[str, Any]:
    started = time.time()
    response: Optional[requests.Response] = None
    try:
        response = requests.post(
            f"{load_settings()['ollama_url']}/api/generate",
            json={
                "model": req.model,
                "prompt": "Reply with one short sentence.",
                "stream": True,
                "options": {"num_predict": 48},
            },
            stream=True,
            timeout=(10, 180),
        )
        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=error_payload(
                    "OLLAMA_ERROR",
                    "Ollama benchmark request failed.",
                    response.text,
                ),
            )
        first_token = None
        eval_count = 0
        eval_duration = 0
        for line in response.iter_lines():
            require_ollama()
            if not line:
                continue
            try:
                data = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if first_token is None and data.get("response"):
                first_token = time.time()
            if data.get("done"):
                eval_count = data.get("eval_count") or 0
                eval_duration = data.get("eval_duration") or 0
                break
        elapsed_ms = int((time.time() - started) * 1000)
        first_token_ms = int((first_token - started) * 1000) if first_token else None
        tokens_per_second = (
            round(eval_count / (eval_duration / 1e9), 1)
            if eval_duration
            else None
        )
        return {
            "model": req.model,
            "first_token_ms": first_token_ms,
            "ttft_seconds": (
                round(first_token_ms / 1000, 2)
                if first_token_ms is not None
                else None
            ),
            "tokens_per_second": tokens_per_second,
            "tokens_per_sec": tokens_per_second,
            "context_window": 32768,
            "recommended_context": 16384,
            "elapsed_ms": elapsed_ms,
            "total_seconds": round(elapsed_ms / 1000, 2),
        }
    except HTTPException:
        raise
    except requests.exceptions.ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=error_payload(
                "OLLAMA_NOT_CONNECTED",
                "Ollama is not connected.",
            ),
        ) from exc
    finally:
        if response is not None:
            response.close()



def build_models_router(
    *,
    database: Any,
    load_settings: Callable[[], Dict[str, Any]],
    save_settings: Callable[[Dict[str, Any]], Any],
    error_payload: Callable[..., Dict[str, Any]],
    create_id: Callable[[str], str],
    require_local_workbench: Callable[[Request], None],
    rag_stats: Callable[[], Dict[str, Any]],
    ollama_models: Callable[[], List[str]],
    require_extension: Callable[[str, Optional[str]], Any],
    app_version: str,
    agent_protocol_version: int,
) -> APIRouter:
    router = APIRouter(tags=["models"])
    APP_VERSION = app_version
    AGENT_PROTOCOL_VERSION = agent_protocol_version

    def _ensure_ollama_enabled(project_id: Optional[str] = None) -> None:
        require_extension("builtin.ollama", project_id)

    def _require_ollama(project_id: Optional[str] = None) -> None:
        _ensure_ollama_enabled(project_id)

    def _ollama_info() -> Dict[str, Any]:
        url = load_settings()["ollama_url"]
        try:
            tags = requests.get(f"{url}/api/tags", timeout=5)
            running = tags.status_code == 200
        except Exception:
            running = False
        version = None
        try:
            ver = requests.get(f"{url}/api/version", timeout=3)
            if ver.status_code == 200:
                version = ver.json().get("version")
        except Exception:
            pass
        return {"installed": True, "running": running, "version": version, "url": url}


    @router.get("/api/environment/status")
    def get_environment_status():
        models = ollama_models()
        return {"status": "ok", "backend": {"status": "ok", "version": APP_VERSION, "agent_protocol": AGENT_PROTOCOL_VERSION}, "ollama": _ollama_info(), "models_count": len(models), "rag": rag_stats()}


    @router.get("/api/environment/hardware")
    def get_environment_hardware():
        ram = _detect_ram()
        return {"os": platform.platform(), "cpu": platform.processor() or platform.machine(), "ram_total_gb": ram["ram_total_gb"], "ram_free_gb": ram["ram_free_gb"], "gpu": _detect_gpus(), "ollama": _ollama_info()}


    @router.get("/api/hardware")
    def get_hardware_legacy():
        hw = get_environment_hardware()
        return {"platform": platform.system(), "cpu": {"name": hw["cpu"], "cores": os.cpu_count()}, "ram": {"total_gb": hw["ram_total_gb"], "available_gb": hw["ram_free_gb"]}, "gpus": hw["gpu"], "has_gpu": bool(hw["gpu"])}


    def _model_fit(entry: Dict[str, Any], ram_total_gb: Optional[float], gpu: List[Dict[str, Any]]) -> Dict[str, str]:
        best_vram = max([g.get("vram_total_gb") or 0 for g in gpu], default=0)
        if best_vram >= entry["recommended_vram_gb"] or (ram_total_gb is not None and ram_total_gb >= entry["recommended_ram_gb"]):
            return {"fit": "good", "level": "good", "label": "建議安裝", "reason": "硬體達到建議記憶體需求。", "estimated_speed": "medium", "est_speed": "中等以上"}
        if ram_total_gb is not None and ram_total_gb >= entry["min_ram_gb"]:
            return {"fit": "ok", "level": "ok", "label": "可安裝", "reason": "硬體達到最低 RAM 需求，但可能較慢。", "estimated_speed": "slow", "est_speed": "偏慢"}
        return {"fit": "poor", "level": "bad", "label": "不建議", "reason": "可用記憶體低於模型最低需求。", "estimated_speed": "unknown", "est_speed": "未知"}


    def _catalog_entry_for_frontend(entry: Dict[str, Any], installed: set[str], hardware: Dict[str, Any]) -> Dict[str, Any]:
        fit = _model_fit(entry, hardware.get("ram_total_gb"), hardware.get("gpu") or [])
        purposes = entry.get("category") or []
        install_names = [entry["name"], *(entry.get("aliases") or [])]
        installed_as = next((name for name in install_names if name in installed), None)
        return {
            **entry,
            "installed": installed_as is not None,
            "installed_as": installed_as,
            "purposes": purposes,
            "size_gb": entry.get("size_gb_estimated"),
            "rec_vram_gb": entry.get("recommended_vram_gb"),
            "context": entry.get("context_window"),
            "description": f"{entry.get('display_name', entry['name'])}，適合{', '.join(purposes) or '一般'}工作流程。",
            "compatibility": fit,
        }


    @router.get("/api/models/catalog")
    def get_models_catalog():
        installed = set(ollama_models())
        hardware = get_environment_hardware()
        catalog = [_catalog_entry_for_frontend(m, installed, hardware) for m in MODEL_CATALOG]
        recommended = [m for m in catalog if not m["installed"] and m["compatibility"]["fit"] in {"good", "ok"}]
        recommended.sort(key=lambda m: (
            {"good": 0, "ok": 1, "poor": 2}.get(m["compatibility"]["fit"], 3),
            int(m.get("recommendation_priority") or 1000),
            float(m.get("size_gb_estimated") or 0),
        ))
        return {
            "models": catalog,
            "catalog": catalog,
            "recommended": recommended[:4],
            "hardware": hardware,
        }


    @router.get("/api/models/recommendations")
    def get_model_recommendations():
        hw = get_environment_hardware()
        ram_total = hw.get("ram_total_gb")
        gpu = hw.get("gpu") or []
        ranked = []
        for item in MODEL_CATALOG:
            fit = _model_fit(item, ram_total, gpu)
            if fit["fit"] in {"good", "ok"}:
                ranked.append({
                    "name": item["name"],
                    "recommendation_priority": item.get("recommendation_priority", 1000),
                    **fit,
                })
        ranked.sort(key=lambda r: (
            {"good": 0, "ok": 1, "poor": 2}.get(r["fit"], 3),
            int(r.get("recommendation_priority") or 1000),
        ))
        return {"hardware_summary": {"ram_total_gb": ram_total, "gpu_best": gpu[0]["name"] if gpu else None}, "recommended": ranked[:4]}


    _MODEL_INSTALL_CONTROLS: Dict[str, Dict[str, Any]] = {}
    _MODEL_INSTALL_LOCK = threading.Lock()
    _MODEL_INSTALL_ACTIVE_STATES = {"queued", "starting", "downloading", "cancelling"}
    _MODEL_INSTALL_TERMINAL_STATES = {"ready", "failed", "cancelled"}


    def _register_model_install(job_id: str) -> threading.Event:
        with _MODEL_INSTALL_LOCK:
            control = _MODEL_INSTALL_CONTROLS.setdefault(job_id, {"cancel": threading.Event(), "response": None})
            return control["cancel"]


    def _set_model_install_response(job_id: str, response: Optional[requests.Response]) -> None:
        with _MODEL_INSTALL_LOCK:
            control = _MODEL_INSTALL_CONTROLS.get(job_id)
            if control is not None:
                control["response"] = response


    def _release_model_install(job_id: str) -> None:
        with _MODEL_INSTALL_LOCK:
            _MODEL_INSTALL_CONTROLS.pop(job_id, None)


    def _has_model_install_control(job_id: str) -> bool:
        with _MODEL_INSTALL_LOCK:
            return job_id in _MODEL_INSTALL_CONTROLS


    def _mark_model_install_cancelled(job_id: str, model: str, message: str = "Model install stopped by user.") -> None:
        current = database.get_model_install_job(job_id) or {}
        database.upsert_model_install_job(
            job_id, model, "cancelled", int(current.get("progress") or 0),
            int(current.get("downloaded_bytes") or 0), int(current.get("total_bytes") or 0),
            message=message,
        )


    def _cancel_model_install(job_id: str) -> bool:
        response = None
        with _MODEL_INSTALL_LOCK:
            control = _MODEL_INSTALL_CONTROLS.get(job_id)
            if control is None:
                return False
            control["cancel"].set()
            response = control.get("response")
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        return True


    def _model_install_worker(job_id: str, model: str) -> None:
        cancel_event = _register_model_install(job_id)
        database.upsert_model_install_job(job_id, model, "starting", 0, message="Starting Ollama pull.")
        response: Optional[requests.Response] = None
        try:
            _ensure_ollama_enabled()
            response = requests.post(f"{load_settings()['ollama_url']}/api/pull", json={"name": model, "stream": True}, stream=True, timeout=(10, 3600))
            _set_model_install_response(job_id, response)
            if cancel_event.is_set():
                _mark_model_install_cancelled(job_id, model)
                return
            if response.status_code != 200:
                database.upsert_model_install_job(job_id, model, "failed", 100, message="Ollama pull failed.", error=response.text)
                return
            last_progress = 0
            for line in response.iter_lines():
                _ensure_ollama_enabled()
                if cancel_event.is_set():
                    _mark_model_install_cancelled(job_id, model)
                    return
                if not line:
                    continue
                try:
                    data = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                total = int(data.get("total") or 0)
                completed = int(data.get("completed") or 0)
                progress = int(completed * 100 / total) if total else last_progress
                last_progress = max(last_progress, progress)
                status_text = data.get("status") or "downloading"
                database.upsert_model_install_job(job_id, model, "downloading", last_progress, completed, total, status_text)
                if data.get("error"):
                    database.upsert_model_install_job(job_id, model, "failed", last_progress, completed, total, status_text, data.get("error"))
                    return
            if cancel_event.is_set():
                _mark_model_install_cancelled(job_id, model)
                return
            current = database.get_model_install_job(job_id) or {}
            database.upsert_model_install_job(
                job_id, model, "ready", 100,
                int(current.get("downloaded_bytes") or 0), int(current.get("total_bytes") or 0),
                message="Model installed.",
            )
        except Exception as exc:
            if cancel_event.is_set():
                _mark_model_install_cancelled(job_id, model)
            else:
                database.upsert_model_install_job(job_id, model, "failed", 100, message="Model install failed.", error=str(exc))
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            _release_model_install(job_id)


    @router.post("/api/models/install")
    def install_model(req: ModelInstallRequest, background_tasks: BackgroundTasks, request: Request):
        require_local_workbench(request)
        _require_ollama()
        for existing in database.list_model_install_jobs(100):
            if existing.get("model") == req.model and existing.get("status") in _MODEL_INSTALL_ACTIVE_STATES:
                if _has_model_install_control(existing["job_id"]):
                    return {"success": True, "job_id": existing["job_id"], "model": req.model, "status": existing["status"], "reused": True}
                _mark_model_install_cancelled(existing["job_id"], req.model, "Install stopped because the backend was restarted.")
        job_id = create_id("job")
        database.upsert_model_install_job(job_id, req.model, "queued", 0, message="Queued model install.")
        _register_model_install(job_id)
        background_tasks.add_task(_model_install_worker, job_id, req.model)
        return {"success": True, "job_id": job_id, "model": req.model, "status": "queued"}


    @router.get("/api/models/install")
    def list_model_install_jobs():
        jobs = database.list_model_install_jobs()
        for job in jobs:
            if job.get("status") in _MODEL_INSTALL_ACTIVE_STATES and not _has_model_install_control(job["job_id"]):
                _mark_model_install_cancelled(job["job_id"], job["model"], "Install stopped because the backend was restarted.")
        return {"jobs": database.list_model_install_jobs()}


    @router.get("/api/models/install/{job_id}")
    def get_model_install_job(job_id: str):
        job = database.get_model_install_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=error_payload("MODEL_INSTALL_JOB_NOT_FOUND", "Model install job not found.", recoverable=False))
        return job


    @router.post("/api/models/install/{job_id}/cancel")
    def cancel_model_install(job_id: str, request: Request):
        require_local_workbench(request)
        job = database.get_model_install_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=error_payload("MODEL_INSTALL_JOB_NOT_FOUND", "Model install job not found.", recoverable=False))
        if job["status"] in _MODEL_INSTALL_TERMINAL_STATES:
            return {"success": True, "job_id": job_id, "status": job["status"], "already_finished": True}
        database.upsert_model_install_job(
            job_id, job["model"], "cancelling", int(job.get("progress") or 0),
            int(job.get("downloaded_bytes") or 0), int(job.get("total_bytes") or 0),
            message="Stopping model install...",
        )
        controlled = _cancel_model_install(job_id)
        if not controlled:
            database.upsert_model_install_job(
                job_id, job["model"], "cancelled", int(job.get("progress") or 0),
                int(job.get("downloaded_bytes") or 0), int(job.get("total_bytes") or 0),
                message="Model install marked as stopped; no active download connection was found.",
            )
        return {"success": True, "job_id": job_id, "status": "cancelling" if controlled else "cancelled"}


    @router.get("/api/models/install/{job_id}/events")
    def get_model_install_events(job_id: str):
        async def event_stream():
            last_payload = None
            for _ in range(3600):
                job = database.get_model_install_job(job_id)
                if not job:
                    yield encode_sse("error", error_payload("MODEL_INSTALL_JOB_NOT_FOUND", "Model install job not found.", recoverable=False))
                    return
                payload = {
                    "job_id": job_id,
                    "model": job["model"],
                    "status": job["status"],
                    "progress": job["progress"],
                    "percent": job["progress"],
                    "downloaded_bytes": job["downloaded_bytes"],
                    "completed": job["downloaded_bytes"],
                    "total_bytes": job["total_bytes"],
                    "total": job["total_bytes"],
                    "message": job.get("message"),
                }
                if payload != last_payload:
                    yield encode_sse("model_install_progress", payload)
                    last_payload = payload
                if job["status"] in _MODEL_INSTALL_TERMINAL_STATES:
                    yield encode_sse("done", {"job_id": job_id, "status": job["status"]})
                    return
                await asyncio.sleep(1)
            yield encode_sse("error", error_payload("MODEL_INSTALL_TIMEOUT", "Model install did not finish within the streaming window."))

        return StreamingResponse(event_stream(), media_type="text/event-stream")


    @router.post("/api/models/pull")
    def api_pull_model_legacy(
        req: ModelInstallRequest,
        background_tasks: BackgroundTasks,
        request: Request,
    ):
        require_local_workbench(request)
        _require_ollama()
        job_id = create_id("job")
        database.upsert_model_install_job(job_id, req.model, "queued", 0, message="Queued model install.")
        _register_model_install(job_id)
        threading.Thread(target=_model_install_worker, args=(job_id, req.model), daemon=True).start()

        async def event_stream():
            last_payload = None
            for _ in range(3600):
                job = database.get_model_install_job(job_id)
                if not job:
                    yield encode_sse("error", {"message": "找不到模型安裝工作。"})
                    return
                payload = {
                    "job_id": job_id,
                    "model": req.model,
                    "status": job["status"],
                    "progress": job["progress"],
                    "percent": job["progress"],
                    "downloaded_bytes": job["downloaded_bytes"],
                    "completed": job["downloaded_bytes"],
                    "total_bytes": job["total_bytes"],
                    "total": job["total_bytes"],
                    "message": job.get("message"),
                }
                if payload != last_payload:
                    yield encode_sse("pull_progress", payload)
                    last_payload = payload
                if job["status"] == "ready":
                    yield encode_sse("done", {"job_id": job_id, "status": "ready"})
                    return
                if job["status"] in {"failed", "cancelled"}:
                    yield encode_sse("error", {"message": job.get("error") or job.get("message") or "模型安裝失敗。"})
                    return
                await asyncio.sleep(1)
            yield encode_sse("error", {"message": "模型安裝逾時。"})

        return StreamingResponse(event_stream(), media_type="text/event-stream")


    @router.post("/api/models/benchmark")
    def api_benchmark_model(req: BenchmarkRequest, request: Request):
        require_local_workbench(request)
        _require_ollama()
        return _run_model_benchmark(
            req,
            load_settings=load_settings,
            error_payload=error_payload,
            require_ollama=_require_ollama,
        )


    @router.post("/api/models/select")
    def select_model(req: SelectModelRequest):
        cfg = load_settings()
        if req.scope == "global":
            cfg["default_chat_model"] = req.model
            save_settings(cfg)
        elif req.scope == "session" and req.session_id:
            database.create_session(req.session_id, model=req.model)
        return {"success": True, "model": req.model, "scope": req.scope, "session_id": req.session_id}


    @router.delete("/api/models/{model_name:path}")
    def api_delete_model(model_name: str, request: Request):
        require_local_workbench(request)
        _require_ollama()
        try:
            response = requests.delete(f"{load_settings()['ollama_url']}/api/delete", json={"name": model_name}, timeout=30)
            if response.status_code == 200:
                return {"success": True, "model": model_name}
            raise HTTPException(status_code=502, detail=error_payload("OLLAMA_DELETE_ERROR", "Ollama model delete failed.", response.text))
        except requests.exceptions.ConnectionError:
            raise HTTPException(status_code=502, detail=error_payload("OLLAMA_NOT_CONNECTED", "Ollama is not connected."))


    @router.get("/api/models/info")
    def api_model_info(name: str):
        _require_ollama()
        try:
            response = requests.post(f"{load_settings()['ollama_url']}/api/show", json={"name": name}, timeout=30)
            if response.status_code != 200:
                raise HTTPException(status_code=404, detail=error_payload("MODEL_NOT_FOUND", "Model not found.", response.text))
            data = response.json()
            details = data.get("details", {}) or {}
            model_info = data.get("model_info", {}) or {}
            context_window = next((v for k, v in model_info.items() if k.endswith("context_length")), None)
            return {"name": name, "context_window": context_window, "parameter_size": details.get("parameter_size"), "quantization": details.get("quantization_level"), "family": details.get("family")}
        except HTTPException:
            raise
        except requests.exceptions.ConnectionError:
            raise HTTPException(status_code=502, detail=error_payload("OLLAMA_NOT_CONNECTED", "Ollama is not connected."))


    @router.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        run = database.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail=error_payload("RUN_NOT_FOUND", "Run not found.", recoverable=False))
        if str(run.get("mode") or "").strip().casefold() == "email":
            # Integration runs are deliberately available only through the
            # mail integration projection.  Do not let a known Run ID expose
            # even metadata through the legacy chat endpoint.
            raise HTTPException(status_code=404, detail=error_payload("RUN_NOT_FOUND", "Run not found.", recoverable=False))
        session = database.get_session(str(run.get("session_id") or ""))
        if not session or session.get("project_id") != run.get("project_id"):
            raise HTTPException(
                status_code=409,
                detail=error_payload(
                    "RUN_SCOPE_CHANGED",
                    "The run no longer belongs to the session's active project scope.",
                    recoverable=False,
                ),
            )
        # This legacy endpoint is intentionally metadata-only.  Execution,
        # Results and Project Skills each have a separately scoped public
        # projection; returning the database row here would expose historical
        # raw events, provider diagnostics and source contents.
        return {
            "run_id": run_id,
            "session_id": run.get("session_id"),
            "turn_id": run.get("turn_id"),
            "project_id": run.get("project_id"),
            "retry_of_run_id": run.get("retry_of_run_id"),
            "model": run.get("model"),
            "mode": run.get("mode"),
            "status": run.get("status"),
            "execution_revision": int(run.get("execution_revision") or 0),
            "created_at": run.get("created_at"),
            "completed_at": run.get("completed_at"),
            "input_manifest": run.get("input_manifest") or {},
        }


    router.model_install_worker = _model_install_worker
    router.cancel_model_install = _cancel_model_install
    router.has_model_install_control = _has_model_install_control
    router.reset_model_install_controls = lambda: _MODEL_INSTALL_CONTROLS.clear()
    return router
