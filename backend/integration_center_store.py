"""Additive SQLite persistence for Project-scoped integration policies.

The store contains identifiers and permission scopes only.  OAuth credentials,
API keys, connector tokens and MCP secret aliases remain in their authoritative
stores and are deliberately outside this schema.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, ContextManager, Mapping, Optional, Sequence

from database import get_db_conn


PERMISSION_MODES = frozenset({"blocked", "restricted", "open"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_INTEGRATION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_CAPABILITY_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")


class IntegrationCenterStoreError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class IntegrationPolicyConflict(IntegrationCenterStoreError):
    def __init__(self, message: str = "整合權限方案已變更，請重新載入後再試。") -> None:
        super().__init__("INTEGRATION_POLICY_REVISION_CONFLICT", message, status_code=409)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _text(value: Any, label: str, *, maximum: int, required: bool = True) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", f"必須提供{label}。")
    if len(result) > maximum or _CONTROL.search(result):
        raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", f"{label}無效。")
    return result


def normalize_project_id(value: Any) -> str:
    return _text(value, "Project ID", maximum=512)


def normalize_policy(policy: Mapping[str, Any], *, project_id: str) -> dict[str, Any]:
    """Normalize an untrusted policy without accepting credential-shaped data."""

    if not isinstance(policy, Mapping):
        raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "整合權限方案必須是物件。")
    project = normalize_project_id(project_id)
    name = _text(policy.get("name") or "Project 整合權限", "方案名稱", maximum=160)
    mode = _text(policy.get("permission_mode") or "blocked", "權限模式", maximum=16).casefold()
    if mode not in PERMISSION_MODES:
        raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "權限模式無效。")
    raw_grants = policy.get("grants") or []
    if not isinstance(raw_grants, Sequence) or isinstance(raw_grants, (str, bytes)):
        raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "整合放行項目必須是清單。")
    if len(raw_grants) > 100:
        raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "整合放行項目最多 100 項。")

    grants: list[dict[str, Any]] = []
    seen_grants: set[tuple[str, str]] = set()
    for raw in raw_grants:
        if not isinstance(raw, Mapping):
            raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "每個整合放行項目都必須是物件。")
        integration_id = _text(raw.get("integration_id"), "整合 ID", maximum=96).casefold()
        if not _INTEGRATION_ID.fullmatch(integration_id):
            raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "整合 ID 無效。")
        connection_id = _text(raw.get("connection_id"), "連線 ID", maximum=512, required=False) or None
        grant_key = (integration_id, connection_id or "")
        if grant_key in seen_grants:
            raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "同一個整合連線不可重複放行。")
        seen_grants.add(grant_key)

        raw_capabilities = raw.get("capabilities") or []
        if not isinstance(raw_capabilities, Sequence) or isinstance(raw_capabilities, (str, bytes)):
            raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "能力項目必須是清單。")
        if len(raw_capabilities) > 128:
            raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "每個整合放行項目最多可選 128 項能力。")
        capabilities: list[str] = []
        for item in raw_capabilities:
            capability = _text(item, "能力 ID", maximum=128).casefold()
            if capability == "*" or not _CAPABILITY_ID.fullmatch(capability):
                raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "能力 ID 無效。")
            capabilities.append(capability)
        capabilities = sorted(set(capabilities))

        raw_resources = raw.get("resources") or []
        if not isinstance(raw_resources, Sequence) or isinstance(raw_resources, (str, bytes)):
            raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "資源範圍必須是清單。")
        if len(raw_resources) > 500:
            raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "每個整合放行項目最多可選 500 個資源。")
        resources: list[dict[str, str]] = []
        seen_resources: set[tuple[str, str]] = set()
        for raw_resource in raw_resources:
            if not isinstance(raw_resource, Mapping):
                raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "每個資源範圍都必須是物件。")
            resource_type = _text(raw_resource.get("resource_type"), "資源類型", maximum=64).casefold()
            if not _INTEGRATION_ID.fullmatch(resource_type):
                raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "資源類型無效。")
            resource_id = _text(raw_resource.get("resource_id"), "資源 ID", maximum=1024)
            resource_key = (resource_type, resource_id)
            if resource_key in seen_resources:
                raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "同一個資源不可重複放行。")
            seen_resources.add(resource_key)
            resources.append({"resource_type": resource_type, "resource_id": resource_id})
        resources.sort(key=lambda item: (item["resource_type"], item["resource_id"]))
        grants.append(
            {
                "integration_id": integration_id,
                "connection_id": connection_id,
                "capabilities": capabilities,
                "resources": resources,
            }
        )
    grants.sort(key=lambda item: (item["integration_id"], item.get("connection_id") or ""))
    return {
        "project_id": project,
        "name": name,
        "permission_mode": mode,
        "grants": grants,
    }


def policy_scope_sha256(policy: Mapping[str, Any]) -> str:
    safe = {
        "project_id": policy.get("project_id"),
        "name": policy.get("name"),
        "permission_mode": policy.get("permission_mode"),
        "grants": policy.get("grants") or [],
    }
    return hashlib.sha256(_json(safe).encode("utf-8")).hexdigest()


class IntegrationCenterStore:
    def __init__(
        self,
        connection_factory: Optional[Callable[[], ContextManager[Any]]] = None,
    ) -> None:
        self._connection_factory = connection_factory or get_db_conn
        self._schema_lock = threading.RLock()
        self._schema_ready = False

    def _ensure_schema(self, conn: Any) -> None:
        conn.execute("PRAGMA foreign_keys = ON")
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS integration_center_policies (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    permission_mode TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS integration_center_policy_grants (
                    grant_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    integration_id TEXT NOT NULL,
                    connection_key TEXT NOT NULL DEFAULT '',
                    connection_id TEXT,
                    capabilities_json TEXT NOT NULL,
                    resources_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, integration_id, connection_key),
                    FOREIGN KEY(project_id) REFERENCES integration_center_policies(project_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_integration_grants_project
                    ON integration_center_policy_grants(project_id, integration_id);

                CREATE TABLE IF NOT EXISTS integration_center_audits (
                    audit_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    status TEXT NOT NULL,
                    policy_revision INTEGER,
                    scope_sha256 TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_integration_audits_project
                    ON integration_center_audits(project_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS integration_center_apply_state (
                    project_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    active_revision INTEGER NOT NULL,
                    pending_revision INTEGER,
                    error_code TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._schema_ready = True

    def ensure_schema(self) -> None:
        with self._connection_factory() as conn:
            self._ensure_schema(conn)

    @staticmethod
    def _default(project_id: str) -> dict[str, Any]:
        return {
            "project_id": project_id,
            "name": "Project 整合權限",
            "permission_mode": "blocked",
            "grants": [],
            "revision": 0,
            "created_at": None,
            "updated_at": None,
        }

    def _read_policy(self, conn: Any, project_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM integration_center_policies WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            return self._default(project_id)
        grant_rows = conn.execute(
            """
            SELECT integration_id, connection_id, capabilities_json, resources_json
            FROM integration_center_policy_grants
            WHERE project_id = ?
            ORDER BY integration_id, connection_key
            """,
            (project_id,),
        ).fetchall()
        return {
            "project_id": project_id,
            "name": str(row["name"]),
            "permission_mode": str(row["permission_mode"]),
            "grants": [
                {
                    "integration_id": str(item["integration_id"]),
                    "connection_id": item["connection_id"],
                    "capabilities": _loads(item["capabilities_json"], []),
                    "resources": _loads(item["resources_json"], []),
                }
                for item in grant_rows
            ],
            "revision": int(row["revision"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def get_policy(self, project_id: str) -> dict[str, Any]:
        project = normalize_project_id(project_id)
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            return self._read_policy(conn, project)

    def get_apply_state(self, project_id: str) -> dict[str, Any]:
        project = normalize_project_id(project_id)
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            policy = self._read_policy(conn, project)
            row = conn.execute(
                "SELECT * FROM integration_center_apply_state WHERE project_id = ?",
                (project,),
            ).fetchone()
        if row is None:
            return {
                "project_id": project,
                "status": "active",
                "active_revision": int(policy["revision"]),
                "pending_revision": None,
                "error_code": None,
                "updated_at": policy.get("updated_at"),
            }
        return {
            "project_id": project,
            "status": str(row["status"]),
            "active_revision": int(row["active_revision"]),
            "pending_revision": row["pending_revision"],
            "error_code": row["error_code"],
            "updated_at": str(row["updated_at"]),
        }

    def block_interrupted_applies(self) -> int:
        """Fail closed policies left mid-apply by an earlier process."""

        now = _iso()
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT project_id, active_revision
                  FROM integration_center_apply_state
                 WHERE status = 'applying'
                """
            ).fetchall()
            for row in rows:
                project = str(row["project_id"])
                revision = int(row["active_revision"] or 0)
                conn.execute(
                    """
                    UPDATE integration_center_apply_state
                       SET status='blocked', pending_revision=NULL,
                           error_code='INTEGRATION_POLICY_APPLY_INTERRUPTED',
                           updated_at=?
                     WHERE project_id=? AND status='applying'
                    """,
                    (now, project),
                )
                conn.execute(
                    """
                    INSERT INTO integration_center_audits (
                        audit_id, project_id, action, actor, status,
                        policy_revision, details_json, error_code, created_at
                    ) VALUES (?, ?, 'policy.reconcile', 'startup', 'failed',
                              ?, '{}', 'INTEGRATION_POLICY_APPLY_INTERRUPTED', ?)
                    """,
                    (f"iaudit_{uuid.uuid4().hex}", project, revision, now),
                )
            return len(rows)

    def begin_apply(self, project_id: str, *, expected_revision: int) -> dict[str, Any]:
        project = normalize_project_id(project_id)
        expected = int(expected_revision)
        now = _iso()
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            current = self._read_policy(conn, project)
            if int(current["revision"]) != expected:
                raise IntegrationPolicyConflict()
            state = conn.execute(
                "SELECT status FROM integration_center_apply_state WHERE project_id = ?",
                (project,),
            ).fetchone()
            if state is not None and str(state["status"]) == "applying":
                raise IntegrationPolicyConflict("另一項整合權限方案正在套用，請稍後再試。")
            conn.execute(
                """
                INSERT INTO integration_center_apply_state (
                    project_id, status, active_revision, pending_revision, error_code, updated_at
                ) VALUES (?, 'applying', ?, ?, NULL, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    status='applying', active_revision=excluded.active_revision,
                    pending_revision=excluded.pending_revision, error_code=NULL,
                    updated_at=excluded.updated_at
                """,
                (project, expected, expected + 1, now),
            )
        return self.get_apply_state(project)

    def finish_apply_failure(
        self,
        project_id: str,
        *,
        active_revision: int,
        compensated: bool,
        error_code: str,
    ) -> dict[str, Any]:
        project = normalize_project_id(project_id)
        status = "active" if compensated else "blocked"
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO integration_center_apply_state (
                    project_id, status, active_revision, pending_revision, error_code, updated_at
                ) VALUES (?, ?, ?, NULL, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    status=excluded.status, active_revision=excluded.active_revision,
                    pending_revision=NULL, error_code=excluded.error_code,
                    updated_at=excluded.updated_at
                """,
                (project, status, int(active_revision), error_code, _iso()),
            )
        return self.get_apply_state(project)

    def replace_policy(
        self,
        *,
        project_id: str,
        expected_revision: int,
        policy: Mapping[str, Any],
        actor: str = "local_session",
        audit_action: str = "policy.replace",
    ) -> dict[str, Any]:
        project = normalize_project_id(project_id)
        normalized = normalize_policy(policy, project_id=project)
        revision = int(expected_revision)
        if revision < 0:
            raise IntegrationCenterStoreError("INTEGRATION_POLICY_INVALID", "整合權限方案版本無效。")
        now = _iso()
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            current = self._read_policy(conn, project)
            if int(current["revision"]) != revision:
                raise IntegrationPolicyConflict()
            next_revision = revision + 1
            if revision == 0:
                conn.execute(
                    """
                    INSERT INTO integration_center_policies (
                        project_id, name, permission_mode, revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (project, normalized["name"], normalized["permission_mode"], next_revision, now, now),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE integration_center_policies
                    SET name = ?, permission_mode = ?, revision = ?, updated_at = ?
                    WHERE project_id = ? AND revision = ?
                    """,
                    (normalized["name"], normalized["permission_mode"], next_revision, now, project, revision),
                )
                if cursor.rowcount != 1:
                    raise IntegrationPolicyConflict()
            conn.execute("DELETE FROM integration_center_policy_grants WHERE project_id = ?", (project,))
            for grant in normalized["grants"]:
                conn.execute(
                    """
                    INSERT INTO integration_center_policy_grants (
                        grant_id, project_id, integration_id, connection_key, connection_id,
                        capabilities_json, resources_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"grant_{uuid.uuid4().hex}",
                        project,
                        grant["integration_id"],
                        grant.get("connection_id") or "",
                        grant.get("connection_id"),
                        _json(grant["capabilities"]),
                        _json(grant["resources"]),
                        now,
                    ),
                )
            saved = self._read_policy(conn, project)
            conn.execute(
                """
                INSERT INTO integration_center_audits (
                    audit_id, project_id, action, actor, status, policy_revision,
                    scope_sha256, details_json, created_at
                ) VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?)
                """,
                (
                    f"iaudit_{uuid.uuid4().hex}",
                    project,
                    _text(audit_action, "稽核動作", maximum=128),
                    _text(actor, "操作者", maximum=128),
                    next_revision,
                    policy_scope_sha256(saved),
                    _json(
                        {
                            "permission_mode": saved["permission_mode"],
                            "integration_count": len(saved["grants"]),
                            "capability_count": sum(len(item["capabilities"]) for item in saved["grants"]),
                            "resource_count": sum(len(item["resources"]) for item in saved["grants"]),
                        }
                    ),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO integration_center_apply_state (
                    project_id, status, active_revision, pending_revision, error_code, updated_at
                ) VALUES (?, 'active', ?, NULL, NULL, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    status='active', active_revision=excluded.active_revision,
                    pending_revision=NULL, error_code=NULL, updated_at=excluded.updated_at
                """,
                (project, next_revision, now),
            )
            return saved

    def audit_failure(
        self,
        *,
        project_id: str,
        action: str,
        actor: str,
        error_code: str,
        policy_revision: Optional[int] = None,
    ) -> None:
        project = normalize_project_id(project_id)
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO integration_center_audits (
                    audit_id, project_id, action, actor, status, policy_revision,
                    details_json, error_code, created_at
                ) VALUES (?, ?, ?, ?, 'failed', ?, '{}', ?, ?)
                """,
                (
                    f"iaudit_{uuid.uuid4().hex}",
                    project,
                    _text(action, "動作", maximum=128),
                    _text(actor, "操作者", maximum=128),
                    policy_revision,
                    _text(error_code, "錯誤代碼", maximum=128),
                    _iso(),
                ),
            )

    def list_audits(self, project_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        project = normalize_project_id(project_id)
        bounded = max(1, min(int(limit), 500))
        with self._connection_factory() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT * FROM integration_center_audits
                WHERE project_id = ?
                ORDER BY created_at DESC, audit_id DESC
                LIMIT ?
                """,
                (project, bounded),
            ).fetchall()
        return [
            {
                "audit_id": str(row["audit_id"]),
                "project_id": str(row["project_id"]),
                "action": str(row["action"]),
                "actor": str(row["actor"]),
                "status": str(row["status"]),
                "policy_revision": row["policy_revision"],
                "scope_sha256": row["scope_sha256"],
                "details": _loads(row["details_json"], {}),
                "error_code": row["error_code"],
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]


__all__ = [
    "IntegrationCenterStore",
    "IntegrationCenterStoreError",
    "IntegrationPolicyConflict",
    "PERMISSION_MODES",
    "normalize_policy",
    "normalize_project_id",
    "policy_scope_sha256",
]
