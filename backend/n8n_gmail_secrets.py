"""DPAPI-protected keys for the local n8n Gmail bridge.

The Gmail bridge deliberately uses three independent 256-bit values:

* an AES-256-GCM content key for private mail data;
* an inbound HMAC key used by n8n when calling Workbench; and
* an outbound webhook key used by Workbench when waking the send workflow.

Only DPAPI ciphertext is written to disk.  The values are never returned by an
HTTP status endpoint and are decryptable only by the same Windows account on
the same machine.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import stat
import threading
from pathlib import Path
from typing import Any

from paths import RUNTIME_ROOT
from secret_store import _protect, _unprotect


_LOCK = threading.RLock()
_VERSION = 1
_KEY_NAMES = ("content_key", "inbound_hmac_key", "outbound_webhook_key")


class N8nGmailSecretError(RuntimeError):
    """Raised when the private integration key store is unsafe or unreadable."""


def _store_path() -> Path:
    override = os.environ.get("WORKBENCH_N8N_GMAIL_SECRET_STORE_PATH")
    return (
        Path(override).expanduser().resolve()
        if override
        else (RUNTIME_ROOT / "secrets" / "n8n-gmail.json").resolve()
    )


def _is_reparse(path: Path) -> bool:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return False
    return bool(
        stat.S_ISLNK(value.st_mode)
        or getattr(value, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _assert_safe_chain(path: Path) -> None:
    """Reject links/reparse points in the existing destination chain."""

    current = path
    existing: list[Path] = []
    while True:
        if current.exists() or current.is_symlink():
            existing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for item in existing:
        if _is_reparse(item):
            raise N8nGmailSecretError("The n8n Gmail secret path is not a direct filesystem path.")


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": _VERSION, "keys": {}}
    _assert_safe_chain(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise N8nGmailSecretError("The n8n Gmail secret store is invalid.") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != _VERSION
        or not isinstance(payload.get("keys"), dict)
    ):
        raise N8nGmailSecretError("The n8n Gmail secret store has an unsupported format.")
    return payload


def _write(path: Path, payload: dict[str, Any]) -> None:
    _assert_safe_chain(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        if _is_reparse(temporary):
            raise N8nGmailSecretError("The n8n Gmail temporary secret path is unsafe.")
        temporary.unlink()
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


class N8nGmailSecretStore:
    """Lazy, DPAPI-backed provider for the Gmail integration keys."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or _store_path()).expanduser().resolve()

    def _key(self, name: str) -> bytes:
        if name not in _KEY_NAMES:
            raise N8nGmailSecretError("Unknown n8n Gmail secret name.")
        with _LOCK:
            payload = _read(self.path)
            keys = payload["keys"]
            envelope = keys.get(name)
            if not envelope:
                raw = secrets.token_bytes(32)
                encoded = base64.urlsafe_b64encode(raw).decode("ascii")
                keys[name] = _protect(encoded)
                _write(self.path, payload)
                return raw
            try:
                raw = base64.b64decode(
                    _unprotect(str(envelope)).encode("ascii"),
                    altchars=b"-_",
                    validate=True,
                )
            except Exception as exc:
                raise N8nGmailSecretError("The n8n Gmail secret cannot be decrypted.") from exc
            if len(raw) != 32:
                raise N8nGmailSecretError("The n8n Gmail secret has an invalid length.")
            return raw

    def content_key(self) -> bytes:
        return self._key("content_key")

    def inbound_hmac_key(self) -> bytes:
        return self._key("inbound_hmac_key")

    def inbound_hmac_credential_value(self) -> str:
        """Return the stable ASCII value provisioned into n8n's Crypto credential.

        The n8n Crypto node treats ``hmacSecret`` as UTF-8 text and does not
        decode hex or base64.  Keep the DPAPI-protected 256-bit key as the
        source of truth, but expose its base64url representation only to the
        local provisioning path and use those exact ASCII bytes for HMAC
        verification in Workbench.
        """

        return base64.urlsafe_b64encode(self.inbound_hmac_key()).decode("ascii")

    def inbound_hmac_verifier_key(self) -> bytes:
        """Return the exact UTF-8 bytes used by n8n's Crypto HMAC node."""

        return self.inbound_hmac_credential_value().encode("ascii")

    def outbound_webhook_key(self) -> bytes:
        return self._key("outbound_webhook_key")

    def status(self) -> dict[str, Any]:
        """Return key availability without exposing values or ciphertext."""

        try:
            for name in _KEY_NAMES:
                self._key(name)
            return {"available": True, "provider": "windows_dpapi", "key_count": 3}
        except Exception:
            return {"available": False, "provider": "windows_dpapi", "key_count": 0}


__all__ = ["N8nGmailSecretError", "N8nGmailSecretStore"]
