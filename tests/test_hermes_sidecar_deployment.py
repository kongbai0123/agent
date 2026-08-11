from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "config" / "hermes-sidecar-manifest.json"
NATIVE_CONFIG_PATH = ROOT / "config" / "hermes-sidecar-native-config.yaml"
DOCKER_CONFIG_PATH = ROOT / "config" / "hermes-sidecar-docker-config.yaml"
INSTALL_SCRIPT = ROOT / "scripts" / "install_hermes_sidecar.ps1"
START_SCRIPT = ROOT / "scripts" / "start_hermes_sidecar.ps1"
RUNTIME_ROOT = ROOT / "runtime" / "hermes"


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _powershell() -> str:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    candidate = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not candidate.exists():
        pytest.skip("Windows PowerShell is unavailable")
    return str(candidate)


def test_release_and_artifacts_are_immutably_pinned() -> None:
    manifest = _manifest()

    assert manifest["release"] == {
        "package_version": "0.18.2",
        "tag": "v2026.7.7.2",
        "tag_object": "b7751df34688835a108e0d630f3495fc11f3df79",
        "source_commit": "9de9c25f620ff7f1ce0fd5457d596052d5159596",
    }
    assert manifest["official_installer"]["sha256"] == (
        "b4998d3b5fc9426f9fe2da1479424db0e840a5e67838a9f2bd14f7d52391cc81"
    )
    assert manifest["docker_image"]["index_digest"] == (
        "sha256:9c841866021c54c4596849f6135717e8a4d52ba510b7f52c50aef1de1a283973"
    )
    assert manifest["docker_image"]["platform_digest"] == (
        "sha256:3db34ce19adfa080736a2a3feb0316dbcccc588faa9afe7fd8ae1c03b4f1a53a"
    )
    assert manifest["docker_image"]["pinned_reference"].endswith(
        "@sha256:3db34ce19adfa080736a2a3feb0316dbcccc588faa9afe7fd8ae1c03b4f1a53a"
    )
    assert all(source.startswith("https://github.com/NousResearch/hermes-agent/") for source in manifest["sources"])


@pytest.mark.parametrize(
    ("mode", "config_path", "base_url"),
    [
        ("native", NATIVE_CONFIG_PATH, "http://127.0.0.1:11434/v1"),
        ("docker", DOCKER_CONFIG_PATH, "http://host.docker.internal:11434/v1"),
    ],
)
def test_fail_closed_config_matches_manifest_and_local_model_schema(
    mode: str, config_path: Path, base_url: str
) -> None:
    manifest = _manifest()
    config = config_path.read_text(encoding="utf-8")

    assert _sha256(config_path) == manifest["runtime"]["config_templates"][mode]["sha256"]
    assert 'default: "gemma4-hermes:latest"' in config
    assert 'provider: "custom"' in config
    assert f'base_url: "{base_url}"' in config
    assert manifest["model"]["base_urls"][mode] == base_url
    assert "context_length: 64000" in config
    assert manifest["model"]["context_length"] == 64000
    assert "max_tokens: 4096" in config
    assert manifest["model"]["max_output_tokens"] == 4096
    assert "toolsets: []" in config
    assert "api_server: []" in config
    assert "mcp_servers: {}" in config
    assert "enabled: []" in config

    disabled = manifest["initial_policy"]["disabled_toolsets"]
    assert len(disabled) == len(set(disabled)) == 25
    for toolset in disabled:
        assert f"    - {toolset}\n" in config


def test_native_install_allowlist_cannot_modify_path_or_install_tools() -> None:
    manifest = _manifest()
    stages = manifest["official_installer"]["native_stages"]
    install_text = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert stages == ["uv", "python", "repository", "venv", "dependencies"]
    assert not {"path", "node", "node-deps", "system-packages", "platform-sdks"}.intersection(stages)
    assert "UV_NO_MODIFY_PATH = \"1\"" in install_text
    assert "UV_CACHE_DIR =" in install_text
    assert "UV_PYTHON_INSTALL_DIR =" in install_text
    assert "PIP_CACHE_DIR =" in install_text
    assert "GIT_CONFIG_GLOBAL = $gitConfigPath" in install_text
    assert "status --porcelain --untracked-files=no" in install_text
    assert "source_worktree_clean = $true" in install_text
    assert "Write-Utf8NoBomText" in install_text
    assert "Set-Content -LiteralPath $receiptPath -Encoding UTF8" not in install_text
    assert "SetEnvironmentVariable(\"Path\", \"User\"" not in install_text
    assert "setx " not in install_text.lower()
    assert "-Branch ([string]$Manifest.release.tag)" in install_text
    assert "-Commit ([string]$Manifest.release.source_commit)" in install_text


