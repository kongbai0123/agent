"""Local management and public v1 boundaries for the Workbench Agent API."""

from __future__ import annotations

import inspect
import json
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Literal, Mapping, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from external_agent_api import (
    ExternalAgentApiError,
    ExternalAgentApiService,
    ExternalApiPrincipal,
)


RuntimeCallback = Callable[..., Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Scope = Literal["runs:create", "runs:read", "runs:cancel", "capabilities:read"]


class ExternalApiKeyCreate(_Strict):
    name: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=128)
    scopes: List[Scope] = Field(min_length=1, max_length=4)
    # JSON transports datetimes as ISO-8601 strings. Keep the surrounding
    # request contract strict while allowing the standard wire format here.
    expires_at: Optional[datetime] = Field(default=None, strict=False)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=6000)
    request_limit_daily: int = Field(default=10_000, ge=1, le=10_000_000)


class ExternalApiKeyPolicyReplace(_Strict):
    revision: int = Field(ge=1)
    enabled: bool
    scopes: List[Scope] = Field(min_length=1, max_length=4)
    expires_at: Optional[datetime] = Field(default=None, strict=False)
    rate_limit_per_minute: int = Field(ge=1, le=6000)
    request_limit_daily: int = Field(ge=1, le=10_000_000)


class ExternalApiKeyRevision(_Strict):
    revision: int = Field(ge=1)


class ExternalApiInstallationReset(_Strict):
    confirmation: Literal["RESET_EXTERNAL_API"]


class ExternalRunCreate(_Strict):
    message: str = Field(min_length=1, max_length=100_000)
    model: Optional[str] = Field(default=None, min_length=1, max_length=255)
    use_rag: bool = False


RunStatus = Literal[
    "queued",
    "pending",
    "running",
    "approval_required",
    "completed",
    "failed",
    "cancelled",
]


class RuntimeErrorDto(_Strict):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1000)
    recoverable: bool = False


class RuntimeRunDto(_Strict):
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_-]{8,80}$")
    project_id: str = Field(min_length=1, max_length=128)
    status: RunStatus
    session_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    model: Optional[str] = Field(default=None, min_length=1, max_length=255)
    answer: Optional[str] = Field(default=None, max_length=262_144)
    error: Optional[RuntimeErrorDto] = None
    created_at: Optional[str] = Field(default=None, max_length=80)
    updated_at: Optional[str] = Field(default=None, max_length=80)
    completed_at: Optional[str] = Field(default=None, max_length=80)


class RuntimeCapabilitiesDto(_Strict):
    project_id: str = Field(min_length=1, max_length=128)
    chat: bool
    streaming: bool = False
    tools: List[str] = Field(default_factory=list, max_length=256)
    models: List[str] = Field(default_factory=list, max_length=256)


def _public_base_url(request: Request) -> str:
    server = request.scope.get("server") or ("127.0.0.1", 8000)
    host = str(server[0] or "127.0.0.1").strip("[]")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        host = "127.0.0.1"
    display_host = f"[{host}]" if ":" in host else host
    port = int(server[1] or 8000)
    scheme = "https" if str(request.scope.get("scheme")) == "https" else "http"
    return f"{scheme}://{display_host}:{port}/api/public/v1"


def _auth_context(principal: ExternalApiPrincipal) -> dict[str, Any]:
    return {
        "installation_id": principal.installation_id,
        "api_key_id": principal.key_id,
        "api_key_name": principal.key_name,
        "project_id": principal.project_id,
        "scopes": sorted(principal.scopes),
        "actor": "external_api",
    }


async def _invoke(callback: RuntimeCallback, *args: Any) -> dict[str, Any]:
    try:
        result = callback(*args)
        if inspect.isawaitable(result):
            result = await result
    except ExternalAgentApiError:
        raise
    except Exception as exc:
        # Never reflect runtime exception text across the public boundary.  The
        # host adapter should translate known failures to ExternalAgentApiError.
        raise ExternalAgentApiError(
            "EXTERNAL_API_RUNTIME_UNAVAILABLE",
            "Agent 執行服務暫時無法使用。",
            status_code=503,
            recoverable=True,
        ) from exc
    if not isinstance(result, Mapping):
        raise ExternalAgentApiError(
            "EXTERNAL_API_RUNTIME_CONTRACT_INVALID",
            "Agent 執行服務回傳了無效結果。",
            status_code=502,
            recoverable=True,
        )
    # Bound serialization before returning a callback result to avoid an
    # accidental unbounded object or non-JSON response crossing this API.
    try:
        encoded = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        raise ExternalAgentApiError(
            "EXTERNAL_API_RUNTIME_CONTRACT_INVALID",
            "Agent 執行服務回傳了無效結果。",
            status_code=502,
        ) from exc
    if len(encoded.encode("utf-8")) > 1_048_576:
        raise ExternalAgentApiError(
            "EXTERNAL_API_RESPONSE_TOO_LARGE",
            "Agent 執行結果超過對外 API 的大小限制。",
            status_code=502,
        )
    return dict(result)


