from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT / "scripts" / "resolve_hermes_launch.py"
LAUNCHER = ROOT / "scripts" / "start_workbench.ps1"
MANIFEST = ROOT / "config" / "hermes-sidecar-manifest.json"
MONITORING_POLICY = {
    "probe_interval_seconds": 10,
    "failure_threshold": 3,
    "max_restarts_per_launch": 2,
    "restart_backoff_seconds": 2,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _installed_runtime(tmp_path: Path, mode: str) -> tuple[Path, Path]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest_path = tmp_path / "config" / "hermes-sidecar-manifest.json"
    _write_json(manifest_path, manifest)

    runtime = tmp_path / "runtime" / "hermes"
    home = runtime / "home"
    secrets = runtime / "secrets"
    home.mkdir(parents=True)
    secrets.mkdir(parents=True)
    template = ROOT / manifest["runtime"]["config_templates"][mode]["path"]
    shutil.copyfile(template, home / "config.yaml")
    key_path = secrets / "api_server.key"
    key_path.write_text("k" * 43, encoding="ascii")

    receipt = {
        "schema_version": 1,
        "deployment_mode": mode,
        "package_version": manifest["release"]["package_version"],
        "source_tag": manifest["release"]["tag"],
        "hermes_home": str(home),
        "api_key_path": str(key_path),
        "config_sha256": _sha256(home / "config.yaml"),
        "tools_enabled": False,
        "mcp_enabled": False,
        "plugins_enabled": False,
    }
    if mode == "native":
        receipt.update(
            {
                "source_commit": manifest["release"]["source_commit"],
                "installer_sha256": manifest["official_installer"]["sha256"],
                "git_global_config_isolated": True,
                "source_worktree_clean": True,
            }
        )
    else:
        receipt.update(
            {
                "index_digest": manifest["docker_image"]["index_digest"],
                "platform_digest": manifest["docker_image"]["platform_digest"],
                "pinned_reference": manifest["docker_image"]["pinned_reference"],
                "image_id": manifest["docker_image"]["platform_digest"],
                "platform": "linux/amd64",
            }
        )
    receipt_path = runtime / "install-receipt.json"
    _write_json(receipt_path, receipt)
    return manifest_path, receipt_path


def _database(path: Path, project: dict[str, object] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                root_path TEXT NOT NULL,
                archived INTEGER NOT NULL,
                path_status TEXT NOT NULL,
                permission_mode TEXT NOT NULL
            )
            """
        )
        if project is not None:
            connection.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?, ?)",
                (
                    project["id"],
                    project["root_path"],
                    project.get("archived", 0),
                    project.get("path_status", "ready"),
                    project.get("permission_mode", "read_only"),
                ),
            )


def _run_resolver(
    *,
    settings: Path,
    receipt: Path,
    manifest: Path,
    database: Path,
    projects_root: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(RESOLVER),
            "--settings",
            str(settings),
            "--receipt",
            str(receipt),
            "--manifest",
            str(manifest),
            "--database",
            str(database),
            "--projects-root",
            str(projects_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_disabled_settings_do_not_require_an_install_or_database(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    _write_json(settings, {"hermes_enabled": False})

    checked = _run_resolver(
        settings=settings,
        receipt=tmp_path / "missing-receipt.json",
        manifest=tmp_path / "missing-manifest.json",
        database=tmp_path / "missing.db",
        projects_root=tmp_path / "missing-projects",
    )

    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout) == {"enabled": False}


@pytest.mark.parametrize(
    ("mode", "expected_mode"),
    (("native", "Native"), ("docker", "Docker")),
)
def test_verified_receipt_selects_the_installed_no_tool_mode(
    tmp_path: Path, mode: str, expected_mode: str
) -> None:
    manifest, receipt = _installed_runtime(tmp_path, mode)
    settings = tmp_path / "settings.json"
    _write_json(
        settings,
        {
            "hermes_enabled": True,
            "hermes_transport": "runs",
            "hermes_tools_enabled": False,
        },
    )

    checked = _run_resolver(
        settings=settings,
        receipt=receipt,
        manifest=manifest,
        database=tmp_path / "not-needed.db",
        projects_root=tmp_path / "not-needed-projects",
    )

    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout) == {
        "enabled": True,
        "deployment_mode": expected_mode,
        "tool_policy_profile": "NoTools",
        "monitoring": MONITORING_POLICY,
    }


def test_project_readonly_plan_resolves_one_reviewed_db_project(tmp_path: Path) -> None:
    manifest, receipt = _installed_runtime(tmp_path, "docker")
    projects_root = tmp_path / "projects"
    project_root = projects_root / "alpha"
    project_root.mkdir(parents=True)
    database = tmp_path / "runtime" / "db" / "workbench.db"
    _database(
        database,
        {
            "id": "project_alpha",
            "root_path": str(project_root),
        },
    )
    settings = tmp_path / "settings.json"
    _write_json(
        settings,
        {
            "hermes_enabled": True,
            "hermes_transport": "runs",
            "hermes_rollout_mode": "canary",
            "hermes_canary_session_ids": ["configured-at-runtime"],
            "hermes_tools_enabled": True,
            "hermes_allowed_capabilities": ["hermes.project.read"],
            "hermes_readonly_project_id": "project_alpha",
        },
    )

    checked = _run_resolver(
        settings=settings,
        receipt=receipt,
        manifest=manifest,
        database=database,
        projects_root=projects_root,
    )

    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout) == {
        "enabled": True,
        "deployment_mode": "Docker",
        "tool_policy_profile": "ProjectReadOnly",
        "project_id": "project_alpha",
        "project_root": str(project_root.resolve()),
        "monitoring": MONITORING_POLICY,
    }


@pytest.mark.parametrize(
    ("project_changes", "root_outside"),
    (
        ({"permission_mode": "confirm_write"}, False),
        ({"path_status": "missing"}, False),
        ({"archived": 1}, False),
        ({}, True),
    ),
)
def test_project_readonly_plan_rejects_unreviewed_project_scope(
    tmp_path: Path,
    project_changes: dict[str, object],
    root_outside: bool,
) -> None:
    manifest, receipt = _installed_runtime(tmp_path, "docker")
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    project_root = (tmp_path / "outside") if root_outside else (projects_root / "alpha")
    project_root.mkdir()
    database = tmp_path / "runtime" / "db" / "workbench.db"
    project = {
        "id": "project_alpha",
        "root_path": str(project_root),
        **project_changes,
    }
    _database(database, project)
    settings = tmp_path / "settings.json"
    _write_json(
        settings,
        {
            "hermes_enabled": True,
            "hermes_transport": "runs",
            "hermes_rollout_mode": "canary",
            "hermes_canary_session_ids": ["runtime-canary"],
            "hermes_tools_enabled": True,
            "hermes_allowed_capabilities": ["hermes.project.read"],
            "hermes_readonly_project_id": "project_alpha",
        },
    )

    checked = _run_resolver(
        settings=settings,
        receipt=receipt,
        manifest=manifest,
        database=database,
        projects_root=projects_root,
    )

    assert checked.returncode == 2
    assert "Hermes launch plan rejected" in checked.stderr
    assert checked.stdout == ""


def test_tools_cannot_use_native_receipt_or_implicit_canary(tmp_path: Path) -> None:
    manifest, receipt = _installed_runtime(tmp_path, "native")
    settings = tmp_path / "settings.json"
    _write_json(
        settings,
        {
            "hermes_enabled": True,
            "hermes_transport": "runs",
            "hermes_rollout_mode": "canary",
            "hermes_canary_session_ids": [],
            "hermes_tools_enabled": True,
            "hermes_allowed_capabilities": ["hermes.project.read"],
            "hermes_readonly_project_id": "project_alpha",
        },
    )

    checked = _run_resolver(
        settings=settings,
        receipt=receipt,
        manifest=manifest,
        database=tmp_path / "missing.db",
        projects_root=tmp_path / "projects",
    )

    assert checked.returncode == 2
    assert "verified Docker deployment" in checked.stderr


def test_duplicate_json_fields_are_rejected(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        '{"hermes_enabled":true,"hermes_enabled":false}',
        encoding="utf-8",
    )
    checked = _run_resolver(
        settings=settings,
        receipt=tmp_path / "missing.json",
        manifest=tmp_path / "missing-manifest.json",
        database=tmp_path / "missing.db",
        projects_root=tmp_path / "projects",
    )

    assert checked.returncode == 2
    assert "duplicate field" in checked.stderr


@pytest.mark.parametrize(
    ("section", "field"),
    (("readiness", "required_consecutive_successes"), ("evidence", "schema_version")),
)
def test_production_policy_rejects_boolean_numbers(
    tmp_path: Path, section: str, field: str
) -> None:
    manifest_path, receipt = _installed_runtime(tmp_path, "docker")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["production_policy"][section][field] = True
    _write_json(manifest_path, manifest)
    settings = tmp_path / "settings.json"
    _write_json(
        settings,
        {"hermes_enabled": True, "hermes_transport": "runs", "hermes_tools_enabled": False},
    )

    checked = _run_resolver(
        settings=settings,
        receipt=receipt,
        manifest=manifest_path,
        database=tmp_path / "unused.db",
        projects_root=tmp_path / "unused-projects",
    )

    assert checked.returncode == 2
    assert "Hermes launch plan rejected" in checked.stderr


def test_workbench_launcher_consumes_the_resolved_profile_without_hardcoding_canary() -> None:
    launcher = LAUNCHER.read_text(encoding="utf-8")

    assert "resolve_hermes_launch.py" in launcher
    assert "--settings $settingsPath" in launcher
    assert "--receipt $hermesInstallReceipt" in launcher
    assert "--database $hermesDatabasePath" in launcher
    assert "--projects-root $hermesProjectsRoot" in launcher
    assert "-DeploymentMode Native" not in launcher
    assert '$startParameters["ProjectId"] = $resolvedProjectId' in launcher
    assert '$startParameters["ProjectRoot"] = $resolvedProjectRoot' in launcher
    assert "@startParameters" in launcher
    assert "sess_" not in launcher
    assert "Stop-HermesSidecar -Process $hermesProcess" in launcher
    assert "com.local-ai-workbench.owner" in launcher
    assert "com.local-ai-workbench.policy" in launcher
    assert "function Invoke-HermesMonitorTick" in launcher
    assert "function Test-HermesRuntimeReady" in launcher
    assert '"http://127.0.0.1:8642/v1/capabilities"' in launcher
    assert '"http://127.0.0.1:8642/v1/toolsets"' in launcher
    assert '"project_read_file,project_search_files"' in launcher
    assert "Start-ManagedHermesSidecar" in launcher
    assert "max_restarts_per_launch" in launcher
    assert "failure_threshold" in launcher
    assert "container rm $containerId" in launcher
    assert 'inspect --format "{{json .Config.Labels}}" $containerId' in launcher
    assert launcher.index('inspect --format "{{.Id}}" $containerName') < launcher.index(
        'inspect --format "{{json .Config.Labels}}" $containerId'
    )
    assert "Re-resolve persisted settings" in launcher
    assert "function Write-HermesProductionEvidence" in launcher
    assert '-Operation "launcher-health-threshold"' in launcher
    assert '-Operation "launcher-restart-succeeded"' in launcher
    assert '-Operation "launcher-restart-exhausted"' in launcher
    assert '[Environment]::SetEnvironmentVariable("HERMES_API_SERVER_KEY", $null, "Process")' in launcher
    assert "--restart-count $RestartCount" in launcher


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell launcher")
def test_workbench_launcher_still_parses_in_windows_powershell() -> None:
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if not powershell.exists():
        pytest.skip("Windows PowerShell is unavailable")
    command = (
        "$tokens=$null;$errors=$null;"
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{LAUNCHER}',[ref]$tokens,[ref]$errors);"
        "if($errors.Count){$errors|ForEach-Object{$_.Message};exit 1}"
    )
    checked = subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell launcher")
def test_continuous_probe_rejects_runtime_tool_policy_drift() -> None:
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if not powershell.exists():
        pytest.skip("Windows PowerShell is unavailable")
    command = (
        "$tokens=$null;$errors=$null;"
        f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{LAUNCHER}',[ref]$tokens,[ref]$errors);"
        "$fn=$ast.Find({param($n)$n -is [System.Management.Automation.Language.FunctionDefinitionAst] "
        "-and $n.Name -eq 'Test-HermesRuntimeReady'},$true);Invoke-Expression $fn.Extent.Text;"
        "$script:hermesProcess=Get-Process -Id $PID;"
        "$script:hermesProcess|Add-Member -NotePropertyName HermesToolPolicyProfile -NotePropertyValue NoTools;"
        "$env:HERMES_API_SERVER_KEY='test-only-key';$script:toolDrift=$false;"
        "function Invoke-RestMethod{param($Method,$Uri,$Headers,$TimeoutSec);"
        "if($Uri.EndsWith('/health')){return '{\"status\":\"ok\",\"platform\":\"hermes-agent\",\"version\":\"0.18.2\"}'|ConvertFrom-Json};"
        "if($Uri.EndsWith('/v1/capabilities')){return '{\"object\":\"hermes.api_server.capabilities\",\"platform\":\"hermes-agent\","
        "\"auth\":{\"type\":\"bearer\",\"required\":true},\"runtime\":{\"mode\":\"server_agent\",\"tool_execution\":\"server\",\"split_runtime\":false},"
        "\"features\":{\"run_approval_response\":true,\"run_events_sse\":true,\"run_status\":true,\"run_stop\":true,\"run_submission\":true},"
        "\"endpoints\":{\"runs\":{\"method\":\"POST\",\"path\":\"/v1/runs\"},\"run_status\":{\"method\":\"GET\",\"path\":\"/v1/runs/{run_id}\"},"
        "\"run_events\":{\"method\":\"GET\",\"path\":\"/v1/runs/{run_id}/events\"},\"run_approval\":{\"method\":\"POST\",\"path\":\"/v1/runs/{run_id}/approval\"},"
        "\"run_stop\":{\"method\":\"POST\",\"path\":\"/v1/runs/{run_id}/stop\"}}}'|ConvertFrom-Json};"
        "if($script:toolDrift){return '{\"object\":\"list\",\"platform\":\"api_server\",\"data\":[{\"name\":\"web\",\"enabled\":true,\"tools\":[\"web_search\"]}]}'|ConvertFrom-Json};"
        "return '{\"object\":\"list\",\"platform\":\"api_server\",\"data\":[{\"name\":\"web\",\"enabled\":false,\"tools\":[\"web_search\"]}]}'|ConvertFrom-Json};"
        "if(-not (Test-HermesRuntimeReady)){exit 1};$script:toolDrift=$true;if(Test-HermesRuntimeReady){exit 2}"
    )
    checked = subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
