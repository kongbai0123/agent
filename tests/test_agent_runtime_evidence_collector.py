"""Security and contract tests for the formal Basic Chat evidence collector."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import collect_agent_runtime_evidence as runtime_collector  # noqa: E402
from collect_agent_runtime_evidence import (  # noqa: E402
    COLLECTOR_SOURCE,
    RuntimeEnvironment,
    RuntimeEvidenceError,
    build_selection,
    collect_runtime_evidence,
)
from evaluate_agent_capabilities import evaluate_task, load_json  # noqa: E402
from export_agent_capability_results import export_evidence  # noqa: E402


SUITE_PATH = ROOT / "evals" / "agent_capability" / "v1" / "tasks.json"
GATE_PATH = ROOT / "evals" / "gates" / "agent_capability_v1.json"
ENVIRONMENT = RuntimeEnvironment(
    git_commit="0123456789abcdef",
    git_digest="sha256:" + "1" * 64,
    git_dirty=True,
    runtime_digest="sha256:" + "2" * 64,
)


def _contracts():
    return load_json(SUITE_PATH), load_json(GATE_PATH)


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _provenance_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".gitignore").write_text(
        "runtime/*\n.env\nbackend/settings.json\n",
        encoding="utf-8",
    )
    (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "add", ".gitignore", "tracked.txt")
    _git(
        repository,
        "-c",
        "user.name=Runtime Collector Test",
        "-c",
        "user.email=runtime-collector@example.invalid",
        "commit",
        "-q",
        "-m",
        "fixture",
    )
    return repository


def test_untracked_source_content_is_bound_but_ignored_runtime_and_secrets_are_not(
    tmp_path, monkeypatch
):
    repository = _provenance_repository(tmp_path)
    backend = repository / "backend"
    backend.mkdir()
    source = backend / "new_runtime.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(runtime_collector, "REPO_ROOT", repository)

    first = runtime_collector._git_provenance()
    suite, gate = _contracts()
    locked_environment = RuntimeEnvironment(
        git_commit=first[0],
        git_digest=first[1],
        git_dirty=first[2],
        runtime_digest="sha256:" + "a" * 64,
    )
    selection = build_selection(
        suite,
        gate,
        subject_id="runtime-source-lock",
        subject_version="1",
        model_id="fixture-model",
        model_version="1",
        environment=locked_environment,
        task_ids=(suite["tasks"][0]["id"],),
    )
    selection["runs"][0]["run_id"] = "run-after-lock"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    second = runtime_collector._git_provenance()
    assert first[1] != second[1]
    changed_environment = RuntimeEnvironment(
        git_commit=second[0],
        git_digest=second[1],
        git_dirty=second[2],
        runtime_digest=locked_environment.runtime_digest,
    )
    with pytest.raises(RuntimeEvidenceError, match="Git 工作樹已改變"):
        runtime_collector._validate_selection(
            selection, suite, gate, changed_environment
        )

    runtime_file = repository / "runtime" / "db" / "workbench.db"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_bytes(b"private runtime one")
    secret = repository / ".env"
    secret.write_text("API_KEY=do-not-read\n", encoding="utf-8")
    before_private_change = runtime_collector._git_provenance()
    runtime_file.write_bytes(b"private runtime two")
    secret.write_text("API_KEY=still-do-not-read\n", encoding="utf-8")
    after_private_change = runtime_collector._git_provenance()
    assert before_private_change == after_private_change


def test_untracked_provenance_rejects_sensitive_or_oversized_unignored_files(
    tmp_path, monkeypatch
):
    repository = _provenance_repository(tmp_path)
    monkeypatch.setattr(runtime_collector, "REPO_ROOT", repository)
    credential = repository / "credentials.json"
    credential.write_text('{"token":"must-not-be-hashed"}', encoding="utf-8")
    with pytest.raises(RuntimeEvidenceError, match="疑似憑證或秘密"):
        runtime_collector._git_provenance()

    credential.unlink()
    oversized = repository / "new-runtime.bin"
    oversized.write_bytes(b"x" * 17)
    monkeypatch.setattr(runtime_collector, "_MAX_UNTRACKED_FILE_BYTES", 16)
    with pytest.raises(RuntimeEvidenceError, match="大小上限"):
        runtime_collector._git_provenance()


def test_git_provenance_failure_is_fail_closed(tmp_path, monkeypatch):
    repository = _provenance_repository(tmp_path)
    monkeypatch.setattr(runtime_collector, "REPO_ROOT", repository)

    def unavailable(*_args, **_kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(runtime_collector.subprocess, "run", unavailable)
    with pytest.raises(RuntimeEvidenceError, match="無法取得 Git 工作樹來源證明"):
        runtime_collector._git_provenance()


def _task(suite: dict, task_id: str) -> dict:
    return next(item for item in suite["tasks"] if item["id"] == task_id)


def _database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            project_id TEXT, retry_of_run_id TEXT, model TEXT, mode TEXT,
            status TEXT, tasks_json TEXT, events_json TEXT, sources_json TEXT,
            metrics_json TEXT, artifacts_json TEXT, input_manifest_json TEXT,
            execution_revision INTEGER, created_at TEXT, completed_at TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            visible_content TEXT, llm_content TEXT, sources_json TEXT,
            process_events_json TEXT, artifacts_json TEXT, turn_id TEXT,
            parent_message_id INTEGER, created_at TEXT
        );
        CREATE TABLE tool_approval_bindings (
            approval_id TEXT PRIMARY KEY, run_id TEXT, project_id TEXT,
            call_id TEXT, tool_name TEXT, arguments_sha256 TEXT,
            summary_json TEXT, status TEXT, created_at TEXT,
            decided_at TEXT, consumed_at TEXT
        );
        """
    )
    return connection


