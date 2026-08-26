"""Shared execution, policy, artifact and health contracts.

This module is deliberately runtime-agnostic.  Integrations may mirror their
domain state here without surrendering their existing source of truth during
the incremental migration.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from structured_log import redact


EXECUTION_STATES = {
    "queued", "preparing", "running", "awaiting_approval", "completed",
    "failed", "cancelled", "execution_unknown",
}
HEALTH_STATES = {"unknown", "healthy", "degraded", "unavailable", "disabled"}
POLICY_ACTIONS = {"allow", "deny", "require_approval"}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,159}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(text):
        raise ValueError(f"invalid {field}")
    return text


def _json(value: Any, *, limit: int = 32768) -> str:
    safe = redact(value if value is not None else {})
    encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError("operation metadata exceeds the bounded contract")
    return encoded


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


class OperationsCore:
    def __init__(self, *, database_module: Any) -> None:
        self.database = database_module
        self._lock = threading.RLock()

    def initialize(self) -> None:
        with self.database.get_db_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS operation_executions (
                    execution_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    owner_type TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    project_id TEXT,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1,
                    parent_execution_id TEXT,
                    correlation_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT,
                    error_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_operation_executions_project
                    ON operation_executions(project_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_operation_executions_owner
                    ON operation_executions(owner_type, owner_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS operation_events (
                    event_id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(execution_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS operation_policy_decisions (
                    decision_id TEXT PRIMARY KEY,
                    execution_id TEXT,
                    project_id TEXT,
                    policy_id TEXT NOT NULL,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_operation_policy_project
                    ON operation_policy_decisions(project_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS operation_artifact_references (
                    reference_id TEXT PRIMARY KEY,
                    execution_id TEXT,
                    project_id TEXT,
                    artifact_kind TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    locator_json TEXT NOT NULL,
                    sha256 TEXT,
                    size_bytes INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_operation_artifacts_project
                    ON operation_artifact_references(project_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS operation_health_contracts (
                    component_type TEXT NOT NULL,
                    component_id TEXT NOT NULL,
                    project_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_success_at TEXT,
                    checked_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(component_type, component_id, project_id)
                );
                """
            )

    def create_execution(self, *, kind: str, owner_type: str, owner_id: str,
                         project_id: Optional[str] = None, status: str = "queued",
                         metadata: Optional[Mapping[str, Any]] = None,
                         execution_id: Optional[str] = None,
                         parent_execution_id: Optional[str] = None,
                         correlation_id: Optional[str] = None) -> dict[str, Any]:
        execution_id = _identifier(execution_id or f"exec_{uuid.uuid4().hex}", "execution_id")
        kind = _identifier(kind, "kind")
        owner_type = _identifier(owner_type, "owner_type")
        owner_id = _identifier(owner_id, "owner_id")
        if status not in EXECUTION_STATES:
            raise ValueError("invalid execution status")
        now = _now()
        with self.database.get_db_conn() as conn:
            conn.execute(
                """INSERT INTO operation_executions
                   (execution_id,kind,owner_type,owner_id,project_id,status,progress,revision,
                    parent_execution_id,correlation_id,metadata_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,0,1,?,?,?,?,?)
                   ON CONFLICT(execution_id) DO NOTHING""",
                (execution_id, kind, owner_type, owner_id, project_id, status,
                 parent_execution_id, correlation_id, _json(metadata), now, now),
            )
        self.append_event(execution_id, "execution.created", {"status": status})
        return self.get_execution(execution_id) or {}

    def update_execution(self, execution_id: str, *, status: Optional[str] = None,
                         progress: Optional[int] = None, metadata: Optional[Mapping[str, Any]] = None,
                         error_code: Optional[str] = None, error_reason: Optional[str] = None,
                         expected_revision: Optional[int] = None) -> dict[str, Any]:
        execution_id = _identifier(execution_id, "execution_id")
        current = self.get_execution(execution_id)
        if current is None:
            raise KeyError(execution_id)
        if expected_revision is not None and int(current["revision"]) != int(expected_revision):
            raise RuntimeError("OPERATION_REVISION_CONFLICT")
        next_status = status or current["status"]
        if next_status not in EXECUTION_STATES:
            raise ValueError("invalid execution status")
        next_progress = int(current["progress"] if progress is None else progress)
        if not 0 <= next_progress <= 100:
            raise ValueError("progress must be between 0 and 100")
        merged = dict(current.get("metadata") or {})
        if metadata:
            merged.update(metadata)
        now = _now()
        terminal = next_status in {"completed", "failed", "cancelled", "execution_unknown"}
        with self.database.get_db_conn() as conn:
            conn.execute(
                """UPDATE operation_executions SET status=?,progress=?,metadata_json=?,error_code=?,
                   error_reason=?,revision=revision+1,updated_at=?,completed_at=? WHERE execution_id=?""",
                (next_status, next_progress, _json(merged), (error_code or None),
                 str(redact(error_reason or ""))[:1000] or None, now, now if terminal else None,
                 execution_id),
            )
        self.append_event(execution_id, "execution.updated", {"status": next_status, "progress": next_progress})
        return self.get_execution(execution_id) or {}

    def append_event(self, execution_id: str, event_type: str, payload: Optional[Mapping[str, Any]] = None) -> None:
        execution_id = _identifier(execution_id, "execution_id")
        event_type = _identifier(event_type, "event_type")
        with self.database.get_db_conn() as conn:
            row = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM operation_events WHERE execution_id=?", (execution_id,)).fetchone()
            conn.execute(
                "INSERT INTO operation_events VALUES (?,?,?,?,?,?)",
                (f"evt_{uuid.uuid4().hex}", execution_id, event_type, int(row[0]), _json(payload, limit=16384), _now()),
            )

    def get_execution(self, execution_id: str) -> Optional[dict[str, Any]]:
        with self.database.get_db_conn() as conn:
            row = conn.execute("SELECT * FROM operation_executions WHERE execution_id=?", (execution_id,)).fetchone()
        return self._execution(row) if row else None

    def list_executions(self, *, project_id: Optional[str] = None, kind: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses, args = [], []
        if project_id:
            clauses.append("project_id=?"); args.append(project_id)
        if kind:
            clauses.append("kind=?"); args.append(kind)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.get_db_conn() as conn:
            rows = conn.execute(f"SELECT * FROM operation_executions{where} ORDER BY updated_at DESC LIMIT ?", (*args, max(1, min(int(limit), 500)))).fetchall()
        return [self._execution(row) for row in rows]

    def list_events(self, execution_id: str) -> list[dict[str, Any]]:
        with self.database.get_db_conn() as conn:
            rows = conn.execute("SELECT * FROM operation_events WHERE execution_id=? ORDER BY sequence", (execution_id,)).fetchall()
        return [{**dict(row), "payload": _loads(row["payload_json"], {})} for row in rows]

    @staticmethod
    def _execution(row: Any) -> dict[str, Any]:
        result = dict(row); result["metadata"] = _loads(result.pop("metadata_json", ""), {})
        return result

    def record_policy_decision(self, *, policy_id: str, subject_type: str, subject_id: str,
                               action: str, reason_code: str, risk_level: str = "low",
                               inputs: Optional[Mapping[str, Any]] = None,
                               detail: Optional[Mapping[str, Any]] = None,
                               execution_id: Optional[str] = None,
                               project_id: Optional[str] = None) -> dict[str, Any]:
        if action not in POLICY_ACTIONS:
            raise ValueError("invalid policy action")
        digest = hashlib.sha256(_json(inputs).encode("utf-8")).hexdigest()
        decision_id = f"decision_{uuid.uuid4().hex}"
        with self.database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO operation_policy_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, execution_id, project_id, _identifier(policy_id, "policy_id"),
                 _identifier(subject_type, "subject_type"), _identifier(subject_id, "subject_id"),
                 action, _identifier(reason_code, "reason_code"), risk_level, digest,
                 _json(detail, limit=16384), _now()),
            )
        return {"decision_id": decision_id, "action": action, "reason_code": reason_code, "input_digest": digest}

    def list_policy_decisions(self, *, project_id: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        where, args = (" WHERE project_id=?", [project_id]) if project_id else ("", [])
        with self.database.get_db_conn() as conn:
            rows = conn.execute(f"SELECT * FROM operation_policy_decisions{where} ORDER BY created_at DESC LIMIT ?", (*args, max(1, min(limit, 500)))).fetchall()
        return [{**dict(row), "detail": _loads(row["detail_json"], {})} for row in rows]

    def register_artifact(self, *, artifact_kind: str, display_name: str, locator: Mapping[str, Any],
                          execution_id: Optional[str] = None, project_id: Optional[str] = None,
                          sha256: Optional[str] = None, size_bytes: Optional[int] = None,
                          metadata: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        reference_id = f"artifact_ref_{uuid.uuid4().hex}"
        with self.database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO operation_artifact_references VALUES (?,?,?,?,?,?,?,?,?,?)",
                (reference_id, execution_id, project_id, _identifier(artifact_kind, "artifact_kind"),
                 str(display_name)[:240], _json(locator, limit=8192), sha256,
                 max(0, int(size_bytes)) if size_bytes is not None else None,
                 _json(metadata, limit=16384), _now()),
            )
        return {"reference_id": reference_id, "artifact_kind": artifact_kind, "display_name": str(display_name)[:240]}

    def list_artifacts(self, *, project_id: Optional[str] = None, execution_id: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses, args = [], []
        if project_id: clauses.append("project_id=?"); args.append(project_id)
        if execution_id: clauses.append("execution_id=?"); args.append(execution_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.get_db_conn() as conn:
            rows = conn.execute(f"SELECT * FROM operation_artifact_references{where} ORDER BY created_at DESC LIMIT ?", (*args, max(1, min(limit, 500)))).fetchall()
        return [{**dict(row), "locator": _loads(row["locator_json"], {}), "metadata": _loads(row["metadata_json"], {})} for row in rows]

    def report_health(self, *, component_type: str, component_id: str, status: str,
                      reason_code: str, project_id: Optional[str] = None,
                      detail: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        if status not in HEALTH_STATES:
            raise ValueError("invalid health status")
        component_type = _identifier(component_type, "component_type")
        component_id = _identifier(component_id, "component_id")
        scope = project_id or ""
        now = _now()
        with self.database.get_db_conn() as conn:
            conn.execute(
                """INSERT INTO operation_health_contracts
                   (component_type,component_id,project_id,status,reason_code,detail_json,
                    consecutive_failures,last_success_at,checked_at,revision)
                   VALUES (?,?,?,?,?,?,?, ?,?,1)
                   ON CONFLICT(component_type,component_id,project_id) DO UPDATE SET
                    status=excluded.status,reason_code=excluded.reason_code,detail_json=excluded.detail_json,
                    consecutive_failures=CASE WHEN excluded.status='healthy' THEN 0 ELSE operation_health_contracts.consecutive_failures+1 END,
                    last_success_at=CASE WHEN excluded.status='healthy' THEN excluded.checked_at ELSE operation_health_contracts.last_success_at END,
                    checked_at=excluded.checked_at,revision=operation_health_contracts.revision+1""",
                (component_type, component_id, scope, status, _identifier(reason_code, "reason_code"),
                 _json(detail, limit=16384), 0 if status == "healthy" else 1,
                 now if status == "healthy" else None, now),
            )
        return self.get_health(component_type, component_id, project_id=project_id) or {}

    def get_health(self, component_type: str, component_id: str, *, project_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        with self.database.get_db_conn() as conn:
            row = conn.execute("SELECT * FROM operation_health_contracts WHERE component_type=? AND component_id=? AND project_id=?", (component_type, component_id, project_id or "")).fetchone()
        if not row: return None
        result = dict(row); result["project_id"] = result["project_id"] or None; result["detail"] = _loads(result.pop("detail_json", ""), {})
        return result

    def list_health(self, *, project_id: Optional[str] = None) -> list[dict[str, Any]]:
        query, args = "SELECT * FROM operation_health_contracts", []
        if project_id is not None:
            query += " WHERE project_id IN ('',?)"; args.append(project_id)
        query += " ORDER BY component_type,component_id,project_id"
        with self.database.get_db_conn() as conn:
            rows = conn.execute(query, args).fetchall()
        result = []
        for row in rows:
            item = dict(row); item["project_id"] = item["project_id"] or None; item["detail"] = _loads(item.pop("detail_json", ""), {}); result.append(item)
        return result
