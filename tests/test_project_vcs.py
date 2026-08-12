from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from project_vcs import (  # noqa: E402
    MAX_DIFF_BYTES,
    ProjectVcsError,
    inspect_project_diff,
    inspect_project_vcs,
)
from structured_log import clear_registered_secrets, register_secret  # noqa: E402


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def _repository(root: Path) -> dict[str, object]:
    root.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(root)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    _git(root, "config", "user.email", "inspector@example.invalid")
    _git(root, "config", "user.name", "Inspector Test")
    (root / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "initial")
    return {
        "id": "project-one",
        "root_path": str(root),
        "path_status": "ready",
        "archived": False,
    }


def test_status_is_project_scoped_and_omits_secret_paths(tmp_path: Path) -> None:
    project = _repository(tmp_path / "repo")
    root = Path(str(project["root_path"]))
    (root / "tracked.txt").write_text("after\n", encoding="utf-8")
    (root / "new.txt").write_text("new\n", encoding="utf-8")
    (root / ".env").write_text("API_KEY=do-not-return\n", encoding="utf-8")

    vcs = inspect_project_vcs(project)

    assert vcs["available"] is True
    assert vcs["branch"] == "main"
    assert vcs["dirty"] is True
    assert {entry["path"] for entry in vcs["changes"]} == {"tracked.txt", "new.txt"}
    assert vcs["redacted_change_count"] == 1
    assert vcs["pushed_this_run"] is None
    assert "root_path" not in vcs


def test_status_omits_common_secret_configuration_names(tmp_path: Path) -> None:
    project = _repository(tmp_path / "repo")
    root = Path(str(project["root_path"]))
    for filename in (
        "secrets.yaml",
        "credentials.yml",
        "auth.toml",
        "token.json",
        "key.yaml",
    ):
        (root / filename).write_text("private\n", encoding="utf-8")
    (root / "visible.txt").write_text("public\n", encoding="utf-8")

    vcs = inspect_project_vcs(project)

    assert {entry["path"] for entry in vcs["changes"]} == {"visible.txt"}
    assert vcs["redacted_change_count"] == 5


def test_diff_is_bounded_and_redacts_secrets_and_private_absolute_paths(tmp_path: Path) -> None:
    project = _repository(tmp_path / "repo")
    root = Path(str(project["root_path"]))
    secret = "sk-abcdefghijklmnop123456"
    (root / "tracked.txt").write_text(
        f"{secret}\nC:\\Users\\private\\notes.txt\n" + ("x" * (MAX_DIFF_BYTES + 4096)),
        encoding="utf-8",
    )

    result = inspect_project_diff(project, "tracked.txt")

    assert result["path"] == "tracked.txt"
    assert result["truncated"] is True
    assert secret not in (result["diff"] or "")
    assert "C:\\Users\\private" not in (result["diff"] or "")
    assert result["redactions"] >= 2
    assert len((result["diff"] or "").encode("utf-8")) <= MAX_DIFF_BYTES


def test_diff_redacts_runtime_registered_secret_without_extra_truncation(
    tmp_path: Path,
) -> None:
    project = _repository(tmp_path / "repo")
    root = Path(str(project["root_path"]))
    secret = "runtime-literal-credential-987654321"
    register_secret(secret)
    try:
        (root / "tracked.txt").write_text(
            ("x" * 5000) + "\n" + secret + "\nend\n",
            encoding="utf-8",
        )

        result = inspect_project_diff(project, "tracked.txt")

        assert secret not in (result["diff"] or "")
        assert "[redacted]" in (result["diff"] or "")
        assert "end" in (result["diff"] or "")
        assert len(result["diff"] or "") > 4000
        assert result["redactions"] >= 1
    finally:
        clear_registered_secrets()


def test_diff_revalidates_path_against_fresh_status(tmp_path: Path) -> None:
    project = _repository(tmp_path / "repo")

    with pytest.raises(ProjectVcsError) as error:
        inspect_project_diff(project, "../outside.txt")
    assert error.value.not_found is True

    with pytest.raises(ProjectVcsError) as error:
        inspect_project_diff(project, "/tracked.txt")
    assert error.value.not_found is True

    with pytest.raises(ProjectVcsError) as error:
        inspect_project_diff(project, "tracked.txt")
    assert error.value.not_found is True


def test_nested_project_cannot_inspect_parent_repository(tmp_path: Path) -> None:
    outer = _repository(tmp_path / "repo")
    nested = Path(str(outer["root_path"])) / "nested"
    nested.mkdir()

    with pytest.raises(ProjectVcsError) as error:
        inspect_project_vcs({**outer, "root_path": str(nested)})
    assert error.value.code == "GIT_ROOT_OUTSIDE_PROJECT"


def test_diff_rejects_unresolved_untracked_file_link(tmp_path: Path) -> None:
    project = _repository(tmp_path / "repo")
    root = Path(str(project["root_path"]))
    secret = root / ".env"
    secret.write_text("UNUSUAL_VALUE=private-plain-text\n", encoding="utf-8")
    link = root / "visible.txt"
    try:
        link.symlink_to(secret)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"File symlinks are unavailable: {exc}")

    with pytest.raises(ProjectVcsError) as error:
        inspect_project_diff(project, "visible.txt")

    assert error.value.not_found is True


def test_project_root_rejects_linked_parent_component(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    project = _repository(real_parent / "repo")
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")

    with pytest.raises(ProjectVcsError) as error:
        inspect_project_vcs(
            {**project, "root_path": str(linked_parent / "repo")}
        )

    assert error.value.code == "PROJECT_ROOT_LINK_DENIED"
