from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from backend.project_knowledge import (
    MAX_DOCUMENT_BYTES,
    MAX_EMBEDDING_BATCH_BYTES,
    MAX_EMBEDDING_BATCH_TEXTS,
    MAX_RERANK_INPUT_BYTES,
    MAX_RESULT_TEXT_BYTES,
    DeterministicLocalEmbedding,
    KnowledgeAdapterError,
    ProjectKnowledgeError,
    ProjectKnowledgeService,
    stable_chunk_text,
)


class RecordingEmbedding:
    adapter_id = "recording-v1"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def embed(self, texts, *, purpose):
        self.calls.append((purpose, tuple(texts)))
        # Deliberately simple keyword dimensions make ranking assertions exact.
        return [
            [
                float(text.casefold().count("alpha")),
                float(text.casefold().count("beta")),
                1.0,
            ]
            for text in texts
        ]


class ReverseReranker:
    adapter_id = "reverse-v1"

    def rerank(self, query, candidates):
        assert query
        return list(range(len(candidates)))


class BlockingEmbedding(RecordingEmbedding):
    adapter_id = "blocking-v1"

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def embed(self, texts, *, purpose):
        if purpose == "document":
            self.started.set()
            if not self.release.wait(timeout=5):
                raise RuntimeError("test embedding release timed out")
        return super().embed(texts, purpose=purpose)


@pytest.fixture
def service(tmp_path):
    return ProjectKnowledgeService(tmp_path / "knowledge.db", chunk_chars=128, overlap_chars=16)


def test_stable_chunking_is_deterministic_and_offsets_reconstruct_text():
    content = ("第一段內容。" * 30) + "\r\n\r\n" + ("Second paragraph. " * 30)
    first = stable_chunk_text(content, max_chars=128, overlap_chars=16)
    second = stable_chunk_text(content.replace("\r\n", "\n"), max_chars=128, overlap_chars=16)

    assert first == second
    normalized = content.replace("\r\n", "\n").strip()
    assert len(first) > 2
    for ordinal, chunk in enumerate(first):
        assert chunk["ordinal"] == ordinal
        assert normalized[chunk["start_offset"] : chunk["end_offset"]] == chunk["text"]
        assert len(chunk["content_sha256"]) == 64


def test_default_embedding_is_reproducible_and_handles_traditional_chinese():
    first = DeterministicLocalEmbedding().embed(["專案知識檢索"], purpose="document")[0]
    second = DeterministicLocalEmbedding().embed(["專案知識檢索"], purpose="query")[0]
    unrelated = DeterministicLocalEmbedding().embed(["完全不同文字"], purpose="query")[0]

    assert first == second
    assert sum(value * value for value in first) == pytest.approx(1.0)
    assert first != unrelated


def test_chunking_policy_can_be_updated_for_future_imports(service):
    service.configure_chunking(chunk_chars=256, overlap_chars=32)
    status = service.status(project_id="project-a")
    assert status["chunk_chars"] == 256
    assert status["overlap_chars"] == 32

    with pytest.raises(ProjectKnowledgeError) as exc_info:
        service.configure_chunking(chunk_chars=100, overlap_chars=10)
    assert exc_info.value.code == "INVALID_CHUNK_POLICY"


def test_import_retrieve_returns_bounded_project_scoped_citations(service):
    one = service.import_document(
        project_id="project-a",
        source_id="manual/alpha.md",
        title="Alpha manual",
        content="Alpha deployment guide. " * 40,
    )
    service.import_document(
        project_id="project-b",
        source_id="private/beta.md",
        title="Private beta",
        content="Alpha private secret from another project. " * 20,
    )

    results = service.retrieve(project_id="project-a", query="alpha deployment", top_k=3)

    assert results
    assert all(result["citation"]["project_id"] == "project-a" for result in results)
    assert all(result["citation"]["document_id"] == one["document_id"] for result in results)
    assert all(result["citation"]["source_id"] == "manual/alpha.md" for result in results)
    assert all(len(result["citation"]["document_sha256"]) == 64 for result in results)
    assert all(len(result["citation"]["chunk_sha256"]) == 64 for result in results)
    assert sum(len(result["text"].encode("utf-8")) for result in results) <= MAX_RESULT_TEXT_BYTES
    assert "private secret" not in " ".join(result["text"] for result in results)


