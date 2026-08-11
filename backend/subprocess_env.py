"""Minimal environment policy for Workbench-owned subprocesses."""

from __future__ import annotations

import os
from typing import Mapping, Optional


_SECRET_ENV_SUFFIXES = (
    "_API_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_KEY",
    "_CREDENTIALS",
    "_PAT",
    "_ACCESS_KEY",
    "_PRIVATE_KEY",
    "_CONNECTION_STRING",
)
_SECRET_ENV_NAMES = {
    "API_KEY",
    "AUTH_TOKEN",
    "BEARER_TOKEN",
    "DATABASE_URL",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "PASSWORD",
    "SECRET",
}
_ALLOWED_ENV_NAMES = {
    "APPDATA",
    "COMSPEC",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "VIRTUAL_ENV",
    "WINDIR",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
}
_ALLOWED_ENV_PREFIXES = ("PROCESSOR_",)


def is_secret_env_name(name: str) -> bool:
    normalized = str(name or "").strip().upper()
    return normalized in _SECRET_ENV_NAMES or normalized.endswith(_SECRET_ENV_SUFFIXES)


def is_allowed_subprocess_env_name(name: str) -> bool:
    normalized = str(name or "").strip().upper()
    return normalized in _ALLOWED_ENV_NAMES or normalized.startswith(
        _ALLOWED_ENV_PREFIXES
    )


def agent_subprocess_env(extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Return only operational variables required by local child processes.

    This is an allowlist rather than a credential-name blacklist: unknown
    variables are denied even when their names do not look secret. Explicit
    extras pass through the same allowlist, preventing callers from
    re-introducing credentials.
    """

    environment = {
        key: value
        for key, value in os.environ.items()
        if is_allowed_subprocess_env_name(key) and not is_secret_env_name(key)
    }
    for key, value in dict(extra or {}).items():
        if is_allowed_subprocess_env_name(key) and not is_secret_env_name(key):
            environment[str(key)] = str(value)
    return environment
