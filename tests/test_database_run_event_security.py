from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import database  # noqa: E402
from structured_log import clear_registered_secrets, register_secret  # noqa: E402


@pytest.fixture()
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "workbench.db"))
    database.init_db()
    return database


def _run(item=database, *, model: str = "model-one") -> str:
    run_id = "run_security_12345678"
    item.upsert_run(
        run_id,
        "session-one",
        "turn-one",
        model,
        "chat",
        "running",
        project_id="project-one",
        sources=[{"kind": "workbench_project_skill", "slug": "review"}],
    )
    return run_id


def test_append_event_enforces_run_session_and_project_binding_and_redacts(
    isolated_database,
) -> None:
    run_id = _run(isolated_database)
    for mismatched in (
        {"run_id": "run_other_12345678"},
        {"session_id": "session-other"},
        {"project_id": "project-other"},
    ):
        with pytest.raises(ValueError, match="mismatched"):
            isolated_database.append_run_event(
                run_id,
                "meta",
                {"runtime": "chat", **mismatched},
            )

    secret = "runtime-event-secret-123456789"
    register_secret(secret)
    try:
        record = isolated_database.append_run_event(
            run_id,
            "error",
            {
                "code": "UPSTREAM_FAILED",
                "message": f"failure {secret}",
                "recoverable": True,
                "command": f"do-not-store {secret}",
            },
        )
    finally:
        clear_registered_secrets()

    assert record["payload"]["message"] == "failure [redacted]"
    assert "command" not in record["payload"]


def test_public_run_events_reprojects_legacy_rows_and_drops_mismatches(
    isolated_database,
) -> None:
    events = [
        {
            "type": "error",
            "run_id": "run_security_12345678",
            "message": "safe message",
            "recoverable": True,
            "command": "raw command",
        },
        {"type": "unknown_event", "message": "must not surface"},
        {
            "type": "error",
            "session_id": "session-other",
            "message": "wrong scope",
        },
        {
            "event": "tool_start",
            "sequence": 9,
            "payload": {
                "tool": "project_read_file",
                "tool_call_id": "call-one",
                "run_id": "run_security_12345678",
                "args": {"path": "secret.txt"},
            },
        },
    ]

    public = isolated_database.public_run_events(
        events,
        run_id="run_security_12345678",
        session_id="session-one",
        project_id="project-one",
    )

    assert [item["event"] for item in public] == ["error", "tool_start"]
    assert public[0]["payload"] == {
        "message": "safe message",
        "recoverable": True,
    }
    assert public[1]["payload"]["args"] == {
        "scope": "active_project",
        "access": "read_only",
        "details_redacted": True,
    }


def test_event_key_is_idempotent_but_cannot_be_reused_for_other_content(
    isolated_database,
) -> None:
    run_id = _run(isolated_database)
    payload = {
        "event_key": "provider-error:one",
        "code": "PROVIDER_ERROR",
        "message": "temporary failure",
        "recoverable": True,
    }

    first = isolated_database.append_run_event(run_id, "error", payload)
    repeated = isolated_database.append_run_event(run_id, "error", payload)

    assert repeated == first
    assert isolated_database.get_run(run_id)["execution_revision"] == 1
    with pytest.raises(ValueError, match="reused"):
        isolated_database.append_run_event(
            run_id,
            "error",
            {**payload, "message": "different failure"},
        )


def test_runtime_transition_updates_model_without_erasing_events_or_sources(
    isolated_database,
) -> None:
    run_id = _run(isolated_database, model="hermes-runtime-model")
    isolated_database.append_run_event(
        run_id,
        "meta",
        {
            "run_id": run_id,
            "session_id": "session-one",
            "project_id": "project-one",
            "runtime": "hermes",
        },
    )

    isolated_database.upsert_run(
        run_id,
        "session-one",
        "turn-one",
        "basic-chat-model",
        "chat",
        "running",
        events=[],
        sources=[],
        project_id="project-one",
    )

    current = isolated_database.get_run(run_id)
    assert current["model"] == "basic-chat-model"
    assert [item["event"] for item in current["events"]] == ["meta"]
    assert current["sources"] == [
        {"kind": "workbench_project_skill", "slug": "review"}
    ]


