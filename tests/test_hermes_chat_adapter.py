from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from hermes import (  # noqa: E402
    HermesProtocolError,
    HermesTextChatAdapter,
    normalize_text_messages,
)


class StubClient:
    def __init__(self, response):
        self.config = SimpleNamespace(enabled=True, default_model="configured-model")
        self.response = response
        self.calls = []

    def request_json(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.response


def test_text_chat_strips_every_non_text_provider_field():
    client = StubClient(
        {
            "id": "chat-1",
            "model": "configured-model",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "answer"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 12},
        }
    )
    adapter = HermesTextChatAdapter(client)
    result = adapter.complete(
        [
            {
                "role": "user",
                "content": "question",
                "tool_calls": [{"dangerous": True}],
                "attachments": ["secret.bin"],
            }
        ],
        session_id="opaque-session",
        session_key="opaque-memory",
    )

    assert result.content == "answer"
    _method, _path, kwargs = client.calls[0]
    assert kwargs["payload"] == {
        "model": "configured-model",
        "messages": [{"role": "user", "content": "question"}],
        "stream": False,
    }
    assert kwargs["headers"] == {
        "X-Hermes-Session-Id": "opaque-session",
        "X-Hermes-Session-Key": "opaque-memory",
    }


@pytest.mark.parametrize(
    "message",
    [
        {"role": "tool", "content": "not allowed"},
        {"role": "user", "content": [{"type": "text", "text": "array"}]},
        {"role": "user", "content": "bad\x00text"},
    ],
)
def test_text_chat_rejects_non_conversational_or_non_text_messages(message):
    with pytest.raises(ValueError):
        normalize_text_messages([message])


def test_text_chat_rejects_missing_or_non_text_response_content():
    for response in ({"choices": []}, {"choices": [{"message": {"content": []}}]}):
        with pytest.raises(HermesProtocolError):
            HermesTextChatAdapter(StubClient(response)).complete(
                [{"role": "user", "content": "hello"}]
            )


def test_session_header_control_characters_are_rejected_before_transport():
    client = StubClient({})
    with pytest.raises(Exception):
        HermesTextChatAdapter(client).complete(
            [{"role": "user", "content": "hello"}], session_id="bad\r\nid"
        )
    assert client.calls == []
