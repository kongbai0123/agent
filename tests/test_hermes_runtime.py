from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import database  # noqa: E402
from chat.hermes_runtime import (  # noqa: E402
    HERMES_READONLY_TOOL_EVENT_ALLOWLIST,
    stream_hermes_chat,
)
from chat.events import encode_sse  # noqa: E402
from chat_cancellation import ChatRunControl  # noqa: E402
from hermes import HermesRunSnapshot, HermesUnavailableError, SSEEvent  # noqa: E402
from hermes import (  # noqa: E402
    HERMES_CONTEXT_WINDOW_TOKENS,
    HERMES_INTERNAL_RESERVE_TOKENS,
    HERMES_OUTPUT_RESERVE_TOKENS,
    HERMES_WORKBENCH_INPUT_BUDGET_TOKENS,
    HermesContextBudgetError,
    budget_hermes_context,
    estimate_run_input_tokens,
)
from hermes_integration import HermesIntegrationDecision  # noqa: E402
from hermes_project_skills_bridge import HermesProjectSkillsAttachment  # noqa: E402
from factual_verifier import EvidenceBundle, EvidenceRecord  # noqa: E402


def parse_events(frames):
    parsed = []
    for frame in frames:
        lines = frame.strip().splitlines()
        parsed.append((lines[0].split(":", 1)[1].strip(), json.loads(lines[1][6:])))
    return parsed


class FakeApprovalStore:
    def __init__(self):
        self.expired = []

    def expire_run(self, run_id):
        self.expired.append(run_id)


class FakeRuns:
    def __init__(self, events, status=None):
        self._events = events
        self._status = status

    @contextmanager
    def open_events(self, _run_id):
        yield iter(self._events)

    def status(self, run_id):
        return self._status or HermesRunSnapshot(
            run_id, "session", "upstream", "failed", {}
        )


class FakeApproval:
    approval_id = "approval-1"
    capability = "hermes.tool"
    summary = "Run a bounded terminal command."
    status = "pending"

    def public_dict(self):
        return {
            "approval_id": self.approval_id,
            "capability": self.capability,
            "summary": self.summary,
            "status": self.status,
        }


class FakeManager:
    def __init__(
        self,
        events=(),
        start_error=None,
        fallback_safe=False,
        prepare_error=None,
        project_id=None,
    ):
        self.config = SimpleNamespace(default_model="hermes-agent")
        self.runs = FakeRuns(events)
        self.approval_store = FakeApprovalStore()
        self.start_error = start_error
        self.prepare_error = prepare_error
        self.fallback_safe = fallback_safe
        self.project_id = project_id
        self.completed = []
        self.cancelled = []
        self.approvals = []
        self.abandoned = []
        self.start_calls = []

    def prepare_project_skills(self, session_id, _query, *, run_id, consume_turn):
        if self.prepare_error:
            raise self.prepare_error
        return HermesProjectSkillsAttachment(
            session_id, self.project_id, run_id, "", (), False
        )

    def decide(self, _session_id):
        return HermesIntegrationDecision(True, "rollout_all", "", SimpleNamespace())

    def start_run(self, **kwargs):
        self.start_calls.append(dict(kwargs))
        if self.start_error:
            raise self.start_error
        return HermesRunSnapshot(
            kwargs["workbench_run_id"],
            kwargs["workbench_session_id"],
            "upstream-1",
            "started",
            {},
        )

    def complete(self, decision, *, success, failure_kind=""):
        self.completed.append((decision, success, failure_kind))

    def abandon(self, decision, *, reason="cancelled"):
        self.abandoned.append((decision, reason))

    def fallback_allowed(self, _run_id, _exc, *, token_emitted):
        return self.fallback_safe and not token_emitted

    def register_approval(self, **kwargs):
        self.approvals.append(kwargs)
        return FakeApproval()

    def cancel(self, run_id):
        self.cancelled.append(run_id)
        return {"run_id": run_id, "cancelled": True}


def setup_turn(label, *, project_id=None):
    database.init_db()
    session_id = f"hermes-runtime-session-{label}"
    turn_id = f"turn-{label}"
    run_id = f"run-{label}"
    if project_id:
        database.create_project(project_id, project_id, str(ROOT))
    database.create_session(session_id, project_id=project_id)
    user_id = database.add_message(
        session_id,
        "user",
        "hello",
        turn_id=turn_id,
    )
    control = ChatRunControl(run_id, session_id, turn_id, "hermes-agent", "chat")
    control.start_deadline(60)
    return session_id, turn_id, run_id, user_id, control


