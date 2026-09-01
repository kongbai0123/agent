from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import app as app_module  # noqa: E402
import structured_log  # noqa: E402
from chat import runtime as chat_runtime  # noqa: E402
from project_knowledge import KnowledgeAdapterError  # noqa: E402
from semantic_retrieval import SemanticConsentRequired  # noqa: E402


FAKE_NVAPI_SECRET = "nvapi-" + "sensitive-example-1234567890"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _project_session() -> tuple[str, str]:
    project_id = _id("project")
    session_id = _id("session")
    app_module.database.create_project(project_id, project_id, str(ROOT))
    app_module.database.create_session(session_id, project_id=project_id)
    return project_id, session_id


class _CloudModelResponse:
    status_code = 200
    text = ""

    def __init__(self) -> None:
        self.closed = False

    def iter_lines(self):
        yield json.dumps({"message": {"content": "已依專案知識回答"}, "done": False}).encode()
        yield json.dumps(
            {
                "message": {},
                "done": True,
                "prompt_eval_count": 4,
                "eval_count": 2,
                "eval_duration": 1_000_000_000,
                "done_reason": "stop",
            }
        ).encode()

    def close(self) -> None:
        self.closed = True


def _failed_v2_knowledge_run(project_id: str, session_id: str) -> str:
    run_id = _id("run")
    turn_id = _id("turn")
    message = "請用專案知識回答"
    message_id = app_module.database.add_message(
        session_id,
        "user",
        message,
        turn_id=turn_id,
    )
    context, sources, evidence = app_module._knowledge_prompt_context(
        [
            {
                "text": f"部署說明；api_key={FAKE_NVAPI_SECRET}",
                "score": 0.75,
                "citation": {
                    "project_id": project_id,
                    "document_id": "doc-1",
                    "chunk_id": "chunk-1",
                    "source_id": "guide.md",
                    "title": "部署指南",
                    "ordinal": 0,
                },
            }
        ],
        project_id=project_id,
        include_evidence=True,
    )
    manifest = app_module._input_manifest(
        app_module.ChatRequest(message=message, use_rag=True),
        user_message_id=message_id,
        prompt_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        project_id=project_id,
        project_skill_context="",
        project_skill_provenance=[],
        project_skills_truncated=False,
        runtime_route="basic",
        user_query=message,
        history_snapshot=[],
        knowledge_context=context,
        knowledge_sources=sources,
        knowledge_evidence_bundle=evidence,
        hook_snapshot=app_module._hook_snapshot_payload(),
    )
    app_module.database.upsert_run(
        run_id,
        session_id,
        turn_id,
        "route-test-model",
        "chat",
        "failed",
        project_id=project_id,
        metrics={
            "runtime": "basic_chat",
            "error": {
                "code": "UPSTREAM_UNAVAILABLE",
                "message": "暫時無法使用模型。",
                "recoverable": True,
            },
        },
        input_manifest=manifest,
    )
    return run_id


def test_manifest_v2_retries_with_masked_context_and_metadata_only_sources():
    project_id, session_id = _project_session()
    run_id = _failed_v2_knowledge_run(project_id, session_id)

    private_manifest = app_module.database.get_run_input_manifest(run_id)
    allowed, reason = app_module._retry_eligibility(
        app_module.database.get_run(run_id)
    )

    assert (allowed, reason) == (True, None)
    assert private_manifest["version"] == 2
    assert private_manifest["knowledge_used"] is True
    assert private_manifest["knowledge_snapshot_sha256"]
    assert FAKE_NVAPI_SECRET not in private_manifest["knowledge_context"]
    assert "[redacted]" in private_manifest["knowledge_context"]
    assert "content" not in private_manifest["knowledge_sources"][0]
    assert private_manifest["knowledge_evidence"][0]["evidence_id"] == "knowledge:chunk-1"
    assert private_manifest["knowledge_evidence"][0]["text"] in private_manifest["knowledge_context"]
    assert len(private_manifest["knowledge_sources"][0]["snippet_sha256"]) == 64
    assert FAKE_NVAPI_SECRET not in json.dumps(
        private_manifest["knowledge_sources"], ensure_ascii=False
    )
    public_sources = chat_runtime._canonical_knowledge_sources(
        private_manifest["knowledge_sources"],
        project_id=project_id,
    )
    assert public_sources[0]["content"] == ""
    assert FAKE_NVAPI_SECRET not in json.dumps(
        public_sources, ensure_ascii=False
    )


