import io
import json
import os
import shutil
import sqlite3
import zipfile
from contextlib import closing, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import database
from conversation_store import (
    _safe_existing_regular_file,
    _safe_regular_files,
    _safe_relative_path,
    export_session,
)
from paths import CONVERSATIONS_DIR, DB_DIR, DB_PATH, PROJECT_RUNTIME_DIR, REPO_ROOT, RUNTIME_ROOT, ensure_runtime_dirs


SESSION_TABLES = (
    "artifact_files",
    "runs",
    "artifacts",
    "attachments",
    "temporary_contexts",
    "files",
    "messages",
    "sessions",
)


def _json_text(value: Any, default: Any) -> str:
    return json.dumps(default if value is None else value, ensure_ascii=False)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _insert_row(connection: sqlite3.Connection, table: str, row: Dict[str, Any]) -> None:
    columns = {item[1] for item in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    selected = [key for key in row if key in columns]
    if not selected:
        return
    connection.execute(
        f"INSERT OR REPLACE INTO {table} ({','.join(selected)}) VALUES ({','.join('?' for _ in selected)})",
        tuple(row[key] for key in selected),
    )


def _session_folders() -> list[Path]:
    ensure_runtime_dirs()
    folders = [
        path
        for project in PROJECT_RUNTIME_DIR.iterdir()
        if project.is_dir()
        for path in (project / "conversations").glob("*")
        if path.is_dir() and (path / "manifest.json").is_file()
    ]
    # Read-only compatibility for a migration interrupted before startup completed.
    if CONVERSATIONS_DIR.is_dir():
        folders.extend(path for path in CONVERSATIONS_DIR.iterdir() if path.is_dir() and (path / "manifest.json").is_file())
    unique = {path.name: path for path in folders}
    return sorted(unique.values(), key=lambda path: path.name)


def export_session_zip(session_id: str) -> Optional[bytes]:
    session_dir = export_session(session_id)
    if not session_dir:
        return None
    allowed_attachments: set[str] = set()
    attachment_root = session_dir / "attachments"
    attachment_manifest = _safe_existing_regular_file(
        attachment_root / "manifest.json",
        attachment_root,
        direct_child=True,
    )
    if attachment_manifest is not None:
        try:
            manifest_items = _read_json(attachment_manifest)
        except (OSError, ValueError, json.JSONDecodeError):
            manifest_items = []
        if isinstance(manifest_items, list):
            for item in manifest_items:
                if not isinstance(item, dict):
                    continue
                try:
                    relative = _safe_relative_path(item.get("storage_path"))
                except ValueError:
                    continue
                if len(relative.parts) == 2 and relative.parts[0] == "attachments":
                    allowed_attachments.add(relative.as_posix())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        resolved_session = session_dir.resolve(strict=True)
        for path in sorted(_safe_regular_files(session_dir)):
            try:
                relative = _safe_relative_path(
                    path.relative_to(resolved_session).as_posix()
                )
            except (ValueError, OSError):
                continue
            relative_text = relative.as_posix()
            if relative.parts[0] == "attachments" and relative_text != (
                "attachments/manifest.json"
            ) and relative_text not in allowed_attachments:
                continue
            archive.write(path, Path(session_id) / relative)
    return buffer.getvalue()


def runtime_health() -> Dict[str, Any]:
    ensure_runtime_dirs()
    with database.DB_LOCK, closing(sqlite3.connect(DB_PATH)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        db_sessions = {row[0] for row in connection.execute("SELECT id FROM sessions").fetchall()}
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "sessions", "messages", "runs", "documents", "attachments", "artifacts",
                "automation_tasks", "automation_runs", "approval_requests",
            )
        }
        counts["pending_approvals"] = connection.execute(
            "SELECT COUNT(*) FROM approval_requests WHERE status = 'pending'"
        ).fetchone()[0]
        orphan_messages = connection.execute(
            "SELECT COUNT(*) FROM messages m LEFT JOIN sessions s ON s.id = m.session_id WHERE s.id IS NULL"
        ).fetchone()[0]
        orphan_runs = connection.execute(
            "SELECT COUNT(*) FROM runs r LEFT JOIN sessions s ON s.id = r.session_id WHERE s.id IS NULL"
        ).fetchone()[0]
    folder_sessions = {path.name for path in _session_folders()}
    missing_folders = sorted(db_sessions - folder_sessions)
    extra_folders = sorted(folder_sessions - db_sessions)
    disk = shutil.disk_usage(RUNTIME_ROOT)
    healthy = integrity == "ok" and not missing_folders and not extra_folders and not orphan_messages and not orphan_runs
    return {
        "status": "healthy" if healthy else "warning",
        "healthy": healthy,
        "database_integrity": integrity,
        "database_path": str(DB_PATH),
        "runtime_path": str(RUNTIME_ROOT),
        "counts": counts,
        "conversation_folders": len(folder_sessions),
        "missing_conversation_folders": missing_folders,
        "extra_conversation_folders": extra_folders,
        "orphan_messages": orphan_messages,
        "orphan_runs": orphan_runs,
        "writable": os.access(RUNTIME_ROOT, os.W_OK) and os.access(DB_DIR, os.W_OK),
        "disk": {"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free},
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _restore_messages(connection: sqlite3.Connection, session_dir: Path) -> int:
    path = session_dir / "messages.jsonl"
    if not path.exists():
        return 0
    restored = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        message = json.loads(line)
        for key in ("sources", "process_events", "artifacts"):
            message[f"{key}_json"] = _json_text(message.pop(key, None), [])
        _insert_row(connection, "messages", message)
        restored += 1
    return restored


def _restore_turns(connection: sqlite3.Connection, session_dir: Path) -> int:
    runs = 0
    turns_dir = session_dir / "turns"
    if not turns_dir.exists():
        return runs
    for turn_dir in sorted(path for path in turns_dir.iterdir() if path.is_dir()):
        run_path = turn_dir / "run.json"
        if not run_path.exists():
            continue
        run = _read_json(run_path)
        for key, default in (("tasks", []), ("events", []), ("sources", []), ("metrics", {}), ("artifacts", [])):
            run[f"{key}_json"] = _json_text(run.pop(key, None), default)
        _insert_row(connection, "runs", run)
        runs += 1
    return runs


def _restore_attachments(connection: sqlite3.Connection, session_dir: Path) -> int:
    attachment_root = session_dir / "attachments"
    manifest_path = _safe_existing_regular_file(
        attachment_root / "manifest.json",
        attachment_root,
        direct_child=True,
    )
    if manifest_path is None:
        return 0
    restored = 0
    items = _read_json(manifest_path)
    if not isinstance(items, list):
        return 0
    for attachment in items:
        if not isinstance(attachment, dict):
            continue
        try:
            archived_path = _safe_relative_path(attachment.get("storage_path"))
        except ValueError:
            continue
        if len(archived_path.parts) != 2 or archived_path.parts[0] != "attachments":
            continue
        local_file = _safe_existing_regular_file(
            attachment_root / archived_path.parts[1],
            attachment_root,
            direct_child=True,
        )
        if local_file is None:
            continue
        attachment["storage_path"] = str(local_file)
        _insert_row(connection, "attachments", attachment)
        restored += 1
    return restored


def _restore_artifacts(connection: sqlite3.Connection, session_dir: Path) -> int:
    artifacts_dir = session_dir / "artifacts"
    if not artifacts_dir.exists():
        return 0
    restored = 0
    for artifact_dir in sorted(artifacts_dir.iterdir()):
        manifest_path = _safe_existing_regular_file(
            artifact_dir / "manifest.json",
            artifact_dir,
            direct_child=True,
        )
        if manifest_path is None:
            continue
        artifact = _read_json(manifest_path)
        if not isinstance(artifact, dict):
            continue
        try:
            artifact_component = _safe_relative_path(artifact.get("id"))
        except ValueError:
            continue
        if (
            len(artifact_component.parts) != 1
            or artifact_component.name != artifact_dir.name
        ):
            continue
        _insert_row(connection, "artifacts", artifact)
        files_dir = artifact_dir / "files"
        for file_path in sorted(_safe_regular_files(files_dir)):
            try:
                relative_path = _safe_relative_path(
                    file_path.relative_to(files_dir.resolve(strict=True)).as_posix()
                ).as_posix()
                content = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError, ValueError):
                continue
            _insert_row(connection, "artifact_files", {"id": f"{artifact['id']}:{relative_path}", "artifact_id": artifact["id"], "path": relative_path, "content": content, "language": file_path.suffix.lstrip(".") or None})
        restored += 1
    return restored


def _build_reconstructed_database(destination: Path) -> Dict[str, Any]:
    with database.DB_LOCK, closing(sqlite3.connect(DB_PATH)) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)
    report: Dict[str, Any] = {"sessions": 0, "messages": 0, "runs": 0, "attachments": 0, "artifacts": 0, "errors": []}
    with closing(sqlite3.connect(destination)) as connection:
        for table in SESSION_TABLES:
            connection.execute(f"DELETE FROM {table}")
        for session_dir in _session_folders():
            try:
                session = _read_json(session_dir / "manifest.json")
                if session.get("session_id") != session_dir.name and session.get("id") != session_dir.name:
                    raise ValueError("manifest session id does not match the folder name")
                session["id"] = session_dir.name
                session.pop("session_id", None)
                _insert_row(connection, "sessions", session)
                report["sessions"] += 1
                report["messages"] += _restore_messages(connection, session_dir)
                report["runs"] += _restore_turns(connection, session_dir)
                report["attachments"] += _restore_attachments(connection, session_dir)
                report["artifacts"] += _restore_artifacts(connection, session_dir)
            except Exception as exc:
                report["errors"].append({"session_id": session_dir.name, "error": str(exc)})
        connection.execute(
            "UPDATE sessions SET message_count = (SELECT COUNT(*) FROM messages WHERE messages.session_id = sessions.id)"
        )
        connection.commit()
        report["integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
        report["orphan_messages"] = connection.execute("SELECT COUNT(*) FROM messages m LEFT JOIN sessions s ON s.id = m.session_id WHERE s.id IS NULL").fetchone()[0]
        report["orphan_runs"] = connection.execute("SELECT COUNT(*) FROM runs r LEFT JOIN sessions s ON s.id = r.session_id WHERE s.id IS NULL").fetchone()[0]
    report["valid"] = report["integrity"] == "ok" and not report["errors"] and not report["orphan_messages"] and not report["orphan_runs"]
    return report


def rebuild_index(apply: bool = False) -> Dict[str, Any]:
    ensure_runtime_dirs()
    temporary = DB_DIR / f"workbench-rebuild-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"
    lock = database.DB_LOCK if apply else nullcontext()
    with lock:
        report = _build_reconstructed_database(temporary)
        report.update({"applied": False, "preview": not apply, "source_folders": len(_session_folders())})
        if not apply or not report["valid"]:
            temporary.unlink(missing_ok=True)
            return report

        backup_dir = REPO_ROOT / "archive" / "backups" / f"runtime-rebuild-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / "workbench-before-rebuild.db"
        with closing(sqlite3.connect(DB_PATH)) as source, closing(sqlite3.connect(backup_path)) as backup:
            source.backup(backup)
        os.replace(temporary, DB_PATH)
        report.update({"applied": True, "preview": False, "backup_path": str(backup_path)})
        return report
