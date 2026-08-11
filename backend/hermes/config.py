"""Fail-closed configuration for a loopback-only Hermes sidecar."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlsplit

from .errors import HermesConfigurationError, HermesDisabledError


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _bounded_float(value: object, *, default: float, low: float, high: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    return max(low, min(high, result))


def validate_loopback_base_url(value: object) -> str:
    """Return a canonical base URL, rejecting remote or credentialed endpoints."""

    raw = str(value or "http://127.0.0.1:8642").strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise HermesConfigurationError("Hermes URL is invalid.") from exc

    if parsed.scheme not in {"http", "https"}:
        raise HermesConfigurationError("Hermes URL must use HTTP or HTTPS.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise HermesConfigurationError("Hermes URL must not contain credentials.")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise HermesConfigurationError("Hermes URL must not contain a path, query, or fragment.")
    if port is not None and not 1 <= port <= 65535:
        raise HermesConfigurationError("Hermes URL port is invalid.")

    hostname = parsed.hostname.casefold()
    if hostname != "localhost":
        try:
            if not ipaddress.ip_address(hostname).is_loopback:
                raise HermesConfigurationError("Hermes must use a loopback address.")
        except ValueError as exc:
            raise HermesConfigurationError("Hermes must use a loopback address.") from exc
    return raw


def validate_header_value(value: object, *, label: str, maximum: int = 256) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or _CONTROL.search(text):
        raise HermesConfigurationError(f"{label} is invalid.")
    return text


@dataclass(frozen=True)
class HermesConfig:
    """Runtime-only Hermes settings.

    The bearer secret is resolved from an environment variable rather than a
    persisted Workbench settings file. Disabled is the safe default.
    """

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8642"
    api_key: str = ""
    api_key_env: str = "HERMES_API_SERVER_KEY"
    default_model: str = "gemma4-hermes:latest"
    timeout_seconds: float = 15.0
    stream_read_timeout_seconds: float = 65.0
    max_response_bytes: int = 1_048_576

    @classmethod
    def from_mapping(
        cls,
        settings: Mapping[str, object],
        *,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "HermesConfig":
        env = os.environ if environ is None else environ
        enabled = _as_bool(settings.get("hermes_enabled", False))
        base_url = validate_loopback_base_url(
            settings.get("hermes_base_url", "http://127.0.0.1:8642")
        )
        api_key_env = str(
            settings.get("hermes_api_key_env") or "HERMES_API_SERVER_KEY"
        ).strip()
        if not _ENV_NAME.fullmatch(api_key_env):
            raise HermesConfigurationError("Hermes API key environment name is invalid.")
        api_key = str(env.get(api_key_env, ""))
        if api_key and (len(api_key) > 4096 or _CONTROL.search(api_key)):
            raise HermesConfigurationError("Hermes API key is invalid.")
        if enabled and len(api_key) < 16:
            raise HermesConfigurationError(
                "Hermes is enabled but its API key is missing or too short."
            )
        model = str(
            settings.get("hermes_model") or "gemma4-hermes:latest"
        ).strip()
        if not model or len(model) > 256 or _CONTROL.search(model):
            raise HermesConfigurationError("Hermes model identifier is invalid.")
        try:
            max_bytes = int(settings.get("hermes_max_response_bytes") or 1_048_576)
        except (TypeError, ValueError):
            max_bytes = 1_048_576
        return cls(
            enabled=enabled,
            base_url=base_url,
            api_key=api_key,
            api_key_env=api_key_env,
            default_model=model,
            timeout_seconds=_bounded_float(
                settings.get("hermes_timeout_seconds"),
                default=15.0,
                low=1.0,
                high=120.0,
            ),
            stream_read_timeout_seconds=_bounded_float(
                settings.get("hermes_stream_read_timeout_seconds"),
                default=65.0,
                low=5.0,
                high=600.0,
            ),
            max_response_bytes=max(65_536, min(8_388_608, max_bytes)),
        )

    def require_enabled(self) -> None:
        if not self.enabled:
            raise HermesDisabledError("Hermes routing is disabled.")
        if len(self.api_key) < 16:
            raise HermesConfigurationError("Hermes API key is not configured.")
