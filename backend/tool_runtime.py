"""Governed host-side tool registry, approvals and execution.

The runtime is intentionally independent from FastAPI, SQLite and any one
connector.  Basic Chat supplies project-scoped definitions and adapters for
extension/resource state, approvals and audits.  Every execution re-checks
current scope, including after a human approval, before consuming the
single-use approval and invoking external code.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Protocol

from jsonschema import Draft202012Validator, SchemaError, ValidationError

if __package__:
    from .hook_runtime import (
        GuardAction,
        HookContext,
        HookDispatcher,
        get_hook_dispatcher,
    )
    from .structured_log import redact
else:  # pragma: no cover - direct backend path imports used by the application
    from hook_runtime import (
        GuardAction,
        HookContext,
        HookDispatcher,
        get_hook_dispatcher,
    )
    from structured_log import redact


_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
_EXTENSION_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_RISK_LEVELS = {
    "read",
    "external_read",
    "verify",
    "write",
    "external_write",
    "system",
    "irreversible",
}


class ToolAccess(str, Enum):
    READ = "read"
    WRITE = "write"


class PolicyAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ToolRuntimeError(RuntimeError):
    code = "TOOL_RUNTIME_ERROR"

    def __init__(self, message: str, *, details: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message)
        self.details = dict(redact(dict(details or {})))

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": self.details}


class ToolDefinitionError(ToolRuntimeError, ValueError):
    code = "TOOL_DEFINITION_INVALID"


class ToolUnavailableError(ToolRuntimeError):
    code = "TOOL_UNAVAILABLE"


class ToolArgumentsInvalidError(ToolRuntimeError, ValueError):
    code = "TOOL_ARGUMENTS_INVALID"


class ToolResultInvalidError(ToolRuntimeError):
    code = "TOOL_RESULT_INVALID"


class ToolPolicyDeniedError(ToolRuntimeError, PermissionError):
    code = "TOOL_POLICY_DENIED"


class ToolApprovalError(ToolRuntimeError, PermissionError):
    code = "TOOL_APPROVAL_INVALID"


class ToolExecutionError(ToolRuntimeError):
    code = "TOOL_EXECUTION_FAILED"


class ToolExecutionUnknownError(ToolExecutionError):
    code = "EXECUTION_UNKNOWN"


class ToolExecutionTimeoutError(ToolExecutionError):
    code = "TOOL_EXECUTION_TIMEOUT"


def _required_text(value: Any, name: str, *, maximum: int = 512) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise ToolDefinitionError(f"{name} is invalid")
    return text


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ToolArgumentsInvalidError("tool arguments must be finite JSON values") from error


def _validation_error(error: ValidationError) -> ToolArgumentsInvalidError:
    path = ".".join(str(item) for item in error.absolute_path)
    return ToolArgumentsInvalidError(
        "tool arguments do not match the declared schema",
        details={"path": path, "validation": error.message[:500]},
    )


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    run_id: str
    project_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    session_id: Optional[str] = None
    connection_id: Optional[str] = None
    resource_id: Optional[str] = None
    deadline_monotonic: Optional[float] = None

    def __post_init__(self) -> None:
        for name in ("call_id", "run_id", "project_id", "tool_name"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        for name in ("session_id", "connection_id", "resource_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required_text(value, name))
        if not isinstance(self.arguments, Mapping):
            raise ToolArgumentsInvalidError("tool arguments must be an object")
        object.__setattr__(self, "arguments", dict(self.arguments))
        if self.deadline_monotonic is not None:
            deadline = float(self.deadline_monotonic)
            if not math.isfinite(deadline) or deadline <= 0:
                raise ToolDefinitionError("deadline_monotonic is invalid")
            object.__setattr__(self, "deadline_monotonic", deadline)


ToolHandler = Callable[[ToolCall], Any]
AvailabilityCheck = Callable[[str], bool]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    access: ToolAccess
    handler: ToolHandler
    extension_id: str
    manifest_sha256: str
    output_schema: Optional[Mapping[str, Any]] = None
    risk_level: str = "external_read"
    timeout_seconds: float = 30.0
    max_result_bytes: int = 16 * 1024
    requires_connection: bool = False
    requires_resource: bool = False
    availability: Optional[AvailabilityCheck] = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        normalized_name = str(self.name or "").strip().casefold()
        if len(normalized_name) > 160 or not _TOOL_NAME.fullmatch(normalized_name):
            raise ToolDefinitionError("tool name must be a lowercase namespace and action")
        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(
            self,
            "description",
            _required_text(self.description, "description", maximum=1000),
        )
        extension_id = str(self.extension_id or "").strip().casefold()
        if len(extension_id) > 128 or not _EXTENSION_ID.fullmatch(extension_id):
            raise ToolDefinitionError("extension_id is invalid")
        object.__setattr__(self, "extension_id", extension_id)
        digest = str(self.manifest_sha256 or "").strip().casefold()
        if not _SHA256.fullmatch(digest):
            raise ToolDefinitionError("manifest_sha256 must be a lowercase SHA-256 digest")
        object.__setattr__(self, "manifest_sha256", digest)
        if not isinstance(self.access, ToolAccess):
            try:
                object.__setattr__(self, "access", ToolAccess(str(self.access)))
            except ValueError as error:
                raise ToolDefinitionError("access must be read or write") from error
        if not callable(self.handler):
            raise ToolDefinitionError("handler must be callable")
        if not isinstance(self.input_schema, Mapping):
            raise ToolDefinitionError("input_schema must be an object")
        input_schema = dict(self.input_schema)
        if input_schema.get("type") != "object":
            raise ToolDefinitionError("input_schema must declare an object at its root")
        try:
            Draft202012Validator.check_schema(input_schema)
        except SchemaError as error:
            raise ToolDefinitionError("input_schema is not valid JSON Schema") from error
        object.__setattr__(self, "input_schema", input_schema)
        if self.output_schema is not None:
            if not isinstance(self.output_schema, Mapping):
                raise ToolDefinitionError("output_schema must be an object")
            output_schema = dict(self.output_schema)
            try:
                Draft202012Validator.check_schema(output_schema)
            except SchemaError as error:
                raise ToolDefinitionError("output_schema is not valid JSON Schema") from error
            object.__setattr__(self, "output_schema", output_schema)
        risk = str(self.risk_level or "").strip().casefold()
        if risk not in _RISK_LEVELS:
            raise ToolDefinitionError("risk_level is invalid")
        if self.access is ToolAccess.WRITE and risk in {"read", "external_read", "verify"}:
            raise ToolDefinitionError("write tools must declare a write-class risk")
        object.__setattr__(self, "risk_level", risk)
        timeout = float(self.timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 120:
            raise ToolDefinitionError("timeout_seconds must be greater than 0 and at most 120")
        object.__setattr__(self, "timeout_seconds", timeout)
        if (
            isinstance(self.max_result_bytes, bool)
            or not isinstance(self.max_result_bytes, int)
            or not 1024 <= self.max_result_bytes <= 65_536
        ):
            raise ToolDefinitionError("max_result_bytes must be between 1024 and 65536")
        if self.availability is not None and not callable(self.availability):
            raise ToolDefinitionError("availability must be callable")

    def model_schema(self) -> dict[str, Any]:
        """OpenAI-compatible function description without runtime metadata."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.input_schema),
            },
        }


