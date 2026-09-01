from __future__ import annotations

import asyncio
import copy
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import app as workbench_app
import model_client
from n8n_agent_task_runtime import N8nAgentTaskError


class _HookRecorder:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def observe(self, event: str, _context) -> None:
        self.events.append(event)


class _CoordinatorRecorder:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def sync_from_settings(self, settings, *, project_ids=None):
        assert settings == {"mcp_servers": []}
        assert tuple(project_ids or ()) == ("project-one",)
        self.events.append("mcp.sync")
        return {"status": "stopped", "running": 0, "extensions": {}}

    async def stop_all(self) -> None:
        self.events.append("mcp.stop")


class _FailingHookRecorder(_HookRecorder):
    def __init__(self, events: list[str], fail_event: str) -> None:
        super().__init__(events)
        self.fail_event = fail_event

    async def observe(self, event: str, context) -> None:
        await super().observe(event, context)
        if event == self.fail_event:
            raise RuntimeError(f"injected {event} failure")


def _patch_minimal_lifespan_runtime(monkeypatch, hook_dispatcher) -> None:
    monkeypatch.setattr(workbench_app, "hook_dispatcher", hook_dispatcher)
    monkeypatch.setattr(workbench_app, "mcp_coordinator", None)
    monkeypatch.setattr(workbench_app, "write_token_file", lambda: None)
    monkeypatch.setattr(workbench_app, "complete_startup", lambda: None)
    monkeypatch.setattr(workbench_app, "hermes_health_supervisor", None)
    monkeypatch.setattr(workbench_app, "hermes_manager_cache", None)
    monkeypatch.setattr(workbench_app, "n8n_lifecycle", None)
    monkeypatch.setattr(workbench_app, "n8n_gmail_service", None)
    monkeypatch.setattr(workbench_app, "n8n_agent_task_runtime", None)
    monkeypatch.setattr(
        workbench_app.database, "get_n8n_gmail_profile", lambda: None
    )
    monkeypatch.setattr(workbench_app, "n8n_background_tasks", set())


def test_lifespan_syncs_before_serving_and_stops_after_background_reconcile(monkeypatch):
    events: list[str] = []
    coordinator = _CoordinatorRecorder(events)

    monkeypatch.setattr(workbench_app, "hook_dispatcher", _HookRecorder(events))
    monkeypatch.setattr(workbench_app, "mcp_coordinator", coordinator)
    monkeypatch.setattr(workbench_app, "load_settings", lambda: {"mcp_servers": []})
    monkeypatch.setattr(
        workbench_app, "_extension_project_ids", lambda: ("project-one",)
    )
    monkeypatch.setattr(workbench_app, "write_token_file", lambda: None)
    monkeypatch.setattr(workbench_app, "complete_startup", lambda: None)
    monkeypatch.setattr(workbench_app, "hermes_health_supervisor", None)
    monkeypatch.setattr(workbench_app, "hermes_manager_cache", None)
    monkeypatch.setattr(workbench_app, "n8n_lifecycle", None)
    monkeypatch.setattr(workbench_app, "n8n_gmail_service", None)
    monkeypatch.setattr(workbench_app, "n8n_agent_task_runtime", None)
    monkeypatch.setattr(
        workbench_app.database, "get_n8n_gmail_profile", lambda: None
    )
    monkeypatch.setattr(workbench_app, "n8n_background_tasks", set())

    async def exercise() -> None:
        background_release = asyncio.Event()

        async with workbench_app.app_lifespan(None):
            events.append("serving")
            assert "mcp.sync" in events
            assert "mcp.stop" not in events

            async def pending_reconcile() -> None:
                await background_release.wait()
                events.append("background.done")

            task = asyncio.create_task(
                pending_reconcile(), name="contract-mcp-reconcile"
            )
            workbench_app.n8n_background_tasks.add(task)
            background_release.set()

        assert task.done()

    asyncio.run(exercise())

    assert events.index("app.starting") < events.index("mcp.sync")
    assert events.index("mcp.sync") < events.index("app.ready")
    assert events.index("app.ready") < events.index("serving")
    assert events.index("serving") < events.index("app.stopping")
    assert events.index("app.stopping") < events.index("background.done")
    assert events.index("background.done") < events.index("mcp.stop")
    assert workbench_app.application_event_loop is None


