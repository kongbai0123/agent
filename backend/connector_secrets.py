"""DPAPI-protected secret envelopes for local OAuth connectors.

Only opaque ciphertext is persisted.  Public OAuth profile metadata and
connection state belong in SQLite; client secrets, token sets and temporary
PKCE verifiers are kept here so they never cross an API response boundary.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from paths import RUNTIME_ROOT
from secret_store import _protect, _unprotect


_VERSION = 1
_LOCK = threading.RLock()
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")


class ConnectorSecretError(RuntimeError):
    """Raised when the connector vault is unsafe, corrupt or unavailable."""


def _absolute_without_following_links(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _default_store_path() -> Path:
    override = os.environ.get("WORKBENCH_CONNECTOR_SECRET_STORE_PATH")
    candidate = Path(override) if override else RUNTIME_ROOT / "secrets" / "connectors.json"
    return _absolute_without_following_links(candidate)


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
    current = _absolute_without_following_links(path)
    while True:
        if current.exists() or current.is_symlink():
            if _is_reparse(current):
                raise ConnectorSecretError(
                    "The connector secret path must not contain links or reparse points."
                )
        if current.parent == current:
            return
        current = current.parent


def _safe_identifier(value: str, label: str) -> str:
    candidate = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(candidate):
        raise ConnectorSecretError(f"{label} is invalid.")
    return candidate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConnectorSecretStore:
    """Small atomic vault with injectable crypto for deterministic tests."""

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        protect: Callable[[str], str] = _protect,
        unprotect: Callable[[str], str] = _unprotect,
    ) -> None:
        self.path = _absolute_without_following_links(path or _default_store_path())
        self._protect = protect
        self._unprotect = unprotect

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": _VERSION, "records": {}}
        _assert_safe_chain(self.path)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConnectorSecretError("The connector secret store is invalid.") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != _VERSION
            or not isinstance(payload.get("records"), dict)
        ):
            raise ConnectorSecretError("The connector secret store has an unsupported format.")
        return payload

    def _write(self, payload: Mapping[str, Any]) -> None:
        _assert_safe_chain(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_chain(self.path.parent)
        temporary = self.path.with_name(
            f".{self.path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
        )
        if temporary.exists() or temporary.is_symlink():
            if _is_reparse(temporary):
                raise ConnectorSecretError("The temporary connector secret path is unsafe.")
            temporary.unlink()
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            _assert_safe_chain(self.path.parent)
            if self.path.exists() and _is_reparse(self.path):
                raise ConnectorSecretError("The connector secret destination is unsafe.")
            os.replace(temporary, self.path)
        finally:
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()

    @staticmethod
    def _record_key(kind: str, record_id: str) -> str:
        return (
            f"{_safe_identifier(kind, 'Secret kind')}:"
            f"{_safe_identifier(record_id, 'Secret record ID')}"
        )

    @staticmethod
    def _plain_payload(values: Mapping[str, Any]) -> str:
        if not isinstance(values, Mapping) or not values:
            raise ConnectorSecretError("A non-empty secret mapping is required.")
        normalized: dict[str, str] = {}
        for key, value in values.items():
            safe_key = _safe_identifier(str(key), "Secret field")
            text = str(value or "")
            if not text or len(text) > 131_072:
                raise ConnectorSecretError(f"Secret field {safe_key} is empty or too long.")
            normalized[safe_key] = text
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 262_144:
            raise ConnectorSecretError("The connector secret envelope is too large.")
        return encoded

    def set(self, kind: str, record_id: str, values: Mapping[str, Any]) -> None:
        key = self._record_key(kind, record_id)
        plaintext = self._plain_payload(values)
        try:
            ciphertext = self._protect(plaintext)
        except Exception as exc:
            raise ConnectorSecretError("The connector secret could not be encrypted.") from exc
        with _LOCK:
            payload = self._read()
            payload["records"][key] = {"ciphertext": ciphertext, "updated_at": _now()}
            self._write(payload)

    def get(self, kind: str, record_id: str) -> dict[str, str]:
        key = self._record_key(kind, record_id)
        with _LOCK:
            envelope = self._read()["records"].get(key)
        if not isinstance(envelope, Mapping) or not envelope.get("ciphertext"):
            return {}
        try:
            decoded = json.loads(self._unprotect(str(envelope["ciphertext"])))
        except Exception as exc:
            raise ConnectorSecretError("The connector secret could not be decrypted.") from exc
        if not isinstance(decoded, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()
        ):
            raise ConnectorSecretError("The connector secret envelope is invalid.")
        return decoded

    def exists(self, kind: str, record_id: str) -> bool:
        key = self._record_key(kind, record_id)
        with _LOCK:
            envelope = self._read()["records"].get(key)
        return bool(isinstance(envelope, Mapping) and envelope.get("ciphertext"))

    def delete(self, kind: str, record_id: str) -> bool:
        key = self._record_key(kind, record_id)
        with _LOCK:
            payload = self._read()
            existed = key in payload["records"]
            payload["records"].pop(key, None)
            if existed:
                self._write(payload)
        return existed

    def delete_record(self, record_id: str) -> int:
        """Delete every secret kind associated with a flow/profile/connection ID."""

        safe_id = _safe_identifier(record_id, "Secret record ID")
        suffix = f":{safe_id}"
        with _LOCK:
            payload = self._read()
            matches = [key for key in payload["records"] if key.endswith(suffix)]
            for key in matches:
                payload["records"].pop(key, None)
            if matches:
                self._write(payload)
        return len(matches)


__all__ = ["ConnectorSecretError", "ConnectorSecretStore"]