def test_manifest_v2_retry_rejects_knowledge_snapshot_tampering():
    project_id, session_id = _project_session()
    run_id = _failed_v2_knowledge_run(project_id, session_id)
    manifest = app_module.database.get_run_input_manifest(run_id)
    manifest["knowledge_context"] += "\n遭竄改的內容"
    with app_module.database.get_db_conn() as connection:
        connection.execute(
            "UPDATE runs SET input_manifest_json=? WHERE id=?",
            (json.dumps(manifest, ensure_ascii=False), run_id),
        )

    allowed, reason = app_module._retry_eligibility(
        app_module.database.get_run(run_id)
    )

    assert allowed is False
    assert reason == "knowledge_snapshot_invalid"


def test_prompt_and_evidence_share_the_same_redacted_16k_snapshot():
    project_id = _id("project")
    raw_secret = FAKE_NVAPI_SECRET
    context, sources, evidence = app_module._knowledge_prompt_context(
        [
            {
                "text": raw_secret + (" 可驗證內容" * 5000),
                "score": 0.9,
                "citation": {
                    "project_id": project_id,
                    "document_id": "doc-bounded",
                    "chunk_id": "chunk-bounded",
                    "title": "界限測試",
                },
            }
        ],
        project_id=project_id,
        include_evidence=True,
    )

    assert len(context.encode("utf-8")) <= 16 * 1024
    assert raw_secret not in context
    assert evidence.records[0].text in context
    assert f"[evidence:{evidence.records[0].evidence_id}]" in context
    assert sources[0]["snippet_sha256"] == evidence.records[0].text_sha256


def test_structured_evidence_snapshot_detects_tampering_and_rebuilds_exactly():
    project_id, session_id = _project_session()
    run_id = _failed_v2_knowledge_run(project_id, session_id)
    manifest = app_module.database.get_run_input_manifest(run_id)

    rebuilt = app_module._knowledge_evidence_from_snapshot(
        manifest["knowledge_evidence"], project_id=project_id
    )
    assert rebuilt is not None
    assert rebuilt.records[0].text in manifest["knowledge_context"]

    manifest["knowledge_evidence"][0]["text"] += "遭竄改"
    assert app_module._knowledge_snapshot_is_valid(manifest) is False


def test_manifest_v2_retry_rejects_cross_project_snapshot_rebinding():
    project_id, session_id = _project_session()
    run_id = _failed_v2_knowledge_run(project_id, session_id)
    manifest = app_module.database.get_run_input_manifest(run_id)
    other_project_id = _id("project")
    app_module.database.create_project(other_project_id, other_project_id, str(ROOT))
    manifest["project_id"] = other_project_id
    manifest["knowledge_sources"][0]["project_id"] = other_project_id
    manifest["knowledge_sources"][0]["citation"]["project_id"] = other_project_id
    manifest["knowledge_evidence"][0]["citation"]["project_id"] = other_project_id
    manifest["knowledge_snapshot_sha256"] = app_module._knowledge_snapshot_sha256(
        manifest["knowledge_context"],
        manifest["knowledge_sources"],
        project_id=other_project_id,
        evidence=manifest["knowledge_evidence"],
    )
    with app_module.database.get_db_conn() as connection:
        connection.execute(
            "UPDATE runs SET input_manifest_json=? WHERE id=?",
            (json.dumps(manifest, ensure_ascii=False), run_id),
        )

    allowed, reason = app_module._retry_eligibility(
        app_module.database.get_run(run_id)
    )

    assert allowed is False
    assert reason == "project_scope_changed"


def test_registered_secret_crossing_old_chunk_boundary_is_redacted(monkeypatch):
    secret = "runtime-literal-secret-1234567890"
    monkeypatch.setattr(structured_log, "_EXTRA_SECRETS", [secret])
    value = ("甲" * 3498) + secret + ("乙" * 32)

    redacted = app_module._redact_knowledge_text(value, limit=len(value) + 10)

    assert secret not in redacted
    assert "[redacted]" in redacted

    boundary_redacted = app_module._redact_knowledge_text(value, limit=3500)
    assert not boundary_redacted.endswith(secret[:2])


