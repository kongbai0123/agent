from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from core import settings as settings_module  # noqa: E402
from extension_catalog import mcp_configuration_payload  # noqa: E402


DIGEST = "a" * 64


def valid_server(tmp_path: Path) -> dict:
    root = tmp_path / "project"
    cwd = root / "mcp"
    return {
        "id": "local-demo",
        "label": "Local Demo",
        "transport": "stdio",
        "executable": str((tmp_path / "bin" / "demo-server.exe").resolve()),
        "expected_executable_sha256": DIGEST,
        "argv": ["--stdio", "--quiet"],
        "cwd": str(cwd.resolve()),
        "allowed_cwd_roots": [str(root.resolve())],
        "environment_keys": ["LANG", "LANG"],
        "secret_aliases": {"SERVICE_TOKEN": "vault.mcp-demo"},
        "tool_policies": {
            "lookup": {
                "access": "read",
                "risk_level": "external_read",
            },
            "update": {
                "access": "write",
                "risk_level": "external_write",
                "requires_connection": True,
                "requires_resource": True,
            },
        },
        "timeout_seconds": 45,
        "enabled": True,
    }


def test_mcp_settings_round_trip_is_normalized_and_secret_free(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("WORKBENCH_SETTINGS_PATH", str(path))
    monkeypatch.setenv("SERVICE_TOKEN", "plaintext-must-never-be-persisted")

    validated = settings_module.validate_settings(
        {"mcp_servers": [valid_server(tmp_path)]}
    )
    settings_module.save_settings(validated)
    loaded = settings_module.load_settings()
    item = loaded["mcp_servers"][0]

    assert item["id"] == "local-demo"
    assert item["transport"] == "stdio"
    assert item["environment_keys"] == ["LANG"]
    assert item["secret_aliases"] == {"SERVICE_TOKEN": "vault.mcp-demo"}
    assert item["tool_policies"]["update"] == {
        "access": "write",
        "risk_level": "external_write",
        "requires_connection": True,
        "requires_resource": True,
    }
    assert item["timeout_seconds"] == 45.0
    serialized = path.read_text(encoding="utf-8")
    assert "plaintext-must-never-be-persisted" not in serialized
    assert '"environment"' not in serialized


def test_partial_settings_update_preserves_strict_mcp_configuration(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("WORKBENCH_SETTINGS_PATH", str(tmp_path / "settings.json"))
    initial = settings_module.validate_settings(
        {"mcp_servers": [valid_server(tmp_path)]}
    )
    settings_module.save_settings(initial)

    updated = settings_module.validate_settings({"ui_language": "en-US"})
    settings_module.save_settings(updated)

    assert updated["ui_language"] == "en-US"
    assert updated["mcp_servers"] == initial["mcp_servers"]
    assert settings_module.load_settings()["mcp_servers"] == initial["mcp_servers"]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"transport": "http"}, "stdio"),
        ({"executable": "relative/server.exe"}, "absolute local path"),
        ({"executable": "https://example.test/server"}, "absolute local path"),
        ({"executable": "C:/tools/server.cmd"}, "shell or script host"),
        ({"executable": "C:/Windows/System32/cmd.exe"}, "shell or script host"),
        ({"timeout_seconds": 29}, "between 30 and 60"),
        ({"timeout_seconds": 61}, "between 30 and 60"),
        ({"timeout_seconds": True}, "must be a number"),
        ({"enabled": "true"}, "must be a boolean"),
        ({"environment_keys": ["SERVICE_TOKEN"]}, "operational allowlisted"),
        ({"secret_aliases": {"PATH": "vault.path"}}, "credential environment"),
        (
            {
                "secret_aliases": {
                    "SERVICE_TOKEN": "ghp_" + "abcdefghijklmnopqrstuvwxyz"
                }
            },
            "non-secret alias",
        ),
        (
            {
                "tool_policies": {
                    "write": {"access": "write", "risk_level": "read"}
                }
            },
            "write-class risk",
        ),
        (
            {
                "tool_policies": {
                    "lookup": {
                        "access": "read",
                        "risk_level": "read",
                        "command": "unsafe",
                    }
                }
            },
            "unknown policy fields",
        ),
        ({"command": ["python", "server.py"]}, "unknown fields"),
        ({"environment": {"API_TOKEN": "plaintext"}}, "unknown fields"),
    ],
)
def test_mcp_settings_reject_unsafe_or_unknown_fields(
    monkeypatch,
    tmp_path,
    changes,
    message,
):
    monkeypatch.setenv("WORKBENCH_SETTINGS_PATH", str(tmp_path / "settings.json"))
    item = valid_server(tmp_path)
    item.update(changes)

    with pytest.raises(ValueError, match=message):
        settings_module.validate_settings({"mcp_servers": [item]})


