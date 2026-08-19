from pathlib import Path
import asyncio
from dataclasses import replace
import json
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
LAUNCHER = (ROOT / "scripts" / "start_workbench.ps1").read_text(encoding="utf-8")
N8N_UI = (ROOT / "frontend" / "n8n-workflows.js").read_text(encoding="utf-8")
GOVERNANCE_UI = (ROOT / "frontend" / "n8n-agent-governance.js").read_text(encoding="utf-8")
APP_UI = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")


def test_optional_integrations_do_not_block_asgi_readiness():
    # The outer lifespan owns process-wide gate cleanup, while the inner
    # runtime lifespan owns optional integration startup.  Inspect both so
    # this contract follows the actual startup boundary after that split.
    lifespan = APP[
        APP.index("async def _app_runtime_lifespan") : APP.index("ensure_runtime_dirs()")
    ]
    assert "await supervisor.probe_once()" not in lifespan
    assert "_schedule_n8n_runtime_start(lifecycle)" in lifespan
    assert "await asyncio.to_thread(lifecycle.start)" not in lifespan
    assert 'bool(profile.get("auto_start"))' in lifespan


def test_n8n_startup_requires_explicit_profile_auto_start(monkeypatch):
    backend = ROOT / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    import app as workbench_app

    class FakeLifecycle:
        def __init__(self):
            self.started = 0
            self.stopped = 0

        def start(self):
            self.started += 1

        def status(self):
            return {"state": "ready" if self.started else "stopped"}

        def stop(self):
            self.stopped += 1

    monkeypatch.setattr(workbench_app, "write_token_file", lambda: None)
    monkeypatch.setattr(workbench_app, "complete_startup", lambda: None)
    monkeypatch.setattr(workbench_app, "hermes_health_supervisor", None)
    monkeypatch.setattr(workbench_app, "hermes_manager_cache", None)
    monkeypatch.setattr(workbench_app, "n8n_gmail_service", None)
    monkeypatch.setattr(
        workbench_app,
        "extension_is_enabled",
        lambda extension_id, project_id=None: extension_id == "builtin.n8n",
    )

    async def exercise(auto_start):
        lifecycle = FakeLifecycle()
        monkeypatch.setattr(workbench_app, "n8n_lifecycle", lifecycle)
        monkeypatch.setattr(
            workbench_app.database,
            "get_n8n_gmail_profile",
            lambda: {"enabled": 1, "auto_start": int(auto_start)},
        )
        async with workbench_app.app_lifespan(None):
            await asyncio.sleep(0.02)
        return lifecycle

    default_off = asyncio.run(exercise(False))
    opted_in = asyncio.run(exercise(True))
    assert default_off.started == 0
    assert default_off.stopped == 0
    assert opted_in.started == 1
    assert opted_in.stopped == 1


def test_launcher_gracefully_stops_owned_n8n_before_killing_backend():
    shutdown = LAUNCHER[LAUNCHER.index("function Stop-ManagedN8nBeforeBackend"):]
    assert '"$backendUrl/api/integrations/n8n/stop"' in shutdown
    assert '"X-Workbench-Token" = $sessionToken' in shutdown
    assert "-TimeoutSec 50" in shutdown
    finalizer = shutdown[shutdown.index("finally {"):]
    assert finalizer.index("Stop-ManagedN8nBeforeBackend") < finalizer.index("Stop-OwnedProcess -Process $backendProcess")


def test_launcher_exposes_core_before_waiting_for_hermes_readiness():
    initialize = LAUNCHER.index("Initialize-HermesBackendEnvironment", LAUNCHER.index("Startup screen opened"))
    backend = LAUNCHER.index('$backendArgs = @("-m", "uvicorn"', initialize)
    ready = LAUNCHER.index('Write-LauncherLog "Workbench core is ready', backend)
    sidecar = LAUNCHER.index("Start-ManagedHermesSidecar -UseResolvedPlan", ready)
    assert initialize < backend < ready < sidecar
    assert "Preload it so the backend can become available" in LAUNCHER


def test_n8n_frontend_work_is_deferred_off_the_critical_chat_path():
    init_slice = N8N_UI[N8N_UI.index("function init(options"):N8N_UI.index("window.workbenchN8nWorkflows")]
    assert "window.setTimeout(() => void startBackgroundSyncWhenEnabled(), 2500)" in init_slice
    assert "if (n8nExtensionReady()) startBackgroundSync()" in N8N_UI
    assert "if (!n8nExtensionReady() && state.backgroundStarted) stopBackgroundSync()" in N8N_UI
    assert "void refreshRuns({ quiet: true })" not in init_slice
    governance_init = GOVERNANCE_UI[GOVERNANCE_UI.index("function init(options"):GOVERNANCE_UI.index("window.workbenchN8nGovernance")]
    assert "state.initialized = true; renderProjects();" in governance_init
    assert "state.refreshTimer = window.setInterval" in governance_init
    assert "renderProjects(); void refreshAll()" not in governance_init


