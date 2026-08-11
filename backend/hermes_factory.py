"""Settings-aware construction and caching for the Hermes integration seam.

The Workbench owns this factory.  It turns the persisted, secret-free settings
mapping plus the live environment into one coherent manager generation.  A
settings or API-key change swaps the generation atomically without mutating a
manager that may still be serving an in-flight chat.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from hermes import (
    HermesConfig,
    HermesConfigurationError,
    HermesRunsBridge,
    HermesSidecarClient,
)
from hermes_approvals import CapabilityAllowlist, CapabilityRule, HermesApprovalGate
from hermes_docker_attestation import (
    HermesDockerAttestation,
    HermesDockerAttestationSpec,
    HermesDockerBindMount,
    attest_live_hermes_docker,
)
from hermes_integration import HermesIntegrationManager, REQUIRED_RUN_FEATURES
from hermes_metrics import (
    HermesMetricsStore,
    PersistentHermesMetricsStore,
    unavailable_metrics_snapshot,
)
from hermes_operations import (
    HermesOperationsConfig,
    HermesOperationsController,
    HermesSidecarManifest,
    OperationsConfigError,
    RolloutConfig,
    SidecarTransport,
)
from hermes_project_skills_bridge import HermesProjectSkillsBridge
from hermes_readonly_tools import build_project_readonly_tools
from project_skill_runtime import ProjectSkillRuntime


DEFAULT_DEPLOYMENT_MANIFEST = (
    Path(__file__).resolve().parent.parent / "config" / "hermes-sidecar-manifest.json"
)
DEFAULT_INSTALLATION_RECEIPT = (
    Path(__file__).resolve().parent.parent / "runtime" / "hermes" / "install-receipt.json"
)
_MAX_MANIFEST_BYTES = 1_048_576
_READONLY_CAPABILITY = "hermes.project.read"
_READONLY_PROFILE = "project-readonly-v1"
_READONLY_TOOLSET = "workbench-readonly"
_READONLY_TOOLS = frozenset({"project_read_file", "project_search_files"})


def _validate_readonly_sidecar_surface(config: HermesConfig) -> Mapping[str, Any]:
    """Verify the authenticated API surface after Docker attestation."""

    client = HermesSidecarClient(config)
    try:
        health = client.health()
        capabilities = client.capabilities()
        toolsets = client.request_json("GET", "/v1/toolsets")
    finally:
        client.close()
    if str(health.get("status") or "").strip().casefold() != "ok":
        raise HermesConfigurationError("Hermes Docker sidecar health is not ready.")
    features = capabilities.get("features")
    if not isinstance(features, Mapping) or any(
        features.get(name) is not True for name in REQUIRED_RUN_FEATURES
    ):
        raise HermesConfigurationError("Hermes Docker Runs capabilities are incomplete.")
    if (
        toolsets.get("object") != "list"
        or toolsets.get("platform") != "api_server"
        or not isinstance(toolsets.get("data"), list)
    ):
        raise HermesConfigurationError("Hermes toolset surface is malformed.")
    enabled = [
        entry
        for entry in toolsets["data"]
        if isinstance(entry, Mapping) and entry.get("enabled") is True
    ]
    if len(enabled) != 1 or enabled[0].get("name") != _READONLY_TOOLSET:
        raise HermesConfigurationError("Hermes exposed an unreviewed toolset.")
    raw_tools = enabled[0].get("tools")
    if not isinstance(raw_tools, list) or {
        str(item) for item in raw_tools if isinstance(item, str)
    } != _READONLY_TOOLS:
        raise HermesConfigurationError("Hermes exposed an unreviewed tool.")
    return {
        "verified": True,
        "health": "ok",
        "toolset": _READONLY_TOOLSET,
        "tools": sorted(_READONLY_TOOLS),
        "features": {name: True for name in sorted(REQUIRED_RUN_FEATURES)},
    }


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OperationsConfigError(
                f"Hermes deployment manifest contains duplicate field {key!r}."
            )
        result[key] = value
    return result


def _load_deployment_manifest(path: Path) -> Mapping[str, Any]:
    resolved = Path(path).resolve()
    try:
        size = resolved.stat().st_size
        if size <= 0 or size > _MAX_MANIFEST_BYTES:
            raise OperationsConfigError(
                "Hermes deployment manifest has an invalid size."
            )
        # PowerShell 5.1 historically emits a UTF-8 BOM. New receipts are
        # written without one, while utf-8-sig keeps older reviewed receipts
        # readable during an in-place upgrade.
        text = resolved.read_text(encoding="utf-8-sig")
        value = json.loads(text, object_pairs_hook=_strict_object_pairs)
    except OperationsConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OperationsConfigError(
            "Hermes deployment manifest could not be loaded."
        ) from exc
    if not isinstance(value, Mapping):
        raise OperationsConfigError("Hermes deployment manifest must be an object.")
    return value


def _validate_installation_receipt(
    path: Path,
    deployment: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any]]:
    """Verify that an enabled integration has a matching fail-closed install."""

    receipt = _load_deployment_manifest(path)
    try:
        release = deployment["release"]
        installer = deployment["official_installer"]
        docker_image = deployment["docker_image"]
        if not all(
            isinstance(item, Mapping)
            for item in (release, installer, docker_image)
        ):
            raise OperationsConfigError(
                "Hermes deployment manifest identity is incomplete."
            )
        if receipt.get("schema_version") != 1:
            raise OperationsConfigError(
                "Hermes installation receipt schema_version must be 1."
            )
        if receipt.get("package_version") != release.get("package_version"):
            raise OperationsConfigError(
                "Hermes installation receipt package version is not pinned."
            )
        if receipt.get("source_tag") != release.get("tag"):
            raise OperationsConfigError(
                "Hermes installation receipt does not match the pinned release."
            )
        mode = str(receipt.get("deployment_mode") or "").strip().casefold()
        if mode == "native":
            if (
                receipt.get("source_commit") != release.get("source_commit")
                or str(receipt.get("installer_sha256") or "").casefold()
                != str(installer.get("sha256") or "").casefold()
            ):
                raise OperationsConfigError(
                    "Hermes native installation receipt is not pinned."
                )
        elif mode == "docker":
            if (
                receipt.get("index_digest") != docker_image.get("index_digest")
                or receipt.get("platform_digest")
                != docker_image.get("platform_digest")
                or receipt.get("pinned_reference")
                != docker_image.get("pinned_reference")
            ):
                raise OperationsConfigError(
                    "Hermes Docker installation receipt is not pinned."
                )
        else:
            raise OperationsConfigError(
                "Hermes installation receipt has an unsupported deployment mode."
            )
        if any(
            _as_bool(receipt.get(name))
            for name in ("tools_enabled", "mcp_enabled", "plugins_enabled")
        ):
            raise OperationsConfigError(
                "Hermes installation receipt is not fail-closed."
            )
        templates = deployment.get("runtime", {}).get("config_templates", {})
        expected_template = templates.get(mode)
        if not isinstance(expected_template, Mapping) or (
            str(receipt.get("config_sha256") or "").casefold()
            != str(expected_template.get("sha256") or "").casefold()
        ):
            raise OperationsConfigError(
                "Hermes installation receipt config hash is not pinned."
            )
        if mode == "native" and (
            receipt.get("git_global_config_isolated") is not True
            or receipt.get("source_worktree_clean") is not True
        ):
            raise OperationsConfigError(
                "Hermes native installation integrity evidence is incomplete."
            )
        return mode, receipt
    except OperationsConfigError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise OperationsConfigError(
            "Hermes installation receipt is incomplete."
        ) from exc


def operations_config_from_settings(
    settings: Mapping[str, object],
    config: HermesConfig,
    *,
    deployment_manifest_path: Path = DEFAULT_DEPLOYMENT_MANIFEST,
) -> HermesOperationsConfig:
    """Translate the pinned deployment receipt format into runtime controls."""

    deployment = _load_deployment_manifest(deployment_manifest_path)
    try:
        if deployment.get("schema_version") != 1:
            raise OperationsConfigError(
                "Hermes deployment manifest schema_version must be 1."
            )
        if deployment.get("component") != "NousResearch/hermes-agent":
            raise OperationsConfigError(
                "Hermes deployment manifest component is not supported."
            )
        release = deployment["release"]
        installer = deployment["official_installer"]
        if not isinstance(release, Mapping) or not isinstance(installer, Mapping):
            raise OperationsConfigError(
                "Hermes deployment manifest identity is incomplete."
            )
        installer_sha = str(installer["sha256"]).strip().casefold()
        artifact_digest = f"sha256:{installer_sha}"
        manifest = HermesSidecarManifest(
            schema_version=1,
            release=str(release["tag"]),
            source_commit=str(release["source_commit"]),
            artifact_digest=artifact_digest,
            transport=SidecarTransport.HTTP,
            endpoint=config.base_url,
            expected_capabilities=tuple(sorted(REQUIRED_RUN_FEATURES)),
            max_concurrency=1,
            api_key_ref=f"env://{config.api_key_env}",
        )
    except OperationsConfigError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise OperationsConfigError(
            "Hermes deployment manifest identity is incomplete."
        ) from exc

    mode = str(settings.get("hermes_rollout_mode") or "disabled").strip().casefold()
    percentage: object = settings.get("hermes_rollout_percentage", 0.0)
    canary_subjects: object = settings.get("hermes_canary_session_ids", [])
    if mode == "disabled":
        percentage, canary_subjects = 0.0, []
    elif mode == "all":
        percentage, canary_subjects = 100.0, []
    elif mode == "percentage":
        canary_subjects = []
    elif mode == "canary":
        percentage = 0.0
    rollout = RolloutConfig.from_mapping(
        {
            "mode": mode,
            "percentage": percentage,
            "canary_subjects": canary_subjects,
        }
    )
    return HermesOperationsConfig(manifest=manifest, rollout=rollout)


@dataclass(frozen=True)
class _ManagerInputs:
    config: HermesConfig
    operations: HermesOperationsConfig
    tools_enabled: bool
    deployment_mode: str
    tool_policy_profile: str
    tool_project_id: Optional[str]
    docker_attestation: Mapping[str, Any]
    fallback_enabled: bool
    fingerprint: str


class HermesIntegrationManagerFactory:
    """Build one manager from one validated settings/environment snapshot."""

    def __init__(
        self,
        project_skill_runtime: ProjectSkillRuntime,
        *,
        deployment_manifest_path: Path = DEFAULT_DEPLOYMENT_MANIFEST,
        installation_receipt_path: Path = DEFAULT_INSTALLATION_RECEIPT,
        docker_attestor: Callable[[HermesDockerAttestationSpec], HermesDockerAttestation] = attest_live_hermes_docker,
        readonly_surface_validator: Callable[[HermesConfig], Mapping[str, Any]] = _validate_readonly_sidecar_surface,
        project_loader: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
        metrics_store: Optional[HermesMetricsStore] = None,
    ) -> None:
        self.project_skill_runtime = project_skill_runtime
        self.deployment_manifest_path = Path(deployment_manifest_path).resolve()
        self.installation_receipt_path = Path(installation_receipt_path).resolve()
        self.docker_attestor = docker_attestor
        self.readonly_surface_validator = readonly_surface_validator
        if project_loader is None:
            from database import get_project

            project_loader = get_project
        self.project_loader = project_loader
        self.metrics_store = (
            metrics_store
            if metrics_store is not None
            else PersistentHermesMetricsStore()
        )

    def prepare(
        self,
        settings: Mapping[str, object],
        *,
        environ: Optional[Mapping[str, str]] = None,
    ) -> _ManagerInputs:
        if not isinstance(settings, Mapping):
            raise HermesConfigurationError("Workbench settings must be an object.")
        env = os.environ if environ is None else environ
        transport = str(settings.get("hermes_transport") or "runs").strip().casefold()
        if transport != "runs":
            raise HermesConfigurationError(
                "Hermes Workbench integration requires the Runs transport."
            )
        deployment = _load_deployment_manifest(self.deployment_manifest_path)
        effective_settings = dict(settings)
        configured_model = str(effective_settings.get("hermes_model") or "").strip()
        if not configured_model or configured_model == "hermes-agent":
            model = deployment.get("model")
            if not isinstance(model, Mapping):
                raise OperationsConfigError(
                    "Hermes deployment manifest model is incomplete."
                )
            effective_settings["hermes_model"] = model.get("default")
        config = HermesConfig.from_mapping(effective_settings, environ=env)
        receipt_mode = ""
        receipt: Mapping[str, Any] = {}
        if config.enabled:
            receipt_mode, receipt = _validate_installation_receipt(
                self.installation_receipt_path,
                deployment,
            )
        operations = operations_config_from_settings(
            effective_settings,
            config,
            deployment_manifest_path=self.deployment_manifest_path,
        )
        requested_tools = _as_bool(settings.get("hermes_tools_enabled", False))
        allowed = settings.get("hermes_allowed_capabilities", ())
        allowed_capabilities = {
            str(item).strip().casefold()
            for item in allowed
            if isinstance(item, str) and item.strip()
        } if isinstance(allowed, (list, tuple, set, frozenset)) else set()
        tools_enabled = False
        tool_project_id: Optional[str] = None
        tool_policy_profile = "no-tools-v1"
        docker_attestation: dict[str, Any] = {}
        if requested_tools and receipt_mode != "docker":
            raise HermesConfigurationError(
                "Hermes tools are forbidden for the native sidecar; a verified "
                "Docker isolation boundary is required."
            )
        if requested_tools and receipt_mode == "docker":
            if allowed_capabilities != {_READONLY_CAPABILITY}:
                raise HermesConfigurationError(
                    "Hermes tools require the exact project read-only capability."
                )
            if operations.rollout.mode.value != "canary" or not operations.rollout.canary_subjects:
                raise HermesConfigurationError(
                    "Hermes read-only tools may only run for an explicit canary session."
                )
            tool_project_id = str(
                settings.get("hermes_readonly_project_id") or ""
            ).strip()
            project = self.project_loader(tool_project_id) if tool_project_id else None
            if (
                not isinstance(project, Mapping)
                or project.get("archived") in {True, 1, "1"}
                or str(project.get("path_status") or "").casefold() != "ready"
                or str(project.get("permission_mode") or "").casefold() != "read_only"
            ):
                raise HermesConfigurationError(
                    "Hermes read-only project is unavailable or not read-only."
                )
            try:
                readonly_bridge = build_project_readonly_tools(project)
            except Exception as exc:
                raise HermesConfigurationError(
                    "Hermes read-only project boundary is invalid."
                ) from exc
            project_root = Path(str(project.get("root_path") or "")).resolve(strict=True)
            if readonly_bridge.project_id != tool_project_id:
                raise HermesConfigurationError("Hermes read-only project identity changed.")

            policy = deployment.get("readonly_tool_policy")
            runtime = deployment.get("runtime")
            docker_image = deployment.get("docker_image")
            if not all(isinstance(item, Mapping) for item in (policy, runtime, docker_image)):
                raise HermesConfigurationError("Hermes read-only deployment policy is incomplete.")
            if (
                policy.get("profile") != _READONLY_PROFILE
                or policy.get("toolset") != _READONLY_TOOLSET
                or set(policy.get("tools") or ()) != _READONLY_TOOLS
                or _as_bool(policy.get("writes_enabled"))
                or _as_bool(policy.get("shell_enabled"))
                or _as_bool(policy.get("network_tools_enabled"))
            ):
                raise HermesConfigurationError("Hermes read-only deployment policy is invalid.")
            config_spec = policy.get("config_template")
            python_spec = policy.get("python_policy")
            if not isinstance(config_spec, Mapping) or not isinstance(python_spec, Mapping):
                raise HermesConfigurationError("Hermes read-only policy artifacts are incomplete.")
            repo_root = self.deployment_manifest_path.parent.parent.resolve()
            readonly_config = (repo_root / str(config_spec.get("path") or "")).resolve(strict=True)
            python_policy = (repo_root / str(python_spec.get("path") or "")).resolve(strict=True)
            for path in (readonly_config, python_policy):
                try:
                    path.relative_to(repo_root)
                except ValueError as exc:
                    raise HermesConfigurationError("Hermes read-only policy artifact escaped the repository.") from exc
            config_sha = hashlib.sha256(readonly_config.read_bytes()).hexdigest()
            python_sha = hashlib.sha256(python_policy.read_bytes()).hexdigest()
            if (
                config_sha != str(config_spec.get("sha256") or "").casefold()
                or python_sha != str(python_spec.get("sha256") or "").casefold()
            ):
                raise HermesConfigurationError("Hermes read-only policy artifact hash mismatch.")
            home_path = Path(str(receipt.get("hermes_home") or "")).resolve(strict=True)
            expected_home = (self.installation_receipt_path.parent / "home").resolve(strict=True)
            if home_path != expected_home:
                raise HermesConfigurationError("Hermes Docker home is not the reviewed runtime directory.")
            root_hash = hashlib.sha256(
                str(project_root).lower().encode("utf-8")
            ).hexdigest()
            expected_labels = dict(policy.get("labels") or {})
            expected_labels.update(
                {
                    "com.local-ai-workbench.project-id": tool_project_id,
                    "com.local-ai-workbench.project-root-sha256": root_hash,
                    "com.local-ai-workbench.config-sha256": config_sha,
                    "com.local-ai-workbench.python-policy-sha256": python_sha,
                }
            )
            spec = HermesDockerAttestationSpec(
                container_name=str(runtime.get("container_name") or ""),
                pinned_reference=str(docker_image.get("pinned_reference") or ""),
                image_id=str(receipt.get("image_id") or ""),
                expected_mounts=(
                    HermesDockerBindMount(home_path, "/opt/data", False),
                    HermesDockerBindMount(readonly_config, "/opt/data/config.yaml", True),
                    HermesDockerBindMount(project_root, str(policy.get("project_mount") or ""), True),
                    HermesDockerBindMount(
                        python_policy.parent,
                        str(python_spec.get("container_directory") or ""),
                        True,
                    ),
                ),
                config_path=readonly_config,
                config_sha256=config_sha,
                api_server_key=config.api_key,
                expected_labels=expected_labels,
                expected_tmpfs=dict(policy.get("tmpfs") or {}),
                expected_environment=dict(policy.get("environment") or {}),
                policy_profile=_READONLY_PROFILE,
            )
            attestation = self.docker_attestor(spec)
            if not attestation.verified:
                raise HermesConfigurationError(
                    f"Hermes Docker isolation attestation failed: {attestation.reason}."
                )
            surface = dict(self.readonly_surface_validator(config))
            docker_attestation = {
                **attestation.public_dict(),
                "surface": surface,
            }
            tools_enabled = True
            tool_policy_profile = _READONLY_PROFILE
        fallback_enabled = _as_bool(settings.get("hermes_fallback_enabled", True))

        # Only the final digest leaves this method.  API keys and canary IDs
        # influence cache invalidation but are never retained in a public key.
        material = json.dumps(
            {
                "config": {
                    "enabled": config.enabled,
                    "base_url": config.base_url,
                    "api_key_sha256": hashlib.sha256(
                        config.api_key.encode("utf-8")
                    ).hexdigest(),
                    "api_key_env": config.api_key_env,
                    "default_model": config.default_model,
                    "timeout_seconds": config.timeout_seconds,
                    "stream_read_timeout_seconds": config.stream_read_timeout_seconds,
                    "max_response_bytes": config.max_response_bytes,
                },
                "manifest_id": operations.manifest.manifest_id,
                "rollout": {
                    "mode": operations.rollout.mode.value,
                    "percentage": operations.rollout.percentage,
                    "canary_subjects": sorted(operations.rollout.canary_subjects),
                    "selection_salt": operations.rollout.selection_salt,
                },
                "tools_enabled": tools_enabled,
                "deployment_mode": receipt_mode,
                "tool_policy_profile": tool_policy_profile,
                "tool_project_id": tool_project_id,
                "docker_attestation": {
                    key: value
                    for key, value in docker_attestation.items()
                    if key != "checked_at"
                },
                "fallback_enabled": fallback_enabled,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _ManagerInputs(
            config=config,
            operations=operations,
            tools_enabled=tools_enabled,
            deployment_mode=receipt_mode,
            tool_policy_profile=tool_policy_profile,
            tool_project_id=tool_project_id,
            docker_attestation=docker_attestation,
            fallback_enabled=fallback_enabled,
            fingerprint=hashlib.sha256(material).hexdigest(),
        )

    def build_prepared(self, prepared: _ManagerInputs) -> HermesIntegrationManager:
        client = HermesSidecarClient(prepared.config)
        try:
            gate = HermesApprovalGate(
                CapabilityAllowlist(
                    (CapabilityRule(_READONLY_CAPABILITY, True),)
                    if prepared.tools_enabled
                    else ()
                )
            )
            return HermesIntegrationManager(
                config=prepared.config,
                runs=HermesRunsBridge(client),
                project_skills=HermesProjectSkillsBridge(
                    self.project_skill_runtime
                ),
                operations=HermesOperationsController(
                    prepared.operations,
                    metrics_store=self.metrics_store,
                    # The already-redacted manager fingerprint separates
                    # deployment mode, model, timeouts, key rotation, and
                    # reviewed tool surface without persisting those values.
                    metrics_cohort_scope=prepared.fingerprint,
                ),
                tools_enabled=prepared.tools_enabled,
                tool_project_id=prepared.tool_project_id,
                tool_capability=_READONLY_CAPABILITY,
                deployment_mode=prepared.deployment_mode,
                tool_policy_profile=prepared.tool_policy_profile,
                docker_attestation=prepared.docker_attestation,
                fallback_enabled=prepared.fallback_enabled,
                approval_gate=gate,
            )
        except Exception:
            client.close()
            raise

    def build(
        self,
        settings: Mapping[str, object],
        *,
        environ: Optional[Mapping[str, str]] = None,
    ) -> HermesIntegrationManager:
        return self.build_prepared(self.prepare(settings, environ=environ))


class HermesIntegrationManagerCache:
    """Atomically reuse managers and rebuild them when settings change.

    Replaced generations remain alive so a settings save cannot tear down an
    active SSE response.  Call :meth:`close` during application shutdown to
    release every retained HTTP session.
    """

    def __init__(self, factory: HermesIntegrationManagerFactory) -> None:
        self.factory = factory
        self._manager: Optional[HermesIntegrationManager] = None
        self._fingerprint = ""
        self._retired: list[HermesIntegrationManager] = []
        self._generation = 0
        self._last_error = ""
        self._lock = threading.RLock()

    def get(
        self,
        settings: Mapping[str, object],
        *,
        environ: Optional[Mapping[str, str]] = None,
    ) -> HermesIntegrationManager:
        prepared = self.factory.prepare(settings, environ=environ)
        with self._lock:
            if (
                self._manager is not None
                and self._fingerprint == prepared.fingerprint
            ):
                self._last_error = ""
                return self._manager
            manager = self._replace(prepared)
            self._last_error = ""
            return manager

    def reload(
        self,
        settings: Mapping[str, object],
        *,
        environ: Optional[Mapping[str, str]] = None,
    ) -> HermesIntegrationManager:
        """Force a fresh generation after a settings-save event."""

        prepared = self.factory.prepare(settings, environ=environ)
        with self._lock:
            manager = self._replace(prepared)
            self._last_error = ""
            return manager

    def try_get(
        self,
        settings: Mapping[str, object],
        *,
        environ: Optional[Mapping[str, str]] = None,
    ) -> Optional[HermesIntegrationManager]:
        """Resolve a manager without allowing bad optional config to stop the app."""

        try:
            return self.get(settings, environ=environ)
        except HermesConfigurationError:
            reason = "configuration_invalid"
        except OperationsConfigError:
            reason = "installation_unavailable"
        with self._lock:
            self._last_error = reason
        return None

    def status(
        self,
        settings: Mapping[str, object],
        *,
        environ: Optional[Mapping[str, str]] = None,
    ) -> dict[str, Any]:
        """Return manager status or a redacted fail-closed configuration state."""

        env = os.environ if environ is None else environ
        manager = self.try_get(settings, environ=env)
        if manager is not None:
            return manager.status()
        api_key_env = str(
            settings.get("hermes_api_key_env") or "HERMES_API_SERVER_KEY"
        ).strip()
        rollout_mode = str(
            settings.get("hermes_rollout_mode") or "disabled"
        ).strip().casefold()
        if rollout_mode not in {"disabled", "canary", "percentage", "all"}:
            rollout_mode = "disabled"
        with self._lock:
            reason = self._last_error or "configuration_invalid"
        health_gate = {
            "allowed": False,
            "reason": reason,
            "health_status": "unhealthy",
            "circuit_state": "unknown",
            "metrics_available": False,
            "evaluated_at": None,
        }
        metrics = unavailable_metrics_snapshot()
        return {
            "enabled": _as_bool(settings.get("hermes_enabled", False)),
            "configured": False,
            "model": "gemma4-hermes:latest",
            "base_url": "http://127.0.0.1:8642",
            "api_key_configured": bool(env.get(api_key_env)),
            "health": {
                "status": "unhealthy",
                "reported_status": "unhealthy",
                "reason": reason,
            },
            "health_gate": health_gate,
            "rollout": {"mode": rollout_mode},
            "metrics": metrics,
            "features": {
                name: False for name in sorted(REQUIRED_RUN_FEATURES)
            },
            "tools_enabled": False,
            "deployment_mode": "",
            "tool_policy_profile": "no-tools-v1",
            "tool_project_scoped": False,
            "docker_attestation": {
                "verified": False,
                "reason": reason,
            },
            "fallback_enabled": _as_bool(
                settings.get("hermes_fallback_enabled", True)
            ),
            "operations": {
                "health_gate": health_gate,
                "metrics": metrics,
            },
            "pending_approval_count": 0,
        }

    def _replace(self, prepared: _ManagerInputs) -> HermesIntegrationManager:
        replacement = self.factory.build_prepared(prepared)
        if self._manager is not None:
            self._retired.append(self._manager)
        self._manager = replacement
        self._fingerprint = prepared.fingerprint
        self._generation += 1
        return replacement

    def state(self) -> dict[str, Any]:
        """Return secret-free cache diagnostics for tests and observability."""

        with self._lock:
            return {
                "configured": self._manager is not None,
                "generation": self._generation,
                "retired_generation_count": len(self._retired),
                "last_error": self._last_error or None,
            }

    def close(self) -> None:
        with self._lock:
            managers = [*self._retired]
            if self._manager is not None:
                managers.append(self._manager)
            self._manager = None
            self._fingerprint = ""
            self._retired = []
            self._last_error = ""
        for manager in managers:
            close: Optional[Callable[[], None]] = getattr(
                manager.runs.client, "close", None
            )
            if callable(close):
                close()


__all__ = [
    "DEFAULT_DEPLOYMENT_MANIFEST",
    "DEFAULT_INSTALLATION_RECEIPT",
    "HermesIntegrationManagerCache",
    "HermesIntegrationManagerFactory",
    "operations_config_from_settings",
]
