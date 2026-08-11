"""Catalog and bounded connectivity checks for imported model APIs."""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

import requests
from model_capabilities import (
    build_openai_chat_payload,
    capability_fingerprint,
    model_capability_profile,
    normalize_language_pair,
    normalize_tool_attestation,
    safe_upstream_error_reason,
)


PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    "gemini": {
        "id": "gemini",
        "label": "Google Gemini API",
        "description": "Google 生成式 AI 模型",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "official_url": "https://aistudio.google.com/apikey",
        "endpoint_editable": False,
        "source_hosts": ["aistudio.google.com", "ai.google.dev"],
        "credential_kind": "api_key",
    },
    "nvidia": {
        "id": "nvidia",
        "label": "NVIDIA API Catalog",
        "description": "NVIDIA 免費端點、合作夥伴端點與 NIM 模型",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "official_url": "https://build.nvidia.com/models",
        "endpoint_editable": False,
        "source_hosts": ["build.nvidia.com"],
        "credential_kind": "api_key",
    },
    "openai": {
        "id": "openai",
        "label": "OpenAI API",
        "description": "OpenAI 模型 API",
        "base_url": "https://api.openai.com/v1",
        "official_url": "https://platform.openai.com/api-keys",
        "endpoint_editable": False,
        "source_hosts": ["platform.openai.com"],
        "credential_kind": "api_key",
    },
    "openai_compatible": {
        "id": "openai_compatible",
        "label": "OpenAI-compatible",
        "description": "OpenRouter、LM Studio、vLLM 或自訂相容端點",
        "base_url": "",
        "official_url": "",
        "endpoint_editable": True,
        "source_hosts": [],
        "credential_kind": "optional_api_key",
    },
}


@dataclass(frozen=True)
class ProviderConnectionFailure(RuntimeError):
    code: str
    message: str
    status_code: int
    recoverable: bool = True

    def __str__(self) -> str:
        return self.message


def catalog_payload() -> list[dict[str, Any]]:
    return [dict(item) for item in PROVIDER_CATALOG.values()]


def infer_provider_type(provider: Mapping[str, Any]) -> str:
    explicit = str(provider.get("provider_type") or "").strip().casefold()
    if explicit in PROVIDER_CATALOG:
        return explicit
    base_url = str(provider.get("base_url") or "").strip().casefold()
    if "generativelanguage.googleapis.com" in base_url:
        return "gemini"
    if "integrate.api.nvidia.com" in base_url:
        return "nvidia"
    if "api.openai.com" in base_url:
        return "openai"
    return "openai_compatible"


def normalize_provider_endpoint(provider_type: str, base_url: str) -> str:
    normalized_type = str(provider_type or "").strip().casefold()
    if normalized_type not in PROVIDER_CATALOG:
        raise ValueError("Unsupported model provider type.")
    catalog_item = PROVIDER_CATALOG[normalized_type]
    endpoint = str(base_url or catalog_item["base_url"] or "").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Provider URL must use http or https.")
    canonical = str(catalog_item["base_url"] or "").rstrip("/")
    if canonical and not catalog_item["endpoint_editable"] and endpoint != canonical:
        raise ValueError("Official provider endpoints cannot be changed.")
    return endpoint


def normalize_provider_source_url(provider_type: str, source_url: str) -> str:
    value = str(source_url or "").strip()
    if not value:
        return ""
    if len(value) > 1000:
        raise ValueError("Provider source URL is too long.")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Provider source URL must be a public HTTPS URL.")
    allowed_hosts = PROVIDER_CATALOG[provider_type]["source_hosts"]
    if allowed_hosts and parsed.hostname.casefold() not in allowed_hosts:
        raise ValueError("Provider source URL does not match the selected provider.")
    return value


def _clean_model_id(value: Any) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", "", str(value or "").strip())
    return cleaned[:200] if cleaned not in {"", ".", ".."} else ""


def _strict_boolean(
    item: Mapping[str, Any],
    field: str,
    *,
    default: bool,
) -> bool:
    value = item.get(field, default)
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean.")
    return value


