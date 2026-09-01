"""Unified, fail-closed integration catalog and Project policy service."""

from __future__ import annotations

import copy
import inspect
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from integration_center_store import (
    IntegrationCenterStore,
    IntegrationCenterStoreError,
    IntegrationPolicyConflict,
    normalize_policy,
    normalize_project_id,
)


_SECRET_FIELD = re.compile(
    r"(?i)(authorization|cookie|password|secret|token|api[_-]?key|private[_-]?key|verifier|credential|hash)"
)
_SECRET_VALUE = re.compile(
    r"(?i)(bearer\s+\S+|wb_(?:live|test)_[A-Za-z0-9_-]+|"
    r"wbk_[a-f0-9]{12}_[A-Za-z0-9_-]{43}|nvapi-[A-Za-z0-9_-]+)"
)


def _capability(identifier: str, label: str, risk: str) -> dict[str, str]:
    return {"id": identifier, "label": label, "risk": risk}


INTEGRATION_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "gmail",
        "name": "Gmail",
        "kind": "oauth_connector",
        "description": "連接 Gmail 帳號後搜尋與閱讀郵件，並治理草稿建立與寄送。",
        "capabilities": [
            _capability("message.read", "搜尋與讀取郵件", "external_read"),
            _capability("draft.create", "建立郵件草稿", "external_write"),
            _capability("draft.send", "寄送郵件草稿", "irreversible"),
        ],
        "resource_types": ["mailbox"],
        "requires_connection": True,
        "resource_scope_required_for_open": True,
    },
    {
        "id": "github",
        "name": "GitHub",
        "kind": "oauth_connector",
        "description": "讀取專案允許的 Repository、Issue、PR 與檢查結果，並治理寫入操作。",
        "capabilities": [
            _capability("repository.read", "讀取 Repository", "external_read"),
            _capability("issue.read", "讀取 Issue", "external_read"),
            _capability("issue.write", "建立或更新 Issue", "external_write"),
            _capability("pull_request.read", "讀取 Pull Request", "external_read"),
            _capability("pull_request.comment", "新增討論留言", "external_write"),
            _capability("discussion.comment", "新增 Issue 或 PR 討論留言", "external_write"),
            _capability("checks.read", "讀取檢查結果", "external_read"),
        ],
        "resource_types": ["repository"],
        "requires_connection": True,
        "resource_scope_required_for_open": True,
    },
    {
        "id": "notion",
        "name": "Notion",
        "kind": "oauth_connector",
        "description": "搜尋及讀取專案允許的頁面與資料庫，並治理內容新增與更新。",
        "capabilities": [
            _capability("content.read", "讀取內容", "external_read"),
            _capability("content.insert", "新增內容", "external_write"),
            _capability("content.update", "更新內容", "external_write"),
        ],
        "resource_types": ["page", "database"],
        "requires_connection": True,
        "resource_scope_required_for_open": True,
    },
    {
        "id": "n8n",
        "name": "n8n",
        "kind": "managed_runtime",
        "description": "執行 Workbench 驗證過的本機工作流程與 Agent 任務。",
        "capabilities": [
            _capability("workflow.read", "查看工作流程", "local_read"),
            _capability("workflow.execute", "執行工作流程", "external_write"),
            _capability("agent.task.submit", "提交 Agent 任務", "external_write"),
        ],
        "resource_types": ["workflow"],
        "requires_connection": False,
        "resource_scope_required_for_open": False,
        "resource_scope_source": "managed_workflow_allowlist",
    },
    {
        "id": "mcp",
        "name": "本機 MCP",
        "kind": "local_process",
        "description": "呼叫已安裝、已信任且通過路徑驗證的本機 stdio MCP 工具。",
        "capabilities": [
            _capability("tool.invoke", "呼叫 MCP 工具", "tool_policy"),
        ],
        "resource_types": ["tool"],
        "requires_connection": True,
        "resource_scope_required_for_open": False,
        "resource_scope_source": "attested_mcp_tool_policy",
    },
    {
        "id": "external_api",
        "name": "Workbench 對外 Agent API",
        "kind": "inbound_api",
        "description": "讓持有本機簽發金鑰的外部系統在指定 Project 內建立及管理 Agent Run。",
        "capabilities": [
            _capability("run.create", "建立 Agent Run", "local_control"),
            _capability("run.read", "讀取 Run 狀態與結果", "external_read"),
            _capability("run.cancel", "取消 Run", "local_control"),
            _capability("capabilities.read", "讀取可用能力", "external_read"),
        ],
        "resource_types": [],
        "requires_connection": False,
        "resource_scope_required_for_open": False,
    },
)

