"""Trusted, typed hook dispatch for Workbench host-side lifecycle events.

Only reviewed in-process plugins may register here.  Local or remote MCP
servers contribute tools through :mod:`tool_runtime`; they never import code
into the FastAPI process and cannot register lifecycle hooks.

Pluggy is intentionally limited to discovering and validating builtin hook
registrations.  Ordering, async execution, timeouts and failure policy live in
``HookDispatcher`` because pluggy itself is synchronous and does not provide
the deterministic priority contract Workbench needs.
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
from collections import deque
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

import pluggy

try:
    from structured_log import redact
except ImportError:  # pragma: no cover - package import compatibility
    from backend.structured_log import redact


HOOK_PROJECT_NAME = "workbench_safe_hooks"
hookspec = pluggy.HookspecMarker(HOOK_PROJECT_NAME)
hookimpl = pluggy.HookimplMarker(HOOK_PROJECT_NAME)

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class HookMode(str, Enum):
    OBSERVE = "observe"
    TRANSFORM = "transform"
    GUARD = "guard"


class GuardAction(str, Enum):
    """A hook can only narrow host policy; there is deliberately no allow."""

    ABSTAIN = "abstain"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


# Public event contract for the safe MVP.  Adding an event is an explicit host
# API change; unknown strings are rejected instead of silently becoming an
# ungoverned extension point.
EVENT_MODES: Mapping[str, frozenset[HookMode]] = {
    "app.starting": frozenset({HookMode.OBSERVE}),
    "app.ready": frozenset({HookMode.OBSERVE}),
    "app.stopping": frozenset({HookMode.OBSERVE}),
    "session.created": frozenset({HookMode.OBSERVE}),
    "session.deleted": frozenset({HookMode.OBSERVE}),
    "chat.input.before_dispatch": frozenset({HookMode.TRANSFORM, HookMode.GUARD}),
    "run.before_start": frozenset({HookMode.GUARD}),
    "run.started": frozenset({HookMode.OBSERVE}),
    "run.completed": frozenset({HookMode.OBSERVE}),
    "run.failed": frozenset({HookMode.OBSERVE}),
    "run.cancelled": frozenset({HookMode.OBSERVE}),
    "model.request.transform": frozenset({HookMode.TRANSFORM}),
    "model.request.guard": frozenset({HookMode.GUARD}),
    "model.started": frozenset({HookMode.OBSERVE}),
    "model.completed": frozenset({HookMode.OBSERVE}),
    "model.failed": frozenset({HookMode.OBSERVE}),
    "tool.arguments.transform": frozenset({HookMode.TRANSFORM}),
    "tool.before_call": frozenset({HookMode.GUARD}),
    "tool.started": frozenset({HookMode.OBSERVE}),
    "tool.completed": frozenset({HookMode.OBSERVE}),
    "tool.failed": frozenset({HookMode.OBSERVE}),
    "response.persisted": frozenset({HookMode.OBSERVE}),
}


class HookRuntimeError(RuntimeError):
    code = "HOOK_RUNTIME_ERROR"

    def __init__(self, message: str, *, registration: "HookRegistration | None" = None) -> None:
        super().__init__(message)
        self.registration = registration


class HookContractError(HookRuntimeError, ValueError):
    code = "HOOK_CONTRACT_INVALID"


class HookTransformFailed(HookRuntimeError):
    code = "HOOK_TRANSFORM_FAILED"


class HookGuardUnavailable(HookRuntimeError):
    code = "HOOK_GUARD_UNAVAILABLE"


class HookSnapshotIncompatible(HookRuntimeError):
    code = "HOOK_SNAPSHOT_INCOMPATIBLE"


def _safe_text(value: Any, field_name: str, *, maximum: int = 512) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise HookContractError(f"{field_name} is invalid")
    return text


@dataclass(frozen=True)
class HookContext:
    """Redacted host context shared with a trusted builtin hook.

    ``metadata`` must contain only the minimum, non-secret data required for
    policy.  The constructor defensively redacts it so accidental tokens never
    reach hook audits or exception representations.
    """

    event: str
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    project_id: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    call_id: Optional[str] = None
    retry_of_run_id: Optional[str] = None
    deadline_monotonic: Optional[float] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.event not in EVENT_MODES:
            raise HookContractError(f"unknown hook event: {self.event}")
        object.__setattr__(self, "event_id", _safe_text(self.event_id, "event_id"))
        for name in ("project_id", "session_id", "run_id", "call_id", "retry_of_run_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _safe_text(value, name))
        if self.deadline_monotonic is not None:
            deadline = float(self.deadline_monotonic)
            if not math.isfinite(deadline) or deadline <= 0:
                raise HookContractError("deadline_monotonic is invalid")
            object.__setattr__(self, "deadline_monotonic", deadline)
        if not isinstance(self.metadata, Mapping):
            raise HookContractError("metadata must be an object")
        object.__setattr__(self, "metadata", redact(dict(self.metadata)))

    def for_event(self, event: str, *, event_id: Optional[str] = None) -> "HookContext":
        return replace(self, event=event, event_id=event_id or uuid.uuid4().hex)


@dataclass(frozen=True)
class GuardDecision:
    action: GuardAction = GuardAction.ABSTAIN
    reason: str = ""
    approval_summary: str = ""
    hook_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, GuardAction):
            try:
                object.__setattr__(self, "action", GuardAction(str(self.action)))
            except ValueError as error:
                raise HookContractError("guard action is invalid") from error
        for name, limit in (("reason", 1000), ("approval_summary", 1000)):
            value = str(getattr(self, name) or "").strip()
            if len(value) > limit:
                raise HookContractError(f"{name} is too long")
            object.__setattr__(self, name, value)


HookHandler = Callable[..., Any]
HookValidator = Callable[[Any], bool | None]


@dataclass(frozen=True)
class HookRegistration:
    hook_id: str
    extension_id: str
    extension_version: str
    manifest_sha256: str
    event: str
    mode: HookMode
    priority: int
    handler: HookHandler
    timeout_seconds: Optional[float] = None
    value_type: Optional[type | tuple[type, ...]] = None
    validator: Optional[HookValidator] = None
    required: bool = True
    trusted_builtin: bool = True

    def __post_init__(self) -> None:
        for field_name in ("hook_id", "extension_id"):
            value = str(getattr(self, field_name) or "").strip().casefold()
            if not _IDENTIFIER.fullmatch(value) or len(value) > 128:
                raise HookContractError(f"{field_name} is invalid")
            object.__setattr__(self, field_name, value)
        object.__setattr__(
            self,
            "extension_version",
            _safe_text(self.extension_version, "extension_version", maximum=80),
        )
        digest = str(self.manifest_sha256 or "").strip().casefold()
        if not _SHA256.fullmatch(digest):
            raise HookContractError("manifest_sha256 must be a lowercase SHA-256 digest")
        object.__setattr__(self, "manifest_sha256", digest)
        if self.event not in EVENT_MODES:
            raise HookContractError(f"unknown hook event: {self.event}")
        if not isinstance(self.mode, HookMode):
            try:
                object.__setattr__(self, "mode", HookMode(str(self.mode)))
            except ValueError as error:
                raise HookContractError("hook mode is invalid") from error
        if self.mode not in EVENT_MODES[self.event]:
            raise HookContractError(f"{self.event} does not support {self.mode.value} hooks")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise HookContractError("priority must be an integer")
        if self.priority < -10_000 or self.priority > 10_000:
            raise HookContractError("priority is outside the supported range")
        if not callable(self.handler):
            raise HookContractError("handler must be callable")
        if self.timeout_seconds is not None:
            timeout = float(self.timeout_seconds)
            if not math.isfinite(timeout) or timeout <= 0 or timeout > 10:
                raise HookContractError("timeout_seconds must be greater than 0 and at most 10")
            object.__setattr__(self, "timeout_seconds", timeout)
        if not self.trusted_builtin:
            raise HookContractError("only trusted builtin code may register lifecycle hooks")
        if type(self.required) is not bool or type(self.trusted_builtin) is not bool:
            raise HookContractError("required and trusted_builtin must be booleans")


@dataclass(frozen=True)
class HookSnapshotEntry:
    extension_id: str
    hook_id: str
    extension_version: str
    manifest_sha256: str
    event: str
    mode: str
    priority: int
    required: bool

    @classmethod
    def from_registration(cls, registration: HookRegistration) -> "HookSnapshotEntry":
        return cls(
            extension_id=registration.extension_id,
            hook_id=registration.hook_id,
            extension_version=registration.extension_version,
            manifest_sha256=registration.manifest_sha256,
            event=registration.event,
            mode=registration.mode.value,
            priority=registration.priority,
            required=registration.required,
        )


@dataclass(frozen=True)
class HookSnapshot:
    entries: tuple[HookSnapshotEntry, ...]

    @property
    def digest(self) -> str:
        payload = [entry.__dict__ for entry in self.entries]
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class HookTransformStep:
    extension_id: str
    hook_id: str
    extension_version: str
    manifest_sha256: str
    input_sha256: str
    output_sha256: str


@dataclass(frozen=True)
class HookTransformResult:
    value: Any
    steps: tuple[HookTransformStep, ...]


def _value_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HookContractError("hook transform values must be finite JSON data") from error
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class HookAuditRecord:
    event: str
    event_id: str
    mode: str
    hook_id: str
    extension_id: str
    extension_version: str
    manifest_sha256: str
    status: str
    duration_ms: int
    error_code: Optional[str] = None
    error_type: Optional[str] = None
    error: Optional[str] = None


class BuiltinHookSpecs:
    @hookspec
    def workbench_hook_registrations(self) -> Iterable[HookRegistration]:
        """Return reviewed lifecycle hook registrations."""


class DiagnosticBuiltinHookPlugin:
    """Harmless example plugin used by contract tests and developer tooling."""

    _DIGEST = hashlib.sha256(b"workbench.diagnostic-hook.v1").hexdigest()

    @staticmethod
    def _observe(_context: HookContext) -> None:
        return None

    @staticmethod
    def _transform(_context: HookContext, value: Any) -> Any:
        return value

    @staticmethod
    def _guard(_context: HookContext) -> GuardDecision:
        return GuardDecision(GuardAction.ABSTAIN, "diagnostic hook abstained")

    @hookimpl
    def workbench_hook_registrations(self) -> Iterable[HookRegistration]:
        common = {
            "extension_id": "workbench.diagnostic",
            "extension_version": "1.0.0",
            "manifest_sha256": self._DIGEST,
            "priority": -10_000,
        }
        return (
            HookRegistration(
                hook_id="diagnostic.observe",
                event="app.ready",
                mode=HookMode.OBSERVE,
                handler=self._observe,
                **common,
            ),
            HookRegistration(
                hook_id="diagnostic.transform",
                event="chat.input.before_dispatch",
                mode=HookMode.TRANSFORM,
                handler=self._transform,
                **common,
            ),
            HookRegistration(
                hook_id="diagnostic.guard",
                event="run.before_start",
                mode=HookMode.GUARD,
                handler=self._guard,
                **common,
            ),
        )


AuditSink = Callable[[HookAuditRecord], Any]


class HookDispatcher:
    """Deterministic async dispatcher with mode-specific failure policy."""

    OBSERVE_TIMEOUT_SECONDS = 0.250
    MUTATING_TIMEOUT_SECONDS = 1.0

    def __init__(
        self,
        registrations: Iterable[HookRegistration] = (),
        *,
        audit_sink: Optional[AuditSink] = None,
        observe_history_size: int = 4096,
    ) -> None:
        self._registrations: dict[tuple[str, str, str], HookRegistration] = {}
        self._audit_sink = audit_sink
        self._observe_history_size = max(0, min(int(observe_history_size), 100_000))
        self._observed_keys: set[tuple[str, str]] = set()
        self._observed_order: deque[tuple[str, str]] = deque()
        self._failure_counts: dict[tuple[str, str], int] = {}
        self._sync_runner: Optional[_SyncCoroutineRunner] = None
        for registration in registrations:
            self.register(registration)

    @classmethod
    def from_builtin_plugins(
        cls,
        plugins: Iterable[object],
        *,
        audit_sink: Optional[AuditSink] = None,
    ) -> "HookDispatcher":
        manager = pluggy.PluginManager(HOOK_PROJECT_NAME)
        manager.add_hookspecs(BuiltinHookSpecs)
        for plugin in plugins:
            manager.register(plugin)
        registrations: list[HookRegistration] = []
        for batch in manager.hook.workbench_hook_registrations():
            registrations.extend(tuple(batch or ()))
        return cls(registrations, audit_sink=audit_sink)

    def register(self, registration: HookRegistration) -> None:
        if not isinstance(registration, HookRegistration):
            raise HookContractError("registration must be HookRegistration")
        key = (registration.extension_id, registration.hook_id, registration.mode.value)
        if key in self._registrations:
            raise HookContractError(
                f"duplicate hook registration: {registration.extension_id}/{registration.hook_id}"
            )
        self._registrations[key] = registration

    def unregister_extension(self, extension_id: str) -> int:
        normalized = str(extension_id or "").strip().casefold()
        keys = [key for key in self._registrations if key[0] == normalized]
        for key in keys:
            del self._registrations[key]
        return len(keys)

    def registrations(
        self,
        event: Optional[str] = None,
        mode: Optional[HookMode] = None,
    ) -> tuple[HookRegistration, ...]:
        values = (
            registration
            for registration in self._registrations.values()
            if (event is None or registration.event == event)
            and (mode is None or registration.mode is mode)
        )
        return tuple(
            sorted(values, key=lambda item: (-item.priority, item.extension_id, item.hook_id))
        )

    def snapshot(
        self,
        *,
        events: Optional[Iterable[str]] = None,
        modes: Optional[Iterable[HookMode]] = None,
    ) -> HookSnapshot:
        selected_events = set(events) if events is not None else None
        selected_modes = set(modes) if modes is not None else None
        entries = tuple(
            HookSnapshotEntry.from_registration(registration)
            for registration in self.registrations()
            if (selected_events is None or registration.event in selected_events)
            and (selected_modes is None or registration.mode in selected_modes)
        )
        return HookSnapshot(entries)

    def verify_snapshot(self, snapshot: HookSnapshot) -> None:
        """Fail when a required hook from a stored run snapshot changed."""

        if not isinstance(snapshot, HookSnapshot):
            raise HookSnapshotIncompatible("stored hook snapshot is invalid")
        current = {
            (
                item.extension_id,
                item.hook_id,
                item.extension_version,
                item.manifest_sha256,
                item.event,
                item.mode,
                item.priority,
            )
            for item in self.snapshot().entries
        }
        missing = [
            item
            for item in snapshot.entries
            if item.required
            and (
                item.extension_id,
                item.hook_id,
                item.extension_version,
                item.manifest_sha256,
                item.event,
                item.mode,
                item.priority,
            )
            not in current
        ]
        if missing:
            names = ", ".join(f"{item.extension_id}/{item.hook_id}" for item in missing[:10])
            raise HookSnapshotIncompatible(
                f"required hook snapshot is no longer compatible: {names}"
            )

    @property
    def degraded_hooks(self) -> Mapping[str, int]:
        return {
            f"{extension_id}/{hook_id}": count
            for (extension_id, hook_id), count in sorted(self._failure_counts.items())
        }

    def clear_event_history(self) -> None:
        self._observed_keys.clear()
        self._observed_order.clear()

    @staticmethod
    def _check_call(event: str, context: HookContext, mode: HookMode) -> None:
        if event not in EVENT_MODES or mode not in EVENT_MODES[event]:
            raise HookContractError(f"{event} does not support {mode.value} hooks")
        if not isinstance(context, HookContext) or context.event != event:
            raise HookContractError("HookContext.event must match the dispatched event")

    @staticmethod
    def _remaining_timeout(
        registration: HookRegistration,
        context: HookContext,
        default: float,
    ) -> float:
        timeout = registration.timeout_seconds or default
        if context.deadline_monotonic is not None:
            timeout = min(timeout, context.deadline_monotonic - time.monotonic())
        if timeout <= 0:
            raise asyncio.TimeoutError("run deadline elapsed before hook execution")
        return timeout

    @staticmethod
    async def _call_handler(handler: HookHandler, *arguments: Any) -> Any:
        if inspect.iscoroutinefunction(handler):
            return await handler(*arguments)
        result = await asyncio.to_thread(handler, *arguments)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _emit_audit(self, record: HookAuditRecord) -> None:
        if self._audit_sink is None:
            return
        try:
            if inspect.iscoroutinefunction(self._audit_sink):
                await self._audit_sink(record)
            else:
                result = await asyncio.to_thread(self._audit_sink, record)
                if inspect.isawaitable(result):
                    await result
        except Exception:
            # An audit destination must be monitored by its owner, but it must
            # never alter hook policy or take the run down.
            return

    async def _invoke(
        self,
        registration: HookRegistration,
        context: HookContext,
        *arguments: Any,
        result_validator: Optional[Callable[[Any], None]] = None,
    ) -> Any:
        started = time.monotonic()
        default = (
            self.OBSERVE_TIMEOUT_SECONDS
            if registration.mode is HookMode.OBSERVE
            else self.MUTATING_TIMEOUT_SECONDS
        )
        try:
            timeout = self._remaining_timeout(registration, context, default)

            async def execute_and_validate() -> Any:
                value = await self._call_handler(registration.handler, context, *arguments)
                if result_validator is not None:
                    result_validator(value)
                return value

            result = await asyncio.wait_for(
                execute_and_validate(),
                timeout=timeout,
            )
        except Exception as error:
            self._failure_counts[(registration.extension_id, registration.hook_id)] = (
                self._failure_counts.get((registration.extension_id, registration.hook_id), 0) + 1
            )
            await self._emit_audit(
                HookAuditRecord(
                    event=context.event,
                    event_id=context.event_id,
                    mode=registration.mode.value,
                    hook_id=registration.hook_id,
                    extension_id=registration.extension_id,
                    extension_version=registration.extension_version,
                    manifest_sha256=registration.manifest_sha256,
                    status="failed",
                    duration_ms=max(0, round((time.monotonic() - started) * 1000)),
                    error_code=(
                        HookTransformFailed.code
                        if registration.mode is HookMode.TRANSFORM
                        else HookGuardUnavailable.code
                        if registration.mode is HookMode.GUARD
                        else "HOOK_OBSERVER_FAILED"
                    ),
                    error_type=type(error).__name__,
                    error=str(redact(str(error)))[:1000],
                )
            )
            raise
        await self._emit_audit(
            HookAuditRecord(
                event=context.event,
                event_id=context.event_id,
                mode=registration.mode.value,
                hook_id=registration.hook_id,
                extension_id=registration.extension_id,
                extension_version=registration.extension_version,
                manifest_sha256=registration.manifest_sha256,
                status="completed",
                duration_ms=max(0, round((time.monotonic() - started) * 1000)),
            )
        )
        return result

    def _mark_observed(self, event: str, event_id: str) -> bool:
        if self._observe_history_size == 0:
            return True
        key = (event, event_id)
        if key in self._observed_keys:
            return False
        self._observed_keys.add(key)
        self._observed_order.append(key)
        while len(self._observed_order) > self._observe_history_size:
            self._observed_keys.discard(self._observed_order.popleft())
        return True

    async def observe(self, event: str, context: HookContext) -> None:
        self._check_call(event, context, HookMode.OBSERVE)
        if not self._mark_observed(event, context.event_id):
            return
        for registration in self.registrations(event, HookMode.OBSERVE):
            try:
                await self._invoke(registration, context)
            except Exception:
                # Observer failure is explicitly fail-open.
                continue

    async def transform_with_trace(
        self,
        event: str,
        context: HookContext,
        value: Any,
    ) -> HookTransformResult:
        self._check_call(event, context, HookMode.TRANSFORM)
        current = value
        steps: list[HookTransformStep] = []
        for registration in self.registrations(event, HookMode.TRANSFORM):
            try:
                def validate(candidate: Any) -> None:
                    if registration.value_type is not None and not isinstance(
                        candidate, registration.value_type
                    ):
                        raise TypeError("transform returned the wrong value type")
                    if registration.validator is not None:
                        valid = registration.validator(candidate)
                        if valid is False:
                            raise ValueError("transform result failed validation")

                input_digest = _value_sha256(current)
                candidate = await self._invoke(
                    registration,
                    context,
                    current,
                    result_validator=validate,
                )
                steps.append(
                    HookTransformStep(
                        extension_id=registration.extension_id,
                        hook_id=registration.hook_id,
                        extension_version=registration.extension_version,
                        manifest_sha256=registration.manifest_sha256,
                        input_sha256=input_digest,
                        output_sha256=_value_sha256(candidate),
                    )
                )
                current = candidate
            except Exception as error:
                if isinstance(error, HookTransformFailed):
                    raise
                raise HookTransformFailed(
                    f"transform hook {registration.hook_id} failed",
                    registration=registration,
                ) from error
        return HookTransformResult(current, tuple(steps))

    async def transform(self, event: str, context: HookContext, value: Any) -> Any:
        return (await self.transform_with_trace(event, context, value)).value

    async def guard(self, event: str, context: HookContext) -> GuardDecision:
        self._check_call(event, context, HookMode.GUARD)
        approval: Optional[GuardDecision] = None
        for registration in self.registrations(event, HookMode.GUARD):
            try:
                def validate(result: Any) -> None:
                    if not isinstance(result, GuardDecision):
                        raise TypeError("guard must return GuardDecision")

                result = await self._invoke(
                    registration,
                    context,
                    result_validator=validate,
                )
            except Exception as error:
                if isinstance(error, HookGuardUnavailable):
                    raise
                raise HookGuardUnavailable(
                    f"guard hook {registration.hook_id} is unavailable",
                    registration=registration,
                ) from error
            decision = replace(result, hook_id=registration.hook_id)
            if decision.action is GuardAction.DENY:
                return decision
            if decision.action is GuardAction.REQUIRE_APPROVAL and approval is None:
                approval = decision
        return approval or GuardDecision(GuardAction.ABSTAIN)

    def _run_sync(self, awaitable: Awaitable[Any]) -> Any:
        """Run a hook boundary from a synchronous host caller.

        A synchronous provider may itself be invoked from an async request
        handler.  Nested ``asyncio.run`` is invalid there, so that uncommon
        compatibility case is sent to a dedicated daemon loop.  Native async
        call sites should always prefer the regular methods above.
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)
        if self._sync_runner is None:
            self._sync_runner = _SyncCoroutineRunner()
        return self._sync_runner.run(awaitable)

    def observe_sync(self, event: str, context: HookContext) -> None:
        self._run_sync(self.observe(event, context))

    def transform_sync(self, event: str, context: HookContext, value: Any) -> Any:
        return self._run_sync(self.transform(event, context, value))

    def guard_sync(self, event: str, context: HookContext) -> GuardDecision:
        return self._run_sync(self.guard(event, context))


