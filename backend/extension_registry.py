"""Unified catalog and lifecycle manager for Workbench extensions."""

from __future__ import annotations

import copy
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import paths
from extension_catalog import (
    CatalogRecord,
    builtin_catalog_records,
    catalog_metadata,
    catalog_record_sha256,
    enabled_settings_extension_ids,
    settings_manifests,
)
from extension_manifest import ExtensionManifest, manifest_sha256, parse_extension_manifest
from extension_store import ExtensionStore, PROJECT_MODES


MAX_LOCAL_MANIFEST_BYTES = 256 * 1024


class ExtensionError(RuntimeError):
    code = "EXTENSION_ERROR"


class ExtensionNotFound(ExtensionError):
    code = "EXTENSION_NOT_FOUND"


class ExtensionConflict(ExtensionError):
    code = "EXTENSION_CONFLICT"


class ExtensionTrustRequired(ExtensionError):
    code = "EXTENSION_TRUST_REQUIRED"


class ExtensionDisabled(ExtensionError):
    code = "EXTENSION_DISABLED"


class ExtensionUnavailable(ExtensionDisabled):
    code = "EXTENSION_UNAVAILABLE"


class ExtensionManifestRejected(ExtensionError):
    code = "EXTENSION_MANIFEST_REJECTED"