def test_lifespan_stops_owned_n8n_when_manifest_rereview_closes_gate(monkeypatch):
    events: list[str] = []

    class Lifecycle:
        running = True

        def status(self, *, probe_node=False):
            assert probe_node is False
            events.append("n8n.status")
            return {"state": "ready" if self.running else "stopped"}

        def stop(self):
            events.append("n8n.stop")
            self.running = False
            return {"state": "stopped"}

    lifecycle = Lifecycle()
    _patch_minimal_lifespan_runtime(monkeypatch, _HookRecorder(events))
    monkeypatch.setattr(workbench_app, "n8n_lifecycle", lifecycle)
    monkeypatch.setattr(
        workbench_app,
        "extension_is_enabled",
        lambda extension_id, project_id=None: False,
    )
    monkeypatch.setattr(
        workbench_app,
        "_on_managed_n8n_stop",
        lambda: events.append("n8n.revoke"),
    )

    async def exercise() -> None:
        async with workbench_app.app_lifespan(None):
            events.append("serving")
            assert lifecycle.running is False

    asyncio.run(exercise())

    assert events.count("n8n.stop") == 1
    assert events.index("n8n.status") < events.index("n8n.revoke")
    assert events.index("n8n.revoke") < events.index("n8n.stop")
    assert events.index("n8n.stop") < events.index("app.ready")
    assert events.index("app.ready") < events.index("serving")


@pytest.mark.parametrize("fail_event", ["app.starting", "app.stopping"])
def test_lifespan_restores_provider_gate_after_startup_or_shutdown_failure(
    monkeypatch,
    fail_event,
):
    events: list[str] = []
    _patch_minimal_lifespan_runtime(
        monkeypatch,
        _FailingHookRecorder(events, fail_event),
    )
    sentinel_gate = lambda _extension_id, _project_id=None: True
    original_gate = model_client.configure_provider_extension_gate(sentinel_gate)

    async def exercise() -> None:
        async with workbench_app.app_lifespan(None):
            assert model_client._PROVIDER_EXTENSION_GATE is workbench_app.extension_is_enabled

    try:
        with pytest.raises(RuntimeError, match=f"injected {fail_event} failure"):
            asyncio.run(exercise())
        assert model_client._PROVIDER_EXTENSION_GATE is sentinel_gate
        assert workbench_app.application_event_loop is None
    finally:
        model_client.configure_provider_extension_gate(original_gate)


def test_mcp_scope_resolution_is_project_bound_and_fails_closed(monkeypatch):
    manifest_digest = "a" * 64

    class Registry:
        def get(self, extension_id, project_id, *, synchronize):
            assert (extension_id, project_id, synchronize) == (
                "mcp.echo",
                "project-one",
                False,
            )
            return {
                "installed": True,
                "trusted": True,
                "effective_enabled": True,
                "manifest_sha256": manifest_digest,
            }

    class Coordinator:
        def health(self, extension_id):
            assert extension_id == "mcp.echo"
            return {
                "status": "healthy",
                "running": True,
                "projects": ["project-other"],
            }

    monkeypatch.setattr(workbench_app, "extension_registry", Registry())
    monkeypatch.setattr(workbench_app, "mcp_coordinator", Coordinator())
    definition = SimpleNamespace(
        extension_id="mcp.echo",
        name="mcp.echo.lookup",
        requires_resource=True,
        requires_connection=True,
    )
    call = SimpleNamespace(project_id="project-one", arguments={})

    scope = workbench_app._resolve_tool_scope(definition, call)

    assert scope.installed and scope.trusted and scope.enabled
    assert scope.healthy is False
    assert scope.resource_allowed is False
    assert scope.connection_enabled is False
    assert scope.manifest_sha256 == manifest_digest

    class FailingRegistry:
        def get(self, *_args, **_kwargs):
            raise workbench_app.ExtensionError("registry unavailable")

    monkeypatch.setattr(workbench_app, "extension_registry", FailingRegistry())
    with pytest.raises(workbench_app.ToolUnavailableError):
        workbench_app._resolve_tool_scope(definition, call)


def test_capability_status_tools_are_globally_registered_but_project_scoped(monkeypatch):
    names = {item.name for item in workbench_app.tool_registry.for_project("project-one")}
    assert {
        "workbench.list_capabilities",
        "workbench.get_capability_status",
    } <= names

    definition = next(
        item
        for item in workbench_app.tool_registry.for_project("project-one")
        if item.name == "workbench.get_capability_status"
    )
    monkeypatch.setattr(
        workbench_app.database,
        "get_project",
        lambda project_id: {"id": project_id} if project_id == "project-one" else None,
    )
    scope = workbench_app._resolve_tool_scope(
        definition,
        SimpleNamespace(project_id="project-one", arguments={}),
    )
    assert scope.installed is True
    assert scope.trusted is True
    assert scope.enabled is True
    assert scope.healthy is True
    assert scope.resource_allowed is True
    assert scope.manifest_sha256 == definition.manifest_sha256

    independent = workbench_app._resolve_tool_scope(
        definition,
        SimpleNamespace(project_id=workbench_app.INDEPENDENT_TOOL_SCOPE, arguments={}),
    )
    assert independent.enabled is False
    assert independent.resource_allowed is False
    assert independent.reason == "PROJECT_REQUIRED"


