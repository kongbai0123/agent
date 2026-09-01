"""Local AI Workbench application entry point.

The production chat runtime remains a single conversational model loop.  The
only background integration composed here is the narrow, manually approved
n8n Gmail bridge; it is isolated from chat/Hermes tools and from general
automation or orchestration capabilities.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import database
from api.routes.attachments import build_attachments_router
from api.routes.chat import router as chat_router
from api.routes.connectors import (
    build_connector_callback_router,
    build_connectors_router,
)
from api.routes.extensions import build_extensions_router
from api.routes.external_agent_api import build_external_agent_api_router
from api.routes.hermes import build_hermes_router
from api.routes.integration_center import build_integration_center_router
from api.routes.knowledge import build_knowledge_router
from api.routes.models import build_models_router
from api.routes.model_governance import build_model_governance_router
from api.routes.mlops import build_mlops_router
from api.routes.operations import build_operations_router
from api.routes.n8n_agent import build_n8n_agent_router
from api.routes.n8n_agent_tasks import build_n8n_agent_tasks_router
from api.routes.n8n_gmail import build_n8n_gmail_router
from api.routes.n8n_runtime import build_n8n_runtime_router
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
from capability_status import (
    CAPABILITY_STATUS_EXTENSION_ID,
    CAPABILITY_STATUS_MANIFEST_SHA256,
    CapabilityStatusService,
    build_capability_status_tool_definitions,
)
from conversation_store import archive_session, ensure_session_folder, export_session
from connector_secrets import ConnectorSecretStore
from connector_service import ConnectorService, ConnectorServiceError
from connector_store import ConnectorStoreError, ConnectorStore
from core.settings import (
    apply_network_settings,
    load_settings,
    normalize_modal_size,
    save_settings,
    validate_settings as validate_chat_settings,
)
from email_draft_runtime import EmailDraftRuntime
from extension_registry import (
    ExtensionError,
    create_extension_registry,
)
from external_agent_api import (
    ExternalAgentApiError,
    ExternalAgentApiService,
)
from integration_center_service import IntegrationCenterService
from integration_center_store import IntegrationCenterStore
from integration_policy_applier import AuthoritativeIntegrationPolicyApplier
from local_session import (
    SESSION_COOKIE_NAME,
    install_local_session_guard,
    is_local_origin,
    session_token,
    write_token_file,
)
from hook_runtime import (
    DiagnosticBuiltinHookPlugin,
    GuardAction,
    HookContext,
    HookDispatcher,
    HookRuntimeError,
    HookSnapshot,
    HookSnapshotEntry,
    configure_hook_dispatcher,
)
from hook_audit_store import HookAuditStore
from host_tools import HostToolRuntime
from mcp_coordinator import MCPSettingsCoordinator
from model_client import (
    configure_provider_extension_gate,
    list_all_models as provider_model_inventory,
    model_profile_for_model,
    provider_for_model,
    require_provider_enabled,
    uses_local_model_slot,
)
from model_governance import (
    GovernanceError,
    ModelGovernanceService,
    configure_model_governance,
)
from mlops_service import MLOpsService
from operations_core import OperationsCore
from n8n_gmail_crypto import AesGcmContentCipher
from n8n_gmail_delivery import N8nDeliveryDispatcher
from n8n_gmail_secrets import N8nGmailSecretStore
from n8n_gmail_service import FIXED_TEST_RECIPIENT, GmailIntegrationError, N8nGmailService
from n8n_agent_governance import N8nAgentGovernanceService, N8nApiBroker
from n8n_agent_model_runtime import N8nAgentModelRuntime
from n8n_agent_planner import N8nPlanModelGenerator, N8nPlanningService
from n8n_agent_secrets import N8nAgentSecretStore
from n8n_agent_task_runtime import N8nAgentTaskRuntime
from n8n_graph_authoring import LazyGraphAuthoringEngine
from n8n_lifecycle import (
    AGENT_BRIDGE_TEMPLATE_ID,
    AGENT_BRIDGE_WORKFLOW_NAME,
    APPROVAL_GATE_TEMPLATE_ID,
    APPROVAL_GATE_WORKFLOW_NAME,
    ManagedN8nLifecycle,
    gmail_workflows_ready,
    inspect_agent_bridge_workflows_readiness,
    inspect_gmail_workflows_readiness,
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
from paths import PROJECTS_ROOT, REPO_ROOT, RUNTIME_ROOT, TEMP_DIR, ensure_runtime_dirs
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
from project_knowledge import ProjectKnowledgeError, ProjectKnowledgeService
from provider_tools import runtime_tool_definitions as provider_runtime_tool_definitions
from factual_verifier import (
    EvidenceBundle,
    EvidenceRecord,
    FactualVerificationError,
)
from secret_store import get_provider_secret
from semantic_retrieval import (
    GovernedProviderEmbeddingAdapter,
    GovernedProviderRerankerAdapter,
    GovernedSemanticProviderClient,
    LocalCrossEncoderRerankerAdapter,
    LocalSentenceTransformerEmbeddingAdapter,
    ModelGovernanceSemanticPolicy,
    OpenAIEmbeddingContract,
    PassagesRerankContract,
    SemanticProviderRoute,
    SemanticConsentRequired,
    SemanticRetrievalError,
    semantic_route_from_provider,
)
from runtime_manager import export_session_zip
from startup_progress import complete_startup, read_startup_status, update_startup
from structured_log import degraded, redact, redact_text
from tool_approval_broker import (
    ToolApprovalBroker,
    ToolApprovalBrokerError,
    ToolApprovalNotFound,
    operation_risk_class,
)
from tool_runtime import (
    PolicyAction,
    PolicyDecision,
    ToolAccess,
    ToolDispatcher,
    ToolRegistry,
    ToolScopeState,
    ToolUnavailableError,
)
from workspace import (
    context_for_project,
    context_payload,
    managed_project_path,
    normalize_path,
    path_status,
    validate_project_path,
    write_project_manifest,
)


APP_VERSION = "0.9.0-model-catalog-beta.11"
SETTINGS_PATH = str(
    Path(
        os.environ.get("WORKBENCH_SETTINGS_PATH")
        or Path(__file__).resolve().with_name("settings.json")
    ).resolve()
)
hermes_manager_cache: Optional[HermesIntegrationManagerCache] = None
hermes_health_supervisor: Optional[HermesHealthSupervisor] = None
hermes_rollout_gate: Optional[HermesRolloutGate] = None
n8n_lifecycle: Optional[ManagedN8nLifecycle] = None
n8n_gmail_service: Optional[N8nGmailService] = None
n8n_agent_task_runtime: Optional[N8nAgentTaskRuntime] = None
n8n_background_tasks: set[asyncio.Task[Any]] = set()
external_api_background_tasks: set[asyncio.Task[Any]] = set()
external_api_active_runs: Dict[str, set[str]] = {}
external_api_run_sessions: Dict[str, str] = {}
MAX_EXTERNAL_API_RUNS = 8
MAX_EXTERNAL_API_RUNS_PER_KEY = 2
extension_registry: Any = None
connector_service: Optional[ConnectorService] = None
external_agent_api_service: Optional[ExternalAgentApiService] = None
integration_center_service: Optional[IntegrationCenterService] = None
capability_status_service: Optional[CapabilityStatusService] = None
host_tool_runtime: Optional[HostToolRuntime] = None
mcp_coordinator: Optional[MCPSettingsCoordinator] = None
model_governance: Optional[ModelGovernanceService] = None
knowledge_service_available = False
application_event_loop: Optional[asyncio.AbstractEventLoop] = None
_KNOWLEDGE_RETRIEVAL_SLOTS = threading.BoundedSemaphore(4)
hook_dispatcher = configure_hook_dispatcher(
    HookDispatcher.from_builtin_plugins([DiagnosticBuiltinHookPlugin()])
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def sse(event: str, data: Dict[str, Any]) -> str:
    return encode_sse(event, data)


def extension_is_enabled(
    extension_id: str,
    project_id: Optional[str] = None,
) -> bool:
    """Fail closed when an extension lifecycle record is unavailable."""

    registry = extension_registry
    return bool(
        registry is not None
        and registry.is_effectively_enabled(extension_id, project_id)
    )


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


class _UnavailableProjectKnowledgeService:
    """Fail the optional knowledge capability without taking down the app.

    The knowledge router is dependency-injected with a service object at import
    time.  Keeping that contract lets startup degrade cleanly even when the
    separate knowledge database cannot be opened.  Reads, writes and project
    deletion cleanup all fail closed so metadata cannot become detached from
    an index that the host was unable to inspect.
    """

    available = False
    embedding_adapter_id = "unavailable"

    def __init__(self, reason: str = "") -> None:
        self.reason = str(reason or "knowledge_service_initialization_failed")[:200]

    @staticmethod
    def _raise_unavailable() -> None:
        raise ProjectKnowledgeError(
            "Project knowledge is temporarily unavailable.",
            code="KNOWLEDGE_SERVICE_UNAVAILABLE",
            status_code=503,
        )

    def configure_chunking(self, **_kwargs: Any) -> None:
        return None

    @contextmanager
    def project_delete_guard(self, **_kwargs: Any):
        self._raise_unavailable()
        yield

    def clear_project(self, **_kwargs: Any) -> Dict[str, int]:
        self._raise_unavailable()

    def status(self, **_kwargs: Any) -> Dict[str, Any]:
        self._raise_unavailable()

    def list_documents(self, **_kwargs: Any) -> List[Dict[str, Any]]:
        self._raise_unavailable()

    def import_document(self, **_kwargs: Any) -> Dict[str, Any]:
        self._raise_unavailable()

    def import_documents(self, **_kwargs: Any) -> List[Dict[str, Any]]:
        self._raise_unavailable()

    def document_chunks(self, **_kwargs: Any) -> List[Dict[str, Any]]:
        self._raise_unavailable()

    def document_chunk_count(self, **_kwargs: Any) -> int:
        self._raise_unavailable()

    def delete_document(self, **_kwargs: Any) -> bool:
        self._raise_unavailable()

    def retrieve(self, **_kwargs: Any) -> List[Dict[str, Any]]:
        self._raise_unavailable()


def _initialize_project_knowledge_service(
    path: Path,
    *,
    chunk_chars: int,
    overlap_chars: int,
    settings: Optional[Dict[str, Any]] = None,
) -> tuple[Any, bool]:
    try:
        embedding_adapter = None
        reranker = None
        if settings is not None:
            embedding_adapter, reranker = _project_knowledge_adapters(settings)
        return (
            ProjectKnowledgeService(
                path,
                chunk_chars=chunk_chars,
                overlap_chars=overlap_chars,
                embedding_adapter=embedding_adapter,
                reranker=reranker,
            ),
            True,
        )
    except Exception as exc:  # noqa: BLE001 - knowledge is an optional capability
        degraded("project_knowledge", "initialize project knowledge service", exc)
        return _UnavailableProjectKnowledgeService(str(exc)), False


def _semantic_provider_config(
    settings: Dict[str, Any], provider_id: str
) -> Dict[str, Any]:
    requested = str(provider_id or "").strip().casefold()
    if not requested:
        raise SemanticRetrievalError(
            "Semantic provider ID is required.",
            code="SEMANTIC_PROVIDER_CONFIG_INVALID",
            status_code=422,
        )
    for raw in settings.get("model_providers") or []:
        if (
            isinstance(raw, dict)
            and str(raw.get("id") or "").strip().casefold() == requested
        ):
            return dict(raw)
    raise SemanticRetrievalError(
        "Configured semantic provider was not found.",
        code="SEMANTIC_PROVIDER_CONFIG_INVALID",
        status_code=422,
    )


def _semantic_route(
    provider: Dict[str, Any], *, capability: str
) -> SemanticProviderRoute:
    configured = dict(provider)
    provider_type = str(configured.get("provider_type") or "").strip().casefold()
    model_id = str(configured.get("selected_model") or "").strip()
    if provider_type == "nvidia":
        parts = model_id.split("/", 1)
        short_model = parts[1] if len(parts) == 2 else parts[0]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", short_model):
            raise SemanticRetrievalError(
                "NVIDIA semantic model ID is invalid.",
                code="SEMANTIC_PROVIDER_CONFIG_INVALID",
                status_code=422,
            )
        prefix = f"/v1/retrieval/nvidia/{short_model}"
        configured.update(
            {
                "base_url": f"https://ai.api.nvidia.com{prefix}",
                "embedding_endpoint": f"{prefix}/embeddings",
                "rerank_endpoint": f"{prefix}/reranking",
                "document_input_type": "passage",
                "query_input_type": "query",
            }
        )
    return semantic_route_from_provider(configured, capability=capability)


def _semantic_provider_access_check(
    route: SemanticProviderRoute,
    *,
    capability: str,
) -> Any:
    """Return a per-call gate that rejects stale routes and Extension state."""

    def check(provider_id: str, project_id: str) -> None:
        current = load_settings()
        provider = _semantic_provider_config(current, provider_id)
        try:
            current_route = _semantic_route(provider, capability=capability)
        except SemanticRetrievalError as exc:
            raise PermissionError(
                "Semantic provider configuration changed; restart required."
            ) from exc
        if current_route != route:
            raise PermissionError(
                "Semantic provider configuration changed; restart required."
            )
        require_provider_enabled(current, provider_id, project_id=project_id)

    return check


def _governed_semantic_client(
    route: SemanticProviderRoute,
    *,
    capability: str,
) -> GovernedSemanticProviderClient:
    governance = globals().get("model_governance")
    if governance is None:
        raise SemanticRetrievalError(
            "Model governance is unavailable.",
            code="SEMANTIC_GOVERNANCE_UNAVAILABLE",
            status_code=503,
        )
    return GovernedSemanticProviderClient(
        route,
        governance=governance,
        access_policy=ModelGovernanceSemanticPolicy(governance),
        provider_access_check=_semantic_provider_access_check(
            route, capability=capability
        ),
        secret_resolver=get_provider_secret,
    )


def _semantic_consent_failure(exc: BaseException) -> Optional[SemanticConsentRequired]:
    """Find the trusted semantic-consent cause without parsing error text."""

    current: Optional[BaseException] = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, SemanticConsentRequired):
            return current
        current = current.__cause__ or current.__context__
    return None


def _semantic_consent_proposal(
    *,
    project_id: str,
    run_id: str,
    requested_model: str,
    failure: SemanticConsentRequired,
) -> Dict[str, Any]:
    """Create the same explicit data-disclosure contract used by chat models."""

    provider_id = str(failure.provider_id or "").strip().casefold()
    selected_model = str(failure.model_reference or "").strip()
    if not provider_id or not selected_model:
        raise GovernanceError(
            "MODEL_DATA_CONSENT_UNAVAILABLE",
            "語意模型未提供可驗證的同意範圍。",
            status_code=503,
        )
    proposal = model_governance.create_data_consent_proposal(
        project_id=project_id,
        run_id=run_id,
        requested_model=requested_model,
        selected_model=selected_model,
        provider_id=provider_id,
        data_types=("documents",),
    )
    return {
        **proposal,
        "data_type": ["documents"],
        "data_type_label": "專案文件內容",
        "risk": "專案文件片段將離開本機，傳送至所列雲端語意模型處理。",
        "consequences": [
            "供應商會依其服務條款處理文件片段，可能受其留存與稽核政策影響。",
            "文件若含機密、個資或未公開資訊，可能造成不適當的外部揭露。",
            "選擇「記住此專案」後，同一專案傳送至這個供應商的文件可依政策自動處理。",
        ],
        "actions": [
            {"id": "model_data_policy", "label": "檢視預算與選模政策"},
            {"id": "knowledge_settings", "label": "改用本機 Embedding"},
        ],
        "consent_target": "semantic_retrieval",
    }


def _knowledge_semantic_consent_proposal(
    *,
    project_id: str,
    run_id: str,
    requested_model: str,
    error: BaseException,
) -> Optional[Dict[str, Any]]:
    """Best-effort proposal bridge for the standalone Knowledge workspace."""

    failure = _semantic_consent_failure(error)
    if failure is None:
        return None
    try:
        return _semantic_consent_proposal(
            project_id=project_id,
            run_id=run_id,
            requested_model=requested_model,
            failure=failure,
        )
    except (AttributeError, GovernanceError, ValueError, sqlite3.Error):
        return None


def _project_knowledge_adapters(
    settings: Dict[str, Any],
) -> tuple[Any, Any]:
    """Resolve local-first semantic adapters without changing legacy defaults."""

    local_embedding = str(settings.get("rag_local_embedding_model_path") or "").strip()
    local_reranker = str(settings.get("rag_local_reranker_model_path") or "").strip()
    embedding_provider = str(settings.get("rag_embedding_provider_id") or "").strip()
    reranker_provider = str(settings.get("rag_reranker_provider_id") or "").strip()

    embedding_adapter: Any = None
    if local_embedding:
        embedding_adapter = LocalSentenceTransformerEmbeddingAdapter(local_embedding)
    elif embedding_provider:
        provider = _semantic_provider_config(settings, embedding_provider)
        route = _semantic_route(provider, capability="embedding")
        embedding_adapter = GovernedProviderEmbeddingAdapter(
            _governed_semantic_client(route, capability="embedding"),
            contract=OpenAIEmbeddingContract(),
        )

    reranker: Any = None
    if local_reranker:
        reranker = LocalCrossEncoderRerankerAdapter(local_reranker)
    elif reranker_provider:
        provider = _semantic_provider_config(settings, reranker_provider)
        route = _semantic_route(provider, capability="rerank")
        reranker = GovernedProviderRerankerAdapter(
            _governed_semantic_client(route, capability="rerank"),
            contract=(
                PassagesRerankContract()
                if str(provider.get("provider_type") or "").casefold() == "nvidia"
                else None
            ),
        )
    return embedding_adapter, reranker


def knowledge_error_payload(
    code: str,
    message: str,
    detail: Optional[str] = None,
    recoverable: bool = True,
    suggestions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Keep optional knowledge startup failures explicit and recoverable."""

    if str(code) == "KNOWLEDGE_SERVICE_UNAVAILABLE":
        message = "知識庫服務暫時無法使用；其他工作區仍可正常操作，請稍後重新啟動再試。"
        recoverable = True
        suggestions = suggestions or ["重新啟動 Workbench", "檢查本機知識庫儲存路徑"]
    return error_payload(code, message, detail, recoverable, suggestions)


