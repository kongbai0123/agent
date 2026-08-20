"""Task-specific adapters for imported model providers.

Specialized models deliberately do not enter the primary chat/Subagent model
inventory.  They become useful through narrow tools whose request shape matches
the model's declared capability.
"""

from __future__ import annotations

import json
import os
import re
import base64
import math
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import requests

import database

from model_capabilities import model_capability_profile, normalize_language_pair
from model_client import (
    model_call_error,
    model_reference,
    post_specialized_completion,
    require_provider_enabled,
)
from model_governance import ModelGovernanceService
from provider_connections import (
    NVIDIA_NEMOTRON_OCR_V2_MODEL,
    _nvidia_ocr_result,
    _post_nvidia_nemotron_ocr_v2_request,
    _validated_ocr_image_data_url,
)
from secret_store import get_provider_secret
from tool_runtime import ToolAccess, ToolCall, ToolDefinition
from workspace import current_workspace


def _load_runtime_settings() -> dict[str, Any]:
    path = Path(
        os.environ.get("WORKBENCH_SETTINGS_PATH")
        or Path(__file__).resolve().with_name("settings.json")
    )
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError("Workbench settings must be a JSON object.")
    return value


def _translation_provider(
    settings: Mapping[str, Any],
    provider_id: str = "",
    *,
    project_id: str | None = None,
) -> tuple[Mapping[str, Any], str]:
    requested = str(provider_id or "").strip().casefold()
    matches: list[tuple[Mapping[str, Any], str]] = []
    for item in settings.get("model_providers") or []:
        if not isinstance(item, Mapping):
            continue
        item_id = str(item.get("id") or "").strip().casefold()
        model = str(item.get("selected_model") or "").strip()
        if not item_id or not model or (requested and item_id != requested):
            continue
        try:
            require_provider_enabled(
                settings,
                item_id,
                project_id=project_id,
            )
        except PermissionError:
            continue
        profile = model_capability_profile(
            model,
            model_kind=str(item.get("model_kind") or ""),
            supports_tools=bool(item.get("supports_tools", False)),
            language_pair=str(item.get("language_pair") or ""),
        )
        if profile.kind == "translation":
            matches.append((item, model_reference(item_id, model)))
    if not matches:
        qualifier = f" {requested!r}" if requested else ""
        raise ValueError(f"No enabled translation-model connection{qualifier} is configured.")
    if len(matches) > 1 and not requested:
        raise ValueError("More than one translation provider is configured; specify provider_id.")
    return matches[0]


def _translation_text(response: Any) -> str:
    payload = response.json()
    content = str((payload.get("message") or {}).get("content") or "").strip()
    if not content:
        raise RuntimeError("The translation provider returned an empty response.")
    return content


def translate_text(
    text: str,
    target_language: str = "zh-cn",
    source_language: str = "en",
    provider_id: str = "",
) -> str:
    """Translate text with a configured specialized translation model."""

    content = str(text or "").strip()
    if not content:
        raise ValueError("text is required.")
    source = re.sub(r"[^A-Za-z0-9-]", "", str(source_language or "").strip()).casefold()
    target = re.sub(r"[^A-Za-z0-9-]", "", str(target_language or "").strip()).casefold()
    if not source or not target:
        raise ValueError("source_language and target_language are required.")
    language_pair = normalize_language_pair(f"{source}-{target}")
    settings = _load_runtime_settings()
    project_id = str(current_workspace().project_id or "").strip() or None
    _provider, model = _translation_provider(
        settings,
        provider_id,
        project_id=project_id,
    )
    response = post_specialized_completion(
        settings,
        {
            "model": model,
            "messages": [
                {"role": "system", "content": language_pair},
                {"role": "user", "content": content},
            ],
        },
        model_kind="translation",
        stream=False,
        timeout=60,
        project_id=project_id,
    )
    if response.status_code < 200 or response.status_code >= 300:
        failure = model_call_error(
            settings,
            model,
            response.status_code,
            response.text,
            project_id=project_id,
        )
        raise RuntimeError(f"{failure['message']} {failure.get('detail', '')}".strip())
    return _translation_text(response)