def model_id_from_source_url(provider_type: str, source_url: str) -> str:
    """Extract one model identifier from a provider integration/model page."""
    normalized_type = str(provider_type or "").strip().casefold()
    source = normalize_provider_source_url(normalized_type, source_url)
    if not source:
        return ""
    parsed = urlsplit(source)
    query = parse_qs(parsed.query)
    for key in ("model", "model_id", "modelId"):
        values = query.get(key)
        if values and (candidate := _clean_model_id(values[0])):
            return candidate
    parts = [
        _clean_model_id(unquote(part))
        for part in parsed.path.split("/")
        if _clean_model_id(unquote(part))
    ]
    if (
        normalized_type == "nvidia"
        and parsed.hostname
        and parsed.hostname.casefold() == "build.nvidia.com"
        and len(parts) >= 2
        and parts[0].casefold() != "models"
    ):
        return f"{parts[0]}/{parts[1]}"
    lowered = [part.casefold() for part in parts]
    for marker in ("models", "model"):
        if marker not in lowered:
            continue
        remainder = parts[lowered.index(marker) + 1:]
        if remainder:
            return "/".join(remainder)
    return ""


def normalize_provider_settings(raw_providers: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_providers, list):
        raise ValueError("model_providers must be an array.")
    if len(raw_providers) > 8:
        raise ValueError("At most 8 model providers are allowed.")
    normalized: list[dict[str, Any]] = []
    provider_ids: set[str] = set()
    for item in raw_providers:
        if not isinstance(item, dict):
            raise ValueError("Each model provider must be an object.")
        provider_id = re.sub(
            r"[^a-z0-9_-]",
            "",
            str(item.get("id") or "").strip().casefold(),
        )[:48]
        if (
            not provider_id
            or not provider_id[0].isalpha()
            or provider_id == "ollama"
            or provider_id in provider_ids
        ):
            raise ValueError(
                "Model provider IDs must be unique, start with a letter, and cannot be ollama."
            )
        provider_ids.add(provider_id)
        provider_type = infer_provider_type(item)
        endpoint = normalize_provider_endpoint(
            provider_type,
            str(item.get("base_url") or ""),
        )
        record = {
            "id": provider_id,
            "label": re.sub(
                r"[\x00-\x1f\x7f]+",
                " ",
                str(item.get("label") or provider_id),
            ).strip()[:60] or provider_id,
            "base_url": endpoint,
            # Provider configuration and ExtensionStore are independent gates.
            # Keep both fields explicit so runtime execution can require both.
            "enabled": _strict_boolean(item, "enabled", default=False),
            "supports_tools": _strict_boolean(
                item,
                "supports_tools",
                default=False,
            ),
            "input_cost_per_million": max(
                0.0,
                min(1_000_000.0, float(item.get("input_cost_per_million") or 0.0)),
            ),
            "output_cost_per_million": max(
                0.0,
                min(1_000_000.0, float(item.get("output_cost_per_million") or 0.0)),
            ),
            "currency": str(item.get("currency") or "USD").strip().upper()[:8] or "USD",
        }
        if item.get("provider_type") or provider_type != "openai_compatible":
            record["provider_type"] = provider_type
        source_url = normalize_provider_source_url(
            provider_type,
            str(item.get("source_url") or ""),
        )
        if source_url:
            record["source_url"] = source_url
        selected_model = (
            model_id_from_source_url(provider_type, source_url)
            or _clean_model_id(item.get("selected_model"))
        )
        if source_url and not selected_model:
            raise ValueError(
                "The model URL does not identify a model; select one model explicitly."
            )
        if selected_model:
            record["selected_model"] = selected_model
            profile = model_capability_profile(
                selected_model,
                model_kind=str(item.get("model_kind") or ""),
                supports_tools=record["supports_tools"],
                language_pair=str(item.get("language_pair") or ""),
            )
            fingerprint = capability_fingerprint(
                selected_model,
                endpoint,
                profile,
            )
            record["model_kind"] = profile.kind
            record["supports_tools"] = profile.supports_tools
            if profile.language_pair:
                record["language_pair"] = profile.language_pair
            record["capability_profile"] = {
                **profile.as_dict(),
                "fingerprint": fingerprint,
            }
            attestation = normalize_tool_attestation(
                item.get("tool_attestation"),
                expected_fingerprint=fingerprint,
                tools_enabled=profile.supports_tools,
            )
            if attestation is not None:
                record["tool_attestation"] = attestation
        normalized.append(record)
    return normalized


