from __future__ import annotations

import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.hook_runtime import GuardAction, GuardDecision, HookDispatcher, HookMode, HookRegistration
from backend.tool_runtime import (
    ApprovalStatus,
    InMemoryApprovalStore,
    ToolAccess,
    ToolApprovalDecision,
    ToolApprovalError,
    ToolApprovalRequired,
    ToolArgumentsInvalidError,
    ToolDefinition,
    ToolDispatcher,
    ToolExecutionUnknownError,
    ToolPolicyDeniedError,
    ToolRegistry,
    ToolScopeState,
    ToolUnavailableError,
)


DIGEST = hashlib.sha256(b"tool extension").hexdigest()
SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}


def definition(handler, *, name="github.read", access=ToolAccess.READ, **changes):
    values = {
        "name": name,
        "description": "A test tool",
        "input_schema": SCHEMA,
        "access": access,
        "handler": handler,
        "extension_id": "builtin.github",
        "manifest_sha256": DIGEST,
        "risk_level": "external_read" if access is ToolAccess.READ else "external_write",
    }
    values.update(changes)
    return ToolDefinition(**values)


def allowed_scope(*, revision=1, resource_allowed=True, connection_id=None, resource_id=None):
    return ToolScopeState(
        installed=True,
        trusted=True,
        enabled=True,
        healthy=True,
        resource_allowed=resource_allowed,
        manifest_sha256=DIGEST,
        resource_revision=revision,
        connection_id=connection_id,
        resource_id=resource_id,
    )


def test_registry_is_project_scoped_and_arguments_are_validated_before_handler():
    calls = []
    registry = ToolRegistry()
    registry.register(definition(lambda call: calls.append(call)), project_ids=("project-a",))
    dispatcher = ToolDispatcher(registry, scope_resolver=lambda _definition, _call: allowed_scope())

    result = asyncio.run(dispatcher.execute(
        run_id="run-1", project_id="project-a", tool_name="github.read", arguments={"value": 4}
    ))
    assert result.content is None
    assert calls[0].arguments == {"value": 4}

    with pytest.raises(ToolUnavailableError):
        asyncio.run(dispatcher.execute(
            run_id="run-2", project_id="project-b", tool_name="github.read", arguments={"value": 4}
        ))
    with pytest.raises(ToolArgumentsInvalidError):
        asyncio.run(dispatcher.execute(
            run_id="run-3", project_id="project-a", tool_name="github.read", arguments={"value": "bad"}
        ))
    assert len(calls) == 1


def test_registry_keeps_same_named_project_tools_isolated_during_parallel_registration():
    registry = ToolRegistry((definition(lambda _call: {"global": True}, name="system.ping"),))
    digest_a = hashlib.sha256(b"project-a github connection").hexdigest()
    digest_b = hashlib.sha256(b"project-b github connection").hexdigest()
    schema_a = {
        "type": "object",
        "properties": {"issue": {"type": "integer"}},
        "required": ["issue"],
        "additionalProperties": False,
    }
    schema_b = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    tool_a = definition(
        lambda call: {"project": "a", "arguments": dict(call.arguments)},
        name="github.lookup",
        input_schema=schema_a,
        extension_id="connector.github-a",
        manifest_sha256=digest_a,
    )
    tool_b = definition(
        lambda call: {"project": "b", "arguments": dict(call.arguments)},
        name="github.lookup",
        input_schema=schema_b,
        extension_id="connector.github-b",
        manifest_sha256=digest_b,
    )
    barrier = threading.Barrier(2)

    def install(project_id, tool):
        barrier.wait(timeout=2)
        registry.register(tool, project_ids=(project_id,))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(install, "project-a", tool_a),
            pool.submit(install, "project-b", tool_b),
        )
        for future in futures:
            future.result(timeout=2)

    assert [item.name for item in registry.for_project("project-a")] == [
        "github.lookup",
        "system.ping",
    ]
    assert [item.name for item in registry.for_project("project-b")] == [
        "github.lookup",
        "system.ping",
    ]
    assert registry.get("project-a", "github.lookup") is tool_a
    assert registry.get("project-b", "github.lookup") is tool_b

    dispatcher = ToolDispatcher(
        registry,
        scope_resolver=lambda selected, _call: ToolScopeState(
            installed=True,
            trusted=True,
            enabled=True,
            healthy=True,
            resource_allowed=True,
            manifest_sha256=selected.manifest_sha256,
        ),
    )

    async def execute_both():
        return await asyncio.gather(
            dispatcher.execute(
                run_id="run-a",
                project_id="project-a",
                tool_name="github.lookup",
                arguments={"issue": 17},
            ),
            dispatcher.execute(
                run_id="run-b",
                project_id="project-b",
                tool_name="github.lookup",
                arguments={"query": "release"},
            ),
        )

    result_a, result_b = asyncio.run(execute_both())
    assert result_a.content == {"project": "a", "arguments": {"issue": 17}}
    assert result_b.content == {"project": "b", "arguments": {"query": "release"}}
    with pytest.raises(ToolArgumentsInvalidError):
        asyncio.run(dispatcher.execute(
            run_id="run-cross",
            project_id="project-b",
            tool_name="github.lookup",
            arguments={"issue": 17},
        ))