class ToolRegistry:
    """Deterministic registry with an explicit project visibility boundary."""

    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        self._global_definitions: dict[str, ToolDefinition] = {}
        self._project_definitions: dict[str, dict[str, ToolDefinition]] = {}
        self._lock = threading.RLock()
        for definition in definitions:
            self.register(definition)

    def register(
        self,
        definition: ToolDefinition,
        *,
        project_ids: Optional[Iterable[str]] = None,
        replace_existing: bool = False,
    ) -> None:
        if not isinstance(definition, ToolDefinition):
            raise ToolDefinitionError("definition must be ToolDefinition")
        projects: Optional[frozenset[str]]
        if project_ids is None:
            projects = None
        else:
            projects = frozenset(
                _required_text(project_id, "project_id") for project_id in project_ids
            )
            if not projects:
                raise ToolDefinitionError("project_ids cannot be empty")

        with self._lock:
            if projects is None:
                if any(
                    definition.name in project_tools
                    for project_tools in self._project_definitions.values()
                ):
                    raise ToolDefinitionError(
                        f"global tool conflicts with a project definition: {definition.name}"
                    )
                if definition.name in self._global_definitions and not replace_existing:
                    raise ToolDefinitionError(
                        f"duplicate global tool definition: {definition.name}"
                    )
                self._global_definitions[definition.name] = definition
                return

            if definition.name in self._global_definitions:
                raise ToolDefinitionError(
                    f"project tool conflicts with a global definition: {definition.name}"
                )
            conflicts = [
                project
                for project in projects
                if definition.name in self._project_definitions.get(project, {})
            ]
            if conflicts and not replace_existing:
                raise ToolDefinitionError(
                    f"duplicate project tool definition: {definition.name}"
                )
            # Validate every target before mutating any project so a failed
            # multi-project registration is atomic.
            for project in projects:
                project_tools = self._project_definitions.setdefault(project, {})
                project_tools[definition.name] = definition

    def unregister(self, name: str, *, project_id: Optional[str] = None) -> bool:
        """Remove one project binding or, for legacy callers, every binding.

        The original API accepted only ``name`` and removed the sole stored
        definition.  Omitting ``project_id`` therefore still removes the
        global definition and every project-local definition with that name.
        """

        normalized = str(name or "").strip().casefold()
        with self._lock:
            if project_id is not None:
                project = _required_text(project_id, "project_id")
                project_tools = self._project_definitions.get(project)
                if project_tools is None:
                    return False
                removed = project_tools.pop(normalized, None) is not None
                if not project_tools:
                    self._project_definitions.pop(project, None)
                return removed

            removed = self._global_definitions.pop(normalized, None) is not None
            for project, project_tools in tuple(self._project_definitions.items()):
                removed = project_tools.pop(normalized, None) is not None or removed
                if not project_tools:
                    self._project_definitions.pop(project, None)
            return removed

    def replace_project(
        self,
        project_id: str,
        definitions: Iterable[ToolDefinition],
    ) -> None:
        """Atomically replace project-owned entries while retaining globals."""

        project = _required_text(project_id, "project_id")
        incoming = tuple(definitions)
        if any(not isinstance(definition, ToolDefinition) for definition in incoming):
            raise ToolDefinitionError("definition must be ToolDefinition")
        names = {definition.name for definition in incoming}
        if len(names) != len(incoming):
            raise ToolDefinitionError("project tool names must be unique")
        with self._lock:
            for definition in incoming:
                if definition.name in self._global_definitions:
                    raise ToolDefinitionError(
                        f"project tool conflicts with a global definition: {definition.name}"
                    )
            replacement = {definition.name: definition for definition in incoming}
            if replacement:
                self._project_definitions[project] = replacement
            else:
                self._project_definitions.pop(project, None)

    def for_project(self, project_id: str) -> tuple[ToolDefinition, ...]:
        project = _required_text(project_id, "project_id")
        with self._lock:
            # Local/global name collisions are rejected at registration time;
            # combining snapshots here is therefore deterministic and cannot
            # let a connector shadow a host-owned global tool.
            combined = dict(self._global_definitions)
            for name, definition in self._project_definitions.get(project, {}).items():
                if name in combined:  # defensive invariant check
                    raise ToolDefinitionError(
                        f"project tool conflicts with a global definition: {name}"
                    )
                combined[name] = definition
            snapshot = tuple(combined.values())
        available: list[ToolDefinition] = []
        for definition in snapshot:
            if definition.availability is not None:
                try:
                    if not bool(definition.availability(project)):
                        continue
                except Exception:
                    continue
            available.append(definition)
        return tuple(sorted(available, key=lambda item: item.name))

    def get(self, project_id: str, name: str) -> ToolDefinition:
        normalized = str(name or "").strip().casefold()
        for definition in self.for_project(project_id):
            if definition.name == normalized:
                return definition
        raise ToolUnavailableError(
            "tool is not available to this project", details={"tool_name": normalized}
        )