def test_git_evidence_preserves_canonical_short_sha_and_success(
    isolated_database,
) -> None:
    run_id = _run(isolated_database)

    record = isolated_database.append_run_event(
        run_id,
        "git_push",
        {
            "short_sha": "abcdef123456",
            "branch": "main",
            "remote": "origin",
            "success": True,
        },
    )

    assert record["payload"] == {
        "short_sha": "abcdef123456",
        "branch": "main",
        "remote": "origin",
        "success": True,
    }


def test_host_plan_evidence_keeps_only_redacted_task_titles_and_statuses(
    isolated_database,
) -> None:
    run_id = _run(isolated_database)

    plan = isolated_database.append_run_event(
        run_id,
        "plan",
        {
            "run_id": run_id,
            "project_id": "project-one",
            "plan_id": "plan_abcdef123456",
            "planner": "deterministic_fallback_v1",
            "task_count": 5,
            "tool_call_limit": 16,
            "tool_calls_per_step": 2,
            "wall_seconds": 600,
            "tasks": [
                {
                    "id": "step-01",
                    "title": "執行需求 1",
                    "status": "pending",
                    "instruction": "do not persist full user task text",
                    "arguments": {"secret": "must-not-persist"},
                }
            ],
        },
    )
    update = isolated_database.append_run_event(
        run_id,
        "task_update",
        {
            "run_id": run_id,
            "project_id": "project-one",
            "plan_id": "plan_abcdef123456",
            "task_id": "step-01",
            "kind": "tool",
            "status": "completed",
            "message": "步驟已完成。",
            "tool_calls_used": 1,
            "tool_call_limit": 2,
            "plan_status": "running",
        },
    )

    assert plan["payload"]["task_count"] == 5
    assert plan["payload"]["tasks"] == [
        {"id": "step-01", "title": "執行需求 1", "status": "pending"}
    ]
    assert "instruction" not in str(plan["payload"])
    assert "must-not-persist" not in str(plan["payload"])
    assert update["payload"]["task_id"] == "step-01"
    assert [item["event"] for item in isolated_database.get_run(run_id)["events"]] == [
        "plan",
        "task_update",
    ]


def test_answer_factuality_validation_persists_only_collector_contract(
    isolated_database,
) -> None:
    run_id = _run(isolated_database)
    record = isolated_database.append_run_event(
        run_id,
        "validation",
        {
            "run_id": run_id,
            "project_id": "project-one",
            "validation_id": f"{run_id}:answer_factuality",
            "name": "answer_factuality",
            "status": "failed",
            "passed": False,
            "failed": 1,
            "skipped": 0,
            "duration_ms": 12.5,
            "summary": "回答的事實驗證未通過。",
            "claim_counts": {"unsupported": 1},
            "claim_text": "不得持久化的宣稱",
            "evidence_text": "不得持久化的證據",
        },
    )

    assert record["payload"] == {
        "validation_id": f"{run_id}:answer_factuality",
        "name": "answer_factuality",
        "status": "failed",
        "passed": False,
        "failed": 1,
        "skipped": 0,
        "duration_ms": 12.5,
        "summary": "回答的事實驗證未通過。",
    }
    assert "宣稱" not in str(record)
    assert "證據" not in str(record)


def test_run_tasks_are_redacted_on_write_and_reload(isolated_database) -> None:
    run_id = "run_redacted_plan_12345678"
    isolated_database.upsert_run(
        run_id,
        "session-one",
        "turn-one",
        "model-one",
        "chat",
        "completed",
        project_id="project-one",
        tasks=[
            {
                "id": "step-01",
                "label": "執行需求 1",
                "status": "completed",
                "instruction": "private planner instruction",
                "arguments": {"path": "D:/private"},
            }
        ],
    )

    assert isolated_database.get_run(run_id)["tasks"] == [
        {"id": "step-01", "label": "執行需求 1", "status": "completed"}
    ]
