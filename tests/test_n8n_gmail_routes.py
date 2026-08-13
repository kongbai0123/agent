from __future__ import annotations

import hashlib
import hmac
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import database
from api.routes.n8n_gmail import build_n8n_gmail_router
from n8n_gmail_crypto import AesGcmContentCipher
from n8n_gmail_service import FIXED_TEST_RECIPIENT, N8nGmailService


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "routes.db"))
    database.init_db()
    database.create_project("fixed_project", "Fixed", str(tmp_path / "fixed"))
    counter = {}

    def ids(prefix):
        counter[prefix] = counter.get(prefix, 0) + 1
        return f"{prefix}_{counter[prefix]}"

    now = datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc)
    service = N8nGmailService(
        cipher=AesGcmContentCipher(lambda: b"c" * 32),
        hmac_secret_provider=lambda: b"h" * 32,
        outbound_secret_provider=lambda: b"o" * 32,
        draft_generator=lambda request: {
            "subject": request["subject"] if request["mode"] == "reply" else "Composed",
            "body_text": "Draft body",
        },
        delivery_dispatcher=lambda payload: None,
        enable_guard=lambda profile: True,
        clock=lambda: now,
        id_factory=ids,
    )
    service.configure_profile({
        "project_id": "fixed_project", "workflow_key": "workflow_one",
        "required_label": "Workbench-Agent", "fixed_recipient": FIXED_TEST_RECIPIENT,
        "instruction": "Reply concisely", "default_model": "model-one",
        "enabled": True, "retention_days": 30,
    })
    app = FastAPI()
    app.include_router(build_n8n_gmail_router(
        service=service,
        require_local=lambda request: None,
        error_payload=lambda code, message, recoverable=False: {
            "code": code, "message": message, "recoverable": recoverable,
        },
    ))
    return TestClient(app), now


def _signed(path, payload, now, nonce):
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = int(now.timestamp())
    canonical = f"POST\n{path}\n{timestamp}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode()
    signature = hmac.new(b"h" * 32, canonical, hashlib.sha256).hexdigest()
    return body, {
        "content-type": "application/json", "X-N8N-Profile": "gmail",
        "X-N8N-Timestamp": str(timestamp), "X-N8N-Nonce": nonce,
        "X-N8N-Signature": f"sha256={signature}",
    }


def test_browser_contract_paths_and_strict_compose(tmp_path, monkeypatch):
    client, _ = _setup(tmp_path, monkeypatch)
    profile = client.get("/api/integrations/n8n/mail-profile")
    assert profile.status_code == 200
    assert profile.json()["instruction"] == "Reply concisely"

    rejected = client.post("/api/integrations/n8n/mail/compose", json={
        "instruction": "Draft", "subject": "Hello", "model": None,
        "body": "not allowed", "recipient": "attacker@example.com",
    })
    assert rejected.status_code == 422
    accepted = client.post("/api/integrations/n8n/mail/compose", json={
        "instruction": "Draft", "subject": "Hello", "model": None,
    })
    assert accepted.status_code == 202
    run_id = accepted.json()["run_id"]
    detail = client.get(f"/api/integrations/n8n/mail-runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "awaiting_approval"
    assert client.get("/api/integrations/n8n/gmail/profile").status_code == 404



def test_signed_event_forbids_inbound_instruction_and_accepts_strict_payload(tmp_path, monkeypatch):
    client, now = _setup(tmp_path, monkeypatch)
    path = "/api/integrations/n8n/v1/gmail/events"
    payload = {
        "event_id": "event_route_1", "workflow_key": "workflow_one",
        "gmail_message_id": "message_route_1", "gmail_thread_id": "thread_route_1",
        "sender": FIXED_TEST_RECIPIENT, "subject": "Subject", "body_text": "Body",
        "labels": ["INBOX", "Workbench-Agent"], "attachments": [], "thread_messages": [],
        "workflow_instruction": "PROMPT_INJECTION_NOT_ALLOWED",
    }
    body, headers = _signed(path, payload, now, "nonce_for_invalid1")
    response = client.post(path, content=body, headers=headers)
    assert response.status_code == 422

    payload.pop("workflow_instruction")
    payload["event_id"] = "event_route_2"
    payload["gmail_message_id"] = "message_route_2"
    body, headers = _signed(path, payload, now, "nonce_for_valid_22")
    response = client.post(path, content=body, headers=headers)
    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_callbacks_require_signature_at_route_boundary(tmp_path, monkeypatch):
    client, _ = _setup(tmp_path, monkeypatch)
    response = client.post(
        "/api/integrations/n8n/v1/gmail/deliveries/missing/claim",
        json={"claim_id": "claim", "claim_token": "x" * 64},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "authentication_failed"