def test_mcp_settings_reject_scope_escape_duplicates_and_excess(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKBENCH_SETTINGS_PATH", str(tmp_path / "settings.json"))
    escaped = valid_server(tmp_path)
    escaped["cwd"] = str((tmp_path / "outside").resolve())
    with pytest.raises(ValueError, match="inside an allowed cwd root"):
        settings_module.validate_settings({"mcp_servers": [escaped]})

    first = valid_server(tmp_path)
    duplicate = copy.deepcopy(first)
    duplicate["label"] = "Duplicate"
    with pytest.raises(ValueError, match="unique safe identifiers"):
        settings_module.validate_settings({"mcp_servers": [first, duplicate]})

    excessive = []
    for index in range(17):
        item = valid_server(tmp_path)
        item["id"] = f"server-{index}"
        excessive.append(item)
    with pytest.raises(ValueError, match="at most 16"):
        settings_module.validate_settings({"mcp_servers": excessive})


def test_tampered_persisted_mcp_fails_closed_without_losing_other_settings(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("WORKBENCH_SETTINGS_PATH", str(path))
    path.write_text(
        json.dumps(
            {
                "ui_language": "en-US",
                "mcp_servers": [
                    {
                        "id": "legacy",
                        "label": "Legacy unsafe command",
                        "command": ["powershell", "-File", "server.ps1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = settings_module.load_settings()

    assert loaded["ui_language"] == "en-US"
    assert loaded["mcp_servers"] == []


def test_direct_save_revalidates_mcp_before_touching_existing_file(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("WORKBENCH_SETTINGS_PATH", str(path))
    valid = settings_module.validate_settings(
        {"mcp_servers": [valid_server(tmp_path)]}
    )
    settings_module.save_settings(valid)
    before = path.read_bytes()
    invalid = copy.deepcopy(valid)
    invalid["mcp_servers"][0]["api_key"] = "plaintext-secret"

    with pytest.raises(ValueError, match="unknown fields"):
        settings_module.save_settings(invalid)

    assert path.read_bytes() == before
    assert b"plaintext-secret" not in before


def test_extension_digest_binds_every_non_secret_mcp_runtime_field(tmp_path):
    base = settings_module.normalize_mcp_servers([valid_server(tmp_path)])[0]
    first = mcp_configuration_payload(base)
    for field, replacement in (
        ("argv", ["--different"]),
        ("allowed_cwd_roots", [str((tmp_path / "other").resolve())]),
        ("environment_keys", ["PATH"]),
        ("secret_aliases", {"SERVICE_TOKEN": "vault.changed"}),
        (
            "tool_policies",
            {"lookup": {"access": "read", "risk_level": "verify"}},
        ),
        ("timeout_seconds", 60),
    ):
        changed = copy.deepcopy(base)
        changed[field] = replacement
        assert mcp_configuration_payload(changed) != first

    changed = copy.deepcopy(base)
    changed["enabled"] = False
    assert mcp_configuration_payload(changed) == first
