from __future__ import annotations

import hashlib
import inspect
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import app as workbench_app
import database
import local_session
import project_storage
from project_skills import (
    ProjectSkillConflict,
    ProjectSkillIntegrityError,
    ProjectSkillNotFound,
    ProjectSkillScopeError,
    ProjectSkillStore,
    ProjectSkillValidationError,
    ProjectSkillVersionConflict,
)


TAIL_REFERENCE_MARKER = "NEBULA_TAIL_ACCESS_7319"


def _multi_megabyte_reference() -> str:
    """Return valid UTF-8 text whose only query marker is near the file tail."""

    filler = "General handbook background without the lookup token.\n"
    target_bytes = 3 * 1024 * 1024
    repeats = (target_bytes // len(filler.encode("utf-8"))) + 1
    content = (filler * repeats) + (
        f"\n{TAIL_REFERENCE_MARKER}: 尾端規則要求使用紅色部署通道。\n"
    )
    assert len(content.encode("utf-8")) > target_bytes
    return content


class ProjectSkillIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_db = database.DB_PATH
        self.original_projects = project_storage.PROJECT_RUNTIME_DIR
        database.DB_PATH = str(self.root / "workbench.db")
        project_storage.PROJECT_RUNTIME_DIR = self.root / "projects"
        database.init_db()
        for project_id in ("project_one", "project_two"):
            database.create_project(
                project_id,
                project_id,
                str(self.root / "workspace" / project_id),
            )
        self.store = ProjectSkillStore(database, project_storage.project_dir)

    def tearDown(self):
        database.DB_PATH = self.original_db
        project_storage.PROJECT_RUNTIME_DIR = self.original_projects
        self.temporary.cleanup()

    def _create(self, project_id: str, *, instructions: str, slug: str = "review"):
        return self.store.create(
            project_id,
            slug=slug,
            name="Review",
            description="Project-specific review rules",
            instructions=instructions,
        )

    def test_same_name_is_allowed_across_projects_but_contents_stay_isolated(self):
        first = self._create("project_one", instructions="Use project one rules.")
        second = self._create("project_two", instructions="Use project two rules.")

        self.assertEqual(first["id"], "project_one:review")
        self.assertEqual(second["id"], "project_two:review")
        self.assertEqual(self.store.get("project_one", "review")["instructions"], "Use project one rules.")
        self.assertEqual(self.store.get("project_two", "review")["instructions"], "Use project two rules.")

    def test_same_project_rejects_slug_and_display_name_collisions(self):
        self._create("project_one", instructions="First.")
        with self.assertRaises(ProjectSkillConflict):
            self._create("project_one", instructions="Duplicate slug.")
        with self.assertRaises(ProjectSkillConflict):
            self.store.create(
                "project_one",
                slug="review-v2",
                name="  review  ",
                instructions="Duplicate display name.",
            )

    def test_display_name_collision_is_atomic_across_store_instances(self):
        stores = [
            ProjectSkillStore(database, project_storage.project_dir),
            ProjectSkillStore(database, project_storage.project_dir),
        ]
        barrier = threading.Barrier(2)
        outcomes = []

        def create(store, slug):
            barrier.wait()
            try:
                store.create(
                    "project_one",
                    slug=slug,
                    name="Atomic name",
                    instructions=f"Instructions for {slug}.",
                )
                outcomes.append("created")
            except ProjectSkillConflict:
                outcomes.append("conflict")

        threads = [
            threading.Thread(target=create, args=(stores[0], "atomic-one")),
            threading.Thread(target=create, args=(stores[1], "atomic-two")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(sorted(outcomes), ["conflict", "created"])
        self.assertEqual(len(self.store.list("project_one")), 1)

    def test_invalid_or_traversal_like_slugs_are_rejected(self):
        for slug in ("../other", "Review", "two words", "a/b", "-leading", "con"):
            with self.subTest(slug=slug), self.assertRaises(ProjectSkillValidationError):
                self.store.create(
                    "project_one",
                    slug=slug,
                    name="Safe name",
                    instructions="Safe instructions.",
                )

    def test_session_loader_derives_project_and_cannot_cross_load(self):
        self._create("project_one", instructions="One only.")
        self._create("project_two", instructions="Two only.")
        database.create_session("session_one", project_id="project_one")
        database.create_session("session_two", project_id="project_two")
        database.create_session("session_independent")

        first = self.store.load_for_session("session_one")
        second = self.store.load_for_session("session_two")

        self.assertEqual(first["project_id"], "project_one")
        self.assertEqual([item["instructions"] for item in first["skills"]], ["One only."])
        self.assertEqual(second["project_id"], "project_two")
        self.assertEqual([item["instructions"] for item in second["skills"]], ["Two only."])
        with self.assertRaises(ProjectSkillScopeError):
            self.store.load_for_session("session_independent")

        self.assertNotIn("project_id", inspect.signature(self.store.load_for_session).parameters)

    def test_skill_can_be_created_disabled_without_a_second_mutation(self):
        created = self.store.create(
            "project_one",
            slug="disabled-at-create",
            name="Disabled at create",
            instructions="Keep this unavailable until enabled.",
            enabled=False,
        )
        database.create_session("session_disabled_create", project_id="project_one")

        self.assertFalse(created["enabled"])
        self.assertEqual(
            self.store.load_for_session("session_disabled_create")["skills"],
            [],
        )

    def test_requested_slug_does_not_fall_back_to_another_project(self):
        self.store.create(
            "project_two",
            slug="other-only",
            name="Other only",
            instructions="Never leak this.",
        )
        database.create_session("session_one", project_id="project_one")

        with self.assertRaises(ProjectSkillNotFound):
            self.store.load_for_session("session_one", ["other-only"])

    def test_moving_a_session_switches_the_project_namespace(self):
        self._create("project_one", instructions="One only.")
        self._create("project_two", instructions="Two only.")
        database.create_session("session_move", project_id="project_one")
        self.assertEqual(
            self.store.load_for_session("session_move")["skills"][0]["instructions"],
            "One only.",
        )

        database.update_session_metadata("session_move", project_id="project_two")
        moved = self.store.load_for_session("session_move")
        self.assertEqual(moved["project_id"], "project_two")
        self.assertEqual(moved["skills"][0]["instructions"], "Two only.")

    def test_copied_skill_with_foreign_ownership_metadata_is_rejected(self):
        self._create("project_one", instructions="One only.")
        source = project_storage.project_dir("project_one") / "skills" / "review"
        destination = project_storage.project_dir("project_two") / "skills" / "review"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)

        with self.assertRaises(ProjectSkillIntegrityError):
            self.store.get("project_two", "review")

    def test_modified_instructions_fail_digest_validation(self):
        self._create("project_one", instructions="Original.")
        skill_path = project_storage.project_dir("project_one") / "skills" / "review"
        (skill_path / "SKILL.md").write_text("Modified.", encoding="utf-8")

        with self.assertRaises(ProjectSkillIntegrityError):
            self.store.get("project_one", "review")

    def test_reference_files_are_deterministic_and_list_returns_metadata_only(self):
        references = {
            "nested/schema.json": '{"kind": "example"}\r\n',
            "guide.md": "Line one\r\nLine two",
        }
        first = self.store.create(
            "project_one",
            slug="with-references",
            name="With references",
            instructions="Use the references.",
            references=references,
        )
        second = self.store.create(
            "project_two",
            slug="with-references",
            name="With references",
            instructions="Use the references.",
            references=dict(reversed(list(references.items()))),
        )

        self.assertEqual(first["sha256"], second["sha256"])
        by_path = {entry["path"]: entry for entry in first["references"]}
        self.assertEqual(by_path["guide.md"]["content"], "Line one\nLine two")
        self.assertEqual(
            by_path["nested/schema.json"]["content"],
            '{"kind": "example"}\n',
        )
        listed = self.store.list("project_one")[0]
        self.assertTrue(listed["references"])
        self.assertNotIn("content", listed["references"][0])

    def test_multi_megabyte_utf8_reference_is_stored_but_listed_as_metadata_only(self):
        content = _multi_megabyte_reference()
        created = self.store.create(
            "project_one",
            slug="large-handbook",
            name="Large handbook",
            instructions="Find only the relevant handbook passage.",
            references={"knowledge/handbook.md": content},
        )

        self.assertGreater(
            created["references"][0]["size_bytes"],
            3 * 1024 * 1024,
        )
        listed = next(
            item
            for item in self.store.list("project_one")
            if item["slug"] == "large-handbook"
        )
        self.assertEqual(listed["references"][0]["path"], "knowledge/handbook.md")
        self.assertNotIn("content", listed["references"][0])

        loaded = self.store.get("project_one", "large-handbook")
        self.assertTrue(loaded["references"][0]["content"].endswith(
            f"{TAIL_REFERENCE_MARKER}: 尾端規則要求使用紅色部署通道。\n"
        ))

    def test_reference_paths_and_types_are_fail_closed(self):
        unsafe_paths = (
            "../escape.md",
            "/absolute.md",
            "folder\\windows.md",
            "folder/../escape.md",
            "folder/./normalized.md",
            "folder//normalized.md",
            "CON.txt",
            ".hidden.md",
            "image.png",
        )
        for path in unsafe_paths:
            with self.subTest(path=path), self.assertRaises(ProjectSkillValidationError):
                self.store.create(
                    "project_one",
                    slug="unsafe-reference",
                    name="Unsafe reference",
                    instructions="Do not write files.",
                    references={path: "text"},
                )
        with self.assertRaises(ProjectSkillValidationError):
            self.store.create(
                "project_one",
                slug="case-collision",
                name="Case collision",
                instructions="Do not collide.",
                references={"Guide.md": "one", "guide.md": "two"},
            )

    def test_modified_reference_fails_complete_package_digest(self):
        self.store.create(
            "project_one",
            slug="reference-integrity",
            name="Reference integrity",
            instructions="Read the guide.",
            references={"guide.md": "Original."},
        )
        reference = (
            project_storage.project_dir("project_one")
            / "skills"
            / "reference-integrity"
            / "references"
            / "guide.md"
        )
        reference.write_text("Modified.", encoding="utf-8")

        with self.assertRaises(ProjectSkillIntegrityError):
            self.store.get("project_one", "reference-integrity")

    def test_update_uses_optimistic_digest_and_records_versions(self):
        created = self.store.create(
            "project_one",
            slug="editable",
            name="Editable",
            instructions="Version one.",
            references={"one.md": "One."},
        )
        updated = self.store.update(
            "project_one",
            "editable",
            expected_sha256=created["sha256"],
            name="Edited",
            version="2.0.0",
            instructions="Version two.",
            references={"two.md": "Two."},
        )

        self.assertNotEqual(updated["sha256"], created["sha256"])
        self.assertEqual(updated["history_count"], 2)
        self.assertEqual(updated["references"][0]["path"], "two.md")
        versions = self.store.versions("project_one", "editable")
        self.assertEqual(
            [entry["sha256"] for entry in versions],
            [updated["sha256"], created["sha256"]],
        )
        with self.assertRaises(ProjectSkillVersionConflict):
            self.store.update(
                "project_one",
                "editable",
                expected_sha256=created["sha256"],
                instructions="Stale overwrite.",
            )
        self.assertEqual(
            self.store.get("project_one", "editable")["instructions"],
            "Version two.",
        )

    def test_version_detail_preserves_an_immutable_instruction_and_reference_snapshot(self):
        created = self.store.create(
            "project_one",
            slug="snapshot-history",
            name="Snapshot history",
            version="1.0.0",
            instructions="Original immutable instructions.",
            references={"guide.md": "Original immutable reference."},
        )
        updated = self.store.update(
            "project_one",
            "snapshot-history",
            expected_sha256=created["sha256"],
            version="2.0.0",
            instructions="Current instructions.",
            references={"current.md": "Current reference."},
        )

        old = self.store.get_version(
            "project_one",
            "snapshot-history",
            created["sha256"],
        )
        self.assertEqual(old["sha256"], created["sha256"])
        self.assertEqual(old["version"], "1.0.0")
        self.assertEqual(old["instructions"], "Original immutable instructions.")
        self.assertEqual(
            {item["path"]: item["content"] for item in old["references"]},
            {"guide.md": "Original immutable reference."},
        )

        self.store.update(
            "project_one",
            "snapshot-history",
            expected_sha256=updated["sha256"],
            version="3.0.0",
            instructions="Newest instructions.",
            references={"newest.md": "Newest reference."},
        )
        old["instructions"] = "Caller mutation must not persist."
        old["references"][0]["content"] = "Caller mutation must not persist."
        reloaded = self.store.get_version(
            "project_one",
            "snapshot-history",
            created["sha256"],
        )
        self.assertEqual(reloaded["instructions"], "Original immutable instructions.")
        self.assertEqual(
            {item["path"]: item["content"] for item in reloaded["references"]},
            {"guide.md": "Original immutable reference."},
        )

    def test_legacy_instruction_digest_is_read_and_migrated_on_update(self):
        self.store.create(
            "project_one",
            slug="legacy",
            name="Legacy",
            instructions="Legacy instructions.",
        )
        skill_path = project_storage.project_dir("project_one") / "skills" / "legacy"
        metadata_path = skill_path / ".project-skill.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        instructions_bytes = (skill_path / "SKILL.md").read_bytes()
        metadata["schema_version"] = 1
        metadata["sha256"] = hashlib.sha256(instructions_bytes).hexdigest()
        metadata.pop("references")
        metadata.pop("history")
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        legacy = self.store.get("project_one", "legacy")
        self.assertEqual(legacy["sha256"], metadata["sha256"])
        migrated = self.store.update(
            "project_one",
            "legacy",
            expected_sha256=legacy["sha256"],
            instructions="Migrated instructions.",
        )
        migrated_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated_metadata["schema_version"], 2)
        self.assertEqual(migrated["history_count"], 2)

    def test_enabled_state_does_not_change_package_or_version_history(self):
        created = self._create("project_one", instructions="Only while enabled.")
        database.create_session("enabled_session", project_id="project_one")

        disabled = self.store.set_enabled(
            "project_one",
            "review",
            enabled=False,
            expected_sha256=created["sha256"],
        )
        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["sha256"], created["sha256"])
        self.assertEqual(disabled["history_count"], created["history_count"])
        self.assertEqual(self.store.load_for_session("enabled_session")["skills"], [])

    def test_rename_and_delete_release_display_name_reservations(self):
        created = self.store.create(
            "project_one",
            slug="rename-me",
            name="Original name",
            instructions="Original.",
        )
        self.store.create(
            "project_one",
            slug="occupied",
            name="Occupied name",
            instructions="Occupied.",
        )
        with self.assertRaises(ProjectSkillConflict):
            self.store.update(
                "project_one",
                "rename-me",
                expected_sha256=created["sha256"],
                name="occupied NAME",
            )

        renamed = self.store.update(
            "project_one",
            "rename-me",
            expected_sha256=created["sha256"],
            name="Renamed",
        )
        self.store.create(
            "project_one",
            slug="reuse-original",
            name="Original name",
            instructions="The old name is reusable.",
        )
        deleted = self.store.delete(
            "project_one",
            "rename-me",
            expected_sha256=renamed["sha256"],
        )
        self.assertTrue(deleted["deleted"])
        self.store.create(
            "project_one",
            slug="reuse-renamed",
            name="Renamed",
            instructions="The deleted name is reusable.",
        )

    def test_delete_only_removes_the_composite_project_identity(self):
        first = self._create("project_one", instructions="One.")
        second = self._create("project_two", instructions="Two.")

        self.store.delete(
            "project_one",
            "review",
            expected_sha256=first["sha256"],
        )
        with self.assertRaises(ProjectSkillNotFound):
            self.store.get("project_one", "review")
        self.assertEqual(self.store.get("project_two", "review")["sha256"], second["sha256"])


class ProjectSkillApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_db = database.DB_PATH
        self.original_projects = project_storage.PROJECT_RUNTIME_DIR
        database.DB_PATH = str(self.root / "workbench.db")
        project_storage.PROJECT_RUNTIME_DIR = self.root / "projects"
        database.init_db()
        database.create_project("api_one", "API one", str(self.root / "api-one"))
        database.create_project("api_two", "API two", str(self.root / "api-two"))
        database.create_session("api_session", project_id="api_one")
        self.headers = {
            "Origin": "http://127.0.0.1:8080",
            "X-Workbench-Token": local_session.session_token(),
        }

    def tearDown(self):
        database.DB_PATH = self.original_db
        project_storage.PROJECT_RUNTIME_DIR = self.original_projects
        self.temporary.cleanup()

    def test_api_uses_composite_identity_and_session_scope(self):
        with TestClient(workbench_app.app) as client:
            payload = {
                "slug": "shared-name",
                "name": "Shared name",
                "instructions": "Only API one.",
            }
            first = client.post(
                "/api/projects/api_one/skills",
                json=payload,
                headers=self.headers,
            )
            self.assertEqual(first.status_code, 201, first.text)

            payload["instructions"] = "Only API two."
            second = client.post(
                "/api/projects/api_two/skills",
                json=payload,
                headers=self.headers,
            )
            self.assertEqual(second.status_code, 201, second.text)

            duplicate = client.post(
                "/api/projects/api_one/skills",
                json=payload,
                headers=self.headers,
            )
            self.assertEqual(duplicate.status_code, 409)
            self.assertEqual(
                duplicate.json()["detail"]["code"],
                "PROJECT_SKILL_NAME_CONFLICT",
            )

            other_only = client.post(
                "/api/projects/api_two/skills",
                json={
                    "slug": "api-two-only",
                    "name": "API two only",
                    "instructions": "Never cross-load this.",
                },
                headers=self.headers,
            )
            self.assertEqual(other_only.status_code, 201, other_only.text)
            cross_project = client.get(
                "/api/projects/api_one/skills/api-two-only",
                headers=self.headers,
            )
            self.assertEqual(cross_project.status_code, 404)
            self.assertEqual(
                cross_project.json()["detail"]["code"],
                "PROJECT_SKILL_NOT_FOUND",
            )

            loaded = client.get(
                "/api/sessions/api_session/skills",
                headers=self.headers,
            )
            self.assertEqual(loaded.status_code, 200, loaded.text)
            body = loaded.json()
            self.assertEqual(body["project_id"], "api_one")
            self.assertEqual(
                [item["slug"] for item in body["skills"]],
                ["shared-name"],
            )
            self.assertTrue(body["skills"][0]["active"])
            self.assertEqual(body["skills"][0]["trigger_mode"], "project_default")
            self.assertFalse(Path(body["skills"][0]["storage_path"]).is_absolute())

    def test_api_exposes_nested_skill_lifecycle(self):
        with TestClient(workbench_app.app) as client:
            created_response = client.post(
                "/api/projects/api_one/skills",
                json={
                    "slug": "lifecycle",
                    "name": "Lifecycle",
                    "instructions": "Version one.",
                    "references": {"guide.md": "Guide one."},
                },
                headers=self.headers,
            )
            self.assertEqual(created_response.status_code, 201, created_response.text)
            created = created_response.json()["skill"]

            updated_response = client.patch(
                "/api/projects/api_one/skills/lifecycle",
                json={
                    "expected_sha256": created["sha256"],
                    "version": "2.0.0",
                    "instructions": "Version two.",
                    "enabled": False,
                },
                headers=self.headers,
            )
            self.assertEqual(updated_response.status_code, 200, updated_response.text)
            updated = updated_response.json()["skill"]
            self.assertFalse(updated["enabled"])

            stale = client.patch(
                "/api/projects/api_one/skills/lifecycle",
                json={
                    "expected_sha256": created["sha256"],
                    "instructions": "Stale.",
                },
                headers=self.headers,
            )
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertEqual(
                stale.json()["detail"]["code"],
                "PROJECT_SKILL_VERSION_CONFLICT",
            )

            disabled_response = client.patch(
                "/api/projects/api_one/skills/lifecycle/state",
                json={
                    "expected_sha256": updated["sha256"],
                    "enabled": True,
                },
                headers=self.headers,
            )
            self.assertEqual(disabled_response.status_code, 200, disabled_response.text)
            disabled = disabled_response.json()["skill"]
            self.assertTrue(disabled["enabled"])
            self.assertEqual(disabled["sha256"], updated["sha256"])

            versions_response = client.get(
                "/api/projects/api_one/skills/lifecycle/versions",
                headers=self.headers,
            )
            self.assertEqual(versions_response.status_code, 200, versions_response.text)
            self.assertEqual(len(versions_response.json()["versions"]), 2)

            wrong_project = client.get(
                "/api/projects/api_two/skills/lifecycle/versions",
                headers=self.headers,
            )
            self.assertEqual(wrong_project.status_code, 404, wrong_project.text)

            deleted_response = client.request(
                "DELETE",
                "/api/projects/api_one/skills/lifecycle",
                json={"expected_sha256": updated["sha256"]},
                headers=self.headers,
            )
            self.assertEqual(deleted_response.status_code, 200, deleted_response.text)
            self.assertTrue(deleted_response.json()["deleted"])
            missing = client.get(
                "/api/projects/api_one/skills/lifecycle",
                headers=self.headers,
            )
            self.assertEqual(missing.status_code, 404, missing.text)

    def test_api_exposes_an_immutable_historical_version_snapshot(self):
        with TestClient(workbench_app.app) as client:
            created_response = client.post(
                "/api/projects/api_one/skills",
                json={
                    "slug": "api-snapshot",
                    "name": "API snapshot",
                    "version": "1.0.0",
                    "instructions": "Original API instructions.",
                    "references": {"guide.md": "Original API reference."},
                },
                headers=self.headers,
            )
            self.assertEqual(created_response.status_code, 201, created_response.text)
            created = created_response.json()["skill"]

            updated_response = client.patch(
                "/api/projects/api_one/skills/api-snapshot",
                json={
                    "expected_sha256": created["sha256"],
                    "version": "2.0.0",
                    "instructions": "Current API instructions.",
                    "references": {"current.md": "Current API reference."},
                },
                headers=self.headers,
            )
            self.assertEqual(updated_response.status_code, 200, updated_response.text)
            updated = updated_response.json()["skill"]

            history_response = client.get(
                "/api/projects/api_one/skills/api-snapshot/versions",
                headers=self.headers,
            )
            self.assertEqual(history_response.status_code, 200, history_response.text)
            self.assertTrue(all(
                "instructions" not in item and "references" not in item
                for item in history_response.json()["versions"]
            ))

            detail_path = (
                "/api/projects/api_one/skills/api-snapshot/versions/"
                f"{created['sha256']}"
            )
            old_response = client.get(detail_path, headers=self.headers)
            self.assertEqual(old_response.status_code, 200, old_response.text)
            old = old_response.json()["version"]
            self.assertEqual(old["instructions"], "Original API instructions.")
            self.assertEqual(
                {item["path"]: item["content"] for item in old["references"]},
                {"guide.md": "Original API reference."},
            )

            newest_response = client.patch(
                "/api/projects/api_one/skills/api-snapshot",
                json={
                    "expected_sha256": updated["sha256"],
                    "version": "3.0.0",
                    "instructions": "Newest API instructions.",
                    "references": {"newest.md": "Newest API reference."},
                },
                headers=self.headers,
            )
            self.assertEqual(newest_response.status_code, 200, newest_response.text)

            reloaded_response = client.get(detail_path, headers=self.headers)
            self.assertEqual(reloaded_response.status_code, 200, reloaded_response.text)
            reloaded = reloaded_response.json()["version"]
            self.assertEqual(reloaded["instructions"], "Original API instructions.")
            self.assertEqual(
                {item["path"]: item["content"] for item in reloaded["references"]},
                {"guide.md": "Original API reference."},
            )

    def test_session_state_and_run_provenance_are_project_derived(self):
        with TestClient(workbench_app.app) as client:
            created_response = client.post(
                "/api/projects/api_one/skills",
                json={
                    "slug": "session-rules",
                    "name": "Session rules",
                    "instructions": "Apply the session release rules.",
                    "references": {"release.md": "Check the release authorization."},
                },
                headers=self.headers,
            )
            self.assertEqual(created_response.status_code, 201, created_response.text)
            created = created_response.json()["skill"]

            disabled = client.put(
                "/api/sessions/api_session/skills/session-rules",
                json={
                    "mode": "disabled",
                    "scope": "session",
                    "expected_sha256": created["sha256"],
                },
                headers=self.headers,
            )
            self.assertEqual(disabled.status_code, 200, disabled.text)
            disabled_item = next(
                item for item in disabled.json()["skills"]
                if item["slug"] == "session-rules"
            )
            self.assertFalse(disabled_item["active"])

            enabled = client.put(
                "/api/sessions/api_session/skills/session-rules",
                json={
                    "mode": "enabled",
                    "scope": "turn",
                    "expected_sha256": created["sha256"],
                },
                headers=self.headers,
            )
            self.assertEqual(enabled.status_code, 200, enabled.text)
            enabled_item = next(
                item for item in enabled.json()["skills"]
                if item["slug"] == "session-rules"
            )
            self.assertEqual(enabled_item["trigger_mode"], "turn")

            built = workbench_app.project_skill_runtime.build_prompt_context(
                "api_session",
                "Check release authorization",
                run_id="run_session_skill_provenance",
                consume_turn=True,
            )
            self.assertIn("Apply the session release rules", built["context"])
            self.assertIn("release authorization", built["context"])

            provenance = client.get(
                "/api/runs/run_session_skill_provenance/skills",
                headers=self.headers,
            )
            self.assertEqual(provenance.status_code, 200, provenance.text)
            recorded = next(
                item for item in provenance.json()["skills"]
                if item["skill_slug"] == "session-rules"
            )
            self.assertEqual(recorded["project_id"], "api_one")
            self.assertEqual(recorded["references"][0]["path"], "release.md")

            other = client.post(
                "/api/projects/api_two/skills",
                json={
                    "slug": "other-project-only",
                    "name": "Other project only",
                    "instructions": "Never cross this project boundary.",
                },
                headers=self.headers,
            )
            self.assertEqual(other.status_code, 201, other.text)
            cross_project = client.put(
                "/api/sessions/api_session/skills/other-project-only",
                json={
                    "mode": "enabled",
                    "scope": "session",
                    "expected_sha256": other.json()["skill"]["sha256"],
                },
                headers=self.headers,
            )
            self.assertEqual(cross_project.status_code, 404, cross_project.text)

            disabled_again = client.put(
                "/api/sessions/api_session/skills/session-rules",
                json={
                    "mode": "disabled",
                    "scope": "session",
                    "expected_sha256": created["sha256"],
                },
                headers=self.headers,
            )
            self.assertEqual(disabled_again.status_code, 200, disabled_again.text)
            deleted = client.request(
                "DELETE",
                "/api/projects/api_one/skills/session-rules",
                json={"expected_sha256": created["sha256"]},
                headers=self.headers,
            )
            self.assertEqual(deleted.status_code, 200, deleted.text)
            recreated = client.post(
                "/api/projects/api_one/skills",
                json={
                    "slug": "session-rules",
                    "name": "Session rules",
                    "instructions": "Apply the session release rules.",
                    "references": {"release.md": "Check the release authorization."},
                },
                headers=self.headers,
            )
            self.assertEqual(recreated.status_code, 201, recreated.text)
            refreshed = client.get(
                "/api/sessions/api_session/skills",
                headers=self.headers,
            )
            recreated_item = next(
                item for item in refreshed.json()["skills"]
                if item["slug"] == "session-rules"
            )
            self.assertEqual(recreated_item["session_override"], "inherit")


if __name__ == "__main__":
    unittest.main()