def test_docker_pull_is_explicit_digest_pinned_and_system_drive_guarded() -> None:
    install_text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    start_text = START_SCRIPT.read_text(encoding="utf-8")

    assert "[switch]$PullDockerImage" in install_text
    assert "if (-not $PullDockerImage)" in install_text
    assert install_text.index("docker_data.vhdx") < install_text.index("docker.exe pull")
    assert "docker.exe pull --platform linux/amd64 $pinnedReference" in install_text
    assert '"--pull", "never"' in start_text
    assert '"--publish", "127.0.0.1:8642:8642"' in start_text
    assert '"--mount", "type=bind,source=${HomePath},target=/opt/data"' in start_text
    assert '"--mount", "type=bind,source=${ReviewedConfigPath},target=/opt/data/config.yaml,readonly"' in start_text
    assert '"--mount", "type=bind,source=${ReviewedProjectRoot},target=/workspace/project,readonly"' in start_text
    assert '"--mount", "type=bind,source=${ReviewedPolicyDirectory},target=/opt/workbench-policy,readonly"' in start_text
    assert '"--read-only"' in start_text
    assert '"--security-opt", "no-new-privileges:true"' in start_text
    assert '"--cap-drop", "ALL"' in start_text
    assert '"--pids-limit", "256"' in start_text
    assert '"--label", "com.local-ai-workbench.policy=project-readonly-v1"' in start_text
    assert "API_SERVER_HOST=0.0.0.0" in start_text
    assert "API_SERVER_CORS_ORIGINS" not in start_text


def test_launcher_exposes_fixed_key_alias_and_returns_trackable_process() -> None:
    start_text = START_SCRIPT.read_text(encoding="utf-8")

    assert "$env:HERMES_API_SERVER_KEY = $apiKey" in start_text
    assert "Start-Process" in start_text
    assert "-PassThru" in start_text
    assert "HermesEndpoint" in start_text
    assert "HermesCapabilities" in start_text
    assert "HermesToolsets" in start_text
    assert "HERMES_SAFE_MODE = \"1\"" in start_text
    assert "Assert-HermesHealth -HealthResponse $health" in start_text
    assert "Assert-HermesCapabilities -CapabilitiesResponse $capabilities" in start_text
    assert "Assert-HermesToolPolicy -ToolsetsResponse $toolsets -Profile $ToolPolicyProfile" in start_text
    assert "$consecutiveReady += 1" in start_text
    assert "$consecutiveReady = 0" in start_text
    assert "required_consecutive_successes" in start_text
    assert "Stop-StartedDockerSidecar -Manifest $manifest -Profile $ToolPolicyProfile" in start_text
    assert "$labels.'com.local-ai-workbench.policy' -ne $expectedPolicy" in start_text
    assert 'inspect --format "{{json .Config.Labels}}" $containerId' in start_text
    assert "returned an unknown shape; stopping fail closed" in start_text
    assert "exposed enabled toolsets despite the no-tool policy" in start_text
    assert "Hermes read-only toolset contains an unexpected tool" in start_text