_CATALOG = {item["id"]: item for item in INTEGRATION_CATALOG}


class IntegrationCenterError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        recoverable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.recoverable = recoverable


class ApplyReceipt(Protocol):
    def rollback(self) -> Any: ...


AuthoritativeApplier = Callable[[str, Mapping[str, Any], Mapping[str, Any]], Any]


@dataclass(frozen=True)
class PermissionDecision:
    decision: str
    permission_mode: str
    reason: str
    policy_revision: int
    integration_id: str
    capability: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "permission_mode": self.permission_mode,
            "reason": self.reason,
            "policy_revision": self.policy_revision,
            "integration_id": self.integration_id,
            "capability": self.capability,
        }


def _safe_summary(value: Any, *, depth: int = 0) -> Any:
    """Bound provider output and remove credential-shaped fields and values."""

    if depth > 5:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:100]:
            safe_key = str(key)[:128]
            if _SECRET_FIELD.search(safe_key):
                continue
            result[safe_key] = _safe_summary(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_summary(item, depth=depth + 1) for item in list(value)[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)[:2000]
    return _SECRET_VALUE.sub("[REDACTED]", text)


class IntegrationCenterService:
    """Aggregate authoritative services and enforce an additional Project gate.

    ``authoritative_applier`` is mandatory for policy mutation.  It must apply
    the policy to the existing Extension/Connector/n8n/MCP gates and may return
    a callable or an object exposing ``rollback()`` for compensation if local
    policy persistence later fails.
    """

    def __init__(
        self,
        *,
        store: Optional[IntegrationCenterStore] = None,
        project_exists: Optional[Callable[[str], bool]] = None,
        authoritative_applier: Optional[AuthoritativeApplier] = None,
        extension_catalog_provider: Optional[Callable[[str], Mapping[str, Any]]] = None,
        connector_service: Any = None,
        n8n_status_provider: Optional[Callable[[], Mapping[str, Any]]] = None,
        gmail_profile_provider: Optional[Callable[[], Mapping[str, Any]]] = None,
        mcp_status_provider: Optional[Callable[[], Mapping[str, Any]]] = None,
        external_api_summary_provider: Optional[Callable[..., Mapping[str, Any]]] = None,
        audit_providers: Optional[
            Sequence[Callable[[str, int], Sequence[Mapping[str, Any]]]]
        ] = None,
    ) -> None:
        self.store = store or IntegrationCenterStore()
        self.project_exists = project_exists or (lambda _project_id: True)
        self.authoritative_applier = authoritative_applier
        self.extension_catalog_provider = extension_catalog_provider
        self.connector_service = connector_service
        self.n8n_status_provider = n8n_status_provider
        self.gmail_profile_provider = gmail_profile_provider
        self.mcp_status_provider = mcp_status_provider
        self.external_api_summary_provider = external_api_summary_provider
        self.audit_providers = tuple(audit_providers or ())
        self._write_lock = threading.RLock()

    def initialize(self) -> None:
        self.store.ensure_schema()
        self.store.block_interrupted_applies()

    def _project(self, project_id: str) -> str:
        project = normalize_project_id(project_id)
        try:
            exists = bool(self.project_exists(project))
        except Exception as exc:
            raise IntegrationCenterError(
                "PROJECT_LOOKUP_FAILED",
                "無法確認指定專案。",
                status_code=503,
                recoverable=True,
            ) from exc
        if not exists:
            raise IntegrationCenterError("PROJECT_NOT_FOUND", "找不到指定專案。", status_code=404)
        return project

    @staticmethod
    def catalog() -> list[dict[str, Any]]:
        return copy.deepcopy(list(INTEGRATION_CATALOG))

    def get_policy(self, project_id: str) -> dict[str, Any]:
        return self.store.get_policy(self._project(project_id))

    def _connector_snapshot(self, project_id: str) -> dict[str, list[dict[str, Any]]]:
        snapshot = {"github": [], "notion": [], "gmail": []}
        if self.connector_service is None:
            return snapshot
        try:
            rows = self.connector_service.list_connections(project_id=project_id)
        except Exception:
            return snapshot
        for raw in rows or []:
            if not isinstance(raw, Mapping):
                continue
            connector_id = str(raw.get("connector_id") or "").casefold()
            if connector_id not in snapshot:
                continue
            binding = raw.get("binding") if isinstance(raw.get("binding"), Mapping) else None
            resources: list[dict[str, str]] = []
            if binding is not None:
                try:
                    bound = self.connector_service.get_bound_resources(
                        project_id=project_id,
                        connection_id=str(raw.get("connection_id") or ""),
                    )
                    for resource in bound.get("resources") or []:
                        if isinstance(resource, Mapping):
                            resources.append(
                                {
                                    "resource_type": str(resource.get("resource_type") or "")[:64],
                                    "resource_id": str(resource.get("resource_id") or "")[:1024],
                                    "display_label": str(resource.get("display_label") or resource.get("resource_id") or "")[:1024],
                                }
                            )
                except Exception:
                    resources = []
            snapshot[connector_id].append(
                {
                    "connection_id": str(raw.get("connection_id") or ""),
                    "status": str(raw.get("status") or "unknown"),
                    "display_name": str(raw.get("display_name") or "")[:512],
                    "workspace_id": str(raw.get("workspace_id") or "")[:512] or None,
                    "granted_permissions": [str(item)[:128] for item in (raw.get("granted_permissions") or [])[:100]],
                    "binding": {
                        "enabled": bool(binding.get("enabled")) if binding else False,
                        "mode": str(binding.get("mode") or "") if binding else None,
                        "revision": int(binding.get("revision") or 0) if binding else 0,
                    },
                    "resources": resources,
                    "validated_at": raw.get("validated_at"),
                    "error_code": str(raw.get("error_code") or "")[:128] or None,
                }
            )
        return snapshot

    def _extension_snapshot(self, project_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if self.extension_catalog_provider is None:
            return {}, []
        try:
            payload = self.extension_catalog_provider(project_id)
            rows = payload.get("extensions") if isinstance(payload, Mapping) else []
        except Exception:
            return {}, []
        indexed: dict[str, Any] = {}
        mcp: list[dict[str, Any]] = []
        for raw in rows or []:
            if not isinstance(raw, Mapping):
                continue
            extension_id = str(raw.get("id") or "").casefold()
            entrypoint = raw.get("entrypoint") if isinstance(raw.get("entrypoint"), Mapping) else {}
            item = {
                "extension_id": extension_id,
                "installed": bool(raw.get("installed")),
                "trusted": bool(raw.get("trusted")),
                "enabled": bool(raw.get("effective_enabled")),
                "permission": copy.deepcopy(raw.get("project_permission")),
                "health": _safe_summary(raw.get("health") or {}),
                "manifest_sha256": str(raw.get("manifest_sha256") or "")[:64] or None,
            }
            indexed[extension_id] = item
            if str(entrypoint.get("type") or "") == "mcp_settings":
                mcp.append(item)
        return indexed, mcp

    @staticmethod
    def _provider_call(provider: Optional[Callable[..., Mapping[str, Any]]], *args: Any) -> tuple[dict[str, Any], Optional[str]]:
        if provider is None:
            return {}, "provider_not_configured"
        try:
            value = provider(*args)
            if inspect.isawaitable(value):
                return {}, "async_provider_not_supported"
            return dict(_safe_summary(value if isinstance(value, Mapping) else {})), None
        except Exception:
            return {}, "provider_unavailable"

    def _validate_policy(self, project_id: str, policy: Mapping[str, Any]) -> dict[str, Any]:
        normalized = normalize_policy(policy, project_id=project_id)
        mode = normalized["permission_mode"]
        connector_snapshot = self._connector_snapshot(project_id)
        _, mcp_extensions = self._extension_snapshot(project_id)
        known_mcp = {str(item.get("extension_id") or "") for item in mcp_extensions}

        for grant in normalized["grants"]:
            integration_id = grant["integration_id"]
            definition = _CATALOG.get(integration_id)
            if definition is None:
                raise IntegrationCenterError(
                    "INTEGRATION_POLICY_UNKNOWN_INTEGRATION",
                    f"不支援的整合項目：{integration_id}",
                    status_code=422,
                )
            allowed_capabilities = {item["id"] for item in definition["capabilities"]}
            unexpected = set(grant["capabilities"]) - allowed_capabilities
            if unexpected:
                raise IntegrationCenterError(
                    "INTEGRATION_POLICY_UNKNOWN_CAPABILITY",
                    f"{definition['name']} 包含不支援的能力。",
                    status_code=422,
                )
            if mode != "blocked" and not grant["capabilities"]:
                raise IntegrationCenterError(
                    "INTEGRATION_POLICY_CAPABILITY_REQUIRED",
                    f"{definition['name']} 必須明確選擇能力。",
                    status_code=422,
                )
            if definition["requires_connection"] and mode != "blocked" and not grant.get("connection_id"):
                raise IntegrationCenterError(
                    "INTEGRATION_POLICY_CONNECTION_REQUIRED",
                    f"{definition['name']} 必須綁定明確連線。",
                    status_code=422,
                )
            allowed_resource_types = set(definition["resource_types"])
            if any(item["resource_type"] not in allowed_resource_types for item in grant["resources"]):
                raise IntegrationCenterError(
                    "INTEGRATION_POLICY_RESOURCE_TYPE_INVALID",
                    f"{definition['name']} 包含不支援的資源類型。",
                    status_code=422,
                )
            if mode == "open" and definition["resource_scope_required_for_open"] and not grant["resources"]:
                raise IntegrationCenterError(
                    "INTEGRATION_POLICY_OPEN_SCOPE_REQUIRED",
                    f"完全開放 {definition['name']} 前必須明確選擇資源。",
                    status_code=422,
                )

            if integration_id in {"github", "notion", "gmail"} and mode != "blocked":
                connections = connector_snapshot[integration_id]
                selected = next(
                    (item for item in connections if item["connection_id"] == grant.get("connection_id")),
                    None,
                )
                if selected is None or not selected.get("binding"):
                    raise IntegrationCenterError(
                        "INTEGRATION_POLICY_CONNECTION_NOT_BOUND",
                        f"{definition['name']} 連線尚未綁定這個專案。",
                        status_code=409,
                    )
                allowed_resources = {
                    (item["resource_type"], item["resource_id"])
                    for item in selected.get("resources") or []
                }
                if any(
                    (item["resource_type"], item["resource_id"]) not in allowed_resources
                    for item in grant["resources"]
                ):
                    raise IntegrationCenterError(
                        "INTEGRATION_POLICY_RESOURCE_NOT_BOUND",
                        f"{definition['name']} 的政策資源未綁定這個專案。",
                        status_code=409,
                    )
            if integration_id == "mcp" and mode != "blocked":
                if grant.get("connection_id") not in known_mcp:
                    raise IntegrationCenterError(
                        "INTEGRATION_POLICY_MCP_NOT_CONFIGURED",
                        "指定的本機 MCP 尚未安裝及信任。",
                        status_code=409,
                    )
        return normalized

    @staticmethod
    def _rollback(receipt: Any) -> bool:
        if receipt is None:
            return True
        try:
            if callable(receipt):
                result = receipt()
            elif callable(getattr(receipt, "rollback", None)):
                result = receipt.rollback()
            else:
                return False
            if inspect.isawaitable(result):
                raise RuntimeError("async policy rollback is not supported")
            return True
        except Exception:
            return False

    def put_policy(
        self,
        project_id: str,
        *,
        expected_revision: int,
        policy: Mapping[str, Any],
        acknowledge_open_risk: bool = False,
        actor: str = "local_session",
        audit_action: str = "policy.replace",
    ) -> dict[str, Any]:
        project = self._project(project_id)
        with self._write_lock:
            current = self.store.get_policy(project)
            if int(current["revision"]) != int(expected_revision):
                raise IntegrationPolicyConflict()
            proposed = self._validate_policy(project, policy)
            if proposed["permission_mode"] == "open" and acknowledge_open_risk is not True:
                raise IntegrationCenterError(
                    "INTEGRATION_POLICY_OPEN_ACKNOWLEDGEMENT_REQUIRED",
                    "完全開放前必須明確確認風險與影響。",
                    status_code=422,
                )
            proposed["revision"] = int(expected_revision) + 1
            if self.authoritative_applier is None:
                self.store.audit_failure(
                    project_id=project,
                    action="policy.apply",
                    actor=actor,
                    error_code="INTEGRATION_POLICY_APPLIER_UNAVAILABLE",
                    policy_revision=int(expected_revision),
                )
                raise IntegrationCenterError(
                    "INTEGRATION_POLICY_APPLIER_UNAVAILABLE",
                    "整合權限閘門尚未完成接線，政策未套用。",
                    status_code=503,
                    recoverable=True,
                )
            self.store.begin_apply(project, expected_revision=int(expected_revision))
            receipt: Any = None
            try:
                receipt = self.authoritative_applier(project, copy.deepcopy(current), copy.deepcopy(proposed))
                if inspect.isawaitable(receipt):
                    raise RuntimeError("async policy appliers are not supported")
            except Exception as exc:
                compensation_incomplete = bool(
                    getattr(exc, "compensation_incomplete", False)
                )
                self.store.finish_apply_failure(
                    project,
                    active_revision=int(expected_revision),
                    compensated=not compensation_incomplete,
                    error_code=(
                        "INTEGRATION_POLICY_COMPENSATION_FAILED"
                        if compensation_incomplete
                        else "INTEGRATION_POLICY_APPLY_FAILED"
                    ),
                )
                self.store.audit_failure(
                    project_id=project,
                    action="policy.apply",
                    actor=actor,
                    error_code=(
                        "INTEGRATION_POLICY_COMPENSATION_FAILED"
                        if compensation_incomplete
                        else "INTEGRATION_POLICY_APPLY_FAILED"
                    ),
                    policy_revision=int(expected_revision),
                )
                if compensation_incomplete:
                    raise IntegrationCenterError(
                        "INTEGRATION_POLICY_COMPENSATION_FAILED",
                        "整合權限回復未完成；此 Project 的整合功能已全面封鎖。",
                        status_code=503,
                        recoverable=True,
                    ) from exc
                raise IntegrationCenterError(
                    "INTEGRATION_POLICY_APPLY_FAILED",
                    "無法安全套用整合權限；原政策仍然有效。",
                    status_code=409,
                    recoverable=True,
                ) from exc
            try:
                return self.store.replace_policy(
                    project_id=project,
                    expected_revision=int(expected_revision),
                    policy=proposed,
                    actor=actor,
                    audit_action=audit_action,
                )
            except Exception as exc:
                compensated = self._rollback(receipt)
                self.store.finish_apply_failure(
                    project,
                    active_revision=int(expected_revision),
                    compensated=compensated,
                    error_code=(
                        "INTEGRATION_POLICY_PERSIST_FAILED"
                        if compensated
                        else "INTEGRATION_POLICY_COMPENSATION_FAILED"
                    ),
                )
                self.store.audit_failure(
                    project_id=project,
                    action="policy.persist",
                    actor=actor,
                    error_code=(
                        "INTEGRATION_POLICY_PERSIST_FAILED"
                        if compensated
                        else "INTEGRATION_POLICY_COMPENSATION_FAILED"
                    ),
                    policy_revision=int(expected_revision),
                )
                if not compensated:
                    raise IntegrationCenterError(
                        "INTEGRATION_POLICY_COMPENSATION_FAILED",
                        "整合權限回復未完成；此 Project 的整合功能已全面封鎖。",
                        status_code=503,
                        recoverable=True,
                    ) from exc
                raise

    @staticmethod
    def _extension_can_import(item: Mapping[str, Any]) -> bool:
        permission = item.get("permission")
        permission_level = (
            str(permission.get("level") or "restricted")
            if isinstance(permission, Mapping)
            else "restricted"
        )
        health = item.get("health") if isinstance(item.get("health"), Mapping) else {}
        health_status = str(health.get("status") or "unknown").casefold()
        return bool(
            item.get("installed")
            and item.get("trusted")
            and item.get("enabled")
            and permission_level != "blocked"
            and health_status in {"ready", "healthy", "ok"}
        )

    def import_existing_project_policy(
        self,
        project_id: str,
        *,
        actor: str = "migration_existing_integration_state",
    ) -> dict[str, Any]:
        """Import only healthy, already-enabled authority into a restricted policy.

        This one-shot migration never widens an existing policy and never turns
        an uncertain integration into an allowed grant.  Connector resources
        are copied as identifiers only; credentials remain in their own stores.
        """

        project = self._project(project_id)
        with self._write_lock:
            current = self.store.get_policy(project)
            if int(current["revision"]) != 0:
                return {"migrated": False, "reason": "policy_exists", "policy": current}
            connector_snapshot = self._connector_snapshot(project)
            extensions, mcp_extensions = self._extension_snapshot(project)
            grants: list[dict[str, Any]] = []

            for integration_id, extension_id in (
                ("github", "connector.github"),
                ("notion", "connector.notion"),
                ("gmail", "connector.gmail"),
            ):
                extension = extensions.get(extension_id, {})
                if not self._extension_can_import(extension):
                    continue
                definition = _CATALOG[integration_id]
                read_capabilities = [
                    item["id"]
                    for item in definition["capabilities"]
                    if item["risk"] == "external_read"
                ]
                all_capabilities = [item["id"] for item in definition["capabilities"]]
                for connection in connector_snapshot[integration_id]:
                    binding = connection.get("binding") or {}
                    if connection.get("status") != "connected" or not binding.get("enabled"):
                        continue
                    resources = [
                        {
                            "resource_type": item["resource_type"],
                            "resource_id": item["resource_id"],
                        }
                        for item in connection.get("resources") or []
                        if item.get("resource_type") and item.get("resource_id")
                    ]
                    if not resources:
                        continue
                    grants.append(
                        {
                            "integration_id": integration_id,
                            "connection_id": connection["connection_id"],
                            "capabilities": (
                                all_capabilities
                                if binding.get("mode") == "read_write"
                                else read_capabilities
                            ),
                            "resources": resources,
                        }
                    )

            n8n_status, n8n_error = self._provider_call(self.n8n_status_provider)
            n8n_extension = extensions.get("builtin.n8n", {})
            n8n_healthy = (
                n8n_error is None
                and str(n8n_status.get("status") or n8n_status.get("state") or "").casefold()
                in {"ready", "running", "healthy"}
            )
            if self._extension_can_import(n8n_extension) and n8n_healthy:
                grants.append(
                    {
                        "integration_id": "n8n",
                        "connection_id": None,
                        "capabilities": [item["id"] for item in _CATALOG["n8n"]["capabilities"]],
                        "resources": [],
                    }
                )
            mcp_status, mcp_error = self._provider_call(self.mcp_status_provider)
            mcp_live = mcp_error is None and str(mcp_status.get("status") or "").casefold() == "healthy"
            if mcp_live:
                for item in mcp_extensions:
                    if not self._extension_can_import(item):
                        continue
                    grants.append(
                        {
                            "integration_id": "mcp",
                            "connection_id": item["extension_id"],
                            "capabilities": ["tool.invoke"],
                            "resources": [],
                        }
                    )

            # The inbound Agent API is new authority, not legacy authority.
            # Merely creating a key must never become implicit Project consent
            # after a restart; the user must explicitly add it to the unified
            # policy before that key can invoke anything.

            if not grants:
                return {
                    "migrated": False,
                    "reason": "no_healthy_enabled_integrations",
                    "policy": current,
                }
            saved = self.put_policy(
                project,
                expected_revision=0,
                policy={
                    "name": "既有整合權限（安全匯入）",
                    "permission_mode": "restricted",
                    "grants": grants,
                },
                actor=actor,
                audit_action="migration_existing_integration_state",
            )
            return {"migrated": True, "reason": "imported", "policy": saved}

    def permission_decision(
        self,
        *,
        project_id: str,
        integration_id: str,
        capability: str,
        connection_id: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        integration = str(integration_id or "").strip().casefold()
        requested_capability = str(capability or "").strip().casefold()
        policy = self.store.get_policy(project)
        apply_state = self.store.get_apply_state(project)
        mode = str(policy["permission_mode"])
        if (
            apply_state["status"] != "active"
            or int(apply_state["active_revision"]) != int(policy["revision"])
        ):
            return PermissionDecision(
                "deny",
                "blocked",
                "policy_apply_not_active",
                int(policy["revision"]),
                integration,
                requested_capability,
            ).as_dict()
        if mode == "blocked":
            return PermissionDecision("deny", mode, "project_policy_blocked", int(policy["revision"]), integration, requested_capability).as_dict()
        definition = _CATALOG.get(integration)
        if definition is None:
            return PermissionDecision("deny", mode, "integration_not_allowed", int(policy["revision"]), integration, requested_capability).as_dict()
        matched: Optional[Mapping[str, Any]] = None
        for grant in policy["grants"]:
            if grant["integration_id"] != integration:
                continue
            bound_connection = grant.get("connection_id")
            if bound_connection and bound_connection != connection_id:
                continue
            if requested_capability not in grant["capabilities"]:
                continue
            scopes = {
                (item["resource_type"], item["resource_id"])
                for item in grant["resources"]
            }
            if resource_id is not None:
                requested_resource = (str(resource_type or "").casefold(), str(resource_id))
                if scopes and requested_resource not in scopes:
                    continue
                if not scopes and not definition.get("resource_scope_source"):
                    continue
            matched = grant
            break
        if matched is None:
            return PermissionDecision("deny", mode, "scope_not_granted", int(policy["revision"]), integration, requested_capability).as_dict()
        if mode == "open":
            return PermissionDecision("allow", mode, "explicit_project_scope", int(policy["revision"]), integration, requested_capability).as_dict()
        risk = next(
            (item["risk"] for item in definition["capabilities"] if item["id"] == requested_capability),
            "tool_policy",
        )
        decision = "allow" if risk in {"local_read", "external_read", "local_control"} else "require_approval"
        reason = "restricted_read_allowed" if decision == "allow" else "restricted_operation_requires_approval"
        return PermissionDecision(decision, mode, reason, int(policy["revision"]), integration, requested_capability).as_dict()

    def external_api_policy_guard(self, project_id: str, required_scope: str) -> bool:
        """Adapter for ``ExternalAgentApiService.policy_guard``.

        The inbound API has no interactive approval channel, so only decisions
        already allowed by the Project policy may pass.  Tool side effects
        created by a Run remain governed separately by ToolDispatcher.
        """

        capability = {
            "runs:create": "run.create",
            "runs:read": "run.read",
            "runs:cancel": "run.cancel",
            "capabilities:read": "capabilities.read",
        }.get(str(required_scope or ""))
        if capability is None:
            return False
        decision = self.permission_decision(
            project_id=project_id,
            integration_id="external_api",
            capability=capability,
        )
        if decision["reason"] == "policy_apply_not_active":
            raise IntegrationCenterError(
                "INTEGRATION_POLICY_UNAVAILABLE",
                "此 Project 的整合權限目前處於安全封鎖狀態。",
                status_code=503,
                recoverable=True,
            )
        return decision["decision"] == "allow"

    def overview(self, project_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        policy = self.store.get_policy(project)
        apply_state = self.store.get_apply_state(project)
        connector_snapshot = self._connector_snapshot(project)
        extensions, mcp_extensions = self._extension_snapshot(project)
        n8n, n8n_error = self._provider_call(self.n8n_status_provider)
        mcp, mcp_error = self._provider_call(self.mcp_status_provider)
        external_api, external_api_error = self._provider_call(self.external_api_summary_provider, project)

        extension_map = {
            "n8n": extensions.get("builtin.n8n", {}),
            "gmail": extensions.get("connector.gmail", {}),
            "github": extensions.get("connector.github", {}),
            "notion": extensions.get("connector.notion", {}),
        }
        grants_by_id: dict[str, list[dict[str, Any]]] = {}
        for grant in policy["grants"]:
            grants_by_id.setdefault(grant["integration_id"], []).append(copy.deepcopy(grant))

        integrations: list[dict[str, Any]] = []
        for definition in INTEGRATION_CATALOG:
            integration_id = definition["id"]
            state: dict[str, Any] = {"status": "not_configured", "healthy": False}
            provider_error: Optional[str] = None
            if integration_id in {"github", "notion", "gmail"}:
                connections = connector_snapshot[integration_id]
                state = {
                    "status": "connected" if any(item["status"] == "connected" for item in connections) else "not_connected",
                    "healthy": any(item["status"] == "connected" for item in connections),
                    "connections": connections,
                    "extension": extension_map[integration_id],
                }
                if len(connections) == 1:
                    state["connection_id"] = connections[0]["connection_id"]
                    state["resources"] = copy.deepcopy(connections[0].get("resources") or [])
            elif integration_id == "n8n":
                state = {**n8n, "extension": extension_map["n8n"]}
                state.setdefault("healthy", str(state.get("state") or state.get("status")) in {"ready", "running", "healthy"})
                provider_error = n8n_error
            elif integration_id == "mcp":
                state = {**mcp, "configured_extensions": mcp_extensions}
                state.setdefault("healthy", str(state.get("status")) == "healthy")
                provider_error = mcp_error
            elif integration_id == "external_api":
                state = external_api
                state.setdefault("status", "ready" if state.get("enabled") else "not_configured")
                state.setdefault("healthy", bool(state.get("enabled")))
                provider_error = external_api_error
            if provider_error:
                state["provider_error"] = provider_error
            integrations.append(
                {
                    **copy.deepcopy(definition),
                    "state": _safe_summary(state),
                    "policy": {
                        "permission_mode": policy["permission_mode"],
                        "grants": grants_by_id.get(integration_id, []),
                        "revision": policy["revision"],
                    },
                }
            )
        return {
            "success": True,
            "catalog_version": 1,
            "project_id": project,
            "integrations": integrations,
            "policy": policy,
            "apply_state": apply_state,
            "summary": {
                "total": len(integrations),
                "configured": sum(bool(item["state"].get("configured") or item["state"].get("connections") or item["state"].get("enabled")) for item in integrations),
                "healthy": sum(bool(item["state"].get("healthy")) for item in integrations),
                "permission_mode": policy["permission_mode"],
                "policy_revision": policy["revision"],
            },
        }

    def audits(self, project_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        project = self._project(project_id)
        bounded = max(1, min(int(limit), 500))
        combined: list[dict[str, Any]] = [
            {**item, "source": "integration_policy"}
            for item in self.store.list_audits(project, limit=bounded)
        ]
        for provider in self.audit_providers:
            try:
                rows = provider(project, bounded)
            except Exception:
                # Audit aggregation is observational. One unavailable source
                # must not hide the authoritative policy log or break the UI.
                continue
            for raw in list(rows or ())[:bounded]:
                if not isinstance(raw, Mapping):
                    continue
                row_project = str(raw.get("project_id") or "")
                if row_project and row_project != project:
                    continue
                safe = _safe_summary(raw)
                if not isinstance(safe, dict):
                    continue
                safe.setdefault("project_id", project)
                safe.setdefault("source", "integration_runtime")
                combined.append(safe)
        combined.sort(
            key=lambda item: (
                str(item.get("created_at") or ""),
                str(item.get("audit_id") or ""),
            ),
            reverse=True,
        )
        return combined[:bounded]


__all__ = [
    "AuthoritativeApplier",
    "INTEGRATION_CATALOG",
    "IntegrationCenterError",
    "IntegrationCenterService",
    "PermissionDecision",
]