def _configured_capability(
    settings: Mapping[str, Any],
    kind: str,
    *,
    project_id: str,
    governance: ModelGovernanceService,
) -> tuple[Mapping[str, Any], str] | None:
    for item in settings.get("model_providers") or []:
        if not isinstance(item, Mapping) or item.get("enabled") is not True:
            continue
        provider_id = str(item.get("id") or "").strip().casefold()
        model = str(item.get("selected_model") or "").strip()
        if not provider_id or not model:
            continue
        try:
            require_provider_enabled(settings, provider_id, project_id=project_id)
            profile = model_capability_profile(
                model,
                model_kind=str(item.get("model_kind") or ""),
                supports_tools=bool(item.get("supports_tools")),
                language_pair=str(item.get("language_pair") or ""),
            )
        except (PermissionError, ValueError):
            continue
        inferred_kind = "ocr" if model.casefold() == NVIDIA_NEMOTRON_OCR_V2_MODEL else profile.kind
        if inferred_kind == kind:
            metadata = governance.credential_metadata(provider_id)
            operational = governance.state(
                provider_id,
                model_id=model,
                endpoint=str(item.get("base_url") or ""),
            )
            if not metadata.get("last_verified_at") or operational.get("state") != "healthy":
                continue
            return item, model
    return None


def _governed_specialized_call(
    governance: ModelGovernanceService,
    *,
    call: ToolCall,
    provider: Mapping[str, Any],
    model: str,
    capability: str,
    invoke: Callable[[], Any],
    image_megabytes: float = 0,
) -> Any:
    provider_id = str(provider.get("id") or "").casefold()
    endpoint = str(provider.get("base_url") or "")
    decision = governance.operational_decision(provider_id, model_id=model, endpoint=endpoint)
    if not decision.allowed:
        raise ValueError(decision.message or decision.code)
    call_id = f"special_{call.call_id}"
    input_rate = max(0.0, float(provider.get("input_cost_per_million") or 0))
    output_rate = max(0.0, float(provider.get("output_cost_per_million") or 0))
    budget = governance.budget_decision(
        project_id=call.project_id,
        run_id=call.run_id,
        call_id=call_id,
        reserve_tokens=4096,
        reserve_cost=(4096 * (input_rate + output_rate) / 1_000_000),
        currency=str(provider.get("currency") or "USD"),
    )
    if not budget.allowed:
        raise ValueError(budget.message)
    started = time.monotonic()
    try:
        result = invoke()
    except Exception as exc:
        status = int(getattr(exc, "status_code", 0) or 0)
        governance.observe_failure(
            provider_id,
            model_id=model,
            endpoint=endpoint,
            status_code=status,
            transport_error=not bool(status),
            capability=capability,
        )
        governance.record_usage(
            call_id=call_id,
            provider_id=provider_id,
            model_id=model,
            capability=capability,
            project_id=call.project_id,
            run_id=call.run_id,
            status="failed",
            image_megabytes=image_megabytes,
            latency_ms=int((time.monotonic() - started) * 1000),
            provider_signal=getattr(exc, "code", type(exc).__name__),
        )
        raise
    provider_usage: dict[str, Any] = {}
    if isinstance(result, Mapping) and isinstance(result.get("_governance_usage"), Mapping):
        provider_usage = dict(result.get("_governance_usage") or {})
        result = {key: value for key, value in result.items() if key != "_governance_usage"}
    prompt_tokens = max(0, int(provider_usage.get("prompt_tokens") or 0))
    completion_tokens = max(0, int(provider_usage.get("completion_tokens") or 0))
    governance.observe_success(provider_id, model_id=model, endpoint=endpoint)
    governance.record_usage(
        call_id=call_id,
        provider_id=provider_id,
        model_id=model,
        capability=capability,
        project_id=call.project_id,
        run_id=call.run_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        image_megabytes=image_megabytes,
        latency_ms=int((time.monotonic() - started) * 1000),
        estimated_cost=(
            prompt_tokens * input_rate / 1_000_000
            + completion_tokens * output_rate / 1_000_000
        ),
        currency=str(provider.get("currency") or "USD"),
    )
    return result


def _ocr_handler(
    settings: Mapping[str, Any],
    governance: ModelGovernanceService,
    provider: Mapping[str, Any],
    model: str,
) -> Callable[[ToolCall], Any]:
    def handler(call: ToolCall) -> Any:
        attachment_id = str(call.arguments.get("attachment_id") or "")
        attachment = database.get_attachment(attachment_id)
        if not attachment or str(attachment.get("project_id") or "") != call.project_id:
            raise ValueError("attachment is not available in the active project")
        mime = str(attachment.get("mime_type") or "").casefold()
        if mime not in {"image/png", "image/jpeg"}:
            raise ValueError("OCR supports PNG or JPEG attachments only")
        path = Path(str(attachment.get("storage_path") or ""))
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise ValueError("attachment storage is unavailable")
        data = path.read_bytes()
        if len(data) > 128 * 1024:
            raise ValueError("OCR direct upload is limited to 128 KiB")
        image_data_url = _validated_ocr_image_data_url(
            f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        )
        return _governed_specialized_call(
            governance,
            call=call,
            provider=provider,
            model=model,
            capability="ocr",
            image_megabytes=len(data) / (1024 * 1024),
            invoke=lambda: _nvidia_ocr_result(
                _post_nvidia_nemotron_ocr_v2_request(
                    api_key=get_provider_secret(str(provider["id"])),
                    image_data_url=image_data_url,
                    timeout_seconds=30,
                )
            ),
        )
    return handler