async def collect(
    manager,
    label,
    fallback=None,
    *,
    temporary_context="",
    knowledge_context="",
    knowledge_sources=None,
    evidence_bundle=None,
    answer_verification_mode="warn",
):
    session_id, turn_id, run_id, user_id, control = setup_turn(
        label, project_id=manager.project_id
    )

    async def basic(received_attachment):
        manager.fallback_attachment = received_attachment
        if fallback:
            for frame in fallback:
                yield frame

    frames = []
    async for frame in stream_hermes_chat(
        manager=manager,
        model="ignored-workbench-model",
        session_id=session_id,
        turn_id=turn_id,
        run_id=run_id,
        prompt_sha256="digest",
        user_message_id=user_id,
        user_query="hello",
        temporary_context=temporary_context,
        knowledge_context=knowledge_context,
        knowledge_sources=knowledge_sources,
        evidence_bundle=evidence_bundle,
        answer_verification_mode=answer_verification_mode,
        run_control=control,
        fallback_stream_factory=basic,
    ):
        frames.append(frame)
    return parse_events(frames), session_id, run_id


def factual_evidence_fixture(*, project_id, content="法國的首都是巴黎。"):
    context = (
        "[evidence:knowledge:chunk-one]\n"
        f"[知識來源 1：專案指南]\n{content}"
    )
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    citation = {
        "project_id": project_id,
        "source_id": "guide.md",
        "title": "專案指南",
        "document_id": "document-one",
        "chunk_id": "chunk-one",
        "chunk_sha256": digest,
    }
    sources = [
        {
            "kind": "project_knowledge",
            "project_id": project_id,
            "source": "專案指南",
            "document_id": "document-one",
            "chunk_id": "chunk-one",
            "snippet_sha256": digest,
            "citation": citation,
        }
    ]
    evidence = EvidenceBundle(
        (
            EvidenceRecord(
                "knowledge:chunk-one",
                content,
                project_id=project_id,
                citation=citation,
            ),
        ),
        project_id=project_id,
    )
    return context, sources, evidence


def test_enabled_hermes_receives_bounded_rag_and_persists_only_safe_citations():
    project_id = "project-hermes-rag"
    raw_source_snippet = "RAW-SOURCE-SNIPPET-MUST-NOT-PERSIST"
    knowledge_context = "[知識來源 1：規格]\nSIDECAR-RAG-CONTEXT"
    manager = FakeManager(
        [
            SSEEvent(
                "message",
                json.dumps(
                    {
                        "event": "run.completed",
                        "output": "RAG answer",
                        "usage": {},
                    }
                ),
            )
        ],
        project_id=project_id,
    )
    sources = [
        {
            "kind": "project_knowledge",
            "project_id": project_id,
            "source": "規格",
            "content": raw_source_snippet,
            "score": 0.9,
            "document_id": "document-one",
            "chunk_id": "chunk-one",
            "snippet_sha256": "a" * 64,
            "citation": {
                "project_id": project_id,
                "document_id": "document-one",
                "chunk_id": "chunk-one",
                "title": "規格",
                "chunk_sha256": "b" * 64,
                "private_excerpt": raw_source_snippet,
            },
        }
    ]

    events, session_id, run_id = asyncio.run(
        collect(
            manager,
            "enabled-rag",
            knowledge_context=knowledge_context,
            knowledge_sources=sources,
        )
    )

    instructions = manager.start_calls[0]["base_instructions"]
    assert "SIDECAR-RAG-CONTEXT" in instructions
    assert "未受信任的 Workbench Project 知識片段" in instructions
    assert events[-2][1]["hermes_context_budget"]["knowledge_source_count"] == 1
    assert events[-2][1]["hermes_context_budget"][
        "knowledge_context_input_chars"
    ] == len(knowledge_context)
    run = database.get_run(run_id)
    message = database.get_messages_by_session(session_id)[-1]
    knowledge_run_sources = [
        item for item in run["sources"] if item.get("kind") == "project_knowledge"
    ]
    knowledge_message_sources = [
        item for item in message["sources"] if item.get("kind") == "project_knowledge"
    ]
    assert knowledge_run_sources == knowledge_message_sources
    assert knowledge_run_sources[0]["content"] == ""
    assert knowledge_run_sources[0]["snippet_sha256"] == "a" * 64
    persisted = json.dumps(
        {"events": events, "run_sources": run["sources"], "message_sources": message["sources"]},
        ensure_ascii=False,
    )
    assert raw_source_snippet not in persisted
    assert "private_excerpt" not in persisted


