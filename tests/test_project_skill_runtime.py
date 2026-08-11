from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import database
from project_skill_runtime import ProjectSkillRuntime
from project_skills import ProjectSkillNotFound, ProjectSkillScopeError


class FakeSkillStore:
    def __init__(self):
        self.skills = {
            "project_one": {
                "review": {
                    "id": "project_one:review",
                    "project_id": "project_one",
                    "slug": "review",
                    "name": "Review",
                    "description": "",
                    "version": "1.0.0",
                    "enabled": True,
                    "sha256": "digest-one",
                    "instructions": "Follow the project review checklist.",
                    "references": [
                        {
                            "path": "references/security.md",
                            "sha256": "reference-one",
                            "content": "Security review requires checking authorization boundaries.",
                        }
                    ],
                }
            },
            "project_two": {
                "review": {
                    "id": "project_two:review",
                    "project_id": "project_two",
                    "slug": "review",
                    "name": "Review",
                    "description": "",
                    "version": "2.0.0",
                    "enabled": True,
                    "sha256": "digest-two",
                    "instructions": "Use project two rules only.",
                    "references": [],
                }
            },
        }

    def list(self, project_id):
        return [
            {key: value for key, value in item.items() if key != "instructions"}
            for item in self.skills.get(project_id, {}).values()
        ]

    def get(self, project_id, slug):
        try:
            return dict(self.skills[project_id][slug])
        except KeyError as exc:
            raise ProjectSkillNotFound(slug) from exc


class ProjectSkillRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.original_db = database.DB_PATH
        database.DB_PATH = str(Path(self.temporary.name) / "workbench.db")
        database.init_db()
        for project_id in ("project_one", "project_two"):
            database.create_project(
                project_id,
                project_id,
                str(Path(self.temporary.name) / project_id),
            )
        database.create_session("session_one", project_id="project_one")
        database.create_session("session_two", project_id="project_two")
        database.create_session("session_independent")
        self.store = FakeSkillStore()
        self.runtime = ProjectSkillRuntime(self.store, database)

    def tearDown(self):
        database.DB_PATH = self.original_db
        self.temporary.cleanup()

    def test_session_activation_is_project_derived(self):
        before = self.runtime.catalog_for_session("session_one")
        self.assertTrue(before["skills"][0]["active"])

        disabled = self.runtime.set_session_state(
            "session_one",
            "review",
            mode="disabled",
            expected_sha256="digest-one",
        )
        self.assertFalse(disabled["skills"][0]["active"])

        activated = self.runtime.set_session_state(
            "session_one",
            "review",
            mode="enabled",
            expected_sha256="digest-one",
        )
        self.assertTrue(activated["skills"][0]["active"])
        self.assertEqual(activated["skills"][0]["project_id"], "project_one")

        second = self.runtime.catalog_for_session("session_two")
        self.assertEqual(second["skills"][0]["sha256"], "digest-two")
        self.assertEqual(second["skills"][0]["trigger_mode"], "project_default")

    def test_independent_session_cannot_activate_project_skill(self):
        with self.assertRaises(ProjectSkillScopeError):
            self.runtime.set_session_state(
                "session_independent",
                "review",
                mode="enabled",
            )

    def test_turn_activation_is_consumed_after_prompt_build(self):
        self.runtime.set_session_state(
            "session_one",
            "review",
            mode="enabled",
            scope="turn",
        )
        built = self.runtime.build_prompt_context(
            "session_one",
            "Please perform a security authorization review.",
            run_id="run_project_skill_test",
            consume_turn=True,
        )

        self.assertIn("Follow the project review checklist", built["context"])
        self.assertIn("authorization boundaries", built["context"])
        self.assertEqual(built["skills"][0]["trigger_mode"], "turn")
        after = self.runtime.catalog_for_session("session_one")["skills"][0]
        self.assertTrue(after["active"])
        self.assertEqual(after["trigger_mode"], "project_default")

        provenance = self.runtime.run_provenance("run_project_skill_test")
        self.assertEqual(provenance[0]["project_id"], "project_one")
        self.assertEqual(provenance[0]["skill_slug"], "review")
        self.assertEqual(provenance[0]["references"][0]["path"], "references/security.md")

    def test_updated_skill_invalidates_explicit_activation(self):
        self.runtime.set_session_state(
            "session_one",
            "review",
            mode="enabled",
            expected_sha256="digest-one",
        )
        self.store.skills["project_one"]["review"]["sha256"] = "digest-updated"
        catalog = self.runtime.catalog_for_session("session_one")
        self.assertFalse(catalog["skills"][0]["active"])
        self.assertTrue(catalog["skills"][0]["activation_stale"])

    def test_disabled_override_blocks_project_default(self):
        self.runtime.set_session_state(
            "session_two",
            "review",
            mode="disabled",
            expected_sha256="digest-two",
        )
        catalog = self.runtime.catalog_for_session("session_two")
        self.assertFalse(catalog["skills"][0]["active"])
        self.assertEqual(catalog["skills"][0]["session_override"], "disabled")

    def test_project_disabled_skill_cannot_be_session_activated(self):
        self.store.skills["project_one"]["review"]["enabled"] = False
        with self.assertRaises(ProjectSkillScopeError):
            self.runtime.set_session_state(
                "session_one",
                "review",
                mode="enabled",
                expected_sha256="digest-one",
            )

    def test_large_reference_is_ranked_and_bounded_in_prompt_context(self):
        large_reference = "security authorization checklist\n" * 1200
        self.store.skills["project_one"]["review"]["references"] = [
            {
                "path": "references/large-security.md",
                "sha256": "large-reference",
                "content": large_reference,
            }
        ]
        built = self.runtime.build_prompt_context(
            "session_one",
            "Review security authorization",
        )

        self.assertLessEqual(len(built["context"]), 32_000)
        self.assertIn("large-security.md", built["context"])
        self.assertTrue(built["skills"][0]["references"][0]["truncated"])
        self.assertTrue(built["truncated"])

    def test_multi_megabyte_reference_retrieves_a_relevant_chunk_near_the_tail(self):
        marker = "NEBULA_TAIL_ACCESS_7319"
        filler = "General handbook background without the lookup token.\n"
        target_bytes = 3 * 1024 * 1024
        repeats = (target_bytes // len(filler.encode("utf-8"))) + 1
        content = (filler * repeats) + (
            f"\n{marker}: 尾端規則要求使用紅色部署通道。\n"
        )
        self.assertGreater(len(content.encode("utf-8")), target_bytes)
        self.store.skills["project_one"]["review"]["references"] = [
            {
                "path": "references/multi-megabyte-handbook.md",
                "sha256": "multi-megabyte-reference",
                "content": content,
            }
        ]

        built = self.runtime.build_prompt_context(
            "session_one",
            f"Locate {marker}",
        )

        self.assertLessEqual(len(built["context"]), 32_000)
        self.assertIn(marker, built["context"])
        self.assertIn("尾端規則要求使用紅色部署通道", built["context"])
        self.assertTrue(built["truncated"])


if __name__ == "__main__":
    unittest.main()
