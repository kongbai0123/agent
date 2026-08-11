"""Provider-neutral chat client for Ollama and OpenAI-compatible servers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Mapping, Optional

import requests
from extension_manifest import safe_settings_identifier
from model_capabilities import (
    ModelCapabilityProfile,
    build_openai_chat_payload,
    capability_fingerprint,
    model_capability_profile,
    normalize_tool_attestation,
    safe_upstream_error_reason,
)
from secret_store import get_provider_secret

_PROVIDER_EXTENSION_GATE: Optional[Callable[[str, Optional[str]], bool]] = None


def configure_provider_extension_gate(
    gate: Optional[Callable[[str, Optional[str]], bool]],
) -> None:
    global _PROVIDER_EXTENSION_GATE
    _PROVIDER_EXTENSION_GATE = gate


def provider_extension_id(provider_id: str) -> str:
    normalized = str(provider_id or "ollama").strip().casefold()
    return (
        "builtin.ollama"
        if normalized in {"", "ollama"}
        else f"provider.{safe_settings_identifier(normalized)}"
    )


def _require_provider_enabled(settings: Mapping[str, Any], provider_id: str) -> None:
    normalized_id = str(provider_id or "ollama").strip().casefold() or "ollama"
    providers = settings.get("model_providers")
    if normalized_id != "ollama" and isinstance(providers, list) and providers:
        configured = next(
            (
                item
                for item in providers
                if isinstance(item, Mapping)
                and str(item.get("id") or "").strip().casefold() == normalized_id
            ),
            None,
        )
        # Missing is handled by the caller as "not configured". A configured
        # provider must carry an affirmative local enable bit even when its
        # trusted ExtensionStore entry is enabled.
        if configured is not None and configured.get("enabled") is not True:
            raise PermissionError(f"Model provider is disabled: {normalized_id}")

    if _PROVIDER_EXTENSION_GATE is None:
        return
    project_id = str(settings.get("_extension_project_id") or "").strip() or None
    extension_id = provider_extension_id(normalized_id)
    if not _PROVIDER_EXTENSION_GATE(extension_id, project_id):
        raise PermissionError(
            f"Model provider extension is disabled for this project: {extension_id}"
        )


@dataclass(frozen=True)
class ModelProviderConfig:
    provider: str
    protocol: str
    base_url: str
    api_key: str = ""
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    currency: str = "USD"

    @classmethod
    def from_settings(
        cls,
        settings: Mapping[str, Any],
        *,
        model: str = "",
        provider_id: str = "",
    ) -> "ModelProviderConfig":
        model_provider, _model_name = split_model_reference(model)
        selected_id = str(provider_id or model_provider or "").strip().casefold()
        providers = settings.get("model_providers")
        modern_selection = bool(providers) or selected_id not in {"", "openai_compatible"}
        if isinstance(providers, list) and modern_selection:
            if not selected_id:
                selected_id = "ollama"
            _require_provider_enabled(settings, selected_id)
            if selected_id == "ollama":
                return cls(
                    "ollama",
                    "ollama",
                    str(settings.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/"),
                )
            for item in providers:
                if not isinstance(item, Mapping):
                    continue
                if str(item.get("id") or "").strip().casefold() != selected_id:
                    continue

                base_url = str(item.get("base_url") or "").strip().rstrip("/")
                if not base_url:
                    raise ValueError(f"Model provider URL is missing: {selected_id}")
                return cls(
                    selected_id,
                    "openai_compatible",
                    base_url,
                    get_provider_secret(selected_id),
                    max(0.0, float(item.get("input_cost_per_million") or 0.0)),
                    max(0.0, float(item.get("output_cost_per_million") or 0.0)),
                    str(item.get("currency") or "USD").upper()[:8],
                )
            raise ValueError(f"Model provider is not configured: {selected_id}")

        # Backwards-compatible migration path for pre-0.5 settings.
        provider = str(settings.get("model_provider") or "ollama").strip().casefold()
        if provider == "ollama":
            _require_provider_enabled(settings, "ollama")
            return cls(
                "ollama",
                "ollama",
                str(settings.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/"),
            )
        if provider != "openai_compatible":
            raise ValueError(f"Unsupported model provider: {provider}")
        _require_provider_enabled(settings, "openai_compatible")
        base_url = str(settings.get("openai_compatible_url") or "").strip().rstrip("/")
        if not base_url:
            raise ValueError("openai_compatible_url is required")
        key_env = str(settings.get("openai_api_key_env") or "OPENAI_API_KEY").strip()
        return cls(
            provider,
            "openai_compatible",
            base_url,
            os.environ.get(key_env, "") if key_env else "",
            max(0.0, float(settings.get("model_input_cost_per_million") or 0.0)),
            max(0.0, float(settings.get("model_output_cost_per_million") or 0.0)),
            str(settings.get("model_cost_currency") or "USD").upper()[:8],
        )


def split_model_reference(model: str) -> tuple[str, str]:
    value = str(model or "").strip()
    if "::" not in value:
        return "", value
    provider_id, model_name = value.split("::", 1)
    return provider_id.strip().casefold(), model_name.strip()


def model_reference(provider_id: str, model_name: str) -> str:
    provider = str(provider_id or "").strip().casefold()
    name = str(model_name or "").strip()
    return name if provider in {"", "ollama"} else f"{provider}::{name}"


def provider_for_model(
    settings: Mapping[str, Any],
    model: str,
    *,
    project_id: Optional[str] = None,
) -> ModelProviderConfig:
    scoped = (
        {**settings, "_extension_project_id": project_id}
        if project_id is not None
        else settings
    )
    return ModelProviderConfig.from_settings(scoped, model=model)


def require_provider_enabled(
    settings: Mapping[str, Any],
    provider_id: str,
    *,
    project_id: Optional[str] = None,
) -> None:
    """Require both the saved provider bit and the ExtensionStore gate."""

    scoped = (
        {**settings, "_extension_project_id": project_id}
        if project_id is not None
        else settings
    )
    _require_provider_enabled(scoped, provider_id)


def uses_local_model_slot(
    settings: Mapping[str, Any],
    model: str,
    *,
    project_id: Optional[str] = None,
) -> bool:
    """Return whether a model owns local Ollama memory and scheduling state."""
    return provider_for_model(
        settings,
        model,
        project_id=project_id,
    ).protocol == "ollama"


def model_profile_for_model(
    settings: Mapping[str, Any],
    model: str,
    *,
    project_id: Optional[str] = None,
) -> ModelCapabilityProfile:
    """Resolve one model's task contract independently of its transport."""

    config = provider_for_model(settings, model, project_id=project_id)
    _provider_id, model_name = split_model_reference(model)
    if config.protocol == "ollama":
        return model_capability_profile(
            model_name or model,
            model_kind="chat",
            supports_tools=True,
            local_chat_default=True,
        )
    providers = settings.get("model_providers")
    if isinstance(providers, list) and providers:
        for item in providers:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("id") or "").strip().casefold() != config.provider:
                continue
            profile_model = model_name or str(item.get("selected_model") or "")
            declared_profile = model_capability_profile(
                profile_model,
                model_kind=str(item.get("model_kind") or ""),
                supports_tools=item.get("supports_tools") is True,
                language_pair=str(item.get("language_pair") or ""),
            )
            if not declared_profile.supports_tools:
                return declared_profile
            fingerprint = capability_fingerprint(
                profile_model,
                config.base_url,
                declared_profile,
            )
            attestation = normalize_tool_attestation(
                item.get("tool_attestation"),
                expected_fingerprint=fingerprint,
                tools_enabled=True,
            )
            if attestation is not None:
                return declared_profile
            # A checkbox is a declaration, not evidence. Runtime tool schemas
            # stay disabled until the current model/endpoint has made a real,
            # validated tool call through the bounded tool-test route.
            return model_capability_profile(
                profile_model,
                model_kind=declared_profile.kind,
                supports_tools=False,
                language_pair=declared_profile.language_pair,
            )
        return model_capability_profile(model_name)
    # Pre-capability single-provider settings explicitly represented a chat
    # endpoint. Preserve implicit tool support only when no ExtensionStore gate
    # exists; production must fail closed until capability metadata is saved.
    return model_capability_profile(
        model_name or model,
        supports_tools=_PROVIDER_EXTENSION_GATE is None,
        legacy_chat_default=True,
    )


