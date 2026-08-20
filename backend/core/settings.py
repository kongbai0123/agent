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
from subprocess_env import is_allowed_subprocess_env_name, is_secret_env_name


DEFAULT_SETTINGS: Dict[str, Any] = {
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_num_ctx": 8192,
    "model_provider": "ollama",
    "openai_compatible_url": "http://127.0.0.1:1234/v1",
    "openai_api_key_env": "OPENAI_API_KEY",
    "model_providers": [],
    "mcp_servers": [],
    "model_input_cost_per_million": 0.0,
    "model_output_cost_per_million": 0.0,
    "model_cost_currency": "USD",
    "default_chat_model": "gemma4-hermes:latest",
    "default_vision_model": "gemma4-hermes:latest",
    "network_proxy": "",
    "tts_auto_play": False,
    "tts_rate": 1.0,
    "ui_language": "zh-TW",
      "settings_modal_width": 1040,
      "settings_modal_height": 760,
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
_MCP_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_MCP_SECRET_ALIAS_RE = re.compile(
    r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
)
_MCP_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_MCP_SHELL_SUFFIXES = frozenset({".bat", ".cmd", ".ps1", ".sh"})
_MCP_SHELL_EXECUTABLES = frozenset(
    {
        "bash",
        "bash.exe",
        "cmd",
        "cmd.exe",
        "cscript",
        "cscript.exe",
        "dash",
        "fish",
        "ksh",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "sh",
        "sh.exe",
        "wscript",
        "wscript.exe",
        "wsl",
        "wsl.exe",
        "zsh",
    }
)
_MCP_SERVER_FIELDS = frozenset(
    {
        "id",
        "label",
        "transport",
        "executable",
        "expected_executable_sha256",
        "argv",
        "cwd",
        "allowed_cwd_roots",
        "environment_keys",
        "secret_aliases",
        "tool_policies",
        "timeout_seconds",
        "enabled",
    }
)
_MCP_RISK_LEVELS = frozenset(
    {
        "read",
        "external_read",
        "verify",
        "write",
        "external_write",
        "system",
        "irreversible",
    }
)
_MCP_READ_RISKS = frozenset({"read", "external_read", "verify"})
_MCP_TOOL_POLICY_FIELDS = frozenset(
    {"access", "risk_level", "requires_connection", "requires_resource"}
)
_MCP_MAX_SERVERS = 16
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


def _mcp_local_path(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an absolute local path.")
    text = value.strip()
    if (
        not text
        or len(text) > 1024
        or any(ord(char) < 32 for char in text)
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text)
        or text.casefold().startswith("file:")
        or text.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\"))
    ):
        raise ValueError(f"{field} must be an absolute local path.")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field} must be an absolute local path.")
    return os.path.normpath(str(path))


def _mcp_path_within(path: str, roots: list[str]) -> bool:
    try:
        normalized_path = os.path.normcase(os.path.abspath(path))
        return any(
            os.path.commonpath(
                [normalized_path, os.path.normcase(os.path.abspath(root))]
            )
            == os.path.normcase(os.path.abspath(root))
            for root in roots
        )
    except (OSError, ValueError):
        return False


def _mcp_string_array(
    value: object,
    *,
    field: str,
    maximum_items: int,
    item_maximum: int,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValueError(f"{field} must be a bounded array of strings.")
    result: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or len(item) > item_maximum
            or "\x00" in item
            or any(ord(char) < 32 for char in item)
        ):
            raise ValueError(f"{field} contains an invalid string.")
        result.append(item)
    return result


def _normalize_mcp_environment_keys(value: object) -> list[str]:
    keys = _mcp_string_array(
        value,
        field="mcp_servers.environment_keys",
        maximum_items=64,
        item_maximum=80,
    )
    result: list[str] = []
    seen: set[str] = set()
    for key in keys:
        normalized = key.strip().upper()
        if (
            not _ENV_NAME_RE.fullmatch(normalized)
            or is_secret_env_name(normalized)
            or not is_allowed_subprocess_env_name(normalized)
        ):
            raise ValueError(
                "mcp_servers.environment_keys accepts only operational allowlisted names."
            )
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


