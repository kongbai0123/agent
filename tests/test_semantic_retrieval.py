from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests

from backend.project_knowledge import KnowledgeAdapterError, ProjectKnowledgeService
from backend.semantic_retrieval import (
    DocumentsRerankContract,
    GovernedProviderEmbeddingAdapter,
    GovernedProviderRerankerAdapter,
    GovernedSemanticProviderClient,
    LocalCrossEncoderRerankerAdapter,
    LocalSentenceTransformerEmbeddingAdapter,
    ModelGovernanceSemanticPolicy,
    OpenAIEmbeddingContract,
    PassagesRerankContract,
    SemanticConsentRequired,
    SemanticProviderRoute,
    SemanticRequestContext,
    SemanticRetrievalError,
)


class FakeGovernance:
    def __init__(self, *, remembered: bool = True) -> None:
        self.remembered = remembered
        self.consumed = False
        self.successes: list[tuple[str, str]] = []
        self.failures: list[dict] = []
        self.usage: list[dict] = []
        self.audits: list[tuple[str, dict]] = []

    def get_routing_policy(self, project_id):
        assert project_id
        return {
            "mode": "auto_within_policy" if self.remembered else "ask",
            "allowed_providers": ["cloud"] if self.remembered else [],
            "data_consent": {"documents": self.remembered},
        }

    def proposal_grants_data(self, proposal_id, **kwargs):
        return bool(
            self.consumed
            and proposal_id == "proposal-1"
            and kwargs["project_id"] == "project-a"
            and kwargs["run_id"] == "run-a"
            and kwargs["selected_model"] == "cloud::embed-model"
        )

    def consume_proposal(self, proposal_id, **kwargs):
        if (
            proposal_id == "proposal-1"
            and kwargs["project_id"] == "project-a"
            and kwargs["run_id"] == "run-a"
            and kwargs["requested_model"] == "chat::original"
        ):
            self.consumed = True
            return "cloud::embed-model"
        return None

    def audit(self, action, **kwargs):
        self.audits.append((action, kwargs))

    def credential_metadata(self, provider_id):
        return {"last_verified_at": "2026-08-26T00:00:00+00:00"}

    def operational_decision(self, provider_id, **kwargs):
        return SimpleNamespace(allowed=True, code="", message="", retry_at=None)

    def budget_decision(self, **kwargs):
        return SimpleNamespace(allowed=True, code="", message="", warnings=())

    def observe_success(self, provider_id, **kwargs):
        self.successes.append((provider_id, kwargs["model_id"]))
        return {"state": "healthy"}

    def observe_failure(self, provider_id, **kwargs):
        record = {"provider_id": provider_id, **kwargs}
        self.failures.append(record)
        return {"state": "degraded", "retry_at": None}

    def record_usage(self, **kwargs):
        self.usage.append(kwargs)


class FakeResponse:
    def __init__(self, status_code, payload, *, headers=None, raw=None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.content = (
            json.dumps(payload).encode("utf-8") if raw is None else raw
        )
        self.closed = False

    def json(self):
        return self._payload

    def iter_content(self, chunk_size):
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index : index + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def _route(**overrides):
    values = {
        "provider_id": "cloud",
        "model_id": "embed-model",
        "base_url": "https://provider.example/v1",
    }
    values.update(overrides)
    return SemanticProviderRoute(**values)


def _client(governance, session, *, route=None, secret="provider-secret"):
    checked = []

    def access(provider_id, project_id):
        checked.append((provider_id, project_id))

    client = GovernedSemanticProviderClient(
        route or _route(),
        governance=governance,
        access_policy=ModelGovernanceSemanticPolicy(governance),
        provider_access_check=access,
        secret_resolver=lambda _provider_id: secret,
        session=session,
    )
    client.checked = checked
    return client


def test_provider_route_is_https_same_origin_and_provider_neutral():
    route = _route(
        embedding_endpoint="/custom/embeddings",
        rerank_endpoint="https://provider.example/custom/rerank",
    )

    assert route.endpoint_for("embedding") == "https://provider.example/custom/embeddings"
    assert route.endpoint_for("rerank") == "https://provider.example/custom/rerank"
    assert route.model_reference == "cloud::embed-model"

    with pytest.raises(SemanticRetrievalError) as insecure:
        _route(base_url="http://provider.example/v1")
    assert insecure.value.code == "SEMANTIC_PROVIDER_INSECURE"
    with pytest.raises(SemanticRetrievalError):
        _route(embedding_endpoint="https://attacker.example/embeddings")


def test_openai_embedding_contract_restores_index_order_and_input_type():
    route = _route(document_input_type="passage", query_input_type="query")
    contract = OpenAIEmbeddingContract()

    assert contract.request_body(route, ["a"], purpose="document")["input_type"] == "passage"
    vectors = contract.parse_response(
        {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]
        },
        expected_count=2,
    )
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]

    with pytest.raises(SemanticRetrievalError):
        contract.parse_response(
            {"data": [{"index": 0, "embedding": [1.0]}]}, expected_count=2
        )


