"""Additive SQLite persistence for local OAuth connector state.

This store deliberately owns its schema instead of extending ``database.py``.
Every method opens a short transaction through the injected connection factory,
which keeps it compatible with the Workbench database lock and test databases.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, ContextManager, Iterable, Mapping, Optional

from database import get_db_conn


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SECRET_KEY = re.compile(
    r"(?i)(authorization|cookie|password|secret|token|api[_-]?key|private[_-]?key|code_verifier)"
)
_CONNECTORS = {"github", "notion"}
_CONNECTION_STATUSES = {
    "connected",
    "degraded",
    "error",
    "refresh_required",
    "revoked",
    "revoke_failed",
}
_FLOW_STATUSES = {"pending", "exchanging", "completed", "failed", "expired"}


class ConnectorStoreError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ConnectorNotFoundError(ConnectorStoreError):
    def __init__(self, message: str = "The connector record was not found.") -> None:
        super().__init__("CONNECTOR_NOT_FOUND", message, status_code=404)


class ConnectorConflictError(ConnectorStoreError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status_code=409)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime] = None) -> str:
    return (value or _now()).isoformat()


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _text(
    value: Any,
    label: str,
    *,
    maximum: int = 512,
    required: bool = True,
) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ConnectorStoreError("INVALID_CONNECTOR_DATA", f"{label} is required.")
    if len(result) > maximum or _CONTROL.search(result):
        raise ConnectorStoreError("INVALID_CONNECTOR_DATA", f"{label} is invalid.")
    return result


def normalize_connector_id(value: Any) -> str:
    connector_id = _text(value, "Connector ID", maximum=32).casefold()
    if connector_id not in _CONNECTORS:
        raise ConnectorNotFoundError("The connector is not supported.")
    return connector_id


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _safe_details(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key)[:128]: "[REDACTED]" if _SECRET_KEY.search(str(key)) else _safe_details(item)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [_safe_details(item) for item in list(value)[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def _dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


class ConnectorStore:
    def __init__(
        self,
        connection_factory: Optional[Callable[[], ContextManager[Any]]] = None,
    ) -> None:
        self._connection_factory = connection_factory or get_db_conn
        self._schema_lock = threading.RLock()
        self._schema_ready = False

    def _ensure_schema(self, conn: Any) -> None:
        # SQLite scopes foreign-key enforcement to each connection, while the
        # schema itself only needs to be created once per store instance.
        conn.execute("PRAGMA foreign_keys = ON")
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS connector_auth_profiles (
                    profile_id TEXT PRIMARY KEY,
                    connector_id TEXT NOT NULL UNIQUE,
                    auth_mode TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    callback_uri TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS connector_connections (
                    connection_id TEXT PRIMARY KEY,
                    connector_id TEXT NOT NULL,
                    auth_profile_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    workspace_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    requested_permissions_json TEXT NOT NULL DEFAULT '[]',
                    granted_permissions_json TEXT NOT NULL DEFAULT '[]',
                    token_expires_at TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    validated_at TEXT,
                    revoked_at TEXT,
                    UNIQUE(connector_id, account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_connector_connections_provider
                    ON connector_connections(connector_id, status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS connector_oauth_flows (
                    flow_id TEXT PRIMARY KEY,
                    connector_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    connection_id TEXT,
                    state_sha256 TEXT NOT NULL UNIQUE,
                    redirect_uri TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT,
                    completed_at TEXT,
                    error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_connector_oauth_expiry
                    ON connector_oauth_flows(status, expires_at);

                CREATE TABLE IF NOT EXISTS connector_project_bindings (
                    connection_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    mode TEXT NOT NULL DEFAULT 'read_write',
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(connection_id, project_id),
                    FOREIGN KEY(connection_id) REFERENCES connector_connections(connection_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_connector_project_bindings_project
                    ON connector_project_bindings(project_id, enabled, updated_at DESC);

                CREATE TABLE IF NOT EXISTS connector_resource_bindings (
                    connection_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    parent_id TEXT,
                    display_label TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(connection_id, project_id, resource_type, resource_id),
                    FOREIGN KEY(connection_id, project_id)
                        REFERENCES connector_project_bindings(connection_id, project_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_connector_resources_scope
                    ON connector_resource_bindings(project_id, connection_id, resource_type);

                CREATE TABLE IF NOT EXISTS connector_audits (
                    audit_id TEXT PRIMARY KEY,
                    connector_id TEXT NOT NULL,
                    connection_id TEXT,
                    project_id TEXT,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_connector_audits_created
                    ON connector_audits(connector_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_connector_audits_connection
                    ON connector_audits(connection_id, created_at DESC);
                """
            )
            self._schema_ready = True

    def ensure_schema(self) -> None:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)

    def upsert_auth_profile(
        self,
        *,
        connector_id: str,
        client_id: str,
        callback_uri: str,
        auth_mode: str = "oauth2",
    ) -> dict[str, Any]:
        connector = normalize_connector_id(connector_id)
        profile_id = f"profile.{connector}"
        now = _iso()
        values = (
            profile_id,
            connector,
            _text(auth_mode, "Auth mode", maximum=32),
            _text(client_id, "Client ID", maximum=512),
            _text(callback_uri, "Callback URI", maximum=2048),
            now,
            now,
        )
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO connector_auth_profiles (
                    profile_id, connector_id, auth_mode, client_id,
                    callback_uri, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(connector_id) DO UPDATE SET
                    auth_mode = excluded.auth_mode,
                    client_id = excluded.client_id,
                    callback_uri = excluded.callback_uri,
                    updated_at = excluded.updated_at
                """,
                values,
            )
            row = conn.execute(
                "SELECT * FROM connector_auth_profiles WHERE connector_id = ?", (connector,)
            ).fetchone()
        return self._profile(row)

    @staticmethod
    def _profile(row: Any) -> dict[str, Any]:
        value = _dict(row)
        return value

    def get_auth_profile(self, connector_id: str) -> Optional[dict[str, Any]]:
        connector = normalize_connector_id(connector_id)
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM connector_auth_profiles WHERE connector_id = ?", (connector,)
            ).fetchone()
        return self._profile(row) if row is not None else None

    def delete_auth_profile(self, connector_id: str) -> bool:
        connector = normalize_connector_id(connector_id)
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            active = conn.execute(
                "SELECT 1 FROM connector_connections WHERE connector_id = ? LIMIT 1", (connector,)
            ).fetchone()
            if active:
                raise ConnectorConflictError(
                    "CONNECTOR_PROFILE_IN_USE",
                    "Disconnect every account before deleting the OAuth profile.",
                )
            cursor = conn.execute(
                "DELETE FROM connector_auth_profiles WHERE connector_id = ?", (connector,)
            )
        return bool(cursor.rowcount)

    def create_oauth_flow(
        self,
        *,
        flow_id: str,
        connector_id: str,
        profile_id: str,
        state_sha256: str,
        redirect_uri: str,
        connection_id: Optional[str] = None,
        ttl_seconds: int = 600,
    ) -> dict[str, Any]:
        connector = normalize_connector_id(connector_id)
        safe_ttl = max(60, min(int(ttl_seconds), 900))
        created = _now()
        values = (
            _text(flow_id, "OAuth flow ID"),
            connector,
            _text(profile_id, "OAuth profile ID"),
            _text(connection_id, "Connection ID", required=False) or None,
            _text(state_sha256, "OAuth state digest", maximum=64),
            _text(redirect_uri, "Redirect URI", maximum=2048),
            "pending",
            _iso(created + timedelta(seconds=safe_ttl)),
            _iso(created),
        )
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO connector_oauth_flows (
                    flow_id, connector_id, profile_id, connection_id,
                    state_sha256, redirect_uri, status, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = conn.execute(
                "SELECT * FROM connector_oauth_flows WHERE flow_id = ?", (values[0],)
            ).fetchone()
        return _dict(row)

    def claim_oauth_flow(self, *, connector_id: str, raw_state: str) -> dict[str, Any]:
        connector = normalize_connector_id(connector_id)
        state = _text(raw_state, "OAuth state", maximum=512)
        state_sha256 = hashlib.sha256(state.encode("utf-8")).hexdigest()
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM connector_oauth_flows WHERE state_sha256 = ?",
                (state_sha256,),
            ).fetchone()
            if row is None or row["connector_id"] != connector:
                raise ConnectorStoreError(
                    "OAUTH_STATE_INVALID", "The OAuth state is invalid.", status_code=400
                )
            if row["status"] != "pending":
                raise ConnectorConflictError(
                    "OAUTH_STATE_REPLAYED", "The OAuth state has already been consumed."
                )
            expires_at = _parse_time(row["expires_at"])
            if expires_at is None or expires_at <= _now():
                conn.execute(
                    "UPDATE connector_oauth_flows SET status = 'expired', error_code = ? WHERE flow_id = ?",
                    ("OAUTH_STATE_EXPIRED", row["flow_id"]),
                )
                raise ConnectorStoreError(
                    "OAUTH_STATE_EXPIRED", "The OAuth state has expired.", status_code=400
                )
            consumed = _iso()
            cursor = conn.execute(
                """
                UPDATE connector_oauth_flows
                SET status = 'exchanging', consumed_at = ?
                WHERE flow_id = ? AND status = 'pending'
                """,
                (consumed, row["flow_id"]),
            )
            if cursor.rowcount != 1:
                raise ConnectorConflictError(
                    "OAUTH_STATE_REPLAYED", "The OAuth state has already been consumed."
                )
            claimed = conn.execute(
                "SELECT * FROM connector_oauth_flows WHERE flow_id = ?", (row["flow_id"],)
            ).fetchone()
        return _dict(claimed)

    def finish_oauth_flow(
        self, flow_id: str, *, success: bool, error_code: Optional[str] = None
    ) -> None:
        safe_id = _text(flow_id, "OAuth flow ID")
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE connector_oauth_flows
                SET status = ?, completed_at = ?, error_code = ?
                WHERE flow_id = ? AND status = 'exchanging'
                """,
                (
                    "completed" if success else "failed",
                    _iso(),
                    _text(error_code, "Error code", maximum=128, required=False) or None,
                    safe_id,
                ),
            )
        if cursor.rowcount != 1:
            raise ConnectorConflictError(
                "OAUTH_FLOW_STATE_CHANGED", "The OAuth flow is no longer being exchanged."
            )

    def expire_oauth_flows(self) -> int:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE connector_oauth_flows
                SET status = 'expired', error_code = 'OAUTH_STATE_EXPIRED'
                WHERE status IN ('pending', 'exchanging') AND expires_at <= ?
                """,
                (_iso(),),
            )
        return int(cursor.rowcount)

    def invalidate_incomplete_oauth_flows(self) -> list[str]:
        """Fail every flow that could otherwise survive an application restart.

        OAuth state and PKCE verifiers are bound to one in-memory application
        lifetime.  Returning the affected IDs lets the service remove only the
        matching encrypted verifier records without touching saved connection
        credentials.
        """

        completed_at = _iso()
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT flow_id FROM connector_oauth_flows
                WHERE status IN ('pending', 'exchanging')
                """
            ).fetchall()
            flow_ids = [str(row["flow_id"]) for row in rows]
            if flow_ids:
                conn.execute(
                    """
                    UPDATE connector_oauth_flows
                    SET status = 'expired', completed_at = ?,
                        error_code = 'OAUTH_FLOW_INVALIDATED_ON_RESTART'
                    WHERE status IN ('pending', 'exchanging')
                    """,
                    (completed_at,),
                )
        return flow_ids

    def oauth_flow_ids(self, *, statuses: Iterable[str]) -> list[str]:
        safe_statuses = sorted({_text(item, "OAuth flow status", maximum=32) for item in statuses})
        if not safe_statuses or any(item not in _FLOW_STATUSES for item in safe_statuses):
            raise ConnectorStoreError("INVALID_OAUTH_FLOW_STATUS", "OAuth flow status is invalid.")
        placeholders = ",".join("?" for _ in safe_statuses)
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"SELECT flow_id FROM connector_oauth_flows WHERE status IN ({placeholders})",
                safe_statuses,
            ).fetchall()
        return [str(row["flow_id"]) for row in rows]

    @staticmethod
    def _connection(row: Any) -> dict[str, Any]:
        value = _dict(row)
        if not value:
            return value
        value["metadata"] = _loads(value.pop("metadata_json", "{}"), {})
        value["requested_permissions"] = _loads(
            value.pop("requested_permissions_json", "[]"), []
        )
        value["granted_permissions"] = _loads(
            value.pop("granted_permissions_json", "[]"), []
        )
        return value

    def save_connection(
        self,
        *,
        connection_id: str,
        connector_id: str,
        auth_profile_id: str,
        account_id: str,
        display_name: str,
        workspace_id: Optional[str],
        metadata: Mapping[str, Any],
        requested_permissions: Iterable[str],
        granted_permissions: Iterable[str],
        token_expires_at: Optional[str],
    ) -> dict[str, Any]:
        connector = normalize_connector_id(connector_id)
        now = _iso()
        account = _text(account_id, "Account ID", maximum=512)
        existing_id: Optional[str] = None
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            existing = conn.execute(
                "SELECT connection_id FROM connector_connections WHERE connector_id = ? AND account_id = ?",
                (connector, account),
            ).fetchone()
            existing_id = str(existing["connection_id"]) if existing else None
            target_id = existing_id or _text(connection_id, "Connection ID")
            conn.execute(
                """
                INSERT INTO connector_connections (
                    connection_id, connector_id, auth_profile_id, status,
                    account_id, display_name, workspace_id, metadata_json,
                    requested_permissions_json, granted_permissions_json,
                    token_expires_at, error_code, created_at, updated_at, validated_at
                ) VALUES (?, ?, ?, 'connected', ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(connection_id) DO UPDATE SET
                    auth_profile_id = excluded.auth_profile_id,
                    status = 'connected',
                    display_name = excluded.display_name,
                    workspace_id = excluded.workspace_id,
                    metadata_json = excluded.metadata_json,
                    requested_permissions_json = excluded.requested_permissions_json,
                    granted_permissions_json = excluded.granted_permissions_json,
                    token_expires_at = excluded.token_expires_at,
                    error_code = NULL,
                    updated_at = excluded.updated_at,
                    validated_at = excluded.validated_at,
                    revoked_at = NULL
                """,
                (
                    target_id,
                    connector,
                    _text(auth_profile_id, "OAuth profile ID"),
                    account,
                    _text(display_name, "Display name", maximum=512),
                    _text(workspace_id, "Workspace ID", maximum=512, required=False) or None,
                    _json(_safe_details(metadata)),
                    _json(sorted({_text(item, "Permission", maximum=128) for item in requested_permissions})),
                    _json(sorted({_text(item, "Permission", maximum=128) for item in granted_permissions})),
                    _text(token_expires_at, "Token expiry", maximum=64, required=False) or None,
                    now,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM connector_connections WHERE connection_id = ?", (target_id,)
            ).fetchone()
        return self._connection(row)

    def get_connection(self, connection_id: str) -> Optional[dict[str, Any]]:
        safe_id = _text(connection_id, "Connection ID")
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM connector_connections WHERE connection_id = ?", (safe_id,)
            ).fetchone()
        return self._connection(row) if row is not None else None

    def list_connections(
        self,
        *,
        connector_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        join = ""
        if connector_id:
            clauses.append("c.connector_id = ?")
            values.append(normalize_connector_id(connector_id))
        if project_id:
            join = " JOIN connector_project_bindings b ON b.connection_id = c.connection_id "
            clauses.append("b.project_id = ?")
            values.append(_text(project_id, "Project ID"))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT c.* FROM connector_connections c" + join + where + " ORDER BY c.updated_at DESC",
                values,
            ).fetchall()
        return [self._connection(row) for row in rows]

    def update_connection_status(
        self,
        connection_id: str,
        *,
        status: str,
        error_code: Optional[str] = None,
        token_expires_at: Optional[str] = None,
        validated: bool = False,
    ) -> dict[str, Any]:
        safe_id = _text(connection_id, "Connection ID")
        safe_status = _text(status, "Connection status", maximum=32).casefold()
        if safe_status not in _CONNECTION_STATUSES:
            raise ConnectorStoreError("INVALID_CONNECTION_STATUS", "Connection status is invalid.")
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE connector_connections
                SET status = ?, error_code = ?, token_expires_at = COALESCE(?, token_expires_at),
                    updated_at = ?, validated_at = CASE WHEN ? THEN ? ELSE validated_at END,
                    revoked_at = CASE WHEN ? = 'revoked' THEN ? ELSE revoked_at END
                WHERE connection_id = ?
                """,
                (
                    safe_status,
                    _text(error_code, "Error code", maximum=128, required=False) or None,
                    _text(token_expires_at, "Token expiry", maximum=64, required=False) or None,
                    _iso(),
                    1 if validated else 0,
                    _iso(),
                    safe_status,
                    _iso(),
                    safe_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM connector_connections WHERE connection_id = ?", (safe_id,)
            ).fetchone()
        if cursor.rowcount != 1 or row is None:
            raise ConnectorNotFoundError("The connector connection was not found.")
        return self._connection(row)

    def delete_connection(self, connection_id: str) -> bool:
        safe_id = _text(connection_id, "Connection ID")
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                "DELETE FROM connector_connections WHERE connection_id = ?", (safe_id,)
            )
        return bool(cursor.rowcount)

    @staticmethod
    def _binding(row: Any) -> dict[str, Any]:
        value = _dict(row)
        if value:
            value["enabled"] = bool(value.get("enabled"))
            value["revision"] = int(value.get("revision") or 0)
        return value

    def put_project_binding(
        self,
        *,
        project_id: str,
        connection_id: str,
        enabled: bool,
        mode: str,
    ) -> dict[str, Any]:
        project = _text(project_id, "Project ID")
        connection = _text(connection_id, "Connection ID")
        safe_mode = _text(mode, "Connection mode", maximum=32).casefold()
        if safe_mode not in {"read_only", "read_write"}:
            raise ConnectorStoreError("INVALID_CONNECTION_MODE", "Connection mode is invalid.")
        now = _iso()
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            if not conn.execute(
                "SELECT 1 FROM connector_connections WHERE connection_id = ?", (connection,)
            ).fetchone():
                raise ConnectorNotFoundError("The connector connection was not found.")
            conn.execute(
                """
                INSERT INTO connector_project_bindings (
                    connection_id, project_id, enabled, mode, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(connection_id, project_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    mode = excluded.mode,
                    updated_at = excluded.updated_at
                """,
                (connection, project, int(bool(enabled)), safe_mode, now, now),
            )
            row = conn.execute(
                """
                SELECT * FROM connector_project_bindings
                WHERE connection_id = ? AND project_id = ?
                """,
                (connection, project),
            ).fetchone()
        return self._binding(row)

    def get_project_binding(
        self, *, project_id: str, connection_id: str
    ) -> Optional[dict[str, Any]]:
        project = _text(project_id, "Project ID")
        connection = _text(connection_id, "Connection ID")
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                """
                SELECT * FROM connector_project_bindings
                WHERE connection_id = ? AND project_id = ?
                """,
                (connection, project),
            ).fetchone()
        return self._binding(row) if row is not None else None

    def list_project_connections(
        self, project_id: str, *, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        project = _text(project_id, "Project ID")
        enabled = " AND b.enabled = 1" if enabled_only else ""
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT c.*, b.enabled AS binding_enabled, b.mode AS binding_mode,
                       b.revision AS binding_revision, b.project_id AS binding_project_id
                FROM connector_connections c
                JOIN connector_project_bindings b ON b.connection_id = c.connection_id
                WHERE b.project_id = ?
                """ + enabled + " ORDER BY c.connector_id, c.updated_at DESC",
                (project,),
            ).fetchall()
        results = []
        for row in rows:
            record = self._connection(row)
            record["binding"] = {
                "project_id": record.pop("binding_project_id"),
                "enabled": bool(record.pop("binding_enabled")),
                "mode": record.pop("binding_mode"),
                "revision": int(record.pop("binding_revision") or 0),
            }
            results.append(record)
        return results

    @staticmethod
    def _resource(row: Any) -> dict[str, Any]:
        value = _dict(row)
        if value:
            value["metadata"] = _loads(value.pop("metadata_json", "{}"), {})
        return value

    def list_resource_bindings(
        self, *, project_id: str, connection_id: str
    ) -> dict[str, Any]:
        project = _text(project_id, "Project ID")
        connection = _text(connection_id, "Connection ID")
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            binding = conn.execute(
                """
                SELECT * FROM connector_project_bindings
                WHERE connection_id = ? AND project_id = ?
                """,
                (connection, project),
            ).fetchone()
            if binding is None:
                raise ConnectorNotFoundError("The project connection binding was not found.")
            rows = conn.execute(
                """
                SELECT * FROM connector_resource_bindings
                WHERE connection_id = ? AND project_id = ?
                ORDER BY resource_type, display_label, resource_id
                """,
                (connection, project),
            ).fetchall()
        return {
            "connection_id": connection,
            "project_id": project,
            "revision": int(binding["revision"] or 0),
            "enabled": bool(binding["enabled"]),
            "mode": str(binding["mode"]),
            "binding": self._binding(binding),
            "resources": [self._resource(row) for row in rows],
        }

    def replace_resource_bindings(
        self,
        *,
        project_id: str,
        connection_id: str,
        expected_revision: int,
        resources: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        project = _text(project_id, "Project ID")
        connection = _text(connection_id, "Connection ID")
        revision = int(expected_revision)
        if revision < 0:
            raise ConnectorStoreError("INVALID_BINDING_REVISION", "Binding revision is invalid.")
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw in list(resources):
            if len(normalized) >= 500:
                raise ConnectorStoreError("TOO_MANY_RESOURCES", "At most 500 resources may be bound.")
            resource_type = _text(raw.get("resource_type"), "Resource type", maximum=64).casefold()
            resource_id = _text(raw.get("resource_id"), "Resource ID", maximum=1024)
            key = (resource_type, resource_id)
            if key in seen:
                raise ConnectorStoreError("DUPLICATE_RESOURCE", "A resource was selected more than once.")
            seen.add(key)
            normalized.append(
                {
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "parent_id": _text(
                        raw.get("parent_id"), "Parent ID", maximum=1024, required=False
                    )
                    or None,
                    "display_label": _text(
                        raw.get("display_label") or resource_id,
                        "Resource label",
                        maximum=1024,
                    ),
                    "metadata": _safe_details(raw.get("metadata") or {}),
                }
            )
        now = _iso()
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE connector_project_bindings
                SET revision = revision + 1, updated_at = ?
                WHERE connection_id = ? AND project_id = ? AND revision = ?
                """,
                (now, connection, project, revision),
            )
            if cursor.rowcount != 1:
                exists = conn.execute(
                    """
                    SELECT revision FROM connector_project_bindings
                    WHERE connection_id = ? AND project_id = ?
                    """,
                    (connection, project),
                ).fetchone()
                if exists is None:
                    raise ConnectorNotFoundError("The project connection binding was not found.")
                raise ConnectorConflictError(
                    "RESOURCE_BINDING_REVISION_CONFLICT",
                    "The resource selection changed. Reload it before saving.",
                )
            conn.execute(
                "DELETE FROM connector_resource_bindings WHERE connection_id = ? AND project_id = ?",
                (connection, project),
            )
            for item in normalized:
                conn.execute(
                    """
                    INSERT INTO connector_resource_bindings (
                        connection_id, project_id, resource_type, resource_id,
                        parent_id, display_label, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        connection,
                        project,
                        item["resource_type"],
                        item["resource_id"],
                        item["parent_id"],
                        item["display_label"],
                        _json(item["metadata"]),
                        now,
                        now,
                    ),
                )
        return self.list_resource_bindings(project_id=project, connection_id=connection)

    def resource_is_bound(
        self,
        *,
        project_id: str,
        connection_id: str,
        resource_type: str,
        resource_id: str,
    ) -> bool:
        project = _text(project_id, "Project ID")
        connection = _text(connection_id, "Connection ID")
        kind = _text(resource_type, "Resource type", maximum=64).casefold()
        identity = _text(resource_id, "Resource ID", maximum=1024)
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                """
                SELECT 1 FROM connector_resource_bindings r
                JOIN connector_project_bindings b
                  ON b.connection_id = r.connection_id AND b.project_id = r.project_id
                WHERE r.connection_id = ? AND r.project_id = ?
                  AND r.resource_type = ? AND r.resource_id = ? AND b.enabled = 1
                """,
                (connection, project, kind, identity),
            ).fetchone()
        return bool(row)

    def audit(
        self,
        *,
        connector_id: str,
        action: str,
        status: str,
        connection_id: Optional[str] = None,
        project_id: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
        error_code: Optional[str] = None,
    ) -> str:
        audit_id = f"caudit_{uuid.uuid4().hex}"
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO connector_audits (
                    audit_id, connector_id, connection_id, project_id,
                    action, status, details_json, error_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    normalize_connector_id(connector_id),
                    _text(connection_id, "Connection ID", required=False) or None,
                    _text(project_id, "Project ID", required=False) or None,
                    _text(action, "Audit action", maximum=128),
                    _text(status, "Audit status", maximum=64),
                    _json(_safe_details(details or {})),
                    _text(error_code, "Error code", maximum=128, required=False) or None,
                    _iso(),
                ),
            )
        return audit_id

    def list_audits(
        self,
        connector_id: str,
        *,
        connection_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        connector = normalize_connector_id(connector_id)
        clauses = ["connector_id = ?"]
        values: list[Any] = [connector]
        if connection_id:
            clauses.append("connection_id = ?")
            values.append(_text(connection_id, "Connection ID"))
        values.append(max(1, min(int(limit), 500)))
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT * FROM connector_audits WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC LIMIT ?",
                values,
            ).fetchall()
        results = []
        for row in rows:
            item = _dict(row)
            item["details"] = _loads(item.pop("details_json", "{}"), {})
            results.append(item)
        return results


__all__ = [
    "ConnectorConflictError",
    "ConnectorNotFoundError",
    "ConnectorStore",
    "ConnectorStoreError",
    "normalize_connector_id",
]
