"""Project settings are durable state, not just labels in the sidebar.

M14 exposes project pinning and permission settings in a dedicated project
dialog.  The database already has both fields, so these tests protect their
behaviour independently of the new UI:

* pinning survives a fresh read and takes precedence over manual ordering;
* projects in the same pin group retain their manual order;
* every supported permission mode round-trips through the project PATCH API
  and the runtime project manifest.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import app as workbench_app  # noqa: E402
import database  # noqa: E402
import local_session  # noqa: E402
import paths  # noqa: E402
import workspace  # noqa: E402


def create_project(project_id: str, root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    return database.create_project(
        project_id,
        project_id,
        str(root),
        "linked",
        "read_only",
        "ready",
    )


def local_headers() -> dict[str, str]:
    return {
        "Origin": "http://127.0.0.1:8080",
        "X-Workbench-Token": local_session.session_token(),
    }


def test_folder_browser_defaults_to_local_projects_root():
    response = TestClient(workbench_app.app).post(
        "/api/projects/browse-directories",
        headers=local_headers(),
        json={"path": None},
    )

    assert response.status_code == 200
    payload = response.json()
    assert Path(payload["current_path"]) == paths.PROJECTS_ROOT
    assert payload["display_path"] == str(paths.PROJECTS_ROOT)


def test_folder_browser_explicit_path_overrides_default(tmp_path):
    requested = tmp_path / "explicit-projects"
    requested.mkdir()
    response = TestClient(workbench_app.app).post(
        "/api/projects/browse-directories",
        headers=local_headers(),
        json={"path": str(requested)},
    )

    assert response.status_code == 200
    assert Path(response.json()["current_path"]) == requested.resolve()


def configure_project_store(monkeypatch, tmp_path: Path) -> Path:
    database_path = tmp_path / "workbench.db"
    runtime_projects = tmp_path / "runtime" / "projects"
    monkeypatch.setattr(database, "DB_PATH", str(database_path))
    monkeypatch.setattr(workspace, "PROJECT_RUNTIME_DIR", runtime_projects)
    database.init_db()
    return runtime_projects


def test_project_pin_persists_and_updates(monkeypatch, tmp_path):
    configure_project_store(monkeypatch, tmp_path)
    create_project("project_one", tmp_path / "one")

    assert database.get_project("project_one")["pinned"] is False
    assert database.update_project("project_one", pinned=True)
    assert database.get_project("project_one")["pinned"] is True

    # A second database connection is used by get_project(), so this is a
    # persistence assertion rather than an in-memory object assertion.
    assert database.update_project("project_one", pinned=False)
    assert database.get_project("project_one")["pinned"] is False


def test_pinned_projects_sort_first_without_destroying_manual_order(
    monkeypatch,
    tmp_path,
):
    configure_project_store(monkeypatch, tmp_path)
    for project_id in ("project_one", "project_two", "project_three"):
        create_project(project_id, tmp_path / project_id)

    assert database.reorder_projects(
        ["project_one", "project_two", "project_three"]
    )
    assert database.update_project("project_three", pinned=True)
    assert database.update_project("project_two", pinned=True)

    # Pinned and unpinned groups each retain their explicit sort_order.
    assert [item["id"] for item in database.get_projects()] == [
        "project_two",
        "project_three",
        "project_one",
    ]

    assert database.update_project("project_three", pinned=False)
    assert [item["id"] for item in database.get_projects()] == [
        "project_two",
        "project_one",
        "project_three",
    ]


def test_patch_round_trips_pin_and_every_project_permission(
    monkeypatch,
    tmp_path,
):
    runtime_projects = configure_project_store(monkeypatch, tmp_path)
    create_project("project_api", tmp_path / "api")
    client = TestClient(workbench_app.app)

    for index, permission_mode in enumerate(
        ("read_only", "confirm_write", "workspace_write")
    ):
        response = client.patch(
            "/api/projects/project_api",
            headers=local_headers(),
            json={
                "pinned": index % 2 == 0,
                "permission_mode": permission_mode,
            },
        )
        assert response.status_code == 200, response.text
        project = response.json()["project"]
        assert project["pinned"] is (index % 2 == 0)
        assert project["permission_mode"] == permission_mode

        stored = database.get_project("project_api")
        assert stored["pinned"] is project["pinned"]
        assert stored["permission_mode"] == permission_mode
        manifest = json.loads(
            (
                runtime_projects
                / "project_api"
                / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        assert manifest["permission_mode"] == permission_mode


def test_patch_rejects_an_unknown_permission_without_changing_the_project(
    monkeypatch,
    tmp_path,
):
    configure_project_store(monkeypatch, tmp_path)
    create_project("project_api", tmp_path / "api")
    response = TestClient(workbench_app.app).patch(
        "/api/projects/project_api",
        headers=local_headers(),
        json={"permission_mode": "unrestricted"},
    )

    assert response.status_code == 422
    assert database.get_project("project_api")["permission_mode"] == "read_only"
