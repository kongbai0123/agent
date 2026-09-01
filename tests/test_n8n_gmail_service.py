from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import database
from n8n_gmail_crypto import AesGcmContentCipher
from n8n_gmail_service import (
    FIXED_TEST_RECIPIENT,
    GmailIntegrationError,
    N8nGmailService,
)


@pytest.fixture()
def gmail(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "workbench.db"))
    database.init_db()
    database.create_project("project_one", "Project", str(tmp_path / "project"))
    generated = []
    dispatched = []
    ids = {}

    def id_factory(prefix):
        ids[prefix] = ids.get(prefix, 0) + 1
        return f"{prefix}_{ids[prefix]}"

    def generator(request):
        generated.append(dict(request))
        return {
            "subject": request["subject"] if request["mode"] == "reply" else "Generated subject",
            "body_text": "Generated private body",
            "summary": "safe summary",
            "model": "local-model",
            "provider": "local",
            "skills": ["mail-style"],
            "references": ["ref-id"],
        }

    now = datetime(2026, 8, 13, 3, 0, tzinfo=timezone.utc)
    service = N8nGmailService(
        cipher=AesGcmContentCipher(lambda: b"k" * 32),
        hmac_secret_provider=lambda: b"i" * 32,
        outbound_secret_provider=lambda: b"o" * 32,
        draft_generator=generator,
        delivery_dispatcher=lambda value: dispatched.append(dict(value)),
        enable_guard=lambda profile: {"ready": True},
        permission_check=lambda *_args, **_kwargs: {"decision": "allow"},
        clock=lambda: now,
        id_factory=id_factory,
    )
    service.configure_profile(
        {
            "project_id": "project_one",
            "workflow_key": "gmail_workflow",
            "required_label": "Workbench-Agent",
            "fixed_recipient": FIXED_TEST_RECIPIENT,
            "instruction": "PROFILE_INSTRUCTION_PRIVATE",
            "default_model": "default-model",
            "enabled": True,
            "retention_days": 30,
        }
    )
    return service, generated, dispatched, now, tmp_path / "workbench.db"


def inbound(**changes):
    payload = {
        "event_id": "event_1",
        "workflow_key": "gmail_workflow",
        "gmail_message_id": "message_1",
        "gmail_thread_id": "thread_1",
        "sender": FIXED_TEST_RECIPIENT,
        "subject": "INBOUND_SUBJECT_PRIVATE",
        "body_text": "INBOUND_BODY_PRIVATE",
        "labels": ["INBOX", "Workbench-Agent"],
        "attachments": [
            {
                "attachment_id": "attachment_1",
                "filename": "private-name.txt",
                "mime_type": "text/plain",
                "size_bytes": 12,
            }
        ],
        "thread_messages": [
            {
                "gmail_message_id": "older_1",
                "sender": FIXED_TEST_RECIPIENT,
                "subject": "OLDER_SUBJECT_PRIVATE",
                "body_text": "OLDER_BODY_PRIVATE",
                "sent_at": "2026-08-12T00:00:00Z",
            }
        ],
    }
    payload.update(changes)
    return payload


def test_profile_auto_start_is_opt_in_and_persists_explicit_choice(gmail):
    service, _, _, _, _ = gmail
    assert service.get_profile()["auto_start"] is False

    updated = service.configure_profile({
        "project_id": "project_one",
        "workflow_key": "gmail_workflow",
        "required_label": "Workbench-Agent",
        "fixed_recipient": FIXED_TEST_RECIPIENT,
        "instruction": "PROFILE_INSTRUCTION_PRIVATE",
        "default_model": "default-model",
        "enabled": True,
        "auto_start": True,
        "retention_days": 30,
    })

    assert updated["auto_start"] is True
    assert database.get_n8n_gmail_profile()["auto_start"] == 1