def _validated_run_result(
    result: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_project_id: str,
) -> dict[str, Any]:
    try:
        validated = RuntimeRunDto.model_validate(result, strict=True)
    except ValidationError as exc:
        raise ExternalAgentApiError(
            "EXTERNAL_API_RUNTIME_CONTRACT_INVALID",
            "Agent 執行服務回傳了不符合公開契約的結果。",
            status_code=502,
        ) from exc
    if validated.run_id != expected_run_id or validated.project_id != expected_project_id:
        raise ExternalAgentApiError(
            "EXTERNAL_API_RUNTIME_SCOPE_MISMATCH",
            "Agent 執行服務回傳的 Run 或 Project 範圍不符。",
            status_code=502,
            recoverable=False,
        )
    return validated.model_dump(mode="json", exclude_none=True)


def _validated_capabilities_result(
    result: Mapping[str, Any], *, expected_project_id: str
) -> dict[str, Any]:
    try:
        validated = RuntimeCapabilitiesDto.model_validate(result, strict=True)
    except ValidationError as exc:
        raise ExternalAgentApiError(
            "EXTERNAL_API_RUNTIME_CONTRACT_INVALID",
            "Agent 能力清單不符合公開契約。",
            status_code=502,
        ) from exc
    if validated.project_id != expected_project_id:
        raise ExternalAgentApiError(
            "EXTERNAL_API_RUNTIME_SCOPE_MISMATCH",
            "Agent 能力清單的 Project 範圍不符。",
            status_code=502,
            recoverable=False,
        )
    payload = validated.model_dump(mode="json", exclude_none=True)
    payload.pop("project_id", None)
    if any(len(item) > 255 for item in payload.get("tools", [])) or any(
        len(item) > 255 for item in payload.get("models", [])
    ):
        raise ExternalAgentApiError(
            "EXTERNAL_API_RUNTIME_CONTRACT_INVALID",
            "Agent 能力清單包含過長的識別碼。",
            status_code=502,
        )
    return payload


