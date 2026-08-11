import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import conversation_store
import database
import runtime_manager
import project_storage


class RuntimeManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_dir = self.root / "runtime" / "db"
        self.db_dir.mkdir(parents=True)
        self.db_path = self.db_dir / "workbench.db"
        self.projects = self.root / "runtime" / "projects"
        self.projects.mkdir(parents=True)
        self.originals = {
            "database_db": database.DB_PATH,
            "store_db": conversation_store.DB_PATH,
            "storage_projects": project_storage.PROJECT_RUNTIME_DIR,
            "manager_db": runtime_manager.DB_PATH,
            "manager_db_dir": runtime_manager.DB_DIR,
            "manager_projects": runtime_manager.PROJECT_RUNTIME_DIR,
            "manager_runtime": runtime_manager.RUNTIME_ROOT,
            "manager_repo": runtime_manager.REPO_ROOT,
        }
        database.DB_PATH = str(self.db_path)
        conversation_store.DB_PATH = self.db_path
        project_storage.PROJECT_RUNTIME_DIR = self.projects
        runtime_manager.DB_PATH = self.db_path
        runtime_manager.DB_DIR = self.db_dir
        runtime_manager.PROJECT_RUNTIME_DIR = self.projects
        runtime_manager.CONVERSATIONS_DIR = self.root / "runtime" / "legacy-conversations"
        runtime_manager.RUNTIME_ROOT = self.root / "runtime"
        runtime_manager.REPO_ROOT = self.root
        database.init_db()
        database.create_session("sess_runtime", "Runtime 測試", "chat", "model")
        user_id = database.add_message("sess_runtime", "user", "問題", turn_id="turn_runtime")
        database.add_message("sess_runtime", "assistant", "回答", turn_id="turn_runtime", parent_message_id=user_id)
        database.upsert_run("run_runtime", "sess_runtime", "turn_runtime", "model", "chat", "completed", completed_at="2026-01-01")
        conversation_store.export_session("sess_runtime")

    def tearDown(self):
        database.DB_PATH = self.originals["database_db"]
        conversation_store.DB_PATH = self.originals["store_db"]
        project_storage.PROJECT_RUNTIME_DIR = self.originals["storage_projects"]
        runtime_manager.DB_PATH = self.originals["manager_db"]
        runtime_manager.DB_DIR = self.originals["manager_db_dir"]
        runtime_manager.PROJECT_RUNTIME_DIR = self.originals["manager_projects"]
        runtime_manager.RUNTIME_ROOT = self.originals["manager_runtime"]
        runtime_manager.REPO_ROOT = self.originals["manager_repo"]
        self.temporary.cleanup()

    def test_health_export_and_rebuild(self):
        health = runtime_manager.runtime_health()
        self.assertTrue(health["healthy"])
        self.assertEqual(health["counts"]["sessions"], 1)
        self.assertEqual(health["counts"]["automation_runs"], 0)
        self.assertEqual(health["counts"]["pending_approvals"], 0)

        exported = runtime_manager.export_session_zip("sess_runtime")
        with zipfile.ZipFile(io.BytesIO(exported)) as archive:
            self.assertIn("sess_runtime/manifest.json", archive.namelist())
            self.assertIn("sess_runtime/messages.jsonl", archive.namelist())

        preview = runtime_manager.rebuild_index(apply=False)
        self.assertTrue(preview["valid"])
        self.assertEqual((preview["sessions"], preview["messages"], preview["runs"]), (1, 2, 1))

        applied = runtime_manager.rebuild_index(apply=True)
        self.assertTrue(applied["applied"])
        self.assertTrue(Path(applied["backup_path"]).is_file())
        self.assertTrue(runtime_manager.runtime_health()["healthy"])
        messages = database.get_messages_by_session("sess_runtime")
        self.assertEqual({message["turn_id"] for message in messages}, {"turn_runtime"})
        self.assertEqual(messages[1]["parent_message_id"], messages[0]["id"])
        reexported = conversation_store.export_session("sess_runtime")
        turn_dir = next(reexported.joinpath("turns").glob("*_turn_runtime"))
        pairing = json.loads((turn_dir / "pairing.json").read_text(encoding="utf-8"))
        self.assertTrue(pairing["complete"])
        self.assertEqual((turn_dir / "response.md").read_text(encoding="utf-8").strip(), "回答")


if __name__ == "__main__":
    unittest.main()
