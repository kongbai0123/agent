from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import hermes_docker_attestation as attestation_module  # noqa: E402
from hermes_docker_attestation import (  # noqa: E402
    HermesDockerBindMount,
    HermesDockerAttestationSpec,
    attest_live_hermes_docker,
)


IMAGE_ID = "sha256:" + "a" * 64
CONTAINER_ID = "b" * 64
PINNED_REFERENCE = "docker.io/nousresearch/hermes-agent@sha256:" + "c" * 64
API_KEY = "phase5-test-key-" + "s" * 48
FIXED_TIME = datetime(2026, 8, 11, 4, 5, 6, tzinfo=timezone.utc)
IMAGE_ENV = ["PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8"]
IMAGE_LABELS = {
    "org.opencontainers.image.source": "https://github.com/NousResearch/hermes-agent"
}
RUNTIME_LABELS = {
    "com.local-ai-workbench.component": "hermes-sidecar",
    "com.local-ai-workbench.policy-profile": "terminal-canary-v1",
    "com.local-ai-workbench.manifest": "sha256:" + "d" * 64,
}
REQUIRED_TMPFS = {
    "/tmp": "rw,nosuid,size=512m",
    "/var/tmp": "rw,noexec,nosuid,size=256m",
    "/run": "rw,exec,nosuid,size=64m",
}


def _env() -> list[str]:
    return [
        *IMAGE_ENV,
        f"API_SERVER_KEY={API_KEY}",
        "API_SERVER_ENABLED=true",
        "API_SERVER_HOST=0.0.0.0",
        "API_SERVER_PORT=8642",
        "HERMES_HOME=/opt/data",
        "HOME=/opt/data",
        "HERMES_SAFE_MODE=1",
    ]


def _container(mount_source: Path) -> dict:
    binding = [{"HostIp": "127.0.0.1", "HostPort": "8642"}]
    return {
        "Id": CONTAINER_ID,
        "Name": "/local-ai-workbench-hermes",
        "Image": IMAGE_ID,
        "State": {
            "Status": "running",
            "Running": True,
            "Paused": False,
            "Restarting": False,
            "Dead": False,
            "Pid": 1234,
            "StartedAt": "2026-08-11T04:00:00.000000000Z",
        },
        "Config": {
            "Image": PINNED_REFERENCE,
            "Entrypoint": ["/init", "/opt/hermes/docker/main-wrapper.sh"],
            "Cmd": ["gateway", "run"],
            "Env": _env(),
            "Labels": {**IMAGE_LABELS, **RUNTIME_LABELS},
        },
        "HostConfig": {
            "Privileged": False,
            "CapAdd": ["DAC_OVERRIDE", "CHOWN", "FOWNER", "SETUID", "SETGID"],
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "NetworkMode": "default",
            "PidMode": "",
            "IpcMode": "private",
            "UTSMode": "",
            "UsernsMode": "",
            "CgroupnsMode": "private",
            "Devices": [],
            "DeviceCgroupRules": None,
            "DeviceRequests": [],
            "Binds": [f"{mount_source}:/opt/data"],
            "VolumesFrom": None,
            "Tmpfs": dict(REQUIRED_TMPFS),
            "PortBindings": {"8642/tcp": binding},
            "PublishAllPorts": False,
            "AutoRemove": True,
            "PidsLimit": 256,
            "ReadonlyRootfs": True,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
        },
        "NetworkSettings": {
            "Ports": {"8642/tcp": binding},
            "Networks": {"bridge": {"NetworkID": "e" * 64}},
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(mount_source),
                "Destination": "/opt/data",
                "Mode": "",
                "RW": True,
                "Propagation": "rprivate",
            },
            {
                "Type": "tmpfs",
                "Source": "",
                "Destination": "/tmp",
                "Mode": REQUIRED_TMPFS["/tmp"],
                "RW": True,
                "Propagation": "",
            },
            {
                "Type": "tmpfs",
                "Source": "",
                "Destination": "/var/tmp",
                "Mode": REQUIRED_TMPFS["/var/tmp"],
                "RW": True,
                "Propagation": "",
            },
            {
                "Type": "tmpfs",
                "Source": "",
                "Destination": "/run",
                "Mode": REQUIRED_TMPFS["/run"],
                "RW": True,
                "Propagation": "",
            },
        ],
    }