def test_external_agent_api_key_is_removed_from_knowledge_text():
    secret = "wbk_0123456789ab_" + ("B" * 43)
    redacted = app_module._redact_knowledge_text(
        f"測試內容包含 {secret}，不應進入知識快照。"
    )
    assert secret not in redacted
    assert "[redacted]" in redacted


def test_project_knowledge_retrieval_does_not_block_the_request_event_loop(monkeypatch):
    caller_thread = threading.get_ident()
    retrieval_threads: list[int] = []

    def fake_retrieve(**_kwargs):
        retrieval_threads.append(threading.get_ident())
        return []

    monkeypatch.setattr(app_module.knowledge_service, "retrieve", fake_retrieve)

    result = asyncio.run(
        app_module._retrieve_project_knowledge_async(
            project_id="project-thread-test",
            query="測試",
            top_k=4,
            candidate_limit=20,
        )
    )

    assert result == []
    assert retrieval_threads and retrieval_threads[0] != caller_thread


def test_use_rag_is_a_document_routing_requirement():
    requirements = app_module._chat_routing_requirements(
        app_module.ChatRequest(message="查詢知識", use_rag=True)
    )

    assert requirements["documents"] is True


def test_stored_image_attachment_requires_vision_and_image_consent(monkeypatch):
    monkeypatch.setattr(
        app_module.database,
        "get_attachment",
        lambda _attachment_id: {"mime_type": "image/png"},
    )

    requirements = app_module._chat_routing_requirements(
        app_module.ChatRequest(
            message="辨識附件",
            attachment_ids=["attachment-image"],
        )
    )

    assert requirements["images"] is True
    assert requirements["documents"] is False


@pytest.mark.parametrize(
    ("request_fields", "expected_type"),
    [
        ({"images": ["aW1hZ2U="]}, "images"),
        ({"attachment_ids": ["attachment-cloud-consent"]}, "documents"),
        ({"temporary_context": "文件中的文字"}, "documents"),
    ],
)
def test_healthy_cloud_model_requires_consent_for_all_rich_inputs(
    monkeypatch,
    request_fields,
    expected_type,
):
    project_id, session_id = _project_session()
    provider_calls: list[bool] = []
    settings = {
        **app_module.load_settings(),
        "default_chat_model": "nvidia::healthy-chat",
        "hermes_enabled": False,
    }
    monkeypatch.setattr(app_module, "load_settings", lambda: settings)
    monkeypatch.setattr(
        app_module,
        "model_profile_for_model",
        lambda *_args, **_kwargs: SimpleNamespace(eligible_for_primary=True),
    )
    monkeypatch.setattr(
        app_module,
        "provider_for_model",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider="nvidia",
            base_url="https://integrate.api.nvidia.com/v1",
            input_cost_per_million=None,
            output_cost_per_million=None,
            currency="USD",
        ),
    )
    monkeypatch.setattr(
        app_module.model_governance,
        "operational_decision",
        lambda *_args, **_kwargs: SimpleNamespace(allowed=True),
    )
    monkeypatch.setattr(
        app_module.model_governance,
        "get_routing_policy",
        lambda _project_id: {
            "project_id": project_id,
            "revision": 4,
            "mode": "ask",
            "allowed_providers": ["nvidia"],
            "data_consent": {"text": True, "images": False, "documents": False},
        },
    )
    monkeypatch.setattr(
        chat_runtime,
        "provider_post_chat",
        lambda *_args, **_kwargs: provider_calls.append(True),
    )

    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        response = client.post(
            "/api/chat",
            json={
                "session_id": session_id,
                "model": "nvidia::healthy-chat",
                "message": "請處理這份資料",
                "run_id": _id("run"),
                **request_fields,
            },
        )

    assert response.status_code == 409
    error = response.json()["detail"]
    assert error["code"] == "MODEL_DATA_CONSENT_REQUIRED"
    assert error["detail"]["requirements"][expected_type] is True
    assert provider_calls == []


