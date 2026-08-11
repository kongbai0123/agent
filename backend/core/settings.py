"""Small, explicit settings surface for the conversational workbench."""

from __future__ import annotations

import json
import os
import re
import ipaddress
from pathlib import Path
from typing import Any, Dict, Mapping
from urllib.parse import urlsplit

from provider_connections import normalize_provider_settings


DEFAULT_SETTINGS: Dict[str, Any] = {
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_num_ctx": 8192,
    "model_provider": "ollama",
    "openai_compatible_url": "http://127.0.0.1:1234/v1",
    "openai_api_key_env": "OPENAI_API_KEY",
    "model_providers": [],
    "model_input_cost_per_million": 0.0,
    "model_output_cost_per_million": 0.0,
    "model_cost_currency": "USD",
    "default_chat_model": "gemma4-hermes:latest",
    "default_vision_model": "gemma4-hermes:latest",
    "network_proxy": "",
    "tts_auto_play": False,
    "tts_rate": 1.0,
    "ui_language": "zh-TW",
    "settings_modal_width": 900,
    "settings_modal_height": 650,
    "chat_run_budget_seconds": 600,
    "cancel_release_grace_seconds": 4.0,
    "cancel_release_poll_seconds": 0.5,
    "cancel_cleanup_wait_seconds": 4.0,
    # Hermes is an optional loopback sidecar.  The bearer value itself is
    # deliberately never persisted here; only its environment-variable name
    # is stored.
    "hermes_enabled": False,
    "hermes_base_url": "http://127.0.0.1:8642",
    "hermes_api_key_env": "HERMES_API_SERVER_KEY",
    "hermes_model": "gemma4-hermes:latest",
    "hermes_transport": "runs",
    "hermes_rollout_mode": "disabled",
    "hermes_rollout_percentage": 0.0,
    "hermes_canary_session_ids": [],
    "hermes_tools_enabled": False,
    "hermes_allowed_capabilities": [],
    "hermes_readonly_project_id": "",
    "hermes_fallback_enabled": True,
    "hermes_timeout_seconds": 15.0,
    "hermes_stream_read_timeout_seconds": 65.0,
    "hermes_max_response_bytes": 1_048_576,
}


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
_SAFE_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
HERMES_PERCENTAGE_LADDER = (5.0, 25.0, 50.0)
_HERMES_FIXED_ROLLOUT_STAGE = {
    "disabled": 0,
    "canary": 1,
    "all": 5,
}


def _hermes_rollout_stage(mode: str, percentage: float) -> int | None:
    if mode in _HERMES_FIXED_ROLLOUT_STAGE:
        return _HERMES_FIXED_ROLLOUT_STAGE[mode]
    if mode != "percentage":
        return None
    try:
        return 2 + HERMES_PERCENTAGE_LADDER.index(float(percentage))
    except ValueError:
        return None