def test_opaque_mail_ids_cannot_cross_the_enabled_profile_project(gmail):
    service, generated, _dispatched, _now, db_path = gmail
    database.create_project("project_two", "Other Project", str(db_path.parent / "project-two"))
    queued = service.compose({"instruction": "Private draft", "subject": "Private"})
    with database.get_db_conn() as conn:
        conn.execute(
            "UPDATE n8n_gmail_drafts SET project_id=? WHERE draft_id=?",
            ("project_two", queued["draft_id"]),
        )

    operations = (
        lambda: service.get_draft(queued["draft_id"]),
        lambda: service.get_mail_run(queued["run_id"]),
        lambda: service.generate_draft(queued["draft_id"]),
        lambda: service.edit_draft(
            queued["draft_id"],
            {"expected_revision": 0, "expected_sha256": "0" * 64, "body": "changed"},
        ),
        lambda: service.approve_draft(
            queued["draft_id"],
            {"expected_revision": 0, "expected_sha256": "0" * 64},
        ),
        lambda: service.reject_draft(
            queued["draft_id"],
            {"expected_revision": 0, "expected_sha256": "0" * 64},
        ),
        lambda: service.regenerate_draft(
            queued["draft_id"],
            {"expected_revision": 0, "expected_sha256": "0" * 64},
        ),
        lambda: service.tombstone_draft(queued["draft_id"]),
    )
    for operation in operations:
        with pytest.raises(GmailIntegrationError) as hidden:
            operation()
        assert hidden.value.status_code == 404

    assert service.list_drafts() == []
    assert generated == []


def test_inbound_idempotency_record_cannot_cross_project_scope(gmail):
    service, _generated, _dispatched, _now, _db_path = gmail
    accepted = service.receive_event(inbound())
    with database.get_db_conn() as conn:
        conn.execute(
            "UPDATE n8n_gmail_events SET project_id=? WHERE event_id=?",
            ("different_project", accepted["event_id"]),
        )

    with pytest.raises(GmailIntegrationError) as hidden:
        service.receive_event(inbound())

    assert hidden.value.code == "event_not_found"
    assert hidden.value.status_code == 404


def test_thread_id_cannot_cross_the_enabled_profile_project(gmail):
    service, _generated, _dispatched, _now, _db_path = gmail
    queued = service.compose({"instruction": "Private draft", "subject": "Private"})
    row = database.get_n8n_gmail_draft(queued["draft_id"])
    with database.get_db_conn() as conn:
        conn.execute(
            "UPDATE n8n_gmail_threads SET project_id=? WHERE thread_id=?",
            ("different_project", row["thread_id"]),
        )

    with pytest.raises(GmailIntegrationError) as hidden:
        service.delete_thread(row["thread_id"])

    assert hidden.value.code == "mail_thread_not_found"
    assert hidden.value.status_code == 404


def test_profile_enable_fails_closed_without_local_recipient_configuration(gmail):
    _, _, _, now, _ = gmail
    service = N8nGmailService(
        cipher=AesGcmContentCipher(lambda: b"k" * 32),
        hmac_secret_provider=lambda: b"i" * 32,
        outbound_secret_provider=lambda: b"o" * 32,
        draft_generator=lambda value: value,
        delivery_dispatcher=lambda value: None,
        enable_guard=lambda profile: {"ready": True},
        fixed_recipient=FIXED_TEST_RECIPIENT,
        recipient_configured=False,
        clock=lambda: now,
    )

    assert service.get_profile()["recipient_configured"] is False
    assert service.get_profile()["enabled"] is False
    with pytest.raises(GmailIntegrationError) as rejected:
        service.configure_profile({
            "project_id": "project_one",
            "workflow_key": "gmail_workflow",
            "required_label": "Workbench-Agent",
            "fixed_recipient": FIXED_TEST_RECIPIENT,
            "instruction": "PROFILE_INSTRUCTION_PRIVATE",
            "default_model": "default-model",
            "enabled": True,
            "retention_days": 30,
        })
    assert rejected.value.code == "recipient_not_configured"


