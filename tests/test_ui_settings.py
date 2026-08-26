import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import app as workbench_app
import local_session


class UiSettingsTests(unittest.TestCase):
    def test_modal_size_defaults_and_validation_bounds(self):
        defaults = workbench_app.load_settings()
        self.assertEqual(defaults["settings_modal_width"], 1040)
        self.assertEqual(defaults["settings_modal_height"], 760)

        minimum = workbench_app.validate_settings({
            "settings_modal_width": 100,
            "settings_modal_height": 100,
        })
        maximum = workbench_app.validate_settings({
            "settings_modal_width": 9999,
            "settings_modal_height": 9999,
        })
        self.assertEqual((minimum["settings_modal_width"], minimum["settings_modal_height"]), (760, 540))
        self.assertEqual((maximum["settings_modal_width"], maximum["settings_modal_height"]), (3840, 2160))

    def test_knowledge_and_planner_settings_are_persisted_with_safe_bounds(self):
        minimum = workbench_app.validate_settings({
            "chunk_size": 1,
            "chunk_overlap": 9999,
            "rag_k": 0,
            "rag_rerank_threshold": -1,
            "agent_max_tool_calls": 0,
            "agent_max_repair_rounds": 99,
            "agent_auto_validate": True,
        })
        self.assertEqual(minimum["chunk_size"], 128)
        self.assertEqual(minimum["chunk_overlap"], 63)
        self.assertEqual(minimum["rag_k"], 1)
        self.assertEqual(minimum["rag_rerank_threshold"], 0.0)
        self.assertEqual(minimum["agent_max_tool_calls"], 1)
        self.assertEqual(minimum["agent_max_repair_rounds"], 3)
        self.assertTrue(minimum["agent_auto_validate"])
        self.assertEqual(minimum["answer_verification_mode"], "warn")

    def test_rag_model_backends_require_matching_providers_or_existing_local_paths(self):
        providers = [
            {
                "id": "embedder",
                "provider_type": "openai_compatible",
                "label": "語意索引模型",
                "base_url": "http://127.0.0.1:9101/v1",
                "selected_model": "custom-embedding-model",
                "model_kind": "embedding",
                "enabled": True,
                "supports_tools": False,
            },
            {
                "id": "reranker",
                "provider_type": "openai_compatible",
                "label": "重新排序模型",
                "base_url": "http://127.0.0.1:9102/v1",
                "selected_model": "custom-rerank-model",
                "model_kind": "rerank",
                "enabled": False,
                "supports_tools": False,
            },
        ]
        selected = workbench_app.validate_settings({
            "model_providers": providers,
            "rag_embedding_provider_id": "EMBEDDER",
            "rag_reranker_provider_id": "reranker",
            "answer_verification_mode": "strict",
        })
        self.assertEqual(selected["rag_embedding_provider_id"], "embedder")
        self.assertEqual(selected["rag_reranker_provider_id"], "reranker")
        self.assertEqual(selected["answer_verification_mode"], "strict")

        with self.assertRaisesRegex(HTTPException, "rag_embedding_provider_id"):
            workbench_app.validate_settings({
                "model_providers": providers,
                "rag_embedding_provider_id": "reranker",
            })
        with self.assertRaisesRegex(HTTPException, "rag_reranker_provider_id"):
            workbench_app.validate_settings({
                "model_providers": providers,
                "rag_reranker_provider_id": "missing",
            })
        with self.assertRaisesRegex(HTTPException, "answer_verification_mode"):
            workbench_app.validate_settings({"answer_verification_mode": "automatic"})

        with tempfile.TemporaryDirectory() as temporary:
            embedding_path = Path(temporary) / "embedding-model"
            reranker_path = Path(temporary) / "reranker-model"
            embedding_path.mkdir()
            reranker_path.mkdir()
            local = workbench_app.validate_settings({
                "model_providers": providers,
                "rag_embedding_provider_id": "",
                "rag_reranker_provider_id": "",
                "rag_local_embedding_model_path": str(embedding_path),
                "rag_local_reranker_model_path": str(reranker_path),
            })
            self.assertEqual(
                Path(local["rag_local_embedding_model_path"]),
                embedding_path.resolve(),
            )
            self.assertEqual(
                Path(local["rag_local_reranker_model_path"]),
                reranker_path.resolve(),
            )
            with self.assertRaisesRegex(HTTPException, "只能選擇一個"):
                workbench_app.validate_settings({
                    "model_providers": providers,
                    "rag_embedding_provider_id": "embedder",
                    "rag_local_embedding_model_path": str(embedding_path),
                })
            with self.assertRaisesRegex(HTTPException, "已存在的本機路徑"):
                workbench_app.validate_settings({
                    "model_providers": providers,
                    "rag_local_embedding_model_path": str(Path(temporary) / "missing"),
                })
            model_file = Path(temporary) / "reranker-model.bin"
            model_file.write_bytes(b"fixture")
            with self.assertRaisesRegex(HTTPException, "模型資料夾"):
                workbench_app.validate_settings({
                    "model_providers": providers,
                    "rag_local_reranker_model_path": str(model_file),
                })

    def test_rag_model_backend_settings_round_trip(self):
        providers = [{
            "id": "embedder",
            "provider_type": "openai_compatible",
            "label": "語意索引模型",
            "base_url": "http://127.0.0.1:9101/v1",
            "selected_model": "custom-embedding-model",
            "model_kind": "embedding",
            "enabled": True,
            "supports_tools": False,
        }]
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = str(Path(temporary) / "settings.json")
            with patch.dict(
                os.environ,
                {"WORKBENCH_SETTINGS_PATH": settings_path},
                clear=False,
            ):
                validated = workbench_app.validate_settings({
                    "model_providers": providers,
                    "rag_embedding_provider_id": "embedder",
                    "answer_verification_mode": "off",
                })
                workbench_app.save_settings(validated)
                loaded = workbench_app.load_settings()
        self.assertEqual(loaded["rag_embedding_provider_id"], "embedder")
        self.assertEqual(loaded["rag_reranker_provider_id"], "")
        self.assertEqual(loaded["rag_local_embedding_model_path"], "")
        self.assertEqual(loaded["answer_verification_mode"], "off")

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