def _models_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/models"


def _chat_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/chat/completions"


def _failure_for_status(
    status_code: int,
    response_text: str = "",
    *,
    secret: str = "",
) -> ProviderConnectionFailure:
    reason = "" if status_code in {401, 403} else safe_upstream_error_reason(
        response_text, secrets=(secret,)
    )
    if status_code in {401, 403}:
        return ProviderConnectionFailure(
            "PROVIDER_AUTH_FAILED",
            "API 金鑰無效，或帳戶沒有使用此端點的權限。",
            401,
            True,
        )
    if status_code == 429:
        if reason:
            return ProviderConnectionFailure(
                "PROVIDER_RATE_LIMITED",
                f"Provider rate limit response. Upstream reason: {reason}",
                429,
                True,
            )
        return ProviderConnectionFailure(
            "PROVIDER_RATE_LIMITED",
            "API 已達速率或試用額度限制，請稍後再試或查看供應商帳戶。",
            429,
            True,
        )
    if status_code >= 500:
        if reason:
            return ProviderConnectionFailure(
                "PROVIDER_UPSTREAM_ERROR",
                f"Provider upstream failure. Upstream reason: {reason}",
                502,
                True,
            )
        return ProviderConnectionFailure(
            "PROVIDER_UPSTREAM_ERROR",
            "API 供應商目前無法完成連線測試。",
            502,
            True,
        )
    if reason:
        return ProviderConnectionFailure(
            "PROVIDER_TEST_FAILED",
            f"Provider rejected the request (HTTP {status_code}). Reason: {reason}",
            502,
            True,
        )
    return ProviderConnectionFailure(
        "PROVIDER_TEST_FAILED",
        f"API 端點拒絕模型清單測試（HTTP {status_code}）。",
        502,
        True,
    )


