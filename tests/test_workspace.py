import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from workspace import WorkspaceContext, path_status, validate_project_path


class WorkspaceIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        self.runtime = Path(self.temporary.name) / "runtime"
        self.root.mkdir()
        self.runtime.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def context(self, permission_mode="workspace_write"):
        return WorkspaceContext(
            "project_test",
            self.root,
            self.root,
            self.runtime,
            permission_mode,
            "ready",
        )

    def test_path_validation_and_status(self):
        self.assertEqual(path_status(self.root), "ready")
        self.assertEqual(path_status(self.root / "missing"), "missing")
        self.assertEqual(validate_project_path(self.root, require_existing=True), self.root.resolve())


if __name__ == "__main__":
    unittest.main()
