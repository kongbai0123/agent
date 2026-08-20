"""Model capability profiles and provider-neutral OpenAI request shaping.

OpenAI-compatible is a transport contract, not a statement about what a model
does.  A translation, embedding, or reranking model must not silently become a
general chat/Agent model merely because it is reachable through
``/chat/completions``.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


MODEL_KINDS = frozenset({
    "chat",
    "translation",
    "embedding",
    "rerank",
    "vision",
    "unknown",
})
AGENT_ROLES = ("primary", "planner", "explorer", "implementer", "critic")
_LANGUAGE_PAIR = re.compile(
    r"^[a-z]{2,3}(?:-[a-z]{2,4})?-[a-z]{2,3}(?:-[a-z]{2,4})?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ModelCapabilityProfile:
    kind: str
    adapter: str
    supports_chat: bool
    supports_stream: bool
    supports_tools: bool
    eligible_for_primary: bool
    eligible_for_subagent: bool
    eligible_roles: tuple[str, ...]
    language_pair: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "adapter": self.adapter,
            "supports_chat": self.supports_chat,
            "supports_stream": self.supports_stream,
            "supports_tools": self.supports_tools,
            "eligible_for_primary": self.eligible_for_primary,
            "eligible_for_subagent": self.eligible_for_subagent,
            "eligible_roles": list(self.eligible_roles),
            "language_pair": self.language_pair,
        }


def normalize_language_pair(value: Any) -> str:
    pair = str(value or "").strip().casefold()
    if pair and not _LANGUAGE_PAIR.fullmatch(pair):
        raise ValueError(
            "language_pair must be a source-target code such as en-zh-tw."
        )
    return pair


def infer_model_kind(model_id: str) -> str:
    """Conservatively infer a task kind from a model identifier.

    Recognizable model identities take precedence over metadata inherited from
    a previously selected model. Explicit metadata remains authoritative for
    opaque identifiers. Known specialized endpoints such as Riva Translate
    fail closed immediately, while unrecognized identifiers remain ``unknown``
    instead of being promoted to chat models.
    """

    name = str(model_id or "").strip().casefold()
    if not name:
        return "unknown"
    if any(token in name for token in ("rerank", "re-rank", "ranker")):
        return "rerank"
    if any(token in name for token in (
        "riva-translate",
        "/translate",
        "-translate",
        "translation",
    )):
        return "translation"
    if any(token in name for token in (
        "/embed",
        "-embed",
        "embedding",
        "text-embedding",
        "/bge-",
        "bge-",
        "e5-",
    )):
        return "embedding"
    if any(token in name for token in (
        "llama-guard",
        "nemoguard",
        "safety-guard",
        "moderation",
        "classifier",
    )):
        return "unknown"
    if any(token in name for token in (
        "vision",
        "/vl",
        "-vl-",
        "llava",
        "vila",
        "image-to-text",
        "ocr",
    )):
        return "vision"
    if any(token in name for token in (
        "chat",
        "instruct",
        "assistant",
        "llama",
        "nemotron",
        "qwen",
        "mistral",
        "mixtral",
        "gemma",
        "deepseek",
        "gpt-",
        "claude",
        "command-r",
    )):
        return "chat"
    return "unknown"


def model_capability_profile(
    model_id: str,
    *,
    model_kind: str = "",
    supports_tools: bool = False,
    language_pair: str = "",
    local_chat_default: bool = False,
    legacy_chat_default: bool = False,
) -> ModelCapabilityProfile:
    explicit = str(model_kind or "").strip().casefold()
    if explicit and explicit not in MODEL_KINDS:
        raise ValueError(
            "model_kind must be chat, translation, embedding, rerank, vision, or unknown."
        )
    inferred = infer_model_kind(model_id)
    stale_non_chat_kind = (
        inferred == "chat"
        and explicit in {"translation", "embedding", "rerank", "vision", "unknown"}
    )
    if (
        explicit
        and inferred in {"translation", "embedding", "rerank", "vision"}
        and explicit != inferred
    ):
        raise ValueError(
            f"model_kind {explicit!r} conflicts with known specialized "
            f"model kind {inferred!r}."
        )
    # A provider card can retain its former specialized selection while its
    # selected_model is replaced.  A recognizable chat model must not inherit
    # that stale purpose.  Explicit metadata remains authoritative for opaque
    # identifiers, while recognizable specialized identifiers still fail
    # closed above.
    kind = inferred if stale_non_chat_kind else (explicit or inferred)
    if kind == "unknown" and (local_chat_default or legacy_chat_default):
        kind = "chat"
    pair = normalize_language_pair(language_pair)
    if kind != "translation" and pair:
        if stale_non_chat_kind:
            pair = ""
        else:
            raise ValueError("language_pair is only valid for translation models.")
    if kind == "chat":
        return ModelCapabilityProfile(
            kind="chat",
            adapter="openai_chat",
            supports_chat=True,
            supports_stream=True,
            supports_tools=bool(supports_tools),
            eligible_for_primary=True,
            eligible_for_subagent=True,
            eligible_roles=AGENT_ROLES,
        )
    if kind == "translation":
        return ModelCapabilityProfile(
            kind="translation",
            adapter="language_pair_system",
            supports_chat=False,
            supports_stream=True,
            supports_tools=False,
            eligible_for_primary=False,
            eligible_for_subagent=False,
            eligible_roles=(),
            language_pair=pair,
        )
    adapter = {
        "embedding": "embedding_input",
        "rerank": "rerank_input",
        "vision": "vision_input",
        "unknown": "unsupported",
    }[kind]
    return ModelCapabilityProfile(
        kind=kind,
        adapter=adapter,
        supports_chat=False,
        supports_stream=False,
        supports_tools=False,
        eligible_for_primary=False,
        eligible_for_subagent=False,
        eligible_roles=(),
    )


def capability_fingerprint(
    model_id: str,
    endpoint: str,
    profile: ModelCapabilityProfile,
) -> str:
    payload = {
        "model_id": str(model_id or "").strip(),
        "endpoint": str(endpoint or "").strip().rstrip("/"),
        "profile": profile.as_dict(),
        "contract_version": 1,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_tool_attestation(
    value: Any,
    *,
    expected_fingerprint: str,
    tools_enabled: bool,
) -> dict[str, Any] | None:
    """Keep an attestation only while it matches the current capability contract."""

    if not tools_enabled or not isinstance(value, Mapping):
        return None
    fingerprint = str(value.get("profile_fingerprint") or "").strip()
    if (
        not fingerprint
        or fingerprint != expected_fingerprint
        or value.get("passed") is not True
    ):
        return None
    return {
        "profile_fingerprint": fingerprint,
        "verified_at": str(value.get("verified_at") or "")[:80],
        "method": str(value.get("method") or "synthetic_tool_call")[:80],
        "passed": True,
    }


def _last_user_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for item in reversed(messages):
        if not isinstance(item, Mapping) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content.strip()
        return ""
    return ""


def _language_pair_from_messages(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for item in messages:
        if not isinstance(item, Mapping) or item.get("role") != "system":
            continue
        try:
            return normalize_language_pair(item.get("content"))
        except ValueError:
            return ""
    return ""


def build_openai_chat_payload(
    payload: Mapping[str, Any],
    *,
    stream: bool,
    profile: ModelCapabilityProfile,
) -> dict[str, Any]:
    """Build the exact OpenAI-compatible body used by preflight and runtime."""

    if profile.kind not in {"chat", "translation"}:
        raise ValueError(
            f"Model kind {profile.kind!r} is not eligible for chat/completions."
        )
    result = {
        key: value
        for key, value in dict(payload).items()
        if key not in {
            "keep_alive",
            "options",
            "language_pair",
            "model_kind",
            "supports_tools",
        }
    }
    options = payload.get("options")
    if isinstance(options, Mapping):
        if options.get("num_predict") is not None:
            result["max_tokens"] = int(options["num_predict"])
        if options.get("temperature") is not None:
            result["temperature"] = float(options["temperature"])

    if profile.kind == "translation":
        pair = (
            profile.language_pair
            or normalize_language_pair(payload.get("language_pair"))
            or _language_pair_from_messages(payload.get("messages"))
        )
        if not pair:
            raise ValueError(
                "Translation models require language_pair, for example en-zh-tw."
            )
        text = _last_user_text(payload.get("messages"))
        if not text:
            raise ValueError("Translation models require one user text input.")
        allowed = {
            key: value
            for key, value in result.items()
            if key in {
                "model",
                "max_tokens",
                "temperature",
                "top_p",
                "seed",
                "stop",
            }
        }
        result = {
            **allowed,
            "messages": [
                {"role": "system", "content": pair},
                {"role": "user", "content": text},
            ],
        }
    elif not profile.supports_tools:
        result.pop("tools", None)
        result.pop("tool_choice", None)

    result["stream"] = bool(stream)
    if stream and profile.supports_stream:
        result["stream_options"] = {"include_usage": True}
    else:
        result.pop("stream_options", None)
    return result


_SECRET_PATTERNS = (
    re.compile(r"\bnvapi-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+\-/=]{8,}\b", re.IGNORECASE),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    ),
)
_IDENTIFIER_PATTERN = re.compile(
    r"\b(account|organization|org|tenant)(?:[\s_-]*id)?"
    r"(\s*(?:[:=]|\bis\b)?\s*)"
    r"([A-Za-z0-9][A-Za-z0-9._:/-]{2,})",
    re.IGNORECASE,
)
_HTML_BLOCKS = re.compile(
    r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG = re.compile(r"<[^>]+>")
_ALLOWED_ERROR_KEYS = frozenset({"message", "detail", "code", "type", "param"})


def _error_strings(value: Any, *, depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    if isinstance(value, Mapping):
        values: list[str] = []
        for key, item in list(value.items())[:40]:
            normalized = str(key).casefold()
            if normalized == "error":
                values.extend(_error_strings(item, depth=depth + 1))
            elif normalized in _ALLOWED_ERROR_KEYS:
                if isinstance(item, str):
                    values.append(item)
                elif isinstance(item, (int, float)):
                    values.append(str(item))
                else:
                    values.extend(_error_strings(item, depth=depth + 1))
        return values
    if isinstance(value, list):
        result: list[str] = []
        for item in value[:20]:
            result.extend(_error_strings(item, depth=depth + 1))
        return result
    if isinstance(value, str):
        return [value]
    return []


def safe_upstream_error_reason(
    response_text: str,
    *,
    secrets: Iterable[str] = (),
) -> str:
    """Extract a useful provider reason without exposing provider/account data."""

    raw = str(response_text or "")[:20_000]
    if not raw.strip():
        return ""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        candidates = [raw]
    else:
        candidates = _error_strings(parsed)
    text = " · ".join(
        candidate.strip()
        for candidate in candidates
        if isinstance(candidate, str) and candidate.strip()
    )
    if not text:
        return ""
    text = html.unescape(text)
    text = _HTML_BLOCKS.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    for secret in secrets:
        literal = str(secret or "").strip()
        if len(literal) >= 8:
            text = text.replace(literal, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    text = _IDENTIFIER_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[redacted]",
        text,
    )
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]
