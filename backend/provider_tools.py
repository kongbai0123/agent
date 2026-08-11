"""Task-specific adapters for imported model providers.

Specialized models deliberately do not enter the primary chat/Subagent model
inventory.  They become useful through narrow tools whose request shape matches
the model's declared capability.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from model_capabilities import model_capability_profile, normalize_language_pair
from model_client import (
    model_call_error,
    model_reference,
    post_specialized_completion,
    require_provider_enabled,
)
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