def _image() -> dict:
    return {
        "Id": IMAGE_ID,
        "RepoDigests": [PINNED_REFERENCE],
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {"Env": list(IMAGE_ENV), "Labels": dict(IMAGE_LABELS)},
    }


def _spec(tmp_path: Path, **updates) -> HermesDockerAttestationSpec:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = home / "config.yaml"
    if not config.exists():
        config.write_text(
            "platform_toolsets:\n  api_server: [terminal]\n",
            encoding="utf-8",
        )
    values = {
        "container_name": "local-ai-workbench-hermes",
        "pinned_reference": PINNED_REFERENCE,
        "image_id": IMAGE_ID,
        "expected_mounts": (
            HermesDockerBindMount(
                source=home,
                destination="/opt/data",
                read_only=False,
            ),
        ),
        "config_path": config,
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "api_server_key": API_KEY,
        "expected_labels": dict(RUNTIME_LABELS),
        "expected_tmpfs": dict(REQUIRED_TMPFS),
        "policy_profile": "terminal-canary-v1",
        "timeout_seconds": 2.0,
        "max_output_bytes": 65_536,
    }
    values.update(updates)
    return HermesDockerAttestationSpec(**values)


def _home_source(spec: HermesDockerAttestationSpec) -> Path:
    return Path(
        next(
            mount.source
            for mount in spec.expected_mounts
            if mount.destination == "/opt/data"
        )
    )


class FixtureRunner:
    def __init__(
        self,
        containers: list[dict],
        image: dict,
    ) -> None:
        self.containers = deque(copy.deepcopy(containers))
        self.image = copy.deepcopy(image)
        self.calls: list[tuple[tuple[str, ...], float, int]] = []

    def __call__(
        self,
        argv: list[str],
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> bytes:
        command = tuple(argv)
        self.calls.append((command, timeout_seconds, max_output_bytes))
        if command[1:3] == ("container", "inspect"):
            payload = self.containers.popleft()
        elif command[1:3] == ("image", "inspect"):
            payload = self.image
        else:  # pragma: no cover - asserts the production argv contract
            raise AssertionError(command)
        return json.dumps([payload], separators=(",", ":")).encode("utf-8")


def _run(
    tmp_path: Path,
    *,
    first: dict | None = None,
    final: dict | None = None,
    image: dict | None = None,
    spec: HermesDockerAttestationSpec | None = None,
):
    active_spec = spec or _spec(tmp_path)
    initial = copy.deepcopy(first or _container(_home_source(active_spec)))
    last = copy.deepcopy(final or initial)
    runner = FixtureRunner([initial, last], image or _image())
    result = attest_live_hermes_docker(
        active_spec,
        runner=runner,
        now=lambda: FIXED_TIME,
    )
    return result, runner


def test_valid_fixture_is_attested_with_argv_only_and_secret_free_output(tmp_path):
    spec = _spec(tmp_path)
    result, runner = _run(tmp_path, spec=spec)

    assert result.verified is True
    assert result.reason == "verified"
    assert result.checked_at == "2026-08-11T04:05:06+00:00"
    assert result.policy_profile == "terminal-canary-v1"
    assert len(result.evidence_sha256) == 64
    assert len(result.container_id_sha256) == 64
    assert result.image_id == IMAGE_ID
    assert result.config_sha256 == spec.config_sha256
    assert [call[0] for call in runner.calls] == [
        (
            "docker.exe",
            "container",
            "inspect",
            "local-ai-workbench-hermes",
        ),
        ("docker.exe", "image", "inspect", PINNED_REFERENCE),
        (
            "docker.exe",
            "container",
            "inspect",
            "local-ai-workbench-hermes",
        ),
    ]
    serialized = json.dumps(result.public_dict(), sort_keys=True)
    assert API_KEY not in serialized
    assert API_KEY not in repr(result)
    assert CONTAINER_ID not in serialized


def test_default_runner_sets_shell_false_and_uses_argv(monkeypatch):
    captured = {}

    class FakeProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"fixture")
            self.stderr = io.BytesIO(b"")
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def kill(self):  # pragma: no cover - valid process is not killed
            self.returncode = -9

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(attestation_module.subprocess, "Popen", fake_popen)
    assert (
        attestation_module._bounded_subprocess(
            ["docker.exe", "container", "inspect", "fixed-name"], 1, 1024
        )
        == b"fixture"
    )
    assert captured["argv"] == [
        "docker.exe",
        "container",
        "inspect",
        "fixed-name",
    ]
    assert captured["kwargs"]["shell"] is False


