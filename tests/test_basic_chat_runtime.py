from __future__ import annotations

import asyncio
import ast
import builtins
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from chat import runtime as chat_runtime
from basic_chat_services import DisabledRAGEngine, build_rag_service
from chat_cancellation import ChatRunControl
from host_tools import HostToolRuntime
from tool_runtime import (
    ToolAccess,
    ToolDefinition,
    ToolDispatcher,
    ToolRegistry,
    ToolScopeState,
)


class FakeDatabase:
    def __init__(self):
        self.messages = [
            {
                "id": 1,
                "session_id": "sess_basic",
                "role": "user",
                "content": "previous question",
                "llm_content": "previous question",
                "turn_id": "turn_previous",
                "parent_message_id": None,
            },
            {
                "id": 2,
                "session_id": "sess_basic",
                "role": "assistant",
                "content": "previous answer",
                "llm_content": "previous answer",
                "turn_id": "turn_previous",
                "parent_message_id": 1,
            },
            {
                "id": 3,
                "session_id": "sess_basic",
                "role": "user",
                "content": "current question",
                "llm_content": "current question",
                "turn_id": "turn_current",
                "parent_message_id": None,
            },
        ]
        self.runs = []
        self.title = None
        self.artifacts = {}
        self.public_events = []

    def get_messages_by_session(self, _session_id):
        return [dict(item) for item in self.messages]

    def get_session(self, session_id):
        project_id = self.runs[-1].get("project_id") if self.runs else None
        return {"id": session_id, "project_id": project_id}

    def add_message(self, session_id, role, content, **kwargs):
        message_id = len(self.messages) + 1
        self.messages.append({
            "id": message_id,
            "session_id": session_id,
            "role": role,
            "content": content,
            "llm_content": kwargs.get("llm_content"),
            "turn_id": kwargs.get("turn_id"),
            "parent_message_id": kwargs.get("parent_message_id"),
            "process_events": kwargs.get("process_events"),
            "sources": kwargs.get("sources"),
            "artifacts": kwargs.get("artifacts"),
        })
        return message_id

    def update_session_title(self, _session_id, title):
        self.title = title
        return True

    def upsert_run(self, *args, **kwargs):
        self.runs.append({
            "run_id": args[0],
            "status": args[5],
            "metrics": kwargs.get("metrics") or {},
            "events": kwargs.get("events"),
            "sources": kwargs.get("sources"),
            "artifacts": kwargs.get("artifacts"),
            "project_id": kwargs.get("project_id"),
            "provided": set(kwargs),
        })

    def save_artifact(
        self, artifact_id, session_id, turn_id, title, artifact_type, files
    ):
        self.artifacts[artifact_id] = {
            "artifact_id": artifact_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "title": title,
            "type": artifact_type,
            "files": files,
        }

    def append_run_event(self, run_id, event, payload):
        self.public_events.append((run_id, event, payload))
        return {"sequence": len(self.public_events)}


class FakeResponse:
    def __init__(self, lines=None, status_code=200, text=""):
        self._lines = list(lines or [])
        self.status_code = status_code
        self.text = text
        self.closed = False

    def iter_lines(self):
        yield from self._lines

    def close(self):
        self.closed = True


def encoded_chunk(content="", *, done=False, **metrics):
    payload = {"message": {"content": content}, "done": done, **metrics}
    return json.dumps(payload).encode("utf-8")


def parse_sse(items):
    parsed = []
    for item in items:
        lines = item.strip().splitlines()
        event = lines[0].split(":", 1)[1].strip()
        data = json.loads(lines[1].split(":", 1)[1].strip())
        parsed.append((event, data))
    return parsed


async def collect_stream(**kwargs):
    return [item async for item in chat_runtime.stream_basic_chat(**kwargs)]


