"""Narrow Workbench -> n8n wake-up client for approved Gmail deliveries."""

from __future__ import annotations

import base64
from typing import Any, Callable, Mapping

import requests


N8N_SEND_WEBHOOK_URL = "http://127.0.0.1:5678/webhook/workbench-gmail-send-v1"
N8N_WEBHOOK_SECRET_HEADER = "X-Workbench-Delivery-Key"


class N8nDeliveryDispatchError(RuntimeError):
    """The fixed loopback send workflow could not be awakened safely."""


class N8nDeliveryDispatcher:
    def __init__(
        self,
        *,
        secret_provider: Callable[[], bytes],
        post: Callable[..., Any] = requests.post,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._secret_provider = secret_provider
        self._post = post
        self._timeout = max(1.0, min(float(timeout_seconds), 30.0))

    def __call__(self, payload: Mapping[str, Any]) -> None:
        delivery_id = str(payload.get("delivery_id") or "")
        claim_token = str(payload.get("claim_token") or "")
        if not delivery_id or not claim_token:
            raise N8nDeliveryDispatchError("Delivery wake-up payload is incomplete.")
        try:
            secret = bytes(self._secret_provider())
        except Exception as exc:
            raise N8nDeliveryDispatchError("Delivery webhook secret is unavailable.") from exc
        if len(secret) != 32:
            raise N8nDeliveryDispatchError("Delivery webhook secret is invalid.")
        encoded = base64.urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")
        response = None
        try:
            response = self._post(
                N8N_SEND_WEBHOOK_URL,
                headers={
                    N8N_WEBHOOK_SECRET_HEADER: encoded,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={"delivery_id": delivery_id, "claim_token": claim_token},
                timeout=(3.0, self._timeout),
                allow_redirects=False,
            )
            if int(getattr(response, "status_code", 599)) not in {200, 202, 204}:
                raise N8nDeliveryDispatchError("The managed n8n send workflow rejected the wake-up.")
        except N8nDeliveryDispatchError:
            raise
        except Exception as exc:
            raise N8nDeliveryDispatchError("The managed n8n send workflow is unavailable.") from exc
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass


__all__ = [
    "N8N_SEND_WEBHOOK_URL",
    "N8N_WEBHOOK_SECRET_HEADER",
    "N8nDeliveryDispatchError",
    "N8nDeliveryDispatcher",
]
