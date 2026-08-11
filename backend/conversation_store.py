import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from paths import DB_PATH, ensure_runtime_dirs
from project_storage import AUTO_PROJECT, conversation_dir, project_dir, session_project_id


def _json_value(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_json(path: Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    _atomic_text(
        path,
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
    )


def _row_dicts(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[Dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _safe_relative_path(value: str) -> Path:
    parts = [part for part in Path(value.replace("\\", "/")).parts if part not in ("", ".", "..", "/")]
    return Path(*parts) if parts else Path("artifact.txt")


def ensure_session_folder(session_id: str, title: str = "New chat", mode: str = "chat", model: Optional[str] = None, project_id=AUTO_PROJECT) -> Path:
    ensure_runtime_dirs()
    session_dir = conversation_dir(session_id, project_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        now = datetime.now(timezone.utc).isoformat()
        _write_json(
            manifest_path,
            {
                "schema_version": 1,
                "session_id": session_id,
                "title": title,
                "mode": mode,
                "model": model,
                "created_at": now,
                "updated_at": now,
            },
        )
    return session_dir


def export_session(session_id: str, db_path: Optional[Path] = None) -> Optional[Path]:
    ensure_runtime_dirs()
    db_path = db_path or DB_PATH
    if not Path(db_path).exists():
        return None
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        session_row = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not session_row:
            return None
        session = dict(session_row)
        session_dir = ensure_session_folder(
            session_id,
            session.get("title") or "New chat",
            session.get("mode") or "chat",
            session.get("model"),
            session.get("project_id"),
        )
        _write_json(session_dir / "manifest.json", {"schema_version": 1, "session_id": session_id, **session})

        messages = _row_dicts(connection, "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,))
        for message in messages:
            for field in ("sources_json", "process_events_json", "artifacts_json"):
                message[field.removesuffix("_json")] = _json_value(message.pop(field, None), [])
        _write_jsonl(session_dir / "messages.jsonl", messages)

        runs = _row_dicts(connection, "SELECT * FROM runs WHERE session_id = ? ORDER BY created_at, id", (session_id,))
        turns_dir = session_dir / "turns"
        staging_turns = session_dir / ".turns.next"
        previous_turns = session_dir / ".turns.previous"
        if staging_turns.exists():
            shutil.rmtree(staging_turns)
        staging_turns.mkdir(parents=True)

        messages_by_turn: Dict[str, list[Dict[str, Any]]] = {}
        unpaired_messages: list[Dict[str, Any]] = []
        for message in messages:
            message_turn_id = str(message.get("turn_id") or "").strip()
            if message_turn_id:
                messages_by_turn.setdefault(message_turn_id, []).append(message)
            else:
                legacy = dict(message)
                legacy["legacy_unlinked"] = True
                unpaired_messages.append(legacy)
        runs_by_turn = {str(run["turn_id"]): run for run in runs if run.get("turn_id")}
        ordered_turn_ids: list[str] = []
        for run in runs:
            value = str(run.get("turn_id") or "").strip()
            if value and value not in ordered_turn_ids:
                ordered_turn_ids.append(value)
        for message in messages:
            value = str(message.get("turn_id") or "").strip()
            if value and value not in ordered_turn_ids:
                ordered_turn_ids.append(value)

        for index, turn_id in enumerate(ordered_turn_ids):
            run = runs_by_turn.get(turn_id)
            turn_messages = messages_by_turn.get(turn_id, [])
            requests = [item for item in turn_messages if item.get("role") == "user"]
            responses = [item for item in turn_messages if item.get("role") == "assistant"]
            turn_dir = staging_turns / f"{index + 1:06d}_{turn_id}"
            turn_dir.mkdir(parents=True, exist_ok=True)
            if requests:
                _write_json(turn_dir / "request.json", requests[0])
                for extra in requests[1:]:
                    duplicate = dict(extra)
                    duplicate["legacy_unlinked"] = False
                    duplicate["unpaired_reason"] = "multiple_user_messages_for_turn"
                    unpaired_messages.append(duplicate)
            if responses:
                _atomic_text(turn_dir / "response.md", (responses[0].get("visible_content") or responses[0].get("content") or "") + "\n")
                for extra in responses[1:]:
                    duplicate = dict(extra)
                    duplicate["legacy_unlinked"] = False
                    duplicate["unpaired_reason"] = "multiple_assistant_messages_for_turn"
                    unpaired_messages.append(duplicate)
            _write_json(turn_dir / "pairing.json", {
                "turn_id": turn_id,
                "pairing_method": "exact_turn_id",
                "request_count": len(requests),
                "response_count": len(responses),
                "run_present": bool(run),
                "complete": len(requests) == 1 and len(responses) == 1 and bool(run),
            })
            if run:
                normalized_run = dict(run)
                for field in ("tasks_json", "events_json", "sources_json", "metrics_json", "artifacts_json"):
                    normalized_run[field.removesuffix("_json")] = _json_value(normalized_run.pop(field, None), [] if field != "metrics_json" else {})
                _write_json(turn_dir / "run.json", normalized_run)
                _write_jsonl(turn_dir / "events.jsonl", normalized_run.get("events", []))
                _write_json(turn_dir / "sources.json", normalized_run.get("sources", []))
                run_events = normalized_run.get("events", [])
                _write_json(turn_dir / "plan.json", {"tasks": normalized_run.get("tasks", [])})
                _write_jsonl(turn_dir / "commentary.jsonl", [item for item in run_events if item.get("type") in {"commentary", "phase", "task_update"}])
                _write_jsonl(turn_dir / "tool-events.jsonl", [item for item in run_events if item.get("type") in {"tool_start", "tool_end"}])
                validations = [item for item in run_events if item.get("type") == "validation"]
                _write_json(turn_dir / "validation.json", validations[-1] if validations else {"passed": False, "details": "尚無驗證紀錄。"})
                _write_jsonl(turn_dir / "repairs.jsonl", [item for item in run_events if item.get("type") == "repair"])
                finals = [item for item in run_events if item.get("type") == "final"]
                _atomic_text(turn_dir / "final.md", ((finals[-1].get("summary") if finals else "") or "") + "\n")
        if unpaired_messages:
            unpaired_dir = staging_turns / "unpaired"
            _write_json(unpaired_dir / "manifest.json", {
                "legacy_unlinked": True,
                "message_count": len(unpaired_messages),
                "reason": "Messages without an exact turn_id are preserved without guessing a request/response relationship.",
            })
            _write_jsonl(unpaired_dir / "messages.jsonl", unpaired_messages)

        if previous_turns.exists():
            shutil.rmtree(previous_turns)
        try:
            if turns_dir.exists():
                os.replace(turns_dir, previous_turns)
            os.replace(staging_turns, turns_dir)
            if previous_turns.exists():
                shutil.rmtree(previous_turns)
        except Exception:
            if not turns_dir.exists() and previous_turns.exists():
                os.replace(previous_turns, turns_dir)
            raise

        attachments = _row_dicts(connection, "SELECT * FROM attachments WHERE session_id = ? ORDER BY created_at", (session_id,))
        _write_json(session_dir / "attachments" / "manifest.json", attachments)
        for attachment in attachments:
            source = Path(attachment["storage_path"])
            destination = session_dir / "attachments" / source.name
            if source.is_file() and source.resolve() != destination.resolve():
                shutil.copy2(source, destination)

        artifacts = _row_dicts(connection, "SELECT * FROM artifacts WHERE session_id = ? ORDER BY created_at", (session_id,))
        for artifact in artifacts:
            artifact_dir = session_dir / "artifacts" / artifact["id"]
            _write_json(artifact_dir / "manifest.json", artifact)
            files = _row_dicts(connection, "SELECT * FROM artifact_files WHERE artifact_id = ? ORDER BY path", (artifact["id"],))
            for item in files:
                _atomic_text(artifact_dir / "files" / _safe_relative_path(item["path"]), item.get("content") or "")
        return session_dir
    finally:
        connection.close()


def archive_session(session_id: str) -> Optional[Path]:
    project_id = session_project_id(session_id)
    source = conversation_dir(session_id, project_id, create=False)
    if not source.exists():
        return None
    ensure_runtime_dirs()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = project_dir(project_id) / "trash" / f"{timestamp}_{session_id}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return destination
