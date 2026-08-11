import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import database


class ProjectOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.original_db = database.DB_PATH
        database.DB_PATH = str(Path(self.temporary.name) / "workbench.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db
        self.temporary.cleanup()

    def test_project_session_lifecycle_preserves_session(self):
        project = database.create_project("project_1", "馬達控制台", str(Path(self.temporary.name) / "motor"))
        self.assertEqual(project["name"], "馬達控制台")
        self.assertTrue(project["expanded"])

        database.create_session("session_1", "監控畫面", project_id="project_1")
        sessions = database.get_all_sessions()
        self.assertEqual(sessions[0]["project_id"], "project_1")
        self.assertEqual(sessions[0]["project_name"], "馬達控制台")

        self.assertTrue(database.update_session_metadata("session_1", pinned=True, archived=True, status="waiting"))
        updated = database.get_all_sessions()[0]
        self.assertTrue(updated["pinned"])
        self.assertTrue(updated["archived"])
        self.assertEqual(updated["status"], "waiting")

        self.assertTrue(database.delete_project("project_1"))
        self.assertEqual(database.get_all_sessions(), [])

    def test_project_counts_search_and_expansion(self):
        database.create_project("project_1", "Agent 前端", str(Path(self.temporary.name) / "frontend"))
        database.create_session("session_1", "Sidebar 設計", project_id="project_1")
        database.create_session("session_2", "封存任務", project_id="project_1")
        database.update_session_metadata("session_2", archived=True)

        project = database.get_projects()[0]
        self.assertEqual(project["task_count"], 2)
        self.assertEqual(project["active_task_count"], 1)
        self.assertEqual(len(database.get_all_sessions("Agent 前端")), 2)

        self.assertTrue(database.update_project("project_1", expanded=False, archived=True))
        project = database.get_project("project_1")
        self.assertFalse(project["expanded"])
        self.assertTrue(project["archived"])

    def test_session_moves_between_projects_and_independent(self):
        database.create_project("project_1", "來源專案", str(Path(self.temporary.name) / "source"))
        database.create_project("project_2", "目標專案", str(Path(self.temporary.name) / "target"))
        database.create_session("session_independent", "既有獨立任務")
        database.create_session("session_2", "既有目標任務", project_id="project_2")
        database.create_session("session_1", "可移動任務", project_id="project_1")

        self.assertTrue(database.update_session_metadata("session_1", project_id="project_2"))
        target_sessions = [item for item in database.get_all_sessions() if item["project_id"] == "project_2"]
        self.assertEqual([item["id"] for item in target_sessions], ["session_2", "session_1"])
        moved = next(item for item in target_sessions if item["id"] == "session_1")
        self.assertEqual(moved["project_id"], "project_2")
        self.assertEqual(moved["project_name"], "目標專案")

        self.assertTrue(database.update_session_metadata("session_1", project_id=None))
        independent_sessions = [item for item in database.get_all_sessions() if item["project_id"] is None]
        self.assertEqual([item["id"] for item in independent_sessions], ["session_independent", "session_1"])
        independent = next(item for item in independent_sessions if item["id"] == "session_1")
        self.assertIsNone(independent["project_id"])
        self.assertIsNone(independent["project_name"])
        self.assertEqual(len(database.get_all_sessions()), 3)

    def test_project_and_session_order_is_persistent(self):
        database.create_project("project_1", "第一專案", str(Path(self.temporary.name) / "first"))
        database.create_project("project_2", "第二專案", str(Path(self.temporary.name) / "second"))
        self.assertTrue(database.reorder_projects(["project_2", "project_1"]))
        self.assertEqual([item["id"] for item in database.get_projects()], ["project_2", "project_1"])

        database.create_session("session_1", "第一任務", project_id="project_1")
        database.create_session("session_2", "第二任務", project_id="project_1")
        self.assertFalse(database.reorder_sessions(["session_1"], "project_1"))
        self.assertTrue(database.reorder_sessions(["session_2", "session_1"], "project_1"))
        project_sessions = [item for item in database.get_all_sessions() if item["project_id"] == "project_1"]
        self.assertEqual([item["id"] for item in project_sessions], ["session_2", "session_1"])

        self.assertTrue(database.reorder_sessions(["session_1"], None))
        moved = next(item for item in database.get_all_sessions() if item["id"] == "session_1")
        self.assertIsNone(moved["project_id"])
        self.assertFalse(database.reorder_projects(["project_1", "project_1"]))

    def test_new_projects_and_sessions_are_inserted_at_the_top(self):
        database.create_project("project_1", "較早專案", str(Path(self.temporary.name) / "older"))
        database.create_project("project_2", "最新專案", str(Path(self.temporary.name) / "newer"))
        self.assertEqual([item["id"] for item in database.get_projects()], ["project_2", "project_1"])

        database.create_session("session_1", "較早任務", project_id="project_2")
        database.create_session("session_2", "最新任務", project_id="project_2")
        project_sessions = [
            item["id"] for item in database.get_all_sessions()
            if item["project_id"] == "project_2"
        ]
        self.assertEqual(project_sessions, ["session_2", "session_1"])

        database.create_session("independent_1", "較早獨立任務")
        database.create_session("independent_2", "最新獨立任務")
        independent = [
            item["id"] for item in database.get_all_sessions()
            if item["project_id"] is None
        ]
        self.assertEqual(independent, ["independent_2", "independent_1"])

    def test_reopening_existing_session_does_not_change_manual_order(self):
        database.create_session("session_1", "第一任務")
        database.create_session("session_2", "第二任務")
        self.assertTrue(database.reorder_sessions(["session_1", "session_2"], None))
        database.create_session("session_2", "第二任務")
        independent = [
            item["id"] for item in database.get_all_sessions()
            if item["project_id"] is None
        ]
        self.assertEqual(independent, ["session_1", "session_2"])

    def test_init_db_migrates_legacy_session_table(self):
        legacy_db = Path(self.temporary.name) / "legacy.db"
        database.DB_PATH = str(legacy_db)
        conn = sqlite3.connect(legacy_db)
        try:
            conn.execute(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    root_path TEXT NOT NULL,
                    expanded INTEGER NOT NULL DEFAULT 1,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("INSERT INTO sessions (id, title, created_at) VALUES ('legacy', '舊對話', '2026-01-01')")
            conn.execute("INSERT INTO projects (id, name, root_path, created_at, updated_at) VALUES ('legacy_project', '舊專案', 'legacy', '2026-01-01', '2026-01-01')")
            conn.commit()
        finally:
            conn.close()

        database.init_db()
        migrated = database.get_all_sessions()[0]
        self.assertEqual(migrated["title"], "舊對話")
        self.assertIsNone(migrated["project_id"])
        self.assertFalse(migrated["pinned"])
        self.assertFalse(migrated["archived"])
        self.assertEqual(migrated["sort_order"], 0)
        self.assertEqual(database.get_projects()[0]["sort_order"], 0)
        migrated_project = database.get_projects()[0]
        self.assertEqual(migrated_project["root_kind"], "linked")
        self.assertEqual(migrated_project["permission_mode"], "workspace_write")
        self.assertEqual(migrated_project["path_status"], "ready")


if __name__ == "__main__":
    unittest.main()
