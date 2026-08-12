from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from chat.generated_artifacts import (  # noqa: E402
    MAX_ARTIFACT_CHARS,
    MAX_TOTAL_ARTIFACT_CHARS,
    extract_generated_code_blocks,
    persist_generated_artifacts,
)


class _Database:
    def __init__(self, *, fail_save: bool = False, fail_event: bool = False):
        self.fail_save = fail_save
        self.fail_event = fail_event
        self.saved = []
        self.events = []

    def save_artifact(self, *args):
        if self.fail_save:
            raise RuntimeError("save failed")
        self.saved.append(args)

    def append_run_event(self, *args):
        if self.fail_event:
            raise RuntimeError("event failed")
        self.events.append(args)
        return {"sequence": len(self.events)}


def test_extracts_only_closed_explicit_allowlisted_fences_with_safe_limits() -> None:
    small_blocks = "\n".join(
        f"```txt\nvalue-{index}\n```" for index in range(10)
    )
    answer = (
        "```javascript\nnot allowlisted\n```\n"
        "```html\n<h1>safe</h1>\n```\n"
        "```py\n"
        + ("x" * (MAX_ARTIFACT_CHARS + 1))
        + "\n```\n"
        + small_blocks
        + "\n```css\nunterminated"
    )

    blocks = extract_generated_code_blocks(answer)

    assert len(blocks) == 8
    assert blocks[0] == {"language": "html", "content": "<h1>safe</h1>\n"}
    assert [item["language"] for item in blocks[1:]] == ["txt"] * 7
    assert all(len(item["content"]) <= MAX_ARTIFACT_CHARS for item in blocks)
    assert "not allowlisted" not in repr(blocks)
    assert "unterminated" not in repr(blocks)


def test_persistence_is_deterministic_and_records_content_free_events() -> None:
    database = _Database()
    answer = "Result\n```HTML title=../../escape.html\n<p>Hello</p>\n```"

    first = persist_generated_artifacts(
        database,
        run_id="run-one",
        session_id="session-one",
        turn_id="turn-one",
        answer=answer,
    )
    second = persist_generated_artifacts(
        database,
        run_id="run-one",
        session_id="session-one",
        turn_id="turn-one",
        answer=answer,
    )

    assert first == second
    assert len(first) == 1
    reference = first[0]
    assert reference["artifact_id"].startswith("artifact-")
    assert reference["relative_path"] == "generated-01.html"
    assert ".." not in reference["relative_path"]
    saved = database.saved[0]
    assert saved[1:5] == (
        "session-one",
        "turn-one",
        "Generated HTML file 1",
        "html",
    )
    assert saved[5] == [
        {
            "path": "generated-01.html",
            "content": "<p>Hello</p>\n",
            "language": "html",
        }
    ]
    event_name, payload = database.events[0][1:]
    assert event_name == "artifact"
    assert payload["artifact_id"] == reference["artifact_id"]
    assert payload["relative_path"] == "generated-01.html"
    assert payload["status"] == "completed"
    assert "content" not in payload
    assert len(payload["sha256"]) == 64


def test_total_character_budget_is_enforced_across_blocks() -> None:
    payload_size = 60 * 1024
    answer = "\n".join(
        f"```txt\n{str(index) * payload_size}\n```" for index in range(6)
    )

    blocks = extract_generated_code_blocks(answer)

    assert len(blocks) == 4
    assert sum(len(item["content"]) for item in blocks) <= MAX_TOTAL_ARTIFACT_CHARS


def test_persistence_failures_degrade_without_escaping() -> None:
    answer = "```json\n{\"safe\": true}\n```"

    assert persist_generated_artifacts(
        _Database(fail_save=True),
        run_id="run-save-failure",
        session_id="session-one",
        turn_id="turn-one",
        answer=answer,
    ) == []

    event_failure = _Database(fail_event=True)
    references = persist_generated_artifacts(
        event_failure,
        run_id="run-event-failure",
        session_id="session-one",
        turn_id="turn-one",
        answer=answer,
    )
    assert len(references) == 1
    assert len(event_failure.saved) == 1