def test_provider_connection(
    *,
    provider_type: str,
    base_url: str,
    api_key: str,
    source_url: str = "",
    selected_model: str = "",
    timeout_seconds: float = 6.0,
    model_kind: str = "",
    supports_tools: bool = False,
    language_pair: str = "",
) -> dict[str, Any]:
    normalized_type = str(provider_type or "").strip().casefold()
    endpoint = normalize_provider_endpoint(normalized_type, base_url)
    credential_kind = PROVIDER_CATALOG[normalized_type]["credential_kind"]
    secret = str(api_key or "").strip()
    if credential_kind == "api_key" and not secret:
        raise ProviderConnectionFailure(
            "PROVIDER_KEY_REQUIRED",
            "請先輸入 API Key。",
            400,
            True,
        )
    headers = {"Accept": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    try:
        response = requests.get(
            _models_url(endpoint),
            headers=headers,
            timeout=max(1.0, min(float(timeout_seconds), 10.0)),
            allow_redirects=False,
        )
    except requests.Timeout as exc:
        raise ProviderConnectionFailure(
            "PROVIDER_TIMEOUT",
            "API 連線測試逾時。",
            504,
            True,
        ) from exc
    except requests.RequestException as exc:
        raise ProviderConnectionFailure(
            "PROVIDER_UNREACHABLE",
            "無法連線到 API 端點。",
            502,
            True,
        ) from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise _failure_for_status(
            response.status_code,
            response.text,
            secret=secret,
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    raw_models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        raw_models = payload.get("models") if isinstance(payload, dict) else None
    models = []
    for item in raw_models if isinstance(raw_models, list) else []:
        model_id = item.get("id") if isinstance(item, dict) else item
        if model_id:
            models.append(str(model_id)[:200])
    scoped_model = (
        model_id_from_source_url(normalized_type, source_url)
        or _clean_model_id(selected_model)
    )
    if scoped_model:
        if scoped_model not in models:
            raise ProviderConnectionFailure(
                "PROVIDER_MODEL_NOT_FOUND",
                "The model identified by the URL is not available from this API key.",
                400,
                True,
            )
        models = [scoped_model]
    profile_payload: dict[str, Any] = {}
    if scoped_model:
        profile = model_capability_profile(
            scoped_model,
            model_kind=model_kind,
            supports_tools=supports_tools,
            language_pair=language_pair,
        )
        profile_payload["model_profile"] = profile.as_dict()
    return {
        "status": "connected",
        "provider_type": normalized_type,
        "endpoint": endpoint,
        "model_count": len(models),
        "models": models[:250],
        **profile_payload,
    }


def _post_provider_model_request(
    *,
    endpoint: str,
    api_key: str,
    request_payload: Mapping[str, Any],
    timeout_seconds: float,
) -> requests.Response:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {str(api_key or '').strip()}",
    }
    try:
        response = requests.post(
            _chat_url(endpoint),
            headers=headers,
            json=request_payload,
            timeout=max(2.0, min(float(timeout_seconds), 30.0)),
            allow_redirects=False,
        )
    except requests.Timeout as exc:
        raise ProviderConnectionFailure(
            "PROVIDER_MODEL_TIMEOUT", "The model response test timed out.", 504, True
        ) from exc
    except requests.RequestException as exc:
        raise ProviderConnectionFailure(
            "PROVIDER_MODEL_UNREACHABLE", "Unable to reach the selected model.", 502, True
        ) from exc
    if response.status_code == 404:
        raise ProviderConnectionFailure(
            "PROVIDER_MODEL_UNAVAILABLE",
            "The provider lists this model, but the current API account cannot call it.",
            409,
            True,
        )
    if response.status_code < 200 or response.status_code >= 300:
        raise _failure_for_status(
            response.status_code,
            response.text,
            secret=str(api_key or "").strip(),
        )
    return response


def _provider_model_reply(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderConnectionFailure(
            "PROVIDER_MODEL_INVALID_RESPONSE",
            "The selected model did not return valid JSON.",
            502,
            True,
        ) from exc
    choices = payload.get("choices") if isinstance(payload, dict) else None
    first = (
        choices[0]
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping)
        else {}
    )
    message = first.get("message")
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        content = "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict)
        )
    reply = str(content or "").strip()
    if not reply:
        raise ProviderConnectionFailure(
            "PROVIDER_MODEL_EMPTY_RESPONSE",
            "The selected model returned no text content.",
            502,
            True,
        )
    return reply


_TOOL_PROBE_NAME = "workbench_capability_probe"


def _validate_provider_tool_probe(
    response: requests.Response,
    expected_nonce: str,
) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderConnectionFailure(
            "PROVIDER_TOOL_ATTESTATION_FAILED",
            "The tool capability probe did not return valid JSON.",
            409,
            True,
        ) from exc
    choices = payload.get("choices") if isinstance(payload, dict) else None
    first = (
        choices[0]
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping)
        else {}
    )
    message = first.get("message")
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    for item in tool_calls if isinstance(tool_calls, list) else []:
        function = item.get("function") if isinstance(item, Mapping) else None
        if not isinstance(function, Mapping):
            continue
        if str(function.get("name") or "") != _TOOL_PROBE_NAME:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                continue
        call_id = re.sub(
            r"[\x00-\x1f\x7f]+",
            "",
            str(item.get("id") or "").strip(),
        )[:200]
        if (
            isinstance(arguments, Mapping)
            and str(arguments.get("nonce") or "") == expected_nonce
            and call_id
        ):
            return {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": _TOOL_PROBE_NAME,
                    "arguments": json.dumps(
                        {"nonce": expected_nonce},
                        separators=(",", ":"),
                    ),
                },
            }
    raise ProviderConnectionFailure(
        "PROVIDER_TOOL_ATTESTATION_FAILED",
        "The model did not return the required tool call and nonce.",
        409,
        True,
    )


