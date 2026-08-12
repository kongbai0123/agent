"""Run-attributed results and separately labelled workspace inspection routes."""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Mapping, Optional

from fastapi import APIRouter, HTTPException, Query

from project_vcs import (
    ProjectVcsError,
    inspect_project_diff,
    inspect_project_vcs,
    is_secret_relative_path,
    normalize_relative_path,
    redact_public_text,
    unavailable_vcs,
)


_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_PUBLIC_TEXT_LIMIT = 4_000
_ARTIFACT_PREVIEW_LIMIT = 64 * 1024


def _bounded_list(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(value[:limit])


def _clip_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _error_detail(
    error_payload: Callable[..., Dict[str, Any]],
    code: str,
    message: str,
    *,
    recoverable: bool = False,
) -> Any:
    return error_payload(code, message, recoverable=recoverable)


def _not_found(
    error_payload: Callable[..., Dict[str, Any]], code: str, message: str
) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail=_error_detail(error_payload, code, message, recoverable=False),
    )


def _safe_label(value: Any, *, limit: int = 240) -> Optional[str]:
    if value is None:
        return None
    text = "".join(character for character in str(value) if ord(character) >= 32)
    public, _ = redact_public_text(text, max_chars=limit)
    return public[:limit] or None


def _safe_filename(value: Any) -> Optional[str]:
    text = str(value or "").replace("\\", "/").rstrip("/")
    name = text.rsplit("/", 1)[-1]
    if not name or name in {".", ".."}:
        return None
    return _safe_label(name, limit=240)


