import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping, Optional

from paths import DB_PATH as RUNTIME_DB_PATH, ensure_runtime_dirs
from structured_log import redact as redact_structured

DB_PATH = str(RUNTIME_DB_PATH)
DB_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_db_conn():
    ensure_runtime_dirs()
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _ensure_columns(cursor: sqlite3.Cursor, table: str, columns: Dict[str, str]) -> None:
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _loads_json(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _ensure_capability_audit_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS capability_audits (
            id TEXT PRIMARY KEY,
            capability_name TEXT NOT NULL,
            source TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            actor TEXT NOT NULL,
            project_id TEXT,
            session_id TEXT,
            run_id TEXT,
            decision TEXT NOT NULL,
            permission_mode TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'started',
            result_json TEXT,
            error_json TEXT,
            duration_ms INTEGER,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_capability_audits_created ON capability_audits(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_capability_audits_run ON capability_audits(run_id, created_at)")


def init_db():
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                root_path TEXT NOT NULL,
                root_kind TEXT NOT NULL DEFAULT 'linked',
                permission_mode TEXT NOT NULL DEFAULT 'read_only',
                path_status TEXT NOT NULL DEFAULT 'ready',
                expanded INTEGER NOT NULL DEFAULT 1,
                pinned INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'chat',
                model TEXT,
                project_id TEXT,
                status TEXT,
                pinned INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                message_count INTEGER DEFAULT 0,
                last_message_preview TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cursor.execute("PRAGMA table_info(projects)")
        legacy_project_columns = {row[1] for row in cursor.fetchall()}
        legacy_project_permissions_missing = "permission_mode" not in legacy_project_columns
        _ensure_columns(
            cursor,
            "projects",
            {
                "sort_order": "INTEGER NOT NULL DEFAULT 0",
                "root_kind": "TEXT NOT NULL DEFAULT 'linked'",
                "permission_mode": "TEXT NOT NULL DEFAULT 'read_only'",
                "path_status": "TEXT NOT NULL DEFAULT 'ready'",
            },
        )
        if legacy_project_permissions_missing:
            # Existing projects predate the permission selector and historically
            # had write access. Preserve their behaviour; only newly created
            # projects use the safer read-only default.
            cursor.execute("UPDATE projects SET permission_mode = 'workspace_write'")
        _ensure_columns(
            cursor,
            "sessions",
            {
                "mode": "TEXT NOT NULL DEFAULT 'chat'",
                "model": "TEXT",
                "message_count": "INTEGER DEFAULT 0",
                "last_message_preview": "TEXT",
                "updated_at": "TEXT",
                "project_id": "TEXT",
                "status": "TEXT",
                "pinned": "INTEGER NOT NULL DEFAULT 0",
                "archived": "INTEGER NOT NULL DEFAULT 0",
                "sort_order": "INTEGER NOT NULL DEFAULT 0",
            },
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                visible_content TEXT,
                llm_content TEXT,
                sources_json TEXT,
                process_events_json TEXT,
                artifacts_json TEXT,
                turn_id TEXT,
                parent_message_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE CASCADE
            )
            """
        )
        _ensure_columns(
            cursor,
            "messages",
            {
                "visible_content": "TEXT",
                "llm_content": "TEXT",
                "sources_json": "TEXT",
                "process_events_json": "TEXT",
                "artifacts_json": "TEXT",
                "turn_id": "TEXT",
                "parent_message_id": "INTEGER",
            },
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_type TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE CASCADE
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                mime_type TEXT,
                storage_path TEXT NOT NULL,
                status TEXT NOT NULL,
                chunk_count INTEGER DEFAULT 0,
                hash TEXT,
                project_id TEXT,
                session_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        _ensure_columns(cursor, "documents", {"project_id": "TEXT", "session_id": "TEXT"})

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ingest_jobs (
                job_id TEXT PRIMARY KEY,
                document_id TEXT,
                filename TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER DEFAULT 0,
                message TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS temporary_contexts (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                filename TEXT NOT NULL,
                text TEXT NOT NULL,
                chunk_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS attachments (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                project_id TEXT,
                filename TEXT,
                mime_type TEXT,
                storage_path TEXT NOT NULL,
                size_bytes INTEGER,
                width INTEGER,
                height INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_columns(cursor, "attachments", {"project_id": "TEXT"})

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                turn_id TEXT,
                title TEXT NOT NULL,
                type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS artifact_files (
                id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                path TEXT NOT NULL,
                content TEXT NOT NULL,
                language TEXT
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                project_id TEXT,
                retry_of_run_id TEXT,
                model TEXT,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                tasks_json TEXT,
                events_json TEXT,
                sources_json TEXT,
                metrics_json TEXT,
                artifacts_json TEXT,
                input_manifest_json TEXT,
                execution_revision INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        _ensure_columns(
            cursor,
            "runs",
            {
                "project_id": "TEXT",
                "retry_of_run_id": "TEXT",
                "input_manifest_json": "TEXT",
                "execution_revision": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_session_created "
            "ON runs(session_id, created_at DESC)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_retry_of "
            "ON runs(retry_of_run_id, created_at DESC)"
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS model_install_jobs (
                job_id TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER DEFAULT 0,
                downloaded_bytes INTEGER DEFAULT 0,
                total_bytes INTEGER DEFAULT 0,
                message TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS model_leases (
                model TEXT PRIMARY KEY,
                owner_type TEXT NOT NULL,
                run_id TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS automation_tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                project_id TEXT,
                input_json TEXT NOT NULL,
                max_attempts INTEGER NOT NULL DEFAULT 1,
                requires_approval INTEGER NOT NULL DEFAULT 0,
                risk_level TEXT NOT NULL DEFAULT 'low',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS automation_runs (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                result_json TEXT,
                error_json TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS automation_steps (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                input_json TEXT,
                output_json TEXT,
                error_json TEXT,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS approval_requests (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reason TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                decided_at TEXT,
                decided_by TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS automation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS capability_audits (
                id TEXT PRIMARY KEY,
                capability_name TEXT NOT NULL,
                source TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                actor TEXT NOT NULL,
                project_id TEXT,
                session_id TEXT,
                run_id TEXT,
                decision TEXT NOT NULL,
                permission_mode TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'started',
                result_json TEXT,
                error_json TEXT,
                duration_ms INTEGER,
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS capability_approvals (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                capability_name TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_at TEXT NOT NULL,
                decided_at TEXT,
                decided_by TEXT
            )
            """
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_automation_runs_task ON automation_runs(task_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_automation_events_run ON automation_events(run_id, id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_capability_audits_created ON capability_audits(created_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_capability_audits_run ON capability_audits(run_id, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_capability_approvals_run ON capability_approvals(run_id, requested_at)")

        now = _now()
        cursor.execute("UPDATE sessions SET created_at = COALESCE(created_at, ?), updated_at = COALESCE(updated_at, ?)", (now, now))
        cursor.execute("UPDATE messages SET visible_content = COALESCE(visible_content, content), llm_content = COALESCE(llm_content, content)")


def create_session(
    session_id: str,
    title: str = "New chat",
    mode: str = "chat",
    model: Optional[str] = None,
    project_id: Optional[str] = None,
) -> str:
    now = _now()
    with get_db_conn() as conn:
        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
            return session_id
        existing = conn.execute(
            """
            SELECT id FROM sessions
            WHERE project_id IS ? AND archived = 0
            ORDER BY pinned DESC,
                     CASE WHEN sort_order > 0 THEN 0 ELSE 1 END,
                     sort_order ASC, updated_at DESC, created_at DESC
            """,
            (project_id,),
        ).fetchall()
        for position, row in enumerate(existing, start=2):
            conn.execute("UPDATE sessions SET sort_order = ? WHERE id = ?", (position, row[0]))
        conn.execute(
            """
            INSERT OR IGNORE INTO sessions (id, title, mode, model, project_id, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, title or "New chat", mode, model, project_id, 1, now, now),
        )
    return session_id


def get_all_sessions(search_query: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = """
        SELECT s.*, p.name AS project_name,
               (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS actual_message_count
        FROM sessions s
        LEFT JOIN projects p ON p.id = s.project_id
    """
    params: tuple[Any, ...] = ()
    if search_query:
        sql += " WHERE s.title LIKE ? OR p.name LIKE ?"
        params = (f"%{search_query}%", f"%{search_query}%")
    sql += " ORDER BY s.pinned DESC, CASE WHEN s.sort_order > 0 THEN 0 ELSE 1 END, s.sort_order ASC, s.updated_at DESC, s.created_at DESC"
    with get_db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        sessions = []
        for row in rows:
            item = dict(row)
            item["message_count"] = item.get("actual_message_count") or item.get("message_count") or 0
            item["pinned"] = bool(item.get("pinned"))
            item["archived"] = bool(item.get("archived"))
            item.pop("actual_message_count", None)
            sessions.append(item)
        return sessions


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    with get_db_conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["pinned"] = bool(item.get("pinned"))
        item["archived"] = bool(item.get("archived"))
        return item


def delete_session(session_id: str) -> bool:
    try:
        with get_db_conn() as conn:
            conn.execute("DELETE FROM artifact_files WHERE artifact_id IN (SELECT id FROM artifacts WHERE session_id = ?)", (session_id,))
            conn.execute("DELETE FROM ingest_jobs WHERE document_id IN (SELECT id FROM documents WHERE session_id = ?)", (session_id,))
            conn.execute("DELETE FROM documents WHERE session_id = ?", (session_id,))
            for table in ("runs", "artifacts", "attachments", "temporary_contexts", "files", "messages"):
                conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
            cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return cur.rowcount > 0
    except Exception as e:
        print(f"[DB] Error deleting session {session_id}: {e}")
        return False


def add_message(
    session_id: str,
    role: str,
    content: str,
    visible_content: Optional[str] = None,
    llm_content: Optional[str] = None,
    sources: Optional[List[Dict[str, Any]]] = None,
    process_events: Optional[List[Dict[str, Any]]] = None,
    artifacts: Optional[List[Dict[str, Any]]] = None,
    turn_id: Optional[str] = None,
    parent_message_id: Optional[int] = None,
) -> int:
    visible = visible_content if visible_content is not None else content
    llm = llm_content if llm_content is not None else content
    with get_db_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO messages (
                session_id, role, content, visible_content, llm_content,
                sources_json, process_events_json, artifacts_json, turn_id, parent_message_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                role,
                content,
                visible,
                llm,
                json.dumps(sources or [], ensure_ascii=False),
                json.dumps(process_events or [], ensure_ascii=False),
                json.dumps(artifacts or [], ensure_ascii=False),
                turn_id,
                parent_message_id,
            ),
        )
        conn.execute(
            """
            UPDATE sessions
            SET message_count = (SELECT COUNT(*) FROM messages WHERE session_id = ?),
                last_message_preview = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (session_id, visible[:120], _now(), session_id),
        )
        return int(cur.lastrowid)


def get_messages_by_session(session_id: str) -> List[Dict[str, Any]]:
    with get_db_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, role, content, visible_content, llm_content,
                   sources_json, process_events_json, artifacts_json, turn_id, parent_message_id, created_at
            FROM messages
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()
        messages = []
        for row in rows:
            item = dict(row)
            item["visible_content"] = (item.get("content") or "") if item.get("visible_content") is None else item["visible_content"]
            item["llm_content"] = (item.get("content") or "") if item.get("llm_content") is None else item["llm_content"]
            item["sources"] = _loads_json(item.pop("sources_json", None), [])
            item["process_events"] = _loads_json(item.pop("process_events_json", None), [])
            item["artifacts"] = _loads_json(item.pop("artifacts_json", None), [])
            item["content"] = item["visible_content"]
            messages.append(item)
        return messages


def get_message(message_id: int) -> Optional[Dict[str, Any]]:
    """Return one persisted message using the same public shape as session history."""

    with get_db_conn() as conn:
        row = conn.execute(
            """
            SELECT id, session_id, role, content, visible_content, llm_content,
                   sources_json, process_events_json, artifacts_json, turn_id,
                   parent_message_id, created_at
            FROM messages WHERE id = ?
            """,
            (int(message_id),),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["visible_content"] = (
        item.get("content") or ""
        if item.get("visible_content") is None
        else item["visible_content"]
    )
    item["llm_content"] = (
        item.get("content") or ""
        if item.get("llm_content") is None
        else item["llm_content"]
    )
    item["sources"] = _loads_json(item.pop("sources_json", None), [])
    item["process_events"] = _loads_json(
        item.pop("process_events_json", None), []
    )
    item["artifacts"] = _loads_json(item.pop("artifacts_json", None), [])
    item["content"] = item["visible_content"]
    return item


def get_clean_history(session_id: str) -> List[Dict[str, str]]:
    return [
        {"role": m["role"], "content": m["llm_content"]}
        for m in get_messages_by_session(session_id)
        if m["role"] in {"user", "assistant", "system"} and m.get("llm_content")
    ]


def update_session_title(session_id: str, title: str) -> bool:
    try:
        with get_db_conn() as conn:
            conn.execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?", (title, _now(), session_id))
        return True
    except Exception as e:
        print(f"[DB] Error updating title: {e}")
        return False


def update_session_metadata(session_id: str, **changes: Any) -> bool:
    allowed = {"title", "mode", "model", "project_id", "status", "pinned", "archived"}
    values = {key: value for key, value in changes.items() if key in allowed}
    if not values:
        return False
    for key in ("pinned", "archived"):
        if key in values:
            values[key] = int(bool(values[key]))
    with get_db_conn() as conn:
        if "project_id" in values:
            values["sort_order"] = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM sessions WHERE project_id IS ?",
                (values["project_id"],),
            ).fetchone()[0]
        values["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = [*values.values(), session_id]
        cur = conn.execute(f"UPDATE sessions SET {assignments} WHERE id = ?", params)
        return cur.rowcount > 0


def create_project(
    project_id: str,
    name: str,
    root_path: str,
    root_kind: str = "linked",
    permission_mode: str = "read_only",
    path_status: str = "ready",
) -> Dict[str, Any]:
    now = _now()
    with get_db_conn() as conn:
        existing = conn.execute(
            """
            SELECT id FROM projects
            WHERE archived = 0
            ORDER BY pinned DESC,
                     CASE WHEN sort_order > 0 THEN 0 ELSE 1 END,
                     sort_order ASC, updated_at DESC, name ASC
            """
        ).fetchall()
        for position, row in enumerate(existing, start=2):
            conn.execute("UPDATE projects SET sort_order = ? WHERE id = ?", (position, row[0]))
        conn.execute(
            """
            INSERT INTO projects (
                id, name, root_path, root_kind, permission_mode, path_status,
                sort_order, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, name, root_path, root_kind, permission_mode, path_status, 1, now, now),
        )
    return get_project(project_id) or {}


def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    with get_db_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["expanded"] = bool(item.get("expanded"))
        item["pinned"] = bool(item.get("pinned"))
        item["archived"] = bool(item.get("archived"))
        return item


def get_project_by_root_path(root_path: str) -> Optional[Dict[str, Any]]:
    with get_db_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE lower(root_path) = lower(?)", (root_path,)).fetchone()
        return dict(row) if row else None


def get_projects(search_query: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = """
        SELECT p.*,
               COUNT(s.id) AS task_count,
               SUM(CASE WHEN s.archived = 0 THEN 1 ELSE 0 END) AS active_task_count
        FROM projects p
        LEFT JOIN sessions s ON s.project_id = p.id
    """
    params: tuple[Any, ...] = ()
    if search_query:
        sql += " WHERE p.name LIKE ?"
        params = (f"%{search_query}%",)
    sql += " GROUP BY p.id ORDER BY p.pinned DESC, CASE WHEN p.sort_order > 0 THEN 0 ELSE 1 END, p.sort_order ASC, p.updated_at DESC, p.name ASC"
    with get_db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        projects = []
        for row in rows:
            item = dict(row)
            item["expanded"] = bool(item.get("expanded"))
            item["pinned"] = bool(item.get("pinned"))
            item["archived"] = bool(item.get("archived"))
            item["task_count"] = int(item.get("task_count") or 0)
            item["active_task_count"] = int(item.get("active_task_count") or 0)
            projects.append(item)
        return projects


def update_project(project_id: str, **changes: Any) -> bool:
    allowed = {"name", "root_path", "root_kind", "permission_mode", "path_status", "expanded", "pinned", "archived"}
    values = {key: value for key, value in changes.items() if key in allowed}
    if not values:
        return False
    for key in ("expanded", "pinned", "archived"):
        if key in values:
            values[key] = int(bool(values[key]))
    values["updated_at"] = _now()
    assignments = ", ".join(f"{key} = ?" for key in values)
    params = [*values.values(), project_id]
    with get_db_conn() as conn:
        cur = conn.execute(f"UPDATE projects SET {assignments} WHERE id = ?", params)
        return cur.rowcount > 0


def delete_project(project_id: str) -> bool:
    with get_db_conn() as conn:
        session_ids = [row[0] for row in conn.execute(
            "SELECT id FROM sessions WHERE project_id = ? ORDER BY sort_order ASC, updated_at DESC",
            (project_id,),
        ).fetchall()]
        for session_id in session_ids:
            conn.execute("DELETE FROM artifact_files WHERE artifact_id IN (SELECT id FROM artifacts WHERE session_id = ?)", (session_id,))
            for table in ("runs", "artifacts", "attachments", "temporary_contexts", "files", "messages"):
                conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.execute("DELETE FROM ingest_jobs WHERE document_id IN (SELECT id FROM documents WHERE project_id = ?)", (project_id,))
        conn.execute("DELETE FROM documents WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM automation_tasks WHERE project_id = ?", (project_id,))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='extension_project_state'").fetchone(): conn.execute("DELETE FROM extension_project_state WHERE project_id = ?", (project_id,))
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cur.rowcount > 0

def reorder_projects(project_ids: List[str]) -> bool:
    if len(project_ids) != len(set(project_ids)):
        return False
    with get_db_conn() as conn:
        active_ids = {row[0] for row in conn.execute("SELECT id FROM projects WHERE archived = 0").fetchall()}
        if active_ids != set(project_ids):
            return False
        now = _now()
        for position, project_id in enumerate(project_ids, start=1):
            conn.execute(
                "UPDATE projects SET sort_order = ?, updated_at = ? WHERE id = ?",
                (position, now, project_id),
            )
    return True


def reorder_sessions(session_ids: List[str], project_id: Optional[str]) -> bool:
    if len(session_ids) != len(set(session_ids)):
        return False
    with get_db_conn() as conn:
        existing = {row[0] for row in conn.execute(
            f"SELECT id FROM sessions WHERE archived = 0 AND id IN ({','.join('?' for _ in session_ids)})",
            session_ids,
        ).fetchall()} if session_ids else set()
        if existing != set(session_ids):
            return False
        current_target_ids = {row[0] for row in conn.execute(
            "SELECT id FROM sessions WHERE archived = 0 AND project_id IS ?",
            (project_id,),
        ).fetchall()}
        if not current_target_ids.issubset(existing):
            return False
        now = _now()
        for position, session_id in enumerate(session_ids, start=1):
            conn.execute(
                "UPDATE sessions SET project_id = ?, sort_order = ?, updated_at = ? WHERE id = ?",
                (project_id, position, now, session_id),
            )
    return True


def add_file(session_id: str, filename: str, file_path: str, file_type: str):
    with get_db_conn() as conn:
        conn.execute(
            "INSERT INTO files (session_id, filename, file_path, file_type) VALUES (?, ?, ?, ?)",
            (session_id, filename, file_path, file_type),
        )


def get_session_files(session_id: str) -> List[Dict[str, Any]]:
    with get_db_conn() as conn:
        rows = conn.execute("SELECT * FROM files WHERE session_id = ?", (session_id,)).fetchall()
        return [dict(row) for row in rows]


def upsert_document(document_id: str, filename: str, storage_path: str, status: str, chunk_count: int = 0, mime_type: Optional[str] = None, file_hash: Optional[str] = None, project_id: Optional[str] = None, session_id: Optional[str] = None) -> None:
    now = _now()
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO documents (id, filename, mime_type, storage_path, status, chunk_count, hash, project_id, session_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                filename=excluded.filename,
                mime_type=excluded.mime_type,
                storage_path=excluded.storage_path,
                status=excluded.status,
                chunk_count=excluded.chunk_count,
                hash=excluded.hash,
                project_id=COALESCE(excluded.project_id, documents.project_id),
                session_id=COALESCE(excluded.session_id, documents.session_id),
                updated_at=excluded.updated_at
            """,
            (document_id, filename, mime_type, storage_path, status, chunk_count, file_hash, project_id, session_id, now, now),
        )


def update_document_storage(document_id: str, storage_path: str, project_id: Optional[str], session_id: Optional[str]) -> None:
    with get_db_conn() as conn:
        conn.execute("UPDATE documents SET storage_path = ?, project_id = ?, session_id = ?, updated_at = ? WHERE id = ?", (storage_path, project_id, session_id, _now(), document_id))


def get_documents(project_id: Optional[str] = None, *, filter_by_project: bool = False) -> List[Dict[str, Any]]:
    with get_db_conn() as conn:
        if filter_by_project:
            rows = conn.execute("SELECT * FROM documents WHERE project_id IS ? ORDER BY updated_at DESC", (project_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM documents ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]


def get_document(document_id: str) -> Optional[Dict[str, Any]]:
    with get_db_conn() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        return dict(row) if row else None


def delete_document_record(document_id: str) -> bool:
    with get_db_conn() as conn:
        cur = conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        return cur.rowcount > 0


def upsert_ingest_job(job_id: str, filename: str, status: str, progress: int, document_id: Optional[str] = None, message: Optional[str] = None, error: Optional[str] = None) -> None:
    now = _now()
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO ingest_jobs (job_id, document_id, filename, status, progress, message, error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                document_id=excluded.document_id,
                status=excluded.status,
                progress=excluded.progress,
                message=excluded.message,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (job_id, document_id, filename, status, progress, message, error, now, now),
        )


def get_ingest_job(job_id: str) -> Optional[Dict[str, Any]]:
    with get_db_conn() as conn:
        row = conn.execute("SELECT * FROM ingest_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def save_temporary_context(context_id: str, session_id: Optional[str], filename: str, text: str, chunk_count: int, expires_at: Optional[str] = None) -> None:
    expires = expires_at or (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO temporary_contexts (id, session_id, filename, text, chunk_count, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (context_id, session_id, filename, text, chunk_count, _now(), expires),
        )


def get_temporary_context(context_id: str) -> Optional[Dict[str, Any]]:
    with get_db_conn() as conn:
        row = conn.execute("SELECT * FROM temporary_contexts WHERE id = ?", (context_id,)).fetchone()
        return dict(row) if row else None


def delete_temporary_context(context_id: str) -> bool:
    with get_db_conn() as conn:
        cur = conn.execute("DELETE FROM temporary_contexts WHERE id = ?", (context_id,))
        return cur.rowcount > 0


def save_attachment(attachment_id: str, session_id: Optional[str], filename: Optional[str], mime_type: str, storage_path: str, size_bytes: int, width: Optional[int] = None, height: Optional[int] = None, project_id: Optional[str] = None) -> None:
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO attachments (id, session_id, project_id, filename, mime_type, storage_path, size_bytes, width, height, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (attachment_id, session_id, project_id, filename, mime_type, storage_path, size_bytes, width, height, _now()),
        )


def get_attachment(attachment_id: str) -> Optional[Dict[str, Any]]:
    with get_db_conn() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
        return dict(row) if row else None


def get_all_attachments() -> List[Dict[str, Any]]:
    with get_db_conn() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM attachments ORDER BY created_at").fetchall()]


def update_attachment_storage(attachment_id: str, storage_path: str, project_id: Optional[str]) -> None:
    with get_db_conn() as conn:
        conn.execute("UPDATE attachments SET storage_path = ?, project_id = ? WHERE id = ?", (storage_path, project_id, attachment_id))


def rebase_session_storage_paths(session_id: str, old_root: str, new_root: str, project_id: Optional[str] = None) -> None:
    old_prefix = str(old_root).rstrip("\\/")
    with get_db_conn() as conn:
        for table in ("attachments", "documents"):
            rows = conn.execute(f"SELECT id, storage_path FROM {table} WHERE session_id = ?", (session_id,)).fetchall()
            for row in rows:
                current = str(row["storage_path"])
                if current.lower().startswith(old_prefix.lower()):
                    replacement = str(new_root) + current[len(old_prefix):]
                    conn.execute(f"UPDATE {table} SET storage_path = ?, project_id = ? WHERE id = ?", (replacement, project_id, row["id"]))


def save_artifact(artifact_id: str, session_id: str, turn_id: Optional[str], title: str, artifact_type: str, files: List[Dict[str, Any]]) -> None:
    now = _now()
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO artifacts (id, session_id, turn_id, title, type, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM artifacts WHERE id = ?), ?), ?)
            """,
            (artifact_id, session_id, turn_id, title, artifact_type, artifact_id, now, now),
        )
        conn.execute("DELETE FROM artifact_files WHERE artifact_id = ?", (artifact_id,))
        for item in files:
            conn.execute(
                """
                INSERT INTO artifact_files (id, artifact_id, path, content, language)
                VALUES (?, ?, ?, ?, ?)
                """,
                (f"{artifact_id}:{item.get('path')}", artifact_id, item.get("path"), item.get("content", ""), item.get("language")),
            )


def get_artifact(artifact_id: str) -> Optional[Dict[str, Any]]:
    with get_db_conn() as conn:
        art = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if not art:
            return None
        files = conn.execute("SELECT path, content, language FROM artifact_files WHERE artifact_id = ? ORDER BY path", (artifact_id,)).fetchall()
        item = dict(art)
        item["artifact_id"] = item.pop("id")
        item["files"] = [dict(row) for row in files]
        return item


def upsert_run(
    run_id: str,
    session_id: str,
    turn_id: str,
    model: Optional[str],
    mode: str,
    status: str,
    tasks: Optional[List[Dict[str, Any]]] = None,
    events: Optional[List[Dict[str, Any]]] = None,
    sources: Optional[List[Dict[str, Any]]] = None,
    metrics: Optional[Dict[str, Any]] = None,
    artifacts: Optional[List[Dict[str, Any]]] = None,
    completed_at: Optional[str] = None,
    project_id: Optional[str] = None,
    retry_of_run_id: Optional[str] = None,
    input_manifest: Optional[Dict[str, Any]] = None,
) -> None:
    now = _now()
    with get_db_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if existing is not None:
            if str(existing["session_id"]) != str(session_id):
                raise ValueError("Run session binding cannot be changed.")
            if str(existing["turn_id"]) != str(turn_id):
                raise ValueError("Run turn binding cannot be changed.")
            stored_project = existing["project_id"]
            if project_id is not None and stored_project != project_id:
                raise ValueError("Run project binding cannot be changed.")
            stored_retry = existing["retry_of_run_id"]
            if retry_of_run_id is not None and stored_retry != retry_of_run_id:
                raise ValueError("Run retry lineage cannot be changed.")

        def encoded(name: str, value: Any, default: Any) -> str:
            if value is not None:
                return json.dumps(value, ensure_ascii=False)
            if existing is not None and existing[name] is not None:
                return str(existing[name])
            return json.dumps(default, ensure_ascii=False)

        tasks_json = encoded("tasks_json", tasks, [])
        # Public execution events are append-only once a run exists.  Runtime
        # transitions (notably Hermes -> basic-chat fallback) must not erase
        # events that were already accepted through append_run_event().
        events_json = (
            str(existing["events_json"])
            if existing is not None and existing["events_json"] is not None
            else encoded("events_json", events, [])
        )
        sources_json = encoded("sources_json", sources, [])
        if existing is not None and not sources:
            existing_sources = _loads_json(existing["sources_json"], [])
            if isinstance(existing_sources, list) and existing_sources:
                # Source provenance is evidence of what entered the run.  An
                # empty fallback update is not authority to discard it.
                sources_json = str(existing["sources_json"])
        metrics_json = encoded("metrics_json", metrics, {})
        artifacts_json = encoded("artifacts_json", artifacts, [])
        input_manifest_json = encoded(
            "input_manifest_json", input_manifest, {}
        )
        conn.execute(
            """
            INSERT INTO runs (
                id, session_id, turn_id, project_id, retry_of_run_id, model,
                mode, status, tasks_json, events_json, sources_json, metrics_json,
                artifacts_json, input_manifest_json, execution_revision,
                created_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                model=excluded.model,
                mode=excluded.mode,
                status=excluded.status,
                tasks_json=excluded.tasks_json,
                events_json=excluded.events_json,
                sources_json=excluded.sources_json,
                metrics_json=excluded.metrics_json,
                artifacts_json=excluded.artifacts_json,
                input_manifest_json=excluded.input_manifest_json,
                completed_at=excluded.completed_at
            """,
            (
                run_id,
                session_id,
                turn_id,
                project_id if existing is None else existing["project_id"],
                (
                    retry_of_run_id
                    if existing is None
                    else existing["retry_of_run_id"]
                ),
                model,
                mode,
                status,
                tasks_json,
                events_json,
                sources_json,
                metrics_json,
                artifacts_json,
                input_manifest_json,
                int(existing["execution_revision"] or 0) if existing else 0,
                now,
                completed_at,
            ),
        )


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    with get_db_conn() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["run_id"] = item.pop("id")
        item["tasks"] = _loads_json(item.pop("tasks_json", None), [])
        item["events"] = _loads_json(item.pop("events_json", None), [])
        item["sources"] = _loads_json(item.pop("sources_json", None), [])
        item["metrics"] = _loads_json(item.pop("metrics_json", None), {})
        item["artifacts"] = _loads_json(item.pop("artifacts_json", None), [])
        manifest = _loads_json(item.pop("input_manifest_json", None), {})
        item["input_manifest"] = public_run_input_manifest(manifest)
        return item


def public_run_input_manifest(manifest: Any) -> Dict[str, Any]:
    """Return reproducibility metadata without prompt/context/image contents."""

    raw = manifest if isinstance(manifest, dict) else {}
    attachment_ids = [
        str(value)
        for value in raw.get("attachment_ids") or []
        if isinstance(value, str) and value
    ][:100]
    return {
        "version": int(raw.get("version") or 0),
        "reproducible": bool(raw.get("reproducible")),
        "reason": str(raw.get("reason") or "")[:128] or None,
        "attachment_ids": attachment_ids,
        "attachment_count": len(attachment_ids),
        "temporary_context_id": (
            str(raw.get("temporary_context_id"))[:256]
            if raw.get("temporary_context_id")
            else None
        ),
        "has_temporary_context": bool(
            raw.get("temporary_context_id") or raw.get("temporary_context")
        ),
        "inline_image_count": max(
            0, min(int(raw.get("inline_image_count") or 0), 100)
        ),
    }


def get_run_input_manifest(run_id: str) -> Dict[str, Any]:
    """Return the private retry manifest to trusted in-process callers only."""

    with get_db_conn() as conn:
        row = conn.execute(
            "SELECT input_manifest_json FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
    return _loads_json(row["input_manifest_json"], {}) if row else {}


def list_session_runs(
    session_id: str,
    *,
    project_id: Optional[str],
    limit: int = 1,
) -> List[Dict[str, Any]]:
    """List only runs still bound to the session's current project scope."""

    safe_limit = max(1, min(int(limit), 100))
    with get_db_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, project_id, status, model, mode, created_at,
                   completed_at, retry_of_run_id
            FROM runs
            WHERE session_id = ? AND project_id IS ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (session_id, project_id, safe_limit),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["run_id"] = item.pop("id")
        result.append(item)
    return result


_RUN_EVENT_FIELDS: Dict[str, tuple[str, ...]] = {
    "meta": (
        "run_id", "session_id", "turn_id", "project_id", "model", "mode",
        "runtime", "project_skill_count", "retry_of_run_id",
    ),
    "metrics": (
        "runtime", "elapsed_ms", "first_token_ms", "token_chars",
        "tokens_per_second",
    ),
    "done": ("run_id", "session_id", "turn_id"),
    "error": ("code", "message", "recoverable"),
    "cancelled": (
        "run_id", "session_id", "turn_id", "message", "recoverable",
        "deadline_exceeded",
    ),
    "tool_start": (
        "tool", "tool_call_id", "run_id", "project_id", "args",
    ),
    "tool_end": (
        "tool", "tool_call_id", "run_id", "project_id", "success",
        "result", "details_redacted", "duration_ms",
    ),
    "approval_required": (
        "approval_id", "capability", "message", "summary", "run_id", "risk",
        "status", "choices", "updated_at", "rationale",
    ),
    "artifact": (
        "artifact_id", "title", "artifact_type", "status", "relative_path",
        "language", "size_bytes", "sha256",
    ),
    "artifact_update": (
        "artifact_id", "title", "artifact_type", "status", "relative_path",
        "language", "size_bytes", "sha256",
    ),
    "file_change": (
        "change_id", "relative_path", "change_type", "status", "additions",
        "deletions", "sha256",
    ),
    "file_changed": (
        "change_id", "relative_path", "change_type", "status", "additions",
        "deletions", "sha256",
    ),
    "file_written": (
        "change_id", "relative_path", "change_type", "status", "additions",
        "deletions", "sha256",
    ),
    "validation": (
        "validation_id", "name", "status", "passed", "failed", "skipped",
        "duration_ms", "summary",
    ),
    "test_result": (
        "validation_id", "name", "status", "passed", "failed", "skipped",
        "duration_ms", "summary",
    ),
    "git_commit": (
        "commit_sha", "short_sha", "branch", "status", "success",
    ),
    "git_push": (
        "commit_sha", "short_sha", "branch", "remote", "status", "success",
    ),
}
_RUN_EVENT_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:@/-"
)
_UNSET_RUN_BINDING = object()


def _safe_relative_event_path(value: Any) -> str:
    text = str(value or "")
    parsed = PurePosixPath(text)
    parts = text.split("/")
    if (
        not text
        or len(text) > 240
        or text != text.strip()
        or "\\" in text
        or ":" in text
        or text.startswith("/")
        or parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("Run event contains an unsafe relative path.")
    return text


def _validate_run_event_binding(
    payload: Mapping[str, Any],
    *,
    run_id: Any = _UNSET_RUN_BINDING,
    session_id: Any = _UNSET_RUN_BINDING,
    project_id: Any = _UNSET_RUN_BINDING,
) -> None:
    for key, expected in (
        ("run_id", run_id),
        ("session_id", session_id),
        ("project_id", project_id),
    ):
        if expected is _UNSET_RUN_BINDING or key not in payload:
            continue
        supplied = payload.get(key)
        if supplied is None:
            continue
        if expected is None or str(supplied) != str(expected):
            raise ValueError(f"Run event contains a mismatched {key} binding.")


def _safe_public_event_text(value: Any, *, key: str, limit: int = 1000) -> str:
    raw = "".join(
        character for character in str(value or "")[:limit] if ord(character) >= 32
    )
    public = redact_structured(raw, key=key)
    return str(public if isinstance(public, str) else "")[:limit]


def _safe_run_event_payload(
    event: str,
    payload: Any,
    *,
    run_id: Any = _UNSET_RUN_BINDING,
    session_id: Any = _UNSET_RUN_BINDING,
    project_id: Any = _UNSET_RUN_BINDING,
) -> Dict[str, Any]:
    if event not in _RUN_EVENT_FIELDS or not isinstance(payload, Mapping):
        raise ValueError("Run event is not part of the public execution contract.")
    _validate_run_event_binding(
        payload,
        run_id=run_id,
        session_id=session_id,
        project_id=project_id,
    )
    result: Dict[str, Any] = {}
    for key in (*_RUN_EVENT_FIELDS[event], "event_key"):
        if key not in payload or payload[key] is None:
            continue
        value = payload[key]
        if key == "relative_path":
            result[key] = _safe_relative_event_path(value)
        elif key == "args":
            # Tool arguments are never persisted.  Only this fixed marker is
            # accepted from the already-sanitized Hermes tool bridge.
            result[key] = {
                "scope": "active_project",
                "access": "read_only",
                "details_redacted": True,
            }
        elif isinstance(value, str):
            raw_text = "".join(
                char for char in value[:1000] if ord(char) >= 32
            )
            safe_text = _safe_public_event_text(value, key=key)
            if key in {"commit_sha", "short_sha", "sha256"} and (
                not raw_text
                or any(
                    char not in "0123456789abcdefABCDEF" for char in raw_text
                )
            ):
                raise ValueError("Run event contains an invalid digest.")
            if key in {"branch", "remote"} and (
                not raw_text
                or len(raw_text) > 128
                or any(
                    char
                    not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-"
                    for char in raw_text
                )
                or raw_text.startswith(("/", "-"))
                or ".." in raw_text
            ):
                raise ValueError("Run event contains an invalid Git identifier.")
            if key == "event_key" and (
                not raw_text
                or len(raw_text) > 128
                or raw_text[0] not in _RUN_EVENT_KEY_CHARS
                or any(char not in _RUN_EVENT_KEY_CHARS for char in raw_text)
                or ".." in raw_text
            ):
                raise ValueError("Run event contains an invalid event key.")
            # Strict identifiers are omitted if they happen to match a runtime
            # registered secret.  Free-form labels retain the useful marker.
            if safe_text != raw_text and key in {
                "commit_sha",
                "short_sha",
                "sha256",
                "branch",
                "remote",
                "event_key",
            }:
                continue
            result[key] = safe_text
        elif isinstance(value, bool):
            result[key] = value
        elif isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Run event contains a non-finite number.")
            result[key] = value
        elif key == "choices" and isinstance(value, (list, tuple)):
            result[key] = [
                choice for choice in value if choice in {"once", "deny"}
            ]
    return result


def public_run_events(
    events: Any,
    *,
    run_id: str,
    session_id: str,
    project_id: Optional[str],
) -> List[Dict[str, Any]]:
    """Fail-closed projection for persisted and legacy execution events.

    Older rows may predate append_run_event(), so every read is projected
    through the current allowlist and binding checks before entering the UI.
    Unknown event types, malformed records, and mismatched bindings are omitted.
    """

    if not isinstance(events, (list, tuple)):
        return []
    public: List[Dict[str, Any]] = []
    seen_event_keys: set[str] = set()
    last_sequence = 0
    for raw in list(events)[-500:]:
        if not isinstance(raw, Mapping):
            continue
        event_name = str(raw.get("event") or raw.get("type") or "").strip().casefold()
        payload = raw.get("payload")
        if not isinstance(payload, Mapping):
            payload = raw
        try:
            _validate_run_event_binding(
                raw,
                run_id=run_id,
                session_id=session_id,
                project_id=project_id,
            )
            safe_payload = _safe_run_event_payload(
                event_name,
                payload,
                run_id=run_id,
                session_id=session_id,
                project_id=project_id,
            )
        except (TypeError, ValueError):
            continue
        event_key = str(safe_payload.get("event_key") or "")
        if event_key and event_key in seen_event_keys:
            continue
        if event_key:
            seen_event_keys.add(event_key)
        sequence = raw.get("sequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence <= last_sequence
        ):
            sequence = last_sequence + 1
        last_sequence = sequence
        record: Dict[str, Any] = {
            "event": event_name,
            "sequence": sequence,
            "payload": safe_payload,
        }
        if raw.get("created_at") is not None:
            created_at = _safe_public_event_text(
                raw.get("created_at"), key="created_at", limit=80
            )
            if created_at:
                record["created_at"] = created_at
        public.append(record)
    return public


def append_run_event(
    run_id: str,
    event: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Atomically append one bounded, allowlisted public execution event."""

    event_name = str(event or "").strip().casefold()
    with get_db_conn() as conn:
        row = conn.execute(
            """
            SELECT session_id, project_id, events_json, execution_revision
            FROM runs WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if not row:
            raise KeyError(run_id)
        safe_payload = _safe_run_event_payload(
            event_name,
            payload,
            run_id=run_id,
            session_id=str(row["session_id"]),
            project_id=row["project_id"],
        )
        events = _loads_json(row["events_json"], [])
        if not isinstance(events, list):
            events = []
        event_key = str(safe_payload.get("event_key") or "")
        if event_key:
            for existing in events:
                projected = public_run_events(
                    [existing],
                    run_id=run_id,
                    session_id=str(row["session_id"]),
                    project_id=row["project_id"],
                )
                if not projected:
                    continue
                current = projected[0]
                if current["payload"].get("event_key") != event_key:
                    continue
                if current["event"] != event_name or current["payload"] != safe_payload:
                    raise ValueError("Run event key was reused with different content.")
                return current
        sequence = int(row["execution_revision"] or 0) + 1
        record = {
            "event": event_name,
            "sequence": sequence,
            "created_at": _now(),
            "payload": safe_payload,
        }
        events.append(record)
        # A direct chat run has few events.  The bound prevents a hostile or
        # broken sidecar from growing the Workbench database without limit.
        events = events[-500:]
        conn.execute(
            """
            UPDATE runs SET events_json = ?, execution_revision = ? WHERE id = ?
            """,
            (json.dumps(events, ensure_ascii=False), sequence, run_id),
        )
    return record


def get_resource_calibration_ratios(signature: str, limit: int = 30) -> List[float]:
    if not signature:
        return []
    ratios: List[float] = []
    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT metrics_json FROM runs WHERE status = 'completed' ORDER BY completed_at DESC LIMIT ?",
            (max(1, min(200, int(limit) * 5)),),
        ).fetchall()
    for row in rows:
        metrics = _loads_json(row["metrics_json"], {})
        resource = metrics.get("resource_monitor") if isinstance(metrics, dict) else None
        if not isinstance(resource, dict) or resource.get("signature") != signature:
            continue
        ratio = resource.get("observed_ratio")
        if isinstance(ratio, (int, float)) and 0.1 <= float(ratio) <= 3.0:
            ratios.append(float(ratio))
        if len(ratios) >= limit:
            break
    return ratios


def upsert_model_lease(model: str, owner_type: str, run_id: Optional[str] = None) -> None:
    normalized = str(model or "").strip()
    if not normalized:
        return
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO model_leases (model, owner_type, run_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(model) DO UPDATE SET
                owner_type=excluded.owner_type,
                run_id=excluded.run_id,
                updated_at=excluded.updated_at
            """,
            (normalized, str(owner_type), run_id, _now()),
        )


def get_model_lease(model: str) -> Optional[Dict[str, Any]]:
    with get_db_conn() as conn:
        row = conn.execute(
            "SELECT model, owner_type, run_id, updated_at FROM model_leases WHERE model = ?",
            (str(model or "").strip(),),
        ).fetchone()
    return dict(row) if row else None


def list_model_leases() -> List[Dict[str, Any]]:
    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT model, owner_type, run_id, updated_at FROM model_leases ORDER BY model"
        ).fetchall()
    return [dict(row) for row in rows]


def delete_model_lease(model: str) -> None:
    with get_db_conn() as conn:
        conn.execute("DELETE FROM model_leases WHERE model = ?", (str(model or "").strip(),))


def create_capability_approval(
    approval_id: str,
    *,
    run_id: str,
    capability_name: str,
    risk_level: str,
    reason: str,
) -> None:
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO capability_approvals (
                id, run_id, capability_name, risk_level, reason, status, requested_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (approval_id, run_id, capability_name, risk_level, reason, _now()),
        )


def decide_capability_approval(approval_id: str, approved: bool, *, decided_by: str) -> bool:
    with get_db_conn() as conn:
        cursor = conn.execute(
            """
            UPDATE capability_approvals
            SET status = ?, decided_at = ?, decided_by = ?
            WHERE id = ? AND status = 'pending'
            """,
            ("approved" if approved else "rejected", _now(), decided_by, approval_id),
        )
        return cursor.rowcount == 1


def expire_capability_approval(approval_id: str) -> bool:
    with get_db_conn() as conn:
        cursor = conn.execute(
            """
            UPDATE capability_approvals
            SET status = 'expired', decided_at = ?, decided_by = 'system_timeout'
            WHERE id = ? AND status = 'pending'
            """,
            (_now(), approval_id),
        )
        return cursor.rowcount == 1


def list_capability_approvals(
    *,
    run_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    clauses, params = [], []
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(500, int(limit))))
    with get_db_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM capability_approvals{where} ORDER BY requested_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_usage_ledger(
    *,
    project_id: Optional[str] = None,
    since: Optional[str] = None,
) -> Dict[str, Any]:
    query = """
        SELECT r.id, r.session_id, r.model, r.status, r.metrics_json, r.created_at,
               s.project_id
        FROM runs r
        LEFT JOIN sessions s ON s.id = r.session_id
        WHERE 1=1
    """
    params: List[Any] = []
    if project_id is not None:
        query += " AND s.project_id = ?"
        params.append(project_id)
    if since:
        query += " AND r.created_at >= ?"
        params.append(since)
    query += " ORDER BY r.created_at DESC"
    with get_db_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "load_duration_ns": 0,
        "eval_duration_ns": 0,
        "runs": 0,
        "estimated_cost": 0.0,
    }
    currencies: Dict[str, float] = {}
    by_provider: Dict[str, Dict[str, Any]] = {}
    by_model: Dict[str, Dict[str, Any]] = {}
    by_role: Dict[str, Dict[str, Any]] = {}
    planner_schema = {"attempts": 0, "successes": 0, "repaired": 0}
    for row in rows:
        metrics = _loads_json(row["metrics_json"], {})
        planner_metric = metrics.get("planner_schema") if isinstance(metrics, dict) else None
        if isinstance(planner_metric, dict) and planner_metric.get("attempted"):
            planner_schema["attempts"] += 1
            planner_schema["successes"] += int(bool(planner_metric.get("success")))
            planner_schema["repaired"] += int(bool(planner_metric.get("repaired")))
        usage = metrics.get("usage") if isinstance(metrics, dict) else None
        if not isinstance(usage, dict):
            continue
        totals["runs"] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "load_duration_ns", "eval_duration_ns"):
            totals[key] += max(0, int(usage.get(key) or 0))
        cost = max(0.0, float(usage.get("estimated_cost") or 0.0))
        currency = str(usage.get("currency") or "USD")
        provider = str(usage.get("provider") or "ollama")
        totals["estimated_cost"] += cost
        currencies[currency] = currencies.get(currency, 0.0) + cost
        provider_bucket = by_provider.setdefault(
            provider,
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "runs": 0, "estimated_cost": 0.0},
        )
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            provider_bucket[key] += max(0, int(usage.get(key) or 0))
        provider_bucket["runs"] += 1
        provider_bucket["estimated_cost"] += cost
        for entry in usage.get("by_agent") or []:
            if not isinstance(entry, dict):
                continue
            model = str(entry.get("model") or row["model"] or "unknown")
            role = str(entry.get("role") or "unknown")
            for bucket, name in ((by_model, model), (by_role, role)):
                current = bucket.setdefault(name, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0})
                current["prompt_tokens"] += max(0, int(entry.get("prompt_tokens") or 0))
                current["completion_tokens"] += max(0, int(entry.get("completion_tokens") or 0))
                current["total_tokens"] += max(0, int(entry.get("total_tokens") or 0))
                current["calls"] += 1
    planner_schema["success_rate"] = (
        round(planner_schema["successes"] / planner_schema["attempts"], 4)
        if planner_schema["attempts"] else None
    )
    return {
        "totals": totals,
        "cost_by_currency": {key: round(value, 8) for key, value in currencies.items()},
        "by_provider": by_provider,
        "by_model": by_model,
        "by_role": by_role,
        "planner_schema": planner_schema,
    }


def create_automation_task(task_id: str, definition: Dict[str, Any]) -> None:
    now = _now()
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO automation_tasks (
                id, title, kind, project_id, input_json, max_attempts,
                requires_approval, risk_level, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                definition["title"],
                definition["kind"],
                definition.get("project_id"),
                json.dumps(definition.get("input") or {}, ensure_ascii=False),
                definition.get("max_attempts", 1),
                int(bool(definition.get("requires_approval"))),
                definition.get("risk_level", "low"),
                now,
                now,
            ),
        )


def _automation_task(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["input"] = _loads_json(item.pop("input_json", None), {})
    item["requires_approval"] = bool(item.get("requires_approval"))
    return item


def get_automation_task(task_id: str) -> Optional[Dict[str, Any]]:
    with get_db_conn() as conn:
        row = conn.execute("SELECT * FROM automation_tasks WHERE id = ?", (task_id,)).fetchone()
        return _automation_task(row) if row else None


def create_automation_run(run_id: str, task_id: str, status: str) -> None:
    with get_db_conn() as conn:
        conn.execute(
            "INSERT INTO automation_runs (id, task_id, status, created_at) VALUES (?, ?, ?, ?)",
            (run_id, task_id, status, _now()),
        )


def _automation_run(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["result"] = _loads_json(item.pop("result_json", None), None)
    item["error"] = _loads_json(item.pop("error_json", None), None)
    return item


def get_automation_run(run_id: str, include_task: bool = False) -> Optional[Dict[str, Any]]:
    with get_db_conn() as conn:
        row = conn.execute("SELECT * FROM automation_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        item = _automation_run(row)
        if include_task:
            task_row = conn.execute("SELECT * FROM automation_tasks WHERE id = ?", (item["task_id"],)).fetchone()
            item["task"] = _automation_task(task_row) if task_row else None
        steps = conn.execute("SELECT * FROM automation_steps WHERE run_id = ? ORDER BY sequence, id", (run_id,)).fetchall()
        item["steps"] = []
        for step_row in steps:
            step = dict(step_row)
            step["input"] = _loads_json(step.pop("input_json", None), {})
            step["output"] = _loads_json(step.pop("output_json", None), None)
            step["error"] = _loads_json(step.pop("error_json", None), None)
            item["steps"].append(step)
        approval = conn.execute("SELECT * FROM approval_requests WHERE run_id = ? ORDER BY requested_at DESC LIMIT 1", (run_id,)).fetchone()
        item["approval"] = dict(approval) if approval else None
        return item


def list_automation_runs(limit: int = 50) -> List[Dict[str, Any]]:
    with get_db_conn() as conn:
        rows = conn.execute("SELECT * FROM automation_runs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
    return [get_automation_run(row["id"], include_task=True) for row in rows]


def update_automation_run(run_id: str, **changes: Any) -> bool:
    allowed = {"status", "attempt", "started_at", "completed_at"}
    values = {key: value for key, value in changes.items() if key in allowed}
    if "result" in changes:
        values["result_json"] = json.dumps(changes["result"], ensure_ascii=False)
    if "error" in changes:
        values["error_json"] = json.dumps(changes["error"], ensure_ascii=False) if changes["error"] is not None else None
    if not values:
        return False
    with get_db_conn() as conn:
        assignments = ", ".join(f"{key} = ?" for key in values)
        cur = conn.execute(f"UPDATE automation_runs SET {assignments} WHERE id = ?", (*values.values(), run_id))
        return cur.rowcount > 0


def create_automation_step(step_id: str, run_id: str, name: str, kind: str, sequence: int, input_data: Dict[str, Any]) -> None:
    with get_db_conn() as conn:
        conn.execute(
            "INSERT INTO automation_steps (id, run_id, name, kind, sequence, input_json) VALUES (?, ?, ?, ?, ?, ?)",
            (step_id, run_id, name, kind, sequence, json.dumps(input_data, ensure_ascii=False)),
        )


def update_automation_step(step_id: str, **changes: Any) -> bool:
    allowed = {"status", "started_at", "completed_at"}
    values = {key: value for key, value in changes.items() if key in allowed}
    for key in ("output", "error"):
        if key in changes:
            values[f"{key}_json"] = json.dumps(changes[key], ensure_ascii=False) if changes[key] is not None else None
    with get_db_conn() as conn:
        assignments = ", ".join(f"{key} = ?" for key in values)
        cur = conn.execute(f"UPDATE automation_steps SET {assignments} WHERE id = ?", (*values.values(), step_id))
        return cur.rowcount > 0


def create_approval_request(approval_id: str, run_id: str, reason: str) -> None:
    with get_db_conn() as conn:
        conn.execute(
            "INSERT INTO approval_requests (id, run_id, reason, requested_at) VALUES (?, ?, ?, ?)",
            (approval_id, run_id, reason, _now()),
        )


def get_pending_approval(run_id: str) -> Optional[Dict[str, Any]]:
    with get_db_conn() as conn:
        row = conn.execute("SELECT * FROM approval_requests WHERE run_id = ? AND status = 'pending' ORDER BY requested_at DESC LIMIT 1", (run_id,)).fetchone()
        return dict(row) if row else None


def decide_approval(approval_id: str, approved: bool, decided_by: str) -> bool:
    with get_db_conn() as conn:
        cur = conn.execute(
            "UPDATE approval_requests SET status = ?, decided_at = ?, decided_by = ? WHERE id = ? AND status = 'pending'",
            ("approved" if approved else "rejected", _now(), decided_by, approval_id),
        )
        return cur.rowcount > 0


def add_automation_event(run_id: str, event_type: str, payload: Dict[str, Any], created_at: Optional[str] = None) -> None:
    with get_db_conn() as conn:
        conn.execute(
            "INSERT INTO automation_events (run_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (run_id, event_type, json.dumps(payload, ensure_ascii=False), created_at or _now()),
        )


def get_automation_events(run_id: str) -> List[Dict[str, Any]]:
    with get_db_conn() as conn:
        rows = conn.execute("SELECT id, event_type, payload_json, created_at FROM automation_events WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        return [
            {**dict(row), "payload": _loads_json(row["payload_json"], {})}
            for row in rows
        ]


def create_capability_audit(
    audit_id: str,
    *,
    capability_name: str,
    source: str,
    risk_level: str,
    actor: str,
    project_id: Optional[str],
    session_id: Optional[str],
    run_id: Optional[str],
    decision: str,
    permission_mode: str,
    arguments: Any,
    reason: Optional[str] = None,
) -> None:
    with get_db_conn() as conn:
        _ensure_capability_audit_schema(conn)
        conn.execute(
            """
            INSERT INTO capability_audits (
                id, capability_name, source, risk_level, actor, project_id,
                session_id, run_id, decision, permission_mode, arguments_json,
                reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id, capability_name, source, risk_level, actor, project_id,
                session_id, run_id, decision, permission_mode,
                json.dumps(arguments, ensure_ascii=False), reason, _now(),
            ),
        )


def finish_capability_audit(
    audit_id: str,
    *,
    status: str,
    duration_ms: int,
    result: Any = None,
    error: Any = None,
) -> bool:
    with get_db_conn() as conn:
        _ensure_capability_audit_schema(conn)
        cur = conn.execute(
            """
            UPDATE capability_audits
            SET status = ?, result_json = ?, error_json = ?, duration_ms = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                status,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                json.dumps(error, ensure_ascii=False) if error is not None else None,
                duration_ms,
                _now(),
                audit_id,
            ),
        )
        return cur.rowcount > 0


def list_capability_audits(limit: int = 100, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
    query = "SELECT * FROM capability_audits"
    values: List[Any] = []
    if run_id:
        query += " WHERE run_id = ?"
        values.append(run_id)
    query += " ORDER BY created_at DESC LIMIT ?"
    values.append(max(1, min(int(limit), 500)))
    with get_db_conn() as conn:
        _ensure_capability_audit_schema(conn)
        rows = conn.execute(query, values).fetchall()
    return [
        {
            **dict(row),
            "arguments": _loads_json(row["arguments_json"], {}),
            "result": _loads_json(row["result_json"], None),
            "error": _loads_json(row["error_json"], None),
        }
        for row in rows
    ]


def upsert_model_install_job(
    job_id: str,
    model: str,
    status: str,
    progress: int = 0,
    downloaded_bytes: int = 0,
    total_bytes: int = 0,
    message: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    now = _now()
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO model_install_jobs (
                job_id, model, status, progress, downloaded_bytes, total_bytes,
                message, error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status=excluded.status,
                progress=excluded.progress,
                downloaded_bytes=excluded.downloaded_bytes,
                total_bytes=excluded.total_bytes,
                message=excluded.message,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (job_id, model, status, progress, downloaded_bytes, total_bytes, message, error, now, now),
        )


def get_model_install_job(job_id: str) -> Optional[Dict[str, Any]]:
    with get_db_conn() as conn:
        row = conn.execute("SELECT * FROM model_install_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def list_model_install_jobs(limit: int = 20) -> List[Dict[str, Any]]:
    safe_limit = max(1, min(100, int(limit)))
    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM model_install_jobs ORDER BY updated_at DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
        return [dict(row) for row in rows]
