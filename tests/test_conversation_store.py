import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import conversation_store
import database
import project_storage


class ConversationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "workbench.db"
        self.original_projects = project_storage.PROJECT_RUNTIME_DIR
        self.original_db = database.DB_PATH
        project_storage.PROJECT_RUNTIME_DIR = self.root / "projects"
        database.DB_PATH = str(self.database)
        self._create_database()

    def tearDown(self):
        project_storage.PROJECT_RUNTIME_DIR = self.original_projects
        database.DB_PATH = self.original_db
        self.temporary.cleanup()

    def _create_database(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(
                """
                CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, mode TEXT, model TEXT, project_id TEXT, message_count INTEGER, last_message_preview TEXT, created_at TEXT, updated_at TEXT);
                CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, visible_content TEXT, llm_content TEXT, sources_json TEXT, process_events_json TEXT, artifacts_json TEXT, turn_id TEXT, parent_message_id INTEGER, created_at TEXT);
                CREATE TABLE runs (id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT, model TEXT, mode TEXT, status TEXT, tasks_json TEXT, events_json TEXT, sources_json TEXT, metrics_json TEXT, artifacts_json TEXT, created_at TEXT, completed_at TEXT);
                CREATE TABLE safir_analyses (run_id TEXT PRIMARY KEY, mode TEXT, analysis_json TEXT, created_at TEXT);
                CREATE TABLE attachments (id TEXT PRIMARY KEY, session_id TEXT, filename TEXT, mime_type TEXT, storage_path TEXT, size_bytes INTEGER, width INTEGER, height INTEGER, created_at TEXT);
                CREATE TABLE artifacts (id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT, title TEXT, type TEXT, created_at TEXT, updated_at TEXT);
                CREATE TABLE artifact_files (id TEXT PRIMARY KEY, artifact_id TEXT, path TEXT, content TEXT, language TEXT);
                """
            )
            connection.execute("INSERT INTO sessions VALUES ('sess_test', '測試對話', 'chat', 'model', 'project_test', 2, '完成', '2026-01-01', '2026-01-01')")
            connection.execute("INSERT INTO messages VALUES (1, 'sess_test', 'user', '問題', '問題', '問題', '[]', '[]', '[]', NULL, NULL, '2026-01-01')")
            connection.execute("INSERT INTO messages VALUES (2, 'sess_test', 'assistant', '回答', '回答', '回答', '[]', '[]', '[]', NULL, NULL, '2026-01-01')")
            connection.execute("INSERT INTO runs VALUES ('run_test', 'sess_test', 'turn_test', 'model', 'chat', 'completed', '[]', '[]', '[]', '{}', '[]', '2026-01-01', '2026-01-01')")
            connection.commit()
        finally:
            connection.close()

    def test_export_and_archive_session(self):
        session_dir = conversation_store.export_session("sess_test", self.database)
        self.assertIsNotNone(session_dir)
        manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
        messages = (session_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(manifest["title"], "測試對話")
        self.assertEqual(session_dir, self.root / "projects" / "project_test" / "conversations" / "sess_test")
        self.assertEqual(len(messages), 2)
        turn_entries = list((session_dir / "turns").iterdir())
        self.assertEqual(len(turn_entries), 2)
        turn_dir = next(path for path in turn_entries if path.name != "unpaired")
        for expected in ("plan.json", "commentary.jsonl", "tool-events.jsonl", "validation.json", "repairs.jsonl", "final.md"):
            self.assertTrue((turn_dir / expected).is_file(), expected)
        self.assertFalse((turn_dir / "request.json").exists())
        unpaired = (session_dir / "turns" / "unpaired" / "messages.jsonl").read_text(encoding="utf-8")
        self.assertIn('"legacy_unlinked":true', unpaired)
        archived = conversation_store.archive_session("sess_test")
        self.assertTrue(archived.exists())
        self.assertFalse(session_dir.exists())
        self.assertEqual(archived.parent.parent.name, "project_test")

    def test_explicit_empty_llm_content_is_not_rehydrated_from_visible_answer(self):
        message_id = database.add_message(
            "sess_test",
            "assistant",
            "visible validation failure",
            visible_content="visible validation failure",
            llm_content="",
            turn_id="failed-turn",
            parent_message_id=1,
        )

        message = next(
            item for item in database.get_messages_by_session("sess_test")
            if item["id"] == message_id
        )

        self.assertEqual(message["content"], "visible validation failure")
        self.assertEqual(message["visible_content"], "visible validation failure")
        self.assertEqual(message["llm_content"], "")

    def test_exact_turn_ids_prevent_consecutive_user_message_shift(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DELETE FROM messages")
            connection.execute("DELETE FROM runs")
            rows = [
                (1, "user", "第一個問題", "turn_one", None),
                (2, "user", "第二個問題", "turn_two", None),
                (3, "assistant", "第二個回答", "turn_two", 2),
                (4, "user", "缺少執行紀錄的問題", "turn_three", None),
                (5, "assistant", "缺少執行紀錄的回答", "turn_three", 4),
                (6, "user", "失敗執行的問題", "turn_four", None),
                (7, "assistant", "失敗執行的回答", "turn_four", 6),
            ]
            for message_id, role, content, turn_id, parent_id in rows:
                connection.execute(
                    "INSERT INTO messages VALUES (?, 'sess_test', ?, ?, ?, ?, '[]', '[]', '[]', ?, ?, '2026-01-01')",
                    (message_id, role, content, content, content, turn_id, parent_id),
                )
            for index, turn_id in enumerate(("turn_one", "turn_two"), start=1):
                connection.execute(
                    "INSERT INTO runs VALUES (?, 'sess_test', ?, 'model', 'chat', 'completed', '[]', '[]', '[]', '{}', '[]', ?, ?)",
                    (f"run_{index}", turn_id, f"2026-01-0{index}", f"2026-01-0{index}"),
                )
            connection.execute(
                "INSERT INTO runs VALUES ('run_failed', 'sess_test', 'turn_four', 'model', 'chat', 'failed', '[]', '[]', '[]', '{}', '[]', '2026-01-04', '2026-01-04')"
            )
            connection.commit()
        finally:
            connection.close()

        session_dir = conversation_store.export_session("sess_test", self.database)
        turn_one = next((session_dir / "turns").glob("*_turn_one"))
        turn_two = next((session_dir / "turns").glob("*_turn_two"))
        turn_three = next((session_dir / "turns").glob("*_turn_three"))
        turn_four = next((session_dir / "turns").glob("*_turn_four"))
        self.assertEqual(json.loads((turn_one / "request.json").read_text(encoding="utf-8"))["content"], "第一個問題")
        self.assertFalse((turn_one / "response.md").exists())
        self.assertEqual(json.loads((turn_two / "request.json").read_text(encoding="utf-8"))["content"], "第二個問題")
        self.assertEqual((turn_two / "response.md").read_text(encoding="utf-8").strip(), "第二個回答")
        missing_run_pairing = json.loads((turn_three / "pairing.json").read_text(encoding="utf-8"))
        self.assertFalse(missing_run_pairing["run_present"])
        self.assertFalse(missing_run_pairing["complete"])
        failed_run = json.loads((turn_four / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(failed_run["status"], "failed")
        self.assertTrue(json.loads((turn_four / "pairing.json").read_text(encoding="utf-8"))["complete"])
        self.assertFalse((session_dir / "turns" / "unpaired").exists())
        message_order = [json.loads(line)["content"] for line in (session_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            message_order,
            ["第一個問題", "第二個問題", "第二個回答", "缺少執行紀錄的問題", "缺少執行紀錄的回答", "失敗執行的問題", "失敗執行的回答"],
        )

        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DELETE FROM messages WHERE turn_id = 'turn_one'")
            connection.execute("DELETE FROM runs WHERE turn_id = 'turn_one'")
            connection.commit()
        finally:
            connection.close()
        conversation_store.export_session("sess_test", self.database)
        self.assertFalse(any((session_dir / "turns").glob("*_turn_one")))


if __name__ == "__main__":
    unittest.main()