def test_default_runner_enforces_combined_output_limit(monkeypatch):
    class FakeProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"x" * 40)
            self.stderr = io.BytesIO(b"secret" * 10)
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(
        attestation_module.subprocess,
        "Popen",
        lambda _argv, **_kwargs: FakeProcess(),
    )
    with pytest.raises(attestation_module._DockerCliFailure) as caught:
        attestation_module._bounded_subprocess(["docker.exe", "inspect"], 1, 64)
    assert caught.value.reason == "docker_output_too_large"
    assert "secret" not in str(caught.value)


def test_default_runner_enforces_timeout(monkeypatch):
    class FakeProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")
            self.returncode = None
            self.killed = False

        def wait(self, timeout=None):
            if not self.killed:
                raise subprocess.TimeoutExpired("docker.exe", timeout)
            self.returncode = -9
            return -9

        def kill(self):
            self.killed = True

    fake = FakeProcess()
    monkeypatch.setattr(
        attestation_module.subprocess,
        "Popen",
        lambda _argv, **_kwargs: fake,
    )
    with pytest.raises(attestation_module._DockerCliFailure) as caught:
        attestation_module._bounded_subprocess(
            ["docker.exe", "inspect"], 0.001, 1024
        )
    assert caught.value.reason == "docker_timeout"
    assert fake.killed is True


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"", "docker_response_invalid"),
        (b"not-json", "docker_response_invalid"),
        (b"{}", "docker_response_invalid"),
        (b"[]", "docker_response_invalid"),
        (b"[{},{}]", "docker_response_invalid"),
        (b'[{"Id":"x","Id":"y"}]', "docker_response_invalid"),
    ],
)
def test_malformed_inspect_responses_fail_closed(tmp_path, raw, reason):
    spec = _spec(tmp_path)

    def runner(_argv, _timeout, _maximum):
        return raw

    result = attest_live_hermes_docker(spec, runner=runner)
    assert result.verified is False
    assert result.reason == reason


def test_oversized_runner_output_is_rejected(tmp_path):
    spec = _spec(tmp_path, max_output_bytes=64)

    result = attest_live_hermes_docker(
        spec,
        runner=lambda _argv, _timeout, _maximum: b"x" * 65,
    )
    assert result.reason == "docker_output_too_large"


def test_runner_exception_text_and_environment_are_never_returned(tmp_path):
    spec = _spec(tmp_path)

    def runner(_argv, _timeout, _maximum):
        raise RuntimeError(f"Config.Env API_SERVER_KEY={API_KEY}")

    result = attest_live_hermes_docker(spec, runner=runner)
    serialized = json.dumps(result.public_dict())
    assert result.reason == "docker_command_failed"
    assert API_KEY not in serialized
    assert "Config.Env" not in serialized


