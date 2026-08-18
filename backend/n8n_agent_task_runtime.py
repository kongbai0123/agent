"""Protected n8n -> Workbench Agent task and runtime-approval boundary.

This module is intentionally standalone.  It owns additive private tables and
does not mount itself in the application.  The n8n caller is authenticated with
the same HMAC request shape as the Gmail bridge, while browser-facing methods
return only bounded metadata.  Instructions, task input, generated output and
the underlying n8n credential id are always encrypted at rest.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

import database
from n8n_gmail_crypto import AesGcmContentCipher


HMAC_PROFILE = "agent-runtime"
TASK_STATES = {"queued", "generating", "succeeded", "generation_failed", "cancelled"}
APPROVAL_STATES = {"pending", "approved", "approved_by_grant", "rejected", "revoked", "expired"}
CREDENTIAL_STATES = {"unknown", "ready", "degraded", "revoked"}
EXTERNAL_ACTIONS = {
    "send_email", "http_write", "database_write", "delete", "publish",
    "external_write",
}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9._-]{0,62}$")
_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SHA_RE = re.compile(r"^[a-f0-9]{64}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_APPROVAL_TOKEN_RE = re.compile(
    r"^(?P<binding>[A-Za-z0-9][A-Za-z0-9._:-]{0,127}):(?P<digest>[a-f0-9]{64})$"
)
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|private[_-]?key|oauth)"
)

_MAX_INPUT_BYTES = 256_000
_MAX_OUTPUT_BYTES = 128_000
_MAX_CONFIG_BYTES = 128_000
_MAX_INSTRUCTION_CHARS = 20_000
_MAX_SKILLS = 8
_MAX_CLOCK_SKEW_SECONDS = 300


class N8nAgentTaskError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        recoverable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.recoverable = recoverable


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise N8nAgentTaskError(
            "N8N_AGENT_JSON_INVALID", "The request contains invalid JSON data.", status_code=422
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _safe_id(value: Any, field: str) -> str:
    text = str(value or "")
    if not _ID_RE.fullmatch(text):
        raise N8nAgentTaskError(
            "N8N_AGENT_REQUEST_INVALID", f"{field} is invalid.", status_code=422
        )
    return text


def _sha256(value: Any, field: str) -> str:
    text = str(value or "")
    if not _SHA_RE.fullmatch(text):
        raise N8nAgentTaskError(
            "N8N_AGENT_REQUEST_INVALID", f"{field} must be a lowercase SHA-256 digest.", status_code=422
        )
    return text


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_SECRET_KEY_RE.search(str(key)) or _contains_secret_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _bounded_json(value: Any, maximum: int, *, reject_secrets: bool = False) -> Any:
    if reject_secrets and _contains_secret_key(value):
        raise N8nAgentTaskError(
            "N8N_AGENT_SECRET_FIELD_FORBIDDEN",
            "Credential and secret fields are forbidden at the Agent task boundary.",
            status_code=422,
        )
    encoded = _canonical(value)
    if len(encoded.encode("utf-8")) > maximum:
        raise N8nAgentTaskError(
            "N8N_AGENT_PAYLOAD_TOO_LARGE", "The Agent task payload is too large.", status_code=413
        )
    return json.loads(encoded)


def _validate_schema_value(value: Any, schema: Mapping[str, Any], path: str = "result") -> None:
    """Validate the small JSON-Schema subset accepted by Agent bindings."""

    expected = schema.get("type")
    if isinstance(expected, list):
        variants = [dict(schema, type=item) for item in expected if isinstance(item, str)]
        if not variants:
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_SCHEMA_INVALID", "The output schema type is invalid.", status_code=422)
        failures = 0
        for variant in variants:
            try:
                _validate_schema_value(value, variant, path)
                return
            except N8nAgentTaskError:
                failures += 1
        if failures:
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_INVALID", f"{path} does not match the output schema.", status_code=422)
    elif expected == "object":
        if not isinstance(value, Mapping):
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_INVALID", f"{path} must be an object.", status_code=422)
        properties = schema.get("properties") or {}
        if not isinstance(properties, Mapping):
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_SCHEMA_INVALID", "The output schema properties are invalid.", status_code=422)
        required = schema.get("required") or []
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_SCHEMA_INVALID", "The output schema required list is invalid.", status_code=422)
        missing = [item for item in required if item not in value]
        if missing:
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_INVALID", f"{path} is missing required fields.", status_code=422)
        if schema.get("additionalProperties") is False and any(key not in properties for key in value):
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_INVALID", f"{path} contains fields outside the output schema.", status_code=422)
        for key, item in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, Mapping):
                _validate_schema_value(item, child_schema, f"{path}.{key}")
    elif expected == "array":
        if not isinstance(value, list):
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_INVALID", f"{path} must be an array.", status_code=422)
        maximum = schema.get("maxItems")
        if isinstance(maximum, int) and len(value) > maximum:
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_INVALID", f"{path} contains too many items.", status_code=422)
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate_schema_value(item, item_schema, f"{path}[{index}]")
    elif expected == "string":
        if not isinstance(value, str):
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_INVALID", f"{path} must be text.", status_code=422)
        maximum = schema.get("maxLength")
        if isinstance(maximum, int) and len(value) > maximum:
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_INVALID", f"{path} is too long.", status_code=422)
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_INVALID", f"{path} must be an integer.", status_code=422)
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_INVALID", f"{path} must be a number.", status_code=422)
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_INVALID", f"{path} must be a boolean.", status_code=422)
    elif expected == "null":
        if value is not None:
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_INVALID", f"{path} must be null.", status_code=422)
    elif expected not in (None, ""):
        raise N8nAgentTaskError("N8N_AGENT_OUTPUT_SCHEMA_INVALID", "The output schema type is unsupported.", status_code=422)

    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or value not in enum:
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_INVALID", f"{path} is outside the allowed values.", status_code=422)


def _normalize_target(kind: str, value: str) -> tuple[str, str]:
    target_kind = str(kind or "").casefold()
    target = str(value or "").strip()
    if target_kind == "email":
        if len(target) > 320 or not _EMAIL_RE.fullmatch(target):
            raise N8nAgentTaskError("N8N_RUNTIME_TARGET_INVALID", "The email target is invalid.", status_code=422)
        canonical = f"mailto:{target.casefold()}"
        return canonical, canonical
    if target_kind == "url":
        if len(target) > 2_048:
            raise N8nAgentTaskError("N8N_RUNTIME_TARGET_INVALID", "The URL target is invalid.", status_code=422)
        try:
            parsed = urlsplit(target)
            if (
                parsed.scheme.casefold() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("unsafe URL")
            port = f":{parsed.port}" if parsed.port else ""
        except (ValueError, UnicodeError) as exc:
            raise N8nAgentTaskError("N8N_RUNTIME_TARGET_INVALID", "The URL target is invalid.", status_code=422) from exc
        path = parsed.path or "/"
        canonical = f"{parsed.scheme.casefold()}://{parsed.hostname.casefold()}{port}{path}"
        return canonical, canonical
    if target_kind == "service":
        service = target.casefold()
        if not re.fullmatch(r"[a-z][a-z0-9._-]{0,62}", service):
            raise N8nAgentTaskError("N8N_RUNTIME_TARGET_INVALID", "The service target is invalid.", status_code=422)
        return f"service:{service}", f"service:{service}"
    raise N8nAgentTaskError("N8N_RUNTIME_TARGET_INVALID", "The target type is unsupported.", status_code=422)


class N8nAgentTaskRuntime:
    """Durable, encrypted task bridge with exact-scope runtime approvals."""

    def __init__(
        self,
        *,
        cipher: AesGcmContentCipher,
        hmac_secret_provider: Callable[[], bytes],
        generator: Callable[[Mapping[str, Any]], Any],
        skill_resolver: Optional[Callable[[str, str, str], Mapping[str, Any]]] = None,
        credential_resolver: Optional[Callable[[str], Mapping[str, Any]]] = None,
        policy_resolver: Optional[Callable[[str], Mapping[str, Any]]] = None,
        execution_gate: Optional[Callable[[str], Any]] = None,
        workflow_revision_resolver: Optional[Callable[[str], Any]] = None,
        clock: Callable[[], datetime] = _utcnow,
        id_factory: Callable[[str], str] = lambda prefix: f"{prefix}_{secrets.token_urlsafe(18)}",
        boot_id: Optional[str] = None,
        max_clock_skew_seconds: int = _MAX_CLOCK_SKEW_SECONDS,
    ) -> None:
        self.cipher = cipher
        self._hmac_secret_provider = hmac_secret_provider
        self._generator = generator
        self._skill_resolver = skill_resolver
        self._credential_resolver = credential_resolver
        self._policy_resolver = policy_resolver
        self._execution_gate = execution_gate
        self._workflow_revision_resolver = workflow_revision_resolver
        self._clock = clock
        self._id_factory = id_factory
        self.boot_id = boot_id or secrets.token_hex(16)
        self._max_clock_skew = int(max_clock_skew_seconds)
        self._lock = threading.RLock()
        self._ensure_schema()
        self._revoke_prior_boot_grants()
        self.recover_incomplete_tasks()

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def _require_execution_enabled(self, project_id: str) -> None:
        gate = self._execution_gate
        if gate is None:
            return
        try:
            result = gate(project_id)
        except Exception as exc:
            raise N8nAgentTaskError(
                str(getattr(exc, "code", "EXTENSION_DISABLED"))[:128],
                "The n8n extension is disabled for this Project.",
                status_code=409,
            ) from exc
        if result is False:
            raise N8nAgentTaskError(
                "EXTENSION_DISABLED",
                "The n8n extension is disabled for this Project.",
                status_code=409,
            )

    def _ensure_schema(self) -> None:
        with database.get_db_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS n8n_agent_workflow_bindings (
                    workflow_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    workflow_name TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS n8n_agent_task_nonces (
                    profile TEXT NOT NULL, nonce TEXT NOT NULL, method TEXT NOT NULL,
                    path TEXT NOT NULL, request_timestamp INTEGER NOT NULL,
                    request_sha256 TEXT NOT NULL, expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(profile, nonce)
                );
                CREATE INDEX IF NOT EXISTS idx_n8n_agent_task_nonces_expiry
                    ON n8n_agent_task_nonces(expires_at);

                CREATE TABLE IF NOT EXISTS n8n_agent_task_bindings (
                    agent_binding_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL, workflow_revision TEXT NOT NULL,
                    active_version_id TEXT,
                    node_id TEXT NOT NULL, config_envelope TEXT NOT NULL,
                    config_digest TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(workflow_id, workflow_revision, node_id)
                );
                CREATE INDEX IF NOT EXISTS idx_n8n_agent_task_bindings_project
                    ON n8n_agent_task_bindings(project_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS n8n_agent_binding_claims (
                    claim_id TEXT PRIMARY KEY, agent_binding_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL, session_id TEXT, proposed_node_id TEXT NOT NULL,
                    workflow_revision TEXT NOT NULL,
                    config_envelope TEXT NOT NULL, config_digest TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'provisional', created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, consumed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_n8n_agent_binding_claims_project
                    ON n8n_agent_binding_claims(project_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS n8n_agent_approval_manifest_claims (
                    claim_id TEXT PRIMARY KEY, approval_binding_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL, session_id TEXT, proposed_node_id TEXT NOT NULL,
                    workflow_revision TEXT NOT NULL, manifest_envelope TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'provisional',
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_n8n_approval_manifest_claims_project
                    ON n8n_agent_approval_manifest_claims(project_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS n8n_agent_approval_manifests (
                    approval_binding_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL, workflow_revision TEXT NOT NULL,
                    active_version_id TEXT, node_id TEXT NOT NULL,
                    manifest_envelope TEXT NOT NULL, manifest_digest TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(workflow_id, workflow_revision, node_id)
                );
                CREATE INDEX IF NOT EXISTS idx_n8n_approval_manifests_project
                    ON n8n_agent_approval_manifests(project_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS n8n_agent_tasks (
                    task_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                    project_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
                    workflow_revision TEXT NOT NULL, node_id TEXT NOT NULL,
                    agent_binding_id TEXT NOT NULL, binding_digest TEXT NOT NULL,
                    request_digest TEXT NOT NULL, input_envelope TEXT NOT NULL,
                    config_envelope TEXT NOT NULL, output_envelope TEXT,
                    output_sha256 TEXT, status TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT, created_at TEXT NOT NULL,
                    started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL,
                    UNIQUE(workflow_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_n8n_agent_tasks_project
                    ON n8n_agent_tasks(project_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_n8n_agent_tasks_status
                    ON n8n_agent_tasks(status, created_at);

                CREATE TABLE IF NOT EXISTS n8n_agent_credential_aliases (
                    project_id TEXT NOT NULL, alias TEXT NOT NULL,
                    credential_ref_envelope TEXT NOT NULL, credential_type TEXT NOT NULL,
                    display_name TEXT NOT NULL, status TEXT NOT NULL,
                    metadata_digest TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, alias)
                );

                CREATE TABLE IF NOT EXISTS n8n_agent_runtime_policy_epochs (
                    project_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS n8n_agent_runtime_approvals (
                    approval_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                    project_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
                    workflow_revision TEXT NOT NULL, node_id TEXT NOT NULL,
                    approval_binding_id TEXT NOT NULL, manifest_digest TEXT NOT NULL,
                    credential_alias TEXT NOT NULL, target_kind TEXT NOT NULL,
                    target_digest TEXT NOT NULL, target_display TEXT NOT NULL,
                    action TEXT NOT NULL, run_key TEXT NOT NULL, task_id TEXT,
                    request_digest TEXT NOT NULL, policy_epoch INTEGER NOT NULL,
                    status TEXT NOT NULL, grant_id TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    UNIQUE(workflow_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_n8n_runtime_approvals_project
                    ON n8n_agent_runtime_approvals(project_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS n8n_agent_runtime_grants (
                    grant_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL, workflow_revision TEXT NOT NULL,
                    node_id TEXT NOT NULL, approval_binding_id TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL, credential_alias TEXT NOT NULL,
                    target_digest TEXT NOT NULL, action TEXT NOT NULL,
                    scope TEXT NOT NULL, run_key TEXT, boot_id TEXT NOT NULL,
                    policy_epoch INTEGER NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    revoked_at TEXT, revoke_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_n8n_runtime_grants_match
                    ON n8n_agent_runtime_grants(
                        project_id, workflow_id, workflow_revision, node_id,
                        approval_binding_id, manifest_digest,
                        credential_alias, target_digest, action, status
                    );
                """
            )
            # Additive migration for databases created by the earlier graph
            # authoring beta.  Old provisional claims intentionally retain an
            # empty token and therefore fail closed until the graph is planned
            # again.
            binding_columns = {
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(n8n_agent_task_bindings)"
                ).fetchall()
            }
            if "active_version_id" not in binding_columns:
                conn.execute(
                    "ALTER TABLE n8n_agent_task_bindings ADD COLUMN active_version_id TEXT"
                )
            claim_columns = {
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(n8n_agent_binding_claims)"
                ).fetchall()
            }
            if "workflow_revision" not in claim_columns:
                conn.execute(
                    "ALTER TABLE n8n_agent_binding_claims ADD COLUMN workflow_revision TEXT NOT NULL DEFAULT ''"
                )
            approval_columns = {
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(n8n_agent_runtime_approvals)"
                ).fetchall()
            }
            if "approval_binding_id" not in approval_columns:
                conn.execute(
                    "ALTER TABLE n8n_agent_runtime_approvals ADD COLUMN approval_binding_id TEXT NOT NULL DEFAULT ''"
                )
            if "manifest_digest" not in approval_columns:
                conn.execute(
                    "ALTER TABLE n8n_agent_runtime_approvals ADD COLUMN manifest_digest TEXT NOT NULL DEFAULT ''"
                )
            grant_columns = {
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(n8n_agent_runtime_grants)"
                ).fetchall()
            }
            if "approval_binding_id" not in grant_columns:
                conn.execute(
                    "ALTER TABLE n8n_agent_runtime_grants ADD COLUMN approval_binding_id TEXT NOT NULL DEFAULT ''"
                )
            if "manifest_digest" not in grant_columns:
                conn.execute(
                    "ALTER TABLE n8n_agent_runtime_grants ADD COLUMN manifest_digest TEXT NOT NULL DEFAULT ''"
                )

    def _project(self, project_id: str) -> Mapping[str, Any]:
        project = database.get_project(str(project_id or "").strip())
        if not project or bool(project.get("archived")):
            raise N8nAgentTaskError("PROJECT_NOT_FOUND", "Project was not found.", status_code=404)
        return project

    def _workflow_project(self, workflow_id: str) -> tuple[str, Mapping[str, Any]]:
        workflow_id = _safe_id(workflow_id, "workflow_id")
        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT * FROM n8n_agent_workflow_bindings WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
        if not row:
            raise N8nAgentTaskError(
                "N8N_WORKFLOW_NOT_MANAGED", "The workflow is not managed by Workbench.", status_code=403
            )
        project_id = str(row["project_id"])
        self._project(project_id)
        return project_id, dict(row)

    def _read_live_active_version(self, workflow_id: str) -> str:
        """Read the authoritative active n8n version without trusting n8n input."""

        if self._workflow_revision_resolver is None:
            raise N8nAgentTaskError(
                "N8N_WORKFLOW_REVISION_UNAVAILABLE",
                "The live n8n workflow revision cannot be verified.",
                status_code=503,
            )
        try:
            resolved = self._workflow_revision_resolver(workflow_id)
        except Exception as exc:
            raise N8nAgentTaskError(
                "N8N_WORKFLOW_REVISION_UNAVAILABLE",
                "The live n8n workflow revision cannot be verified.",
                status_code=503,
            ) from exc
        if isinstance(resolved, Mapping):
            if resolved.get("active") is not True:
                raise N8nAgentTaskError(
                    "N8N_WORKFLOW_NOT_ACTIVE",
                    "The managed n8n workflow is not active.",
                    status_code=409,
                )
            resolved = resolved.get("active_version_id") or resolved.get("activeVersionId")
        try:
            return _safe_id(resolved, "active_version_id")
        except N8nAgentTaskError as exc:
            raise N8nAgentTaskError(
                "N8N_WORKFLOW_REVISION_UNAVAILABLE",
                "The live n8n workflow revision cannot be verified.",
                status_code=503,
            ) from exc

    def _revision_drift(self, project_id: str, workflow_id: str) -> None:
        """Disable stale bindings and revoke every pending/active permission."""

        now = _iso(self._now())
        with database.get_db_conn() as conn:
            conn.execute(
                """
                UPDATE n8n_agent_task_bindings
                   SET active=0,revision=revision+1,updated_at=?
                 WHERE project_id=? AND workflow_id=? AND active=1
                """,
                (now, project_id, workflow_id),
            )
            conn.execute(
                """
                UPDATE n8n_agent_approval_manifests
                   SET active=0,revision=revision+1,updated_at=?
                 WHERE project_id=? AND workflow_id=? AND active=1
                """,
                (now, project_id, workflow_id),
            )
        self._revoke_grants(
            project_id=project_id,
            workflow_id=workflow_id,
            reason="workflow_revision_changed",
        )

    def _assert_live_binding_revision(self, row: Mapping[str, Any]) -> str:
        expected = str(row.get("active_version_id") or "").strip()
        if not expected:
            raise N8nAgentTaskError(
                "N8N_AGENT_BINDING_NOT_ACTIVATED",
                "The Agent binding has not been reconciled with an active n8n version.",
                status_code=409,
            )
        try:
            live = self._read_live_active_version(str(row["workflow_id"]))
        except N8nAgentTaskError as exc:
            if exc.code == "N8N_WORKFLOW_NOT_ACTIVE":
                self._revision_drift(
                    str(row["project_id"]), str(row["workflow_id"])
                )
                raise N8nAgentTaskError(
                    "N8N_WORKFLOW_REVISION_CHANGED",
                    "The managed n8n workflow is no longer active.",
                    status_code=409,
                ) from exc
            raise
        if not hmac.compare_digest(expected, live):
            self._revision_drift(str(row["project_id"]), str(row["workflow_id"]))
            raise N8nAgentTaskError(
                "N8N_WORKFLOW_REVISION_CHANGED",
                "The active n8n workflow changed and must be reviewed again.",
                status_code=409,
            )
        return live

    def _assert_live_workflow_token(
        self, *, project_id: str, workflow_id: str, workflow_revision: str
    ) -> str:
        """Resolve one active binding for the static compiled revision token."""

        with database.get_db_conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM n8n_agent_task_bindings
                 WHERE project_id=? AND workflow_id=? AND workflow_revision=? AND active=1
                 ORDER BY updated_at DESC LIMIT 1
                """,
                (project_id, workflow_id, workflow_revision),
            ).fetchone()
        if not row:
            self._revoke_grants(
                project_id=project_id,
                workflow_id=workflow_id,
                reason="workflow_revision_changed",
            )
            raise N8nAgentTaskError(
                "N8N_AGENT_BINDING_SCOPE_MISMATCH",
                "The workflow revision token is not active.",
                status_code=409,
            )
        return self._assert_live_binding_revision(dict(row))

    def authenticate_request(
        self, *, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> None:
        timestamp_raw = headers.get("x-n8n-timestamp") or headers.get("X-N8N-Timestamp")
        nonce = headers.get("x-n8n-nonce") or headers.get("X-N8N-Nonce") or ""
        signature = headers.get("x-n8n-signature") or headers.get("X-N8N-Signature") or ""
        profile = headers.get("x-n8n-profile") or headers.get("X-N8N-Profile") or ""
        if profile != HMAC_PROFILE or not timestamp_raw or not _NONCE_RE.fullmatch(nonce):
            raise N8nAgentTaskError(
                "N8N_AGENT_AUTHENTICATION_FAILED", "Invalid n8n authentication headers.", status_code=401
            )
        try:
            timestamp = int(timestamp_raw)
            request_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (TypeError, ValueError, OSError) as exc:
            raise N8nAgentTaskError(
                "N8N_AGENT_AUTHENTICATION_FAILED", "Invalid n8n request timestamp.", status_code=401
            ) from exc
        now = self._now()
        if abs((now - request_time).total_seconds()) > self._max_clock_skew:
            raise N8nAgentTaskError("N8N_AGENT_REQUEST_EXPIRED", "The signed request expired.", status_code=401)
        body_sha = hashlib.sha256(body).hexdigest()
        canonical = f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_sha}".encode("utf-8")
        try:
            secret = bytes(self._hmac_secret_provider())
        except Exception as exc:
            raise N8nAgentTaskError(
                "N8N_AGENT_AUTH_UNAVAILABLE", "Agent bridge authentication is unavailable.", status_code=503
            ) from exc
        if len(secret) < 32:
            raise N8nAgentTaskError(
                "N8N_AGENT_AUTH_UNAVAILABLE", "Agent bridge authentication is unavailable.", status_code=503
            )
        expected = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
        supplied = signature.removeprefix("sha256=").casefold()
        if len(supplied) != 64 or not hmac.compare_digest(expected, supplied):
            raise N8nAgentTaskError(
                "N8N_AGENT_AUTHENTICATION_FAILED", "Invalid n8n request signature.", status_code=401
            )
        expires_at = _iso(request_time + timedelta(seconds=self._max_clock_skew))
        with database.get_db_conn() as conn:
            conn.execute("DELETE FROM n8n_agent_task_nonces WHERE expires_at < ?", (_iso(now),))
            try:
                conn.execute(
                    """
                    INSERT INTO n8n_agent_task_nonces(
                        profile,nonce,method,path,request_timestamp,request_sha256,expires_at,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (HMAC_PROFILE, nonce, method.upper(), path, timestamp, body_sha, expires_at, _iso(now)),
                )
            except Exception as exc:
                # SQLite's unique constraint is the replay boundary.  Do not
                # include the nonce or signature in the error.
                if exc.__class__.__name__ == "IntegrityError":
                    raise N8nAgentTaskError(
                        "N8N_AGENT_REPLAY_DETECTED", "This signed request was already used.", status_code=409
                    ) from exc
                raise

    def _binding_config(self, row: Mapping[str, Any]) -> dict[str, Any]:
        aad = f"n8n-agent-binding:{row['project_id']}:{row['agent_binding_id']}:{row['config_digest']}"
        try:
            value = _loads(self.cipher.decrypt_text(str(row["config_envelope"]), aad=aad), {})
        except Exception as exc:
            raise N8nAgentTaskError(
                "N8N_AGENT_BINDING_UNAVAILABLE", "The Agent binding cannot be decrypted.", status_code=503
            ) from exc
        if not isinstance(value, Mapping):
            raise N8nAgentTaskError("N8N_AGENT_BINDING_UNAVAILABLE", "The Agent binding is invalid.", status_code=503)
        return dict(value)

    def _normalize_skills(self, project_id: str, value: Any, *, include_instructions: bool) -> list[dict[str, Any]]:
        if value is None:
            value = []
        if not isinstance(value, list) or len(value) > _MAX_SKILLS:
            raise N8nAgentTaskError("N8N_AGENT_SKILLS_INVALID", "The Agent binding Skills are invalid.", status_code=422)
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, Mapping):
                raise N8nAgentTaskError("N8N_AGENT_SKILLS_INVALID", "The Agent binding Skills are invalid.", status_code=422)
            slug = str(item.get("slug") or "")
            sha = str(item.get("sha256") or "")
            if not _SKILL_RE.fullmatch(slug) or not _SHA_RE.fullmatch(sha) or slug in seen:
                raise N8nAgentTaskError("N8N_AGENT_SKILLS_INVALID", "The Agent binding Skills are invalid.", status_code=422)
            seen.add(slug)
            entry: dict[str, Any] = {"slug": slug, "sha256": sha}
            if include_instructions:
                if self._skill_resolver is None:
                    raise N8nAgentTaskError(
                        "N8N_AGENT_SKILL_RESOLVER_UNAVAILABLE", "Project Skills cannot be resolved.", status_code=503
                    )
                try:
                    resolved = self._skill_resolver(project_id, slug, sha)
                except Exception as exc:
                    raise N8nAgentTaskError(
                        "N8N_AGENT_SKILL_STALE", "A bound Project Skill is unavailable or changed.", status_code=409
                    ) from exc
                if not isinstance(resolved, Mapping) or resolved.get("sha256") != sha:
                    raise N8nAgentTaskError(
                        "N8N_AGENT_SKILL_STALE", "A bound Project Skill is unavailable or changed.", status_code=409
                    )
                instructions = str(resolved.get("instructions") or "")
                if not instructions or len(instructions) > 20_000:
                    raise N8nAgentTaskError("N8N_AGENT_SKILL_STALE", "A bound Project Skill is invalid.", status_code=409)
                entry["instructions"] = instructions
            result.append(entry)
        return result

    def create_binding(self, value: Mapping[str, Any]) -> dict[str, Any]:
        project_id = _safe_id(value.get("project_id"), "project_id")
        workflow_id = _safe_id(value.get("workflow_id"), "workflow_id")
        workflow_revision = _safe_id(value.get("workflow_revision"), "workflow_revision")
        node_id = _safe_id(value.get("node_id"), "node_id")
        bound_project, _ = self._workflow_project(workflow_id)
        if bound_project != project_id:
            raise N8nAgentTaskError("N8N_WORKFLOW_SCOPE_MISMATCH", "The workflow is not available in this Project.", status_code=404)
        instruction = str(value.get("instruction") or "").strip()
        model = str(value.get("model") or "").strip()
        if not instruction or len(instruction) > _MAX_INSTRUCTION_CHARS or not model or len(model) > 255:
            raise N8nAgentTaskError("N8N_AGENT_BINDING_INVALID", "Instruction and model are required.", status_code=422)
        schema = value.get("output_schema")
        if not isinstance(schema, Mapping):
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_SCHEMA_INVALID", "An output schema is required.", status_code=422)
        schema = _bounded_json(schema, 32_000, reject_secrets=True)
        # Validate the schema shape now; actual values are checked per task.
        if schema.get("type") not in {"object", "array", "string", "number", "integer", "boolean", "null"}:
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_SCHEMA_INVALID", "The output schema type is unsupported.", status_code=422)
        skills = self._normalize_skills(project_id, value.get("skills"), include_instructions=False)
        config = {"instruction": instruction, "model": model, "output_schema": schema, "skills": skills}
        config = _bounded_json(config, _MAX_CONFIG_BYTES, reject_secrets=True)
        config_digest = _digest(
            {
                "workflow_id": workflow_id, "workflow_revision": workflow_revision,
                "node_id": node_id, "instruction_sha256": _digest(instruction),
                "model": model, "output_schema": schema, "skills": skills,
            }
        )
        binding_id = self._id_factory("nab")
        now = _iso(self._now())
        aad = f"n8n-agent-binding:{project_id}:{binding_id}:{config_digest}"
        envelope = self.cipher.encrypt_text(_canonical(config), aad=aad)
        try:
            with database.get_db_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO n8n_agent_task_bindings(
                        agent_binding_id,project_id,workflow_id,workflow_revision,node_id,
                        config_envelope,config_digest,active,revision,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,0,1,?,?)
                    """,
                    (binding_id, project_id, workflow_id, workflow_revision, node_id, envelope, config_digest, now, now),
                )
        except Exception as exc:
            if exc.__class__.__name__ == "IntegrityError":
                raise N8nAgentTaskError(
                    "N8N_AGENT_BINDING_CONFLICT", "This workflow node already has an Agent binding.", status_code=409
                ) from exc
            raise
        return self.get_binding(binding_id, project_id=project_id)

    def _normalized_binding_config(self, project_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        instruction = str(value.get("instruction") or "").strip()
        model = str(value.get("model") or "").strip()
        if not instruction or len(instruction) > _MAX_INSTRUCTION_CHARS or not model or len(model) > 255:
            raise N8nAgentTaskError("N8N_AGENT_BINDING_INVALID", "Instruction and model are required.", status_code=422)
        schema = value.get("output_schema")
        if not isinstance(schema, Mapping):
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_SCHEMA_INVALID", "An output schema is required.", status_code=422)
        schema = _bounded_json(schema, 32_000, reject_secrets=True)
        if schema.get("type") not in {"object", "array", "string", "number", "integer", "boolean", "null"}:
            raise N8nAgentTaskError("N8N_AGENT_OUTPUT_SCHEMA_INVALID", "The output schema type is unsupported.", status_code=422)
        skills = self._normalize_skills(project_id, value.get("skills"), include_instructions=False)
        return _bounded_json(
            {"instruction": instruction, "model": model, "output_schema": schema, "skills": skills},
            _MAX_CONFIG_BYTES,
            reject_secrets=True,
        )

    def binding_resolver(
        self, type_name: str, safe_node: Mapping[str, Any], context: Mapping[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Reserve an opaque binding while the semantic graph is compiled.

        The returned id is safe to place in the n8n Execute Sub-workflow node.
        It does not become executable until :meth:`finalize_bindings` atomically
        binds it to the broker-created workflow revision and exact n8n node id.
        """

        normalized_type = re.sub(r"[^a-z0-9]+", "", str(type_name or "").casefold())
        if normalized_type in {
            "workbenchapproval", "workbenchapprovalbridge", "approvalbridge"
        }:
            return self._approval_binding_resolver(safe_node, context)
        if normalized_type not in {"workbenchagent", "workbenchagentbridge", "agentbridge"}:
            return None
        if not isinstance(safe_node, Mapping) or not isinstance(context, Mapping):
            raise N8nAgentTaskError("N8N_AGENT_BINDING_INVALID", "The Agent node binding is invalid.", status_code=422)
        project_id = _safe_id(context.get("project_id"), "project_id")
        self._project(project_id)
        workflow_revision = _safe_id(
            context.get("_workbench_revision_token"), "workflow_revision"
        )
        if not workflow_revision.startswith("wbr_"):
            raise N8nAgentTaskError(
                "N8N_AGENT_BINDING_INVALID",
                "The server workflow revision token is invalid.",
                status_code=422,
            )
        session_id = str(context.get("session_id") or "").strip() or None
        if session_id:
            session = database.get_session(session_id)
            if not session or session.get("project_id") != project_id or bool(session.get("archived")):
                raise N8nAgentTaskError("SESSION_SCOPE_MISMATCH", "The Session does not belong to this Project.", status_code=409)
        node_id = _safe_id(
            safe_node.get("id") or safe_node.get("node_id") or safe_node.get("key"), "node_id"
        )
        config_source = safe_node.get("agent")
        if not isinstance(config_source, Mapping):
            parameters = safe_node.get("parameters")
            config_source = parameters if isinstance(parameters, Mapping) else safe_node
        config = self._normalized_binding_config(project_id, config_source)
        claim_id = self._id_factory("nabc")
        binding_id = self._id_factory("nab")
        config_digest = _digest(
            {
                "project_id": project_id,
                "session_id": session_id,
                "proposed_node_id": node_id,
                "workflow_revision": workflow_revision,
                "instruction_sha256": _digest(config["instruction"]),
                "model": config["model"],
                "output_schema": config["output_schema"],
                "skills": config["skills"],
            }
        )
        aad = f"n8n-agent-binding-claim:{project_id}:{claim_id}:{config_digest}"
        envelope = self.cipher.encrypt_text(_canonical(config), aad=aad)
        now = self._now()
        with database.get_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO n8n_agent_binding_claims(
                    claim_id,agent_binding_id,project_id,session_id,proposed_node_id,
                    workflow_revision,config_envelope,config_digest,status,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?,'provisional',?,?)
                """,
                (
                    claim_id, binding_id, project_id, session_id, node_id,
                    workflow_revision, envelope, config_digest, _iso(now),
                    _iso(now + timedelta(hours=1)),
                ),
            )
        return {
            "binding_claim_id": claim_id,
            "agent_binding_id": binding_id,
            "workflow_revision": workflow_revision,
            "node_parameters": {"agent_binding_id": binding_id},
            "output_schema": config["output_schema"],
            "output_schema_digest": _digest(config["output_schema"]),
            "config_digest": config_digest,
        }

    @staticmethod
    def _normalized_approval_manifest(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise N8nAgentTaskError(
                "N8N_APPROVAL_MANIFEST_INVALID",
                "The server approval action manifest is invalid.",
                status_code=422,
            )
        required = {
            "schema", "approval_node_id", "downstream_node_id",
            "downstream_node_type", "credential_alias", "credential_type",
            "target_kind", "target_rule", "action", "operation",
        }
        if set(value) != required or value.get("schema") != "approval_action_manifest.v1":
            raise N8nAgentTaskError(
                "N8N_APPROVAL_MANIFEST_INVALID",
                "The server approval action manifest shape is invalid.",
                status_code=422,
            )
        approval_node_id = _safe_id(value.get("approval_node_id"), "approval_node_id")
        downstream_node_id = _safe_id(value.get("downstream_node_id"), "downstream_node_id")
        node_type = str(value.get("downstream_node_type") or "")
        alias = str(value.get("credential_alias") or "")
        credential_type = str(value.get("credential_type") or "")
        target_kind = str(value.get("target_kind") or "")
        action = str(value.get("action") or "")
        operation = str(value.get("operation") or "")
        if (
            not node_type
            or len(node_type) > 255
            or not _ALIAS_RE.fullmatch(alias)
            or not credential_type
            or len(credential_type) > 128
            or target_kind not in {"email", "url", "service"}
            or action not in EXTERNAL_ACTIONS
            or len(operation) > 64
        ):
            raise N8nAgentTaskError(
                "N8N_APPROVAL_MANIFEST_INVALID",
                "The server approval action manifest scope is invalid.",
                status_code=422,
            )
        target_rule = value.get("target_rule")
        if not isinstance(target_rule, Mapping):
            raise N8nAgentTaskError(
                "N8N_APPROVAL_MANIFEST_INVALID",
                "The server approval target rule is invalid.",
                status_code=422,
            )
        if target_rule.get("mode") == "static":
            target_value = str(target_rule.get("value") or "")
            if set(target_rule) != {"mode", "value"} or not target_value or len(target_value) > 2048:
                raise N8nAgentTaskError(
                    "N8N_APPROVAL_MANIFEST_INVALID",
                    "The static approval target is invalid.",
                    status_code=422,
                )
            normalized_target = {"mode": "static", "value": target_value}
        elif target_rule.get("mode") == "json_field":
            field = str(target_rule.get("field") or "")
            if set(target_rule) != {"mode", "field"} or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]{0,127}", field
            ):
                raise N8nAgentTaskError(
                    "N8N_APPROVAL_MANIFEST_INVALID",
                    "The dynamic approval target selector is invalid.",
                    status_code=422,
                )
            normalized_target = {"mode": "json_field", "field": field}
        else:
            raise N8nAgentTaskError(
                "N8N_APPROVAL_MANIFEST_INVALID",
                "The server approval target rule is unsupported.",
                status_code=422,
            )
        return _bounded_json(
            {
                "schema": "approval_action_manifest.v1",
                "approval_node_id": approval_node_id,
                "downstream_node_id": downstream_node_id,
                "downstream_node_type": node_type,
                "credential_alias": alias,
                "credential_type": credential_type,
                "target_kind": target_kind,
                "target_rule": normalized_target,
                "action": action,
                "operation": operation,
            },
            16_000,
            reject_secrets=True,
        )

    def _approval_binding_resolver(
        self, safe_node: Mapping[str, Any], context: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(safe_node, Mapping) or not isinstance(context, Mapping):
            raise N8nAgentTaskError(
                "N8N_APPROVAL_MANIFEST_INVALID",
                "The approval action binding is invalid.",
                status_code=422,
            )
        project_id = _safe_id(context.get("project_id"), "project_id")
        self._project(project_id)
        workflow_revision = _safe_id(
            context.get("_workbench_revision_token"), "workflow_revision"
        )
        if not workflow_revision.startswith("wbr_"):
            raise N8nAgentTaskError(
                "N8N_APPROVAL_MANIFEST_INVALID",
                "The server workflow revision token is invalid.",
                status_code=422,
            )
        session_id = str(context.get("session_id") or "").strip() or None
        if session_id:
            session = database.get_session(session_id)
            if not session or session.get("project_id") != project_id or bool(session.get("archived")):
                raise N8nAgentTaskError(
                    "SESSION_SCOPE_MISMATCH",
                    "The Session does not belong to this Project.",
                    status_code=409,
                )
        node_key = _safe_id(
            safe_node.get("key") or safe_node.get("id") or safe_node.get("node_id"),
            "node_id",
        )
        manifests = context.get("_approval_action_manifests")
        manifest = self._normalized_approval_manifest(
            manifests.get(node_key) if isinstance(manifests, Mapping) else None
        )
        claim_id = self._id_factory("nabc")
        binding_id = self._id_factory("wba")
        manifest_digest = _digest(manifest)
        aad = f"n8n-approval-manifest-claim:{project_id}:{claim_id}:{manifest_digest}"
        envelope = self.cipher.encrypt_text(_canonical(manifest), aad=aad)
        now = self._now()
        with database.get_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO n8n_agent_approval_manifest_claims(
                    claim_id,approval_binding_id,project_id,session_id,proposed_node_id,
                    workflow_revision,manifest_envelope,manifest_digest,status,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?,'provisional',?,?)
                """,
                (
                    claim_id, binding_id, project_id, session_id, node_key,
                    workflow_revision, envelope, manifest_digest, _iso(now),
                    _iso(now + timedelta(hours=1)),
                ),
            )
        return {
            "binding_claim_id": claim_id,
            "approval_binding_id": binding_id,
            "workflow_revision": workflow_revision,
            "manifest_digest": manifest_digest,
            "node_id": manifest["approval_node_id"],
        }

    def finalize_bindings(
        self,
        workflow_id: str,
        workflow_revision: str,
        binding_claims: Sequence[Any],
        project_id: str,
        session_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Consume compiler claims after n8n returns the created workflow id."""

        workflow_id = _safe_id(workflow_id, "workflow_id")
        n8n_draft_revision = _safe_id(workflow_revision, "workflow_revision")
        project_id = _safe_id(project_id, "project_id")
        bound_project, _ = self._workflow_project(workflow_id)
        if bound_project != project_id:
            raise N8nAgentTaskError("N8N_WORKFLOW_SCOPE_MISMATCH", "The workflow is not available in this Project.", status_code=404)
        if session_id:
            session = database.get_session(session_id)
            if not session or session.get("project_id") != project_id or bool(session.get("archived")):
                raise N8nAgentTaskError("SESSION_SCOPE_MISMATCH", "The Session does not belong to this Project.", status_code=409)
        if not isinstance(binding_claims, Sequence) or isinstance(binding_claims, (str, bytes)) or len(binding_claims) > 64:
            raise N8nAgentTaskError("N8N_AGENT_BINDING_INVALID", "Binding claims are invalid.", status_code=422)
        requested: list[
            tuple[str, str, Optional[str], Optional[str], Optional[str]]
        ] = []
        for item in binding_claims:
            if isinstance(item, str):
                requested.append(
                    ("workbench.agent", _safe_id(item, "binding_claim_id"), None, None, None)
                )
            elif isinstance(item, Mapping):
                kind = str(item.get("kind") or "workbench.agent")
                if kind not in {"workbench.agent", "workbench.approval"}:
                    raise N8nAgentTaskError(
                        "N8N_AGENT_BINDING_INVALID",
                        "Binding claim kind is invalid.",
                        status_code=422,
                    )
                requested.append(
                    (
                        kind,
                        _safe_id(item.get("binding_claim_id"), "binding_claim_id"),
                        _safe_id(item.get("node_id"), "node_id") if item.get("node_id") else None,
                        _safe_id(item.get("workflow_revision"), "workflow_revision")
                        if item.get("workflow_revision") else None,
                        _sha256(item.get("manifest_digest"), "manifest_digest")
                        if item.get("manifest_digest") else None,
                    )
                )
            else:
                raise N8nAgentTaskError("N8N_AGENT_BINDING_INVALID", "Binding claims are invalid.", status_code=422)
        now = self._now()
        prepared: list[tuple[dict[str, Any], str, str]] = []
        prepared_approvals: list[tuple[dict[str, Any], str, str]] = []
        with database.get_db_conn() as conn:
            for kind, claim_id, final_node_id, requested_revision, requested_digest in requested:
                if kind == "workbench.approval":
                    row = conn.execute(
                        "SELECT * FROM n8n_agent_approval_manifest_claims WHERE claim_id=? AND project_id=?",
                        (claim_id, project_id),
                    ).fetchone()
                    if (
                        not row or row["status"] != "provisional"
                        or (_parse_time(row["expires_at"]) or now) <= now
                        or (session_id or None) != (row["session_id"] or None)
                    ):
                        raise N8nAgentTaskError(
                            "N8N_APPROVAL_MANIFEST_CLAIM_INVALID",
                            "An approval manifest claim is missing, expired, or already used.",
                            status_code=409,
                        )
                    claim = dict(row)
                    compiled_revision = _safe_id(
                        claim.get("workflow_revision"), "workflow_revision"
                    )
                    if (
                        not compiled_revision.startswith("wbr_")
                        or (
                            requested_revision is not None
                            and not hmac.compare_digest(compiled_revision, requested_revision)
                        )
                        or (
                            requested_digest is not None
                            and not hmac.compare_digest(
                                str(claim["manifest_digest"]), requested_digest
                            )
                        )
                    ):
                        raise N8nAgentTaskError(
                            "N8N_APPROVAL_MANIFEST_CLAIM_INVALID",
                            "The compiled approval manifest changed.",
                            status_code=409,
                        )
                    claim_aad = (
                        f"n8n-approval-manifest-claim:{project_id}:{claim_id}:"
                        f"{claim['manifest_digest']}"
                    )
                    manifest_text = self.cipher.decrypt_text(
                        claim["manifest_envelope"], aad=claim_aad
                    )
                    manifest = self._normalized_approval_manifest(
                        _loads(manifest_text, None)
                    )
                    if not hmac.compare_digest(
                        _digest(manifest), str(claim["manifest_digest"])
                    ):
                        raise N8nAgentTaskError(
                            "N8N_APPROVAL_MANIFEST_CLAIM_INVALID",
                            "The stored approval manifest failed verification.",
                            status_code=409,
                        )
                    node_id = final_node_id or str(claim["proposed_node_id"])
                    if not hmac.compare_digest(
                        str(manifest["approval_node_id"]), node_id
                    ):
                        raise N8nAgentTaskError(
                            "N8N_APPROVAL_MANIFEST_CLAIM_INVALID",
                            "The approval node identity changed during reconciliation.",
                            status_code=409,
                        )
                    final_aad = (
                        f"n8n-approval-manifest:{project_id}:"
                        f"{claim['approval_binding_id']}:{claim['manifest_digest']}"
                    )
                    final_envelope = self.cipher.encrypt_text(
                        _canonical(manifest), aad=final_aad
                    )
                    prepared_approvals.append((claim, node_id, final_envelope))
                    continue
                row = conn.execute(
                    "SELECT * FROM n8n_agent_binding_claims WHERE claim_id=? AND project_id=?",
                    (claim_id, project_id),
                ).fetchone()
                if (
                    not row or row["status"] != "provisional"
                    or (_parse_time(row["expires_at"]) or now) <= now
                    or (session_id or None) != (row["session_id"] or None)
                ):
                    raise N8nAgentTaskError("N8N_AGENT_BINDING_CLAIM_INVALID", "A binding claim is missing, expired, or already used.", status_code=409)
                claim = dict(row)
                compiled_revision = _safe_id(
                    claim.get("workflow_revision"), "workflow_revision"
                )
                if (
                    not compiled_revision.startswith("wbr_")
                    or (
                        requested_revision is not None
                        and not hmac.compare_digest(
                            compiled_revision, requested_revision
                        )
                    )
                ):
                    raise N8nAgentTaskError(
                        "N8N_AGENT_BINDING_CLAIM_INVALID",
                        "The compiled workflow revision token changed.",
                        status_code=409,
                    )
                claim_aad = f"n8n-agent-binding-claim:{project_id}:{claim_id}:{claim['config_digest']}"
                config = self.cipher.decrypt_text(claim["config_envelope"], aad=claim_aad)
                node_id = final_node_id or str(claim["proposed_node_id"])
                final_digest = _digest(
                    {
                        "workflow_id": workflow_id,
                        "workflow_revision": compiled_revision,
                        "n8n_draft_revision": n8n_draft_revision,
                        "node_id": node_id, "claim_config_digest": claim["config_digest"],
                    }
                )
                final_aad = f"n8n-agent-binding:{project_id}:{claim['agent_binding_id']}:{final_digest}"
                final_envelope = self.cipher.encrypt_text(config, aad=final_aad)
                prepared.append((claim, node_id, final_envelope + "\n" + final_digest))
            for claim, node_id, packed in prepared:
                final_envelope, final_digest = packed.rsplit("\n", 1)
                conn.execute(
                    """
                    INSERT INTO n8n_agent_task_bindings(
                        agent_binding_id,project_id,workflow_id,workflow_revision,node_id,
                        config_envelope,config_digest,active,revision,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,0,1,?,?)
                    """,
                    (
                        claim["agent_binding_id"], project_id, workflow_id,
                        claim["workflow_revision"],
                        node_id, final_envelope, final_digest, _iso(now), _iso(now),
                    ),
                )
                conn.execute(
                    "UPDATE n8n_agent_binding_claims SET status='consumed',consumed_at=? WHERE claim_id=? AND status='provisional'",
                    (_iso(now), claim["claim_id"]),
                )
            for claim, node_id, final_envelope in prepared_approvals:
                active_binding = conn.execute(
                    """
                    SELECT active_version_id FROM n8n_agent_task_bindings
                     WHERE project_id=? AND workflow_id=? AND workflow_revision=?
                       AND active=1 AND active_version_id IS NOT NULL
                     ORDER BY updated_at DESC LIMIT 1
                    """,
                    (project_id, workflow_id, claim["workflow_revision"]),
                ).fetchone()
                active_version_id = (
                    str(active_binding["active_version_id"])
                    if active_binding and active_binding["active_version_id"] else None
                )
                conn.execute(
                    """
                    INSERT INTO n8n_agent_approval_manifests(
                        approval_binding_id,project_id,workflow_id,workflow_revision,
                        active_version_id,node_id,manifest_envelope,manifest_digest,
                        active,revision,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,1,?,?)
                    """,
                    (
                        claim["approval_binding_id"], project_id, workflow_id,
                        claim["workflow_revision"], active_version_id, node_id,
                        final_envelope, claim["manifest_digest"],
                        1 if active_version_id else 0, _iso(now), _iso(now),
                    ),
                )
                conn.execute(
                    "UPDATE n8n_agent_approval_manifest_claims SET status='consumed',consumed_at=? WHERE claim_id=? AND status='provisional'",
                    (_iso(now), claim["claim_id"]),
                )
        finalized = [
            self.get_binding(claim["agent_binding_id"], project_id=project_id)
            for claim, _, _ in prepared
        ]
        finalized.extend(
            self.get_approval_manifest(
                claim["approval_binding_id"], project_id=project_id
            )
            for claim, _, _ in prepared_approvals
        )
        return finalized

    def get_approval_manifest(
        self, approval_binding_id: str, *, project_id: str
    ) -> dict[str, Any]:
        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT * FROM n8n_agent_approval_manifests WHERE approval_binding_id=? AND project_id=?",
                (
                    _safe_id(approval_binding_id, "approval_binding_id"),
                    _safe_id(project_id, "project_id"),
                ),
            ).fetchone()
        if not row:
            raise N8nAgentTaskError(
                "N8N_APPROVAL_MANIFEST_NOT_FOUND",
                "The approval action manifest was not found.",
                status_code=404,
            )
        return {
            "kind": "workbench.approval",
            "approval_binding_id": row["approval_binding_id"],
            "project_id": row["project_id"],
            "workflow_id": row["workflow_id"],
            "workflow_revision": row["workflow_revision"],
            "active_version_id": row["active_version_id"],
            "node_id": row["node_id"],
            "manifest_digest": row["manifest_digest"],
            "active": bool(row["active"]),
            "revision": int(row["revision"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def activate_bindings(
        self,
        workflow_id: str,
        workflow_revision: str,
        binding_ids: Sequence[str],
        project_id: str,
    ) -> list[dict[str, Any]]:
        """Enable exact bindings only after n8n publish/activate reconciliation."""

        workflow_id = _safe_id(workflow_id, "workflow_id")
        active_version_id = _safe_id(workflow_revision, "active_version_id")
        project_id = _safe_id(project_id, "project_id")
        bound_project, _ = self._workflow_project(workflow_id)
        if bound_project != project_id:
            raise N8nAgentTaskError(
                "N8N_WORKFLOW_SCOPE_MISMATCH", "The workflow is not available in this Project.", status_code=404
            )
        if (
            not isinstance(binding_ids, Sequence)
            or isinstance(binding_ids, (str, bytes))
            or not binding_ids
            or len(binding_ids) > 64
        ):
            raise N8nAgentTaskError("N8N_AGENT_BINDING_INVALID", "Binding ids are required.", status_code=422)
        ids = [_safe_id(item, "agent_binding_id") for item in binding_ids]
        if len(ids) != len(set(ids)):
            raise N8nAgentTaskError("N8N_AGENT_BINDING_INVALID", "Binding ids are invalid.", status_code=422)
        live_version_id = self._read_live_active_version(workflow_id)
        if not hmac.compare_digest(active_version_id, live_version_id):
            self._revision_drift(project_id, workflow_id)
            raise N8nAgentTaskError(
                "N8N_WORKFLOW_REVISION_CHANGED",
                "The active n8n workflow changed during activation.",
                status_code=409,
            )
        now = _iso(self._now())
        placeholders = ",".join("?" for _ in ids)
        with database.get_db_conn() as conn:
            rows = conn.execute(
                f"SELECT agent_binding_id,workflow_revision FROM n8n_agent_task_bindings WHERE project_id=? AND workflow_id=? AND agent_binding_id IN ({placeholders})",
                (project_id, workflow_id, *ids),
            ).fetchall()
            if {row["agent_binding_id"] for row in rows} != set(ids):
                raise N8nAgentTaskError("N8N_AGENT_BINDING_NOT_FOUND", "An Agent binding was not found.", status_code=404)
            revision_tokens = {str(row["workflow_revision"]) for row in rows}
            if len(revision_tokens) != 1 or not next(iter(revision_tokens)).startswith("wbr_"):
                raise N8nAgentTaskError(
                    "N8N_AGENT_BINDING_SCOPE_MISMATCH",
                    "Agent bindings do not belong to one compiled graph revision.",
                    status_code=409,
                )
            compiled_revision = next(iter(revision_tokens))
            conn.execute(
                "UPDATE n8n_agent_task_bindings SET active=0,revision=revision+1,updated_at=? WHERE project_id=? AND workflow_id=?",
                (now, project_id, workflow_id),
            )
            conn.execute(
                f"UPDATE n8n_agent_task_bindings SET active_version_id=?,active=1,revision=revision+1,updated_at=? WHERE project_id=? AND workflow_id=? AND agent_binding_id IN ({placeholders})",
                (active_version_id, now, project_id, workflow_id, *ids),
            )
            conn.execute(
                "UPDATE n8n_agent_approval_manifests SET active=0,revision=revision+1,updated_at=? WHERE project_id=? AND workflow_id=?",
                (now, project_id, workflow_id),
            )
            conn.execute(
                """
                UPDATE n8n_agent_approval_manifests
                   SET active_version_id=?,active=1,revision=revision+1,updated_at=?
                 WHERE project_id=? AND workflow_id=? AND workflow_revision=?
                """,
                (
                    active_version_id, now, project_id, workflow_id,
                    compiled_revision,
                ),
            )
        self.notify_workflow_changed(project_id, workflow_id, reason="workflow_version_activated")
        return [self.get_binding(item, project_id=project_id) for item in ids]

    def update_binding(self, binding_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        binding_id = _safe_id(binding_id, "agent_binding_id")
        project_id = _safe_id(value.get("project_id"), "project_id")
        expected_revision = value.get("expected_revision")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise N8nAgentTaskError("N8N_AGENT_BINDING_INVALID", "expected_revision is required.", status_code=422)
        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT * FROM n8n_agent_task_bindings WHERE agent_binding_id=? AND project_id=?",
                (binding_id, project_id),
            ).fetchone()
        if not row:
            raise N8nAgentTaskError("N8N_AGENT_BINDING_NOT_FOUND", "The Agent binding was not found.", status_code=404)
        merged = {
            **dict(value), "project_id": project_id, "workflow_id": row["workflow_id"],
            "node_id": row["node_id"],
        }
        workflow_revision = _safe_id(merged.get("workflow_revision"), "workflow_revision")
        instruction = str(merged.get("instruction") or "").strip()
        model = str(merged.get("model") or "").strip()
        schema = _bounded_json(merged.get("output_schema"), 32_000, reject_secrets=True)
        skills = self._normalize_skills(project_id, merged.get("skills"), include_instructions=False)
        if not instruction or len(instruction) > _MAX_INSTRUCTION_CHARS or not model or len(model) > 255:
            raise N8nAgentTaskError("N8N_AGENT_BINDING_INVALID", "Instruction and model are required.", status_code=422)
        config = _bounded_json(
            {"instruction": instruction, "model": model, "output_schema": schema, "skills": skills},
            _MAX_CONFIG_BYTES, reject_secrets=True,
        )
        config_digest = _digest(
            {
                "workflow_id": row["workflow_id"], "workflow_revision": workflow_revision,
                "node_id": row["node_id"], "instruction_sha256": _digest(instruction),
                "model": model, "output_schema": schema, "skills": skills,
            }
        )
        aad = f"n8n-agent-binding:{project_id}:{binding_id}:{config_digest}"
        envelope = self.cipher.encrypt_text(_canonical(config), aad=aad)
        now = _iso(self._now())
        with database.get_db_conn() as conn:
            changed = conn.execute(
                """
                UPDATE n8n_agent_task_bindings
                   SET workflow_revision=?,config_envelope=?,config_digest=?,active=0,
                       revision=revision+1,updated_at=?
                 WHERE agent_binding_id=? AND project_id=? AND revision=?
                """,
                (workflow_revision, envelope, config_digest, now, binding_id, project_id, expected_revision),
            )
        if changed.rowcount != 1:
            raise N8nAgentTaskError("N8N_AGENT_BINDING_STALE", "The Agent binding changed; refresh it first.", status_code=409)
        self.notify_workflow_changed(project_id, str(row["workflow_id"]), reason="workflow_binding_changed")
        return self.get_binding(binding_id, project_id=project_id)

    def get_binding(self, binding_id: str, *, project_id: str) -> dict[str, Any]:
        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT * FROM n8n_agent_task_bindings WHERE agent_binding_id=? AND project_id=?",
                (_safe_id(binding_id, "agent_binding_id"), _safe_id(project_id, "project_id")),
            ).fetchone()
        if not row:
            raise N8nAgentTaskError("N8N_AGENT_BINDING_NOT_FOUND", "The Agent binding was not found.", status_code=404)
        config = self._binding_config(dict(row))
        return {
            "agent_binding_id": row["agent_binding_id"], "project_id": row["project_id"],
            "workflow_id": row["workflow_id"], "workflow_revision": row["workflow_revision"],
            "node_id": row["node_id"], "model": config.get("model"),
            "skills": config.get("skills") or [], "output_schema_digest": _digest(config.get("output_schema") or {}),
            "config_digest": row["config_digest"], "active": bool(row["active"]),
            "revision": row["revision"], "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def list_bindings(self, project_id: str) -> list[dict[str, Any]]:
        self._project(project_id)
        with database.get_db_conn() as conn:
            rows = conn.execute(
                "SELECT agent_binding_id FROM n8n_agent_task_bindings WHERE project_id=? ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
        return [self.get_binding(row["agent_binding_id"], project_id=project_id) for row in rows]

    def deactivate_binding(self, binding_id: str, *, project_id: str) -> dict[str, Any]:
        binding = self.get_binding(binding_id, project_id=project_id)
        with database.get_db_conn() as conn:
            conn.execute(
                "UPDATE n8n_agent_task_bindings SET active=0,revision=revision+1,updated_at=? WHERE agent_binding_id=? AND project_id=?",
                (_iso(self._now()), binding_id, project_id),
            )
        self.notify_workflow_changed(project_id, binding["workflow_id"], reason="agent_binding_disabled")
        return self.get_binding(binding_id, project_id=project_id)

    def _resolve_task_binding(
        self, *, binding_id: str, workflow_id: str, workflow_revision: str, node_id: str, project_id: str
    ) -> tuple[Mapping[str, Any], dict[str, Any]]:
        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT * FROM n8n_agent_task_bindings WHERE agent_binding_id=? AND project_id=?",
                (binding_id, project_id),
            ).fetchone()
        if not row or not bool(row["active"]):
            raise N8nAgentTaskError("N8N_AGENT_BINDING_NOT_FOUND", "The Agent binding is unavailable.", status_code=404)
        if (
            row["workflow_id"] != workflow_id or row["workflow_revision"] != workflow_revision
            or row["node_id"] != node_id
        ):
            self._revoke_grants(project_id=project_id, workflow_id=workflow_id, reason="workflow_revision_changed")
            raise N8nAgentTaskError(
                "N8N_AGENT_BINDING_SCOPE_MISMATCH", "The Agent binding does not match this workflow revision and node.", status_code=409
            )
        self._assert_live_binding_revision(dict(row))
        config = self._binding_config(dict(row))
        config["skills"] = self._normalize_skills(project_id, config.get("skills"), include_instructions=True)
        return dict(row), config

    def submit_task(self, value: Mapping[str, Any]) -> dict[str, Any]:
        if "project_id" in value:
            raise N8nAgentTaskError(
                "N8N_AGENT_PROJECT_FORBIDDEN", "n8n cannot select a Workbench Project.", status_code=422
            )
        request_id = _safe_id(value.get("request_id"), "request_id")
        workflow_id = _safe_id(value.get("workflow_id"), "workflow_id")
        workflow_revision = _safe_id(value.get("workflow_revision"), "workflow_revision")
        node_id = _safe_id(value.get("node_id"), "node_id")
        binding_id = _safe_id(value.get("agent_binding_id"), "agent_binding_id")
        project_id, _ = self._workflow_project(workflow_id)
        self._require_execution_enabled(project_id)
        binding, config = self._resolve_task_binding(
            binding_id=binding_id, workflow_id=workflow_id, workflow_revision=workflow_revision,
            node_id=node_id, project_id=project_id,
        )
        input_value = _bounded_json(value.get("input"), _MAX_INPUT_BYTES, reject_secrets=True)
        input_sha = _digest(input_value)
        request_digest = _digest(
            {
                "request_id": request_id, "project_id": project_id, "workflow_id": workflow_id,
                "workflow_revision": workflow_revision, "node_id": node_id,
                "agent_binding_id": binding_id, "binding_digest": binding["config_digest"],
                "input_sha256": input_sha,
            }
        )
        with database.get_db_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM n8n_agent_tasks WHERE workflow_id=? AND request_id=?",
                (workflow_id, request_id),
            ).fetchone()
        if existing:
            if existing["request_digest"] != request_digest:
                raise N8nAgentTaskError(
                    "N8N_AGENT_TASK_CONFLICT", "The task request id was reused with different content.", status_code=409
                )
            result = self._task_public(dict(existing), include_result=False)
            result["idempotent"] = True
            return result
        task_id = self._id_factory("nat")
        now = _iso(self._now())
        input_aad = f"n8n-agent-task-input:{task_id}:{request_digest}"
        config_snapshot = {
            "instruction": config["instruction"], "model": config["model"],
            "output_schema": config["output_schema"], "skills": config["skills"],
        }
        config_snapshot = _bounded_json(config_snapshot, _MAX_CONFIG_BYTES, reject_secrets=True)
        config_aad = f"n8n-agent-task-config:{task_id}:{binding['config_digest']}"
        input_envelope = self.cipher.encrypt_text(_canonical(input_value), aad=input_aad)
        config_envelope = self.cipher.encrypt_text(_canonical(config_snapshot), aad=config_aad)
        with database.get_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO n8n_agent_tasks(
                    task_id,request_id,project_id,workflow_id,workflow_revision,node_id,
                    agent_binding_id,binding_digest,request_digest,input_envelope,config_envelope,
                    status,cancel_requested,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued',0,?,?)
                """,
                (
                    task_id, request_id, project_id, workflow_id, workflow_revision, node_id,
                    binding_id, binding["config_digest"], request_digest, input_envelope,
                    config_envelope, now, now,
                ),
            )
        result = self._task_public(self._task_row(task_id), include_result=False)
        result["idempotent"] = False
        return result

    def _task_row(self, task_id: str) -> dict[str, Any]:
        with database.get_db_conn() as conn:
            row = conn.execute("SELECT * FROM n8n_agent_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise N8nAgentTaskError("N8N_AGENT_TASK_NOT_FOUND", "The Agent task was not found.", status_code=404)
        return dict(row)

    def _task_public(self, row: Mapping[str, Any], *, include_result: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "task_id": row["task_id"], "request_id": row["request_id"], "status": row["status"],
            "request_digest": row["request_digest"], "result_sha256": row.get("output_sha256"),
            "error_code": row.get("error_code"), "created_at": row["created_at"],
            "updated_at": row["updated_at"], "completed_at": row.get("completed_at"),
        }
        if include_result and row["status"] == "succeeded" and row.get("output_envelope"):
            aad = f"n8n-agent-task-output:{row['task_id']}:{row['output_sha256']}"
            try:
                result["result"] = _loads(self.cipher.decrypt_text(str(row["output_envelope"]), aad=aad), None)
            except Exception as exc:
                raise N8nAgentTaskError(
                    "N8N_AGENT_RESULT_UNAVAILABLE", "The Agent task result cannot be decrypted.", status_code=503
                ) from exc
        return result

    def get_task_for_n8n(self, task_id: str, *, workflow_id: str) -> dict[str, Any]:
        row = self._task_row(_safe_id(task_id, "task_id"))
        if row["workflow_id"] != _safe_id(workflow_id, "workflow_id"):
            raise N8nAgentTaskError("N8N_AGENT_TASK_NOT_FOUND", "The Agent task was not found.", status_code=404)
        project_id, _ = self._workflow_project(workflow_id)
        self._assert_live_workflow_token(
            project_id=project_id,
            workflow_id=workflow_id,
            workflow_revision=str(row["workflow_revision"]),
        )
        return self._task_public(row, include_result=True)

    def get_task_public(self, task_id: str, *, project_id: str) -> dict[str, Any]:
        self._project(project_id)
        row = self._task_row(_safe_id(task_id, "task_id"))
        if row["project_id"] != project_id:
            raise N8nAgentTaskError("N8N_AGENT_TASK_NOT_FOUND", "The Agent task was not found.", status_code=404)
        return self._task_public(row, include_result=False)

    def list_tasks(self, project_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        self._project(project_id)
        limit = max(1, min(int(limit), 250))
        with database.get_db_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM n8n_agent_tasks WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [self._task_public(dict(row), include_result=False) for row in rows]

    def process_task(self, task_id: str) -> dict[str, Any]:
        task_id = _safe_id(task_id, "task_id")
        pending = self._task_row(task_id)
        self._require_execution_enabled(str(pending["project_id"]))
        now = _iso(self._now())
        with database.get_db_conn() as conn:
            claimed = conn.execute(
                """
                UPDATE n8n_agent_tasks SET status='generating',started_at=?,updated_at=?
                 WHERE task_id=? AND status='queued' AND cancel_requested=0
                """,
                (now, now, task_id),
            )
        if claimed.rowcount != 1:
            return self._task_public(self._task_row(task_id), include_result=False)
        row = self._task_row(task_id)
        try:
            self._assert_live_workflow_token(
                project_id=str(row["project_id"]),
                workflow_id=str(row["workflow_id"]),
                workflow_revision=str(row["workflow_revision"]),
            )
            input_aad = f"n8n-agent-task-input:{task_id}:{row['request_digest']}"
            config_aad = f"n8n-agent-task-config:{task_id}:{row['binding_digest']}"
            input_value = _loads(self.cipher.decrypt_text(row["input_envelope"], aad=input_aad), None)
            config = _loads(self.cipher.decrypt_text(row["config_envelope"], aad=config_aad), {})
            if not isinstance(config, Mapping):
                raise N8nAgentTaskError("N8N_AGENT_CONFIG_INVALID", "The Agent task configuration is invalid.", status_code=503)
            request = {
                "security": {
                    "tools": [], "external_actions": False,
                    "input_trust": "untrusted", "secrets_allowed": False,
                    "project_id": row["project_id"],
                },
                "trusted": {
                    "instruction": config.get("instruction"), "model": config.get("model"),
                    "output_schema": config.get("output_schema"), "skills": config.get("skills") or [],
                },
                "untrusted_input": input_value,
            }
            # Recheck immediately before model execution.  Approval, queueing,
            # or decryption time never carries extension authority forward.
            self._require_execution_enabled(str(row["project_id"]))
            output = self._generator(request)
            output = _bounded_json(output, _MAX_OUTPUT_BYTES, reject_secrets=True)
            schema = config.get("output_schema")
            if not isinstance(schema, Mapping):
                raise N8nAgentTaskError("N8N_AGENT_OUTPUT_SCHEMA_INVALID", "The output schema is invalid.", status_code=503)
            _validate_schema_value(output, schema)
            output_sha = _digest(output)
            output_aad = f"n8n-agent-task-output:{task_id}:{output_sha}"
            output_envelope = self.cipher.encrypt_text(_canonical(output), aad=output_aad)
            completed = _iso(self._now())
            with database.get_db_conn() as conn:
                current = conn.execute(
                    "SELECT status,cancel_requested FROM n8n_agent_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if not current or current["status"] == "cancelled" or bool(current["cancel_requested"]):
                    conn.execute(
                        "UPDATE n8n_agent_tasks SET status='cancelled',output_envelope=NULL,output_sha256=NULL,completed_at=?,updated_at=? WHERE task_id=?",
                        (completed, completed, task_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE n8n_agent_tasks SET status='succeeded',output_envelope=?,output_sha256=?,
                            error_code=NULL,completed_at=?,updated_at=? WHERE task_id=? AND status='generating'
                        """,
                        (output_envelope, output_sha, completed, completed, task_id),
                    )
        except Exception as exc:
            code = exc.code if isinstance(exc, N8nAgentTaskError) else "N8N_AGENT_GENERATION_FAILED"
            completed = _iso(self._now())
            with database.get_db_conn() as conn:
                current = conn.execute("SELECT cancel_requested FROM n8n_agent_tasks WHERE task_id=?", (task_id,)).fetchone()
                if current and bool(current["cancel_requested"]):
                    conn.execute(
                        "UPDATE n8n_agent_tasks SET status='cancelled',error_code=NULL,completed_at=?,updated_at=? WHERE task_id=?",
                        (completed, completed, task_id),
                    )
                else:
                    conn.execute(
                        "UPDATE n8n_agent_tasks SET status='generation_failed',error_code=?,completed_at=?,updated_at=? WHERE task_id=? AND status='generating'",
                        (str(code)[:128], completed, completed, task_id),
                    )
        return self._task_public(self._task_row(task_id), include_result=False)

    def process_next_task(self) -> Optional[dict[str, Any]]:
        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT task_id FROM n8n_agent_tasks WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
        return self.process_task(row["task_id"]) if row else None

    def recover_incomplete_tasks(self) -> int:
        now = _iso(self._now())
        with database.get_db_conn() as conn:
            changed = conn.execute(
                """
                UPDATE n8n_agent_tasks
                   SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'queued' END,
                       started_at=NULL,updated_at=?
                 WHERE status='generating'
                """,
                (now,),
            )
        return changed.rowcount

    def cancel_task(self, task_id: str, *, workflow_id: str) -> dict[str, Any]:
        row = self._task_row(_safe_id(task_id, "task_id"))
        workflow_id = _safe_id(workflow_id, "workflow_id")
        if row["workflow_id"] != workflow_id:
            raise N8nAgentTaskError("N8N_AGENT_TASK_NOT_FOUND", "The Agent task was not found.", status_code=404)
        self._workflow_project(workflow_id)
        now = _iso(self._now())
        with database.get_db_conn() as conn:
            conn.execute(
                """
                UPDATE n8n_agent_tasks SET cancel_requested=1,
                    status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
                    completed_at=CASE WHEN status='queued' THEN ? ELSE completed_at END,updated_at=?
                 WHERE task_id=? AND status IN ('queued','generating')
                """,
                (now, now, task_id),
            )
        return self._task_public(self._task_row(task_id), include_result=False)

    def adopt_credential_alias(self, value: Mapping[str, Any]) -> dict[str, Any]:
        project_id = _safe_id(value.get("project_id"), "project_id")
        self._project(project_id)
        alias = str(value.get("alias") or "")
        credential_id = str(value.get("credential_id") or "").strip()
        if not _ALIAS_RE.fullmatch(alias) or not credential_id or len(credential_id) > 255:
            raise N8nAgentTaskError("N8N_CREDENTIAL_ALIAS_INVALID", "The credential alias is invalid.", status_code=422)
        if self._credential_resolver is None:
            raise N8nAgentTaskError(
                "N8N_CREDENTIAL_RESOLVER_UNAVAILABLE", "Credential metadata cannot be verified.", status_code=503
            )
        try:
            metadata = self._credential_resolver(credential_id)
        except Exception as exc:
            raise N8nAgentTaskError("N8N_CREDENTIAL_NOT_FOUND", "The n8n credential was not found.", status_code=404) from exc
        if not isinstance(metadata, Mapping):
            raise N8nAgentTaskError("N8N_CREDENTIAL_NOT_FOUND", "The n8n credential was not found.", status_code=404)
        credential_type = str(metadata.get("type") or "")[:128]
        display_name = str(metadata.get("name") or alias)[:255]
        status = str(metadata.get("status") or "unknown").casefold()
        if status not in CREDENTIAL_STATES:
            status = "unknown"
        if not credential_type or _contains_secret_key(metadata):
            raise N8nAgentTaskError("N8N_CREDENTIAL_METADATA_INVALID", "Credential metadata is invalid.", status_code=422)
        digest = _digest({"credential_id_sha256": _digest(credential_id), "type": credential_type, "name": display_name, "status": status})
        aad = f"n8n-agent-credential:{project_id}:{alias}:{digest}"
        envelope = self.cipher.encrypt_text(credential_id, aad=aad)
        now = _iso(self._now())
        with database.get_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO n8n_agent_credential_aliases(
                    project_id,alias,credential_ref_envelope,credential_type,display_name,status,
                    metadata_digest,revision,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,1,?,?)
                ON CONFLICT(project_id,alias) DO UPDATE SET
                    credential_ref_envelope=excluded.credential_ref_envelope,
                    credential_type=excluded.credential_type,display_name=excluded.display_name,
                    status=excluded.status,metadata_digest=excluded.metadata_digest,
                    revision=n8n_agent_credential_aliases.revision+1,updated_at=excluded.updated_at
                """,
                (project_id, alias, envelope, credential_type, display_name, status, digest, now, now),
            )
        self._revoke_grants(project_id=project_id, credential_alias=alias, reason="credential_alias_changed")
        return self.get_credential_alias(project_id, alias)

    @staticmethod
    def _credential_public(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "project_id": row["project_id"], "alias": row["alias"],
            "credential_type": row["credential_type"], "display_name": row["display_name"],
            "status": row["status"], "metadata_digest": row["metadata_digest"],
            "revision": row["revision"], "updated_at": row["updated_at"],
        }

    def get_credential_alias(self, project_id: str, alias: str) -> dict[str, Any]:
        self._project(project_id)
        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT * FROM n8n_agent_credential_aliases WHERE project_id=? AND alias=?",
                (project_id, alias),
            ).fetchone()
        if not row:
            raise N8nAgentTaskError("N8N_CREDENTIAL_ALIAS_NOT_FOUND", "The credential alias was not found.", status_code=404)
        return self._credential_public(dict(row))

    def list_credential_aliases(self, project_id: str) -> list[dict[str, Any]]:
        self._project(project_id)
        with database.get_db_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM n8n_agent_credential_aliases WHERE project_id=? ORDER BY alias", (project_id,)
            ).fetchall()
        return [self._credential_public(dict(row)) for row in rows]

    def resolve_credential_alias(self, project_id: str, alias: str) -> str:
        public = self.get_credential_alias(project_id, alias)
        if public["status"] != "ready":
            raise N8nAgentTaskError("N8N_CREDENTIAL_NOT_READY", "The credential alias is not ready.", status_code=409)
        return self._credential_reference(project_id, alias)

    def _credential_reference(self, project_id: str, alias: str) -> str:
        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT * FROM n8n_agent_credential_aliases WHERE project_id=? AND alias=?", (project_id, alias)
            ).fetchone()
        if not row:
            raise N8nAgentTaskError("N8N_CREDENTIAL_ALIAS_NOT_FOUND", "The credential alias was not found.", status_code=404)
        aad = f"n8n-agent-credential:{project_id}:{alias}:{row['metadata_digest']}"
        return self.cipher.decrypt_text(row["credential_ref_envelope"], aad=aad)

    def credential_alias_resolver(
        self, project_id: str, alias: str, expected_type: Optional[str] = None
    ) -> dict[str, Any]:
        """Internal graph-compiler resolver; never expose this result in a DTO."""

        public = self.get_credential_alias(project_id, alias)
        if expected_type and public["credential_type"] != expected_type:
            raise N8nAgentTaskError(
                "N8N_CREDENTIAL_TYPE_MISMATCH", "The credential alias has the wrong type.", status_code=409
            )
        credential_id = self.resolve_credential_alias(project_id, alias)
        return {
            "id": credential_id,
            "name": public["display_name"],
            "type": public["credential_type"],
            "alias": public["alias"],
            "metadata_digest": public["metadata_digest"],
        }

    def refresh_credential_alias(self, project_id: str, alias: str) -> dict[str, Any]:
        self.get_credential_alias(project_id, alias)
        credential_id = self._credential_reference(project_id, alias)
        return self.adopt_credential_alias({"project_id": project_id, "alias": alias, "credential_id": credential_id})

    def revoke_credential_alias(self, project_id: str, alias: str) -> dict[str, Any]:
        current = self.get_credential_alias(project_id, alias)
        now = _iso(self._now())
        with database.get_db_conn() as conn:
            conn.execute(
                "UPDATE n8n_agent_credential_aliases SET status='revoked',revision=revision+1,updated_at=? WHERE project_id=? AND alias=?",
                (now, project_id, alias),
            )
        self._revoke_grants(project_id=project_id, credential_alias=alias, reason="credential_alias_revoked")
        return {**current, **self.get_credential_alias(project_id, alias)}

    def _policy_epoch(self, project_id: str) -> int:
        now = _iso(self._now())
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO n8n_agent_runtime_policy_epochs(project_id,epoch,updated_at) VALUES(?,1,?) ON CONFLICT(project_id) DO NOTHING",
                (project_id, now),
            )
            row = conn.execute(
                "SELECT epoch FROM n8n_agent_runtime_policy_epochs WHERE project_id=?", (project_id,)
            ).fetchone()
        return int(row["epoch"])

    def notify_policy_changed(self, project_id: str, *, reason: str = "policy_changed") -> int:
        self._project(project_id)
        now = _iso(self._now())
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO n8n_agent_runtime_policy_epochs(project_id,epoch,updated_at) VALUES(?,1,?) ON CONFLICT(project_id) DO UPDATE SET epoch=epoch+1,updated_at=excluded.updated_at",
                (project_id, now),
            )
        return self._revoke_grants(project_id=project_id, reason=reason)

    def notify_workflow_changed(self, project_id: str, workflow_id: str, *, reason: str = "workflow_changed") -> int:
        self._project(project_id)
        return self._revoke_grants(project_id=project_id, workflow_id=workflow_id, reason=reason)

    def _revoke_prior_boot_grants(self) -> int:
        return self._revoke_grants(reason="workbench_restarted", prior_boot_only=True)

    def _revoke_grants(
        self, *, project_id: Optional[str] = None, workflow_id: Optional[str] = None,
        credential_alias: Optional[str] = None, reason: str, prior_boot_only: bool = False,
    ) -> int:
        clauses = ["status='active'"]
        values: list[Any] = []
        if project_id is not None:
            clauses.append("project_id=?"); values.append(project_id)
        if workflow_id is not None:
            clauses.append("workflow_id=?"); values.append(workflow_id)
        if credential_alias is not None:
            clauses.append("credential_alias=?"); values.append(credential_alias)
        if prior_boot_only:
            clauses.append("boot_id<>?"); values.append(self.boot_id)
        now = _iso(self._now())
        where = " AND ".join(clauses)
        with database.get_db_conn() as conn:
            rows = conn.execute(f"SELECT grant_id FROM n8n_agent_runtime_grants WHERE {where}", tuple(values)).fetchall()
            conn.execute(
                f"UPDATE n8n_agent_runtime_grants SET status='revoked',revoked_at=?,revoke_reason=? WHERE {where}",
                (now, reason, *values),
            )
            if rows:
                placeholders = ",".join("?" for _ in rows)
                conn.execute(
                    f"UPDATE n8n_agent_runtime_approvals SET status='revoked',updated_at=? WHERE grant_id IN ({placeholders}) AND status IN ('approved','approved_by_grant')",
                    (now, *(row["grant_id"] for row in rows)),
                )
            approval_clauses = ["status IN ('pending','approved','approved_by_grant')"]
            approval_values: list[Any] = []
            if project_id is not None:
                approval_clauses.append("project_id=?"); approval_values.append(project_id)
            if workflow_id is not None:
                approval_clauses.append("workflow_id=?"); approval_values.append(workflow_id)
            if credential_alias is not None:
                approval_clauses.append("credential_alias=?"); approval_values.append(credential_alias)
            if not prior_boot_only:
                conn.execute(
                    f"UPDATE n8n_agent_runtime_approvals SET status='revoked',updated_at=? WHERE {' AND '.join(approval_clauses)}",
                    (now, *approval_values),
                )
        return len(rows)

    def _expire_runtime_state(self) -> None:
        now = _iso(self._now())
        with database.get_db_conn() as conn:
            grants = conn.execute(
                "SELECT grant_id FROM n8n_agent_runtime_grants WHERE status='active' AND expires_at<=?", (now,)
            ).fetchall()
            conn.execute(
                "UPDATE n8n_agent_runtime_grants SET status='expired',revoked_at=?,revoke_reason='expired' WHERE status='active' AND expires_at<=?",
                (now, now),
            )
            conn.execute(
                "UPDATE n8n_agent_runtime_approvals SET status='expired',updated_at=? WHERE status IN ('pending','approved') AND expires_at<=?",
                (now, now),
            )
            if grants:
                placeholders = ",".join("?" for _ in grants)
                conn.execute(
                    f"UPDATE n8n_agent_runtime_approvals SET status='expired',updated_at=? WHERE grant_id IN ({placeholders}) AND status IN ('approved','approved_by_grant')",
                    (now, *(row["grant_id"] for row in grants)),
                )

    @staticmethod
    def _runtime_approval_identity(value: Mapping[str, Any]) -> tuple[str, str]:
        binding_id = str(value.get("approval_binding_id") or "").strip()
        manifest_digest = str(value.get("manifest_digest") or "").strip()
        if binding_id or manifest_digest:
            return (
                _safe_id(binding_id, "approval_binding_id"),
                _sha256(manifest_digest, "manifest_digest"),
            )
        # The current signed route predates these two explicit DTO fields.  The
        # protected gate therefore carries them as one opaque node_id token;
        # arbitrary n8n workflows cannot turn that token into broader scope.
        token = str(value.get("approval_token") or value.get("node_id") or "")
        match = _APPROVAL_TOKEN_RE.fullmatch(token)
        if not match:
            raise N8nAgentTaskError(
                "N8N_APPROVAL_MANIFEST_REQUIRED",
                "A server-issued approval binding and manifest digest are required.",
                status_code=409,
            )
        return (
            _safe_id(match.group("binding"), "approval_binding_id"),
            _sha256(match.group("digest"), "manifest_digest"),
        )

    def _resolve_runtime_approval_manifest(
        self,
        *,
        project_id: str,
        workflow_id: str,
        workflow_revision: str,
        approval_binding_id: str,
        manifest_digest: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with database.get_db_conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM n8n_agent_approval_manifests
                 WHERE project_id=? AND workflow_id=? AND workflow_revision=?
                   AND approval_binding_id=? AND manifest_digest=? AND active=1
                """,
                (
                    project_id, workflow_id, workflow_revision,
                    approval_binding_id, manifest_digest,
                ),
            ).fetchone()
        if not row:
            raise N8nAgentTaskError(
                "N8N_APPROVAL_MANIFEST_SCOPE_MISMATCH",
                "The approval action is not bound to this active workflow revision.",
                status_code=409,
            )
        manifest_row = dict(row)
        expected_active = str(manifest_row.get("active_version_id") or "").strip()
        if not expected_active:
            raise N8nAgentTaskError(
                "N8N_APPROVAL_MANIFEST_NOT_ACTIVATED",
                "The approval action manifest has not been reconciled with an active n8n version.",
                status_code=409,
            )
        live = self._read_live_active_version(workflow_id)
        if not hmac.compare_digest(expected_active, live):
            self._revision_drift(project_id, workflow_id)
            raise N8nAgentTaskError(
                "N8N_WORKFLOW_REVISION_CHANGED",
                "The active n8n workflow changed and must be reviewed again.",
                status_code=409,
            )
        aad = (
            f"n8n-approval-manifest:{project_id}:{approval_binding_id}:"
            f"{manifest_digest}"
        )
        try:
            manifest = self._normalized_approval_manifest(
                _loads(self.cipher.decrypt_text(manifest_row["manifest_envelope"], aad=aad), None)
            )
        except Exception as exc:
            if isinstance(exc, N8nAgentTaskError):
                raise
            raise N8nAgentTaskError(
                "N8N_APPROVAL_MANIFEST_UNAVAILABLE",
                "The server approval action manifest cannot be verified.",
                status_code=503,
            ) from exc
        if (
            not hmac.compare_digest(_digest(manifest), manifest_digest)
            or not hmac.compare_digest(
                str(manifest.get("approval_node_id") or ""),
                str(manifest_row.get("node_id") or ""),
            )
        ):
            raise N8nAgentTaskError(
                "N8N_APPROVAL_MANIFEST_SCOPE_MISMATCH",
                "The stored approval action manifest failed verification.",
                status_code=409,
            )
        return manifest_row, manifest

    def request_runtime_approval(self, value: Mapping[str, Any]) -> dict[str, Any]:
        if "project_id" in value:
            raise N8nAgentTaskError("N8N_AGENT_PROJECT_FORBIDDEN", "n8n cannot select a Workbench Project.", status_code=422)
        request_id = _safe_id(value.get("request_id"), "request_id")
        workflow_id = _safe_id(value.get("workflow_id"), "workflow_id")
        workflow_revision = _safe_id(value.get("workflow_revision"), "workflow_revision")
        approval_binding_id, manifest_digest = self._runtime_approval_identity(value)
        run_key = _safe_id(value.get("run_key"), "run_key")
        task_id = str(value.get("task_id") or "").strip() or None
        project_id, _ = self._workflow_project(workflow_id)
        manifest_row, manifest = self._resolve_runtime_approval_manifest(
            project_id=project_id,
            workflow_id=workflow_id,
            workflow_revision=workflow_revision,
            approval_binding_id=approval_binding_id,
            manifest_digest=manifest_digest,
        )
        node_id = str(manifest_row["node_id"])
        alias = str(manifest["credential_alias"])
        action = str(manifest["action"])
        supplied_alias = str(value.get("credential_alias") or "")
        supplied_action = str(value.get("action") or "")
        supplied_target_kind = str(value.get("target_kind") or "")
        if (
            not hmac.compare_digest(supplied_alias, alias)
            or not hmac.compare_digest(supplied_action, action)
            or not hmac.compare_digest(
                supplied_target_kind, str(manifest["target_kind"])
            )
        ):
            raise N8nAgentTaskError(
                "N8N_RUNTIME_ACTION_SCOPE_MISMATCH",
                "The requested action does not match its server-owned manifest.",
                status_code=409,
            )
        credential = self.get_credential_alias(project_id, alias)
        if credential["status"] != "ready":
            raise N8nAgentTaskError("N8N_CREDENTIAL_NOT_READY", "The credential alias is not ready.", status_code=409)
        if task_id:
            task = self._task_row(_safe_id(task_id, "task_id"))
            if task["project_id"] != project_id or task["workflow_id"] != workflow_id:
                raise N8nAgentTaskError("N8N_AGENT_TASK_NOT_FOUND", "The Agent task was not found.", status_code=404)
        canonical_target, target_display = _normalize_target(
            str(manifest["target_kind"]), str(value.get("target") or "")
        )
        target_rule = manifest.get("target_rule") or {}
        if isinstance(target_rule, Mapping) and target_rule.get("mode") == "static":
            expected_target, _ = _normalize_target(
                str(manifest["target_kind"]), str(target_rule.get("value") or "")
            )
            if not hmac.compare_digest(canonical_target, expected_target):
                raise N8nAgentTaskError(
                    "N8N_RUNTIME_ACTION_SCOPE_MISMATCH",
                    "The runtime target does not match its reviewed static target.",
                    status_code=409,
                )
        target_digest = _digest(canonical_target)
        epoch = self._policy_epoch(project_id)
        request_digest = _digest(
            {
                "request_id": request_id, "project_id": project_id, "workflow_id": workflow_id,
                "workflow_revision": workflow_revision, "node_id": node_id,
                "approval_binding_id": approval_binding_id,
                "manifest_digest": manifest_digest,
                "credential_alias": alias, "credential_digest": credential["metadata_digest"],
                "target_kind": manifest["target_kind"], "target_digest": target_digest,
                "action": action, "run_key": run_key, "task_id": task_id,
                "policy_epoch": epoch,
            }
        )
        self._expire_runtime_state()
        # Seeing a different exact revision is an explicit workflow-change signal.
        with database.get_db_conn() as conn:
            stale = conn.execute(
                """
                SELECT grant_id FROM n8n_agent_runtime_grants
                 WHERE project_id=? AND workflow_id=? AND workflow_revision<>? AND status='active'
                """,
                (project_id, workflow_id, workflow_revision),
            ).fetchall()
        if stale:
            self._revoke_grants(project_id=project_id, workflow_id=workflow_id, reason="workflow_revision_changed")
        with database.get_db_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM n8n_agent_runtime_approvals WHERE workflow_id=? AND request_id=?",
                (workflow_id, request_id),
            ).fetchone()
        if existing:
            if existing["request_digest"] != request_digest:
                raise N8nAgentTaskError("N8N_RUNTIME_ACTION_CONFLICT", "The action request id was reused with different content.", status_code=409)
            result = self._approval_public(dict(existing)); result["idempotent"] = True
            return result
        now = self._now()
        with database.get_db_conn() as conn:
            grants = conn.execute(
                """
                SELECT * FROM n8n_agent_runtime_grants
                 WHERE project_id=? AND workflow_id=? AND workflow_revision=? AND node_id=?
                   AND approval_binding_id=? AND manifest_digest=?
                   AND credential_alias=? AND target_digest=? AND action=? AND status='active'
                   AND boot_id=? AND policy_epoch=? AND expires_at>?
                 ORDER BY created_at DESC
                """,
                (
                    project_id, workflow_id, workflow_revision, node_id,
                    approval_binding_id, manifest_digest, alias, target_digest,
                    action, self.boot_id, epoch, _iso(now),
                ),
            ).fetchall()
        matching = next(
            (row for row in grants if row["scope"] == "timed" or row["run_key"] == run_key), None
        )
        approval_id = self._id_factory("nra")
        status = "approved_by_grant" if matching else "pending"
        grant_id = matching["grant_id"] if matching else None
        expires = min(
            _parse_time(matching["expires_at"]) if matching else now + timedelta(hours=1),
            now + timedelta(hours=1),
        )
        with database.get_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO n8n_agent_runtime_approvals(
                    approval_id,request_id,project_id,workflow_id,workflow_revision,node_id,
                    approval_binding_id,manifest_digest,credential_alias,target_kind,
                    target_digest,target_display,action,run_key,task_id,
                    request_digest,policy_epoch,status,grant_id,created_at,updated_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    approval_id, request_id, project_id, workflow_id, workflow_revision, node_id,
                    approval_binding_id, manifest_digest, alias,
                    str(manifest["target_kind"]), target_digest, target_display, action,
                    run_key, task_id, request_digest, epoch, status, grant_id,
                    _iso(now), _iso(now), _iso(expires),
                ),
            )
        result = self._approval_public(self._approval_row(approval_id)); result["idempotent"] = False
        return result

    def _approval_row(self, approval_id: str) -> dict[str, Any]:
        self._expire_runtime_state()
        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT * FROM n8n_agent_runtime_approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
        if not row:
            raise N8nAgentTaskError("N8N_RUNTIME_APPROVAL_NOT_FOUND", "The runtime approval was not found.", status_code=404)
        return dict(row)

    @staticmethod
    def _approval_public(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "approval_id": row["approval_id"], "request_id": row["request_id"],
            "project_id": row["project_id"], "workflow_id": row["workflow_id"],
            "workflow_revision": row["workflow_revision"], "node_id": row["node_id"],
            "approval_binding_id": row.get("approval_binding_id"),
            "manifest_digest": row.get("manifest_digest"),
            "credential_alias": row["credential_alias"], "target_kind": row["target_kind"],
            "target": row["target_display"], "action": row["action"], "run_key": row["run_key"],
            "task_id": row.get("task_id"), "request_digest": row["request_digest"],
            "status": row["status"], "grant_id": row.get("grant_id"),
            "expires_at": row["expires_at"], "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def get_runtime_approval_for_n8n(self, approval_id: str, *, workflow_id: str) -> dict[str, Any]:
        row = self._approval_row(_safe_id(approval_id, "approval_id"))
        if row["workflow_id"] != _safe_id(workflow_id, "workflow_id"):
            raise N8nAgentTaskError("N8N_RUNTIME_APPROVAL_NOT_FOUND", "The runtime approval was not found.", status_code=404)
        project_id, _ = self._workflow_project(workflow_id)
        self._resolve_runtime_approval_manifest(
            project_id=project_id,
            workflow_id=workflow_id,
            workflow_revision=str(row["workflow_revision"]),
            approval_binding_id=_safe_id(
                row.get("approval_binding_id"), "approval_binding_id"
            ),
            manifest_digest=_sha256(
                row.get("manifest_digest"), "manifest_digest"
            ),
        )
        return self._approval_public(row)

    def list_runtime_approvals(self, project_id: str, *, status: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        self._project(project_id); self._expire_runtime_state()
        if status is not None and status not in APPROVAL_STATES:
            raise N8nAgentTaskError("N8N_RUNTIME_APPROVAL_INVALID", "The approval status is invalid.", status_code=422)
        query = "SELECT * FROM n8n_agent_runtime_approvals WHERE project_id=?"
        values: list[Any] = [project_id]
        if status:
            query += " AND status=?"; values.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"; values.append(max(1, min(int(limit), 250)))
        with database.get_db_conn() as conn:
            rows = conn.execute(query, tuple(values)).fetchall()
        return [self._approval_public(dict(row)) for row in rows]

    def decide_runtime_approval(
        self, approval_id: str, *, project_id: str, expected_digest: str,
        approved: bool, duration_minutes: int = 0,
    ) -> dict[str, Any]:
        row = self._approval_row(_safe_id(approval_id, "approval_id"))
        self._project(project_id)
        if row["project_id"] != project_id:
            raise N8nAgentTaskError("N8N_RUNTIME_APPROVAL_NOT_FOUND", "The runtime approval was not found.", status_code=404)
        if row["request_digest"] != _sha256(expected_digest, "expected_digest"):
            raise N8nAgentTaskError("N8N_RUNTIME_APPROVAL_STALE", "The runtime action changed; refresh it first.", status_code=409)
        if row["status"] != "pending":
            raise N8nAgentTaskError("N8N_RUNTIME_APPROVAL_DECIDED", "The runtime approval was already decided.", status_code=409)
        if isinstance(duration_minutes, bool) or not isinstance(duration_minutes, int) or not 0 <= duration_minutes <= 60:
            raise N8nAgentTaskError("N8N_RUNTIME_GRANT_INVALID", "Grant duration must be between 0 and 60 minutes.", status_code=422)
        now = self._now()
        if not approved:
            with database.get_db_conn() as conn:
                conn.execute(
                    "UPDATE n8n_agent_runtime_approvals SET status='rejected',updated_at=? WHERE approval_id=? AND status='pending'",
                    (_iso(now), approval_id),
                )
            return self._approval_public(self._approval_row(approval_id))
        self._resolve_runtime_approval_manifest(
            project_id=project_id,
            workflow_id=str(row["workflow_id"]),
            workflow_revision=str(row["workflow_revision"]),
            approval_binding_id=_safe_id(
                row.get("approval_binding_id"), "approval_binding_id"
            ),
            manifest_digest=_sha256(
                row.get("manifest_digest"), "manifest_digest"
            ),
        )
        if row["policy_epoch"] != self._policy_epoch(project_id):
            raise N8nAgentTaskError("N8N_RUNTIME_APPROVAL_STALE", "The Project policy changed.", status_code=409)
        # Zero minutes is a one-shot decision for this exact request.  It must
        # not create a reusable per-run grant: a workflow can emit the same
        # external action more than once, and the default contract requires
        # every email/delete/HTTP write to pause independently.
        if duration_minutes == 0:
            with database.get_db_conn() as conn:
                changed = conn.execute(
                    "UPDATE n8n_agent_runtime_approvals SET status='approved',grant_id=NULL,updated_at=? WHERE approval_id=? AND status='pending'",
                    (_iso(now), approval_id),
                )
            if changed.rowcount != 1:
                raise N8nAgentTaskError(
                    "N8N_RUNTIME_APPROVAL_DECIDED",
                    "The runtime approval was already decided.",
                    status_code=409,
                )
            return self._approval_public(self._approval_row(approval_id))

        # Reusable time-limited permission is an explicit Full Audit feature.
        # Session-scoped elevation cannot be proven by an autonomous n8n run,
        # so it remains fail closed here.
        try:
            live_policy = (
                self._policy_resolver(project_id)
                if self._policy_resolver is not None else None
            )
        except Exception:
            live_policy = None
        if (
            not isinstance(live_policy, Mapping)
            or live_policy.get("mode") != "full_audit"
            or live_policy.get("elevation_policy") == "session"
            or live_policy.get("runtime_ready") is not True
        ):
            raise N8nAgentTaskError(
                "N8N_RUNTIME_TIMED_GRANT_FORBIDDEN",
                "Timed automatic permission requires an active non-Session Full Audit policy.",
                status_code=403,
            )
        grant_id = self._id_factory("nrg")
        scope = "timed"
        expires = min(
            _parse_time(row["expires_at"]) or now,
            now + timedelta(minutes=duration_minutes),
        )
        with database.get_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO n8n_agent_runtime_grants(
                    grant_id,project_id,workflow_id,workflow_revision,node_id,
                    approval_binding_id,manifest_digest,credential_alias,
                    target_digest,action,scope,run_key,boot_id,policy_epoch,status,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active',?,?)
                """,
                (
                    grant_id, project_id, row["workflow_id"], row["workflow_revision"], row["node_id"],
                    row["approval_binding_id"], row["manifest_digest"],
                    row["credential_alias"], row["target_digest"], row["action"], scope,
                    None, self.boot_id,
                    row["policy_epoch"], _iso(now), _iso(expires),
                ),
            )
            changed = conn.execute(
                "UPDATE n8n_agent_runtime_approvals SET status='approved',grant_id=?,updated_at=? WHERE approval_id=? AND status='pending'",
                (grant_id, _iso(now), approval_id),
            )
        if changed.rowcount != 1:
            self._revoke_grants(project_id=project_id, reason="approval_race")
            raise N8nAgentTaskError("N8N_RUNTIME_APPROVAL_DECIDED", "The runtime approval was already decided.", status_code=409)
        return self._approval_public(self._approval_row(approval_id))


__all__ = [
    "APPROVAL_STATES", "CREDENTIAL_STATES", "EXTERNAL_ACTIONS", "HMAC_PROFILE",
    "N8nAgentTaskError", "N8nAgentTaskRuntime", "TASK_STATES",
]
