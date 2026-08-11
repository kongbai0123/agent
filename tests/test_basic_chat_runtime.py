from __future__ import annotations

import asyncio
import ast
import builtins
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

    def get_messages_by_session(self, _session_id):
        return [dict(item) for item in self.messages]

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
            "events": kwargs.get("events") or [],
        })


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
    assert fake_db.runs[-1]["status"] == "completed"
    assert fake_db.runs[-1]["events"] == []


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
    assert response.closed


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
    assert "'rail-knowledge', 'rail-runs', 'rail-artifacts', 'rail-extensions'" in basic_mode
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