@dataclass(frozen=True)
class ToolScopeState:
    installed: bool
    trusted: bool
    enabled: bool
    healthy: bool
    resource_allowed: bool
    manifest_sha256: str
    resource_revision: int = 0
    connection_enabled: bool = True
    connection_id: Optional[str] = None
    resource_id: Optional[str] = None
    reason: str = ""

    def __post_init__(self) -> None:
        for name in (
            "installed",
            "trusted",
            "enabled",
            "healthy",
            "resource_allowed",
            "connection_enabled",
        ):
            if type(getattr(self, name)) is not bool:
                raise ToolDefinitionError(f"{name} must be a boolean")
        digest = str(self.manifest_sha256 or "").strip().casefold()
        if not _SHA256.fullmatch(digest):
            raise ToolDefinitionError("scope manifest_sha256 is invalid")
        object.__setattr__(self, "manifest_sha256", digest)
        if isinstance(self.resource_revision, bool) or not isinstance(self.resource_revision, int):
            raise ToolDefinitionError("resource_revision must be an integer")
        if self.resource_revision < 0:
            raise ToolDefinitionError("resource_revision cannot be negative")
        for name in ("connection_id", "resource_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required_text(value, name))
        object.__setattr__(self, "reason", str(self.reason or "")[:1000])


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.action, PolicyAction):
            try:
                object.__setattr__(self, "action", PolicyAction(str(self.action)))
            except ValueError as error:
                raise ToolPolicyDeniedError("fixed policy action is invalid") from error
        reason = str(self.reason or "").strip()
        if not reason or len(reason) > 1000:
            raise ToolPolicyDeniedError("fixed policy reason is invalid")
        object.__setattr__(self, "reason", reason)