def test_basic_prompt_uses_only_complete_history_and_one_current_user():
    persisted = [
        {"id": 1, "role": "user", "llm_content": "paired user", "turn_id": "old", "parent_message_id": None},
        {"id": 2, "role": "assistant", "llm_content": "paired answer", "turn_id": "old", "parent_message_id": 1},
        {"id": 3, "role": "user", "llm_content": "failed orphan", "turn_id": "failed", "parent_message_id": None},
        {"id": 4, "role": "user", "llm_content": "current", "turn_id": "current", "parent_message_id": None},
    ]
    request_messages = [
        SimpleNamespace(role="user", content="paired user"),
        SimpleNamespace(role="assistant", content="paired answer"),
        SimpleNamespace(role="user", content="current"),
    ]

    messages = chat_runtime.build_basic_messages(
        persisted_messages=persisted,
        request_messages=request_messages,
        user_query="current",
        current_turn_id="current",
        temporary_context="explicit context",
        images=["image-data"],
    )

    assert [item["role"] for item in messages] == ["system", "user", "assistant", "user"]
    assert [item["content"] for item in messages].count("current") == 1
    assert "failed orphan" not in [item["content"] for item in messages]
    assert "explicit context" in messages[0]["content"]
    assert messages[-1]["images"] == ["image-data"]
    assert "only when they are explicitly supplied" in messages[0]["content"]
    assert "Do not claim to use tools" not in messages[0]["content"]


def test_independent_task_tool_note_explains_project_requirement():
    payload = {
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "open Chrome"},
        ]
    }
    result = chat_runtime._payload_with_tool_availability_note(
        payload,
        "No tools were supplied because this conversation is not assigned to a Project.",
    )

    assert result is not payload
    assert result["messages"] is not payload["messages"]
    assert "not assigned to a Project" in result["messages"][0]["content"]
    assert payload["messages"][0]["content"] == "system"