def build_external_agent_api_router(
    *,
    service: ExternalAgentApiService,
    require_local: Callable[[Request], None],
    error_payload: Callable[..., Dict[str, Any]],
    submit_run: RuntimeCallback,
    get_run: RuntimeCallback,
    cancel_run: RuntimeCallback,
    capabilities: RuntimeCallback,
) -> APIRouter:
    """Build routes while keeping the chat/runtime implementation injected."""

    router = APIRouter(tags=["integration-center", "public-agent-api"])

    def failure(exc: BaseException) -> HTTPException:
        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, ExternalAgentApiError):
            headers: dict[str, str] = {}
            if exc.retry_after is not None:
                headers["Retry-After"] = str(exc.retry_after)
            if exc.status_code == 401:
                headers["WWW-Authenticate"] = "Bearer"
            return HTTPException(
                status_code=exc.status_code,
                detail=error_payload(
                    exc.code,
                    exc.message,
                    recoverable=exc.recoverable,
                ),
                headers=headers or None,
            )
        return HTTPException(
            status_code=500,
            detail=error_payload(
                "EXTERNAL_API_INTERNAL_ERROR",
                "Workbench 對外 API 發生內部錯誤。",
                recoverable=False,
            ),
        )

    def local(request: Request) -> None:
        require_local(request)

    def authorize(
        request: Request,
        *,
        scope: str,
        action: str,
    ) -> ExternalApiPrincipal:
        return service.authenticate(
            request.headers.get("authorization"),
            required_scope=scope,
            action=action,
        )

    @router.get("/api/integration-center/api-keys")
    def list_api_keys(request: Request):
        local(request)
        try:
            return service.list_keys(api_base_url=_public_base_url(request))
        except Exception as exc:
            raise failure(exc) from exc

    @router.post("/api/integration-center/api-keys", status_code=201)
    def create_api_key(
        payload: ExternalApiKeyCreate,
        request: Request,
        response: Response,
    ):
        local(request)
        try:
            result = service.issue_key(
                name=payload.name,
                project_id=payload.project_id,
                scopes=payload.scopes,
                expires_at=payload.expires_at,
                rate_limit_per_minute=payload.rate_limit_per_minute,
                request_limit_daily=payload.request_limit_daily,
            )
            response.headers["Cache-Control"] = "no-store"
            return result
        except Exception as exc:
            raise failure(exc) from exc

    @router.put("/api/integration-center/api-keys/{key_id}")
    def replace_api_key_policy(
        key_id: str,
        payload: ExternalApiKeyPolicyReplace,
        request: Request,
    ):
        local(request)
        try:
            return service.replace_key_policy(
                key_id=key_id,
                expected_revision=payload.revision,
                enabled=payload.enabled,
                scopes=payload.scopes,
                expires_at=payload.expires_at,
                rate_limit_per_minute=payload.rate_limit_per_minute,
                request_limit_daily=payload.request_limit_daily,
            )
        except Exception as exc:
            raise failure(exc) from exc

    @router.post("/api/integration-center/api-keys/{key_id}/rotate", status_code=201)
    def rotate_api_key(
        key_id: str,
        payload: ExternalApiKeyRevision,
        request: Request,
        response: Response,
    ):
        local(request)
        try:
            result = service.rotate_key(
                key_id=key_id, expected_revision=payload.revision
            )
            response.headers["Cache-Control"] = "no-store"
            return result
        except Exception as exc:
            raise failure(exc) from exc

    @router.post("/api/integration-center/api-keys/{key_id}/revoke")
    def revoke_api_key(
        key_id: str,
        payload: ExternalApiKeyRevision,
        request: Request,
    ):
        local(request)
        try:
            return service.revoke_key(
                key_id=key_id, expected_revision=payload.revision
            )
        except Exception as exc:
            raise failure(exc) from exc

    @router.get("/api/integration-center/api-audits")
    def list_api_audits(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ):
        local(request)
        try:
            return {
                "success": True,
                "audits": service.store.list_audits(limit=limit),
                "auth_failures": service.store.list_auth_failures(limit=limit),
            }
        except Exception as exc:
            raise failure(exc) from exc

    @router.post("/api/integration-center/installation/reset")
    def reset_external_api_installation(
        payload: ExternalApiInstallationReset,
        request: Request,
    ):
        local(request)
        try:
            return service.reset_installation(confirmation=payload.confirmation)
        except Exception as exc:
            raise failure(exc) from exc

    @router.get("/api/public/v1/capabilities")
    async def public_capabilities(request: Request):
        principal: Optional[ExternalApiPrincipal] = None
        try:
            principal = authorize(
                request,
                scope="capabilities:read",
                action="public.capabilities.read",
            )
            raw_result = await _invoke(capabilities, _auth_context(principal))
            result = _validated_capabilities_result(
                raw_result, expected_project_id=principal.project_id
            )
            service.record_operation(
                principal=principal,
                action="public.capabilities.read.result",
                status="succeeded",
            )
            return {
                "success": True,
                "installation_id": principal.installation_id,
                "project_id": principal.project_id,
                "scopes": sorted(principal.scopes),
                "capabilities": result,
            }
        except Exception as exc:
            if principal is not None:
                service.record_operation(
                    principal=principal,
                    action="public.capabilities.read.result",
                    status="failed",
                    error_code=getattr(exc, "code", "EXTERNAL_API_RUNTIME_ERROR"),
                )
            raise failure(exc) from exc

    @router.post("/api/public/v1/runs", status_code=202)
    async def public_create_run(payload: ExternalRunCreate, request: Request):
        principal: Optional[ExternalApiPrincipal] = None
        reservation: Optional[dict[str, Any]] = None
        try:
            principal = authorize(
                request, scope="runs:create", action="public.run.create"
            )
            runtime_payload = payload.model_dump(mode="json")
            reservation = service.reserve_idempotent_run(
                principal=principal,
                idempotency_key=request.headers.get("idempotency-key"),
                request_payload=runtime_payload,
            )
            run_id = str(reservation["run_id"])
            if reservation["replayed"]:
                replay = reservation.get("response")
                if not isinstance(replay, Mapping):
                    replay = {
                        "run_id": run_id,
                        "project_id": principal.project_id,
                        "status": "queued",
                    }
                result = _validated_run_result(
                    replay,
                    expected_run_id=run_id,
                    expected_project_id=principal.project_id,
                )
                service.record_operation(
                    principal=principal,
                    action="public.run.create.replay",
                    status="succeeded",
                    run_id=run_id,
                )
                return {"success": True, "idempotency_replayed": True, **result}
            raw_result = await _invoke(
                submit_run,
                run_id,
                runtime_payload,
                _auth_context(principal),
            )
            result = _validated_run_result(
                raw_result,
                expected_run_id=run_id,
                expected_project_id=principal.project_id,
            )
            service.complete_idempotent_run(
                reservation=reservation,
                response=result,
                succeeded=True,
            )
            service.record_operation(
                principal=principal,
                action="public.run.create.result",
                status="succeeded",
                run_id=run_id,
            )
            return {"success": True, "idempotency_replayed": False, **result}
        except Exception as exc:
            if principal is not None:
                if reservation is not None and not reservation.get("replayed"):
                    run_id = str(reservation["run_id"])
                    failure_response = {
                        "run_id": run_id,
                        "project_id": principal.project_id,
                        "status": "failed",
                        "error": {
                            "code": str(
                                getattr(
                                    exc,
                                    "code",
                                    "EXTERNAL_API_RUNTIME_UNAVAILABLE",
                                )
                            )[:128],
                            "message": "Agent 執行服務未能接受這項工作。",
                            "recoverable": bool(getattr(exc, "recoverable", True)),
                        },
                    }
                    try:
                        service.complete_idempotent_run(
                            reservation=reservation,
                            response=failure_response,
                            succeeded=False,
                            error_code=failure_response["error"]["code"],
                        )
                    except Exception:
                        pass
                service.record_operation(
                    principal=principal,
                    action="public.run.create.result",
                    status="failed",
                    error_code=getattr(exc, "code", "EXTERNAL_API_RUNTIME_ERROR"),
                )
            raise failure(exc) from exc

    @router.get("/api/public/v1/runs/{run_id}")
    async def public_get_run(run_id: str, request: Request):
        principal: Optional[ExternalApiPrincipal] = None
        try:
            principal = authorize(
                request, scope="runs:read", action="public.run.read"
            )
            normalized = service.require_run(principal=principal, run_id=run_id)
            raw_result = await _invoke(get_run, normalized, _auth_context(principal))
            result = _validated_run_result(
                raw_result,
                expected_run_id=normalized,
                expected_project_id=principal.project_id,
            )
            service.record_operation(
                principal=principal,
                action="public.run.read.result",
                status="succeeded",
                run_id=normalized,
            )
            return {"success": True, **result}
        except Exception as exc:
            if principal is not None:
                service.record_operation(
                    principal=principal,
                    action="public.run.read.result",
                    status="failed",
                    run_id=run_id,
                    error_code=getattr(exc, "code", "EXTERNAL_API_RUNTIME_ERROR"),
                )
            raise failure(exc) from exc

    @router.post("/api/public/v1/runs/{run_id}/cancel")
    async def public_cancel_run(run_id: str, request: Request):
        principal: Optional[ExternalApiPrincipal] = None
        try:
            principal = authorize(
                request, scope="runs:cancel", action="public.run.cancel"
            )
            normalized = service.require_run(principal=principal, run_id=run_id)
            raw_result = await _invoke(
                cancel_run, normalized, _auth_context(principal)
            )
            result = _validated_run_result(
                raw_result,
                expected_run_id=normalized,
                expected_project_id=principal.project_id,
            )
            service.mark_run_cancelled(run_id=normalized)
            service.record_operation(
                principal=principal,
                action="public.run.cancel.result",
                status="succeeded",
                run_id=normalized,
            )
            return {"success": True, **result}
        except Exception as exc:
            if principal is not None:
                service.record_operation(
                    principal=principal,
                    action="public.run.cancel.result",
                    status="failed",
                    run_id=run_id,
                    error_code=getattr(exc, "code", "EXTERNAL_API_RUNTIME_ERROR"),
                )
            raise failure(exc) from exc

    return router


__all__ = [
    "ExternalApiKeyCreate",
    "ExternalApiKeyPolicyReplace",
    "ExternalApiKeyRevision",
    "ExternalApiInstallationReset",
    "ExternalRunCreate",
    "build_external_agent_api_router",
]