def test_hermes_strict_factual_gate_never_emits_or_persists_unverified_answer():
    project_id = "project-hermes-factual-strict"
    context, sources, evidence = factual_evidence_fixture(project_id=project_id)
    unverified_answer = "法國的首都是里昂。[evidence:knowledge:chunk-one]"
    manager = FakeManager(
        [
            SSEEvent(
                "message",
                json.dumps(
                    {
                        "event": "message.delta",
                        "delta": unverified_answer,
                    }
                ),
            ),
            SSEEvent(
                "message",
                json.dumps(
                    {
                        "event": "run.completed",
                        "output": unverified_answer,
                        "usage": {},
                    }
                ),
            ),
        ],
        project_id=project_id,
    )

    events, session_id, run_id = asyncio.run(
        collect(
            manager,
            "factual-strict",
            knowledge_context=context,
            knowledge_sources=sources,
            evidence_bundle=evidence,
            answer_verification_mode="strict",
        )
    )

    assert [name for name, _payload in events] == ["meta", "validation", "error"]
    assert not any(name == "token" for name, _payload in events)
    validation = events[1][1]
    assert validation["name"] == "answer_factuality"
    assert validation["passed"] is False
    assert events[-1][1]["code"] == "ANSWER_FACT_VERIFICATION_FAILED"
    assert database.get_run(run_id)["status"] == "failed"
    assert [message["role"] for message in database.get_messages_by_session(session_id)] == [
        "user"
    ]
    assert manager.abandoned[-1][1] == "answer_verification_failed"
    public_state = json.dumps(
        {"events": events, "run": database.get_run(run_id)},
        ensure_ascii=False,
    )
    assert unverified_answer not in public_state


def test_hermes_strict_rejection_cleanup_failure_cannot_trigger_fallback(monkeypatch):
    project_id = "project-hermes-factual-cleanup"
    context, sources, evidence = factual_evidence_fixture(project_id=project_id)
    unverified_answer = "法國的首都是里昂。[evidence:knowledge:chunk-one]"
    fallback = [encode_sse("token", {"content": "must-not-fallback"})]
    manager = FakeManager(
        [
            SSEEvent(
                "message",
                json.dumps(
                    {
                        "event": "run.completed",
                        "output": unverified_answer,
                        "usage": {},
                    }
                ),
            )
        ],
        fallback_safe=True,
        project_id=project_id,
    )

    def fail_persistence(**_kwargs):
        raise OSError("injected failure persistence error")

    def fail_abandon(_decision, *, reason="cancelled"):
        raise RuntimeError(reason)

    monkeypatch.setattr("chat.hermes_runtime._persist_failure", fail_persistence)
    manager.abandon = fail_abandon

    events, session_id, _run_id = asyncio.run(
        collect(
            manager,
            "factual-cleanup",
            fallback,
            knowledge_context=context,
            knowledge_sources=sources,
            evidence_bundle=evidence,
            answer_verification_mode="strict",
        )
    )

    assert [name for name, _payload in events] == ["meta", "validation", "error"]
    assert "must-not-fallback" not in json.dumps(events)
    assert unverified_answer not in json.dumps(events, ensure_ascii=False)
    assert [message["role"] for message in database.get_messages_by_session(session_id)] == [
        "user"
    ]


def test_hermes_warn_gate_adds_fixed_warning_without_exposing_evidence_text():
    project_id = "project-hermes-factual-warn"
    raw_evidence = "法國的首都是巴黎。PRIVATE-EVIDENCE-MUST-NOT-LEAK"
    context, sources, evidence = factual_evidence_fixture(
        project_id=project_id,
        content=raw_evidence,
    )
    unverified_answer = "法國的首都是里昂。[evidence:knowledge:chunk-one]"
    manager = FakeManager(
        [
            SSEEvent(
                "message",
                json.dumps(
                    {
                        "event": "run.completed",
                        "output": unverified_answer,
                        "usage": {},
                    }
                ),
            )
        ],
        project_id=project_id,
    )

    events, session_id, run_id = asyncio.run(
        collect(
            manager,
            "factual-warn",
            knowledge_context=context,
            knowledge_sources=sources,
            evidence_bundle=evidence,
        )
    )

    assert [name for name, _payload in events] == [
        "meta",
        "validation",
        "token",
        "token",
        "metrics",
        "done",
    ]
    visible = "".join(
        payload["content"] for name, payload in events if name == "token"
    )
    assert visible.startswith("⚠️ 事實驗證提醒：")
    assert visible.endswith(unverified_answer)
    assert database.get_messages_by_session(session_id)[-1]["content"] == visible
    assert "事實引用規則" in manager.start_calls[0]["base_instructions"]
    public_state = json.dumps(
        {
            "events": events,
            "run": database.get_run(run_id),
            "message": database.get_messages_by_session(session_id)[-1],
        },
        ensure_ascii=False,
    )
    assert "PRIVATE-EVIDENCE-MUST-NOT-LEAK" not in public_state