def _selection(suite: dict, gate: dict, task_id: str, run_id: str) -> dict:
    selection = build_selection(
        suite,
        gate,
        subject_id="formal-runtime-eval",
        subject_version="test",
        model_id="test-model",
        model_version="fixture",
        config={"temperature": 0},
        policy={"permission": "restricted"},
        trial=1,
        environment=ENVIRONMENT,
        capture_started_at="2026-08-26T00:00:00+00:00",
        task_ids=[task_id],
    )
    selection["runs"][0]["run_id"] = run_id
    return selection


def _insert_run(
    connection: sqlite3.Connection,
    *,
    task: dict,
    run_id: str = "run-formal-1",
    project_id: str | None = "project-a",
    events: list[dict] | None = None,
    sources: list[dict] | None = None,
    private_sources: list[dict] | None = None,
    artifacts: list[dict] | None = None,
    answer: str = "安全完成；即使原文有 nvapi-example-secret 也不應被匯出。",
    status: str = "completed",
    persist_assistant: bool = True,
) -> None:
    prompt = task["prompt"].strip()
    manifest = {
        "version": 2,
        "user_message_id": 1,
        "user_message": prompt,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "project_id": project_id,
        "knowledge_used": bool(sources),
        "knowledge_sources": private_sources or [],
    }
    connection.execute(
        """
        INSERT INTO runs VALUES (?, 'session-a', 'turn-a', ?, NULL, 'test-model',
          'chat', ?, '[]', ?, ?, '{}', ?, ?, 10,
          '2026-08-26T00:01:00+00:00', '2026-08-26T00:02:00+00:00')
        """,
        (
            run_id,
            project_id,
            status,
            json.dumps(events or [], ensure_ascii=False),
            json.dumps(sources or [], ensure_ascii=False),
            json.dumps(artifacts or [], ensure_ascii=False),
            json.dumps(manifest, ensure_ascii=False),
        ),
    )
    connection.execute(
        "INSERT INTO messages VALUES (1, 'session-a', 'user', ?, ?, ?, '[]', '[]', '[]', 'turn-a', NULL, '2026-08-26T00:01:00+00:00')",
        (prompt, prompt, prompt),
    )
    if persist_assistant:
        connection.execute(
            "INSERT INTO messages VALUES (2, 'session-a', 'assistant', ?, ?, ?, ?, '[]', '[]', 'turn-a', 1, '2026-08-26T00:02:00+00:00')",
            (answer, answer, answer, json.dumps(sources or [], ensure_ascii=False)),
        )
    connection.commit()


def _runtime_event(sequence: int, event: str, payload: dict) -> dict:
    return {
        "event": event,
        "sequence": sequence,
        "created_at": f"2026-08-26T00:01:{sequence:02d}+00:00",
        "payload": payload,
    }


def test_real_basic_chat_run_exports_through_existing_exporter_without_content(tmp_path):
    suite, gate = _contracts()
    task = _task(suite, "tool-select-project-search")
    database_path = tmp_path / "workbench.db"
    connection = _database(database_path)
    _insert_run(
        connection,
        task=task,
        events=[
            _runtime_event(
                1,
                "tool_start",
                {
                    "tool": "search_files",
                    "tool_call_id": "call-1",
                    "run_id": "run-formal-1",
                    "project_id": "project-a",
                    "args": {"scope": "active_project", "access": "read_only", "details_redacted": True},
                },
            ),
            _runtime_event(
                2,
                "tool_end",
                {
                    "tool": "search_files",
                    "tool_call_id": "call-1",
                    "run_id": "run-formal-1",
                    "project_id": "project-a",
                    "success": True,
                    "result": "completed",
                    "details_redacted": True,
                    "duration_ms": 12,
                },
            ),
        ],
    )
    connection.close()

    evidence = collect_runtime_evidence(
        database_path,
        _selection(suite, gate, task["id"], "run-formal-1"),
        suite,
        gate,
        environment=ENVIRONMENT,
    )
    results = export_evidence(evidence, suite, gate)

    assert results["provenance"]["source"] == COLLECTOR_SOURCE
    assert [event["type"] for event in results["results"][0]["events"]] == [
        "tool_started",
        "tool_completed",
        "answer_final",
    ]
    assert evaluate_task(task, results["results"][0])["passed"] is True
    serialized = json.dumps(evidence, ensure_ascii=False)
    assert "nvapi-example-secret" not in serialized
    assert task["prompt"] not in serialized


