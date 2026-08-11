"""Windows DPAPI-backed storage for model provider credentials.

The public settings file stores provider metadata only. Credential ciphertext
is kept under the ignored runtime directory and can be decrypted only by the
same Windows user account on the same machine.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any, Iterable, Optional

from paths import RUNTIME_ROOT


_LOCK = threading.RLock()
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _store_path() -> Path:
    override = os.environ.get("WORKBENCH_SECRET_STORE_PATH")
    return Path(override).expanduser().resolve() if override else RUNTIME_ROOT / "secrets" / "model-providers.json"


def normalize_provider_id(provider_id: str) -> str:
    normalized = str(provider_id or "").strip().casefold()
    if not _PROVIDER_ID.fullmatch(normalized):
        raise ValueError("Provider ID must start with a letter and contain only a-z, 0-9, _ or -.")
    return normalized


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return (
        _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))),
        buffer,
    )


def _protect(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Model provider secret storage requires Windows DPAPI.")
    source, source_buffer = _blob(value.encode("utf-8"))
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "Local AI Workbench provider secret",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(output.pbData, output.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        kernel32.LocalFree(output.pbData)
        del source_buffer


def _unprotect(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Model provider secret storage requires Windows DPAPI.")
    encrypted = base64.b64decode(value.encode("ascii"), validate=True)
    source, source_buffer = _blob(encrypted)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(output.pbData)
        del source_buffer


def _read() -> dict[str, dict[str, Any]]:
    path = _store_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write(payload: dict[str, dict[str, Any]]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def set_provider_secret(provider_id: str, secret: str) -> dict[str, Any]:
    normalized = normalize_provider_id(provider_id)
    plain = str(secret or "").strip()
    if not plain:
        raise ValueError("API key cannot be empty.")
    if len(plain) > 16_384:
        raise ValueError("API key is too long.")
    with _LOCK:
        payload = _read()
        payload[normalized] = {
            "ciphertext": _protect(plain),
            "last4": plain[-4:] if len(plain) >= 4 else plain,
        }
        _write(payload)
    return {"provider_id": normalized, "configured": True, "last4": payload[normalized]["last4"]}


def get_provider_secret(provider_id: str) -> str:
    normalized = normalize_provider_id(provider_id)
    with _LOCK:
        record = _read().get(normalized)
    if not record or not record.get("ciphertext"):
        return ""
    return _unprotect(str(record["ciphertext"]))


def delete_provider_secret(provider_id: str) -> bool:
    normalized = normalize_provider_id(provider_id)
    with _LOCK:
        payload = _read()
        existed = normalized in payload
        payload.pop(normalized, None)
        if existed:
            _write(payload)
    return existed


def provider_secret_statuses(provider_ids: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    with _LOCK:
        payload = _read()
    requested = (
        [normalize_provider_id(item) for item in provider_ids]
        if provider_ids is not None
        else sorted(payload)
    )
    return [
        {
            "provider_id": provider_id,
            "configured": bool(payload.get(provider_id, {}).get("ciphertext")),
            "last4": str(payload.get(provider_id, {}).get("last4") or ""),
        }
        for provider_id in requested
    ]
