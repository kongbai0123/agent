from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from hook_runtime import (
    GuardAction,
    GuardDecision,
    HookContext,
    HookDispatcher,
    HookMode,
    HookRegistration,
)
from model_gateway import ModelGateway, ModelGatewayDenied


_DIGEST = hashlib.sha256(b"gateway-test").hexdigest()


def _registration(hook_id, event, mode, handler):
    return HookRegistration(
        hook_id=hook_id,
        extension_id="builtin.gateway-test",
        extension_version="1",
        manifest_sha256=_DIGEST,
        event=event,
        mode=mode,
        priority=10,
        handler=handler,
    )


def test_model_gateway_transforms_then_invokes_transport_and_observes_terminal_event():
    events = []
    dispatcher = HookDispatcher(
        [
            _registration(
                "transform",
                "model.request.transform",
                HookMode.TRANSFORM,
                lambda _context, value: {**value, "marker": "governed"},
            ),
            _registration(
                "guard",
                "model.request.guard",
                HookMode.GUARD,
                lambda _context: GuardDecision(GuardAction.ABSTAIN),
            ),
            _registration(
                "started",
                "model.started",
                HookMode.OBSERVE,
                lambda _context: events.append("started"),
            ),
            _registration(
                "completed",
                "model.completed",
                HookMode.OBSERVE,
                lambda _context: events.append("completed"),
            ),
        ]
    )
    gateway = ModelGateway(dispatcher)

    async def exercise():
        call = await gateway.start(
            context=HookContext(event="model.request.transform", run_id="run-1"),
            payload={"model": "test", "messages": []},
            transport=lambda payload: payload,
        )
        assert call.response["marker"] == "governed"
        await gateway.completed(call)
        await gateway.completed(call)

    asyncio.run(exercise())
    assert events == ["started", "completed"]


def test_model_gateway_guard_denial_never_invokes_transport():
    called = []
    dispatcher = HookDispatcher(
        [
            _registration(
                "guard",
                "model.request.guard",
                HookMode.GUARD,
                lambda _context: GuardDecision(GuardAction.DENY, "blocked"),
            )
        ]
    )
    gateway = ModelGateway(dispatcher)

    async def exercise():
        with pytest.raises(ModelGatewayDenied, match="blocked"):
            await gateway.start(
                context=HookContext(event="model.request.transform", run_id="run-1"),
                payload={"model": "test", "messages": []},
                transport=lambda payload: called.append(payload),
            )

    asyncio.run(exercise())
    assert called == []
