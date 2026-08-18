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
        summary = f"{request.binding.tool_name}"
        if resource:
            summary += f" · {resource}"
        return {
            "approval_id": request.approval_id,
            "capability": request.binding.tool_name,
            "message": request.reason or "Agent 要求執行外部寫入。",
            "summary": summary,
            "run_id": request.binding.run_id,
            "risk": str(request.summary.get("risk_level") or "external_write"),
            "status": "pending",
            "choices": ["once", "deny"],
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
