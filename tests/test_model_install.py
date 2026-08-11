import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import app as workbench_app


class FakePullResponse:
    status_code = 200
    text = ""

    def __init__(self, lines, before_second=None):
        self.lines = lines
        self.before_second = before_second
        self.closed = False

    def iter_lines(self):
        for index, line in enumerate(self.lines):
            if index == 1 and self.before_second:
                self.before_second()
            yield line

    def close(self):
        self.closed = True


class ModelInstallWorkerTests(unittest.TestCase):
    def setUp(self):
        workbench_app.models_router.reset_model_install_controls()
        self.jobs = {}

    def fake_upsert(self, job_id, model, status, progress=0, downloaded_bytes=0, total_bytes=0, message=None, error=None):
        self.jobs[job_id] = {
            "job_id": job_id,
            "model": model,
            "status": status,
            "progress": progress,
            "downloaded_bytes": downloaded_bytes,
            "total_bytes": total_bytes,
            "message": message,
            "error": error,
        }

    def fake_get(self, job_id):
        return self.jobs.get(job_id)

    def run_worker(self, response, job_id="job_test", model="test-model:latest"):
        with patch.object(workbench_app.database, "upsert_model_install_job", side_effect=self.fake_upsert), \
             patch.object(workbench_app.database, "get_model_install_job", side_effect=self.fake_get), \
             patch.object(workbench_app, "load_settings", return_value={"ollama_url": "http://127.0.0.1:11434"}), \
             patch("api.routes.models.requests.post", return_value=response):
            workbench_app.models_router.model_install_worker(job_id, model)
        return self.jobs[job_id]

    def test_completed_pull_reaches_ready_with_exact_progress(self):
        response = FakePullResponse([
            b'{"status":"pulling","completed":50,"total":100}',
            b'{"status":"success","completed":100,"total":100}',
        ])
        job = self.run_worker(response)
        self.assertEqual(job["status"], "ready")
        self.assertEqual(job["progress"], 100)
        self.assertEqual(job["downloaded_bytes"], 100)
        self.assertEqual(job["total_bytes"], 100)
        self.assertTrue(response.closed)

    def test_cancel_closes_stream_and_never_becomes_ready(self):
        job_id = "job_cancel"
        response = FakePullResponse(
            [
                b'{"status":"pulling","completed":25,"total":100}',
                b'{"status":"pulling","completed":50,"total":100}',
            ],
            before_second=lambda: workbench_app.models_router.cancel_model_install(job_id),
        )
        job = self.run_worker(response, job_id=job_id)
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["progress"], 25)
        self.assertEqual(job["downloaded_bytes"], 25)
        self.assertTrue(response.closed)
        self.assertFalse(workbench_app.models_router.has_model_install_control(job_id))


if __name__ == "__main__":
    unittest.main()
