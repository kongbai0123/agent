"""Live, fail-closed Docker isolation attestation for the Hermes sidecar.

The installation receipt proves what was installed.  This module proves that
the *currently running* container bound to the Hermes loopback port still
matches that reviewed installation and launch policy.

Docker inspect output contains the API server key in ``Config.Env``.  Raw
inspect data and command failures therefore never leave this module: callers
receive only bounded reason codes and hashes derived from a secret-free
evidence projection.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from subprocess_env import agent_subprocess_env
except ModuleNotFoundError:  # package import used by standalone operations CLI
    from backend.subprocess_env import agent_subprocess_env


DEFAULT_DOCKER_TIMEOUT_SECONDS = 5.0
DEFAULT_DOCKER_OUTPUT_BYTES = 1_048_576
MAX_CONFIG_BYTES = 1_048_576

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONFIG_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PINNED_REFERENCE_RE = re.compile(
    r"^[a-z0-9][a-z0-9._/:~-]{0,511}@sha256:[0-9a-f]{64}$"
)
_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_LABEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_CONTAINER_PORT = "8642/tcp"
_CONTAINER_HOME = "/opt/data"
_EXPECTED_ENTRYPOINT = ("/init", "/opt/hermes/docker/main-wrapper.sh")
_EXPECTED_COMMAND = ("gateway", "run")
_REQUIRED_CAP_ADD = {
    "chown",
    "dac_override",
    "fowner",
    "setgid",
    "setuid",
}
_REQUIRED_TMPFS = {
    "/tmp": frozenset({"rw", "nosuid", "size=512m"}),
    "/var/tmp": frozenset({"rw", "noexec", "nosuid", "size=256m"}),
    "/run": frozenset({"rw", "exec", "nosuid", "size=64m"}),
}
_REQUIRED_RUNTIME_ENVIRONMENT = {
    "API_SERVER_ENABLED": "true",
    "API_SERVER_HOST": "0.0.0.0",
    "API_SERVER_PORT": "8642",
    "HERMES_HOME": _CONTAINER_HOME,
    "HOME": _CONTAINER_HOME,
    "HERMES_SAFE_MODE": "1",
}


class _AttestationRejected(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _DockerCliFailure(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


DockerCommandRunner = Callable[[Sequence[str], float, int], bytes | str]


@dataclass(frozen=True)
class HermesDockerBindMount:
    """One exact host bind mount expected on the reviewed container."""

    source: Path
    destination: str
    read_only: bool
    propagation: str = "rprivate"


@dataclass(frozen=True)
class HermesDockerAttestationSpec:
    """Reviewed identity and launch policy expected for one live container.

    ``api_server_key`` participates only in an in-memory equality check against
    Docker's environment.  It is excluded from repr/equality and never enters
    the public attestation or its evidence digest.
    """

    container_name: str
    pinned_reference: str
    image_id: str
    expected_mounts: Sequence[HermesDockerBindMount]
    config_path: Path
    config_sha256: str
    api_server_key: str = field(repr=False, compare=False)
    expected_labels: Mapping[str, str] = field(default_factory=dict)
    expected_tmpfs: Mapping[str, str] = field(default_factory=dict)
    expected_environment: Mapping[str, str] = field(default_factory=dict)
    policy_profile: str = "no-tools-v1"
    docker_executable: str = "docker.exe"
    timeout_seconds: float = DEFAULT_DOCKER_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_DOCKER_OUTPUT_BYTES


@dataclass(frozen=True)
class HermesDockerAttestation:
    """Secret-free result suitable for status APIs, logs, and cache keys."""

    verified: bool
    reason: str
    checked_at: str
    policy_profile: str
    evidence_sha256: str = ""
    container_id_sha256: str = ""
    image_id: str = ""
    config_sha256: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "reason": self.reason,
            "checked_at": self.checked_at,
            "policy_profile": self.policy_profile,
            "evidence_sha256": self.evidence_sha256,
            "container_id_sha256": self.container_id_sha256,
            "image_id": self.image_id,
            "config_sha256": self.config_sha256,
        }


def _checked_at(now: Callable[[], datetime] | None) -> str:
    value = now() if now is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _failure(
    spec: HermesDockerAttestationSpec,
    reason: str,
    checked_at: str,
) -> HermesDockerAttestation:
    profile = str(getattr(spec, "policy_profile", "") or "")[:128]
    return HermesDockerAttestation(
        verified=False,
        reason=reason,
        checked_at=checked_at,
        policy_profile=profile,
    )


def _read_pipe_bounded(
    stream: Any,
    chunks: list[bytes],
    *,
    shared: dict[str, int],
    lock: threading.Lock,
    overflow: threading.Event,
    maximum: int,
) -> None:
    try:
        while True:
            chunk = stream.read(65_536)
            if not chunk:
                return
            if not isinstance(chunk, bytes):
                chunk = str(chunk).encode("utf-8", errors="replace")
            with lock:
                remaining = maximum + 1 - shared["size"]
                if remaining > 0:
                    kept = chunk[:remaining]
                    chunks.append(kept)
                    shared["size"] += len(kept)
                if len(chunk) > max(0, remaining) or shared["size"] > maximum:
                    overflow.set()
                    return
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _bounded_subprocess(
    argv: Sequence[str],
    timeout_seconds: float,
    max_output_bytes: int,
) -> bytes:
    """Run one argv-only command while bounding time and combined output."""

    if (
        not isinstance(argv, (list, tuple))
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
        or timeout_seconds <= 0
        or max_output_bytes <= 0
    ):
        raise _DockerCliFailure("docker_invocation_invalid")

    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=agent_subprocess_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, ValueError) as exc:
        raise _DockerCliFailure("docker_unavailable") from exc

    if process.stdout is None or process.stderr is None:  # pragma: no cover
        try:
            process.kill()
        except Exception:
            pass
        raise _DockerCliFailure("docker_io_unavailable")

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    shared = {"size": 0}
    lock = threading.Lock()
    overflow = threading.Event()
    readers = [
        threading.Thread(
            target=_read_pipe_bounded,
            args=(stream, chunks),
            kwargs={
                "shared": shared,
                "lock": lock,
                "overflow": overflow,
                "maximum": max_output_bytes,
            },
            daemon=True,
        )
        for stream, chunks in (
            (process.stdout, stdout_chunks),
            (process.stderr, stderr_chunks),
        )
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    while True:
        if overflow.is_set():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            process.wait(timeout=min(0.05, remaining))
            break
        except subprocess.TimeoutExpired:
            continue

    if timed_out or overflow.is_set():
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=2.0)
    except Exception:
        pass
    for reader in readers:
        reader.join(timeout=2.0)

    if timed_out:
        raise _DockerCliFailure("docker_timeout")
    if overflow.is_set() or shared["size"] > max_output_bytes:
        raise _DockerCliFailure("docker_output_too_large")
    if process.returncode != 0:
        # stderr may contain environment values or other sensitive details.
        raise _DockerCliFailure("docker_command_failed")
    return b"".join(stdout_chunks)


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _inspect_one(
    argv: Sequence[str],
    *,
    runner: DockerCommandRunner,
    timeout_seconds: float,
    max_output_bytes: int,
) -> Mapping[str, Any]:
    try:
        raw = runner(argv, timeout_seconds, max_output_bytes)
    except _DockerCliFailure:
        raise
    except Exception as exc:
        # Never propagate runner text: a Docker failure can echo Config.Env.
        raise _DockerCliFailure("docker_command_failed") from exc
    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
    elif isinstance(raw, bytes):
        encoded = raw
    else:
        raise _DockerCliFailure("docker_response_invalid")
    if not encoded or len(encoded) > max_output_bytes:
        reason = "docker_output_too_large" if encoded else "docker_response_invalid"
        raise _DockerCliFailure(reason)
    try:
        decoded = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_strict_object_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _DockerCliFailure("docker_response_invalid") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != 1
        or not isinstance(decoded[0], Mapping)
    ):
        raise _DockerCliFailure("docker_response_invalid")
    return decoded[0]


def _mapping(value: Any, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _AttestationRejected(reason)
    return value


def _string_list(value: Any, reason: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _AttestationRejected(reason)
    return tuple(value)


def _environment(value: Any, reason: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in _string_list(value, reason):
        if "=" not in entry:
            raise _AttestationRejected(reason)
        name, item_value = entry.split("=", 1)
        if not _ENV_NAME_RE.fullmatch(name) or name in result:
            raise _AttestationRejected(reason)
        result[name] = item_value
    return result


def _labels(value: Any, reason: str) -> dict[str, str]:
    if value is None:
        return {}
    source = _mapping(value, reason)
    result: dict[str, str] = {}
    for key, item_value in source.items():
        if (
            not isinstance(key, str)
            or not _LABEL_NAME_RE.fullmatch(key)
            or not isinstance(item_value, str)
            or _CONTROL_RE.search(item_value)
        ):
            raise _AttestationRejected(reason)
        result[key] = item_value
    return result


def _canonical_host_path(value: str | Path) -> str:
    try:
        resolved = Path(value).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _AttestationRejected("mounts_invalid") from exc
    return os.path.normcase(os.path.normpath(os.fspath(resolved)))


def _container_path_for_config(spec: HermesDockerAttestationSpec) -> str:
    config_path = Path(spec.config_path).resolve(strict=True)
    candidates: list[str] = []
    for expected in spec.expected_mounts:
        source = Path(expected.source).resolve(strict=True)
        destination = expected.destination.rstrip("/") or "/"
        if source.is_file():
            if source == config_path:
                candidates.append(destination)
            continue
        try:
            relative = config_path.relative_to(source)
        except ValueError:
            continue
        suffix = relative.as_posix()
        candidates.append(destination + ("/" + suffix if suffix else ""))
    if candidates != ["/opt/data/config.yaml"]:
        raise _AttestationRejected("config_path_invalid")
    return candidates[0]


def _hash_config(spec: HermesDockerAttestationSpec) -> str:
    config_path = Path(spec.config_path)
    try:
        if config_path.is_symlink() or not config_path.is_file():
            raise _AttestationRejected("config_file_invalid")
        _container_path_for_config(spec)
        size = config_path.stat().st_size
        if size <= 0 or size > MAX_CONFIG_BYTES:
            raise _AttestationRejected("config_file_invalid")
        digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    except _AttestationRejected:
        raise
    except OSError as exc:
        raise _AttestationRejected("config_file_invalid") from exc
    if digest != spec.config_sha256:
        raise _AttestationRejected("config_hash_mismatch")
    return digest


def _validate_spec(spec: HermesDockerAttestationSpec) -> None:
    if not isinstance(spec, HermesDockerAttestationSpec):
        raise _AttestationRejected("spec_invalid")
    if not _CONTAINER_NAME_RE.fullmatch(spec.container_name):
        raise _AttestationRejected("spec_invalid")
    if not _PINNED_REFERENCE_RE.fullmatch(spec.pinned_reference):
        raise _AttestationRejected("spec_invalid")
    if not _DIGEST_RE.fullmatch(spec.image_id):
        raise _AttestationRejected("spec_invalid")
    if not _CONFIG_DIGEST_RE.fullmatch(spec.config_sha256):
        raise _AttestationRejected("spec_invalid")
    if not _PROFILE_RE.fullmatch(spec.policy_profile):
        raise _AttestationRejected("spec_invalid")
    if (
        not isinstance(spec.docker_executable, str)
        or not spec.docker_executable
        or _CONTROL_RE.search(spec.docker_executable)
        or spec.timeout_seconds <= 0
        or spec.timeout_seconds > 30
        or spec.max_output_bytes <= 0
        or spec.max_output_bytes > 8 * 1_048_576
    ):
        raise _AttestationRejected("spec_invalid")
    if (
        not isinstance(spec.api_server_key, str)
        or len(spec.api_server_key.encode("utf-8")) < 32
        or _CONTROL_RE.search(spec.api_server_key)
    ):
        raise _AttestationRejected("spec_invalid")
    expected_labels = _labels(spec.expected_labels, "spec_invalid")
    if not expected_labels:
        raise _AttestationRejected("spec_invalid")
    expected_environment = _mapping(spec.expected_environment, "spec_invalid")
    for name, value in expected_environment.items():
        if (
            not isinstance(name, str)
            or not _ENV_NAME_RE.fullmatch(name)
            or name in {"API_SERVER_KEY", "HERMES_YOLO_MODE"}
            or name in _REQUIRED_RUNTIME_ENVIRONMENT
            or not isinstance(value, str)
            or len(value) > 4096
            or _CONTROL_RE.search(value)
        ):
            raise _AttestationRejected("spec_invalid")
    if (
        not isinstance(spec.expected_mounts, (list, tuple))
        or not spec.expected_mounts
        or any(
            not isinstance(item, HermesDockerBindMount)
            for item in spec.expected_mounts
        )
    ):
        raise _AttestationRejected("spec_invalid")
    destinations: set[str] = set()
    has_container_home = False
    for expected in spec.expected_mounts:
        destination = str(expected.destination or "").rstrip("/") or "/"
        if (
            not destination.startswith("/")
            or ".." in destination.split("/")
            or destination in destinations
            or expected.propagation.casefold() != "rprivate"
            or not isinstance(expected.read_only, bool)
        ):
            raise _AttestationRejected("spec_invalid")
        source = Path(expected.source)
        try:
            absolute = os.path.normcase(os.path.abspath(os.fspath(source)))
            real = _canonical_host_path(source)
        except _AttestationRejected as exc:
            raise _AttestationRejected("spec_invalid") from exc
        if source.is_symlink() or absolute != real or _contains_socket_path(real):
            raise _AttestationRejected("spec_invalid")
        destinations.add(destination)
        has_container_home = has_container_home or destination == _CONTAINER_HOME
    if not has_container_home:
        raise _AttestationRejected("spec_invalid")
    tmpfs = _mapping(spec.expected_tmpfs, "spec_invalid")
    normalized_tmpfs: dict[str, frozenset[str]] = {}
    for raw_destination, options in tmpfs.items():
        destination = (
            raw_destination.rstrip("/") or "/"
            if isinstance(raw_destination, str)
            else ""
        )
        if (
            not isinstance(raw_destination, str)
            or not destination.startswith("/")
            or ".." in destination.split("/")
            or destination in destinations
            or not isinstance(options, str)
            or _CONTROL_RE.search(options)
        ):
            raise _AttestationRejected("spec_invalid")
        destinations.add(destination)
        normalized_tmpfs[destination] = _tmpfs_options(options)
    if any(
        normalized_tmpfs.get(destination) != options
        for destination, options in _REQUIRED_TMPFS.items()
    ):
        raise _AttestationRejected("spec_invalid")


def _validate_image(
    image: Mapping[str, Any],
    spec: HermesDockerAttestationSpec,
) -> tuple[dict[str, str], dict[str, str]]:
    if image.get("Id") != spec.image_id:
        raise _AttestationRejected("image_id_mismatch")
    if image.get("Os") != "linux" or image.get("Architecture") != "amd64":
        raise _AttestationRejected("image_platform_mismatch")
    repo_digests = _string_list(image.get("RepoDigests"), "image_identity_invalid")
    # Docker Desktop canonicalizes Docker Hub references differently across
    # inspect fields: Config.Image retains ``docker.io/`` while RepoDigests
    # commonly omits that default registry.  Accept only that one equivalent
    # spelling; the repository path and sha256 digest still have to match
    # exactly, and the container/image IDs are checked independently above.
    accepted_references = {spec.pinned_reference}
    if spec.pinned_reference.startswith("docker.io/"):
        accepted_references.add(spec.pinned_reference[len("docker.io/") :])
    if not accepted_references.intersection(repo_digests):
        raise _AttestationRejected("image_digest_mismatch")
    image_config = _mapping(image.get("Config"), "image_config_invalid")
    return (
        _environment(image_config.get("Env"), "image_environment_invalid"),
        _labels(image_config.get("Labels"), "image_labels_invalid"),
    )


def _validate_ports(host_config: Mapping[str, Any], network: Mapping[str, Any]) -> None:
    expected = [{"HostIp": "127.0.0.1", "HostPort": "8642"}]
    bindings = _mapping(host_config.get("PortBindings"), "port_binding_invalid")
    if dict(bindings) != {_CONTAINER_PORT: expected}:
        raise _AttestationRejected("port_binding_invalid")
    network_ports = _mapping(network.get("Ports"), "port_binding_invalid")
    if dict(network_ports) != {_CONTAINER_PORT: expected}:
        raise _AttestationRejected("port_binding_invalid")
    if host_config.get("PublishAllPorts") is not False:
        raise _AttestationRejected("port_binding_invalid")
    networks = _mapping(network.get("Networks"), "network_attachment_invalid")
    if set(networks) != {"bridge"}:
        raise _AttestationRejected("network_attachment_invalid")


def _contains_socket_path(value: str) -> bool:
    normalized = value.strip().casefold().replace("\\", "/")
    return (
        "docker.sock" in normalized
        or "docker_engine" in normalized
        or normalized.startswith("npipe:")
    )


def _tmpfs_options(value: str) -> frozenset[str]:
    return frozenset(
        item.strip().casefold()
        for item in str(value or "").split(",")
        if item.strip()
    )


def _validate_mounts(
    container: Mapping[str, Any],
    host_config: Mapping[str, Any],
    spec: HermesDockerAttestationSpec,
) -> None:
    mounts = container.get("Mounts")
    expected_binds = {
        expected.destination.rstrip("/") or "/": expected
        for expected in spec.expected_mounts
    }
    expected_tmpfs = {
        destination.rstrip("/") or "/": str(options)
        for destination, options in spec.expected_tmpfs.items()
    }
    if not isinstance(mounts, list) or len(mounts) not in {
        len(expected_binds),
        len(expected_binds) + len(expected_tmpfs),
    }:
        raise _AttestationRejected("mounts_invalid")
    seen: set[str] = set()
    for raw_mount in mounts:
        mount = _mapping(raw_mount, "mounts_invalid")
        destination_value = mount.get("Destination")
        if not isinstance(destination_value, str):
            raise _AttestationRejected("mounts_invalid")
        destination = destination_value.rstrip("/") or "/"
        if destination in seen or _contains_socket_path(destination):
            raise _AttestationRejected("mounts_invalid")
        seen.add(destination)
        if destination in expected_binds:
            expected = expected_binds[destination]
            source = mount.get("Source")
            if (
                mount.get("Type") != "bind"
                or not isinstance(source, str)
                or _canonical_host_path(source)
                != _canonical_host_path(expected.source)
                or mount.get("RW") is not (not expected.read_only)
                or str(mount.get("Propagation") or "rprivate").casefold()
                != expected.propagation.casefold()
                or _contains_socket_path(source)
            ):
                raise _AttestationRejected("mounts_invalid")
        elif destination in expected_tmpfs:
            options = _tmpfs_options(expected_tmpfs[destination])
            mode = _tmpfs_options(str(mount.get("Mode") or ""))
            expected_rw = "ro" not in options
            if (
                mount.get("Type") != "tmpfs"
                or str(mount.get("Source") or "")
                or mount.get("RW") is not expected_rw
                or (mode and mode != options)
            ):
                raise _AttestationRejected("mounts_invalid")
        else:
            raise _AttestationRejected("mounts_invalid")
    bind_destinations = set(expected_binds)
    all_destinations = bind_destinations | set(expected_tmpfs)
    if frozenset(seen) not in {
        frozenset(bind_destinations),
        frozenset(all_destinations),
    }:
        raise _AttestationRejected("mounts_invalid")
    binds = _string_list(host_config.get("Binds"), "mounts_invalid")
    host_mounts = host_config.get("Mounts")
    if binds:
        if (
            len(binds) != len(expected_binds)
            or any(_contains_socket_path(item) for item in binds)
            or host_mounts not in (None, [])
        ):
            raise _AttestationRejected("mounts_invalid")
    else:
        if not isinstance(host_mounts, list) or len(host_mounts) != len(expected_binds):
            raise _AttestationRejected("mounts_invalid")
        host_seen: set[str] = set()
        for raw_mount in host_mounts:
            mount = _mapping(raw_mount, "mounts_invalid")
            target_value = mount.get("Target")
            source = mount.get("Source")
            if not isinstance(target_value, str) or not isinstance(source, str):
                raise _AttestationRejected("mounts_invalid")
            target = target_value.rstrip("/") or "/"
            expected = expected_binds.get(target)
            if (
                expected is None
                or target in host_seen
                or mount.get("Type") != "bind"
                or _canonical_host_path(source) != _canonical_host_path(expected.source)
                or bool(mount.get("ReadOnly", False)) is not expected.read_only
                or _contains_socket_path(source)
            ):
                raise _AttestationRejected("mounts_invalid")
            bind_options = mount.get("BindOptions")
            if bind_options not in (None, {}):
                options = _mapping(bind_options, "mounts_invalid")
                if set(options) - {"Propagation"} or str(
                    options.get("Propagation") or "rprivate"
                ).casefold() != expected.propagation.casefold():
                    raise _AttestationRejected("mounts_invalid")
            host_seen.add(target)
        if host_seen != bind_destinations:
            raise _AttestationRejected("mounts_invalid")
    if host_config.get("VolumesFrom") not in (None, []):
        raise _AttestationRejected("mounts_invalid")
    actual_tmpfs = host_config.get("Tmpfs")
    if expected_tmpfs:
        actual_tmpfs_mapping = _mapping(actual_tmpfs, "mounts_invalid")
        normalized_actual = {
            str(destination).rstrip("/") or "/": _tmpfs_options(str(options))
            for destination, options in actual_tmpfs_mapping.items()
        }
        normalized_expected = {
            destination: _tmpfs_options(options)
            for destination, options in expected_tmpfs.items()
        }
        if normalized_actual != normalized_expected:
            raise _AttestationRejected("mounts_invalid")
    elif actual_tmpfs not in (None, {}, []):
        raise _AttestationRejected("mounts_invalid")


def _validate_security(host_config: Mapping[str, Any]) -> None:
    if host_config.get("Privileged") is not False:
        raise _AttestationRejected("privileged_container")
    cap_add = {
        item.casefold().removeprefix("cap_")
        for item in _string_list(
            host_config.get("CapAdd"), "capabilities_invalid"
        )
    }
    if cap_add != _REQUIRED_CAP_ADD:
        raise _AttestationRejected("capabilities_invalid")
    cap_drop = {item.casefold().removeprefix("cap_") for item in _string_list(
        host_config.get("CapDrop"), "capabilities_invalid"
    )}
    if cap_drop != {"all"}:
        raise _AttestationRejected("capabilities_invalid")
    security_options = {
        item.casefold() for item in _string_list(
            host_config.get("SecurityOpt"), "security_options_invalid"
        )
    }
    if not security_options.intersection(
        {
            "no-new-privileges",
            "no-new-privileges:true",
            "no-new-privileges=true",
        }
    ):
        raise _AttestationRejected("security_options_invalid")
    if any(
        "unconfined" in item or item in {"label:disable", "no-new-privileges:false"}
        for item in security_options
    ):
        raise _AttestationRejected("security_options_invalid")
    for field_name in (
        "NetworkMode",
        "PidMode",
        "IpcMode",
        "UTSMode",
        "UsernsMode",
        "CgroupnsMode",
    ):
        value = str(host_config.get(field_name) or "").strip().casefold()
        if value == "host" or value.startswith("container:"):
            raise _AttestationRejected("host_namespace_forbidden")
    for field_name in ("Devices", "DeviceCgroupRules", "DeviceRequests"):
        if host_config.get(field_name) not in (None, []):
            raise _AttestationRejected("devices_forbidden")
    if host_config.get("AutoRemove") is not True:
        raise _AttestationRejected("container_lifecycle_invalid")
    if host_config.get("PidsLimit") != 256:
        raise _AttestationRejected("resource_limits_invalid")
    if host_config.get("ReadonlyRootfs") is not True:
        raise _AttestationRejected("readonly_rootfs_required")
    restart_policy = _mapping(
        host_config.get("RestartPolicy"), "container_lifecycle_invalid"
    )
    if str(restart_policy.get("Name") or "no").casefold() not in {"", "no"}:
        raise _AttestationRejected("container_lifecycle_invalid")


def _validate_container(
    container: Mapping[str, Any],
    image_environment: Mapping[str, str],
    image_labels: Mapping[str, str],
    spec: HermesDockerAttestationSpec,
) -> dict[str, Any]:
    container_id = container.get("Id")
    if not isinstance(container_id, str) or not re.fullmatch(
        r"[0-9a-f]{64}", container_id
    ):
        raise _AttestationRejected("container_identity_invalid")
    if str(container.get("Name") or "").removeprefix("/") != spec.container_name:
        raise _AttestationRejected("container_identity_invalid")
    if container.get("Image") != spec.image_id:
        raise _AttestationRejected("container_image_mismatch")

    state = _mapping(container.get("State"), "container_state_invalid")
    if (
        state.get("Running") is not True
        or str(state.get("Status") or "").casefold() != "running"
        or not isinstance(state.get("Pid"), int)
        or state.get("Pid", 0) <= 0
        or not isinstance(state.get("StartedAt"), str)
        or not state.get("StartedAt")
        or state.get("Paused") is True
        or state.get("Restarting") is True
        or state.get("Dead") is True
    ):
        raise _AttestationRejected("container_not_running")

    config = _mapping(container.get("Config"), "container_config_invalid")
    if config.get("Image") != spec.pinned_reference:
        raise _AttestationRejected("container_image_mismatch")
    entrypoint = tuple(
        _string_list(config.get("Entrypoint"), "container_command_invalid")
    )
    if entrypoint != _EXPECTED_ENTRYPOINT:
        raise _AttestationRejected("container_command_invalid")
    if tuple(_string_list(config.get("Cmd"), "container_command_invalid")) != _EXPECTED_COMMAND:
        raise _AttestationRejected("container_command_invalid")

    actual_environment = _environment(
        config.get("Env"), "container_environment_invalid"
    )
    if "HERMES_YOLO_MODE" in actual_environment:
        raise _AttestationRejected("container_environment_invalid")
    expected_environment = dict(image_environment)
    expected_environment.update(_REQUIRED_RUNTIME_ENVIRONMENT)
    expected_environment.update(dict(spec.expected_environment))
    actual_api_key = actual_environment.pop("API_SERVER_KEY", None)
    if not isinstance(actual_api_key, str) or not hmac.compare_digest(
        actual_api_key.encode("utf-8"), spec.api_server_key.encode("utf-8")
    ):
        raise _AttestationRejected("container_environment_invalid")
    if actual_environment != expected_environment:
        raise _AttestationRejected("container_environment_invalid")

    actual_labels = _labels(config.get("Labels"), "container_labels_invalid")
    expected_labels = dict(image_labels)
    expected_labels.update(dict(spec.expected_labels))
    if actual_labels != expected_labels:
        raise _AttestationRejected("container_labels_invalid")

    host_config = _mapping(container.get("HostConfig"), "host_config_invalid")
    network = _mapping(
        container.get("NetworkSettings"), "port_binding_invalid"
    )
    _validate_ports(host_config, network)
    _validate_mounts(container, host_config, spec)
    _validate_security(host_config)

    return {
        "container_id": container_id,
        "container_name": spec.container_name,
        "image_id": spec.image_id,
        "pinned_reference": spec.pinned_reference,
        "policy_profile": spec.policy_profile,
        "host_binding": "127.0.0.1:8642",
        "container_home": _CONTAINER_HOME,
        "mounts": [
            {
                "source": _canonical_host_path(item.source),
                "destination": item.destination.rstrip("/") or "/",
                "read_only": item.read_only,
                "propagation": item.propagation,
            }
            for item in spec.expected_mounts
        ],
        "tmpfs": sorted(
            (destination, sorted(_tmpfs_options(options)))
            for destination, options in spec.expected_tmpfs.items()
        ),
        "labels": sorted(spec.expected_labels.items()),
        "environment": sorted(spec.expected_environment.items()),
        "environment_verified": True,
        "security": {
            "privileged": False,
            "cap_drop": ["ALL"],
            "cap_add": sorted(item.upper() for item in _REQUIRED_CAP_ADD),
            "no_new_privileges": True,
            "host_namespaces": False,
            "devices": False,
            "pids_limit": 256,
            "readonly_rootfs": True,
        },
    }


def attest_live_hermes_docker(
    spec: HermesDockerAttestationSpec,
    *,
    runner: DockerCommandRunner | None = None,
    now: Callable[[], datetime] | None = None,
) -> HermesDockerAttestation:
    """Attest the currently running Hermes Docker container.

    Every failure returns a stable, secret-free reason code.  The function
    never accepts a caller-provided receipt as proof of live state; it always
    asks Docker for both the named container and the digest-pinned image.
    """

    checked_at = _checked_at(now)
    command_runner = runner or _bounded_subprocess
    try:
        _validate_spec(spec)
        config_digest = _hash_config(spec)
        container = _inspect_one(
            [
                spec.docker_executable,
                "container",
                "inspect",
                spec.container_name,
            ],
            runner=command_runner,
            timeout_seconds=spec.timeout_seconds,
            max_output_bytes=spec.max_output_bytes,
        )
        image = _inspect_one(
            [
                spec.docker_executable,
                "image",
                "inspect",
                spec.pinned_reference,
            ],
            runner=command_runner,
            timeout_seconds=spec.timeout_seconds,
            max_output_bytes=spec.max_output_bytes,
        )
        image_environment, image_labels = _validate_image(image, spec)
        first_evidence = _validate_container(
            container,
            image_environment,
            image_labels,
            spec,
        )
        final_container = _inspect_one(
            [
                spec.docker_executable,
                "container",
                "inspect",
                spec.container_name,
            ],
            runner=command_runner,
            timeout_seconds=spec.timeout_seconds,
            max_output_bytes=spec.max_output_bytes,
        )
        evidence = _validate_container(
            final_container,
            image_environment,
            image_labels,
            spec,
        )
        first_state = _mapping(container.get("State"), "container_state_invalid")
        final_state = _mapping(
            final_container.get("State"), "container_state_invalid"
        )
        if (
            first_evidence["container_id"] != evidence["container_id"]
            or container.get("Image") != final_container.get("Image")
            or first_state.get("StartedAt") != final_state.get("StartedAt")
        ):
            raise _AttestationRejected("container_replaced_during_attestation")
        evidence["config_sha256"] = config_digest
        canonical = json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return HermesDockerAttestation(
            verified=True,
            reason="verified",
            checked_at=checked_at,
            policy_profile=spec.policy_profile,
            evidence_sha256=hashlib.sha256(canonical).hexdigest(),
            container_id_sha256=hashlib.sha256(
                str(evidence["container_id"]).encode("ascii")
            ).hexdigest(),
            image_id=spec.image_id,
            config_sha256=config_digest,
        )
    except _DockerCliFailure as exc:
        return _failure(spec, exc.reason, checked_at)
    except _AttestationRejected as exc:
        return _failure(spec, exc.reason, checked_at)
    except Exception:
        # Last-resort boundary: never let malformed inspect data or exception
        # text containing an environment value escape to a status endpoint.
        return _failure(spec, "attestation_failed", checked_at)


__all__ = [
    "DEFAULT_DOCKER_OUTPUT_BYTES",
    "DEFAULT_DOCKER_TIMEOUT_SECONDS",
    "DockerCommandRunner",
    "HermesDockerBindMount",
    "HermesDockerAttestation",
    "HermesDockerAttestationSpec",
    "attest_live_hermes_docker",
]