def test_initial_project_and_provider_status_load_in_parallel_with_shared_inventory_cache():
    init_slice = APP_UI[APP_UI.index("async function initApp()") : APP_UI.index("function getOllamaConnectionStatus")]
    assert "const statusPromise = checkSystemStatus();" in init_slice
    assert "const sessionsPromise = loadSessions();" in init_slice
    assert "await sessionsPromise;" in init_slice
    assert "_MODEL_INVENTORY_CACHE_SECONDS" in APP
    assert "_model_inventory_cache_lock" in APP
    assert "_model_inventory_cache_key(settings)" in APP


def test_n8n_agent_runtime_uses_live_policy_catalog_and_one_lazy_graph(monkeypatch):
    backend = ROOT / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    import app as workbench_app

    assert workbench_app.n8n_agent_governance.graph_authoring is workbench_app.n8n_graph_authoring
    assert workbench_app.n8n_agent_planner.graph_authoring is workbench_app.n8n_graph_authoring
    assert (
        workbench_app.n8n_agent_planner.generator.catalog_search.__self__
        is workbench_app.n8n_agent_governance
    )
    assert (
        workbench_app.n8n_agent_governance.policy_change_callback
        is workbench_app._on_n8n_agent_policy_change
    )
    assert (
        workbench_app.n8n_agent_governance.workflow_change_callback
        is workbench_app._on_n8n_agent_workflow_change
    )

    class LiveGovernance:
        def get_policy(self, project_id):
            return {"project_id": project_id, "mode": "full_audit", "revision": 7}

    monkeypatch.setattr(workbench_app, "n8n_agent_governance", LiveGovernance())
    policy = workbench_app.n8n_agent_task_runtime._policy_resolver("project-live")
    assert policy == {"project_id": "project-live", "mode": "full_audit", "revision": 7}

    monkeypatch.setenv("WORKBENCH_N8N_APPROVAL_GATE_WORKFLOW_ID", "approval-gate")
    protected = workbench_app._configured_n8n_protected_workflows()
    assert protected["workbench.approval"] == {
        "workflow_id": "approval-gate",
        "name": "Workbench Approval Gate v1",
    }