def _ranking_endpoint(provider: Mapping[str, Any], model: str) -> str:
    if str(provider.get("provider_type") or "").casefold() == "nvidia":
        short = model.split("/", 1)[-1]
        return f"https://ai.api.nvidia.com/v1/retrieval/nvidia/{short}/reranking"
    base = str(provider.get("base_url") or "").rstrip("/")
    return f"{base}/ranking" if base.endswith("/v1") else f"{base}/v1/ranking"


def _rerank_handler(governance: ModelGovernanceService, provider: Mapping[str, Any], model: str) -> Callable[[ToolCall], Any]:
    def handler(call: ToolCall) -> Any:
        query = str(call.arguments.get("query") or "").strip()
        passages = call.arguments.get("passages") or []
        if not query or not isinstance(passages, list) or not passages:
            raise ValueError("query and passages are required")
        normalized = []
        total_chars = 0
        for item in passages[:20]:
            if not isinstance(item, Mapping):
                continue
            text = str(item.get("text") or "")[:8192]
            total_chars += len(text)
            normalized.append({"id": str(item.get("id") or len(normalized))[:128], "text": text})
        if not normalized or total_chars > 65536:
            raise ValueError("rerank input is empty or exceeds 64 KiB")
        def invoke() -> Any:
            response = requests.post(
                _ranking_endpoint(provider, model),
                headers={"Authorization": f"Bearer {get_provider_secret(str(provider['id']))}", "Content-Type": "application/json"},
                json={"model": model, "query": {"text": query[:8192]}, "passages": [{"text": item["text"]} for item in normalized], "truncate": "END"},
                timeout=30,
            )
            if not 200 <= response.status_code < 300:
                error = RuntimeError(f"rerank provider rejected the request (HTTP {response.status_code})")
                error.status_code = response.status_code
                raise error
            payload = response.json(); rankings = payload.get("rankings") or payload.get("data") or []
            result = []
            for rank in rankings[:20] if isinstance(rankings, list) else []:
                if not isinstance(rank, Mapping): continue
                index = int(rank.get("index") or 0)
                if 0 <= index < len(normalized):
                    score = rank.get("logit", rank.get("score", 0))
                    result.append({"id": normalized[index]["id"], "score": float(score) if isinstance(score, (int, float)) and math.isfinite(float(score)) else 0.0})
            usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
            return {"rankings": result, "_governance_usage": dict(usage)}
        return _governed_specialized_call(governance, call=call, provider=provider, model=model, capability="rerank", invoke=invoke)
    return handler


def _embedding_endpoint(provider: Mapping[str, Any], model: str) -> str:
    if str(provider.get("provider_type") or "").casefold() == "nvidia":
        short = model.split("/", 1)[-1]
        return f"https://ai.api.nvidia.com/v1/retrieval/nvidia/{short}/embeddings"
    base = str(provider.get("base_url") or "").rstrip("/")
    return f"{base}/embeddings" if base.endswith("/v1") else f"{base}/v1/embeddings"


def _semantic_handler(governance: ModelGovernanceService, provider: Mapping[str, Any], model: str) -> Callable[[ToolCall], Any]:
    def handler(call: ToolCall) -> Any:
        query = str(call.arguments.get("query") or "")[:8192]
        candidates = [str(item)[:8192] for item in (call.arguments.get("candidates") or [])[:20]]
        if not query or not candidates or sum(map(len, candidates)) > 65536:
            raise ValueError("semantic match input is empty or exceeds 64 KiB")
        def invoke() -> Any:
            response = requests.post(
                _embedding_endpoint(provider, model),
                headers={"Authorization": f"Bearer {get_provider_secret(str(provider['id']))}", "Content-Type": "application/json"},
                json={"model": model, "input": [query, *candidates], "input_type": "query"},
                timeout=30,
            )
            if not 200 <= response.status_code < 300:
                error = RuntimeError(f"embedding provider rejected the request (HTTP {response.status_code})"); error.status_code = response.status_code; raise error
            payload = response.json(); rows = payload.get("data") or []
            vectors = [row.get("embedding") for row in rows if isinstance(row, Mapping)]
            if len(vectors) != len(candidates) + 1 or not all(isinstance(vector, list) for vector in vectors):
                raise ValueError("embedding provider returned an invalid response")
            q = vectors[0]
            def cosine(vector: list[Any]) -> float:
                if len(vector) != len(q): return 0.0
                dot = sum(float(a) * float(b) for a, b in zip(q, vector)); left = math.sqrt(sum(float(a) ** 2 for a in q)); right = math.sqrt(sum(float(b) ** 2 for b in vector))
                return 0.0 if not left or not right else max(-1.0, min(1.0, dot / (left * right)))
            usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
            return {"matches": sorted(({"index": index, "score": cosine(vector)} for index, vector in enumerate(vectors[1:])), key=lambda item: (-item["score"], item["index"])), "_governance_usage": dict(usage)}
        return _governed_specialized_call(governance, call=call, provider=provider, model=model, capability="embedding", invoke=invoke)
    return handler


