from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "hermes-readonly-policy" / "sitecustomize.py"
CONFIG_PATH = ROOT / "config" / "hermes-sidecar-docker-readonly-config.yaml"
MANIFEST_PATH = ROOT / "config" / "hermes-sidecar-manifest.json"
HERMES_SOURCE = ROOT / "runtime" / "hermes" / "source"


def _load_inactive_policy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("WORKBENCH_POLICY_PROFILE", raising=False)
    monkeypatch.delenv("WORKBENCH_PROJECT_ROOT", raising=False)
    module_name = f"_workbench_readonly_policy_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, POLICY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    prior_bytecode_setting = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prior_bytecode_setting
    return module


@pytest.fixture
def policy_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_inactive_policy(monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    root = str(project.resolve())
    monkeypatch.setattr(module, "PROJECT_ROOT", root)
    monkeypatch.setenv("WORKBENCH_POLICY_PROFILE", module.POLICY_PROFILE)
    monkeypatch.setenv("WORKBENCH_PROJECT_ROOT", root)
    return module, project


def _payload(value: str) -> dict:
    parsed = json.loads(value)
    assert isinstance(parsed, dict)
    return parsed


def test_readonly_config_exposes_only_workbench_tools() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert config["toolsets"] == ["workbench-readonly"]
    assert config["platform_toolsets"] == {"api_server": ["workbench-readonly"]}
    assert config["mcp_servers"] == {}
    assert config["plugins"] == {"enabled": [], "disabled": []}

    disabled = set(config["agent"]["disabled_toolsets"])
    assert {
        "web",
        "browser",
        "terminal",
        "file",
        "code_execution",
        "skills",
        "delegation",
        "computer_use",
    }.issubset(disabled)


def test_policy_hash_and_checkout_match_pinned_hermes_release() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["release"] == {
        "package_version": "0.18.2",
        "tag": "v2026.7.7.2",
        "tag_object": "b7751df34688835a108e0d630f3495fc11f3df79",
        "source_commit": "9de9c25f620ff7f1ce0fd5457d596052d5159596",
    }
    assert manifest["readonly_tool_policy"]["tools"] == [
        "project_read_file",
        "project_search_files",
    ]
    assert manifest["readonly_tool_policy"]["python_policy"]["sha256"] == hashlib.sha256(
        POLICY_PATH.read_bytes()
    ).hexdigest()
    assert manifest["readonly_tool_policy"]["config_template"]["sha256"] == hashlib.sha256(
        CONFIG_PATH.read_bytes()
    ).hexdigest()


def test_policy_registers_exact_tools_with_pinned_hermes_runtime(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    script = r"""
import importlib.util
import json
import os
import sys

policy_path, project_root, config_path = sys.argv[1:4]
os.environ.pop("WORKBENCH_POLICY_PROFILE", None)
os.environ.pop("WORKBENCH_PROJECT_ROOT", None)
spec = importlib.util.spec_from_file_location("_policy_compat", policy_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.PROJECT_ROOT = project_root
os.environ["WORKBENCH_POLICY_PROFILE"] = module.POLICY_PROFILE
os.environ["WORKBENCH_PROJECT_ROOT"] = project_root
module._activate()

import yaml
from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS, _get_platform_tools
from model_tools import get_tool_definitions
from toolsets import resolve_toolset

config = yaml.safe_load(open(config_path, encoding="utf-8"))
enabled = _get_platform_tools(config, "api_server", include_default_mcp_servers=False)
definitions = get_tool_definitions(
    enabled_toolsets=sorted(enabled),
    disabled_toolsets=config["agent"]["disabled_toolsets"],
    quiet_mode=True,
    skip_tool_search_assembly=True,
)
print(json.dumps({
    "resolved": resolve_toolset("workbench-readonly"),
    "enabled": sorted(enabled),
    "definitions": sorted(item["function"]["name"] for item in definitions),
    "configurable_count": sum(1 for item in CONFIGURABLE_TOOLSETS if item[0] == "workbench-readonly"),
}))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(HERMES_SOURCE)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(POLICY_PATH),
            str(project.resolve()),
            str(CONFIG_PATH),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.splitlines()[-1])
    expected = ["project_read_file", "project_search_files"]
    assert result == {
        "resolved": expected,
        "enabled": ["workbench-readonly"],
        "definitions": expected,
        "configurable_count": 1,
    }


def test_read_file_is_utf8_paginated_and_bounded(policy_env) -> None:
    module, project = policy_env
    (project / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = _payload(module.project_read_file({"path": "notes.txt", "offset": 2, "limit": 1}))

    assert result == {
        "ok": True,
        "path": "notes.txt",
        "lines": [{"line": 2, "text": "beta"}],
        "next_offset": 3,
        "truncated": True,
    }


@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("../outside.txt", "path_outside_project"),
        ("folder/../../outside.txt", "path_outside_project"),
        ("/etc/passwd", "path_outside_project"),
        (r"..\\outside.txt", "path_invalid"),
        ("~/outside.txt", "path_invalid"),
        ("bad\x00name", "path_invalid"),
    ],
)
def test_read_file_rejects_path_escape_forms(policy_env, path: str, error: str) -> None:
    module, _project = policy_env
    assert _payload(module.project_read_file({"path": path})) == {"ok": False, "error": error}


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".git/config",
        ".ssh/config",
        "credentials.json",
        "nested/server.pem",
    ],
)
def test_read_file_rejects_sensitive_paths_before_io(policy_env, path: str) -> None:
    module, _project = policy_env
    assert _payload(module.project_read_file({"path": path})) == {
        "ok": False,
        "error": "sensitive_path_denied",
    }


def test_read_file_rejects_hardlink_to_file_outside_project(policy_env, tmp_path: Path) -> None:
    module, project = policy_env
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("TOP-SECRET", encoding="utf-8")
    linked = project / "innocent.txt"
    try:
        os.link(outside, linked)
    except OSError as exc:  # pragma: no cover - filesystem capability dependent
        pytest.skip(f"hard links unavailable: {exc}")
    assert linked.stat().st_nlink > 1

    assert _payload(module.project_read_file({"path": "innocent.txt"})) == {
        "ok": False,
        "error": "hardlink_path_denied",
    }


def test_search_skips_hardlinks_and_sensitive_files(policy_env, tmp_path: Path) -> None:
    module, project = policy_env
    (project / "visible.txt").write_text("Needle in project", encoding="utf-8")
    (project / ".env").write_text("Needle in secret", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("Needle outside project", encoding="utf-8")
    linked = project / "linked.txt"
    try:
        os.link(outside, linked)
    except OSError as exc:  # pragma: no cover - filesystem capability dependent
        pytest.skip(f"hard links unavailable: {exc}")

    result = _payload(
        module.project_search_files(
            {"pattern": "needle", "path": ".", "file_glob": "*.txt", "limit": 10}
        )
    )

    assert result["ok"] is True
    assert result["results"] == [
        {"path": "visible.txt", "line": 1, "text": "Needle in project"}
    ]


def test_read_file_rejects_binary_non_utf8_and_oversized_files(policy_env) -> None:
    module, project = policy_env
    (project / "binary.bin").write_bytes(b"hello\x00world")
    (project / "latin1.txt").write_bytes(b"\xff")
    (project / "large.txt").write_bytes(b"x" * (module.MAX_FILE_BYTES + 1))

    assert _payload(module.project_read_file({"path": "binary.bin"}))["error"] == "binary_file_denied"
    assert _payload(module.project_read_file({"path": "latin1.txt"}))["error"] == "non_utf8_file_denied"
    assert _payload(module.project_read_file({"path": "large.txt"}))["error"] == "file_too_large"


def test_search_is_literal_case_insensitive_and_glob_scoped(policy_env) -> None:
    module, project = policy_env
    nested = project / "nested"
    nested.mkdir()
    (nested / "one.txt").write_text("Alpha [literal]", encoding="utf-8")
    (nested / "two.md").write_text("alpha [literal]", encoding="utf-8")

    result = _payload(
        module.project_search_files(
            {"pattern": "[LITERAL]", "path": "nested", "file_glob": "*.txt", "limit": 5}
        )
    )

    assert result["results"] == [
        {"path": "nested/one.txt", "line": 1, "text": "Alpha [literal]"}
    ]


def test_policy_fails_closed_when_profile_or_root_contract_drifts(policy_env, monkeypatch) -> None:
    module, _project = policy_env
    monkeypatch.setenv("WORKBENCH_POLICY_PROFILE", "wrong-profile")
    assert _payload(module.project_search_files({"pattern": "anything"})) == {
        "ok": False,
        "error": "policy_profile_invalid",
    }

    monkeypatch.setenv("WORKBENCH_POLICY_PROFILE", module.POLICY_PROFILE)
    monkeypatch.setenv("WORKBENCH_PROJECT_ROOT", "/wrong/root")
    assert _payload(module.project_search_files({"pattern": "anything"})) == {
        "ok": False,
        "error": "project_root_invalid",
    }


def test_schemas_forbid_unreviewed_arguments() -> None:
    source = POLICY_PATH.read_text(encoding="utf-8")
    assert 'name="project_read_file"' in source
    assert 'name="project_search_files"' in source
    assert 'name="write_file"' not in source
    assert 'name="patch"' not in source
    assert source.count('"additionalProperties": False') == 2