def test_hermes_verified_answer_passes_gate_before_first_token():
    project_id = "project-hermes-factual-verified"
    context, sources, evidence = factual_evidence_fixture(project_id=project_id)
    verified_answer = "法國的首都是巴黎。[evidence:knowledge:chunk-one]"
    manager = FakeManager(
        [
            SSEEvent(
                "message",
                json.dumps(
                    {
                        "event": "run.completed",
                        "output": verified_answer,
                        "usage": {},
                    }
                ),
            )
        ],
        project_id=project_id,
    )

    events, session_id, _run_id = asyncio.run(
        collect(
            manager,
            "factual-verified",
            knowledge_context=context,
            knowledge_sources=sources,
            evidence_bundle=evidence,
            answer_verification_mode="strict",
        )
    )

    assert [name for name, _payload in events] == [
        "meta",
        "validation",
        "token",
        "metrics",
        "done",
    ]
    assert events[1][1]["passed"] is True
    assert events[2][1]["content"] == verified_answer
    assert database.get_messages_by_session(session_id)[-1]["content"] == verified_answer


def test_pinned_v0182_events_fill_missing_authoritative_suffix_and_persist():
    manager = FakeManager(
        [
            SSEEvent("message", json.dumps({"event": "message.delta", "delta": "Hel"})),
            SSEEvent("message", json.dumps({"event": "message.delta", "delta": "lo"})),
            SSEEvent(
                "message",
                json.dumps(
                    {
                        "event": "run.completed",
                        "output": "Hello world",
                        "usage": {
                            "input_tokens": 4,
                            "output_tokens": 3,
                            "total_tokens": 7,
                        },
                    }
                ),
            ),
        ]
    )
    events, session_id, _run_id = asyncio.run(collect(manager, "success"))

    assert [name for name, _ in events] == [
        "meta", "token", "token", "token", "metrics", "done"
    ]
    assert "".join(data["content"] for name, data in events if name == "token") == "Hello world"
    assert events[-2][1]["usage"] == {
        "prompt_tokens": 4,
        "completion_tokens": 3,
        "total_tokens": 7,
    }
    messages = database.get_messages_by_session(session_id)
    assert messages[-1]["content"] == "Hello world"
    assert manager.completed[-1][1] is True


def test_hermes_terminal_event_failure_never_fails_or_falls_back_after_completion(
    monkeypatch,
):
    original_append = database.append_run_event

    def fail_terminal_evidence(run_id, event, payload):
        if event in {"metrics", "done"}:
            raise OSError("injected terminal evidence failure")
        return original_append(run_id, event, payload)

    monkeypatch.setattr(database, "append_run_event", fail_terminal_evidence)
    fallback = [encode_sse("token", {"content": "must-not-be-emitted"})]
    manager = FakeManager(
        [
            SSEEvent(
                "message",
                json.dumps(
                    {
                        "event": "run.completed",
                        "output": "one durable Hermes answer",
                        "usage": {},
                    }
                ),
            )
        ],
        fallback_safe=True,
    )

    events, session_id, run_id = asyncio.run(
        collect(manager, "terminal-evidence-failure", fallback)
    )

    assert [name for name, _ in events] == ["meta", "token", "metrics", "done"]
    assert "must-not-be-emitted" not in json.dumps(events)
    assert database.get_run(run_id)["status"] == "completed"
    current_assistants = [
        message
        for message in database.get_messages_by_session(session_id)
        if message["role"] == "assistant"
        and message["turn_id"] == "turn-terminal-evidence-failure"
    ]
    assert [message["content"] for message in current_assistants] == [
        "one durable Hermes answer"
    ]
    assert [success for _decision, success, _kind in manager.completed] == [True]
    assert not hasattr(manager, "fallback_attachment")