def test_retry_document_requirement_is_derived_from_snapshot_not_mutable_flag():
    requirements = app_module._chat_routing_requirements(
        app_module.ChatRequest(message="重試"),
        retry_manifest={
            "knowledge_used": False,
            "knowledge_context": "已保存的知識內容",
            "knowledge_sources": [],
        },
    )

    assert requirements["documents"] is True


def test_whole_run_retry_preserves_one_time_consent_and_budget_authority():
    project_id, session_id = _project_session()
    source_id = _failed_v2_knowledge_run(project_id, session_id)
    proposal_id = "mrp_" + ("a" * 32)
    override_id = "mbo_" + ("b" * 32)

    restored, source, manifest = app_module._retry_request(
        app_module.ChatRequest(
            session_id=session_id,
            retry_of_run_id=source_id,
            run_id=_id("run"),
            routing_proposal_id=proposal_id,
            budget_override_id=override_id,
        )
    )

    assert source and manifest
    assert restored.routing_proposal_id == proposal_id
    assert restored.budget_override_id == override_id


def test_healthy_cloud_model_cannot_bypass_project_document_consent(monkeypatch):
    project_id, session_id = _project_session()
    provider_calls: list[bool] = []
    settings = {
        **app_module.load_settings(),
        "default_chat_model": "nvidia::healthy-chat",
        "hermes_enabled": False,
    }
    monkeypatch.setattr(app_module, "load_settings", lambda: settings)
    monkeypatch.setattr(
        app_module,
        "model_profile_for_model",
        lambda *_args, **_kwargs: SimpleNamespace(eligible_for_primary=True),
    )
    monkeypatch.setattr(
        app_module,
        "provider_for_model",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider="nvidia",
            base_url="https://integrate.api.nvidia.com/v1",
            input_cost_per_million=None,
            output_cost_per_million=None,
            currency="USD",
        ),
    )
    monkeypatch.setattr(
        app_module.model_governance,
        "operational_decision",
        lambda *_args, **_kwargs: SimpleNamespace(allowed=True),
    )
    monkeypatch.setattr(app_module, "uses_local_model_slot", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        app_module.model_governance,
        "get_routing_policy",
        lambda _project_id: {
            "project_id": project_id,
            "revision": 3,
            "mode": "ask",
            "allowed_providers": ["nvidia"],
            "data_consent": {"text": True, "images": False, "documents": False},
        },
    )
    monkeypatch.setattr(
        chat_runtime,
        "provider_post_chat",
        lambda *_args, **_kwargs: provider_calls.append(True),
    )

    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        response = client.post(
            "/api/chat",
            json={
                "session_id": session_id,
                "model": "nvidia::healthy-chat",
                "message": "請用專案知識回答",
                "use_rag": True,
                "run_id": _id("run"),
            },
        )

    assert response.status_code == 409
    error = response.json()["detail"]
    detail = error["detail"]
    assert error["code"] == "MODEL_DATA_CONSENT_REQUIRED"
    assert detail["required_data"] == ["documents"]
    assert detail["requirements"] == {"documents": True}
    assert detail["provider"] == "nvidia"
    assert detail["model"] == "nvidia::healthy-chat"
    assert detail["proposal_id"].startswith("mrp_")
    assert detail["policy_revision"] == 3
    assert detail["risk"]
    assert len(detail["consequences"]) == 3
    with app_module.database.get_db_conn() as connection:
        proposal = connection.execute(
            "SELECT * FROM model_routing_proposals WHERE proposal_id=?",
            (detail["proposal_id"],),
        ).fetchone()
    assert proposal["project_id"] == project_id
    assert proposal["run_id"] == detail["run_id"]
    assert proposal["requested_model"] == "nvidia::healthy-chat"
    assert proposal["selected_model"] == "nvidia::healthy-chat"
    assert json.loads(proposal["requirements_json"])["documents"] is True
    assert provider_calls == []


