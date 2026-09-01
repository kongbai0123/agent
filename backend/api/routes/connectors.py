"""HTTP boundaries for local OAuth connectors."""

from __future__ import annotations

from html import escape
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from api.schemas.connectors import (
    ConnectorAuthProfileUpdate,
    ConnectorOAuthStart,
    ConnectorResourceBindingsReplace,
    ProjectConnectionUpdate,
)
from connector_secrets import ConnectorSecretError
from connector_service import ConnectorService, ConnectorServiceError
from connector_store import ConnectorStoreError, normalize_connector_id


def _failure(
    exc: BaseException, error_payload: Callable[..., Dict[str, Any]]
) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, (ConnectorServiceError, ConnectorStoreError)):
        return HTTPException(
            status_code=exc.status_code,
            detail=error_payload(
                exc.code,
                exc.message,
                recoverable=getattr(exc, "recoverable", exc.status_code >= 500),
            ),
        )
    if isinstance(exc, ConnectorSecretError):
        return HTTPException(
            status_code=500,
            detail=error_payload(
                "CONNECTOR_SECRET_STORE_ERROR",
                "The local connector secret store is unavailable.",
                recoverable=True,
            ),
        )
    return HTTPException(
        status_code=500,
        detail=error_payload(
            "CONNECTOR_INTERNAL_ERROR",
            "The connector request failed.",
            recoverable=False,
        ),
    )


