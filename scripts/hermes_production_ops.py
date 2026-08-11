"""Fail-closed production verification and recovery for the Hermes sidecar.

This script is intentionally separate from the Workbench application API.  It
never enables tools, never deletes runtime data, and only stops or restarts the
one Docker container carrying the reviewed Workbench ownership labels.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from backend.hermes_docker_attestation import (  # noqa: E402
    HermesDockerAttestationSpec,
    HermesDockerBindMount,
    attest_live_hermes_docker,
)
from resolve_hermes_launch import (  # noqa: E402
    LaunchPlanError,
    _load_json_object,
    _validate_manifest,
    _validate_receipt,
    resolve_launch_plan,
)


_MAX_HTTP_BYTES = 1_048_576
_MAX_DOCKER_BYTES = 262_144
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_HEALTH = {"status": "ok", "platform": "hermes-agent", "version": "0.18.2"}
_EXPECTED_FEATURES = (
    "run_approval_response",
    "run_events_sse",
    "run_status",
    "run_stop",
    "run_submission",
)
_EXPECTED_ENDPOINTS = {
    "runs": ("POST", "/v1/runs"),
    "run_status": ("GET", "/v1/runs/{run_id}"),
    "run_events": ("GET", "/v1/runs/{run_id}/events"),
    "run_approval": ("POST", "/v1/runs/{run_id}/approval"),
    "run_stop": ("POST", "/v1/runs/{run_id}/stop"),
}
_FORBIDDEN_EVIDENCE_FIELDS = frozenset(
    {
        "api_key",
        "authorization",
        "canary_session_ids",
        "environment",
        "project_id",
        "project_root",
    }
)


class ProductionOpsError(RuntimeError):
    """A safe, stable failure code for operator output and evidence."""

    def __init__(self, code: str, *, checks: Mapping[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.checks = dict(checks or {})


@dataclass(frozen=True)
class RuntimeContext:
    plan: Mapping[str, Any] = field(repr=False)
    manifest: Mapping[str, Any] = field(repr=False)
    receipt: Mapping[str, Any] = field(repr=False)
    api_key: str = field(repr=False, compare=False)


@dataclass(frozen=True)
class Paths:
    settings: Path
    receipt: Path
    manifest: Path
    database: Path
    projects_root: Path
    evidence_dir: Path


HttpGetter = Callable[[str, str, float], Mapping[str, Any]]
Sleeper = Callable[[float], None]


def _strict_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProductionOpsError("response_invalid")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ProductionOpsError("response_invalid")


def _http_json(url: str, api_key: str, timeout_seconds: float) -> Mapping[str, Any]:
    request = Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise ProductionOpsError("endpoint_unavailable")
            payload = response.read(_MAX_HTTP_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ProductionOpsError("endpoint_unavailable") from exc
    if not payload or len(payload) > _MAX_HTTP_BYTES:
        raise ProductionOpsError("response_invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionOpsError("response_invalid") from exc
    if not isinstance(value, dict):
        raise ProductionOpsError("response_invalid")
    return value


def _require_readiness_contract(
    context: RuntimeContext,
    *,
    http_get: HttpGetter = _http_json,
) -> None:
    runtime = context.manifest["runtime"]
    base_url = "http://127.0.0.1:8642"
    health = http_get(base_url + str(runtime["health_path"]), context.api_key, 5.0)
    if any(health.get(key) != value for key, value in _EXPECTED_HEALTH.items()):
        raise ProductionOpsError("health_contract_mismatch")

    capabilities = http_get(
        base_url + str(runtime["capabilities_path"]), context.api_key, 5.0
    )
    auth = capabilities.get("auth")
    runtime_contract = capabilities.get("runtime")
    features = capabilities.get("features")
    endpoints = capabilities.get("endpoints")
    if (
        capabilities.get("object") != "hermes.api_server.capabilities"
        or capabilities.get("platform") != "hermes-agent"
        or not isinstance(auth, dict)
        or auth.get("type") != "bearer"
        or auth.get("required") is not True
        or not isinstance(runtime_contract, dict)
        or runtime_contract.get("mode") != "server_agent"
        or runtime_contract.get("tool_execution") != "server"
        or runtime_contract.get("split_runtime") is not False
        or not isinstance(features, dict)
        or not isinstance(endpoints, dict)
    ):
        raise ProductionOpsError("capabilities_contract_mismatch")
    if any(features.get(name) is not True for name in _EXPECTED_FEATURES):
        raise ProductionOpsError("capabilities_contract_mismatch")
    for name, (method, path) in _EXPECTED_ENDPOINTS.items():
        endpoint = endpoints.get(name)
        if not isinstance(endpoint, dict) or endpoint.get("method") != method or endpoint.get("path") != path:
            raise ProductionOpsError("capabilities_contract_mismatch")

    toolsets = http_get(base_url + str(runtime["toolsets_path"]), context.api_key, 5.0)
    data = toolsets.get("data")
    if toolsets.get("object") != "list" or toolsets.get("platform") != "api_server" or not isinstance(data, list):
        raise ProductionOpsError("tool_policy_mismatch")
    enabled: list[Mapping[str, Any]] = []
    for entry in data:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or type(entry.get("enabled")) is not bool
            or not isinstance(entry.get("tools"), list)
            or not all(isinstance(tool, str) for tool in entry["tools"])
        ):
            raise ProductionOpsError("tool_policy_mismatch")
        if entry["enabled"]:
            enabled.append(entry)
    profile = context.plan["tool_policy_profile"]
    if profile == "NoTools" and enabled:
        raise ProductionOpsError("tool_policy_mismatch")
    if profile == "ProjectReadOnly":
        if len(enabled) != 1 or enabled[0]["name"] != "workbench-readonly":
            raise ProductionOpsError("tool_policy_mismatch")
        if sorted(set(enabled[0]["tools"])) != ["project_read_file", "project_search_files"]:
            raise ProductionOpsError("tool_policy_mismatch")


def _read_api_key(receipt: Mapping[str, Any]) -> str:
    try:
        value = Path(str(receipt["api_key_path"])).read_text(encoding="ascii").strip()
    except (KeyError, OSError, UnicodeError) as exc:
        raise ProductionOpsError("api_key_unavailable") from exc
    if len(value) < 43 or any(char.isspace() for char in value):
        raise ProductionOpsError("api_key_invalid")
    return value


def _context(paths: Paths, *, require_enabled: bool = True) -> RuntimeContext | None:
    try:
        plan = resolve_launch_plan(
            settings_path=paths.settings,
            receipt_path=paths.receipt,
            manifest_path=paths.manifest,
            database_path=paths.database,
            projects_root=paths.projects_root,
        )
    except LaunchPlanError as exc:
        raise ProductionOpsError("launch_plan_rejected") from exc
    if not plan["enabled"]:
        if require_enabled:
            raise ProductionOpsError("hermes_disabled")
        return None
    try:
        manifest = _load_json_object(paths.manifest, label="Hermes deployment manifest")
        receipt = _load_json_object(paths.receipt, label="Hermes installation receipt")
    except LaunchPlanError as exc:
        raise ProductionOpsError("deployment_identity_invalid") from exc
    assert manifest is not None and receipt is not None
    return RuntimeContext(plan, manifest, receipt, _read_api_key(receipt))


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ProductionOpsError("evidence_input_unavailable") from exc


def _docker_executable() -> str:
    executable = shutil.which("docker.exe") or shutil.which("docker")
    if not executable:
        raise ProductionOpsError("docker_unavailable")
    return executable


def _docker_spec(context: RuntimeContext, docker_executable: str) -> HermesDockerAttestationSpec:
    manifest = context.manifest
    receipt = context.receipt
    runtime = manifest["runtime"]
    home = Path(str(receipt["hermes_home"])).resolve(strict=True)
    profile = context.plan["tool_policy_profile"]
    policy = manifest["readonly_tool_policy"]
    expected_tmpfs = dict(policy["tmpfs"])
    if profile == "ProjectReadOnly":
        project_root = Path(str(context.plan["project_root"])).resolve(strict=True)
        config_path = (REPO_ROOT / str(policy["config_template"]["path"])).resolve(strict=True)
        python_policy = (REPO_ROOT / str(policy["python_policy"]["path"])).resolve(strict=True)
        config_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest()
        python_sha256 = hashlib.sha256(python_policy.read_bytes()).hexdigest()
        root_sha256 = hashlib.sha256(str(project_root).lower().encode("utf-8")).hexdigest()
        labels = dict(policy["labels"])
        labels.update(
            {
                "com.local-ai-workbench.project-id": str(context.plan["project_id"]),
                "com.local-ai-workbench.project-root-sha256": root_sha256,
                "com.local-ai-workbench.config-sha256": config_sha256,
                "com.local-ai-workbench.python-policy-sha256": python_sha256,
            }
        )
        mounts = (
            HermesDockerBindMount(home, "/opt/data", False),
            HermesDockerBindMount(config_path, "/opt/data/config.yaml", True),
            HermesDockerBindMount(project_root, str(policy["project_mount"]), True),
            HermesDockerBindMount(
                python_policy.parent,
                str(policy["python_policy"]["container_directory"]),
                True,
            ),
        )
        expected_environment = dict(policy["environment"])
        policy_profile = "project-readonly-v1"
    else:
        config_path = home / "config.yaml"
        config_sha256 = str(receipt["config_sha256"])
        labels = {
            "com.local-ai-workbench.component": "hermes-sidecar",
            "com.local-ai-workbench.policy": "no-tools-v1",
            "com.local-ai-workbench.owner": "workbench",
        }
        mounts = (HermesDockerBindMount(home, "/opt/data", False),)
        expected_environment = {}
        policy_profile = "no-tools-v1"
    return HermesDockerAttestationSpec(
        container_name=str(runtime["container_name"]),
        pinned_reference=str(manifest["docker_image"]["pinned_reference"]),
        image_id=str(receipt["image_id"]),
        expected_mounts=mounts,
        config_path=config_path,
        config_sha256=config_sha256,
        api_server_key=context.api_key,
        expected_labels=labels,
        expected_tmpfs=expected_tmpfs,
        expected_environment=expected_environment,
        policy_profile=policy_profile,
        docker_executable=docker_executable,
    )


def _attest_docker(context: RuntimeContext) -> Mapping[str, Any]:
    if context.plan["deployment_mode"] != "Docker":
        return {"applicable": False, "verified": True}
    attestation = attest_live_hermes_docker(_docker_spec(context, _docker_executable()))
    if not attestation.verified:
        raise ProductionOpsError(
            "docker_attestation_failed",
            checks={"docker_attestation_reason": attestation.reason},
        )
    return {
        "applicable": True,
        "verified": True,
        "evidence_sha256": attestation.evidence_sha256,
        "container_id_sha256": attestation.container_id_sha256,
    }


def _docker_run(executable: str, arguments: Sequence[str], *, timeout: float = 20.0) -> str:
    try:
        completed = subprocess.run(
            [executable, *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProductionOpsError("docker_command_failed") from exc
    if completed.returncode != 0 or len(completed.stdout) > _MAX_DOCKER_BYTES:
        raise ProductionOpsError("docker_command_failed")
    try:
        return completed.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeError as exc:
        raise ProductionOpsError("docker_response_invalid") from exc


def _owned_container_id(
    manifest: Mapping[str, Any],
    *,
    allowed_policies: frozenset[str],
    missing_ok: bool,
) -> tuple[str, str] | None:
    executable = _docker_executable()
    _docker_run(executable, ["info"], timeout=10.0)
    name = str(manifest["runtime"]["container_name"])
    inventory = _docker_run(
        executable,
        ["container", "ls", "--all", "--filter", f"name=^/{name}$", "--format", "{{.ID}}"],
    ).splitlines()
    inventory = [item.strip() for item in inventory if item.strip()]
    if not inventory:
        if missing_ok:
            return None
        raise ProductionOpsError("owned_container_missing")
    if len(inventory) != 1:
        raise ProductionOpsError("container_identity_invalid")
    container_id = _docker_run(executable, ["container", "inspect", "--format", "{{.Id}}", name])
    labels_payload = _docker_run(
        executable,
        ["container", "inspect", "--format", "{{json .Config.Labels}}", container_id],
    )
    try:
        labels = json.loads(labels_payload, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ProductionOpsError) as exc:
        raise ProductionOpsError("container_identity_invalid") from exc
    if (
        not _CONTAINER_ID_RE.fullmatch(container_id)
        or not isinstance(labels, dict)
        or labels.get("com.local-ai-workbench.owner") != "workbench"
        or labels.get("com.local-ai-workbench.component") != "hermes-sidecar"
        or labels.get("com.local-ai-workbench.policy") not in allowed_policies
    ):
        raise ProductionOpsError("container_not_owned")
    return executable, container_id


def _stop_owned_container(
    manifest: Mapping[str, Any], *, remove: bool, missing_ok: bool
) -> Mapping[str, Any]:
    owned = _owned_container_id(
        manifest,
        allowed_policies=frozenset({"no-tools-v1", "project-readonly-v1"}),
        missing_ok=missing_ok,
    )
    if owned is None:
        return {"container_present": False, "container_stopped": False, "container_removed": False}
    executable, container_id = owned
    container_digest = hashlib.sha256(container_id.encode("ascii")).hexdigest()
    _docker_run(executable, ["container", "stop", "--time", "10", container_id], timeout=20.0)
    removed = False
    if remove:
        remaining = _docker_run(
            executable,
            ["container", "ls", "--all", "--filter", f"id={container_id}", "--format", "{{.ID}}"],
        )
        if remaining.strip():
            _docker_run(executable, ["container", "rm", container_id])
        removed = True
    return {
        "container_present": True,
        "container_stopped": True,
        "container_removed": removed,
        "container_id_sha256": container_digest,
        "runtime_data_preserved": True,
    }


def _verify_readiness(
    context: RuntimeContext,
    *,
    http_get: HttpGetter = _http_json,
    sleeper: Sleeper = time.sleep,
) -> Mapping[str, Any]:
    readiness = context.manifest["production_policy"]["readiness"]
    required = int(readiness["required_consecutive_successes"])
    interval = int(readiness["probe_interval_milliseconds"]) / 1000.0
    deadline = time.monotonic() + int(readiness["startup_timeout_seconds"])
    successes = 0
    last_error = "readiness_timeout"
    while time.monotonic() < deadline:
        try:
            _require_readiness_contract(context, http_get=http_get)
            successes += 1
            if successes >= required:
                return {
                    "health_verified": True,
                    "capabilities_verified": True,
                    "tool_policy_verified": True,
                    "consecutive_successes": successes,
                }
        except ProductionOpsError as exc:
            successes = 0
            last_error = exc.code
        sleeper(interval)
    raise ProductionOpsError(last_error)


def verify(paths: Paths) -> Mapping[str, Any]:
    context = _context(paths, require_enabled=False)
    if context is None:
        return {"enabled": False, "verified": True}
    checks = dict(_verify_readiness(context))
    checks["docker"] = _attest_docker(context)
    return {
        "enabled": True,
        "verified": True,
        "deployment_mode": str(context.plan["deployment_mode"]),
        "tool_policy_profile": str(context.plan["tool_policy_profile"]),
        "checks": checks,
    }


def monitor(paths: Paths, *, samples: int, interval_seconds: float) -> Mapping[str, Any]:
    if samples < 1 or samples > 100 or interval_seconds < 0.1 or interval_seconds > 300:
        raise ProductionOpsError("monitor_arguments_invalid")
    context = _context(paths)
    assert context is not None
    docker = _attest_docker(context)
    failures = 0
    successful = 0
    threshold = int(context.plan["monitoring"]["failure_threshold"])
    for index in range(samples):
        try:
            _require_readiness_contract(context)
            failures = 0
            successful += 1
        except ProductionOpsError:
            failures += 1
            if failures >= threshold:
                raise ProductionOpsError(
                    "health_failure_threshold",
                    checks={"samples_completed": index + 1, "consecutive_failures": failures},
                )
        if index + 1 < samples:
            time.sleep(interval_seconds)
    return {
        "enabled": True,
        "verified": True,
        "samples_requested": samples,
        "samples_successful": successful,
        "docker": docker,
    }


def restart_owned(paths: Paths) -> Mapping[str, Any]:
    context = _context(paths)
    assert context is not None
    if context.plan["deployment_mode"] != "Docker":
        raise ProductionOpsError("native_process_not_attributable")
    before = _attest_docker(context)
    policy = "project-readonly-v1" if context.plan["tool_policy_profile"] == "ProjectReadOnly" else "no-tools-v1"
    owned = _owned_container_id(
        context.manifest,
        allowed_policies=frozenset({policy}),
        missing_ok=False,
    )
    assert owned is not None
    executable, container_id = owned
    _docker_run(executable, ["container", "restart", "--time", "10", container_id], timeout=30.0)
    try:
        readiness = _verify_readiness(context)
        after = _attest_docker(context)
    except ProductionOpsError:
        _stop_owned_container(context.manifest, remove=False, missing_ok=True)
        raise
    return {"restarted": True, "before": before, "after": after, "readiness": readiness}


def cleanup_owned(paths: Paths) -> Mapping[str, Any]:
    try:
        manifest = _load_json_object(paths.manifest, label="Hermes deployment manifest")
        assert manifest is not None
        _validate_manifest(manifest)
    except (LaunchPlanError, AssertionError) as exc:
        raise ProductionOpsError("deployment_identity_invalid") from exc
    return _stop_owned_container(manifest, remove=True, missing_ok=True)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def rollback_no_tools(paths: Paths) -> Mapping[str, Any]:
    try:
        manifest = _load_json_object(paths.manifest, label="Hermes deployment manifest")
        settings = _load_json_object(paths.settings, label="Workbench settings")
        assert manifest is not None and settings is not None
        _validate_manifest(manifest)
        loaded_settings_sha256 = _sha256_file(paths.settings)
        receipt = _load_json_object(
            paths.receipt,
            label="Hermes installation receipt",
            missing_ok=True,
        )
        deployment_mode = (
            _validate_receipt(receipt, manifest, paths.receipt)
            if receipt is not None
            else "uninstalled"
        )
    except (LaunchPlanError, AssertionError) as exc:
        raise ProductionOpsError("rollback_input_invalid") from exc

    container = (
        _stop_owned_container(manifest, remove=True, missing_ok=True)
        if deployment_mode == "docker"
        else {
            "container_present": False,
            "container_stopped": False,
            "container_removed": False,
        }
    )
    before_sha256 = _sha256_file(paths.settings)
    if before_sha256 != loaded_settings_sha256:
        raise ProductionOpsError("rollback_settings_changed")
    settings["hermes_tools_enabled"] = False
    settings["hermes_allowed_capabilities"] = []
    settings["hermes_readonly_project_id"] = ""
    _atomic_json(paths.settings, settings)
    after_sha256 = _sha256_file(paths.settings)
    return {
        "safe_tool_policy_profile": "NoTools",
        "settings_narrowed": True,
        "settings_before_sha256": before_sha256,
        "settings_after_sha256": after_sha256,
        "container": container,
        "runtime_data_preserved": True,
    }


def launcher_event(operation: str, *, restart_count: int | None) -> tuple[str, Mapping[str, Any]]:
    reviewed = {
        "launcher-health-threshold": ("restart_requested", "health_probe_failed"),
        "launcher-restart-succeeded": ("passed", "health_probe_failed"),
        "launcher-restart-exhausted": ("failed", "health_probe_failed"),
    }
    if operation not in reviewed or restart_count is None or restart_count < 0 or restart_count > 2:
        raise ProductionOpsError("launcher_evidence_invalid")
    result, failure_code = reviewed[operation]
    return result, {
        "restart_count": restart_count,
        "health_failure_code": failure_code,
        "restart_limit_exhausted": operation == "launcher-restart-exhausted",
    }


def _assert_evidence_safe(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in _FORBIDDEN_EVIDENCE_FIELDS:
                raise ProductionOpsError("evidence_not_safe")
            _assert_evidence_safe(item)
    elif isinstance(value, list):
        for item in value:
            _assert_evidence_safe(item)


def write_evidence(
    paths: Paths,
    *,
    operation: str,
    result: str,
    details: Mapping[str, Any],
    error_code: str = "",
) -> Path:
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "result": result,
        "release": {"package_version": "0.18.2", "source_tag": "v2026.7.7.2"},
        "input_digests": {
            "manifest_sha256": _sha256_file(paths.manifest),
            "settings_sha256": _sha256_file(paths.settings) if paths.settings.is_file() else "missing",
            "receipt_sha256": _sha256_file(paths.receipt) if paths.receipt.is_file() else "missing",
        },
        "details": dict(details),
    }
    if error_code:
        evidence["error_code"] = error_code
    _assert_evidence_safe(evidence)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = paths.evidence_dir / f"{timestamp}-{operation}-{uuid.uuid4().hex[:8]}.json"
    _atomic_json(target, evidence)
    return target


def _default_paths(args: argparse.Namespace) -> Paths:
    runtime_root = Path(os.environ.get("WORKBENCH_RUNTIME_DIR") or REPO_ROOT / "runtime")
    settings = Path(os.environ.get("WORKBENCH_SETTINGS_PATH") or REPO_ROOT / "backend" / "settings.json")
    return Paths(
        settings=Path(args.settings or settings).resolve(),
        receipt=Path(args.receipt or REPO_ROOT / "runtime" / "hermes" / "install-receipt.json").resolve(),
        manifest=Path(args.manifest or REPO_ROOT / "config" / "hermes-sidecar-manifest.json").resolve(),
        database=Path(args.database or runtime_root / "db" / "workbench.db").resolve(),
        projects_root=Path(args.projects_root or REPO_ROOT / "projects").resolve(),
        evidence_dir=Path(args.evidence_dir or REPO_ROOT / "runtime" / "hermes" / "evidence").resolve(),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify or safely recover the Workbench Hermes sidecar.")
    parser.add_argument(
        "operation",
        choices=(
            "verify",
            "monitor",
            "restart-owned",
            "cleanup-owned",
            "rollback-no-tools",
            "launcher-health-threshold",
            "launcher-restart-succeeded",
            "launcher-restart-exhausted",
        ),
    )
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--projects-root", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--samples", type=int, default=6)
    parser.add_argument("--interval-seconds", type=float, default=10.0)
    parser.add_argument("--restart-count", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = _default_paths(args)
    operation = args.operation
    try:
        evidence_result = "passed"
        if operation.startswith("launcher-"):
            evidence_result, details = launcher_event(
                operation, restart_count=args.restart_count
            )
        elif operation == "verify":
            details = verify(paths)
        elif operation == "monitor":
            details = monitor(paths, samples=args.samples, interval_seconds=args.interval_seconds)
        elif operation == "restart-owned":
            details = restart_owned(paths)
        elif operation == "cleanup-owned":
            details = cleanup_owned(paths)
        else:
            details = rollback_no_tools(paths)
        evidence_path = write_evidence(
            paths, operation=operation, result=evidence_result, details=details
        )
        print(json.dumps({"ok": True, "operation": operation, "evidence_file": str(evidence_path)}))
        return 0
    except ProductionOpsError as exc:
        try:
            evidence_path = write_evidence(
                paths,
                operation=operation,
                result="failed",
                details=exc.checks,
                error_code=exc.code,
            )
            print(
                json.dumps(
                    {"ok": False, "operation": operation, "error_code": exc.code, "evidence_file": str(evidence_path)}
                ),
                file=sys.stderr,
            )
        except Exception:
            print(
                json.dumps({"ok": False, "operation": operation, "error_code": exc.code}),
                file=sys.stderr,
            )
        return 2
    except Exception:
        report: dict[str, Any] = {
            "ok": False,
            "operation": operation,
            "error_code": "unexpected_failure",
        }
        try:
            evidence_path = write_evidence(
                paths,
                operation=operation,
                result="failed",
                details={},
                error_code="unexpected_failure",
            )
            report["evidence_file"] = str(evidence_path)
        except Exception:
            pass
        print(json.dumps(report), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