def _prepare_tool_probe(
    *,
    provider_type: str,
    base_url: str,
    api_key: str,
    selected_model: str,
    timeout_seconds: float,
    model_kind: str,
    supports_tools: bool,
    language_pair: str,
) -> tuple[Any, dict[str, Any]]:
    profile = model_capability_profile(
        selected_model,
        model_kind=model_kind,
        supports_tools=supports_tools,
        language_pair=language_pair,
    )
    if profile.kind != "chat" or not profile.supports_tools:
        raise ProviderConnectionFailure(
            "PROVIDER_TOOL_CAPABILITY_NOT_DECLARED",
            "Tool verification requires a chat model with supports_tools enabled.",
            400,
            False,
        )
    connection = test_provider_connection(
        provider_type=provider_type,
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=min(timeout_seconds, 10.0),
        selected_model=selected_model,
        model_kind=profile.kind,
        supports_tools=True,
        language_pair=profile.language_pair,
    )
    if selected_model not in connection["models"]:
        raise ProviderConnectionFailure(
            "PROVIDER_MODEL_NOT_FOUND",
            "The selected model is not available from this API key.",
            400,
            True,
        )
    return profile, connection


def _tool_probe_payload(selected_model: str, nonce: str, profile: Any) -> dict[str, Any]:
    return build_openai_chat_payload(
        {
            "model": selected_model,
            "messages": [{
                "role": "user",
                "content": (
                    f"Call {_TOOL_PROBE_NAME} once with nonce {nonce}. "
                    "Do not answer with normal text."
                ),
            }],
            "tools": [{
                "type": "function",
                "function": {
                    "name": _TOOL_PROBE_NAME,
                    "description": "Return the one-time Workbench capability nonce.",
                    "parameters": {
                        "type": "object",
                        "properties": {"nonce": {"type": "string"}},
                        "required": ["nonce"],
                        "additionalProperties": False,
                    },
                },
            }],
            "tool_choice": {
                "type": "function",
                "function": {"name": _TOOL_PROBE_NAME},
            },
            "max_tokens": 128,
            "temperature": 0.0,
        },
        stream=False,
        profile=profile,
    )


def _tool_completion_payload(
    selected_model: str,
    nonce: str,
    profile: Any,
    probe_payload: Mapping[str, Any],
    tool_call: Mapping[str, Any],
) -> dict[str, Any]:
    return build_openai_chat_payload(
        {
            "model": selected_model,
            "messages": [
                probe_payload["messages"][0],
                {"role": "assistant", "tool_calls": [tool_call]},
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": _TOOL_PROBE_NAME,
                    "content": json.dumps(
                        {"nonce": nonce, "status": "verified"},
                        separators=(",", ":"),
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Complete the probe by returning the verified nonce "
                        "exactly once in normal text."
                    ),
                },
            ],
            "tools": probe_payload["tools"],
            "tool_choice": "none",
            "max_tokens": 128,
            "temperature": 0.0,
        },
        stream=False,
        profile=profile,
    )


def _run_tool_probe_handshake(
    *,
    endpoint: str,
    api_key: str,
    selected_model: str,
    profile: Any,
    timeout_seconds: float,
    nonce: str,
) -> None:
    probe_payload = _tool_probe_payload(selected_model, nonce, profile)
    probe_response = _post_provider_model_request(
        endpoint=endpoint,
        api_key=api_key,
        request_payload=probe_payload,
        timeout_seconds=timeout_seconds,
    )
    tool_call = _validate_provider_tool_probe(probe_response, nonce)
    completion_payload = _tool_completion_payload(
        selected_model,
        nonce,
        profile,
        probe_payload,
        tool_call,
    )
    completion = _post_provider_model_request(
        endpoint=endpoint,
        api_key=api_key,
        request_payload=completion_payload,
        timeout_seconds=timeout_seconds,
    )
    if nonce not in _provider_model_reply(completion):
        raise ProviderConnectionFailure(
            "PROVIDER_TOOL_ATTESTATION_FAILED",
            "The model did not consume the tool result in the completion round.",
            409,
            True,
        )


