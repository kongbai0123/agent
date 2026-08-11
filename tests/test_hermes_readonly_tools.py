from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from backend.hermes_readonly_tools import (
    READ_ONLY_TOOL_NAMES,
    HermesProjectReadOnlyTools,
    HermesProjectScopeError,
    HermesReadOnlyAccessError,
    ReadOnlyToolLimits,
    build_project_readonly_tools,
)


def _project(root: Path, **overrides: object) -> dict[str, object]:
    project: dict[str, object] = {
        "id": "project-one",
        "name": "Project One",
        "root_path": str(root),
        "path_status": "ready",
        "permission_mode": "read_only",
        "archived": False,
    }
    project.update(overrides)
    return project


def _bridge(root: Path, *, limits: ReadOnlyToolLimits | None = None) -> HermesProjectReadOnlyTools:
    root.mkdir(parents=True, exist_ok=True)
    return build_project_readonly_tools(_project(root), limits=limits)


def _make_symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable in this test environment: {exc}")


def _make_directory_link_or_skip(link: Path, target: Path) -> None:
    if os.name != "nt":
        _make_symlink_or_skip(link, target, directory=True)
        return
    environment = dict(os.environ)
    environment["WORKBENCH_TEST_JUNCTION"] = str(link)
    environment["WORKBENCH_TEST_TARGET"] = str(target)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "New-Item -ItemType Junction "
                "-Path $env:WORKBENCH_TEST_JUNCTION "
                "-Target $env:WORKBENCH_TEST_TARGET | Out-Null"
            ),
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0 or not link.exists():
        reason = (completed.stderr or completed.stdout).strip().replace("\n", " ")
        pytest.skip(
            "Windows junctions are unavailable in this test environment: "
            + reason[:200]
        )


def test_public_surface_contains_only_two_read_tools_and_fresh_schemas(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path / "project")

    first = bridge.tool_schemas()
    first[0]["name"] = "mutated"
    second = bridge.tool_schemas()

    assert READ_ONLY_TOOL_NAMES == frozenset({"read_file", "search_files"})
    assert bridge.tool_names == READ_ONLY_TOOL_NAMES
    assert {schema["name"] for schema in second} == READ_ONLY_TOOL_NAMES
    assert all(schema["parameters"]["additionalProperties"] is False for schema in second)
    assert not hasattr(bridge, "write_file")
    assert not hasattr(bridge, "patch")

    denied = bridge.invoke("write_file", {"path": "notes.txt", "content": "x"})
    assert denied["ok"] is False
    assert denied["error"]["code"] == "TOOL_NOT_ALLOWED"
    assert denied["audit"]["tool"] == "unrecognized"