@dataclass(frozen=True)
class ToolApprovalBinding:
    tool_name: str
    project_id: str
    run_id: str
    call_id: str
    connection_id: Optional[str]
    resource_id: Optional[str]
    manifest_sha256: str
    resource_revision: int
    arguments_sha256: str

    def __post_init__(self) -> None:
        for name in ("tool_name", "project_id", "run_id", "call_id"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        for name in ("connection_id", "resource_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required_text(value, name))
        for name in ("manifest_sha256", "arguments_sha256"):
            value = str(getattr(self, name) or "").strip().casefold()
            if not _SHA256.fullmatch(value):
                raise ToolApprovalError(f"{name} is invalid")
            object.__setattr__(self, name, value)
        if isinstance(self.resource_revision, bool) or not isinstance(self.resource_revision, int):
            raise ToolApprovalError("resource_revision must be an integer")
        if self.resource_revision < 0:
            raise ToolApprovalError("resource_revision cannot be negative")

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            _canonical_json(
                {
                    "tool_name": self.tool_name,
                    "project_id": self.project_id,
                    "run_id": self.run_id,
                    "call_id": self.call_id,
                    "connection_id": self.connection_id,
                    "resource_id": self.resource_id,
                    "manifest_sha256": self.manifest_sha256,
                    "resource_revision": self.resource_revision,
                    "arguments_sha256": self.arguments_sha256,
                }
            )
        ).hexdigest()


@dataclass(frozen=True)
class ToolApprovalRequest:
    approval_id: str
    binding: ToolApprovalBinding
    binding_sha256: str
    summary: Mapping[str, Any]
    reason: str
    status: ApprovalStatus
    created_at: float
    expires_at: float
    decided_at: Optional[float] = None
    decided_by: Optional[str] = None
    rationale: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool_name": self.binding.tool_name,
            "project_id": self.binding.project_id,
            "run_id": self.binding.run_id,
            "call_id": self.binding.call_id,
            "connection_id": self.binding.connection_id,
            "resource_id": self.binding.resource_id,
            "binding_sha256": self.binding_sha256,
            "summary": dict(redact(dict(self.summary))),
            "reason": self.reason,
            "status": self.status.value,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "rationale": self.rationale,
            "choices": ["once", "deny"],
        }


@dataclass(frozen=True)
class ToolApprovalDecision:
    approved: bool
    decided_by: str = "user"
    rationale: str = ""

    def __post_init__(self) -> None:
        if type(self.approved) is not bool:
            raise ToolApprovalError("approved must be a boolean")
        object.__setattr__(self, "decided_by", _required_text(self.decided_by, "decided_by"))
        rationale = str(self.rationale or "").strip()
        if len(rationale) > 1000:
            raise ToolApprovalError("approval rationale is too long")
        object.__setattr__(self, "rationale", rationale)


class ToolApprovalRequired(ToolApprovalError):
    code = "APPROVAL_REQUIRED"

    def __init__(self, request: ToolApprovalRequest) -> None:
        super().__init__(
            "tool execution requires a single-use approval",
            details=request.public_dict(),
        )
        self.request = request


class ApprovalStore(Protocol):
    async def request(
        self,
        binding: ToolApprovalBinding,
        *,
        summary: Mapping[str, Any],
        reason: str,
        ttl_seconds: float,
    ) -> ToolApprovalRequest: ...

    async def decide(
        self,
        approval_id: str,
        decision: ToolApprovalDecision,
    ) -> ToolApprovalRequest: ...

    async def consume(
        self,
        approval_id: str,
        binding: ToolApprovalBinding,
    ) -> ToolApprovalRequest: ...


