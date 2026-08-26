"""Project-scoped, provider-neutral knowledge retrieval primitives.

The module intentionally owns an additive SQLite database so it can be wired
into the Workbench without changing the legacy database schema.  It provides a
small but complete ingestion and retrieval boundary:

* every document and chunk is keyed by project;
* imports are content-addressed and unchanged documents are no-ops;
* embeddings and reranking are adapter interfaces;
* a deterministic local embedding is always available offline; and
* retrieval returns bounded text together with stable citation information.

This is an index, not an attachment store.  Callers remain responsible for
retaining the original document and enforcing any project/file permissions
before passing text to :meth:`ProjectKnowledgeService.import_document`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterable, Mapping, Protocol, Sequence

try:  # Package imports in tests; top-level imports in the packaged backend.
    from .semantic_retrieval import SemanticRequestContext, SemanticRetrievalError
except ImportError:  # pragma: no cover - exercised by the desktop entrypoint
    from semantic_retrieval import SemanticRequestContext, SemanticRetrievalError


MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 32 * 1024
MAX_QUERY_CHARS = 8 * 1024
MAX_CHUNKS_PER_DOCUMENT = 10_000
MAX_SCAN_CHUNKS = 20_000
MAX_IMPORT_DOCUMENTS = 100
MAX_TOP_K = 20
MAX_CANDIDATES = 100
MAX_RESULT_TEXT_BYTES = 16 * 1024
MAX_EMBEDDING_DIMENSION = 4096
MAX_EMBEDDING_BATCH_TEXTS = 64
MAX_EMBEDDING_BATCH_BYTES = 256 * 1024
MAX_RERANK_INPUT_BYTES = 64 * 1024
DEFAULT_CHUNK_CHARS = 1200
DEFAULT_CHUNK_OVERLAP = 160

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_INLINE_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TOKEN = re.compile(r"[\w-]+", re.UNICODE)
_SPLIT_MARKERS = ("\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", "; ", "；", "，", ", ", " ")


class ProjectKnowledgeError(ValueError):
    """A stable service error suitable for translation at an API boundary."""

    code = "PROJECT_KNOWLEDGE_ERROR"
    status_code = 400

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class KnowledgeNotFoundError(ProjectKnowledgeError):
    code = "KNOWLEDGE_DOCUMENT_NOT_FOUND"
    status_code = 404


class KnowledgeAdapterError(ProjectKnowledgeError):
    code = "KNOWLEDGE_ADAPTER_FAILED"
    status_code = 502


class EmbeddingAdapter(Protocol):
    """Replaceable embedding interface used for documents and queries."""

    adapter_id: str

    def embed(self, texts: Sequence[str], *, purpose: str) -> Sequence[Sequence[float]]:
        """Return one finite, fixed-dimension vector per input text."""


class RerankerAdapter(Protocol):
    """Optional reranker interface; scores correspond to candidate order."""

    adapter_id: str

    def rerank(
        self,
        query: str,
        candidates: Sequence[Mapping[str, Any]],
    ) -> Sequence[float]:
        """Return one finite relevance score per candidate."""


class ProjectEmbeddingAdapter(Protocol):
    """Project-aware embedding interface for governed provider adapters."""

    adapter_id: str

    def embed_for_project(
        self,
        texts: Sequence[str],
        *,
        purpose: str,
        context: SemanticRequestContext,
    ) -> Sequence[Sequence[float]]:
        """Return vectors after checking the project execution authority."""


class ProjectRerankerAdapter(Protocol):
    """Project-aware reranker interface for governed provider adapters."""

    adapter_id: str

    def rerank_for_project(
        self,
        query: str,
        candidates: Sequence[Mapping[str, Any]],
        *,
        context: SemanticRequestContext,
    ) -> Sequence[float]:
        """Return scores after checking the project execution authority."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(value: bytes | str) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def _identifier(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(result):
        raise ProjectKnowledgeError(
            f"{label} must be 1-128 letters, numbers, dots, underscores, colons, or hyphens.",
            code="INVALID_KNOWLEDGE_SCOPE",
        )
    return result


def _visible_text(value: Any, label: str, *, maximum: int) -> str:
    result = str(value or "").strip()
    if not result or len(result) > maximum or _INLINE_CONTROL.search(result):
        raise ProjectKnowledgeError(
            f"{label} must contain 1-{maximum} visible characters.",
            code="INVALID_KNOWLEDGE_DOCUMENT",
        )
    return result


