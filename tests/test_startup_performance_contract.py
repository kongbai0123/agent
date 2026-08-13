from pathlib import Path
import asyncio
import sys


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
LAUNCHER = (ROOT / "scripts" / "start_workbench.ps1").read_text(encoding="utf-8")
N8N_UI = (ROOT / "frontend" / "n8n-workflows.js").read_text(encoding="utf-8")
GOVERNANCE_UI = (ROOT / "frontend" / "n8n-agent-governance.js").read_text(encoding="utf-8")
APP_UI = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")


def test_optional_integrations_do_not_block_asgi_readiness():
    lifespan = APP[APP.index("async def app_lifespan"):APP.index("ensure_runtime_dirs()")]
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
    assert "window.setTimeout(startBackgroundSync, 2500)" in init_slice
    assert "void refreshRuns({ quiet: true })" not in init_slice
    governance_init = GOVERNANCE_UI[GOVERNANCE_UI.index("function init(options"):GOVERNANCE_UI.index("window.workbenchN8nGovernance")]
    assert "renderProjects();\n        state.refreshTimer" in governance_init
    assert "renderProjects(); void refreshAll()" not in governance_init


def test_initial_project_and_provider_status_load_in_parallel_with_shared_inventory_cache():
    init_slice = APP_UI[APP_UI.index("async function initApp()") : APP_UI.index("function getOllamaConnectionStatus")]
    assert "const statusPromise = checkSystemStatus();" in init_slice
    assert "const sessionsPromise = loadSessions();" in init_slice
    assert "await sessionsPromise;" in init_slice
    assert "_MODEL_INVENTORY_CACHE_SECONDS" in APP
    assert "_model_inventory_cache_lock" in APP
    assert "_model_inventory_cache_key(settings)" in APP
