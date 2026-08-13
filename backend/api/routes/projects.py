"""Project workspace routes."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from api.schemas.projects import (
    BrowseDirectoriesRequest,
    CreateProjectRequest,
    PatchProjectRequest,
    RelinkProjectRequest,
    ReorderProjectsRequest,
    ValidateProjectPathRequest,
)
from subprocess_env import agent_subprocess_env


def build_projects_router(
    *,
    database: Any,
    require_local: Callable[[Request], None],
    error_payload: Callable[..., Dict[str, Any]],
    create_id: Callable[[str], str],
    default_project_root: Path,
    validate_project_path: Callable[..., Path],
    managed_project_path: Callable[[str, str], Path],
    path_status: Callable[[Any], str],
    write_project_manifest: Callable[[Dict[str, Any]], Any],
    context_for_project: Callable[[Dict[str, Any]], Any],
    context_payload: Callable[[Any], Dict[str, Any]],
    project_storage_dir: Callable[..., Path],
    normalize_path: Callable[[Any], Path],
    project_change_guard: Optional[
        Callable[[str, str, Optional[Dict[str, Any]]], None]
    ] = None,
) -> APIRouter:
    router = APIRouter(tags=["projects"])
    @router.get("/api/projects")
    def api_get_projects(search: Optional[str] = None):
        projects = database.get_projects(search)
        for project in projects:
            current_status = path_status(project["root_path"])
            if current_status != project.get("path_status"):
                database.update_project(project["id"], path_status=current_status)
            project["path_status"] = current_status
        return {"projects": projects}
    @router.post("/api/projects/validate-path")
    def api_validate_project_path(req: ValidateProjectPathRequest):
        try:
            root = validate_project_path(
                req.root_path,
                require_existing=req.require_existing,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=error_payload("INVALID_PROJECT_PATH", str(exc)),
            ) from exc
        duplicate = database.get_project_by_root_path(str(root))
        return {
            "success": True,
            "root_path": str(root),
            "path_status": path_status(root),
            "duplicate_project_id": duplicate["id"] if duplicate else None,
        }
    @router.post("/api/projects/browse-directories")
    def api_browse_directories(req: BrowseDirectoriesRequest, request: Request):
        require_local(request)
        if req.path == "__roots__":
            if os.name == "nt":
                roots = [
                    Path(f"{letter}:\\")
                    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                    if Path(f"{letter}:\\").exists()
                ]
            else:
                roots = [Path("/")]
            return {
                "success": True,
                "current_path": "__roots__",
                "display_path": "這台電腦",
                "parent_path": None,
                "directories": [
                    {"name": str(root), "path": str(root)} for root in roots
                ],
            }

        candidate = (
            Path(req.path).expanduser()
            if req.path
            else Path(default_project_root).expanduser()
        )
        if not req.path and not candidate.is_dir():
            candidate = Path.home()
        try:
            current = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(
                status_code=400,
                detail=error_payload(
                    "DIRECTORY_NOT_FOUND",
                    "資料夾不存在或無法存取。",
                ),
            ) from exc
        if not current.is_dir():
            raise HTTPException(
                status_code=400,
                detail=error_payload("NOT_A_DIRECTORY", "指定路徑不是資料夾。"),
            )
        directories = []
        try:
            for child in current.iterdir():
                try:
                    if child.is_dir():
                        directories.append(
                            {
                                "name": child.name,
                                "path": str(child.resolve(strict=False)),
                            }
                        )
                except OSError:
                    continue
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail=error_payload(
                    "DIRECTORY_ACCESS_DENIED",
                    "沒有權限瀏覽此資料夾。",
                ),
            ) from exc
        directories.sort(key=lambda item: item["name"].casefold())
        return {
            "success": True,
            "current_path": str(current),
            "display_path": str(current),
            "parent_path": (
                "__roots__" if current.parent == current else str(current.parent)
            ),
            "directories": directories,
        }

    @router.post("/api/projects/reorder")
    def api_reorder_projects(req: ReorderProjectsRequest):
        if not database.reorder_projects(req.project_ids):
            raise HTTPException(
                status_code=400,
                detail=error_payload(
                    "INVALID_PROJECT_ORDER",
                    "Project order contains duplicate or unknown IDs.",
                ),
            )
        return {"success": True, "project_ids": req.project_ids}

    @router.post("/api/projects")
    def api_create_project(req: CreateProjectRequest):
        name = req.name.strip()
        project_id = create_id("project")
        try:
            if req.root_kind == "linked":
                if not req.root_path:
                    raise ValueError("連結既有專案時必須指定資料夾路徑。")
                root = validate_project_path(req.root_path, require_existing=True)
            else:
                root = (
                    validate_project_path(req.root_path)
                    if req.root_path
                    else managed_project_path(name, project_id)
                )
                root.mkdir(parents=True, exist_ok=False)
        except (ValueError, FileExistsError) as exc:
            raise HTTPException(
                status_code=400,
                detail=error_payload("INVALID_PROJECT_PATH", str(exc)),
            ) from exc
        duplicate = database.get_project_by_root_path(str(root))
        if duplicate:
            raise HTTPException(
                status_code=409,
                detail=error_payload(
                    "DUPLICATE_PROJECT_PATH",
                    "此資料夾已連結到其他專案。",
                ),
            )
        project = database.create_project(
            project_id,
            name,
            str(root),
            req.root_kind,
            req.permission_mode,
            path_status(root),
        )
        write_project_manifest(project)
        return {"success": True, "project": project}

    @router.patch("/api/projects/{project_id}")
    def api_patch_project(project_id: str, req: PatchProjectRequest):
        changes = req.model_dump(exclude_unset=True)
        if project_change_guard is not None and changes.get("archived") is True:
            project_change_guard(project_id, "archive", changes)
        if "name" in changes:
            changes["name"] = changes["name"].strip()
        if "root_path" in changes:
            raise HTTPException(
                status_code=400,
                detail=error_payload(
                    "USE_RELINK_ENDPOINT",
                    "請使用重新連結功能變更專案路徑。",
                ),
            )
        if not database.update_project(project_id, **changes):
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "PROJECT_NOT_FOUND",
                    "Project was not found.",
                    recoverable=False,
                ),
            )
        project = database.get_project(project_id)
        if project:
            write_project_manifest(project)
        return {"success": True, "project": project}

    @router.get("/api/projects/{project_id}/workspace-status")
    def api_project_workspace_status(project_id: str):
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "PROJECT_NOT_FOUND",
                    "Project was not found.",
                    recoverable=False,
                ),
            )
        context = context_for_project(project)
        database.update_project(project_id, path_status=context.path_status)
        return {"success": True, "workspace": context_payload(context)}

    @router.post("/api/projects/{project_id}/relink")
    def api_relink_project(project_id: str, req: RelinkProjectRequest):
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "PROJECT_NOT_FOUND",
                    "Project was not found.",
                    recoverable=False,
                ),
            )
        if project_change_guard is not None:
            project_change_guard(
                project_id,
                "relink",
                {"root_path": req.root_path},
            )
        try:
            root = validate_project_path(req.root_path, require_existing=True)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=error_payload("INVALID_PROJECT_PATH", str(exc)),
            ) from exc
        duplicate = database.get_project_by_root_path(str(root))
        if duplicate and duplicate["id"] != project_id:
            raise HTTPException(
                status_code=409,
                detail=error_payload(
                    "DUPLICATE_PROJECT_PATH",
                    "此資料夾已連結到其他專案。",
                ),
            )
        database.update_project(
            project_id,
            root_path=str(root),
            root_kind="linked",
            path_status=path_status(root),
        )
        updated = database.get_project(project_id)
        write_project_manifest(updated)
        return {"success": True, "project": updated}

    @router.delete("/api/projects/{project_id}")
    def api_delete_project(project_id: str):
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "PROJECT_NOT_FOUND",
                    "Project was not found.",
                    recoverable=False,
                ),
            )
        if project_change_guard is not None:
            project_change_guard(project_id, "delete", None)
        storage = project_storage_dir(project_id, create=False)
        if not database.delete_project(project_id):
            raise HTTPException(
                status_code=500,
                detail=error_payload(
                    "PROJECT_DELETE_FAILED",
                    "Project metadata could not be deleted.",
                ),
            )
        if storage.exists():
            shutil.rmtree(storage)
        return {
            "success": True,
            "project_id": project_id,
            "deleted_storage": str(storage),
        }

    @router.post("/api/projects/{project_id}/open-folder")
    def api_open_project_folder(project_id: str):
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "PROJECT_NOT_FOUND",
                    "Project was not found.",
                    recoverable=False,
                ),
            )
        folder = normalize_path(project["root_path"])
        if not folder.exists():
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "PROJECT_FOLDER_NOT_FOUND",
                    "Project folder was not found.",
                ),
            )
        if os.name == "nt":
            os.startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)], env=agent_subprocess_env())
        else:
            subprocess.Popen(
                ["xdg-open", str(folder)],
                env=agent_subprocess_env(),
            )
        return {"success": True, "root_path": str(folder)}

    return router