def test_global_mcp_extension_state_is_persisted_before_runtime_sync(monkeypatch):
    settings = {
        "mcp_servers": [
            {"id": "echo", "enabled": True},
            {"id": "other", "enabled": True},
        ]
    }
    events: list[tuple[str, object]] = []

    monkeypatch.setattr(
        workbench_app, "load_settings", lambda: copy.deepcopy(settings)
    )
    monkeypatch.setattr(
        workbench_app,
        "save_settings",
        lambda value: events.append(("save", copy.deepcopy(value))),
    )
    monkeypatch.setattr(
        workbench_app,
        "_schedule_mcp_sync",
        lambda: events.append(("sync", None)),
    )

    workbench_app._handle_extension_state_change("mcp.echo", False, {})

    assert [name for name, _value in events] == ["save", "sync"]
    saved = events[0][1]
    assert isinstance(saved, dict)
    assert saved["mcp_servers"] == [
        {"id": "echo", "enabled": False},
        {"id": "other", "enabled": True},
    ]

    events.clear()
    with pytest.raises(ValueError, match="unavailable"):
        workbench_app._handle_extension_state_change("mcp.missing", True, {})
    assert events == []


def test_mcp_state_handler_is_reversible_and_schedules_compensating_sync(
    monkeypatch,
):
    settings = {
        "mcp_servers": [
            {"id": "echo", "enabled": True},
            {"id": "other", "enabled": True},
        ]
    }
    saved_states: list[bool] = []
    sync_states: list[bool] = []

    def load():
        return copy.deepcopy(settings)

    def save(value):
        settings.clear()
        settings.update(copy.deepcopy(value))
        saved_states.append(bool(settings["mcp_servers"][0]["enabled"]))

    monkeypatch.setattr(workbench_app, "load_settings", load)
    monkeypatch.setattr(workbench_app, "save_settings", save)
    monkeypatch.setattr(
        workbench_app,
        "_schedule_mcp_sync",
        lambda: sync_states.append(
            bool(settings["mcp_servers"][0]["enabled"])
        ),
    )

    workbench_app._handle_extension_state_change("mcp.echo", False, {})
    workbench_app._handle_extension_state_change("mcp.echo", True, {})

    assert settings["mcp_servers"] == [
        {"id": "echo", "enabled": True},
        {"id": "other", "enabled": True},
    ]
    assert saved_states == [False, True]
    assert sync_states == [False, True]


def test_mcp_rollback_handler_only_schedules_digest_bound_compensation(
    monkeypatch,
):
    events: list[str] = []
    monkeypatch.setattr(
        workbench_app,
        "save_settings",
        lambda _value: pytest.fail("rollback handler rewrote MCP settings"),
    )
    monkeypatch.setattr(
        workbench_app,
        "_schedule_mcp_sync",
        lambda: events.append("sync"),
    )

    workbench_app._handle_extension_state_rollback("mcp.echo", False, {})

    assert events == ["sync"]
    assert (
        workbench_app.extension_registry.state_rollback_handler
        is workbench_app._handle_extension_state_rollback
    )


def test_n8n_rollback_handler_retries_only_fail_closed_cleanup(monkeypatch):
    events: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        workbench_app,
        "_handle_extension_state_change",
        lambda extension_id, enabled, _item: events.append(
            (extension_id, enabled)
        ),
    )

    workbench_app._handle_extension_state_rollback("builtin.n8n", False, {})
    workbench_app._handle_extension_state_rollback("builtin.n8n", True, {})

    assert events == [("builtin.n8n", False)]


def test_n8n_task_runtime_has_independent_fail_closed_extension_gate(monkeypatch):
    events: list[tuple[str, str]] = []

    class Registry:
        def require_enabled(self, extension_id, project_id):
            events.append(("gate", f"{extension_id}:{project_id}"))
            return {"effective_enabled": True}

    class Governance:
        def get_policy(self, project_id):
            events.append(("policy", project_id))
            return {"project_id": project_id, "mode": "full_audit"}

    monkeypatch.setattr(workbench_app, "extension_registry", Registry())
    monkeypatch.setattr(workbench_app, "n8n_agent_governance", Governance())

    workbench_app.n8n_agent_task_runtime._require_execution_enabled("project-one")
    assert workbench_app._resolve_live_n8n_agent_policy("project-one") == {
        "project_id": "project-one",
        "mode": "full_audit",
    }
    assert events == [
        ("gate", "builtin.n8n:project-one"),
        ("policy", "project-one"),
    ]
    assert callable(workbench_app.n8n_agent_task_runtime._execution_gate)
    runtime_type = type(workbench_app.n8n_agent_task_runtime)
    assert "self._require_execution_enabled(project_id)" in inspect.getsource(
        runtime_type.submit_task
    )
    # Check before claiming the queued task and once more immediately before
    # model execution so authority cannot be retained across queue/decrypt time.
    assert inspect.getsource(runtime_type.process_task).count(
        "self._require_execution_enabled"
    ) >= 2
    assert (
        workbench_app.n8n_agent_task_runtime._policy_resolver
        is workbench_app._resolve_live_n8n_agent_policy
    )

    class DisabledRegistry:
        def require_enabled(self, _extension_id, _project_id):
            raise workbench_app.ExtensionError("disabled")

    monkeypatch.setattr(workbench_app, "extension_registry", DisabledRegistry())
    with pytest.raises(N8nAgentTaskError) as denied:
        workbench_app.n8n_agent_task_runtime._require_execution_enabled(
            "project-one"
        )
    assert getattr(denied.value, "code", None) == "EXTENSION_ERROR"


