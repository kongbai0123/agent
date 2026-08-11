from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from hermes_factory import (  # noqa: E402
    HermesIntegrationManagerCache,
    HermesIntegrationManagerFactory,
)
from hermes import HermesConfigurationError  # noqa: E402
from hermes_docker_attestation import HermesDockerAttestation  # noqa: E402


def native_receipt(tmp_path: Path) -> Path:
    manifest = json.loads(
        (ROOT / "config" / "hermes-sidecar-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    path = tmp_path / "install-receipt.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "deployment_mode": "native",
                "source_tag": manifest["release"]["tag"],
                "source_commit": manifest["release"]["source_commit"],
                "installer_sha256": manifest["official_installer"]["sha256"],
                "package_version": manifest["release"]["package_version"],
                "config_sha256": manifest["runtime"]["config_templates"]["native"]["sha256"],
                "git_global_config_isolated": True,
                "source_worktree_clean": True,
                "tools_enabled": False,
                "mcp_enabled": False,
                "plugins_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    return path


def docker_receipt(tmp_path: Path) -> Path:
    manifest = json.loads(
        (ROOT / "config" / "hermes-sidecar-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    home = tmp_path / "home"
    home.mkdir()
    path = tmp_path / "install-receipt.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "deployment_mode": "docker",
                "source_tag": manifest["release"]["tag"],
                "package_version": manifest["release"]["package_version"],
                "index_digest": manifest["docker_image"]["index_digest"],
                "platform_digest": manifest["docker_image"]["platform_digest"],
                "pinned_reference": manifest["docker_image"]["pinned_reference"],
                "image_id": manifest["docker_image"]["platform_digest"],
                "platform": manifest["docker_image"]["platform"],
                "hermes_home": str(home),
                "config_sha256": manifest["runtime"]["config_templates"]["docker"]["sha256"],
                "tools_enabled": False,
                "mcp_enabled": False,
                "plugins_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    return path


def factory(tmp_path: Path, *, receipt: Path | None = None):
    return HermesIntegrationManagerFactory(
        object(),  # bridge runtime is exercised in its own focused suite
        installation_receipt_path=receipt or tmp_path / "missing-receipt.json",
    )


def enabled_settings():
    return {
        "hermes_enabled": True,
        "hermes_transport": "runs",
        "hermes_model": "hermes-agent",  # legacy default is upgraded from manifest
        "hermes_rollout_mode": "all",
        "hermes_rollout_percentage": 100,
        "hermes_tools_enabled": False,
        "hermes_fallback_enabled": True,
    }


def readonly_settings(project_id: str = "project-readonly"):
    return {
        **enabled_settings(),
        "hermes_rollout_mode": "canary",
        "hermes_rollout_percentage": 0,
        "hermes_canary_session_ids": ["session-readonly"],
        "hermes_tools_enabled": True,
        "hermes_allowed_capabilities": ["hermes.project.read"],
        "hermes_readonly_project_id": project_id,
    }


def test_try_get_is_fail_closed_and_status_safe_when_key_or_receipt_missing(tmp_path):
    cache = HermesIntegrationManagerCache(factory(tmp_path))
    settings = enabled_settings()

    assert cache.try_get(settings, environ={}) is None
    key_status = cache.status(settings, environ={})
    assert key_status["configured"] is False
    assert key_status["health"]["reason"] == "configuration_invalid"
    assert key_status["health_gate"]["allowed"] is False
    assert key_status["metrics"]["available"] is False
    assert key_status["operations"]["metrics"] == key_status["metrics"]

    secret = "x" * 32
    receipt_status = cache.status(
        settings, environ={"HERMES_API_SERVER_KEY": secret}
    )
    assert receipt_status["health"]["reason"] == "installation_unavailable"
    assert secret not in json.dumps(receipt_status)


def test_cache_reuses_atomically_and_reload_replaces_without_exposing_secret(tmp_path):
    receipt = native_receipt(tmp_path)
    cache = HermesIntegrationManagerCache(factory(tmp_path, receipt=receipt))
    settings = enabled_settings()
    env = {"HERMES_API_SERVER_KEY": "a" * 32}

    with ThreadPoolExecutor(max_workers=6) as pool:
        managers = list(pool.map(lambda _n: cache.get(settings, environ=env), range(12)))
    assert len({id(item) for item in managers}) == 1
    first = managers[0]
    assert first.config.default_model == "gemma4-hermes:latest"

    second = cache.reload(settings, environ=env)
    assert second is not first
    assert cache.state() == {
        "configured": True,
        "generation": 2,
        "retired_generation_count": 1,
        "last_error": None,
    }
    assert "a" * 32 not in json.dumps(cache.state())
    cache.close()


def test_factory_accepts_a_legacy_utf8_bom_receipt(tmp_path):
    receipt = native_receipt(tmp_path)
    receipt.write_bytes(b"\xef\xbb\xbf" + receipt.read_bytes())
    cache = HermesIntegrationManagerCache(factory(tmp_path, receipt=receipt))

    manager = cache.get(
        enabled_settings(),
        environ={"HERMES_API_SERVER_KEY": "c" * 32},
    )

    assert manager.config.enabled is True
    cache.close()


def test_native_tools_cannot_be_enabled_by_an_environment_flag(tmp_path):
    receipt = native_receipt(tmp_path)
    cache = HermesIntegrationManagerCache(factory(tmp_path, receipt=receipt))
    settings = {
        **enabled_settings(),
        "hermes_tools_enabled": True,
        "hermes_allowed_capabilities": ["hermes.tool"],
    }
    env = {"HERMES_API_SERVER_KEY": "b" * 32}
    assert cache.try_get(settings, environ=env) is None

    isolated = {**env, "WORKBENCH_HERMES_OS_ISOLATED": "1"}
    assert cache.try_get(settings, environ=isolated) is None
    assert cache.status(settings, environ=isolated)["tools_enabled"] is False
    cache.close()


def test_docker_readonly_tools_require_and_publish_live_attestation(tmp_path):
    receipt = docker_receipt(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "README.md").write_text("safe", encoding="utf-8")
    seen = []

    def attest(spec):
        seen.append(spec)
        return HermesDockerAttestation(
            verified=True,
            reason="verified",
            checked_at=datetime.now(timezone.utc).isoformat(),
            policy_profile="project-readonly-v1",
            evidence_sha256="e" * 64,
            container_id_sha256="c" * 64,
            image_id=spec.image_id,
            config_sha256=spec.config_sha256,
        )

    factory_instance = HermesIntegrationManagerFactory(
        object(),
        installation_receipt_path=receipt,
        docker_attestor=attest,
        readonly_surface_validator=lambda _config: {
            "verified": True,
            "toolset": "workbench-readonly",
            "tools": ["project_read_file", "project_search_files"],
        },
        project_loader=lambda project_id: {
            "id": project_id,
            "root_path": str(project_root),
            "path_status": "ready",
            "permission_mode": "read_only",
            "archived": False,
        },
    )

    prepared = factory_instance.prepare(
        readonly_settings(),
        environ={"HERMES_API_SERVER_KEY": "d" * 32},
    )

    assert prepared.tools_enabled is True
    assert prepared.deployment_mode == "docker"
    assert prepared.tool_policy_profile == "project-readonly-v1"
    assert prepared.tool_project_id == "project-readonly"
    assert prepared.docker_attestation["verified"] is True
    assert prepared.docker_attestation["surface"]["verified"] is True
    assert len(seen) == 1
    assert {mount.destination for mount in seen[0].expected_mounts} == {
        "/opt/data",
        "/opt/data/config.yaml",
        "/workspace/project",
        "/opt/workbench-policy",
    }
    assert seen[0].expected_environment["WORKBENCH_POLICY_PROFILE"] == "project-readonly-v1"
    assert seen[0].expected_labels["com.local-ai-workbench.project-id"] == "project-readonly"


def test_docker_readonly_tools_fail_closed_on_attestation_or_scope(tmp_path):
    receipt = docker_receipt(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir()

    def unverified(spec):
        return HermesDockerAttestation(
            verified=False,
            reason="container_not_running",
            checked_at=datetime.now(timezone.utc).isoformat(),
            policy_profile=spec.policy_profile,
        )

    kwargs = {
        "installation_receipt_path": receipt,
        "docker_attestor": unverified,
        "readonly_surface_validator": lambda _config: {"verified": True},
        "project_loader": lambda _project_id: {
            "id": "project-readonly",
            "root_path": str(project_root),
            "path_status": "ready",
            "permission_mode": "read_only",
            "archived": False,
        },
    }
    with pytest.raises(HermesConfigurationError, match="attestation failed"):
        HermesIntegrationManagerFactory(object(), **kwargs).prepare(
            readonly_settings(),
            environ={"HERMES_API_SERVER_KEY": "e" * 32},
        )

    wrong_capability = {
        **readonly_settings(),
        "hermes_allowed_capabilities": ["hermes.tool"],
    }
    with pytest.raises(HermesConfigurationError, match="exact project read-only"):
        HermesIntegrationManagerFactory(object(), **kwargs).prepare(
            wrong_capability,
            environ={"HERMES_API_SERVER_KEY": "e" * 32},
        )

    mismatched = {
        **kwargs,
        "project_loader": lambda _project_id: {
            "id": "another-project",
            "root_path": str(project_root),
            "path_status": "ready",
            "permission_mode": "read_only",
            "archived": False,
        },
    }
    with pytest.raises(HermesConfigurationError, match="identity changed"):
        HermesIntegrationManagerFactory(object(), **mismatched).prepare(
            readonly_settings(),
            environ={"HERMES_API_SERVER_KEY": "e" * 32},
        )