def _normalize_content(value: Any) -> str:
    if not isinstance(value, str):
        raise ProjectKnowledgeError(
            "Document content must be UTF-8 text.", code="INVALID_KNOWLEDGE_DOCUMENT"
        )
    content = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content or _CONTROL.search(content):
        raise ProjectKnowledgeError(
            "Document content cannot be empty or contain unsafe control characters.",
            code="INVALID_KNOWLEDGE_DOCUMENT",
        )
    if len(content.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ProjectKnowledgeError(
            f"Document content exceeds {MAX_DOCUMENT_BYTES} bytes.",
            code="KNOWLEDGE_INPUT_TOO_LARGE",
            status_code=413,
        )
    return content


def _metadata_json(value: Mapping[str, Any] | None) -> str:
    metadata = dict(value or {})
    try:
        encoded = json.dumps(
            metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise ProjectKnowledgeError(
            "Document metadata must be JSON-compatible.",
            code="INVALID_KNOWLEDGE_DOCUMENT",
        ) from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ProjectKnowledgeError(
            f"Document metadata exceeds {MAX_METADATA_BYTES} bytes.",
            code="KNOWLEDGE_INPUT_TOO_LARGE",
            status_code=413,
        )
    return encoded


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    if maximum_bytes <= 3:
        return ""
    return encoded[: maximum_bytes - 3].decode("utf-8", errors="ignore").rstrip() + "…"


def stable_chunk_text(
    content: str,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP,
) -> list[dict[str, Any]]:
    """Split normalized text deterministically and preserve source offsets.

    Splits prefer paragraph, line, sentence, punctuation, then word boundaries.
    Chunk IDs are intentionally assigned by the service because duplicate text
    needs a document-local occurrence counter.
    """

    text = _normalize_content(content)
    maximum = int(max_chars)
    overlap = int(overlap_chars)
    if maximum < 128 or maximum > 16_384:
        raise ProjectKnowledgeError(
            "Chunk size must be between 128 and 16384 characters.",
            code="INVALID_CHUNK_POLICY",
        )
    if overlap < 0 or overlap >= maximum // 2:
        raise ProjectKnowledgeError(
            "Chunk overlap must be non-negative and less than half the chunk size.",
            code="INVALID_CHUNK_POLICY",
        )

    chunks: list[dict[str, Any]] = []
    start = 0
    length = len(text)
    while start < length:
        raw_end = min(length, start + maximum)
        end = raw_end
        if raw_end < length:
            floor = start + max(64, int(maximum * 0.55))
            for marker in _SPLIT_MARKERS:
                found = text.rfind(marker, floor, raw_end)
                if found >= floor:
                    end = found + len(marker)
                    break

        visible_start = start
        visible_end = end
        while visible_start < visible_end and text[visible_start].isspace():
            visible_start += 1
        while visible_end > visible_start and text[visible_end - 1].isspace():
            visible_end -= 1
        if visible_start < visible_end:
            chunk_text = text[visible_start:visible_end]
            chunks.append(
                {
                    "ordinal": len(chunks),
                    "start_offset": visible_start,
                    "end_offset": visible_end,
                    "text": chunk_text,
                    "content_sha256": _sha256(chunk_text),
                }
            )
        if end >= length:
            break
        next_start = max(start + 1, end - overlap)
        # Prefer beginning the overlap at a boundary without ever skipping
        # beyond the non-overlapped end of the previous chunk.
        boundary_limit = end
        boundary = -1
        for marker in reversed(_SPLIT_MARKERS):
            found = text.find(marker, next_start, boundary_limit)
            if found != -1:
                boundary = found + len(marker)
                break
        start = boundary if next_start < boundary <= boundary_limit else next_start

    if len(chunks) > MAX_CHUNKS_PER_DOCUMENT:
        raise ProjectKnowledgeError(
            f"Document produces more than {MAX_CHUNKS_PER_DOCUMENT} chunks.",
            code="KNOWLEDGE_INPUT_TOO_LARGE",
            status_code=413,
        )
    return chunks


class DeterministicLocalEmbedding:
    """Small offline hashed-feature embedding with reproducible output.

    It is deliberately not presented as a semantic foundation model.  It gives
    the knowledge service a private, dependency-free lexical fallback and a
    stable adapter contract until a governed provider embedding is selected.
    """

    def __init__(self, dimension: int = 256) -> None:
        if dimension < 64 or dimension > MAX_EMBEDDING_DIMENSION:
            raise ValueError("dimension must be between 64 and 4096")
        self.dimension = int(dimension)
        # Configuration is part of the identity so changing dimensions causes
        # an explicit incremental reindex instead of silently mixing vectors.
        self.adapter_id = f"local-hash-embedding-v1-d{self.dimension}"

    @staticmethod
    def _features(text: str) -> Iterable[str]:
        normalized = " ".join(str(text).casefold().split())
        yield from _TOKEN.findall(normalized)
        compact = "".join(character for character in normalized if not character.isspace())
        # Character n-grams make the fallback usable for scripts that do not
        # consistently separate words (including Traditional Chinese).
        for size in (2, 3):
            for index in range(max(0, len(compact) - size + 1)):
                yield f"c{size}:{compact[index:index + size]}"

    def embed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        if purpose not in {"document", "query"}:
            raise ValueError("purpose must be document or query")
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            for feature in self._features(str(text)):
                digest = hashlib.sha256(feature.encode("utf-8")).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimension
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[index] += sign
            magnitude = math.sqrt(sum(value * value for value in vector))
            vectors.append(
                [value / magnitude for value in vector] if magnitude else vector
            )
        return vectors


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


class ProjectKnowledgeService:
    """Durable project-scoped document index and bounded retriever."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        embedding_adapter: EmbeddingAdapter | None = None,
        reranker: RerankerAdapter | None = None,
        connection_factory: Callable[[], ContextManager[Any]] | None = None,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        overlap_chars: int = DEFAULT_CHUNK_OVERLAP,
        max_project_chunks: int = MAX_SCAN_CHUNKS,
        reranker_fail_open: bool = True,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding_adapter = embedding_adapter or DeterministicLocalEmbedding()
        self.reranker = reranker
        self.connection_factory = connection_factory
        self.chunk_chars = int(chunk_chars)
        self.overlap_chars = int(overlap_chars)
        self.max_project_chunks = int(max_project_chunks)
        self.reranker_fail_open = bool(reranker_fail_open)
        if self.max_project_chunks < 1 or self.max_project_chunks > MAX_SCAN_CHUNKS:
            raise ProjectKnowledgeError(
                f"Project chunk limit must be between 1 and {MAX_SCAN_CHUNKS}.",
                code="INVALID_CHUNK_POLICY",
            )
        # Validate the chunk policy even before the first document is imported.
        stable_chunk_text("validation", max_chars=self.chunk_chars, overlap_chars=self.overlap_chars)
        self._lock = threading.RLock()
        self._deleting_projects: set[str] = set()
        self._schema_ready = False
        self.ensure_schema()

    @contextmanager
    def project_delete_guard(self, *, project_id: str) -> Iterable[None]:
        """Serialize final import/cleanup/delete and reject writes after deletion.

        The caller must keep this context open until the authoritative Project
        metadata row is deleted.  An import that started first finishes before
        cleanup; a later import sees the tombstone and fails closed.
        """

        project = _identifier(project_id, "Project ID")
        with self._lock:
            if project in self._deleting_projects:
                raise ProjectKnowledgeError(
                    "Project knowledge is being deleted.",
                    code="KNOWLEDGE_PROJECT_DELETING",
                    status_code=409,
                )
            self._deleting_projects.add(project)
            try:
                yield
            except Exception:
                # Metadata still exists when the coordinated deletion fails,
                # so allow a repaired operation to retry later.
                self._deleting_projects.discard(project)
                raise

    @contextmanager
    def _connection(self) -> Iterable[Any]:
        if self.connection_factory is not None:
            with self.connection_factory() as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                yield connection
            return
        connection = sqlite3.connect(str(self.database_path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            # Foreign-key enforcement is connection-local in SQLite.  Keeping
            # this here (rather than only in schema setup) guarantees scoped
            # document deletes cannot leave retrievable orphan chunks.
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    project_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    content_bytes INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    embedding_adapter_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, document_id),
                    UNIQUE(project_id, source_id)
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_documents_project
                    ON knowledge_documents(project_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    project_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    start_offset INTEGER NOT NULL,
                    end_offset INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    text TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    embedding_dimension INTEGER NOT NULL,
                    embedding_adapter_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, document_id, chunk_id),
                    UNIQUE(project_id, document_id, ordinal),
                    FOREIGN KEY(project_id, document_id)
                        REFERENCES knowledge_documents(project_id, document_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_project
                    ON knowledge_chunks(project_id, document_id, ordinal);
                """
            )
            self._schema_ready = True

    @property
    def embedding_adapter_id(self) -> str:
        return self._validated_adapter_id(
            self.embedding_adapter, label="Embedding adapter"
        )

    @staticmethod
    def _validated_adapter_id(adapter: Any, *, label: str) -> str:
        value = str(getattr(adapter, "adapter_id", "") or "").strip()
        if not value or len(value) > 128 or _INLINE_CONTROL.search(value):
            raise KnowledgeAdapterError(f"{label} has an invalid adapter_id.")
        return value

    def configure_adapters(
        self,
        *,
        embedding_adapter: EmbeddingAdapter | None,
        reranker: RerankerAdapter | None,
    ) -> None:
        """Atomically apply future semantic calls without rewriting old vectors."""

        selected_embedding = embedding_adapter or DeterministicLocalEmbedding()
        self._validated_adapter_id(selected_embedding, label="Embedding adapter")
        if reranker is not None:
            self._validated_adapter_id(reranker, label="Reranker adapter")
        with self._lock:
            self.embedding_adapter = selected_embedding
            self.reranker = reranker

    def configure_chunking(self, *, chunk_chars: int, overlap_chars: int) -> None:
        """Apply the policy used by future imports without rewriting old evidence."""

        maximum = int(chunk_chars)
        overlap = int(overlap_chars)
        stable_chunk_text(
            "configuration validation",
            max_chars=maximum,
            overlap_chars=overlap,
        )
        with self._lock:
            self.chunk_chars = maximum
            self.overlap_chars = overlap

    def _embed(
        self,
        texts: Sequence[str],
        *,
        purpose: str,
        context: SemanticRequestContext,
        adapter: Any | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []
        normalized: list[list[float]] = []
        dimension: int | None = None
        pending: list[str] = []
        pending_bytes = 0
        selected_adapter = adapter or self.embedding_adapter

        def flush() -> None:
            nonlocal dimension, pending, pending_bytes
            if not pending:
                return
            try:
                contextual = getattr(selected_adapter, "embed_for_project", None)
                if callable(contextual):
                    raw = contextual(
                        list(pending),
                        purpose=purpose,
                        context=context,
                    )
                else:
                    raw = selected_adapter.embed(list(pending), purpose=purpose)
                vectors = list(raw)
            except ProjectKnowledgeError:
                raise
            except SemanticRetrievalError as exc:
                wrapped = KnowledgeAdapterError(
                    str(exc),
                    code=exc.code,
                    status_code=exc.status_code,
                )
                for name in ("provider_id", "model_reference"):
                    value = str(getattr(exc, name, "") or "").strip()
                    if value:
                        setattr(wrapped, name, value)
                raise wrapped from exc
            except Exception as exc:
                raise KnowledgeAdapterError("Embedding adapter failed.") from exc
            if len(vectors) != len(pending):
                raise KnowledgeAdapterError("Embedding adapter returned the wrong vector count.")
            for raw_vector in vectors:
                try:
                    vector = [float(value) for value in raw_vector]
                except (TypeError, ValueError, OverflowError) as exc:
                    raise KnowledgeAdapterError("Embedding adapter returned an invalid vector.") from exc
                if not vector or len(vector) > MAX_EMBEDDING_DIMENSION:
                    raise KnowledgeAdapterError("Embedding vector dimension is invalid.")
                if not all(math.isfinite(value) for value in vector):
                    raise KnowledgeAdapterError("Embedding vector contains non-finite values.")
                if dimension is None:
                    dimension = len(vector)
                elif len(vector) != dimension:
                    raise KnowledgeAdapterError("Embedding vectors have inconsistent dimensions.")
                normalized.append(vector)
            pending = []
            pending_bytes = 0

        for text in texts:
            value = str(text)
            value_bytes = len(value.encode("utf-8"))
            if value_bytes > MAX_EMBEDDING_BATCH_BYTES:
                raise ProjectKnowledgeError(
                    "A single embedding input exceeds the adapter request limit.",
                    code="KNOWLEDGE_INPUT_TOO_LARGE",
                    status_code=413,
                )
            if pending and (
                len(pending) >= MAX_EMBEDDING_BATCH_TEXTS
                or pending_bytes + value_bytes > MAX_EMBEDDING_BATCH_BYTES
            ):
                flush()
            pending.append(value)
            pending_bytes += value_bytes
        flush()
        return normalized

    @staticmethod
    def _document_id(project_id: str, source_id: str) -> str:
        digest = _sha256(f"{project_id}\x00{source_id}")[:24]
        return f"doc_{digest}"

    @staticmethod
    def _chunk_ids(document_id: str, chunks: Sequence[Mapping[str, Any]]) -> list[str]:
        occurrences: dict[str, int] = {}
        result: list[str] = []
        for chunk in chunks:
            digest = str(chunk["content_sha256"])
            occurrence = occurrences.get(digest, 0)
            occurrences[digest] = occurrence + 1
            result.append(_sha256(f"{document_id}\x00{digest}\x00{occurrence}")[:32])
        return result

    def import_document(
        self,
        *,
        project_id: str,
        source_id: str,
        title: str,
        content: str,
        metadata: Mapping[str, Any] | None = None,
        document_id: str | None = None,
        run_id: str = "",
        consent_proposal_id: str = "",
        requested_model: str = "",
        budget_override_id: str = "",
    ) -> dict[str, Any]:
        return self.import_documents(
            project_id=project_id,
            run_id=run_id,
            consent_proposal_id=consent_proposal_id,
            requested_model=requested_model,
            budget_override_id=budget_override_id,
            documents=(
                {
                    "source_id": source_id,
                    "title": title,
                    "content": content,
                    "metadata": metadata,
                    "document_id": document_id,
                },
            ),
        )[0]

    @staticmethod
    def _document_snapshot(row: Any | None) -> tuple[Any, ...] | None:
        if row is None:
            return None
        keys = (
            "document_id",
            "source_id",
            "title",
            "content_sha256",
            "content_bytes",
            "metadata_json",
            "embedding_adapter_id",
            "created_at",
            "updated_at",
        )
        return tuple(row[key] for key in keys)

    @staticmethod
    def _cached_vector(row: Mapping[str, Any], *, adapter_id: str) -> list[float] | None:
        if str(row.get("embedding_adapter_id") or "") != adapter_id:
            return None
        try:
            vector = [float(value) for value in json.loads(str(row["embedding_json"]))]
            recorded_dimension = int(row["embedding_dimension"])
        except (KeyError, TypeError, ValueError, OverflowError, json.JSONDecodeError):
            return None
        if (
            not vector
            or len(vector) != recorded_dimension
            or len(vector) > MAX_EMBEDDING_DIMENSION
            or not all(math.isfinite(value) for value in vector)
        ):
            return None
        return vector

    def import_documents(
        self,
        *,
        project_id: str,
        documents: Sequence[Mapping[str, Any]],
        run_id: str = "",
        consent_proposal_id: str = "",
        requested_model: str = "",
        budget_override_id: str = "",
    ) -> list[dict[str, Any]]:
        """Validate, embed, and commit a document batch atomically.

        No database mutation occurs until every document has passed validation,
        chunking, project-capacity checks, and embedding.  A second capacity and
        optimistic-concurrency check runs inside the write transaction so a
        concurrent writer cannot make a previously safe batch exceed the limit.
        """

        project = _identifier(project_id, "Project ID")
        semantic_context = SemanticRequestContext(
            project_id=project,
            run_id=run_id,
            consent_proposal_id=consent_proposal_id,
            requested_model=requested_model,
            budget_override_id=budget_override_id,
        )
        if isinstance(documents, (str, bytes)):
            documents = ()
        items = list(documents)
        if not items or len(items) > MAX_IMPORT_DOCUMENTS:
            raise ProjectKnowledgeError(
                f"A batch must contain 1-{MAX_IMPORT_DOCUMENTS} documents.",
                code="INVALID_KNOWLEDGE_DOCUMENT",
            )

        adapter_id = self.embedding_adapter_id
        prepared: list[dict[str, Any]] = []
        seen_sources: set[str] = set()
        seen_document_ids: set[str] = set()
        for item in items:
            if not isinstance(item, Mapping):
                raise ProjectKnowledgeError(
                    "Each batch item must be a document mapping.",
                    code="INVALID_KNOWLEDGE_DOCUMENT",
                )
            source = _visible_text(item.get("source_id"), "Source ID", maximum=512)
            heading = _visible_text(item.get("title"), "Document title", maximum=512)
            normalized = _normalize_content(item.get("content"))
            metadata = item.get("metadata")
            if metadata is not None and not isinstance(metadata, Mapping):
                raise ProjectKnowledgeError(
                    "Document metadata must be an object.",
                    code="INVALID_KNOWLEDGE_DOCUMENT",
                )
            metadata_encoded = _metadata_json(metadata)
            requested_document_id = item.get("document_id")
            doc_id = (
                _identifier(requested_document_id, "Document ID")
                if requested_document_id is not None
                else self._document_id(project, source)
            )
            if source in seen_sources or doc_id in seen_document_ids:
                raise ProjectKnowledgeError(
                    "A batch cannot contain duplicate source or document IDs.",
                    code="KNOWLEDGE_BATCH_CONFLICT",
                    status_code=409,
                )
            seen_sources.add(source)
            seen_document_ids.add(doc_id)
            chunks = stable_chunk_text(
                normalized,
                max_chars=self.chunk_chars,
                overlap_chars=self.overlap_chars,
            )
            prepared.append(
                {
                    "source_id": source,
                    "title": heading,
                    "content": normalized,
                    "content_sha256": _sha256(normalized),
                    "content_bytes": len(normalized.encode("utf-8")),
                    "metadata_json": metadata_encoded,
                    "document_id": doc_id,
                    "chunks": chunks,
                    "chunk_ids": self._chunk_ids(doc_id, chunks),
                }
            )

        with self._lock:
            if project in self._deleting_projects:
                raise ProjectKnowledgeError(
                    "Project knowledge is being deleted.",
                    code="KNOWLEDGE_PROJECT_DELETING",
                    status_code=409,
                )
            with self._connection() as connection:
                current_total = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM knowledge_chunks WHERE project_id = ?",
                        (project,),
                    ).fetchone()[0]
                )
                replaced_chunk_count = 0
                for plan in prepared:
                    source_owner = connection.execute(
                        """SELECT document_id FROM knowledge_documents
                           WHERE project_id = ? AND source_id = ?""",
                        (project, plan["source_id"]),
                    ).fetchone()
                    if source_owner and str(source_owner["document_id"]) != plan["document_id"]:
                        raise ProjectKnowledgeError(
                            "Source ID already belongs to another document in this project.",
                            code="KNOWLEDGE_SOURCE_CONFLICT",
                            status_code=409,
                        )
                    existing_document = connection.execute(
                        """SELECT * FROM knowledge_documents
                           WHERE project_id = ? AND document_id = ?""",
                        (project, plan["document_id"]),
                    ).fetchone()
                    existing_chunks = {
                        str(row["chunk_id"]): dict(row)
                        for row in connection.execute(
                            """SELECT * FROM knowledge_chunks
                               WHERE project_id = ? AND document_id = ?""",
                            (project, plan["document_id"]),
                        ).fetchall()
                    }
                    plan["existing_document"] = dict(existing_document) if existing_document else None
                    plan["snapshot"] = self._document_snapshot(existing_document)
                    plan["existing_chunks"] = existing_chunks
                    replaced_chunk_count += len(existing_chunks)

                projected_total = (
                    current_total
                    - replaced_chunk_count
                    + sum(len(plan["chunks"]) for plan in prepared)
                )
                if projected_total > self.max_project_chunks:
                    raise ProjectKnowledgeError(
                        "Project knowledge import would exceed the bounded chunk limit.",
                        code="KNOWLEDGE_PROJECT_CHUNK_LIMIT",
                        status_code=409,
                    )

            missing: list[tuple[dict[str, Any], int]] = []
            for plan in prepared:
                cached_vectors: dict[int, list[float]] = {}
                for index, chunk_id in enumerate(plan["chunk_ids"]):
                    previous = plan["existing_chunks"].get(chunk_id)
                    vector = (
                        self._cached_vector(previous, adapter_id=adapter_id)
                        if previous is not None
                        else None
                    )
                    if vector is None:
                        missing.append((plan, index))
                    else:
                        cached_vectors[index] = vector
                plan["vectors"] = cached_vectors

            embedded_vectors = self._embed(
                [str(plan["chunks"][index]["text"]) for plan, index in missing],
                purpose="document",
                context=semantic_context,
            )
            for (plan, index), vector in zip(missing, embedded_vectors):
                plan["vectors"][index] = vector

            dimensions = {
                len(vector)
                for plan in prepared
                for vector in plan["vectors"].values()
            }
            if len(dimensions) != 1:
                all_positions = [
                    (plan, index)
                    for plan in prepared
                    for index in range(len(plan["chunks"]))
                ]
                all_vectors = self._embed(
                    [str(plan["chunks"][index]["text"]) for plan, index in all_positions],
                    purpose="document",
                    context=semantic_context,
                )
                for plan in prepared:
                    plan["vectors"] = {}
                for (plan, index), vector in zip(all_positions, all_vectors):
                    plan["vectors"][index] = vector
                missing = all_positions

            missing_positions_by_plan: dict[int, set[int]] = {
                id(plan): set() for plan in prepared
            }
            for item_plan, position in missing:
                missing_positions_by_plan[id(item_plan)].add(position)

            now = _iso_now()
            try:
                with self._connection() as connection:
                    if not bool(getattr(connection, "in_transaction", False)):
                        connection.execute("BEGIN IMMEDIATE")
                    current_total = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM knowledge_chunks WHERE project_id = ?",
                            (project,),
                        ).fetchone()[0]
                    )
                    replaced_chunk_count = 0
                    for plan in prepared:
                        source_owner = connection.execute(
                            """SELECT document_id FROM knowledge_documents
                               WHERE project_id = ? AND source_id = ?""",
                            (project, plan["source_id"]),
                        ).fetchone()
                        if source_owner and str(source_owner["document_id"]) != plan["document_id"]:
                            raise ProjectKnowledgeError(
                                "Source ID changed while the batch was being prepared.",
                                code="KNOWLEDGE_IMPORT_CONFLICT",
                                status_code=409,
                            )
                        current_document = connection.execute(
                            """SELECT * FROM knowledge_documents
                               WHERE project_id = ? AND document_id = ?""",
                            (project, plan["document_id"]),
                        ).fetchone()
                        if self._document_snapshot(current_document) != plan["snapshot"]:
                            raise ProjectKnowledgeError(
                                "Document changed while the batch was being prepared.",
                                code="KNOWLEDGE_IMPORT_CONFLICT",
                                status_code=409,
                            )
                        replaced_chunk_count += int(
                            connection.execute(
                                """SELECT COUNT(*) FROM knowledge_chunks
                                   WHERE project_id = ? AND document_id = ?""",
                                (project, plan["document_id"]),
                            ).fetchone()[0]
                        )
                    projected_total = (
                        current_total
                        - replaced_chunk_count
                        + sum(len(plan["chunks"]) for plan in prepared)
                    )
                    if projected_total > self.max_project_chunks:
                        raise ProjectKnowledgeError(
                            "Project knowledge import would exceed the bounded chunk limit.",
                            code="KNOWLEDGE_PROJECT_CHUNK_LIMIT",
                            status_code=409,
                        )

                    for plan in prepared:
                        existing_document = plan["existing_document"]
                        unchanged = bool(
                            existing_document
                            and existing_document["content_sha256"] == plan["content_sha256"]
                            and existing_document["embedding_adapter_id"] == adapter_id
                            and existing_document["source_id"] == plan["source_id"]
                            and existing_document["title"] == plan["title"]
                            and existing_document["metadata_json"] == plan["metadata_json"]
                            and len(plan["existing_chunks"]) == len(plan["chunks"])
                            and not missing_positions_by_plan[id(plan)]
                        )
                        plan["unchanged"] = unchanged
                        if unchanged:
                            continue
                        if existing_document:
                            connection.execute(
                                """UPDATE knowledge_documents
                                   SET source_id = ?, title = ?, content_sha256 = ?,
                                       content_bytes = ?, metadata_json = ?,
                                       embedding_adapter_id = ?, updated_at = ?
                                   WHERE project_id = ? AND document_id = ?""",
                                (
                                    plan["source_id"], plan["title"], plan["content_sha256"],
                                    plan["content_bytes"], plan["metadata_json"], adapter_id,
                                    now, project, plan["document_id"],
                                ),
                            )
                        else:
                            connection.execute(
                                """INSERT INTO knowledge_documents (
                                       project_id, document_id, source_id, title,
                                       content_sha256, content_bytes, metadata_json,
                                       embedding_adapter_id, created_at, updated_at
                                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    project, plan["document_id"], plan["source_id"], plan["title"],
                                    plan["content_sha256"], plan["content_bytes"],
                                    plan["metadata_json"], adapter_id, now, now,
                                ),
                            )
                        connection.execute(
                            "DELETE FROM knowledge_chunks WHERE project_id = ? AND document_id = ?",
                            (project, plan["document_id"]),
                        )
                        for index, (chunk, chunk_id) in enumerate(
                            zip(plan["chunks"], plan["chunk_ids"])
                        ):
                            previous = plan["existing_chunks"].get(chunk_id)
                            created_at = (
                                str(previous["created_at"])
                                if previous is not None
                                and index not in missing_positions_by_plan[id(plan)]
                                else now
                            )
                            vector = plan["vectors"][index]
                            connection.execute(
                                """INSERT INTO knowledge_chunks (
                                       project_id, document_id, chunk_id, ordinal,
                                       start_offset, end_offset, content_sha256, text,
                                       embedding_json, embedding_dimension,
                                       embedding_adapter_id, created_at, updated_at
                                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    project, plan["document_id"], chunk_id, int(chunk["ordinal"]),
                                    int(chunk["start_offset"]), int(chunk["end_offset"]),
                                    str(chunk["content_sha256"]), str(chunk["text"]),
                                    json.dumps(vector, separators=(",", ":")), len(vector),
                                    adapter_id, created_at, now,
                                ),
                            )
            except ProjectKnowledgeError:
                raise
            except sqlite3.DatabaseError as exc:
                raise ProjectKnowledgeError(
                    "Knowledge batch could not be committed.",
                    code="KNOWLEDGE_IMPORT_FAILED",
                    status_code=500,
                ) from exc

            results: list[dict[str, Any]] = []
            for plan in prepared:
                embedded_count = len(missing_positions_by_plan[id(plan)])
                results.append(
                    {
                        "project_id": project,
                        "document_id": plan["document_id"],
                        "source_id": plan["source_id"],
                        "content_sha256": plan["content_sha256"],
                        "chunk_count": len(plan["chunks"]),
                        "embedded_chunk_count": 0 if plan["unchanged"] else embedded_count,
                        "reused_chunk_count": (
                            len(plan["chunks"])
                            if plan["unchanged"]
                            else len(plan["chunks"]) - embedded_count
                        ),
                        "status": (
                            "unchanged"
                            if plan["unchanged"]
                            else "updated" if plan["existing_document"] else "created"
                        ),
                    }
                )
            return results

    def list_documents(self, *, project_id: str) -> list[dict[str, Any]]:
        project = _identifier(project_id, "Project ID")
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT document_id, source_id, title, content_sha256,
                          content_bytes, metadata_json, embedding_adapter_id,
                          created_at, updated_at,
                          (SELECT COUNT(*) FROM knowledge_chunks AS chunks
                           WHERE chunks.project_id = documents.project_id
                             AND chunks.document_id = documents.document_id) AS chunk_count
                   FROM knowledge_documents AS documents
                   WHERE project_id = ?
                   ORDER BY updated_at DESC, document_id ASC""",
                (project,),
            ).fetchall()
        return [
            {
                **{key: row[key] for key in row.keys() if key != "metadata_json"},
                "project_id": project,
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in rows
        ]

    def delete_document(self, *, project_id: str, document_id: str) -> bool:
        project = _identifier(project_id, "Project ID")
        doc_id = _identifier(document_id, "Document ID")
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM knowledge_documents WHERE project_id = ? AND document_id = ?",
                (project, doc_id),
            )
            return cursor.rowcount > 0

    def document_chunks(
        self, *, project_id: str, document_id: str
    ) -> list[dict[str, Any]]:
        """Return bounded, ordered chunk previews for one scoped document."""

        project = _identifier(project_id, "Project ID")
        doc_id = _identifier(document_id, "Document ID")
        with self._connection() as connection:
            exists = connection.execute(
                """SELECT 1 FROM knowledge_documents
                   WHERE project_id = ? AND document_id = ?""",
                (project, doc_id),
            ).fetchone()
            if not exists:
                raise KnowledgeNotFoundError("Knowledge document was not found.")
            rows = connection.execute(
                """SELECT chunk_id, ordinal, start_offset, end_offset,
                          content_sha256, text
                   FROM knowledge_chunks
                   WHERE project_id = ? AND document_id = ?
                   ORDER BY ordinal ASC
                   LIMIT 1000""",
                (project, doc_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def document_chunk_count(self, *, project_id: str, document_id: str) -> int:
        """Return the authoritative scoped chunk count for preview pagination."""

        project = _identifier(project_id, "Project ID")
        doc_id = _identifier(document_id, "Document ID")
        with self._connection() as connection:
            exists = connection.execute(
                """SELECT 1 FROM knowledge_documents
                   WHERE project_id = ? AND document_id = ?""",
                (project, doc_id),
            ).fetchone()
            if not exists:
                raise KnowledgeNotFoundError("Knowledge document was not found.")
            return int(
                connection.execute(
                    """SELECT COUNT(*) FROM knowledge_chunks
                       WHERE project_id = ? AND document_id = ?""",
                    (project, doc_id),
                ).fetchone()[0]
            )

    def clear_project(self, *, project_id: str) -> dict[str, int]:
        """Delete only one project's index and report the affected counts."""

        project = _identifier(project_id, "Project ID")
        with self._lock, self._connection() as connection:
            document_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_documents WHERE project_id = ?",
                    (project,),
                ).fetchone()[0]
            )
            chunk_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_chunks WHERE project_id = ?",
                    (project,),
                ).fetchone()[0]
            )
            connection.execute(
                "DELETE FROM knowledge_documents WHERE project_id = ?", (project,)
            )
        return {"document_count": document_count, "chunk_count": chunk_count}

    def status(self, *, project_id: str) -> dict[str, Any]:
        project = _identifier(project_id, "Project ID")
        with self._lock, self._connection() as connection:
            adapter_id = self.embedding_adapter_id
            reranker_id = (
                str(getattr(self.reranker, "adapter_id", "") or "") or None
            )
            document_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_documents WHERE project_id = ?",
                    (project,),
                ).fetchone()[0]
            )
            chunk_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_chunks WHERE project_id = ?",
                    (project,),
                ).fetchone()[0]
            )
            current_adapter_chunk_count = int(
                connection.execute(
                    """SELECT COUNT(*) FROM knowledge_chunks
                       WHERE project_id = ? AND embedding_adapter_id = ?""",
                    (project, adapter_id),
                ).fetchone()[0]
            )
        reindex_required = bool(
            document_count > 0
            and (
                current_adapter_chunk_count == 0
                or current_adapter_chunk_count != chunk_count
            )
        )
        if reindex_required:
            index_status = "degraded"
            health_status = "degraded"
            limit_status = (
                "exceeded"
                if chunk_count > self.max_project_chunks
                else "reached"
                if chunk_count == self.max_project_chunks
                else "within_limit"
            )
        elif chunk_count > self.max_project_chunks:
            index_status = "degraded"
            health_status = "degraded"
            limit_status = "exceeded"
        elif chunk_count == self.max_project_chunks:
            index_status = "ready"
            health_status = "degraded"
            limit_status = "reached"
        else:
            index_status = "ready" if chunk_count else "empty"
            health_status = "healthy"
            limit_status = "within_limit"
        return {
            "enabled": True,
            "project_id": project,
            "index_status": index_status,
            "health_status": health_status,
            "limit_status": limit_status,
            "document_count": document_count,
            "chunk_count": chunk_count,
            "current_adapter_chunk_count": current_adapter_chunk_count,
            "reindex_required": reindex_required,
            "max_chunk_count": self.max_project_chunks,
            "remaining_chunk_count": max(0, self.max_project_chunks - chunk_count),
            "chunk_chars": self.chunk_chars,
            "overlap_chars": self.overlap_chars,
            "embedding_adapter": adapter_id,
            "reranker": reranker_id,
            "reranker_failure_mode": (
                "embedding_fallback" if self.reranker_fail_open else "fail_closed"
            ),
        }

    def retrieve(
        self,
        *,
        project_id: str,
        query: str,
        top_k: int = 5,
        candidate_limit: int = 40,
        run_id: str = "",
        consent_proposal_id: str = "",
        requested_model: str = "",
        budget_override_id: str = "",
    ) -> list[dict[str, Any]]:
        project = _identifier(project_id, "Project ID")
        with self._lock:
            embedding_adapter = self.embedding_adapter
            adapter_id = self._validated_adapter_id(
                embedding_adapter, label="Embedding adapter"
            )
            reranker = self.reranker
            reranker_fail_open = self.reranker_fail_open
        semantic_context = SemanticRequestContext(
            project_id=project,
            run_id=run_id,
            consent_proposal_id=consent_proposal_id,
            requested_model=requested_model,
            budget_override_id=budget_override_id,
        )
        query_text = str(query or "").strip()
        if not query_text or len(query_text) > MAX_QUERY_CHARS or _CONTROL.search(query_text):
            raise ProjectKnowledgeError(
                f"Query must contain 1-{MAX_QUERY_CHARS} visible characters.",
                code="INVALID_KNOWLEDGE_QUERY",
            )
        requested = int(top_k)
        candidate_count = int(candidate_limit)
        if requested < 1 or requested > MAX_TOP_K:
            raise ProjectKnowledgeError(
                f"top_k must be between 1 and {MAX_TOP_K}.",
                code="INVALID_KNOWLEDGE_QUERY",
            )
        if candidate_count < requested or candidate_count > MAX_CANDIDATES:
            raise ProjectKnowledgeError(
                f"candidate_limit must be between top_k and {MAX_CANDIDATES}.",
                code="INVALID_KNOWLEDGE_QUERY",
            )

        query_vector = self._embed(
            [query_text],
            purpose="query",
            context=semantic_context,
            adapter=embedding_adapter,
        )[0]
        with self._connection() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_chunks WHERE project_id = ?",
                    (project,),
                ).fetchone()[0]
            )
            if total > self.max_project_chunks:
                raise ProjectKnowledgeError(
                    "Project knowledge index exceeds the bounded local scan limit.",
                    code="KNOWLEDGE_INDEX_TOO_LARGE",
                    status_code=409,
                )
            rows = connection.execute(
                """SELECT chunks.*, documents.source_id, documents.title,
                          documents.content_sha256 AS document_sha256
                   FROM knowledge_chunks AS chunks
                   JOIN knowledge_documents AS documents
                     ON documents.project_id = chunks.project_id
                    AND documents.document_id = chunks.document_id
                   WHERE chunks.project_id = ?
                     AND chunks.embedding_adapter_id = ?
                   ORDER BY chunks.document_id ASC, chunks.ordinal ASC""",
                (project, adapter_id),
            ).fetchall()

        candidates: list[dict[str, Any]] = []
        for row in rows:
            try:
                vector = [float(value) for value in json.loads(row["embedding_json"])]
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if len(vector) != len(query_vector) or not all(math.isfinite(value) for value in vector):
                continue
            candidates.append(
                {
                    "chunk_id": str(row["chunk_id"]),
                    "document_id": str(row["document_id"]),
                    "source_id": str(row["source_id"]),
                    "title": str(row["title"]),
                    "ordinal": int(row["ordinal"]),
                    "start_offset": int(row["start_offset"]),
                    "end_offset": int(row["end_offset"]),
                    "content_sha256": str(row["content_sha256"]),
                    "document_sha256": str(row["document_sha256"]),
                    "text": str(row["text"]),
                    "embedding_score": cosine_similarity(query_vector, vector),
                }
            )
        candidates.sort(
            key=lambda item: (-item["embedding_score"], item["document_id"], item["ordinal"])
        )
        candidates = candidates[:candidate_count]

        retrieval_mode = "embedding"
        degradation_code: str | None = None
        if reranker is not None and candidates:
            rerank_candidates: list[dict[str, Any]] = []
            rerank_bytes = len(query_text.encode("utf-8"))
            for candidate in candidates:
                remaining = MAX_RERANK_INPUT_BYTES - rerank_bytes
                if remaining <= 0:
                    break
                bounded = dict(candidate)
                bounded["text"] = _truncate_utf8(candidate["text"], remaining)
                if not bounded["text"]:
                    break
                rerank_bytes += len(bounded["text"].encode("utf-8"))
                rerank_candidates.append(bounded)
            candidates = candidates[: len(rerank_candidates)]
            try:
                contextual = getattr(reranker, "rerank_for_project", None)
                if callable(contextual):
                    raw_scores = contextual(
                        query_text,
                        tuple(rerank_candidates),
                        context=semantic_context,
                    )
                else:
                    raw_scores = reranker.rerank(
                        query_text, tuple(rerank_candidates)
                    )
                scores = [float(value) for value in raw_scores]
            except Exception as exc:
                if not reranker_fail_open:
                    if isinstance(exc, SemanticRetrievalError):
                        raise KnowledgeAdapterError(
                            str(exc), code=exc.code, status_code=exc.status_code
                        ) from exc
                    raise KnowledgeAdapterError("Reranker adapter failed.") from exc
                scores = []
                degradation_code = str(
                    getattr(exc, "code", "RERANKER_UNAVAILABLE")
                )[:80]
            valid_scores = bool(
                len(scores) == len(candidates)
                and all(math.isfinite(value) for value in scores)
            )
            if not valid_scores and not reranker_fail_open:
                raise KnowledgeAdapterError("Reranker returned invalid scores.")
            if valid_scores:
                retrieval_mode = "embedding_rerank"
                for candidate, score in zip(candidates, scores):
                    candidate["rerank_score"] = score
                    candidate["score"] = score
                candidates.sort(
                    key=lambda item: (
                        -item["score"],
                        -item["embedding_score"],
                        item["chunk_id"],
                    )
                )
            else:
                retrieval_mode = "embedding_fallback"
                degradation_code = degradation_code or "RERANKER_INVALID_RESPONSE"
                for candidate in candidates:
                    candidate["rerank_score"] = None
                    candidate["score"] = candidate["embedding_score"]
        else:
            for candidate in candidates:
                candidate["rerank_score"] = None
                candidate["score"] = candidate["embedding_score"]

        results: list[dict[str, Any]] = []
        remaining_bytes = MAX_RESULT_TEXT_BYTES
        for candidate in candidates[:requested]:
            if remaining_bytes <= 0:
                break
            snippet = _truncate_utf8(candidate["text"], remaining_bytes)
            used = len(snippet.encode("utf-8"))
            if not snippet:
                break
            remaining_bytes -= used
            results.append(
                {
                    "text": snippet,
                    "score": candidate["score"],
                    "embedding_score": candidate["embedding_score"],
                    "rerank_score": candidate["rerank_score"],
                    "retrieval_mode": retrieval_mode,
                    "degradation_code": degradation_code,
                    "citation": {
                        "project_id": project,
                        "document_id": candidate["document_id"],
                        "chunk_id": candidate["chunk_id"],
                        "source_id": candidate["source_id"],
                        "title": candidate["title"],
                        "ordinal": candidate["ordinal"],
                        "start_offset": candidate["start_offset"],
                        "end_offset": candidate["end_offset"],
                        "document_sha256": candidate["document_sha256"],
                        "chunk_sha256": candidate["content_sha256"],
                    },
                }
            )
        return results


__all__ = [
    "DEFAULT_CHUNK_CHARS",
    "DEFAULT_CHUNK_OVERLAP",
    "DeterministicLocalEmbedding",
    "EmbeddingAdapter",
    "ProjectEmbeddingAdapter",
    "ProjectRerankerAdapter",
    "KnowledgeAdapterError",
    "KnowledgeNotFoundError",
    "MAX_IMPORT_DOCUMENTS",
    "MAX_SCAN_CHUNKS",
    "ProjectKnowledgeError",
    "ProjectKnowledgeService",
    "RerankerAdapter",
    "cosine_similarity",
    "stable_chunk_text",
]