def test_hermes_completion_persists_generated_artifact_refs():
    manager = FakeManager(
        [
            SSEEvent(
                "message",
                json.dumps(
                    {
                        "event": "run.completed",
                        "output": "```svg\n<svg viewBox=\"0 0 1 1\"></svg>\n```",
                        "usage": {},
                    }
                ),
            )
        ]
    )

    _events, session_id, run_id = asyncio.run(
        collect(manager, "generated-artifact")
    )

    message = database.get_messages_by_session(session_id)[-1]
    run = database.get_run(run_id)
    assert len(message["artifacts"]) == 1
    assert message["artifacts"] == run["artifacts"]
    artifact_id = message["artifacts"][0]["artifact_id"]
    artifact = database.get_artifact(artifact_id)
    assert artifact["session_id"] == session_id
    assert artifact["turn_id"] == "turn-generated-artifact"
    assert artifact["files"] == [
        {
            "path": "generated-01.svg",
            "content": '<svg viewBox="0 0 1 1"></svg>\n',
            "language": "svg",
        }
    ]
    assert any(
        event["event"] == "artifact"
        and event["payload"]["artifact_id"] == artifact_id
        for event in run["events"]
    )


def test_approval_event_matches_existing_frontend_contract():
    manager = FakeManager(
        [
            SSEEvent(
                "message",
                json.dumps(
                    {
                        "event": "approval.request",
                        "timestamp": 1,
                        "command": "echo safe",
                        "choices": ["once", "deny"],
                        "risk": "high",
                    }
                ),
            ),
            SSEEvent(
                "message",
                json.dumps(
                    {
                        "event": "run.completed",
                        "output": "done",
                        "usage": {},
                    }
                ),
            ),
        ]
    )
    events, _session_id, run_id = asyncio.run(collect(manager, "approval"))
    approval = next(data for name, data in events if name == "approval_required")
    assert approval == {
        "approval_id": "approval-1",
        "capability": "hermes.tool",
        "message": "Run a bounded terminal command.",
        "run_id": run_id,
        "risk": "high",
    }


@pytest.mark.parametrize(
    ("tool", "label"),
    [
        ("project_read_file", "tool-read"),
        ("project_search_files", "tool-search"),
    ],
)
def test_readonly_tool_events_match_frontend_contract_without_sensitive_data(
    tool, label
):
    manager = FakeManager(
        [
            SSEEvent(
                "message",
                json.dumps(
                    {
                        "event": "tool.started",
                        "tool": tool,
                        "preview": "D:/private/project/secret.txt top-secret-query",
                        "args": {
                            "path": "D:/private/project/secret.txt",
                            "query": "top-secret-query",
                            "content": "top-secret-content",
                        },
                    }
                ),
            ),
            SSEEvent(
                "message",
                json.dumps(
                    {
                        "event": "tool.completed",
                        "tool": tool,
                        "duration": 0.125,
                        "error": False,
                        "result": {
                            "path": "D:/private/project/secret.txt",
                            "content": "top-secret-result",
                        },
                        "output": "top-secret-output",
                    }
                ),
            ),
            SSEEvent(
                "message",
                json.dumps(
                    {
                        "event": "run.completed",
                        "output": "done",
                        "usage": {},
                    }
                ),
            ),
        ],
        project_id=f"project-{label}",
    )

    events, _session_id, run_id = asyncio.run(collect(manager, label))

    assert HERMES_READONLY_TOOL_EVENT_ALLOWLIST == frozenset(
        {"project_read_file", "project_search_files"}
    )
    assert [name for name, _ in events] == [
        "meta",
        "tool_start",
        "tool_end",
        "token",
        "metrics",
        "done",
    ]
    started = next(data for name, data in events if name == "tool_start")
    completed = next(data for name, data in events if name == "tool_end")
    assert started == {
        "tool": tool,
        "tool_call_id": "hermes-readonly-1",
        "sequence": 1,
        "run_id": run_id,
        "args": {
            "scope": "active_project",
            "access": "read_only",
            "details_redacted": True,
        },
    }
    assert completed == {
        "tool": tool,
        "tool_call_id": "hermes-readonly-1",
        "sequence": 1,
        "run_id": run_id,
        "success": True,
        "result": "completed",
        "details_redacted": True,
        "duration_ms": 125,
    }
    public_tool_events = json.dumps([started, completed], ensure_ascii=False)
    for secret in (
        "D:/private/project/secret.txt",
        "top-secret-query",
        "top-secret-content",
        "top-secret-result",
        "top-secret-output",
    ):
        assert secret not in public_tool_events
    assert set(started["args"]) == {"scope", "access", "details_redacted"}