def test_read_file_is_project_relative_bounded_and_audited(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text(
        "first line\nsecond line\nthird line\nfourth line\n", encoding="utf-8"
    )
    bridge = _bridge(root)

    result = bridge.read_file(
        "docs/guide.md",
        offset=2,
        limit=2,
        audit_context={
            "request_id": "request-1",
            "run_id": "run-1",
            "session_id": "session-1",
            "ignored": "not-exported",
        },
    )

    assert result == {
        **{key: value for key, value in result.items() if key == "audit"},
        "ok": True,
        "tool": "read_file",
        "path": "docs/guide.md",
        "content": "second line\nthird line",
        "offset": 2,
        "lines_returned": 2,
        "total_lines": 4,
        "truncated": True,
        "content_truncated": False,
        "next_offset": 4,
    }
    audit = result["audit"]
    assert audit["decision"] == "allow"
    assert audit["reason"] == "ok"
    assert audit["project_id"] == "project-one"
    assert audit["resource"] == "docs/guide.md"
    assert audit["mode"] == "read_only"
    assert audit["request_id"] == "request-1"
    assert audit["run_id"] == "run-1"
    assert audit["session_id"] == "session-1"
    assert "ignored" not in audit
    assert str(root) not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize(
    ("requested", "expected_code"),
    [
        ("../outside.txt", "PATH_TRAVERSAL_DENIED"),
        ("docs/../../outside.txt", "PATH_TRAVERSAL_DENIED"),
        ("/outside.txt", "ABSOLUTE_PATH_DENIED"),
        ("C:\\outside.txt", "ABSOLUTE_PATH_DENIED"),
        ("~/outside.txt", "ABSOLUTE_PATH_DENIED"),
        ("NUL.txt", "DEVICE_PATH_DENIED"),
        ("folder/COM1.log", "DEVICE_PATH_DENIED"),
    ],
)
def test_read_file_rejects_escape_absolute_and_device_paths(
    tmp_path: Path, requested: str, expected_code: str
) -> None:
    bridge = _bridge(tmp_path / "project")

    result = bridge.invoke("read_file", {"path": requested})

    assert result["ok"] is False
    assert result["error"]["code"] == expected_code
    encoded = json.dumps(result, ensure_ascii=False)
    assert requested not in encoded
    assert str(tmp_path) not in encoded
    assert len(result["audit"]["request_sha256"]) == 64


def test_direct_failures_are_typed_and_json_safe(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path / "project")

    with pytest.raises(HermesReadOnlyAccessError) as captured:
        bridge.read_file("missing.txt")

    error = captured.value
    assert error.code == "FILE_NOT_FOUND"
    assert error.to_result()["error"] == {
        "code": "FILE_NOT_FOUND",
        "message": "The requested path was not found.",
    }
    assert str(tmp_path) not in json.dumps(error.to_result(), ensure_ascii=False)


def test_secret_binary_and_non_utf8_files_are_denied(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text("API_TOKEN=must-not-leak", encoding="utf-8")
    (root / ".env.example").write_text("API_TOKEN=example", encoding="utf-8")
    (root / "binary.txt").write_bytes(b"text\x00binary")
    (root / "legacy.txt").write_bytes(b"\xff\xfe\xfd")
    (root / "picture.png").write_bytes(b"not-even-a-real-image")
    (root / "secrets.yaml").write_text("password: no", encoding="utf-8")
    bridge = _bridge(root)

    assert bridge.invoke("read_file", {"path": ".env"})["error"]["code"] == "SECRET_PATH_DENIED"
    assert bridge.invoke("read_file", {"path": "secrets.yaml"})["error"]["code"] == "SECRET_PATH_DENIED"
    assert bridge.invoke("read_file", {"path": "binary.txt"})["error"]["code"] == "BINARY_FILE_DENIED"
    assert bridge.invoke("read_file", {"path": "legacy.txt"})["error"]["code"] == "BINARY_FILE_DENIED"
    assert bridge.invoke("read_file", {"path": "picture.png"})["error"]["code"] == "BINARY_FILE_DENIED"
    assert bridge.read_file(".env.example")["content"] == "API_TOKEN=example"


def test_symlink_file_and_directory_are_never_followed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-secret", encoding="utf-8")
    if os.name != "nt":
        _make_symlink_or_skip(root / "linked-file.txt", outside / "secret.txt")
    _make_directory_link_or_skip(root / "linked-directory", outside)
    bridge = _bridge(root)

    file_result = bridge.invoke(
        "read_file", {"path": "linked-directory/secret.txt"}
    )
    directory_result = bridge.invoke(
        "search_files", {"pattern": "outside-secret", "path": "linked-directory"}
    )
    project_search = bridge.search_files("outside-secret")

    assert file_result["error"]["code"] == "PATH_LINK_DENIED"
    assert directory_result["error"]["code"] == "PATH_LINK_DENIED"
    assert project_search["matches"] == []
    assert project_search["stats"]["links_skipped"] == (2 if os.name != "nt" else 1)


def test_hardlinked_files_are_never_read_or_searched(tmp_path: Path) -> None:
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    source = outside / "shared.txt"
    source.write_text("outside-hardlink-secret", encoding="utf-8")
    linked = root / "linked.txt"
    try:
        os.link(source, linked)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable in this test environment: {exc}")
    bridge = _bridge(root)

    read_result = bridge.invoke("read_file", {"path": "linked.txt"})
    search_result = bridge.search_files("outside-hardlink-secret")

    assert read_result["error"]["code"] == "HARDLINK_DENIED"
    assert search_result["matches"] == []
    assert search_result["stats"]["hardlinks_skipped"] == 1


def test_read_limits_reject_large_files_and_bound_returned_bytes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "large.txt").write_text("x" * 65, encoding="utf-8")
    (root / "long-lines.txt").write_text(
        "abcdefghijk\nsecond-line\nthird-line", encoding="utf-8"
    )
    limits = ReadOnlyToolLimits(max_file_bytes=64, max_read_result_bytes=5)
    bridge = _bridge(root, limits=limits)

    too_large = bridge.invoke("read_file", {"path": "large.txt"})
    bounded = bridge.read_file("long-lines.txt", limit=3)

    assert too_large["error"]["code"] == "FILE_SIZE_LIMIT"
    assert bounded["content"] == "abcde"
    assert len(bounded["content"].encode("utf-8")) <= 5
    assert bounded["content_truncated"] is True
    assert bounded["lines_returned"] == 1
    assert bounded["next_offset"] == 2
    assert bounded["audit"]["bytes_returned"] == 5


def test_search_content_is_literal_scoped_paginated_and_filters_unsafe_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.txt").write_text(
        "Needle one\na.b literal\naxb must not match period query", encoding="utf-8"
    )
    (root / "src" / "b.txt").write_text("needle two", encoding="utf-8")
    (root / ".env").write_text("needle secret", encoding="utf-8")
    (root / "blob.bin").write_bytes(b"needle\x00binary")
    bridge = _bridge(root)

    first = bridge.search_files("needle", path=".", limit=1)
    second = bridge.search_files("needle", path=".", limit=1, offset=1)
    literal = bridge.search_files("a.b", path="src")

    assert first["count"] == 1
    assert first["matches"][0]["path"] == "src/a.txt"
    assert first["matches"][0]["line"] == 1
    assert first["truncated"] is True
    assert first["next_offset"] == 1
    assert second["matches"][0]["path"] == "src/b.txt"
    assert second["truncated"] is False
    assert literal["count"] == 1
    assert literal["matches"][0]["text"] == "a.b literal"
    assert first["stats"]["secret_paths_skipped"] >= 1
    assert first["stats"]["binary_files_skipped"] >= 1

    audit_text = json.dumps(first["audit"], ensure_ascii=False)
    assert "needle" not in audit_text
    assert str(root) not in audit_text
    assert len(first["audit"]["query_sha256"]) == 64