def _event_parts(event: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(event, Mapping):
        return "", {}
    name = str(event.get("event") or event.get("type") or "").strip().casefold()
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        payload = event
    return name, dict(payload)


def _event_belongs_to_run(payload: Mapping[str, Any], run_id: str) -> bool:
    supplied = payload.get("run_id")
    return supplied is None or str(supplied) == run_id


def _validation_count(payload: Mapping[str, Any], name: str) -> Optional[int]:
    value = payload.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return max(0, min(int(value), 1_000_000_000))


def _run_evidence(run: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only explicitly typed evidence; never infer it from prose/tools."""

    run_id = str(run.get("run_id") or "")
    changes: list[dict[str, Any]] = []
    validations: list[dict[str, Any]] = []
    commits: list[dict[str, Any]] = []
    pushes: list[dict[str, Any]] = []
    omitted = 0

    for event in _bounded_list(run.get("events"), 500):
        name, payload = _event_parts(event)
        if not _event_belongs_to_run(payload, run_id):
            omitted += 1
            continue
        if name in {"file_change", "file_changed", "file_written"}:
            try:
                path = normalize_relative_path(
                    payload.get("relative_path") or payload.get("path")
                )
            except ProjectVcsError:
                omitted += 1
                continue
            if is_secret_relative_path(path):
                omitted += 1
                continue
            action = str(
                payload.get("change_type")
                or payload.get("action")
                or payload.get("status")
                or "modified"
            ).casefold()
            if action not in {"added", "created", "modified", "deleted", "renamed"}:
                action = "modified"
            change = {
                "path": path,
                "action": action,
                "source": "run_event",
            }
            for count_name in ("additions", "deletions"):
                count = _validation_count(payload, count_name)
                if count is not None:
                    change[count_name] = count
            changes.append(change)
        elif name in {"validation", "test_result"}:
            status = str(payload.get("status") or "").strip().casefold()
            passed = payload.get("passed")
            if isinstance(passed, bool):
                status = "passed" if passed else "failed"
            passed_count = _validation_count(payload, "passed")
            failed_count = _validation_count(payload, "failed")
            skipped_count = _validation_count(payload, "skipped")
            if status in {"error", "failure"} or (failed_count or 0) > 0:
                status = "failed"
            elif not status and passed_count is not None:
                status = "passed"
            if status not in {"passed", "failed", "skipped", "unknown"}:
                status = "unknown"
            validations.append(
                {
                    "name": _safe_label(payload.get("name") or payload.get("label") or "Validation"),
                    "status": status,
                    "passed": status == "passed" if status != "unknown" else None,
                    "summary": _safe_label(
                        payload.get("summary") or payload.get("details"),
                        limit=_PUBLIC_TEXT_LIMIT,
                    ),
                    "duration_ms": (
                        max(0, min(int(payload.get("duration_ms")), 86_400_000))
                        if isinstance(payload.get("duration_ms"), (int, float))
                        and not isinstance(payload.get("duration_ms"), bool)
                        else None
                    ),
                    "passed_count": passed_count,
                    "failed_count": failed_count,
                    "skipped_count": skipped_count,
                    "source": "run_event",
                }
            )
        elif name == "git_commit":
            commit = str(
                payload.get("commit_sha")
                or payload.get("short_sha")
                or payload.get("commit")
                or payload.get("sha")
                or ""
            ).strip()
            status = str(payload.get("status") or "").casefold()
            terminal = isinstance(payload.get("success"), bool) or status in {
                "success", "completed", "failed", "error",
            }
            success = payload.get("success") is True or status in {"success", "completed"}
            valid_commit = bool(_COMMIT_RE.fullmatch(commit))
            if terminal and (valid_commit or not success):
                commits.append(
                    {
                        "commit": commit.casefold() if valid_commit else None,
                        "branch": _safe_label(payload.get("branch"), limit=240),
                        "success": bool(success and valid_commit),
                    }
                )
            elif terminal:
                omitted += 1
        elif name == "git_push":
            status = str(payload.get("status") or "").casefold()
            terminal = isinstance(payload.get("success"), bool) or status in {
                "success", "completed", "pushed", "failed", "error",
            }
            success = payload.get("success") is True or status in {"success", "completed", "pushed"}
            if terminal:
                commit = str(
                    payload.get("commit_sha")
                    or payload.get("short_sha")
                    or payload.get("commit")
                    or payload.get("sha")
                    or ""
                ).strip()
                valid_commit = bool(_COMMIT_RE.fullmatch(commit))
                if valid_commit or not success:
                    pushes.append(
                        {
                            "commit": commit.casefold() if valid_commit else None,
                            "branch": _safe_label(payload.get("branch"), limit=240),
                            "remote": _safe_label(payload.get("remote"), limit=120),
                            "success": bool(success and valid_commit),
                        }
                    )
                else:
                    omitted += 1

    # Avoid duplicated lifecycle updates producing duplicate rows.
    unique_changes = list({(item["path"], item["action"]): item for item in changes}.values())
    return {
        "changes": unique_changes,
        "validations": validations,
        "vcs": {
            "commits": commits,
            "pushes": pushes,
            "committed_this_run": (
                any(item["success"] for item in commits) if commits else None
            ),
            "pushed_this_run": (
                any(item["success"] for item in pushes) if pushes else None
            ),
            "scope": "run_evidence",
            "status": "recorded" if commits or pushes else "unknown",
        },
        "omitted_evidence_count": omitted,
    }


def _artifact_ids(run: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for item in _bounded_list(run.get("artifacts"), 100):
        if isinstance(item, str) and item:
            result.add(item[:256])
        elif isinstance(item, Mapping):
            artifact_id = item.get("artifact_id") or item.get("id")
            if artifact_id:
                result.add(str(artifact_id)[:256])
    for event in _bounded_list(run.get("events"), 500):
        name, payload = _event_parts(event)
        if name not in {"artifact", "artifact_update"}:
            continue
        artifact_id = payload.get("artifact_id")
        if artifact_id:
            result.add(str(artifact_id)[:256])
    return result


def _artifact_for_run(database: Any, run: Mapping[str, Any], artifact_id: str) -> Optional[dict[str, Any]]:
    if artifact_id not in _artifact_ids(run):
        return None
    artifact = database.get_artifact(artifact_id)
    if not artifact:
        return None
    if str(artifact.get("session_id") or "") != str(run.get("session_id") or ""):
        return None
    # Exact turn binding is required.  An artifact without one is session data,
    # not proof that this particular run generated it.
    if not artifact.get("turn_id") or str(artifact.get("turn_id")) != str(run.get("turn_id") or ""):
        return None
    return artifact


def _artifact_summary(database: Any, run: Mapping[str, Any], artifact_id: str) -> Optional[dict[str, Any]]:
    artifact = _artifact_for_run(database, run, artifact_id)
    if not artifact:
        return None
    files = []
    omitted = 0
    all_files = _bounded_list(artifact.get("files"), 10_000)
    omitted += max(0, len(all_files) - 200)
    for file in all_files[:200]:
        try:
            path = normalize_relative_path(file.get("path"))
        except ProjectVcsError:
            omitted += 1
            continue
        if is_secret_relative_path(path):
            omitted += 1
            continue
        content = str(file.get("content") or "")
        files.append(
            {
                "path": path,
                "language": _safe_label(file.get("language"), limit=80),
                "size_chars": len(content),
                "preview_available": True,
            }
        )
    return {
        "artifact_id": artifact_id,
        "title": _safe_label(artifact.get("title"), limit=240) or "Artifact",
        "type": _safe_label(artifact.get("type"), limit=80) or "text",
        "created_at": artifact.get("created_at"),
        "updated_at": artifact.get("updated_at"),
        "files": files,
        "file_count": len(files),
        "omitted_file_count": omitted,
        "source": "run_evidence",
    }


def _run_artifacts(database: Any, run: Mapping[str, Any]) -> list[dict[str, Any]]:
    summaries = []
    for artifact_id in sorted(_artifact_ids(run)):
        summary = _artifact_summary(database, run, artifact_id)
        if summary:
            summaries.append(summary)
    return summaries


def _source_summaries(database: Any, run: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
    summaries: list[dict[str, Any]] = []
    omitted = 0
    run_project_id = run.get("project_id")
    source_items = _bounded_list(run.get("sources"), 10_000)
    omitted += max(0, len(source_items) - 200)
    for item in source_items[:200]:
        if not isinstance(item, Mapping):
            omitted += 1
            continue
        source_project = item.get("project_id")
        if run_project_id is not None and source_project is not None and str(source_project) != str(run_project_id):
            omitted += 1
            continue
        kind = str(item.get("kind") or "source").casefold()[:80]
        if kind in {"project_skill", "workbench_project_skill"}:
            kind = "workbench_project_skill"
        if (
            kind == "workbench_project_skill"
            and (
                run_project_id is None
                or str(source_project or "") != str(run_project_id)
            )
        ):
            omitted += 1
            continue
        summary: dict[str, Any] = {
            "kind": kind,
            "source_id": _safe_label(item.get("source_id") or item.get("id"), limit=240),
            "name": _safe_label(
                item.get("name") or item.get("title") or item.get("slug"),
                limit=240,
            ),
            "truncated": bool(item.get("truncated")),
            "source": "run_evidence",
        }
        if kind == "workbench_project_skill":
            summary.update(
                {
                    "project_id": str(source_project),
                    "slug": _safe_label(item.get("slug"), limit=160),
                    "version": _safe_label(item.get("version"), limit=80),
                    "trigger_mode": _safe_label(item.get("trigger_mode"), limit=40),
                    "references": [],
                }
            )
            for reference in _bounded_list(item.get("references"), 100):
                if not isinstance(reference, Mapping):
                    continue
                try:
                    path = normalize_relative_path(reference.get("path"))
                except ProjectVcsError:
                    continue
                if not is_secret_relative_path(path):
                    summary["references"].append(
                        {"path": path, "truncated": bool(reference.get("truncated"))}
                    )
        summaries.append(summary)

    manifest = run.get("input_manifest") if isinstance(run.get("input_manifest"), Mapping) else {}
    for attachment_id in _bounded_list(manifest.get("attachment_ids"), 100):
        attachment = database.get_attachment(str(attachment_id))
        if not attachment or str(attachment.get("session_id") or "") != str(run.get("session_id") or ""):
            omitted += 1
            continue
        if run_project_id is not None and str(attachment.get("project_id") or "") != str(run_project_id):
            omitted += 1
            continue
        summaries.append(
            {
                "kind": "attachment",
                "source_id": str(attachment_id),
                "name": _safe_filename(attachment.get("filename")),
                "mime_type": _safe_label(attachment.get("mime_type"), limit=120),
                "size_bytes": max(0, int(attachment.get("size_bytes") or 0)),
                "source": "run_input_manifest",
            }
        )
    context_id = manifest.get("temporary_context_id")
    if context_id:
        context = database.get_temporary_context(str(context_id))
        if context and str(context.get("session_id") or "") == str(run.get("session_id") or ""):
            summaries.append(
                {
                    "kind": "temporary_context",
                    "source_id": str(context_id),
                    "name": _safe_filename(context.get("filename")),
                    "source": "run_input_manifest",
                }
            )
        else:
            omitted += 1
    elif manifest.get("has_temporary_context") is True:
        summaries.append(
            {
                "kind": "temporary_context",
                "source_id": None,
                "name": "Temporary context",
                "source": "run_input_manifest",
            }
        )
    inline_image_count = manifest.get("inline_image_count")
    if (
        isinstance(inline_image_count, (int, float))
        and not isinstance(inline_image_count, bool)
        and inline_image_count > 0
    ):
        summaries.append(
            {
                "kind": "inline_images",
                "source_id": None,
                "name": "Inline images",
                "count": max(1, min(int(inline_image_count), 100)),
                "source": "run_input_manifest",
            }
        )
    return summaries, omitted


def _project_for_run(database: Any, run: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    # project_id is immutable run-time evidence.  Never substitute the session's
    # current project for a legacy row that lacks it.
    project_id = run.get("project_id")
    if not project_id:
        return None
    return database.get_project(str(project_id))


def _bound_run(
    database: Any,
    error_payload: Callable[..., Dict[str, Any]],
    run_id: str,
) -> dict[str, Any]:
    run = database.get_run(run_id)
    if not run:
        raise _not_found(error_payload, "RUN_NOT_FOUND", "Run was not found.")
    session_id = str(run.get("session_id") or "")
    session = database.get_session(session_id)
    if not session or session.get("project_id") != run.get("project_id"):
        raise HTTPException(
            status_code=409,
            detail=_error_detail(
                error_payload,
                "RUN_SCOPE_CHANGED",
                "The run no longer belongs to the session's active project scope.",
                recoverable=False,
            ),
        )
    return run


def _public_evidence_run(database: Any, run: Mapping[str, Any]) -> dict[str, Any]:
    """Re-project even legacy events before Results or previews inspect them."""

    projected = dict(run)
    projector = getattr(database, "public_run_events", None)
    if callable(projector):
        projected["events"] = projector(
            run.get("events"),
            run_id=str(run.get("run_id") or ""),
            session_id=str(run.get("session_id") or ""),
            project_id=run.get("project_id"),
        )
    return projected


def build_run_results_router(
    *,
    database: Any,
    error_payload: Callable[..., Dict[str, Any]],
) -> APIRouter:
    router = APIRouter(tags=["run-results"])

    @router.get("/api/projects/{project_id}/vcs/status")
    def get_project_vcs_status(project_id: str):
        project = database.get_project(project_id)
        if not project:
            raise _not_found(error_payload, "PROJECT_NOT_FOUND", "Project was not found.")
        try:
            vcs = inspect_project_vcs(project)
        except ProjectVcsError as exc:
            vcs = unavailable_vcs(exc)
        return {"success": True, "project_id": project_id, "vcs": vcs}

    @router.get("/api/projects/{project_id}/vcs/diff")
    def get_project_vcs_diff(
        project_id: str,
        path: str = Query(..., min_length=1, max_length=1024),
    ):
        project = database.get_project(project_id)
        if not project:
            raise _not_found(error_payload, "PROJECT_NOT_FOUND", "Project was not found.")
        try:
            diff = inspect_project_diff(project, path)
        except ProjectVcsError as exc:
            if exc.not_found:
                raise _not_found(
                    error_payload, "VCS_PATH_NOT_FOUND", "Changed file was not found."
                ) from exc
            raise HTTPException(
                status_code=409,
                detail=_error_detail(error_payload, exc.code, exc.message, recoverable=True),
            ) from exc
        return {"success": True, "project_id": project_id, **diff}

    @router.get("/api/runs/{run_id}/results")
    def get_run_results(run_id: str):
        run = _public_evidence_run(
            database,
            _bound_run(database, error_payload, run_id),
        )
        evidence = _run_evidence(run)
        project = _project_for_run(database, run)
        workspace: dict[str, Any]
        if project is None:
            workspace = {
                "available": False,
                "repository": False,
                "reason": "run_project_unavailable" if run.get("project_id") else "legacy_run_project_unknown",
                "changes": [],
                "change_count": 0,
                "pushed_this_run": None,
                "scope": "workspace",
            }
        else:
            try:
                workspace = inspect_project_vcs(project)
            except ProjectVcsError as exc:
                workspace = unavailable_vcs(exc)
        sources, omitted_sources = _source_summaries(database, run)
        return {
            "success": True,
            "run_id": run_id,
            "session_id": run.get("session_id"),
            "project_id": run.get("project_id"),
            "status": run.get("status"),
            "revision": int(run.get("execution_revision") or 0),
            "artifacts": _run_artifacts(database, run),
            "changes": evidence["changes"],
            "validations": evidence["validations"],
            "vcs": {
                "run_evidence": evidence["vcs"],
                "workspace": workspace,
            },
            "sources": sources,
            "omitted_evidence_count": evidence["omitted_evidence_count"] + omitted_sources,
        }

    @router.get("/api/runs/{run_id}/artifacts/{artifact_id}/preview")
    def get_run_artifact_preview(
        run_id: str,
        artifact_id: str,
        path: str = Query(..., min_length=1, max_length=1024),
    ):
        run = _public_evidence_run(
            database,
            _bound_run(database, error_payload, run_id),
        )
        artifact = _artifact_for_run(database, run, artifact_id)
        if not artifact:
            raise _not_found(error_payload, "ARTIFACT_NOT_FOUND", "Artifact was not found.")
        try:
            requested = normalize_relative_path(path)
        except ProjectVcsError as exc:
            raise _not_found(error_payload, "ARTIFACT_FILE_NOT_FOUND", "Artifact file was not found.") from exc
        if is_secret_relative_path(requested):
            raise _not_found(error_payload, "ARTIFACT_FILE_NOT_FOUND", "Artifact file was not found.")
        file = next(
            (
                item
                for item in _bounded_list(artifact.get("files"), 10_000)
                if isinstance(item, Mapping)
                and _artifact_file_path(item) == requested
            ),
            None,
        )
        if not file:
            raise _not_found(error_payload, "ARTIFACT_FILE_NOT_FOUND", "Artifact file was not found.")
        raw = str(file.get("content") or "")
        clipped = raw[:_ARTIFACT_PREVIEW_LIMIT]
        public, redactions = redact_public_text(clipped, max_chars=_ARTIFACT_PREVIEW_LIMIT)
        public, byte_clipped = _clip_utf8(public, _ARTIFACT_PREVIEW_LIMIT)
        return {
            "success": True,
            "run_id": run_id,
            "artifact_id": artifact_id,
            "path": requested,
            "language": _safe_label(file.get("language"), limit=80),
            "content": public,
            "render_mode": "text",
            "truncated": len(raw) > len(clipped) or byte_clipped,
            "redactions": redactions,
            "max_chars": _ARTIFACT_PREVIEW_LIMIT,
            "max_bytes": _ARTIFACT_PREVIEW_LIMIT,
        }

    return router


def _artifact_file_path(item: Mapping[str, Any]) -> Optional[str]:
    try:
        path = normalize_relative_path(item.get("path"))
    except ProjectVcsError:
        return None
    return None if is_secret_relative_path(path) else path
