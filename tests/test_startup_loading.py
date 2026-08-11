import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOADING_PAGE = ROOT / "frontend" / "loading.html"
LAUNCHER = ROOT / "scripts" / "start_workbench.ps1"


class StartupLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = LOADING_PAGE.read_text(encoding="utf-8")
        cls.launcher = LAUNCHER.read_text(encoding="utf-8")

    def test_loading_page_exposes_stage_progress_and_slow_feedback(self):
        self.assertIn('role="progressbar"', self.page)
        self.assertIn("啟動階段進度", self.page)
        self.assertIn("已等待 0 秒", self.page)
        self.assertIn("載入後端與專案索引", self.page)
        self.assertIn("檢查模型服務", self.page)
        self.assertIn("準備工作區", self.page)
        self.assertIn("不代表程式已當機", self.page)
        self.assertIn("立即重新檢查", self.page)

    def test_loading_page_checks_backend_without_blocking_on_model_status(self):
        self.assertIn("/startup-status.json", self.page)
        self.assertIn("current_documents", self.page)
        self.assertIn("eta_seconds", self.page)
        health_check = self.page.index('request("/api/health"')
        redirect = self.page.index("location.replace(targetUrl.href)")
        self.assertLess(health_check, redirect)
        self.assertNotIn('request("/api/status"', self.page)

    def test_loading_page_restricts_navigation_and_api_to_local_origins(self):
        self.assertIn('["127.0.0.1", "localhost"]', self.page)
        self.assertIn("candidate.origin === backendUrl", self.page)
        self.assertIn('new URL("index.html", `${backendUrl}/`)', self.page)
        self.assertIn('cache: "no-store"', self.page)

    def test_launcher_opens_loading_page_before_starting_backend(self):
        open_loading_page = self.launcher.index("--app=$loadingUrl")
        start_backend = self.launcher.index("$backendProcess = Start-Process")
        wait_backend = self.launcher.index("Wait-BackendReady -Url")
        self.assertLess(open_loading_page, start_backend)
        self.assertLess(start_backend, wait_backend)
        self.assertIn('if ($readyState -eq "window_closed")', self.launcher)
        self.assertIn("Stop-OwnedProcess -Process $browserProcess", self.launcher)
        self.assertIn("startup_http_server.py", self.launcher)
        self.assertIn("WORKBENCH_STARTUP_RUN_ID", self.launcher)
        self.assertIn('"--window-size=1920,1080"', self.launcher)
        self.assertIn("Resolve-LaunchedBrowserProcess", self.launcher)
        self.assertIn("Stop-StaleLauncherBrowser", self.launcher)

    def test_loading_page_reports_when_launcher_service_disappears(self):
        self.assertIn("consecutiveStatusFailures", self.page)
        self.assertIn("無法連線到啟動服務", self.page)
        self.assertIn("連接埠已變更", self.page)

    def test_launcher_detects_ipv4_ipv6_and_wildcard_port_conflicts(self):
        port_check = self.launcher[
            self.launcher.index("function Test-TcpPortAvailable"):
            self.launcher.index("function Wait-HttpReady")
        ]
        self.assertIn("Get-NetTCPConnection", port_check)
        self.assertIn("-State Listen", port_check)
        self.assertIn("if ($null -ne $existingListener) { return $false }", port_check)
        listener_lookup = self.launcher[
            self.launcher.index("function Get-LoopbackListenerProcess"):
            self.launcher.index("function Stop-RecognizedWorkbenchService")
        ]
        self.assertNotIn('Where-Object { $_.LocalAddress -eq "127.0.0.1" }', listener_lookup)

    def test_launcher_has_service_discovery_functions(self):
        discovery_block = self.launcher[
            self.launcher.index("function Get-JsonObjectFromFile"):
            self.launcher.index("function Wait-HttpReady")
        ]
        self.assertIn("Get-PortCandidatesFromConfig", discovery_block)
        self.assertIn("Get-CachedCandidatePorts", discovery_block)
        self.assertIn("Get-DiscoveryCandidatePorts", discovery_block)
        self.assertIn("Resolve-ServicePort", discovery_block)
        self.assertIn("Test-ServiceHealthy", discovery_block)
        self.assertIn("Update-PortDiscoveryCache", discovery_block)
        self.assertIn("Write-PortDiscoverySummary", discovery_block)
        self.assertIn("server-discovery-config.json", self.launcher)
        self.assertIn("server-discovery-cache.json", self.launcher)

    def test_launcher_uses_discovery_port_resolution(self):
        run_block = self.launcher[
            self.launcher.index("if ($BackendPort -eq $FrontendPort)"):
            self.launcher.index("encodedBackendUrl")
        ]
        self.assertIn("$backendPlan = Resolve-ServicePort -Kind \"backend\" -RequestedPort $BackendPort", run_block)
        self.assertIn("$frontendPlan = Resolve-ServicePort -Kind \"frontend\" -RequestedPort $FrontendPort", run_block)
        self.assertIn("Update-PortDiscoveryCache -Kind \"backend\"", run_block)
        self.assertIn("Update-PortDiscoveryCache -Kind \"frontend\"", run_block)
        self.assertIn("Write-PortDiscoverySummary -Kind \"backend\"", run_block)
        self.assertIn("Write-PortDiscoverySummary -Kind \"frontend\"", run_block)
        self.assertIn("backendUrl = \"http://127.0.0.1:$BackendPort\"", run_block)
        self.assertIn("Frontend port resolution: using configured/cached candidate", run_block)

    def test_discovery_config_example_exists_with_candidates(self):
        config_example = ROOT / "runtime" / "server-discovery-config.json.example"
        self.assertTrue(config_example.exists())
        content = config_example.read_text(encoding="utf-8")
        self.assertIn('"backend": [', content)
        self.assertIn('"frontend": [', content)

    def test_launcher_does_not_register_an_inherited_job_twice(self):
        native_job_api = self.launcher[
            self.launcher.index("function Initialize-KillOnCloseJob"):
            self.launcher.index("function Find-Browser")
        ]
        self.assertIn("IsProcessInJob", native_job_api)
        self.assertIn("if ($alreadyRegistered)", native_job_api)
        inherited = native_job_api.index("if ($alreadyRegistered)")
        assign = native_job_api.index("AssignProcessToJobObject($Job, $Process.Handle)")
        self.assertLess(inherited, assign)
        self.assertIn("already inherited automatic cleanup registration", native_job_api)
    def test_launcher_adopts_a_reparented_worker_from_its_listener(self):
        worker_block = self.launcher[
            self.launcher.index("function Get-ServiceWorker"):
            self.launcher.index("function Stop-OwnedProcess")
        ]
        self.assertIn("netstat.exe -ano -p tcp", worker_block)
        self.assertIn("Get-Process -Id ([int]$Matches[1])", worker_block)
        self.assertIn("if (-not $SmokeTest -and -not $NoBrowser)", self.launcher)

    def test_second_launch_opens_or_activates_the_existing_workbench(self):
        helper_block = self.launcher[
            self.launcher.index("function Find-HealthyWorkbenchBackendPort"):
            self.launcher.index("function Get-ServiceWorker")
        ]
        self.assertIn('Test-ServiceHealthy -Kind "backend"', helper_block)
        self.assertIn("Get-LauncherBrowserProcess", helper_block)
        self.assertIn("AppActivate($existingBrowser.Id)", helper_block)
        self.assertIn('"--app=$existingUrl"', helper_block)
        mutex_block = self.launcher[
            self.launcher.index("$hasMutex = Wait-ForLauncherMutex"):
            self.launcher.index("if (-not (Test-Path -LiteralPath $pythonPath))")
        ]
        self.assertIn("Open-ExistingWorkbenchWindow -RequestedPort $BackendPort", mutex_block)
        self.assertIn("if (-not $SmokeTest -and -not $NoBrowser)", mutex_block)

    def test_gui_launch_takes_over_a_headless_launcher(self):
        helper_block = self.launcher[
            self.launcher.index("function Get-HeadlessWorkbenchLauncher"):
            self.launcher.index("function Get-ServiceWorker")
        ]
        self.assertIn("start_workbench.ps1", helper_block)
        self.assertIn("-NoBrowser", helper_block)
        self.assertIn("Wait-ForLauncherMutex", helper_block)
        mutex_block = self.launcher[
            self.launcher.index("$hasMutex = Wait-ForLauncherMutex"):
            self.launcher.index("if (-not (Test-Path -LiteralPath $pythonPath))")
        ]
        self.assertIn("GUI launch is taking over headless launcher", mutex_block)
        self.assertIn("Stop-Process -Id $headlessProcess.Id", mutex_block)
        self.assertIn("TimeoutMilliseconds 15000", mutex_block)
        self.assertIn("continuing with an interactive workbench launch", mutex_block)


    def test_discovery_summary_contains_required_fields(self):
        summary_block = self.launcher[self.launcher.index("function Write-PortDiscoverySummary"):self.launcher.index("function Find-Browser")]
        self.assertIn("candidate_hit=", summary_block)
        self.assertIn("health_ok=", summary_block)
        self.assertIn("fallback_used=", summary_block)
        self.assertIn("checked_ports=", summary_block)


if __name__ == "__main__":
    unittest.main()