def test_config_must_be_current_regular_reviewed_file(tmp_path):
    spec = _spec(tmp_path, config_sha256="0" * 64)
    calls = []

    result = attest_live_hermes_docker(
        spec,
        runner=lambda *args: calls.append(args) or b"[]",
    )
    assert result.reason == "config_hash_mismatch"
    assert calls == []


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (lambda c: c["State"].update(Running=False), "container_not_running"),
        (lambda c: c["State"].update(Status="exited"), "container_not_running"),
        (lambda c: c["State"].update(Pid=0), "container_not_running"),
        (lambda c: c["State"].update(Paused=True), "container_not_running"),
        (lambda c: c["State"].update(Restarting=True), "container_not_running"),
        (lambda c: c["State"].update(Dead=True), "container_not_running"),
        (lambda c: c.update(Name="/other"), "container_identity_invalid"),
        (lambda c: c.update(Image="sha256:" + "9" * 64), "container_image_mismatch"),
        (
            lambda c: c["Config"].update(Image="nousresearch/hermes-agent:latest"),
            "container_image_mismatch",
        ),
        (
            lambda c: c["Config"].update(Cmd=["sleep", "infinity"]),
            "container_command_invalid",
        ),
        (
            lambda c: c["Config"].update(Entrypoint=["/bin/sh"]),
            "container_command_invalid",
        ),
    ],
)
def test_container_identity_state_and_command_are_fail_closed(tmp_path, mutator, reason):
    spec = _spec(tmp_path)
    container = _container(_home_source(spec))
    mutator(container)
    result, _runner = _run(tmp_path, first=container, final=container, spec=spec)
    assert result.reason == reason


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (lambda image: image.update(Id="sha256:" + "9" * 64), "image_id_mismatch"),
        (lambda image: image.update(Os="windows"), "image_platform_mismatch"),
        (lambda image: image.update(Architecture="arm64"), "image_platform_mismatch"),
        (
            lambda image: image.update(
                RepoDigests=["docker.io/attacker/hermes-agent@sha256:" + "c" * 64]
            ),
            "image_digest_mismatch",
        ),
        (lambda image: image.update(RepoDigests=[]), "image_digest_mismatch"),
    ],
)
def test_pinned_image_identity_and_platform_are_required(tmp_path, mutator, reason):
    image = _image()
    mutator(image)
    result, _runner = _run(tmp_path, image=image)
    assert result.reason == reason


def test_docker_hub_repo_digest_without_default_registry_is_equivalent(tmp_path):
    image = _image()
    image["RepoDigests"] = [PINNED_REFERENCE.removeprefix("docker.io/")]

    result, _runner = _run(tmp_path, image=image)

    assert result.verified is True
    assert result.reason == "verified"


def test_docker_desktop_mount_inspect_shape_is_verified(tmp_path):
    spec = _spec(tmp_path)
    container = _container(_home_source(spec))
    container["Mounts"] = [
        mount for mount in container["Mounts"] if mount["Type"] == "bind"
    ]
    container["HostConfig"]["Binds"] = None
    container["HostConfig"]["Mounts"] = [
        {
            "Type": "bind",
            "Source": str(_home_source(spec)),
            "Target": "/opt/data",
        }
    ]

    result, _runner = _run(tmp_path, first=container, final=container, spec=spec)

    assert result.verified is True
    assert result.reason == "verified"


def test_docker_cap_prefix_inspect_shape_is_verified(tmp_path):
    container = _container(tmp_path / "home")
    container["HostConfig"]["CapAdd"] = [
        f"CAP_{item}" for item in container["HostConfig"]["CapAdd"]
    ]

    result, _runner = _run(tmp_path, first=container, final=container)

    assert result.verified is True
    assert result.reason == "verified"


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (
            lambda c: c["HostConfig"]["PortBindings"]["8642/tcp"][0].update(
                HostIp="0.0.0.0"
            ),
            "port_binding_invalid",
        ),
        (
            lambda c: c["NetworkSettings"]["Ports"]["8642/tcp"][0].update(
                HostPort="9999"
            ),
            "port_binding_invalid",
        ),
        (
            lambda c: c["HostConfig"].update(PublishAllPorts=True),
            "port_binding_invalid",
        ),
        (
            lambda c: c["HostConfig"]["PortBindings"].update(
                {"9999/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9999"}]}
            ),
            "port_binding_invalid",
        ),
        (
            lambda c: c["NetworkSettings"]["Networks"].update({"extra": {}}),
            "network_attachment_invalid",
        ),
    ],
)
def test_only_loopback_8642_and_one_bridge_network_are_allowed(tmp_path, mutator, reason):
    spec = _spec(tmp_path)
    container = _container(_home_source(spec))
    mutator(container)
    result, _runner = _run(tmp_path, first=container, final=container, spec=spec)
    assert result.reason == reason


@pytest.mark.parametrize(
    "mutator",
    [
        lambda c: c["Mounts"][0].update(Source=str(Path(c["Mounts"][0]["Source"]).parent)),
        lambda c: c["Mounts"][0].update(Destination="/workspace"),
        lambda c: c["Mounts"][0].update(RW=False),
        lambda c: c["Mounts"][0].update(Propagation="shared"),
        lambda c: c["Mounts"].append(
            {
                "Type": "bind",
                "Source": "/var/run/docker.sock",
                "Destination": "/var/run/docker.sock",
                "RW": True,
            }
        ),
        lambda c: c["HostConfig"].update(Tmpfs={"/tmp": "rw"}),
        lambda c: c["HostConfig"]["Binds"].append(
            "//./pipe/docker_engine:/var/run/docker.sock"
        ),
    ],
)
def test_mount_is_exact_and_cannot_expose_docker_socket(tmp_path, mutator):
    spec = _spec(tmp_path)
    container = _container(_home_source(spec))
    mutator(container)
    result, _runner = _run(tmp_path, first=container, final=container, spec=spec)
    assert result.reason == "mounts_invalid"