def test_basic_stream_has_no_agent_events_or_tool_payload(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(chat_runtime, "database", fake_db)
    captured = {}
    response = FakeResponse([
        encoded_chunk("hello "),
        encoded_chunk("world"),
        encoded_chunk(
            done=True,
            prompt_eval_count=12,
            eval_count=2,
            load_duration=100,
            eval_duration=1_000_000_000,
            done_reason="stop",
        ),
    ])

    def fake_post_chat(settings, payload, **kwargs):
        captured["settings"] = settings
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return response

    control = ChatRunControl(
        "run_basic1234",
        "sess_basic",
        "turn_current",
        "model-a",
        "chat",
    )
    control.start_deadline(60)
    control.set_preexisting_models(set())
    archived = []
    request = SimpleNamespace(messages=[
        SimpleNamespace(role="user", content="previous question"),
        SimpleNamespace(role="assistant", content="previous answer"),
        SimpleNamespace(role="user", content="current question"),
    ])

    items = asyncio.run(collect_stream(
        request=request,
        settings={"ollama_url": "http://127.0.0.1:11434", "model_provider": "ollama"},
        model="model-a",
        session_id="sess_basic",
        turn_id="turn_current",
        run_id="run_basic1234",
        prompt_sha256="digest",
        user_message_id=3,
        user_query="current question",
        temporary_context="",
        images=[],
        run_control=control,
        project_id="project-one",
        project_skill_sources=[
            {
                "kind": "project_skill",
                "project_id": "project-one",
                "slug": "release-review",
                "version": "1.2.3",
                "trigger_mode": "session",
            }
        ],
        archive_sync=lambda session_id: archived.append(session_id) or True,
        post_chat=fake_post_chat,
    ))

    events = parse_sse(items)
    event_names = [event for event, _ in events]
    assert event_names == ["meta", "token", "token", "metrics", "done"]
    assert not set(event_names) & {
        "plan", "task_update", "tool_start", "tool_end", "agent_spawned",
        "validation", "repair", "sources", "approval_required", "final",
    }
    assert events[0][1]["runtime"] == "chat"
    assert "features" not in events[0][1]
    assert captured["payload"]["keep_alive"] == 0
    assert "tools" not in captured["payload"]
    assert "tool_choice" not in captured["payload"]
    assert [item["content"] for item in captured["payload"]["messages"]].count("current question") == 1
    assert response.closed
    assert archived == ["sess_basic"]
    assistant = fake_db.messages[-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "hello world"
    assert assistant["turn_id"] == "turn_current"
    assert assistant["parent_message_id"] == 3
    assert assistant["process_events"] == []
    assert assistant["sources"] == [
        {
            "kind": "workbench_project_skill",
            "project_id": "project-one",
            "slug": "release-review",
            "version": "1.2.3",
            "trigger_mode": "session",
        }
    ]
    assert fake_db.runs[-1]["status"] == "completed"
    assert all("events" not in item["provided"] for item in fake_db.runs)
    assert all("artifacts" not in item["provided"] for item in fake_db.runs)
    assert fake_db.runs[0]["sources"] == assistant["sources"]
    assert fake_db.runs[-1]["sources"] == assistant["sources"]


def test_basic_stream_runs_project_scoped_read_tool_before_final_answer(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(chat_runtime, "database", fake_db)
    digest = hashlib.sha256(b"github-test").hexdigest()
    definition = ToolDefinition(
        name="github.read_file",
        description="Read one allowed repository file",
        input_schema={
            "type": "object",
            "properties": {
                "repository": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["repository", "path"],
            "additionalProperties": False,
        },
        access=ToolAccess.READ,
        handler=lambda call: {"path": call.arguments["path"], "content": "hello"},
        extension_id="connector.github",
        manifest_sha256=digest,
    )
    registry = ToolRegistry([definition])
    dispatcher = ToolDispatcher(
        registry,
        scope_resolver=lambda _definition, _call: ToolScopeState(
            installed=True,
            trusted=True,
            enabled=True,
            healthy=True,
            resource_allowed=True,
            manifest_sha256=digest,
        ),
    )

    class ReadOnlyApprovals:
        def event_queue(self, _run_id):
            return asyncio.Queue()

        async def approval_callback(self, _request):
            raise AssertionError("read tools must not request approval")

        def mark_consumed(self, _approval_id):
            return None

        def close_run(self, _run_id):
            return None

    runtime = HostToolRuntime(
        registry=registry,
        dispatcher=dispatcher,
        approval_broker=ReadOnlyApprovals(),
    )
    tool_response = FakeResponse([
        json.dumps({
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "function": {
                        "name": "github.read_file",
                        "arguments": {"repository": "owner/repo", "path": "README.md"},
                    },
                }],
            },
            "done": True,
            "prompt_eval_count": 10,
            "eval_count": 2,
            "done_reason": "tool_calls",
        }).encode("utf-8")
    ])
    final_response = FakeResponse([
        encoded_chunk("The README says hello."),
        encoded_chunk(done=True, prompt_eval_count=12, eval_count=5, done_reason="stop"),
    ])
    captured_payloads = []

    def fake_post_chat(_settings, payload, **_kwargs):
        captured_payloads.append(payload)
        return tool_response if len(captured_payloads) == 1 else final_response

    control = ChatRunControl(
        "run_tools", "sess_basic", "turn_current", "model-a", "chat"
    )
    control.start_deadline(60)
    events = parse_sse(asyncio.run(collect_stream(
        request=SimpleNamespace(messages=[]),
        settings={"ollama_url": "http://127.0.0.1:11434", "model_provider": "ollama"},
        model="model-a",
        session_id="sess_basic",
        turn_id="turn_current",
        run_id="run_tools",
        prompt_sha256="digest",
        user_message_id=3,
        user_query="read the README",
        temporary_context="",
        images=[],
        run_control=control,
        project_id="project-one",
        post_chat=fake_post_chat,
        host_tool_runtime=runtime,
    )))

    names = [event for event, _payload in events]
    assert names == ["meta", "tool_start", "tool_end", "token", "metrics", "done"]
    assert captured_payloads[0]["tools"][0]["function"]["name"] == "github.read_file"
    assert any(message.get("role") == "tool" for message in captured_payloads[1]["messages"])
    assert fake_db.messages[-1]["content"] == "The README says hello."
    assert fake_db.runs[-1]["metrics"]["model_eval"]["tool_calls"] == 1


def test_basic_stream_runs_global_mcp_tool_in_independent_chat(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(chat_runtime, "database", fake_db)
    monkeypatch.setattr(
        chat_runtime,
        "model_supports_tools",
        lambda _settings, _model, *, project_id=None: True,
    )
    digest = hashlib.sha256(b"browser-mcp-test").hexdigest()
    definition = ToolDefinition(
        name="mcp.browser.browser_navigate",
        description="Navigate the isolated browser",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        access=ToolAccess.READ,
        handler=lambda call: {"url": call.arguments["url"], "title": "n8n"},
        extension_id="mcp.browser-playwright",
        manifest_sha256=digest,
    )
    registry = ToolRegistry()
    registry.register(definition, project_ids=("__independent_chat__",))
    dispatcher = ToolDispatcher(
        registry,
        scope_resolver=lambda _definition, _call: ToolScopeState(
            installed=True,
            trusted=True,
            enabled=True,
            healthy=True,
            resource_allowed=True,
            manifest_sha256=digest,
        ),
    )

    class ReadOnlyApprovals:
        def event_queue(self, _run_id):
            return asyncio.Queue()

        async def approval_callback(self, _request):
            raise AssertionError("read tools must not request approval")

        def mark_consumed(self, _approval_id):
            return None

        def close_run(self, _run_id):
            return None

    runtime = HostToolRuntime(
        registry=registry,
        dispatcher=dispatcher,
        approval_broker=ReadOnlyApprovals(),
        independent_scope_id="__independent_chat__",
    )
    tool_response = FakeResponse([
        json.dumps({
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "browser-call-1",
                    "function": {
                        "name": "mcp.browser.browser_navigate",
                        "arguments": {"url": "https://www.google.com/search?q=n8n"},
                    },
                }],
            },
            "done": True,
            "done_reason": "tool_calls",
        }).encode("utf-8")
    ])
    final_response = FakeResponse([
        encoded_chunk("已開啟瀏覽器並搜尋 n8n。"),
        encoded_chunk(done=True, prompt_eval_count=12, eval_count=5, done_reason="stop"),
    ])
    payloads = []

    def fake_post_chat(_settings, payload, **_kwargs):
        payloads.append(payload)
        return tool_response if len(payloads) == 1 else final_response

    control = ChatRunControl(
        "run_independent_tools", "sess_basic", "turn_current", "model-a", "chat"
    )
    control.start_deadline(60)
    events = parse_sse(asyncio.run(collect_stream(
        request=SimpleNamespace(messages=[]),
        settings={"ollama_url": "http://127.0.0.1:11434", "model_provider": "ollama"},
        model="model-a",
        session_id="sess_basic",
        turn_id="turn_current",
        run_id="run_independent_tools",
        prompt_sha256="digest",
        user_message_id=3,
        user_query="請開啟瀏覽器搜尋 n8n",
        temporary_context="",
        images=[],
        run_control=control,
        project_id=None,
        post_chat=fake_post_chat,
        host_tool_runtime=runtime,
    )))

    assert [name for name, _payload in events] == [
        "meta", "tool_start", "tool_end", "token", "metrics", "done"
    ]
    assert payloads[0]["tools"][0]["function"]["name"] == (
        "mcp.browser.browser_navigate"
    )
    assert any(message.get("role") == "tool" for message in payloads[1]["messages"])
    assert fake_db.messages[-1]["content"] == "已開啟瀏覽器並搜尋 n8n。"


