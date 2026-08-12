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
import stat
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
from api.routes.run_results import build_run_results_router
from api.routes.sessions import build_sessions_router
from api.routes.settings import build_settings_router
from api.routes.system import build_system_router
from api.schemas.chat import ChatRequest
from chat.events import encode_sse
from chat.hermes_runtime import stream_hermes_chat
from chat.runtime import (
    completed_conversation_history,
    normalize_history_snapshot,
    stream_basic_chat,
)
from chat_cancellation import (
    cancel_chat_run,
    cancel_or_defer_chat_run,
    cancel_session_chat_runs,
    get_chat_run,
    has_active_chat_run,
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
from hermes_approval_store import PersistentHermesApprovalStore
from hermes_project_skills_bridge import HermesProjectSkillsAttachment
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
from structured_log import redact
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


def _run_public_error(run: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    metrics = run.get("metrics") if isinstance(run.get("metrics"), dict) else {}
    raw = metrics.get("error") if isinstance(metrics, dict) else None
    if isinstance(raw, dict):
        return {
            "code": str(raw.get("code") or "RUN_FAILED")[:128],
            "message": str(raw.get("message") or "The run failed.")[:1000],
            "recoverable": bool(raw.get("recoverable")),
        }
    if run.get("status") == "cancelled":
        return {
            "code": "RUN_CANCELLED",
            "message": "The run was cancelled.",
            "recoverable": True,
        }
    return None


def _public_run_tasks(value: Any) -> List[Dict[str, Any]]:
    """Project persisted task rows into a small display-only contract."""

    if not isinstance(value, (list, tuple)):
        return []
    allowed_statuses = {
        "pending", "queued", "in_progress", "running", "completed",
        "failed", "cancelled", "skipped", "waiting_approval",
    }
    result: List[Dict[str, Any]] = []
    for index, raw in enumerate(value[:100]):
        if not isinstance(raw, dict):
            continue
        task_id = str(redact(raw.get("id") or f"task-{index + 1}", key="id"))
        label = str(
            redact(
                raw.get("label") or raw.get("title") or "Agent step",
                key="label",
            )
        )
        status = str(raw.get("status") or "pending").strip().casefold()
        if status not in allowed_statuses:
            status = "pending"
        result.append(
            {
                "id": "".join(char for char in task_id[:128] if ord(char) >= 32),
                "label": "".join(char for char in label[:240] if ord(char) >= 32),
                "status": status,
            }
        )
    return result


def _temporary_context_is_current(context: Dict[str, Any]) -> bool:
    expires_at = str(context.get("expires_at") or "")
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return expires > datetime.now(timezone.utc)


def _authorized_attachment_path(
    attachment: Optional[Dict[str, Any]],
    *,
    session_id: str,
    project_id: Optional[str],
) -> Optional[Path]:
    if not attachment:
        return None
    if (
        attachment.get("session_id") != session_id
        or attachment.get("project_id") != project_id
    ):
        return None
    candidate = Path(str(attachment.get("storage_path") or ""))
    try:
        if not candidate.is_absolute():
            return None
        root = Path(
            project_attachments_dir(
                session_id,
                project_id,
            )
        )
        if not root.is_absolute():
            return None

        # Compare the stored path before resolving it.  Resolving first would
        # erase the evidence that the attachment itself (or a parent directory)
        # is a symlink/junction and could make a link inside the managed folder
        # look like an ordinary file.
        raw_root = Path(os.path.abspath(root))
        raw_candidate = Path(os.path.abspath(candidate))
        if os.path.normcase(str(raw_candidate.parent)) != os.path.normcase(
            str(raw_root)
        ):
            return None

        # Fail closed on every existing component in the managed path chain,
        # including the unresolved candidate.  On Windows, directory junctions
        # are reparse points even when stat.S_ISLNK() is false.
        paths: List[Path] = []
        current = Path(raw_root.anchor)
        if raw_root.anchor:
            paths.append(current)
        for part in raw_root.parts[1:]:
            current = current / part
            paths.append(current)
        paths.append(raw_candidate)
        for path in paths:
            info = os.lstat(path)
            attributes = int(getattr(info, "st_file_attributes", 0) or 0)
            if stat.S_ISLNK(info.st_mode) or bool(
                attributes
                & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            ):
                return None

        resolved_root = raw_root.resolve(strict=True)
        resolved = raw_candidate.resolve(strict=True)
        if (
            os.path.normcase(str(resolved.parent))
            != os.path.normcase(str(resolved_root))
            or not stat.S_ISREG(os.stat(resolved, follow_symlinks=False).st_mode)
        ):
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def _retry_eligibility(run: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    status = str(run.get("status") or "").casefold()
    if status not in {"completed", "failed", "cancelled"}:
        return False, "run_not_terminal"
    if status == "completed":
        return False, "run_completed"
    error = _run_public_error(run)
    if not error or error.get("recoverable") is not True:
        return False, "error_not_recoverable"

    session_id = str(run.get("session_id") or "")
    session = database.get_session(session_id)
    if not session:
        return False, "session_missing"
    project_id = run.get("project_id")
    if session.get("project_id") != project_id:
        return False, "project_scope_changed"
    if project_id:
        project = database.get_project(str(project_id))
        if not project or project.get("archived"):
            return False, "project_unavailable"

    manifest = database.get_run_input_manifest(str(run.get("run_id") or ""))
    if (
        not isinstance(manifest, dict)
        or int(manifest.get("version") or 0) != 1
        or manifest.get("reproducible") is not True
    ):
        return False, str(manifest.get("reason") or "input_manifest_unavailable")
    try:
        message_id = int(manifest.get("user_message_id"))
    except (TypeError, ValueError):
        return False, "user_input_unavailable"
    snapshot_message = str(manifest.get("user_message") or "").strip()
    history_snapshot = manifest.get("history_snapshot")
    if not snapshot_message or not isinstance(history_snapshot, list):
        return False, "input_snapshot_unavailable"
    if normalize_history_snapshot(history_snapshot) != history_snapshot:
        return False, "input_snapshot_invalid"
    normalized_snapshot = snapshot_message.replace("\r\n", "\n").replace(
        "\r", "\n"
    ).strip()
    if hashlib.sha256(normalized_snapshot.encode("utf-8")).hexdigest() != str(
        manifest.get("prompt_sha256") or ""
    ):
        return False, "input_snapshot_invalid"
    message = database.get_message(message_id)
    if (
        not message
        or message.get("role") != "user"
        or message.get("session_id") != session_id
        or message.get("turn_id") != run.get("turn_id")
        or not str(message.get("llm_content") or "").strip()
        or str(message.get("llm_content") or "").strip() != snapshot_message
    ):
        return False, "user_input_unavailable"

    context_id = manifest.get("temporary_context_id")
    if context_id:
        context = database.get_temporary_context(str(context_id))
        if (
            not context
            or context.get("session_id") != session_id
            or not _temporary_context_is_current(context)
        ):
            return False, "temporary_context_unavailable"

    for attachment_id in manifest.get("attachment_ids") or []:
        attachment = database.get_attachment(str(attachment_id))
        attachment_path = _authorized_attachment_path(
            attachment,
            session_id=session_id,
            project_id=project_id,
        )
        if attachment_path is None:
            return False, "attachment_unavailable"
    return True, None


def _retry_request(
    request: ChatRequest,
) -> tuple[ChatRequest, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    source_id = request.retry_of_run_id
    if not source_id:
        return request, None, None
    source = database.get_run(source_id)
    if not source:
        raise HTTPException(
            status_code=404,
            detail=error_payload(
                "RETRY_RUN_NOT_FOUND", "The source run was not found.", recoverable=False
            ),
        )
    allowed, reason = _retry_eligibility(source)
    if not allowed:
        raise HTTPException(
            status_code=409,
            detail=error_payload(
                "RUN_RETRY_NOT_ALLOWED",
                "This run cannot be retried with its original input.",
                detail=reason,
                recoverable=False,
            ),
        )
    if request.session_id and request.session_id != source.get("session_id"):
        raise HTTPException(
            status_code=409,
            detail=error_payload(
                "RETRY_SESSION_MISMATCH",
                "A retry must stay in the source run's session.",
                recoverable=False,
            ),
        )
    if request.model and request.model != source.get("model"):
        raise HTTPException(
            status_code=409,
            detail=error_payload(
                "RETRY_MODEL_MISMATCH",
                "A whole-run retry must use the source run's model.",
                recoverable=False,
            ),
        )
    manifest = database.get_run_input_manifest(source_id)
    restored = ChatRequest(
        session_id=str(source["session_id"]),
        message=str(manifest.get("user_message") or ""),
        model=str(source.get("model") or "") or None,
        mode="chat",
        use_rag=False,
        attachment_ids=list(manifest.get("attachment_ids") or []),
        images=[],
        temporary_context_id=manifest.get("temporary_context_id"),
        temporary_context=str(manifest.get("temporary_context") or ""),
        run_id=request.run_id,
        retry_of_run_id=source_id,
    )
    return restored, source, manifest


def _resolve_chat_inputs(
    request: ChatRequest,
    *,
    session_id: str,
    project_id: Optional[str],
) -> tuple[str, List[str]]:
    temporary_text = str(request.temporary_context or "")
    if request.temporary_context_id:
        context = database.get_temporary_context(request.temporary_context_id)
        if (
            not context
            or context.get("session_id") != session_id
            or not _temporary_context_is_current(context)
        ):
            raise HTTPException(
                status_code=409,
                detail=error_payload(
                    "TEMPORARY_CONTEXT_SCOPE_MISMATCH",
                    "Temporary context is unavailable for this session.",
                    recoverable=True,
                ),
            )
        temporary_text = str(context.get("text") or "")

    images = [
        image.split(",", 1)[1] if "," in image else image
        for image in request.images
    ]
    seen: set[str] = set()
    for attachment_id in request.attachment_ids:
        if attachment_id in seen:
            continue
        seen.add(attachment_id)
        attachment = database.get_attachment(attachment_id)
        attachment_path = _authorized_attachment_path(
            attachment,
            session_id=session_id,
            project_id=project_id,
        )
        if attachment_path is None:
            raise HTTPException(
                status_code=409,
                detail=error_payload(
                    "ATTACHMENT_SCOPE_MISMATCH",
                    "An attachment is unavailable for this session and project.",
                    recoverable=True,
                ),
            )
        assert attachment is not None
        with attachment_path.open("rb") as file:
            images.append(base64.b64encode(file.read()).decode("utf-8"))
    return temporary_text, images


def _input_manifest(
    request: ChatRequest,
    *,
    user_message_id: int,
    prompt_sha256: str,
    project_id: Optional[str],
    project_skill_context: str,
    project_skill_provenance: List[Dict[str, Any]],
    project_skills_truncated: bool,
    runtime_route: str,
    user_query: str,
    history_snapshot: List[Dict[str, str]],
) -> Dict[str, Any]:
    inline_image_count = len(request.images)
    return {
        "version": 1,
        "reproducible": inline_image_count == 0,
        "reason": "inline_images_not_persisted" if inline_image_count else None,
        "user_message_id": int(user_message_id),
        "user_message": str(user_query),
        "prompt_sha256": prompt_sha256,
        "history_snapshot": [dict(item) for item in history_snapshot],
        "project_id": project_id,
        "attachment_ids": list(dict.fromkeys(request.attachment_ids)),
        "temporary_context_id": request.temporary_context_id,
        "temporary_context": str(request.temporary_context or ""),
        "inline_image_count": inline_image_count,
        "project_skill_context": project_skill_context,
        "project_skill_provenance": project_skill_provenance,
        "project_skills_truncated": bool(project_skills_truncated),
        "runtime_route": runtime_route,
    }


def _hydrate_execution_approvals(
    run_id: str,
    events: List[Dict[str, Any]],
    *,
    session_id: str,
    project_id: Optional[str],
) -> List[Dict[str, Any]]:
    try:
        approvals = PersistentHermesApprovalStore().list_for_run(run_id)
    except Exception:
        approvals = []
    by_id = {
        item.approval_id: item.public_dict()
        for item in approvals
        if item.workbench_session_id == session_id
        and item.project_id == project_id
    }
    hydrated: List[Dict[str, Any]] = []
    seen: set[str] = set()
    max_sequence = 0
    for raw in events:
        event = dict(raw) if isinstance(raw, dict) else {}
        max_sequence = max(max_sequence, int(event.get("sequence") or 0))
        payload = dict(event.get("payload") or {})
        approval_id = str(payload.get("approval_id") or "")
        if event.get("event") == "approval_required" and approval_id in by_id:
            current = by_id[approval_id]
            payload.update(
                {
                    "status": current["status"],
                    "choices": current["choices"],
                    "updated_at": current["updated_at"],
                    "rationale": current["rationale"],
                }
            )
            event["payload"] = payload
            seen.add(approval_id)
        hydrated.append(event)
    for approval_id, current in by_id.items():
        if approval_id in seen:
            continue
        max_sequence += 1
        hydrated.append(
            {
                "event": "approval_required",
                "sequence": max_sequence,
                "created_at": current["created_at"],
                "payload": {
                    "approval_id": approval_id,
                    "capability": current["capability"],
                    "summary": current["summary"],
                    "message": current["summary"],
                    "run_id": run_id,
                    "status": current["status"],
                    "choices": current["choices"],
                    "updated_at": current["updated_at"],
                    "rationale": current["rationale"],
                },
            }
        )
    return hydrated


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
    has_active_chat_run=has_active_chat_run,
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

run_results_router = build_run_results_router(
    database=database,
    error_payload=error_payload,
)

for domain_router in (
    system_router,
    sessions_router,
    projects_router,
    project_skills_router,
    attachments_router,
    hermes_router,
    run_results_router,
    settings_router,
    models_router,
):
    app.include_router(domain_router)


@chat_router.post("/api/chat")
async def chat(request: ChatRequest):
    request, retry_source, retry_manifest = _retry_request(request)
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

    temporary_text, images = _resolve_chat_inputs(
        request,
        session_id=session_id,
        project_id=project_id,
    )

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
    retry_runtime = (
        str((retry_source.get("metrics") or {}).get("runtime") or "")
        if retry_source
        else ""
    )
    if retry_manifest and (
        retry_manifest.get("runtime_route") == "basic"
        or retry_runtime == "basic_chat"
    ):
        hermes_manager = None
    if retry_manifest and retry_runtime == "hermes" and hermes_manager is None:
        raise HTTPException(
            status_code=409,
            detail=error_payload(
                "HERMES_RETRY_UNAVAILABLE",
                "Hermes is unavailable for this whole-run retry.",
                recoverable=True,
            ),
        )

    hermes_skill_attachment = None
    project_skill_context = ""
    project_skill_provenance: List[Dict[str, Any]] = []
    project_skills_truncated = False
    try:
        if retry_manifest is not None:
            if retry_manifest.get("project_id") != project_id:
                raise ProjectSkillError(
                    "Retry Project Skill context belongs to another project."
                )
            project_skill_context = str(
                retry_manifest.get("project_skill_context") or ""
            )
            project_skill_provenance = [
                dict(item)
                for item in retry_manifest.get("project_skill_provenance") or []
                if isinstance(item, dict)
            ]
            project_skills_truncated = bool(
                retry_manifest.get("project_skills_truncated")
            )
            if project_id and project_skill_provenance:
                project_skill_runtime.record_provenance(
                    run_id,
                    session_id,
                    str(project_id),
                    project_skill_provenance,
                )
            if hermes_manager is not None:
                sources = tuple(
                    hermes_manager.project_skills._source(str(project_id), item)
                    for item in project_skill_provenance
                ) if project_id else ()
                hermes_skill_attachment = HermesProjectSkillsAttachment(
                    session_id=session_id,
                    project_id=project_id,
                    workbench_run_id=run_id,
                    instructions=project_skill_context,
                    sources=sources,
                    truncated=project_skills_truncated,
                )
        elif hermes_manager is not None:
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
            project_skill_provenance = hermes_skill_attachment.provenance
            project_skills_truncated = hermes_skill_attachment.truncated
        else:
            project_skill_prompt = project_skill_runtime.build_prompt_context(
                session_id,
                user_query,
                run_id=run_id,
                consume_turn=True,
            )
            project_skill_context = str(project_skill_prompt.get("context") or "")
            project_skill_provenance = [
                dict(item)
                for item in project_skill_prompt.get("skills") or []
                if isinstance(item, dict)
            ]
            project_skills_truncated = bool(project_skill_prompt.get("truncated"))
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

    current_session = database.get_session(session_id)
    if not current_session or current_session.get("project_id") != project_id:
        raise HTTPException(
            status_code=409,
            detail=error_payload(
                "SESSION_PROJECT_CHANGED",
                "The session project changed while this run was being prepared.",
                recoverable=True,
            ),
        )

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

    ensure_session_folder(session_id, model=model, project_id=project_id)
    history_snapshot = (
        normalize_history_snapshot(retry_manifest.get("history_snapshot") or [])
        if retry_manifest is not None
        else completed_conversation_history(
            database.get_messages_by_session(session_id),
            current_turn_id=turn_id,
        )
    )
    if retry_manifest is not None:
        user_message_id = int(retry_manifest["user_message_id"])
    else:
        user_message_id = database.add_message(
            session_id,
            "user",
            user_query,
            visible_content=user_query,
            llm_content=user_query,
            turn_id=turn_id,
        )
    run_input_manifest = _input_manifest(
        request,
        user_message_id=user_message_id,
        prompt_sha256=prompt_sha256,
        project_id=project_id,
        project_skill_context=project_skill_context,
        project_skill_provenance=project_skill_provenance,
        project_skills_truncated=project_skills_truncated,
        runtime_route="hermes" if hermes_manager is not None else "basic",
        user_query=user_query,
        history_snapshot=history_snapshot,
    )

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
            project_skill_sources=project_skill_provenance,
            retry_of_run_id=request.retry_of_run_id,
            input_manifest=run_input_manifest,
            history_snapshot=history_snapshot,
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
                    project_id=project_id,
                    retry_of_run_id=request.retry_of_run_id,
                    input_manifest=run_input_manifest,
                    history_snapshot=history_snapshot,
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


@chat_router.get("/api/sessions/{session_id}/runs")
def latest_session_runs(session_id: str, limit: int = 1):
    session = database.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=404,
            detail=error_payload(
                "SESSION_NOT_FOUND", "Session not found.", recoverable=False
            ),
        )
    return {
        "success": True,
        "session_id": session_id,
        "runs": database.list_session_runs(
            session_id,
            project_id=session.get("project_id"),
            limit=limit,
        ),
    }


@chat_router.get("/api/runs/{run_id}/execution")
def run_execution_snapshot(run_id: str):
    run = database.get_run(run_id)
    if not run:
        raise HTTPException(
            status_code=404,
            detail=error_payload(
                "RUN_NOT_FOUND", "Run not found.", recoverable=False
            ),
        )
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
    allowed, reason = _retry_eligibility(run)
    public_events = database.public_run_events(
        run.get("events"),
        run_id=run_id,
        session_id=str(run.get("session_id") or ""),
        project_id=run.get("project_id"),
    )
    events = _hydrate_execution_approvals(
        run_id,
        public_events,
        session_id=str(run.get("session_id") or ""),
        project_id=run.get("project_id"),
    )
    return {
        "success": True,
        "run_id": run_id,
        "session_id": run.get("session_id"),
        "project_id": run.get("project_id"),
        "status": run.get("status"),
        "revision": int(run.get("execution_revision") or 0),
        "tasks": _public_run_tasks(run.get("tasks")),
        "events": events,
        "error": _run_public_error(run),
        "retry": {"allowed": allowed, "reason": reason},
    }


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