def test_replace_and_unregister_project_preserve_other_projects_and_globals():
    global_tool = definition(lambda _call: {}, name="system.ping")
    project_a_v1 = definition(lambda _call: {"version": "a1"}, name="github.lookup")
    project_b = definition(lambda _call: {"version": "b"}, name="github.lookup")
    project_a_v2 = definition(
        lambda _call: {"version": "a2"},
        name="github.lookup",
        description="Project A replacement",
    )
    registry = ToolRegistry((global_tool,))
    registry.register(project_a_v1, project_ids=("project-a",))
    registry.register(project_b, project_ids=("project-b",))

    registry.replace_project("project-a", (project_a_v2,))

    assert registry.get("project-a", "github.lookup") is project_a_v2
    assert registry.get("project-b", "github.lookup") is project_b
    assert registry.get("project-a", "system.ping") is global_tool
    assert registry.get("project-b", "system.ping") is global_tool

    assert registry.unregister("github.lookup", project_id="project-a") is True
    with pytest.raises(ToolUnavailableError):
        registry.get("project-a", "github.lookup")
    assert registry.get("project-b", "github.lookup") is project_b
    assert registry.get("project-a", "system.ping") is global_tool


def test_write_requires_single_use_digest_bound_approval_and_rechecks_scope():
    calls = []
    revisions = {"current": 4}
    store = InMemoryApprovalStore(id_factory=lambda: "approval-1")
    tool = definition(lambda call: calls.append(call.arguments) or {"ok": True}, access=ToolAccess.WRITE)
    registry = ToolRegistry((tool,))

    def scope(_definition, _call):
        return allowed_scope(revision=revisions["current"])

    dispatcher = ToolDispatcher(registry, scope_resolver=scope, approval_store=store)

    async def approve_then_change_scope(request):
        assert request.binding.arguments_sha256
        revisions["current"] = 5
        return ToolApprovalDecision(True, rationale="Approved exact write")

    with pytest.raises(ToolApprovalError, match="binding"):
        asyncio.run(dispatcher.execute(
            run_id="run-write",
            project_id="project-a",
            tool_name="github.read",
            arguments={"value": 7},
            approval_callback=approve_then_change_scope,
        ))
    assert calls == []