def test_provider_tool_call(
    *,
    provider_type: str,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float = 30.0,
    model_kind: str = "",
    supports_tools: bool = False,
    language_pair: str = "",
) -> dict[str, Any]:
    """Prove tool support with a call/result/final-completion handshake."""

    selected_model = str(model or "").strip()
    profile, connection = _prepare_tool_probe(
        provider_type=provider_type,
        base_url=base_url,
        api_key=api_key,
        selected_model=selected_model,
        timeout_seconds=timeout_seconds,
        model_kind=model_kind,
        supports_tools=supports_tools,
        language_pair=language_pair,
    )
    nonce = secrets.token_urlsafe(18)
    _run_tool_probe_handshake(
        endpoint=connection["endpoint"],
        api_key=api_key,
        selected_model=selected_model,
        profile=profile,
        timeout_seconds=timeout_seconds,
        nonce=nonce,
    )
    fingerprint = capability_fingerprint(
        selected_model,
        connection["endpoint"],
        profile,
    )
    return {
        "status": "tool_call_verified",
        "selected_model": selected_model,
        "model_profile": profile.as_dict(),
        "tool_attestation": {
            "profile_fingerprint": fingerprint,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "method": "synthetic_tool_call",
            "passed": True,
        },
    }
def test_provider_model_response(
    *,
    provider_type: str,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    prompt: str,
    timeout_seconds: float = 30.0,
    model_kind: str = "",
    supports_tools: bool = False,
    language_pair: str = "",
) -> dict[str, Any]:
    selected_model = str(model or "").strip()
    profile = model_capability_profile(
        selected_model,
        model_kind=model_kind,
        supports_tools=supports_tools,
        language_pair=language_pair,
    )
    if profile.kind == "translation" and not profile.language_pair:
        profile = model_capability_profile(
            selected_model,
            model_kind="translation",
            supports_tools=False,
            language_pair=system_prompt,
        )
    if profile.kind not in {"chat", "translation"}:
        raise ProviderConnectionFailure(
            "PROVIDER_MODEL_KIND_REQUIRED",
            "Classify this model as chat or use its specialized capability adapter.",
            400,
            False,
        )
    connection = test_provider_connection(
        provider_type=provider_type,
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=min(timeout_seconds, 10.0),
        selected_model=selected_model,
        model_kind=profile.kind,
        supports_tools=profile.supports_tools,
        language_pair=profile.language_pair,
    )
    selected_model = str(model or "").strip()
    if selected_model not in connection["models"]:
        raise ProviderConnectionFailure(
            "PROVIDER_MODEL_NOT_FOUND",
            "指定模型不在這組 API 金鑰可使用的模型清單中。",
            400,
            True,
        )
    messages = []
    if str(system_prompt or "").strip():
        messages.append({"role": "system", "content": str(system_prompt).strip()})
    messages.append({"role": "user", "content": str(prompt).strip()})
    request_payload = build_openai_chat_payload(
        {
            "model": selected_model,
            "messages": messages,
            "max_tokens": 512,
            "temperature": 0.2,
            "language_pair": profile.language_pair,
        },
        stream=False,
        profile=profile,
    )
    response = _post_provider_model_request(
        endpoint=connection["endpoint"],
        api_key=api_key,
        request_payload=request_payload,
        timeout_seconds=timeout_seconds,
    )
    reply = _provider_model_reply(response)
    return {
        "status": "responded",
        "selected_model": selected_model,
        "response": reply[:12_000],
        "model_profile": profile.as_dict(),
    }
