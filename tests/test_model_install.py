import asyncio
import os
import sys
import threading
import unittest
from unittest.mock import patch

from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import app as workbench_app
from api.schemas.models import ModelInstallRequest


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

    def test_terminal_install_event_stream_emits_progress_and_done_frames(self):
        job = {
            "job_id": "job_stream",
            "model": "qwen3.5:4b",
            "status": "ready",
            "progress": 100,
            "downloaded_bytes": 42,
            "total_bytes": 42,
            "message": "Model installed.",
        }
        endpoint = next(
            route.endpoint
            for route in workbench_app.models_router.routes
            if route.path == "/api/models/install/{job_id}/events"
        )
        with patch.object(workbench_app.database, "get_model_install_job", return_value=job):
            response = endpoint("job_stream")

            async def collect_frames():
                return [frame async for frame in response.body_iterator]

            frames = asyncio.run(collect_frames())

        text = "".join(
            frame.decode("utf-8") if isinstance(frame, bytes) else frame
            for frame in frames
        )
        self.assertIn("event: model_install_progress", text)
        self.assertIn('"model": "qwen3.5:4b"', text)
        self.assertIn("event: done", text)


class ModelInstallRequestTests(unittest.TestCase):
    def test_safe_ollama_reference_is_trimmed_and_accepted(self):
        request = ModelInstallRequest(model="  hf.co/acme/model-name:Q4_K_M  ")
        self.assertEqual(request.model, "hf.co/acme/model-name:Q4_K_M")

    def test_unsafe_or_non_model_references_are_rejected(self):
        for value in (
            "", "https://ollama.com/library/qwen3", "../model:latest",
            "owner//model:latest", "model name:latest", "model:$bad",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ModelInstallRequest(model=value)


if __name__ == "__main__":
    unittest.main()