def test_project_delete_guard_serializes_inflight_import_and_leaves_tombstone(tmp_path):
    adapter = BlockingEmbedding()
    service = ProjectKnowledgeService(
        tmp_path / "knowledge.db",
        embedding_adapter=adapter,
    )
    import_errors: list[Exception] = []
    delete_finished = threading.Event()

    def run_import():
        try:
            service.import_document(
                project_id="project-race",
                source_id="race.md",
                title="Race",
                content="content arriving during project deletion",
            )
        except Exception as exc:  # pragma: no cover - asserted below
            import_errors.append(exc)

    def run_delete():
        with service.project_delete_guard(project_id="project-race"):
            service.clear_project(project_id="project-race")
        delete_finished.set()

    importer = threading.Thread(target=run_import)
    importer.start()
    assert adapter.started.wait(timeout=2)
    deleter = threading.Thread(target=run_delete)
    deleter.start()
    assert not delete_finished.wait(timeout=0.1)

    adapter.release.set()
    importer.join(timeout=5)
    deleter.join(timeout=5)

    assert not importer.is_alive() and not deleter.is_alive()
    assert import_errors == []
    assert service.status(project_id="project-race")["document_count"] == 0
    with pytest.raises(ProjectKnowledgeError) as blocked:
        service.import_document(
            project_id="project-race",
            source_id="late.md",
            title="Late",
            content="must not recreate an orphan index",
        )
    assert blocked.value.code == "KNOWLEDGE_PROJECT_DELETING"


def test_same_import_is_noop_and_changed_import_reuses_unchanged_chunks(tmp_path):
    adapter = RecordingEmbedding()
    service = ProjectKnowledgeService(
        tmp_path / "knowledge.db",
        embedding_adapter=adapter,
        chunk_chars=128,
        overlap_chars=0,
    )
    content = ("alpha section sentence. " * 6) + "\n\n" + ("beta section sentence. " * 6)
    created = service.import_document(
        project_id="p1", source_id="guide", title="Guide", content=content
    )
    first_embedded = sum(len(texts) for purpose, texts in adapter.calls if purpose == "document")

    unchanged = service.import_document(
        project_id="p1", source_id="guide", title="Guide", content=content
    )
    after_noop = sum(len(texts) for purpose, texts in adapter.calls if purpose == "document")
    updated = service.import_document(
        project_id="p1",
        source_id="guide",
        title="Guide",
        content=content + "\n\nnew independent ending " * 8,
    )
    after_update = sum(len(texts) for purpose, texts in adapter.calls if purpose == "document")

    assert unchanged["status"] == "unchanged"
    assert unchanged["embedded_chunk_count"] == 0
    assert after_noop == first_embedded == created["chunk_count"]
    assert updated["status"] == "updated"
    assert updated["reused_chunk_count"] >= 1
    assert after_update - after_noop == updated["embedded_chunk_count"]


def test_replaceable_embedding_and_reranker_control_order(tmp_path):
    adapter = RecordingEmbedding()
    service = ProjectKnowledgeService(
        tmp_path / "knowledge.db",
        embedding_adapter=adapter,
        reranker=ReverseReranker(),
        chunk_chars=128,
        overlap_chars=0,
    )
    service.import_document(
        project_id="p1",
        source_id="mixed",
        title="Mixed",
        content=("alpha " * 30) + "\n\n" + ("beta " * 30),
    )

    results = service.retrieve(project_id="p1", query="alpha", top_k=2, candidate_limit=2)

    assert len(results) == 2
    assert results[0]["rerank_score"] > results[1]["rerank_score"]
    assert any(purpose == "query" for purpose, _ in adapter.calls)


def test_delete_is_scoped_and_cascades_chunks(service):
    left = service.import_document(
        project_id="left", source_id="same", title="Left", content="left only " * 30
    )
    right = service.import_document(
        project_id="right", source_id="same", title="Right", content="right only " * 30
    )

    assert service.delete_document(project_id="left", document_id=right["document_id"]) is False
    assert service.delete_document(project_id="left", document_id=left["document_id"]) is True
    assert service.retrieve(project_id="left", query="left") == []
    assert service.retrieve(project_id="right", query="right")

    with sqlite3.connect(service.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_chunks WHERE project_id = 'left'"
        ).fetchone()[0] == 0


def test_lists_only_documents_in_requested_project(service):
    service.import_document(
        project_id="p1", source_id="one", title="One", content="one " * 40,
        metadata={"language": "zh-TW"},
    )
    service.import_document(
        project_id="p2", source_id="two", title="Two", content="two " * 40
    )

    listed = service.list_documents(project_id="p1")

    assert len(listed) == 1
    assert listed[0]["project_id"] == "p1"
    assert listed[0]["source_id"] == "one"
    assert listed[0]["metadata"] == {"language": "zh-TW"}


@pytest.mark.parametrize(
    "operation",
    [
        lambda service: service.import_document(
            project_id="../escape", source_id="x", title="x", content="content"
        ),
        lambda service: service.retrieve(project_id="p1", query="", top_k=1),
        lambda service: service.retrieve(project_id="p1", query="x", top_k=21),
        lambda service: service.retrieve(
            project_id="p1", query="x", top_k=5, candidate_limit=4
        ),
    ],
)
def test_invalid_scope_and_query_limits_fail_closed(service, operation):
    with pytest.raises(ProjectKnowledgeError):
        operation(service)