class InMemoryApprovalStore:
    """Single-process reference store; production may inject a SQLite CAS adapter."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory
        self._records: dict[str, ToolApprovalRequest] = {}
        self._lock = asyncio.Lock()

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now) or now < 0:
            raise ToolApprovalError("approval clock is invalid")
        return now

    def _fresh(self, record: ToolApprovalRequest, now: float) -> ToolApprovalRequest:
        if record.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED} and now >= record.expires_at:
            record = replace(record, status=ApprovalStatus.EXPIRED)
            self._records[record.approval_id] = record
        return record

    async def request(
        self,
        binding: ToolApprovalBinding,
        *,
        summary: Mapping[str, Any],
        reason: str,
        ttl_seconds: float = 600.0,
    ) -> ToolApprovalRequest:
        ttl = float(ttl_seconds)
        if not math.isfinite(ttl) or ttl <= 0 or ttl > 600:
            raise ToolApprovalError("approval TTL must be greater than 0 and at most 600 seconds")
        async with self._lock:
            now = self._now()
            approval_id = _required_text(self._id_factory(), "approval_id")
            if approval_id in self._records:
                raise ToolApprovalError("duplicate approval ID")
            record = ToolApprovalRequest(
                approval_id=approval_id,
                binding=binding,
                binding_sha256=binding.digest,
                summary=dict(redact(dict(summary))),
                reason=str(reason or "Approval required")[:1000],
                status=ApprovalStatus.PENDING,
                created_at=now,
                expires_at=now + ttl,
            )
            self._records[approval_id] = record
            return record

    async def get(self, approval_id: str) -> Optional[ToolApprovalRequest]:
        async with self._lock:
            record = self._records.get(str(approval_id or "").strip())
            return self._fresh(record, self._now()) if record is not None else None

    async def decide(
        self,
        approval_id: str,
        decision: ToolApprovalDecision,
    ) -> ToolApprovalRequest:
        if not isinstance(decision, ToolApprovalDecision):
            raise ToolApprovalError("decision must be ToolApprovalDecision")
        async with self._lock:
            record = self._records.get(str(approval_id or "").strip())
            if record is None:
                raise ToolApprovalError("approval does not exist")
            now = self._now()
            record = self._fresh(record, now)
            if record.status is not ApprovalStatus.PENDING:
                raise ToolApprovalError(f"approval is already {record.status.value}")
            record = replace(
                record,
                status=ApprovalStatus.APPROVED if decision.approved else ApprovalStatus.DENIED,
                decided_at=now,
                decided_by=_required_text(decision.decided_by, "decided_by"),
                rationale=str(decision.rationale or "")[:1000],
            )
            self._records[approval_id] = record
            return record

    async def consume(
        self,
        approval_id: str,
        binding: ToolApprovalBinding,
    ) -> ToolApprovalRequest:
        async with self._lock:
            record = self._records.get(str(approval_id or "").strip())
            if record is None:
                raise ToolApprovalError("approval does not exist")
            record = self._fresh(record, self._now())
            if record.status is not ApprovalStatus.APPROVED:
                raise ToolApprovalError(f"approval is {record.status.value}")
            if record.binding_sha256 != binding.digest:
                raise ToolApprovalError("approval binding no longer matches the invocation")
            record = replace(record, status=ApprovalStatus.CONSUMED)
            self._records[approval_id] = record
            return record

    async def invalidate_pending(self) -> int:
        """Fail closed on process startup/restart."""

        async with self._lock:
            count = 0
            for approval_id, record in tuple(self._records.items()):
                if record.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}:
                    self._records[approval_id] = replace(record, status=ApprovalStatus.EXPIRED)
                    count += 1
            return count


@dataclass(frozen=True)
class ToolAuditRecord:
    audit_id: str
    event: str
    tool_name: str
    call_id: str
    run_id: str
    project_id: str
    access: str
    risk_level: str
    status: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    duration_ms: Optional[int] = None


@dataclass(frozen=True)
class ToolExecutionResult:
    call_id: str
    tool_name: str
    content: Any
    audit_id: str
    duration_ms: int
    approval_id: Optional[str] = None
    truncated: bool = False


ScopeResolver = Callable[[ToolDefinition, ToolCall], ToolScopeState | Awaitable[ToolScopeState]]
PolicyEvaluator = Callable[
    [ToolDefinition, ToolCall, ToolScopeState], PolicyDecision | Awaitable[PolicyDecision]
]
ApprovalCallback = Callable[
    [ToolApprovalRequest],
    bool | ToolApprovalDecision | Awaitable[bool | ToolApprovalDecision],
]
ToolAuditSink = Callable[[ToolAuditRecord], Any]


async def _call_injected(callback: Callable[..., Any], *arguments: Any) -> Any:
    if inspect.iscoroutinefunction(callback):
        return await callback(*arguments)
    result = await asyncio.to_thread(callback, *arguments)
    return await result if inspect.isawaitable(result) else result


class ToolDispatcher:
    """Validate, authorize, approve, execute and sanitize one host tool call."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        scope_resolver: ScopeResolver,
        hook_dispatcher: Optional[HookDispatcher] = None,
        policy_evaluator: Optional[PolicyEvaluator] = None,
        approval_store: Optional[ApprovalStore] = None,
        audit_sink: Optional[ToolAuditSink] = None,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry must be ToolRegistry")
        if not callable(scope_resolver):
            raise TypeError("scope_resolver must be callable")
        self.registry = registry
        self.scope_resolver = scope_resolver
        self.hooks = hook_dispatcher or get_hook_dispatcher()
        self.policy_evaluator = policy_evaluator or self._default_policy
        self.approvals = approval_store or InMemoryApprovalStore()
        self.audit_sink = audit_sink
        self.id_factory = id_factory
        self.clock = clock

    @staticmethod
    def _default_policy(
        definition: ToolDefinition,
        _call: ToolCall,
        _scope: ToolScopeState,
    ) -> PolicyDecision:
        if definition.access is ToolAccess.WRITE:
            return PolicyDecision(PolicyAction.REQUIRE_APPROVAL, "External write requires approval")
        return PolicyDecision(PolicyAction.ALLOW, "Read operation is permitted")

    async def _emit_audit(self, record: ToolAuditRecord) -> None:
        if self.audit_sink is None:
            return
        try:
            if inspect.iscoroutinefunction(self.audit_sink):
                await self.audit_sink(record)
            else:
                result = await asyncio.to_thread(self.audit_sink, record)
                if inspect.isawaitable(result):
                    await result
        except Exception:
            return

    @staticmethod
    def _validate_arguments(definition: ToolDefinition, arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise ToolArgumentsInvalidError("tool arguments must be an object")
        normalized = dict(arguments)
        _canonical_json(normalized)
        try:
            Draft202012Validator(definition.input_schema).validate(normalized)
        except ValidationError as error:
            raise _validation_error(error) from error
        return normalized

    @staticmethod
    def _validate_scope(
        definition: ToolDefinition,
        call: ToolCall,
        scope: ToolScopeState,
    ) -> None:
        if scope.manifest_sha256 != definition.manifest_sha256:
            raise ToolUnavailableError(
                "extension manifest changed and must be trusted again",
                details={"tool_name": definition.name},
            )
        checks = (
            (scope.installed, "extension is not installed"),
            (scope.trusted, "extension is not trusted"),
            (scope.enabled, "extension is disabled"),
            (scope.healthy, "extension or connector is unhealthy"),
        )
        for allowed, reason in checks:
            if not allowed:
                raise ToolUnavailableError(scope.reason or reason, details={"tool_name": definition.name})
        if definition.requires_connection:
            if (
                not call.connection_id
                or not scope.connection_id
                or call.connection_id != scope.connection_id
                or not scope.connection_enabled
            ):
                raise ToolUnavailableError(
                    "connector connection is not enabled or no longer matches",
                    details={"tool_name": definition.name},
                )
        if definition.requires_resource:
            if (
                not call.resource_id
                or not scope.resource_id
                or call.resource_id != scope.resource_id
                or not scope.resource_allowed
            ):
                raise ToolPolicyDeniedError(
                    "resource is not bound to this project",
                    details={"tool_name": definition.name, "resource_id": call.resource_id},
                )

    @staticmethod
    def _binding(
        definition: ToolDefinition,
        call: ToolCall,
        scope: ToolScopeState,
    ) -> ToolApprovalBinding:
        return ToolApprovalBinding(
            tool_name=definition.name,
            project_id=call.project_id,
            run_id=call.run_id,
            call_id=call.call_id,
            connection_id=call.connection_id,
            resource_id=call.resource_id,
            manifest_sha256=scope.manifest_sha256,
            resource_revision=scope.resource_revision,
            arguments_sha256=hashlib.sha256(_canonical_json(call.arguments)).hexdigest(),
        )

    @staticmethod
    def _hook_context(call: ToolCall, definition: ToolDefinition, event: str) -> HookContext:
        return HookContext(
            event=event,
            project_id=call.project_id,
            session_id=call.session_id,
            run_id=call.run_id,
            call_id=call.call_id,
            deadline_monotonic=call.deadline_monotonic,
            metadata={
                "tool_name": definition.name,
                "extension_id": definition.extension_id,
                "manifest_sha256": definition.manifest_sha256,
                "access": definition.access.value,
                "risk_level": definition.risk_level,
                "connection_id": call.connection_id,
                "resource_id": call.resource_id,
            },
        )

    async def _resolve_scope(self, definition: ToolDefinition, call: ToolCall) -> ToolScopeState:
        try:
            scope = await _call_injected(self.scope_resolver, definition, call)
        except ToolRuntimeError:
            raise
        except Exception as error:
            raise ToolUnavailableError(
                "tool scope could not be verified",
                details={"error_type": type(error).__name__},
            ) from error
        if not isinstance(scope, ToolScopeState):
            raise ToolUnavailableError("scope resolver returned an invalid state")
        self._validate_scope(definition, call, scope)
        return scope

    async def _policy(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        scope: ToolScopeState,
    ) -> tuple[PolicyDecision, Any]:
        try:
            fixed = await _call_injected(self.policy_evaluator, definition, call, scope)
        except ToolRuntimeError:
            raise
        except Exception as error:
            raise ToolPolicyDeniedError(
                "fixed host policy is unavailable",
                details={"error_type": type(error).__name__},
            ) from error
        if not isinstance(fixed, PolicyDecision):
            raise ToolPolicyDeniedError("fixed policy returned an invalid decision")
        if fixed.action is PolicyAction.DENY:
            raise ToolPolicyDeniedError(fixed.reason or "fixed host policy denied the tool")
        guard = await self.hooks.guard(
            "tool.before_call", self._hook_context(call, definition, "tool.before_call")
        )
        if guard.action is GuardAction.DENY:
            raise ToolPolicyDeniedError(guard.reason or "a trusted hook denied the tool")
        return fixed, guard

    async def _request_approval(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        scope: ToolScopeState,
        reason: str,
        approval_callback: Optional[ApprovalCallback],
    ) -> str:
        binding = self._binding(definition, call, scope)
        request = await self.approvals.request(
            binding,
            summary={
                "tool_name": definition.name,
                "access": definition.access.value,
                "risk_level": definition.risk_level,
                "arguments": redact(dict(call.arguments)),
                "resource_id": call.resource_id,
            },
            reason=reason,
            ttl_seconds=600,
        )
        await self._emit_audit(
            ToolAuditRecord(
                audit_id=request.approval_id,
                event="approval_required",
                tool_name=definition.name,
                call_id=call.call_id,
                run_id=call.run_id,
                project_id=call.project_id,
                access=definition.access.value,
                risk_level=definition.risk_level,
                status="pending",
                payload=request.public_dict(),
            )
        )
        if approval_callback is None:
            raise ToolApprovalRequired(request)
        remaining = request.expires_at - time.time()
        if call.deadline_monotonic is not None:
            remaining = min(remaining, call.deadline_monotonic - self.clock())
        if remaining <= 0:
            raise ToolApprovalError("approval expired before a decision was received")
        try:
            raw_decision = await asyncio.wait_for(
                _call_injected(approval_callback, request), timeout=remaining
            )
        except asyncio.TimeoutError as error:
            raise ToolApprovalError("approval decision timed out") from error
        if isinstance(raw_decision, ToolApprovalDecision):
            decision = raw_decision
        elif type(raw_decision) is bool:
            decision = ToolApprovalDecision(raw_decision)
        else:
            raise ToolApprovalError(
                "approval callback must return bool or ToolApprovalDecision"
            )
        decided = await self.approvals.decide(request.approval_id, decision)
        if decided.status is not ApprovalStatus.APPROVED:
            raise ToolApprovalError("user denied the tool operation")
        return request.approval_id

    @staticmethod
    async def _call_handler(handler: ToolHandler, call: ToolCall) -> Any:
        if inspect.iscoroutinefunction(handler):
            return await handler(call)
        result = await asyncio.to_thread(handler, call)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _sanitize_result(definition: ToolDefinition, value: Any) -> tuple[Any, bool]:
        if definition.output_schema is not None:
            try:
                Draft202012Validator(definition.output_schema).validate(value)
            except ValidationError as error:
                raise ToolResultInvalidError(
                    "tool result does not match its declared schema",
                    details={"validation": error.message[:500]},
                ) from error
        safe = redact(value)
        try:
            encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            safe = redact(repr(value))
            encoded = json.dumps(safe, ensure_ascii=False).encode("utf-8")
        if len(encoded) <= definition.max_result_bytes:
            return safe, False
        preview = encoded[: max(0, definition.max_result_bytes - 256)].decode(
            "utf-8", errors="ignore"
        )
        return {
            "truncated": True,
            "original_bytes": len(encoded),
            "preview": preview,
        }, True

    async def execute(
        self,
        *,
        run_id: str,
        project_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        session_id: Optional[str] = None,
        call_id: Optional[str] = None,
        connection_id: Optional[str] = None,
        resource_id: Optional[str] = None,
        deadline_monotonic: Optional[float] = None,
        approval_callback: Optional[ApprovalCallback] = None,
        approval_id: Optional[str] = None,
    ) -> ToolExecutionResult:
        definition = self.registry.get(project_id, tool_name)
        original = self._validate_arguments(definition, arguments)
        call = ToolCall(
            call_id=call_id or self.id_factory(),
            run_id=run_id,
            project_id=project_id,
            session_id=session_id,
            tool_name=definition.name,
            arguments=original,
            connection_id=connection_id,
            resource_id=resource_id,
            deadline_monotonic=deadline_monotonic,
        )

        # A disabled, untrusted or stale extension never reaches even trusted
        # argument-transform hooks.  The second scope read below validates the
        # transformed argument-derived resource immediately before policy.
        await self._resolve_scope(definition, call)

        transformed = await self.hooks.transform(
            "tool.arguments.transform",
            self._hook_context(call, definition, "tool.arguments.transform"),
            dict(original),
        )
        transformed = self._validate_arguments(definition, transformed)
        call = replace(call, arguments=transformed)
        scope = await self._resolve_scope(definition, call)
        fixed, guard = await self._policy(definition, call, scope)
        # WRITE still requires approval under the default fixed policy.  An
        # explicitly selected project permission level may return ALLOW;
        # trusted guards can independently retain an approval requirement.
        needs_approval = (
            fixed.action is PolicyAction.REQUIRE_APPROVAL
            or guard.action is GuardAction.REQUIRE_APPROVAL
        )

        used_approval_id: Optional[str] = approval_id
        if needs_approval and used_approval_id is None:
            reason = (
                guard.approval_summary
                or guard.reason
                or fixed.reason
                or "This external operation requires approval"
            )
            used_approval_id = await self._request_approval(
                definition, call, scope, reason, approval_callback
            )

        if needs_approval:
            # Re-check every mutable boundary after the user has seen and
            # approved the exact operation.  No transform runs after approval.
            current_scope = await self._resolve_scope(definition, call)
            current_fixed, current_guard = await self._policy(definition, call, current_scope)
            if current_fixed.action is PolicyAction.DENY or current_guard.action is GuardAction.DENY:
                raise ToolPolicyDeniedError("tool policy changed after approval")
            binding = self._binding(definition, call, current_scope)
            await self.approvals.consume(str(used_approval_id or ""), binding)

        audit_id = self.id_factory()
        started = self.clock()
        await self._emit_audit(
            ToolAuditRecord(
                audit_id=audit_id,
                event="tool_started",
                tool_name=definition.name,
                call_id=call.call_id,
                run_id=call.run_id,
                project_id=call.project_id,
                access=definition.access.value,
                risk_level=definition.risk_level,
                status="started",
                payload={"arguments": redact(dict(call.arguments)), "resource_id": call.resource_id},
            )
        )
        await self.hooks.observe(
            "tool.started", self._hook_context(call, definition, "tool.started")
        )

        timeout = definition.timeout_seconds
        if deadline_monotonic is not None:
            timeout = min(timeout, deadline_monotonic - self.clock())
        if timeout <= 0:
            error: Exception = asyncio.TimeoutError("run deadline elapsed")
        else:
            error = RuntimeError("uninitialized execution error")
            try:
                raw_result = await asyncio.wait_for(
                    self._call_handler(definition.handler, call), timeout=timeout
                )
                content, truncated = self._sanitize_result(definition, raw_result)
            except asyncio.CancelledError as caught:
                error = caught
            except Exception as caught:
                error = caught
            else:
                duration = max(0, round((self.clock() - started) * 1000))
                await self._emit_audit(
                    ToolAuditRecord(
                        audit_id=audit_id,
                        event="tool_completed",
                        tool_name=definition.name,
                        call_id=call.call_id,
                        run_id=call.run_id,
                        project_id=call.project_id,
                        access=definition.access.value,
                        risk_level=definition.risk_level,
                        status="completed",
                        duration_ms=duration,
                        payload={"result": content, "truncated": truncated},
                    )
                )
                await self.hooks.observe(
                    "tool.completed", self._hook_context(call, definition, "tool.completed")
                )
                return ToolExecutionResult(
                    call_id=call.call_id,
                    tool_name=definition.name,
                    content=content,
                    audit_id=audit_id,
                    duration_ms=duration,
                    approval_id=used_approval_id,
                    truncated=truncated,
                )

        duration = max(0, round((self.clock() - started) * 1000))
        await self._emit_audit(
            ToolAuditRecord(
                audit_id=audit_id,
                event="tool_failed",
                tool_name=definition.name,
                call_id=call.call_id,
                run_id=call.run_id,
                project_id=call.project_id,
                access=definition.access.value,
                risk_level=definition.risk_level,
                status="failed",
                duration_ms=duration,
                payload={"error_type": type(error).__name__, "error": str(redact(str(error)))},
            )
        )
        await self.hooks.observe(
            "tool.failed", self._hook_context(call, definition, "tool.failed")
        )
        if isinstance(error, asyncio.CancelledError):
            if definition.access is ToolAccess.WRITE:
                raise ToolExecutionUnknownError(
                    "external write was cancelled after dispatch and may have completed; verify the remote system"
                ) from error
            raise error
        if isinstance(error, (asyncio.TimeoutError, TimeoutError, ConnectionError, BrokenPipeError)) or bool(
            getattr(error, "execution_state_unknown", False)
        ):
            if definition.access is ToolAccess.WRITE:
                raise ToolExecutionUnknownError(
                    "external write may have completed; verify the remote system before retrying"
                ) from error
            raise ToolExecutionTimeoutError("tool execution timed out") from error
        if isinstance(error, ToolRuntimeError):
            raise error
        raise ToolExecutionError(
            "tool execution failed", details={"error_type": type(error).__name__}
        ) from error


__all__ = [
    "ApprovalStatus",
    "ApprovalStore",
    "InMemoryApprovalStore",
    "PolicyAction",
    "PolicyDecision",
    "ToolAccess",
    "ToolApprovalBinding",
    "ToolApprovalDecision",
    "ToolApprovalError",
    "ToolApprovalRequest",
    "ToolApprovalRequired",
    "ToolArgumentsInvalidError",
    "ToolAuditRecord",
    "ToolCall",
    "ToolDefinition",
    "ToolDefinitionError",
    "ToolDispatcher",
    "ToolExecutionError",
    "ToolExecutionResult",
    "ToolExecutionTimeoutError",
    "ToolExecutionUnknownError",
    "ToolPolicyDeniedError",
    "ToolRegistry",
    "ToolResultInvalidError",
    "ToolRuntimeError",
    "ToolScopeState",
    "ToolUnavailableError",
]
