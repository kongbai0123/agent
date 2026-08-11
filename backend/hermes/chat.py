"""Text-only Hermes chat adapter with an explicit fallback boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence

from .client import HermesSidecarClient
from .config import validate_header_value
from .errors import HermesProtocolError


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ALLOWED_ROLES = {"system", "developer", "user", "assistant"}


def normalize_text_messages(messages: Sequence[Mapping[str, Any]]) -> list[Dict[str, str]]:
    if not messages or len(messages) > 256:
        raise ValueError("Hermes chat requires between 1 and 256 messages.")
    result: list[Dict[str, str]] = []
    total_chars = 0
    for item in messages:
        if not isinstance(item, Mapping):
            raise ValueError("Hermes chat messages must be objects.")
        role = str(item.get("role") or "").strip().casefold()
        content = item.get("content")
        if role not in _ALLOWED_ROLES or not isinstance(content, str):
            raise ValueError("Hermes chat accepts text-only conversational messages.")
        if _CONTROL.search(content):
            raise ValueError("Hermes chat message contains invalid control characters.")
        total_chars += len(content)
        if total_chars > 1_048_576:
            raise ValueError("Hermes chat context exceeded the size limit.")
        # Deliberately copy only role/content. Tool calls, attachments, names,
        # and provider-specific control fields never cross this phase boundary.
        result.append({"role": role, "content": content})
    return result


@dataclass(frozen=True)
class HermesChatResult:
    content: str
    response_id: str = ""
    model: str = ""
    finish_reason: str = ""
    usage: Optional[Mapping[str, Any]] = None


class HermesTextChatAdapter:
    """Optional backend. Typed Hermes errors let the caller choose fallback."""

    def __init__(self, client: HermesSidecarClient) -> None:
        self.client = client

    @property
    def enabled(self) -> bool:
        return self.client.config.enabled

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str = "",
        session_id: str = "",
        session_key: str = "",
    ) -> HermesChatResult:
        normalized = normalize_text_messages(messages)
        selected_model = str(model or self.client.config.default_model).strip()
        if not selected_model or len(selected_model) > 256:
            raise ValueError("Hermes model identifier is invalid.")
        headers: Dict[str, str] = {}
        if session_id:
            headers["X-Hermes-Session-Id"] = validate_header_value(
                session_id, label="Hermes session ID"
            )
        if session_key:
            headers["X-Hermes-Session-Key"] = validate_header_value(
                session_key, label="Hermes session key"
            )
        response = self.client.request_json(
            "POST",
            "/v1/chat/completions",
            payload={
                "model": selected_model,
                "messages": normalized,
                "stream": False,
            },
            headers=headers,
        )
        try:
            choice = response["choices"][0]
            message = choice["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise HermesProtocolError("Hermes chat response is missing text content.") from exc
        if not isinstance(content, str):
            raise HermesProtocolError("Hermes chat returned non-text content.")
        usage = response.get("usage")
        return HermesChatResult(
            content=content,
            response_id=str(response.get("id") or ""),
            model=str(response.get("model") or selected_model),
            finish_reason=str(choice.get("finish_reason") or ""),
            usage=usage if isinstance(usage, Mapping) else None,
        )