def test_readonly_tool_failure_is_reduced_to_boolean_status():
    manager = FakeManager(
        [
            SSEEvent(
                "message",
                json.dumps(
                    {"event": "tool.started", "tool": "project_read_file"}
                ),
            ),
            SSEEvent(
                "message",
                json.dumps(
                    {
                        "event": "tool.completed",
                        "tool": "project_read_file",
                        "error": {"message": "raw filesystem failure secret"},
                        "result": "raw file content",
                        "duration": float("inf"),
                    }
                ),
            ),
            SSEEvent(
                "message",
                json.dumps(
                    {"event": "run.completed", "output": "safe answer", "usage": {}}
                ),
            ),
        ],
        project_id="project-tool-error",
    )

    events, _session_id, _run_id = asyncio.run(collect(manager, "tool-error"))

    completed = next(data for name, data in events if name == "tool_end")
    assert completed["success"] is False
    assert completed["result"] == "error"
    assert "duration_ms" not in completed
    assert "raw filesystem failure secret" not in json.dumps(events)
    assert "raw file content" not in json.dumps(events)


@pytest.mark.parametrize(
    ("event_name", "tool", "label"),
    [
        ("tool.started", "terminal", "denied-start"),
        ("tool.completed", "write_file", "denied-complete"),
        ("tool.failed", "project_read_file", "denied-event-type"),
        ("tool.started", None, "denied-missing-name"),
        ("tool.completed", "project_read_file", "denied-unmatched-complete"),
    ],
)
def test_non_allowlisted_or_malformed_tool_events_fail_closed_without_fallback(
    event_name, tool, label
):
    payload = {
        "event": event_name,
        "tool": tool,
        "preview": "D:/must-not-leak.txt",
        "result": "must-not-leak-result",
    }
    fallback = [encode_sse("token", {"content": "must-not-fallback"})]
    manager = FakeManager(
        [
            SSEEvent("message", json.dumps(payload)),
            SSEEvent(
                "message",
                json.dumps(
                    {"event": "run.completed", "output": "must-not-complete"}
                ),
            ),
        ],
        fallback_safe=True,
        project_id=f"project-{label}",
    )

    events, _session_id, run_id = asyncio.run(collect(manager, label, fallback))

    assert [name for name, _ in events] == ["meta", "error"]
    error = events[-1][1]
    assert error["code"] == "HERMES_TOOL_EVENT_DENIED"
    assert error["recoverable"] is False
    encoded = json.dumps(events, ensure_ascii=False)
    assert "D:/must-not-leak.txt" not in encoded
    assert "must-not-leak-result" not in encoded
    assert "must-not-fallback" not in encoded
    assert "must-not-complete" not in encoded
    assert manager.cancelled == [run_id]
    assert manager.completed[-1][1:] == (False, "tool_policy_denied")
    assert database.get_run(run_id)["status"] == "failed"


def test_project_tool_event_without_active_project_fails_closed():
    manager = FakeManager(
        [
            SSEEvent(
                "message",
                json.dumps(
                    {"event": "tool.started", "tool": "project_read_file"}
                ),
            )
        ],
        fallback_safe=True,
    )

    events, _session_id, _run_id = asyncio.run(
        collect(manager, "tool-without-project")
    )

    assert [name for name, _ in events] == ["meta", "error"]
    assert events[-1][1]["code"] == "HERMES_TOOL_EVENT_DENIED"


def test_run_completion_with_unfinished_readonly_tool_fails_closed():
    manager = FakeManager(
        [
            SSEEvent(
                "message",
                json.dumps(
                    {"event": "tool.started", "tool": "project_search_files"}
                ),
            ),
            SSEEvent(
                "message",
                json.dumps(
                    {"event": "run.completed", "output": "must-not-complete"}
                ),
            ),
        ],
        project_id="project-unfinished-tool",
    )

    events, _session_id, _run_id = asyncio.run(
        collect(manager, "unfinished-tool")
    )

    assert [name for name, _ in events] == ["meta", "tool_start", "error"]
    assert events[-1][1]["code"] == "HERMES_TOOL_EVENT_DENIED"
    assert "must-not-complete" not in json.dumps(events)