def test_one_time_document_consent_is_approved_consumed_and_not_persisted(monkeypatch):
    project_id, session_id = _project_session()
    model_response = _CloudModelResponse()
    provider_calls: list[dict] = []
    retrieval_calls: list[dict] = []
    settings = {
        **app_module.load_settings(),
        "default_chat_model": "nvidia::healthy-chat",
        "hermes_enabled": False,
    }
    monkeypatch.setattr(app_module, "load_settings", lambda: settings)
    monkeypatch.setattr(
        app_module,
        "model_profile_for_model",
        lambda *_args, **_kwargs: SimpleNamespace(eligible_for_primary=True),
    )
    monkeypatch.setattr(
        app_module,
        "provider_for_model",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider="nvidia",
            base_url="https://integrate.api.nvidia.com/v1",
            input_cost_per_million=None,
            output_cost_per_million=None,
            currency="USD",
        ),
    )
    monkeypatch.setattr(
        app_module.model_governance,
        "operational_decision",
        lambda *_args, **_kwargs: SimpleNamespace(allowed=True),
    )
    monkeypatch.setattr(app_module, "uses_local_model_slot", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        chat_runtime,
        "model_profile_for_model",
        lambda *_args, **_kwargs: SimpleNamespace(
            supports_chat=True,
            eligible_for_primary=True,
        ),
    )
    monkeypatch.setattr(chat_runtime, "model_supports_tools", lambda *_args, **_kwargs: False)
    def fake_retrieve(**kwargs):
        retrieval_calls.append(dict(kwargs))
        return []

    monkeypatch.setattr(app_module.knowledge_service, "retrieve", fake_retrieve)

    def fake_provider(_settings, payload, **_kwargs):
        provider_calls.append(payload)
        return model_response

    monkeypatch.setattr(chat_runtime, "provider_post_chat", fake_provider)
    run_id = _id("run")
    request_payload = {
        "session_id": session_id,
        "model": "nvidia::healthy-chat",
        "message": "請用專案知識回答",
        "use_rag": True,
        "run_id": run_id,
    }

    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        blocked = client.post("/api/chat", json=request_payload)
        proposal_id = blocked.json()["detail"]["detail"]["proposal_id"]
        approved = client.post(
            f"/api/model-routing/proposals/{proposal_id}/approve",
            json={"remember_project": False},
        )
        completed = client.post(
            "/api/chat",
            json={**request_payload, "routing_proposal_id": proposal_id},
        )

    assert blocked.status_code == 409
    assert approved.status_code == 200
    assert completed.status_code == 200
    assert completed.headers["content-type"].startswith("text/event-stream")
    assert len(provider_calls) == 1
    assert retrieval_calls == [
        {
            "project_id": project_id,
            "query": "請用專案知識回答",
            "top_k": int(settings.get("rag_k") or 4),
            "candidate_limit": max(20, int(settings.get("rag_k") or 4) * 5),
            "run_id": run_id,
            "consent_proposal_id": proposal_id,
            "requested_model": "nvidia::healthy-chat",
            "budget_override_id": "",
        }
    ]
    assert model_response.closed
    policy = app_module.model_governance.get_routing_policy(project_id)
    assert policy["data_consent"]["documents"] is False
    with app_module.database.get_db_conn() as connection:
        proposal = connection.execute(
            "SELECT status, consumed_at FROM model_routing_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
    assert proposal["status"] == "consumed"
    assert proposal["consumed_at"]


def test_cloud_embedding_consent_does_not_replace_the_primary_chat_model(monkeypatch):
    project_id, session_id = _project_session()
    model_response = _CloudModelResponse()
    provider_models: list[str] = []
    retrieval_calls: list[dict] = []
    settings = {
        **app_module.load_settings(),
        "default_chat_model": "local-chat",
        "hermes_enabled": False,
    }
    monkeypatch.setattr(app_module, "load_settings", lambda: settings)

    def profile(_settings, model, **_kwargs):
        return SimpleNamespace(eligible_for_primary=model != "semantic::embed-model")

    monkeypatch.setattr(app_module, "model_profile_for_model", profile)
    monkeypatch.setattr(
        app_module,
        "provider_for_model",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider="ollama",
            base_url="http://127.0.0.1:11434",
            input_cost_per_million=None,
            output_cost_per_million=None,
            currency="USD",
        ),
    )
    monkeypatch.setattr(
        app_module.model_governance,
        "operational_decision",
        lambda *_args, **_kwargs: SimpleNamespace(allowed=True),
    )
    monkeypatch.setattr(app_module, "uses_local_model_slot", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        chat_runtime,
        "model_profile_for_model",
        lambda *_args, **_kwargs: SimpleNamespace(
            supports_chat=True,
            eligible_for_primary=True,
        ),
    )
    monkeypatch.setattr(chat_runtime, "model_supports_tools", lambda *_args, **_kwargs: False)

    def fake_retrieve(**kwargs):
        retrieval_calls.append(dict(kwargs))
        if len(retrieval_calls) == 1:
            cause = SemanticConsentRequired(
                "approval required",
                provider_id="semantic",
                model_reference="semantic::embed-model",
            )
            wrapped = KnowledgeAdapterError(
                "approval required",
                code=cause.code,
                status_code=cause.status_code,
            )
            raise wrapped from cause
        return []

    monkeypatch.setattr(app_module.knowledge_service, "retrieve", fake_retrieve)

    def fake_provider(_settings, payload, **kwargs):
        provider_models.append(str(kwargs.get("model") or payload.get("model") or ""))
        return model_response

    monkeypatch.setattr(chat_runtime, "provider_post_chat", fake_provider)
    run_id = _id("run")
    request_payload = {
        "session_id": session_id,
        "model": "local-chat",
        "message": "請用專案知識回答",
        "use_rag": True,
        "run_id": run_id,
    }

    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        blocked = client.post("/api/chat", json=request_payload)
        detail = blocked.json()["detail"]["detail"]
        approved = client.post(
            f"/api/model-routing/proposals/{detail['proposal_id']}/approve",
            json={"remember_project": False},
        )
        completed = client.post(
            "/api/chat",
            json={
                **request_payload,
                "routing_proposal_id": detail["proposal_id"],
            },
        )

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "MODEL_DATA_CONSENT_REQUIRED"
    assert detail["consent_target"] == "semantic_retrieval"
    assert detail["provider"] == "semantic"
    assert detail["selected_model"] == "semantic::embed-model"
    assert approved.status_code == 200
    assert completed.status_code == 200
    assert retrieval_calls[0]["consent_proposal_id"] == ""
    assert retrieval_calls[1]["consent_proposal_id"] == detail["proposal_id"]
    assert provider_models == ["local-chat"]