def test_basic_stream_never_retries_an_indeterminate_external_write(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(chat_runtime, "database", fake_db)
    digest = hashlib.sha256(b"github-write-test").hexdigest()
    executions = []

    def uncertain_write(call):
        executions.append(dict(call.arguments))
        raise TimeoutError("connection ended after dispatch")

    definition = ToolDefinition(
        name="github.create_issue",
        description="Create one issue after approval",
        input_schema={
            "type": "object",
            "properties": {
                "repository": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["repository", "title"],
            "additionalProperties": False,
        },
        access=ToolAccess.WRITE,
        handler=uncertain_write,
        extension_id="connector.github",
        manifest_sha256=digest,
        risk_level="external_write",
    )
    registry = ToolRegistry([definition])
    dispatcher = ToolDispatcher(
        registry,
        scope_resolver=lambda _definition, _call: ToolScopeState(
            installed=True,
            trusted=True,
            enabled=True,
            healthy=True,
            resource_allowed=True,
            manifest_sha256=digest,
        ),
    )

    class AutoApprove:
        def event_queue(self, _run_id):
            return asyncio.Queue()

        async def approval_callback(self, _request):
            return True

        def mark_consumed(self, _approval_id):
            return None

        def close_run(self, _run_id):
            return None

    runtime = HostToolRuntime(
        registry=registry,
        dispatcher=dispatcher,
        approval_broker=AutoApprove(),
    )
    first_response = FakeResponse([
        json.dumps({
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "write-1",
                        "function": {
                            "name": "github.create_issue",
                            "arguments": {
                                "repository": "owner/repo",
                                "title": "First",
                            },
                        },
                    },
                    {
                        "id": "write-2",
                        "function": {
                            "name": "github.create_issue",
                            "arguments": {
                                "repository": "owner/repo",
                                "title": "Must not execute",
                            },
                        },
                    },
                ],
            },
            "done": True,
            "done_reason": "tool_calls",
        }).encode("utf-8")
    ])
    final_response = FakeResponse([
        encoded_chunk("Please verify GitHub before trying again."),
        encoded_chunk(done=True, done_reason="stop"),
    ])
    captured_payloads = []

    def fake_post_chat(_settings, payload, **_kwargs):
        captured_payloads.append(payload)
        return first_response if len(captured_payloads) == 1 else final_response

    control = ChatRunControl(
        "run_unknown_write", "sess_basic", "turn_current", "model-a", "chat"
    )
    control.start_deadline(60)
    events = parse_sse(asyncio.run(collect_stream(
        request=SimpleNamespace(messages=[]),
        settings={"ollama_url": "http://127.0.0.1:11434", "model_provider": "ollama"},
        model="model-a",
        session_id="sess_basic",
        turn_id="turn_current",
        run_id="run_unknown_write",
        prompt_sha256="digest",
        user_message_id=3,
        user_query="create the issue",
        temporary_context="",
        images=[],
        run_control=control,
        project_id="project-one",
        post_chat=fake_post_chat,
        host_tool_runtime=runtime,
    )))

    assert executions == [{"repository": "owner/repo", "title": "First"}]
    assert len(captured_payloads) == 2
    forced = captured_payloads[1]
    assert forced["tool_choice"] == "none"
    assert "tools" not in forced
    assert "Do not call or retry any tool" in forced["messages"][-1]["content"]
    tool_results = [
        json.loads(message["content"])
        for message in forced["messages"]
        if message.get("role") == "tool"
    ]
    assert [item["code"] for item in tool_results] == [
        "EXECUTION_UNKNOWN",
        "TOOL_SKIPPED_AFTER_EXECUTION_UNKNOWN",
    ]
    assert any(payload.get("result") == "TOOL_SKIPPED_AFTER_EXECUTION_UNKNOWN" for name, payload in events if name == "tool_end")
    assert fake_db.messages[-1]["content"] == "Please verify GitHub before trying again."
    assert fake_db.runs[-1]["metrics"]["model_eval"]["execution_unknown"] is True


