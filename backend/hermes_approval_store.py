"""Durable, narrowly scoped Workbench decisions for Hermes run approvals."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, ContextManager, Mapping, Optional

from database import get_db_conn
from structured_log import redact


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_STATUSES = {
    "pending",
    "resolving_once",
    "resolving_deny",
    "approved_once",
    "denied",
    "denied_policy",
    "denied_missing_live_grant",
    "resolution_unknown",
    "expired",
}


class HermesApprovalStoreError(ValueError):
    pass


class HermesApprovalConflictError(HermesApprovalStoreError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, label: str, *, maximum: int = 512, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise HermesApprovalStoreError(f"{label} is required.")
    if len(text) > maximum or _CONTROL.search(text):
        raise HermesApprovalStoreError(f"{label} is invalid.")
    return text


def approval_event_fingerprint(workbench_run_id: str, event: Mapping[str, Any]) -> str:
    """Deduplicate SSE replay without persisting the raw approval payload."""

    safe = {
        "run": _text(workbench_run_id, "Workbench run ID"),
        "request_id": str(event.get("request_id") or "")[:256],
        "timestamp": str(event.get("timestamp") or "")[:64],
        "tool": str(event.get("tool") or event.get("tool_name") or "")[:128],
        "command_sha256": hashlib.sha256(
            str(event.get("command") or "").encode("utf-8", errors="replace")
        ).hexdigest(),
    }
    canonical = json.dumps(safe, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def approval_summary(event: Mapping[str, Any]) -> str:
    """Return a redacted preview; the full command never enters Workbench DB."""

    candidate = (
        event.get("description")
        or event.get("reason")
        or event.get("command")
        or event.get("tool")
        or event.get("tool_name")
        or "Hermes requested permission to use a tool."
    )
    return str(redact(str(candidate)))[:1000]


@dataclass(frozen=True)
class PersistentHermesApproval:
    approval_id: str
    event_fingerprint: str
    workbench_run_id: str
    workbench_session_id: str
    project_id: Optional[str]
    capability: str
    resource: str
    summary: str
    status: str
    choices: tuple[str, ...]
    created_at: str
    updated_at: str
    rationale: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "run_id": self.workbench_run_id,
            "session_id": self.workbench_session_id,
            "project_id": self.project_id,
            "capability": self.capability,
            "resource": self.resource,
            "summary": self.summary,
            "status": self.status,
            "choices": list(self.choices),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "rationale": self.rationale,
        }


class PersistentHermesApprovalStore:
    """SQLite source of truth for pending and terminal approval decisions."""

    def __init__(
        self,
        connection_factory: Optional[Callable[[], ContextManager[Any]]] = None,
    ) -> None:
        self._connection_factory = connection_factory or get_db_conn
        self._schema_lock = threading.RLock()
        self._schema_ready = False

    def _ensure_schema(self, conn: Any) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hermes_approval_requests (
                    approval_id TEXT PRIMARY KEY,
                    event_fingerprint TEXT NOT NULL UNIQUE,
                    workbench_run_id TEXT NOT NULL,
                    workbench_session_id TEXT NOT NULL,
                    project_id TEXT,
                    capability TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL,
                    choices_json TEXT NOT NULL,
                    rationale TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_hermes_approvals_pending
                ON hermes_approval_requests(status, created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_hermes_approvals_run
                ON hermes_approval_requests(workbench_run_id, created_at)
                """
            )
            has_sessions = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
            ).fetchone()
            if has_sessions:
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS trg_hermes_approval_session_cleanup
                    AFTER DELETE ON sessions
                    BEGIN
                        DELETE FROM hermes_approval_requests
                        WHERE workbench_session_id = OLD.id;
                    END
                    """
                )
            self._schema_ready = True

    @staticmethod
    def _record(row: Any) -> PersistentHermesApproval:
        try:
            raw_choices = json.loads(row["choices_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            raw_choices = []
        choices = tuple(
            item for item in raw_choices if item in {"once", "deny"}
        )
        return PersistentHermesApproval(
            approval_id=row["approval_id"],
            event_fingerprint=row["event_fingerprint"],
            workbench_run_id=row["workbench_run_id"],
            workbench_session_id=row["workbench_session_id"],
            project_id=row["project_id"],
            capability=row["capability"],
            resource=row["resource"],
            summary=row["summary"],
            status=row["status"],
            choices=choices,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            rationale=str(row["rationale"] or ""),
        )

    def get(self, approval_id: str) -> Optional[PersistentHermesApproval]:
        safe_id = _text(approval_id, "Approval ID")
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM hermes_approval_requests WHERE approval_id = ?",
                (safe_id,),
            ).fetchone()
        return self._record(row) if row is not None else None

    def find_event(self, fingerprint: str) -> Optional[PersistentHermesApproval]:
        safe_fingerprint = _text(fingerprint, "Approval fingerprint", maximum=64)
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM hermes_approval_requests WHERE event_fingerprint = ?",
                (safe_fingerprint,),
            ).fetchone()
        return self._record(row) if row is not None else None

    def create(
        self,
        *,
        approval_id: str,
        event_fingerprint: str,
        workbench_run_id: str,
        workbench_session_id: str,
        project_id: Optional[str],
        capability: str,
        resource: str,
        summary: str,
        status: str = "pending",
        choices: tuple[str, ...] = ("once", "deny"),
    ) -> PersistentHermesApproval:
        safe_status = _text(status, "Approval status", maximum=32)
        if safe_status not in _STATUSES:
            raise HermesApprovalStoreError("Approval status is invalid.")
        safe_choices = tuple(item for item in choices if item in {"once", "deny"})
        if not safe_choices:
            safe_choices = ("deny",)
        now = _now()
        values = (
            _text(approval_id, "Approval ID"),
            _text(event_fingerprint, "Approval fingerprint", maximum=64),
            _text(workbench_run_id, "Workbench run ID"),
            _text(workbench_session_id, "Workbench session ID"),
            _text(project_id, "Project ID", required=False) or None,
            _text(capability, "Capability", maximum=128),
            _text(resource, "Approval resource"),
            _text(summary, "Approval summary", maximum=1000),
            safe_status,
            json.dumps(safe_choices),
            now,
            now,
        )
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                INSERT OR IGNORE INTO hermes_approval_requests (
                    approval_id, event_fingerprint, workbench_run_id,
                    workbench_session_id, project_id, capability, resource,
                    summary, status, choices_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = conn.execute(
                "SELECT * FROM hermes_approval_requests WHERE event_fingerprint = ?",
                (values[1],),
            ).fetchone()
        if row is None:
            raise HermesApprovalStoreError("Approval request could not be persisted.")
        return self._record(row)

    def claim(
        self,
        approval_id: str,
        *,
        choice: str,
        rationale: str,
    ) -> PersistentHermesApproval:
        safe_id = _text(approval_id, "Approval ID")
        safe_choice = str(choice or "").strip().casefold()
        if safe_choice not in {"once", "deny"}:
            raise HermesApprovalStoreError("Approval choice must be once or deny.")
        safe_rationale = _text(
            rationale or ("Approved once." if safe_choice == "once" else "Denied."),
            "Approval rationale",
            maximum=1000,
        )
        resolving = f"resolving_{safe_choice}"
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE hermes_approval_requests
                SET status = ?, rationale = ?, updated_at = ?
                WHERE approval_id = ? AND status = 'pending'
                """,
                (resolving, safe_rationale, _now(), safe_id),
            )
            row = conn.execute(
                "SELECT * FROM hermes_approval_requests WHERE approval_id = ?",
                (safe_id,),
            ).fetchone()
        if row is None:
            raise KeyError(safe_id)
        if cursor.rowcount != 1:
            raise HermesApprovalConflictError(
                f"Approval is already in state {row['status']}."
            )
        return self._record(row)

    def finish(
        self,
        approval_id: str,
        *,
        status: str,
    ) -> PersistentHermesApproval:
        safe_id = _text(approval_id, "Approval ID")
        safe_status = _text(status, "Approval status", maximum=32)
        if safe_status not in {
            "approved_once",
            "denied",
            "denied_missing_live_grant",
            "resolution_unknown",
        }:
            raise HermesApprovalStoreError("Terminal approval status is invalid.")
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE hermes_approval_requests SET status = ?, updated_at = ?
                WHERE approval_id = ? AND status LIKE 'resolving_%'
                """,
                (safe_status, _now(), safe_id),
            )
            row = conn.execute(
                "SELECT * FROM hermes_approval_requests WHERE approval_id = ?",
                (safe_id,),
            ).fetchone()
        if row is None:
            raise KeyError(safe_id)
        if cursor.rowcount != 1:
            raise HermesApprovalConflictError("Approval is not being resolved.")
        return self._record(row)

    def expire_run(self, workbench_run_id: str) -> int:
        run_id = _text(workbench_run_id, "Workbench run ID")
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE hermes_approval_requests SET status = 'expired', updated_at = ?
                WHERE workbench_run_id = ? AND status = 'pending'
                """,
                (_now(), run_id),
            )
        return int(cursor.rowcount)

    def list_pending(
        self,
        *,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[PersistentHermesApproval]:
        clauses = ["status = 'pending'"]
        values: list[Any] = []
        if session_id:
            clauses.append("workbench_session_id = ?")
            values.append(_text(session_id, "Workbench session ID"))
        if run_id:
            clauses.append("workbench_run_id = ?")
            values.append(_text(run_id, "Workbench run ID"))
        safe_limit = max(1, min(int(limit), 500))
        values.append(safe_limit)
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT * FROM hermes_approval_requests WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at ASC LIMIT ?",
                values,
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_for_run(
        self,
        workbench_run_id: str,
        *,
        limit: int = 100,
    ) -> list[PersistentHermesApproval]:
        """Return persisted approval state for execution-snapshot hydration."""

        run_id = _text(workbench_run_id, "Workbench run ID")
        safe_limit = max(1, min(int(limit), 500))
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT * FROM hermes_approval_requests
                WHERE workbench_run_id = ?
                ORDER BY created_at ASC LIMIT ?
                """,
                (run_id, safe_limit),
            ).fetchall()
        return [self._record(row) for row in rows]


__all__ = [
    "HermesApprovalConflictError",
    "HermesApprovalStoreError",
    "PersistentHermesApproval",
    "PersistentHermesApprovalStore",
    "approval_event_fingerprint",
    "approval_summary",
]
