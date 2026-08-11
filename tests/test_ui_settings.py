import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import app as workbench_app
import local_session


class UiSettingsTests(unittest.TestCase):
    def test_modal_size_defaults_and_validation_bounds(self):
        defaults = workbench_app.load_settings()
        self.assertEqual(defaults["settings_modal_width"], 900)
        self.assertEqual(defaults["settings_modal_height"], 650)

        minimum = workbench_app.validate_settings({
            "settings_modal_width": 100,
            "settings_modal_height": 100,
        })
        maximum = workbench_app.validate_settings({
            "settings_modal_width": 9999,
            "settings_modal_height": 9999,
        })
        self.assertEqual((minimum["settings_modal_width"], minimum["settings_modal_height"]), (620, 420))
        self.assertEqual((maximum["settings_modal_width"], maximum["settings_modal_height"]), (3840, 2160))

    def test_ui_state_endpoint_persists_without_browser_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = str(Path(temporary) / "settings.json")
            with patch.dict(
                os.environ,
                {"WORKBENCH_SETTINGS_PATH": settings_path},
                clear=False,
            ):
                response = TestClient(workbench_app.app).post(
                    "/api/settings/ui-state",
                    headers={
                        "Origin": "http://127.0.0.1:8080",
                        "X-Workbench-Token": local_session.session_token(),
                    },
                    json={
                        "settings_modal_width": 1040,
                        "settings_modal_height": 720,
                    },
                )
                result = response.json()
                loaded = workbench_app.load_settings()

        self.assertTrue(result["success"])
        self.assertEqual(loaded["settings_modal_width"], 1040)
        self.assertEqual(loaded["settings_modal_height"], 720)
        self.assertEqual(loaded["ollama_url"], "http://127.0.0.1:11434")


if __name__ == "__main__":
    unittest.main()