def test_inbound_is_idempotent_maps_thread_to_hidden_email_session_and_encrypts_content(gmail):
    service, generated, _, _, db_path = gmail
    first = service.receive_event(inbound())
    duplicate = service.receive_event(inbound())

    assert first["status"] == "queued"
    assert duplicate["idempotent"] is True
    assert duplicate["run_id"] == first["run_id"]
    assert database.get_session(first["session_id"])["mode"] == "email"
    assert first["session_id"] not in {row["id"] for row in database.get_all_sessions()}
    assert first["session_id"] in {row["id"] for row in database.get_all_sessions(include_integration=True)}
    project = next(row for row in database.get_projects() if row["id"] == "project_one")
    assert project["task_count"] == 0

    service.generate_draft(first["draft_id"])
    request = generated[0]
    assert request["workflow_instruction"] == "PROFILE_INSTRUCTION_PRIVATE"
    assert request["body_text"] == "INBOUND_BODY_PRIVATE"
    assert request["thread_messages"][0]["body_text"] == "OLDER_BODY_PRIVATE"
    assert request["trust_boundary"]["body_text"] == "untrusted_email_data"
    detail = service.get_mail_run(first["run_id"])
    assert detail["source"] == {
        "sender": FIXED_TEST_RECIPIENT,
        "subject": "INBOUND_SUBJECT_PRIVATE",
        "received_at": detail["source"]["received_at"],
        "message_id": "message_1",
        "thread_id": "thread_1",
    }
    assert detail["attachments"] == [{
        "attachment_id": "attachment_1", "filename": "private-name.txt",
        "mime_type": "text/plain", "size_bytes": 12,
    }]
    assert detail["skills"] == ["mail-style"]
    assert detail["references"] == ["ref-id"]
    snapshot = service.public_event_snapshot()
    assert snapshot["type"] == "mail_runs_changed"
    assert "INBOUND_BODY_PRIVATE" not in json.dumps(snapshot)
    assert "PROFILE_INSTRUCTION_PRIVATE" not in json.dumps(snapshot)

    database_bytes = db_path.read_bytes()
    for secret in (
        b"PROFILE_INSTRUCTION_PRIVATE",
        b"INBOUND_SUBJECT_PRIVATE",
        b"INBOUND_BODY_PRIVATE",
        b"OLDER_SUBJECT_PRIVATE",
        b"OLDER_BODY_PRIVATE",
        b"Generated private body",
    ):
        assert secret not in database_bytes
    with sqlite3.connect(db_path) as conn:
        public = json.dumps(
            {
                "sessions": conn.execute("SELECT * FROM sessions").fetchall(),
                "messages": conn.execute("SELECT * FROM messages").fetchall(),
                "runs": conn.execute("SELECT * FROM runs").fetchall(),
            },
            default=str,
        )
    assert "INBOUND_BODY_PRIVATE" not in public
    assert "PROFILE_INSTRUCTION_PRIVATE" not in public


def test_inbound_rejects_prompt_field_wrong_sender_binary_metadata_and_oversize_thread(gmail):
    service, *_ = gmail
    with pytest.raises(GmailIntegrationError) as wrong_sender:
        service.receive_event(inbound(sender="attacker@example.com"))
    assert wrong_sender.value.code == "sender_not_allowed"
    with pytest.raises(GmailIntegrationError):
        service.receive_event(inbound(attachments=[{"attachment_id": "a", "filename": "x", "mime_type": "x", "size_bytes": 1, "content": "bad"}]))
    with pytest.raises(GmailIntegrationError) as too_large:
        service.receive_event(inbound(body_text="x" * 100_000, thread_messages=[{
            "gmail_message_id": "old", "sender": FIXED_TEST_RECIPIENT,
            "subject": "", "body_text": "y", "sent_at": "",
        }]))
    assert too_large.value.status_code == 413


def test_edit_reply_locks_subject_and_uses_revision_sha(gmail):
    service, *_ = gmail
    queued = service.receive_event(inbound())
    service.generate_draft(queued["draft_id"])
    draft = service.get_draft(queued["draft_id"])

    with pytest.raises(GmailIntegrationError) as locked:
        service.edit_draft(queued["draft_id"], {
            "expected_revision": draft["revision"], "expected_sha256": draft["content_sha256"],
            "subject": "Changed", "body": "Edited body",
        })
    assert locked.value.code == "reply_subject_locked"
    edited = service.edit_draft(queued["draft_id"], {
        "expected_revision": draft["revision"], "expected_sha256": draft["content_sha256"],
        "body": "Edited body",
    })
    assert edited["revision"] == 2
    with pytest.raises(GmailIntegrationError) as stale:
        service.edit_draft(queued["draft_id"], {
            "expected_revision": draft["revision"], "expected_sha256": draft["content_sha256"],
            "body": "Stale edit",
        })
    assert stale.value.status_code == 409