def test_consumed_approval_is_joined_by_call_and_exact_argument_digest(tmp_path):
    suite, gate = _contracts()
    task = _task(suite, "safety-external-write-approval")
    database_path = tmp_path / "workbench.db"
    connection = _database(database_path)
    approval_id = "approval-1"
    argument_sha = "a" * 64
    _insert_run(
        connection,
        task=task,
        events=[
            _runtime_event(
                1,
                "approval_required",
                {
                    "approval_id": approval_id,
                    "capability": "留言",
                    "message": "需要批准",
                    "summary": "GitHub",
                    "run_id": "run-formal-1",
                    "risk": "external_write",
                    "status": "pending",
                    "choices": ["once", "deny"],
                },
            ),
            _runtime_event(
                2,
                "tool_start",
                {
                    "tool": "github.issue.comment",
                    "tool_call_id": "call-write",
                    "run_id": "run-formal-1",
                    "project_id": "project-a",
                    "args": {"scope": "active_project", "access": "write", "details_redacted": True},
                },
            ),
            _runtime_event(
                3,
                "tool_end",
                {
                    "tool": "github.issue.comment",
                    "tool_call_id": "call-write",
                    "run_id": "run-formal-1",
                    "project_id": "project-a",
                    "success": True,
                    "result": "completed",
                    "details_redacted": True,
                    "duration_ms": 20,
                },
            ),
        ],
    )
    connection.execute(
        "INSERT INTO tool_approval_bindings VALUES (?, 'run-formal-1', 'project-a', 'call-write', 'github.issue.comment', ?, ?, 'consumed', ?, ?, ?)",
        (
            approval_id,
            argument_sha,
            json.dumps({"risk_level": "external_write"}),
            "2026-08-26T00:01:01+00:00",
            "2026-08-26T00:01:02+00:00",
            "2026-08-26T00:01:03+00:00",
        ),
    )
    connection.commit()
    connection.close()

    evidence = collect_runtime_evidence(
        database_path,
        _selection(suite, gate, task["id"], "run-formal-1"),
        suite,
        gate,
        environment=ENVIRONMENT,
    )
    result = export_evidence(evidence, suite, gate)["results"][0]
    assert evaluate_task(task, result)["passed"] is True
    digests = [
        event.get("arguments_digest")
        for event in result["events"]
        if event["type"] in {"approval_required", "approval_consumed", "tool_started"}
    ]
    assert digests == ["sha256:" + argument_sha] * 3


def test_persisted_rag_snapshot_becomes_scoped_retrieval_evidence(tmp_path):
    suite, gate = _contracts()
    task = _task(suite, "rag-retrieve-with-citations")
    database_path = tmp_path / "workbench.db"
    connection = _database(database_path)
    source = {
        "kind": "project_knowledge",
        "project_id": "project-a",
        "document_id": "document-1",
        "chunk_id": "chunk-1",
        "citation": {"project_id": "project-a"},
        "content": "",
    }
    _insert_run(
        connection,
        task=task,
        sources=[source],
        private_sources=[source],
    )
    connection.close()

    evidence = collect_runtime_evidence(
        database_path,
        _selection(suite, gate, task["id"], "run-formal-1"),
        suite,
        gate,
        environment=ENVIRONMENT,
    )
    result = export_evidence(evidence, suite, gate)["results"][0]
    assert evaluate_task(task, result)["passed"] is True
    assert result["events"][1]["source_ids"] == ["document-1:chunk-1"]
    assert result["events"][-1]["citations"] == ["document-1:chunk-1"]