def test_pre_submission_unavailable_can_fallback_without_duplicate_meta():
    fallback = [
        encode_sse("meta", {"runtime": "basic_chat"}),
        encode_sse("token", {"content": "fallback"}),
        encode_sse("metrics", {"runtime": "basic_chat"}),
        encode_sse("done", {"run_id": "fallback"}),
    ]
    manager = FakeManager(
        start_error=HermesUnavailableError("offline"), fallback_safe=True
    )
    events, _session_id, _run_id = asyncio.run(
        collect(manager, "fallback-safe", fallback)
    )
    assert [name for name, _ in events] == ["meta", "token", "metrics", "done"]
    assert events[0][1]["runtime"] == "basic_chat"
    assert manager.fallback_attachment.session_id.endswith("fallback-safe")


def test_project_skill_preflight_failure_is_bounded_and_persisted():
    manager = FakeManager(prepare_error=RuntimeError("raw project skill secret"))
    events, _session_id, run_id = asyncio.run(collect(manager, "preflight-error"))
    assert [name for name, _ in events] == ["error"]
    assert "raw project skill secret" not in json.dumps(events)
    assert database.get_run(run_id)["status"] == "failed"


def test_submission_unknown_or_post_token_failure_never_falls_back():
    fallback = [encode_sse("token", {"content": "must-not-run"})]
    unknown = FakeManager(
        start_error=HermesUnavailableError("timeout"), fallback_safe=False
    )
    events, _session_id, _run_id = asyncio.run(
        collect(unknown, "fallback-unsafe", fallback)
    )
    assert [name for name, _ in events] == ["error"]

    post_token = FakeManager(
        [
            SSEEvent("message", json.dumps({"event": "message.delta", "delta": "partial"})),
            SSEEvent("message", json.dumps({"event": "run.failed", "error": "raw secret"})),
        ],
        fallback_safe=True,
    )
    events, _session_id, _run_id = asyncio.run(
        collect(post_token, "after-token", fallback)
    )
    assert [name for name, _ in events] == ["meta", "token", "error"]
    assert "raw secret" not in json.dumps(events)


def test_user_cancellation_does_not_trip_operations_failure():
    manager = FakeManager(
        [SSEEvent("message", json.dumps({"event": "run.cancelled"}))]
    )
    events, _session_id, run_id = asyncio.run(collect(manager, "cancelled"))
    assert [name for name, _ in events] == ["meta", "cancelled"]
    assert manager.cancelled == [run_id]
    assert manager.completed == []
    assert manager.abandoned[-1][1] == "cancelled"


def test_shared_context_budget_preserves_skills_and_user_before_optional_context():
    skill_tail = "--- END WORKBENCH PROJECT SKILLS ATTACHMENT ---"
    skill_attachment = "專" * 32_000 + skill_tail
    history = []
    for index in range(12):
        history.extend(
            (
                {"role": "user", "content": f"old-{index}-" + "歷" * 1_990},
                {"role": "assistant", "content": "答" * 2_000},
            )
        )
    user_input = "用" * 4_000

    budgeted = budget_hermes_context(
        user_input=user_input,
        fixed_instructions="Workbench fixed safety policy.",
        project_skill_instructions=skill_attachment,
        temporary_context="暫" * 24_000,
        history=history,
    )
    merged = budgeted.base_instructions + "\n\n" + skill_attachment

    assert merged.endswith(skill_tail)
    assert budgeted.history_messages_dropped == len(history)
    assert budgeted.temporary_context_truncated is True
    assert estimate_run_input_tokens(user_input, merged, budgeted.history) <= (
        HERMES_WORKBENCH_INPUT_BUDGET_TOKENS
    )
    assert HERMES_CONTEXT_WINDOW_TOKENS == 64_000
    assert HERMES_OUTPUT_RESERVE_TOKENS >= 4_096
    assert HERMES_INTERNAL_RESERVE_TOKENS >= 4_096


def test_shared_context_budget_fails_when_mandatory_scoped_input_cannot_fit():
    with pytest.raises(HermesContextBudgetError):
        budget_hermes_context(
            user_input="用" * 8_000,
            fixed_instructions="fixed",
            project_skill_instructions="專" * 50_000,
            temporary_context="optional",
            history=[],
        )
