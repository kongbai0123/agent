import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import startup_progress


class StartupProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.original_status = startup_progress.STATUS_PATH
        self.original_history = startup_progress.HISTORY_PATH
        self.original_run_id = os.environ.get("WORKBENCH_STARTUP_RUN_ID")
        startup_progress.STATUS_PATH = root / "status.json"
        startup_progress.HISTORY_PATH = root / "history.json"
        os.environ["WORKBENCH_STARTUP_RUN_ID"] = "startup_test"

    def tearDown(self):
        startup_progress.STATUS_PATH = self.original_status
        startup_progress.HISTORY_PATH = self.original_history
        if self.original_run_id is None:
            os.environ.pop("WORKBENCH_STARTUP_RUN_ID", None)
        else:
            os.environ["WORKBENCH_STARTUP_RUN_ID"] = self.original_run_id
        self.temporary.cleanup()

    def test_records_real_stages_document_counts_and_history_eta(self):
        with patch.object(startup_progress.time, "time", side_effect=[100.0, 103.0, 108.0, 110.0]):
            startup_progress.begin_startup("startup_test")
            startup_progress.update_startup("vector", "開啟向量索引", progress_percent=50)
            startup_progress.update_startup(
                "bm25", "建立 BM25 索引", progress_percent=80,
                current_documents=3, total_documents=5,
            )
            completed = startup_progress.complete_startup()

        self.assertEqual(completed["status"], "ready")
        self.assertEqual(completed["total_seconds"], 10.0)
        self.assertEqual(completed["stage_durations"]["vector"], 5.0)
        self.assertEqual(completed["stage_durations"]["bm25"], 2.0)

        with patch.object(startup_progress.time, "time", side_effect=[200.0, 204.0]):
            startup_progress.begin_startup("startup_test")
            status = startup_progress.read_startup_status()
        self.assertEqual(status["history_samples"], 1)
        self.assertEqual(status["estimated_total_seconds"], 10.0)
        self.assertEqual(status["eta_seconds"], 6.0)

    def test_imports_do_not_overwrite_status_outside_launcher(self):
        startup_progress.begin_startup("startup_test")
        os.environ.pop("WORKBENCH_STARTUP_RUN_ID", None)
        before = startup_progress.STATUS_PATH.read_text(encoding="utf-8")
        startup_progress.update_startup("unexpected", "不應寫入", progress_percent=90)
        self.assertEqual(startup_progress.STATUS_PATH.read_text(encoding="utf-8"), before)