def clear_project_knowledge_for_delete(project_id: str) -> Dict[str, int]:
    """Clear project knowledge before metadata deletion, or fail closed."""

    service = globals().get("knowledge_service")
    if service is None:
        raise ProjectKnowledgeError(
            "Project knowledge cleanup is unavailable.",
            code="KNOWLEDGE_SERVICE_UNAVAILABLE",
            status_code=503,
        )
    try:
        return service.clear_project(project_id=project_id)
    except Exception as exc:  # noqa: BLE001 - optional index must not trap a project
        degraded(
            "project_knowledge",
            "clear project knowledge during project deletion",
            exc,
            project_id=project_id,
        )
        raise


def project_knowledge_delete_guard(project_id: str):
    """Hold the Project knowledge lifecycle boundary through metadata deletion."""

    service = globals().get("knowledge_service")
    if service is None or not hasattr(service, "project_delete_guard"):
        raise ProjectKnowledgeError(
            "Project knowledge deletion guard is unavailable.",
            code="KNOWLEDGE_SERVICE_UNAVAILABLE",
            status_code=503,
        )
    return service.project_delete_guard(project_id=project_id)


def _retrieve_project_knowledge(**kwargs: Any) -> List[Dict[str, Any]]:
    """Run one bounded synchronous retrieval outside the ASGI event loop."""

    if not _KNOWLEDGE_RETRIEVAL_SLOTS.acquire(timeout=5.0):
        raise ProjectKnowledgeError(
            "Project knowledge retrieval is busy.",
            code="KNOWLEDGE_RETRIEVAL_BUSY",
            status_code=503,
        )
    try:
        return knowledge_service.retrieve(**kwargs)
    finally:
        _KNOWLEDGE_RETRIEVAL_SLOTS.release()


async def _retrieve_project_knowledge_async(**kwargs: Any) -> List[Dict[str, Any]]:
    """Keep synchronous SQLite/vector work away from request orchestration."""

    return await asyncio.to_thread(_retrieve_project_knowledge, **kwargs)


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


def guard_integration_session(
    session_id: str,
    action: str,
    _changes: Optional[Dict[str, Any]] = None,
) -> None:
    """Keep private email sessions outside all general chat surfaces."""

    session = database.get_session(session_id)
    if session and str(session.get("mode") or "").casefold() == "email":
        raise HTTPException(
            status_code=404,
            detail=error_payload(
                "SESSION_NOT_FOUND",
                "Session was not found.",
                recoverable=False,
            ),
        )


def guard_n8n_project_change(
    project_id: str,
    _action: str,
    _changes: Optional[Dict[str, Any]] = None,
) -> None:
    service = n8n_gmail_service
    if service is None:
        return
    try:
        service.assert_project_mutable(project_id)
    except GmailIntegrationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=error_payload(
                exc.code,
                exc.message,
                recoverable=exc.recoverable,
            ),
        ) from exc


def n8n_profile_enable_guard(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Require the reviewed runtime, isolation and Gmail workflows."""

    lifecycle = n8n_lifecycle
    if lifecycle is None:
        return {
            "ready": False,
            "code": "n8n_runtime_unavailable",
            "message": "The managed n8n runtime is unavailable.",
            "status_code": 503,
        }
    project = database.get_project(str(profile.get("project_id") or ""))
    if (
        not project
        or bool(project.get("archived"))
        or str(project.get("path_status") or "") != "ready"
    ):
        return {
            "ready": False,
            "code": "gmail_project_not_ready",
            "message": "The fixed Gmail project is not ready.",
            "status_code": 409,
        }
    status = lifecycle.status(probe_node=True)
    installation = status.get("installation") or {}
    if installation.get("valid") is not True:
        return {
            "ready": False,
            "code": "n8n_installation_invalid",
            "message": "The pinned n8n installation is not ready.",
            "status_code": 409,
        }
    if status.get("isolation_ready") is not True:
        return {
            "ready": False,
            "code": "n8n_isolation_not_ready",
            "message": "The low-privilege n8n account and ACLs are not ready.",
            "status_code": 409,
        }
    if status.get("state") in {"port_conflict", "upgrade_required", "failed"}:
        return {
            "ready": False,
            "code": "n8n_runtime_not_ready",
            "message": "The managed n8n runtime is not in a safe state.",
            "status_code": 409,
        }
    if not gmail_workflows_ready(lifecycle.paths):
        return {
            "ready": False,
            "code": "gmail_workflows_not_ready",
            "message": "Configure, publish and activate both reviewed Gmail workflows first.",
            "status_code": 409,
        }
    return {"ready": True}


def validate_settings(data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        validated = validate_chat_settings(data)
        # Stage the complete adapter graph before settings are persisted.  This
        # prevents the UI from reporting a new semantic backend while the live
        # knowledge service silently retains an older adapter after a config
        # construction failure.
        _project_knowledge_adapters(validated)
        return validated
    except (SemanticRetrievalError, TypeError, ValueError) as exc:
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
    service = globals().get("knowledge_service")
    if service is not None:
        raw_chunk_chars = settings.get("chunk_size")
        raw_overlap_chars = settings.get("chunk_overlap")
        chunk_chars = max(
            128,
            min(3000, int(600 if raw_chunk_chars is None or raw_chunk_chars == "" else raw_chunk_chars)),
        )
        overlap_chars = max(
            0,
            min(
                min(1000, chunk_chars // 2 - 1),
                int(120 if raw_overlap_chars is None or raw_overlap_chars == "" else raw_overlap_chars),
            ),
        )
        service.configure_chunking(
            chunk_chars=chunk_chars,
            overlap_chars=overlap_chars,
        )
        configure_adapters = getattr(service, "configure_adapters", None)
        if callable(configure_adapters):
            try:
                embedding_adapter, reranker = _project_knowledge_adapters(settings)
                configure_adapters(
                    embedding_adapter=embedding_adapter,
                    reranker=reranker,
                )
            except Exception as exc:  # noqa: BLE001 - retain the last safe adapters
                degraded(
                    "project_knowledge",
                    "apply semantic adapter configuration",
                    exc,
                )
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


_MODEL_INVENTORY_CACHE_SECONDS = 3.0
_model_inventory_cache_lock = threading.Lock()
_model_inventory_cache: Dict[str, Any] = {
    "key": "",
    "expires_at": 0.0,
    "items": [],
}


def _model_inventory_cache_key(settings: Dict[str, Any]) -> str:
    relevant = {
        "ollama_url": settings.get("ollama_url"),
        "model_provider": settings.get("model_provider"),
        "model_providers": settings.get("model_providers") or [],
    }
    encoded = json.dumps(relevant, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def model_inventory() -> List[Dict[str, Any]]:
    settings = load_settings()
    cache_key = _model_inventory_cache_key(settings)
    now = time.monotonic()
    with _model_inventory_cache_lock:
        if (
            _model_inventory_cache["key"] == cache_key
            and now < float(_model_inventory_cache["expires_at"])
        ):
            return [dict(item) for item in _model_inventory_cache["items"]]
        try:
            items = [
                item
                for item in provider_model_inventory(settings, timeout=5)
                if isinstance(item, dict) and item.get("name")
            ]
        except Exception:
            items = []
        _model_inventory_cache.update({
            "key": cache_key,
            "expires_at": time.monotonic() + _MODEL_INVENTORY_CACHE_SECONDS,
            "items": [dict(item) for item in items],
        })
        return [dict(item) for item in items]


def ollama_models() -> List[str]:
    return [str(item["name"]) for item in model_inventory()]


def disabled_service_status() -> Dict[str, Any]:
    service = globals().get("knowledge_service")
    if service is not None and bool(globals().get("knowledge_service_available")):
        return {
            "enabled": True,
            "index_status": "project_scoped",
            "document_count": None,
            "chunk_count": None,
            "embedding_model": service.embedding_adapter_id,
        }
    return {
        "enabled": False,
        "index_status": "unavailable" if service is not None else "disabled",
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


_KNOWLEDGE_TEXT_LIMIT = 128 * 1024
_KNOWLEDGE_LABEL_LIMIT = 512
_KNOWLEDGE_SECRET_PATTERNS = (
    re.compile(r"\bnvapi-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bwbk_[a-f0-9]{12}_[A-Za-z0-9_-]{43}\b", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", re.IGNORECASE),
    re.compile(r"\bxox[a-z]-[A-Za-z0-9-]{10,}\b", re.IGNORECASE),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+\-/=]{8,}\b", re.IGNORECASE),
    re.compile(r"https?://[^\s/:@]+:[^\s/@]+@", re.IGNORECASE),
    re.compile(
        r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?"
        r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
        re.IGNORECASE | re.DOTALL,
    ),
)
_KNOWLEDGE_SECRET_ASSIGNMENT = re.compile(
    r"\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|password|"
    r"secret|authorization|private[_-]?key)\b(\s*[:=]\s*)"
    r"(?:['\"]?)[^\s,;'\"]{4,}(?:['\"]?)",
    re.IGNORECASE,
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _redact_knowledge_text(value: Any, *, limit: int = _KNOWLEDGE_TEXT_LIMIT) -> str:
    """Redact common and registered credentials before durable RAG snapshots."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        character
        for character in text
        if character in {"\n", "\t"} or ord(character) >= 32
    )
    for pattern in _KNOWLEDGE_SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    text = _KNOWLEDGE_SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[redacted]",
        text,
    )
    # Run registered-literal redaction across the complete value before the
    # bounded result is returned. Splitting first could expose a secret that
    # straddles a chunk boundary.
    return redact_text(text, max_length=max(0, int(limit)))


def _is_loopback_model_endpoint(value: Any) -> bool:
    """Return true only for an explicit HTTP(S) loopback endpoint."""

    try:
        parsed = urlsplit(str(value or "").strip())
        hostname = str(parsed.hostname or "").strip().casefold()
        if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        if hostname == "localhost":
            return True
        return ipaddress.ip_address(hostname).is_loopback
    except (ValueError, TypeError):
        return False


def _safe_knowledge_label(value: Any, *, fallback: str = "") -> str:
    return _redact_knowledge_text(value, limit=_KNOWLEDGE_LABEL_LIMIT).strip() or fallback


def _safe_knowledge_citation(
    value: Any,
    *,
    project_id: str,
    document_id: str,
    chunk_id: str,
) -> Dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    citation: Dict[str, Any] = {
        "project_id": project_id,
        "document_id": document_id,
        "chunk_id": chunk_id,
    }
    for key in ("source_id", "title"):
        if raw.get(key):
            citation[key] = _safe_knowledge_label(raw.get(key))
    for key in ("ordinal", "start_offset", "end_offset"):
        if raw.get(key) is None:
            continue
        try:
            citation[key] = max(0, int(raw.get(key)))
        except (TypeError, ValueError):
            continue
    for key in ("document_sha256", "chunk_sha256"):
        digest = str(raw.get(key) or "").strip().casefold()
        if _SHA256_RE.fullmatch(digest):
            citation[key] = digest
    return citation


def _safe_knowledge_source(
    value: Any,
    *,
    project_id: str,
) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    if str(value.get("project_id") or "") != project_id:
        return None
    document_id = _safe_knowledge_label(value.get("document_id"))[:128]
    chunk_id = _safe_knowledge_label(value.get("chunk_id"))[:128]
    if not document_id or not chunk_id:
        return None
    citation = _safe_knowledge_citation(
        value.get("citation"),
        project_id=project_id,
        document_id=document_id,
        chunk_id=chunk_id,
    )
    title = _safe_knowledge_label(
        value.get("source") or citation.get("title") or citation.get("source_id"),
        fallback="知識庫文件",
    )
    raw_content = value.get("content")
    if raw_content is not None:
        snippet_digest = hashlib.sha256(
            _redact_knowledge_text(raw_content).encode("utf-8")
        ).hexdigest()
    else:
        candidate_digest = str(value.get("snippet_sha256") or "").casefold()
        snippet_digest = (
            candidate_digest
            if _SHA256_RE.fullmatch(candidate_digest)
            else hashlib.sha256(b"").hexdigest()
        )
    score: Optional[float]
    try:
        score = float(value.get("score")) if value.get("score") is not None else None
        if score is not None and not (-1.0e308 <= score <= 1.0e308):
            score = None
    except (TypeError, ValueError):
        score = None
    return {
        "kind": "project_knowledge",
        "project_id": project_id,
        "source": title,
        "score": score,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "citation": citation,
        "snippet_sha256": snippet_digest,
    }


