from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from email_draft_runtime import EmailDraftRuntime  # noqa: E402
from hook_runtime import (  # noqa: E402
    GuardAction,
    GuardDecision,
    HookContext,
    HookDispatcher,
    HookMode,
    HookRegistration,
    HookTransformFailed,
)
from model_gateway import (  # noqa: E402
    ModelGateway,
    configure_model_gateway,
    get_model_gateway,
    model_hook_context,
    validate_tool_free_model_payload,
)
from n8n_agent_model_runtime import (  # noqa: E402
    N8nAgentModelError,
    N8nAgentModelRuntime,
)
from n8n_agent_planner import N8nPlanModelGenerator  # noqa: E402


_DIGEST = hashlib.sha256(b"host-model-gateway-integration").hexdigest()


def _registration(hook_id, event, mode, handler):
    return HookRegistration(
        hook_id=hook_id,
        extension_id="builtin.host-model-test",
        extension_version="1",
        manifest_sha256=_DIGEST,
        event=event,
        mode=mode,
        priority=10,
        handler=handler,
    )


class Response:
    status_code = 200
    provider = "fixture"

    def __init__(self, value):
        self.value = value
        self.closed = False

    def json(self):
        return {"message": {"content": self.value}}

    def close(self):
        self.closed = True


class EmailDatabase:
    def get_session(self, session_id):
        if session_id != "email-session":
            return None
        return {"id": session_id, "mode": "email", "project_id": "project-a"}


class EmailSkills:
    def build_prompt_context(self, _session_id, _query, **_kwargs):
        return {
            "project_id": "project-a",
            "context": "",
            "skills": [],
            "truncated": False,
        }


