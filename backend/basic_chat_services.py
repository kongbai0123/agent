"""Service boundaries used by the intentionally tool-free basic chat mode."""

from __future__ import annotations

from typing import Any


class _EmptyVectorStore:
    def get(self, **_kwargs: Any) -> dict[str, list[Any]]:
        return {"documents": []}


class DisabledRAGEngine:
    """Stable no-op RAG interface without importing the retired RAG runtime."""

    def __init__(self) -> None:
        self.vector_store = _EmptyVectorStore()

    def query(self, _query: str, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []


def build_rag_service(*, basic_only: bool, **_kwargs: Any) -> DisabledRAGEngine:
    if not basic_only:
        raise RuntimeError("The RAG runtime is not available in basic chat mode.")
    return DisabledRAGEngine()