def _canonical_private_knowledge_sources(
    value: Any,
    *,
    project_id: Optional[str],
) -> List[Dict[str, Any]]:
    expected_project = str(project_id or "").strip()
    if not expected_project or not isinstance(value, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in value[:20]:
        source = _safe_knowledge_source(item, project_id=expected_project)
        if source is not None:
            result.append(source)
    return result


def _private_knowledge_evidence_snapshot(
    bundle: Optional[EvidenceBundle],
    *,
    project_id: Optional[str],
) -> List[Dict[str, Any]]:
    """Persist only the already-redacted evidence needed for a safe retry."""

    expected_project = str(project_id or "").strip()
    if bundle is None:
        return []
    if not expected_project or bundle.project_id != expected_project:
        raise FactualVerificationError(
            "Knowledge evidence belongs to another project.",
            code="VERIFICATION_EVIDENCE_SCOPE_MISMATCH",
        )
    result: List[Dict[str, Any]] = []
    for record in bundle.records[:20]:
        citation = dict(record.citation)
        chunk_id = str(citation.get("chunk_id") or "").strip()
        document_id = str(citation.get("document_id") or "").strip()
        if (
            record.evidence_id != f"knowledge:{chunk_id}"
            or not chunk_id
            or not document_id
        ):
            raise FactualVerificationError(
                "Knowledge evidence identity is invalid.",
                code="INVALID_VERIFICATION_INPUT",
            )
        text = _redact_knowledge_text(record.text).strip()
        if not text:
            continue
        result.append(
            {
                "evidence_id": record.evidence_id,
                "text": text,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "citation": citation,
            }
        )
    return result


def _knowledge_evidence_from_snapshot(
    value: Any,
    *,
    project_id: Optional[str],
) -> Optional[EvidenceBundle]:
    """Rebuild typed evidence without parsing untrusted prompt prose."""

    expected_project = str(project_id or "").strip()
    if not isinstance(value, list):
        return None
    if not expected_project:
        if value:
            raise FactualVerificationError(
                "Knowledge evidence requires a project.",
                code="VERIFICATION_EVIDENCE_SCOPE_MISMATCH",
            )
        return None
    records: List[EvidenceRecord] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            raise FactualVerificationError(
                "Knowledge evidence snapshot is invalid.",
                code="INVALID_VERIFICATION_INPUT",
            )
        text = str(item.get("text") or "")
        digest = str(item.get("text_sha256") or "").casefold()
        citation = item.get("citation")
        if (
            not text
            or _redact_knowledge_text(text) != text
            or not _SHA256_RE.fullmatch(digest)
            or not secrets.compare_digest(
                digest, hashlib.sha256(text.encode("utf-8")).hexdigest()
            )
            or not isinstance(citation, dict)
            or str(citation.get("project_id") or "") != expected_project
        ):
            raise FactualVerificationError(
                "Knowledge evidence snapshot is invalid.",
                code="INVALID_VERIFICATION_INPUT",
            )
        record = EvidenceRecord(
            evidence_id=str(item.get("evidence_id") or ""),
            text=text,
            kind="project_knowledge",
            project_id=expected_project,
            citation=citation,
        )
        chunk_id = str(record.citation.get("chunk_id") or "")
        if record.evidence_id != f"knowledge:{chunk_id}":
            raise FactualVerificationError(
                "Knowledge evidence identity is invalid.",
                code="INVALID_VERIFICATION_INPUT",
            )
        records.append(record)
    return EvidenceBundle(tuple(records), project_id=expected_project)


def _knowledge_snapshot_sha256(
    context: str,
    sources: List[Dict[str, Any]],
    *,
    project_id: Optional[str],
    evidence: Optional[List[Dict[str, Any]]] = None,
) -> str:
    snapshot: Dict[str, Any] = {
        "project_id": project_id,
        "knowledge_context": context,
        "knowledge_sources": sources,
    }
    # Keep old v2 manifests retry-compatible while binding all newly created
    # manifests to their structured, masked evidence records.
    if evidence is not None:
        snapshot["knowledge_evidence"] = evidence
    payload = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _knowledge_snapshot_is_valid(manifest: Dict[str, Any]) -> bool:
    context = manifest.get("knowledge_context")
    raw_sources = manifest.get("knowledge_sources")
    if not isinstance(context, str) or not isinstance(raw_sources, list):
        return False
    if len(context) > _KNOWLEDGE_TEXT_LIMIT or len(raw_sources) > 20:
        return False
    if (context or raw_sources) and not str(manifest.get("project_id") or "").strip():
        return False
    if _redact_knowledge_text(context) != context:
        return False
    canonical_sources = _canonical_private_knowledge_sources(
        raw_sources,
        project_id=manifest.get("project_id"),
    )
    if canonical_sources != raw_sources:
        return False
    raw_evidence = manifest.get("knowledge_evidence")
    canonical_evidence: Optional[List[Dict[str, Any]]] = None
    if raw_evidence is not None:
        try:
            bundle = _knowledge_evidence_from_snapshot(
                raw_evidence,
                project_id=manifest.get("project_id"),
            )
            canonical_evidence = _private_knowledge_evidence_snapshot(
                bundle,
                project_id=manifest.get("project_id"),
            )
        except FactualVerificationError:
            return False
        if canonical_evidence != raw_evidence:
            return False
    stored_digest = str(manifest.get("knowledge_snapshot_sha256") or "").casefold()
    if not _SHA256_RE.fullmatch(stored_digest):
        return False
    expected_digest = _knowledge_snapshot_sha256(
        context,
        canonical_sources,
        project_id=manifest.get("project_id"),
        evidence=canonical_evidence,
    )
    return secrets.compare_digest(stored_digest, expected_digest)


def _hook_snapshot_payload() -> List[Dict[str, Any]]:
    return [
        dict(entry.__dict__)
        for entry in hook_dispatcher.snapshot().entries
    ]


def _stored_hook_snapshot_is_compatible(manifest: Dict[str, Any]) -> bool:
    raw_entries = manifest.get("hook_snapshot")
    # Runs created before the Hook MVP remain retry-compatible. Their input is
    # transformed once during the new attempt and receives a current snapshot.
    if raw_entries is None:
        return True
    if not isinstance(raw_entries, list):
        return False
    try:
        snapshot = HookSnapshot(
            tuple(
                HookSnapshotEntry(**dict(item))
                for item in raw_entries
                if isinstance(item, dict)
            )
        )
        if len(snapshot.entries) != len(raw_entries):
            return False
        hook_dispatcher.verify_snapshot(snapshot)
        return True
    except (HookRuntimeError, TypeError, ValueError):
        return False


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
    try:
        manifest_version = int(manifest.get("version") or 0)
    except (AttributeError, TypeError, ValueError):
        manifest_version = 0
    if (
        not isinstance(manifest, dict)
        or manifest_version not in {1, 2}
        or manifest.get("reproducible") is not True
    ):
        reason = manifest.get("reason") if isinstance(manifest, dict) else None
        return False, str(reason or "input_manifest_unavailable")
    if manifest_version == 2 and not _knowledge_snapshot_is_valid(manifest):
        return False, "knowledge_snapshot_invalid"
    if manifest.get("project_id") != project_id:
        return False, "project_scope_changed"
    if not _stored_hook_snapshot_is_compatible(manifest):
        return False, "required_hook_snapshot_incompatible"
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
    if str(source.get("mode") or "").strip().casefold() == "email":
        # Integration retries use the encrypted mail state machine.  Keep a
        # known private Run ID indistinguishable from an unknown chat Run.
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
        routing_proposal_id=request.routing_proposal_id,
        budget_override_id=request.budget_override_id,
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
    knowledge_context: str = "",
    knowledge_sources: Optional[List[Dict[str, Any]]] = None,
    knowledge_evidence_bundle: Optional[EvidenceBundle] = None,
    hook_snapshot: Optional[List[Dict[str, Any]]] = None,
    hook_transform_steps: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    inline_image_count = len(request.images)
    safe_knowledge_context = _redact_knowledge_text(knowledge_context)
    safe_knowledge_sources = _canonical_private_knowledge_sources(
        list(knowledge_sources or []),
        project_id=project_id,
    )
    safe_knowledge_evidence = _private_knowledge_evidence_snapshot(
        knowledge_evidence_bundle,
        project_id=project_id,
    )
    return {
        "version": 2,
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
        # Whole-run retry reuses the exact bounded retrieval snapshot.  It must
        # not silently retrieve newer chunks under an old run identity.  The
        # private snapshot keeps masked prompt context; public sources contain
        # citation metadata and a snippet digest, never the raw snippet.
        "knowledge_context": safe_knowledge_context,
        "knowledge_sources": safe_knowledge_sources,
        "knowledge_evidence": safe_knowledge_evidence,
        "knowledge_snapshot_sha256": _knowledge_snapshot_sha256(
            safe_knowledge_context,
            safe_knowledge_sources,
            project_id=project_id,
            evidence=safe_knowledge_evidence,
        ),
        "knowledge_used": bool(
            request.use_rag or safe_knowledge_context or safe_knowledge_sources
        ),
        "runtime_route": runtime_route,
        "hook_snapshot": list(hook_snapshot or []),
        "hook_transform_steps": list(hook_transform_steps or []),
    }


def _truncate_utf8_text(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    if maximum_bytes <= 3:
        return ""
    prefix = encoded[: maximum_bytes - 3].decode("utf-8", errors="ignore").rstrip()
    return f"{prefix}…" if prefix else ""


def _knowledge_prompt_context(
    hits: List[Dict[str, Any]],
    *,
    project_id: str,
    include_evidence: bool = False,
) -> Any:
    """Build one 16 KiB prompt/evidence snapshot from the same masked text."""

    sections: List[str] = []
    sources: List[Dict[str, Any]] = []
    evidence_records: List[EvidenceRecord] = []
    used_bytes = 0
    prompt_limit = 16 * 1024
    for hit in hits[:20]:
        raw_citation = dict(hit.get("citation") or {})
        if str(raw_citation.get("project_id") or "") != str(project_id):
            continue
        content = _redact_knowledge_text(hit.get("text")).strip()
        preliminary_source = _safe_knowledge_source(
            {
                "kind": "project_knowledge",
                "project_id": str(project_id),
                "source": raw_citation.get("title") or raw_citation.get("source_id"),
                "score": hit.get("score"),
                "document_id": str(raw_citation.get("document_id") or ""),
                "chunk_id": str(raw_citation.get("chunk_id") or ""),
                "citation": raw_citation,
            },
            project_id=str(project_id),
        )
        if not content or preliminary_source is None:
            continue
        chunk_id = str(preliminary_source["chunk_id"])
        title = str(preliminary_source["source"])
        index = len(sources) + 1
        marker = f"[evidence:knowledge:{chunk_id}]"
        header = f"{marker}\n[知識來源 {index}：{title}]\n"
        separator_bytes = 2 if sections else 0
        remaining = prompt_limit - used_bytes - separator_bytes - len(
            header.encode("utf-8")
        )
        bounded_content = _truncate_utf8_text(content, remaining)
        if not bounded_content:
            break
        section = f"{header}{bounded_content}"
        source = _safe_knowledge_source(
            {
                **preliminary_source,
                "content": bounded_content,
            },
            project_id=str(project_id),
        )
        if source is not None:
            sections.append(section)
            used_bytes += separator_bytes + len(section.encode("utf-8"))
            sources.append(source)
            evidence_records.append(
                EvidenceRecord(
                    evidence_id=f"knowledge:{chunk_id}",
                    text=bounded_content,
                    kind="project_knowledge",
                    project_id=str(project_id),
                    citation=dict(source["citation"]),
                )
            )
        if used_bytes >= prompt_limit:
            break
    context = "\n\n".join(sections)
    evidence = EvidenceBundle(tuple(evidence_records), project_id=str(project_id))
    return (context, sources, evidence) if include_evidence else (context, sources)


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


def _schedule_n8n_recovery(callable_: Any, identifier: str) -> None:
    """Run one durable recovery action without blocking the event loop."""

    async def run() -> None:
        try:
            await asyncio.to_thread(callable_, identifier)
        except Exception as exc:  # private state already records the safe error
            print(f"[N8N] Recovery action failed: {type(exc).__name__}")

    task = asyncio.create_task(run())
    n8n_background_tasks.add(task)
    task.add_done_callback(n8n_background_tasks.discard)


def _schedule_n8n_runtime_start(lifecycle: Any) -> None:
    """Start optional n8n after ASGI readiness instead of blocking the UI."""

    async def run() -> None:
        try:
            await asyncio.to_thread(lifecycle.start)
        except Exception as exc:  # fail closed; core Workbench stays available
            print(f"[N8N] Managed runtime remained disabled: {type(exc).__name__}")
        finally:
            # Connector, MCP and inbound-API migration must not depend on the
            # optional managed n8n process starting successfully.
            await asyncio.to_thread(_migrate_existing_integration_policies)

    task = asyncio.create_task(run(), name="managed-n8n-startup")
    n8n_background_tasks.add(task)
    task.add_done_callback(n8n_background_tasks.discard)


def _schedule_n8n_agent_task_recovery(runtime: N8nAgentTaskRuntime) -> None:
    """Resume durable, already-authenticated Agent tasks off the startup path."""

    async def run() -> None:
        try:
            while await asyncio.to_thread(runtime.process_next_task) is not None:
                pass
        except Exception as exc:  # task state contains the bounded safe error
            print(f"[N8N] Agent task recovery stopped: {type(exc).__name__}")

    task = asyncio.create_task(run(), name="n8n-agent-task-recovery")
    n8n_background_tasks.add(task)
    task.add_done_callback(n8n_background_tasks.discard)


def _revoke_n8n_runtime_grants(reason: str = "n8n_stopped") -> None:
    """Fail closed when n8n stops without exposing private approval state."""

    runtime = n8n_agent_task_runtime
    if runtime is None:
        return
    try:
        with database.get_db_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT project_id FROM n8n_agent_runtime_grants "
                "WHERE status='active'"
            ).fetchall()
        for row in rows:
            runtime.notify_policy_changed(str(row["project_id"]), reason=reason)
    except Exception as exc:
        # Grant checks remain fail closed on boot/policy epoch.  This hook is
        # best effort so an integration problem cannot take down core chat.
        print(f"[N8N] Runtime grant revocation incomplete: {type(exc).__name__}")


def _on_managed_n8n_stop() -> None:
    n8n_agent_governance.downgrade_smart_policies("n8n_stopped")
    _revoke_n8n_runtime_grants("n8n_stopped")


def _reconcile_n8n_extension_runtime(lifecycle: Any) -> None:
    """Stop an owned runtime when the current extension grant is closed.

    Catalog synchronization can invalidate an older manifest approval before
    the lifecycle object exists, so the normal extension state-change callback
    cannot perform its cleanup during that transition.  Re-check the persisted
    grant at application startup and rely on ``status``/``stop`` to retain the
    lifecycle's strict process-ownership checks.
    """

    if extension_is_enabled("builtin.n8n"):
        return
    state = lifecycle.status(probe_node=False)
    if str(state.get("state") or "") not in {"ready", "starting", "degraded"}:
        return
    try:
        _on_managed_n8n_stop()
    finally:
        lifecycle.stop()


def _extension_project_ids() -> tuple[str, ...]:
    """Return stable project identities used for project-scoped runtimes."""

    try:
        return tuple(
            sorted(
                str(project["id"])
                for project in database.get_projects()
                if project.get("id") and not bool(project.get("archived"))
            )
        )
    except Exception:
        return ()


def _resolve_mcp_secret(alias: str) -> str:
    """Resolve a non-secret alias from an explicitly named process variable."""

    normalized = re.sub(r"[^A-Z0-9]+", "_", str(alias or "").upper()).strip("_")
    value = os.environ.get(f"WORKBENCH_MCP_SECRET_{normalized}") if normalized else None
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("The configured MCP secret alias is unavailable.")
    return value


def _schedule_mcp_sync() -> None:
    """Reconcile MCP safely from sync settings/extension route threads."""

    coordinator = mcp_coordinator
    loop = application_event_loop
    if coordinator is None or loop is None or not loop.is_running():
        return

    async def reconcile() -> None:
        try:
            # A global MCP enable toggles its settings-backed enable bit inside
            # the registry callback. Refresh the persisted catalog before the
            # coordinator reads effective state, even if the event loop wins
            # the race with set_global() returning to its caller.
            await asyncio.to_thread(extension_registry.sync)
            await coordinator.sync_from_settings(
                load_settings(),
                project_ids=_extension_project_ids(),
            )
        except Exception as exc:
            # Coordinator health contains the bounded failure.  Core chat must
            # remain available even when one local child cannot start.
            print(f"[MCP] Reconciliation failed: {type(exc).__name__}")

    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if current is loop:
        task = loop.create_task(reconcile(), name="mcp-settings-reconcile")
        n8n_background_tasks.add(task)
        task.add_done_callback(n8n_background_tasks.discard)
    else:
        asyncio.run_coroutine_threadsafe(reconcile(), loop)


@asynccontextmanager
async def _app_runtime_lifespan(_app: FastAPI):
    await hook_dispatcher.observe(
        "app.starting",
        HookContext(
            event="app.starting",
            metadata={"app_version": APP_VERSION},
        ),
    )
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
        except Exception as exc:  # pragma: no cover - startup best effort
            print(f"[HERMES] Health supervisor failed: {type(exc).__name__}")
    lifecycle = n8n_lifecycle
    mail_service = n8n_gmail_service
    if lifecycle is not None:
        try:
            await asyncio.to_thread(_reconcile_n8n_extension_runtime, lifecycle)
        except Exception as exc:
            # Authorization remains disabled even if ownership-safe cleanup
            # cannot complete; never broaden the stop target as a fallback.
            print(f"[N8N] Disabled runtime reconciliation incomplete: {type(exc).__name__}")
    profile = database.get_n8n_gmail_profile()
    n8n_auto_start = bool(
        lifecycle is not None
        and extension_is_enabled("builtin.n8n")
        and profile
        and bool(profile.get("enabled"))
        and bool(profile.get("auto_start"))
    )
    if n8n_auto_start:
        _schedule_n8n_runtime_start(lifecycle)
    if (
        mail_service is not None
        and extension_is_enabled("builtin.n8n")
        and profile
        and bool(profile.get("enabled"))
    ):
        try:
            database.expire_n8n_gmail_deliveries(now=now_iso())
            for draft_id in mail_service.recover_generation_jobs():
                _schedule_n8n_recovery(mail_service.generate_draft, draft_id)
            for delivery_id in mail_service.recover_delivery_jobs():
                _schedule_n8n_recovery(mail_service.dispatch_delivery, delivery_id)
            mail_service.purge_retention()
        except Exception as exc:  # content remains encrypted and durable
            print(f"[N8N] Recovery scan failed: {type(exc).__name__}")
    task_runtime = n8n_agent_task_runtime
    if task_runtime is not None and extension_is_enabled("builtin.n8n"):
        _schedule_n8n_agent_task_recovery(task_runtime)
    coordinator = mcp_coordinator
    if coordinator is not None:
        try:
            await coordinator.sync_from_settings(
                load_settings(),
                project_ids=_extension_project_ids(),
            )
        except Exception as exc:
            print(f"[MCP] Startup reconciliation failed: {type(exc).__name__}")
    if not n8n_auto_start:
        await asyncio.to_thread(_migrate_existing_integration_policies)
    try:
        await hook_dispatcher.observe(
            "app.ready",
            HookContext(
                event="app.ready",
                metadata={"app_version": APP_VERSION},
            ),
        )
        yield
    finally:
        await hook_dispatcher.observe(
            "app.stopping",
            HookContext(
                event="app.stopping",
                metadata={"app_version": APP_VERSION},
            ),
        )
        background_tasks = tuple(
            set(n8n_background_tasks) | set(external_api_background_tasks)
        )
        if background_tasks:
            _, pending = await asyncio.wait(
                background_tasks, timeout=30.0
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if coordinator is not None:
            try:
                await coordinator.stop_all()
            except Exception as exc:
                print(f"[MCP] Shutdown incomplete: {type(exc).__name__}")
        if lifecycle is not None:
            try:
                state = await asyncio.to_thread(lifecycle.status)
                if state.get("state") in {"ready", "starting", "degraded"}:
                    _on_managed_n8n_stop()
                    await asyncio.to_thread(lifecycle.stop)
            except Exception as exc:  # ownership checks remain authoritative
                print(f"[N8N] Managed shutdown incomplete: {type(exc).__name__}")
        if supervisor is not None:
            await supervisor.stop()
        if cache is not None:
            cache.close()


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    """Install process-wide runtime gates for exactly one app lifespan.

    The runtime context can fail before yielding or while shutting down. Keep
    the provider gate restoration in this outermost finally so neither path
    can leak extension authority into a later app/test lifespan.
    """

    global application_event_loop
    application_event_loop = asyncio.get_running_loop()
    previous_provider_gate = configure_provider_extension_gate(extension_is_enabled)
    try:
        async with _app_runtime_lifespan(_app):
            yield
    finally:
        configure_provider_extension_gate(previous_provider_gate)
        application_event_loop = None


ensure_runtime_dirs()
apply_network_settings(load_settings())
update_startup(
    "database",
    "正在檢查工作區資料庫。",
    detail="建立或更新本機資料結構",
    progress_percent=28,
)
database.init_db()
operations_core = OperationsCore(database_module=database)
operations_core.initialize()
model_governance = ModelGovernanceService(database_module=database, operations=operations_core)
model_governance.initialize()
configure_model_governance(model_governance)
mlops_service = MLOpsService(
    database_module=database,
    operations=operations_core,
    storage_root=RUNTIME_ROOT / "mlops",
)
mlops_service.initialize()
knowledge_settings = load_settings()
raw_knowledge_chunk_chars = knowledge_settings.get("chunk_size")
raw_knowledge_overlap_chars = knowledge_settings.get("chunk_overlap")
knowledge_chunk_chars = max(
    128,
    min(
        3000,
        int(
            600
            if raw_knowledge_chunk_chars is None or raw_knowledge_chunk_chars == ""
            else raw_knowledge_chunk_chars
        ),
    ),
)
knowledge_overlap_chars = max(
    0,
    min(
        min(1000, knowledge_chunk_chars // 2 - 1),
        int(
            120
            if raw_knowledge_overlap_chars is None or raw_knowledge_overlap_chars == ""
            else raw_knowledge_overlap_chars
        ),
    ),
)
knowledge_service, knowledge_service_available = _initialize_project_knowledge_service(
    RUNTIME_ROOT / "knowledge" / "project-knowledge.sqlite3",
    chunk_chars=knowledge_chunk_chars,
    overlap_chars=knowledge_overlap_chars,
    settings=knowledge_settings,
)
hook_audit_store = HookAuditStore(database_module=database)
hook_dispatcher = configure_hook_dispatcher(
    HookDispatcher.from_builtin_plugins(
        [DiagnosticBuiltinHookPlugin()],
        audit_sink=hook_audit_store.record,
    )
)
migrate_legacy_storage()
update_startup(
    "workspace",
    "正在準備聊天工作區。",
    detail="載入模型、對話與介面設定",
    progress_percent=92,
)


def _extension_runtime_health(item: Dict[str, Any]) -> tuple[str, Any]:
    extension_id = str(item.get("id") or "")
    if extension_id in {"connector.github", "connector.notion", "connector.gmail"}:
        assert connector_service is not None
        return connector_service.extension_health(extension_id)
    if extension_id == "builtin.n8n":
        lifecycle = n8n_lifecycle
        if lifecycle is None:
            return "unavailable", {"reason": "runtime_not_initialized"}
        status = lifecycle.status(probe_node=True)
        state = str(status.get("state") or "failed")
        return (
            "ready" if state == "ready" else "degraded" if state == "degraded" else "unavailable",
            {"state": state, "reason": status.get("reason")},
        )
    if extension_id.startswith("mcp."):
        coordinator = mcp_coordinator
        if coordinator is None:
            return "unavailable", {"reason": "mcp_coordinator_unavailable"}
        health = coordinator.health(extension_id)
        status = str(health.get("status") or "unknown")
        public_detail = {
            "status": status,
            "running": bool(health.get("running")),
            "tool_count": int(health.get("tool_count") or 0),
            "projects": list(health.get("projects") or []),
            "error_code": health.get("error_code"),
        }
        if status == "healthy" and health.get("running"):
            return "ready", public_detail
        return "degraded" if status not in {"unknown", "disabled"} else "unavailable", public_detail
    return "ready", {"registered": True}


def _handle_extension_state_change(
    extension_id: str,
    enabled: bool,
    _item: Dict[str, Any],
) -> None:
    """Immediately revoke runtime authority when a global extension is disabled."""

    if extension_id.startswith("mcp."):
        settings_id = extension_id.removeprefix("mcp.")
        cfg = load_settings()
        updated = False
        servers: list[dict[str, Any]] = []
        for raw in cfg.get("mcp_servers") or []:
            server = dict(raw)
            if str(server.get("id") or "").strip().casefold() == settings_id:
                server["enabled"] = bool(enabled)
                updated = True
            servers.append(server)
        if not updated:
            raise ValueError("The MCP settings entry is unavailable.")
        cfg["mcp_servers"] = servers
        save_settings(cfg)
        _schedule_mcp_sync()

    if extension_id == "builtin.n8n" and not enabled and n8n_lifecycle is not None:
        _on_managed_n8n_stop()
        state = n8n_lifecycle.status(probe_node=False)
        if str(state.get("state") or "") in {"ready", "starting", "degraded"}:
            n8n_lifecycle.stop()


def _handle_extension_state_rollback(
    extension_id: str,
    enabled: bool,
    item: Dict[str, Any],
) -> None:
    """Converge a compensated transition without rewriting MCP settings."""

    if extension_id.startswith("mcp."):
        # ExtensionRegistry already restored the one digest-bound settings
        # mirror.  Only enqueue reconciliation here, avoiding an ID-only write
        # against a concurrently replaced MCP configuration.
        _schedule_mcp_sync()
        return
    if extension_id == "builtin.n8n" and not enabled:
        # A failed revocation remains fail closed.  Retrying cleanup is safe and
        # never reopens the persisted authorization gate.
        _handle_extension_state_change(extension_id, False, item)


def _handle_project_extension_state_change(
    extension_id: str,
    _project_id: str,
    _mode: str,
    _item: Dict[str, Any],
) -> None:
    if extension_id.startswith("mcp."):
        _schedule_mcp_sync()


def _integration_connector_resource_filter(
    project_id: str,
    connector_id: str,
    connection_id: str,
    capability: str,
    resources: Any,
) -> List[Dict[str, Any]]:
    """Return only connector roots selected in the active central policy."""

    service = integration_center_service
    if service is None:
        raise RuntimeError("integration permission service is unavailable")
    allowed: List[Dict[str, Any]] = []
    for raw in list(resources or ())[:500]:
        if not isinstance(raw, dict):
            continue
        decision = service.permission_decision(
            project_id=project_id,
            integration_id=connector_id,
            capability=capability,
            connection_id=connection_id,
            resource_type=str(raw.get("resource_type") or ""),
            resource_id=str(raw.get("resource_id") or ""),
        )
        if str(decision.get("decision") or "deny") != "deny":
            allowed.append(dict(raw))
    return allowed


connector_service = ConnectorService(
    store=ConnectorStore(),
    secrets_store=ConnectorSecretStore(),
    project_exists=lambda project_id: database.get_project(project_id) is not None,
    resource_policy_filter=_integration_connector_resource_filter,
)
connector_service.initialize()
extension_registry = create_extension_registry(
    load_settings=load_settings,
    save_settings=save_settings,
    apply_configuration=apply_runtime_configuration,
    require_project=database.get_project,
    health_probes={
        "github": _extension_runtime_health,
        "notion": _extension_runtime_health,
        "gmail": _extension_runtime_health,
        "n8n": _extension_runtime_health,
        "mcp": _extension_runtime_health,
    },
    state_change_handler=_handle_extension_state_change,
    state_rollback_handler=_handle_extension_state_rollback,
    project_state_change_handler=_handle_project_extension_state_change,
    synchronize=True,
)


def _external_agent_policy_guard(project_id: str, required_scope: str) -> bool:
    """Fail closed until the unified Project policy service is ready."""

    service = integration_center_service
    if service is None:
        raise ExternalAgentApiError(
            "EXTERNAL_API_POLICY_UNAVAILABLE",
            "整合權限服務尚未就緒，未執行外部要求。",
            status_code=503,
            recoverable=True,
        )
    return service.external_api_policy_guard(project_id, required_scope)


external_agent_api_service = ExternalAgentApiService(
    project_exists=lambda project_id: database.get_project(project_id) is not None,
    policy_guard=_external_agent_policy_guard,
)
try:
    external_agent_api_service.initialize()
except Exception as exc:
    # A copied/corrupt DPAPI vault must degrade only the inbound integration;
    # core chat and the local management UI still need to start so the user can
    # repair or explicitly reset the installation identity.
    print(f"[EXTERNAL API] Credential initialization failed: {type(exc).__name__}")


def _manifest_digest(extension_id: str) -> str:
    row = extension_registry.store.get(extension_id)
    return str((row or {}).get("manifest_sha256") or "")


tool_registry = ToolRegistry()
tool_approval_broker = ToolApprovalBroker(database_module=database)
INDEPENDENT_TOOL_SCOPE = "__independent_chat__"
mcp_coordinator = MCPSettingsCoordinator(
    extension_registry=extension_registry,
    tool_registry=tool_registry,
    allowed_cwd_roots=(REPO_ROOT, PROJECTS_ROOT),
    project_ids_provider=_extension_project_ids,
    secret_resolver=_resolve_mcp_secret,
    operations=operations_core,
)


async def _prepare_project_tools(project_id: str) -> None:
    try:
        await mcp_coordinator.sync_from_settings(
            load_settings(),
            project_ids=_extension_project_ids(),
        )
    except Exception as exc:
        # MCP health/audit remains inspectable while connector tools and chat
        # continue to operate normally.
        print(f"[MCP] Project tool preparation failed: {type(exc).__name__}")
    if project_id == INDEPENDENT_TOOL_SCOPE:
        tool_registry.replace_project(
            project_id,
            mcp_coordinator.definitions_for_global_scope(project_id),
        )
        return
    mcp_definitions = tuple(
        definition
        for definition in tool_registry.for_project(project_id)
        if str(definition.extension_id).startswith("mcp.")
    )
    connector_definitions = tuple(
        definition
        for definition in connector_service.runtime_tool_definitions(
            project_id,
            _manifest_digest,
        )
        if extension_is_enabled(definition.extension_id, project_id)
    )
    provider_definitions = tuple(
        definition
        for definition in provider_runtime_tool_definitions(
            load_settings(),
            project_id=project_id,
            manifest_digest=_manifest_digest,
            governance=model_governance,
        )
        if extension_is_enabled(definition.extension_id, project_id)
    )
    # One atomic project replacement avoids a window where a healthy MCP tool
    # disappears while connector definitions are refreshed.
    tool_registry.replace_project(
        project_id,
        (*connector_definitions, *provider_definitions, *mcp_definitions),
    )


def _resolve_tool_scope(definition: Any, call: Any) -> ToolScopeState:
    if str(definition.extension_id) == CAPABILITY_STATUS_EXTENSION_ID:
        project_available = bool(
            call.project_id
            and call.project_id != INDEPENDENT_TOOL_SCOPE
            and database.get_project(call.project_id) is not None
        )
        return ToolScopeState(
            installed=True,
            trusted=True,
            enabled=project_available,
            healthy=project_available and capability_status_service is not None,
            resource_allowed=project_available,
            manifest_sha256=CAPABILITY_STATUS_MANIFEST_SHA256,
            resource_revision=0,
            connection_enabled=True,
            reason="" if project_available else "PROJECT_REQUIRED",
        )
    if str(definition.extension_id).startswith("mcp."):
        independent_scope = call.project_id == INDEPENDENT_TOOL_SCOPE
        registry_project_id = None if independent_scope else call.project_id
        try:
            item = extension_registry.get(
                definition.extension_id,
                registry_project_id,
                synchronize=False,
            )
            health = mcp_coordinator.health(definition.extension_id)
        except ExtensionError as exc:
            raise ToolUnavailableError(
                str(exc) or "MCP scope is unavailable.",
                details={"tool_name": definition.name},
            ) from exc
        active_projects = {str(value) for value in health.get("projects") or []}
        return ToolScopeState(
            installed=bool(item.get("installed")),
            trusted=bool(item.get("trusted")),
            enabled=bool(item.get("effective_enabled")),
            healthy=bool(health.get("running")) and (
                independent_scope or call.project_id in active_projects
            ),
            resource_allowed=not bool(definition.requires_resource),
            manifest_sha256=str(item.get("manifest_sha256") or ""),
            resource_revision=0,
            connection_enabled=not bool(definition.requires_connection),
            reason=str(health.get("error_code") or health.get("status") or ""),
        )
    if str(definition.extension_id).startswith("provider."):
        provider_id = str(definition.extension_id).split(".", 1)[1]
        configured = next(
            (
                item for item in load_settings().get("model_providers") or []
                if isinstance(item, dict)
                and str(item.get("id") or "").casefold() == provider_id
            ),
            None,
        )
        try:
            item = extension_registry.get(
                definition.extension_id,
                call.project_id,
                synchronize=False,
            )
        except ExtensionError as exc:
            raise ToolUnavailableError(str(exc), details={"tool_name": definition.name}) from exc
        model_id = str((configured or {}).get("selected_model") or "")
        endpoint = str((configured or {}).get("base_url") or "")
        decision = model_governance.operational_decision(
            provider_id,
            model_id=model_id,
            endpoint=endpoint,
        )
        policy = model_governance.get_routing_policy(call.project_id)
        consent = policy.get("data_consent") or {}
        allowed_provider = provider_id in set(policy.get("allowed_providers") or [])
        consent_allowed = allowed_provider and bool(consent.get("text"))
        if definition.name == "provider.ocr_image":
            consent_allowed = consent_allowed and bool(consent.get("images"))
        return ToolScopeState(
            installed=bool(item.get("installed")),
            trusted=bool(item.get("trusted")),
            enabled=bool(item.get("effective_enabled")),
            healthy=(
                bool(configured)
                and configured.get("enabled") is True
                and decision.allowed
                and consent_allowed
            ),
            resource_allowed=consent_allowed,
            manifest_sha256=str(item.get("manifest_sha256") or ""),
            resource_revision=int(policy.get("revision") or 0),
            connection_enabled=True,
            reason=(decision.message if not decision.allowed else "Project data consent is required" if not consent_allowed else ""),
        )
    try:
        invocation = connector_service.resolve_tool_invocation(
            call.project_id,
            definition.name,
            call.arguments,
            verify_remote_scope=True,
        )
        item = extension_registry.get(
            definition.extension_id,
            call.project_id,
            synchronize=False,
        )
        connection = connector_service.get_connection(invocation["connection_id"])
    except (ConnectorServiceError, ConnectorStoreError, ExtensionError) as exc:
        raise ToolUnavailableError(
            str(exc) or "Connector scope is unavailable.",
            details={"tool_name": definition.name},
        ) from exc
    return ToolScopeState(
        installed=bool(item.get("installed")),
        trusted=bool(item.get("trusted")),
        enabled=bool(item.get("effective_enabled")),
        healthy=str(connection.get("status") or "") == "connected",
        resource_allowed=True,
        manifest_sha256=str(item.get("manifest_sha256") or ""),
        resource_revision=int(invocation.get("resource_revision") or 0),
        connection_enabled=True,
        connection_id=str(invocation["connection_id"]),
        resource_id=str(invocation["resource_id"]),
        reason=str(connection.get("error_code") or ""),
    )


_INTEGRATION_TOOL_CAPABILITIES = {
    "github.list_repositories": "repository.read",
    "github.read_file": "repository.read",
    "github.list_commits": "repository.read",
    "github.list_issues": "issue.read",
    "github.get_issue": "issue.read",
    "github.list_pull_requests": "pull_request.read",
    "github.get_pull_request": "pull_request.read",
    "github.get_check_runs": "checks.read",
    "github.create_issue": "issue.write",
    "github.update_issue": "issue.write",
    # GitHub exposes Issue and Pull Request conversations through one endpoint.
    "github.add_issue_comment": "discussion.comment",
    "notion.search": "content.read",
    "notion.retrieve_page": "content.read",
    "notion.retrieve_database": "content.read",
    "notion.create_page": "content.insert",
    "notion.update_page": "content.update",
    "notion.append_blocks": "content.update",
    "gmail.search_messages": "message.read",
    "gmail.get_message": "message.read",
    "gmail.create_draft": "draft.create",
    "gmail.send_draft": "draft.send",
}


def _unified_tool_permission(definition, call, scope) -> Optional[PolicyDecision]:
    """Apply the central Project capability/resource policy at call time."""

    service = integration_center_service
    extension_id = str(definition.extension_id or "")
    tool_name = str(definition.name or "")
    integration_id = ""
    capability = ""
    connection_id = getattr(scope, "connection_id", None)
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None

    if extension_id.startswith("connector."):
        integration_id = extension_id.split(".", 1)[1]
        capability = _INTEGRATION_TOOL_CAPABILITIES.get(tool_name, "")
        try:
            invocation = connector_service.resolve_tool_invocation(
                call.project_id,
                tool_name,
                call.arguments,
                verify_remote_scope=True,
            )
        except (ConnectorServiceError, ConnectorStoreError):
            return PolicyDecision(
                PolicyAction.DENY,
                "無法重新確認第三方連線與資源範圍。",
            )
        connection_id = str(invocation.get("connection_id") or "") or connection_id
        policy_resource = invocation.get("verified_scope_root")
        candidate_resource = str(
            (policy_resource or {}).get("resource_id")
            if isinstance(policy_resource, dict)
            else invocation.get("resource_id") or ""
        )
        if candidate_resource and candidate_resource != "*":
            resource_type = str(
                (policy_resource or {}).get("resource_type")
                if isinstance(policy_resource, dict)
                else invocation.get("resource_type") or ""
            ) or None
            resource_id = candidate_resource
    elif extension_id.startswith("mcp."):
        integration_id = "mcp"
        capability = "tool.invoke"
        connection_id = extension_id
        resource_type = "tool"
        resource_id = tool_name
    else:
        return None

    if service is None or not capability:
        return PolicyDecision(
            PolicyAction.DENY,
            "整合權限服務尚未就緒，未執行外部工具。",
        )
    if not call.project_id or call.project_id == INDEPENDENT_TOOL_SCOPE:
        return PolicyDecision(
            PolicyAction.DENY,
            "第三方與 MCP 工具必須先綁定 Project，才能套用整合權限。",
        )
    try:
        decision = service.permission_decision(
            project_id=call.project_id,
            integration_id=integration_id,
            capability=capability,
            connection_id=connection_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
    except Exception:
        return PolicyDecision(
            PolicyAction.DENY,
            "目前無法確認整合權限，已停止工具呼叫。",
        )
    action = str(decision.get("decision") or "deny")
    if action == "allow":
        return PolicyDecision(PolicyAction.ALLOW, "整合中心已允許此能力與資源")
    if action == "require_approval":
        return PolicyDecision(
            PolicyAction.REQUIRE_APPROVAL,
            "整合中心限制權限：此操作會改變外部資料，需逐次批准。",
        )
    return PolicyDecision(
        PolicyAction.DENY,
        "整合中心未放行此能力、連線或資源範圍。",
    )


def _evaluate_tool_permission(definition, call, scope) -> PolicyDecision:
    """Intersect unified scope with the existing extension safety policy."""

    unified = _unified_tool_permission(definition, call, scope)
    if unified is not None and unified.action is PolicyAction.DENY:
        return unified

    level = "restricted"
    if call.project_id:
        registry_project_id = (
            None if call.project_id == INDEPENDENT_TOOL_SCOPE else call.project_id
        )
        try:
            item = extension_registry.get(
                definition.extension_id,
                registry_project_id,
                synchronize=False,
            )
            level = str((item.get("project_permission") or {}).get("level") or "restricted")
        except ExtensionError:
            level = "restricted"
    if level == "blocked":
        return PolicyDecision(
            PolicyAction.DENY,
            "此專案未開放這項擴充權限；請到外掛詳細頁調整權限等級。",
        )
    if definition.access is ToolAccess.READ:
        return unified or PolicyDecision(PolicyAction.ALLOW, "唯讀操作已允許")
    if unified is not None and unified.action is PolicyAction.REQUIRE_APPROVAL:
        return unified
    if level == "open":
        return unified or PolicyDecision(
            PolicyAction.ALLOW,
            "此專案已明確選擇開放權限",
        )
    operation_class = operation_risk_class(
        definition.name,
        definition.risk_level,
    )
    if operation_class == "low_risk":
        return PolicyDecision(
            PolicyAction.ALLOW,
            "限制權限允許低風險操作",
        )
    return PolicyDecision(
        PolicyAction.REQUIRE_APPROVAL,
        "此操作可能輸入資料、改變外部狀態或造成不可逆結果，需要使用者批准。",
    )


tool_dispatcher = ToolDispatcher(
    tool_registry,
    scope_resolver=_resolve_tool_scope,
    hook_dispatcher=hook_dispatcher,
    policy_evaluator=_evaluate_tool_permission,
)


async def _query_capability_status(project_id: str, query: str) -> Dict[str, Any]:
    service = capability_status_service
    if service is None:
        return {
            "schema_version": 1,
            "project_id": str(project_id or ""),
            "query": str(query or "")[:500],
            "items": [],
            "summary": {"total": 0, "available": 0, "blocked": 0},
            "error": {
                "code": "CAPABILITY_STATUS_UNAVAILABLE",
                "message": "Workbench 功能狀態服務尚未就緒。",
            },
        }
    return await service.query(project_id, query)


host_tool_runtime = HostToolRuntime(
    registry=tool_registry,
    dispatcher=tool_dispatcher,
    approval_broker=tool_approval_broker,
    prepare_project=_prepare_project_tools,
    capability_status_query=_query_capability_status,
    independent_scope_id=INDEPENDENT_TOOL_SCOPE,
    resolve_call_context=lambda project_id, definition, arguments: (
        connector_service.resolve_host_call_context(
            project_id,
            definition,
            arguments,
        )
        if str(definition.extension_id).startswith("connector.")
        else {}
    ),
)


def save_settings_and_sync(settings: Dict[str, Any]) -> None:
    save_settings(settings)
    extension_registry.sync()
    _schedule_mcp_sync()


def require_extension_http(
    extension_id: str,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        return extension_registry.require_enabled(extension_id, project_id)
    except ExtensionError as exc:
        raise HTTPException(
            status_code=409,
            detail=error_payload(
                getattr(exc, "code", "EXTENSION_DISABLED"),
                str(exc),
                recoverable=True,
            ),
        ) from exc


app = FastAPI(title="Local AI Workbench Chat API", lifespan=app_lifespan)
install_local_session_guard(app, error_payload)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-Workbench-Token",
    ],
)


@app.exception_handler(RequestValidationError)
async def redacted_request_validation_error(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Keep malformed credential payloads out of FastAPI's default 422 body."""

    errors = []
    for raw in exc.errors():
        errors.append(
            {
                "type": str(raw.get("type") or "validation_error")[:128],
                "loc": [str(part)[:128] for part in raw.get("loc") or ()],
                "msg": str(redact(str(raw.get("msg") or "Invalid request.")))[:500],
            }
        )
    return JSONResponse(status_code=422, content={"detail": errors})


_PUBLIC_AGENT_API_BODY_LIMIT = 128 * 1024


@app.middleware("http")
async def limit_public_agent_api_body(request: Request, call_next):
    """Bound unauthenticated request parsing before the public route runs."""

    if (
        request.url.path.startswith("/api/public/v1/")
        and request.method in {"POST", "PUT", "PATCH"}
    ):
        content_length = request.headers.get("content-length")
        try:
            declared = int(content_length) if content_length is not None else None
        except ValueError:
            declared = -1
        if declared is not None and (
            declared < 0 or declared > _PUBLIC_AGENT_API_BODY_LIMIT
        ):
            return JSONResponse(
                status_code=413,
                content=error_payload(
                    "EXTERNAL_API_REQUEST_TOO_LARGE",
                    "對外 API 要求內容不可超過 128 KiB。",
                    recoverable=False,
                ),
            )
        chunks: List[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > _PUBLIC_AGENT_API_BODY_LIMIT:
                return JSONResponse(
                    status_code=413,
                    content=error_payload(
                        "EXTERNAL_API_REQUEST_TOO_LARGE",
                        "對外 API 要求內容不可超過 128 KiB。",
                        recoverable=False,
                    ),
                )
            chunks.append(chunk)
        body = b"".join(chunks)
        request._body = body
        delivered = False

        async def replay_body() -> Dict[str, Any]:
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = replay_body
    return await call_next(request)


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
    save_settings=save_settings_and_sync,
    error_payload=error_payload,
    create_id=create_id,
    require_local_workbench=require_local_workbench,
    rag_stats=disabled_service_status,
    ollama_models=ollama_models,
    require_extension=require_extension_http,
    app_version=APP_VERSION,
    agent_protocol_version=1,
    operations=operations_core,
)

operations_router = build_operations_router(
    core=operations_core,
    require_local=require_local_workbench,
    require_project=database.get_project,
)

mlops_router = build_mlops_router(
    service=mlops_service,
    require_local=require_local_workbench,
    require_project=database.get_project,
)

knowledge_router = build_knowledge_router(
    service=knowledge_service,
    require_local=require_local_workbench,
    require_project=database.get_project,
    extract_pdf_text=extract_pdf_text,
    temporary_root=TEMP_DIR,
    error_payload=knowledge_error_payload,
    settings_loader=load_settings,
    semantic_consent_proposal_factory=_knowledge_semantic_consent_proposal,
)

settings_router = build_settings_router(
    load_settings=load_settings,
    save_settings=save_settings_and_sync,
    validate_settings=validate_settings,
    effective_config=effective_config,
    normalize_modal_size=normalize_settings_modal_size,
    apply_configuration=apply_runtime_configuration,
    error_payload=error_payload,
    require_local=require_local_workbench,
    hermes_rollout_guard=guard_hermes_rollout,
    model_governance=model_governance,
)

model_governance_router = build_model_governance_router(
    service=model_governance,
    load_settings=load_settings,
    model_inventory=model_inventory,
    require_local=require_local_workbench,
    require_project=database.get_project,
    error_payload=error_payload,
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
    health_reporter=lambda status, reason, detail: operations_core.report_health(
        component_type="hermes",
        component_id="sidecar",
        status=status,
        reason_code=reason,
        detail=detail,
    ),
)
hermes_rollout_gate = HermesRolloutGate(status_provider=hermes_operational_status)
project_skills_router = build_project_skills_router(
    store=project_skill_store,
    runtime=project_skill_runtime,
    require_local=require_local_workbench,
    error_payload=error_payload,
    session_access_guard=guard_integration_session,
)


def _resolve_n8n_agent_skill(
    project_id: str, slug: str, sha256: str
) -> Dict[str, Any]:
    """Load one immutable Project Skill snapshot for the tool-free runtime."""

    snapshot = project_skill_store.get_version(project_id, slug, sha256)
    return {
        "slug": str(snapshot["slug"]),
        "sha256": str(snapshot["sha256"]),
        "instructions": str(snapshot["instructions"]),
    }


def _configured_n8n_protected_workflows() -> Dict[str, Dict[str, str]]:
    """Read reviewed bridge identities without probing n8n during startup."""

    configured: Dict[str, Dict[str, str]] = {}
    for node_type, variables, name in (
        (
            "workbench.agent",
            ("WORKBENCH_N8N_AGENT_BRIDGE_WORKFLOW_ID",),
            AGENT_BRIDGE_WORKFLOW_NAME,
        ),
        (
            "workbench.approval",
            (
                "WORKBENCH_N8N_APPROVAL_GATE_WORKFLOW_ID",
                "WORKBENCH_N8N_APPROVAL_BRIDGE_WORKFLOW_ID",
            ),
            APPROVAL_GATE_WORKFLOW_NAME,
        ),
    ):
        workflow_id = next(
            (
                str(os.environ.get(variable) or "").strip()
                for variable in variables
                if str(os.environ.get(variable) or "").strip()
            ),
            "",
        )
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workflow_id):
            configured[node_type] = {"workflow_id": workflow_id, "name": name}
    return configured


n8n_lifecycle = ManagedN8nLifecycle()
n8n_secret_store = N8nGmailSecretStore()
n8n_agent_secret_store = N8nAgentSecretStore()


def _inspect_configured_n8n_agent_bridges() -> Dict[str, Any]:
    """Lazily attest exact callable bridges and configured workflow IDs."""

    try:
        raw = inspect_agent_bridge_workflows_readiness(n8n_lifecycle.paths)
    except Exception:
        raw = {
            "ready": False,
            "code": "agent_bridge_workflows_not_ready",
            "blockers": ["agent_bridge_read_failed"],
            "workflows": {},
        }
    blockers = [
        str(item)[:128]
        for item in raw.get("blockers") or []
        if isinstance(item, str)
    ]
    configured = _configured_n8n_protected_workflows()
    expected = {
        "workbench.agent": AGENT_BRIDGE_TEMPLATE_ID,
        "workbench.approval": APPROVAL_GATE_TEMPLATE_ID,
    }
    safe_workflows: Dict[str, Dict[str, Any]] = {}
    for node_type, template_id in expected.items():
        item = (raw.get("workflows") or {}).get(template_id)
        item = item if isinstance(item, dict) else {}
        attested_id = str(item.get("workflow_id") or "").strip()
        configured_id = str(
            (configured.get(node_type) or {}).get("workflow_id") or ""
        ).strip()
        safe_workflows[template_id] = {
            "workflow_id": attested_id or None,
            "present": item.get("present") is True,
            "published": item.get("published") is True,
            "active": item.get("active") is True,
            "valid": item.get("valid") is True,
            "protected": True,
        }
        if not configured_id:
            blockers.append(f"{template_id}_configured_id_missing")
        elif not attested_id or not secrets.compare_digest(configured_id, attested_id):
            blockers.append(f"{template_id}_configured_id_mismatch")

    # The compiler captured these identities at process construction.  An
    # environment change requires a Workbench restart, never a live retarget.
    engine = globals().get("n8n_graph_authoring")
    protected = getattr(engine, "protected_workflows", {}) if engine is not None else {}
    if isinstance(protected, dict):
        for node_type, template_id in expected.items():
            compiled_id = str(
                (protected.get(node_type) or {}).get("workflow_id") or ""
            ).strip() if isinstance(protected.get(node_type), dict) else ""
            configured_id = str(
                (configured.get(node_type) or {}).get("workflow_id") or ""
            ).strip()
            if compiled_id and configured_id and not secrets.compare_digest(
                compiled_id, configured_id
            ):
                blockers.append(f"{template_id}_compiler_id_stale")

    blockers = list(dict.fromkeys(blockers))
    ready = raw.get("ready") is True and not blockers
    return {
        "ready": ready,
        "code": "ready" if ready else "agent_bridge_workflows_not_ready",
        "blockers": blockers,
        "workflows": safe_workflows,
        "credential_bindings": {
            "hmac_bound": (raw.get("credential_bindings") or {}).get("hmac_bound") is True,
            "hmac_configured": (raw.get("credential_bindings") or {}).get("hmac_configured") is True,
        },
    }


def _require_configured_n8n_agent_bridges() -> Dict[str, Any]:
    report = _inspect_configured_n8n_agent_bridges()
    if report.get("ready") is not True:
        raise RuntimeError("The reviewed Workbench Agent bridge workflows are not ready.")
    return report


def _resolve_managed_n8n_credential(credential_id: str) -> Dict[str, Any]:
    """Return bounded credential metadata from the managed read-only DB."""

    opaque_id = str(credential_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", opaque_id):
        raise KeyError("invalid n8n credential id")
    root = n8n_lifecycle.paths.n8n_dir.resolve()
    candidate = n8n_lifecycle.paths.n8n_dir / "database.sqlite"
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise KeyError("managed n8n database is unavailable") from exc
    if not resolved.is_file():
        raise KeyError("managed n8n database is unavailable")
    uri = f"file:{resolved.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            row = connection.execute(
                """
                SELECT id, name, type,
                       CASE WHEN data IS NOT NULL AND length(data) >= 16
                            THEN 1 ELSE 0 END AS configured
                FROM credentials_entity WHERE id=?
                """,
                (opaque_id,),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise KeyError("n8n credential metadata is unavailable") from exc
    if row is None or str(row["id"]) != opaque_id:
        raise KeyError("n8n credential was not found")
    return {
        "id": opaque_id,
        "name": str(row["name"] or "")[:255],
        "type": str(row["type"] or "")[:128],
        "status": "ready" if int(row["configured"] or 0) == 1 else "degraded",
    }


def _resolve_live_managed_n8n_workflow_revision(workflow_id: str) -> Dict[str, Any]:
    """Read the exact active n8n version from the managed DB, never from Agent input."""

    opaque_id = str(workflow_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", opaque_id):
        raise KeyError("invalid n8n workflow id")
    root = n8n_lifecycle.paths.n8n_dir.resolve()
    candidate = n8n_lifecycle.paths.n8n_dir / "database.sqlite"
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise KeyError("managed n8n database is unavailable") from exc
    if not resolved.is_file():
        raise KeyError("managed n8n database is unavailable")
    uri = f"file:{resolved.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            row = connection.execute(
                "SELECT id, active, activeVersionId FROM workflow_entity WHERE id=?",
                (opaque_id,),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise KeyError("n8n workflow revision metadata is unavailable") from exc
    if row is None or str(row["id"]) != opaque_id:
        raise KeyError("n8n workflow was not found")
    active_version_id = str(row["activeVersionId"] or "").strip()
    if bool(row["active"]) and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", active_version_id
    ):
        raise KeyError("n8n active workflow revision is unavailable")
    return {
        "active": bool(row["active"]),
        "active_version_id": active_version_id,
    }


def _resolve_live_n8n_agent_policy(project_id: str) -> Dict[str, Any]:
    """Resolve policy at decision time so the Agent cannot retain elevation."""

    return n8n_agent_governance.get_policy(project_id)


def _integration_n8n_permission(
    project_id: str,
    capability: str,
    *,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
) -> Dict[str, Any]:
    service = integration_center_service
    if service is None:
        raise RuntimeError("integration permission service is unavailable")
    return service.permission_decision(
        project_id=project_id,
        integration_id="n8n",
        capability=capability,
        resource_type=resource_type,
        resource_id=resource_id,
    )


n8n_agent_task_runtime = N8nAgentTaskRuntime(
    cipher=AesGcmContentCipher(n8n_agent_secret_store.content_key),
    hmac_secret_provider=n8n_secret_store.inbound_hmac_verifier_key,
    generator=N8nAgentModelRuntime(settings_loader=load_settings),
    skill_resolver=_resolve_n8n_agent_skill,
    credential_resolver=_resolve_managed_n8n_credential,
    policy_resolver=_resolve_live_n8n_agent_policy,
    execution_gate=lambda project_id: extension_registry.require_enabled(
        "builtin.n8n", project_id
    ),
    integration_permission_check=_integration_n8n_permission,
    workflow_revision_resolver=_resolve_live_managed_n8n_workflow_revision,
)


def _resolve_n8n_graph_credential(
    alias: str, expected_type: str, context: Dict[str, Any]
) -> Dict[str, Any]:
    project_id = str(context.get("project_id") or "").strip()
    return n8n_agent_task_runtime.credential_alias_resolver(
        project_id, alias, expected_type
    )


n8n_graph_authoring = LazyGraphAuthoringEngine(
    credential_resolver=_resolve_n8n_graph_credential,
    protected_workflows=_configured_n8n_protected_workflows(),
    binding_resolver=n8n_agent_task_runtime.binding_resolver,
)


def _finalize_n8n_graph_bindings(
    claims: List[Dict[str, Any]], context: Dict[str, Any]
) -> List[Dict[str, Any]]:
    _require_configured_n8n_agent_bridges()
    return n8n_agent_task_runtime.finalize_bindings(
        str(context.get("workflow_id") or ""),
        str(context.get("workflow_revision") or ""),
        claims,
        str(context.get("project_id") or ""),
        session_id=str(context.get("session_id") or "").strip() or None,
    )


def _activate_n8n_graph_bindings(
    context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Activate only Agent bindings present in the reconciled active graph."""

    _require_configured_n8n_agent_bridges()

    project_id = str(context.get("project_id") or "").strip()
    workflow_id = str(context.get("workflow_id") or "").strip()
    workflow_revision = str(context.get("workflow_revision") or "").strip()
    workflow = context.get("workflow")
    if not isinstance(workflow, dict):
        raise RuntimeError("The reconciled active workflow is unavailable.")

    graph_bindings: Dict[str, tuple[str, str]] = {}
    protected_agent = n8n_graph_authoring.protected_workflows.get("workbench.agent")
    protected_agent_id = (
        str(protected_agent.get("workflow_id") or "").strip()
        if isinstance(protected_agent, dict)
        else ""
    )
    candidate_bindings: Dict[str, tuple[str, str, str]] = {}
    nodes = workflow.get("nodes")
    for node in nodes if isinstance(nodes, list) else []:
        if (
            not isinstance(node, dict)
            or node.get("type") != "n8n-nodes-base.executeWorkflow"
        ):
            continue
        node_id = str(node.get("id") or "").strip()
        parameters = node.get("parameters")
        workflow_target = parameters.get("workflowId") if isinstance(parameters, dict) else None
        target_id = (
            str(workflow_target.get("value") or "").strip()
            if isinstance(workflow_target, dict)
            else ""
        )
        inputs = parameters.get("workflowInputs") if isinstance(parameters, dict) else None
        values = inputs.get("value") if isinstance(inputs, dict) else None
        binding_id = str(values.get("agent_binding_id") or "").strip() if isinstance(values, dict) else ""
        compiled_revision = (
            str(values.get("workflow_revision") or "").strip()
            if isinstance(values, dict)
            else ""
        )
        if node_id and binding_id:
            candidate_bindings[node_id] = (
                binding_id,
                target_id,
                compiled_revision,
            )
        if (
            protected_agent_id
            and node_id
            and binding_id
            and target_id == protected_agent_id
        ):
            graph_bindings[node_id] = (binding_id, compiled_revision)

    workflow_bindings = [
        binding
        for binding in n8n_agent_task_runtime.list_bindings(project_id)
        if (
            str(binding.get("project_id") or "") == project_id
            and str(binding.get("workflow_id") or "") == workflow_id
        )
    ]
    known_binding_ids = {
        str(binding.get("agent_binding_id") or "") for binding in workflow_bindings
    }
    if any(
        binding_id in known_binding_ids and target_id != protected_agent_id
        for binding_id, target_id, _revision in candidate_bindings.values()
    ):
        raise RuntimeError("An Agent binding targets an unverified bridge workflow.")
    selected = [
        str(binding["agent_binding_id"])
        for binding in workflow_bindings
        if graph_bindings.get(str(binding.get("node_id") or ""))
        == (
            str(binding.get("agent_binding_id") or ""),
            str(binding.get("workflow_revision") or ""),
        )
    ]
    selected_pairs = {
        (
            str(binding.get("node_id") or ""),
            (
                str(binding.get("agent_binding_id") or ""),
                str(binding.get("workflow_revision") or ""),
            ),
        )
        for binding in workflow_bindings
        if str(binding.get("agent_binding_id") or "") in selected
    }
    if set(graph_bindings.items()) != selected_pairs:
        raise RuntimeError("The active workflow contains an unknown Agent binding.")
    if not selected:
        for binding in workflow_bindings:
            if binding.get("active") is True:
                n8n_agent_task_runtime.deactivate_binding(
                    str(binding["agent_binding_id"]), project_id=project_id
                )
        return []
    return n8n_agent_task_runtime.activate_bindings(
        workflow_id, workflow_revision, selected, project_id
    )


def _on_n8n_agent_policy_change(project_id: str, reason: str) -> None:
    n8n_agent_task_runtime.notify_policy_changed(project_id, reason=reason)


def _on_n8n_agent_workflow_change(context: Dict[str, Any]) -> None:
    """Revoke grants and disable bindings invalidated by graph lifecycle changes."""

    project_id = str(context.get("project_id") or "").strip()
    workflow_id = str(context.get("workflow_id") or "").strip()
    operation = str(context.get("operation") or "").strip()
    if operation in {"update_draft", "deactivate", "delete"}:
        bindings = n8n_agent_task_runtime.list_bindings(project_id)
        for binding in bindings:
            if (
                str(binding.get("project_id") or "") == project_id
                and str(binding.get("workflow_id") or "") == workflow_id
                and binding.get("active") is True
            ):
                n8n_agent_task_runtime.deactivate_binding(
                    str(binding["agent_binding_id"]), project_id=project_id
                )
    n8n_agent_task_runtime.notify_workflow_changed(
        project_id, workflow_id, reason=f"workflow_{operation or 'changed'}"
    )


_stored_n8n_mail_profile = database.get_n8n_gmail_profile() or {}
_stored_n8n_recipient = str(
    _stored_n8n_mail_profile.get("fixed_recipient") or ""
).strip()
_configured_n8n_recipient = str(
    os.environ.get("WORKBENCH_N8N_GMAIL_RECIPIENT")
    or (
        _stored_n8n_recipient
        if _stored_n8n_recipient.casefold() != FIXED_TEST_RECIPIENT.casefold()
        else ""
    )
    or ""
).strip()
_n8n_recipient_is_configured = bool(
    _configured_n8n_recipient
    and _configured_n8n_recipient.casefold() != FIXED_TEST_RECIPIENT.casefold()
)


def _integration_gmail_permission(
    project_id: str,
    capability: str,
    *,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
) -> Dict[str, Any]:
    service = integration_center_service
    if service is None:
        raise RuntimeError("integration permission service is unavailable")
    return service.permission_decision(
        project_id=project_id,
        integration_id="gmail",
        capability=capability,
        resource_type=resource_type,
        resource_id=resource_id,
    )


n8n_gmail_service = N8nGmailService(
    cipher=AesGcmContentCipher(n8n_secret_store.content_key),
    hmac_secret_provider=n8n_secret_store.inbound_hmac_verifier_key,
    outbound_secret_provider=n8n_secret_store.outbound_webhook_key,
    draft_generator=EmailDraftRuntime(
        settings_loader=load_settings,
        project_skill_runtime=project_skill_runtime,
        database=database,
    ),
    delivery_dispatcher=N8nDeliveryDispatcher(
        secret_provider=n8n_secret_store.outbound_webhook_key,
    ),
    enable_guard=n8n_profile_enable_guard,
    permission_check=_integration_gmail_permission,
    fixed_recipient=_configured_n8n_recipient or FIXED_TEST_RECIPIENT,
    recipient_configured=_n8n_recipient_is_configured,
)


def _integration_n8n_status() -> Dict[str, Any]:
    state = n8n_lifecycle.status(probe_node=False)
    status = str(state.get("state") or "stopped")
    return {
        "status": status,
        "healthy": status == "ready",
        "managed": bool(state.get("managed")),
        "version": str(state.get("version") or "")[:128] or None,
    }


def _integration_gmail_status() -> Dict[str, Any]:
    profile = n8n_gmail_service.get_profile()
    return {
        "configured": bool(profile.get("configured")),
        "enabled": bool(profile.get("enabled")),
        "project_id": profile.get("project_id"),
        "required_label": str(profile.get("required_label") or "")[:256] or None,
        "fixed_recipient": str(profile.get("fixed_recipient") or "")[:320] or None,
        "recipient_configured": bool(profile.get("recipient_configured")),
        "crypto_ready": bool(profile.get("crypto_ready")),
        "isolation_ready": bool(profile.get("isolation_ready")),
        "pending_approvals": int(
            n8n_gmail_service.public_event_snapshot().get("pending_approvals") or 0
        ),
    }


def _integration_mcp_status() -> Dict[str, Any]:
    snapshot = mcp_coordinator.health()
    extensions = snapshot.get("extensions") if isinstance(snapshot, dict) else {}
    return {
        "status": str(snapshot.get("status") or "stopped"),
        "healthy": str(snapshot.get("status") or "") == "healthy",
        "running": int(snapshot.get("running") or 0),
        "extensions": [
            {
                "extension_id": str(extension_id),
                "status": str((record or {}).get("status") or "unknown"),
                "running": bool((record or {}).get("running")),
                "projects": [str(value) for value in (record or {}).get("projects") or []],
                "tool_count": int((record or {}).get("tool_count") or 0),
            }
            for extension_id, record in list((extensions or {}).items())[:64]
            if isinstance(record, dict)
        ],
    }


def _integration_external_api_status(project_id: str) -> Dict[str, Any]:
    payload = external_agent_api_service.list_keys()
    keys = [
        item
        for item in payload.get("api_keys") or []
        if isinstance(item, dict) and str(item.get("project_id") or "") == project_id
    ]
    active = sum(str(item.get("status") or "") == "active" for item in keys)
    recovery_required = bool(payload.get("credential_recovery_required"))
    return {
        "configured": bool(keys),
        "enabled": active > 0 and not recovery_required,
        "healthy": active > 0 and not recovery_required,
        "status": (
            "credential_recovery_required"
            if recovery_required
            else "ready"
            if active
            else "not_configured"
        ),
        "credential_recovery_required": recovery_required,
        "active_key_count": active,
        "key_count": len(keys),
    }


def _integration_runtime_audits(
    project_id: str,
    limit: int,
) -> List[Dict[str, Any]]:
    """Merge safe connector and inbound-API events for one Project."""

    bounded = max(1, min(int(limit), 500))
    result: List[Dict[str, Any]] = []
    try:
        rows = external_agent_api_service.store.list_audits(
            limit=min(500, bounded * 4)
        )
    except Exception:
        rows = []
    for row in rows:
        if str(row.get("project_id") or "") != project_id:
            continue
        result.append(
            {
                "audit_id": str(row.get("audit_id") or "")[:128],
                "project_id": project_id,
                "action": str(row.get("action") or "external_api.operation")[:128],
                "actor": "external_api",
                "status": str(row.get("status") or "unknown")[:64],
                "details": row.get("details") if isinstance(row.get("details"), dict) else {},
                "error_code": str(row.get("error_code") or "")[:128] or None,
                "created_at": str(row.get("created_at") or "")[:80],
                "source": "external_api",
            }
        )
    for connector_id in ("github", "notion", "gmail"):
        try:
            connector_rows = connector_service.store.list_audits(
                connector_id,
                limit=min(500, bounded * 4),
            )
        except Exception:
            connector_rows = []
        for row in connector_rows:
            if str(row.get("project_id") or "") != project_id:
                continue
            result.append(
                {
                    "audit_id": str(row.get("audit_id") or "")[:128],
                    "project_id": project_id,
                    "action": str(row.get("action") or "connector.operation")[:128],
                    "actor": f"connector.{connector_id}",
                    "status": str(row.get("status") or "unknown")[:64],
                    "details": row.get("details") if isinstance(row.get("details"), dict) else {},
                    "error_code": str(row.get("error_code") or "")[:128] or None,
                    "created_at": str(row.get("created_at") or "")[:80],
                    "source": "connector",
                }
            )
    return result[: min(500, bounded * 3)]


def _integration_runtime_gate_setter(
    _project_id: str,
    _mode: str,
    _grants: List[Dict[str, Any]],
) -> Any:
    """Attest that operation-time policy checks are connected.

    Policy persistence remains the source of truth. Connector and MCP tools
    consult it in ``_evaluate_tool_permission``; n8n/Gmail retain their
    extension/project gates; the inbound API consults ``policy_guard`` on every
    request. The applier calls this adapter before committing so a missing live
    service cannot produce a policy that only looks active in the UI.
    """

    if integration_center_service is None:
        raise RuntimeError("integration runtime gate is unavailable")
    return lambda: None


integration_policy_applier = AuthoritativeIntegrationPolicyApplier(
    extension_registry=extension_registry,
    connector_service=connector_service,
    connector_gate_setter=_integration_runtime_gate_setter,
    n8n_gate_setter=_integration_runtime_gate_setter,
    mcp_gate_setter=_integration_runtime_gate_setter,
    external_api_gate_setter=_integration_runtime_gate_setter,
)
integration_center_service = IntegrationCenterService(
    store=IntegrationCenterStore(),
    project_exists=lambda project_id: database.get_project(project_id) is not None,
    authoritative_applier=integration_policy_applier,
    extension_catalog_provider=extension_registry.catalog,
    connector_service=connector_service,
    n8n_status_provider=_integration_n8n_status,
    gmail_profile_provider=_integration_gmail_status,
    mcp_status_provider=_integration_mcp_status,
    external_api_summary_provider=_integration_external_api_status,
    audit_providers=(_integration_runtime_audits,),
)
integration_center_service.initialize()
capability_status_service = CapabilityStatusService(
    project_exists=lambda project_id: database.get_project(project_id) is not None,
    integration_overview_provider=integration_center_service.overview,
    extension_catalog_provider=extension_registry.catalog,
    model_overview_provider=lambda project_id: model_governance.overview(
        project_id=project_id,
        providers=load_settings().get("model_providers") or [],
    ),
)
for _definition in build_capability_status_tool_definitions(capability_status_service):
    tool_registry.register(_definition, replace_existing=True)


def _migrate_existing_integration_policies() -> None:
    """One-shot, fail-closed import of healthy pre-existing Project grants."""

    for project in database.get_projects():
        project_id = str((project or {}).get("id") or "").strip()
        if not project_id:
            continue
        try:
            integration_center_service.import_existing_project_policy(project_id)
        except Exception as exc:
            # An uncertain integration remains blocked. One Project migration
            # must never prevent core chat or another Project from loading.
            print(
                f"[INTEGRATION] Existing policy import failed for {project_id[:64]}: "
                f"{type(exc).__name__}"
            )

n8n_agent_governance = N8nAgentGovernanceService(
    broker=N8nApiBroker(n8n_agent_secret_store.api_key),
    cipher=AesGcmContentCipher(n8n_agent_secret_store.content_key),
    n8n_running=lambda: n8n_lifecycle.status(probe_node=False).get("state") == "ready",
    # High-risk nodes remain fail-closed until the separate disposable runner
    # has its own attestation implementation.
    high_risk_runner_ready=lambda: False,
    graph_authoring=n8n_graph_authoring,
    graph_binding_finalizer=_finalize_n8n_graph_bindings,
    graph_binding_activator=_activate_n8n_graph_bindings,
    policy_change_callback=_on_n8n_agent_policy_change,
    workflow_change_callback=_on_n8n_agent_workflow_change,
    integration_permission_check=_integration_n8n_permission,
)


def _n8n_agent_planning_context(
    project_id: str, *, session_id: str
) -> Dict[str, Any]:
    """Expose only safe Project aliases and active Skill snapshot summaries."""

    aliases: List[Dict[str, Any]] = []
    try:
        aliases = [
            {
                "alias": str(item.get("alias") or "")[:128],
                "credential_type": str(item.get("credential_type") or "")[:128],
                "status": str(item.get("status") or "unknown")[:32],
            }
            for item in n8n_agent_task_runtime.list_credential_aliases(project_id)
            if isinstance(item, dict)
        ][:100]
    except Exception:
        aliases = []

    skills: List[Dict[str, Any]] = []
    try:
        catalog = project_skill_runtime.catalog_for_session(session_id)
        if str(catalog.get("project_id") or "") == project_id:
            skills = [
                {
                    "slug": str(item.get("slug") or "")[:63],
                    "name": str(item.get("name") or "")[:80],
                    "description": str(item.get("description") or "")[:500],
                    "version": str(item.get("version") or "")[:64],
                    "sha256": str(item.get("sha256") or "")[:64],
                    "active": item.get("active") is True,
                }
                for item in catalog.get("skills") or []
                if isinstance(item, dict)
            ][:100]
    except Exception:
        skills = []

    settings = load_settings() or {}
    return {
        "default_model": str(settings.get("default_chat_model") or "")[:255],
        "credential_aliases": aliases,
        "project_skills": skills,
    }


n8n_agent_planner = N8nPlanningService(
    governance_service=n8n_agent_governance,
    generator=N8nPlanModelGenerator(
        settings_loader=load_settings,
        catalog_search=n8n_agent_governance.search_node_catalog,
    ),
    graph_authoring=n8n_graph_authoring,
    protected_workflow_guard=_inspect_configured_n8n_agent_bridges,
    planning_context_provider=_n8n_agent_planning_context,
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
    project_change_guard=guard_n8n_project_change,
    project_delete_cleanup=clear_project_knowledge_for_delete,
    project_delete_guard=project_knowledge_delete_guard,
)

n8n_gmail_router = build_n8n_gmail_router(
    service=n8n_gmail_service,
    require_local=require_local_workbench,
    error_payload=error_payload,
    require_extension=require_extension_http,
)
n8n_agent_router = build_n8n_agent_router(
    service=n8n_agent_governance,
    secret_store=n8n_agent_secret_store,
    planner=n8n_agent_planner,
    require_local=require_local_workbench,
    error_payload=error_payload,
)
n8n_agent_tasks_router = build_n8n_agent_tasks_router(
    runtime=n8n_agent_task_runtime,
    require_local=require_local_workbench,
    error_payload=error_payload,
)
n8n_runtime_router = build_n8n_runtime_router(
    lifecycle=n8n_lifecycle,
    require_local=require_local_workbench,
    error_payload=error_payload,
    workflow_ready=lambda: gmail_workflows_ready(n8n_lifecycle.paths),
    workflow_status=lambda: inspect_gmail_workflows_readiness(n8n_lifecycle.paths),
    mail_status=n8n_gmail_service.public_event_snapshot,
    on_stop=_on_managed_n8n_stop,
    require_extension=require_extension_http,
)

extensions_router = build_extensions_router(
    registry=extension_registry,
    require_local=require_local_workbench,
    error_payload=error_payload,
)
connectors_router = build_connectors_router(
    service=connector_service,
    require_local=require_local_workbench,
    error_payload=error_payload,
    require_extension=require_extension_http,
)
connector_callbacks_router = build_connector_callback_router(
    service=connector_service,
    require_extension=require_extension_http,
)


def _observe_session_lifecycle(action: str, payload: Dict[str, Any]) -> None:
    event = f"session.{action}"
    hook_dispatcher.observe_sync(
        event,
        HookContext(
            event=event,
            project_id=(
                str(payload.get("project_id"))
                if payload.get("project_id")
                else None
            ),
            session_id=str(payload.get("session_id") or ""),
            metadata={"model": payload.get("model")},
        ),
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
    session_change_guard=guard_integration_session,
    session_lifecycle_observer=_observe_session_lifecycle,
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
    session_access_guard=guard_integration_session,
)


def _resolve_tool_approval(
    run_id: str,
    approval_id: str,
    approved: bool,
) -> Optional[Dict[str, Any]]:
    try:
        return tool_approval_broker.decide(
            run_id=run_id,
            approval_id=approval_id,
            approved=approved,
            decided_by="local_session",
            rationale=(
                "Approved once by the local Workbench user."
                if approved
                else "Denied by the local Workbench user."
            ),
        )
    except ToolApprovalNotFound:
        return None
    except ToolApprovalBrokerError as exc:
        raise HTTPException(
            status_code=409,
            detail=error_payload(
                getattr(exc, "code", "TOOL_APPROVAL_INVALID"),
                str(exc),
                recoverable=True,
            ),
        ) from exc

hermes_router = build_hermes_router(
    manager_provider=lambda: hermes_manager_cache.try_get(load_settings()),
    status_provider=hermes_status_payload,
    require_local=require_local_workbench,
    error_payload=error_payload,
    cancel_local_run=cancel_chat_run,
    rollback_handler=rollback_hermes_rollout,
    generic_approval_resolver=_resolve_tool_approval,
)

run_results_router = build_run_results_router(
    database=database,
    error_payload=error_payload,
)

integration_center_router = build_integration_center_router(
    service=integration_center_service,
    require_local=require_local_workbench,
    error_payload=error_payload,
)

for domain_router in (
    system_router,
    sessions_router,
    projects_router,
    project_skills_router,
    attachments_router,
    extensions_router,
    connectors_router,
    connector_callbacks_router,
    hermes_router,
    n8n_gmail_router,
    n8n_agent_router,
    n8n_agent_tasks_router,
    n8n_runtime_router,
    run_results_router,
    settings_router,
    models_router,
    model_governance_router,
    operations_router,
    mlops_router,
    knowledge_router,
    integration_center_router,
):
    app.include_router(domain_router)


def _chat_routing_requirements(
    request: ChatRequest,
    *,
    retry_manifest: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    external_data_types = _chat_external_data_types(
        request,
        retry_manifest=retry_manifest,
    )
    return {
        "kind": "chat",
        "tools": False,
        "text": True,
        "images": "images" in external_data_types,
        "documents": "documents" in external_data_types,
    }


def _chat_external_data_types(
    request: ChatRequest,
    *,
    retry_manifest: Optional[Dict[str, Any]] = None,
) -> tuple[str, ...]:
    retry_uses_documents = bool(
        isinstance(retry_manifest, dict)
        and (
            retry_manifest.get("knowledge_context")
            or retry_manifest.get("knowledge_sources")
            or retry_manifest.get("attachment_ids")
            or retry_manifest.get("temporary_context_id")
            or retry_manifest.get("temporary_context")
        )
    )
    result: List[str] = []
    if request.images:
        result.append("images")
    attachment_documents = False
    for attachment_id in request.attachment_ids:
        attachment = database.get_attachment(str(attachment_id))
        mime_type = str((attachment or {}).get("mime_type") or "").casefold()
        if mime_type.startswith("image/"):
            if "images" not in result:
                result.append("images")
        else:
            # Unknown IDs remain fail-closed as documents here and are rejected
            # by the later authoritative scope check before any provider call.
            attachment_documents = True
    if (
        attachment_documents
        or request.temporary_context_id
        or str(request.temporary_context or "").strip()
        or request.use_rag
        or retry_uses_documents
    ):
        result.append("documents")
    return tuple(result)


def _routing_proposal_includes_data_consent(
    proposal_id: Optional[str],
    *,
    data_types: tuple[str, ...],
    project_id: Optional[str],
    run_id: str,
    requested_model: str,
    selected_model: str,
) -> bool:
    """Accept only consumed document authority bound to this exact request."""

    if not proposal_id or model_governance is None:
        return False
    try:
        return bool(data_types) and all(
            model_governance.proposal_grants_data(
                str(proposal_id),
                data_type=data_type,
                project_id=project_id,
                run_id=run_id,
                requested_model=requested_model,
                selected_model=selected_model,
            )
            for data_type in data_types
        )
    except (AttributeError, TypeError, ValueError, sqlite3.Error):
        return False


def _remote_model_data_consent(
    settings: Dict[str, Any],
    *,
    project_id: Optional[str],
    model: str,
    data_types: tuple[str, ...],
    approved_once: bool,
) -> tuple[bool, Dict[str, Any]]:
    """Require project or bound one-time consent before rich-data disclosure."""

    try:
        provider_config = provider_for_model(
            settings,
            model,
            project_id=project_id,
        )
        provider = provider_config.provider.casefold()
    except (AttributeError, PermissionError, ValueError):
        return False, {
            "project_id": project_id,
            "model": model,
            "required_data": list(data_types),
            "reason": "model_provider_unavailable",
        }
    if provider == "ollama" and _is_loopback_model_endpoint(
        getattr(provider_config, "base_url", "")
    ):
        return True, {"provider": provider, "consent_source": "local_boundary"}
    if approved_once:
        return True, {"provider": provider, "consent_source": "routing_proposal"}
    if not project_id:
        return False, {
            "project_id": None,
            "provider": provider,
            "model": model,
            "required_data": list(data_types),
            "reason": "one_time_consent_required",
            "policy_revision": 0,
        }
    try:
        policy = model_governance.get_routing_policy(project_id)
    except (AttributeError, sqlite3.Error):
        return False, {
            "project_id": project_id,
            "provider": provider,
            "model": model,
            "required_data": list(data_types),
            "reason": "routing_policy_unavailable",
        }
    provider_allowed = provider in {
        str(item).casefold() for item in policy.get("allowed_providers") or []
    }
    consent = policy.get("data_consent") or {}
    data_allowed = {
        data_type: bool(consent.get(data_type)) for data_type in data_types
    }
    allowed = provider_allowed and all(data_allowed.values())
    return allowed, {
        "project_id": project_id,
        "provider": provider,
        "model": model,
        "required_data": list(data_types),
        "policy_revision": int(policy.get("revision") or 0),
        "provider_allowed": provider_allowed,
        "data_allowed": data_allowed,
        "documents_allowed": data_allowed.get("documents", True),
        "images_allowed": data_allowed.get("images", True),
        "policy_endpoint": f"/api/projects/{project_id}/model-routing-policy",
    }


def _remote_knowledge_consent(
    settings: Dict[str, Any],
    *,
    project_id: str,
    model: str,
    approved_once: bool,
) -> tuple[bool, Dict[str, Any]]:
    """Compatibility wrapper for the project-knowledge policy boundary."""

    return _remote_model_data_consent(
        settings,
        project_id=project_id,
        model=model,
        data_types=("documents",),
        approved_once=approved_once,
    )


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

    existing_session = database.get_session(session_id)
    session = existing_session
    if session and str(session.get("mode") or "").casefold() == "email":
        raise HTTPException(
            status_code=404,
            detail=error_payload(
                "SESSION_NOT_FOUND",
                "Session was not found.",
                recoverable=False,
            ),
        )
    project = (
        database.get_project(session["project_id"])
        if session and session.get("project_id")
        else None
    )
    project_id = project.get("id") if project else None
    settings = {**settings, "_extension_project_id": project_id}
    routing_requirements = _chat_routing_requirements(
        request,
        retry_manifest=retry_manifest,
    )
    external_data_types = _chat_external_data_types(
        request,
        retry_manifest=retry_manifest,
    )
    routing_proposal_data_consent = False
    semantic_consent_proposal_id = ""

    requested_model = model
    routing_decision: Dict[str, Any] = {}
    if request.routing_proposal_id:
        selected = model_governance.consume_proposal(
            request.routing_proposal_id,
            project_id=project_id,
            requested_model=requested_model,
            run_id=run_id,
        )
        if not selected:
            raise HTTPException(
                status_code=409,
                detail=error_payload(
                    "ROUTING_PROPOSAL_INVALID",
                    "選模建議已過期、已使用或不屬於目前專案。",
                    recoverable=True,
                ),
            )
        selected_data_consent = _routing_proposal_includes_data_consent(
            request.routing_proposal_id,
            data_types=external_data_types,
            project_id=project_id,
            run_id=run_id,
            requested_model=requested_model,
            selected_model=selected,
        )
        try:
            selected_profile = model_profile_for_model(
                settings,
                selected,
                project_id=project_id,
            )
        except (ValueError, PermissionError):
            selected_profile = None
        if selected_profile is None or not selected_profile.eligible_for_primary:
            if selected_data_consent and request.use_rag and project_id:
                # A project-knowledge consent proposal is bound to its dedicated
                # Embedding model. Consuming that authority must never replace the
                # primary chat model with a non-chat model.
                semantic_consent_proposal_id = request.routing_proposal_id
                model = requested_model
            else:
                raise HTTPException(
                    status_code=409,
                    detail=error_payload(
                        "ROUTING_PROPOSAL_INVALID",
                        "這份同意只授權專案知識的專用模型，不能用來取代主要對話模型。",
                        recoverable=True,
                    ),
                )
        else:
            model = selected
            routing_proposal_data_consent = selected_data_consent
            routing_decision = {
                "routed": model != requested_model,
                "requested_model": requested_model,
                "model": model,
                "reason": "user_approved",
                "provider": provider_for_model(
                    settings, model, project_id=project_id
                ).provider,
            }
    else:
        try:
            requested_profile = model_profile_for_model(
                settings,
                requested_model,
                project_id=project_id,
            )
        except (ValueError, PermissionError):
            requested_profile = None
        requested_route_blocked = False
        if requested_profile is not None and requested_profile.eligible_for_primary:
            try:
                requested_provider = provider_for_model(
                    settings,
                    requested_model,
                    project_id=project_id,
                )
                requested_route_blocked = not model_governance.operational_decision(
                    requested_provider.provider,
                    model_id=(requested_model.split("::", 1)[1] if "::" in requested_model else requested_model),
                    endpoint=requested_provider.base_url,
                    claim_half_open=False,
                ).allowed
            except (ValueError, PermissionError):
                requested_route_blocked = True
        if requested_profile is None or not requested_profile.eligible_for_primary or requested_route_blocked:
            try:
                resolution = model_governance.resolve_route(
                    project_id=project_id,
                    run_id=run_id,
                    requested_model=requested_model,
                    requirements=routing_requirements,
                    candidates=model_inventory(),
                )
            except GovernanceError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail=error_payload(exc.code, str(exc), exc.details, recoverable=True),
                ) from exc
            if resolution.get("status") == "approval_required":
                raise HTTPException(
                    status_code=409,
                    detail=error_payload(
                        "MODEL_ROUTE_APPROVAL_REQUIRED",
                        "目前模型不適合此工作或暫時不可用；請確認建議模型後再執行。",
                        resolution,
                        recoverable=True,
                    ),
                )
            model = str(resolution["model"])
            routing_decision = {
                "routed": model != requested_model,
                "requested_model": requested_model,
                **resolution,
            }
    if external_data_types:
        consented, consent_detail = _remote_model_data_consent(
            settings,
            project_id=str(project_id) if project_id else None,
            model=model,
            data_types=external_data_types,
            approved_once=routing_proposal_data_consent,
        )
        if not consented:
            try:
                consent_proposal = model_governance.create_data_consent_proposal(
                    project_id=str(project_id) if project_id else None,
                    run_id=run_id,
                    requested_model=requested_model,
                    selected_model=model,
                    provider_id=str(consent_detail.get("provider") or ""),
                    data_types=external_data_types,
                )
            except (AttributeError, GovernanceError, ValueError, sqlite3.Error) as exc:
                raise HTTPException(
                    status_code=503,
                    detail=error_payload(
                        "MODEL_DATA_CONSENT_UNAVAILABLE",
                        "目前無法建立安全的文件傳送同意，尚未將任何文件送往雲端。",
                        {"reason": "proposal_creation_failed"},
                        recoverable=True,
                        suggestions=["稍後重試", "改用本機模型"],
                    ),
                ) from exc
            data_type_label = (
                "圖片與文件內容"
                if set(external_data_types) == {"images", "documents"}
                else "圖片"
                if external_data_types == ("images",)
                else "文件與文字內容"
            )
            consent_payload = {
                **consent_detail,
                **consent_proposal,
                "data_type": list(external_data_types),
                "data_type_label": data_type_label,
                "risk": f"{data_type_label}將離開本機，傳送至所列雲端供應商處理。",
                "consequences": [
                    "供應商會依其服務條款處理這些資料，可能受其留存與稽核政策影響。",
                    "資料若含機密、個資或未公開資訊，可能造成不適當的外部揭露。",
                    "選擇「記住此專案」後，未來符合相同專案政策的同類資料可自動傳送，直到你變更政策。",
                ],
                "actions": [
                    {"id": "model_data_policy", "label": "檢視預算與選模政策"},
                    {"id": "choose_model", "label": "改用其他模型"},
                ],
            }
            raise HTTPException(
                status_code=409,
                detail=error_payload(
                    "MODEL_DATA_CONSENT_REQUIRED",
                    "將圖片或文件內容傳送到雲端模型前，需要先取得明確同意。",
                    consent_payload,
                    recoverable=True,
                    suggestions=["檢查供應商、風險與後果後選擇僅本次同意或記住此專案"],
                ),
            )
    settings["_governance_run_id"] = run_id
    settings["_governance_budget_override_id"] = request.budget_override_id or ""

    if existing_session is None:
        database.create_session(session_id, model=model)
        session = database.get_session(session_id)
    elif model != requested_model:
        database.update_session_metadata(session_id, model=model)

    if existing_session is None:
        await hook_dispatcher.observe(
            "session.created",
            HookContext(
                event="session.created",
                project_id=project_id,
                session_id=session_id,
                metadata={"model": model},
            ),
        )

    temporary_text, images = _resolve_chat_inputs(
        request,
        session_id=session_id,
        project_id=project_id,
    )

    input_hook_context = HookContext(
        event="chat.input.before_dispatch",
        project_id=project_id,
        session_id=session_id,
        run_id=run_id,
        retry_of_run_id=request.retry_of_run_id,
        metadata={"model": model, "input_kind": "chat"},
    )
    hook_snapshot = _hook_snapshot_payload()
    if retry_manifest is not None and "hook_snapshot" in retry_manifest:
        dispatch_query = str(retry_manifest.get("user_message") or "")
        hook_transform_steps = [
            dict(item)
            for item in retry_manifest.get("hook_transform_steps") or []
            if isinstance(item, dict)
        ]
    else:
        try:
            transformed = await hook_dispatcher.transform_with_trace(
                "chat.input.before_dispatch",
                input_hook_context,
                user_query,
            )
        except HookRuntimeError as exc:
            raise HTTPException(
                status_code=409,
                detail=error_payload(
                    getattr(exc, "code", "HOOK_TRANSFORM_FAILED"),
                    str(exc),
                    recoverable=True,
                ),
            ) from exc
        dispatch_query = str(transformed.value or "").strip()
        hook_transform_steps = [dict(step.__dict__) for step in transformed.steps]
    if not dispatch_query:
        raise HTTPException(
            status_code=409,
            detail=error_payload(
                "HOOK_TRANSFORM_FAILED",
                "A trusted input hook produced an empty chat request.",
                recoverable=True,
            ),
        )
    try:
        input_guard = await hook_dispatcher.guard(
            "chat.input.before_dispatch",
            input_hook_context,
        )
    except HookRuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail=error_payload(
                getattr(exc, "code", "HOOK_GUARD_UNAVAILABLE"),
                str(exc),
                recoverable=True,
            ),
        ) from exc
    if input_guard.action is not GuardAction.ABSTAIN:
        raise HTTPException(
            status_code=403 if input_guard.action is GuardAction.DENY else 409,
            detail=error_payload(
                "HOOK_GUARD_DENIED"
                if input_guard.action is GuardAction.DENY
                else "HOOK_APPROVAL_UNSUPPORTED",
                input_guard.reason
                or "A trusted hook did not permit this chat input.",
                recoverable=True,
            ),
        )

    # Hermes accepts text plus the host-bounded Project knowledge context.
    # Images and stored attachments stay on the mature basic-chat path until
    # their boundary is explicitly reviewed. A missing/invalid optional sidecar
    # also resolves to basic chat.
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
                dispatch_query,
                run_id=run_id,
                consume_turn=True,
            )
            project_skill_context = hermes_skill_attachment.instructions
            project_skill_provenance = hermes_skill_attachment.provenance
            project_skills_truncated = hermes_skill_attachment.truncated
        else:
            project_skill_prompt = project_skill_runtime.build_prompt_context(
                session_id,
                dispatch_query,
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

    knowledge_context = ""
    knowledge_sources: List[Dict[str, Any]] = []
    knowledge_evidence_bundle: Optional[EvidenceBundle] = None
    if retry_manifest is not None:
        knowledge_context = str(retry_manifest.get("knowledge_context") or "")
        knowledge_sources = [
            dict(item)
            for item in retry_manifest.get("knowledge_sources") or []
            if isinstance(item, dict)
            and str(item.get("project_id") or "") == str(project_id or "")
        ]
        try:
            knowledge_evidence_bundle = _knowledge_evidence_from_snapshot(
                retry_manifest.get("knowledge_evidence"),
                project_id=project_id,
            )
        except FactualVerificationError as exc:
            raise HTTPException(
                status_code=409,
                detail=error_payload(
                    exc.code,
                    "專案知識證據快照無法安全重建，請建立新的執行。",
                    recoverable=True,
                ),
            ) from exc
    elif request.use_rag and not project_id:
        raise HTTPException(
            status_code=409,
            detail=error_payload(
                "KNOWLEDGE_PROJECT_REQUIRED",
                "使用專案知識前，請先將目前對話指派到一個專案。",
                recoverable=True,
            ),
        )
    elif request.use_rag and project_id:
        try:
            retrieved = await _retrieve_project_knowledge_async(
                project_id=str(project_id),
                query=dispatch_query,
                top_k=int(settings.get("rag_k") or 4),
                candidate_limit=max(20, int(settings.get("rag_k") or 4) * 5),
                run_id=run_id,
                consent_proposal_id=(
                    semantic_consent_proposal_id
                    or (
                        request.routing_proposal_id
                        if routing_proposal_data_consent
                        else ""
                    )
                ),
                requested_model=requested_model,
                # A model budget override is a single-use grant and may already
                # be consumed by the primary chat route. Semantic retrieval has
                # its own budget decision and must not reuse that authority.
                budget_override_id="",
            )
            threshold = float(settings.get("rag_rerank_threshold") or 0.0)
            if threshold > 0:
                retrieved = [
                    item
                    for item in retrieved
                    if float(item.get("score") or 0.0) >= threshold
                ]
            (
                knowledge_context,
                knowledge_sources,
                knowledge_evidence_bundle,
            ) = _knowledge_prompt_context(
                retrieved,
                project_id=str(project_id),
                include_evidence=True,
            )
        except ProjectKnowledgeError as exc:
            consent_failure = _semantic_consent_failure(exc)
            if consent_failure is not None:
                try:
                    consent_payload = _semantic_consent_proposal(
                        project_id=str(project_id),
                        run_id=run_id,
                        requested_model=requested_model,
                        failure=consent_failure,
                    )
                except (GovernanceError, ValueError, sqlite3.Error) as proposal_exc:
                    raise HTTPException(
                        status_code=503,
                        detail=error_payload(
                            "MODEL_DATA_CONSENT_UNAVAILABLE",
                            "目前無法建立安全的語意模型資料傳送同意；尚未送出任何文件。",
                            {"reason": "proposal_creation_failed"},
                            recoverable=True,
                            suggestions=["稍後重試", "改用本機 Embedding"],
                        ),
                    ) from proposal_exc
                raise HTTPException(
                    status_code=409,
                    detail=error_payload(
                        "MODEL_DATA_CONSENT_REQUIRED",
                        "將專案文件片段傳送到雲端語意模型前，需要先取得明確同意。",
                        consent_payload,
                        recoverable=True,
                        suggestions=[
                            "檢查供應商、模型、風險與後果後，選擇僅本次同意或記住此專案"
                        ],
                    ),
                ) from exc
            raise HTTPException(
                status_code=exc.status_code,
                detail=error_payload(
                    exc.code,
                    "目前無法取得專案知識；請檢查索引後再試一次。",
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

    run_hook_context = HookContext(
        event="run.before_start",
        project_id=project_id,
        session_id=session_id,
        run_id=run_id,
        retry_of_run_id=request.retry_of_run_id,
        deadline_monotonic=(
            time.monotonic() + float(settings.get("chat_run_budget_seconds") or 600)
        ),
        metadata={
            "model": model,
            "runtime": "hermes" if hermes_manager is not None else "basic_chat",
        },
    )
    try:
        run_guard = await hook_dispatcher.guard("run.before_start", run_hook_context)
    except HookRuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail=error_payload(
                getattr(exc, "code", "HOOK_GUARD_UNAVAILABLE"),
                str(exc),
                recoverable=True,
            ),
        ) from exc
    if run_guard.action is not GuardAction.ABSTAIN:
        raise HTTPException(
            status_code=403 if run_guard.action is GuardAction.DENY else 409,
            detail=error_payload(
                "HOOK_GUARD_DENIED"
                if run_guard.action is GuardAction.DENY
                else "HOOK_APPROVAL_UNSUPPORTED",
                run_guard.reason or "A trusted hook did not permit this run.",
                recoverable=True,
            ),
        )

    cancel_session_chat_runs(session_id, exclude_run_id=run_id)
    normalized_prompt = dispatch_query.replace("\r\n", "\n").replace("\r", "\n").strip()
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
            llm_content=dispatch_query,
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
        user_query=dispatch_query,
        history_snapshot=history_snapshot,
        knowledge_context=knowledge_context,
        knowledge_sources=knowledge_sources,
        knowledge_evidence_bundle=knowledge_evidence_bundle,
        hook_snapshot=hook_snapshot,
        hook_transform_steps=hook_transform_steps,
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
            user_query=dispatch_query,
            temporary_context=temporary_text,
            images=images,
            run_control=run_control,
            project_id=project_id,
            project_skill_context=fallback_skill_context,
            project_skill_sources=project_skill_provenance,
            knowledge_context=knowledge_context,
            knowledge_sources=knowledge_sources,
            evidence_bundle=knowledge_evidence_bundle,
            retry_of_run_id=request.retry_of_run_id,
            input_manifest=run_input_manifest,
            history_snapshot=history_snapshot,
            archive_sync=sync_session_archive,
            host_tool_runtime=host_tool_runtime,
            routing_decision=routing_decision,
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
                    user_query=dispatch_query,
                    temporary_context=temporary_text,
                    knowledge_context=knowledge_context,
                    knowledge_sources=knowledge_sources,
                    evidence_bundle=knowledge_evidence_bundle,
                    answer_verification_mode=str(
                        settings.get("answer_verification_mode") or "warn"
                    ),
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
    if existing and str(existing.get("mode") or "").casefold() == "email":
        raise HTTPException(
            status_code=404,
            detail=error_payload(
                "RUN_NOT_FOUND", "Run not found.", recoverable=False
            ),
        )
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
    guard_integration_session(session_id, "runs_read")
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
    if str(run.get("mode") or "").casefold() == "email":
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


def _external_api_runtime_error(exc: HTTPException) -> ExternalAgentApiError:
    """Translate an internal chat rejection without exposing provider details."""

    detail = exc.detail if isinstance(exc.detail, dict) else {}
    code = str(detail.get("code") or "EXTERNAL_API_RUN_REJECTED")[:128]
    status_code = int(exc.status_code)
    if code in {
        "MODEL_ROUTE_APPROVAL_REQUIRED",
        "MODEL_DATA_CONSENT_REQUIRED",
        "MODEL_BUDGET_OVERRIDE_REQUIRED",
    }:
        message = "此工作需要先回到 Workbench 完成模型、資料傳送或預算同意。"
    elif status_code == 429:
        message = "Agent 目前忙碌或已達使用限制，請稍後再試。"
    elif status_code in {401, 403}:
        message = "目前的 Project 政策不允許執行這項工作。"
    elif status_code == 404:
        message = "找不到這項工作需要的本機資源。"
    else:
        message = "Workbench 未能接受這項 Agent 工作，請回到執行紀錄檢查設定。"
    return ExternalAgentApiError(
        code,
        message,
        status_code=max(400, min(status_code, 503)),
        recoverable=bool(detail.get("recoverable", status_code >= 409)),
    )


def _release_external_api_slot(api_key_id: str, run_id: str) -> None:
    active = external_api_active_runs.get(api_key_id)
    if active is not None:
        active.discard(run_id)
        if not active:
            external_api_active_runs.pop(api_key_id, None)
    external_api_run_sessions.pop(run_id, None)


async def _consume_external_api_stream(
    response: StreamingResponse,
    *,
    api_key_id: str,
    run_id: str,
) -> None:
    """Drive the existing SSE runtime while keeping its internals private."""

    try:
        async for _chunk in response.body_iterator:
            # Public callers poll the strict Run DTO. Raw SSE, prompts, tool
            # arguments and provider payloads never cross this boundary.
            pass
    except asyncio.CancelledError:
        cancel_or_defer_chat_run(run_id)
        raise
    except Exception as exc:
        # The mature chat runtime persists a redacted failure whenever it has
        # registered the Run. Keep the core Workbench alive if an unexpected
        # iterator error occurs before that persistence point.
        print(f"[EXTERNAL API] Background Run failed: {type(exc).__name__}")
    finally:
        _release_external_api_slot(api_key_id, run_id)


async def _external_api_submit_run(
    run_id: str,
    payload: Dict[str, Any],
    auth_context: Dict[str, Any],
) -> Dict[str, Any]:
    project_id = str(auth_context.get("project_id") or "")
    api_key_id = str(auth_context.get("api_key_id") or "")
    if not project_id or not api_key_id or database.get_project(project_id) is None:
        raise ExternalAgentApiError(
            "EXTERNAL_API_PROJECT_NOT_FOUND",
            "找不到此 API Key 綁定的 Project。",
            status_code=404,
            recoverable=False,
        )
    total_active = sum(len(item) for item in external_api_active_runs.values())
    key_active = external_api_active_runs.setdefault(api_key_id, set())
    if total_active >= MAX_EXTERNAL_API_RUNS or len(key_active) >= MAX_EXTERNAL_API_RUNS_PER_KEY:
        if not key_active:
            external_api_active_runs.pop(api_key_id, None)
        raise ExternalAgentApiError(
            "EXTERNAL_API_CONCURRENCY_LIMITED",
            "此 Workbench 或 API Key 已有過多執行中的工作，請稍後再試。",
            status_code=429,
            recoverable=True,
            retry_after=5,
        )

    settings = load_settings()
    model = str(payload.get("model") or settings.get("default_chat_model") or "").strip()
    if not model:
        raise ExternalAgentApiError(
            "EXTERNAL_API_MODEL_UNAVAILABLE",
            "目前沒有可用的主要對話模型。",
            status_code=409,
            recoverable=True,
        )
    session_id = create_id("sess")
    database.create_session(
        session_id,
        title="外部 API 工作",
        mode="chat",
        model=model,
        project_id=project_id,
    )
    key_active.add(run_id)
    external_api_run_sessions[run_id] = session_id
    try:
        response = await chat(
            ChatRequest(
                session_id=session_id,
                message=str(payload.get("message") or ""),
                model=model,
                use_rag=bool(payload.get("use_rag")),
                run_id=run_id,
            )
        )
        if not isinstance(response, StreamingResponse):
            raise ExternalAgentApiError(
                "EXTERNAL_API_RUNTIME_CONTRACT_INVALID",
                "Agent 執行服務未建立有效的串流工作。",
                status_code=502,
                recoverable=True,
            )
        task = asyncio.create_task(
            _consume_external_api_stream(
                response,
                api_key_id=api_key_id,
                run_id=run_id,
            ),
            name=f"external-agent-{run_id}",
        )
        external_api_background_tasks.add(task)
        task.add_done_callback(external_api_background_tasks.discard)
    except HTTPException as exc:
        _release_external_api_slot(api_key_id, run_id)
        if database.get_run(run_id) is None and not database.get_messages_by_session(session_id):
            database.delete_session(session_id)
        raise _external_api_runtime_error(exc) from exc
    except ExternalAgentApiError:
        _release_external_api_slot(api_key_id, run_id)
        if database.get_run(run_id) is None and not database.get_messages_by_session(session_id):
            database.delete_session(session_id)
        raise
    except Exception as exc:
        _release_external_api_slot(api_key_id, run_id)
        if database.get_run(run_id) is None and not database.get_messages_by_session(session_id):
            database.delete_session(session_id)
        raise ExternalAgentApiError(
            "EXTERNAL_API_RUNTIME_UNAVAILABLE",
            "Agent 執行服務暫時無法使用。",
            status_code=503,
            recoverable=True,
        ) from exc
    return {
        "run_id": run_id,
        "project_id": project_id,
        "session_id": session_id,
        "model": model,
        "status": "queued",
        "created_at": now_iso(),
    }


def _external_api_run_status(value: Any) -> str:
    status = str(value or "").strip().casefold()
    if status in {"waiting_approval", "awaiting_approval", "approval_required"}:
        return "approval_required"
    if status in {"queued", "pending", "running", "completed", "failed", "cancelled"}:
        return status
    return "running"


def _external_api_public_run_error(run: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw = _run_public_error(run)
    if raw is None:
        return None
    code = str(raw.get("code") or "RUN_FAILED")[:128]
    if code == "RUN_CANCELLED":
        message = "此 Agent 工作已取消。"
    else:
        message = "Agent 工作執行失敗；請回到 Workbench 查看受治理的執行紀錄。"
    return {
        "code": code,
        "message": message,
        "recoverable": bool(raw.get("recoverable")),
    }


def _external_api_get_run(
    run_id: str,
    auth_context: Dict[str, Any],
) -> Dict[str, Any]:
    project_id = str(auth_context.get("project_id") or "")
    run = database.get_run(run_id)
    if run is None:
        session_id = external_api_run_sessions.get(run_id)
        if session_id:
            return {
                "run_id": run_id,
                "project_id": project_id,
                "session_id": session_id,
                "status": "running" if get_chat_run(run_id) is not None else "queued",
            }
        return {
            "run_id": run_id,
            "project_id": project_id,
            "status": "failed",
            "error": {
                "code": "EXTERNAL_API_RUN_NOT_STARTED",
                "message": "Agent 工作未能啟動，請使用新的 Idempotency-Key 明確重試。",
                "recoverable": True,
            },
        }
    if str(run.get("project_id") or "") != project_id:
        raise ExternalAgentApiError(
            "EXTERNAL_API_RUN_NOT_FOUND",
            "找不到此 Project 的 Agent 工作。",
            status_code=404,
            recoverable=False,
        )
    result: Dict[str, Any] = {
        "run_id": run_id,
        "project_id": project_id,
        "session_id": str(run.get("session_id") or "") or None,
        "model": str(run.get("model") or "") or None,
        "status": _external_api_run_status(run.get("status")),
        "created_at": str(run.get("created_at") or "") or None,
        "completed_at": str(run.get("completed_at") or "") or None,
    }
    if result["status"] == "completed" and result.get("session_id"):
        turn_id = str(run.get("turn_id") or "")
        for message in reversed(database.get_messages_by_session(result["session_id"])):
            if str(message.get("role") or "") != "assistant":
                continue
            if turn_id and str(message.get("turn_id") or "") != turn_id:
                continue
            result["answer"] = str(message.get("visible_content") or message.get("content") or "")[:262_144]
            break
    public_error = _external_api_public_run_error(run)
    if public_error is not None:
        result["error"] = public_error
    return {key: value for key, value in result.items() if value is not None}


def _external_api_cancel_run(
    run_id: str,
    auth_context: Dict[str, Any],
) -> Dict[str, Any]:
    project_id = str(auth_context.get("project_id") or "")
    existing = database.get_run(run_id)
    if existing is not None and str(existing.get("project_id") or "") != project_id:
        raise ExternalAgentApiError(
            "EXTERNAL_API_RUN_NOT_FOUND",
            "找不到此 Project 的 Agent 工作。",
            status_code=404,
            recoverable=False,
        )
    if existing is not None and str(existing.get("status") or "") in {
        "completed",
        "failed",
        "cancelled",
    }:
        return _external_api_get_run(run_id, auth_context)
    cancel_or_defer_chat_run(run_id)
    return {
        "run_id": run_id,
        "project_id": project_id,
        "session_id": (
            str(existing.get("session_id") or "")
            if existing is not None
            else external_api_run_sessions.get(run_id)
        ),
        "model": str(existing.get("model") or "") if existing is not None else None,
        "status": "cancelled",
    }


async def _external_api_capabilities(
    auth_context: Dict[str, Any],
) -> Dict[str, Any]:
    project_id = str(auth_context.get("project_id") or "")
    if not project_id or database.get_project(project_id) is None:
        raise ExternalAgentApiError(
            "EXTERNAL_API_PROJECT_NOT_FOUND",
            "找不到此 API Key 綁定的 Project。",
            status_code=404,
            recoverable=False,
        )
    try:
        await _prepare_project_tools(project_id)
        definitions = tool_registry.for_project(project_id)
    except Exception:
        definitions = ()
    policy = integration_center_service.get_policy(project_id)
    grants = policy.get("grants") if isinstance(policy, dict) else []
    tools: List[str] = []
    for definition in definitions:
        extension_id = str(definition.extension_id or "")
        allowed = True
        if extension_id.startswith("connector."):
            integration_id = extension_id.split(".", 1)[1]
            capability = _INTEGRATION_TOOL_CAPABILITIES.get(definition.name, "")
            allowed = any(
                grant.get("integration_id") == integration_id
                and capability in (grant.get("capabilities") or [])
                for grant in grants or []
                if isinstance(grant, dict)
            )
        elif extension_id.startswith("mcp."):
            allowed = any(
                grant.get("integration_id") == "mcp"
                and "tool.invoke" in (grant.get("capabilities") or [])
                and grant.get("connection_id") in {None, "", extension_id}
                for grant in grants or []
                if isinstance(grant, dict)
            )
        if allowed:
            tools.append(str(definition.name)[:255])
    settings = load_settings()
    models: List[str] = []
    for item in model_inventory():
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        try:
            profile = model_profile_for_model(settings, name, project_id=project_id)
            provider_for_model(settings, name, project_id=project_id)
        except (PermissionError, ValueError):
            continue
        if profile.eligible_for_primary:
            models.append(name[:255])
    return {
        "project_id": project_id,
        "chat": True,
        "streaming": False,
        "tools": sorted(set(tools))[:256],
        "models": sorted(set(models))[:256],
    }


external_agent_api_router = build_external_agent_api_router(
    service=external_agent_api_service,
    require_local=require_local_workbench,
    error_payload=error_payload,
    submit_run=_external_api_submit_run,
    get_run=_external_api_get_run,
    cancel_run=_external_api_cancel_run,
    capabilities=_external_api_capabilities,
)
app.include_router(external_agent_api_router)
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