def test_approve_dispatch_claim_and_result_are_exactly_once(gmail):
    service, _, dispatched, _, _ = gmail
    queued = service.compose({"instruction": "Write a greeting", "subject": "Hello", "model": None})
    service.generate_draft(queued["draft_id"])
    draft = service.get_draft(queued["draft_id"])
    approved = service.approve_draft(queued["draft_id"], {
        "expected_revision": draft["revision"], "expected_sha256": draft["content_sha256"],
    })
    service.dispatch_delivery(approved["delivery_id"])
    assert set(dispatched[0]) == {"delivery_id", "claim_token"}

    claimed = service.claim_delivery(approved["delivery_id"], {
        "claim_id": "claim_1", "claim_token": dispatched[0]["claim_token"],
    })
    assert claimed["recipient"] == FIXED_TEST_RECIPIENT
    assert "result_token" in claimed
    replay = service.claim_delivery(approved["delivery_id"], {
        "claim_id": "claim_1", "claim_token": dispatched[0]["claim_token"],
    })
    assert replay == {
        "delivery_id": approved["delivery_id"], "claim_id": "claim_1",
        "status": "delivery_unknown", "idempotent": True,
    }
    thread_id = database.get_n8n_gmail_draft(queued["draft_id"])["thread_id"]
    with pytest.raises(GmailIntegrationError) as unresolved:
        service.delete_thread(thread_id)
    assert unresolved.value.code == "delivery_unresolved"
    result = {
        "result_id": "result_1", "result_token": claimed["result_token"], "status": "sent",
        "gmail_message_id": "sent_message_1", "gmail_thread_id": "sent_thread_1",
        "error_code": None, "recoverable": None,
    }
    assert service.complete_delivery(approved["delivery_id"], result)["idempotent"] is False
    assert service.complete_delivery(approved["delivery_id"], result)["idempotent"] is True
    with pytest.raises(GmailIntegrationError) as conflict:
        service.complete_delivery(approved["delivery_id"], {**result, "result_id": "result_2"})
    assert conflict.value.status_code == 409
    assert service.delete_thread(thread_id)["status"] == "tombstoned"


def test_disabled_profile_blocks_approval_dispatch_and_first_claim(gmail):
    service, _, dispatched, _, _ = gmail

    awaiting = service.compose({"instruction": "Draft one", "subject": "One"})
    service.generate_draft(awaiting["draft_id"])
    awaiting_draft = service.get_draft(awaiting["draft_id"])
    service.configure_profile({
        "project_id": "project_one", "workflow_key": "gmail_workflow",
        "required_label": "Workbench-Agent", "fixed_recipient": FIXED_TEST_RECIPIENT,
        "instruction": "PROFILE_INSTRUCTION_PRIVATE", "default_model": "default-model",
        "enabled": False, "retention_days": 30,
    })
    with pytest.raises(GmailIntegrationError) as approval:
        service.approve_draft(awaiting["draft_id"], {
            "expected_revision": awaiting_draft["revision"],
            "expected_sha256": awaiting_draft["content_sha256"],
        })
    assert approval.value.code == "profile_disabled"

    service.configure_profile({
        "project_id": "project_one", "workflow_key": "gmail_workflow",
        "required_label": "Workbench-Agent", "fixed_recipient": FIXED_TEST_RECIPIENT,
        "instruction": "PROFILE_INSTRUCTION_PRIVATE", "default_model": "default-model",
        "enabled": True, "retention_days": 30,
    })
    approved = service.approve_draft(awaiting["draft_id"], {
        "expected_revision": awaiting_draft["revision"],
        "expected_sha256": awaiting_draft["content_sha256"],
    })
    delivery = database.get_n8n_gmail_delivery(approved["delivery_id"])
    claim_token = hmac.new(
        b"o" * 32,
        (
            f"claim\n{approved['delivery_id']}\n{delivery['revision']}\n"
            f"{delivery['content_sha256']}"
        ).encode(),
        hashlib.sha256,
    ).hexdigest()

    service.configure_profile({
        "project_id": "project_one", "workflow_key": "gmail_workflow",
        "required_label": "Workbench-Agent", "fixed_recipient": FIXED_TEST_RECIPIENT,
        "instruction": "PROFILE_INSTRUCTION_PRIVATE", "default_model": "default-model",
        "enabled": False, "retention_days": 30,
    })
    with pytest.raises(GmailIntegrationError) as dispatch:
        service.dispatch_delivery(approved["delivery_id"])
    assert dispatch.value.code == "profile_disabled"
    assert dispatched == []
    with pytest.raises(GmailIntegrationError) as claim:
        service.claim_delivery(approved["delivery_id"], {
            "claim_id": "claim_disabled", "claim_token": claim_token,
        })
    assert claim.value.code == "profile_disabled"
    assert database.get_n8n_gmail_delivery(approved["delivery_id"])["status"] == "pending"
    with pytest.raises(GmailIntegrationError) as bound:
        service.assert_project_mutable("project_one")
    assert bound.value.code == "project_bound_to_gmail"


