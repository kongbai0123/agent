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
    infer_provider_type,
    normalize_provider_endpoint,
    test_provider_connection,
    test_provider_model_response,
    test_provider_tool_call,
)
from secret_store import (
    delete_provider_secret,
    get_provider_secret,
    provider_secret_statuses,
    provider_credential_version,
    set_provider_secret,
)
from model_governance import ModelGovernanceService


def _resolve_provider_api_key(
    request_data: ProviderConnectionTestRequest,
    load_settings: Callable[[], Dict[str, Any]],
) -> str:
    supplied = (
        request_data.api_key.get_secret_value()
        if request_data.api_key is not None
        else ""
    )
    if supplied:
        return supplied

    provider_id = str(request_data.provider_id).strip().casefold()
    configured = next(
        (
            item
            for item in load_settings().get("model_providers", [])
            if isinstance(item, dict)
            and str(item.get("id") or "").strip().casefold() == provider_id
        ),
        None,
    )
    if configured is None:
        raise ProviderConnectionFailure(
            "PROVIDER_KEY_REQUIRED",
            "目前的供應商設定沒有可安全重用的 API Key；請重新輸入。",
            400,
            True,
        )

    requested_type = str(request_data.provider_type or "").strip().casefold()
    configured_type = infer_provider_type(configured)
    requested_endpoint = normalize_provider_endpoint(
        requested_type,
        request_data.base_url,
    )
    configured_endpoint = normalize_provider_endpoint(
        configured_type,
        str(configured.get("base_url") or ""),
    )
    if (
        configured_type != requested_type
        or configured_endpoint != requested_endpoint
    ):
        raise ProviderConnectionFailure(
            "PROVIDER_SECRET_CONTEXT_MISMATCH",
            (
                "已儲存的 API Key 屬於不同的供應商或端點；"
                "請為目前設定重新輸入 API Key。"
            ),
            409,
            True,
        )
    return get_provider_secret(provider_id)


def _build_provider_model_test_router(
    *,
    load_settings: Callable[[], Dict[str, Any]],
    error_payload: Callable[..., Dict[str, Any]],
    require_local: Optional[Callable[[Request], None]],
    model_governance: Optional[ModelGovernanceService],
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
        try:
            result = test_provider_model_response(
                provider_type=request_data.provider_type,
                base_url=request_data.base_url,
                api_key=_resolve_provider_api_key(request_data, load_settings),
                model=request_data.model,
                system_prompt=request_data.system_prompt,
                prompt=request_data.prompt,
                model_kind=request_data.model_kind,
                supports_tools=request_data.supports_tools,
                language_pair=request_data.language_pair,
                source_url=request_data.source_url,
                image_data_url=request_data.image_data_url,
            )
        except (ValueError, ProviderConnectionFailure) as exc:
            if model_governance is not None and isinstance(exc, ProviderConnectionFailure):
                model_governance.observe_failure(
                    request_data.provider_id,
                    model_id=request_data.model,
                    endpoint=request_data.base_url,
                    status_code=exc.status_code,
                    capability="ocr" if request_data.model_kind == "vision" else request_data.model_kind or "chat",
                )
            raise HTTPException(
                status_code=getattr(exc, "status_code", 400),
                detail=error_payload(
                    getattr(exc, "code", "INVALID_PROVIDER_CONFIGURATION"),
                    str(exc),
                    recoverable=getattr(exc, "recoverable", False),
                ),
            ) from exc
        if model_governance is not None:
            model_governance.mark_verified(
                provider_id,
                model_id=request_data.model,
                endpoint=request_data.base_url,
            )
        return {"success": True, "provider_id": provider_id, **result}

    @router.post("/api/settings/providers/tool-test")
    def post_provider_tool_test(
        request_data: ProviderToolTestRequest,
        request: Request,
    ):
        if require_local is not None:
            require_local(request)
        provider_id = str(request_data.provider_id).strip().casefold()
        try:
            result = test_provider_tool_call(
                provider_type=request_data.provider_type,
                base_url=request_data.base_url,
                api_key=_resolve_provider_api_key(request_data, load_settings),
                model=request_data.model,
                model_kind=request_data.model_kind,
                supports_tools=request_data.supports_tools,
                language_pair=request_data.language_pair,
            )
        except (ValueError, ProviderConnectionFailure) as exc:
            if model_governance is not None and isinstance(exc, ProviderConnectionFailure):
                model_governance.observe_failure(
                    request_data.provider_id,
                    model_id=request_data.model,
                    endpoint=request_data.base_url,
                    status_code=exc.status_code,
                    capability="tools",
                )
            raise HTTPException(
                status_code=getattr(exc, "status_code", 400),
                detail=error_payload(
                    getattr(exc, "code", "INVALID_PROVIDER_CONFIGURATION"),
                    str(exc),
                    recoverable=getattr(exc, "recoverable", False),
                ),
            ) from exc
        if model_governance is not None:
            model_governance.mark_verified(provider_id, model_id=request_data.model, endpoint=request_data.base_url)
        return {"success": True, "provider_id": provider_id, **result}

    return router


def build_provider_settings_router(
    *,
    load_settings: Callable[[], Dict[str, Any]],
    error_payload: Callable[..., Dict[str, Any]],
    require_local: Optional[Callable[[Request], None]] = None,
    model_governance: Optional[ModelGovernanceService] = None,
) -> APIRouter:
    router = APIRouter()
    router.include_router(_build_provider_model_test_router(
        load_settings=load_settings,
        error_payload=error_payload,
        require_local=require_local,
        model_governance=model_governance,
    ))

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
        try:
            result = test_provider_connection(
                provider_type=request_data.provider_type,
                base_url=request_data.base_url,
                api_key=_resolve_provider_api_key(request_data, load_settings),
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
            if model_governance is not None:
                model_governance.observe_failure(
                    request_data.provider_id,
                    model_id=request_data.selected_model,
                    endpoint=request_data.base_url,
                    status_code=exc.status_code,
                    capability=request_data.model_kind or "connection",
                )
            raise HTTPException(
                status_code=exc.status_code,
                detail=error_payload(
                    exc.code,
                    exc.message,
                    recoverable=exc.recoverable,
                ),
            ) from exc
        if model_governance is not None:
            model_governance.observe_success(
                provider_id,
                model_id=request_data.selected_model,
                endpoint=request_data.base_url,
            )
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
            if model_governance is not None:
                model_governance.rotate_credential(
                    provider_id,
                    provider_credential_version(provider_id),
                )
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
            if deleted and model_governance is not None:
                model_governance.clear_credential(provider_id)
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