def test_basic_terminal_event_failure_does_not_reverse_durable_completion(
    monkeypatch,
):
    fake_db = FakeDatabase()
    original_append = fake_db.append_run_event

    def fail_terminal_evidence(run_id, event, payload):
        if event in {"metrics", "done"}:
            raise OSError("injected terminal evidence failure")
        return original_append(run_id, event, payload)

    fake_db.append_run_event = fail_terminal_evidence
    monkeypatch.setattr(chat_runtime, "database", fake_db)
    response = FakeResponse([
        encoded_chunk("one durable answer"),
        encoded_chunk(done=True, eval_count=3, eval_duration=1_000_000_000),
    ])
    control = ChatRunControl(
        "run_terminal_evidence",
        "sess_basic",
        "turn_current",
        "model-a",
        "chat",
    )

    events = parse_sse(asyncio.run(collect_stream(
        request=SimpleNamespace(messages=[]),
        settings={"ollama_url": "http://127.0.0.1:11434", "model_provider": "ollama"},
        model="model-a",
        session_id="sess_basic",
        turn_id="turn_current",
        run_id="run_terminal_evidence",
        prompt_sha256="digest",
        user_message_id=3,
        user_query="current question",
        temporary_context="",
        images=[],
        run_control=control,
        post_chat=lambda *_args, **_kwargs: response,
    )))

    assert [event for event, _ in events] == ["meta", "token", "metrics", "done"]
    assert [run["status"] for run in fake_db.runs] == ["running", "completed"]
    current_assistants = [
        message
        for message in fake_db.messages
        if message["role"] == "assistant"
        and message["turn_id"] == "turn_current"
    ]
    assert [message["content"] for message in current_assistants] == [
        "one durable answer"
    ]