def test_both_generic_rerank_contracts_require_complete_scores():
    candidates = ({"text": "first"}, {"text": "second"})
    documents = DocumentsRerankContract()
    passages = PassagesRerankContract()

    assert documents.request_body(_route(), "q", candidates)["documents"] == [
        "first",
        "second",
    ]
    assert documents.parse_response(
        {
            "results": [
                {"index": 1, "relevance_score": 0.2},
                {"index": 0, "relevance_score": 0.8},
            ]
        },
        expected_count=2,
    ) == [0.8, 0.2]
    assert passages.request_body(_route(), "q", candidates)["passages"] == [
        {"text": "first"},
        {"text": "second"},
    ]
    with pytest.raises(SemanticRetrievalError):
        passages.parse_response(
            {"rankings": [{"index": 0, "logit": 1.0}]}, expected_count=2
        )


def test_cloud_policy_requires_remembered_or_one_time_project_consent():
    governance = FakeGovernance(remembered=False)
    policy = ModelGovernanceSemanticPolicy(governance)
    route = _route()

    with pytest.raises(SemanticConsentRequired):
        policy.authorize(
            route,
            SemanticRequestContext(project_id="project-a", run_id="run-a"),
            data_type="documents",
        )

    policy.authorize(
        route,
        SemanticRequestContext(
            project_id="project-a",
            run_id="run-a",
            consent_proposal_id="proposal-1",
            requested_model="chat::original",
        ),
        data_type="documents",
    )
    assert governance.consumed is True
    assert governance.audits[-1][0] == "semantic_data_allowed"

    with pytest.raises(SemanticConsentRequired):
        policy.authorize(
            route,
            SemanticRequestContext(
                project_id="project-b",
                run_id="run-a",
                consent_proposal_id="proposal-1",
                requested_model="chat::original",
            ),
            data_type="documents",
        )


def test_unapproved_cloud_semantic_call_sends_zero_provider_requests():
    governance = FakeGovernance(remembered=False)
    session = FakeSession(
        [FakeResponse(200, {"data": [{"index": 0, "embedding": [1.0]}]})]
    )
    client = _client(governance, session)
    adapter = GovernedProviderEmbeddingAdapter(client)

    with pytest.raises(SemanticConsentRequired):
        adapter.embed_for_project(
            ["private project document"],
            purpose="document",
            context=SemanticRequestContext(
                project_id="project-a",
                run_id="run-unapproved",
            ),
        )

    assert session.calls == []
    assert governance.usage == []
    assert governance.successes == []


def test_governed_embedding_uses_project_gate_health_budget_and_ledger():
    governance = FakeGovernance()
    response = FakeResponse(
        200,
        {"data": [{"index": 0, "embedding": [0.25, 0.75]}], "usage": {"prompt_tokens": 7}},
    )
    session = FakeSession([response])
    client = _client(governance, session)
    adapter = GovernedProviderEmbeddingAdapter(client)

    vectors = adapter.embed_for_project(
        ["project document"],
        purpose="document",
        context=SemanticRequestContext(project_id="project-a", run_id="run-a"),
    )

    assert vectors == [[0.25, 0.75]]
    assert client.checked == [("cloud", "project-a")]
    assert session.calls[0]["url"] == "https://provider.example/v1/embeddings"
    assert session.calls[0]["headers"]["Authorization"] == "Bearer provider-secret"
    assert governance.successes == [("cloud", "embed-model")]
    assert governance.usage[-1]["project_id"] == "project-a"
    assert governance.usage[-1]["capability"] == "embedding"
    assert governance.usage[-1]["prompt_tokens"] == 7
    assert response.closed is True


