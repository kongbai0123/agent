"""Installation-bound credentials for the public Workbench Agent API.

This module deliberately owns an additive SQLite schema and exposes no chat
implementation.  The HTTP adapter in :mod:`api.routes.external_agent_api`
injects the existing Workbench run runtime through a small protocol.

API key material is generated on the Workbench computer.  A key is bound to a
persistent random installation identity and verified with an HMAC pepper kept
in the existing DPAPI-backed secret vault.  SQLite contains only a lookup
prefix and the HMAC digest; the complete key is returned exactly once.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import platform
import re
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, ContextManager, Iterable, Mapping, Optional

from connector_secrets import ConnectorSecretError, ConnectorSecretStore
from database import get_db_conn


SUPPORTED_SCOPES = frozenset(
    {"runs:create", "runs:read", "runs:cancel", "capabilities:read"}
)
_KEY_PATTERN = re.compile(
    r"^wbk_(?P<tag>[a-f0-9]{12})_(?P<secret>[A-Za-z0-9_-]{43})$"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SECRET_FIELD = re.compile(
    r"(?i)(authorization|cookie|password|secret|token|api[_-]?key|private[_-]?key)"
)
_SECRET_VALUE = re.compile(
    r"(?i)(bearer\s+\S+|wbk_[a-f0-9]{12}_[A-Za-z0-9_-]{43}|"
    r"nvapi-[A-Za-z0-9_-]+)"
)
_INSTALLATION_SECRET_KIND = "external-api-installation"


class ExternalAgentApiError(RuntimeError):
    """Safe, typed error for management and public API boundaries."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        recoverable: bool = True,
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.recoverable = recoverable
        self.retry_after = retry_after


@dataclass(frozen=True)
class ExternalApiPrincipal:
    installation_id: str
    key_id: str
    project_id: str
    scopes: frozenset[str]
    key_name: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ExternalAgentApiError(
                "EXTERNAL_API_EXPIRY_INVALID",
                "API Key 到期時間格式無效。",
                status_code=422,
            ) from exc
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _safe_text(value: Any, label: str, *, maximum: int = 128) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or _CONTROL.search(text):
        raise ExternalAgentApiError(
            "EXTERNAL_API_REQUEST_INVALID",
            f"{label} 無效。",
            status_code=422,
        )
    return text