def test_document_and_metadata_limits_are_enforced(service):
    with pytest.raises(ProjectKnowledgeError) as document_error:
        service.import_document(
            project_id="p1",
            source_id="large",
            title="Large",
            content="x" * (MAX_DOCUMENT_BYTES + 1),
        )
    assert document_error.value.status_code == 413

    with pytest.raises(ProjectKnowledgeError) as metadata_error:
        service.import_document(
            project_id="p1",
            source_id="metadata",
            title="Metadata",
            content="valid",
            metadata={"value": "x" * (40 * 1024)},
        )
    assert metadata_error.value.status_code == 413


def test_invalid_adapter_output_never_mutates_index(tmp_path):
    class BrokenEmbedding:
        adapter_id = "broken-v1"

        def embed(self, texts, *, purpose):
            return [[float("nan")]] * len(texts)

    service = ProjectKnowledgeService(
        tmp_path / "knowledge.db",
        embedding_adapter=BrokenEmbedding(),
        chunk_chars=128,
        overlap_chars=0,
    )

    with pytest.raises(KnowledgeAdapterError):
        service.import_document(
            project_id="p1", source_id="broken", title="Broken", content="text " * 40
        )
    assert service.list_documents(project_id="p1") == []


def test_batch_validation_and_embedding_failures_never_partially_commit(tmp_path):
    service = ProjectKnowledgeService(
        tmp_path / "knowledge.db", chunk_chars=128, overlap_chars=0
    )
    with pytest.raises(ProjectKnowledgeError) as validation_error:
        service.import_documents(
            project_id="p1",
            documents=(
                {"source_id": "valid", "title": "Valid", "content": "valid text"},
                {"source_id": "invalid", "title": "Invalid", "content": ""},
            ),
        )
    assert validation_error.value.code == "INVALID_KNOWLEDGE_DOCUMENT"
    assert service.list_documents(project_id="p1") == []

    class RejectingEmbedding:
        adapter_id = "rejecting-v1"

        def embed(self, texts, *, purpose):
            raise RuntimeError("adapter unavailable")

    failing = ProjectKnowledgeService(
        tmp_path / "failing.db",
        embedding_adapter=RejectingEmbedding(),
        chunk_chars=128,
        overlap_chars=0,
    )
    with pytest.raises(KnowledgeAdapterError):
        failing.import_documents(
            project_id="p1",
            documents=(
                {"source_id": "one", "title": "One", "content": "first document"},
                {"source_id": "two", "title": "Two", "content": "second document"},
            ),
        )
    assert failing.list_documents(project_id="p1") == []


def test_batch_database_failure_rolls_back_all_documents(tmp_path):
    service = ProjectKnowledgeService(
        tmp_path / "knowledge.db", chunk_chars=128, overlap_chars=0
    )
    with sqlite3.connect(service.database_path) as connection:
        connection.execute(
            """CREATE TRIGGER reject_second_batch_chunk
               BEFORE INSERT ON knowledge_chunks
               WHEN NEW.text LIKE '%explode%'
               BEGIN
                   SELECT RAISE(ABORT, 'forced batch failure');
               END"""
        )

    with pytest.raises(ProjectKnowledgeError) as exc_info:
        service.import_documents(
            project_id="p1",
            documents=(
                {"source_id": "one", "title": "One", "content": "safe content"},
                {"source_id": "two", "title": "Two", "content": "explode content"},
            ),
        )

    assert exc_info.value.code == "KNOWLEDGE_IMPORT_FAILED"
    assert service.list_documents(project_id="p1") == []
    assert service.status(project_id="p1")["chunk_count"] == 0


def test_project_chunk_limit_blocks_before_embedding_and_remains_retrievable(tmp_path):
    adapter = RecordingEmbedding()
    service = ProjectKnowledgeService(
        tmp_path / "knowledge.db",
        embedding_adapter=adapter,
        chunk_chars=128,
        overlap_chars=0,
        max_project_chunks=3,
    )
    created = service.import_document(
        project_id="p1", source_id="at-limit", title="At limit", content="alpha" * 60
    )
    assert created["chunk_count"] == 3

    status = service.status(project_id="p1")
    assert status["index_status"] == "ready"
    assert status["health_status"] == "degraded"
    assert status["limit_status"] == "reached"
    assert status["remaining_chunk_count"] == 0
    assert status["max_chunk_count"] == 3
    assert service.retrieve(project_id="p1", query="alpha", top_k=1)

    embedded_before = sum(
        len(texts) for purpose, texts in adapter.calls if purpose == "document"
    )
    with pytest.raises(ProjectKnowledgeError) as exc_info:
        service.import_document(
            project_id="p1", source_id="overflow", title="Overflow", content="beta"
        )
    assert exc_info.value.code == "KNOWLEDGE_PROJECT_CHUNK_LIMIT"
    embedded_after = sum(
        len(texts) for purpose, texts in adapter.calls if purpose == "document"
    )
    assert embedded_after == embedded_before
    assert [item["source_id"] for item in service.list_documents(project_id="p1")] == [
        "at-limit"
    ]


