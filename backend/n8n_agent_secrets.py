"""DPAPI-protected secrets used only by the Workbench n8n governance broker."""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any

from paths import RUNTIME_ROOT
from secret_store import _protect, _unprotect


_LOCK = threading.RLock()
_VERSION = 1


class N8nAgentSecretError(RuntimeError):
    pass


class N8nAgentSecretStore:
    """Keep the n8n API key and encryption key outside settings and SQLite."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or (RUNTIME_ROOT / "secrets" / "n8n-agent.json")).resolve()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": _VERSION, "values": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise N8nAgentSecretError("The n8n agent secret store is invalid.") from exc
        if value.get("version") != _VERSION or not isinstance(value.get("values"), dict):
            raise N8nAgentSecretError("The n8n agent secret store version is unsupported.")
        return value

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    def content_key(self) -> bytes:
        with _LOCK:
            payload = self._read()
            envelope = payload["values"].get("content_key")
            if not envelope:
                raw = secrets.token_bytes(32)
                payload["values"]["content_key"] = _protect(base64.b64encode(raw).decode("ascii"))
                self._write(payload)
                return raw
            try:
                raw = base64.b64decode(_unprotect(str(envelope)), validate=True)
            except Exception as exc:
                raise N8nAgentSecretError("The n8n agent encryption key is unavailable.") from exc
            if len(raw) != 32:
                raise N8nAgentSecretError("The n8n agent encryption key is invalid.")
            return raw

    def set_api_key(self, api_key: str) -> None:
        secret = str(api_key or "").strip()
        if len(secret) < 16 or len(secret) > 16_384 or any(ord(ch) < 32 for ch in secret):
            raise N8nAgentSecretError("The n8n API key is invalid.")
        with _LOCK:
            payload = self._read()
            payload["values"]["api_key"] = _protect(secret)
            self._write(payload)

    def api_key(self) -> str:
        with _LOCK:
            envelope = self._read()["values"].get("api_key")
            if not envelope:
                raise N8nAgentSecretError("The n8n API key is not configured.")
            return _unprotect(str(envelope))

    def api_key_configured(self) -> bool:
        try:
            return bool(self.api_key())
        except Exception:
            return False


__all__ = ["N8nAgentSecretError", "N8nAgentSecretStore"]
