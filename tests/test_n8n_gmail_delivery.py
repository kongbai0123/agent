from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from n8n_gmail_delivery import (  # noqa: E402
    N8N_SEND_WEBHOOK_URL,
    N8nDeliveryDispatchError,
    N8nDeliveryDispatcher,
)


class Response:
    def __init__(self, status_code=202):
        self.status_code = status_code
        self.closed = False

    def close(self):
        self.closed = True


def test_dispatch_uses_only_fixed_loopback_url_and_narrow_payload():
    seen = {}
    response = Response()

    def post(url, **kwargs):
        seen.update(url=url, **kwargs)
        return response

    dispatcher = N8nDeliveryDispatcher(secret_provider=lambda: b"s" * 32, post=post)
    dispatcher({"delivery_id": "delivery-1", "claim_token": "claim-1", "ignored": "x"})

    assert seen["url"] == N8N_SEND_WEBHOOK_URL
    assert seen["json"] == {"delivery_id": "delivery-1", "claim_token": "claim-1"}
    assert seen["allow_redirects"] is False
    assert seen["headers"]["X-Workbench-Delivery-Key"]
    assert "s" * 32 not in str(seen)
    assert response.closed is True


def test_dispatch_fails_closed_for_bad_secret_or_http_response():
    with pytest.raises(N8nDeliveryDispatchError):
        N8nDeliveryDispatcher(secret_provider=lambda: b"short")(
            {"delivery_id": "d", "claim_token": "c"}
        )
    with pytest.raises(N8nDeliveryDispatchError):
        N8nDeliveryDispatcher(
            secret_provider=lambda: b"s" * 32,
            post=lambda *_args, **_kwargs: Response(302),
        )({"delivery_id": "d", "claim_token": "c"})
