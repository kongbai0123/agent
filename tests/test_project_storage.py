import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import database
import project_storage
import workspace


class ProjectStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_db = database.DB_PATH
        self.original_projects = project_storage.PROJECT_RUNTIME_DIR
        self.original_workspace_projects = workspace.PROJECT_RUNTIME_DIR
        self.original_legacy_paths = (
            project_storage.ATTACHMENTS_DIR,
            project_storage.KNOWLEDGE_DOCUMENTS_DIR,
            project_storage.SCREENSHOTS_DIR,
            project_storage.REPO_ROOT,
        )
        database_path = self.root / "runtime" / "db" / "workbench.db"
        database_path.parent.mkdir(parents=True)
        database.DB_PATH = str(database_path)
        project_storage.PROJECT_RUNTIME_DIR = self.root / "runtime" / "projects"
        workspace.PROJECT_RUNTIME_DIR = project_storage.PROJECT_RUNTIME_DIR
        project_storage.ATTACHMENTS_DIR = self.root / "runtime" / "shared-attachments"
        project_storage.KNOWLEDGE_DOCUMENTS_DIR = self.root / "runtime" / "knowledge" / "documents"
        project_storage.SCREENSHOTS_DIR = self.root / "runtime" / "screenshots"
        project_storage.REPO_ROOT = self.root
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db
        project_storage.PROJECT_RUNTIME_DIR = self.original_projects
        workspace.PROJECT_RUNTIME_DIR = self.original_workspace_projects
        (
            project_storage.ATTACHMENTS_DIR,
            project_storage.KNOWLEDGE_DOCUMENTS_DIR,
            project_storage.SCREENSHOTS_DIR,
            project_storage.REPO_ROOT,
        ) = self.original_legacy_paths
        self.temporary.cleanup()

    def test_managed_workspace_and_chat_content_share_project_boundary(self):
        managed = workspace.managed_project_path("Demo", "project_one")
        self.assertEqual(managed, project_storage.PROJECT_RUNTIME_DIR / "project_one" / "workspace")
        database.create_project("project_one", "Demo", str(managed), "managed")
        database.create_session("session_one", "Chat", project_id="project_one")

        conversation = project_storage.conversation_dir("session_one")
        attachment = project_storage.attachments_dir("session_one") / "image.png"
        imported = project_storage.imports_dir("session_one") / "notes.md"
        attachment.write_bytes(b"image")
        imported.write_text("notes", encoding="utf-8")

        boundary = project_storage.project_dir("project_one")
        self.assertTrue(attachment.is_relative_to(boundary))
        self.assertTrue(imported.is_relative_to(boundary))
        self.assertTrue(conversation.is_relative_to(boundary))

    def test_moving_chat_moves_files_and_rebases_database_paths(self):
        for project_id in ("project_one", "project_two"):
            database.create_project(project_id, project_id, str(self.root / project_id))
        database.create_session("session_move", "Move", project_id="project_one")
        source_file = project_storage.attachments_dir("session_move") / "file.txt"
        source_file.write_text("payload", encoding="utf-8")
        database.save_attachment("att_move", "session_move", "file.txt", "text/plain", str(source_file), 7, project_id="project_one")

        destination = project_storage.move_session("session_move", "project_one", "project_two")
        moved_file = destination / "attachments" / "file.txt"
        self.assertTrue(moved_file.is_file())
        self.assertFalse(source_file.exists())
        self.assertEqual(database.get_attachment("att_move")["storage_path"], str(moved_file))
        self.assertEqual(database.get_attachment("att_move")["project_id"], "project_two")

    def test_delete_project_cascades_chat_metadata(self):
        database.create_project("project_delete", "Delete", str(self.root / "delete"))
        database.create_session("session_delete", "Delete chat", project_id="project_delete")
        database.add_message("session_delete", "user", "content")
        self.assertTrue(database.delete_project("project_delete"))
        self.assertIsNone(database.get_project("project_delete"))
        self.assertIsNone(database.get_session("session_delete"))

    def test_legacy_sources_migrate_into_owning_project(self):
        database.create_project("project_legacy", "Legacy", str(self.root / "legacy"))
        database.create_session("session_legacy", "Legacy chat", project_id="project_legacy")
        legacy_chat = project_storage.PROJECT_RUNTIME_DIR.parent / "conversations" / "session_legacy"
        legacy_chat.mkdir(parents=True)
        (legacy_chat / "manifest.json").write_text("{}", encoding="utf-8")

        legacy_attachment = self.root / "shared-attachments" / "image.png"
        legacy_attachment.parent.mkdir()
        legacy_attachment.write_bytes(b"image")
        database.save_attachment("att_legacy", "session_legacy", "image.png", "image/png", str(legacy_attachment), 5)

        legacy_document = self.root / "knowledge" / "document.txt"
        legacy_document.parent.mkdir()
        legacy_document.write_text("document", encoding="utf-8")
        database.upsert_document("doc_legacy", "document.txt", str(legacy_document), "indexed", session_id="session_legacy", project_id="project_legacy")

        report = project_storage.migrate_legacy_storage()
        destination = project_storage.conversation_dir("session_legacy")
        self.assertEqual(report["errors"], [])
        self.assertTrue((destination / "manifest.json").is_file())
        self.assertTrue((destination / "attachments" / "image.png").is_file())
        self.assertTrue((destination / "imports" / "document.txt").is_file())
        self.assertFalse(legacy_chat.exists())
        self.assertTrue(database.get_attachment("att_legacy")["storage_path"].startswith(str(destination)))


if __name__ == "__main__":
    unittest.main()