def test_provider_failure_does_not_persist_assistant(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(chat_runtime, "database", fake_db)
    response = FakeResponse(status_code=503, text="offline")
    control = ChatRunControl(
        "run_basic5678",
        "sess_basic",
        "turn_current",
        "model-a",
        "chat",
    )

    items = asyncio.run(collect_stream(
        request=SimpleNamespace(messages=[]),
        settings={"ollama_url": "http://127.0.0.1:11434", "model_provider": "ollama"},
        model="model-a",
        session_id="sess_basic",
        turn_id="turn_current",
        run_id="run_basic5678",
        prompt_sha256="digest",
        user_message_id=3,
        user_query="current question",
        temporary_context="",
        images=[],
        run_control=control,
        post_chat=lambda *_args, **_kwargs: response,
    ))

    events = parse_sse(items)
    assert [event for event, _ in events] == ["meta", "error"]
    assert fake_db.messages[-1]["role"] == "user"
    assert fake_db.runs[-1]["status"] == "failed"


def test_completion_fails_closed_if_session_project_changes(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(chat_runtime, "database", fake_db)
    monkeypatch.setattr(
        fake_db,
        "get_session",
        lambda session_id: {"id": session_id, "project_id": "project-two"},
    )
    response = FakeResponse([
        encoded_chunk("answer from old project"),
        encoded_chunk(done=True, eval_count=2),
    ])
    control = ChatRunControl(
        "run_scope_change", "sess_basic", "turn_current", "model-a", "chat"
    )

    events = parse_sse(asyncio.run(collect_stream(
        request=SimpleNamespace(messages=[]),
        settings={"ollama_url": "http://127.0.0.1:11434", "model_provider": "ollama"},
        model="model-a", session_id="sess_basic", turn_id="turn_current",
        run_id="run_scope_change", prompt_sha256="digest", user_message_id=3,
        user_query="current question", temporary_context="", images=[],
        run_control=control, project_id="project-one",
        archive_sync=lambda _session_id: (_ for _ in ()).throw(
            AssertionError("changed scope must not be archived")
        ),
        post_chat=lambda *_args, **_kwargs: response,
    )))

    assert [event for event, _ in events] == ["meta", "token", "error"]
    assert events[-1][1]["code"] == "SESSION_PROJECT_CHANGED"
    assert [item for item in fake_db.messages if item["role"] == "assistant"] == [
        fake_db.messages[1]
    ]
    assert fake_db.runs[-1]["status"] == "failed"
    assert response.closed


def test_provider_failure_classification_preserves_project_scope(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(chat_runtime, "database", fake_db)
    seen = {}

    def classify(_settings, _model, _status, _text, *, project_id=None):
        seen["project_id"] = project_id
        return {
            "code": "PROVIDER_ERROR",
            "message": "Provider failed.",
            "recoverable": True,
        }

    monkeypatch.setattr(chat_runtime, "model_call_error", classify)
    control = ChatRunControl(
        "run_scoped_failure", "sess_basic", "turn_current", "model-a", "chat"
    )
    response = FakeResponse(status_code=503, text="unavailable")

    asyncio.run(collect_stream(
        request=SimpleNamespace(messages=[]),
        settings={"ollama_url": "http://127.0.0.1:11434", "model_provider": "ollama"},
        model="model-a", session_id="sess_basic", turn_id="turn_current",
        run_id="run_scoped_failure", prompt_sha256="digest", user_message_id=3,
        user_query="hello", temporary_context="", images=[],
        run_control=control, project_id="project-one",
        post_chat=lambda *_args, **_kwargs: response,
    ))

    assert seen == {"project_id": "project-one"}


def test_transport_failure_classification_preserves_project_scope(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(chat_runtime, "database", fake_db)
    seen = {}

    def classify(_settings, _model, _error, *, project_id=None):
        seen["project_id"] = project_id
        return {
            "code": "PROVIDER_UNREACHABLE",
            "message": "Provider unavailable.",
            "recoverable": True,
        }

    monkeypatch.setattr(chat_runtime, "model_transport_error", classify)
    control = ChatRunControl(
        "run_scoped_transport_failure",
        "sess_basic",
        "turn_current",
        "model-a",
        "chat",
    )

    def failed_post_chat(*_args, **_kwargs):
        raise RuntimeError("transport failed")

    asyncio.run(collect_stream(
        request=SimpleNamespace(messages=[]),
        settings={
            "ollama_url": "http://127.0.0.1:11434",
            "model_provider": "ollama",
        },
        model="model-a",
        session_id="sess_basic",
        turn_id="turn_current",
        run_id="run_scoped_transport_failure",
        prompt_sha256="digest",
        user_message_id=3,
        user_query="hello",
        temporary_context="",
        images=[],
        run_control=control,
        project_id="project-one",
        post_chat=failed_post_chat,
    ))

    assert seen == {"project_id": "project-one"}


def test_basic_completion_persists_generated_artifact_refs(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(chat_runtime, "database", fake_db)
    response = FakeResponse([
        encoded_chunk("Here is the file:\n```html\n<h1>Hello</h1>\n```"),
        encoded_chunk(done=True, eval_count=8, eval_duration=1_000_000_000),
    ])
    control = ChatRunControl(
        "run_generated_basic", "sess_basic", "turn_current", "model-a", "chat"
    )

    events = parse_sse(asyncio.run(collect_stream(
        request=SimpleNamespace(messages=[]),
        settings={"ollama_url": "http://127.0.0.1:11434", "model_provider": "ollama"},
        model="model-a", session_id="sess_basic", turn_id="turn_current",
        run_id="run_generated_basic", prompt_sha256="digest", user_message_id=3,
        user_query="create html", temporary_context="", images=[],
        run_control=control, post_chat=lambda *_args, **_kwargs: response,
    )))

    assert [event for event, _ in events] == ["meta", "token", "metrics", "done"]
    assistant_refs = fake_db.messages[-1]["artifacts"]
    run_refs = fake_db.runs[-1]["artifacts"]
    assert assistant_refs == run_refs
    assert len(assistant_refs) == 1
    artifact_id = assistant_refs[0]["artifact_id"]
    assert fake_db.artifacts[artifact_id]["files"][0]["path"] == "generated-01.html"
    assert any(
        event == "artifact" and payload["artifact_id"] == artifact_id
        for _run_id, event, payload in fake_db.public_events
    )


def test_hidden_reasoning_is_filtered_across_stream_chunks(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(chat_runtime, "database", fake_db)
    response = FakeResponse([
        encoded_chunk("<thi"),
        encoded_chunk("nk>private chain of thought"),
        encoded_chunk("</th"),
        encoded_chunk("ink>Public answer"),
        encoded_chunk(done=True, eval_count=2, eval_duration=1_000_000_000),
    ])
    control = ChatRunControl(
        "run_hidden1234", "sess_basic", "turn_current", "model-a", "chat"
    )

    items = asyncio.run(collect_stream(
        request=SimpleNamespace(messages=[]),
        settings={"ollama_url": "http://127.0.0.1:11434", "model_provider": "ollama"},
        model="model-a", session_id="sess_basic", turn_id="turn_current",
        run_id="run_hidden1234", prompt_sha256="digest", user_message_id=3,
        user_query="current question", temporary_context="", images=[],
        run_control=control, post_chat=lambda *_args, **_kwargs: response,
    ))

    serialized = "".join(items)
    events = parse_sse(items)
    assert "private chain of thought" not in serialized
    assert [event for event, _ in events] == ["meta", "token", "metrics", "done"]
    assert events[1][1]["content"] == "Public answer"
    assert fake_db.messages[-1]["content"] == "Public answer"


def test_chat_route_is_wired_only_to_basic_stream():
    source = (BACKEND / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    chat_function = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "chat"
    )
    called_names = {
        node.func.id
        for node in ast.walk(chat_function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "stream_basic_chat" in called_names
    assert "coordinated_chat_stream" not in called_names
    assert "RunResourceMonitor" not in called_names
    assert "resolve_safir_mode" not in called_names


def test_basic_mode_uses_disabled_rag_without_loading_runtime(monkeypatch):
    imported = []
    real_import = builtins.__import__

    def tracking_import(name, *args, **kwargs):
        imported.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", tracking_import)
    service = build_rag_service(
        basic_only=True,
        persist_directory="unused",
        chunk_size=600,
        chunk_overlap=120,
    )
    assert isinstance(service, DisabledRAGEngine)
    assert service.query("ignored") == []
    assert service.vector_store.get(include=["documents"])["documents"] == []
    assert "rag_engine" not in imported


def test_frontend_forces_basic_chat_and_hides_collaboration_panel():
    source = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    basic_mode = (ROOT / "frontend" / "basic-chat-mode.js").read_text(encoding="utf-8")
    index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "const BASIC_CHAT_MODE = true;" in basic_mode
    assert "use_rag: BASIC_CHAT_MODE ? false : ragToggle.checked" in source
    assert "!BASIC_CHAT_MODE && question.startsWith('/skill')" in source
    assert "skill_ids: explicitSkillIds" in source  # accepted for compatibility; backend ignores it
    assert "railAgents.hidden = true;" in basic_mode
    assert "'rail-knowledge', 'rail-runs', 'rail-artifacts'" in basic_mode
    assert "'rail-extensions'" not in basic_mode
    assert "'[data-target=\"tab-settings-agent\"]'" in basic_mode
    assert "'[data-itab=\"safir\"]'" in basic_mode
    assert "basicPaletteActions" in source
    assert "configureBasicWizard" in source
    assert "useBasicKnowledgeStatus" in source
    assert "configureBasicWelcomeDashboard" in source
    assert "基本聊天模式：未使用 RAG、工具或多 Agent。" not in source
    assert "tts-btn-trigger" not in source
    assert "speechSynthesis" not in source
    assert 'id="setting-tts-auto"' not in index
    assert index.index("basic-chat-mode.js") < index.index("app.js")
    assert '<body class="basic-chat-mode">' in index


def test_frontend_has_public_thinking_state_and_readable_answer_layout():
    source = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")
    assert "Agent 正在思考" in source
    assert 'class="assistant-thinking" role="status" aria-live="polite"' in source
    assert "setAssistantResponsePhase(assistantMsgEl, 'answering')" in source
    assert "setAssistantResponsePhase(assistantMsgEl, 'clear')" in source
    assert "assistant-answer-content" in source
    assert "body.basic-chat-mode .message.assistant .message-bubble" in styles
    assert ".assistant-thinking-dots i" in styles
    assert "@media (max-width: 640px)" in styles
    assert "body.basic-chat-mode main.chat-container" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles


def test_frontend_keeps_copy_and_regenerate_for_live_and_restored_answers():
    source = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert "bar.appendChild(mkBtn('複製', 'copy'" in source
    assert "bar.appendChild(mkBtn('重新生成', 'refresh-cw'" in source
    assert "const renderedMessages = data.messages.map(msg => appendHistoricalMessage(msg));" in source
    assert "appendAnswerFooter(lastAssistantBubble" in source
    assert "showMetrics: false" in source