def _readonly_profile(tmp_path: Path):
    base = _spec(tmp_path)
    home = _home_source(base)
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("readonly fixture", encoding="utf-8")
    policy = tmp_path / "readonly-policy.yaml"
    policy.write_text(
        "platform_toolsets:\n  api_server: [file]\n",
        encoding="utf-8",
    )
    labels = dict(RUNTIME_LABELS)
    labels["com.local-ai-workbench.policy-profile"] = "readonly-project-v1"
    spec = _spec(
        tmp_path,
        expected_mounts=(
            HermesDockerBindMount(home, "/opt/data", read_only=False),
            HermesDockerBindMount(
                project,
                "/workspace/project",
                read_only=True,
            ),
            HermesDockerBindMount(
                policy,
                "/opt/data/config.yaml",
                read_only=True,
            ),
        ),
        config_path=policy,
        config_sha256=hashlib.sha256(policy.read_bytes()).hexdigest(),
        expected_tmpfs=dict(REQUIRED_TMPFS),
        expected_labels=labels,
        policy_profile="readonly-project-v1",
    )
    container = _container(home)
    container["Config"]["Labels"] = {**IMAGE_LABELS, **labels}
    container["Mounts"].extend(
        [
            {
                "Type": "bind",
                "Source": str(project),
                "Destination": "/workspace/project",
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
            {
                "Type": "bind",
                "Source": str(policy),
                "Destination": "/opt/data/config.yaml",
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
        ]
    )
    container["HostConfig"]["Binds"].extend(
        [
            f"{project}:/workspace/project:ro",
            f"{policy}:/opt/data/config.yaml:ro",
        ]
    )
    return spec, container


def test_readonly_profile_can_describe_project_policy_tmpfs_and_labels(tmp_path):
    spec, container = _readonly_profile(tmp_path)
    result, _runner = _run(
        tmp_path,
        first=container,
        final=container,
        spec=spec,
    )
    assert result.verified is True
    assert result.policy_profile == "readonly-project-v1"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda c: c["Mounts"][4].update(RW=True),
        lambda c: c["Mounts"][5].update(Source=c["Mounts"][4]["Source"]),
        lambda c: c["HostConfig"]["Tmpfs"].update({"/tmp": "rw"}),
        lambda c: c["Config"]["Labels"].pop(
            "com.local-ai-workbench.policy-profile"
        ),
    ],
)
def test_readonly_profile_mount_policy_tampering_is_rejected(tmp_path, mutator):
    spec, container = _readonly_profile(tmp_path)
    mutator(container)
    result, _runner = _run(
        tmp_path,
        first=container,
        final=container,
        spec=spec,
    )
    assert result.verified is False
    assert result.reason in {"mounts_invalid", "container_labels_invalid"}


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (lambda c: c["HostConfig"].update(Privileged=True), "privileged_container"),
        (lambda c: c["HostConfig"].update(CapAdd=["SYS_ADMIN"]), "capabilities_invalid"),
        (
            lambda c: c["HostConfig"].update(
                CapAdd=["DAC_OVERRIDE", "CHOWN", "FOWNER", "SETUID"]
            ),
            "capabilities_invalid",
        ),
        (
            lambda c: c["HostConfig"]["CapAdd"].append("NET_ADMIN"),
            "capabilities_invalid",
        ),
        (lambda c: c["HostConfig"].update(CapDrop=[]), "capabilities_invalid"),
        (lambda c: c["HostConfig"].update(SecurityOpt=[]), "security_options_invalid"),
        (
            lambda c: c["HostConfig"].update(
                SecurityOpt=["no-new-privileges:true", "seccomp=unconfined"]
            ),
            "security_options_invalid",
        ),
        (lambda c: c["HostConfig"].update(NetworkMode="host"), "host_namespace_forbidden"),
        (lambda c: c["HostConfig"].update(PidMode="host"), "host_namespace_forbidden"),
        (
            lambda c: c["HostConfig"].update(IpcMode="container:abc"),
            "host_namespace_forbidden",
        ),
        (
            lambda c: c["HostConfig"].update(DeviceRequests=[{"Driver": "nvidia"}]),
            "devices_forbidden",
        ),
        (
            lambda c: c["HostConfig"].update(AutoRemove=False),
            "container_lifecycle_invalid",
        ),
        (
            lambda c: c["HostConfig"].update(PidsLimit=0),
            "resource_limits_invalid",
        ),
        (
            lambda c: c["HostConfig"].update(ReadonlyRootfs=False),
            "readonly_rootfs_required",
        ),
        (
            lambda c: c["HostConfig"].update(RestartPolicy={"Name": "always"}),
            "container_lifecycle_invalid",
        ),
    ],
)
def test_security_boundary_is_fail_closed(tmp_path, mutator, reason):
    spec = _spec(tmp_path)
    container = _container(_home_source(spec))
    mutator(container)
    result, _runner = _run(tmp_path, first=container, final=container, spec=spec)
    assert result.reason == reason