def test_semantic_consent_cannot_be_repurposed_as_a_chat_model_switch(monkeypatch):
    project_id, session_id = _project_session()
    run_id = _id("run")
    proposal = app_module.model_governance.create_data_consent_proposal(
        project_id=project_id,
        run_id=run_id,
        requested_model="local-chat",
        selected_model="semantic::embed-model",
        provider_id="semantic",
        data_types=("documents",),
    )
    app_module.model_governance.approve_proposal(
        proposal["proposal_id"], remember_project=False
    )
    settings = {
        **app_module.load_settings(),
        "default_chat_model": "local-chat",
        "hermes_enabled": False,
    }
    monkeypatch.setattr(app_module, "load_settings", lambda: settings)
    monkeypatch.setattr(
        app_module,
        "model_profile_for_model",
        lambda _settings, model, **_kwargs: SimpleNamespace(
            eligible_for_primary=model != "semantic::embed-model"
        ),
    )
    provider_calls: list[bool] = []
    monkeypatch.setattr(
        chat_runtime,
        "provider_post_chat",
        lambda *_args, **_kwargs: provider_calls.append(True),
    )

    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        response = client.post(
            "/api/chat",
            json={
                "session_id": session_id,
                "model": "local-chat",
                "message": "一般聊天",
                "run_id": run_id,
                "routing_proposal_id": proposal["proposal_id"],
                "use_rag": False,
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ROUTING_PROPOSAL_INVALID"
    assert provider_calls == []


def test_project_policy_allows_cloud_rag_after_explicit_consent(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "provider_for_model",
        lambda *_args, **_kwargs: SimpleNamespace(provider="nvidia"),
    )
    monkeypatch.setattr(
        app_module.model_governance,
        "get_routing_policy",
        lambda _project_id: {
            "revision": 2,
            "allowed_providers": ["nvidia"],
            "data_consent": {"documents": True},
        },
    )

    allowed, detail = app_module._remote_knowledge_consent(
        {},
        project_id="project-consented",
        model="nvidia::healthy-chat",
        approved_once=False,
    )

    assert allowed is True
    assert detail["documents_allowed"] is True


def test_remote_ollama_requires_document_consent(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "provider_for_model",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider="ollama", base_url="https://ollama.example.test"
        ),
    )
    monkeypatch.setattr(
        app_module.model_governance,
        "get_routing_policy",
        lambda _project_id: {
            "revision": 1,
            "allowed_providers": ["ollama"],
            "data_consent": {"documents": False},
        },
    )

    allowed, detail = app_module._remote_knowledge_consent(
        {},
        project_id="project-remote-ollama",
        model="remote-model",
        approved_once=False,
    )

    assert allowed is False
    assert detail["provider"] == "ollama"
    assert detail["documents_allowed"] is False


def test_loopback_ollama_stays_inside_local_boundary(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "provider_for_model",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider="ollama", base_url="http://[::1]:11434"
        ),
    )

    allowed, detail = app_module._remote_knowledge_consent(
        {},
        project_id="project-local-ollama",
        model="local-model",
        approved_once=False,
    )

    assert allowed is True
    assert detail["consent_source"] == "local_boundary"


