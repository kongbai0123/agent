"""Local AI Workbench application entry point.

The production runtime is intentionally a single conversational model loop.
This module only composes chat, sessions, projects, attachments, settings, and
model-provider management; orchestration, tools, retrieval pipelines, and
background automation are not part of the application graph.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import database
from api.routes.attachments import build_attachments_router
from api.routes.chat import router as chat_router
from api.routes.hermes import build_hermes_router
from api.routes.models import build_models_router
from api.routes.project_skills import build_project_skills_router
from api.routes.projects import build_projects_router
from api.routes.sessions import build_sessions_router
from api.routes.settings import build_settings_router
from api.routes.system import build_system_router
from api.schemas.chat import ChatRequest
from chat.events import encode_sse
from chat.hermes_runtime import stream_hermes_chat
from chat.runtime import stream_basic_chat
from chat_cancellation import (
    cancel_chat_run,
    cancel_or_defer_chat_run,
    cancel_session_chat_runs,
    get_chat_run,
    register_chat_run,
    release_chat_run,
)
from conversation_store import archive_session, ensure_session_folder, export_session
from core.settings import (
    apply_network_settings,
    load_settings,
    normalize_modal_size,
    save_settings,
    validate_settings as validate_chat_settings,
)
from local_session import (
    SESSION_COOKIE_NAME,
    install_local_session_guard,
    is_local_origin,
    session_token,
    write_token_file,
)
from model_client import (
    list_all_models as provider_model_inventory,
    model_profile_for_model,
    provider_for_model,
    uses_local_model_slot,
)
from hermes_factory import (
    HermesIntegrationManagerCache,
    HermesIntegrationManagerFactory,
)
from hermes_rollout import HermesRolloutError, HermesRolloutGate
from hermes_supervisor import HermesHealthSupervisor
from ollama_cleanup import loaded_models_snapshot
from paths import PROJECTS_ROOT, REPO_ROOT, ensure_runtime_dirs
from pdf_parser import extract_pdf_text
from project_storage import (
    attachments_dir as project_attachments_dir,
    imports_dir as project_imports_dir,
    migrate_legacy_storage,
    move_session as move_session_storage,
    project_dir as project_storage_dir,
    session_project_id,
)
from project_skill_runtime import ProjectSkillRuntime
from project_skills import ProjectSkillError, ProjectSkillStore
from runtime_manager import export_session_zip
from startup_progress import complete_startup, read_startup_status, update_startup
from workspace import (
    context_for_project,
    context_payload,
    managed_project_path,
    normalize_path,
    path_status,
    validate_project_path,
    write_project_manifest,
)


APP_VERSION = "0.6.0-chat-reset"
SETTINGS_PATH = str(
    Path(
        os.environ.get("WORKBENCH_SETTINGS_PATH")
        or Path(__file__).resolve().with_name("settings.json")
    ).resolve()
)
hermes_manager_cache: Optional[HermesIntegrationManagerCache] = None
hermes_health_supervisor: Optional[HermesHealthSupervisor] = None
hermes_rollout_gate: Optional[HermesRolloutGate] = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def sse(event: str, data: Dict[str, Any]) -> str:
    return encode_sse(event, data)


def error_payload(
    code: str,
    message: str,
    detail: Optional[str] = None,
    recoverable: bool = True,
    suggestions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "success": False,
        "code": code,
        "message": message,
        "content": message,
        "detail": detail,
        "recoverable": recoverable,
        "suggestions": suggestions or [],
    }


def require_local_workbench(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and not re.match(
        r"^https?://(?:127\.0\.0\.1|localhost)(?::\d+)?$",
        origin,
        re.IGNORECASE,
    ):
        raise HTTPException(
            status_code=403,
            detail=error_payload(
                "LOCAL_ORIGIN_REQUIRED",
                "此管理功能只允許本機工作台使用。",
                recoverable=False,
            ),
        )


def validate_settings(data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return validate_chat_settings(data)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=error_payload("INVALID_SETTINGS", str(exc)),
        ) from exc


def normalize_settings_modal_size(data: Dict[str, Any]) -> Dict[str, int]:
    try:
        return normalize_modal_size(data)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=error_payload("INVALID_SETTINGS", str(exc)),
        ) from exc


def apply_runtime_configuration(settings: Dict[str, Any]) -> None:
    apply_network_settings(settings)
    cache = hermes_manager_cache
    if cache is not None:
        # Hermes is optional. A missing sidecar/key/receipt is reflected by its
        # status endpoint and must never make an otherwise valid settings save
        # or the basic chat runtime unavailable.
        cache.try_get(settings)


def base_hermes_status() -> Dict[str, Any]:
    """Return the manager status without recursively adding rollout controls."""

    settings = load_settings()
    cache = hermes_manager_cache
    if cache is None:
        return {
            "enabled": bool(settings.get("hermes_enabled")),
            "configured": False,
            "health": {"status": "unhealthy", "reason": "manager_unavailable"},
            "operations": {},
        }
    return cache.status(settings)


def hermes_operational_status() -> Dict[str, Any]:
    """Compose manager plus supervisor state without rollout recursion."""
    status = dict(base_hermes_status())
    supervisor = hermes_health_supervisor
    supervisor_status = (
        supervisor.status()
        if supervisor is not None
        else {
            "running": False,
            "state": "stopped",
            "last_reason": "supervisor_unavailable",
        }
    )
    status["supervisor"] = supervisor_status
    return status


def hermes_status_payload() -> Dict[str, Any]:
    """Compose one redacted production status for the local control API."""

    status = hermes_operational_status()
    gate = hermes_rollout_gate
    if gate is not None:
        status["rollout_control"] = gate.readiness(
            load_settings(), status=status
        )
    return status


def guard_hermes_rollout(
    current_settings: Dict[str, Any],
    requested_settings: Dict[str, Any],
) -> None:
    gate = hermes_rollout_gate
    if gate is None:
        raise HTTPException(
            status_code=503,
            detail=error_payload(
                "HERMES_ROLLOUT_GATE_UNAVAILABLE",
                "Hermes rollout controls are unavailable.",
            ),
        )
    try:
        gate.guard(current_settings, requested_settings)
    except HermesRolloutError as exc:
        raise HTTPException(
            status_code=409,
            detail=error_payload(exc.code, str(exc), recoverable=True),
        ) from exc


def rollback_hermes_rollout() -> Dict[str, Any]:
    """Fail safely back to basic chat without deleting Hermes runtime data."""

    cfg = validate_settings(
        {
            "hermes_rollout_mode": "disabled",
            "hermes_rollout_percentage": 0.0,
            "hermes_canary_session_ids": [],
            "hermes_tools_enabled": False,
            "hermes_allowed_capabilities": [],
            "hermes_readonly_project_id": "",
        }
    )
    save_settings(cfg)
    apply_runtime_configuration(cfg)
    return {
        "rolled_back": True,
        "rollout": {"mode": "disabled", "percentage": 0.0},
        "tools_enabled": False,
        "preserved_runtime_data": True,
    }


def model_inventory() -> List[Dict[str, Any]]:
    try:
        return [
            item
            for item in provider_model_inventory(load_settings(), timeout=5)
            if isinstance(item, dict) and item.get("name")
        ]
    except Exception:
        return []


def ollama_models() -> List[str]:
    return [str(item["name"]) for item in model_inventory()]


def disabled_service_status() -> Dict[str, Any]:
    return {
        "enabled": False,
        "index_status": "disabled",
        "document_count": 0,
        "chunk_count": 0,
        "embedding_model": None,
    }


def effective_config(settings: Dict[str, Any]) -> Dict[str, Any]:
    available = set(ollama_models())
    return {
        "ollama_connected": bool(available),
        "chat_model_available": settings.get("default_chat_model") in available,
        "vision_model_available": settings.get("default_vision_model") in available,
    }


def sync_session_archive(session_id: str) -> bool:
    try:
        return export_session(session_id) is not None
    except Exception:
        return False


def get_chat_user_message(request: ChatRequest) -> str:
    if request.message and request.message.strip():
        return request.message.strip()
    for item in reversed(request.messages):
        if item.role == "user" and item.content.strip():
            return item.content.strip()
    raise HTTPException(
        status_code=400,
        detail=error_payload("EMPTY_MESSAGE", "A user message is required."),
    )


def configure_chat_run_billing(
    run_control: Any,
    settings: Dict[str, Any],
    model: str,
    project_id: Optional[str],
) -> Any:
    run_control.project_id = project_id
    try:
        provider = provider_for_model(settings, model, project_id=project_id)
        profile = model_profile_for_model(settings, model, project_id=project_id)
    except (PermissionError, ValueError) as exc:
        release_chat_run(run_control.run_id, run_control)
        raise HTTPException(
            status_code=409,
            detail=error_payload(
                "MODEL_PROVIDER_UNAVAILABLE", str(exc), recoverable=True
            ),
        ) from exc
    if not profile.eligible_for_primary:
        release_chat_run(run_control.run_id, run_control)
        raise HTTPException(
            status_code=409,
            detail=error_payload(
                "MODEL_NOT_CHAT_CAPABLE",
                f"{profile.kind} models cannot be used for chat.",
                recoverable=False,
            ),
        )
    run_control.configure_billing(
        provider=provider.provider,
        input_cost_per_million=provider.input_cost_per_million,
        output_cost_per_million=provider.output_cost_per_million,
        currency=provider.currency,
    )
    return provider


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    try:
        write_token_file()
    except Exception as exc:  # pragma: no cover - startup best effort
        print(f"[SESSION] Unable to persist session token: {exc}")
    complete_startup()
    cache = hermes_manager_cache
    supervisor = hermes_health_supervisor
    if supervisor is not None:
        try:
            await supervisor.start()
            await supervisor.probe_once()
        except Exception as exc:  # pragma: no cover - startup best effort
            print(f"[HERMES] Health supervisor failed: {type(exc).__name__}")
    try:
        yield
    finally:
        if supervisor is not None:
            await supervisor.stop()
        if cache is not None:
            cache.close()


ensure_runtime_dirs()
apply_network_settings(load_settings())
update_startup(
    "database",
    "正在檢查工作區資料庫。",
    detail="建立或更新本機資料結構",
    progress_percent=28,
)
database.init_db()
migrate_legacy_storage()
update_startup(
    "workspace",
    "正在準備聊天工作區。",
    detail="載入模型、對話與介面設定",
    progress_percent=92,
)


app = FastAPI(title="Local AI Workbench Chat API", lifespan=app_lifespan)
install_local_session_guard(app, error_payload)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Workbench-Token"],
)


@app.middleware("http")
async def add_browser_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), geolocation=(), payment=(), usb=()"
    )
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'")
    return response


system_router = build_system_router(
    app_version=APP_VERSION,
    # The public system route is consumed by the browser as a list of model
    # names.  Keep the richer inventory internal to provider/model management.
    model_inventory=ollama_models,
    settings_loader=load_settings,
    startup_status=read_startup_status,
)

models_router = build_models_router(
    database=database,
    load_settings=load_settings,
    save_settings=save_settings,
    error_payload=error_payload,
    create_id=create_id,
    require_local_workbench=require_local_workbench,
    rag_stats=disabled_service_status,
    ollama_models=ollama_models,
    require_extension=lambda _extension_id, _project_id=None: None,
    app_version=APP_VERSION,
    agent_protocol_version=1,
)

settings_router = build_settings_router(
    load_settings=load_settings,
    save_settings=save_settings,
    validate_settings=validate_settings,
    effective_config=effective_config,
    normalize_modal_size=normalize_settings_modal_size,
    apply_configuration=apply_runtime_configuration,
    error_payload=error_payload,
    require_local=require_local_workbench,
    hermes_rollout_guard=guard_hermes_rollout,
)

projects_router = build_projects_router(
    database=database,
    require_local=require_local_workbench,
    error_payload=error_payload,
    create_id=create_id,
    default_project_root=PROJECTS_ROOT,
    validate_project_path=validate_project_path,
    managed_project_path=managed_project_path,
    path_status=path_status,
    write_project_manifest=write_project_manifest,
    context_for_project=context_for_project,
    context_payload=context_payload,
    project_storage_dir=project_storage_dir,
    normalize_path=normalize_path,
)

project_skill_store = ProjectSkillStore(
    database=database,
    project_dir_factory=project_storage_dir,
)
project_skill_runtime = ProjectSkillRuntime(
    store=project_skill_store,
    database=database,
)
hermes_manager_cache = HermesIntegrationManagerCache(
    HermesIntegrationManagerFactory(project_skill_runtime)
)
hermes_health_supervisor = HermesHealthSupervisor(
    settings_loader=load_settings,
    manager_provider=lambda settings: hermes_manager_cache.try_get(settings),
    probe_interval_seconds=10,
    failure_threshold=3,
)
hermes_rollout_gate = HermesRolloutGate(status_provider=hermes_operational_status)
project_skills_router = build_project_skills_router(
    store=project_skill_store,
    runtime=project_skill_runtime,
    require_local=require_local_workbench,
    error_payload=error_payload,
)

sessions_router = build_sessions_router(
    database=database,
    create_id=create_id,
    now_iso=now_iso,
    error_payload=error_payload,
    ensure_session_folder=ensure_session_folder,
    sync_session_archive=sync_session_archive,
    move_session_storage=move_session_storage,
    archive_session=archive_session,
    export_session_zip=export_session_zip,
)

attachments_router = build_attachments_router(
    database=database,
    error_payload=error_payload,
    create_id=create_id,
    session_project_id=session_project_id,
    project_imports_dir=project_imports_dir,
    project_attachments_dir=project_attachments_dir,
    extract_pdf_text=extract_pdf_text,
    sync_session_archive=sync_session_archive,
)

hermes_router = build_hermes_router(
    manager_provider=lambda: hermes_manager_cache.try_get(load_settings()),
    status_provider=hermes_status_payload,
    require_local=require_local_workbench,
    error_payload=error_payload,
    cancel_local_run=cancel_chat_run,
    rollback_handler=rollback_hermes_rollout,
)

for domain_router in (
    system_router,
    sessions_router,
    projects_router,
    project_skills_router,
    attachments_router,
    hermes_router,
    settings_router,
    models_router,
):
    app.include_router(domain_router)


@chat_router.post("/api/chat")
async def chat(request: ChatRequest):
    user_query = get_chat_user_message(request)
    settings = load_settings()
    model = request.model or settings["default_chat_model"]
    session_id = request.session_id or create_id("sess")
    turn_id = create_id("turn")
    run_id = request.run_id or create_id("run")

    if request.run_id and (
        get_chat_run(run_id) is not None or database.get_run(run_id) is not None
    ):
        raise HTTPException(
            status_code=409,
            detail=error_payload(
                "RUN_ID_ALREADY_EXISTS",
                "This run_id is already bound to another turn.",
            ),
        )

    database.create_session(session_id, model=model)
    session = database.get_session(session_id)
    project = (
        database.get_project(session["project_id"])
        if session and session.get("project_id")
        else None
    )
    project_id = project.get("id") if project else None

    # Hermes currently accepts text-only turns. Images and stored attachments
    # stay on the mature basic-chat path until their boundary is explicitly
    # reviewed. A missing/invalid optional sidecar also resolves to basic chat.
    hermes_manager = None
    if (
        settings.get("hermes_enabled")
        and not request.images
        and not request.attachment_ids
    ):
        hermes_manager = hermes_manager_cache.try_get(settings)

    hermes_skill_attachment = None
    project_skill_context = ""
    try:
        if hermes_manager is not None:
            # Resolve once and pass the exact same immutable attachment to both
            # Hermes and its basic-chat fallback. This is important for
            # one-turn Skill activation and provenance identity.
            hermes_skill_attachment = hermes_manager.prepare_project_skills(
                session_id,
                user_query,
                run_id=run_id,
                consume_turn=True,
            )
            project_skill_context = hermes_skill_attachment.instructions
        else:
            project_skill_prompt = project_skill_runtime.build_prompt_context(
                session_id,
                user_query,
                run_id=run_id,
                consume_turn=True,
            )
            project_skill_context = str(project_skill_prompt.get("context") or "")
    except ProjectSkillError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=error_payload(
                exc.code,
                str(exc),
                recoverable=exc.status_code < 500,
            ),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail=error_payload(
                "PROJECT_SKILL_CONTEXT_INVALID",
                "Project Skill context could not be prepared for this turn.",
                recoverable=True,
            ),
        ) from exc

    cancel_session_chat_runs(session_id, exclude_run_id=run_id)
    normalized_prompt = user_query.replace("\r\n", "\n").replace("\r", "\n").strip()
    prompt_sha256 = hashlib.sha256(normalized_prompt.encode("utf-8")).hexdigest()
    run_control = register_chat_run(
        run_id,
        session_id,
        turn_id,
        model,
        "chat",
        prompt_digest=prompt_sha256,
    )
    run_control.start_deadline(settings.get("chat_run_budget_seconds"))

    configure_chat_run_billing(run_control, settings, model, project_id)
    if uses_local_model_slot(settings, model, project_id=project_id):
        snapshot = loaded_models_snapshot(settings["ollama_url"])
        run_control.set_preexisting_models(snapshot)
    else:
        run_control.set_preexisting_models(None)

    ensure_session_folder(session_id, model=model)
    user_message_id = database.add_message(
        session_id,
        "user",
        user_query,
        visible_content=user_query,
        llm_content=user_query,
        turn_id=turn_id,
    )

    temporary_text = request.temporary_context or ""
    if request.temporary_context_id:
        context = database.get_temporary_context(request.temporary_context_id)
        if context:
            temporary_text = context["text"]

    images = [image.split(",", 1)[1] if "," in image else image for image in request.images]
    for attachment_id in request.attachment_ids:
        attachment = database.get_attachment(attachment_id)
        if attachment and os.path.exists(attachment["storage_path"]):
            with open(attachment["storage_path"], "rb") as file:
                images.append(base64.b64encode(file.read()).decode("utf-8"))

    async def basic_stream(skill_attachment=None):
        fallback_skill_context = (
            str(getattr(skill_attachment, "instructions", "") or "")
            if skill_attachment is not None
            else project_skill_context
        )
        async for item in stream_basic_chat(
            request,
            settings=settings,
            model=model,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            prompt_sha256=prompt_sha256,
            user_message_id=user_message_id,
            user_query=user_query,
            temporary_context=temporary_text,
            images=images,
            run_control=run_control,
            project_id=project_id,
            project_skill_context=fallback_skill_context,
            archive_sync=sync_session_archive,
        ):
            yield item

    async def event_stream():
        try:
            if hermes_manager is not None:
                async for item in stream_hermes_chat(
                    manager=hermes_manager,
                    model=model,
                    session_id=session_id,
                    turn_id=turn_id,
                    run_id=run_id,
                    prompt_sha256=prompt_sha256,
                    user_message_id=user_message_id,
                    user_query=user_query,
                    temporary_context=temporary_text,
                    run_control=run_control,
                    fallback_stream_factory=basic_stream,
                    attachment=hermes_skill_attachment,
                    archive_sync=sync_session_archive,
                ):
                    yield item
            else:
                async for item in basic_stream():
                    yield item
        finally:
            release_chat_run(run_id, run_control)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store"},
    )


@chat_router.post("/api/chat/runs/{run_id}/cancel")
def cancel_active_chat(run_id: str, request: Request):
    require_local_workbench(request)
    existing = database.get_run(run_id)
    if existing and existing.get("status") in {"completed", "failed", "cancelled"}:
        return {
            "success": True,
            "run_id": run_id,
            "cancelled": existing.get("status") == "cancelled",
            "already_finished": True,
        }
    result = cancel_chat_run(run_id)
    return {"success": True, **(result or cancel_or_defer_chat_run(run_id))}


app.include_router(chat_router)


FRONTEND_DIR = REPO_ROOT / "frontend"
INDEX_TEMPLATE_PATH = FRONTEND_DIR / "index.html"


def _set_browser_session_cookie(response: Response) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token(),
        httponly=True,
        secure=False,
        samesite="strict",
        path="/",
    )


def _index_response() -> HTMLResponse:
    nonce = secrets.token_urlsafe(24)
    html = INDEX_TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "{{CSP_NONCE}}", nonce
    )
    response = HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-src 'self' blob:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )
    _set_browser_session_cookie(response)
    return response


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def frontend_index() -> HTMLResponse:
    return _index_response()


@app.post("/session/bootstrap", include_in_schema=False)
async def bootstrap_browser_session(request: Request) -> Response:
    origin = request.headers.get("origin")
    expected_origin = str(request.base_url).rstrip("/")
    if not origin or origin.rstrip("/") != expected_origin or not is_local_origin(origin):
        return Response(status_code=403)
    response = Response(status_code=204, headers={"Cache-Control": "no-store, max-age=0"})
    _set_browser_session_cookie(response)
    return response


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
