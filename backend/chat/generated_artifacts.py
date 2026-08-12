"""Persist bounded text artifacts explicitly returned in assistant answers.

Generated artifacts are evidence attached to one completed run.  The parser is
deliberately conservative: only closed fenced code blocks with a fixed,
allowlisted language are accepted, and neither filenames nor paths are taken
from model output.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Mapping


LOGGER = logging.getLogger(__name__)

MAX_ARTIFACTS = 8
MAX_ARTIFACT_CHARS = 64 * 1024
MAX_TOTAL_ARTIFACT_CHARS = 256 * 1024

_LANGUAGE_EXTENSIONS: Mapping[str, str] = {
    "html": "html",
    "svg": "svg",
    "xml": "xml",
    "css": "css",
    "js": "js",
    "ts": "ts",
    "py": "py",
    "md": "md",
    "json": "json",
    "txt": "txt",
}
_OPENING_FENCE = re.compile(
    r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<info>[^\r\n]*)$"
)


def _is_closing_fence(line: str, marker: str, minimum: int) -> bool:
    candidate = line.rstrip("\r\n")
    match = re.fullmatch(r"[ \t]{0,3}([`~]+)[ \t]*", candidate)
    if not match:
        return False
    fence = match.group(1)
    return fence[0] == marker and len(fence) >= minimum


def extract_generated_code_blocks(answer: str) -> list[dict[str, Any]]:
    """Return bounded, closed, explicitly typed code blocks from an answer."""

    accepted: list[dict[str, Any]] = []
    total_chars = 0
    open_marker: str | None = None
    open_length = 0
    open_language: str | None = None
    chunks: list[str] = []
    candidate_chars = 0
    candidate_oversized = False

    for line in str(answer or "").splitlines(keepends=True):
        if open_marker is None:
            match = _OPENING_FENCE.fullmatch(line.rstrip("\r\n"))
            if not match:
                continue
            fence = match.group("fence")
            info = match.group("info").strip()
            language = info.split(None, 1)[0].casefold() if info else ""
            open_marker = fence[0]
            open_length = len(fence)
            open_language = language if language in _LANGUAGE_EXTENSIONS else None
            chunks = []
            candidate_chars = 0
            candidate_oversized = False
            continue

        if _is_closing_fence(line, open_marker, open_length):
            if (
                open_language is not None
                and not candidate_oversized
                and len(accepted) < MAX_ARTIFACTS
            ):
                content = "".join(chunks)
                # SQLite and JSON persistence both expect valid Unicode.  This
                # replacement is only relevant to malformed in-process input.
                content = content.encode("utf-8", "replace").decode("utf-8")
                size = len(content)
                if (
                    content
                    and size <= MAX_ARTIFACT_CHARS
                    and total_chars + size <= MAX_TOTAL_ARTIFACT_CHARS
                ):
                    accepted.append(
                        {
                            "language": open_language,
                            "content": content,
                        }
                    )
                    total_chars += size
            open_marker = None
            open_length = 0
            open_language = None
            chunks = []
            candidate_chars = 0
            candidate_oversized = False
            if len(accepted) >= MAX_ARTIFACTS:
                break
            continue

        if open_language is not None:
            candidate_chars += len(line)
            if candidate_chars <= MAX_ARTIFACT_CHARS:
                chunks.append(line)
            else:
                # Continue scanning for the matching close without retaining a
                # model-controlled oversized payload in a second buffer.
                chunks = []
                candidate_oversized = True

    return accepted


def _artifact_identity(
    *, run_id: str, index: int, language: str, content: str
) -> str:
    digest = hashlib.sha256()
    for value in (str(run_id), str(index), language, content):
        digest.update(value.encode("utf-8", "replace"))
        digest.update(b"\0")
    return f"artifact-{digest.hexdigest()[:32]}"


def persist_generated_artifacts(
    database: Any,
    *,
    run_id: str,
    session_id: str,
    turn_id: str,
    answer: str,
) -> list[dict[str, Any]]:
    """Persist generated text files without allowing failures to fail chat.

    A successful database save is returned as a small reference suitable for
    both ``messages.artifacts`` and ``runs.artifacts``.  Public event recording
    is best-effort and never includes the generated content.
    """

    try:
        blocks = extract_generated_code_blocks(answer)
    except Exception as exc:  # pragma: no cover - defensive isolation
        LOGGER.warning(
            "Generated artifact extraction degraded (%s).", type(exc).__name__
        )
        return []

    references: list[dict[str, Any]] = []
    save_artifact = getattr(database, "save_artifact", None)
    if not callable(save_artifact):
        return references
    append_event = getattr(database, "append_run_event", None)

    for index, block in enumerate(blocks, start=1):
        language = str(block["language"])
        content = str(block["content"])
        extension = _LANGUAGE_EXTENSIONS[language]
        relative_path = f"generated-{index:02d}.{extension}"
        title = f"Generated {language.upper()} file {index}"
        artifact_id = _artifact_identity(
            run_id=run_id,
            index=index,
            language=language,
            content=content,
        )
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        try:
            save_artifact(
                artifact_id,
                session_id,
                turn_id,
                title,
                language,
                [
                    {
                        "path": relative_path,
                        "content": content,
                        "language": language,
                    }
                ],
            )
        except Exception as exc:
            LOGGER.warning(
                "Generated artifact save degraded (%s).", type(exc).__name__
            )
            continue

        reference = {
            "artifact_id": artifact_id,
            "title": title,
            "type": language,
            "relative_path": relative_path,
            "language": language,
            "size_chars": len(content),
            "size_bytes": len(encoded),
        }
        references.append(reference)
        if callable(append_event):
            try:
                append_event(
                    run_id,
                    "artifact",
                    {
                        "artifact_id": artifact_id,
                        "title": title,
                        "artifact_type": language,
                        "status": "completed",
                        "relative_path": relative_path,
                        "language": language,
                        "size_bytes": len(encoded),
                        "sha256": digest,
                        "event_key": f"artifact:{artifact_id}",
                    },
                )
            except Exception as exc:
                LOGGER.warning(
                    "Generated artifact event recording degraded (%s).",
                    type(exc).__name__,
                )

    return references


__all__ = [
    "MAX_ARTIFACTS",
    "MAX_ARTIFACT_CHARS",
    "MAX_TOTAL_ARTIFACT_CHARS",
    "extract_generated_code_blocks",
    "persist_generated_artifacts",
]
