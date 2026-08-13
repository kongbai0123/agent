"""Strict HTTP boundary for the n8n Gmail integration."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from n8n_gmail_service import GmailIntegrationError, N8nGmailService


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProfileUpdate(_StrictModel):
    project_id: str = Field(min_length=1, max_length=128)
    instruction: str = Field(min_length=1, max_length=20_000)
    default_model: Optional[str] = Field(default=None, max_length=255)
    enabled: bool
    auto_start: bool = False


class AttachmentMetadata(_StrictModel):
    attachment_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(max_length=512)
    mime_type: str = Field(default="application/octet-stream", max_length=255)
    size_bytes: int = Field(ge=0)


class ThreadMessage(_StrictModel):
    gmail_message_id: str = Field(min_length=1, max_length=128)
    sender: str = Field(default="", max_length=320)
    subject: str = Field(default="", max_length=998)
    body_text: str = Field(default="", max_length=100_000)
    sent_at: str = Field(default="", max_length=64)


class InboundEvent(_StrictModel):
    event_id: str = Field(min_length=1, max_length=128)
    workflow_key: str = Field(min_length=1, max_length=128)
    gmail_message_id: str = Field(min_length=1, max_length=128)
    gmail_thread_id: str = Field(min_length=1, max_length=128)
    sender: str = Field(min_length=3, max_length=320)
    subject: str = Field(default="", max_length=998)
    body_text: str = Field(default="", max_length=100_000)
    labels: List[str] = Field(min_length=1, max_length=100)
    attachments: List[AttachmentMetadata] = Field(default_factory=list, max_length=50)
    thread_messages: List[ThreadMessage] = Field(default_factory=list, max_length=20)


class ComposeRequest(_StrictModel):
    instruction: str = Field(min_length=1, max_length=20_000)
    model: Optional[str] = Field(default=None, max_length=255)
    subject: str = Field(default="", max_length=998)


class DraftEditRequest(_StrictModel):
    expected_revision: int = Field(ge=1)
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    subject: Optional[str] = Field(default=None, max_length=998)
    body: str = Field(max_length=500_000)


class DraftApproveRequest(_StrictModel):
    expected_revision: int = Field(ge=1)
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class DeliveryClaimRequest(_StrictModel):
    claim_id: str = Field(min_length=1, max_length=128)
    claim_token: str = Field(min_length=32, max_length=256)


class DeliveryResultRequest(_StrictModel):
    result_id: str = Field(min_length=1, max_length=128)
    result_token: str = Field(min_length=32, max_length=256)
    status: Literal["sent", "failed"]
    gmail_message_id: Optional[str] = Field(default=None, max_length=128)
    gmail_thread_id: Optional[str] = Field(default=None, max_length=128)
    error_code: Optional[str] = Field(default=None, max_length=128)
    recoverable: Optional[bool] = None


def _failure(exc: BaseException, error_payload: Callable[..., Dict[str, Any]]) -> HTTPException:
    if isinstance(exc, GmailIntegrationError):
        return HTTPException(
            status_code=exc.status_code,
            detail=error_payload(exc.code, exc.message, recoverable=exc.recoverable),
        )
    if isinstance(exc, ValidationError):
        return HTTPException(
            status_code=422,
            detail=error_payload("invalid_request", "The request body is invalid.", recoverable=True),
        )
    return HTTPException(
        status_code=500,
        detail=error_payload("gmail_integration_error", "The Gmail integration request failed.", recoverable=False),
    )


def build_n8n_gmail_router(
    *,
    service: N8nGmailService,
    require_local: Callable[[Request], None],
    error_payload: Callable[..., Dict[str, Any]],
) -> APIRouter:
    """Build the router.  The caller owns mounting and local-middleware bypasses."""

    router = APIRouter(tags=["n8n-gmail"])

    def local(request: Request) -> None:
        require_local(request)

    def generate_in_background(draft_id: str) -> None:
        try:
            service.generate_draft(draft_id)
        except Exception:
            # The service already persists a content-free failure state.
            return

    def dispatch_in_background(delivery_id: str) -> None:
        try:
            service.dispatch_delivery(delivery_id)
        except Exception:
            # The delivery remains approved_queued and records only a safe code.
            return

    async def signed_payload(request: Request, model_type: type[_StrictModel]) -> _StrictModel:
        body = await request.body()
        try:
            service.authenticate_request(
                method=request.method, path=request.url.path, headers=request.headers, body=body
            )
            return model_type.model_validate_json(body, strict=True)
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.get("/api/integrations/n8n/mail-profile")
    def get_profile(request: Request):
        local(request)
        return service.get_profile()

    @router.put("/api/integrations/n8n/mail-profile")
    def put_profile(payload: ProfileUpdate, request: Request):
        local(request)
        try:
            # Workflow identity, label, recipient and retention are V1 server
            # policy.  They are intentionally absent from the browser request.
            return service.configure_profile(
                {
                    **payload.model_dump(),
                    "workflow_key": "workbench-gmail-inbound-v1",
                    "required_label": "Workbench-Agent",
                    "fixed_recipient": service.fixed_recipient,
                    "retention_days": 30,
                }
            )
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.get("/api/integrations/n8n/mail-runs")
    def list_mail_runs(
        request: Request,
        status: Optional[str] = Query(default=None, max_length=64),
        limit: int = Query(default=100, ge=1, le=250),
    ):
        local(request)
        try:
            return {"runs": service.list_drafts(status=status, limit=limit)}
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.get("/api/integrations/n8n/mail-runs/{run_id}")
    def get_mail_run(run_id: str, request: Request):
        local(request)
        try:
            return service.get_mail_run(run_id)
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.post("/api/integrations/n8n/mail/compose", status_code=202)
    def compose(payload: ComposeRequest, request: Request, background: BackgroundTasks):
        local(request)
        try:
            result = service.compose(payload.model_dump())
            background.add_task(generate_in_background, result["draft_id"])
            return result
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.patch("/api/integrations/n8n/mail-drafts/{draft_id}")
    def edit_draft(draft_id: str, payload: DraftEditRequest, request: Request):
        local(request)
        try:
            return service.edit_draft(draft_id, payload.model_dump())
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.post("/api/integrations/n8n/mail-drafts/{draft_id}/approve")
    def approve_draft(
        draft_id: str, payload: DraftApproveRequest, request: Request,
        background: BackgroundTasks,
    ):
        local(request)
        try:
            result = service.approve_draft(draft_id, payload.model_dump())
            background.add_task(dispatch_in_background, result["delivery_id"])
            return result
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.post("/api/integrations/n8n/mail-drafts/{draft_id}/reject")
    def reject_draft(draft_id: str, payload: DraftApproveRequest, request: Request):
        local(request)
        try:
            return service.reject_draft(draft_id, payload.model_dump())
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.post("/api/integrations/n8n/mail-drafts/{draft_id}/regenerate", status_code=202)
    def regenerate_draft(
        draft_id: str, payload: DraftApproveRequest, request: Request,
        background: BackgroundTasks,
    ):
        local(request)
        try:
            result = service.regenerate_draft(draft_id, payload.model_dump())
            background.add_task(generate_in_background, draft_id)
            return result
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.delete("/api/integrations/n8n/mail-threads/{thread_id}")
    def delete_thread(thread_id: str, request: Request):
        local(request)
        try:
            return service.delete_thread(thread_id)
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.post("/api/integrations/n8n/mail-retention/purge")
    def purge_retention(request: Request):
        local(request)
        try:
            return service.purge_retention()
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.post("/api/integrations/n8n/v1/gmail/events", status_code=202)
    async def receive_event(request: Request, background: BackgroundTasks):
        payload = await signed_payload(request, InboundEvent)
        try:
            result = service.receive_event(payload.model_dump())
            if not result["idempotent"] and result.get("draft_id"):
                background.add_task(generate_in_background, result["draft_id"])
            return result
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.post("/api/integrations/n8n/v1/gmail/deliveries/{delivery_id}/claim")
    async def claim_delivery(delivery_id: str, request: Request):
        payload = await signed_payload(request, DeliveryClaimRequest)
        try:
            return service.claim_delivery(delivery_id, payload.model_dump())
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    @router.post("/api/integrations/n8n/v1/gmail/deliveries/{delivery_id}/result")
    async def complete_delivery(delivery_id: str, request: Request):
        payload = await signed_payload(request, DeliveryResultRequest)
        try:
            return service.complete_delivery(delivery_id, payload.model_dump())
        except Exception as exc:
            raise _failure(exc, error_payload) from exc

    return router


INTEGRATION_CALLBACK_PREFIX = "/api/integrations/n8n/v1/gmail/"