def test_invalid_provider_payload_degrades_health_without_leaking_secret():
    governance = FakeGovernance()
    response = FakeResponse(200, {"data": []})
    session = FakeSession([response])
    adapter = GovernedProviderEmbeddingAdapter(_client(governance, session))

    with pytest.raises(SemanticRetrievalError) as exc_info:
        adapter.embed_for_project(
            ["document"],
            purpose="document",
            context=SemanticRequestContext(project_id="project-a"),
        )

    assert "provider-secret" not in str(exc_info.value)
    assert governance.successes == []
    assert governance.failures[-1]["status_code"] == 502
    assert governance.usage[-1]["status"] == "failed"


def test_http_failure_is_sanitized_and_never_retried():
    governance = FakeGovernance()
    response = FakeResponse(
        429,
        {"error": "provider-secret upstream detail"},
        headers={"Retry-After": "30"},
    )
    session = FakeSession([response])
    adapter = GovernedProviderEmbeddingAdapter(_client(governance, session))

    with pytest.raises(SemanticRetrievalError) as exc_info:
        adapter.embed_for_project(
            ["document"],
            purpose="query",
            context=SemanticRequestContext(project_id="project-a"),
        )

    assert exc_info.value.code == "SEMANTIC_PROVIDER_HTTP_429"
    assert "provider-secret" not in str(exc_info.value)
    assert len(session.calls) == 1
    assert governance.failures[-1]["retry_after"] == "30"


def test_governed_reranker_is_wire_contract_pluggable():
    governance = FakeGovernance()
    response = FakeResponse(
        200,
        {
            "rankings": [
                {"index": 1, "logit": 0.2},
                {"index": 0, "logit": 0.9},
            ]
        },
    )
    route = _route(model_id="rerank-model", rerank_endpoint="/ranking")
    client = _client(governance, FakeSession([response]), route=route)
    adapter = GovernedProviderRerankerAdapter(
        client, contract=PassagesRerankContract()
    )

    scores = adapter.rerank_for_project(
        "query",
        ({"text": "a"}, {"text": "b"}),
        context=SemanticRequestContext(project_id="project-a"),
    )

    assert scores == [0.9, 0.2]
    assert client.session.calls[0]["url"] == "https://provider.example/ranking"


def test_local_models_only_load_from_existing_absolute_tree(tmp_path):
    model_dir = tmp_path / "embedding-model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")

    class Encoded:
        def tolist(self):
            return [[1.0, 0.0], [0.0, 1.0]]

    class FakeEmbeddingModel:
        def __init__(self):
            self.calls = []

        def encode(self, texts, **kwargs):
            self.calls.append((list(texts), kwargs))
            return Encoded()

    fake = FakeEmbeddingModel()
    adapter = LocalSentenceTransformerEmbeddingAdapter(model_dir, model=fake)

    assert adapter.embed(["a", "b"], purpose="document") == [
        [1.0, 0.0],
        [0.0, 1.0],
    ]
    assert fake.calls[0][1]["normalize_embeddings"] is True
    assert adapter.adapter_id.startswith("local-sentence-transformer-")

    with pytest.raises(SemanticRetrievalError):
        LocalSentenceTransformerEmbeddingAdapter("relative/model")


def test_local_cross_encoder_returns_one_score_per_candidate(tmp_path):
    model_dir = tmp_path / "reranker-model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")

    class Predicted:
        def tolist(self):
            return [0.1, 0.9]

    class FakeCrossEncoder:
        def predict(self, pairs, **kwargs):
            assert pairs == [("q", "one"), ("q", "two")]
            return Predicted()

    adapter = LocalCrossEncoderRerankerAdapter(
        model_dir, model=FakeCrossEncoder()
    )
    assert adapter.rerank("q", ({"text": "one"}, {"text": "two"})) == [0.1, 0.9]