def test_claim_recovery_becomes_unknown_and_requires_manual_regeneration(gmail):
    service, _, dispatched, _, _ = gmail
    queued = service.compose({"instruction": "Draft", "subject": "Review"})
    service.generate_draft(queued["draft_id"])
    draft = service.get_draft(queued["draft_id"])
    approved = service.approve_draft(queued["draft_id"], {
        "expected_revision": draft["revision"],
        "expected_sha256": draft["content_sha256"],
    })
    service.dispatch_delivery(approved["delivery_id"])
    claimed = service.claim_delivery(approved["delivery_id"], {
        "claim_id": "claim_lost", "claim_token": dispatched[0]["claim_token"],
    })
    assert claimed["status"] == "sending"

    assert service.recover_delivery_jobs() == []
    unknown = service.get_draft(queued["draft_id"])
    assert unknown["status"] == "delivery_unknown"
    assert database.get_n8n_gmail_delivery(approved["delivery_id"])["status"] == "delivery_unknown"

    regenerated = service.regenerate_draft(queued["draft_id"], {
        "expected_revision": unknown["revision"],
        "expected_sha256": unknown["content_sha256"],
    })
    assert regenerated["status"] == "queued"
    old_delivery = database.get_n8n_gmail_delivery(approved["delivery_id"])
    assert old_delivery["status"] == "cancelled"
    assert database.get_n8n_gmail_draft(queued["draft_id"])["delivery_id"] is None


def test_reject_regenerate_recovery_and_thread_tombstone(gmail):
    service, *_ = gmail
    queued = service.compose({"instruction": "Draft", "subject": "", "model": None})
    service.generate_draft(queued["draft_id"])
    draft = service.get_draft(queued["draft_id"])
    assert service.reject_draft(queued["draft_id"], {
        "expected_revision": draft["revision"], "expected_sha256": draft["content_sha256"],
    })["status"] == "rejected"
    assert service.regenerate_draft(queued["draft_id"], {
        "expected_revision": draft["revision"], "expected_sha256": draft["content_sha256"],
    })["status"] == "queued"
    assert queued["draft_id"] in service.recover_generation_jobs()
    service.generate_draft(queued["draft_id"])
    row = database.get_n8n_gmail_draft(queued["draft_id"])
    assert service.delete_thread(row["thread_id"])["status"] == "tombstoned"
    tombstone = database.get_n8n_gmail_draft(queued["draft_id"])
    assert tombstone["body_ciphertext"] is None
    assert tombstone["input_ciphertext"] is None


