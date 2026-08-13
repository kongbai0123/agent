"""Project-scoped Skill management and session-safe loading routes."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, status

from api.schemas.project_skills import (
    CreateProjectSkillRequest,
    DeleteProjectSkillRequest,
    ProjectSkillStateRequest,
    SessionProjectSkillStateRequest,
    UpdateProjectSkillRequest,
)
from project_skill_runtime import ProjectSkillRuntime
from project_skills import ProjectSkillError, ProjectSkillStore


def _failure(
    exc: ProjectSkillError,
    error_payload: Callable[..., Dict[str, Any]],
) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail=error_payload(
            exc.code,
            str(exc),
            recoverable=exc.status_code < 500,
        ),
    )


def build_project_skills_router(
    *,
    store: ProjectSkillStore,
    runtime: ProjectSkillRuntime,
    require_local: Callable[[Request], None],
    error_payload: Callable[..., Dict[str, Any]],
    session_access_guard: Optional[Callable[[str, str], None]] = None,
) -> APIRouter:
    router = APIRouter(tags=["project-skills"])

    @router.get("/api/projects/{project_id}/skills")
    def list_project_skills(project_id: str):
        try:
            return {
                "success": True,
                "project_id": project_id,
                "skills": store.list(project_id),
            }
        except ProjectSkillError as exc:
            raise _failure(exc, error_payload) from exc

    @router.post(
        "/api/projects/{project_id}/skills",
        status_code=status.HTTP_201_CREATED,
    )
    def create_project_skill(
        project_id: str,
        body: CreateProjectSkillRequest,
        request: Request,
    ):
        require_local(request)
        try:
            skill = store.create(
                project_id,
                slug=body.slug,
                name=body.name,
                description=body.description,
                version=body.version,
                instructions=body.instructions,
                enabled=body.enabled,
                references=body.references,
            )
            return {"success": True, "skill": skill}
        except ProjectSkillError as exc:
            raise _failure(exc, error_payload) from exc

    @router.get("/api/projects/{project_id}/skills/{skill_slug}")
    def get_project_skill(project_id: str, skill_slug: str):
        try:
            return {"success": True, "skill": store.get(project_id, skill_slug)}
        except ProjectSkillError as exc:
            raise _failure(exc, error_payload) from exc

    @router.put("/api/projects/{project_id}/skills/{skill_slug}")
    @router.patch("/api/projects/{project_id}/skills/{skill_slug}")
    def update_project_skill(
        project_id: str,
        skill_slug: str,
        body: UpdateProjectSkillRequest,
        request: Request,
    ):
        require_local(request)
        values = body.model_dump(exclude_unset=True)
        expected_sha256 = values.pop("expected_sha256")
        try:
            skill = store.update(
                project_id,
                skill_slug,
                expected_sha256=expected_sha256,
                **values,
            )
            return {"success": True, "skill": skill}
        except ProjectSkillError as exc:
            raise _failure(exc, error_payload) from exc

    @router.patch("/api/projects/{project_id}/skills/{skill_slug}/state")
    def set_project_skill_state(
        project_id: str,
        skill_slug: str,
        body: ProjectSkillStateRequest,
        request: Request,
    ):
        require_local(request)
        try:
            skill = store.set_enabled(
                project_id,
                skill_slug,
                enabled=body.enabled,
                expected_sha256=body.expected_sha256,
            )
            return {"success": True, "skill": skill}
        except ProjectSkillError as exc:
            raise _failure(exc, error_payload) from exc

    @router.get("/api/projects/{project_id}/skills/{skill_slug}/versions")
    def list_project_skill_versions(project_id: str, skill_slug: str):
        try:
            return {
                "success": True,
                "project_id": project_id,
                "skill_slug": skill_slug,
                "versions": store.versions(project_id, skill_slug),
            }
        except ProjectSkillError as exc:
            raise _failure(exc, error_payload) from exc

    @router.get(
        "/api/projects/{project_id}/skills/{skill_slug}/versions/{version_sha256}"
    )
    def get_project_skill_version(
        project_id: str,
        skill_slug: str,
        version_sha256: str,
    ):
        try:
            return {
                "success": True,
                "project_id": project_id,
                "skill_slug": skill_slug,
                "version": store.get_version(
                    project_id,
                    skill_slug,
                    version_sha256,
                ),
            }
        except ProjectSkillError as exc:
            raise _failure(exc, error_payload) from exc

    @router.delete("/api/projects/{project_id}/skills/{skill_slug}")
    def delete_project_skill(
        project_id: str,
        skill_slug: str,
        body: DeleteProjectSkillRequest,
        request: Request,
    ):
        require_local(request)
        try:
            deleted = store.delete(
                project_id,
                skill_slug,
                expected_sha256=body.expected_sha256,
            )
            runtime.purge_skill_state(project_id, skill_slug)
            return {"success": True, **deleted}
        except ProjectSkillError as exc:
            raise _failure(exc, error_payload) from exc

    @router.get("/api/sessions/{session_id}/skills")
    def load_session_skills(session_id: str):
        if session_access_guard is not None:
            session_access_guard(session_id, "skills_read")
        try:
            return {"success": True, **runtime.catalog_for_session(session_id)}
        except ProjectSkillError as exc:
            raise _failure(exc, error_payload) from exc

    @router.put("/api/sessions/{session_id}/skills/{skill_slug}")
    def set_session_skill_state(
        session_id: str,
        skill_slug: str,
        body: SessionProjectSkillStateRequest,
        request: Request,
    ):
        require_local(request)
        if session_access_guard is not None:
            session_access_guard(session_id, "skills_write")
        try:
            return {
                "success": True,
                **runtime.set_session_state(
                    session_id,
                    skill_slug,
                    mode=body.mode,
                    scope=body.scope,
                    expected_sha256=body.expected_sha256,
                ),
            }
        except ProjectSkillError as exc:
            raise _failure(exc, error_payload) from exc

    @router.get("/api/runs/{run_id}/skills")
    def load_run_skill_provenance(run_id: str):
        try:
            run = runtime.database.get_run(run_id)
            if not run:
                raise HTTPException(
                    status_code=404,
                    detail=error_payload(
                        "RUN_NOT_FOUND", "Run not found.", recoverable=False
                    ),
                )
            session_id = str(run.get("session_id") or "")
            if session_access_guard is not None:
                session_access_guard(session_id, "run_skills_read")
            project_id = run.get("project_id")
            session = runtime.database.get_session(session_id)
            if not session or session.get("project_id") != project_id:
                raise HTTPException(
                    status_code=409,
                    detail=error_payload(
                        "RUN_SKILL_SCOPE_MISMATCH",
                        "Run Skill provenance no longer matches its session scope.",
                        recoverable=False,
                    ),
                )
            skills = runtime.run_provenance(run_id)
            if any(
                item.get("run_id") != run_id
                or item.get("session_id") != session_id
                or item.get("project_id") != project_id
                for item in skills
            ):
                raise HTTPException(
                    status_code=409,
                    detail=error_payload(
                        "RUN_SKILL_SCOPE_MISMATCH",
                        "Run Skill provenance contains a mismatched scope.",
                        recoverable=False,
                    ),
                )
            return {
                "success": True,
                "run_id": run_id,
                "session_id": session_id,
                "project_id": project_id,
                "skills": skills,
            }
        except HTTPException:
            raise
        except ProjectSkillError as exc:
            raise _failure(exc, error_payload) from exc

    return router