def test_project_service_passes_authoritative_context_and_reranker_fails_open(tmp_path):
    class ContextEmbedding:
        adapter_id = "context-embedding-v1"

        def __init__(self):
            self.contexts = []

        def embed_for_project(self, texts, *, purpose, context):
            self.contexts.append((purpose, context))
            return [[float(str(text).count("alpha")), 1.0] for text in texts]

    class UnavailableReranker:
        adapter_id = "unavailable-reranker-v1"

        def rerank_for_project(self, query, candidates, *, context):
            assert context.project_id == "project-a"
            raise SemanticRetrievalError(
                "provider unavailable", code="PROVIDER_COOLDOWN", status_code=409
            )

    embedding = ContextEmbedding()
    service = ProjectKnowledgeService(
        tmp_path / "knowledge.db",
        embedding_adapter=embedding,
        reranker=UnavailableReranker(),
        chunk_chars=128,
        overlap_chars=0,
    )
    service.import_document(
        project_id="project-a",
        source_id="guide",
        title="Guide",
        content="alpha project knowledge " * 20,
        run_id="import-run",
    )
    results = service.retrieve(
        project_id="project-a",
        query="alpha",
        run_id="query-run",
        consent_proposal_id="proposal-1",
        requested_model="chat::original",
    )

    assert results
    assert results[0]["retrieval_mode"] == "embedding_fallback"
    assert results[0]["degradation_code"] == "PROVIDER_COOLDOWN"
    assert {item[1].project_id for item in embedding.contexts} == {"project-a"}
    assert {item[1].run_id for item in embedding.contexts} == {
        "import-run",
        "query-run",
    }


def test_project_service_can_make_reranker_failure_fail_closed(tmp_path):
    class FailedReranker:
        adapter_id = "strict-reranker-v1"

        def rerank(self, query, candidates):
            raise RuntimeError("raw upstream body must not escape")

    service = ProjectKnowledgeService(
        tmp_path / "knowledge.db",
        reranker=FailedReranker(),
        reranker_fail_open=False,
        chunk_chars=128,
        overlap_chars=0,
    )
    service.import_document(
        project_id="project-a",
        source_id="guide",
        title="Guide",
        content="alpha project knowledge " * 20,
    )

    with pytest.raises(KnowledgeAdapterError) as exc_info:
        service.retrieve(project_id="project-a", query="alpha")
    assert str(exc_info.value) == "Reranker adapter failed."


def test_project_service_maps_consent_denial_and_never_mutates_index(tmp_path):
    class DeniedEmbedding:
        adapter_id = "denied-embedding-v1"

        def embed_for_project(self, texts, *, purpose, context):
            raise SemanticConsentRequired(
                "approval required",
                provider_id="semantic",
                model_reference="semantic::embed-model",
            )

    service = ProjectKnowledgeService(
        tmp_path / "knowledge.db", embedding_adapter=DeniedEmbedding()
    )

    with pytest.raises(KnowledgeAdapterError) as exc_info:
        service.import_document(
            project_id="project-a",
            source_id="guide",
            title="Guide",
            content="private project document",
        )
    assert exc_info.value.code == "SEMANTIC_DATA_CONSENT_REQUIRED"
    assert exc_info.value.provider_id == "semantic"
    assert exc_info.value.model_reference == "semantic::embed-model"
    assert service.list_documents(project_id="project-a") == []


def test_transport_exception_does_not_retry_or_echo_details():
    governance = FakeGovernance()

    class FailedSession:
        def __init__(self):
            self.calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            raise requests.ConnectionError("provider-secret socket detail")

    session = FailedSession()
    adapter = GovernedProviderEmbeddingAdapter(_client(governance, session))
    with pytest.raises(SemanticRetrievalError) as exc_info:
        adapter.embed_for_project(
            ["document"],
            purpose="document",
            context=SemanticRequestContext(project_id="project-a"),
        )

    assert session.calls == 1
    assert "provider-secret" not in str(exc_info.value)
    assert governance.usage[-1]["provider_signal"] == "transport_error"