def _validate_loopback_url(value: object) -> str:
    raw = str(value or DEFAULT_SETTINGS["hermes_base_url"]).strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("hermes_base_url is invalid.") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("hermes_base_url must use http or https.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("hermes_base_url must not contain credentials.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("hermes_base_url must not contain a path, query, or fragment.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("hermes_base_url port is invalid.")
    hostname = parsed.hostname.casefold()
    if hostname != "localhost":
        try:
            if not ipaddress.ip_address(hostname).is_loopback:
                raise ValueError("hermes_base_url must use a loopback address.")
        except ValueError as exc:
            if str(exc) == "hermes_base_url must use a loopback address.":
                raise
            raise ValueError("hermes_base_url must use a loopback address.") from exc
    return raw


def _bounded_string_list(
    value: object,
    *,
    field: str,
    maximum_items: int,
    item_maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list.")
    if len(value) > maximum_items:
        raise ValueError(f"{field} contains too many entries.")
    result: list[str] = []
    seen: set[str] = set()
    for raw_item in value:
        item = str(raw_item or "").strip()
        if not item or len(item) > item_maximum or any(ord(char) < 32 for char in item):
            raise ValueError(f"{field} contains an invalid entry.")
        if pattern is not None and not pattern.fullmatch(item):
            raise ValueError(f"{field} contains an invalid entry.")
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def settings_path() -> Path:
    return Path(
        os.environ.get("WORKBENCH_SETTINGS_PATH")
        or Path(__file__).resolve().parents[1] / "settings.json"
    ).resolve()


def _known_settings(raw: Mapping[str, Any]) -> Dict[str, Any]:
    settings = {
        key: raw.get(key, default)
        for key, default in DEFAULT_SETTINGS.items()
    }
    # One release of the basic runtime still stored the deadline under the
    # older orchestration-oriented key. Read it once without exposing that
    # legacy surface to the rest of the application.
    if "chat_run_budget_seconds" not in raw and "agent_run_budget_seconds" in raw:
        settings["chat_run_budget_seconds"] = raw.get("agent_run_budget_seconds")
    return settings


def load_settings() -> Dict[str, Any]:
    path = settings_path()
    if not path.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            return dict(DEFAULT_SETTINGS)
        return _known_settings(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)


def normalize_modal_size(data: Mapping[str, Any]) -> Dict[str, int]:
    try:
        width = int(data.get("settings_modal_width", 900))
        height = int(data.get("settings_modal_height", 650))
    except (TypeError, ValueError) as exc:
        raise ValueError("Settings modal dimensions must be integers.") from exc
    return {
        "settings_modal_width": max(620, min(3840, width)),
        "settings_modal_height": max(420, min(2160, height)),
    }


def validate_settings(data: Mapping[str, Any]) -> Dict[str, Any]:
    # Settings are updated by more than one UI card.  Treat an omitted key as
    # "leave unchanged" so saving the general card cannot silently disable a
    # separately configured Hermes rollout (and vice versa).
    current = load_settings()
    provided = {key: data[key] for key in DEFAULT_SETTINGS if key in data}
    if (
        "chat_run_budget_seconds" not in provided
        and "agent_run_budget_seconds" in data
    ):
        provided["chat_run_budget_seconds"] = data["agent_run_budget_seconds"]
    merged = {**current, **provided}
    merged["ollama_url"] = str(merged.get("ollama_url") or DEFAULT_SETTINGS["ollama_url"]).strip().rstrip("/")
    if not re.match(r"^https?://", merged["ollama_url"]):
        raise ValueError("ollama_url must use http or https.")
    merged["ollama_num_ctx"] = max(4096, min(32768, int(merged.get("ollama_num_ctx") or 8192)))

    merged["model_provider"] = str(merged.get("model_provider") or "ollama").strip()
    if merged["model_provider"] not in {"ollama", "openai_compatible"}:
        raise ValueError("model_provider is invalid.")
    merged["openai_compatible_url"] = str(
        merged.get("openai_compatible_url") or DEFAULT_SETTINGS["openai_compatible_url"]
    ).strip().rstrip("/")
    if not re.match(r"^https?://", merged["openai_compatible_url"]):
        raise ValueError("openai_compatible_url must use http or https.")
    merged["openai_api_key_env"] = re.sub(
        r"[^A-Za-z0-9_]", "", str(merged.get("openai_api_key_env") or "OPENAI_API_KEY")
    )[:80]
    merged["model_providers"] = normalize_provider_settings(merged.get("model_providers"))
    merged["model_input_cost_per_million"] = max(
        0.0, min(1_000_000.0, float(merged.get("model_input_cost_per_million") or 0.0))
    )
    merged["model_output_cost_per_million"] = max(
        0.0, min(1_000_000.0, float(merged.get("model_output_cost_per_million") or 0.0))
    )
    merged["model_cost_currency"] = str(merged.get("model_cost_currency") or "USD").strip().upper()[:8]

    proxy = str(merged.get("network_proxy") or "").strip()
    if proxy and not re.match(r"^https?://", proxy):
        raise ValueError("network_proxy must use http or https.")
    merged["network_proxy"] = proxy
    if merged.get("ui_language") not in {"zh-TW", "en-US"}:
        raise ValueError("ui_language must be zh-TW or en-US.")
    merged.update(normalize_modal_size(merged))
    merged["tts_auto_play"] = bool(merged.get("tts_auto_play", False))
    merged["tts_rate"] = max(0.5, min(2.0, float(merged.get("tts_rate") or 1.0)))
    merged["chat_run_budget_seconds"] = max(
        0, min(7200, int(merged.get("chat_run_budget_seconds") or 600))
    )
    merged["cancel_release_grace_seconds"] = max(
        1.0, min(15.0, float(merged.get("cancel_release_grace_seconds") or 4.0))
    )
    merged["cancel_release_poll_seconds"] = max(
        0.2, min(2.0, float(merged.get("cancel_release_poll_seconds") or 0.5))
    )
    merged["cancel_cleanup_wait_seconds"] = max(
        1.0, min(15.0, float(merged.get("cancel_cleanup_wait_seconds") or 4.0))
    )

    merged["hermes_enabled"] = bool(merged.get("hermes_enabled", False))
    merged["hermes_base_url"] = _validate_loopback_url(
        merged.get("hermes_base_url")
    )
    api_key_env = str(
        merged.get("hermes_api_key_env") or "HERMES_API_SERVER_KEY"
    ).strip()
    if not _ENV_NAME_RE.fullmatch(api_key_env):
        raise ValueError("hermes_api_key_env is invalid.")
    merged["hermes_api_key_env"] = api_key_env
    model = str(
        merged.get("hermes_model") or "gemma4-hermes:latest"
    ).strip()
    if not model or len(model) > 256 or any(ord(char) < 32 for char in model):
        raise ValueError("hermes_model is invalid.")
    merged["hermes_model"] = model
    transport = str(merged.get("hermes_transport") or "runs").strip().casefold()
    if transport not in {"runs", "chat"}:
        raise ValueError("hermes_transport must be runs or chat.")
    merged["hermes_transport"] = transport

    rollout_mode = str(
        merged.get("hermes_rollout_mode") or "disabled"
    ).strip().casefold()
    if rollout_mode not in {"disabled", "canary", "percentage", "all"}:
        raise ValueError("hermes_rollout_mode is invalid.")
    try:
        rollout_percentage = float(merged.get("hermes_rollout_percentage") or 0.0)
    except (TypeError, ValueError) as exc:
        raise ValueError("hermes_rollout_percentage must be a number.") from exc
    canary_ids = _bounded_string_list(
        merged.get("hermes_canary_session_ids"),
        field="hermes_canary_session_ids",
        maximum_items=500,
        item_maximum=256,
    )
    current_rollout_mode = str(
        current.get("hermes_rollout_mode") or "disabled"
    ).strip().casefold()
    if current_rollout_mode not in {"disabled", "canary", "percentage", "all"}:
        current_rollout_mode = "disabled"
    try:
        current_rollout_percentage = float(
            current.get("hermes_rollout_percentage") or 0.0
        )
    except (TypeError, ValueError):
        current_rollout_percentage = 0.0
    if (
        current_rollout_mode == "canary"
        and rollout_mode == "percentage"
        and "hermes_rollout_percentage" not in provided
    ):
        rollout_percentage = HERMES_PERCENTAGE_LADDER[0]
    elif (
        current_rollout_mode == "all"
        and rollout_mode == "percentage"
        and "hermes_rollout_percentage" not in provided
    ):
        rollout_percentage = HERMES_PERCENTAGE_LADDER[-1]
    current_stage = _hermes_rollout_stage(
        current_rollout_mode,
        current_rollout_percentage,
    )
    target_stage = _hermes_rollout_stage(rollout_mode, rollout_percentage)
    if current_stage is None and target_stage != _HERMES_FIXED_ROLLOUT_STAGE["disabled"]:
        raise ValueError(
            "The persisted Hermes rollout stage is invalid; reset it before promotion."
        )
    if target_stage is None:
        raise ValueError(
            "Hermes percentage rollout must use exactly 5, 25, or 50 percent."
        )
    expanding = current_stage is not None and target_stage > current_stage
    if expanding and bool(current.get("hermes_tools_enabled", False)):
        raise ValueError(
            "Disable Hermes project tools and save before expanding the text rollout."
        )
    if current_stage is not None and target_stage > current_stage + 1:
        raise ValueError(
            "Hermes rollout expansion must advance one stage at a time: "
            "disabled, canary, 5 percent, 25 percent, 50 percent, then all."
        )
    if rollout_mode == "disabled":
        rollout_percentage = 0.0
        canary_ids = []
    elif rollout_mode == "all":
        rollout_percentage = 100.0
        canary_ids = []
    elif rollout_mode == "percentage":
        if not 0.0 < rollout_percentage < 100.0:
            raise ValueError("Hermes percentage rollout must be between 0 and 100.")
        canary_ids = []
    elif not canary_ids:
        raise ValueError("Hermes canary rollout requires at least one session ID.")
    else:
        rollout_percentage = 0.0
    merged["hermes_rollout_mode"] = rollout_mode
    merged["hermes_rollout_percentage"] = rollout_percentage
    merged["hermes_canary_session_ids"] = canary_ids

    merged["hermes_tools_enabled"] = bool(
        merged.get("hermes_tools_enabled", False)
    )
    merged["hermes_allowed_capabilities"] = _bounded_string_list(
        merged.get("hermes_allowed_capabilities"),
        field="hermes_allowed_capabilities",
        maximum_items=128,
        item_maximum=128,
        pattern=_SAFE_CAPABILITY_RE,
    )
    readonly_project_id = str(
        merged.get("hermes_readonly_project_id") or ""
    ).strip()
    if merged["hermes_tools_enabled"]:
        if transport != "runs" or rollout_mode != "canary" or not canary_ids:
            raise ValueError(
                "Hermes project tools require live Docker isolation, Runs transport, "
                "and an explicit canary session."
            )
        if merged["hermes_allowed_capabilities"] != ["hermes.project.read"]:
            raise ValueError(
                "Hermes tools permit only the exact hermes.project.read capability."
            )
        if not _PROJECT_ID_RE.fullmatch(readonly_project_id):
            raise ValueError("hermes_readonly_project_id is invalid.")
    else:
        merged["hermes_allowed_capabilities"] = []
        readonly_project_id = ""
    merged["hermes_readonly_project_id"] = readonly_project_id
    merged["hermes_fallback_enabled"] = bool(
        merged.get("hermes_fallback_enabled", True)
    )
    merged["hermes_timeout_seconds"] = max(
        1.0, min(120.0, float(merged.get("hermes_timeout_seconds") or 15.0))
    )
    merged["hermes_stream_read_timeout_seconds"] = max(
        5.0,
        min(
            600.0,
            float(merged.get("hermes_stream_read_timeout_seconds") or 65.0),
        ),
    )
    try:
        max_response_bytes = int(
            merged.get("hermes_max_response_bytes") or 1_048_576
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("hermes_max_response_bytes must be an integer.") from exc
    merged["hermes_max_response_bytes"] = max(
        65_536, min(8_388_608, max_response_bytes)
    )
    return _known_settings(merged)


def save_settings(data: Mapping[str, Any]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_known_settings(data), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def apply_network_settings(settings: Mapping[str, Any]) -> None:
    proxy = str(settings.get("network_proxy") or "").strip()
    if proxy:
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy
    else:
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