@pytest.mark.parametrize(
    "mutator",
    [
        lambda c: c["Config"]["Labels"].pop("com.local-ai-workbench.component"),
        lambda c: c["Config"]["Labels"].update({"unexpected": "label"}),
        lambda c: c["Config"].update(
            Env=[item for item in c["Config"]["Env"] if not item.startswith("HERMES_SAFE_MODE=")]
        ),
        lambda c: c["Config"].update(
            Env=[
                ("API_SERVER_KEY=wrong-" + "x" * 64)
                if item.startswith("API_SERVER_KEY=")
                else item
                for item in c["Config"]["Env"]
            ]
        ),
        lambda c: c["Config"]["Env"].append("HERMES_YOLO_MODE=1"),
        lambda c: c["Config"]["Env"].append("UNREVIEWED_ENV=value"),
        lambda c: c["Config"]["Env"].append("HOME=/other"),
    ],
)
def test_labels_and_environment_must_match_exactly(tmp_path, mutator):
    spec = _spec(tmp_path)
    container = _container(_home_source(spec))
    mutator(container)
    result, _runner = _run(tmp_path, first=container, final=container, spec=spec)
    assert result.verified is False
    assert result.reason in {
        "container_labels_invalid",
        "container_environment_invalid",
    }
    assert API_KEY not in json.dumps(result.public_dict())


@pytest.mark.parametrize("changed_field", ["Id", "StartedAt"])
def test_same_name_container_replacement_during_attestation_is_rejected(
    tmp_path, changed_field
):
    spec = _spec(tmp_path)
    first = _container(_home_source(spec))
    final = copy.deepcopy(first)
    if changed_field == "Id":
        final["Id"] = "f" * 64
    else:
        final["State"]["StartedAt"] = "2026-08-11T04:04:00.000000000Z"

    result, _runner = _run(tmp_path, first=first, final=final, spec=spec)
    assert result.reason == "container_replaced_during_attestation"


@pytest.mark.parametrize(
    "updates",
    [
        {"api_server_key": "short"},
        {"expected_labels": {}},
        {"expected_tmpfs": {}},
        {
            "expected_tmpfs": {
                **REQUIRED_TMPFS,
                "/run": "rw,noexec,nosuid,size=64m",
            }
        },
        {"pinned_reference": "nousresearch/hermes-agent:latest"},
        {"image_id": "sha256:not-a-digest"},
        {"policy_profile": "../escape"},
        {"timeout_seconds": 60},
    ],
)
def test_invalid_attestation_specs_do_not_invoke_docker(tmp_path, updates):
    spec = _spec(tmp_path, **updates)
    calls = []
    result = attest_live_hermes_docker(
        spec,
        runner=lambda *args: calls.append(args) or b"[]",
    )
    assert result.reason == "spec_invalid"
    assert calls == []


def test_reason_and_evidence_are_deterministic_for_same_fixture(tmp_path):
    first, _runner = _run(tmp_path)
    second, _runner = _run(tmp_path)
    assert first.public_dict() == second.public_dict()
