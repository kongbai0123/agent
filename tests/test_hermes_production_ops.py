from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hermes_production_ops as ops  # noqa: E402


def _context(profile: str = "NoTools") -> ops.RuntimeContext:
    return ops.RuntimeContext(
        plan={
            "enabled": True,
            "deployment_mode": "Docker",
            "tool_policy_profile": profile,
            "monitoring": {
                "probe_interval_seconds": 10,
                "failure_threshold": 3,
                "max_restarts_per_launch": 2,
                "restart_backoff_seconds": 2,
            },
        },
        manifest={
            "runtime": {
                "health_path": "/health",
                "capabilities_path": "/v1/capabilities",
                "toolsets_path": "/v1/toolsets",
            }
        },
        receipt={},
        api_key="secret-key-that-must-never-enter-evidence",
    )


def _responses(*, profile: str = "NoTools") -> dict[str, dict[str, object]]:
    enabled = []
    if profile == "ProjectReadOnly":
        enabled = [
            {
                "name": "workbench-readonly",
                "enabled": True,
                "tools": ["project_search_files", "project_read_file"],
            }
        ]
    return {
        "/health": {"status": "ok", "platform": "hermes-agent", "version": "0.18.2"},
        "/v1/capabilities": {
            "object": "hermes.api_server.capabilities",
            "platform": "hermes-agent",
            "auth": {"type": "bearer", "required": True},
            "runtime": {
                "mode": "server_agent",
                "tool_execution": "server",
                "split_runtime": False,
            },
            "features": {name: True for name in ops._EXPECTED_FEATURES},
            "endpoints": {
                name: {"method": method, "path": path}
                for name, (method, path) in ops._EXPECTED_ENDPOINTS.items()
            },
        },
        "/v1/toolsets": {"object": "list", "platform": "api_server", "data": enabled},
    }


@pytest.mark.parametrize("profile", ["NoTools", "ProjectReadOnly"])
def test_readiness_contract_accepts_only_the_reviewed_surface(profile: str) -> None:
    responses = _responses(profile=profile)

    def get(url: str, api_key: str, timeout: float):
        assert api_key.startswith("secret-key")
        assert timeout == 5.0
        return responses[url.removeprefix("http://127.0.0.1:8642")]

    ops._require_readiness_contract(_context(profile), http_get=get)


def test_readiness_contract_rejects_health_capability_and_tool_drift() -> None:
    context = _context()
    responses = _responses()

    def get(url: str, _api_key: str, _timeout: float):
        return responses[url.removeprefix("http://127.0.0.1:8642")]

    responses["/health"]["version"] = "0.18.3"
    with pytest.raises(ops.ProductionOpsError, match="health_contract_mismatch"):
        ops._require_readiness_contract(context, http_get=get)

    responses = _responses()
    responses["/v1/capabilities"]["features"]["run_stop"] = False
    with pytest.raises(ops.ProductionOpsError, match="capabilities_contract_mismatch"):
        ops._require_readiness_contract(context, http_get=get)

    responses = _responses()
    responses["/v1/toolsets"]["data"] = [
        {"name": "web", "enabled": True, "tools": ["web_search"]}
    ]
    with pytest.raises(ops.ProductionOpsError, match="tool_policy_mismatch"):
        ops._require_readiness_contract(context, http_get=get)


def _paths(tmp_path: Path, settings: dict[str, object]) -> ops.Paths:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    receipt = tmp_path / "install-receipt.json"
    receipt.write_text('{"schema_version":1}', encoding="utf-8")
    return ops.Paths(
        settings=settings_path,
        receipt=receipt,
        manifest=ROOT / "config" / "hermes-sidecar-manifest.json",
        database=tmp_path / "workbench.db",
        projects_root=tmp_path / "projects",
        evidence_dir=tmp_path / "evidence",
    )


