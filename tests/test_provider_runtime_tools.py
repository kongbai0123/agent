from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import provider_tools


class GovernanceStub:
    def credential_metadata(self, _provider_id):
        return {"last_verified_at": "2026-01-01T00:00:00+00:00"}

    def state(self, _provider_id, **_kwargs):
        return {"state": "healthy"}


def test_specialized_provider_tools_are_narrow_and_namespaced(monkeypatch):
    monkeypatch.setattr(provider_tools, "require_provider_enabled", lambda *_args, **_kwargs: None)
    settings = {
        "model_providers": [
            {"id": "ocr", "provider_type": "nvidia", "enabled": True, "selected_model": "nvidia/nemotron-ocr-v2", "model_kind": "vision"},
            {"id": "rank", "provider_type": "nvidia", "enabled": True, "selected_model": "nvidia/llama-nemotron-rerank-1b-v2", "model_kind": "rerank"},
            {"id": "embed", "provider_type": "nvidia", "enabled": True, "selected_model": "nvidia/llama-nemotron-embed-1b-v2", "model_kind": "embedding"},
            {"id": "translate", "provider_type": "openai_compatible", "enabled": True, "selected_model": "riva-translate", "model_kind": "translation", "language_pair": "en-zh-tw"},
        ]
    }
    definitions = provider_tools.runtime_tool_definitions(
        settings,
        project_id="p1",
        manifest_digest=lambda _extension_id: "a" * 64,
        governance=GovernanceStub(),
    )
    assert {item.name for item in definitions} == {
        "provider.ocr_image",
        "provider.rerank_passages",
        "provider.semantic_match",
        "provider.translate_text",
    }
    assert all(item.access.value == "read" for item in definitions)
    assert all(item.max_result_bytes == 16 * 1024 for item in definitions)
    ocr = next(item for item in definitions if item.name == "provider.ocr_image")
    assert ocr.input_schema["required"] == ["attachment_id"]


def test_nvidia_specialized_endpoints_are_not_chat_endpoints():
    provider = {"provider_type": "nvidia", "base_url": "https://integrate.api.nvidia.com/v1"}
    assert provider_tools._ranking_endpoint(provider, "nvidia/llama-nemotron-rerank-1b-v2").endswith("/llama-nemotron-rerank-1b-v2/reranking")
    assert provider_tools._embedding_endpoint(provider, "nvidia/llama-nemotron-embed-1b-v2").endswith("/llama-nemotron-embed-1b-v2/embeddings")


def test_unverified_specialized_model_is_not_exposed(monkeypatch):
    monkeypatch.setattr(provider_tools, "require_provider_enabled", lambda *_args, **_kwargs: None)

    class UnverifiedGovernance(GovernanceStub):
        def credential_metadata(self, _provider_id):
            return {"last_verified_at": None}

    definitions = provider_tools.runtime_tool_definitions(
        {
            "model_providers": [{
                "id": "rank",
                "provider_type": "nvidia",
                "enabled": True,
                "selected_model": "nvidia/llama-nemotron-rerank-1b-v2",
                "model_kind": "rerank",
            }]
        },
        project_id="p1",
        manifest_digest=lambda _extension_id: "a" * 64,
        governance=UnverifiedGovernance(),
    )
    assert definitions == ()