def _agent_request():
    return {
        "security": {
            "project_id": "project-a",
            "session_id": "agent-session",
            "run_id": "agent-run",
        },
        "trusted": {
            "instruction": "Summarize the input.",
            "model": "model-a",
            "skills": [],
            "output_schema": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
        "untrusted_input": {"message": "hello"},
    }


def _email_request():
    return {
        "mode": "compose",
        "run_id": "email-run",
        "session_id": "email-session",
        "project_id": "project-a",
        "model": "model-a",
        "workflow_instruction": "Write a short reply.",
        "subject": "",
        "body_text": "Hello",
        "thread_messages": [],
        "attachments": [],
        "sender": "sender@example.test",
        "recipient": "recipient@example.test",
    }


def _planner_output():
    def choice(label, recommended=False):
        return {
            "label": label,
            "description": "Prepare a workflow for review.",
            "operation": "create_draft",
            "workflow_id": None,
            "workflow_name": None,
            "architecture": {
                "schema": "workbench.n8n.architecture.v1",
                "goal": "Prepare a workflow",
                "steps": [
                    {
                        "key": "edit",
                        "capability": "Edit Fields",
                        "purpose": "Prepare data",
                    }
                ],
                "edges": [],
                "required_inputs": [],
                "assumptions": [],
            },
            "expected_result": "A reviewable proposal.",
            "risks": ["No change occurs during planning."],
            "permissions": ["Approval is required."],
            "recommended": recommended,
        }

    return {
        "assistant_message": "Nothing has been changed.",
        "risk_summary": ["Planning is read-only."],
        "expected_result": "A reviewable proposal.",
        "permission_requirements": ["Approval is required."],
        "choices": [choice("Minimal", True), choice("Observable")],
    }


def test_all_sync_host_runtimes_apply_hooks_and_terminal_events():
    events = []
    contexts = []

    def transform(context, value):
        runtime = context.metadata["runtime"]
        contexts.append(context)
        events.append((runtime, "transform"))
        return {**value, "gateway_marker": runtime}

    def guard(context):
        events.append((context.metadata["runtime"], "guard"))
        return GuardDecision(GuardAction.ABSTAIN)

    def observe(context):
        events.append((context.metadata["runtime"], context.event))

    dispatcher = HookDispatcher(
        [
            _registration("transform", "model.request.transform", HookMode.TRANSFORM, transform),
            _registration("guard", "model.request.guard", HookMode.GUARD, guard),
            _registration("started", "model.started", HookMode.OBSERVE, observe),
            _registration("completed", "model.completed", HookMode.OBSERVE, observe),
            _registration("failed", "model.failed", HookMode.OBSERVE, observe),
        ]
    )
    previous = get_model_gateway()
    configure_model_gateway(ModelGateway(dispatcher))
    captured = []

    def agent_post(_settings, payload, **_kwargs):
        captured.append(payload)
        return Response(json.dumps({"summary": "ok"}))

    def email_post(_settings, payload, **_kwargs):
        captured.append(payload)
        return Response(
            json.dumps(
                {
                    "subject": "Hello",
                    "body_text": "Draft",
                    "summary": "",
                    "intent": "",
                    "tone": "",
                    "needs_human_attention": False,
                    "warnings": [],
                }
            )
        )

    def planner_post(_settings, payload, **_kwargs):
        captured.append(payload)
        return Response(json.dumps(_planner_output()))

    try:
        assert N8nAgentModelRuntime(lambda: {}, post_chat=agent_post)(
            _agent_request()
        ) == {"summary": "ok"}
        email = EmailDraftRuntime(
            lambda: {"default_chat_model": "model-a"},
            EmailSkills(),
            EmailDatabase(),
            email_post,
        )(_email_request())
        assert email["body_text"] == "Draft"
        planner = N8nPlanModelGenerator(
            lambda: {"default_chat_model": "model-a"}, post_chat=planner_post
        )
        assert len(
            planner(
                {
                    "phase": "architecture",
                    "project_id": "project-a",
                    "session_id": "planner-session",
                    "run_id": "planner-run",
                    "model": "model-a",
                    "structured_mode": "json_object",
                    "policy": {"mode": "restricted"},
                    "workflow_inventory": {"workflows": []},
                    "conversation": [{"role": "user", "content": "Build a workflow"}],
                }
            )["choices"]
        ) == 2
    finally:
        configure_model_gateway(previous)

    assert [payload["gateway_marker"] for payload in captured] == [
        "n8n_agent",
        "email_draft",
        "n8n_planner",
    ]
    assert events == [
        ("n8n_agent", "transform"),
        ("n8n_agent", "guard"),
        ("n8n_agent", "model.started"),
        ("n8n_agent", "model.completed"),
        ("email_draft", "transform"),
        ("email_draft", "guard"),
        ("email_draft", "model.started"),
        ("email_draft", "model.completed"),
        ("n8n_planner", "transform"),
        ("n8n_planner", "guard"),
        ("n8n_planner", "model.started"),
        ("n8n_planner", "model.completed"),
    ]
    assert {(item.project_id, item.session_id, item.run_id) for item in contexts} == {
        ("project-a", "agent-session", "agent-run"),
        ("project-a", "email-session", "email-run"),
        ("project-a", "planner-session", "planner-run"),
    }


def test_invalid_structured_response_is_failed_before_bounded_retry_completes():
    events = []
    dispatcher = HookDispatcher(
        [
            _registration(
                "started",
                "model.started",
                HookMode.OBSERVE,
                lambda context: events.append(context.event),
            ),
            _registration(
                "completed",
                "model.completed",
                HookMode.OBSERVE,
                lambda context: events.append(context.event),
            ),
            _registration(
                "failed",
                "model.failed",
                HookMode.OBSERVE,
                lambda context: events.append(context.event),
            ),
        ]
    )
    responses = iter([Response("not json"), Response(json.dumps({"summary": "ok"}))])
    previous = get_model_gateway()
    configure_model_gateway(ModelGateway(dispatcher))
    try:
        result = N8nAgentModelRuntime(
            lambda: {}, post_chat=lambda *_args, **_kwargs: next(responses)
        )(_agent_request())
    finally:
        configure_model_gateway(previous)

    assert result == {"summary": "ok"}
    assert events == [
        "model.started",
        "model.failed",
        "model.started",
        "model.completed",
    ]


def test_sync_facade_is_safe_when_called_with_an_active_event_loop():
    captured = []
    gateway = ModelGateway(HookDispatcher())

    async def exercise():
        with gateway.post_chat_sync(
            context=model_hook_context(runtime="fixture", model="model-a"),
            settings={"provider": "fixture"},
            payload={"model": "model-a", "messages": [], "stream": False},
            post_chat=lambda settings, payload, **kwargs: (
                captured.append((settings, payload, kwargs)) or Response("{}")
            ),
            post_chat_kwargs={"stream": False},
            validator=validate_tool_free_model_payload,
        ) as call:
            assert call.response.status_code == 200

    asyncio.run(exercise())
    assert captured[0][1]["model"] == "model-a"


def test_tool_free_host_policy_rejects_hook_added_tools_without_retry_or_transport():
    transforms = []
    transports = []

    def add_tools(_context, value):
        transforms.append(True)
        return {**value, "tools": [{"type": "function"}]}

    dispatcher = HookDispatcher(
        [
            _registration(
                "add-tools",
                "model.request.transform",
                HookMode.TRANSFORM,
                add_tools,
            )
        ]
    )
    previous = get_model_gateway()
    configure_model_gateway(ModelGateway(dispatcher))
    try:
        with pytest.raises(N8nAgentModelError) as caught:
            N8nAgentModelRuntime(
                lambda: {},
                post_chat=lambda *_args, **_kwargs: transports.append(True),
            )(_agent_request())
    finally:
        configure_model_gateway(previous)

    assert caught.value.code == "N8N_AGENT_MODEL_UNAVAILABLE"
    assert isinstance(caught.value.__cause__, HookTransformFailed)
    assert transforms == [True]
    assert transports == []


def test_host_runtimes_have_no_direct_injected_transport_calls():
    for relative in (
        "backend/n8n_agent_model_runtime.py",
        "backend/email_draft_runtime.py",
        "backend/n8n_agent_planner.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "self.post_chat(" not in source