def test_write_without_callback_returns_resumable_approval_and_consumes_once():
    calls = []
    store = InMemoryApprovalStore(id_factory=lambda: "approval-resume")
    registry = ToolRegistry((definition(
        lambda call: calls.append(call.arguments) or {"ok": True},
        name="notion.update_page",
        access=ToolAccess.WRITE,
    ),))
    dispatcher = ToolDispatcher(
        registry, scope_resolver=lambda _definition, _call: allowed_scope(), approval_store=store
    )

    with pytest.raises(ToolApprovalRequired) as pending_error:
        asyncio.run(dispatcher.execute(
            run_id="run-1", project_id="project-a", tool_name="notion.update_page",
            arguments={"value": 2}, call_id="call-fixed",
        ))
    request = pending_error.value.request
    assert request.status is ApprovalStatus.PENDING
    assert calls == []

    asyncio.run(store.decide(
        request.approval_id, ToolApprovalDecision(True, "user", "Looks correct")
    ))
    result = asyncio.run(dispatcher.execute(
        run_id="run-1", project_id="project-a", tool_name="notion.update_page",
        arguments={"value": 2}, call_id="call-fixed", approval_id=request.approval_id,
    ))
    assert result.approval_id == request.approval_id
    assert calls == [{"value": 2}]
    assert asyncio.run(store.get(request.approval_id)).status is ApprovalStatus.CONSUMED

    with pytest.raises(ToolApprovalError, match="consumed"):
        asyncio.run(dispatcher.execute(
            run_id="run-1", project_id="project-a", tool_name="notion.update_page",
            arguments={"value": 2}, call_id="call-fixed", approval_id=request.approval_id,
        ))


def test_argument_transform_is_revalidated_and_guard_denial_prevents_approval():
    registrations = (
        HookRegistration(
            hook_id="bad-transform",
            extension_id="builtin.policy",
            extension_version="1",
            manifest_sha256=hashlib.sha256(b"policy").hexdigest(),
            event="tool.arguments.transform",
            mode=HookMode.TRANSFORM,
            priority=10,
            handler=lambda _context, _value: {"value": "not-an-integer"},
        ),
        HookRegistration(
            hook_id="deny-tool",
            extension_id="builtin.policy",
            extension_version="1",
            manifest_sha256=hashlib.sha256(b"policy").hexdigest(),
            event="tool.before_call",
            mode=HookMode.GUARD,
            priority=10,
            handler=lambda _context: GuardDecision(GuardAction.DENY, "blocked by policy"),
        ),
    )
    registry = ToolRegistry((definition(lambda _call: pytest.fail("must not execute")),))
    dispatcher = ToolDispatcher(
        registry,
        scope_resolver=lambda _definition, _call: allowed_scope(),
        hook_dispatcher=HookDispatcher(registrations),
    )

    with pytest.raises(ToolArgumentsInvalidError):
        asyncio.run(dispatcher.execute(
            run_id="run-1", project_id="project-a", tool_name="github.read", arguments={"value": 1}
        ))

    deny_only = ToolDispatcher(
        registry,
        scope_resolver=lambda _definition, _call: allowed_scope(),
        hook_dispatcher=HookDispatcher((registrations[1],)),
    )
    with pytest.raises(ToolPolicyDeniedError, match="blocked by policy"):
        asyncio.run(deny_only.execute(
            run_id="run-2", project_id="project-a", tool_name="github.read", arguments={"value": 1}
        ))


def test_resource_boundary_and_unknown_write_timeout_fail_closed():
    async def slow(_call):
        await asyncio.sleep(0.1)

    read_resource = definition(
        lambda _call: {}, requires_resource=True, name="github.resource"
    )
    denied_dispatcher = ToolDispatcher(
        ToolRegistry((read_resource,)),
        scope_resolver=lambda _definition, _call: allowed_scope(
            resource_allowed=False, resource_id="repo-2"
        ),
    )
    with pytest.raises(ToolPolicyDeniedError, match="not bound"):
        asyncio.run(denied_dispatcher.execute(
            run_id="run-1", project_id="project-a", tool_name="github.resource",
            arguments={"value": 1}, resource_id="repo-2",
        ))

    write = definition(
        slow, name="github.update_issue", access=ToolAccess.WRITE, timeout_seconds=0.01
    )
    timeout_dispatcher = ToolDispatcher(
        ToolRegistry((write,)), scope_resolver=lambda _definition, _call: allowed_scope()
    )
    with pytest.raises(ToolExecutionUnknownError):
        asyncio.run(timeout_dispatcher.execute(
            run_id="run-2", project_id="project-a", tool_name="github.update_issue",
            arguments={"value": 1}, approval_callback=lambda _request: True,
        ))
