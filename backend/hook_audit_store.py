"""Durable, redacted audit sink for trusted host Hook executions."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import database
    from hook_runtime import HookAuditRecord
except ImportError:  # pragma: no cover - package import compatibility
    from backend import database
    from backend.hook_runtime import HookAuditRecord


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HookAuditStore:
    """Append-only Hook audit records containing no context values or secrets."""

    def __init__(self, *, database_module: Any = database) -> None:
        self.database = database_module
        self._schema_lock = threading.RLock()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self._schema_lock, self.database.get_db_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS hook_audits (
                    audit_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    hook_id TEXT NOT NULL,
                    extension_id TEXT NOT NULL,
                    extension_version TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    error_code TEXT,
                    error_type TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_hook_audits_event
                    ON hook_audits(event, event_id, audit_sequence);
                CREATE INDEX IF NOT EXISTS idx_hook_audits_extension
                    ON hook_audits(extension_id, audit_sequence DESC);
                """
            )

    def record(self, record: HookAuditRecord) -> None:
        if not isinstance(record, HookAuditRecord):
            raise TypeError("record must be HookAuditRecord")
        with self.database.get_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO hook_audits (
                    event, event_id, mode, hook_id, extension_id,
                    extension_version, manifest_sha256, status, duration_ms,
                    error_code, error_type, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.event,
                    record.event_id,
                    record.mode,
                    record.hook_id,
                    record.extension_id,
                    record.extension_version,
                    record.manifest_sha256,
                    record.status,
                    max(0, int(record.duration_ms)),
                    record.error_code,
                    record.error_type,
                    str(record.error or "")[:1000] or None,
                    _now_iso(),
                ),
            )

    __call__ = record

    def list(
        self,
        *,
        extension_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        query = "SELECT * FROM hook_audits"
        parameters: tuple[Any, ...]
        if extension_id:
            query += " WHERE extension_id = ?"
            parameters = (str(extension_id), bounded)
        else:
            parameters = (bounded,)
        query += " ORDER BY audit_sequence DESC LIMIT ?"
        with self.database.get_db_conn() as conn:
            return [dict(row) for row in conn.execute(query, parameters).fetchall()]


__all__ = ["HookAuditStore"]