def test_search_files_target_and_glob_return_only_readable_relative_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src" / "main.py").write_text("print('hello')", encoding="utf-8")
    (root / "src" / "types.py").write_text("VALUE = 1", encoding="utf-8")
    (root / "docs" / "main.md").write_text("main docs", encoding="utf-8")
    (root / "src" / "image.png").write_bytes(b"binary")
    bridge = _bridge(root)

    result = bridge.search_files(
        "*.py", target="files", path=".", file_glob="src/*.py"
    )

    assert [item["path"] for item in result["matches"]] == [
        "src/main.py",
        "src/types.py",
    ]
    assert all(not Path(item["path"]).is_absolute() for item in result["matches"])
    assert result["stats"]["binary_files_skipped"] >= 1


def test_search_scan_and_result_limits_fail_closed_without_unbounded_output(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    for index in range(4):
        (root / f"{index}.txt").write_text(
            f"bounded-match-{index}", encoding="utf-8"
        )
    limits = ReadOnlyToolLimits(
        max_search_results=2,
        max_search_scanned_files=1,
        max_search_entries=10,
        max_search_result_bytes=1_000,
    )
    bridge = _bridge(root, limits=limits)

    result = bridge.search_files("bounded-match", limit=2)

    assert result["scan_truncated"] is True
    assert result["truncated"] is True
    assert result["count"] <= 1
    assert result["stats"]["files_seen"] <= 1
    assert result["next_offset"] == result["count"]
    assert len(json.dumps(result["matches"]).encode("utf-8")) < 200


def test_secret_search_scope_is_denied_without_echoing_the_path_or_pattern(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text("top-secret-pattern", encoding="utf-8")
    bridge = _bridge(root)

    result = bridge.invoke(
        "search_files",
        {"pattern": "top-secret-pattern", "path": ".env"},
        audit_context={"session_id": "bad id with spaces"},
    )

    encoded = json.dumps(result, ensure_ascii=False)
    assert result["error"]["code"] == "SECRET_PATH_DENIED"
    assert ".env" not in encoded
    assert "top-secret-pattern" not in encoded
    assert "session_id" not in result["audit"]


def test_project_object_and_root_are_revalidated_for_each_call(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "before.txt").write_text("before", encoding="utf-8")
    bridge = _bridge(root)

    old_root = tmp_path / "old-project"
    root.rename(old_root)
    root.mkdir()
    (root / "after.txt").write_text("after", encoding="utf-8")

    result = bridge.invoke("read_file", {"path": "after.txt"})

    assert result["ok"] is False
    assert result["error"]["code"] == "PROJECT_ROOT_CHANGED"
    assert "after" not in json.dumps(result)


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"id": "bad id"}, "PROJECT_SCOPE_INVALID"),
        ({"archived": True}, "PROJECT_SCOPE_INACTIVE"),
        ({"path_status": "missing"}, "PROJECT_ROOT_UNAVAILABLE"),
        ({"root_path": "relative/project"}, "PROJECT_ROOT_INVALID"),
    ],
)
def test_invalid_authoritative_project_scope_is_rejected(
    tmp_path: Path, overrides: dict[str, object], expected_code: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()

    with pytest.raises(HermesProjectScopeError) as captured:
        HermesProjectReadOnlyTools(_project(root, **overrides))

    assert captured.value.code == expected_code
    assert str(root) not in json.dumps(captured.value.to_result())


def test_project_root_cannot_be_a_symlink_or_filesystem_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-project"
    linked_root = tmp_path / "linked-project"
    real_root.mkdir()
    _make_directory_link_or_skip(linked_root, real_root)

    with pytest.raises(HermesProjectScopeError) as linked:
        HermesProjectReadOnlyTools(_project(linked_root))
    with pytest.raises(HermesProjectScopeError) as broad:
        HermesProjectReadOnlyTools(_project(Path(tmp_path.anchor)))

    assert linked.value.code == "PROJECT_ROOT_LINK_DENIED"
    assert broad.value.code == "PROJECT_ROOT_INVALID"


def test_invalid_limits_and_arguments_are_rejected_safely(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ReadOnlyToolLimits(max_file_bytes=17 * 1024 * 1024)
    with pytest.raises(TypeError):
        ReadOnlyToolLimits(max_read_lines=True)  # type: ignore[arg-type]

    bridge = _bridge(tmp_path / "project")
    extra = bridge.invoke("read_file", {"path": "x", "unexpected": True})
    bad_limit = bridge.invoke("search_files", {"pattern": "x", "limit": 101})
    bad_regex_assumption = bridge.invoke(
        "search_files", {"pattern": "[", "target": "content"}
    )

    assert extra["error"]["code"] == "INVALID_ARGUMENTS"
    assert bad_limit["error"]["code"] == "LIMIT_EXCEEDED"
    # Content searches are deliberately literal, so a regex metacharacter is safe.
    assert bad_regex_assumption["ok"] is True
