"""SQLite persistence for extension lifecycle, scope, health, and audit."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import database
from extension_catalog import (
    CatalogRecord,
    canonical_catalog_record_bytes,
    catalog_record_contract,
    catalog_record_sha256,
)
from structured_log import redact


PROJECT_MODES = frozenset({"inherit", "enabled", "disabled"})
PERMISSION_LEVELS = frozenset({"blocked", "restricted", "open"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=repr)


def _loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class ExtensionStore:
    """Own extension state without requiring changes to the core DB module."""

    def __init__(self) -> None:
        self._schema_lock = threading.Lock()
        self._schema_ready_for: Optional[str] = None

    def ensure_schema(self) -> None:
        database_identity = str(database.DB_PATH)
        if self._schema_ready_for == database_identity:
            return
        with self._schema_lock:
            if self._schema_ready_for == database_identity:
                return
            with database.get_db_conn() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS extension_installations (
                        extension_id TEXT PRIMARY KEY,
                        manifest_json TEXT NOT NULL,
                        manifest_sha256 TEXT NOT NULL,
                        contract_type TEXT NOT NULL DEFAULT 'manifest-v1',
                        origin TEXT NOT NULL,
                        source_kind TEXT NOT NULL,
                        source_ref TEXT,
                        configuration_enabled INTEGER NOT NULL DEFAULT 1,
                        installed INTEGER NOT NULL DEFAULT 0,
                        trusted_manifest_sha256 TEXT,
                        trusted_at TEXT,
                        trusted_by TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS extension_global_state (
                        extension_id TEXT PRIMARY KEY,
                        global_enabled INTEGER NOT NULL DEFAULT 0,
                        approved_manifest_sha256 TEXT,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS extension_project_state (
                        extension_id TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        approved_manifest_sha256 TEXT,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (extension_id, project_id)
                    );
                    CREATE TABLE IF NOT EXISTS extension_project_permissions (
                        extension_id TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        permission_level TEXT NOT NULL DEFAULT 'restricted',
                        revision INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (extension_id, project_id)
                    );
                    CREATE TABLE IF NOT EXISTS extension_health (
                        extension_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        detail_json TEXT NOT NULL,
                        checked_at TEXT NOT NULL,
                        latency_ms INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS extension_audits (
                        id TEXT PRIMARY KEY,
                        extension_id TEXT NOT NULL,
                        project_id TEXT,
                        action TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        error_json TEXT,
                        manifest_sha256 TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_extension_audits_extension
                        ON extension_audits(extension_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_extension_audits_project
                        ON extension_audits(project_id, created_at DESC);
                    """
                )
                self._ensure_column(
                    conn,
                    "extension_installations",
                    "contract_type",
                    "TEXT NOT NULL DEFAULT 'manifest-v1'",
                )
                self._ensure_column(
                    conn,
                    "extension_installations",
                    "configuration_enabled",
                    "INTEGER NOT NULL DEFAULT 1",
                )
                for table in ("extension_global_state", "extension_project_state"):
                    self._ensure_column(
                        conn,
                        table,
                        "approved_manifest_sha256",
                        "TEXT",
                    )
            self._schema_ready_for = database_identity

    @staticmethod
    def _ensure_column(
        conn: Any,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def upsert_catalog_entry(
        self,
        record: CatalogRecord,
        *,
        source_kind: str,
        source_ref: str = "",
        implicit_trust: bool = False,
        migrate_existing_configuration: bool = False,
        configuration_enabled: bool = True,
    ) -> None:
        """Synchronize one trusted catalog definition.

        ``migrate_existing_configuration`` is honored only for a brand-new DB
        row.  It preserves a configuration the user had already enabled before
        Extension Center existed; later digest changes always fail closed.
        """

        self.ensure_schema()
        digest = catalog_record_sha256(record)
        manifest_json = canonical_catalog_record_bytes(record).decode("utf-8")
        contract_type = catalog_record_contract(record)
        now = _now()
        with database.get_db_conn() as conn:
            before = self._snapshot_conn(conn, record.id)
            migrate = bool(migrate_existing_configuration and before is None)
            trusted = digest if (implicit_trust or migrate) else None
            trusted_by = (
                "migration_existing_user_configuration"
                if migrate
                else "workbench" if implicit_trust else None
            )
            conn.execute(
                """
                INSERT INTO extension_installations (
                    extension_id, manifest_json, manifest_sha256, contract_type,
                    origin, source_kind, source_ref, configuration_enabled, installed,
                    trusted_manifest_sha256, trusted_at, trusted_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(extension_id) DO UPDATE SET
                    manifest_json=excluded.manifest_json,
                    manifest_sha256=excluded.manifest_sha256,
                    contract_type=excluded.contract_type,
                    origin=excluded.origin,
                    source_kind=excluded.source_kind,
                    source_ref=excluded.source_ref,
                    configuration_enabled=excluded.configuration_enabled,
                    trusted_manifest_sha256=CASE
                        WHEN excluded.trusted_by='workbench'
                        THEN excluded.trusted_manifest_sha256
                        ELSE extension_installations.trusted_manifest_sha256
                    END,
                    trusted_at=CASE
                        WHEN excluded.trusted_by='workbench'
                        THEN excluded.trusted_at
                        ELSE extension_installations.trusted_at
                    END,
                    trusted_by=CASE
                        WHEN excluded.trusted_by='workbench'
                        THEN excluded.trusted_by
                        ELSE extension_installations.trusted_by
                    END,
                    updated_at=excluded.updated_at
                """,
                (
                    record.id,
                    manifest_json,
                    digest,
                    contract_type,
                    record.origin,
                    source_kind,
                    source_ref or None,
                    int(configuration_enabled),
                    int(record.default_installed),
                    trusted,
                    now if trusted else None,
                    trusted_by,
                    now,
                    now,
                ),
            )
            initial_enabled = bool(
                record.default_installed
                and trusted
                and (record.default_enabled or migrate)
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO extension_global_state
                    (extension_id, global_enabled, approved_manifest_sha256, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    record.id,
                    int(initial_enabled),
                    digest if initial_enabled else None,
                    now,
                ),
            )
            if before and before["manifest_sha256"] != digest:
                conn.execute(
                    """
                    UPDATE extension_global_state
                    SET global_enabled=0, approved_manifest_sha256=NULL, updated_at=?
                    WHERE extension_id=?
                    """,
                    (now, record.id),
                )
                conn.execute(
                    """
                    UPDATE extension_project_state
                    SET mode=CASE WHEN mode='enabled' THEN 'inherit' ELSE mode END,
                        approved_manifest_sha256=NULL, updated_at=?
                    WHERE extension_id=?
                    """,
                    (now, record.id),
                )
                self._audit_conn(
                    conn,
                    record.id,
                    "manifest_changed",
                    "workbench_sync",
                    before=before,
                    after=self._snapshot_conn(conn, record.id),
                )
            elif migrate:
                self._audit_conn(
                    conn,
                    record.id,
                    "migration_existing_user_configuration",
                    "workbench_migration",
                    before=None,
                    after=self._snapshot_conn(conn, record.id),
                )

    def mark_missing_settings(self, active_ids: Iterable[str]) -> None:
        self._mark_missing_source("settings", active_ids)

    def mark_missing_local(self, active_ids: Iterable[str]) -> None:
        self._mark_missing_source("local_file", active_ids)

    def _mark_missing_source(
        self,
        source_kind: str,
        active_ids: Iterable[str],
    ) -> None:
        if source_kind not in {"settings", "local_file"}:
            raise ValueError(f"unsupported extension source kind: {source_kind}")
        self.ensure_schema()
        active = set(active_ids)
        with database.get_db_conn() as conn:
            rows = conn.execute(
                "SELECT extension_id FROM extension_installations WHERE source_kind=?",
                (source_kind,),
            ).fetchall()
            for row in rows:
                extension_id = str(row["extension_id"])
                if extension_id in active:
                    continue
                before = self._snapshot_conn(conn, extension_id)
                if before and not before["installed"] and not before["global_enabled"]:
                    continue
                now = _now()
                conn.execute(
                    """
                    UPDATE extension_installations
                    SET installed=0, configuration_enabled=0,
                        trusted_manifest_sha256=NULL,
                        trusted_at=NULL, trusted_by=NULL, updated_at=?
                    WHERE extension_id=?
                    """,
                    (now, extension_id),
                )
                conn.execute(
                    """
                    UPDATE extension_global_state
                    SET global_enabled=0, approved_manifest_sha256=NULL, updated_at=?
                    WHERE extension_id=?
                    """,
                    (now, extension_id),
                )
                conn.execute(
                    "DELETE FROM extension_project_state WHERE extension_id=?",
                    (extension_id,),
                )
                conn.execute(
                    "DELETE FROM extension_project_permissions WHERE extension_id=?",
                    (extension_id,),
                )
                conn.execute(
                    "DELETE FROM extension_health WHERE extension_id=?",
                    (extension_id,),
                )
                self._audit_conn(
                    conn,
                    extension_id,
                    "source_missing",
                    "workbench_sync",
                    before=before,
                    after=self._snapshot_conn(conn, extension_id),
                )

    def get(self, extension_id: str) -> Optional[dict[str, Any]]:
        self.ensure_schema()
        with database.get_db_conn() as conn:
            row = conn.execute(
                """
                SELECT i.*, COALESCE(g.global_enabled, 0) AS global_enabled,
                       g.approved_manifest_sha256 AS global_approved_manifest_sha256,
                       h.status AS health_status, h.detail_json AS health_detail_json,
                       h.checked_at AS health_checked_at, h.latency_ms AS health_latency_ms
                FROM extension_installations i
                LEFT JOIN extension_global_state g USING(extension_id)
                LEFT JOIN extension_health h USING(extension_id)
                WHERE i.extension_id=?
                """,
                (extension_id,),
            ).fetchone()
        return self._row(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        self.ensure_schema()
        with database.get_db_conn() as conn:
            rows = conn.execute(
                """
                SELECT i.*, COALESCE(g.global_enabled, 0) AS global_enabled,
                       g.approved_manifest_sha256 AS global_approved_manifest_sha256,
                       h.status AS health_status, h.detail_json AS health_detail_json,
                       h.checked_at AS health_checked_at, h.latency_ms AS health_latency_ms
                FROM extension_installations i
                LEFT JOIN extension_global_state g USING(extension_id)
                LEFT JOIN extension_health h USING(extension_id)
                ORDER BY i.extension_id
                """
            ).fetchall()
        results = [self._row(row) for row in rows]
        return sorted(
            results,
            key=lambda item: (
                str((item.get("manifest") or {}).get("name") or "").casefold(),
                item["extension_id"],
            ),
        )

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["manifest"] = _loads(item.pop("manifest_json", None), {})
        item["installed"] = bool(item.get("installed"))
        item["configuration_enabled"] = bool(item.get("configuration_enabled", 1))
        item["global_enabled"] = bool(item.get("global_enabled"))
        item["trusted"] = bool(
            item.get("trusted_manifest_sha256")
            and item.get("trusted_manifest_sha256") == item.get("manifest_sha256")
        )
        status = item.pop("health_status", None)
        item["health"] = {
            "status": status or "unchecked",
            "detail": _loads(item.pop("health_detail_json", None), {}),
            "checked_at": item.pop("health_checked_at", None),
            "latency_ms": int(item.pop("health_latency_ms", 0) or 0),
        }
        return item

    def project_state(
        self,
        extension_id: str,
        project_id: Optional[str],
    ) -> dict[str, Optional[str]]:
        if not project_id:
            return {"mode": "inherit", "approved_manifest_sha256": None}
        self.ensure_schema()
        with database.get_db_conn() as conn:
            row = conn.execute(
                """
                SELECT mode, approved_manifest_sha256
                FROM extension_project_state
                WHERE extension_id=? AND project_id=?
                """,
                (extension_id, project_id),
            ).fetchone()
        return (
            {
                "mode": str(row["mode"]),
                "approved_manifest_sha256": row["approved_manifest_sha256"],
            }
            if row
            else {"mode": "inherit", "approved_manifest_sha256": None}
        )

    def install(self, extension_id: str, *, actor: str = "local_user") -> None:
        self._change_installation(extension_id, installed=True, action="install", actor=actor)

    def remove(self, extension_id: str, *, actor: str = "local_user") -> None:
        self._change_installation(extension_id, installed=False, action="remove", actor=actor)

    def _change_installation(
        self,
        extension_id: str,
        *,
        installed: bool,
        action: str,
        actor: str,
    ) -> None:
        self.ensure_schema()
        now = _now()
        with database.get_db_conn() as conn:
            before = self._snapshot_conn(conn, extension_id)
            if before is None:
                raise KeyError(extension_id)
            conn.execute(
                "UPDATE extension_installations SET installed=?, updated_at=? WHERE extension_id=?",
                (int(installed), now, extension_id),
            )
            if not installed:
                conn.execute(
                    """
                    UPDATE extension_global_state
                    SET global_enabled=0, approved_manifest_sha256=NULL, updated_at=?
                    WHERE extension_id=?
                    """,
                    (now, extension_id),
                )
                conn.execute(
                    """
                    UPDATE extension_installations
                    SET trusted_manifest_sha256=NULL, trusted_at=NULL,
                        trusted_by=NULL, updated_at=?
                    WHERE extension_id=?
                    """,
                    (now, extension_id),
                )
                conn.execute(
                    "DELETE FROM extension_project_state WHERE extension_id=?",
                    (extension_id,),
                )
                conn.execute(
                    "DELETE FROM extension_health WHERE extension_id=?",
                    (extension_id,),
                )
            self._audit_conn(
                conn,
                extension_id,
                action,
                actor,
                before=before,
                after=self._snapshot_conn(conn, extension_id),
            )

    def trust(
        self,
        extension_id: str,
        digest: str,
        *,
        trusted_by: str = "local_user",
    ) -> None:
        self.ensure_schema()
        now = _now()
        with database.get_db_conn() as conn:
            before = self._snapshot_conn(conn, extension_id)
            if before is None:
                raise KeyError(extension_id)
            if before["manifest_sha256"] != digest:
                raise ValueError("manifest hash does not match the installed manifest")
            conn.execute(
                """
                UPDATE extension_installations
                SET trusted_manifest_sha256=?, trusted_at=?, trusted_by=?, updated_at=?
                WHERE extension_id=?
                """,
                (digest, now, trusted_by[:80], now, extension_id),
            )
            self._audit_conn(
                conn,
                extension_id,
                "trust",
                trusted_by,
                before=before,
                after=self._snapshot_conn(conn, extension_id),
            )

    def set_global(
        self,
        extension_id: str,
        enabled: bool,
        *,
        approved_manifest_sha256: Optional[str] = None,
        actor: str = "local_user",
    ) -> None:
        self.ensure_schema()
        now = _now()
        with database.get_db_conn() as conn:
            before = self._snapshot_conn(conn, extension_id)
            if before is None:
                raise KeyError(extension_id)
            if enabled and (
                approved_manifest_sha256 != before["manifest_sha256"]
                or before["trusted_manifest_sha256"] != before["manifest_sha256"]
            ):
                raise ValueError("enabled state must approve the current trusted manifest")
            conn.execute(
                """
                INSERT INTO extension_global_state(
                    extension_id, global_enabled, approved_manifest_sha256, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(extension_id) DO UPDATE SET
                    global_enabled=excluded.global_enabled,
                    approved_manifest_sha256=excluded.approved_manifest_sha256,
                    updated_at=excluded.updated_at
                """,
                (
                    extension_id,
                    int(enabled),
                    approved_manifest_sha256 if enabled else None,
                    now,
                ),
            )
            self._audit_conn(
                conn,
                extension_id,
                "enable" if enabled else "disable",
                actor,
                before=before,
                after=self._snapshot_conn(conn, extension_id),
            )

    def set_project_mode(
        self,
        extension_id: str,
        project_id: str,
        mode: str,
        *,
        approved_manifest_sha256: Optional[str] = None,
        actor: str = "local_user",
    ) -> None:
        if mode not in PROJECT_MODES:
            raise ValueError(f"unsupported project extension mode: {mode}")
        self.ensure_schema()
        now = _now()
        with database.get_db_conn() as conn:
            snapshot = self._snapshot_conn(conn, extension_id)
            if snapshot is None:
                raise KeyError(extension_id)
            before = {
                "mode": self._project_mode_conn(conn, extension_id, project_id)
            }
            if mode == "enabled" and (
                approved_manifest_sha256 != snapshot["manifest_sha256"]
                or snapshot["trusted_manifest_sha256"] != snapshot["manifest_sha256"]
            ):
                raise ValueError("project enable must approve the current trusted manifest")
            if mode == "inherit":
                conn.execute(
                    """
                    DELETE FROM extension_project_state
                    WHERE extension_id=? AND project_id=?
                    """,
                    (extension_id, project_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO extension_project_state(
                        extension_id, project_id, mode,
                        approved_manifest_sha256, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(extension_id, project_id) DO UPDATE SET
                        mode=excluded.mode,
                        approved_manifest_sha256=excluded.approved_manifest_sha256,
                        updated_at=excluded.updated_at
                    """,
                    (
                        extension_id,
                        project_id,
                        mode,
                        approved_manifest_sha256 if mode == "enabled" else None,
                        now,
                    ),
                )
            self._audit_conn(
                conn,
                extension_id,
                "project_override",
                actor,
                project_id=project_id,
                before=before,
                after={"mode": mode},
            )

    def project_permission(self, extension_id: str, project_id: Optional[str]) -> dict[str, Any]:
        self.ensure_schema()
        if not project_id:
            return {"level": "restricted", "revision": 0, "updated_at": None}
        with database.get_db_conn() as conn:
            row = conn.execute(
                """
                SELECT permission_level, revision, updated_at
                FROM extension_project_permissions
                WHERE extension_id=? AND project_id=?
                """,
                (extension_id, project_id),
            ).fetchone()
        if not row:
            return {"level": "restricted", "revision": 0, "updated_at": None}
        return {
            "level": str(row["permission_level"]),
            "revision": int(row["revision"]),
            "updated_at": row["updated_at"],
        }

    def set_project_permission(
        self,
        extension_id: str,
        project_id: str,
        level: str,
        *,
        expected_revision: int,
        actor: str = "local_user",
    ) -> dict[str, Any]:
        if level not in PERMISSION_LEVELS:
            raise ValueError(f"unsupported extension permission level: {level}")
        self.ensure_schema()
        now = _now()
        with database.get_db_conn() as conn:
            snapshot = self._snapshot_conn(conn, extension_id)
            if snapshot is None:
                raise KeyError(extension_id)
            row = conn.execute(
                """
                SELECT permission_level, revision, updated_at
                FROM extension_project_permissions
                WHERE extension_id=? AND project_id=?
                """,
                (extension_id, project_id),
            ).fetchone()
            current = {
                "level": str(row["permission_level"]) if row else "restricted",
                "revision": int(row["revision"]) if row else 0,
                "updated_at": row["updated_at"] if row else None,
            }
            if int(expected_revision) != current["revision"]:
                raise ValueError("extension permission revision changed; reload and try again")
            next_revision = current["revision"] + 1
            conn.execute(
                """
                INSERT INTO extension_project_permissions(
                    extension_id, project_id, permission_level, revision, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(extension_id, project_id) DO UPDATE SET
                    permission_level=excluded.permission_level,
                    revision=excluded.revision,
                    updated_at=excluded.updated_at
                """,
                (extension_id, project_id, level, next_revision, now),
            )
            after = {"level": level, "revision": next_revision, "updated_at": now}
            self._audit_conn(
                conn,
                extension_id,
                "project_permission_level",
                actor,
                project_id=project_id,
                before=current,
                after=after,
            )
        return after

    def set_health(
        self,
        extension_id: str,
        status: str,
        detail: Any,
        latency_ms: int,
        *,
        actor: str = "local_user",
        audit_status: str = "completed",
    ) -> None:
        self.ensure_schema()
        now = _now()
        safe_detail = redact(detail)
        with database.get_db_conn() as conn:
            if self._snapshot_conn(conn, extension_id) is None:
                raise KeyError(extension_id)
            conn.execute(
                """
                INSERT INTO extension_health(extension_id, status, detail_json, checked_at, latency_ms)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(extension_id) DO UPDATE SET
                    status=excluded.status, detail_json=excluded.detail_json,
                    checked_at=excluded.checked_at, latency_ms=excluded.latency_ms
                """,
                (
                    extension_id,
                    status[:40],
                    _json(safe_detail),
                    now,
                    max(0, int(latency_ms)),
                ),
            )
            self._audit_conn(
                conn,
                extension_id,
                "health",
                actor,
                after={"status": status, "detail": safe_detail, "latency_ms": latency_ms},
                status=audit_status,
                error=safe_detail if audit_status == "failed" else None,
            )

    def record_failure(
        self,
        extension_id: str,
        action: str,
        error: Any,
        *,
        actor: str = "local_user",
        project_id: Optional[str] = None,
    ) -> None:
        self.ensure_schema()
        with database.get_db_conn() as conn:
            self._audit_conn(
                conn,
                extension_id,
                action,
                actor,
                project_id=project_id,
                status="failed",
                error=error,
            )

    def list_audits(self, extension_id: str, limit: int = 100) -> list[dict[str, Any]]:
        self.ensure_schema()
        with database.get_db_conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM extension_audits
                WHERE extension_id=?
                ORDER BY created_at DESC LIMIT ?
                """,
                (extension_id, max(1, min(int(limit), 500))),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json", None), {})
            item["error"] = _loads(item.pop("error_json", None), None)
            results.append(item)
        return results

    @staticmethod
    def _snapshot_conn(conn: Any, extension_id: str) -> Optional[dict[str, Any]]:
        row = conn.execute(
            """
            SELECT i.extension_id, i.manifest_sha256, i.contract_type,
                   i.origin, i.source_kind, i.configuration_enabled, i.installed,
                   i.trusted_manifest_sha256,
                   COALESCE(g.global_enabled, 0) AS global_enabled,
                   g.approved_manifest_sha256 AS global_approved_manifest_sha256
            FROM extension_installations i
            LEFT JOIN extension_global_state g USING(extension_id)
            WHERE i.extension_id=?
            """,
            (extension_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        for key in ("configuration_enabled", "installed", "global_enabled"):
            result[key] = bool(result[key])
        return result

    @staticmethod
    def _project_mode_conn(conn: Any, extension_id: str, project_id: str) -> str:
        row = conn.execute(
            "SELECT mode FROM extension_project_state WHERE extension_id=? AND project_id=?",
            (extension_id, project_id),
        ).fetchone()
        return str(row["mode"]) if row else "inherit"

    @staticmethod
    def _audit_conn(
        conn: Any,
        extension_id: str,
        action: str,
        actor: str,
        *,
        project_id: Optional[str] = None,
        before: Any = None,
        after: Any = None,
        status: str = "completed",
        error: Any = None,
    ) -> None:
        snapshot = after if isinstance(after, dict) else before
        digest = snapshot.get("manifest_sha256") if isinstance(snapshot, dict) else None
        payload = redact({"before": before, "after": after})
        conn.execute(
            """
            INSERT INTO extension_audits (
                id, extension_id, project_id, action, actor, status,
                payload_json, error_json, manifest_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"extaudit_{uuid.uuid4().hex}",
                extension_id,
                project_id,
                action[:64],
                actor[:80],
                status[:32],
                _json(payload),
                _json(redact(error)) if error is not None else None,
                digest,
                _now(),
            ),
        )