def test_embedding_configuration_change_forces_safe_reindex(tmp_path):
    database_path = tmp_path / "knowledge.db"
    first = ProjectKnowledgeService(
        database_path,
        embedding_adapter=DeterministicLocalEmbedding(64),
        chunk_chars=128,
        overlap_chars=0,
    )
    created = first.import_document(
        project_id="p1", source_id="guide", title="Guide", content="alpha " * 50
    )
    second = ProjectKnowledgeService(
        database_path,
        embedding_adapter=DeterministicLocalEmbedding(128),
        chunk_chars=128,
        overlap_chars=0,
    )

    stale_status = second.status(project_id="p1")
    assert stale_status["chunk_count"] == created["chunk_count"]
    assert stale_status["current_adapter_chunk_count"] == 0
    assert stale_status["reindex_required"] is True
    assert stale_status["index_status"] == "degraded"
    assert second.retrieve(project_id="p1", query="alpha") == []

    reindexed = second.import_document(
        project_id="p1", source_id="guide", title="Guide", content="alpha " * 50
    )

    assert reindexed["status"] == "updated"
    assert reindexed["embedded_chunk_count"] == created["chunk_count"]
    assert second.retrieve(project_id="p1", query="alpha")
    rebuilt_status = second.status(project_id="p1")
    assert rebuilt_status["current_adapter_chunk_count"] == created["chunk_count"]
    assert rebuilt_status["reindex_required"] is False
    assert rebuilt_status["index_status"] == "ready"


def test_runtime_adapter_switch_is_atomic_and_marks_existing_vectors_stale(tmp_path):
    service = ProjectKnowledgeService(
        tmp_path / "knowledge.db",
        embedding_adapter=DeterministicLocalEmbedding(64),
        chunk_chars=128,
        overlap_chars=0,
    )
    created = service.import_document(
        project_id="p1", source_id="guide", title="Guide", content="alpha " * 30
    )

    service.configure_adapters(
        embedding_adapter=DeterministicLocalEmbedding(128),
        reranker=None,
    )

    status = service.status(project_id="p1")
    assert status["chunk_count"] == created["chunk_count"]
    assert status["current_adapter_chunk_count"] == 0
    assert status["reindex_required"] is True


def test_embedding_batches_and_reranker_payload_are_bounded(tmp_path):
    adapter = RecordingEmbedding()

    class MeasuringReranker:
        adapter_id = "measuring-v1"

        def __init__(self):
            self.payload_bytes = 0

        def rerank(self, query, candidates):
            self.payload_bytes = len(query.encode("utf-8")) + sum(
                len(candidate["text"].encode("utf-8")) for candidate in candidates
            )
            return [1.0] * len(candidates)

    reranker = MeasuringReranker()
    service = ProjectKnowledgeService(
        tmp_path / "knowledge.db",
        embedding_adapter=adapter,
        reranker=reranker,
        chunk_chars=128,
        overlap_chars=0,
    )
    service.import_document(
        project_id="p1",
        source_id="large",
        title="Large",
        content=("alpha bounded adapter request. " * 400),
    )
    service.retrieve(project_id="p1", query="alpha", top_k=10, candidate_limit=100)

    document_calls = [texts for purpose, texts in adapter.calls if purpose == "document"]
    assert all(len(texts) <= MAX_EMBEDDING_BATCH_TEXTS for texts in document_calls)
    assert all(
        sum(len(text.encode("utf-8")) for text in texts) <= MAX_EMBEDDING_BATCH_BYTES
        for texts in document_calls
    )
    assert reranker.payload_bytes <= MAX_RERANK_INPUT_BYTES


def test_database_never_contains_cross_project_query_filter_shortcuts(service):
    service.import_document(
        project_id="p1", source_id="one", title="One", content="shared keyword " * 30
    )
    service.import_document(
        project_id="p2", source_id="two", title="Two", content="shared keyword " * 30
    )

    p1_ids = {result["citation"]["document_id"] for result in service.retrieve(
        project_id="p1", query="shared keyword", top_k=5
    )}
    p2_ids = {result["citation"]["document_id"] for result in service.retrieve(
        project_id="p2", query="shared keyword", top_k=5
    )}

    assert p1_ids
    assert p2_ids
    assert p1_ids.isdisjoint(p2_ids)
    with sqlite3.connect(service.database_path) as connection:
        projects = {
            row[0] for row in connection.execute("SELECT DISTINCT project_id FROM knowledge_chunks")
        }
    assert projects == {"p1", "p2"}