def test_toolset_response_guard_fails_closed_for_enabled_or_unknown_shapes() -> None:
    powershell = _powershell()
    command = (
        "$tokens=$null;$errors=$null;"
        f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{START_SCRIPT}',[ref]$tokens,[ref]$errors);"
        "$fn=$ast.Find({param($n)$n -is [System.Management.Automation.Language.FunctionDefinitionAst] "
        "-and $n.Name -eq 'Assert-HermesToolPolicy'},$true);"
        "Invoke-Expression $fn.Extent.Text;"
        "$ok='{""object"":""list"",""platform"":""api_server"",""data"":[{""name"":""web"",""enabled"":false,""tools"":[""web_search""]}]}'|ConvertFrom-Json;"
        "Assert-HermesToolPolicy $ok NoTools;"
        "$readonly='{""object"":""list"",""platform"":""api_server"",""data"":[{""name"":""workbench-readonly"",""enabled"":true,""tools"":[""project_search_files"",""project_read_file""]}]}'|ConvertFrom-Json;"
        "Assert-HermesToolPolicy $readonly ProjectReadOnly;"
        "$bad='{""object"":""list"",""platform"":""api_server"",""data"":[{""name"":""web"",""enabled"":true,""tools"":[""web_search""]}]}'|ConvertFrom-Json;"
        "$badRejected=$false;try{Assert-HermesToolPolicy $bad NoTools}catch{$badRejected=$true};"
        "$profileRejected=$false;try{Assert-HermesToolPolicy $bad ProjectReadOnly}catch{$profileRejected=$true};"
        "$unknownRejected=$false;try{Assert-HermesToolPolicy ([pscustomobject]@{object='list'}) NoTools}catch{$unknownRejected=$true};"
        "if(-not $badRejected -or -not $profileRejected -or -not $unknownRejected){exit 1}"
    )
    checked = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_readiness_guards_reject_identity_or_runs_contract_drift() -> None:
    powershell = _powershell()
    command = (
        "$tokens=$null;$errors=$null;"
        f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{START_SCRIPT}',[ref]$tokens,[ref]$errors);"
        "$names=@('Assert-HermesHealth','Assert-HermesCapabilities');"
        "$ast.FindAll({param($n)$n -is [System.Management.Automation.Language.FunctionDefinitionAst] "
        "-and $names -contains $n.Name},$true)|ForEach-Object{Invoke-Expression $_.Extent.Text};"
        "$policy='{\"health\":{\"status\":\"ok\",\"platform\":\"hermes-agent\",\"version\":\"0.18.2\"},"
        "\"required_features\":[\"run_approval_response\",\"run_events_sse\",\"run_status\",\"run_stop\",\"run_submission\"],"
        "\"required_endpoints\":{\"runs\":\"/v1/runs\",\"run_status\":\"/v1/runs/{run_id}\","
        "\"run_events\":\"/v1/runs/{run_id}/events\",\"run_approval\":\"/v1/runs/{run_id}/approval\","
        "\"run_stop\":\"/v1/runs/{run_id}/stop\"}}'|ConvertFrom-Json;"
        "$health='{\"status\":\"ok\",\"platform\":\"hermes-agent\",\"version\":\"0.18.2\"}'|ConvertFrom-Json;"
        "$caps='{\"object\":\"hermes.api_server.capabilities\",\"platform\":\"hermes-agent\","
        "\"auth\":{\"type\":\"bearer\",\"required\":true},"
        "\"runtime\":{\"mode\":\"server_agent\",\"tool_execution\":\"server\",\"split_runtime\":false},"
        "\"features\":{\"run_approval_response\":true,\"run_events_sse\":true,\"run_status\":true,\"run_stop\":true,\"run_submission\":true},"
        "\"endpoints\":{\"runs\":{\"method\":\"POST\",\"path\":\"/v1/runs\"},"
        "\"run_status\":{\"method\":\"GET\",\"path\":\"/v1/runs/{run_id}\"},"
        "\"run_events\":{\"method\":\"GET\",\"path\":\"/v1/runs/{run_id}/events\"},"
        "\"run_approval\":{\"method\":\"POST\",\"path\":\"/v1/runs/{run_id}/approval\"},"
        "\"run_stop\":{\"method\":\"POST\",\"path\":\"/v1/runs/{run_id}/stop\"}}}'|ConvertFrom-Json;"
        "Assert-HermesHealth $health $policy;Assert-HermesCapabilities $caps $policy;"
        "$health.version='0.18.3';$badHealth=$false;try{Assert-HermesHealth $health $policy}catch{$badHealth=$true};"
        "$caps.features.run_stop=$false;$badCaps=$false;try{Assert-HermesCapabilities $caps $policy}catch{$badCaps=$true};"
        "if(-not $badHealth -or -not $badCaps){exit 1}"
    )
    checked = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.parametrize("script", [INSTALL_SCRIPT, START_SCRIPT])
def test_powershell_scripts_parse_and_validate_without_side_effects(script: Path) -> None:
    powershell = _powershell()
    parser_command = (
        "$tokens=$null;$errors=$null;"
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{script}',[ref]$tokens,[ref]$errors);"
        "if($errors.Count){$errors|ForEach-Object{$_.Message};exit 1}"
    )
    parsed = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", parser_command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert parsed.returncode == 0, parsed.stdout + parsed.stderr

    validated = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-ValidateOnly"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert "True" in validated.stdout
    assert "tools_enabled" in validated.stdout


def test_installed_runtime_receipt_if_present() -> None:
    receipt_path = RUNTIME_ROOT / "install-receipt.json"
    if not receipt_path.exists():
        pytest.skip("Hermes runtime has not been installed yet")

    receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    manifest = _manifest()
    assert receipt["package_version"] == "0.18.2"
    assert receipt["source_tag"] == "v2026.7.7.2"
    assert receipt.get("source_commit", manifest["release"]["source_commit"]) == manifest["release"]["source_commit"]
    mode = receipt["deployment_mode"]
    expected_config_hash = manifest["runtime"]["config_templates"][mode]["sha256"]
    assert receipt["config_sha256"] == expected_config_hash
    assert receipt["tools_enabled"] is False
    assert receipt["mcp_enabled"] is False
    assert receipt["plugins_enabled"] is False
    assert (RUNTIME_ROOT / "home" / "config.yaml").resolve() == (ROOT / "runtime" / "hermes" / "home" / "config.yaml").resolve()
    assert _sha256(RUNTIME_ROOT / "home" / "config.yaml") == expected_config_hash