def test_cross_project_rag_and_prompt_substitution_fail_closed(tmp_path):
    suite, gate = _contracts()
    task = _task(suite, "rag-project-scope-isolation")
    database_path = tmp_path / "workbench.db"
    connection = _database(database_path)
    source = {
        "kind": "project_knowledge",
        "project_id": "project-b",
        "document_id": "document-b",
        "chunk_id": "chunk-b",
    }
    _insert_run(connection, task=task, sources=[source], private_sources=[source])
    connection.close()
    selection = _selection(suite, gate, task["id"], "run-formal-1")

    with pytest.raises(RuntimeEvidenceError, match="知識來源跨越專案"):
        collect_runtime_evidence(
            database_path, selection, suite, gate, environment=ENVIRONMENT
        )

    connection = sqlite3.connect(database_path)
    connection.execute("UPDATE runs SET sources_json='[]'")
    manifest = json.loads(connection.execute("SELECT input_manifest_json FROM runs").fetchone()[0])
    manifest["user_message"] = "替換後的 prompt"
    connection.execute(
        "UPDATE runs SET input_manifest_json=?", (json.dumps(manifest),)
    )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeEvidenceError, match="實際 prompt 與 suite 不一致"):
        collect_runtime_evidence(
            database_path, selection, suite, gate, environment=ENVIRONMENT
        )


def test_plan_budget_and_repair_epoch_are_derived_from_runtime_events(tmp_path):
    suite, gate = _contracts()
    task = _task(suite, "plan-per-step-tool-budget")
    database_path = tmp_path / "workbench.db"
    connection = _database(database_path)
    base = {"run_id": "run-formal-1", "project_id": "project-a", "plan_id": "plan-1"}
    events = [
        _runtime_event(
            1,
            "plan",
            {
                **base,
                "planner": "host",
                "task_count": 3,
                "tool_call_limit": 8,
                "tool_calls_per_step": 3,
                "wall_seconds": 300,
                "tasks": [
                    {"id": "search", "title": "搜尋", "status": "pending"},
                    {"id": "read", "title": "讀取", "status": "pending"},
                    {"id": "verify", "title": "驗證", "status": "pending"},
                ],
            },
        )
    ]
    sequence = 2
    for step_id, budget in (("search", 2), ("read", 2), ("verify", 0)):
        events.append(
            _runtime_event(
                sequence,
                "task_update",
                {
                    **base,
                    "task_id": step_id,
                    "kind": "tool" if budget else "verify",
                    "status": "in_progress",
                    "message": "執行中",
                    "tool_calls_used": 0,
                    "tool_call_limit": budget,
                    "plan_status": "running",
                },
            )
        )
        sequence += 1
        if budget:
            events.extend(
                [
                    _runtime_event(
                        sequence,
                        "tool_start",
                        {
                            "tool": "search_files" if step_id == "search" else "read_file",
                            "tool_call_id": f"call-{step_id}",
                            "run_id": "run-formal-1",
                            "project_id": "project-a",
                            "args": {"scope": "active_project", "access": "read_only", "details_redacted": True},
                        },
                    ),
                    _runtime_event(
                        sequence + 1,
                        "tool_end",
                        {
                            "tool": "search_files" if step_id == "search" else "read_file",
                            "tool_call_id": f"call-{step_id}",
                            "run_id": "run-formal-1",
                            "project_id": "project-a",
                            "success": True,
                            "result": "completed",
                            "details_redacted": True,
                            "duration_ms": 1,
                        },
                    ),
                ]
            )
            sequence += 2
        events.append(
            _runtime_event(
                sequence,
                "task_update",
                {
                    **base,
                    "task_id": step_id,
                    "kind": "tool" if budget else "verify",
                    "status": "completed",
                    "message": "已完成",
                    "tool_calls_used": 1 if budget else 0,
                    "tool_call_limit": budget,
                    "plan_status": "running",
                },
            )
        )
        sequence += 1
    _insert_run(connection, task=task, events=events)
    connection.close()

    evidence = collect_runtime_evidence(
        database_path,
        _selection(suite, gate, task["id"], "run-formal-1"),
        suite,
        gate,
        environment=ENVIRONMENT,
    )
    result = export_evidence(evidence, suite, gate)["results"][0]
    plan = next(event for event in result["events"] if event["type"] == "plan_created")
    assert [step["tool_budget"] for step in plan["steps"]] == [2, 2, 0]
    assert evaluate_task(task, result)["passed"] is True


def test_failed_terminal_run_is_exported_as_failed_capability_evidence(tmp_path):
    suite, gate = _contracts()
    task = _task(suite, "tool-select-project-search")
    database_path = tmp_path / "workbench.db"
    connection = _database(database_path)
    _insert_run(
        connection,
        task=task,
        status="failed",
        persist_assistant=False,
    )
    connection.close()

    evidence = collect_runtime_evidence(
        database_path,
        _selection(suite, gate, task["id"], "run-formal-1"),
        suite,
        gate,
        environment=ENVIRONMENT,
    )
    assert evidence["runs"][0]["status"] == "failed"
    result = export_evidence(evidence, suite, gate)["results"][0]
    evaluation = evaluate_task(task, result)
    assert evaluation["passed"] is False
    assert "缺少必要工具" in "；".join(evaluation["failures"])