def test_live_workflow_revision_resolver_reads_exact_managed_active_version(
    tmp_path, monkeypatch
):
    backend = ROOT / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    import app as workbench_app

    n8n_dir = tmp_path / "managed-n8n"
    n8n_dir.mkdir()
    with sqlite3.connect(n8n_dir / "database.sqlite") as conn:
        conn.execute(
            """
            CREATE TABLE workflow_entity(
                id TEXT PRIMARY KEY, active INTEGER NOT NULL, activeVersionId TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO workflow_entity VALUES(?,?,?)",
            ("workflow-one", 1, "active-version-exact-001"),
        )
    monkeypatch.setattr(
        workbench_app.n8n_lifecycle,
        "paths",
        replace(workbench_app.n8n_lifecycle.paths, n8n_dir=n8n_dir),
    )
    assert workbench_app._resolve_live_managed_n8n_workflow_revision(
        "workflow-one"
    ) == {
        "active": True,
        "active_version_id": "active-version-exact-001",
    }


def test_configured_agent_bridge_attestation_matches_exact_safe_ids(monkeypatch):
    backend = ROOT / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    import app as workbench_app

    monkeypatch.setenv("WORKBENCH_N8N_AGENT_BRIDGE_WORKFLOW_ID", "agent-bridge-id")
    monkeypatch.setenv("WORKBENCH_N8N_APPROVAL_GATE_WORKFLOW_ID", "approval-gate-id")
    monkeypatch.setitem(
        workbench_app.n8n_graph_authoring.protected_workflows,
        "workbench.agent",
        {"workflow_id": "agent-bridge-id", "name": "Workbench Agent Bridge v1"},
    )
    monkeypatch.setitem(
        workbench_app.n8n_graph_authoring.protected_workflows,
        "workbench.approval",
        {"workflow_id": "approval-gate-id", "name": "Workbench Approval Gate v1"},
    )

    def inspected(_paths):
        return {
            "ready": True,
            "blockers": [],
            "workflows": {
                workbench_app.AGENT_BRIDGE_TEMPLATE_ID: {
                    "workflow_id": "agent-bridge-id", "present": True,
                    "published": True, "active": False, "valid": True,
                },
                workbench_app.APPROVAL_GATE_TEMPLATE_ID: {
                    "workflow_id": "approval-gate-id", "present": True,
                    "published": True, "active": False, "valid": True,
                },
            },
            "credential_bindings": {
                "hmac_bound": True, "hmac_configured": True,
                "credential_id": "must-not-be-returned",
            },
        }

    monkeypatch.setattr(
        workbench_app, "inspect_agent_bridge_workflows_readiness", inspected
    )
    report = workbench_app._inspect_configured_n8n_agent_bridges()
    assert report["ready"] is True
    assert "must-not-be-returned" not in json.dumps(report)

    monkeypatch.setenv(
        "WORKBENCH_N8N_APPROVAL_GATE_WORKFLOW_ID", "wrong-approval-id"
    )
    drifted = workbench_app._inspect_configured_n8n_agent_bridges()
    assert drifted["ready"] is False
    assert (
        f"{workbench_app.APPROVAL_GATE_TEMPLATE_ID}_configured_id_mismatch"
        in drifted["blockers"]
    )


def test_graph_binding_activator_selects_only_exact_active_agent_nodes(monkeypatch):
    backend = ROOT / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    import app as workbench_app

    monkeypatch.setattr(
        workbench_app,
        "_require_configured_n8n_agent_bridges",
        lambda: {"ready": True},
    )

    monkeypatch.setitem(
        workbench_app.n8n_graph_authoring.protected_workflows,
        "workbench.agent",
        {"workflow_id": "protected-agent", "name": "Workbench Agent Bridge v1"},
    )
    monkeypatch.setattr(
        workbench_app.n8n_agent_task_runtime,
        "list_bindings",
        lambda project_id: [
            {
                "agent_binding_id": "binding-exact",
                "project_id": project_id,
                "workflow_id": "workflow-one",
                "node_id": "agent-node",
                "workflow_revision": "wbr_compiled_revision_token_001",
                "active": False,
            },
            {
                "agent_binding_id": "binding-other-workflow",
                "project_id": project_id,
                "workflow_id": "workflow-two",
                "node_id": "other-node",
                "workflow_revision": "wbr_other_revision_token_002",
                "active": False,
            },
        ],
    )
    activated = []

    def activate(workflow_id, revision, binding_ids, project_id):
        activated.append((workflow_id, revision, list(binding_ids), project_id))
        return [{"agent_binding_id": item, "active": True} for item in binding_ids]

    monkeypatch.setattr(workbench_app.n8n_agent_task_runtime, "activate_bindings", activate)
    result = workbench_app._activate_n8n_graph_bindings(
        {
            "project_id": "project-one",
            "workflow_id": "workflow-one",
            "workflow_revision": "active-version-one",
            "workflow": {
                "nodes": [
                    {
                        "id": "agent-node",
                        "type": "n8n-nodes-base.executeWorkflow",
                        "parameters": {
                            "workflowId": {"value": "protected-agent"},
                            "workflowInputs": {
                                "value": {
                                    "agent_binding_id": "binding-exact",
                                    "workflow_revision": "wbr_compiled_revision_token_001",
                                }
                            },
                        },
                    },
                    {
                        "id": "untrusted-node",
                        "type": "n8n-nodes-base.executeWorkflow",
                        "parameters": {
                            "workflowId": {"value": "some-other-workflow"},
                            "workflowInputs": {
                                "value": {
                                    "agent_binding_id": "binding-other-workflow",
                                    "workflow_revision": "wbr_other_revision_token_002",
                                }
                            },
                        },
                    },
                ]
            },
        }
    )

    assert result == [{"agent_binding_id": "binding-exact", "active": True}]
    assert activated == [
        ("workflow-one", "active-version-one", ["binding-exact"], "project-one")
    ]


def test_workflow_change_callback_deactivates_invalidated_exact_bindings(monkeypatch):
    backend = ROOT / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    import app as workbench_app

    monkeypatch.setattr(
        workbench_app.n8n_agent_task_runtime,
        "list_bindings",
        lambda project_id: [
            {
                "agent_binding_id": "binding-active",
                "project_id": project_id,
                "workflow_id": "workflow-one",
                "active": True,
            },
            {
                "agent_binding_id": "binding-inactive",
                "project_id": project_id,
                "workflow_id": "workflow-one",
                "active": False,
            },
            {
                "agent_binding_id": "binding-other",
                "project_id": project_id,
                "workflow_id": "workflow-two",
                "active": True,
            },
        ],
    )
    deactivated = []
    notified = []
    monkeypatch.setattr(
        workbench_app.n8n_agent_task_runtime,
        "deactivate_binding",
        lambda binding_id, project_id: deactivated.append((binding_id, project_id)),
    )
    monkeypatch.setattr(
        workbench_app.n8n_agent_task_runtime,
        "notify_workflow_changed",
        lambda project_id, workflow_id, reason: notified.append(
            (project_id, workflow_id, reason)
        ),
    )

    workbench_app._on_n8n_agent_workflow_change(
        {
            "project_id": "project-one",
            "workflow_id": "workflow-one",
            "operation": "update_draft",
        }
    )

    assert deactivated == [("binding-active", "project-one")]
    assert notified == [
        ("project-one", "workflow-one", "workflow_update_draft")
    ]