class _SyncCoroutineRunner:
    """Small private loop bridge for legacy synchronous model callers."""

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread = threading.Thread(
            target=self._serve,
            name="workbench-hook-sync-loop",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=2):
            raise HookRuntimeError("could not start the synchronous hook bridge")

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()

    def run(self, awaitable: Awaitable[Any]) -> Any:
        if self._loop is None or not self._loop.is_running():
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise HookRuntimeError("synchronous hook bridge is unavailable")
        future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
        return future.result()


_DEFAULT_DISPATCHER = HookDispatcher()


def configure_hook_dispatcher(dispatcher: Optional[HookDispatcher]) -> HookDispatcher:
    """Replace the process-wide dispatcher and return the configured value."""

    global _DEFAULT_DISPATCHER
    if dispatcher is not None and not isinstance(dispatcher, HookDispatcher):
        raise TypeError("dispatcher must be HookDispatcher or None")
    _DEFAULT_DISPATCHER = dispatcher or HookDispatcher()
    return _DEFAULT_DISPATCHER


def get_hook_dispatcher() -> HookDispatcher:
    return _DEFAULT_DISPATCHER


__all__ = [
    "BuiltinHookSpecs",
    "DiagnosticBuiltinHookPlugin",
    "EVENT_MODES",
    "GuardAction",
    "GuardDecision",
    "HookAuditRecord",
    "HookContext",
    "HookContractError",
    "HookDispatcher",
    "HookGuardUnavailable",
    "HookMode",
    "HookRegistration",
    "HookRuntimeError",
    "HookSnapshot",
    "HookSnapshotEntry",
    "HookSnapshotIncompatible",
    "HookTransformResult",
    "HookTransformStep",
    "HookTransformFailed",
    "configure_hook_dispatcher",
    "get_hook_dispatcher",
    "hookimpl",
    "hookspec",
]