def test_host_tool_runtime_routes_connector_context_but_not_mcp(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    class Connectors:
        def resolve_host_call_context(self, project_id, definition, arguments):
            calls.append((project_id, definition.extension_id, dict(arguments)))
            return {"connection_id": "connection-one", "resource_id": "repo-one"}

    monkeypatch.setattr(workbench_app, "connector_service", Connectors())
    connector = SimpleNamespace(extension_id="connector.github")
    mcp = SimpleNamespace(extension_id="mcp.echo")

    async def exercise():
        connector_context = await workbench_app.host_tool_runtime.resolve_call_context(
            "project-one", connector, {"repository": "repo-one"}
        )
        mcp_context = await workbench_app.host_tool_runtime.resolve_call_context(
            "project-one", mcp, {"value": 1}
        )
        return connector_context, mcp_context

    connector_context, mcp_context = asyncio.run(exercise())

    assert connector_context.connection_id == "connection-one"
    assert connector_context.resource_id == "repo-one"
    assert mcp_context.connection_id is None
    assert mcp_context.resource_id is None
    assert calls == [
        ("project-one", "connector.github", {"repository": "repo-one"})
    ]
    assert workbench_app.host_tool_runtime.dispatcher is workbench_app.tool_dispatcher
    assert workbench_app.tool_dispatcher.hooks is workbench_app.hook_dispatcher


@pytest.mark.parametrize(
    ("level", "access", "tool_name", "risk_level", "expected"),
    [
        ("blocked", "read", "mcp.browser.browser_snapshot", "external_read", "deny"),
        ("restricted", "read", "mcp.browser.browser_snapshot", "external_read", "allow"),
        ("restricted", "write", "mcp.browser.browser_tabs", "external_write", "allow"),
        ("restricted", "write", "mcp.browser.browser_type", "external_write", "require_approval"),
        ("restricted", "write", "connector.github.issue_update", "irreversible", "require_approval"),
        ("open", "write", "connector.github.issue_update", "irreversible", "allow"),
    ],
)
def test_project_permission_level_controls_fixed_tool_policy(
    monkeypatch, level, access, tool_name, risk_level, expected
):
    class Registry:
        def get(self, extension_id, project_id, *, synchronize=False):
            assert extension_id == "mcp.browser" or extension_id == "connector.github"
            assert project_id == "project-one"
            assert synchronize is False
            return {"project_permission": {"level": level, "revision": 3}}

    monkeypatch.setattr(workbench_app, "extension_registry", Registry())
    # This test isolates the legacy fixed extension policy. The unified
    # integration gate has separate scope/revision coverage and is intersected
    # with this result in production.
    monkeypatch.setattr(workbench_app, "_unified_tool_permission", lambda *_args: None)
    definition = SimpleNamespace(
        extension_id="connector.github" if tool_name.startswith("connector.") else "mcp.browser",
        name=tool_name,
        risk_level=risk_level,
        access=workbench_app.ToolAccess(access),
    )
    call = SimpleNamespace(project_id="project-one")
    decision = workbench_app._evaluate_tool_permission(definition, call, None)
    assert decision.action.value == expected


def test_independent_chat_cannot_bypass_project_scoped_integration_policy(monkeypatch):
    calls = []

    class Registry:
        def get(self, extension_id, project_id, *, synchronize=False):
            calls.append((extension_id, project_id, synchronize))
            return {"project_permission": {"level": "open", "revision": 2}}

    monkeypatch.setattr(workbench_app, "extension_registry", Registry())
    definition = SimpleNamespace(
        extension_id="mcp.browser-playwright",
        name="mcp.browser.browser_type",
        risk_level="external_write",
        access=workbench_app.ToolAccess.WRITE,
    )
    call = SimpleNamespace(project_id=workbench_app.INDEPENDENT_TOOL_SCOPE)

    decision = workbench_app._evaluate_tool_permission(definition, call, None)

    assert decision.action.value == "deny"
    assert calls == []