def test_rollback_only_narrows_tools_and_preserves_runtime_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = {
        "hermes_enabled": True,
        "hermes_rollout_mode": "canary",
        "hermes_canary_session_ids": ["sensitive-session"],
        "hermes_tools_enabled": True,
        "hermes_allowed_capabilities": ["hermes.project.read"],
        "hermes_readonly_project_id": "sensitive-project",
        "unrelated_setting": "preserved",
    }
    paths = _paths(tmp_path, original)
    monkeypatch.setattr(ops, "_validate_receipt", lambda *_args, **_kwargs: "docker")
    monkeypatch.setattr(
        ops,
        "_stop_owned_container",
        lambda *_args, **_kwargs: {
            "container_present": True,
            "container_stopped": True,
            "container_removed": True,
            "runtime_data_preserved": True,
        },
    )

    details = ops.rollback_no_tools(paths)
    updated = json.loads(paths.settings.read_text(encoding="utf-8"))

    assert updated["hermes_tools_enabled"] is False
    assert updated["hermes_allowed_capabilities"] == []
    assert updated["hermes_readonly_project_id"] == ""
    assert updated["hermes_enabled"] is True
    assert updated["hermes_rollout_mode"] == "canary"
    assert updated["hermes_canary_session_ids"] == ["sensitive-session"]
    assert updated["unrelated_setting"] == "preserved"
    assert details["safe_tool_policy_profile"] == "NoTools"
    assert details["runtime_data_preserved"] is True


def test_native_rollback_does_not_touch_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(
        tmp_path,
        {
            "hermes_enabled": True,
            "hermes_tools_enabled": True,
            "hermes_allowed_capabilities": ["hermes.project.read"],
            "hermes_readonly_project_id": "project-alpha",
        },
    )
    monkeypatch.setattr(ops, "_validate_receipt", lambda *_args, **_kwargs: "native")

    def unexpected_docker(*_args, **_kwargs):
        raise AssertionError("native rollback must not inspect Docker")

    monkeypatch.setattr(ops, "_stop_owned_container", unexpected_docker)
    details = ops.rollback_no_tools(paths)

    assert details["container"] == {
        "container_present": False,
        "container_stopped": False,
        "container_removed": False,
    }


def test_rollback_refuses_to_overwrite_concurrent_settings_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(
        tmp_path,
        {
            "hermes_enabled": True,
            "hermes_tools_enabled": True,
            "unrelated_setting": "before",
        },
    )
    monkeypatch.setattr(ops, "_validate_receipt", lambda *_args, **_kwargs: "docker")

    def change_settings(*_args, **_kwargs):
        current = json.loads(paths.settings.read_text(encoding="utf-8"))
        current["unrelated_setting"] = "concurrent-update"
        paths.settings.write_text(json.dumps(current), encoding="utf-8")
        return {"container_stopped": True, "runtime_data_preserved": True}

    monkeypatch.setattr(ops, "_stop_owned_container", change_settings)
    with pytest.raises(ops.ProductionOpsError, match="rollback_settings_changed"):
        ops.rollback_no_tools(paths)

    current = json.loads(paths.settings.read_text(encoding="utf-8"))
    assert current["unrelated_setting"] == "concurrent-update"


def test_evidence_is_whitelisted_and_contains_no_secret_or_project_identity(tmp_path: Path) -> None:
    paths = _paths(
        tmp_path,
        {
            "hermes_enabled": True,
            "hermes_canary_session_ids": ["session-that-must-not-leak"],
            "hermes_readonly_project_id": "project-that-must-not-leak",
            "api_key": "key-that-must-not-leak",
        },
    )
    evidence_path = ops.write_evidence(
        paths,
        operation="verify",
        result="passed",
        details={
            "verified": True,
            "container_id_sha256": "a" * 64,
            "settings_before_sha256": "b" * 64,
        },
    )
    text = evidence_path.read_text(encoding="utf-8")
    value = json.loads(text)

    assert value["result"] == "passed"
    assert "session-that-must-not-leak" not in text
    assert "project-that-must-not-leak" not in text
    assert "key-that-must-not-leak" not in text
    assert not ops._FORBIDDEN_EVIDENCE_FIELDS.intersection(
        key.casefold() for key in _all_keys(value)
    )

    with pytest.raises(ops.ProductionOpsError, match="evidence_not_safe"):
        ops.write_evidence(
            paths,
            operation="verify",
            result="passed",
            details={"project_id": "forbidden"},
        )


@pytest.mark.parametrize(
    ("operation", "expected_result", "exhausted"),
    (
        ("launcher-health-threshold", "restart_requested", False),
        ("launcher-restart-succeeded", "passed", False),
        ("launcher-restart-exhausted", "failed", True),
    ),
)
def test_launcher_evidence_has_only_bounded_redacted_health_state(
    operation: str, expected_result: str, exhausted: bool
) -> None:
    result, details = ops.launcher_event(operation, restart_count=2)

    assert result == expected_result
    assert details == {
        "restart_count": 2,
        "health_failure_code": "health_probe_failed",
        "restart_limit_exhausted": exhausted,
    }
    ops._assert_evidence_safe(details)