def _normalize_mcp_secret_aliases(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 64:
        raise ValueError("mcp_servers.secret_aliases must be a bounded object.")
    result: dict[str, str] = {}
    for raw_name, raw_alias in value.items():
        if not isinstance(raw_name, str) or not isinstance(raw_alias, str):
            raise ValueError("mcp_servers.secret_aliases contains an invalid entry.")
        name = raw_name.strip().upper()
        alias = raw_alias.strip().casefold()
        if not _ENV_NAME_RE.fullmatch(name) or not is_secret_env_name(name):
            raise ValueError(
                "mcp_servers.secret_aliases keys must be credential environment names."
            )
        if (
            not _MCP_SECRET_ALIAS_RE.fullmatch(alias)
            or len(alias) > 128
            or alias.startswith(("sk-", "ghp_", "gho_", "ghu_", "ghs_", "github_pat_"))
        ):
            raise ValueError(
                "mcp_servers.secret_aliases values must be non-secret alias identifiers."
            )
        result[name] = alias
    return result


def _normalize_mcp_tool_policies(value: object) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 128:
        raise ValueError("mcp_servers.tool_policies must be a bounded object.")
    result: dict[str, dict[str, Any]] = {}
    for raw_name, raw_policy in value.items():
        if (
            not isinstance(raw_name, str)
            or not raw_name.strip()
            or len(raw_name) > 128
            or any(ord(char) < 32 for char in raw_name)
            or not isinstance(raw_policy, dict)
        ):
            raise ValueError("mcp_servers.tool_policies contains an invalid tool.")
        unknown = set(raw_policy) - _MCP_TOOL_POLICY_FIELDS
        if unknown:
            raise ValueError(
                "mcp_servers.tool_policies contains unknown policy fields."
            )
        access = raw_policy.get("access")
        risk_level = raw_policy.get("risk_level")
        if access not in {"read", "write"} or risk_level not in _MCP_RISK_LEVELS:
            raise ValueError("mcp_servers.tool_policies contains an invalid policy.")
        if access == "write" and risk_level in _MCP_READ_RISKS:
            raise ValueError(
                "mcp_servers write tools require a write-class risk level."
            )
        requires_connection = raw_policy.get("requires_connection", False)
        requires_resource = raw_policy.get("requires_resource", False)
        if type(requires_connection) is not bool or type(requires_resource) is not bool:
            raise ValueError(
                "mcp_servers tool policy flags must be booleans."
            )
        result[raw_name.strip()] = {
            "access": access,
            "risk_level": risk_level,
            "requires_connection": requires_connection,
            "requires_resource": requires_resource,
        }
    return result


def normalize_mcp_servers(value: object) -> list[dict[str, Any]]:
    """Validate the persistable, non-secret local stdio MCP configuration."""

    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MCP_MAX_SERVERS:
        raise ValueError(f"mcp_servers must contain at most {_MCP_MAX_SERVERS} entries.")
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("Each mcp_servers entry must be an object.")
        unknown = set(raw) - _MCP_SERVER_FIELDS
        if unknown:
            raise ValueError("mcp_servers contains unknown fields.")
        raw_id = raw.get("id")
        if not isinstance(raw_id, str):
            raise ValueError("mcp_servers.id is invalid.")
        server_id = raw_id.strip().casefold()
        if (
            not _MCP_ID_RE.fullmatch(server_id)
            or len(server_id) > 64
            or server_id in seen_ids
        ):
            raise ValueError("mcp_servers IDs must be unique safe identifiers.")
        seen_ids.add(server_id)

        raw_label = raw.get("label", server_id)
        if not isinstance(raw_label, str):
            raise ValueError("mcp_servers.label is invalid.")
        label = raw_label.strip()
        if (
            not label
            or len(label) > 80
            or any(ord(char) < 32 for char in label)
        ):
            raise ValueError("mcp_servers.label is invalid.")
        if raw.get("transport", "stdio") != "stdio":
            raise ValueError("mcp_servers supports only the local stdio transport.")

        executable = _mcp_local_path(
            raw.get("executable"),
            field="mcp_servers.executable",
        )
        executable_path = Path(executable)
        if (
            executable_path.suffix.casefold() in _MCP_SHELL_SUFFIXES
            or executable_path.name.casefold() in _MCP_SHELL_EXECUTABLES
        ):
            raise ValueError("mcp_servers executable cannot be a shell or script host.")
        executable_sha256 = raw.get("expected_executable_sha256")
        if executable_sha256 is not None and (
            not isinstance(executable_sha256, str)
            or not _MCP_SHA256_RE.fullmatch(executable_sha256)
        ):
            raise ValueError(
                "mcp_servers.expected_executable_sha256 must be a lowercase SHA-256."
            )

        argv = _mcp_string_array(
            raw.get("argv"),
            field="mcp_servers.argv",
            maximum_items=64,
            item_maximum=2048,
        )
        raw_roots = raw.get("allowed_cwd_roots")
        if not isinstance(raw_roots, list) or not raw_roots or len(raw_roots) > 16:
            raise ValueError(
                "mcp_servers.allowed_cwd_roots requires 1 to 16 local paths."
            )
        roots: list[str] = []
        seen_roots: set[str] = set()
        for root in raw_roots:
            normalized = _mcp_local_path(
                root,
                field="mcp_servers.allowed_cwd_roots",
            )
            identity = os.path.normcase(normalized)
            if identity not in seen_roots:
                roots.append(normalized)
                seen_roots.add(identity)
        cwd = raw.get("cwd")
        normalized_cwd = (
            _mcp_local_path(cwd, field="mcp_servers.cwd")
            if cwd is not None
            else None
        )
        if normalized_cwd is not None and not _mcp_path_within(normalized_cwd, roots):
            raise ValueError("mcp_servers.cwd must be inside an allowed cwd root.")

        environment_keys = _normalize_mcp_environment_keys(
            raw.get("environment_keys")
        )
        secret_aliases = _normalize_mcp_secret_aliases(raw.get("secret_aliases"))
        if set(environment_keys) & set(secret_aliases):
            raise ValueError(
                "mcp_servers environment keys cannot also be secret aliases."
            )
        tool_policies = _normalize_mcp_tool_policies(raw.get("tool_policies"))
        timeout = raw.get("timeout_seconds", 30)
        if type(timeout) not in {int, float}:
            raise ValueError("mcp_servers.timeout_seconds must be a number.")
        timeout_seconds = float(timeout)
        if not 30 <= timeout_seconds <= 60:
            raise ValueError(
                "mcp_servers.timeout_seconds must be between 30 and 60 seconds."
            )
        enabled = raw.get("enabled", False)
        if type(enabled) is not bool:
            raise ValueError("mcp_servers.enabled must be a boolean.")

        item: dict[str, Any] = {
            "id": server_id,
            "label": label,
            "transport": "stdio",
            "executable": executable,
            "argv": argv,
            "cwd": normalized_cwd,
            "allowed_cwd_roots": roots,
            "environment_keys": environment_keys,
            "secret_aliases": secret_aliases,
            "tool_policies": tool_policies,
            "timeout_seconds": timeout_seconds,
            "enabled": enabled,
        }
        if executable_sha256 is not None:
            item["expected_executable_sha256"] = executable_sha256
        result.append(item)
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
        settings = _known_settings(raw)
        try:
            settings["mcp_servers"] = normalize_mcp_servers(
                raw.get("mcp_servers")
            )
        except ValueError:
            # A tampered or legacy executable command must not be revived.
            # Keep the rest of the user's settings available and fail closed
            # only for the optional MCP process list.
            settings["mcp_servers"] = []
        return settings
    except (OSError, UnicodeError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)


def normalize_modal_size(data: Mapping[str, Any]) -> Dict[str, int]:
    try:
        width = int(data.get("settings_modal_width", 1040))
        height = int(data.get("settings_modal_height", 760))
    except (TypeError, ValueError) as exc:
        raise ValueError("Settings modal dimensions must be integers.") from exc
    return {
        "settings_modal_width": max(760, min(3840, width)),
        "settings_modal_height": max(540, min(2160, height)),
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
    merged["mcp_servers"] = normalize_mcp_servers(merged.get("mcp_servers"))
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
    persisted = _known_settings(data)
    # Enforce the same strict boundary even for internal callers that bypass
    # validate_settings (for example the small UI-state update endpoint).
    persisted["mcp_servers"] = normalize_mcp_servers(
        persisted.get("mcp_servers")
    )
    path.write_text(
        json.dumps(persisted, indent=2, ensure_ascii=False) + "\n",
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