def test_semantic_adapter_selection_is_local_first(monkeypatch, tmp_path):
    embedding_path = tmp_path / "embedding"
    reranker_path = tmp_path / "reranker"
    embedding_path.mkdir()
    reranker_path.mkdir()
    embedding = object()
    reranker = object()
    monkeypatch.setattr(
        app_module,
        "LocalSentenceTransformerEmbeddingAdapter",
        lambda path: embedding if Path(path) == embedding_path else None,
    )
    monkeypatch.setattr(
        app_module,
        "LocalCrossEncoderRerankerAdapter",
        lambda path: reranker if Path(path) == reranker_path else None,
    )
    monkeypatch.setattr(
        app_module,
        "_semantic_provider_config",
        lambda *_args, **_kwargs: pytest.fail("provider fallback must not run"),
    )

    selected = app_module._project_knowledge_adapters(
        {
            "rag_local_embedding_model_path": str(embedding_path),
            "rag_local_reranker_model_path": str(reranker_path),
            "rag_embedding_provider_id": "cloud-embed",
            "rag_reranker_provider_id": "cloud-rerank",
        }
    )

    assert selected == (embedding, reranker)


def test_nvidia_semantic_routes_use_retrieval_endpoints_and_passages_contract(
    monkeypatch,
):
    provider = {
        "id": "nvidia-rerank",
        "provider_type": "nvidia",
        "selected_model": "nvidia/llama-nemotron-rerank-1b-v2",
        "model_kind": "rerank",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "enabled": True,
    }
    route = app_module._semantic_route(provider, capability="rerank")
    client = SimpleNamespace(route=route)
    monkeypatch.setattr(
        app_module,
        "_governed_semantic_client",
        lambda _route, **_kwargs: client,
    )

    _, reranker = app_module._project_knowledge_adapters(
        {"model_providers": [provider], "rag_reranker_provider_id": "nvidia-rerank"}
    )

    assert route.endpoint_for("rerank") == (
        "https://ai.api.nvidia.com/v1/retrieval/nvidia/"
        "llama-nemotron-rerank-1b-v2/reranking"
    )
    assert isinstance(reranker.contract, app_module.PassagesRerankContract)


def test_semantic_provider_gate_rechecks_current_model_and_project(monkeypatch):
    route = app_module.SemanticProviderRoute(
        provider_id="semantic-cloud",
        model_id="embed-model",
        base_url="https://semantic.example/v1",
    )
    checked: list[tuple[str, str]] = []
    settings = {
        "model_providers": [
            {
                "id": "semantic-cloud",
                "provider_type": "openai_compatible",
                "model_kind": "embedding",
                "selected_model": "embed-model",
                "base_url": "https://semantic.example/v1",
            }
        ]
    }
    monkeypatch.setattr(app_module, "load_settings", lambda: settings)
    monkeypatch.setattr(
        app_module,
        "require_provider_enabled",
        lambda _settings, provider_id, *, project_id=None: checked.append(
            (provider_id, project_id)
        ),
    )
    gate = app_module._semantic_provider_access_check(route, capability="embedding")

    gate("semantic-cloud", "project-a")
    assert checked == [("semantic-cloud", "project-a")]

    settings["model_providers"][0]["selected_model"] = "changed-model"
    with pytest.raises(PermissionError):
        gate("semantic-cloud", "project-a")