class ExtensionRegistry:
    """Synchronize trusted definitions and enforce persisted lifecycle state.

    Call :meth:`sync` during application startup and after settings changes.
    Runtime authorization uses :meth:`require_enabled`, which deliberately does
    not scan files or write to SQLite on the hot path.
    """

    def __init__(
        self,
        load_settings: Callable[[], dict[str, Any]],
        *,
        save_settings: Optional[Callable[[dict[str, Any]], Any]] = None,
        apply_configuration: Optional[Callable[[dict[str, Any]], Any]] = None,
        require_project: Optional[Callable[[str], Any]] = None,
        local_dir: Optional[Path] = None,
        store: Optional[ExtensionStore] = None,
        health_probes: Optional[Mapping[str, Callable[[dict[str, Any]], Any]]] = None,
        state_change_handler: Optional[
            Callable[[str, bool, dict[str, Any]], Any]
        ] = None,
        state_rollback_handler: Optional[
            Callable[[str, bool, dict[str, Any]], Any]
        ] = None,
        project_state_change_handler: Optional[
            Callable[[str, str, str, dict[str, Any]], Any]
        ] = None,
    ):
        self.load_settings = load_settings
        self.save_settings = save_settings
        self.apply_configuration = apply_configuration
        self.require_project = require_project
        self._enforce_runtime_local_dir = local_dir is None
        self.local_dir = (
            Path(local_dir)
            if local_dir is not None
            else Path(paths.RUNTIME_ROOT) / "extensions" / "local"
        )
        self.store = store or ExtensionStore()
        self.health_probes = dict(health_probes or {})
        self.state_change_handler = state_change_handler
        self.state_rollback_handler = state_rollback_handler
        self.project_state_change_handler = project_state_change_handler
        self._local_candidates: dict[str, ExtensionManifest] = {}
        self._local_sources: dict[str, str] = {}
        self._local_errors: list[dict[str, str]] = []
        self._active_records: dict[str, CatalogRecord] = {}
        self._sync_lock = threading.RLock()

    def initialize(self) -> dict[str, CatalogRecord]:
        """Create additive tables and synchronize the current local catalog."""

        return self.sync()

    def sync(self) -> dict[str, CatalogRecord]:
        with self._sync_lock:
            settings = self.load_settings()
            if not isinstance(settings, Mapping):
                raise ExtensionConflict("settings loader did not return an object")
            configured = settings_manifests(settings)
            configured_by_id: dict[str, ExtensionManifest] = {}
            for manifest in configured:
                if manifest.id in configured_by_id:
                    raise ExtensionConflict(f"duplicate extension ID: {manifest.id}")
                configured_by_id[manifest.id] = manifest

            scanned_local, local_sources, local_errors = self._scan_local_manifests()
            valid_local: dict[str, ExtensionManifest] = {}
            valid_sources: dict[str, str] = {}
            for extension_id, manifest in scanned_local.items():
                try:
                    self._validate_settings_binding(manifest, configured_by_id)
                    valid_local[extension_id] = manifest
                    valid_sources[extension_id] = local_sources[extension_id]
                except ExtensionError as exc:
                    local_errors.append(
                        {
                            "filename": local_sources.get(extension_id, ""),
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                        }
                    )

            self._local_candidates = valid_local
            self._local_sources = valid_sources
            self._local_errors = local_errors
            self.store.mark_missing_local(valid_local)
            self.store.mark_missing_settings(
                manifest.id
                for manifest in configured
                if manifest.id not in valid_local
            )

            records: dict[str, CatalogRecord] = {}
            for record in builtin_catalog_records():
                if record.id in records:
                    raise ExtensionConflict(f"duplicate extension ID: {record.id}")
                records[record.id] = record
                source_kind = (
                    "builtin_connector"
                    if record.id.startswith("connector.")
                    else "builtin"
                )
                self.store.upsert_catalog_entry(
                    record,
                    source_kind=source_kind,
                    source_ref=record.entrypoint.adapter,
                    implicit_trust=True,
                    migrate_existing_configuration=record.id
                    in {"builtin.ollama"},
                    configuration_enabled=True,
                )

            migrated_enabled = enabled_settings_extension_ids(settings)
            for configured_manifest in configured:
                record = valid_local.get(configured_manifest.id, configured_manifest)
                if record.id in records:
                    raise ExtensionConflict(f"duplicate extension ID: {record.id}")
                records[record.id] = record
                local_override = record.id in valid_local
                self.store.upsert_catalog_entry(
                    record,
                    source_kind="local_file" if local_override else "settings",
                    source_ref=(
                        valid_sources.get(record.id, "")
                        if local_override
                        else str(record.entrypoint.settings_id or "")
                    ),
                    implicit_trust=False,
                    migrate_existing_configuration=(
                        not local_override and record.id in migrated_enabled
                    ),
                    configuration_enabled=record.id in migrated_enabled,
                )

            self._active_records = records
            return dict(records)

    def catalog(self, project_id: Optional[str] = None) -> dict[str, Any]:
        self._validate_project(project_id)
        active_records = self.sync()
        rows = {
            item["extension_id"]: item
            for item in self.store.list()
            if item["extension_id"] in active_records
        }
        items = [self._item(row, project_id) for row in rows.values()]
        items.sort(key=lambda item: (str(item["name"]).casefold(), item["id"]))
        return {
            "success": True,
            "catalog_version": 1,
            "extensions": items,
            "sections": {
                "installed": [item for item in items if item["installed"]],
                "available": [
                    item for item in items if item["available"] and not item["installed"]
                ],
                "local": [item for item in items if item["origin"] == "local"],
                "connectors": [item for item in items if item["kind"] == "connector"],
                "unavailable": [
                    item for item in items if not item["runtime_available"]
                ],
            },
            "local_errors": self._local_errors,
        }

    def inspect_local(self, filename: str, project_id: Optional[str] = None) -> dict[str, Any]:
        self._validate_project(project_id)
        path = self._safe_local_path(filename)
        manifest = self._read_local_manifest(path)
        configured = {
            item.id: item for item in settings_manifests(self.load_settings())
        }
        self._validate_settings_binding(manifest, configured)
        self.sync()
        row = self.store.get(manifest.id)
        if row and row["manifest_sha256"] != manifest_sha256(manifest):
            raise ExtensionConflict(
                f"extension ID already belongs to another manifest: {manifest.id}"
            )
        return self._item(row, project_id) if row else self._candidate_item(manifest)

    def install(
        self,
        extension_id: str,
        expected_sha256: str,
        *,
        actor: str = "local_user",
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            self.sync()
            record, source_kind, source_ref = self._find_record(extension_id)
            metadata = catalog_metadata(extension_id)
            if not metadata["runtime_available"]:
                raise ExtensionUnavailable(
                    str(metadata.get("availability_reason") or "extension unavailable")
                )
            digest = catalog_record_sha256(record)
            if digest != expected_sha256:
                raise ExtensionConflict("manifest changed after it was reviewed")
            row = self.store.get(extension_id)
            if row and row["manifest_sha256"] != digest:
                raise ExtensionConflict(
                    "extension ID is already installed with a different manifest"
                )
            if not row:
                self.store.upsert_catalog_entry(
                    record,
                    source_kind=source_kind,
                    source_ref=source_ref,
                    implicit_trust=source_kind in {"builtin", "builtin_connector"},
                )
            self.store.install(extension_id, actor=actor)
            return self.get(extension_id, project_id)
        except Exception as exc:
            self._record_failure(extension_id, "install", exc, actor=actor)
            raise

    def trust(
        self,
        extension_id: str,
        expected_sha256: str,
        *,
        trusted_by: str = "local_user",
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            self.sync()
            row = self._required_row(extension_id)
            if row["manifest_sha256"] != expected_sha256:
                raise ExtensionConflict("manifest changed after it was reviewed")
            self.store.trust(extension_id, expected_sha256, trusted_by=trusted_by)
            return self.get(extension_id, project_id)
        except Exception as exc:
            self._record_failure(extension_id, "trust", exc, actor=trusted_by)
            raise

    def set_global(
        self,
        extension_id: str,
        enabled: bool,
        *,
        expected_sha256: Optional[str] = None,
        actor: str = "local_user",
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        action = "enable" if enabled else "disable"
        try:
            # Lifecycle callbacks may persist settings and enqueue runtime
            # reconciliation.  Serialize the complete transition so a failed
            # request cannot compensate over a newer successful request.
            with self._sync_lock:
                self.sync()
                row = self._required_row(extension_id)
                if enabled:
                    self._require_runtime_available(extension_id)
                    if not row["installed"]:
                        raise ExtensionConflict(
                            "install the extension before enabling it"
                        )
                    if not row["trusted"]:
                        raise ExtensionTrustRequired(
                            "trust the current manifest before enabling it"
                        )
                    if expected_sha256 != row["manifest_sha256"]:
                        raise ExtensionConflict(
                            "enable must approve the current manifest"
                        )

                previous_enabled = bool(row.get("global_enabled"))
                previous_approved_sha256 = row.get(
                    "global_approved_manifest_sha256"
                )
                settings_before = copy.deepcopy(self.load_settings())
                if not isinstance(settings_before, Mapping):
                    raise ExtensionConflict("settings loader did not return an object")
                settings_mirror = self._mcp_settings_mirror_snapshot(
                    row,
                    settings_before,
                )

                try:
                    self.store.set_global(
                        extension_id,
                        enabled,
                        approved_manifest_sha256=expected_sha256,
                        actor=actor,
                    )
                except ValueError as exc:
                    raise ExtensionConflict(str(exc)) from exc
                try:
                    self._apply_runtime_state(extension_id, enabled)
                    # State handlers may update a settings-backed availability
                    # mirror.  Synchronize it inside the compensated section,
                    # then return without another fallible catalog scan.
                    self.sync()
                except Exception as exc:
                    self._rollback_global_state(
                        extension_id,
                        previous_enabled=previous_enabled,
                        previous_approved_sha256=previous_approved_sha256,
                        settings_mirror=settings_mirror,
                        fail_closed=not enabled,
                        original_error=exc,
                    )
                    raise
                return self.get(
                    extension_id,
                    project_id,
                    synchronize=False,
                )
        except Exception as exc:
            self._record_failure(extension_id, action, exc, actor=actor)
            raise

    def _rollback_global_state(
        self,
        extension_id: str,
        *,
        previous_enabled: bool,
        previous_approved_sha256: Optional[str],
        settings_mirror: Optional[dict[str, Any]],
        fail_closed: bool,
        original_error: Exception,
    ) -> None:
        """Compensate a failed lifecycle transition without restoring grants.

        A failed grant is restored exactly.  A failed revocation remains
        disabled because its cleanup may already have stopped a process,
        downgraded policy, or consumed approval state.
        """

        rollback_errors: list[Exception] = []
        desired_enabled = False if fail_closed else previous_enabled
        desired_mirror_enabled = (
            False
            if fail_closed
            else bool((settings_mirror or {}).get("enabled"))
        )

        # The DB was already changed to disabled for a revocation.  Never reopen
        # that authorization gate merely because cleanup failed.
        if not fail_closed:
            try:
                self.store.set_global(
                    extension_id,
                    previous_enabled,
                    approved_manifest_sha256=(
                        previous_approved_sha256 if previous_enabled else None
                    ),
                    actor="runtime_rollback",
                )
            except Exception as exc:
                rollback_errors.append(exc)
                self._record_failure(
                    extension_id,
                    "global_rollback_db",
                    exc,
                    actor="runtime_rollback",
                )
                # If exact restoration itself fails, retry with the most
                # restrictive state.  This cannot recover a persistent DB
                # outage, but a transient approval/digest failure must never
                # leave a failed grant authorized.
                try:
                    self.store.set_global(
                        extension_id,
                        False,
                        actor="runtime_rollback_fail_closed",
                    )
                    desired_enabled = False
                    desired_mirror_enabled = False
                except Exception as fail_closed_exc:
                    rollback_errors.append(fail_closed_exc)
                    self._record_failure(
                        extension_id,
                        "global_rollback_fail_closed",
                        fail_closed_exc,
                        actor="runtime_rollback",
                    )

        # Only the reviewed MCP entry's enabled mirror is changed.  The
        # settings ID and configuration digest must still match the manifest,
        # so unrelated concurrent settings writes are preserved.
        if settings_mirror is not None:
            try:
                self._set_mcp_settings_mirror(
                    settings_mirror,
                    desired_mirror_enabled,
                )
            except Exception as exc:
                rollback_errors.append(exc)
                self._record_failure(
                    extension_id,
                    "global_rollback_settings",
                    exc,
                    actor="runtime_rollback",
                )

        # Applications may provide a side-effect-free rollback callback (MCP
        # uses it only to enqueue reconciliation).  Older injectors retain the
        # desired-state reverse-call behavior for backward compatibility.
        compensation_handler = (
            self.state_rollback_handler or self.state_change_handler
        )
        if compensation_handler is not None:
            try:
                compensation_handler(
                    extension_id,
                    (
                        desired_mirror_enabled
                        if settings_mirror is not None
                        else desired_enabled
                    ),
                    self._item(self._required_row(extension_id), None),
                )
            except Exception as exc:
                rollback_errors.append(exc)
                self._record_failure(
                    extension_id,
                    "global_rollback_handler",
                    exc,
                    actor="runtime_rollback",
                )

        if self.apply_configuration is not None:
            try:
                current_settings = self.load_settings()
                if not isinstance(current_settings, Mapping):
                    raise ExtensionConflict(
                        "settings loader did not return an object"
                    )
                self.apply_configuration(copy.deepcopy(dict(current_settings)))
            except Exception as exc:
                rollback_errors.append(exc)
                self._record_failure(
                    extension_id,
                    "global_rollback_runtime",
                    exc,
                    actor="runtime_rollback",
                )

        try:
            self.sync()
        except Exception as exc:
            rollback_errors.append(exc)
            self._record_failure(
                extension_id,
                "global_rollback_sync",
                exc,
                actor="runtime_rollback",
            )

        if rollback_errors and hasattr(original_error, "add_note"):
            original_error.add_note(
                "Lifecycle rollback encountered: "
                + ", ".join(type(exc).__name__ for exc in rollback_errors)
            )

    def _mcp_settings_mirror_snapshot(
        self,
        row: Mapping[str, Any],
        settings: Mapping[str, Any],
    ) -> Optional[dict[str, Any]]:
        manifest = row.get("manifest")
        entrypoint = (
            manifest.get("entrypoint")
            if isinstance(manifest, Mapping)
            else None
        )
        if (
            not isinstance(entrypoint, Mapping)
            or entrypoint.get("type") != "mcp_settings"
        ):
            return None
        settings_id = str(entrypoint.get("settings_id") or "").strip()
        configuration_sha256 = str(
            entrypoint.get("configuration_sha256") or ""
        )
        if not settings_id or not configuration_sha256:
            raise ExtensionConflict("MCP settings binding is incomplete")
        matches = [
            item
            for item in settings.get("mcp_servers") or []
            if isinstance(item, Mapping)
            and str(item.get("id") or "").strip().casefold()
            == settings_id.casefold()
        ]
        if len(matches) != 1:
            raise ExtensionConflict("MCP settings binding is unavailable or ambiguous")
        self._require_mcp_configuration_digest(
            settings,
            settings_id=settings_id,
            configuration_sha256=configuration_sha256,
        )
        return {
            "settings_id": settings_id,
            "configuration_sha256": configuration_sha256,
            "enabled": bool(matches[0].get("enabled")),
        }

    def _set_mcp_settings_mirror(
        self,
        snapshot: Mapping[str, Any],
        enabled: bool,
    ) -> None:
        if self.save_settings is None:
            raise ExtensionConflict("settings persistence is unavailable")
        current = self.load_settings()
        if not isinstance(current, Mapping):
            raise ExtensionConflict("settings loader did not return an object")
        settings_id = str(snapshot.get("settings_id") or "").strip()
        configuration_sha256 = str(
            snapshot.get("configuration_sha256") or ""
        )
        self._require_mcp_configuration_digest(
            current,
            settings_id=settings_id,
            configuration_sha256=configuration_sha256,
        )

        cfg = copy.deepcopy(dict(current))
        matches = 0
        servers: list[Any] = []
        for raw in cfg.get("mcp_servers") or []:
            if not isinstance(raw, Mapping):
                servers.append(raw)
                continue
            server = dict(raw)
            if (
                str(server.get("id") or "").strip().casefold()
                == settings_id.casefold()
            ):
                server["enabled"] = bool(enabled)
                matches += 1
            servers.append(server)
        if matches != 1:
            raise ExtensionConflict("MCP settings binding is unavailable or ambiguous")
        cfg["mcp_servers"] = servers
        self.save_settings(cfg)

    @staticmethod
    def _require_mcp_configuration_digest(
        settings: Mapping[str, Any],
        *,
        settings_id: str,
        configuration_sha256: str,
    ) -> None:
        matches = [
            manifest
            for manifest in settings_manifests(settings)
            if manifest.entrypoint.type == "mcp_settings"
            and str(manifest.entrypoint.settings_id or "").strip().casefold()
            == settings_id.casefold()
        ]
        if len(matches) != 1 or (
            matches[0].entrypoint.configuration_sha256
            != configuration_sha256
        ):
            raise ExtensionConflict(
                "MCP configuration changed during the lifecycle transition"
            )

    def set_project_mode(
        self,
        extension_id: str,
        project_id: str,
        mode: str,
        *,
        expected_sha256: Optional[str] = None,
        actor: str = "local_user",
    ) -> dict[str, Any]:
        try:
            with self._sync_lock:
                if mode not in PROJECT_MODES:
                    raise ExtensionConflict(
                        f"unsupported project extension mode: {mode}"
                    )
                self._validate_project(project_id)
                self.sync()
                row = self._required_row(extension_id)
                if mode == "enabled":
                    self._require_runtime_available(extension_id)
                    if not row["installed"]:
                        raise ExtensionConflict(
                            "install the extension before enabling it"
                        )
                    if expected_sha256 != row["manifest_sha256"]:
                        raise ExtensionConflict(
                            "project enable must approve the current manifest"
                        )
                    if not row["trusted"]:
                        raise ExtensionTrustRequired(
                            "trust the current manifest before enabling it"
                        )

                previous_state = self.store.project_state(extension_id, project_id)
                try:
                    self.store.set_project_mode(
                        extension_id,
                        project_id,
                        mode,
                        approved_manifest_sha256=expected_sha256,
                        actor=actor,
                    )
                except ValueError as exc:
                    raise ExtensionConflict(str(exc)) from exc
                item = self._item(self._required_row(extension_id), project_id)
                if self.project_state_change_handler is not None:
                    try:
                        self.project_state_change_handler(
                            extension_id,
                            project_id,
                            mode,
                            item,
                        )
                    except Exception as exc:
                        self._rollback_project_state(
                            extension_id,
                            project_id,
                            previous_state=previous_state,
                            fail_closed=mode == "disabled",
                            original_error=exc,
                        )
                        raise
                return item
        except Exception as exc:
            self._record_failure(
                extension_id,
                "project_override",
                exc,
                actor=actor,
                project_id=project_id,
            )
            raise

    def _rollback_project_state(
        self,
        extension_id: str,
        project_id: str,
        *,
        previous_state: Mapping[str, Optional[str]],
        fail_closed: bool,
        original_error: Exception,
    ) -> None:
        previous_mode = str(previous_state.get("mode") or "inherit")
        previous_approved_sha256 = previous_state.get(
            "approved_manifest_sha256"
        )
        rollback_errors: list[Exception] = []
        desired_mode = "disabled" if fail_closed else previous_mode
        if not fail_closed:
            try:
                self.store.set_project_mode(
                    extension_id,
                    project_id,
                    previous_mode,
                    approved_manifest_sha256=(
                        previous_approved_sha256
                        if previous_mode == "enabled"
                        else None
                    ),
                    actor="runtime_rollback",
                )
            except Exception as exc:
                rollback_errors.append(exc)
                self._record_failure(
                    extension_id,
                    "project_rollback_db",
                    exc,
                    actor="runtime_rollback",
                    project_id=project_id,
                )
                try:
                    self.store.set_project_mode(
                        extension_id,
                        project_id,
                        "disabled",
                        actor="runtime_rollback_fail_closed",
                    )
                    desired_mode = "disabled"
                except Exception as fail_closed_exc:
                    rollback_errors.append(fail_closed_exc)
                    self._record_failure(
                        extension_id,
                        "project_rollback_fail_closed",
                        fail_closed_exc,
                        actor="runtime_rollback",
                        project_id=project_id,
                    )

        # MCP project handlers only enqueue reconciliation.  A second callback
        # converges on either the restored grant state or the retained disabled
        # state when a revocation callback failed.
        if self.project_state_change_handler is not None:
            try:
                self.project_state_change_handler(
                    extension_id,
                    project_id,
                    desired_mode,
                    self._item(self._required_row(extension_id), project_id),
                )
            except Exception as exc:
                rollback_errors.append(exc)
                self._record_failure(
                    extension_id,
                    "project_rollback_handler",
                    exc,
                    actor="runtime_rollback",
                    project_id=project_id,
                )

        if rollback_errors and hasattr(original_error, "add_note"):
            original_error.add_note(
                "Project lifecycle rollback encountered: "
                + ", ".join(type(exc).__name__ for exc in rollback_errors)
            )

    def get(
        self,
        extension_id: str,
        project_id: Optional[str] = None,
        *,
        synchronize: bool = True,
    ) -> dict[str, Any]:
        self._validate_project(project_id)
        if synchronize:
            self.sync()
        return self._item(self._required_row(extension_id), project_id)

    def is_effectively_enabled(
        self,
        extension_id: str,
        project_id: Optional[str] = None,
    ) -> bool:
        """Fast runtime gate: no settings reload, directory scan, or DB write."""

        try:
            self._validate_project(project_id)
            return bool(
                self._item(self._required_row(extension_id), project_id)[
                    "effective_enabled"
                ]
            )
        except ExtensionNotFound:
            return False

    def require_enabled(
        self,
        extension_id: str,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self._validate_project(project_id)
        item = self._item(self._required_row(extension_id), project_id)
        if not item["effective_enabled"]:
            if not item["runtime_available"]:
                raise ExtensionUnavailable(
                    str(item.get("availability_reason") or "extension unavailable")
                )
            raise ExtensionDisabled(
                f"extension is not enabled for this project: {extension_id}"
            )
        return item

    def refresh_health(
        self,
        extension_id: str,
        *,
        actor: str = "local_user",
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        item = self.get(extension_id, project_id)
        started = time.monotonic()
        try:
            if not item["runtime_available"]:
                status, detail = "unavailable", {
                    "reason": item.get("availability_reason") or "adapter_unavailable"
                }
            elif not item["installed"]:
                status, detail = "unavailable", {"reason": "not_installed"}
            elif not item["effective_enabled"]:
                status, detail = "disabled", {"reason": "not_effectively_enabled"}
            else:
                status, detail = self._run_health_probe(item)
            self.store.set_health(
                extension_id,
                status,
                detail,
                int((time.monotonic() - started) * 1000),
                actor=actor,
            )
        except Exception as exc:
            detail = {"error_type": type(exc).__name__, "error": str(exc)}
            self.store.set_health(
                extension_id,
                "error",
                detail,
                int((time.monotonic() - started) * 1000),
                actor=actor,
                audit_status="failed",
            )
        return self.get(extension_id, project_id)

    def audits(self, extension_id: str, limit: int = 100) -> list[dict[str, Any]]:
        self.sync()
        self._required_row(extension_id)
        return self.store.list_audits(extension_id, limit)

    def remove(
        self,
        extension_id: str,
        *,
        actor: str = "local_user",
    ) -> dict[str, Any]:
        try:
            self.sync()
            row = self._required_row(extension_id)
            if not row["manifest"].get("removable"):
                raise ExtensionConflict(
                    "this extension cannot be removed; disable it instead"
                )
            # Revoke runtime authority before touching settings or credentials.
            # Cleanup can fail (for example DPAPI or filesystem errors), but a
            # failed removal must never leave the extension executable.
            self.store.set_global(
                extension_id,
                False,
                actor="remove_fail_closed",
            )
            self._apply_runtime_state(extension_id, False)
            if row["source_kind"] == "settings":
                self._remove_settings_backed(row)
            self.store.remove(extension_id, actor=actor)
        except Exception as exc:
            self._record_failure(extension_id, "remove", exc, actor=actor)
            raise
        return {"success": True, "extension_id": extension_id, "removed": True}

    def _item(self, row: dict[str, Any], project_id: Optional[str]) -> dict[str, Any]:
        manifest = dict(row["manifest"])
        metadata = catalog_metadata(row["extension_id"])
        project_state = self.store.project_state(row["extension_id"], project_id)
        mode = str(project_state["mode"])
        digest = row["manifest_sha256"]
        global_approval_current = row.get("global_approved_manifest_sha256") == digest
        project_approval_current = (
            mode != "enabled"
            or project_state["approved_manifest_sha256"] == digest
        )
        runtime_available = bool(metadata["runtime_available"])
        effective = bool(
            runtime_available
            and row.get("configuration_enabled", True)
            and row["installed"]
            and row["trusted"]
            and row["global_enabled"]
            and global_approval_current
            and mode != "disabled"
            and project_approval_current
        )
        health = row["health"]
        if not runtime_available:
            health = {
                "status": "unavailable",
                "detail": {
                    "reason": metadata.get("availability_reason")
                    or "adapter_unavailable"
                },
                "checked_at": health.get("checked_at"),
                "latency_ms": int(health.get("latency_ms") or 0),
            }
        return {
            **manifest,
            **metadata,
            "contract_type": row.get("contract_type") or "manifest-v1",
            "manifest_sha256": digest,
            "installed": row["installed"],
            "configuration_enabled": row.get("configuration_enabled", True),
            "available": bool(not row["installed"] and runtime_available),
            "trusted": row["trusted"],
            "global_enabled": row["global_enabled"],
            "global_approval_current": global_approval_current,
            "project_override": mode,
            "project_approval_current": project_approval_current,
            "effective_enabled": effective,
            "health": health,
            "source_kind": row["source_kind"],
        }

    def _candidate_item(self, manifest: ExtensionManifest) -> dict[str, Any]:
        metadata = catalog_metadata(manifest.id)
        return {
            **manifest.model_dump(mode="json"),
            **metadata,
            "contract_type": "manifest-v1",
            "manifest_sha256": manifest_sha256(manifest),
            "installed": False,
            "configuration_enabled": True,
            "available": bool(metadata["runtime_available"]),
            "trusted": False,
            "global_enabled": False,
            "global_approval_current": False,
            "project_override": "inherit",
            "project_approval_current": True,
            "effective_enabled": False,
            "health": {
                "status": "unchecked",
                "detail": {},
                "checked_at": None,
                "latency_ms": 0,
            },
            "source_kind": "local_file",
        }

    def _find_record(self, extension_id: str) -> tuple[CatalogRecord, str, str]:
        record = self._active_records.get(extension_id)
        if record is None:
            raise ExtensionNotFound(f"extension was not found: {extension_id}")
        if extension_id in self._local_candidates:
            return record, "local_file", self._local_sources.get(extension_id, "")
        if extension_id.startswith("connector."):
            return record, "builtin_connector", record.entrypoint.adapter
        if extension_id.startswith("builtin."):
            return record, "builtin", record.entrypoint.adapter
        return record, "settings", str(record.entrypoint.settings_id or "")

    def _required_row(self, extension_id: str) -> dict[str, Any]:
        row = self.store.get(extension_id)
        if not row:
            raise ExtensionNotFound(f"extension was not found: {extension_id}")
        return row

    def _validate_project(self, project_id: Optional[str]) -> None:
        if (
            project_id
            and self.require_project is not None
            and not self.require_project(project_id)
        ):
            raise ExtensionNotFound(f"project was not found: {project_id}")

    def _require_runtime_available(self, extension_id: str) -> None:
        metadata = catalog_metadata(extension_id)
        if not metadata["runtime_available"]:
            raise ExtensionUnavailable(
                str(metadata.get("availability_reason") or "extension unavailable")
            )

    @staticmethod
    def _validate_settings_binding(
        manifest: ExtensionManifest,
        configured: Mapping[str, ExtensionManifest],
    ) -> None:
        target = configured.get(manifest.id)
        if target is None:
            raise ExtensionManifestRejected(
                "local manifest ID must equal its configured MCP/provider extension ID"
            )
        source_entrypoint = manifest.entrypoint
        target_entrypoint = target.entrypoint
        if (
            source_entrypoint.type != target_entrypoint.type
            or source_entrypoint.adapter != target_entrypoint.adapter
            or source_entrypoint.settings_id != target_entrypoint.settings_id
            or source_entrypoint.configuration_sha256
            != target_entrypoint.configuration_sha256
        ):
            raise ExtensionManifestRejected(
                "local manifest entrypoint does not match the current settings digest"
            )
        required = {
            permission.id: permission.risk for permission in target.permissions
        }
        declared = {
            permission.id: permission.risk for permission in manifest.permissions
        }
        if any(
            declared.get(permission_id) != risk
            for permission_id, risk in required.items()
        ):
            raise ExtensionManifestRejected(
                "local manifest omitted or weakened an adapter permission"
            )

    def _apply_runtime_state(self, extension_id: str, enabled: bool) -> None:
        if self.state_change_handler is not None:
            self.state_change_handler(
                extension_id,
                enabled,
                self._item(self._required_row(extension_id), None),
            )
        if self.apply_configuration is not None:
            self.apply_configuration(self.load_settings())

    def _record_failure(
        self,
        extension_id: str,
        action: str,
        exc: Exception,
        *,
        actor: str,
        project_id: Optional[str] = None,
    ) -> None:
        try:
            self.store.record_failure(
                extension_id,
                action,
                {"error_type": type(exc).__name__, "error": str(exc)},
                actor=actor,
                project_id=project_id,
            )
        except Exception:
            # Audit failure cannot replace the original lifecycle error.
            pass

    def _scan_local_manifests(
        self,
    ) -> tuple[
        dict[str, ExtensionManifest],
        dict[str, str],
        list[dict[str, str]],
    ]:
        manifests: dict[str, ExtensionManifest] = {}
        sources: dict[str, str] = {}
        errors: list[dict[str, str]] = []
        if not self.local_dir.exists():
            return manifests, sources, errors
        for path in sorted(self.local_dir.glob("*.json")):
            try:
                manifest = self._read_local_manifest(self._safe_local_path(path.name))
                if manifest.id in manifests:
                    raise ExtensionConflict(
                        f"duplicate local extension ID: {manifest.id}"
                    )
                manifests[manifest.id] = manifest
                sources[manifest.id] = path.name
            except Exception as exc:
                errors.append(
                    {
                        "filename": path.name,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    }
                )
        return manifests, sources, errors

    def _safe_local_path(self, filename: str) -> Path:
        value = str(filename or "").strip()
        if not value or Path(value).name != value or not value.lower().endswith(".json"):
            raise ExtensionManifestRejected(
                "local manifest filename must be one JSON basename"
            )
        if "/" in value or "\\" in value or ".." in value:
            raise ExtensionManifestRejected("local manifest traversal is not allowed")
        unresolved_base = self.local_dir
        if unresolved_base.is_symlink() or self._is_reparse_point(unresolved_base):
            raise ExtensionManifestRejected(
                "local extension directory links are not allowed"
            )
        base = unresolved_base.resolve(strict=False)
        if self._enforce_runtime_local_dir:
            runtime_root = Path(paths.RUNTIME_ROOT).resolve(strict=False)
            try:
                base.relative_to(runtime_root)
            except ValueError as exc:
                raise ExtensionManifestRejected(
                    "local extension directory escaped the runtime root"
                ) from exc
        candidate_unresolved = base / value
        if candidate_unresolved.is_symlink() or self._is_reparse_point(candidate_unresolved):
            raise ExtensionManifestRejected("local manifest links are not allowed")
        candidate = candidate_unresolved.resolve(strict=False)
        if candidate.parent != base:
            raise ExtensionManifestRejected("local manifest escaped its trusted directory")
        if not candidate.is_file():
            raise ExtensionNotFound(f"local manifest was not found: {value}")
        return candidate

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            attributes = getattr(
                path.stat(follow_symlinks=False), "st_file_attributes", 0
            )
            return bool(
                attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
        except OSError:
            return False

    @staticmethod
    def _read_local_manifest(path: Path) -> ExtensionManifest:
        try:
            path_before = path.stat(follow_symlinks=False)
            if (
                stat.S_ISLNK(path_before.st_mode)
                or not stat.S_ISREG(path_before.st_mode)
                or getattr(path_before, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise ExtensionManifestRejected(
                    "local manifest must be a regular file, not a link"
                )
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                snapshot = stream.read(MAX_LOCAL_MANIFEST_BYTES + 1)
                after = os.fstat(stream.fileno())
            path_after = path.stat(follow_symlinks=False)
            if len(snapshot) > MAX_LOCAL_MANIFEST_BYTES:
                raise ExtensionManifestRejected("local manifest is too large")
            def identity(value: Any) -> tuple[Any, ...]:
                return (
                    value.st_dev,
                    value.st_ino,
                    value.st_size,
                    getattr(value, "st_mtime_ns", None),
                )
            if (
                identity(path_before) != identity(before)
                or identity(before) != identity(after)
                or identity(after) != identity(path_after)
                or len(snapshot) != after.st_size
            ):
                raise ExtensionManifestRejected(
                    "local manifest changed while it was read"
                )
            payload = json.loads(snapshot.decode("utf-8-sig"))
            manifest = parse_extension_manifest(payload)
        except ExtensionManifestRejected:
            raise
        except Exception as exc:
            raise ExtensionManifestRejected(f"invalid local manifest: {exc}") from exc
        if manifest.origin != "local":
            raise ExtensionManifestRejected(
                "local manifest files must declare origin=local"
            )
        if manifest.default_installed or manifest.default_enabled:
            raise ExtensionManifestRejected(
                "local manifests require explicit install, trust, and enable actions"
            )
        return manifest

    def _run_health_probe(self, item: dict[str, Any]) -> tuple[str, Any]:
        probe_name = str(item.get("health_probe") or "static")
        adapter = str((item.get("entrypoint") or {}).get("adapter") or "")
        custom = (
            self.health_probes.get(item["id"])
            or self.health_probes.get(adapter)
            or self.health_probes.get(probe_name)
        )
        if custom:
            result = custom(item)
            if isinstance(result, tuple) and len(result) == 2:
                return str(result[0]), result[1]
            if isinstance(result, Mapping) and result.get("status"):
                return str(result["status"]), dict(result)
            return "ready", result
        probe = getattr(self, f"_probe_{probe_name}", None)
        if not callable(probe):
            return "unavailable", {"reason": "health_probe_not_registered"}
        return probe(item)

    @staticmethod
    def _probe_n8n(_item: dict[str, Any]) -> tuple[str, Any]:
        return "unavailable", {"reason": "managed_n8n_probe_not_registered"}

    @staticmethod
    def _probe_cursor(_item: dict[str, Any]) -> tuple[str, Any]:
        return "unavailable", {"reason": "cursor_adapter_not_implemented"}

    @staticmethod
    def _probe_excel(_item: dict[str, Any]) -> tuple[str, Any]:
        return "unavailable", {"reason": "excel_adapter_not_implemented"}

    def _probe_ollama(self, _item: dict[str, Any]) -> tuple[str, Any]:
        import requests

        base_url = str(self.load_settings().get("ollama_url") or "").rstrip("/")
        session = requests.Session()
        session.trust_env = False
        response = session.get(f"{base_url}/api/version", timeout=5)
        return (
            "ready" if response.ok else "unavailable",
            {"url": base_url, "http_status": response.status_code},
        )

    @staticmethod
    def _probe_mcp(_item: dict[str, Any]) -> tuple[str, Any]:
        return "unavailable", {"reason": "mcp_runtime_probe_not_registered"}

    @staticmethod
    def _probe_github(_item: dict[str, Any]) -> tuple[str, Any]:
        return "unavailable", {"reason": "connector_health_probe_not_registered"}

    @staticmethod
    def _probe_notion(_item: dict[str, Any]) -> tuple[str, Any]:
        return "unavailable", {"reason": "connector_health_probe_not_registered"}

    def _probe_model_provider(self, item: dict[str, Any]) -> tuple[str, Any]:
        from model_client import list_models

        settings_id = str((item.get("entrypoint") or {}).get("settings_id") or "")
        models = list_models(
            self.load_settings(),
            timeout=5,
            provider_id=settings_id,
        )
        return "ready", {
            "provider_id": settings_id,
            "models_count": len(models),
            "probe": "GET models (read-only, timeout=5s)",
        }

    @staticmethod
    def _probe_static(_item: dict[str, Any]) -> tuple[str, Any]:
        return "ready", {"registered": True}

    def _remove_settings_backed(self, row: dict[str, Any]) -> None:
        if self.save_settings is None:
            raise ExtensionConflict("settings-backed removal is unavailable")
        manifest = row["manifest"]
        entrypoint = manifest.get("entrypoint") or {}
        settings_id = str(entrypoint.get("settings_id") or "")
        cfg = dict(self.load_settings())
        if entrypoint.get("type") == "mcp_settings":
            cfg["mcp_servers"] = [
                item
                for item in cfg.get("mcp_servers") or []
                if str((item or {}).get("id") or "") != settings_id
            ]
        elif entrypoint.get("type") == "provider_settings":
            cfg["model_providers"] = [
                item
                for item in cfg.get("model_providers") or []
                if str((item or {}).get("id") or "").casefold()
                != settings_id.casefold()
            ]
            if settings_id == "openai_compatible":
                cfg["model_provider"] = "ollama"
        else:
            raise ExtensionConflict("unsupported settings-backed extension")
        self.save_settings(cfg)
        if entrypoint.get("type") == "provider_settings":
            from secret_store import delete_provider_secret

            delete_provider_secret(settings_id)
        if self.apply_configuration is not None:
            self.apply_configuration(cfg)


def create_extension_registry(
    *,
    load_settings: Callable[[], dict[str, Any]],
    save_settings: Optional[Callable[[dict[str, Any]], Any]] = None,
    apply_configuration: Optional[Callable[[dict[str, Any]], Any]] = None,
    require_project: Optional[Callable[[str], Any]] = None,
    local_dir: Optional[Path] = None,
    store: Optional[ExtensionStore] = None,
    health_probes: Optional[Mapping[str, Callable[[dict[str, Any]], Any]]] = None,
    state_change_handler: Optional[
        Callable[[str, bool, dict[str, Any]], Any]
    ] = None,
    state_rollback_handler: Optional[
        Callable[[str, bool, dict[str, Any]], Any]
    ] = None,
    project_state_change_handler: Optional[
        Callable[[str, str, str, dict[str, Any]], Any]
    ] = None,
    synchronize: bool = True,
) -> ExtensionRegistry:
    """Application factory seam used by ``backend.app`` and isolated tests."""

    registry = ExtensionRegistry(
        load_settings,
        save_settings=save_settings,
        apply_configuration=apply_configuration,
        require_project=require_project,
        local_dir=local_dir,
        store=store,
        health_probes=health_probes,
        state_change_handler=state_change_handler,
        state_rollback_handler=state_rollback_handler,
        project_state_change_handler=project_state_change_handler,
    )
    if synchronize:
        registry.initialize()
    return registry
