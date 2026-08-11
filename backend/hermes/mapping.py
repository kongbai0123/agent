"""Persistent Workbench-to-Hermes session and run identity mapping."""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, ContextManager, Optional

from database import get_db_conn

from .errors import HermesConflictError


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workbench_id(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 256 or _CONTROL.search(text):
        raise ValueError(f"{label} is invalid.")
    return text


@dataclass(frozen=True)
class HermesSessionMapping:
    workbench_session_id: str
    workbench_scope: str
    hermes_session_id: str
    hermes_session_key: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class HermesRunMapping:
    workbench_run_id: str
    workbench_session_id: str
    hermes_run_id: str
    status: str
    previous_response_id: str
    created_at: str
    updated_at: str


class HermesRunMappingStore:
    """Lazy schema that can share Workbench SQLite without database.py edits."""

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
                CREATE TABLE IF NOT EXISTS hermes_session_mappings (
                    workbench_session_id TEXT PRIMARY KEY,
                    workbench_scope TEXT NOT NULL DEFAULT '',
                    hermes_session_id TEXT NOT NULL UNIQUE,
                    hermes_session_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            session_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(hermes_session_mappings)"
                ).fetchall()
            }
            if "workbench_scope" not in session_columns:
                conn.execute(
                    "ALTER TABLE hermes_session_mappings "
                    "ADD COLUMN workbench_scope TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hermes_run_mappings (
                    workbench_run_id TEXT PRIMARY KEY,
                    workbench_session_id TEXT NOT NULL,
                    hermes_run_id TEXT UNIQUE,
                    status TEXT NOT NULL,
                    previous_response_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_hermes_runs_session
                ON hermes_run_mappings(workbench_session_id, created_at)
                """
            )
            # A production Workbench database has a sessions table. Standalone
            # mapping tests intentionally do not, so install the cleanup trigger
            # only when the parent table is present.
            has_sessions = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
            ).fetchone()
            if has_sessions:
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS trg_hermes_session_mapping_cleanup
                    AFTER DELETE ON sessions
                    BEGIN
                        DELETE FROM hermes_run_mappings
                        WHERE workbench_session_id = OLD.id;
                        DELETE FROM hermes_session_mappings
                        WHERE workbench_session_id = OLD.id;
                    END
                    """
                )
            self._schema_ready = True

    @staticmethod
    def _session(row: Any) -> HermesSessionMapping:
        return HermesSessionMapping(
            workbench_session_id=row["workbench_session_id"],
            workbench_scope=str(row["workbench_scope"] or ""),
            hermes_session_id=row["hermes_session_id"],
            hermes_session_key=row["hermes_session_key"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _run(row: Any) -> HermesRunMapping:
        return HermesRunMapping(
            workbench_run_id=row["workbench_run_id"],
            workbench_session_id=row["workbench_session_id"],
            hermes_run_id=str(row["hermes_run_id"] or ""),
            status=row["status"],
            previous_response_id=str(row["previous_response_id"] or ""),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_or_create_session(
        self,
        workbench_session_id: str,
        *,
        workbench_scope: Optional[str] = None,
    ) -> HermesSessionMapping:
        workbench_id = _workbench_id(workbench_session_id, "Workbench session ID")
        scope = None if workbench_scope is None else str(workbench_scope).strip()
        if scope is not None and (len(scope) > 256 or _CONTROL.search(scope)):
            raise ValueError("Workbench session scope is invalid.")
        now = _now()
        proposed_id = f"wb-session-{uuid.uuid4().hex}"
        proposed_key = f"wb-memory-{uuid.uuid4().hex}"
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                INSERT OR IGNORE INTO hermes_session_mappings (
                    workbench_session_id, workbench_scope,
                    hermes_session_id, hermes_session_key,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (workbench_id, scope or "", proposed_id, proposed_key, now, now),
            )
            existing = conn.execute(
                "SELECT workbench_scope FROM hermes_session_mappings "
                "WHERE workbench_session_id = ?",
                (workbench_id,),
            ).fetchone()
            if (
                scope is not None
                and existing is not None
                and str(existing["workbench_scope"] or "") != scope
            ):
                # Moving a Workbench task between projects rotates both Hermes
                # transcript and long-term-memory identifiers. This prevents a
                # previous project's context from crossing the new boundary.
                conn.execute(
                    """
                    UPDATE hermes_session_mappings
                    SET workbench_scope = ?, hermes_session_id = ?,
                        hermes_session_key = ?, updated_at = ?
                    WHERE workbench_session_id = ?
                    """,
                    (scope, proposed_id, proposed_key, now, workbench_id),
                )
            row = conn.execute(
                "SELECT * FROM hermes_session_mappings WHERE workbench_session_id = ?",
                (workbench_id,),
            ).fetchone()
        if row is None:
            raise HermesConflictError("Hermes session mapping could not be created.")
        return self._session(row)

    def get_session(self, workbench_session_id: str) -> Optional[HermesSessionMapping]:
        workbench_id = _workbench_id(workbench_session_id, "Workbench session ID")
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM hermes_session_mappings WHERE workbench_session_id = ?",
                (workbench_id,),
            ).fetchone()
        return self._session(row) if row is not None else None

    def get_session_by_hermes_id(self, hermes_session_id: str) -> Optional[HermesSessionMapping]:
        upstream_id = _workbench_id(hermes_session_id, "Hermes session ID")
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM hermes_session_mappings WHERE hermes_session_id = ?",
                (upstream_id,),
            ).fetchone()
        return self._session(row) if row is not None else None

    def reserve_run(
        self,
        workbench_run_id: str,
        workbench_session_id: str,
        *,
        previous_response_id: str = "",
    ) -> HermesRunMapping:
        run_id = _workbench_id(workbench_run_id, "Workbench run ID")
        session_id = _workbench_id(workbench_session_id, "Workbench session ID")
        previous = str(previous_response_id or "").strip()
        if len(previous) > 256 or _CONTROL.search(previous):
            raise ValueError("Previous response ID is invalid.")
        now = _now()
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                INSERT OR IGNORE INTO hermes_run_mappings (
                    workbench_run_id, workbench_session_id, hermes_run_id,
                    status, previous_response_id, created_at, updated_at
                ) VALUES (?, ?, NULL, 'creating', ?, ?, ?)
                """,
                (run_id, session_id, previous or None, now, now),
            )
            row = conn.execute(
                "SELECT * FROM hermes_run_mappings WHERE workbench_run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise HermesConflictError("Hermes run mapping could not be reserved.")
        result = self._run(row)
        if result.workbench_session_id != session_id:
            raise HermesConflictError("Workbench run ID belongs to another session.")
        return result

    def bind_run(
        self,
        workbench_run_id: str,
        hermes_run_id: str,
        *,
        status: str,
    ) -> HermesRunMapping:
        workbench_id = _workbench_id(workbench_run_id, "Workbench run ID")
        upstream_id = _workbench_id(hermes_run_id, "Hermes run ID")
        safe_status = _workbench_id(status or "queued", "Hermes run status")[:64]
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            try:
                cursor = conn.execute(
                    """
                    UPDATE hermes_run_mappings
                    SET hermes_run_id = ?, status = ?, updated_at = ?
                    WHERE workbench_run_id = ?
                      AND (hermes_run_id IS NULL OR hermes_run_id = ?)
                    """,
                    (upstream_id, safe_status, _now(), workbench_id, upstream_id),
                )
            except Exception as exc:
                raise HermesConflictError("Hermes run ID is already mapped.") from exc
            row = conn.execute(
                "SELECT * FROM hermes_run_mappings WHERE workbench_run_id = ?",
                (workbench_id,),
            ).fetchone()
        if cursor.rowcount != 1 or row is None:
            raise HermesConflictError("Hermes run mapping changed unexpectedly.")
        return self._run(row)

    def update_status(self, workbench_run_id: str, status: str) -> HermesRunMapping:
        workbench_id = _workbench_id(workbench_run_id, "Workbench run ID")
        safe_status = _workbench_id(status, "Hermes run status")[:64]
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE hermes_run_mappings SET status = ?, updated_at = ?
                WHERE workbench_run_id = ?
                """,
                (safe_status, _now(), workbench_id),
            )
            row = conn.execute(
                "SELECT * FROM hermes_run_mappings WHERE workbench_run_id = ?",
                (workbench_id,),
            ).fetchone()
        if cursor.rowcount != 1 or row is None:
            raise KeyError(workbench_id)
        return self._run(row)

    def get_run(self, workbench_run_id: str) -> Optional[HermesRunMapping]:
        workbench_id = _workbench_id(workbench_run_id, "Workbench run ID")
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM hermes_run_mappings WHERE workbench_run_id = ?",
                (workbench_id,),
            ).fetchone()
        return self._run(row) if row is not None else None

    def get_run_by_hermes_id(self, hermes_run_id: str) -> Optional[HermesRunMapping]:
        upstream_id = _workbench_id(hermes_run_id, "Hermes run ID")
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM hermes_run_mappings WHERE hermes_run_id = ?",
                (upstream_id,),
            ).fetchone()
        return self._run(row) if row is not None else None
