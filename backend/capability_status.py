"""Project-scoped, read-only Workbench capability diagnostics.

This module deliberately composes public snapshots from the existing
Extension, Integration Center and Model Governance services.  It never reads
secret stores and never mutates runtime state.  The same service is used by
the model-facing tools and by the host-side status-question preflight so the
two paths cannot disagree about whether a capability is available.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

try:
    from tool_runtime import ToolAccess, ToolDefinition
except ImportError:  # pragma: no cover - package import compatibility
    from backend.tool_runtime import ToolAccess, ToolDefinition


CAPABILITY_STATUS_EXTENSION_ID = "builtin.capability-status"
CAPABILITY_STATUS_MANIFEST_SHA256 = hashlib.sha256(
    b"local-ai-workbench:capability-status:v1"
).hexdigest()

_SECRET_FIELD = re.compile(
    r"(?i)(authorization|cookie|password|secret|token|api[_-]?key|private[_-]?key|"
    r"verifier|credential|client[_-]?secret|access[_-]?token|refresh[_-]?token)"
)
_SECRET_VALUE = re.compile(
    r"(?i)(bearer\s+\S+|nvapi-[A-Za-z0-9_-]+|"
    r"wbk_[A-Za-z0-9_-]+|ya29\.[A-Za-z0-9._-]+)"
)

_ALIASES: dict[str, tuple[str, ...]] = {
    "gmail": ("gmail", "google mail", "郵件", "信箱", "電子郵件"),
    "github": ("github", "repository", "repo", "儲存庫", "程式碼庫"),
    "notion": ("notion", "頁面", "資料庫"),
    "n8n": ("n8n", "工作流程", "自動化"),
    "mcp": ("mcp", "model context protocol", "工具伺服器"),
    "external_api": ("external api", "agent api", "對外 api", "api key", "api 金鑰"),
    "playwright": ("playwright", "browser", "chrome", "瀏覽器"),
    "models": ("model", "provider", "模型", "供應商", "ollama", "nvidia"),
    "extensions": ("extension", "plugin", "外掛", "擴充"),
}

_REASONS: dict[str, str] = {
    "ready": "此能力已通過目前 Project 的安裝、連線、健康與權限檢查。",
    "not_installed": "尚未安裝這項擴充功能。",
    "not_trusted": "擴充內容尚未受信任，或內容變更後需要重新審查。",
    "not_enabled": "擴充功能目前未啟用。",
    "connection_required": "尚未完成帳號或服務連線。",
    "project_binding_required": "連線尚未綁定至目前 Project。",
    "resource_binding_required": "目前 Project 尚未選擇允許存取的資源。",
    "permission_not_granted": "整合中心尚未放行這項能力或資源範圍。",
    "project_policy_blocked": "目前 Project 的整合權限設為不開放。",
    "policy_apply_not_active": "整合政策尚未安全套用，系統已維持封鎖。",
    "unhealthy": "服務健康檢查未通過，暫時不會提供給 Agent。",
    "provider_disabled": "模型供應商已設定，但目前未啟用。",
    "provider_unavailable": "模型供應商目前不可用，請先檢查金鑰、額度或連線。",
    "provider_unverified": "尚無已知阻擋，但這組模型連線尚未完成即時驗證。",
    "auth_required": "API 憑證已失效或被拒絕，系統已停止使用這條模型路由。",
    "permission_denied": "目前憑證沒有使用這個模型或能力的權限。",
    "quota_exhausted": "供應商回報額度或計費不可用，系統已暫停這條路由。",
    "rate_limited": "供應商目前限制請求頻率，系統會等待冷卻時間。",
    "model_unavailable": "指定模型目前無法由這個供應商端點使用。",
    "degraded": "供應商服務目前壅塞或降級。",
    "unreachable": "目前無法連線至模型供應商。",
    "project_required": "此查詢必須在一個 Project 對話中執行。",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: Any, limit: int = 512) -> str:
    return _SECRET_VALUE.sub("[REDACTED]", str(value or ""))[:limit]


def _safe_public(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:100]:
            key = str(raw_key)[:128]
            if _SECRET_FIELD.search(key):
                continue
            result[key] = _safe_public(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_public(item, depth=depth + 1) for item in list(value)[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _bounded_text(value, 2000)


async def _call(provider: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    value = provider(*args, **kwargs)
    if inspect.isawaitable(value):
        value = await value
    return value


class CapabilityStatusError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CapabilityStatusService:
    """Build one bounded status contract from authoritative runtime services."""

    def __init__(
        self,
        *,
        project_exists: Callable[[str], bool],
        integration_overview_provider: Callable[[str], Any],
        extension_catalog_provider: Callable[[str], Any],
        model_overview_provider: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.project_exists = project_exists
        self.integration_overview_provider = integration_overview_provider
        self.extension_catalog_provider = extension_catalog_provider
        self.model_overview_provider = model_overview_provider

    def _project(self, project_id: str) -> str:
        project = str(project_id or "").strip()
        if not project or project.startswith("__independent"):
            raise CapabilityStatusError("PROJECT_REQUIRED", _REASONS["project_required"])
        try:
            exists = bool(self.project_exists(project))
        except Exception as exc:
            raise CapabilityStatusError("PROJECT_LOOKUP_FAILED", "目前無法確認指定 Project。") from exc
        if not exists:
            raise CapabilityStatusError("PROJECT_NOT_FOUND", "找不到指定 Project。")
        return project

    @staticmethod
    def _repair(kind: str, capability_id: str, reason_code: str) -> dict[str, str]:
        if kind == "model_provider":
            connection_reasons = {
                "provider_disabled", "provider_unavailable", "auth_required",
                "permission_denied", "model_unavailable", "unreachable",
            }
            return {
                "workspace": "cloud",
                "section": "connections" if reason_code in connection_reasons else "health",
                "label": "開啟雲端模型連線設定" if reason_code in connection_reasons else "開啟雲端的用量與健康",
            }
        if reason_code in {"not_installed", "not_trusted", "not_enabled"}:
            return {
                "workspace": "extensions",
                "section": "installed",
                "label": "檢查安裝、信任與啟用狀態",
            }
        if capability_id == "external_api":
            return {"workspace": "integrations", "section": "api", "label": "開啟對外 Agent API"}
        if reason_code in {"permission_not_granted", "project_policy_blocked", "policy_apply_not_active", "resource_binding_required"}:
            return {"workspace": "integrations", "section": "policy", "label": "檢查 Project 權限與資源範圍"}
        return {
            "workspace": "integrations",
            "section": "services" if kind == "oauth_connector" or reason_code in {"connection_required", "unhealthy"} else "overview",
            "label": "連線、重新連線或重新驗證" if reason_code in {"connection_required", "unhealthy"} else "開啟統一整合中心",
        }

    @staticmethod
    def _reason(
        *,
        installed: bool,
        trusted: bool,
        enabled: bool,
        connected: bool,
        healthy: bool,
        project_allowed: bool,
        resource_allowed: bool,
        policy_reason: str = "",
    ) -> tuple[str, str]:
        if not installed:
            code = "not_installed"
        elif not trusted:
            code = "not_trusted"
        elif not enabled:
            code = "not_enabled"
        elif not connected:
            code = "connection_required"
        elif policy_reason == "policy_apply_not_active":
            code = "policy_apply_not_active"
        elif policy_reason == "project_policy_blocked":
            code = "project_policy_blocked"
        elif not project_allowed:
            code = "permission_not_granted"
        elif not resource_allowed:
            code = "resource_binding_required"
        elif not healthy:
            code = "unhealthy"
        else:
            code = "ready"
        return code, _REASONS[code]

    async def _snapshots(self, project: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        integration = await _call(self.integration_overview_provider, project)
        extensions = await _call(self.extension_catalog_provider, project)
        models: dict[str, Any] = {}
        if self.model_overview_provider is not None:
            try:
                raw = await _call(self.model_overview_provider, project)
                if isinstance(raw, Mapping):
                    models = dict(raw)
            except Exception:
                models = {"providers": [], "unavailable": True}
        return (
            dict(integration) if isinstance(integration, Mapping) else {},
            dict(extensions) if isinstance(extensions, Mapping) else {},
            models,
        )

    @staticmethod
    def _extension_index(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        return {
            str(item.get("id") or "").casefold(): item
            for item in payload.get("extensions") or []
            if isinstance(item, Mapping) and item.get("id")
        }

    def _integration_item(
        self,
        raw: Mapping[str, Any],
        extension_index: Mapping[str, Mapping[str, Any]],
        apply_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        capability_id = str(raw.get("id") or "").casefold()
        kind = str(raw.get("kind") or "integration")
        state = raw.get("state") if isinstance(raw.get("state"), Mapping) else {}
        extension_id = {
            "gmail": "connector.gmail",
            "github": "connector.github",
            "notion": "connector.notion",
            "n8n": "builtin.n8n",
        }.get(capability_id)
        extension = extension_index.get(extension_id or "", {})
        if not extension and isinstance(state.get("extension"), Mapping):
            extension = state.get("extension") or {}

        requires_connection = bool(raw.get("requires_connection"))
        connections = [item for item in state.get("connections") or [] if isinstance(item, Mapping)]
        connected = (
            any(str(item.get("status") or "").casefold() == "connected" for item in connections)
            if requires_connection and capability_id != "mcp"
            else bool(state.get("enabled") or state.get("configured") or state.get("running") or state.get("healthy"))
        )
        if not requires_connection:
            connected = True

        installed = bool(extension.get("installed")) if extension_id else True
        trusted = bool(extension.get("trusted")) if extension_id else True
        enabled = bool(extension.get("effective_enabled", extension.get("enabled"))) if extension_id else bool(
            state.get("enabled", True)
        )
        if capability_id == "mcp":
            configured = [item for item in state.get("configured_extensions") or [] if isinstance(item, Mapping)]
            installed = any(bool(item.get("installed")) for item in configured)
            trusted = any(bool(item.get("trusted")) for item in configured)
            enabled = any(bool(item.get("enabled")) for item in configured)
            connected = installed and trusted and enabled

        policy = raw.get("policy") if isinstance(raw.get("policy"), Mapping) else {}
        grants = [item for item in policy.get("grants") or [] if isinstance(item, Mapping)]
        permission_mode = str(policy.get("permission_mode") or "blocked")
        apply_status = str(apply_state.get("status") or "")
        policy_reason = ""
        if apply_status != "active":
            policy_reason = "policy_apply_not_active"
        elif permission_mode == "blocked":
            policy_reason = "project_policy_blocked"
        project_allowed = bool(grants) and not policy_reason

        if requires_connection and capability_id != "mcp":
            bound = [
                item
                for item in connections
                if isinstance(item.get("binding"), Mapping)
                and bool((item.get("binding") or {}).get("enabled"))
            ]
            resource_allowed = any(bool(item.get("resources")) for item in bound)
        elif capability_id == "mcp":
            resource_allowed = any(bool(item.get("connection_id")) for item in grants)
        else:
            resource_allowed = project_allowed

        healthy = bool(state.get("healthy"))
        code, reason = self._reason(
            installed=installed,
            trusted=trusted,
            enabled=enabled,
            connected=connected,
            healthy=healthy,
            project_allowed=project_allowed,
            resource_allowed=resource_allowed,
            policy_reason=policy_reason,
        )
        return {
            "id": capability_id,
            "name": _bounded_text(raw.get("name") or capability_id, 120),
            "kind": kind,
            "description": _bounded_text(raw.get("description"), 500),
            "installed": installed,
            "trusted": trusted,
            "enabled": enabled,
            "connected": connected,
            "healthy": healthy,
            "project_allowed": project_allowed,
            "resource_allowed": resource_allowed,
            "available": code == "ready",
            "permission": {
                "mode": permission_mode,
                "revision": int(policy.get("revision") or 0),
                "granted_capabilities": sorted(
                    {
                        str(value)
                        for grant in grants
                        for value in (grant.get("capabilities") or [])
                    }
                )[:100],
            },
            "capabilities": [
                {
                    "id": _bounded_text(item.get("id"), 128),
                    "label": _bounded_text(item.get("label"), 160),
                    "risk": _bounded_text(item.get("risk"), 64),
                }
                for item in raw.get("capabilities") or []
                if isinstance(item, Mapping)
            ][:50],
            "reason_code": code,
            "reason": reason,
            "repair": self._repair(kind, capability_id, code),
            "last_checked_at": _bounded_text(
                state.get("validated_at") or state.get("checked_at") or _now_iso(), 80
            ),
        }

    def _extension_item(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        capability_id = str(raw.get("id") or "").casefold()
        health = raw.get("health") if isinstance(raw.get("health"), Mapping) else {}
        permission = raw.get("project_permission") if isinstance(raw.get("project_permission"), Mapping) else {}
        installed = bool(raw.get("installed"))
        trusted = bool(raw.get("trusted"))
        enabled = bool(raw.get("effective_enabled"))
        permission_mode = str(permission.get("level") or "restricted")
        project_allowed = permission_mode != "blocked"
        healthy = str(health.get("status") or "unknown").casefold() in {"ready", "healthy", "ok"}
        code, reason = self._reason(
            installed=installed,
            trusted=trusted,
            enabled=enabled,
            connected=True,
            healthy=healthy,
            project_allowed=project_allowed,
            resource_allowed=True,
            policy_reason="project_policy_blocked" if not project_allowed else "",
        )
        return {
            "id": capability_id,
            "name": _bounded_text(raw.get("name") or capability_id, 120),
            "kind": "extension",
            "description": _bounded_text(raw.get("description"), 500),
            "installed": installed,
            "trusted": trusted,
            "enabled": enabled,
            "connected": True,
            "healthy": healthy,
            "project_allowed": project_allowed,
            "resource_allowed": True,
            "available": code == "ready",
            "permission": {"mode": permission_mode},
            "capabilities": [],
            "reason_code": code,
            "reason": reason,
            "repair": self._repair("extension", capability_id, code),
            "last_checked_at": _bounded_text(health.get("checked_at") or _now_iso(), 80),
        }

    def _model_item(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        provider_id = str(raw.get("provider_id") or "").casefold()
        operational = raw.get("operational") if isinstance(raw.get("operational"), Mapping) else {}
        enabled = bool(raw.get("enabled"))
        state = str(operational.get("state") or "unknown").casefold()
        healthy = state == "healthy"
        blocking_states = {
            "auth_required", "permission_denied", "quota_exhausted", "rate_limited",
            "model_unavailable", "degraded", "unreachable",
        }
        available = enabled and state not in blocking_states
        if not enabled:
            code = "provider_disabled"
        elif state == "healthy":
            code = "ready"
        elif state == "unknown":
            code = "provider_unverified"
        else:
            code = state if state in _REASONS else "provider_unavailable"
        credential = raw.get("credential") if isinstance(raw.get("credential"), Mapping) else {}
        return {
            "id": f"provider.{provider_id}",
            "name": _bounded_text(provider_id or "模型供應商", 120),
            "kind": "model_provider",
            "description": _bounded_text(raw.get("model_id") or "已設定的模型供應商", 300),
            "installed": True,
            "trusted": True,
            "enabled": enabled,
            "connected": available,
            "healthy": healthy,
            "project_allowed": True,
            "resource_allowed": True,
            "available": available,
            "permission": {"mode": "model_routing_policy"},
            "key_lifecycle": {
                "expires_at": _bounded_text(credential.get("expires_at"), 80) or None,
                "expiry_source": _bounded_text(credential.get("expiry_source"), 40) or "unknown",
                "never_expires": bool(credential.get("never_expires")),
                "remaining_days": credential.get("remaining_days") if isinstance(credential.get("remaining_days"), int) else None,
                "last_verified_at": _bounded_text(credential.get("last_verified_at"), 80) or None,
            },
            "capabilities": [],
            "reason_code": code,
            "reason": _REASONS[code],
            "repair": self._repair("model_provider", provider_id, code),
            "last_checked_at": _bounded_text(operational.get("updated_at") or _now_iso(), 80),
        }

    async def list_capabilities(self, project_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        integration, extensions, models = await self._snapshots(project)
        extension_index = self._extension_index(extensions)
        apply_state = integration.get("apply_state") if isinstance(integration.get("apply_state"), Mapping) else {}
        items: list[dict[str, Any]] = []
        represented_extension_ids = {"connector.gmail", "connector.github", "connector.notion", "builtin.n8n"}
        for raw in integration.get("integrations") or []:
            if isinstance(raw, Mapping):
                items.append(self._integration_item(raw, extension_index, apply_state))
        for raw in extensions.get("extensions") or []:
            if not isinstance(raw, Mapping):
                continue
            extension_id = str(raw.get("id") or "").casefold()
            if extension_id in represented_extension_ids or str(raw.get("entrypoint", {}).get("type") if isinstance(raw.get("entrypoint"), Mapping) else "") == "mcp_settings":
                continue
            items.append(self._extension_item(raw))
        for raw in models.get("providers") or []:
            if isinstance(raw, Mapping):
                items.append(self._model_item(raw))
        items.sort(key=lambda item: (str(item["kind"]), str(item["name"]).casefold(), str(item["id"])))
        payload = {
            "schema_version": 1,
            "project_id": project,
            "queried_at": _now_iso(),
            "items": items[:200],
            "summary": {
                "total": len(items),
                "available": sum(bool(item.get("available")) for item in items),
                "blocked": sum(not bool(item.get("available")) for item in items),
            },
        }
        return _safe_public(payload)

    @staticmethod
    def _matches(item: Mapping[str, Any], query: str) -> bool:
        normalized = query.casefold().strip()
        if not normalized:
            return True
        haystack = " ".join(
            [str(item.get("id") or ""), str(item.get("name") or ""), str(item.get("description") or "")]
        ).casefold()
        if normalized in haystack:
            return True
        for alias_id, aliases in _ALIASES.items():
            if any(alias in normalized for alias in aliases):
                if alias_id == "models":
                    return str(item.get("kind")) == "model_provider"
                if alias_id == "extensions":
                    return str(item.get("kind")) == "extension"
                if alias_id == "playwright":
                    return "playwright" in haystack or "browser" in haystack
                return str(item.get("id")) == alias_id
        return any(marker in normalized for marker in ("後台功能", "哪些功能", "所有功能", "可用工具"))

    async def query(self, project_id: str, query: str = "") -> dict[str, Any]:
        payload = await self.list_capabilities(project_id)
        normalized = _bounded_text(query, 500).strip()
        matches = [item for item in payload["items"] if self._matches(item, normalized)]
        return {
            **payload,
            "query": normalized,
            "items": matches,
            "summary": {
                "total": len(matches),
                "available": sum(bool(item.get("available")) for item in matches),
                "blocked": sum(not bool(item.get("available")) for item in matches),
            },
        }


def build_capability_status_tool_definitions(
    service: CapabilityStatusService,
) -> tuple[ToolDefinition, ...]:
    async def list_handler(call: Any) -> dict[str, Any]:
        return await service.list_capabilities(str(call.project_id or ""))

    async def get_handler(call: Any) -> dict[str, Any]:
        return await service.query(
            str(call.project_id or ""),
            str(call.arguments.get("capability") or ""),
        )

    common = {
        "access": ToolAccess.READ,
        "extension_id": CAPABILITY_STATUS_EXTENSION_ID,
        "manifest_sha256": CAPABILITY_STATUS_MANIFEST_SHA256,
        "risk_level": "read",
        "timeout_seconds": 3.0,
        "max_result_bytes": 16 * 1024,
        "requires_connection": False,
        "requires_resource": False,
    }
    return (
        ToolDefinition(
            name="workbench.list_capabilities",
            description=(
                "列出目前 Project 可用與受阻擋的 Workbench 功能，包括外掛、連線、"
                "MCP、n8n、對外 Agent API 與模型供應商；只回傳安全狀態。"
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=list_handler,
            **common,
        ),
        ToolDefinition(
            name="workbench.get_capability_status",
            description=(
                "查詢指定 Workbench 功能是否已安裝、連線、啟用、健康並獲目前 Project 放行，"
                "同時回傳不能使用的原因與修復入口。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "capability": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                        "description": "功能名稱，例如 Gmail、MCP、Playwright、n8n 或 NVIDIA。",
                    }
                },
                "required": ["capability"],
                "additionalProperties": False,
            },
            handler=get_handler,
            **common,
        ),
    )


__all__ = [
    "CAPABILITY_STATUS_EXTENSION_ID",
    "CAPABILITY_STATUS_MANIFEST_SHA256",
    "CapabilityStatusError",
    "CapabilityStatusService",
    "build_capability_status_tool_definitions",
]
