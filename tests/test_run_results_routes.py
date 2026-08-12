from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from api.routes.run_results import build_run_results_router  # noqa: E402


def _error_payload(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def _repository(root: Path) -> Path:
    root.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(root)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    for key, value in (("user.email", "test@example.invalid"), ("user.name", "Test")):
        subprocess.run(
            ["git", "-C", str(root), "config", key, value],
            check=True,
            capture_output=True,
            timeout=10,
        )
    (root / "readme.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "readme.md"], check=True, timeout=10)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return root


class _Database:
    def __init__(self, root: Path) -> None:
        self.project = {
            "id": "project-one",
            "root_path": str(root),
            "path_status": "ready",
            "archived": False,
        }
        self.runs: dict[str, dict[str, Any]] = {}
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.attachments: dict[str, dict[str, Any]] = {}
        self.contexts: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {
            "session-one": {
                "id": "session-one",
                "project_id": "project-one",
            }
        }

    def get_project(self, project_id: str):
        return self.project if project_id == self.project["id"] else None

    def get_run(self, run_id: str):
        return self.runs.get(run_id)

    def get_session(self, session_id: str):
        return self.sessions.get(session_id)

    def get_artifact(self, artifact_id: str):
        return self.artifacts.get(artifact_id)

    def get_attachment(self, attachment_id: str):
        return self.attachments.get(attachment_id)

    def get_temporary_context(self, context_id: str):
        return self.contexts.get(context_id)


def _client(database: _Database) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_run_results_router(database=database, error_payload=_error_payload)
    )
    return TestClient(app)


def test_results_separate_run_evidence_from_workspace_and_never_infer_push(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    database = _Database(root)
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    database.runs["run-one"] = {
        "run_id": "run-one",
        "session_id": "session-one",
        "turn_id": "turn-one",
        "project_id": "project-one",
        "status": "completed",
        "execution_revision": 4,
        "events": [
            {
                "event": "file_change",
                "payload": {
                    "run_id": "run-one",
                    "relative_path": "src/app.py",
                    "change_type": "modified",
                    "status": "completed",
                    "additions": 4,
                    "deletions": 1,
                },
            },
            {
                "event": "file_change",
                "payload": {
                    "run_id": "run-one",
                    "relative_path": "../outside",
                    "change_type": "modified",
                },
            },
            {
                "event": "validation",
                "payload": {
                    "run_id": "run-one",
                    "name": "pytest",
                    "status": "passed",
                    "passed": 12,
                    "failed": 0,
                    "skipped": 2,
                    "summary": "12 passed",
                },
            },
            {
                "event": "git_commit",
                "payload": {
                    "run_id": "run-one",
                    "short_sha": commit[:12],
                    "status": "completed",
                    "branch": "main",
                },
            },
            {
                "event": "git_push",
                "payload": {
                    "run_id": "run-one",
                    "status": "completed",
                    "branch": "main",
                    "remote": "origin",
                },
            },
        ],
        "sources": [
            {
                "kind": "project_skill",
                "source_id": "skill:review",
                "project_id": "project-one",
                "slug": "review",
                "version": "1.0.0",
                "trigger_mode": "session",
                "references": [{"path": "references/checklist.md", "truncated": False}],
            }
        ],
        "artifacts": [],
        "input_manifest": {
            "has_temporary_context": True,
            "temporary_context_id": None,
            "inline_image_count": 2,
        },
    }

    response = _client(database).get("/api/runs/run-one/results")

    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 4
    assert body["changes"] == [
        {
            "path": "src/app.py",
            "action": "modified",
            "source": "run_event",
            "additions": 4,
            "deletions": 1,
        }
    ]
    assert body["validations"][0]["status"] == "passed"
    assert body["validations"][0]["passed_count"] == 12
    assert body["validations"][0]["failed_count"] == 0
    assert body["validations"][0]["skipped_count"] == 2
    assert body["vcs"]["run_evidence"]["committed_this_run"] is True
    assert body["vcs"]["run_evidence"]["commits"][0]["commit"] == commit[:12]
    assert body["vcs"]["run_evidence"]["pushed_this_run"] is None
    assert body["vcs"]["run_evidence"]["pushes"] == []
    assert body["vcs"]["workspace"]["pushed_this_run"] is None
    assert body["vcs"]["workspace"]["scope"] == "workspace"
    assert body["sources"][0]["kind"] == "workbench_project_skill"
    assert body["sources"][0]["project_id"] == "project-one"
    assert body["sources"][0]["slug"] == "review"
    assert body["sources"][1] == {
        "kind": "temporary_context",
        "source_id": None,
        "name": "Temporary context",
        "source": "run_input_manifest",
    }
    assert body["sources"][2] == {
        "kind": "inline_images",
        "source_id": None,
        "name": "Inline images",
        "count": 2,
        "source": "run_input_manifest",
    }
    assert body["omitted_evidence_count"] == 2


def test_legacy_run_does_not_borrow_current_session_project(tmp_path: Path) -> None:
    database = _Database(_repository(tmp_path / "repo"))
    database.sessions["session-one"]["project_id"] = None
    database.runs["legacy"] = {
        "run_id": "legacy",
        "session_id": "session-one",
        "turn_id": "turn-one",
        "project_id": None,
        "status": "completed",
        "events": [],
        "sources": [],
        "artifacts": [],
        "input_manifest": {},
    }

    body = _client(database).get("/api/runs/legacy/results").json()

    assert body["project_id"] is None
    assert body["vcs"]["workspace"]["available"] is False
    assert body["vcs"]["workspace"]["reason"] == "legacy_run_project_unknown"


def test_artifact_preview_requires_exact_run_session_and_turn_and_redacts(tmp_path: Path) -> None:
    database = _Database(_repository(tmp_path / "repo"))
    database.runs["run-one"] = {
        "run_id": "run-one",
        "session_id": "session-one",
        "turn_id": "turn-one",
        "project_id": "project-one",
        "status": "completed",
        "events": [
            {
                "event": "artifact",
                "payload": {
                    "run_id": "run-one",
                    "artifact_id": "artifact-one",
                    "title": "Report",
                    "artifact_type": "markdown",
                    "status": "completed",
                },
            }
        ],
        "sources": [],
        "artifacts": [{"artifact_id": "wrong-turn"}],
        "input_manifest": {},
    }
    database.artifacts["artifact-one"] = {
        "artifact_id": "artifact-one",
        "session_id": "session-one",
        "turn_id": "turn-one",
        "title": "Report",
        "type": "markdown",
        "files": [
            {
                "path": "report.md",
                "content": "token=ghp_abcdefghijklmnopqrstuvwxyz1234\nC:\\Users\\private\\note.txt",
                "language": "markdown",
            }
        ],
    }
    database.artifacts["wrong-turn"] = {
        "artifact_id": "wrong-turn",
        "session_id": "session-one",
        "turn_id": "another-turn",
        "title": "Wrong",
        "type": "text",
        "files": [{"path": "wrong.txt", "content": "no"}],
    }
    client = _client(database)

    results = client.get("/api/runs/run-one/results").json()
    assert [item["artifact_id"] for item in results["artifacts"]] == ["artifact-one"]

    response = client.get(
        "/api/runs/run-one/artifacts/artifact-one/preview",
        params={"path": "report.md"},
    )
    assert response.status_code == 200
    preview = response.json()
    assert "ghp_" not in preview["content"]
    assert "C:\\Users\\private" not in preview["content"]
    assert preview["render_mode"] == "text"

    cross_turn = client.get(
        "/api/runs/run-one/artifacts/wrong-turn/preview",
        params={"path": "wrong.txt"},
    )
    assert cross_turn.status_code == 404


def test_diff_route_returns_404_for_clean_or_cross_scope_path(tmp_path: Path) -> None:
    database = _Database(_repository(tmp_path / "repo"))
    client = _client(database)

    clean = client.get(
        "/api/projects/project-one/vcs/diff", params={"path": "readme.md"}
    )
    traversal = client.get(
        "/api/projects/project-one/vcs/diff", params={"path": "../outside.txt"}
    )

    assert clean.status_code == 404
    assert traversal.status_code == 404


def test_results_and_preview_fail_closed_after_session_project_move(
    tmp_path: Path,
) -> None:
    database = _Database(_repository(tmp_path / "repo"))
    database.runs["run-one"] = {
        "run_id": "run-one",
        "session_id": "session-one",
        "turn_id": "turn-one",
        "project_id": "project-one",
        "status": "completed",
        "events": [],
        "sources": [],
        "artifacts": [],
        "input_manifest": {},
    }
    database.sessions["session-one"]["project_id"] = "project-two"
    client = _client(database)

    results = client.get("/api/runs/run-one/results")
    preview = client.get(
        "/api/runs/run-one/artifacts/artifact-one/preview",
        params={"path": "report.md"},
    )

    assert results.status_code == 409
    assert results.json()["detail"]["code"] == "RUN_SCOPE_CHANGED"
    assert preview.status_code == 409
    assert preview.json()["detail"]["code"] == "RUN_SCOPE_CHANGED"