def _safe_id(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(text):
        raise ExternalAgentApiError(
            "EXTERNAL_API_REQUEST_INVALID",
            f"{label} 無效。",
            status_code=422,
        )
    return text


def _safe_details(value: Any) -> Any:
    """Bound audit metadata and redact fields that could contain credentials."""

    if isinstance(value, Mapping):
        return {
            str(key)[:80]: "[REDACTED]" if _SECRET_FIELD.search(str(key)) else _safe_details(item)
            for key, item in list(value.items())[:50]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_details(item) for item in list(value)[:50]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _SECRET_VALUE.sub("[REDACTED]", str(value)[:500])


def _normalize_scopes(scopes: Iterable[Any]) -> tuple[str, ...]:
    normalized = tuple(sorted({str(scope or "").strip() for scope in scopes}))
    if not normalized or any(scope not in SUPPORTED_SCOPES for scope in normalized):
        raise ExternalAgentApiError(
            "EXTERNAL_API_SCOPE_INVALID",
            "API Key 包含不支援的權限範圍。",
            status_code=422,
        )
    return normalized


class ExternalAgentApiStore:
    """Short-transaction persistence for installation identity and API keys."""

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
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS external_api_installations (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    installation_id TEXT NOT NULL UNIQUE,
                    installation_tag TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS external_api_keys (
                    key_id TEXT PRIMARY KEY,
                    installation_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    key_prefix TEXT NOT NULL UNIQUE,
                    key_digest TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    expires_at TEXT,
                    rate_limit_per_minute INTEGER NOT NULL,
                    request_limit_daily INTEGER NOT NULL,
                    minute_window_started_at TEXT,
                    minute_request_count INTEGER NOT NULL DEFAULT 0,
                    daily_window_date TEXT,
                    daily_request_count INTEGER NOT NULL DEFAULT 0,
                    total_request_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT,
                    revoked_at TEXT,
                    rotated_from_key_id TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (installation_id)
                        REFERENCES external_api_installations(installation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_external_api_keys_project
                    ON external_api_keys(project_id, enabled, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_external_api_keys_installation
                    ON external_api_keys(installation_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS external_api_runs (
                    run_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    created_by_key_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    cancelled_at TEXT,
                    FOREIGN KEY (created_by_key_id) REFERENCES external_api_keys(key_id)
                );
                CREATE INDEX IF NOT EXISTS idx_external_api_runs_project
                    ON external_api_runs(project_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS external_api_idempotency (
                    idempotency_id TEXT PRIMARY KEY,
                    key_id TEXT NOT NULL,
                    idempotency_key_digest TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    response_json TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (key_id, idempotency_key_digest),
                    FOREIGN KEY (key_id) REFERENCES external_api_keys(key_id),
                    FOREIGN KEY (run_id) REFERENCES external_api_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_external_api_idempotency_key
                    ON external_api_idempotency(key_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS external_api_audits (
                    audit_id TEXT PRIMARY KEY,
                    installation_id TEXT,
                    key_id TEXT,
                    project_id TEXT,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_external_api_audits_created
                    ON external_api_audits(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_external_api_audits_key
                    ON external_api_audits(key_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS external_api_auth_failure_audits (
                    installation_id TEXT NOT NULL,
                    key_id TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    error_code TEXT NOT NULL,
                    bucket_started_at TEXT NOT NULL,
                    failure_count INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (
                        installation_id, key_id, project_id, action,
                        error_code, bucket_started_at
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_external_api_auth_failures_last
                    ON external_api_auth_failure_audits(last_seen_at DESC);
                """
            )
            self._schema_ready = True

    def ensure_schema(self) -> None:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)

    def recover_interrupted_idempotency(self, *, now: str) -> int:
        """Mark pre-dispatch reservations left by an earlier process unknown.

        Replaying them as queued would claim that a Run exists when the process
        may have crashed before dispatch. They are therefore never retried
        automatically; the caller must use a new Idempotency-Key.
        """

        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE external_api_idempotency
                   SET state = 'dispatch_unknown',
                       error_code = 'EXTERNAL_API_DISPATCH_UNKNOWN',
                       updated_at = ?
                 WHERE state = 'reserved'
                """,
                (now,),
            )
            return int(cursor.rowcount or 0)

    def ensure_installation(self, *, now: str, label: str) -> dict[str, Any]:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM external_api_installations WHERE singleton = 1"
            ).fetchone()
            if row is None:
                installation_id = f"inst_{uuid.uuid4().hex}"
                installation_tag = secrets.token_hex(6)
                conn.execute(
                    """
                    INSERT INTO external_api_installations (
                        singleton, installation_id, installation_tag, label, created_at
                    ) VALUES (1, ?, ?, ?, ?)
                    """,
                    (installation_id, installation_tag, label, now),
                )
                row = conn.execute(
                    "SELECT * FROM external_api_installations WHERE singleton = 1"
                ).fetchone()
            return dict(row)

    def get_installation(self) -> dict[str, Any]:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM external_api_installations WHERE singleton = 1"
            ).fetchone()
            return dict(row) if row is not None else {}

    def insert_key(self, record: Mapping[str, Any]) -> None:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO external_api_keys (
                    key_id, installation_id, name, key_prefix, key_digest,
                    project_id, scopes_json, enabled, expires_at,
                    rate_limit_per_minute, request_limit_daily,
                    minute_window_started_at, minute_request_count,
                    daily_window_date, daily_request_count, total_request_count,
                    created_at, updated_at, last_used_at, revoked_at,
                    rotated_from_key_id, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, 0, NULL, 0, 0,
                          ?, ?, NULL, NULL, ?, 1)
                """,
                (
                    record["key_id"],
                    record["installation_id"],
                    record["name"],
                    record["key_prefix"],
                    record["key_digest"],
                    record["project_id"],
                    record["scopes_json"],
                    record.get("expires_at"),
                    record["rate_limit_per_minute"],
                    record["request_limit_daily"],
                    record["created_at"],
                    record["created_at"],
                    record.get("rotated_from_key_id"),
                ),
            )

    def list_keys(self) -> list[dict[str, Any]]:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT * FROM external_api_keys ORDER BY created_at DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def key_count(self) -> int:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            return int(
                conn.execute("SELECT COUNT(*) FROM external_api_keys").fetchone()[0]
            )

    def get_key(self, key_id: str) -> dict[str, Any]:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM external_api_keys WHERE key_id = ?", (key_id,)
            ).fetchone()
            return dict(row) if row is not None else {}

    def get_key_by_prefix(self, prefix: str) -> dict[str, Any]:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM external_api_keys WHERE key_prefix = ?", (prefix,)
            ).fetchone()
            return dict(row) if row is not None else {}

    def replace_key_policy(
        self,
        *,
        key_id: str,
        expected_revision: int,
        enabled: bool,
        scopes_json: str,
        expires_at: Optional[str],
        rate_limit_per_minute: int,
        request_limit_daily: int,
        updated_at: str,
    ) -> bool:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE external_api_keys
                   SET enabled = ?, scopes_json = ?, expires_at = ?,
                       rate_limit_per_minute = ?, request_limit_daily = ?,
                       updated_at = ?, revision = revision + 1
                 WHERE key_id = ? AND revision = ? AND revoked_at IS NULL
                """,
                (
                    int(enabled),
                    scopes_json,
                    expires_at,
                    rate_limit_per_minute,
                    request_limit_daily,
                    updated_at,
                    key_id,
                    expected_revision,
                ),
            )
            return cursor.rowcount == 1

    def revoke_key(self, *, key_id: str, expected_revision: int, now: str) -> bool:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE external_api_keys
                   SET enabled = 0, revoked_at = ?, updated_at = ?,
                       revision = revision + 1
                 WHERE key_id = ? AND revision = ? AND revoked_at IS NULL
                """,
                (now, now, key_id, expected_revision),
            )
            return cursor.rowcount == 1

    def consume_request(
        self,
        *,
        key_id: str,
        now: datetime,
    ) -> tuple[dict[str, Any], Optional[str], Optional[int]]:
        """Atomically consume one request and return (row, error, retry_after)."""

        now_iso = _iso(now)
        minute_floor = now.replace(second=0, microsecond=0)
        minute_iso = _iso(minute_floor)
        day = now.date().isoformat()
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            # Serialize this consume with policy replacement/revocation. An
            # already authenticated request is not allowed to slip through a
            # concurrently revoked credential.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM external_api_keys WHERE key_id = ?", (key_id,)
            ).fetchone()
            if row is None:
                return {}, "not_found", None
            current = dict(row)
            if current.get("revoked_at") or not bool(current.get("enabled")):
                return current, "inactive", None
            minute_count = (
                int(current.get("minute_request_count") or 0)
                if current.get("minute_window_started_at") == minute_iso
                else 0
            )
            daily_count = (
                int(current.get("daily_request_count") or 0)
                if current.get("daily_window_date") == day
                else 0
            )
            if minute_count >= int(current["rate_limit_per_minute"]):
                retry_after = max(1, 60 - now.second)
                return current, "minute_limit", retry_after
            if daily_count >= int(current["request_limit_daily"]):
                next_day = datetime.combine(
                    now.date(), datetime.min.time(), tzinfo=timezone.utc
                ).timestamp() + 86400
                return current, "daily_limit", max(1, int(next_day - now.timestamp()))
            conn.execute(
                """
                UPDATE external_api_keys
                   SET minute_window_started_at = ?, minute_request_count = ?,
                       daily_window_date = ?, daily_request_count = ?,
                       total_request_count = total_request_count + 1,
                       last_used_at = ?, updated_at = ?
                 WHERE key_id = ?
                """,
                (
                    minute_iso,
                    minute_count + 1,
                    day,
                    daily_count + 1,
                    now_iso,
                    now_iso,
                    key_id,
                ),
            )
            current.update(
                {
                    "minute_window_started_at": minute_iso,
                    "minute_request_count": minute_count + 1,
                    "daily_window_date": day,
                    "daily_request_count": daily_count + 1,
                    "last_used_at": now_iso,
                }
            )
            return current, None, None

    def bind_run(self, *, run_id: str, project_id: str, key_id: str, now: str) -> None:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            try:
                conn.execute(
                    """
                    INSERT INTO external_api_runs (
                        run_id, project_id, created_by_key_id, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (run_id, project_id, key_id, now),
                )
            except Exception as exc:
                raise ExternalAgentApiError(
                    "EXTERNAL_API_RUN_BINDING_CONFLICT",
                    "此執行識別碼已被使用。",
                    status_code=409,
                ) from exc

    def reserve_idempotent_run(
        self,
        *,
        key_id: str,
        idempotency_key_digest: str,
        request_digest: str,
        run_id: str,
        project_id: str,
        now: str,
    ) -> dict[str, Any]:
        """Atomically reserve an idempotency key and its server-owned Run ID."""

        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            existing = conn.execute(
                """
                SELECT * FROM external_api_idempotency
                 WHERE key_id = ? AND idempotency_key_digest = ?
                """,
                (key_id, idempotency_key_digest),
            ).fetchone()
            if existing is not None:
                current = dict(existing)
                if not hmac.compare_digest(
                    str(current["request_digest"]), str(request_digest)
                ):
                    raise ExternalAgentApiError(
                        "EXTERNAL_API_IDEMPOTENCY_CONFLICT",
                        "相同 Idempotency-Key 已用於不同的請求內容。",
                        status_code=409,
                        recoverable=False,
                    )
                current["replayed"] = True
                return current
            conn.execute(
                """
                INSERT INTO external_api_runs (
                    run_id, project_id, created_by_key_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (run_id, project_id, key_id, now),
            )
            idempotency_id = f"eidem_{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO external_api_idempotency (
                    idempotency_id, key_id, idempotency_key_digest,
                    request_digest, run_id, project_id, state,
                    response_json, error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', NULL, NULL, ?, ?)
                """,
                (
                    idempotency_id,
                    key_id,
                    idempotency_key_digest,
                    request_digest,
                    run_id,
                    project_id,
                    now,
                    now,
                ),
            )
            return {
                "idempotency_id": idempotency_id,
                "key_id": key_id,
                "idempotency_key_digest": idempotency_key_digest,
                "request_digest": request_digest,
                "run_id": run_id,
                "project_id": project_id,
                "state": "reserved",
                "response_json": None,
                "error_code": None,
                "created_at": now,
                "updated_at": now,
                "replayed": False,
            }

    def complete_idempotent_run(
        self,
        *,
        idempotency_id: str,
        state: str,
        response: Mapping[str, Any],
        error_code: Optional[str],
        now: str,
    ) -> None:
        if state not in {"dispatched", "dispatch_failed"}:
            raise ValueError("Invalid idempotency completion state.")
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE external_api_idempotency
                   SET state = ?, response_json = ?, error_code = ?, updated_at = ?
                 WHERE idempotency_id = ? AND state = 'reserved'
                """,
                (state, _json(response), error_code, now, idempotency_id),
            )
            if cursor.rowcount != 1:
                raise ExternalAgentApiError(
                    "EXTERNAL_API_IDEMPOTENCY_STATE_CONFLICT",
                    "外部 API 執行狀態已變更。",
                    status_code=409,
                )

    def require_run_project(self, *, run_id: str, project_id: str) -> dict[str, Any]:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM external_api_runs WHERE run_id = ? AND project_id = ?",
                (run_id, project_id),
            ).fetchone()
            if row is None:
                raise ExternalAgentApiError(
                    "EXTERNAL_API_RUN_NOT_FOUND",
                    "找不到執行紀錄，或此 API Key 無權存取。",
                    status_code=404,
                    recoverable=False,
                )
            return dict(row)

    def mark_run_cancelled(self, *, run_id: str, now: str) -> None:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            conn.execute(
                "UPDATE external_api_runs SET cancelled_at = COALESCE(cancelled_at, ?) WHERE run_id = ?",
                (now, run_id),
            )

    def audit(
        self,
        *,
        installation_id: Optional[str],
        key_id: Optional[str],
        project_id: Optional[str],
        action: str,
        status: str,
        details: Optional[Mapping[str, Any]] = None,
        error_code: Optional[str] = None,
        now: Optional[str] = None,
    ) -> str:
        audit_id = f"eaudit_{uuid.uuid4().hex}"
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO external_api_audits (
                    audit_id, installation_id, key_id, project_id, action,
                    status, details_json, error_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    installation_id,
                    key_id,
                    project_id,
                    str(action)[:128],
                    str(status)[:32],
                    _json(_safe_details(details or {})),
                    str(error_code)[:128] if error_code else None,
                    now or _iso(_utcnow()),
                ),
            )
        return audit_id

    def record_auth_failure(
        self,
        *,
        installation_id: str,
        key_id: Optional[str],
        project_id: Optional[str],
        action: str,
        error_code: str,
        now: datetime,
    ) -> None:
        """Aggregate unauthenticated failures by minute to bound DB growth."""

        bucket = _iso(now.replace(second=0, microsecond=0))
        now_iso = _iso(now)
        cutoff = _iso(now - timedelta(days=30))
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO external_api_auth_failure_audits (
                    installation_id, key_id, project_id, action, error_code,
                    bucket_started_at, failure_count, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT (
                    installation_id, key_id, project_id, action,
                    error_code, bucket_started_at
                ) DO UPDATE SET
                    failure_count = failure_count + 1,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    installation_id,
                    str(key_id or ""),
                    str(project_id or ""),
                    str(action)[:128],
                    str(error_code)[:128],
                    bucket,
                    now_iso,
                    now_iso,
                ),
            )
            conn.execute(
                "DELETE FROM external_api_auth_failure_audits WHERE last_seen_at < ?",
                (cutoff,),
            )
            conn.execute(
                """
                DELETE FROM external_api_auth_failure_audits
                 WHERE rowid IN (
                    SELECT rowid FROM external_api_auth_failure_audits
                     ORDER BY last_seen_at DESC
                     LIMIT -1 OFFSET 5000
                 )
                """
            )

    def list_auth_failures(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 500)
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT installation_id,
                       NULLIF(key_id, '') AS key_id,
                       NULLIF(project_id, '') AS project_id,
                       action, error_code, bucket_started_at, failure_count,
                       first_seen_at, last_seen_at
                  FROM external_api_auth_failure_audits
                 ORDER BY last_seen_at DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
            return [dict(row) for row in rows]

    def reset_installation(
        self,
        *,
        installation_id: str,
        installation_tag: str,
        label: str,
        now: str,
    ) -> dict[str, Any]:
        """Retire all credentials and replace the installation identity."""

        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            old = conn.execute(
                "SELECT * FROM external_api_installations WHERE singleton = 1"
            ).fetchone()
            if old is None:
                raise ExternalAgentApiError(
                    "EXTERNAL_API_INSTALLATION_MISSING",
                    "找不到 Workbench 安裝身分。",
                    status_code=409,
                )
            revoked_count = int(
                conn.execute("SELECT COUNT(*) FROM external_api_keys").fetchone()[0]
            )
            conn.execute("DELETE FROM external_api_idempotency")
            conn.execute("DELETE FROM external_api_runs")
            conn.execute("DELETE FROM external_api_keys")
            conn.execute("DELETE FROM external_api_auth_failure_audits")
            conn.execute(
                """
                UPDATE external_api_installations
                   SET installation_id = ?, installation_tag = ?,
                       label = ?, created_at = ?
                 WHERE singleton = 1
                """,
                (installation_id, installation_tag, label, now),
            )
            conn.execute(
                """
                INSERT INTO external_api_audits (
                    audit_id, installation_id, key_id, project_id, action,
                    status, details_json, error_code, created_at
                ) VALUES (?, ?, NULL, NULL, 'installation.reset',
                          'succeeded', ?, NULL, ?)
                """,
                (
                    f"eaudit_{uuid.uuid4().hex}",
                    str(old["installation_id"]),
                    _json({"revoked_key_count": revoked_count}),
                    now,
                ),
            )
            return {
                "installation_id": installation_id,
                "installation_tag": installation_tag,
                "label": label,
                "created_at": now,
                "revoked_key_count": revoked_count,
            }

    def list_audits(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 500)
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT * FROM external_api_audits ORDER BY created_at DESC LIMIT ?",
                (bounded,),
            ).fetchall()
            return [
                {
                    **dict(row),
                    "details": _loads(row["details_json"], {}),
                }
                for row in rows
            ]


class ExternalAgentApiService:
    """Issue, govern and authenticate installation-bound external API keys."""

    def __init__(
        self,
        *,
        store: Optional[ExternalAgentApiStore] = None,
        secret_store: Optional[ConnectorSecretStore] = None,
        project_exists: Optional[Callable[[str], bool]] = None,
        policy_guard: Optional[Callable[[str, str], Any]] = None,
        clock: Callable[[], datetime] = _utcnow,
        installation_label: Optional[str] = None,
    ) -> None:
        self.store = store or ExternalAgentApiStore()
        self.secret_store = secret_store or ConnectorSecretStore()
        self.project_exists = project_exists or (lambda _project_id: True)
        self.policy_guard = policy_guard
        self.clock = clock
        self.installation_label = self._installation_label(installation_label)
        self._lock = threading.RLock()
        self._installation: dict[str, Any] = {}
        self._pepper = ""
        self._initialized = False
        self._credential_recovery_required = False

    @staticmethod
    def _installation_label(value: Optional[str]) -> str:
        candidate = str(value or platform.node() or "Local AI Workbench").strip()
        candidate = _CONTROL.sub("", candidate)[:80]
        return candidate or "Local AI Workbench"

    def initialize(self) -> dict[str, Any]:
        """Create the durable installation identity and its DPAPI HMAC pepper."""

        with self._lock:
            now = _iso(self.clock())
            installation = self.store.ensure_installation(
                now=now, label=self.installation_label
            )
            self.store.recover_interrupted_idempotency(now=now)
            installation_id = str(installation["installation_id"])
            secret_error = False
            try:
                secret = self.secret_store.get(
                    _INSTALLATION_SECRET_KIND, installation_id
                ).get("hmac_pepper", "")
            except ConnectorSecretError:
                secret = ""
                secret_error = True
            if not secret:
                if secret_error or self.store.key_count() > 0:
                    self._installation = installation
                    self._pepper = ""
                    self._initialized = True
                    self._credential_recovery_required = True
                    return self._installation_payload()
                try:
                    secret = secrets.token_urlsafe(48)
                    self.secret_store.set(
                        _INSTALLATION_SECRET_KIND,
                        installation_id,
                        {"hmac_pepper": secret},
                    )
                except ConnectorSecretError:
                    self._installation = installation
                    self._pepper = ""
                    self._initialized = True
                    self._credential_recovery_required = True
                    return self._installation_payload()
            self._installation = installation
            self._pepper = secret
            self._initialized = True
            self._credential_recovery_required = False
            return self._installation_payload()

    def _ready(self) -> None:
        if not self._initialized:
            self.initialize()
        if self._credential_recovery_required or not self._pepper:
            raise ExternalAgentApiError(
                "EXTERNAL_API_CREDENTIAL_RECOVERY_REQUIRED",
                "此 Workbench 的對外 API 驗證資料遺失或無法解密；請在本機重設安裝身分。",
                status_code=503,
                recoverable=True,
            )

    @property
    def credential_recovery_required(self) -> bool:
        if not self._initialized:
            self.initialize()
        return self._credential_recovery_required

    def _installation_payload(self, *, api_base_url: str = "") -> dict[str, Any]:
        return {
            "id": str(self._installation["installation_id"]),
            "label": str(self._installation["label"]),
            "api_base_url": str(api_base_url or "").rstrip("/"),
            "created_at": str(self._installation["created_at"]),
        }

    def installation(self, *, api_base_url: str = "") -> dict[str, Any]:
        if not self._initialized:
            self.initialize()
        return self._installation_payload(api_base_url=api_base_url)

    def _digest(self, api_key: str) -> str:
        self._ready()
        return hmac.new(
            self._pepper.encode("utf-8"),
            api_key.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _metadata(self, row: Mapping[str, Any]) -> dict[str, Any]:
        now = self.clock()
        expires = _parse_time(row.get("expires_at"))
        if row.get("revoked_at"):
            status = "revoked"
        elif not bool(row.get("enabled")):
            status = "disabled"
        elif expires and expires <= now:
            status = "expired"
        else:
            status = "active"
        return {
            "id": str(row["key_id"]),
            "name": str(row["name"]),
            "prefix": str(row["key_prefix"]),
            "project_id": str(row["project_id"]),
            "scopes": list(_loads(row.get("scopes_json"), [])),
            "status": status,
            "expires_at": row.get("expires_at"),
            "rate_limit_per_minute": int(row["rate_limit_per_minute"]),
            "request_limit_daily": int(row["request_limit_daily"]),
            "created_at": str(row["created_at"]),
            "last_used_at": row.get("last_used_at"),
            "revision": int(row["revision"]),
        }

    def list_keys(self, *, api_base_url: str = "") -> dict[str, Any]:
        if not self._initialized:
            self.initialize()
        return {
            "success": True,
            "installation": self.installation(api_base_url=api_base_url),
            "credential_recovery_required": self.credential_recovery_required,
            "api_keys": [self._metadata(row) for row in self.store.list_keys()],
        }

    def reset_installation(self, *, confirmation: Any) -> dict[str, Any]:
        if str(confirmation or "") != "RESET_EXTERNAL_API":
            raise ExternalAgentApiError(
                "EXTERNAL_API_RESET_CONFIRMATION_REQUIRED",
                "重設安裝身分前，必須輸入 RESET_EXTERNAL_API。",
                status_code=422,
                recoverable=False,
            )
        if not self._initialized:
            self.initialize()
        old_installation_id = str(self._installation["installation_id"])
        new_installation_id = f"inst_{uuid.uuid4().hex}"
        new_installation_tag = secrets.token_hex(6)
        new_pepper = secrets.token_urlsafe(48)
        now = _iso(self.clock())
        try:
            self.secret_store.set(
                _INSTALLATION_SECRET_KIND,
                new_installation_id,
                {"hmac_pepper": new_pepper},
            )
        except ConnectorSecretError as exc:
            raise ExternalAgentApiError(
                "EXTERNAL_API_RESET_SECRET_STORE_UNAVAILABLE",
                "目前無法安全建立新的安裝驗證資料。",
                status_code=503,
                recoverable=True,
            ) from exc
        try:
            replacement = self.store.reset_installation(
                installation_id=new_installation_id,
                installation_tag=new_installation_tag,
                label=self.installation_label,
                now=now,
            )
        except Exception:
            self.secret_store.delete(
                _INSTALLATION_SECRET_KIND, new_installation_id
            )
            raise
        try:
            self.secret_store.delete(
                _INSTALLATION_SECRET_KIND, old_installation_id
            )
        except ConnectorSecretError:
            # The retired pepper cannot validate any remaining key because the
            # reset transaction removed every key and changed installation ID.
            pass
        self._installation = {
            "installation_id": new_installation_id,
            "installation_tag": new_installation_tag,
            "label": self.installation_label,
            "created_at": now,
        }
        self._pepper = new_pepper
        self._initialized = True
        self._credential_recovery_required = False
        return {
            "success": True,
            "installation": self._installation_payload(),
            "revoked_key_count": int(replacement["revoked_key_count"]),
            "notice": "已撤銷所有舊 API Key 並建立新的 Workbench 安裝身分。",
        }

    def issue_key(
        self,
        *,
        name: Any,
        project_id: Any,
        scopes: Iterable[Any],
        expires_at: Any,
        rate_limit_per_minute: int,
        request_limit_daily: int,
        rotated_from_key_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self._ready()
        normalized_name = _safe_text(name, "API Key 名稱", maximum=80)
        normalized_project = _safe_id(project_id, "Project ID")
        if not self.project_exists(normalized_project):
            raise ExternalAgentApiError(
                "EXTERNAL_API_PROJECT_NOT_FOUND",
                "找不到指定的 Project。",
                status_code=404,
                recoverable=False,
            )
        normalized_scopes = _normalize_scopes(scopes)
        expiry = _parse_time(expires_at)
        now = self.clock()
        if expiry is not None and expiry <= now:
            raise ExternalAgentApiError(
                "EXTERNAL_API_EXPIRY_INVALID",
                "API Key 到期時間必須晚於目前時間。",
                status_code=422,
            )
        if not 1 <= int(rate_limit_per_minute) <= 6000:
            raise ExternalAgentApiError(
                "EXTERNAL_API_RATE_LIMIT_INVALID",
                "每分鐘請求上限必須介於 1 到 6000。",
                status_code=422,
            )
        if not 1 <= int(request_limit_daily) <= 10_000_000:
            raise ExternalAgentApiError(
                "EXTERNAL_API_DAILY_LIMIT_INVALID",
                "每日請求上限必須介於 1 到 10000000。",
                status_code=422,
            )
        random_part = secrets.token_urlsafe(32)
        tag = str(self._installation["installation_tag"])
        secret_value = f"wbk_{tag}_{random_part}"
        key_id = f"wak_{uuid.uuid4().hex}"
        prefix = f"wbk_{tag}_{random_part[:12]}"
        created_at = _iso(now)
        record = {
            "key_id": key_id,
            "installation_id": self._installation["installation_id"],
            "name": normalized_name,
            "key_prefix": prefix,
            "key_digest": self._digest(secret_value),
            "project_id": normalized_project,
            "scopes_json": _json(normalized_scopes),
            "expires_at": _iso(expiry) if expiry else None,
            "rate_limit_per_minute": int(rate_limit_per_minute),
            "request_limit_daily": int(request_limit_daily),
            "created_at": created_at,
            "rotated_from_key_id": rotated_from_key_id,
        }
        self.store.insert_key(record)
        row = self.store.get_key(key_id)
        self.store.audit(
            installation_id=str(self._installation["installation_id"]),
            key_id=key_id,
            project_id=normalized_project,
            action="api_key.issue" if not rotated_from_key_id else "api_key.rotate.issue",
            status="succeeded",
            details={"scopes": normalized_scopes},
            now=created_at,
        )
        return {
            "success": True,
            "api_key": self._metadata(row),
            "secret": secret_value,
            "notice": "請立即安全保存此 API Key；完整金鑰只會顯示這一次。",
        }

    def replace_key_policy(
        self,
        *,
        key_id: Any,
        expected_revision: int,
        enabled: bool,
        scopes: Iterable[Any],
        expires_at: Any,
        rate_limit_per_minute: int,
        request_limit_daily: int,
    ) -> dict[str, Any]:
        self._ready()
        normalized_id = _safe_id(key_id, "API Key ID")
        existing = self.store.get_key(normalized_id)
        if not existing:
            raise ExternalAgentApiError(
                "EXTERNAL_API_KEY_NOT_FOUND", "找不到 API Key。", status_code=404
            )
        normalized_scopes = _normalize_scopes(scopes)
        expiry = _parse_time(expires_at)
        if expiry is not None and expiry <= self.clock():
            raise ExternalAgentApiError(
                "EXTERNAL_API_EXPIRY_INVALID",
                "API Key 到期時間必須晚於目前時間。",
                status_code=422,
            )
        if not 1 <= int(rate_limit_per_minute) <= 6000:
            raise ExternalAgentApiError(
                "EXTERNAL_API_RATE_LIMIT_INVALID",
                "每分鐘請求上限必須介於 1 到 6000。",
                status_code=422,
            )
        if not 1 <= int(request_limit_daily) <= 10_000_000:
            raise ExternalAgentApiError(
                "EXTERNAL_API_DAILY_LIMIT_INVALID",
                "每日請求上限必須介於 1 到 10000000。",
                status_code=422,
            )
        updated_at = _iso(self.clock())
        changed = self.store.replace_key_policy(
            key_id=normalized_id,
            expected_revision=int(expected_revision),
            enabled=bool(enabled),
            scopes_json=_json(normalized_scopes),
            expires_at=_iso(expiry) if expiry else None,
            rate_limit_per_minute=int(rate_limit_per_minute),
            request_limit_daily=int(request_limit_daily),
            updated_at=updated_at,
        )
        if not changed:
            raise ExternalAgentApiError(
                "EXTERNAL_API_KEY_REVISION_CONFLICT",
                "API Key 已被其他操作更新，請重新整理後再試。",
                status_code=409,
            )
        row = self.store.get_key(normalized_id)
        self.store.audit(
            installation_id=str(self._installation["installation_id"]),
            key_id=normalized_id,
            project_id=str(existing["project_id"]),
            action="api_key.policy.replace",
            status="succeeded",
            details={"enabled": enabled, "scopes": normalized_scopes},
            now=updated_at,
        )
        return {"success": True, "api_key": self._metadata(row)}

    def revoke_key(self, *, key_id: Any, expected_revision: int) -> dict[str, Any]:
        self._ready()
        normalized_id = _safe_id(key_id, "API Key ID")
        row = self.store.get_key(normalized_id)
        if not row:
            raise ExternalAgentApiError(
                "EXTERNAL_API_KEY_NOT_FOUND", "找不到 API Key。", status_code=404
            )
        now = _iso(self.clock())
        if not self.store.revoke_key(
            key_id=normalized_id,
            expected_revision=int(expected_revision),
            now=now,
        ):
            raise ExternalAgentApiError(
                "EXTERNAL_API_KEY_REVISION_CONFLICT",
                "API Key 已被撤銷或由其他操作更新，請重新整理後再試。",
                status_code=409,
            )
        current = self.store.get_key(normalized_id)
        self.store.audit(
            installation_id=str(self._installation["installation_id"]),
            key_id=normalized_id,
            project_id=str(row["project_id"]),
            action="api_key.revoke",
            status="succeeded",
            now=now,
        )
        return {"success": True, "api_key": self._metadata(current)}

    def rotate_key(self, *, key_id: Any, expected_revision: int) -> dict[str, Any]:
        normalized_id = _safe_id(key_id, "API Key ID")
        row = self.store.get_key(normalized_id)
        if not row:
            raise ExternalAgentApiError(
                "EXTERNAL_API_KEY_NOT_FOUND", "找不到 API Key。", status_code=404
            )
        self.revoke_key(key_id=normalized_id, expected_revision=expected_revision)
        try:
            return self.issue_key(
                name=str(row["name"]),
                project_id=str(row["project_id"]),
                scopes=_loads(row["scopes_json"], []),
                expires_at=row.get("expires_at"),
                rate_limit_per_minute=int(row["rate_limit_per_minute"]),
                request_limit_daily=int(row["request_limit_daily"]),
                rotated_from_key_id=normalized_id,
            )
        except Exception:
            # Rotation is deliberately fail-closed: the old credential remains
            # revoked when creation of the replacement fails.
            raise

    @staticmethod
    def _bearer(authorization: Any) -> str:
        value = str(authorization or "")
        if not value.startswith("Bearer ") or value.count(" ") != 1:
            raise ExternalAgentApiError(
                "EXTERNAL_API_AUTH_REQUIRED",
                "請提供有效的 Workbench API Key。",
                status_code=401,
                recoverable=False,
            )
        candidate = value[7:]
        if not _KEY_PATTERN.fullmatch(candidate):
            raise ExternalAgentApiError(
                "EXTERNAL_API_AUTH_INVALID",
                "Workbench API Key 無效。",
                status_code=401,
                recoverable=False,
            )
        return candidate

    def authenticate(
        self,
        authorization: Any,
        *,
        required_scope: str,
        action: str,
    ) -> ExternalApiPrincipal:
        self._ready()
        if required_scope not in SUPPORTED_SCOPES:
            raise RuntimeError("Unsupported server-side external API scope.")
        try:
            candidate = self._bearer(authorization)
        except ExternalAgentApiError as exc:
            self.store.record_auth_failure(
                installation_id=str(self._installation["installation_id"]),
                key_id=None,
                project_id=None,
                action=action,
                error_code=exc.code,
                now=self.clock(),
            )
            raise
        match = _KEY_PATTERN.fullmatch(candidate)
        assert match is not None
        if match.group("tag") != str(self._installation["installation_tag"]):
            row: dict[str, Any] = {}
        else:
            prefix = f"wbk_{match.group('tag')}_{match.group('secret')[:12]}"
            row = self.store.get_key_by_prefix(prefix)
        if not row or not hmac.compare_digest(
            str(row.get("key_digest") or ""), self._digest(candidate)
        ):
            self.store.record_auth_failure(
                installation_id=str(self._installation["installation_id"]),
                key_id=None,
                project_id=None,
                action=action,
                error_code="EXTERNAL_API_AUTH_INVALID",
                now=self.clock(),
            )
            raise ExternalAgentApiError(
                "EXTERNAL_API_AUTH_INVALID",
                "Workbench API Key 無效。",
                status_code=401,
                recoverable=False,
            )
        key_id = str(row["key_id"])
        project_id = str(row["project_id"])
        now = self.clock()
        error: Optional[ExternalAgentApiError] = None
        expires = _parse_time(row.get("expires_at"))
        if str(row["installation_id"]) != str(self._installation["installation_id"]):
            error = ExternalAgentApiError(
                "EXTERNAL_API_INSTALLATION_MISMATCH",
                "此 API Key 不屬於目前的 Workbench 安裝。",
                status_code=401,
                recoverable=False,
            )
        elif row.get("revoked_at") or not bool(row.get("enabled")):
            error = ExternalAgentApiError(
                "EXTERNAL_API_KEY_INACTIVE",
                "此 API Key 已停用或撤銷。",
                status_code=401,
                recoverable=False,
            )
        elif expires and expires <= now:
            error = ExternalAgentApiError(
                "EXTERNAL_API_KEY_EXPIRED",
                "此 API Key 已到期。",
                status_code=401,
                recoverable=False,
            )
        scopes = frozenset(_loads(row.get("scopes_json"), []))
        if error is None and required_scope not in scopes:
            error = ExternalAgentApiError(
                "EXTERNAL_API_SCOPE_DENIED",
                "此 API Key 未取得執行這項操作的權限。",
                status_code=403,
                recoverable=False,
            )
        if error is None and self.policy_guard is None:
            error = ExternalAgentApiError(
                "EXTERNAL_API_POLICY_UNAVAILABLE",
                "目前無法確認此 Project 的整合權限政策。",
                status_code=503,
                recoverable=True,
            )
        elif error is None:
            try:
                assert self.policy_guard is not None
                allowed_by_policy = self.policy_guard(project_id, required_scope)
            except ExternalAgentApiError as exc:
                error = exc
            except Exception:
                error = ExternalAgentApiError(
                    "EXTERNAL_API_POLICY_UNAVAILABLE",
                    "目前無法確認此 Project 的整合權限政策。",
                    status_code=503,
                    recoverable=True,
                )
            else:
                if allowed_by_policy is False:
                    error = ExternalAgentApiError(
                        "EXTERNAL_API_POLICY_DENIED",
                        "此 Project 的整合權限政策不允許這項外部 API 操作。",
                        status_code=403,
                        recoverable=False,
                    )
        if error is None:
            consumed, limit_error, retry_after = self.store.consume_request(
                key_id=key_id, now=now
            )
            if limit_error:
                if limit_error in {"inactive", "not_found"}:
                    error = ExternalAgentApiError(
                        "EXTERNAL_API_KEY_INACTIVE",
                        "此 API Key 已停用或撤銷。",
                        status_code=401,
                        recoverable=False,
                    )
                else:
                    error = ExternalAgentApiError(
                        "EXTERNAL_API_RATE_LIMITED"
                        if limit_error == "minute_limit"
                        else "EXTERNAL_API_DAILY_LIMIT_REACHED",
                        "已達此 API Key 的請求上限，請稍後再試。",
                        status_code=429,
                        retry_after=retry_after,
                    )
            else:
                row = consumed
        if error is not None:
            self.store.record_auth_failure(
                installation_id=str(self._installation["installation_id"]),
                key_id=key_id,
                project_id=project_id,
                action=action,
                error_code=error.code,
                now=self.clock(),
            )
            raise error
        self.store.audit(
            installation_id=str(self._installation["installation_id"]),
            key_id=key_id,
            project_id=project_id,
            action=action,
            status="allowed",
            details={"scope": required_scope},
        )
        return ExternalApiPrincipal(
            installation_id=str(self._installation["installation_id"]),
            key_id=key_id,
            project_id=project_id,
            scopes=scopes,
            key_name=str(row["name"]),
        )

    def bind_run(self, *, principal: ExternalApiPrincipal, run_id: Any) -> None:
        normalized_run = _safe_id(run_id, "Run ID")
        self.store.bind_run(
            run_id=normalized_run,
            project_id=principal.project_id,
            key_id=principal.key_id,
            now=_iso(self.clock()),
        )

    @staticmethod
    def _idempotency_key(value: Any) -> str:
        candidate = str(value or "").strip()
        if (
            not 8 <= len(candidate) <= 128
            or _CONTROL.search(candidate)
            or any(character.isspace() for character in candidate)
        ):
            raise ExternalAgentApiError(
                "EXTERNAL_API_IDEMPOTENCY_KEY_INVALID",
                "請提供 8 到 128 個字元且不含空白的 Idempotency-Key。",
                status_code=422,
                recoverable=False,
            )
        return candidate

    def reserve_idempotent_run(
        self,
        *,
        principal: ExternalApiPrincipal,
        idempotency_key: Any,
        request_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return an existing reservation or bind a new server-generated Run."""

        candidate = self._idempotency_key(idempotency_key)
        try:
            canonical_request = _json(request_payload)
        except (TypeError, ValueError) as exc:
            raise ExternalAgentApiError(
                "EXTERNAL_API_REQUEST_INVALID",
                "Run 請求內容無效。",
                status_code=422,
            ) from exc
        request_digest = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        idempotency_digest = hmac.new(
            self._pepper.encode("utf-8"),
            f"{principal.key_id}\0{candidate}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        with self._lock:
            reservation = self.store.reserve_idempotent_run(
                key_id=principal.key_id,
                idempotency_key_digest=idempotency_digest,
                request_digest=request_digest,
                run_id=f"run_{uuid.uuid4().hex}",
                project_id=principal.project_id,
                now=_iso(self.clock()),
            )
        response = _loads(reservation.get("response_json"), None)
        if str(reservation.get("state") or "") == "dispatch_unknown":
            response = {
                "run_id": str(reservation["run_id"]),
                "project_id": str(reservation["project_id"]),
                "status": "failed",
                "error": {
                    "code": "EXTERNAL_API_DISPATCH_UNKNOWN",
                    "message": "先前程序在工作送出前中斷；系統不會自動重送，請使用新的 Idempotency-Key。",
                    "recoverable": True,
                },
            }
        return {
            "idempotency_id": str(reservation["idempotency_id"]),
            "run_id": str(reservation["run_id"]),
            "project_id": str(reservation["project_id"]),
            "state": str(reservation["state"]),
            "replayed": bool(reservation.get("replayed")),
            "response": response if isinstance(response, Mapping) else None,
            "error_code": reservation.get("error_code"),
        }

    def complete_idempotent_run(
        self,
        *,
        reservation: Mapping[str, Any],
        response: Mapping[str, Any],
        succeeded: bool,
        error_code: Optional[str] = None,
    ) -> None:
        self.store.complete_idempotent_run(
            idempotency_id=_safe_id(
                reservation.get("idempotency_id"), "Idempotency ID"
            ),
            state="dispatched" if succeeded else "dispatch_failed",
            response=response,
            error_code=error_code,
            now=_iso(self.clock()),
        )

    def require_run(self, *, principal: ExternalApiPrincipal, run_id: Any) -> str:
        normalized_run = _safe_id(run_id, "Run ID")
        self.store.require_run_project(
            run_id=normalized_run, project_id=principal.project_id
        )
        return normalized_run

    def mark_run_cancelled(self, *, run_id: str) -> None:
        self.store.mark_run_cancelled(run_id=run_id, now=_iso(self.clock()))

    def record_operation(
        self,
        *,
        principal: ExternalApiPrincipal,
        action: str,
        status: str,
        run_id: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        self.store.audit(
            installation_id=principal.installation_id,
            key_id=principal.key_id,
            project_id=principal.project_id,
            action=action,
            status=status,
            details={"run_id": run_id} if run_id else {},
            error_code=error_code,
        )


__all__ = [
    "ExternalAgentApiError",
    "ExternalAgentApiService",
    "ExternalAgentApiStore",
    "ExternalApiPrincipal",
    "SUPPORTED_SCOPES",
]