def build_connectors_router(
    *,
    service: ConnectorService,
    require_local: Callable[[Request], None],
    error_payload: Callable[..., Dict[str, Any]],
    require_extension: Optional[Callable[[str, Optional[str]], Any]] = None,
) -> APIRouter:
    router = APIRouter(tags=["connectors"])

    def local(request: Request) -> None:
        require_local(request)

    def require_connector(
        connector_id: str,
        project_id: Optional[str] = None,
    ) -> None:
        if require_extension is None:
            return
        connector = normalize_connector_id(connector_id)
        try:
            outcome = require_extension(f"connector.{connector}", project_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail=error_payload(
                    str(getattr(exc, "code", "EXTENSION_DISABLED"))[:128],
                    "The connector extension is disabled for this Project.",
                    recoverable=True,
                ),
            ) from exc
        if outcome is False:
            raise HTTPException(
                status_code=409,
                detail=error_payload(
                    "EXTENSION_DISABLED",
                    "The connector extension is disabled for this Project.",
                    recoverable=True,
                ),
            )

    @router.get("/api/connectors")
    def get_connector_catalog():
        return {"success": True, "connectors": service.catalog()}

    @router.get("/api/connectors/connections")
    def get_connections(
        connector_id: Optional[str] = Query(default=None, max_length=32),
        project_id: Optional[str] = Query(default=None, max_length=512),
    ):
        try:
            return {
                "success": True,
                "connections": service.list_connections(
                    connector_id=connector_id, project_id=project_id
                ),
            }
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.put("/api/connectors/{connector_id}/auth-profile")
    def put_auth_profile(
        connector_id: str,
        payload: ConnectorAuthProfileUpdate,
        request: Request,
    ):
        local(request)
        try:
            require_connector(connector_id)
            profile = service.configure_auth_profile(
                connector_id,
                client_id=payload.client_id,
                client_secret=(
                    payload.client_secret.get_secret_value()
                    if payload.client_secret is not None
                    else None
                ),
                callback_uri=payload.callback_uri,
            )
            return {"success": True, "profile": profile}
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.get("/api/connectors/{connector_id}/auth-profile/status")
    def get_auth_profile_status(connector_id: str):
        try:
            return {
                "success": True,
                "profile": service.auth_profile_status(connector_id),
            }
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.delete("/api/connectors/{connector_id}/auth-profile")
    def delete_auth_profile(connector_id: str, request: Request):
        local(request)
        try:
            return {
                "success": True,
                "connector_id": connector_id,
                "deleted": service.delete_auth_profile(connector_id),
            }
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.post("/api/connectors/{connector_id}/oauth/start")
    def start_oauth(
        connector_id: str,
        payload: ConnectorOAuthStart,
        request: Request,
    ):
        local(request)
        try:
            require_connector(connector_id)
            return {
                "success": True,
                **service.start_oauth(
                    connector_id, connection_id=payload.connection_id
                ),
            }
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.get("/api/connectors/connections/{connection_id}")
    def get_connection(connection_id: str):
        try:
            return {"success": True, "connection": service.get_connection(connection_id)}
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.post("/api/connectors/connections/{connection_id}/health")
    def post_connection_health(connection_id: str, request: Request):
        local(request)
        try:
            connection = service.get_connection(connection_id)
            require_connector(str(connection["connector_id"]))
            return {"success": True, "connection": service.health(connection_id)}
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.delete("/api/connectors/connections/{connection_id}")
    def delete_connection(
        connection_id: str,
        request: Request,
        force_local: bool = Query(default=False),
    ):
        local(request)
        try:
            return {
                "success": True,
                **service.disconnect(connection_id, force_local=force_local),
            }
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.put("/api/projects/{project_id}/connections/{connection_id}")
    def put_project_connection(
        project_id: str,
        connection_id: str,
        payload: ProjectConnectionUpdate,
        request: Request,
    ):
        local(request)
        try:
            if payload.enabled:
                connection = service.get_connection(connection_id)
                require_connector(str(connection["connector_id"]), project_id)
            return {
                "success": True,
                "binding": service.put_project_binding(
                    project_id=project_id,
                    connection_id=connection_id,
                    enabled=payload.enabled,
                    mode=payload.mode,
                ),
            }
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.get("/api/connectors/connections/{connection_id}/resources")
    def get_discoverable_resources(
        connection_id: str,
        resource_type: Optional[str] = Query(default=None, alias="type", max_length=64),
        q: str = Query(default="", max_length=512),
    ):
        try:
            connection = service.get_connection(connection_id)
            require_connector(str(connection["connector_id"]))
            return {
                "success": True,
                "connection_id": connection_id,
                "resources": service.list_resources(
                    connection_id, resource_type=resource_type, query=q
                ),
            }
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.get("/api/projects/{project_id}/connections/{connection_id}/resources")
    def get_project_resources(project_id: str, connection_id: str):
        try:
            return {
                "success": True,
                **service.get_bound_resources(
                    project_id=project_id, connection_id=connection_id
                ),
            }
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.put("/api/projects/{project_id}/connections/{connection_id}/resources")
    def put_project_resources(
        project_id: str,
        connection_id: str,
        payload: ConnectorResourceBindingsReplace,
        request: Request,
    ):
        local(request)
        try:
            connection = service.get_connection(connection_id)
            # Clearing an allowlist is a local, authority-reducing recovery
            # action and remains available after the extension is disabled.
            if payload.resources:
                require_connector(str(connection["connector_id"]), project_id)
            resources = [item.model_dump(mode="python") for item in payload.resources]
            return {
                "success": True,
                **service.replace_resources(
                    project_id=project_id,
                    connection_id=connection_id,
                    expected_revision=payload.revision,
                    resources=resources,
                ),
            }
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.get("/api/connectors/{connector_id}/audits")
    def get_connector_audits(
        connector_id: str,
        connection_id: Optional[str] = Query(default=None, max_length=512),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        try:
            return {
                "success": True,
                "audits": service.store.list_audits(
                    connector_id, connection_id=connection_id, limit=limit
                ),
            }
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    return router


def _callback_html(*, success: bool, connector_id: str) -> HTMLResponse:
    connector = {"github": "GitHub", "notion": "Notion", "gmail": "Gmail"}.get(
        connector_id, "Connector"
    )
    if success:
        title = f"{connector} connected"
        message = "The account is connected. You can close this window and return to Workbench."
        status_code = 200
    else:
        title = f"{connector} connection failed"
        message = "The account was not connected. Return to Workbench to review the status and try again."
        status_code = 400
    html = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{escape(title)}</title></head><body><main><h1>{escape(title)}</h1>"
        f"<p>{escape(message)}</p></main></body></html>"
    )
    return HTMLResponse(
        html,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def build_connector_callback_router(
    *,
    service: ConnectorService,
    require_extension: Optional[Callable[[str, Optional[str]], Any]] = None,
) -> APIRouter:
    """Build cookie-independent callbacks. Mount before the frontend catch-all."""

    router = APIRouter(tags=["connector-oauth-callbacks"])

    def authorize(connector_id: str) -> Any:
        if require_extension is None:
            return True
        return require_extension(f"connector.{connector_id}", None)

    def complete(
        connector_id: str,
        *,
        state: str,
        code: Optional[str],
        provider_error: Optional[str],
    ) -> HTMLResponse:
        try:
            service.complete_oauth(
                connector_id,
                state=state,
                code=code,
                provider_error=provider_error,
                authorize=authorize,
            )
            return _callback_html(success=True, connector_id=connector_id)
        except Exception:
            return _callback_html(success=False, connector_id=connector_id)

    @router.get("/oauth/callback/github", include_in_schema=False)
    def github_callback(
        state: str = Query(min_length=16, max_length=512),
        code: Optional[str] = Query(default=None, max_length=4096),
        error: Optional[str] = Query(default=None, max_length=512),
    ):
        return complete("github", state=state, code=code, provider_error=error)

    @router.get("/oauth/callback/notion", include_in_schema=False)
    def notion_callback(
        state: str = Query(min_length=16, max_length=512),
        code: Optional[str] = Query(default=None, max_length=4096),
        error: Optional[str] = Query(default=None, max_length=512),
    ):
        return complete("notion", state=state, code=code, provider_error=error)

    @router.get("/oauth/callback/gmail", include_in_schema=False)
    def gmail_callback(
        state: str = Query(min_length=16, max_length=512),
        code: Optional[str] = Query(default=None, max_length=4096),
        error: Optional[str] = Query(default=None, max_length=512),
    ):
        return complete("gmail", state=state, code=code, provider_error=error)

    return router


__all__ = ["build_connector_callback_router", "build_connectors_router"]