def _translate_handler(settings: Mapping[str, Any], provider: Mapping[str, Any], model: str, project_id: str) -> Callable[[ToolCall], Any]:
    def handler(call: ToolCall) -> Any:
        text = str(call.arguments.get("text") or "").strip()
        source = re.sub(r"[^A-Za-z0-9-]", "", str(call.arguments.get("source_language") or "en")).casefold()
        target = re.sub(r"[^A-Za-z0-9-]", "", str(call.arguments.get("target_language") or "zh-tw")).casefold()
        if not text or len(text) > 32768:
            raise ValueError("translation text is required and limited to 32 KiB")
        pair = normalize_language_pair(f"{source}-{target}")
        scoped = {**dict(settings), "_governance_run_id": call.run_id}
        response = post_specialized_completion(
            scoped,
            {"model": model_reference(str(provider["id"]), model), "messages": [{"role": "system", "content": pair}, {"role": "user", "content": text}]},
            model_kind="translation",
            stream=False,
            timeout=60,
            project_id=project_id,
        )
        try:
            if not 200 <= response.status_code < 300:
                raise RuntimeError(model_call_error(scoped, model_reference(str(provider["id"]), model), response.status_code, response.text, project_id=project_id)["message"])
            return {"translation": _translation_text(response)}
        finally:
            response.close()
    return handler


def runtime_tool_definitions(
    settings: Mapping[str, Any],
    *,
    project_id: str,
    manifest_digest: Callable[[str], str],
    governance: ModelGovernanceService,
) -> tuple[ToolDefinition, ...]:
    definitions: list[ToolDefinition] = []
    specs = (
        ("ocr", "provider.ocr_image", "Read text from a PNG or JPEG attachment in the active project.", _ocr_handler,
         {"type": "object", "properties": {"attachment_id": {"type": "string", "minLength": 1, "maxLength": 160}}, "required": ["attachment_id"], "additionalProperties": False}),
        ("rerank", "provider.rerank_passages", "Rerank bounded candidate passages for a query.", _rerank_handler,
         {"type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 8192}, "passages": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "object", "properties": {"id": {"type": "string", "maxLength": 128}, "text": {"type": "string", "minLength": 1, "maxLength": 8192}}, "required": ["text"], "additionalProperties": False}}}, "required": ["query", "passages"], "additionalProperties": False}),
        ("embedding", "provider.semantic_match", "Score bounded candidate texts by semantic similarity without returning raw embeddings.", _semantic_handler,
         {"type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 8192}, "candidates": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "string", "minLength": 1, "maxLength": 8192}}}, "required": ["query", "candidates"], "additionalProperties": False}),
    )
    for kind, name, description, handler_factory, schema in specs:
        configured = _configured_capability(
            settings, kind, project_id=project_id, governance=governance
        )
        if configured is None:
            continue
        provider, model = configured; extension_id = f"provider.{str(provider['id']).casefold()}"; digest = manifest_digest(extension_id)
        if not digest:
            continue
        definitions.append(ToolDefinition(name=name, description=description, input_schema=schema, access=ToolAccess.READ, handler=handler_factory(settings, governance, provider, model) if kind == "ocr" else handler_factory(governance, provider, model), extension_id=extension_id, manifest_sha256=digest, risk_level="external_read", timeout_seconds=30, max_result_bytes=16 * 1024))
    translation = _configured_capability(
        settings, "translation", project_id=project_id, governance=governance
    )
    if translation is not None:
        provider, model = translation; extension_id = f"provider.{str(provider['id']).casefold()}"; digest = manifest_digest(extension_id)
        if digest:
            definitions.append(ToolDefinition(
                name="provider.translate_text",
                description="Translate bounded text with the configured project translation model.",
                input_schema={"type": "object", "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 32768}, "source_language": {"type": "string", "default": "en", "maxLength": 16}, "target_language": {"type": "string", "default": "zh-tw", "maxLength": 16}}, "required": ["text"], "additionalProperties": False},
                access=ToolAccess.READ,
                handler=_translate_handler(settings, provider, model, project_id),
                extension_id=extension_id,
                manifest_sha256=digest,
                risk_level="external_read",
                timeout_seconds=60,
                max_result_bytes=16 * 1024,
            ))
    return tuple(definitions)
