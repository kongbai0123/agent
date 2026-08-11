"""Resolve one fail-closed Hermes sidecar launch plan for Workbench.

The Workbench launcher runs before the backend is available.  This helper uses
only the Python standard library so it can validate the persisted deployment
receipt and, for the reviewed read-only profile, resolve the configured project
root from SQLite without importing or starting the application.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable
from urllib.parse import quote


_MAX_JSON_BYTES = 1_048_576
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PINNED_RELEASE = {
    "package_version": "0.18.2",
    "tag": "v2026.7.7.2",
    "source_commit": "9de9c25f620ff7f1ce0fd5457d596052d5159596",
    "installer_sha256": "b4998d3b5fc9426f9fe2da1479424db0e840a5e67838a9f2bd14f7d52391cc81",
    "index_digest": "sha256:9c841866021c54c4596849f6135717e8a4d52ba510b7f52c50aef1de1a283973",
    "platform_digest": "sha256:3db34ce19adfa080736a2a3feb0316dbcccc588faa9afe7fd8ae1c03b4f1a53a",
    "pinned_reference": (
        "docker.io/nousresearch/hermes-agent@"
        "sha256:3db34ce19adfa080736a2a3feb0316dbcccc588faa9afe7fd8ae1c03b4f1a53a"
    ),
}


class LaunchPlanError(ValueError):
    """A persisted launch input failed closed validation."""


def _strict_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise LaunchPlanError(f"JSON contains duplicate field {key!r}.")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise LaunchPlanError(f"JSON contains unsupported constant {value!r}.")


def _load_json_object(path: Path, *, label: str, missing_ok: bool = False) -> dict[str, Any] | None:
    resolved = Path(path).resolve()
    if missing_ok and not resolved.exists():
        return None
    try:
        size = resolved.stat().st_size
        if size <= 0 or size > _MAX_JSON_BYTES:
            raise LaunchPlanError(f"{label} has an invalid size.")
        value = json.loads(
            resolved.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except LaunchPlanError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LaunchPlanError(f"{label} could not be loaded.") from exc
    if not isinstance(value, dict):
        raise LaunchPlanError(f"{label} must be a JSON object.")
    return value


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LaunchPlanError(f"{label} is incomplete.")
    return value


def _exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise LaunchPlanError(f"{label} must be true or false.")
    return value


def _production_monitoring_policy(manifest: dict[str, Any]) -> dict[str, int]:
    production = _mapping(manifest.get("production_policy"), "Hermes production policy")
    readiness = _mapping(production.get("readiness"), "Hermes readiness policy")
    expected_health = {
        "status": "ok",
        "platform": "hermes-agent",
        "version": _PINNED_RELEASE["package_version"],
    }
    expected_features = [
        "run_approval_response",
        "run_events_sse",
        "run_status",
        "run_stop",
        "run_submission",
    ]
    expected_endpoints = {
        "runs": "/v1/runs",
        "run_status": "/v1/runs/{run_id}",
        "run_events": "/v1/runs/{run_id}/events",
        "run_approval": "/v1/runs/{run_id}/approval",
        "run_stop": "/v1/runs/{run_id}/stop",
    }
    expected_readiness_numbers = {
        "startup_timeout_seconds": 60,
        "probe_interval_milliseconds": 500,
        "required_consecutive_successes": 2,
    }
    if (
        any(
            type(readiness.get(name)) is not int or readiness.get(name) != expected
            for name, expected in expected_readiness_numbers.items()
        )
        or readiness.get("health") != expected_health
        or readiness.get("required_features") != expected_features
        or readiness.get("required_endpoints") != expected_endpoints
    ):
        raise LaunchPlanError("Hermes readiness policy is not pinned.")

    monitoring = _mapping(production.get("monitoring"), "Hermes monitoring policy")
    expected_monitoring = {
        "probe_interval_seconds": 10,
        "failure_threshold": 3,
        "max_restarts_per_launch": 2,
        "restart_backoff_seconds": 2,
    }
    if monitoring != expected_monitoring or any(
        type(value) is not int for value in monitoring.values()
    ):
        raise LaunchPlanError("Hermes monitoring policy is not pinned.")

    rollback = _mapping(production.get("rollback"), "Hermes rollback policy")
    if (
        rollback.get("safe_tool_policy_profile") != "NoTools"
        or rollback.get("stop_owned_container_only") is not True
        or rollback.get("preserve_runtime_data") is not True
    ):
        raise LaunchPlanError("Hermes rollback policy is not fail closed.")
    evidence = _mapping(production.get("evidence"), "Hermes evidence policy")
    if (
        type(evidence.get("schema_version")) is not int
        or evidence.get("schema_version") != 1
        or evidence.get("relative_directory") != "runtime/hermes/evidence"
        or evidence.get("forbidden_fields")
        != [
            "api_key",
            "authorization",
            "canary_session_ids",
            "environment",
            "project_id",
            "project_root",
        ]
    ):
        raise LaunchPlanError("Hermes evidence policy is not pinned.")
    return dict(expected_monitoring)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise LaunchPlanError("The installed Hermes runtime config could not be read.") from exc
    return digest.hexdigest()


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise LaunchPlanError("Hermes deployment manifest schema_version must be 1.")
    if manifest.get("component") != "NousResearch/hermes-agent":
        raise LaunchPlanError("Hermes deployment manifest component is not supported.")

    release = _mapping(manifest.get("release"), "Hermes release identity")
    installer = _mapping(manifest.get("official_installer"), "Hermes installer identity")
    image = _mapping(manifest.get("docker_image"), "Hermes Docker identity")
    if (
        release.get("package_version") != _PINNED_RELEASE["package_version"]
        or release.get("tag") != _PINNED_RELEASE["tag"]
        or release.get("source_commit") != _PINNED_RELEASE["source_commit"]
        or str(installer.get("sha256") or "").casefold()
        != _PINNED_RELEASE["installer_sha256"]
        or image.get("index_digest") != _PINNED_RELEASE["index_digest"]
        or image.get("platform_digest") != _PINNED_RELEASE["platform_digest"]
        or image.get("pinned_reference") != _PINNED_RELEASE["pinned_reference"]
        or image.get("platform") != "linux/amd64"
    ):
        raise LaunchPlanError("Hermes deployment manifest identity is not pinned.")

    runtime = _mapping(manifest.get("runtime"), "Hermes runtime policy")
    if (
        runtime.get("host") != "127.0.0.1"
        or runtime.get("host_port") != 8642
        or runtime.get("container_name") != "local-ai-workbench-hermes"
    ):
        raise LaunchPlanError("Hermes runtime boundary is not pinned.")
    templates = _mapping(runtime.get("config_templates"), "Hermes config templates")
    for mode in ("native", "docker"):
        template = _mapping(templates.get(mode), f"Hermes {mode} config template")
        digest = str(template.get("sha256") or "").casefold()
        if not _SHA256_RE.fullmatch(digest):
            raise LaunchPlanError(f"Hermes {mode} config template hash is invalid.")
    _production_monitoring_policy(manifest)


def _validate_receipt(
    receipt: dict[str, Any],
    manifest: dict[str, Any],
    receipt_path: Path,
) -> str:
    if type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1:
        raise LaunchPlanError("Hermes installation receipt schema_version must be 1.")
    mode = receipt.get("deployment_mode")
    if mode not in {"native", "docker"}:
        raise LaunchPlanError("Hermes installation receipt deployment mode is invalid.")

    release = _mapping(manifest.get("release"), "Hermes release identity")
    installer = _mapping(manifest.get("official_installer"), "Hermes installer identity")
    image = _mapping(manifest.get("docker_image"), "Hermes Docker identity")
    if (
        receipt.get("package_version") != release.get("package_version")
        or receipt.get("source_tag") != release.get("tag")
    ):
        raise LaunchPlanError("Hermes installation receipt release is not pinned.")

    if mode == "native":
        if (
            receipt.get("source_commit") != release.get("source_commit")
            or str(receipt.get("installer_sha256") or "").casefold()
            != str(installer.get("sha256") or "").casefold()
            or receipt.get("git_global_config_isolated") is not True
            or receipt.get("source_worktree_clean") is not True
        ):
            raise LaunchPlanError("Hermes native installation receipt is incomplete.")
    else:
        if (
            receipt.get("index_digest") != image.get("index_digest")
            or receipt.get("platform_digest") != image.get("platform_digest")
            or receipt.get("pinned_reference") != image.get("pinned_reference")
            or receipt.get("image_id") != image.get("platform_digest")
            or receipt.get("platform") != "linux/amd64"
        ):
            raise LaunchPlanError("Hermes Docker installation receipt is not pinned.")

    for field in ("tools_enabled", "mcp_enabled", "plugins_enabled"):
        if _exact_bool(receipt.get(field), f"Hermes receipt {field}"):
            raise LaunchPlanError("Hermes installation receipt is not fail closed.")

    templates = _mapping(
        _mapping(manifest.get("runtime"), "Hermes runtime policy").get("config_templates"),
        "Hermes config templates",
    )
    template = _mapping(templates.get(mode), f"Hermes {mode} config template")
    expected_hash = str(template.get("sha256") or "").casefold()
    if str(receipt.get("config_sha256") or "").casefold() != expected_hash:
        raise LaunchPlanError("Hermes installation receipt config hash is not pinned.")

    runtime_root = receipt_path.resolve().parent
    expected_home = (runtime_root / "home").resolve()
    expected_key = (runtime_root / "secrets" / "api_server.key").resolve()
    try:
        receipt_home = Path(str(receipt.get("hermes_home") or "")).resolve(strict=True)
        receipt_key = Path(str(receipt.get("api_key_path") or "")).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LaunchPlanError("Hermes installation receipt paths are unavailable.") from exc
    if receipt_home != expected_home or receipt_key != expected_key:
        raise LaunchPlanError("Hermes installation receipt escaped its runtime directory.")
    config_path = expected_home / "config.yaml"
    if not config_path.is_file() or _file_sha256(config_path) != expected_hash:
        raise LaunchPlanError("Installed Hermes runtime config failed verification.")
    return mode


def _validate_canaries(value: object) -> None:
    if not isinstance(value, list) or not value or len(value) > 500:
        raise LaunchPlanError("Hermes project tools require explicit canary sessions.")
    for item in value:
        if (
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 256
            or any(ord(char) < 32 for char in item)
        ):
            raise LaunchPlanError("Hermes canary session IDs are invalid.")


def _read_project(
    database_path: Path,
    projects_root: Path,
    project_id: str,
) -> Path:
    database_path = database_path.resolve()
    if not database_path.is_file():
        raise LaunchPlanError("Workbench project database is unavailable.")
    uri = "file:" + quote(database_path.as_posix(), safe="/:") + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                "SELECT id, root_path, archived, path_status, permission_mode "
                "FROM projects WHERE id = ? LIMIT 2",
                (project_id,),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise LaunchPlanError("Workbench project database could not be read safely.") from exc
    if len(rows) != 1 or str(rows[0]["id"]) != project_id:
        raise LaunchPlanError("The configured Hermes read-only project does not exist.")
    row = rows[0]
    if (
        row["archived"] != 0
        or str(row["path_status"] or "").strip().casefold() != "ready"
        or str(row["permission_mode"] or "").strip().casefold() != "read_only"
    ):
        raise LaunchPlanError("The configured Hermes project is not active and read only.")
    raw_root = row["root_path"]
    if (
        not isinstance(raw_root, str)
        or not raw_root.strip()
        or "," in raw_root
        or any(ord(char) < 32 or ord(char) == 127 for char in raw_root)
    ):
        raise LaunchPlanError("The configured Hermes project root is invalid.")
    try:
        root = Path(raw_root).resolve(strict=True)
        reviewed_root = projects_root.resolve(strict=True)
        relative = root.relative_to(reviewed_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LaunchPlanError(
            "The configured Hermes project is outside the reviewed projects directory."
        ) from exc
    if not root.is_dir() or not relative.parts:
        raise LaunchPlanError("The configured Hermes project root is invalid.")
    return root


def resolve_launch_plan(
    *,
    settings_path: Path,
    receipt_path: Path,
    manifest_path: Path,
    database_path: Path,
    projects_root: Path,
) -> dict[str, object]:
    settings = _load_json_object(
        settings_path,
        label="Workbench settings",
        missing_ok=True,
    )
    if settings is None or "hermes_enabled" not in settings:
        return {"enabled": False}
    enabled = _exact_bool(settings.get("hermes_enabled"), "hermes_enabled")
    if not enabled:
        return {"enabled": False}

    if settings.get("hermes_transport", "runs") != "runs":
        raise LaunchPlanError("Hermes Workbench integration requires Runs transport.")
    tools_enabled = _exact_bool(
        settings.get("hermes_tools_enabled", False),
        "hermes_tools_enabled",
    )
    manifest = _load_json_object(manifest_path, label="Hermes deployment manifest")
    receipt = _load_json_object(receipt_path, label="Hermes installation receipt")
    assert manifest is not None and receipt is not None
    _validate_manifest(manifest)
    mode = _validate_receipt(receipt, manifest, receipt_path)

    plan: dict[str, object] = {
        "enabled": True,
        "deployment_mode": "Docker" if mode == "docker" else "Native",
        "tool_policy_profile": "NoTools",
        "monitoring": _production_monitoring_policy(manifest),
    }
    if not tools_enabled:
        return plan
    if mode != "docker":
        raise LaunchPlanError(
            "Hermes project tools require the verified Docker deployment."
        )
    if settings.get("hermes_rollout_mode") != "canary":
        raise LaunchPlanError("Hermes project tools require canary rollout.")
    _validate_canaries(settings.get("hermes_canary_session_ids"))
    if settings.get("hermes_allowed_capabilities") != ["hermes.project.read"]:
        raise LaunchPlanError(
            "Hermes project tools require the exact read-only capability."
        )
    project_id = settings.get("hermes_readonly_project_id")
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
        raise LaunchPlanError("Hermes read-only project ID is invalid.")
    project_root = _read_project(database_path, projects_root, project_id)
    plan.update(
        {
            "tool_policy_profile": "ProjectReadOnly",
            "project_id": project_id,
            "project_root": str(project_root),
        }
    )
    return plan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve a Workbench Hermes launch plan.")
    parser.add_argument("--settings", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--projects-root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = resolve_launch_plan(
            settings_path=args.settings,
            receipt_path=args.receipt,
            manifest_path=args.manifest,
            database_path=args.database,
            projects_root=args.projects_root,
        )
    except LaunchPlanError as exc:
        print(f"Hermes launch plan rejected: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("Hermes launch plan rejected by an unexpected validation failure.", file=sys.stderr)
        return 2
    print(json.dumps(plan, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