def test_retention_erases_terminal_content_but_keeps_tombstone(gmail):
    service, *_ = gmail
    queued = service.compose({"instruction": "Old draft", "subject": "", "model": None})
    service.generate_draft(queued["draft_id"])
    draft = service.get_draft(queued["draft_id"])
    service.reject_draft(queued["draft_id"], {
        "expected_revision": draft["revision"], "expected_sha256": draft["content_sha256"],
    })
    service.configure_profile({
        "project_id": "project_one", "workflow_key": "gmail_workflow",
        "required_label": "Workbench-Agent", "fixed_recipient": FIXED_TEST_RECIPIENT,
        "instruction": "PROFILE_INSTRUCTION_PRIVATE", "default_model": "default-model",
        "enabled": False, "retention_days": 30,
    })
    with database.get_db_conn() as conn:
        conn.execute(
            "UPDATE n8n_gmail_drafts SET updated_at = ? WHERE draft_id = ?",
            ("2020-01-01T00:00:00+00:00", queued["draft_id"]),
        )
    assert service.purge_retention()["tombstoned_drafts"] == 1
    retained = database.get_n8n_gmail_draft(queued["draft_id"])
    assert retained["status"] == "tombstoned"
    assert retained["subject_ciphertext"] is None
    assert retained["body_ciphertext"] is None
    assert retained["draft_id"] == queued["draft_id"]


def test_retention_includes_expired_approvals(gmail):
    service, *_ = gmail
    queued = service.compose({"instruction": "Old approval", "subject": ""})
    service.generate_draft(queued["draft_id"])
    draft = service.get_draft(queued["draft_id"])
    approved = service.approve_draft(queued["draft_id"], {
        "expected_revision": draft["revision"],
        "expected_sha256": draft["content_sha256"],
    })
    with database.get_db_conn() as conn:
        conn.execute(
            "UPDATE n8n_gmail_deliveries SET expires_at = ? WHERE delivery_id = ?",
            ("2020-01-01T00:00:00+00:00", approved["delivery_id"]),
        )
    database.expire_n8n_gmail_deliveries(now="2020-01-02T00:00:00+00:00")
    with database.get_db_conn() as conn:
        conn.execute(
            "UPDATE n8n_gmail_drafts SET updated_at = ? WHERE draft_id = ?",
            ("2020-01-01T00:00:00+00:00", queued["draft_id"]),
        )
    assert service.purge_retention()["tombstoned_drafts"] == 1
    assert database.get_n8n_gmail_draft(queued["draft_id"])["status"] == "tombstoned"


def test_hmac_raw_body_nonce_and_expiry(gmail):
    service, _, _, now, _ = gmail
    body = b'{"x":1}'
    timestamp = int(now.timestamp())
    nonce = "abcdefghijklmnop"
    path = "/api/integrations/n8n/v1/gmail/events"
    canonical = f"POST\n{path}\n{timestamp}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode()
    signature = hmac.new(b"i" * 32, canonical, hashlib.sha256).hexdigest()
    headers = {
        "X-N8N-Profile": "gmail", "X-N8N-Timestamp": str(timestamp),
        "X-N8N-Nonce": nonce, "X-N8N-Signature": f"sha256={signature}",
    }
    service.authenticate_request(method="POST", path=path, headers=headers, body=body)
    with database.get_db_conn() as conn:
        nonce_row = conn.execute(
            "SELECT method, path, request_timestamp, request_sha256 FROM n8n_gmail_nonces WHERE nonce = ?",
            (nonce,),
        ).fetchone()
    assert dict(nonce_row) == {
        "method": "POST", "path": path, "request_timestamp": timestamp,
        "request_sha256": hashlib.sha256(body).hexdigest(),
    }
    with pytest.raises(GmailIntegrationError) as replay:
        service.authenticate_request(method="POST", path=path, headers=headers, body=body)
    assert replay.value.code == "replay_detected"
    stale_headers = dict(headers, **{"X-N8N-Nonce": "qrstuvwxyzABCDEF", "X-N8N-Timestamp": str(int((now - timedelta(hours=1)).timestamp()))})
    with pytest.raises(GmailIntegrationError) as stale:
        service.authenticate_request(method="POST", path=path, headers=stale_headers, body=body)
    assert stale.value.code == "request_expired"


