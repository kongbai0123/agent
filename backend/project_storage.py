import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

import database
from paths import ATTACHMENTS_DIR, KNOWLEDGE_DOCUMENTS_DIR, PROJECT_RUNTIME_DIR, REPO_ROOT, SCREENSHOTS_DIR


INDEPENDENT_PROJECT_ID = "_independent"
AUTO_PROJECT = object()


def _safe_id(value: Optional[str], fallback: str) -> str:
    candidate = str(value or fallback)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
        raise ValueError(f"Invalid storage identifier: {candidate}")
    return candidate


def project_dir(project_id: Optional[str], *, create: bool = True) -> Path:
    path = PROJECT_RUNTIME_DIR / _safe_id(project_id, INDEPENDENT_PROJECT_ID)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def session_project_id(session_id: Optional[str]) -> Optional[str]:
    if not session_id:
        return None
    session = database.get_session(session_id)
    return session.get("project_id") if session else None


def conversation_dir(session_id: str, project_id=AUTO_PROJECT, *, create: bool = True) -> Path:
    resolved_project = session_project_id(session_id) if project_id is AUTO_PROJECT else project_id
    path = project_dir(resolved_project, create=create) / "conversations" / _safe_id(session_id, "unassigned")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def imports_dir(session_id: Optional[str], project_id=AUTO_PROJECT) -> Path:
    if session_id:
        path = conversation_dir(session_id, project_id) / "imports"
    else:
        path = project_dir(None if project_id is AUTO_PROJECT else project_id) / "imports" / "unassigned"
    path.mkdir(parents=True, exist_ok=True)
    return path


def attachments_dir(session_id: Optional[str], project_id=AUTO_PROJECT) -> Path:
    if session_id:
        path = conversation_dir(session_id, project_id) / "attachments"
    else:
        path = project_dir(None if project_id is AUTO_PROJECT else project_id) / "attachments" / "unassigned"
    path.mkdir(parents=True, exist_ok=True)
    return path


def move_session(session_id: str, old_project_id: Optional[str], new_project_id: Optional[str]) -> Optional[Path]:
    safe_session_id = _safe_id(session_id, "unassigned")
    source = project_dir(old_project_id, create=False) / "conversations" / safe_session_id
    destination = project_dir(new_project_id, create=False) / "conversations" / safe_session_id
    if source == destination or not source.exists():
        return destination if destination.exists() else None
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.copytree(source, destination, dirs_exist_ok=True)
        shutil.rmtree(source)
    else:
        shutil.move(str(source), str(destination))
    database.rebase_session_storage_paths(session_id, str(source), str(destination), new_project_id)
    return destination


def migrate_legacy_storage() -> dict:
    """Move legacy source files into project-owned folders. Safe to run repeatedly."""
    report = {"conversations": 0, "attachments": 0, "documents": 0, "errors": []}
    legacy_conversations = PROJECT_RUNTIME_DIR.parent / "conversations"
    if legacy_conversations.is_dir():
        for source in legacy_conversations.iterdir():
            if not source.is_dir():
                continue
            try:
                destination = conversation_dir(source.name, create=False)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    shutil.copytree(source, destination, dirs_exist_ok=True)
                    shutil.rmtree(source)
                else:
                    shutil.move(str(source), str(destination))
                report["conversations"] += 1
            except Exception as exc:
                report["errors"].append({"path": str(source), "error": str(exc)})

    for attachment in database.get_all_attachments():
        source = Path(attachment["storage_path"])
        if not source.is_file():
            continue
        try:
            destination = attachments_dir(attachment.get("session_id")) / source.name
            if source.resolve() != destination.resolve():
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.move(str(source), str(destination))
                else:
                    source.unlink(missing_ok=True)
                database.update_attachment_storage(attachment["id"], str(destination), session_project_id(attachment.get("session_id")))
                report["attachments"] += 1
        except Exception as exc:
            report["errors"].append({"path": str(source), "error": str(exc)})

    for document in database.get_documents():
        source = Path(document["storage_path"])
        if not source.is_file():
            continue
        try:
            project_id = document.get("project_id") or session_project_id(document.get("session_id"))
            destination = imports_dir(document.get("session_id"), project_id) / source.name
            if source.resolve() != destination.resolve():
                if not destination.exists():
                    shutil.move(str(source), str(destination))
                else:
                    source.unlink(missing_ok=True)
                database.update_document_storage(document["id"], str(destination), project_id, document.get("session_id"))
                report["documents"] += 1
        except Exception as exc:
                report["errors"].append({"path": str(source), "error": str(exc)})

    orphan_sources = (
        (ATTACHMENTS_DIR, project_dir(None) / "orphaned" / "attachments"),
        (KNOWLEDGE_DOCUMENTS_DIR, project_dir(None) / "orphaned" / "imports"),
        (SCREENSHOTS_DIR, project_dir(None) / "orphaned" / "screenshots"),
        (REPO_ROOT / "frontend" / "screenshots", project_dir(None) / "orphaned" / "screenshots" / "legacy-frontend"),
    )
    for source_root, destination_root in orphan_sources:
        if not source_root.is_dir():
            continue
        for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
            try:
                relative = source.relative_to(source_root)
                destination = destination_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    destination = destination.with_name(f"{destination.stem}-legacy{destination.suffix}")
                shutil.move(str(source), str(destination))
            except Exception as exc:
                report["errors"].append({"path": str(source), "error": str(exc)})

    for legacy_root in (legacy_conversations, ATTACHMENTS_DIR, KNOWLEDGE_DOCUMENTS_DIR, SCREENSHOTS_DIR, REPO_ROOT / "frontend" / "screenshots"):
        try:
            if legacy_root.is_dir() and not any(legacy_root.iterdir()):
                legacy_root.rmdir()
        except OSError:
            pass

    report_path = PROJECT_RUNTIME_DIR / "migration-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
