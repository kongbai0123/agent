import os
import struct
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "LocalAIWorkbench.exe"
SOURCE = ROOT / "launcher" / "LocalAIWorkbenchLauncher.cs"
ICON = ROOT / "launcher" / "LocalAIWorkbench.ico"
BUILD_SCRIPT = ROOT / "scripts" / "build_launcher.ps1"
BOOTSTRAP = ROOT / "scripts" / "launch_workbench.ps1"
UPDATER = ROOT / "scripts" / "update_workbench.ps1"
START_SCRIPT = ROOT / "scripts" / "start_workbench.ps1"
POWERSHELL = Path(
    os.environ.get("SystemRoot", r"C:\Windows")
) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _pe_subsystem(payload: bytes) -> int:
    pe_offset = struct.unpack_from("<I", payload, 0x3C)[0]
    if payload[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise AssertionError("missing PE signature")
    optional_header = pe_offset + 24
    return struct.unpack_from("<H", payload, optional_header + 68)[0]


class WindowsLauncherTests(unittest.TestCase):
    def test_packaged_launcher_is_a_windows_gui_executable(self):
        payload = LAUNCHER.read_bytes()
        self.assertEqual(payload[:2], b"MZ")
        self.assertEqual(_pe_subsystem(payload), 2, "launcher must use the GUI subsystem")

    def test_launcher_has_reproducible_source_icon_and_build_script(self):
        self.assertTrue(SOURCE.exists())
        self.assertTrue(ICON.exists())
        self.assertTrue(BUILD_SCRIPT.exists())
        icon_payload = ICON.read_bytes()
        reserved, image_type, image_count = struct.unpack_from("<HHH", icon_payload)
        self.assertEqual((reserved, image_type), (0, 1))
        self.assertGreaterEqual(image_count, 7)

    def test_launcher_only_forwards_allowlisted_options(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('"--skip-update"', source)
        self.assertIn('"--smoke-test"', source)
        self.assertIn('"--backend-port"', source)
        self.assertIn('"--frontend-port"', source)
        self.assertIn("Unsupported launcher option", source)
        self.assertIn("WindowsPowerShell", source)
        self.assertIn('Path.Combine(projectRoot, "scripts", "launch_workbench.ps1")', source)

    def test_new_launcher_replaces_the_legacy_root_entrypoints(self):
        self.assertFalse((ROOT / "Start_LLM.bat").exists())
        self.assertFalse((ROOT / "Start_LLM.vbs").exists())
        self.assertTrue(BOOTSTRAP.exists())
        self.assertTrue(UPDATER.exists())
        self.assertTrue((ROOT / "scripts" / "start_workbench.ps1").exists())

    def test_updater_is_fail_closed(self):
        updater = UPDATER.read_text(encoding="utf-8")
        self.assertIn("https://github.com/kongbai0123/agent", updater)
        self.assertIn("--untracked-files=all", updater)
        self.assertIn("--ff-only", updater)
        self.assertIn('$state.remote_commit', updater)
        self.assertIn("Assert-ApplyPreconditions", updater)
        self.assertIn("Get-CurrentBranch", updater)
        self.assertIn("Release-LauncherMaintenanceMutex", updater)
        self.assertIn("Tracked project files contain local changes", updater)
        self.assertIn("histories have diverged", updater)
        self.assertIn("Invoke-StagedValidation", updater)
        self.assertNotIn("reset --hard", updater)
        self.assertNotIn("git pull", updater)
        self.assertNotIn('Arguments @("stash"', updater)

    def test_updater_recognizes_both_bootstrap_and_runtime_supervisors(self):
        updater = UPDATER.read_text(encoding="utf-8")
        self.assertIn("(?:launch|start)_workbench", updater)
        self.assertIn("Test-LauncherMutexHeld", updater)
        self.assertIn("Local\\LlmWorkbenchLauncher", updater)

    def test_production_validation_exercises_the_packaged_entrypoint(self):
        updater = UPDATER.read_text(encoding="utf-8")
        self.assertIn('"tests/test_windows_launcher.py"', updater)
        self.assertIn('"tests/test_workbench_updater.py"', updater)
        self.assertIn('$stagedLauncher = Join-Path $stagingRoot "LocalAIWorkbench.exe"', updater)
        self.assertIn('"--smoke-test"', updater)
        self.assertIn('"--wait"', updater)
        self.assertIn("[System.IO.DirectoryInfo]::new($venvJunction).Delete()", updater)

    def test_interactive_relaunch_waits_for_launcher_lifecycle_handoff(self):
        script = START_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("$launcherShutdownHandoffMilliseconds = 75000", script)
        self.assertIn("function Wait-ForInteractiveLauncherHandoff", script)
        self.assertIn('state = "mutex_acquired"', script)
        self.assertIn('state = "window_ready"', script)
        self.assertIn('state = "timed_out"', script)
        self.assertIn(
            "Treat detection as success so",
            script,
        )

        mutex_branch = script.split(
            'Write-LauncherLog "Another launcher owns the service lifecycle;',
            1,
        )[1].split("if (-not (Test-Path -LiteralPath $pythonPath))", 1)[0]
        wait_position = mutex_branch.index("Wait-ForInteractiveLauncherHandoff")
        open_position = mutex_branch.index("Open-ExistingWorkbenchWindow")
        self.assertLess(wait_position, open_position)
        self.assertIn(
            'if ($handoff.state -eq "mutex_acquired")',
            mutex_branch,
        )
        self.assertNotIn("Find-HealthyWorkbenchBackendPort", mutex_branch[:wait_position])

    @unittest.skipUnless(
        os.name == "nt" and POWERSHELL.exists(),
        "Windows launcher mutex behavior",
    )
    def test_smoke_test_fails_when_launcher_mutex_is_already_held(self):
        holder_script = (
            "$m=[Threading.Mutex]::new($false,'Local\\LlmWorkbenchLauncher');"
            "$owned=$false;"
            "try{$owned=$m.WaitOne(0,$false);"
            "[Console]::Out.WriteLine('ready');[Console]::Out.Flush();"
            "Start-Sleep -Seconds 30}"
            "finally{if($owned){$m.ReleaseMutex()};$m.Dispose()}"
        )
        holder = subprocess.Popen(
            [
                str(POWERSHELL),
                "-NoLogo",
                "-NoProfile",
                "-Command",
                holder_script,
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "ready")
            completed = subprocess.run(
                [
                    str(POWERSHELL),
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(START_SCRIPT),
                    "-SmokeTest",
                    "-BackendPort",
                    "19380",
                    "-FrontendPort",
                    "19381",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
        finally:
            holder.terminate()
            try:
                holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
