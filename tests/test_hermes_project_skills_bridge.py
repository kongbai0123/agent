from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import database
from hermes_project_skills_bridge import (
    MAX_HERMES_PROJECT_SKILL_INSTRUCTIONS_CHARS,
    HermesProjectSkillBridgeError,
    HermesProjectSkillsBridge,
)
from project_skill_runtime import ProjectSkillRuntime
from project_skills import ProjectSkillStore


class HermesProjectSkillsBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_db = database.DB_PATH
        database.DB_PATH = str(self.root / "workbench.db")
        database.init_db()

        for project_id in ("project_one", "project_two"):
            project_root = self.root / "linked-projects" / project_id
            project_root.mkdir(parents=True)
            database.create_project(project_id, project_id, str(project_root))
        database.create_session("session_one", project_id="project_one")
        database.create_session("session_two", project_id="project_two")
        database.create_session("session_independent")

        def isolated_project_dir(project_id: str, *, create: bool = True) -> Path:
            path = self.root / "runtime-projects" / project_id
            if create:
                path.mkdir(parents=True, exist_ok=True)
            return path

        self.store = ProjectSkillStore(
            database=database,
            project_dir_factory=isolated_project_dir,
        )
        self.large_reference = (
            "large-reference-needle authorization checklist\n" * 5_000
        )
        self.skill_one = self.store.create(
            "project_one",
            slug="review",
            name="Review",
            version="1.2.3",
            instructions="PROJECT_ONE_ONLY: apply the first project rules.",
            references={"large/security.md": self.large_reference},
        )
        self.skill_two = self.store.create(
            "project_two",
            slug="review",
            name="Review",
            version="2.4.0",
            instructions="PROJECT_TWO_ONLY: apply the second project rules.",
            references={"notes/security.md": "SECOND_REFERENCE_ONLY"},
        )
        self.store.create(
            "project_one",
            slug="disabled-skill",
            name="Disabled",
            instructions="DISABLED_CONTENT_MUST_NOT_CROSS_THE_BOUNDARY",
            enabled=False,
        )
        self.runtime = ProjectSkillRuntime(self.store, database)
        self.bridge = HermesProjectSkillsBridge(self.runtime)

    def tearDown(self) -> None:
        database.DB_PATH = self.original_db
        self.temporary.cleanup()

    def test_same_slug_is_session_scoped_without_cross_project_leakage(self) -> None:
        first = self.bridge.prepare(
            "session_one",
            "large-reference-needle authorization",
        )
        second = self.bridge.prepare("session_two", "SECOND_REFERENCE_ONLY")

        self.assertEqual(first.project_id, "project_one")
        self.assertEqual(second.project_id, "project_two")
        self.assertIn("PROJECT_ONE_ONLY", first.instructions)
        self.assertNotIn("PROJECT_TWO_ONLY", first.instructions)
        self.assertIn("PROJECT_TWO_ONLY", second.instructions)
        self.assertNotIn("PROJECT_ONE_ONLY", second.instructions)
        self.assertNotIn(
            "DISABLED_CONTENT_MUST_NOT_CROSS_THE_BOUNDARY",
            first.instructions,
        )

        self.assertEqual(first.sources[0].slug, "review")
        self.assertEqual(second.sources[0].slug, "review")
        self.assertNotEqual(first.sources[0].source_id, second.sources[0].source_id)
        self.assertNotEqual(first.sources[0].source_uri, second.sources[0].source_uri)

        # Logical provenance is exported; local project/skill directories are not.
        self.assertNotIn(str(self.root), first.instructions)
        self.assertNotIn(str(self.root), str(first.provenance))

    def test_project_is_rederived_from_current_session_database_state(self) -> None:
        before = self.bridge.prepare("session_one", "project rules")
        self.assertEqual(before.project_id, "project_one")
        self.assertIn("PROJECT_ONE_ONLY", before.instructions)

        database.update_session_metadata("session_one", project_id="project_two")
        after = self.bridge.prepare("session_one", "project rules")

        self.assertEqual(after.project_id, "project_two")
        self.assertIn("PROJECT_TWO_ONLY", after.instructions)
        self.assertNotIn("PROJECT_ONE_ONLY", after.instructions)
        with self.assertRaises(TypeError):
            self.bridge.prepare(  # type: ignore[call-arg]
                "session_one",
                "project rules",
                project_id="project_one",
            )

    def test_disabled_session_override_exports_no_skill(self) -> None:
        self.runtime.set_session_state(
            "session_one",
            "review",
            mode="disabled",
            expected_sha256=self.skill_one["sha256"],
        )

        attachment = self.bridge.prepare("session_one", "authorization")

        self.assertFalse(attachment.has_skills)
        self.assertEqual(attachment.instructions, "")
        self.assertEqual(attachment.provenance, [])
        self.assertEqual(
            attachment.as_run_kwargs("Hermes base instructions"),
            {"instructions": "Hermes base instructions"},
        )

    def test_turn_scope_is_forwarded_and_consumed_once(self) -> None:
        self.runtime.set_session_state(
            "session_one",
            "review",
            mode="enabled",
            scope="turn",
            expected_sha256=self.skill_one["sha256"],
        )

        attachment = self.bridge.prepare(
            "session_one",
            "authorization",
            consume_turn=True,
        )

        self.assertEqual(attachment.sources[0].trigger_mode, "turn")
        after = self.runtime.catalog_for_session("session_one")["skills"]
        review = next(item for item in after if item["slug"] == "review")
        self.assertEqual(review["trigger_mode"], "project_default")

    def test_large_reference_is_bounded_and_versioned_provenance_is_recorded(self) -> None:
        attachment = self.bridge.prepare(
            "session_one",
            "large-reference-needle authorization",
            run_id="workbench-run-one",
        )

        self.assertLessEqual(
            len(attachment.instructions),
            MAX_HERMES_PROJECT_SKILL_INSTRUCTIONS_CHARS,
        )
        self.assertLess(len(attachment.instructions), len(self.large_reference))
        self.assertTrue(attachment.truncated)
        source = attachment.sources[0]
        self.assertEqual(source.version, "1.2.3")
        self.assertEqual(source.sha256, self.skill_one["sha256"])
        self.assertEqual(source.trigger_mode, "project_default")
        self.assertTrue(source.references[0].truncated)
        self.assertEqual(
            source.references[0].sha256,
            self.skill_one["references"][0]["sha256"],
        )

        recorded = self.runtime.run_provenance("workbench-run-one")
        self.assertEqual(recorded[0]["project_id"], "project_one")
        self.assertEqual(recorded[0]["skill_slug"], "review")
        self.assertEqual(recorded[0]["version"], "1.2.3")
        self.assertEqual(recorded[0]["skill_sha256"], self.skill_one["sha256"])
        self.assertEqual(
            attachment.as_run_kwargs("Keep Workbench safety policy."),
            {
                "instructions": (
                    "Keep Workbench safety policy.\n\n" + attachment.instructions
                )
            },
        )

    def test_independent_session_produces_an_empty_safe_attachment(self) -> None:
        attachment = self.bridge.prepare("session_independent", "anything")

        self.assertIsNone(attachment.project_id)
        self.assertFalse(attachment.has_skills)
        self.assertEqual(attachment.as_run_kwargs(), {"instructions": None})

    def test_malformed_runtime_reference_path_fails_closed(self) -> None:
        class MalformedRuntime:
            @staticmethod
            def build_prompt_context(*_args, **_kwargs):
                return {
                    "project_id": "project_one",
                    "context": "bounded",
                    "skills": [
                        {
                            "slug": "review",
                            "version": "1.0.0",
                            "sha256": "digest",
                            "trigger_mode": "project_default",
                            "references": [
                                {
                                    "path": "C:/other-project/secret.md",
                                    "sha256": "reference-digest",
                                }
                            ],
                            "context_chars": 7,
                        }
                    ],
                }

        with self.assertRaises(HermesProjectSkillBridgeError):
            HermesProjectSkillsBridge(MalformedRuntime()).prepare(  # type: ignore[arg-type]
                "session_one",
                "anything",
            )


if __name__ == "__main__":
    unittest.main()