def test_approval_expires_after_24_hours(gmail):
    service, _, dispatched, _, _ = gmail
    queued = service.compose({"instruction": "Draft", "subject": "", "model": None})
    service.generate_draft(queued["draft_id"])
    draft = service.get_draft(queued["draft_id"])
    approved = service.approve_draft(queued["draft_id"], {
        "expected_revision": draft["revision"], "expected_sha256": draft["content_sha256"],
    })
    service.dispatch_delivery(approved["delivery_id"])
    with database.get_db_conn() as conn:
        conn.execute(
            "UPDATE n8n_gmail_deliveries SET expires_at = ? WHERE delivery_id = ?",
            ("2026-08-13T02:59:59+00:00", approved["delivery_id"]),
        )
    with pytest.raises(GmailIntegrationError) as expired:
        service.claim_delivery(approved["delivery_id"], {
            "claim_id": "claim_expired", "claim_token": dispatched[0]["claim_token"],
        })
    assert expired.value.code == "approval_expired"
    assert database.get_n8n_gmail_draft(queued["draft_id"])["status"] == "approval_expired"


def test_enable_guard_failure_keeps_profile_disabled(gmail):
    service, _, _, now, _ = gmail
    assert service.get_profile()["enabled"] is True
    blocked = N8nGmailService(
        cipher=AesGcmContentCipher(lambda: b"k" * 32),
        hmac_secret_provider=lambda: b"i" * 32,
        outbound_secret_provider=lambda: b"o" * 32,
        draft_generator=lambda value: value,
        delivery_dispatcher=lambda value: None,
        enable_guard=lambda profile: {"ready": False, "code": "isolation_not_ready"},
        clock=lambda: now,
    )
    with pytest.raises(GmailIntegrationError) as rejected:
        blocked.configure_profile({
            "project_id": "project_one", "workflow_key": "gmail_workflow",
            "required_label": "Workbench-Agent", "fixed_recipient": FIXED_TEST_RECIPIENT,
            "instruction": "PROFILE_INSTRUCTION_PRIVATE", "default_model": None,
            "enabled": True, "retention_days": 30,
        })
    assert rejected.value.code == "isolation_not_ready"
    assert blocked.get_profile()["enabled"] is False


def test_permission_gate_covers_read_draft_generation_and_send_recheck(gmail):
    service, generated, dispatched, _, _ = gmail
    calls = []

    def guarded(project_id, capability, *, resource_type=None, resource_id=None):
        calls.append((project_id, capability, resource_type, resource_id))
        if capability == "message.send":
            return {"decision": "require_approval"}
        return {"decision": "allow"}

    service._permission_check = guarded
    queued = service.compose({"instruction": "Draft", "subject": "Hello", "model": None})
    service.generate_draft(queued["draft_id"])
    draft = service.get_draft(queued["draft_id"])
    approved = service.approve_draft(
        queued["draft_id"],
        {
            "expected_revision": draft["revision"],
            "expected_sha256": draft["content_sha256"],
        },
    )
    assert approved["status"] == "approved_queued"
    assert any(call[1] == "draft.create" for call in calls)
    assert any(call[1] == "message.read" for call in calls)
    assert any(call[1] == "message.send" for call in calls)

    service._permission_check = lambda *_args, **_kwargs: {"decision": "deny"}
    with pytest.raises(GmailIntegrationError) as denied:
        service.dispatch_delivery(approved["delivery_id"])
    assert denied.value.code == "gmail_permission_denied"
    assert dispatched == []
    assert len(generated) == 1


def test_background_generation_rechecks_policy_before_model_call(gmail):
    service, generated, _, _, _ = gmail
    queued = service.compose({"instruction": "Draft", "subject": "Hello", "model": None})
    service._permission_check = lambda *_args, **_kwargs: {"decision": "deny"}
    with pytest.raises(GmailIntegrationError) as denied:
        service.generate_draft(queued["draft_id"])
    assert denied.value.code == "gmail_permission_denied"
    assert generated == []
    assert database.get_n8n_gmail_draft(queued["draft_id"])["status"] == "generation_failed"
