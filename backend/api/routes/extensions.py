"""Extension catalog, trust, scope, health, audit, and removal routes."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from api.schemas.extensions import (
    ExtensionGlobalStateRequest,
    ExtensionInstallRequest,
    ExtensionTrustRequest,
    LocalExtensionInspectRequest,
    ProjectExtensionStateRequest,
    ProjectExtensionPermissionRequest,
)
from extension_registry import (
    ExtensionConflict,
    ExtensionDisabled,
    ExtensionError,
    ExtensionManifestRejected,
    ExtensionNotFound,
    ExtensionRegistry,
    ExtensionTrustRequired,
)


def _failure(
    exc: ExtensionError,
    error_payload: Callable[..., Dict[str, Any]],
) -> HTTPException:
    status = 500
    if isinstance(exc, ExtensionNotFound):
        status = 404
    elif isinstance(exc, ExtensionManifestRejected):
        status = 400
    elif isinstance(
        exc,
        (ExtensionConflict, ExtensionDisabled, ExtensionTrustRequired),
    ):
        status = 409
    return HTTPException(
        status_code=status,
        detail=error_payload(
            exc.code,
            str(exc),
            recoverable=status < 500,
        ),
    )


def build_extensions_router(
    *,
    registry: ExtensionRegistry,
    require_local: Callable[[Request], None],
    error_payload: Callable[..., Dict[str, Any]],
) -> APIRouter:
    """Build a router without importing application globals.

    The caller owns the local-session guard and the registry lifecycle, which
    keeps this module independently testable and avoids circular imports.
    """

    router = APIRouter(tags=["extensions"])

    @router.get("/api/extensions")
    def catalog(project_id: Optional[str] = None):
        try:
            return registry.catalog(project_id)
        except ExtensionError as exc:
            raise _failure(exc, error_payload) from exc

    @router.post("/api/extensions/local/inspect")
    def inspect_local(
        body: LocalExtensionInspectRequest,
        request: Request,
        project_id: Optional[str] = None,
    ):
        require_local(request)
        try:
            return {
                "success": True,
                "extension": registry.inspect_local(body.filename, project_id),
            }
        except ExtensionError as exc:
            raise _failure(exc, error_payload) from exc

    @router.get("/api/extensions/{extension_id}/audits")
    def audits(
        extension_id: str,
        limit: int = Query(default=100, ge=1, le=500),
    ):
        try:
            return {
                "success": True,
                "extension_id": extension_id,
                "audits": registry.audits(extension_id, limit),
            }
        except ExtensionError as exc:
            raise _failure(exc, error_payload) from exc

    @router.post("/api/extensions/{extension_id}/install")
    def install(
        extension_id: str,
        body: ExtensionInstallRequest,
        request: Request,
        project_id: Optional[str] = None,
    ):
        require_local(request)
        try:
            item = registry.install(
                extension_id,
                body.manifest_sha256,
                project_id=project_id,
            )
            return {"success": True, "extension": item}
        except ExtensionError as exc:
            raise _failure(exc, error_payload) from exc

    @router.post("/api/extensions/{extension_id}/trust")
    def trust(
        extension_id: str,
        body: ExtensionTrustRequest,
        request: Request,
        project_id: Optional[str] = None,
    ):
        require_local(request)
        try:
            item = registry.trust(
                extension_id,
                body.manifest_sha256,
                trusted_by="local_session",
                project_id=project_id,
            )
            return {"success": True, "extension": item}
        except ExtensionError as exc:
            raise _failure(exc, error_payload) from exc

    @router.patch("/api/extensions/{extension_id}/state")
    def global_state(
        extension_id: str,
        body: ExtensionGlobalStateRequest,
        request: Request,
        project_id: Optional[str] = None,
    ):
        require_local(request)
        try:
            item = registry.set_global(
                extension_id,
                body.global_enabled,
                expected_sha256=body.manifest_sha256,
                project_id=project_id,
            )
            return {"success": True, "extension": item}
        except ExtensionError as exc:
            raise _failure(exc, error_payload) from exc

    @router.put("/api/projects/{project_id}/extensions/{extension_id}")
    def project_state(
        project_id: str,
        extension_id: str,
        body: ProjectExtensionStateRequest,
        request: Request,
    ):
        require_local(request)
        try:
            item = registry.set_project_mode(
                extension_id,
                project_id,
                body.mode,
                expected_sha256=body.manifest_sha256,
            )
            return {"success": True, "extension": item}
        except ExtensionError as exc:
            raise _failure(exc, error_payload) from exc

    @router.put("/api/projects/{project_id}/extensions/{extension_id}/permission")
    def project_permission(
        project_id: str,
        extension_id: str,
        body: ProjectExtensionPermissionRequest,
        request: Request,
    ):
        require_local(request)
        try:
            item = registry.set_project_permission(
                extension_id,
                project_id,
                body.level,
                expected_revision=body.revision,
            )
            return {"success": True, "extension": item}
        except ExtensionError as exc:
            raise _failure(exc, error_payload) from exc

    @router.put("/api/extensions/{extension_id}/permission")
    def global_permission(
        extension_id: str,
        body: ProjectExtensionPermissionRequest,
        request: Request,
    ):
        require_local(request)
        try:
            item = registry.set_global_permission(
                extension_id,
                body.level,
                expected_revision=body.revision,
            )
            return {"success": True, "extension": item}
        except ExtensionError as exc:
            raise _failure(exc, error_payload) from exc

    @router.post("/api/extensions/{extension_id}/health")
    def health(
        extension_id: str,
        request: Request,
        project_id: Optional[str] = None,
    ):
        require_local(request)
        try:
            item = registry.refresh_health(extension_id, project_id=project_id)
            return {"success": True, "extension": item}
        except ExtensionError as exc:
            raise _failure(exc, error_payload) from exc

    @router.delete("/api/extensions/{extension_id}")
    def remove(extension_id: str, request: Request):
        require_local(request)
        try:
            return registry.remove(extension_id)
        except ExtensionError as exc:
            raise _failure(exc, error_payload) from exc

    return router
