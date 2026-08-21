"""Run-bound human approval bridge for governed Basic Chat tools.

The ToolDispatcher owns the immutable invocation binding and performs the
authoritative consume step.  This broker persists a redacted mirror for audit,
delivers one public SSE request, and safely carries a local user's decision
from the compatibility HTTP route back to the dispatcher event loop.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

try:
    import database
    from tool_runtime import ToolApprovalDecision, ToolApprovalRequest
except ImportError:  # pragma: no cover - package import compatibility
    from backend import database
    from backend.tool_runtime import ToolApprovalDecision, ToolApprovalRequest


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def approval_risk_presentation(request: ToolApprovalRequest) -> dict[str, Any]:
    """Return a redacted, user-facing explanation of one exact operation."""

    tool_name = str(request.binding.tool_name or "")
    risk = str(request.summary.get("risk_level") or "external_write")
    suffix = tool_name.rsplit(".", 1)[-1]
    operation_label = {
        "browser_tabs": "管理瀏覽器頁籤",
        "browser_close": "關閉目前頁面",
        "browser_type": "在網頁欄位輸入文字",
        "browser_fill_form": "填寫網頁表單",
        "browser_select_option": "選擇網頁選項",
        "browser_file_upload": "上傳檔案到網站",
        "browser_click": "點擊網頁控制項",
        "browser_press_key": "在網頁中按下鍵盤按鍵",
        "browser_handle_dialog": "回應網站對話框",
    }.get(suffix, f"執行工具「{tool_name}」")
    arguments = request.summary.get("arguments")
    safe_arguments = arguments if isinstance(arguments, Mapping) else {}
    text_value = safe_arguments.get("text")
    field_values = safe_arguments.get("fields")
    paths = safe_arguments.get("paths")
    if isinstance(text_value, str):
        input_summary = f"準備輸入 {len(text_value)} 個字元；內容不會顯示在稽核事件中。"
    elif isinstance(field_values, list):
        input_summary = f"準備填寫 {len(field_values)} 個欄位；欄位內容不會顯示在稽核事件中。"
    elif isinstance(paths, list):
        input_summary = f"準備上傳 {len(paths)} 個檔案；核准前仍須通過專案檔案範圍驗證。"
    else:
        input_summary = "沒有可安全顯示的輸入內容；請依操作名稱與可能後果判斷。"
    if risk == "irreversible":
        operation_class = "high_risk"
    elif risk == "system":
        operation_class = "system"
    elif suffix in {"browser_tabs", "browser_close"}:
        operation_class = "low_risk"
    elif suffix in {
        "browser_type", "browser_fill_form", "browser_select_option",
        "browser_file_upload",
    }:
        operation_class = "data_input"
    else:
        operation_class = "external_write"

    copy = {
        "low_risk": {
            "risk_title": "低風險瀏覽器操作",
            "consequence": "可能切換或關閉頁籤，並使尚未送出的頁面內容遺失。",
            "data_disclosure": "不會因這次核准而取得其他專案檔案或秘密。",
            "reversibility": "通常可以重新開啟頁面，但未儲存的表單內容可能無法復原。",
        },
        "data_input": {
            "risk_title": "資料輸入",
            "consequence": "輸入內容會交給目前網站；網站可能保存、分析或轉交這些資料。",
            "data_disclosure": "只允許本次工具呼叫所綁定的輸入；不會授權網站讀取其他對話或檔案。",
            "reversibility": "文字在送出前通常可修改；一旦表單送出，可能無法由 Workbench 撤回。",
        },
        "external_write": {
            "risk_title": "外部網站操作",
            "consequence": "可能觸發導覽、送出表單、建立資料、發布內容、授權或其他網站副作用。",
            "data_disclosure": "網站元素可能來自不可信內容；只在你理解目標與後果時允許。",
            "reversibility": "實際結果由網站決定，部分操作可能無法撤回。",
        },
        "high_risk": {
            "risk_title": "高風險且可能不可逆",
            "consequence": "可能造成付款、刪除、正式發布、帳號授權或其他重大外部變更。",
            "data_disclosure": "可能向第三方提交資料或授予權限。核准前應自行確認網站、對象與內容。",
            "reversibility": "可能無法撤回；Workbench 不會把這類同意記住為專案自動授權。",
        },
        "system": {
            "risk_title": "系統操作",
            "consequence": "可能啟動程式、變更本機環境或接觸電腦資料。",
            "data_disclosure": "核准前必須確認程式、路徑、資料範圍與外部目的地。",
            "reversibility": "可能難以復原；Workbench 不會把這類同意記住為專案自動授權。",
        },
    }[operation_class]
    return {
        "operation_class": operation_class,
        "operation_label": operation_label,
        "target": request.binding.resource_id or "目前開啟的網站或工具目標",
        "input_summary": input_summary,
        **copy,
        "approval_scope": "僅允許這一次完全相同的操作；10 分鐘內有效，使用後立即失效。",
    }


class ToolApprovalBrokerError(RuntimeError):
    code = "TOOL_APPROVAL_INVALID"


class ToolApprovalNotFound(ToolApprovalBrokerError):
    code = "TOOL_APPROVAL_NOT_FOUND"


class ToolApprovalConflict(ToolApprovalBrokerError):
    code = "TOOL_APPROVAL_CONFLICT"


class ToolApprovalExpired(ToolApprovalBrokerError):
    code = "TOOL_APPROVAL_EXPIRED"


@dataclass
class _PendingDecision:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[ToolApprovalDecision]
    request: ToolApprovalRequest


class ToolApprovalBroker:
    """Persistent redacted audit plus in-process, restart-fail-closed waiters."""

    def __init__(self, *, database_module: Any = database) -> None:
        self.database = database_module
        self._lock = threading.RLock()
        self._pending: dict[str, _PendingDecision] = {}
        self._event_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.ensure_schema()
        self.invalidate_incomplete()

    def ensure_schema(self) -> None:
        with self.database.get_db_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_approval_bindings (
                    approval_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    connection_id TEXT,
                    resource_id TEXT,
                    binding_sha256 TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    resource_revision INTEGER NOT NULL,
                    arguments_sha256 TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    decided_by TEXT,
                    consumed_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tool_approvals_run "
                "ON tool_approval_bindings(run_id, created_at)"
            )

    def invalidate_incomplete(self) -> int:
        """A process restart cannot retain a live in-memory decision channel."""

        with self.database.get_db_conn() as conn:
            cursor = conn.execute(
                """
                UPDATE tool_approval_bindings
                SET status = 'expired', decided_at = COALESCE(decided_at, ?),
                    decided_by = COALESCE(decided_by, 'system_restart')
                WHERE status IN ('pending', 'approved')
                """,
                (_now_iso(),),
            )
            return int(cursor.rowcount or 0)

    @staticmethod
    def _safe_summary(request: ToolApprovalRequest) -> dict[str, Any]:
        raw = dict(request.summary or {})
        return {
            "tool_name": request.binding.tool_name,
            "access": str(raw.get("access") or "write")[:32],
            "risk_level": str(raw.get("risk_level") or "external_write")[:32],
            "resource_id": request.binding.resource_id,
        }

    def _persist_request(self, request: ToolApprovalRequest) -> None:
        safe_summary = self._safe_summary(request)
        binding = request.binding
        with self.database.get_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO tool_approval_bindings (
                    approval_id, run_id, project_id, call_id, tool_name,
                    connection_id, resource_id, binding_sha256, manifest_sha256,
                    resource_revision, arguments_sha256, summary_json, reason,
                    status, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    request.approval_id,
                    binding.run_id,
                    binding.project_id,
                    binding.call_id,
                    binding.tool_name,
                    binding.connection_id,
                    binding.resource_id,
                    request.binding_sha256,
                    binding.manifest_sha256,
                    binding.resource_revision,
                    binding.arguments_sha256,
                    json.dumps(safe_summary, ensure_ascii=False, sort_keys=True),
                    str(request.reason or "External write requires approval")[:1000],
                    float(request.expires_at),
                    _now_iso(),
                ),
            )
        self.database.create_capability_approval(
            request.approval_id,
            run_id=binding.run_id,
            capability_name=binding.tool_name,
            risk_level=str(safe_summary["risk_level"]),
            reason=str(request.reason or "External write requires approval")[:1000],
        )

    def event_queue(self, run_id: str) -> asyncio.Queue[dict[str, Any]]:
        normalized = str(run_id or "").strip()
        if not normalized:
            raise ValueError("run_id is required")
        queue = self._event_queues.get(normalized)
        if queue is None:
            queue = asyncio.Queue()
            self._event_queues[normalized] = queue
        return queue

    @staticmethod
    def public_event(request: ToolApprovalRequest) -> dict[str, Any]:
        resource = request.binding.resource_id
        presentation = approval_risk_presentation(request)
        summary = f"{request.binding.tool_name}"
        if resource:
            summary += f" · {resource}"
        return {
            "approval_id": request.approval_id,
            "capability": presentation["operation_label"],
            "tool_name": request.binding.tool_name,
            "message": request.reason or "Agent 要求執行外部寫入。",
            "summary": summary,
            "run_id": request.binding.run_id,
            "risk": str(request.summary.get("risk_level") or "external_write"),
            "status": "pending",
            "choices": ["once", "deny"],
            **presentation,
            "updated_at": _now_iso(),
        }

    async def approval_callback(
        self, request: ToolApprovalRequest
    ) -> ToolApprovalDecision:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ToolApprovalDecision] = loop.create_future()
        self._persist_request(request)
        with self._lock:
            if request.approval_id in self._pending:
                raise ToolApprovalConflict("approval ID is already pending")
            self._pending[request.approval_id] = _PendingDecision(loop, future, request)
        await self.event_queue(request.binding.run_id).put(self.public_event(request))
        remaining = max(0.0, float(request.expires_at) - time.time())
        try:
            if remaining <= 0:
                raise asyncio.TimeoutError
            return await asyncio.wait_for(future, timeout=remaining)
        except asyncio.TimeoutError as error:
            self._expire(request.approval_id, "system_timeout")
            raise ToolApprovalExpired("approval expired before a decision") from error
        finally:
            with self._lock:
                self._pending.pop(request.approval_id, None)

    def _expire(self, approval_id: str, decided_by: str) -> None:
        with self.database.get_db_conn() as conn:
            conn.execute(
                """
                UPDATE tool_approval_bindings
                SET status = 'expired', decided_at = ?, decided_by = ?
                WHERE approval_id = ? AND status = 'pending'
                """,
                (_now_iso(), decided_by, approval_id),
            )
        self.database.expire_capability_approval(approval_id)

    def decide(
        self,
        *,
        run_id: str,
        approval_id: str,
        approved: bool,
        decided_by: str,
        rationale: str = "",
    ) -> dict[str, Any]:
        normalized_id = str(approval_id or "").strip()
        normalized_run = str(run_id or "").strip()
        with self.database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT * FROM tool_approval_bindings WHERE approval_id = ?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise ToolApprovalNotFound("approval was not found")
            record = dict(row)
            if str(record.get("run_id") or "") != normalized_run:
                raise ToolApprovalConflict("approval belongs to another run")
            if str(record.get("status") or "") != "pending":
                raise ToolApprovalConflict(
                    f"approval is already {record.get('status') or 'resolved'}"
                )
            if time.time() >= float(record.get("expires_at") or 0):
                self._expire(normalized_id, "system_timeout")
                raise ToolApprovalExpired("approval has expired")
            status = "approved" if approved else "denied"
            cursor = conn.execute(
                """
                UPDATE tool_approval_bindings
                SET status = ?, decided_at = ?, decided_by = ?
                WHERE approval_id = ? AND run_id = ? AND status = 'pending'
                """,
                (
                    status,
                    _now_iso(),
                    str(decided_by or "local_user")[:128],
                    normalized_id,
                    normalized_run,
                ),
            )
            if cursor.rowcount != 1:
                raise ToolApprovalConflict("approval decision lost a concurrent update")
        if not self.database.decide_capability_approval(
            normalized_id,
            bool(approved),
            decided_by=str(decided_by or "local_user")[:128],
        ):
            raise ToolApprovalConflict("approval audit state is no longer pending")
        with self._lock:
            pending = self._pending.get(normalized_id)
        if pending is None or pending.future.done():
            raise ToolApprovalExpired("approval is no longer attached to an active run")
        decision = ToolApprovalDecision(
            approved=bool(approved),
            decided_by=str(decided_by or "local_user")[:128],
            rationale=str(rationale or "")[:1000],
        )
        pending.loop.call_soon_threadsafe(pending.future.set_result, decision)
        return {
            "approval_id": normalized_id,
            "run_id": normalized_run,
            "approved": bool(approved),
            "status": status,
        }

    def mark_consumed(self, approval_id: Optional[str]) -> None:
        if not approval_id:
            return
        with self.database.get_db_conn() as conn:
            conn.execute(
                """
                UPDATE tool_approval_bindings
                SET status = 'consumed', consumed_at = ?
                WHERE approval_id = ? AND status = 'approved'
                """,
                (_now_iso(), str(approval_id)),
            )

    def close_run(self, run_id: str) -> None:
        normalized = str(run_id or "").strip()
        with self._lock:
            pending = [item for item in self._pending.values() if item.request.binding.run_id == normalized]
        for item in pending:
            if not item.future.done():
                item.loop.call_soon_threadsafe(
                    item.future.set_exception,
                    ToolApprovalExpired("run ended before approval"),
                )
            self._expire(item.request.approval_id, "system_run_end")
        self._event_queues.pop(normalized, None)


__all__ = [
    "ToolApprovalBroker",
    "ToolApprovalBrokerError",
    "ToolApprovalConflict",
    "ToolApprovalExpired",
    "ToolApprovalNotFound",
]
