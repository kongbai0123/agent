"""Application service for the narrow n8n <-> Workbench Gmail bridge.

Only IDs, digests and provenance metadata are written to Workbench runs.  Email
content and workflow instructions live exclusively in AES-GCM encrypted private
integration tables.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import database
from n8n_gmail_crypto import AesGcmContentCipher, GmailCryptoError


PROFILE_ID = "gmail"
DEFAULT_LABEL = "Workbench-Agent"
FIXED_TEST_RECIPIENT = "workbench-canary@example.test"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_MAX_SUBJECT = 998
_MAX_BODY = 500_000
_MAX_INSTRUCTION = 20_000
_MAX_EMAIL_CONTEXT = 100_000
_MAX_ATTACHMENTS = 50


class GmailIntegrationError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400, recoverable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.recoverable = recoverable


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _safe_id(value: Any, field: str) -> str:
    text = str(value or "")
    if not _ID_RE.fullmatch(text):
        raise GmailIntegrationError("invalid_request", f"{field} is invalid.")
    return text


def _text(value: Any, field: str, maximum: int, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise GmailIntegrationError("invalid_request", f"{field} must be text.")
    if not allow_empty and not value.strip():
        raise GmailIntegrationError("invalid_request", f"{field} is required.")
    if len(value) > maximum:
        raise GmailIntegrationError("invalid_request", f"{field} is too large.", status_code=413)
    return value


class N8nGmailService:
    """State machine and security boundary for one fixed-project Gmail profile."""

    def __init__(
        self,
        *,
        cipher: AesGcmContentCipher,
        hmac_secret_provider: Callable[[], bytes],
        outbound_secret_provider: Callable[[], bytes],
        draft_generator: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        delivery_dispatcher: Callable[[Mapping[str, str]], Any],
        enable_guard: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        fixed_recipient: str = FIXED_TEST_RECIPIENT,
        recipient_configured: bool = True,
        clock: Callable[[], datetime] = _utcnow,
        id_factory: Callable[[str], str] = lambda prefix: f"{prefix}_{secrets.token_urlsafe(18)}",
        max_clock_skew_seconds: int = 300,
    ) -> None:
        self.cipher = cipher
        self._hmac_secret_provider = hmac_secret_provider
        self._outbound_secret_provider = outbound_secret_provider
        self._draft_generator = draft_generator
        self._delivery_dispatcher = delivery_dispatcher
        self._enable_guard = enable_guard
        self.fixed_recipient = _text(
            fixed_recipient, "fixed_recipient", 320, allow_empty=False
        ).lower()
        if not _EMAIL_RE.fullmatch(self.fixed_recipient):
            raise GmailIntegrationError("invalid_request", "fixed_recipient is invalid.")
        self.recipient_configured = bool(recipient_configured)
        self._clock = clock
        self._id_factory = id_factory
        self._max_clock_skew = max(30, min(int(max_clock_skew_seconds), 900))

    def _hmac_secret(self) -> bytes:
        try:
            value = bytes(self._hmac_secret_provider())
        except Exception as exc:
            raise GmailIntegrationError(
                "integration_secret_unavailable", "The n8n signing secret is unavailable.", status_code=503
            ) from exc
        if len(value) < 32:
            raise GmailIntegrationError(
                "integration_secret_invalid", "The n8n signing secret must contain at least 32 bytes.", status_code=503
            )
        return value

    def _outbound_secret(self) -> bytes:
        try:
            value = bytes(self._outbound_secret_provider())
        except Exception as exc:
            raise GmailIntegrationError(
                "outbound_secret_unavailable", "The outbound n8n secret is unavailable.", status_code=503
            ) from exc
        if len(value) < 32:
            raise GmailIntegrationError(
                "outbound_secret_invalid", "The outbound n8n secret must contain at least 32 bytes.", status_code=503
            )
        return value

    def get_profile(self) -> Dict[str, Any]:
        row = database.get_n8n_gmail_profile()
        if not row:
            return {
                "profile_id": PROFILE_ID,
                "configured": False,
                "enabled": False,
                "auto_start": False,
                "project_id": None,
                "workflow_key": None,
                "required_label": DEFAULT_LABEL,
                "fixed_recipient": self.fixed_recipient,
                "recipient_configured": self.recipient_configured,
                "instruction": "",
                "default_model": None,
                "retention_days": 30,
                "crypto_ready": self.cipher.available,
                "isolation_ready": False,
            }
        instruction = ""
        crypto_ready = self.cipher.available
        if row.get("instruction_ciphertext") and crypto_ready:
            try:
                instruction = self.cipher.decrypt_text(
                    row["instruction_ciphertext"], aad="gmail-profile-instruction:gmail"
                )
            except GmailCryptoError:
                crypto_ready = False
        isolation_ready = False
        if crypto_ready and self._enable_guard is not None:
            try:
                outcome = self._enable_guard(dict(row))
                isolation_ready = bool(
                    outcome if not isinstance(outcome, Mapping) else outcome.get("ready")
                )
            except Exception:
                isolation_ready = False
        recipient_matches_profile = (
            self.recipient_configured
            and str(row.get("fixed_recipient") or "").casefold()
            == self.fixed_recipient.casefold()
        )
        return {
            "profile_id": PROFILE_ID,
            "configured": True,
            "enabled": bool(row["enabled"]) and crypto_ready and isolation_ready and recipient_matches_profile,
            "stored_enabled": bool(row["enabled"]),
            "auto_start": bool(row.get("auto_start", 0)),
            "project_id": row["project_id"],
            "workflow_key": row["workflow_key"],
            "required_label": row["required_label"],
            "fixed_recipient": self.fixed_recipient,
            "recipient_configured": recipient_matches_profile,
            "instruction": instruction,
            "default_model": row.get("default_model"),
            "retention_days": int(row["retention_days"]),
            "crypto_ready": crypto_ready,
            "isolation_ready": isolation_ready,
            "updated_at": row["updated_at"],
        }

    def configure_profile(self, settings: Mapping[str, Any]) -> Dict[str, Any]:
        project_id = _safe_id(settings.get("project_id"), "project_id")
        current = database.get_n8n_gmail_profile() or {}
        editable_fields = {
            "project_id", "instruction", "default_model", "enabled", "auto_start",
            "workflow_key", "required_label", "fixed_recipient", "retention_days",
        }
        if set(settings) - editable_fields:
            raise GmailIntegrationError("invalid_request", "The mail profile contains unknown fields.")
        workflow_key = _safe_id(
            settings.get("workflow_key") or current.get("workflow_key") or "workbench_gmail_v1",
            "workflow_key",
        )
        required_label = _text(settings.get("required_label", DEFAULT_LABEL), "required_label", 128, allow_empty=False)
        fixed_recipient = _text(
            settings.get("fixed_recipient", self.fixed_recipient),
            "fixed_recipient",
            320,
            allow_empty=False,
        ).lower()
        instruction = _text(settings.get("instruction", ""), "instruction", _MAX_INSTRUCTION, allow_empty=False)
        default_model_value = settings.get("default_model")
        default_model = None if default_model_value in (None, "") else _text(
            default_model_value, "default_model", 255, allow_empty=False
        )
        if fixed_recipient.casefold() != self.fixed_recipient.casefold():
            raise GmailIntegrationError(
                "recipient_locked", "The v1 recipient is locked to the configured test address.", status_code=409
            )
        if not _EMAIL_RE.fullmatch(fixed_recipient):
            raise GmailIntegrationError("invalid_request", "fixed_recipient is invalid.")
        retention_days = int(settings.get("retention_days", current.get("retention_days", 30)))
        if not 1 <= retention_days <= 3650:
            raise GmailIntegrationError("invalid_request", "retention_days must be between 1 and 3650.")
        enabled = bool(settings.get("enabled", False))
        auto_start = bool(settings.get("auto_start", False))
        if enabled and not self.recipient_configured:
            raise GmailIntegrationError(
                "recipient_not_configured",
                "Configure the fixed Gmail recipient in the local Workbench environment before enabling the profile.",
                status_code=409,
            )
        # Remove the previous authorization before encryption/readiness work.
        # This also guarantees that disabling still succeeds safely if the key
        # provider becomes unavailable during the remaining update.
        database.disable_n8n_gmail_profile()
        project = database.get_project(project_id)
        if not project:
            raise GmailIntegrationError("project_not_found", "The fixed Workbench project does not exist.", status_code=404)
        proposed = {
            "profile_id": PROFILE_ID, "project_id": project_id, "workflow_key": workflow_key,
            "required_label": required_label, "fixed_recipient": fixed_recipient,
            "retention_days": retention_days, "enabled": enabled,
            "auto_start": auto_start,
            "instruction_ciphertext": self.cipher.encrypt_text(
                instruction, aad="gmail-profile-instruction:gmail"
            ),
            "default_model": default_model,
        }
        if enabled:
            if self._enable_guard is None:
                raise GmailIntegrationError(
                    "enable_guard_missing", "The n8n isolation readiness guard is unavailable.", status_code=503
                )
            try:
                self.cipher.assert_available()
                self._hmac_secret()
                self._outbound_secret()
                outcome = self._enable_guard(proposed)
            except GmailIntegrationError:
                raise
            except GmailCryptoError as exc:
                raise GmailIntegrationError("crypto_unavailable", str(exc), status_code=503) from exc
            except Exception as exc:
                raise GmailIntegrationError("enable_guard_failed", "Gmail isolation readiness check failed.", status_code=503) from exc
            if outcome is False:
                raise GmailIntegrationError("isolation_not_ready", "The isolated n8n runtime is not ready.", status_code=409)
            if isinstance(outcome, Mapping) and not bool(outcome.get("ready")):
                raise GmailIntegrationError(
                    str(outcome.get("code") or "isolation_not_ready"),
                    str(outcome.get("message") or "The isolated n8n runtime is not ready."),
                    status_code=int(outcome.get("status_code") or 409),
                )
        try:
            database.upsert_n8n_gmail_profile(
                project_id=project_id,
                workflow_key=workflow_key,
                required_label=required_label,
                fixed_recipient=fixed_recipient,
                instruction_ciphertext=proposed["instruction_ciphertext"],
                default_model=default_model,
                enabled=enabled,
                auto_start=auto_start,
                retention_days=retention_days,
            )
        except ValueError as exc:
            raise GmailIntegrationError("profile_binding_conflict", str(exc), status_code=409) from exc
        return self.get_profile()

    def project_is_bound(self, project_id: str) -> bool:
        return database.n8n_gmail_project_binding(project_id) is not None

    def assert_project_mutable(self, project_id: str) -> None:
        if self.project_is_bound(project_id):
            raise GmailIntegrationError(
                "project_bound_to_gmail",
                "Rebind the disabled Gmail profile before moving or deleting this project.",
                status_code=409,
            )

    def authenticate_request(self, *, method: str, path: str, headers: Mapping[str, str], body: bytes) -> None:
        timestamp_raw = headers.get("x-n8n-timestamp") or headers.get("X-N8N-Timestamp")
        nonce = headers.get("x-n8n-nonce") or headers.get("X-N8N-Nonce") or ""
        signature = headers.get("x-n8n-signature") or headers.get("X-N8N-Signature") or ""
        profile = headers.get("x-n8n-profile") or headers.get("X-N8N-Profile") or ""
        if profile != PROFILE_ID or not timestamp_raw or not _NONCE_RE.fullmatch(nonce):
            raise GmailIntegrationError("authentication_failed", "Invalid n8n authentication headers.", status_code=401)
        try:
            timestamp = int(timestamp_raw)
        except (TypeError, ValueError) as exc:
            raise GmailIntegrationError("authentication_failed", "Invalid n8n timestamp.", status_code=401) from exc
        now = self._clock()
        request_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        if abs((now - request_time).total_seconds()) > self._max_clock_skew:
            raise GmailIntegrationError("request_expired", "The n8n request timestamp is outside the allowed window.", status_code=401)
        body_sha = hashlib.sha256(body).hexdigest()
        canonical = f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_sha}".encode("utf-8")
        expected = hmac.new(self._hmac_secret(), canonical, hashlib.sha256).hexdigest()
        supplied = signature.removeprefix("sha256=")
        if len(supplied) != 64 or not hmac.compare_digest(expected, supplied.casefold()):
            raise GmailIntegrationError("authentication_failed", "Invalid n8n request signature.", status_code=401)
        expires_at = _iso(request_time + timedelta(seconds=self._max_clock_skew))
        if not database.reserve_n8n_gmail_nonce(
            PROFILE_ID, nonce, body_sha, method=method, path=path,
            request_timestamp=timestamp, expires_at=expires_at, created_at=_iso(now)
        ):
            raise GmailIntegrationError("replay_detected", "This n8n nonce was already used.", status_code=409)

    def _profile_ready(self) -> Dict[str, Any]:
        profile = database.get_n8n_gmail_profile()
        if not profile or not bool(profile["enabled"]):
            raise GmailIntegrationError("profile_disabled", "The Gmail profile is disabled.", status_code=503)
        try:
            self.cipher.assert_available()
            self._hmac_secret()
            self._outbound_secret()
        except GmailCryptoError as exc:
            raise GmailIntegrationError("crypto_unavailable", str(exc), status_code=503) from exc
        if self._enable_guard is None:
            raise GmailIntegrationError("enable_guard_missing", "The n8n readiness guard is unavailable.", status_code=503)
        try:
            outcome = self._enable_guard(dict(profile))
        except Exception as exc:
            raise GmailIntegrationError("isolation_not_ready", "The n8n isolation check failed.", status_code=503) from exc
        ready = bool(outcome if not isinstance(outcome, Mapping) else outcome.get("ready"))
        if not ready:
            raise GmailIntegrationError("isolation_not_ready", "The isolated n8n runtime is not ready.", status_code=503)
        return profile

    @staticmethod
    def _validate_attachments(value: Any) -> List[Dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > _MAX_ATTACHMENTS:
            raise GmailIntegrationError("invalid_request", "attachments must be a metadata-only list.")
        allowed = {"attachment_id", "filename", "mime_type", "size_bytes"}
        output = []
        for item in value:
            if not isinstance(item, Mapping) or set(item) - allowed:
                raise GmailIntegrationError("invalid_request", "Attachment content and unknown fields are forbidden.")
            if any(key in item for key in ("content", "data", "path", "url")):
                raise GmailIntegrationError("invalid_request", "Attachment binaries and locations are forbidden.")
            size = int(item.get("size_bytes") or 0)
            if size < 0:
                raise GmailIntegrationError("invalid_request", "Attachment size is invalid.")
            output.append({
                "attachment_id": _safe_id(item.get("attachment_id"), "attachment_id"),
                "filename": _text(item.get("filename", ""), "filename", 512),
                "mime_type": _text(item.get("mime_type", "application/octet-stream"), "mime_type", 255),
                "size_bytes": size,
            })
        return output

    @staticmethod
    def _validate_thread_messages(value: Any, *, current_body: str) -> List[Dict[str, Any]]:
        if value is None:
            value = []
        if not isinstance(value, list) or len(value) > 20:
            raise GmailIntegrationError("invalid_request", "thread_messages must contain at most 20 items.")
        allowed = {"gmail_message_id", "sender", "subject", "body_text", "sent_at"}
        output: List[Dict[str, Any]] = []
        total = len(current_body)
        for item in value:
            if not isinstance(item, Mapping) or set(item) - allowed:
                raise GmailIntegrationError("invalid_request", "thread_messages contains unknown fields.")
            body = _text(item.get("body_text", ""), "thread message body_text", _MAX_EMAIL_CONTEXT)
            total += len(body)
            if total > _MAX_EMAIL_CONTEXT:
                raise GmailIntegrationError(
                    "email_context_too_large", "The email and thread context exceeds 100,000 characters.", status_code=413
                )
            output.append({
                "gmail_message_id": _safe_id(item.get("gmail_message_id"), "thread gmail_message_id"),
                "sender": _text(item.get("sender", ""), "thread sender", 320),
                "subject": _text(item.get("subject", ""), "thread subject", _MAX_SUBJECT),
                "body_text": body,
                "sent_at": _text(item.get("sent_at", ""), "thread sent_at", 64),
            })
        return output

    def _new_thread(self, project_id: str, *, gmail_thread_id: Optional[str], title: str) -> Dict[str, Any]:
        if gmail_thread_id:
            existing = database.get_n8n_gmail_thread(gmail_thread_id=gmail_thread_id)
            if existing:
                session = database.get_session(existing["session_id"])
                if (
                    existing["project_id"] != project_id or existing["tombstoned_at"]
                    or not session or session.get("project_id") != project_id or session.get("mode") != "email"
                ):
                    raise GmailIntegrationError("thread_scope_conflict", "The Gmail thread binding is invalid.", status_code=409)
                return existing
        session_id = self._id_factory("email_session")
        thread_id = self._id_factory("email_thread")
        # Never copy an email subject into the public sessions table.
        safe_title = "Gmail thread" if gmail_thread_id else "New email"
        database.create_session(session_id, title=safe_title, mode="email", project_id=project_id)
        return database.create_n8n_gmail_thread(
            thread_id=thread_id, project_id=project_id, session_id=session_id,
            gmail_thread_id=gmail_thread_id,
        )

    def receive_event(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        allowed_event_fields = {
            "event_id", "workflow_key", "gmail_message_id", "gmail_thread_id",
            "sender", "subject", "body_text", "labels", "attachments", "thread_messages",
        }
        if set(payload) - allowed_event_fields:
            raise GmailIntegrationError("invalid_request", "The Gmail event contains unknown fields.")
        profile = self._profile_ready()
        workflow_key = _safe_id(payload.get("workflow_key"), "workflow_key")
        if workflow_key != profile["workflow_key"]:
            raise GmailIntegrationError("workflow_not_allowed", "This n8n workflow is not allowlisted.", status_code=403)
        labels = payload.get("labels")
        if not isinstance(labels, list) or not all(isinstance(item, str) for item in labels):
            raise GmailIntegrationError("invalid_request", "labels must be a list of strings.")
        label_set = {item.casefold() for item in labels}
        if profile["required_label"].casefold() not in label_set or "inbox" not in label_set or "sent" in label_set:
            raise GmailIntegrationError("message_not_eligible", "The Gmail message does not satisfy the inbound label policy.", status_code=403)
        event_id = _safe_id(payload.get("event_id"), "event_id")
        message_id = _safe_id(payload.get("gmail_message_id"), "gmail_message_id")
        gmail_thread_id = _safe_id(payload.get("gmail_thread_id"), "gmail_thread_id")
        subject = _text(payload.get("subject", ""), "subject", _MAX_SUBJECT)
        body_text = _text(payload.get("body_text", ""), "body_text", _MAX_EMAIL_CONTEXT)
        sender = _text(payload.get("sender", ""), "sender", 320)
        if sender.casefold() != profile["fixed_recipient"].casefold():
            raise GmailIntegrationError(
                "sender_not_allowed", "Inbound v1 accepts only the fixed test mailbox.", status_code=403
            )
        attachments = self._validate_attachments(payload.get("attachments"))
        thread_messages = self._validate_thread_messages(payload.get("thread_messages"), current_body=body_text)
        normalized = {
            "event_id": event_id, "gmail_message_id": message_id,
            "gmail_thread_id": gmail_thread_id, "workflow_key": workflow_key,
            "subject": subject,
            "body_text": body_text, "sender": sender, "labels": sorted(labels),
            "attachments": attachments, "thread_messages": thread_messages,
        }
        request_sha = _sha(normalized)
        duplicate = database.find_n8n_gmail_event(
            event_id=event_id, gmail_message_id=message_id
        )
        if duplicate:
            if duplicate["request_sha256"] != request_sha:
                raise GmailIntegrationError(
                    "idempotency_conflict",
                    "The event ID or Gmail message ID was reused with different content.",
                    status_code=409,
                )
            return {
                "accepted": True, "idempotent": True, "event_id": duplicate["event_id"],
                "draft_id": duplicate["draft_id"], "run_id": duplicate["run_id"],
                "session_id": duplicate["session_id"], "status": duplicate["state"],
            }
        thread = self._new_thread(profile["project_id"], gmail_thread_id=gmail_thread_id, title=subject or "Email reply")
        run_id = self._id_factory("email_run")
        draft_id = self._id_factory("email_draft")
        encrypted = self.cipher.encrypt_text(
            json.dumps(normalized, ensure_ascii=False), aad=f"gmail-event:{event_id}"
        )
        created, event = database.create_n8n_gmail_event({
            "event_id": event_id, "project_id": profile["project_id"],
            "gmail_message_id": message_id, "gmail_thread_id": gmail_thread_id,
            "thread_id": thread["thread_id"], "session_id": thread["session_id"],
            "run_id": run_id, "request_sha256": request_sha,
            "payload_ciphertext": encrypted, "state": "received",
        })
        if not created:
            if event["request_sha256"] != request_sha:
                raise GmailIntegrationError("idempotency_conflict", "The event ID or Gmail message ID was reused with different content.", status_code=409)
            return {
                "accepted": True, "idempotent": True, "event_id": event["event_id"],
                "draft_id": event["draft_id"], "run_id": event["run_id"],
                "session_id": event["session_id"], "status": event["state"],
            }
        database.upsert_run(
            run_id, thread["session_id"], event_id, None, "email", "queued",
            project_id=profile["project_id"], input_manifest={
                "version": 1, "reproducible": False, "reason": "encrypted_integration_input",
                "gmail_event_id": event_id, "gmail_message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(),
                "input_sha256": request_sha, "attachment_ids": [item["attachment_id"] for item in attachments],
            },
        )
        database.create_n8n_gmail_draft({
            "draft_id": draft_id, "project_id": profile["project_id"],
            "thread_id": thread["thread_id"], "session_id": thread["session_id"],
            "run_id": run_id, "event_id": event_id, "kind": "reply",
            "gmail_message_id": message_id, "gmail_thread_id": gmail_thread_id,
            "content_sha256": hashlib.sha256(b"").hexdigest(), "status": "queued", "revision": 0,
        })
        database.update_n8n_gmail_event(event_id, draft_id=draft_id)
        database.update_n8n_gmail_event(event_id, state="queued")
        return {
            "accepted": True, "idempotent": False, "event_id": event_id,
            "draft_id": draft_id, "run_id": run_id, "session_id": thread["session_id"],
            "status": "queued",
        }

    def compose(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if set(payload) - {"instruction", "model", "subject"}:
            raise GmailIntegrationError("invalid_request", "The compose request contains unknown fields.")
        profile = self._profile_ready()
        instruction = _text(payload.get("instruction", ""), "instruction", _MAX_INSTRUCTION, allow_empty=False)
        subject = _text(payload.get("subject", ""), "subject", _MAX_SUBJECT)
        model_value = payload.get("model")
        requested_model = profile.get("default_model") if model_value in (None, "") else _text(
            model_value, "model", 255, allow_empty=False
        )
        thread = self._new_thread(profile["project_id"], gmail_thread_id=None, title=subject or "New email")
        run_id = self._id_factory("email_run")
        draft_id = self._id_factory("email_draft")
        generation_input = {
            "compose_instruction": instruction, "requested_model": requested_model,
            "subject": subject, "body_text": "",
            "sender": "", "attachments": [], "gmail_message_id": None, "gmail_thread_id": None,
            "thread_messages": [],
        }
        digest = _sha(generation_input)
        encrypted = self.cipher.encrypt_text(
            json.dumps(generation_input, ensure_ascii=False), aad=f"gmail-draft-input:{draft_id}"
        )
        database.upsert_run(
            run_id, thread["session_id"], draft_id, None, "email", "queued",
            project_id=profile["project_id"], input_manifest={
                "version": 1, "reproducible": False, "reason": "encrypted_integration_input",
                "input_sha256": digest, "attachment_ids": [],
            },
        )
        database.create_n8n_gmail_draft({
            "draft_id": draft_id, "project_id": profile["project_id"],
            "thread_id": thread["thread_id"], "session_id": thread["session_id"],
            "run_id": run_id, "kind": "compose", "input_ciphertext": encrypted,
            "content_sha256": hashlib.sha256(b"").hexdigest(), "status": "queued", "revision": 0,
        })
        return {"draft_id": draft_id, "run_id": run_id, "session_id": thread["session_id"], "status": "queued"}

    def _generation_input(self, draft: Mapping[str, Any]) -> Dict[str, Any]:
        if draft.get("event_id"):
            with database.get_db_conn() as conn:
                event = conn.execute(
                    "SELECT payload_ciphertext FROM n8n_gmail_events WHERE event_id = ?",
                    (draft["event_id"],),
                ).fetchone()
            if not event or not event["payload_ciphertext"]:
                raise GmailIntegrationError("input_unavailable", "Encrypted Gmail input is unavailable.", status_code=410)
            raw = self.cipher.decrypt_text(event["payload_ciphertext"], aad=f"gmail-event:{draft['event_id']}")
        else:
            raw = self.cipher.decrypt_text(
                draft["input_ciphertext"], aad=f"gmail-draft-input:{draft['draft_id']}"
            )
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise GmailIntegrationError("input_invalid", "Encrypted Gmail input is invalid.", status_code=500)
        return value

    def generate_draft(self, draft_id: str) -> Dict[str, Any]:
        draft_id = _safe_id(draft_id, "draft_id")
        if not database.claim_n8n_gmail_generation(draft_id):
            current = database.get_n8n_gmail_draft(draft_id)
            if not current:
                raise GmailIntegrationError("draft_not_found", "Draft not found.", status_code=404)
            return {"draft_id": draft_id, "status": current["status"], "claimed": False}
        draft = database.get_n8n_gmail_draft(draft_id)
        try:
            source = self._generation_input(draft)
            profile = self._profile_ready()
            profile_instruction = self.cipher.decrypt_text(
                profile["instruction_ciphertext"], aad="gmail-profile-instruction:gmail"
            )
            compose_instruction = str(source.get("compose_instruction") or "")
            trusted_instruction = profile_instruction
            if draft["kind"] == "compose" and compose_instruction:
                trusted_instruction += "\n\nWORKBENCH COMPOSE REQUEST:\n" + compose_instruction
            request = {
                "mode": draft["kind"], "run_id": draft["run_id"],
                "session_id": draft["session_id"], "project_id": draft["project_id"],
                "model": source.get("requested_model") or profile.get("default_model"),
                "workflow_instruction": trusted_instruction,
                "subject": source.get("subject", ""), "body_text": source.get("body_text", ""),
                "thread_messages": source.get("thread_messages", []),
                "attachments": source.get("attachments", []),
                "sender": source.get("sender", ""), "recipient": profile["fixed_recipient"],
                "gmail_message_id": draft.get("gmail_message_id"),
                "gmail_thread_id": draft.get("gmail_thread_id"),
                "trust_boundary": {
                    "workflow_instruction": "trusted_profile_configuration",
                    "subject": "untrusted_email_data", "body_text": "untrusted_email_data",
                    "sender": "untrusted_email_data", "attachments": "untrusted_metadata",
                },
            }
            generated = self._draft_generator(request)
            if not isinstance(generated, Mapping):
                raise ValueError("draft generator must return a mapping")
            allowed = {
                "subject", "body_text", "summary", "intent", "tone", "needs_human_attention",
                "warnings", "model", "provider", "skills", "references",
                "context_truncated",
            }
            if set(generated) - allowed:
                raise ValueError("draft generator returned unknown fields")
            subject = _text(generated.get("subject"), "generated subject", _MAX_SUBJECT)
            body_text = _text(generated.get("body_text"), "generated body_text", _MAX_BODY)
            if draft["kind"] == "reply" and subject != str(source.get("subject") or ""):
                raise ValueError("reply subject must remain unchanged")
            recipient = profile["fixed_recipient"]
            content_sha = _sha({"recipient": recipient, "subject": subject, "body_text": body_text})
            meta = {key: generated[key] for key in generated if key not in ("subject", "body_text")}
            for key in ("summary", "intent", "tone", "model", "provider"):
                if key in meta and meta[key] is not None and not isinstance(meta[key], str):
                    raise ValueError(f"{key} must be text")
            for key in ("warnings", "skills", "references"):
                if key in meta and not isinstance(meta[key], list):
                    raise ValueError(f"{key} must be a list")
            if "needs_human_attention" in meta and not isinstance(meta["needs_human_attention"], bool):
                raise ValueError("needs_human_attention must be boolean")
            if "context_truncated" in meta and not isinstance(meta["context_truncated"], bool):
                raise ValueError("context_truncated must be boolean")
            complete = database.complete_n8n_gmail_generation(
                draft_id,
                recipient_ciphertext=self.cipher.encrypt_text(recipient, aad=f"gmail-draft-recipient:{draft_id}"),
                subject_ciphertext=self.cipher.encrypt_text(subject, aad=f"gmail-draft-subject:{draft_id}"),
                body_ciphertext=self.cipher.encrypt_text(body_text, aad=f"gmail-draft-body:{draft_id}"),
                generation_meta_ciphertext=self.cipher.encrypt_text(
                    json.dumps(meta, ensure_ascii=False), aad=f"gmail-draft-meta:{draft_id}"
                ),
                content_sha256=content_sha,
            )
            if not complete:
                raise GmailIntegrationError("generation_state_conflict", "Draft generation state changed.", status_code=409)
            database.upsert_run(
                draft["run_id"], draft["session_id"], draft.get("event_id") or draft_id,
                meta.get("model"), "email", "awaiting_approval", project_id=draft["project_id"],
                metrics={"provider": meta.get("provider"), "skills_count": len(meta.get("skills") or []),
                         "references_count": len(meta.get("references") or [])},
            )
            return {"draft_id": draft_id, "status": "awaiting_approval", "claimed": True}
        except Exception as exc:
            database.fail_n8n_gmail_generation(draft_id, "draft_generation_failed")
            database.upsert_run(
                draft["run_id"], draft["session_id"], draft.get("event_id") or draft_id,
                None, "email", "generation_failed", project_id=draft["project_id"], completed_at=_iso(self._clock()),
            )
            if isinstance(exc, GmailIntegrationError):
                raise
            raise GmailIntegrationError(
                "draft_generation_failed", "Email draft generation failed.", status_code=502, recoverable=True
            ) from exc

    def recover_generation_jobs(self) -> List[str]:
        return database.recover_n8n_gmail_generations()

    def _set_run_status(self, draft: Mapping[str, Any], status: str, *, completed: bool = False) -> None:
        run = database.get_run(str(draft["run_id"])) or {}
        database.upsert_run(
            str(draft["run_id"]), str(draft["session_id"]),
            str(draft.get("event_id") or draft["draft_id"]), run.get("model"),
            "email", status, project_id=str(draft["project_id"]),
            completed_at=_iso(self._clock()) if completed else None,
        )

    def _public_draft(self, row: Mapping[str, Any], *, include_body: bool) -> Dict[str, Any]:
        result = {
            "draft_id": row["draft_id"], "project_id": row["project_id"],
            "binding_id": row["thread_id"], "thread_id": row["thread_id"],
            "session_id": row["session_id"], "run_id": row["run_id"], "kind": row["kind"],
            "status": row["status"], "revision": int(row["revision"]),
            "content_sha256": row["content_sha256"], "sha256": row["content_sha256"],
            "gmail_thread_id": row["gmail_thread_id"],
            "delivery_id": row.get("delivery_id"), "approved_at": row.get("approved_at"),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }
        if row.get("subject_ciphertext"):
            result["subject"] = self.cipher.decrypt_text(
                row["subject_ciphertext"], aad=f"gmail-draft-subject:{row['draft_id']}"
            )
        else:
            result["subject"] = None
        if include_body and row.get("body_ciphertext"):
            result["body_text"] = self.cipher.decrypt_text(
                row["body_ciphertext"], aad=f"gmail-draft-body:{row['draft_id']}"
            )
            result["body"] = result["body_text"]
            result["recipient"] = self.cipher.decrypt_text(
                row["recipient_ciphertext"], aad=f"gmail-draft-recipient:{row['draft_id']}"
            )
            if row.get("generation_meta_ciphertext"):
                try:
                    generation = json.loads(self.cipher.decrypt_text(
                        row["generation_meta_ciphertext"], aad=f"gmail-draft-meta:{row['draft_id']}"
                    ))
                    result["generation"] = generation
                    if isinstance(generation, dict):
                        for key in (
                            "summary", "intent", "tone", "needs_human_attention",
                            "warnings", "model", "provider", "skills", "references",
                            "context_truncated",
                        ):
                            if key in generation:
                                result[key] = generation[key]
                except GmailCryptoError:
                    result["generation"] = None
            if row.get("event_id"):
                with database.get_db_conn() as conn:
                    event = conn.execute(
                        "SELECT payload_ciphertext, created_at FROM n8n_gmail_events WHERE event_id = ?",
                        (row["event_id"],),
                    ).fetchone()
                if event and event["payload_ciphertext"]:
                    try:
                        source = json.loads(self.cipher.decrypt_text(
                            event["payload_ciphertext"], aad=f"gmail-event:{row['event_id']}"
                        ))
                        result["source"] = {
                            "sender": source.get("sender", ""),
                            "subject": source.get("subject", ""),
                            "received_at": event["created_at"],
                            "message_id": source.get("gmail_message_id", ""),
                            "thread_id": source.get("gmail_thread_id", ""),
                        }
                        result["attachments"] = source.get("attachments", [])
                    except (GmailCryptoError, ValueError, TypeError):
                        result["source"] = None
                        result["attachments"] = []
            if row.get("delivery_id"):
                delivery = database.get_n8n_gmail_delivery(row["delivery_id"])
                if delivery:
                    result["delivery"] = {
                        "delivery_id": delivery["delivery_id"],
                        "status": delivery["status"],
                        "message_id": delivery.get("gmail_message_id"),
                        "thread_id": delivery.get("gmail_thread_id"),
                        "error_code": delivery.get("error_code"),
                        "recoverable": (
                            None if delivery.get("recoverable") is None
                            else bool(delivery["recoverable"])
                        ),
                        "completed_at": delivery.get("completed_at"),
                        "expires_at": delivery.get("expires_at"),
                    }
        return result

    def get_draft(self, draft_id: str) -> Dict[str, Any]:
        row = database.get_n8n_gmail_draft(_safe_id(draft_id, "draft_id"))
        if not row or row["tombstoned_at"]:
            raise GmailIntegrationError("draft_not_found", "Draft not found.", status_code=404)
        return self._public_draft(row, include_body=True)

    def get_mail_run(self, run_id: str) -> Dict[str, Any]:
        row = database.get_n8n_gmail_draft_by_run(_safe_id(run_id, "run_id"))
        if not row or row["tombstoned_at"]:
            raise GmailIntegrationError("mail_run_not_found", "Mail run not found.", status_code=404)
        return self._public_draft(row, include_body=True)

    def list_drafts(self, *, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        return [self._public_draft(row, include_body=False) for row in database.list_n8n_gmail_drafts(status=status, limit=limit)]

    def public_event_snapshot(self) -> Dict[str, Any]:
        """Content-free change signal for the browser SSE channel."""

        rows = database.list_n8n_gmail_drafts(limit=250)
        counts: Dict[str, int] = {}
        latest = None
        for row in rows:
            status = str(row.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
            updated = row.get("updated_at")
            if updated and (latest is None or updated > latest):
                latest = updated
        fingerprint = _sha(
            [
                (row["run_id"], row["draft_id"], row["status"], row["revision"], row["updated_at"])
                for row in rows
            ]
        )
        return {
            "type": "mail_runs_changed",
            "pending_approvals": counts.get("awaiting_approval", 0),
            "counts": counts,
            "latest_updated_at": latest,
            "fingerprint": fingerprint,
        }

    def edit_draft(self, draft_id: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        draft_id = _safe_id(draft_id, "draft_id")
        current = database.get_n8n_gmail_draft(draft_id)
        if not current or current["tombstoned_at"]:
            raise GmailIntegrationError("draft_not_found", "Draft not found.", status_code=404)
        expected_revision = int(payload.get("expected_revision"))
        expected_sha = str(payload.get("expected_sha256") or "")
        supplied_subject = payload.get("subject")
        if current["kind"] == "reply" and supplied_subject is not None:
            raise GmailIntegrationError(
                "reply_subject_locked", "Reply subjects cannot be edited in v1.", status_code=409
            )
        stored_subject = self.cipher.decrypt_text(
            current["subject_ciphertext"], aad=f"gmail-draft-subject:{draft_id}"
        )
        subject = stored_subject if supplied_subject is None else _text(supplied_subject, "subject", _MAX_SUBJECT)
        body_text = _text(payload.get("body"), "body", _MAX_BODY)
        recipient = self.cipher.decrypt_text(
            current["recipient_ciphertext"], aad=f"gmail-draft-recipient:{draft_id}"
        )
        content_sha = _sha({"recipient": recipient, "subject": subject, "body_text": body_text})
        updated = database.edit_n8n_gmail_draft(
            draft_id, expected_revision=expected_revision, expected_sha256=expected_sha,
            subject_ciphertext=self.cipher.encrypt_text(subject, aad=f"gmail-draft-subject:{draft_id}"),
            body_ciphertext=self.cipher.encrypt_text(body_text, aad=f"gmail-draft-body:{draft_id}"),
            content_sha256=content_sha,
        )
        if not updated:
            raise GmailIntegrationError("draft_revision_conflict", "The draft changed; reload before editing.", status_code=409)
        return self._public_draft(updated, include_body=True)

    def approve_draft(self, draft_id: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        draft_id = _safe_id(draft_id, "draft_id")
        profile = self._profile_ready()
        current = database.get_n8n_gmail_draft(draft_id)
        if not current:
            raise GmailIntegrationError("draft_not_found", "Draft not found.", status_code=404)
        if str(current.get("project_id") or "") != str(profile.get("project_id") or ""):
            raise GmailIntegrationError(
                "draft_scope_changed",
                "The draft no longer belongs to the enabled Gmail project.",
                status_code=409,
            )
        expected_revision = int(payload.get("expected_revision"))
        expected_sha = str(payload.get("expected_sha256") or "")
        delivery_id = self._id_factory("email_delivery")
        claim_token = hmac.new(
            self._outbound_secret(),
            f"claim\n{delivery_id}\n{expected_revision}\n{expected_sha}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        accepted, delivery = database.approve_n8n_gmail_draft(
            draft_id, expected_revision=expected_revision, expected_sha256=expected_sha,
            delivery_id=delivery_id,
            claim_token_sha256=hashlib.sha256(claim_token.encode()).hexdigest(),
            expires_at=_iso(self._clock() + timedelta(hours=24)),
        )
        if not accepted or not delivery:
            raise GmailIntegrationError("draft_revision_conflict", "The draft cannot be approved in its current state.", status_code=409)
        self._set_run_status(current, "approved_queued")
        return {"draft_id": draft_id, "delivery_id": delivery["delivery_id"], "status": "approved_queued", "idempotent": delivery["delivery_id"] != delivery_id}

    def dispatch_delivery(self, delivery_id: str) -> Dict[str, Any]:
        profile = self._profile_ready()
        delivery = database.get_n8n_gmail_delivery(_safe_id(delivery_id, "delivery_id"))
        if not delivery:
            raise GmailIntegrationError("delivery_not_found", "Delivery not found.", status_code=404)
        if str(delivery.get("project_id") or "") != str(profile.get("project_id") or ""):
            raise GmailIntegrationError(
                "delivery_scope_changed",
                "The delivery no longer belongs to the enabled Gmail project.",
                status_code=409,
            )
        if delivery["status"] != "pending":
            return {"delivery_id": delivery_id, "status": delivery["status"], "dispatched": False}
        claim_token = hmac.new(
            self._outbound_secret(),
            f"claim\n{delivery_id}\n{delivery['revision']}\n{delivery['content_sha256']}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        try:
            self._delivery_dispatcher({"delivery_id": delivery_id, "claim_token": claim_token})
        except Exception as exc:
            database.record_n8n_gmail_dispatch(delivery_id, succeeded=False)
            raise GmailIntegrationError(
                "delivery_dispatch_failed", "The n8n delivery trigger failed.", status_code=502, recoverable=True
            ) from exc
        database.record_n8n_gmail_dispatch(delivery_id, succeeded=True)
        return {"delivery_id": delivery_id, "status": "approved_queued", "dispatched": True}

    def recover_delivery_jobs(self, *, limit: int = 100) -> List[str]:
        now = _iso(self._clock())
        database.expire_n8n_gmail_deliveries(now=now)
        database.recover_n8n_gmail_claimed_deliveries()
        return database.list_pending_n8n_gmail_deliveries(now=now, limit=limit)

    def reject_draft(self, draft_id: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if not database.reject_n8n_gmail_draft(
            _safe_id(draft_id, "draft_id"), expected_revision=int(payload.get("expected_revision")),
            expected_sha256=str(payload.get("expected_sha256") or ""),
        ):
            raise GmailIntegrationError("draft_revision_conflict", "The draft cannot be rejected in its current state.", status_code=409)
        current = database.get_n8n_gmail_draft(draft_id)
        if current:
            self._set_run_status(current, "rejected", completed=True)
        return {"draft_id": draft_id, "status": "rejected"}

    def regenerate_draft(self, draft_id: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if not database.queue_n8n_gmail_regeneration(
            _safe_id(draft_id, "draft_id"), expected_revision=int(payload.get("expected_revision")),
            expected_sha256=str(payload.get("expected_sha256") or ""),
            empty_sha256=hashlib.sha256(b"").hexdigest(),
        ):
            raise GmailIntegrationError("draft_revision_conflict", "The draft cannot be regenerated in its current state.", status_code=409)
        current = database.get_n8n_gmail_draft(draft_id)
        if current:
            self._set_run_status(current, "queued")
        return {"draft_id": draft_id, "status": "queued"}

    def claim_delivery(self, delivery_id: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        delivery_id = _safe_id(delivery_id, "delivery_id")
        claim_id = _safe_id(payload.get("claim_id"), "claim_id")
        claim_token = str(payload.get("claim_token") or "")
        delivery = database.get_n8n_gmail_delivery(delivery_id)
        if not delivery or not claim_token or not hmac.compare_digest(
            hashlib.sha256(claim_token.encode()).hexdigest(), str(delivery.get("claim_token_sha256") or "")
        ):
            raise GmailIntegrationError("claim_token_invalid", "The delivery claim token is invalid.", status_code=401)
        # A duplicate claim for the same already-started execution is content
        # free and may be acknowledged even if the profile was subsequently
        # disabled.  A *first* claim, however, must pass the live isolation,
        # workflow and immutable Project checks before plaintext is released.
        if delivery.get("status") == "pending":
            profile = self._profile_ready()
            if str(delivery.get("project_id") or "") != str(profile.get("project_id") or ""):
                raise GmailIntegrationError(
                    "delivery_scope_changed",
                    "The delivery no longer belongs to the enabled Gmail project.",
                    status_code=409,
                )
        token = secrets.token_urlsafe(32)
        outcome, delivery = database.claim_n8n_gmail_delivery(
            delivery_id, claim_id=claim_id, result_token_sha256=hashlib.sha256(token.encode()).hexdigest(),
            now=_iso(self._clock()),
        )
        if outcome == "missing":
            raise GmailIntegrationError("delivery_not_found", "Delivery not found.", status_code=404)
        if outcome == "conflict":
            raise GmailIntegrationError("delivery_claim_conflict", "Delivery was already claimed or completed.", status_code=409)
        if outcome == "expired":
            raise GmailIntegrationError("approval_expired", "The 24-hour approval window expired.", status_code=409)
        if outcome == "replay":
            database.mark_n8n_gmail_delivery_unknown(delivery_id)
            return {
                "delivery_id": delivery_id,
                "claim_id": claim_id,
                "status": "delivery_unknown",
                "idempotent": True,
            }
        draft = database.get_n8n_gmail_draft(delivery["draft_id"])
        self._set_run_status(draft, "sending")
        public = self._public_draft(draft, include_body=True)
        return {
            "delivery_id": delivery_id, "claim_id": claim_id, "status": "sending",
            "idempotent": False, "result_token": token,
            "kind": draft["kind"], "recipient": public["recipient"], "subject": public["subject"],
            "body_text": public["body_text"], "gmail_thread_id": draft["gmail_thread_id"],
            "revision": int(delivery["revision"]), "content_sha256": delivery["content_sha256"],
        }

    def complete_delivery(self, delivery_id: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        delivery_id = _safe_id(delivery_id, "delivery_id")
        delivery = database.get_n8n_gmail_delivery(delivery_id)
        if not delivery:
            raise GmailIntegrationError("delivery_not_found", "Delivery not found.", status_code=404)
        token = str(payload.get("result_token") or "")
        if not token or not hmac.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), str(delivery.get("result_token_sha256") or "")
        ):
            raise GmailIntegrationError("result_token_invalid", "The delivery result token is invalid.", status_code=401)
        result_id = _safe_id(payload.get("result_id"), "result_id")
        status = str(payload.get("status") or "")
        if status not in ("sent", "failed"):
            raise GmailIntegrationError("invalid_request", "Delivery status must be sent or failed.")
        gmail_message_id = payload.get("gmail_message_id")
        gmail_thread_id = payload.get("gmail_thread_id")
        error_code = payload.get("error_code")
        recoverable = payload.get("recoverable")
        if status == "sent":
            gmail_message_id = _safe_id(gmail_message_id, "gmail_message_id")
            gmail_thread_id = _safe_id(gmail_thread_id, "gmail_thread_id")
            if error_code is not None or recoverable is not None:
                raise GmailIntegrationError("invalid_request", "Successful results cannot include an error.")
        else:
            error_code = _safe_id(error_code, "error_code")
            if not isinstance(recoverable, bool):
                raise GmailIntegrationError("invalid_request", "Failed results require recoverable boolean.")
            gmail_message_id = None
            gmail_thread_id = None
        normalized = {
            "result_id": result_id, "status": status, "gmail_message_id": gmail_message_id,
            "gmail_thread_id": gmail_thread_id, "error_code": error_code, "recoverable": recoverable,
        }
        outcome, final = database.finish_n8n_gmail_delivery(
            delivery_id, result_id=result_id, result_sha256=_sha(normalized), status=status,
            gmail_message_id=gmail_message_id, gmail_thread_id=gmail_thread_id,
            error_code=error_code, recoverable=recoverable,
        )
        if outcome == "missing":
            raise GmailIntegrationError("delivery_not_found", "Delivery not found.", status_code=404)
        if outcome == "conflict":
            raise GmailIntegrationError("delivery_result_conflict", "A different delivery result already exists.", status_code=409)
        draft = database.get_n8n_gmail_draft(final["draft_id"])
        if draft:
            self._set_run_status(draft, str(final["status"]), completed=True)
        return {"delivery_id": delivery_id, "status": final["status"], "idempotent": outcome == "replay"}

    def tombstone_draft(self, draft_id: str) -> Dict[str, Any]:
        if not database.tombstone_n8n_gmail_draft(_safe_id(draft_id, "draft_id")):
            raise GmailIntegrationError("draft_not_found", "Draft not found.", status_code=404)
        return {"draft_id": draft_id, "status": "tombstoned"}

    def delete_thread(self, thread_id: str) -> Dict[str, Any]:
        thread_id = _safe_id(thread_id, "thread_id")
        if database.n8n_gmail_thread_has_unresolved_delivery(thread_id):
            raise GmailIntegrationError(
                "delivery_unresolved",
                "Resolve the in-progress or unknown delivery before deleting this mail thread.",
                status_code=409,
            )
        if not database.tombstone_n8n_gmail_thread(thread_id):
            raise GmailIntegrationError("mail_thread_not_found", "Mail thread not found.", status_code=404)
        return {"thread_id": thread_id, "status": "tombstoned"}

    def purge_retention(self, *, cutoff: Optional[datetime] = None) -> Dict[str, int]:
        profile = database.get_n8n_gmail_profile()
        if not profile:
            return {"tombstoned_drafts": 0}
        actual = cutoff or (self._clock() - timedelta(days=int(profile["retention_days"])))
        return database.purge_n8n_gmail_retention(_iso(actual))