@pytest.mark.parametrize("count", [-1, 3, None])
def test_launcher_evidence_rejects_unbounded_restart_counts(count: int | None) -> None:
    with pytest.raises(ops.ProductionOpsError, match="launcher_evidence_invalid"):
        ops.launcher_event("launcher-health-threshold", restart_count=count)


def test_launcher_event_cli_writes_redacted_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(
        tmp_path,
        {
            "hermes_enabled": True,
            "hermes_canary_session_ids": ["do-not-record-session"],
            "hermes_readonly_project_id": "do-not-record-project",
        },
    )
    monkeypatch.setenv("HERMES_API_SERVER_KEY", "do-not-record-api-key")
    checked = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "hermes_production_ops.py"),
            "launcher-restart-exhausted",
            "--restart-count",
            "2",
            "--settings",
            str(paths.settings),
            "--receipt",
            str(paths.receipt),
            "--manifest",
            str(paths.manifest),
            "--evidence-dir",
            str(paths.evidence_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert checked.returncode == 0, checked.stdout + checked.stderr
    report = json.loads(checked.stdout)
    evidence = Path(report["evidence_file"]).read_text(encoding="utf-8")
    record = json.loads(evidence)
    assert record["operation"] == "launcher-restart-exhausted"
    assert record["result"] == "failed"
    assert record["details"]["restart_count"] == 2
    assert record["details"]["health_failure_code"] == "health_probe_failed"
    assert "do-not-record-api-key" not in evidence
    assert "do-not-record-project" not in evidence
    assert "do-not-record-session" not in evidence


def test_unexpected_failure_still_writes_generic_redacted_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path, {"hermes_enabled": True})
    monkeypatch.setattr(ops, "_default_paths", lambda _args: paths)

    def fail(_paths: ops.Paths):
        raise RuntimeError("secret exception text must never be recorded")

    monkeypatch.setattr(ops, "verify", fail)
    assert ops.main(["verify"]) == 2
    report = json.loads(capsys.readouterr().err)
    evidence = Path(report["evidence_file"]).read_text(encoding="utf-8")

    assert report["error_code"] == "unexpected_failure"
    assert "secret exception text" not in evidence
    assert json.loads(evidence)["error_code"] == "unexpected_failure"


def _all_keys(value: object) -> list[str]:
    if isinstance(value, dict):
        return [str(key) for key in value] + [
            nested for item in value.values() for nested in _all_keys(item)
        ]
    if isinstance(value, list):
        return [nested for item in value for nested in _all_keys(item)]
    return []


def test_owned_container_guard_rejects_label_drift_before_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = json.loads(
        (ROOT / "config" / "hermes-sidecar-manifest.json").read_text(encoding="utf-8")
    )
    container_id = "a" * 64
    monkeypatch.setattr(ops, "_docker_executable", lambda: "docker.exe")

    def run(_executable: str, arguments: list[str], **_kwargs) -> str:
        joined = " ".join(arguments)
        if arguments == ["info"]:
            return ""
        if "container ls" in joined:
            return container_id[:12]
        if "{{.Id}}" in arguments:
            return container_id
        if "{{json .Config.Labels}}" in arguments:
            return json.dumps(
                {
                    "com.local-ai-workbench.owner": "someone-else",
                    "com.local-ai-workbench.component": "hermes-sidecar",
                    "com.local-ai-workbench.policy": "no-tools-v1",
                }
            )
        raise AssertionError(arguments)

    monkeypatch.setattr(ops, "_docker_run", run)
    with pytest.raises(ops.ProductionOpsError, match="container_not_owned"):
        ops._owned_container_id(
            manifest,
            allowed_policies=frozenset({"no-tools-v1"}),
            missing_ok=False,
        )


def test_operations_never_delete_runtime_data_or_enable_tools() -> None:
    source = (SCRIPTS / "hermes_production_ops.py").read_text(encoding="utf-8")

    assert "rmtree(" not in source
    assert "docker volume" not in source
    assert 'settings["hermes_tools_enabled"] = False' in source
    assert 'settings["hermes_allowed_capabilities"] = []' in source
    assert 'settings["hermes_readonly_project_id"] = ""' in source
    assert 'settings["hermes_tools_enabled"] = True' not in source
