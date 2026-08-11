"""Imported model API catalog, credential, and connectivity routes."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from api.schemas.settings import (
    ProviderConnectionTestRequest,
    ProviderModelTestRequest,
    ProviderSecretRequest,
    ProviderToolTestRequest,
)
from provider_connections import (
    ProviderConnectionFailure,
    catalog_payload,
    test_provider_connection,
    test_provider_model_response,
    test_provider_tool_call,
)
from secret_store import (
    delete_provider_secret,
    get_provider_secret,
    provider_secret_statuses,
    set_provider_secret,
)


def _build_provider_model_test_router(
    *,
    error_payload: Callable[..., Dict[str, Any]],
    require_local: Optional[Callable[[Request], None]],
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/settings/providers/model-test")
    def post_provider_model_test(
        request_data: ProviderModelTestRequest,
        request: Request,
    ):
        if require_local is not None:
            require_local(request)
        provider_id = str(request_data.provider_id).strip().casefold()
        supplied = (
            request_data.api_key.get_secret_value()
            if request_data.api_key is not None
            else ""
        )
        try:
            result = test_provider_model_response(
                provider_type=request_data.provider_type,
                base_url=request_data.base_url,
                api_key=supplied or get_provider_secret(provider_id),
                model=request_data.model,
                system_prompt=request_data.system_prompt,
                prompt=request_data.prompt,
                model_kind=request_data.model_kind,
                supports_tools=request_data.supports_tools,
                language_pair=request_data.language_pair,
            )
        except (ValueError, ProviderConnectionFailure) as exc:
            raise HTTPException(
                status_code=getattr(exc, "status_code", 400),
                detail=error_payload(
                    getattr(exc, "code", "INVALID_PROVIDER_CONFIGURATION"),
                    str(exc),
                    recoverable=getattr(exc, "recoverable", False),
                ),
            ) from exc
        return {"success": True, "provider_id": provider_id, **result}

    @router.post("/api/settings/providers/tool-test")
    def post_provider_tool_test(
        request_data: ProviderToolTestRequest,
        request: Request,
    ):
        if require_local is not None:
            require_local(request)
        provider_id = str(request_data.provider_id).strip().casefold()
        supplied = (
            request_data.api_key.get_secret_value()
            if request_data.api_key is not None
            else ""
        )
        try:
            result = test_provider_tool_call(
                provider_type=request_data.provider_type,
                base_url=request_data.base_url,
                api_key=supplied or get_provider_secret(provider_id),
                model=request_data.model,
                model_kind=request_data.model_kind,
                supports_tools=request_data.supports_tools,
                language_pair=request_data.language_pair,
            )
        except (ValueError, ProviderConnectionFailure) as exc:
            raise HTTPException(
                status_code=getattr(exc, "status_code", 400),
                detail=error_payload(
                    getattr(exc, "code", "INVALID_PROVIDER_CONFIGURATION"),
                    str(exc),
                    recoverable=getattr(exc, "recoverable", False),
                ),
            ) from exc
        return {"success": True, "provider_id": provider_id, **result}

    return router


def build_provider_settings_router(
    *,
    load_settings: Callable[[], Dict[str, Any]],
    error_payload: Callable[..., Dict[str, Any]],
    require_local: Optional[Callable[[Request], None]] = None,
) -> APIRouter:
    router = APIRouter()
    router.include_router(_build_provider_model_test_router(error_payload=error_payload, require_local=require_local))

    def local(request: Request) -> None:
        if require_local is not None:
            require_local(request)

    @router.get("/api/settings/secrets")
    def get_settings_secrets():
        provider_ids = [
            item["id"]
            for item in load_settings().get("model_providers", [])
            if isinstance(item, dict) and item.get("id")
        ]
        return {
            "success": True,
            "providers": provider_secret_statuses(provider_ids),
        }

    @router.get("/api/settings/providers/catalog")
    def get_provider_catalog():
        return {"success": True, "providers": catalog_payload()}

    @router.post("/api/settings/providers/test")
    def post_provider_test(request_data: ProviderConnectionTestRequest, request: Request):
        local(request)
        provider_id = str(request_data.provider_id).strip().casefold()
        supplied = (
            request_data.api_key.get_secret_value()
            if request_data.api_key is not None
            else ""
        )
        try:
            result = test_provider_connection(
                provider_type=request_data.provider_type,
                base_url=request_data.base_url,
                api_key=supplied or get_provider_secret(provider_id),
                source_url=request_data.source_url,
                selected_model=request_data.selected_model,
                model_kind=request_data.model_kind,
                supports_tools=request_data.supports_tools,
                language_pair=request_data.language_pair,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=error_payload(
                    "INVALID_PROVIDER_CONFIGURATION",
                    str(exc),
                    recoverable=False,
                ),
            ) from exc
        except ProviderConnectionFailure as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=error_payload(
                    exc.code,
                    exc.message,
                    recoverable=exc.recoverable,
                ),
            ) from exc
        return {"success": True, "provider_id": provider_id, **result}

    @router.post("/api/settings/secrets")
    def post_settings_secret(request_data: ProviderSecretRequest, request: Request):
        local(request)
        configured_ids = {
            item["id"]
            for item in load_settings().get("model_providers", [])
            if isinstance(item, dict) and item.get("id")
        }
        provider_id = str(request_data.provider_id).strip().casefold()
        if provider_id not in configured_ids:
            raise HTTPException(
                status_code=404,
                detail=error_payload(
                    "MODEL_PROVIDER_NOT_FOUND",
                    "Model provider is not configured.",
                    recoverable=False,
                ),
            )
        try:
            status = set_provider_secret(provider_id, request_data.api_key)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(
                status_code=400,
                detail=error_payload("SECRET_STORE_ERROR", str(exc), recoverable=True),
            ) from exc
        return {"success": True, **status}

    @router.delete("/api/settings/secrets/{provider_id}")
    def delete_settings_secret(provider_id: str, request: Request):
        local(request)
        try:
            deleted = delete_provider_secret(provider_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=error_payload(
                    "INVALID_PROVIDER_ID",
                    str(exc),
                    recoverable=False,
                ),
            ) from exc
        return {
            "success": True,
            "provider_id": provider_id,
            "configured": False,
            "deleted": deleted,
        }

    return router
