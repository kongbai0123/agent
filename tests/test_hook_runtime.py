from __future__ import annotations

import asyncio
import hashlib
import time

import pytest

from backend.hook_runtime import (
    DiagnosticBuiltinHookPlugin,
    GuardAction,
    GuardDecision,
    HookContext,
    HookDispatcher,
    HookGuardUnavailable,
    HookMode,
    HookRegistration,
    HookTransformFailed,
)


DIGEST = hashlib.sha256(b"test hook extension").hexdigest()


def registration(
    hook_id: str,
    event: str,
    mode: HookMode,
    handler,
    *,
    extension_id: str = "test.extension",
    priority: int = 0,
    timeout: float | None = None,
):
    return HookRegistration(
        hook_id=hook_id,
        extension_id=extension_id,
        extension_version="1.0.0",
        manifest_sha256=DIGEST,
        event=event,
        mode=mode,
        priority=priority,
        handler=handler,
        timeout_seconds=timeout,
    )


def test_transform_order_is_priority_then_extension_and_hook_id():
    visited = []

    def append(label):
        def handler(_context, value):
            visited.append(label)
            return [*value, label]

        return handler

    dispatcher = HookDispatcher(
        (
            registration("z", "chat.input.before_dispatch", HookMode.TRANSFORM, append("z"), priority=5),
            registration(
                "b", "chat.input.before_dispatch", HookMode.TRANSFORM, append("b"),
                extension_id="alpha.extension", priority=5,
            ),
            registration(
                "a", "chat.input.before_dispatch", HookMode.TRANSFORM, append("a"),
                extension_id="alpha.extension", priority=5,
            ),
            registration("first", "chat.input.before_dispatch", HookMode.TRANSFORM, append("first"), priority=10),
        )
    )
    context = HookContext(event="chat.input.before_dispatch")

    result = asyncio.run(dispatcher.transform(context.event, context, []))

    assert result == ["first", "a", "b", "z"]
    assert visited == result


def test_observer_failure_is_audited_fail_open_and_event_is_deduplicated():
    calls = []
    audits = []

    def failing(_context):
        calls.append("failed")
        raise RuntimeError("secret sk-1234567890123456")

    def healthy(_context):
        calls.append("healthy")

    dispatcher = HookDispatcher(
        (
            registration("failing", "app.ready", HookMode.OBSERVE, failing, priority=10),
            registration("healthy", "app.ready", HookMode.OBSERVE, healthy),
        ),
        audit_sink=audits.append,
    )
    context = HookContext(event="app.ready", event_id="same-event")

    asyncio.run(dispatcher.observe(context.event, context))
    asyncio.run(dispatcher.observe(context.event, context))

    assert calls == ["failed", "healthy"]
    assert dispatcher.degraded_hooks == {"test.extension/failing": 1}
    failed = next(record for record in audits if record.status == "failed")
    assert failed.error_code == "HOOK_OBSERVER_FAILED"
    assert "sk-" not in (failed.error or "")


def test_transform_and_guard_timeout_fail_closed_with_stable_codes():
    async def slow_transform(_context, value):
        await asyncio.sleep(0.05)
        return value

    async def slow_guard(_context):
        await asyncio.sleep(0.05)
        return GuardDecision()

    transform_dispatcher = HookDispatcher(
        (registration(
            "slow-transform", "chat.input.before_dispatch", HookMode.TRANSFORM,
            slow_transform, timeout=0.01,
        ),)
    )
    guard_dispatcher = HookDispatcher(
        (registration(
            "slow-guard", "run.before_start", HookMode.GUARD, slow_guard, timeout=0.01,
        ),)
    )

    with pytest.raises(HookTransformFailed) as transform_error:
        asyncio.run(transform_dispatcher.transform(
            "chat.input.before_dispatch", HookContext(event="chat.input.before_dispatch"), "hello"
        ))
    assert transform_error.value.code == "HOOK_TRANSFORM_FAILED"

    with pytest.raises(HookGuardUnavailable) as guard_error:
        asyncio.run(guard_dispatcher.guard(
            "run.before_start", HookContext(event="run.before_start")
        ))
    assert guard_error.value.code == "HOOK_GUARD_UNAVAILABLE"


def test_guard_can_only_abstain_deny_or_require_approval_and_deny_wins():
    dispatcher = HookDispatcher(
        (
            registration(
                "approval", "run.before_start", HookMode.GUARD,
                lambda _context: GuardDecision(GuardAction.REQUIRE_APPROVAL, "review"),
                priority=10,
            ),
            registration(
                "deny", "run.before_start", HookMode.GUARD,
                lambda _context: GuardDecision(GuardAction.DENY, "blocked"),
            ),
        )
    )

    decision = asyncio.run(dispatcher.guard(
        "run.before_start", HookContext(event="run.before_start")
    ))

    assert decision.action is GuardAction.DENY
    assert decision.reason == "blocked"
    assert decision.hook_id == "deny"


def test_pluggy_builtin_discovery_and_sync_facade_work_inside_active_loop():
    dispatcher = HookDispatcher.from_builtin_plugins((DiagnosticBuiltinHookPlugin(),))
    assert len(dispatcher.registrations()) == 3

    async def invoke_sync_facade():
        context = HookContext(event="chat.input.before_dispatch")
        return dispatcher.transform_sync(context.event, context, {"safe": True})

    assert asyncio.run(invoke_sync_facade()) == {"safe": True}