def model_supports_tools(
    settings: Mapping[str, Any],
    model: str,
    *,
    project_id: Optional[str] = None,
) -> bool:
    """Return whether this exact model connection may receive tool schemas.

    Ollama keeps its existing native-tool behaviour. Modern imported providers
    are fail-closed because OpenAI-compatible transport does not imply that a
    particular hosted model accepts ``tools``. A provider must declare
    ``supports_tools`` and retain a current passed tool-call attestation.

    Legacy single-provider settings retain their previous behaviour.
    """
    return model_profile_for_model(
        settings, model, project_id=project_id
    ).supports_tools


def provider_chat_payload(
    settings: Mapping[str, Any],
    model: str,
    payload: Mapping[str, Any],
    *,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Remove optional capabilities that the selected connection cannot use."""
    result = dict(payload)
    if not model_supports_tools(
        settings,
        model,
        project_id=project_id,
    ):
        result.pop("tools", None)
        result.pop("tool_choice", None)
    return result


def subagent_chat_payload(
    settings: Mapping[str, Any],
    model: str,
    payload: Mapping[str, Any],
    *,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Prepare a role call without leaking Ollama-only controls to providers."""
    result = dict(payload)
    if uses_local_model_slot(settings, model, project_id=project_id):
        result["think"] = False
    else:
        profile = model_profile_for_model(
            settings, model, project_id=project_id
        )
        if not profile.eligible_for_subagent:
            raise ValueError(
                f"Model kind {profile.kind!r} is not eligible for Subagent roles."
            )
    return result


def model_call_error(
    settings: Mapping[str, Any],
    model: str,
    status_code: int,
    response_text: str = "",
    *,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify an upstream failure without exposing provider response secrets."""
    config = provider_for_model(settings, model, project_id=project_id)
    status = int(status_code or 0)
    if config.protocol == "ollama":
        return {
            "code": "OLLAMA_ERROR",
            "message": "Ollama returned an error.",
            "detail": str(response_text or "")[:1000],
            "provider": "ollama",
        }
    provider_label = config.provider.upper()
    reason = "" if status in {401, 403} else safe_upstream_error_reason(
        response_text,
        secrets=(config.api_key,),
    )
    if status in {401, 403}:
        code = "PROVIDER_AUTH_FAILED"
        message = f"{provider_label} API 金鑰無效，或帳戶沒有使用此模型的權限。"
    elif status == 404:
        code = "PROVIDER_MODEL_UNAVAILABLE"
        message = (
            f"{provider_label} 已列出此模型，但目前帳戶無法呼叫它。"
            "請在 API 連線中先取得模型回覆，再切換使用。"
        )
    elif status == 429:
        code = "PROVIDER_RATE_LIMITED"
        message = f"{provider_label} 已達速率或試用額度限制，請稍後再試。"
    elif status >= 500:
        code = "PROVIDER_UPSTREAM_ERROR"
        message = f"{provider_label} 目前無法完成模型請求。"
    else:
        code = "PROVIDER_MODEL_ERROR"
        message = f"{provider_label} 拒絕模型請求（HTTP {status or 'unknown'}）。"
    if reason:
        message = f"{message} Upstream reason: {reason}"
    return {
        "code": code,
        "message": message,
        "detail": f"{config.provider} HTTP {status or 'unknown'}"
        + (f": {reason}" if reason else ""),
        "provider": config.provider,
    }


def model_transport_error(
    settings: Mapping[str, Any],
    model: str,
    exception: Exception,
    *,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    config = provider_for_model(settings, model, project_id=project_id)
    if config.protocol == "ollama":
        return {
            "code": "OLLAMA_NOT_CONNECTED",
            "message": "Connection to Ollama failed.",
            "detail": str(exception),
            "provider": "ollama",
        }
    return {
        "code": "PROVIDER_UNREACHABLE",
        "message": (
            f"{config.provider.upper()} 連線失敗，"
            "請檢查網路、API 額度與模型權限。"
        ),
        "detail": f"{config.provider} request failed",
        "provider": config.provider,
    }


def _openai_url(config: ModelProviderConfig, suffix: str) -> str:
    base = config.base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/{suffix.lstrip('/')}"
    return f"{base}/v1/{suffix.lstrip('/')}"


def _headers(config: ModelProviderConfig) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    return headers


def _openai_payload(
    payload: Mapping[str, Any],
    stream: bool,
    profile: Optional[ModelCapabilityProfile] = None,
) -> Dict[str, Any]:
    resolved = profile or model_capability_profile(
        str(payload.get("model") or "legacy-chat"),
        model_kind="chat",
        supports_tools=True,
        legacy_chat_default=True,
    )
    return build_openai_chat_payload(payload, stream=stream, profile=resolved)


class CompatibleChatResponse:
    """Expose OpenAI SSE as the Ollama-shaped stream expected by existing loops."""

    def __init__(self, response: requests.Response, provider: str, protocol: Optional[str] = None):
        self._response = response
        self.provider = provider
        self.protocol = protocol or provider
        self.status_code = response.status_code

    @property
    def text(self) -> str:
        return self._response.text

    def close(self) -> None:
        self._response.close()

    def json(self) -> Dict[str, Any]:
        payload = self._response.json()
        if self.protocol == "ollama":
            return payload
        choices = payload.get("choices") or []
        message = dict((choices[0].get("message") if choices else None) or {})
        message["tool_calls"] = _normalize_openai_tool_calls(message.get("tool_calls") or [])
        usage = payload.get("usage") or {}
        return {
            "message": message,
            "prompt_eval_count": int(usage.get("prompt_tokens") or 0),
            "eval_count": int(usage.get("completion_tokens") or 0),
            "done_reason": str((choices[0].get("finish_reason") if choices else None) or ""),
            "done": True,
        }

    def iter_lines(self) -> Iterator[bytes]:
        if self.protocol == "ollama":
            if not hasattr(self._response, "iter_lines"):
                yield json.dumps(self._response.json(), ensure_ascii=False).encode("utf-8")
                return
            yield from self._response.iter_lines()
            return
        tool_calls: Dict[int, Dict[str, Any]] = {}
        usage: Dict[str, Any] = {}
        finish_reason = ""
        for raw in self._response.iter_lines():
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            if not text.startswith("data:"):
                continue
            data = text[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if choices and choices[0].get("finish_reason") is not None:
                finish_reason = str(choices[0].get("finish_reason") or "")
            delta = (choices[0].get("delta") if choices else None) or {}
            content = delta.get("content")
            if content:
                yield json.dumps({"message": {"content": content}, "done": False}).encode("utf-8")
            for item in delta.get("tool_calls") or []:
                index = int(item.get("index") or 0)
                current = tool_calls.setdefault(index, {"id": item.get("id"), "function": {"name": "", "arguments": ""}})
                if item.get("id"):
                    current["id"] = item["id"]
                function = item.get("function") or {}
                current["function"]["name"] += str(function.get("name") or "")
                current["function"]["arguments"] += str(function.get("arguments") or "")
        final: Dict[str, Any] = {
            "message": {},
            "done": True,
            "prompt_eval_count": int(usage.get("prompt_tokens") or 0),
            "eval_count": int(usage.get("completion_tokens") or 0),
            "done_reason": finish_reason,
        }
        if tool_calls:
            final["message"]["tool_calls"] = _normalize_openai_tool_calls(
                [tool_calls[index] for index in sorted(tool_calls)]
            )
        yield json.dumps(final).encode("utf-8")


def _normalize_openai_tool_calls(tool_calls: list[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    normalized = []
    for item in tool_calls:
        function = dict(item.get("function") or {})
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        normalized.append({
            "id": item.get("id"),
            "function": {
                "name": str(function.get("name") or ""),
                "arguments": arguments if isinstance(arguments, dict) else {},
            },
        })
    return normalized


def _post_completion(
    settings: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    required_model_kind: str,
    stream: bool,
    timeout: Any,
    project_id: Optional[str],
) -> CompatibleChatResponse:
    model = str(payload.get("model") or "")
    profile = model_profile_for_model(
        settings,
        model,
        project_id=project_id,
    )
    if profile.kind != required_model_kind:
        raise ValueError(
            f"Model kind {profile.kind!r} cannot be used through the "
            f"{required_model_kind!r} request path."
        )
    original_payload = provider_chat_payload(
        settings,
        model,
        payload,
        project_id=project_id,
    )
    scoped_settings = (
        {**settings, "_extension_project_id": project_id}
        if project_id is not None
        else settings
    )
    config = ModelProviderConfig.from_settings(
        scoped_settings,
        model=model,
    )
    _provider_id, request_model = split_model_reference(model)
    if request_model:
        original_payload["model"] = request_model
    if config.protocol == "ollama":
        options = dict(original_payload.get("options") or {})
        options.setdefault(
            "num_ctx",
            max(4096, min(32768, int(settings.get("ollama_num_ctx") or 8192))),
        )
        original_payload["options"] = options
        response = requests.post(
            f"{config.base_url}/api/chat",
            json={**original_payload, "stream": stream},
            stream=stream,
            timeout=timeout,
        )
    else:
        response = requests.post(
            _openai_url(config, "chat/completions"),
            json=_openai_payload(original_payload, stream, profile),
            headers=_headers(config),
            stream=stream,
            timeout=timeout,
        )
    return CompatibleChatResponse(response, config.provider, config.protocol)


def post_chat(
    settings: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    stream: bool = True,
    timeout: Any = 360,
    project_id: Optional[str] = None,
) -> CompatibleChatResponse:
    """Call a general chat model; specialized models fail closed."""

    return _post_completion(
        settings,
        payload,
        required_model_kind="chat",
        stream=stream,
        timeout=timeout,
        project_id=project_id,
    )


def post_specialized_completion(
    settings: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    model_kind: str,
    stream: bool = False,
    timeout: Any = 360,
    project_id: Optional[str] = None,
) -> CompatibleChatResponse:
    """Call a narrow capability adapter without entering chat/Agent paths."""

    specialized_kind = str(model_kind or "").strip().casefold()
    if specialized_kind != "translation":
        raise ValueError("Only the translation specialized adapter is available.")
    return _post_completion(
        settings,
        payload,
        required_model_kind=specialized_kind,
        stream=stream,
        timeout=timeout,
        project_id=project_id,
    )


def list_models(
    settings: Mapping[str, Any],
    timeout: int = 5,
    *,
    provider_id: str = "",
    project_id: Optional[str] = None,
) -> list[Dict[str, Any]]:
    scoped_settings = (
        {**settings, "_extension_project_id": project_id}
        if project_id is not None
        else settings
    )
    namespaced = isinstance(settings.get("model_providers"), list) or bool(provider_id)
    config = ModelProviderConfig.from_settings(scoped_settings, provider_id=provider_id)
    if config.protocol == "ollama":
        response = requests.get(f"{config.base_url}/api/tags", timeout=timeout)
        response.raise_for_status()
        profile = model_capability_profile(
            "ollama",
            model_kind="chat",
            supports_tools=True,
            local_chat_default=True,
        )
        return [
            {
                **item,
                "provider": "ollama",
                "provider_label": "Ollama",
                "kind": profile.kind,
                "profile": profile.as_dict(),
            }
            for item in response.json().get("models") or []
            if isinstance(item, Mapping) and item.get("name")
        ]
    configured_provider = next(
        (
            item
            for item in settings.get("model_providers") or []
            if isinstance(item, Mapping)
            and str(item.get("id") or "").strip().casefold() == config.provider
        ),
        None,
    )
    if configured_provider is not None:
        selected_model = str(configured_provider.get("selected_model") or "").strip()
        if not selected_model:
            return []
        reference = (
            model_reference(config.provider, selected_model)
            if namespaced
            else selected_model
        )
        profile = model_capability_profile(
            selected_model,
            model_kind=str(configured_provider.get("model_kind") or ""),
            supports_tools=bool(configured_provider.get("supports_tools", False)),
            language_pair=str(configured_provider.get("language_pair") or ""),
        )
        return [{
            "name": reference,
            "model": reference,
            "provider": config.provider,
            "scoped": True,
            "kind": profile.kind,
            "profile": profile.as_dict(),
        }]
    response = requests.get(_openai_url(config, "models"), headers=_headers(config), timeout=timeout)
    response.raise_for_status()
    return [
        {
            **({"kind": profile.kind, "profile": profile.as_dict()}),
            "name": (
                model_reference(config.provider, str(item.get("id") or ""))
                if namespaced
                else str(item.get("id") or "")
            ),
            "model": (
                model_reference(config.provider, str(item.get("id") or ""))
                if namespaced
                else str(item.get("id") or "")
            ),
            "provider": config.provider,
        }
        for item in response.json().get("data") or []
        for profile in [model_capability_profile(
            str(item.get("id") or ""),
            supports_tools=True,
            legacy_chat_default=True,
        )]
        if item.get("id")
    ]


def _list_all_model_inventory(
    settings: Mapping[str, Any], timeout: int = 5
) -> list[Dict[str, Any]]:
    models: list[Dict[str, Any]] = []
    try:
        models.extend(list_models(settings, timeout=timeout, provider_id="ollama"))
    except Exception:
        pass
    providers = settings.get("model_providers")
    if not isinstance(providers, list) or not providers:
        if str(settings.get("model_provider") or "ollama") != "ollama":
            try:
                models.extend(list_models(settings, timeout=timeout))
            except Exception:
                pass
        return models
    for item in providers:
        if not isinstance(item, Mapping) or item.get("enabled") is not True:
            continue
        provider_id = str(item.get("id") or "").strip().casefold()
        if not provider_id or provider_id == "ollama":
            continue
        try:
            provider_models = list_models(settings, timeout=timeout, provider_id=provider_id)
        except Exception:
            continue
        label = str(item.get("label") or provider_id)
        models.extend([{**model, "provider_label": label} for model in provider_models])
    return models


def list_all_models(
    settings: Mapping[str, Any], timeout: int = 5
) -> list[Dict[str, Any]]:
    """Return only models that may serve as the primary chat model."""

    return [
        item
        for item in _list_all_model_inventory(settings, timeout=timeout)
        if str(item.get("kind") or "") == "chat"
        and bool((item.get("profile") or {}).get("eligible_for_primary"))
    ]


def list_specialized_models(
    settings: Mapping[str, Any], timeout: int = 5
) -> list[Dict[str, Any]]:
    """Return task-specific models kept out of primary/Subagent chat lists."""

    specialized = {"translation", "embedding", "rerank", "vision"}
    return [
        item
        for item in _list_all_model_inventory(settings, timeout=timeout)
        if str(item.get("kind") or "") in specialized
    ]


def list_tool_models(
    settings: Mapping[str, Any], timeout: int = 5
) -> list[Dict[str, Any]]:
    """Compatibility name for consumers that expose specialized API tools."""

    return list_specialized_models(settings, timeout=timeout)
