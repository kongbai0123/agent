"""Fail-closed lifecycle policy for the Workbench-managed n8n service.

This module intentionally has no FastAPI or database dependency.  The app
router may call the small public surface at the bottom of the file, while the
Workbench launcher may own one :class:`ManagedN8nLifecycle` instance.  All
writable n8n paths are derived from ``WORKBENCH_RUNTIME_DIR`` (``D:\\llm\\runtime``
for the supported desktop deployment); no n8n command is ever launched with
the user's Windows profile as its home directory.

The ownership rules are deliberately strict.  A process is managed only when
its PID, creation time, executable, exact command line, version and listener
all match the atomically-written lifecycle record.  Merely finding ``node`` or
an n8n health endpoint on port 5678 is never sufficient authority to stop it.
"""

from __future__ import annotations

import copy
import base64
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

import psutil
import requests

from paths import REPO_ROOT, RUNTIME_ROOT
from subprocess_env import agent_subprocess_env


N8N_VERSION = "2.32.5"
NODE_VERSION = "24.15.0"
N8N_HOST = "127.0.0.1"
N8N_PORT = 5678
N8N_BASE_URL = f"http://{N8N_HOST}:{N8N_PORT}"
N8N_SERVICE_ACCOUNT = "WorkbenchN8n"
WORKBENCH_INTEGRATION_BASE_URL = (
    "http://127.0.0.1:8000/api/integrations/n8n/v1"
)

WORKBENCH_HMAC_CREDENTIAL_PLACEHOLDER = "__WORKBENCH_HMAC_CREDENTIAL_ID__"
WORKBENCH_WEBHOOK_CREDENTIAL_PLACEHOLDER = "__WORKBENCH_WEBHOOK_CREDENTIAL_ID__"
GMAIL_CREDENTIAL_PLACEHOLDER = "__GMAIL_CREDENTIAL_ID__"

DISABLED_NODES = (
    "n8n-nodes-base.code",
    "n8n-nodes-base.executeCommand",
    "n8n-nodes-base.localFileTrigger",
    "n8n-nodes-base.readWriteFile",
)

ALLOWED_TEMPLATE_NODE_TYPES = frozenset(
    {
        "n8n-nodes-base.crypto",
        "n8n-nodes-base.gmail",
        "n8n-nodes-base.httpRequest",
        "n8n-nodes-base.if",
        "n8n-nodes-base.respondToWebhook",
        "n8n-nodes-base.scheduleTrigger",
        "n8n-nodes-base.set",
        "n8n-nodes-base.webhook",
    }
)

AGENT_BRIDGE_TEMPLATE_ID = "workbench-agent-bridge-v1"
AGENT_BRIDGE_WORKFLOW_NAME = "Workbench Agent Bridge v1"
APPROVAL_GATE_TEMPLATE_ID = "workbench-approval-gate-v1"
APPROVAL_GATE_WORKFLOW_NAME = "Workbench Approval Gate v1"
AGENT_RUNTIME_PROFILE = "agent-runtime"

AGENT_TASK_SUBMIT_URL = (
    f"{WORKBENCH_INTEGRATION_BASE_URL}/agent/tasks"
)
AGENT_TASK_STATUS_URL = (
    "=http://127.0.0.1:8000/api/integrations/n8n/v1/agent/tasks/"
    "{{$('Submit Agent Task').item.json.body.task_id}}/status"
)
RUNTIME_APPROVAL_SUBMIT_URL = (
    f"{WORKBENCH_INTEGRATION_BASE_URL}/agent/runtime-actions"
)
RUNTIME_APPROVAL_STATUS_URL = (
    "=http://127.0.0.1:8000/api/integrations/n8n/v1/agent/runtime-actions/"
    "{{$('Request Runtime Approval').item.json.body.approval_id}}/status"
)

AGENT_TEMPLATE_NODE_TYPES = frozenset(
    {
        "n8n-nodes-base.crypto",
        "n8n-nodes-base.executeWorkflowTrigger",
        "n8n-nodes-base.httpRequest",
        "n8n-nodes-base.if",
        "n8n-nodes-base.set",
        "n8n-nodes-base.wait",
    }
)

GMAIL_INBOUND_URL = f"{WORKBENCH_INTEGRATION_BASE_URL}/gmail/events"
GMAIL_SEND_WEBHOOK_PATH = "workbench-gmail-send-v1"
WORKBENCH_WORKFLOW_KEY_PLACEHOLDER = "__WORKBENCH_WORKFLOW_KEY__"
GMAIL_SEND_CLAIM_URL = (
    "=http://127.0.0.1:8000/api/integrations/n8n/v1/gmail/deliveries/"
    "{{$json.delivery_id}}/claim"
)
GMAIL_SEND_RESULT_URL = (
    "=http://127.0.0.1:8000/api/integrations/n8n/v1/gmail/deliveries/"
    "{{$json.delivery_id}}/result"
)
GMAIL_DRAFT_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts/send"
ALLOWED_TEMPLATE_HTTP_URLS = frozenset(
    {
        GMAIL_INBOUND_URL,
        GMAIL_SEND_CLAIM_URL,
        GMAIL_SEND_RESULT_URL,
        GMAIL_DRAFT_SEND_URL,
    }
)

_STATE_SCHEMA_VERSION = 1
_MAX_STATE_BYTES = 64 * 1024
_MAX_TEMPLATE_BYTES = 1024 * 1024
_CREDENTIAL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_WINDOWS_REPARSE_POINT = 0x400
_LOCK = threading.RLock()
_AUTO_LAUNCHER = object()
_LAUNCH_CREDENTIAL_FILE = "n8n-service-account.dpapi.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class N8nLifecycleError(RuntimeError):
    """Base class with a stable machine-readable reason code."""

    code = "N8N_LIFECYCLE_ERROR"

    def __init__(self, message: str, *, details: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.details = dict(details or {})


class N8nConfigurationError(N8nLifecycleError):
    code = "N8N_CONFIGURATION_INVALID"


class N8nPortConflict(N8nLifecycleError):
    code = "N8N_PORT_CONFLICT"


class N8nOwnershipError(N8nLifecycleError):
    code = "N8N_OWNERSHIP_UNVERIFIED"


class N8nStartupError(N8nLifecycleError):
    code = "N8N_START_FAILED"


class N8nTemplateError(ValueError):
    """A workflow template violates the reviewed v1 policy."""


@dataclass(frozen=True)
class ManagedN8nPaths:
    runtime_root: Path
    tool_dir: Path
    node_executable: Path
    n8n_entry: Path
    data_home: Path
    n8n_dir: Path
    file_exchange_dir: Path
    managed_dir: Path
    state_path: Path
    lock_path: Path
    log_dir: Path
    temp_dir: Path
    npm_cache_dir: Path
    profile_home: Path
    secrets_dir: Path
    launch_credential_path: Path

    @classmethod
    def from_runtime_root(
        cls,
        runtime_root: Path | str,
        *,
        node_executable: Path | str | None = None,
    ) -> "ManagedN8nPaths":
        root = Path(runtime_root).expanduser().resolve()
        tool_dir = root / "tools" / "n8n"
        preferred_node = root / "tools" / "node" / NODE_VERSION / "node.exe"
        detected_node = shutil.which("node")
        node = Path(
            node_executable
            or (preferred_node if preferred_node.is_file() else detected_node or preferred_node)
        ).expanduser().resolve()
        data_home = root / "n8n-data"
        managed_dir = root / "n8n-managed"
        return cls(
            runtime_root=root,
            tool_dir=tool_dir,
            node_executable=node,
            n8n_entry=tool_dir / "node_modules" / "n8n" / "bin" / "n8n",
            data_home=data_home,
            n8n_dir=data_home / ".n8n",
            file_exchange_dir=data_home / ".n8n-files",
            managed_dir=managed_dir,
            state_path=managed_dir / "lifecycle.json",
            lock_path=managed_dir / "lifecycle.lock",
            log_dir=root / "logs" / "n8n",
            temp_dir=root / "temp" / "n8n",
            npm_cache_dir=root / "cache" / "npm",
            profile_home=managed_dir / "profile",
            secrets_dir=root / "secrets",
            launch_credential_path=root / "secrets" / _LAUNCH_CREDENTIAL_FILE,
        )

    @classmethod
    def default(cls) -> "ManagedN8nPaths":
        return cls.from_runtime_root(RUNTIME_ROOT)

    def writable_paths(self) -> tuple[Path, ...]:
        return (
            self.data_home,
            self.file_exchange_dir,
            self.managed_dir,
            self.log_dir,
            self.temp_dir,
            self.npm_cache_dir,
            self.profile_home,
        )


@dataclass(frozen=True)
class LifecycleRecord:
    schema_version: int
    owner: str
    owner_id: str
    pid: int
    process_created_at: float
    started_at: str
    version: str
    node_version: str
    node_executable: str
    n8n_entry: str
    command_sha256: str
    host: str
    port: int


@dataclass(frozen=True)
class PortInspection:
    state: str
    pids: tuple[int, ...]
    reason: str


@dataclass(frozen=True)
class HealthProbe:
    healthy: bool
    status_code: Optional[int]
    reason: str
    elapsed_ms: float


class WindowsRunAsProcess:
    """Small subprocess-compatible owner for a CreateProcessWithLogonW handle.

    Only the process ID, exit status and control handle are retained.  The
    password and complete environment are never stored on this object.
    """

    def __init__(self, *, pid: int, process_handle: int, kernel32: Any) -> None:
        self.pid = int(pid)
        self._handle = int(process_handle)
        self._kernel32 = kernel32
        self.returncode: Optional[int] = None

    def poll(self) -> Optional[int]:
        if self.returncode is not None:
            return self.returncode
        import ctypes
        from ctypes import wintypes

        exit_code = wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(
            wintypes.HANDLE(self._handle), ctypes.byref(exit_code)
        ):
            raise OSError(ctypes.get_last_error(), "GetExitCodeProcess failed")
        if int(exit_code.value) == 259:  # STILL_ACTIVE
            return None
        self.returncode = int(exit_code.value)
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        import ctypes
        from ctypes import wintypes

        milliseconds = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        outcome = int(
            self._kernel32.WaitForSingleObject(
                wintypes.HANDLE(self._handle), wintypes.DWORD(milliseconds)
            )
        )
        if outcome == 0x00000102:  # WAIT_TIMEOUT
            raise subprocess.TimeoutExpired("managed-n8n", timeout)
        if outcome != 0:  # WAIT_OBJECT_0
            raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
        value = self.poll()
        if value is None:
            raise OSError("Managed n8n signalled without an exit code")
        return value

    def terminate(self) -> None:
        import ctypes
        from ctypes import wintypes

        if self.poll() is None and not self._kernel32.TerminateProcess(
            wintypes.HANDLE(self._handle), 1
        ):
            raise OSError(ctypes.get_last_error(), "TerminateProcess failed")

    def close(self) -> None:
        if self._handle:
            from ctypes import wintypes

            self._kernel32.CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = 0

    def __del__(self) -> None:  # pragma: no cover - best-effort handle hygiene
        try:
            self.close()
        except Exception:
            pass


class _WindowsRunAsApi:
    """Reviewed, narrow Win32 adapter; imported lazily on Windows only."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise N8nConfigurationError("The n8n run-as launcher requires Windows.")
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        # ctypes otherwise assumes c_int parameters/results.  On 64-bit
        # Windows that truncates HANDLE values (including the current-process
        # pseudo handle) and makes DuplicateHandle fail with ERROR_INVALID_HANDLE.
        self.kernel32.GetCurrentProcess.argtypes = []
        self.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel32.DuplicateHandle.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self.kernel32.DuplicateHandle.restype = wintypes.BOOL

    def unprotect(self, ciphertext: bytes) -> bytearray:
        ctypes = self.ctypes
        wintypes = self.wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        source_buffer = (ctypes.c_ubyte * len(ciphertext)).from_buffer_copy(ciphertext)
        source = DATA_BLOB(len(ciphertext), source_buffer)
        output = DATA_BLOB()
        self.crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p,
            ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(DATA_BLOB),
        ]
        self.crypt32.CryptUnprotectData.restype = wintypes.BOOL
        if not self.crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(output)
        ):
            raise N8nConfigurationError(
                "The protected n8n launch credential cannot be decrypted.",
                details={"blockers": ["launch_credential_unreadable"]},
            )
        try:
            if output.cbData < 32 or output.cbData > 1024:
                raise N8nConfigurationError(
                    "The protected n8n launch credential is invalid.",
                    details={"blockers": ["launch_credential_invalid"]},
                )
            return bytearray(ctypes.string_at(output.pbData, output.cbData))
        finally:
            if output.pbData:
                ctypes.memset(output.pbData, 0, output.cbData)
                self.kernel32.LocalFree(output.pbData)

    def _password_buffer(self, password_utf8: bytearray) -> Any:
        ctypes = self.ctypes
        source = (ctypes.c_char * len(password_utf8)).from_buffer(password_utf8)
        required = self.kernel32.MultiByteToWideChar(
            65001, 0x8, source, len(password_utf8), None, 0
        )
        if required <= 0:
            raise N8nConfigurationError(
                "The protected n8n launch credential is invalid.",
                details={"blockers": ["launch_credential_invalid"]},
            )
        password = ctypes.create_unicode_buffer(required + 1)
        if self.kernel32.MultiByteToWideChar(
            65001, 0x8, source, len(password_utf8), password, required
        ) != required:
            ctypes.memset(password, 0, ctypes.sizeof(password))
            raise N8nConfigurationError(
                "The protected n8n launch credential is invalid.",
                details={"blockers": ["launch_credential_invalid"]},
            )
        return password

    def spawn(
        self,
        *,
        username: str,
        domain: str,
        password_utf8: bytearray,
        command: Sequence[str],
        cwd: str,
        env: Mapping[str, str],
        stdin: Any,
        stdout: Any,
        stderr: Any,
        creationflags: int,
    ) -> WindowsRunAsProcess:
        ctypes = self.ctypes
        wintypes = self.wintypes

        class STARTUPINFO(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE),
            ]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
            ]

        import msvcrt

        opened_stdin = None
        duplicates: list[int] = []

        def duplicate_stream(stream: Any, *, input_stream: bool = False) -> int:
            nonlocal opened_stdin
            if stream == subprocess.DEVNULL:
                opened_stdin = open(os.devnull, "rb" if input_stream else "ab")
                stream = opened_stdin
            if stream is None or not hasattr(stream, "fileno"):
                raise N8nConfigurationError("The isolated launcher requires explicit log handles.")
            source_handle = msvcrt.get_osfhandle(stream.fileno())
            duplicate = wintypes.HANDLE()
            current = self.kernel32.GetCurrentProcess()
            if not self.kernel32.DuplicateHandle(
                current,
                wintypes.HANDLE(source_handle),
                current,
                ctypes.byref(duplicate),
                0,
                True,
                0x2,
            ):
                raise OSError(ctypes.get_last_error(), "DuplicateHandle failed")
            value = int(duplicate.value)
            duplicates.append(value)
            return value

        password = self._password_buffer(password_utf8)
        try:
            startup = STARTUPINFO()
            startup.cb = ctypes.sizeof(STARTUPINFO)
            startup.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
            startup.hStdInput = duplicate_stream(stdin, input_stream=True)
            startup.hStdOutput = duplicate_stream(stdout)
            startup.hStdError = duplicate_stream(stderr)
            process_info = PROCESS_INFORMATION()
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(command)))
            environment_entries: list[str] = []
            for key, value in sorted(env.items(), key=lambda item: item[0].casefold()):
                if not key or "=" in key or "\0" in key or "\0" in str(value):
                    raise N8nConfigurationError("The isolated n8n environment is invalid.")
                environment_entries.append(f"{key}={value}")
            environment = ctypes.create_unicode_buffer("\0".join(environment_entries) + "\0\0")
            self.advapi32.CreateProcessWithLogonW.argtypes = [
                wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
                wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFO), ctypes.POINTER(PROCESS_INFORMATION),
            ]
            self.advapi32.CreateProcessWithLogonW.restype = wintypes.BOOL
            flags = int(creationflags) | 0x00000400  # CREATE_UNICODE_ENVIRONMENT
            if not self.advapi32.CreateProcessWithLogonW(
                username,
                domain,
                password,
                0,  # do not load a C-drive user profile; all homes are explicit
                str(command[0]),
                command_line,
                flags,
                environment,
                cwd,
                ctypes.byref(startup),
                ctypes.byref(process_info),
            ):
                raise OSError(ctypes.get_last_error(), "CreateProcessWithLogonW failed")
            self.kernel32.CloseHandle(process_info.hThread)
            return WindowsRunAsProcess(
                pid=int(process_info.dwProcessId),
                process_handle=int(process_info.hProcess),
                kernel32=self.kernel32,
            )
        finally:
            ctypes.memset(password, 0, ctypes.sizeof(password))
            for handle in duplicates:
                self.kernel32.CloseHandle(wintypes.HANDLE(handle))
            if opened_stdin is not None:
                opened_stdin.close()


class WindowsRunAsLauncher:
    """Callable launcher restricted to the pinned Node+n8n command."""

    def __init__(
        self,
        paths: ManagedN8nPaths,
        *,
        api: Optional[Any] = None,
    ) -> None:
        self.paths = paths
        self._api = api

    def _read_credential(self) -> tuple[str, str, bytes]:
        path = self.paths.launch_credential_path
        if not _safe_regular_file(path, self.paths.secrets_dir, max_bytes=32 * 1024):
            raise N8nConfigurationError(
                "The protected n8n launch credential is missing or unsafe.",
                details={"blockers": ["launch_credential_missing"]},
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            required = {"schema_version", "account", "ciphertext", "created_at"}
            if not isinstance(payload, dict) or set(payload) != required:
                raise ValueError("invalid credential shape")
            if int(payload["schema_version"]) != 1:
                raise ValueError("invalid credential version")
            account = str(payload["account"])
            domain, separator, username = account.partition("\\")
            local_names = {
                socket.gethostname().casefold(),
                str(os.environ.get("COMPUTERNAME") or "").casefold(),
            }
            local_names.discard("")
            if (
                not separator
                or username.casefold() != N8N_SERVICE_ACCOUNT.casefold()
                or domain.casefold() not in local_names
                or not isinstance(payload["created_at"], str)
            ):
                raise ValueError("credential account mismatch")
            ciphertext = base64.b64decode(
                str(payload["ciphertext"]), validate=True
            )
            if not 32 <= len(ciphertext) <= 16 * 1024:
                raise ValueError("invalid credential ciphertext")
            return domain, username, ciphertext
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise N8nConfigurationError(
                "The protected n8n launch credential is invalid.",
                details={"blockers": ["launch_credential_invalid"]},
            ) from exc

    def __call__(self, **options: Any) -> Any:
        if options.get("shell") is not False:
            raise N8nConfigurationError("The isolated n8n launcher forbids shell execution.")
        command = tuple(map(str, options.get("command") or ()))
        if tuple(map(_canonical_command_item, command)) != tuple(
            map(_canonical_command_item, _command(self.paths))
        ):
            raise N8nConfigurationError("The isolated launcher accepts only pinned n8n.")
        if _canonical(str(options.get("cwd") or "")) != _canonical(self.paths.tool_dir):
            raise N8nConfigurationError("The isolated n8n working directory is invalid.")
        environment = options.get("env")
        if not isinstance(environment, Mapping):
            raise N8nConfigurationError("The isolated n8n environment is missing.")
        domain, username, ciphertext = self._read_credential()
        api = self._api or _WindowsRunAsApi()
        password = api.unprotect(ciphertext)
        if not isinstance(password, bytearray) or not 32 <= len(password) <= 1024:
            if isinstance(password, bytearray):
                password[:] = b"\0" * len(password)
            raise N8nConfigurationError(
                "The protected n8n launch credential is invalid.",
                details={"blockers": ["launch_credential_invalid"]},
            )
        try:
            return api.spawn(
                username=username,
                domain=domain,
                password_utf8=password,
                command=command,
                cwd=str(self.paths.tool_dir),
                env={str(key): str(value) for key, value in environment.items()},
                stdin=options.get("stdin"),
                stdout=options.get("stdout"),
                stderr=options.get("stderr"),
                creationflags=int(options.get("creationflags") or 0),
            )
        finally:
            password[:] = b"\0" * len(password)


def _canonical(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _is_reparse_point(path: Path) -> bool:
    try:
        stat = path.lstat()
    except OSError:
        return False
    return path.is_symlink() or bool(
        getattr(stat, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


def _safe_regular_file(path: Path, root: Path, *, max_bytes: int) -> bool:
    if not _is_relative_to(path, root) or _is_reparse_point(path):
        return False
    try:
        return path.is_file() and path.stat().st_size <= max_bytes
    except OSError:
        return False


def _command(paths: ManagedN8nPaths) -> tuple[str, ...]:
    return (
        str(paths.node_executable),
        str(paths.n8n_entry),
        "start",
    )


def _command_digest(command: Sequence[str]) -> str:
    canonical = "\0".join(_canonical(item) if index < 2 else item for index, item in enumerate(command))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_runtime_layout(
    paths: ManagedN8nPaths,
    *,
    require_d_drive: bool = True,
) -> None:
    root = paths.runtime_root.resolve(strict=False)
    if require_d_drive and os.name == "nt" and root.drive.casefold() != "d:":
        raise N8nConfigurationError(
            "Managed n8n runtime must be located on the D drive.",
            details={"runtime_root": str(root)},
        )
    for candidate in (
        paths.tool_dir,
        paths.data_home,
        paths.managed_dir,
        paths.log_dir,
        paths.temp_dir,
        paths.npm_cache_dir,
        paths.profile_home,
    ):
        if not _is_relative_to(candidate, root):
            raise N8nConfigurationError(
                "An n8n path escapes the managed runtime root.",
                details={"path": str(candidate)},
            )


def ensure_runtime_layout(paths: ManagedN8nPaths) -> None:
    """Create only reviewed D-drive runtime directories; never create workflows."""

    validate_runtime_layout(paths, require_d_drive=False)
    for directory in paths.writable_paths():
        directory.mkdir(parents=True, exist_ok=True)


def build_managed_environment(
    paths: ManagedN8nPaths,
    *,
    source: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Return a minimal n8n environment with every writable home on runtime.

    Unknown parent variables and secret-looking variables are not inherited.
    Gmail OAuth and the Workbench HMAC stay in n8n's encrypted credential
    store, never in this process environment.
    """

    validate_runtime_layout(paths, require_d_drive=False)
    parent = dict(source) if source is not None else os.environ
    environment = agent_subprocess_env()
    # agent_subprocess_env reads os.environ.  When tests supply a source,
    # rebuild the same small operational subset from that source instead.
    if source is not None:
        allowed = {
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "NUMBER_OF_PROCESSORS",
            "OS",
            "PATH",
            "PATHEXT",
            "SSL_CERT_DIR",
            "SSL_CERT_FILE",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "WINDIR",
        }
        environment = {
            key: str(value)
            for key, value in parent.items()
            if key.upper() in allowed
        }

    profile = str(paths.profile_home)
    roaming = str(paths.profile_home / "AppData" / "Roaming")
    local = str(paths.profile_home / "AppData" / "Local")
    exchange = str(paths.file_exchange_dir)
    environment.update(
        {
            "HOME": profile,
            "USERPROFILE": profile,
            "APPDATA": roaming,
            "LOCALAPPDATA": local,
            "TEMP": str(paths.temp_dir),
            "TMP": str(paths.temp_dir),
            "NPM_CONFIG_CACHE": str(paths.npm_cache_dir),
            "N8N_USER_FOLDER": str(paths.data_home),
            "N8N_LISTEN_ADDRESS": N8N_HOST,
            "N8N_HOST": "localhost",
            "N8N_PORT": str(N8N_PORT),
            "N8N_PROTOCOL": "http",
            "N8N_EDITOR_BASE_URL": "http://localhost:5678",
            "WEBHOOK_URL": "http://localhost:5678/",
            "GENERIC_TIMEZONE": "Asia/Taipei",
            "TZ": "Asia/Taipei",
            "N8N_DIAGNOSTICS_ENABLED": "false",
            "N8N_VERSION_NOTIFICATIONS_ENABLED": "false",
            "N8N_VERSION_NOTIFICATIONS_WHATS_NEW_ENABLED": "false",
            "N8N_BLOCK_ENV_ACCESS_IN_NODE": "true",
            "N8N_COMMUNITY_PACKAGES_ENABLED": "false",
            "N8N_UNVERIFIED_PACKAGES_ENABLED": "false",
            "N8N_VERIFIED_PACKAGES_ENABLED": "false",
            "N8N_COMMUNITY_PACKAGES_PREVENT_LOADING": "true",
            "N8N_SSRF_PROTECTION_ENABLED": "true",
            "N8N_SSRF_BLOCKED_IP_RANGES": "default",
            "N8N_SSRF_ALLOWED_IP_RANGES": "127.0.0.1/32,::1/128",
            "N8N_SSRF_ALLOWED_HOSTNAMES": "localhost",
            "N8N_RESTRICT_FILE_ACCESS_TO": exchange,
            "N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES": "true",
            "N8N_SECURE_COOKIE": "false",
            "NODES_EXCLUDE": json.dumps(DISABLED_NODES, separators=(",", ":")),
            "EXECUTIONS_DATA_PRUNE": "true",
            "EXECUTIONS_DATA_MAX_AGE": "168",
            "EXECUTIONS_DATA_PRUNE_MAX_COUNT": "5000",
            "EXECUTIONS_DATA_SAVE_ON_SUCCESS": "none",
            "EXECUTIONS_DATA_SAVE_ON_ERROR": "none",
            "EXECUTIONS_DATA_SAVE_ON_PROGRESS": "false",
            "EXECUTIONS_DATA_SAVE_MANUAL_EXECUTIONS": "false",
            "N8N_GRACEFUL_SHUTDOWN_TIMEOUT": "30",
            "N8N_LOG_LEVEL": "info",
            "N8N_LOG_FORMAT": "json",
            "N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS": "false",
        }
    )
    return environment


def validate_installation(
    paths: ManagedN8nPaths,
    *,
    probe_node: bool = True,
) -> dict[str, Any]:
    """Verify the exact package pin without ever invoking the n8n CLI."""

    issues: list[str] = []
    package_root = paths.tool_dir / "node_modules" / "n8n"
    package_path = package_root / "package.json"
    version: Optional[str] = None
    if not _safe_regular_file(package_path, package_root, max_bytes=1024 * 1024):
        issues.append("n8n_package_missing")
    else:
        try:
            payload = json.loads(package_path.read_text(encoding="utf-8"))
            version = str(payload.get("version") or "")
        except (OSError, UnicodeError, ValueError):
            issues.append("n8n_package_invalid")
        else:
            if version != N8N_VERSION:
                issues.append("n8n_version_mismatch")
    if not _safe_regular_file(paths.n8n_entry, package_root, max_bytes=1024 * 1024):
        issues.append("n8n_entry_missing")
    node_version: Optional[str] = None
    if not paths.node_executable.is_file() or _is_reparse_point(paths.node_executable):
        issues.append("node_missing")
    elif probe_node:
        try:
            result = subprocess.run(
                [str(paths.node_executable), "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=False,
                env=build_managed_environment(paths),
            )
            node_version = (result.stdout or result.stderr).strip().lstrip("v")
            if result.returncode != 0 or node_version != NODE_VERSION:
                issues.append("node_version_mismatch")
        except (OSError, subprocess.SubprocessError):
            issues.append("node_probe_failed")
    return {
        "valid": not issues,
        "version": version,
        "expected_version": N8N_VERSION,
        "node_version": node_version,
        "expected_node_version": NODE_VERSION,
        "issues": issues,
        "installation_scope": "workbench_runtime",
    }


def inspect_port(
    *,
    host: str = N8N_HOST,
    port: int = N8N_PORT,
) -> PortInspection:
    """Inspect listeners without connecting to or trusting an unknown service."""

    try:
        listeners: set[int] = set()
        for connection in psutil.net_connections(kind="tcp"):
            if connection.status != psutil.CONN_LISTEN or not connection.laddr:
                continue
            if int(connection.laddr.port) != port:
                continue
            # A wildcard or IPv6 listener still owns the fixed port and must
            # block startup; never treat it as an available IPv4-only port.
            if connection.pid is not None:
                listeners.add(int(connection.pid))
        if not listeners:
            # Detect the rare listener whose PID psutil could not reveal.
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind((host, port))
            except OSError:
                return PortInspection("unknown", (), "listener_pid_unavailable")
            finally:
                probe.close()
            return PortInspection("free", (), "no_listener")
        if len(listeners) > 1:
            return PortInspection("conflict", tuple(sorted(listeners)), "multiple_listeners")
        return PortInspection("listening", tuple(listeners), "listener_found")
    except (OSError, psutil.Error):
        return PortInspection("unknown", (), "inspection_failed")


def probe_health(
    *,
    readiness: bool = False,
    timeout: float = 3.0,
    session: Optional[requests.Session] = None,
) -> HealthProbe:
    endpoint = "/healthz/readiness" if readiness else "/healthz"
    started = time.monotonic()
    owned_session = session is None
    client = session or requests.Session()
    client.trust_env = False
    try:
        response = client.get(f"{N8N_BASE_URL}{endpoint}", timeout=timeout)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        healthy = response.status_code == 200 and payload == {"status": "ok"}
        return HealthProbe(
            healthy,
            response.status_code,
            "probe_ok" if healthy else "unexpected_response",
            round((time.monotonic() - started) * 1000, 3),
        )
    except requests.RequestException:
        return HealthProbe(
            False,
            None,
            "connection_failed",
            round((time.monotonic() - started) * 1000, 3),
        )
    finally:
        if owned_session:
            client.close()


def _record_from_payload(payload: Mapping[str, Any]) -> LifecycleRecord:
    required = {
        "schema_version",
        "owner",
        "owner_id",
        "pid",
        "process_created_at",
        "started_at",
        "version",
        "node_version",
        "node_executable",
        "n8n_entry",
        "command_sha256",
        "host",
        "port",
    }
    if set(payload) != required:
        raise ValueError("lifecycle record has unknown or missing fields")
    record = LifecycleRecord(
        schema_version=int(payload["schema_version"]),
        owner=str(payload["owner"]),
        owner_id=str(payload["owner_id"]),
        pid=int(payload["pid"]),
        process_created_at=float(payload["process_created_at"]),
        started_at=str(payload["started_at"]),
        version=str(payload["version"]),
        node_version=str(payload["node_version"]),
        node_executable=str(payload["node_executable"]),
        n8n_entry=str(payload["n8n_entry"]),
        command_sha256=str(payload["command_sha256"]),
        host=str(payload["host"]),
        port=int(payload["port"]),
    )
    if (
        record.schema_version != _STATE_SCHEMA_VERSION
        or record.owner != "local-ai-workbench"
        or not re.fullmatch(r"[0-9a-f]{32}", record.owner_id)
        or record.pid <= 0
        or record.process_created_at <= 0
        or record.version != N8N_VERSION
        or record.node_version != NODE_VERSION
        or not re.fullmatch(r"[0-9a-f]{64}", record.command_sha256)
        or record.host != N8N_HOST
        or record.port != N8N_PORT
    ):
        raise ValueError("lifecycle record does not match managed policy")
    return record


def read_lifecycle_record(paths: ManagedN8nPaths) -> Optional[LifecycleRecord]:
    if not paths.state_path.exists():
        return None
    if not _safe_regular_file(
        paths.state_path, paths.managed_dir, max_bytes=_MAX_STATE_BYTES
    ):
        raise N8nOwnershipError("The n8n lifecycle record is unsafe or unreadable.")
    try:
        payload = json.loads(paths.state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("record must be an object")
        return _record_from_payload(payload)
    except (OSError, UnicodeError, ValueError) as exc:
        raise N8nOwnershipError("The n8n lifecycle record is invalid.") from exc


def _write_lifecycle_record(paths: ManagedN8nPaths, record: LifecycleRecord) -> None:
    paths.managed_dir.mkdir(parents=True, exist_ok=True)
    temporary = paths.state_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(asdict(record), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, paths.state_path)


def _process_command(process: psutil.Process) -> tuple[str, ...]:
    try:
        return tuple(str(item) for item in process.cmdline())
    except (psutil.Error, OSError):
        return ()


def verify_owned_process(
    paths: ManagedN8nPaths,
    record: LifecycleRecord,
    *,
    listener_pids: Sequence[int] | None = None,
) -> tuple[bool, str, Optional[psutil.Process]]:
    """Prove ownership; never adopt a same-name or same-port process."""

    if listener_pids is not None and set(map(int, listener_pids)) != {record.pid}:
        return False, "listener_pid_mismatch", None
    if _canonical(record.node_executable) != _canonical(paths.node_executable):
        return False, "node_path_mismatch", None
    if _canonical(record.n8n_entry) != _canonical(paths.n8n_entry):
        return False, "entry_path_mismatch", None
    expected_command = _command(paths)
    if record.command_sha256 != _command_digest(expected_command):
        return False, "command_digest_mismatch", None
    try:
        process = psutil.Process(record.pid)
        created = float(process.create_time())
        executable = process.exe()
        command = _process_command(process)
    except (psutil.Error, OSError):
        return False, "process_unavailable", None
    if abs(created - record.process_created_at) > 0.01:
        return False, "pid_reused", None
    if _canonical(executable) != _canonical(paths.node_executable):
        return False, "process_executable_mismatch", None
    if tuple(map(_canonical_command_item, command)) != tuple(
        map(_canonical_command_item, expected_command)
    ):
        return False, "process_command_mismatch", None
    return True, "ownership_verified", process


def _canonical_command_item(item: str) -> str:
    text = str(item)
    if text.casefold() == "start":
        return "start"
    return _canonical(text)


@contextmanager
def _operation_lock(paths: ManagedN8nPaths) -> Iterator[None]:
    """Cross-process one-byte lock plus an in-process reentrant guard."""

    paths.managed_dir.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        handle = open(paths.lock_path, "a+b")
        try:
            handle.seek(0)
            if handle.read(1) == b"":
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def inspect_isolation(
    paths: Optional[ManagedN8nPaths] = None,
    *,
    script_path: Path | str | None = None,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Ask the non-mutating setup checker to attest account and path ACLs.

    The checker emits only booleans, SIDs, paths and stable blocker codes.  It
    never emits or accepts the dedicated account password.  Failure to run or
    parse the checker is itself a blocker; no interactive-user fallback exists.
    """

    managed = paths or ManagedN8nPaths.default()
    if os.name != "nt":
        return {
            "isolation_ready": False,
            "blockers": ["windows_isolation_required"],
            "account": N8N_SERVICE_ACCOUNT,
            "checked_at": _utc_now(),
        }
    checker = (
        Path(script_path)
        if script_path
        else REPO_ROOT / "scripts" / "setup_managed_n8n_isolation.ps1"
    )
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell or not checker.is_file() or _is_reparse_point(checker):
        return {
            "isolation_ready": False,
            "blockers": ["isolation_checker_missing"],
            "account": N8N_SERVICE_ACCOUNT,
            "checked_at": _utc_now(),
        }
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(checker),
                "-Mode",
                "Check",
                "-RuntimeRoot",
                str(managed.runtime_root),
                "-Json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8-sig",
            errors="replace",
            timeout=float(timeout_seconds),
            shell=False,
            env=agent_subprocess_env(),
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode != 0 or len(lines) != 1:
            raise ValueError("checker did not return one successful JSON record")
        payload = json.loads(lines[0])
        if not isinstance(payload, dict):
            raise ValueError("checker payload is not an object")
        blockers = payload.get("blockers")
        if not isinstance(blockers, list) or not all(
            isinstance(item, str) and re.fullmatch(r"[a-z0-9_]{1,64}", item)
            for item in blockers
        ):
            raise ValueError("checker blockers are invalid")
        ready = payload.get("isolation_ready") is True and not blockers
        return {
            "isolation_ready": ready,
            "blockers": blockers,
            "account": N8N_SERVICE_ACCOUNT,
            "account_exists": payload.get("account_exists") is True,
            "account_enabled": payload.get("account_enabled") is True,
            "account_non_admin": payload.get("account_non_admin") is True,
            "credential_ready": payload.get("credential_ready") is True,
            "acl_ready": payload.get("acl_ready") is True,
            "account_sid": str(payload.get("account_sid") or ""),
            "checked_at": str(payload.get("checked_at") or _utc_now()),
        }
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return {
            "isolation_ready": False,
            "blockers": ["isolation_check_failed"],
            "account": N8N_SERVICE_ACCOUNT,
            "checked_at": _utc_now(),
        }


class ManagedN8nLifecycle:
    """Own one local n8n 2.32.5 process and no other process."""

    def __init__(
        self,
        paths: Optional[ManagedN8nPaths] = None,
        *,
        require_d_drive: bool = True,
        isolation_checker: Optional[Any] = None,
        isolated_launcher: Any = _AUTO_LAUNCHER,
    ) -> None:
        self.paths = paths or ManagedN8nPaths.default()
        self.require_d_drive = bool(require_d_drive)
        self._isolation_checker = isolation_checker or inspect_isolation
        self._isolated_launcher = (
            WindowsRunAsLauncher(self.paths)
            if isolated_launcher is _AUTO_LAUNCHER and os.name == "nt"
            else None if isolated_launcher is _AUTO_LAUNCHER else isolated_launcher
        )
        self._popen: Optional[Any] = None

    def status(self, *, probe_node: bool = False) -> dict[str, Any]:
        isolation = self._isolation_checker(self.paths)
        try:
            validate_runtime_layout(
                self.paths, require_d_drive=self.require_d_drive
            )
        except N8nConfigurationError as exc:
            return self._status(
                "failed", exc.code, installation=None, isolation=isolation
            )
        installation = validate_installation(self.paths, probe_node=probe_node)
        port = inspect_port()
        try:
            record = read_lifecycle_record(self.paths)
        except N8nOwnershipError as exc:
            return self._status(
                "port_conflict" if port.state != "free" else "failed",
                exc.code,
                installation=installation,
                port=port,
                isolation=isolation,
            )
        if port.state == "free":
            if record is not None:
                owned, reason, _ = verify_owned_process(self.paths, record)
                if owned:
                    return self._status(
                        "starting",
                        "awaiting_listener",
                        installation=installation,
                        port=port,
                        pid=record.pid,
                        isolation=isolation,
                    )
                if reason not in {"process_unavailable", "pid_reused"}:
                    return self._status(
                        "failed",
                        reason,
                        installation=installation,
                        port=port,
                        isolation=isolation,
                    )
            state = "stopped" if installation["valid"] else "upgrade_required"
            return self._status(
                state,
                "no_listener" if installation["valid"] else "installation_invalid",
                installation=installation,
                port=port,
                isolation=isolation,
            )
        if port.state != "listening" or record is None:
            return self._status(
                "port_conflict",
                port.reason if record is None else "listener_conflict",
                installation=installation,
                port=port,
                isolation=isolation,
            )
        owned, reason, _ = verify_owned_process(
            self.paths, record, listener_pids=port.pids
        )
        if not owned:
            return self._status(
                "port_conflict",
                reason,
                installation=installation,
                port=port,
                isolation=isolation,
            )
        live = probe_health(readiness=False)
        ready = probe_health(readiness=True)
        state = "ready" if live.healthy and ready.healthy else (
            "starting" if live.healthy else "degraded"
        )
        return self._status(
            state,
            "ready" if state == "ready" else ready.reason,
            installation=installation,
            port=port,
            pid=record.pid,
            health={"liveness": asdict(live), "readiness": asdict(ready)},
            isolation=isolation,
        )

    def start(self, *, timeout_seconds: float = 120.0) -> dict[str, Any]:
        if not 1 <= float(timeout_seconds) <= 300:
            raise ValueError("timeout_seconds must be between 1 and 300")
        with _operation_lock(self.paths):
            validate_runtime_layout(
                self.paths, require_d_drive=self.require_d_drive
            )
            ensure_runtime_layout(self.paths)
            isolation = self._isolation_checker(self.paths)
            if isolation.get("isolation_ready") is not True:
                raise N8nConfigurationError(
                    "Managed n8n isolation is not ready.",
                    details={"blockers": list(isolation.get("blockers") or [])},
                )
            if self._isolated_launcher is None:
                raise N8nConfigurationError(
                    "The reviewed WorkbenchN8n run-as launcher is not configured.",
                    details={"blockers": ["isolated_launcher_unconfigured"]},
                )
            installation = validate_installation(self.paths, probe_node=True)
            if not installation["valid"]:
                raise N8nConfigurationError(
                    "The pinned n8n installation is incomplete or mismatched.",
                    details={"issues": installation["issues"]},
                )
            current = self.status(probe_node=False)
            if current["state"] == "ready":
                return current
            if current["state"] in {"port_conflict", "starting", "degraded"}:
                raise N8nPortConflict(
                    "Port 5678 is not safely available for a new managed n8n process.",
                    details={"state": current["state"], "reason": current["reason"]},
                )

            command = _command(self.paths)
            environment = build_managed_environment(self.paths)
            owner_id = uuid.uuid4().hex
            environment["WORKBENCH_N8N_OWNER_ID"] = owner_id
            stdout_path = self.paths.log_dir / "n8n.stdout.log"
            stderr_path = self.paths.log_dir / "n8n.stderr.log"
            creationflags = 0
            popen_options: dict[str, Any] = {}
            if os.name == "nt":
                creationflags = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                )
            else:
                popen_options["start_new_session"] = True
            try:
                with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
                    process = self._isolated_launcher(
                        command=list(command),
                        cwd=str(self.paths.tool_dir),
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                        shell=False,
                        creationflags=creationflags,
                        **popen_options,
                    )
                self._popen = process
                ps_process = psutil.Process(process.pid)
                record = LifecycleRecord(
                    schema_version=_STATE_SCHEMA_VERSION,
                    owner="local-ai-workbench",
                    owner_id=owner_id,
                    pid=process.pid,
                    process_created_at=float(ps_process.create_time()),
                    started_at=_utc_now(),
                    version=N8N_VERSION,
                    node_version=NODE_VERSION,
                    node_executable=str(self.paths.node_executable),
                    n8n_entry=str(self.paths.n8n_entry),
                    command_sha256=_command_digest(command),
                    host=N8N_HOST,
                    port=N8N_PORT,
                )
                _write_lifecycle_record(self.paths, record)
            except Exception as exc:
                self._popen = None
                raise N8nStartupError("Could not launch managed n8n.") from exc

            deadline = time.monotonic() + float(timeout_seconds)
            try:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise N8nStartupError(
                            "Managed n8n exited before becoming ready.",
                            details={"exit_code": process.returncode},
                        )
                    port = inspect_port()
                    if port.state == "listening":
                        owned, reason, _ = verify_owned_process(
                            self.paths, record, listener_pids=port.pids
                        )
                        if not owned:
                            raise N8nPortConflict(
                                "Another process acquired port 5678 during startup.",
                                details={"reason": reason},
                            )
                        if probe_health(readiness=True).healthy:
                            return self.status(probe_node=False)
                    elif port.state in {"unknown", "conflict"}:
                        raise N8nPortConflict(
                            "Port 5678 could not be verified during startup.",
                            details={"reason": port.reason},
                        )
                    time.sleep(0.25)
                raise N8nStartupError("Managed n8n did not become ready in time.")
            except Exception:
                self._stop_verified_record(record, graceful_seconds=5.0)
                self._remove_state_if_owned(record)
                raise

    def stop(self, *, graceful_seconds: float = 35.0) -> dict[str, Any]:
        if not 1 <= float(graceful_seconds) <= 60:
            raise ValueError("graceful_seconds must be between 1 and 60")
        with _operation_lock(self.paths):
            port = inspect_port()
            record = read_lifecycle_record(self.paths)
            if record is None:
                if port.state == "free":
                    return self._status("stopped", "already_stopped")
                raise N8nOwnershipError(
                    "Refusing to stop a listener without a managed lifecycle record."
                )
            listener_pids = port.pids if port.state == "listening" else None
            owned, reason, _ = verify_owned_process(
                self.paths, record, listener_pids=listener_pids
            )
            if not owned:
                if port.state == "free" and reason in {
                    "process_unavailable",
                    "pid_reused",
                }:
                    self._remove_state_if_owned(record)
                    return self._status("stopped", "stale_record_removed")
                raise N8nOwnershipError(
                    "Refusing to stop a process whose ownership is not proven.",
                    details={"reason": reason, "port_state": port.state},
                )
            self._stop_verified_record(record, graceful_seconds=graceful_seconds)
            self._remove_state_if_owned(record)
            self._popen = None
            return self._status("stopped", "stopped")

    def _stop_verified_record(
        self, record: LifecycleRecord, *, graceful_seconds: float
    ) -> None:
        owned, reason, process = verify_owned_process(self.paths, record)
        if not owned or process is None:
            raise N8nOwnershipError(
                "Process ownership changed before shutdown.", details={"reason": reason}
            )
        descendants = process.children(recursive=True)
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGINT)
            process.wait(timeout=float(graceful_seconds))
            return
        except (psutil.TimeoutExpired, OSError, SystemError, psutil.Error):
            pass
        # Re-verify PID identity immediately before the irreversible fallback.
        owned, reason, process = verify_owned_process(self.paths, record)
        if not owned or process is None:
            raise N8nOwnershipError(
                "Process ownership changed before forced shutdown.",
                details={"reason": reason},
            )
        for child in descendants:
            try:
                child.kill()
            except psutil.Error:
                continue
        process.kill()
        try:
            process.wait(timeout=5)
        except psutil.Error:
            raise N8nLifecycleError("Managed n8n did not stop after forced shutdown.")

    def _remove_state_if_owned(self, record: LifecycleRecord) -> None:
        try:
            current = read_lifecycle_record(self.paths)
        except N8nOwnershipError:
            return
        if current == record:
            self.paths.state_path.unlink(missing_ok=True)

    def _status(
        self,
        state: str,
        reason: str,
        *,
        installation: Optional[Mapping[str, Any]] = None,
        port: Optional[PortInspection] = None,
        pid: Optional[int] = None,
        health: Optional[Mapping[str, Any]] = None,
        isolation: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        isolation_payload = dict(isolation or self._isolation_checker(self.paths))
        return {
            "state": state,
            "reason": reason,
            "managed": state not in {"port_conflict"},
            "version": N8N_VERSION,
            "node_version": NODE_VERSION,
            "url": N8N_BASE_URL,
            "host": N8N_HOST,
            "port": N8N_PORT,
            "pid": pid,
            "runtime_root": str(self.paths.runtime_root),
            "data_home": str(self.paths.data_home),
            "installation": dict(installation or {}),
            "listener": asdict(port) if port else None,
            "health": dict(health or {}),
            "isolation_ready": isolation_payload.get("isolation_ready") is True,
            "isolation_blockers": list(isolation_payload.get("blockers") or []),
            "isolation": isolation_payload,
            "checked_at": _utc_now(),
        }


def inspect_stray_user_profile(
    paths: Optional[ManagedN8nPaths] = None,
    *,
    profile_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Read-only preflight for the accidental ``%USERPROFILE%\\.n8n`` file.

    It never deletes, renames, chmods, or rewrites anything.  Callers may show
    ``safe_candidate`` to a human before a separate, explicitly authorized
    cleanup operation.
    """

    managed = paths or ManagedN8nPaths.default()
    directory = Path(profile_dir) if profile_dir else Path.home() / ".n8n"
    report: dict[str, Any] = {
        "path": str(directory),
        "state": "absent",
        "safe_candidate": False,
        "reason": "not_found",
        "entries": [],
        "matches_managed_key": None,
    }
    if not directory.exists():
        return report
    if not directory.is_dir() or _is_reparse_point(directory):
        report.update(state="unsafe", reason="not_plain_directory")
        return report
    try:
        entries = list(directory.iterdir())
    except OSError:
        report.update(state="unsafe", reason="unreadable")
        return report
    report["entries"] = [item.name for item in entries]
    if len(entries) != 1 or entries[0].name != "config":
        report.update(state="in_use", reason="contains_additional_data")
        return report
    config = entries[0]
    if not _safe_regular_file(config, directory, max_bytes=4096):
        report.update(state="unsafe", reason="config_not_plain_file")
        return report
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        report.update(state="unsafe", reason="config_invalid")
        return report
    if (
        not isinstance(payload, dict)
        or set(payload) != {"encryptionKey"}
        or not isinstance(payload["encryptionKey"], str)
        or len(payload["encryptionKey"]) != 32
    ):
        report.update(state="in_use", reason="config_shape_unexpected")
        return report
    managed_config = managed.n8n_dir / "config"
    if _safe_regular_file(managed_config, managed.n8n_dir, max_bytes=4096):
        try:
            managed_payload = json.loads(managed_config.read_text(encoding="utf-8"))
            report["matches_managed_key"] = (
                managed_payload.get("encryptionKey") == payload["encryptionKey"]
            )
        except (OSError, UnicodeError, ValueError, AttributeError):
            report["matches_managed_key"] = None
    report.update(
        state="candidate",
        safe_candidate=True,
        reason="single_generated_config_only",
    )
    return report


def load_workflow_template(path: Path | str) -> dict[str, Any]:
    candidate = Path(path)
    root = candidate.parent
    if not _safe_regular_file(candidate, root, max_bytes=_MAX_TEMPLATE_BYTES):
        raise N8nTemplateError("Workflow template must be a small regular JSON file.")
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise N8nTemplateError("Workflow template is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise N8nTemplateError("Workflow template root must be an object.")
    return payload


def validate_workflow_template(
    workflow: Mapping[str, Any],
    *,
    require_placeholders: bool = True,
) -> dict[str, Any]:
    """Validate one reviewed managed template without executing or importing."""

    if workflow.get("templateId") in {
        AGENT_BRIDGE_TEMPLATE_ID,
        APPROVAL_GATE_TEMPLATE_ID,
    }:
        return validate_agent_workflow_template(
            workflow, require_placeholders=require_placeholders
        )

    payload = dict(workflow)
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    forbidden_fragments = (
        "n8n-nodes-base.code",
        "n8n-nodes-base.executeCommand",
        "workflow_instruction",
        "$env",
        "process.env",
        "require(",
    )
    if any(fragment in serialized for fragment in forbidden_fragments):
        raise N8nTemplateError("Workflow template contains forbidden executable or instruction data.")
    required_root = {
        "templateId",
        "name",
        "active",
        "nodes",
        "connections",
        "settings",
        "staticData",
        "pinData",
    }
    if set(payload) != required_root:
        raise N8nTemplateError("Workflow template has unknown or missing root fields.")
    template_id = payload.get("templateId")
    if template_id not in {"workbench-gmail-inbound-v1", "workbench-gmail-send-v1"}:
        raise N8nTemplateError("Unknown workflow template ID.")
    if payload.get("active") is not False:
        raise N8nTemplateError("Managed workflow templates must remain inactive.")
    if payload.get("staticData") is not None or payload.get("pinData") != {}:
        raise N8nTemplateError("Workflow templates may not contain static or pinned data.")
    if payload.get("settings") != {"executionOrder": "v1"}:
        raise N8nTemplateError("Workflow settings do not match the reviewed policy.")
    nodes = payload.get("nodes")
    connections = payload.get("connections")
    if not isinstance(nodes, list) or not nodes or not isinstance(connections, dict):
        raise N8nTemplateError("Workflow nodes and connections are required.")
    names: set[str] = set()
    ids: set[str] = set()
    node_map: dict[str, Mapping[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise N8nTemplateError("Each workflow node must be an object.")
        allowed_node_keys = {
            "parameters",
            "id",
            "name",
            "type",
            "typeVersion",
            "position",
            "credentials",
            "onError",
            "webhookId",
        }
        if not set(node).issubset(allowed_node_keys):
            raise N8nTemplateError("A workflow node contains an unreviewed field.")
        node_type = node.get("type")
        name = node.get("name")
        node_id = node.get("id")
        if node_type not in ALLOWED_TEMPLATE_NODE_TYPES:
            raise N8nTemplateError(f"Node type is not allowed: {node_type}")
        if not isinstance(name, str) or not name or name in names:
            raise N8nTemplateError("Workflow node names must be unique.")
        if not isinstance(node_id, str) or not node_id or node_id in ids:
            raise N8nTemplateError("Workflow node IDs must be unique.")
        names.add(name)
        ids.add(node_id)
        node_map[name] = node
        parameters = node.get("parameters")
        if not isinstance(parameters, dict):
            raise N8nTemplateError("Workflow node parameters must be an object.")
        if node_type == "n8n-nodes-base.httpRequest":
            if parameters.get("method") != "POST":
                raise N8nTemplateError("Managed HTTP nodes must use POST.")
            if parameters.get("url") not in ALLOWED_TEMPLATE_HTTP_URLS:
                raise N8nTemplateError("HTTP node URL is outside the reviewed API.")
            if parameters.get("url") == GMAIL_DRAFT_SEND_URL:
                if (
                    parameters.get("authentication") != "predefinedCredentialType"
                    or parameters.get("nodeCredentialType") != "gmailOAuth2"
                ):
                    raise N8nTemplateError("The Gmail draft send call must use predefined Gmail OAuth.")
                _validate_credential(
                    node,
                    "gmailOAuth2",
                    GMAIL_CREDENTIAL_PLACEHOLDER,
                    require_placeholders=require_placeholders,
                )
            else:
                _validate_signed_http_node(node)
        elif node_type == "n8n-nodes-base.gmail":
            _validate_credential(
                node,
                "gmailOAuth2",
                GMAIL_CREDENTIAL_PLACEHOLDER,
                require_placeholders=require_placeholders,
            )
        elif node_type == "n8n-nodes-base.crypto":
            action = parameters.get("action")
            if action not in {"hash", "hmac", "generate"}:
                raise N8nTemplateError("Crypto nodes are restricted to hash, HMAC and nonce generation.")
            if action == "hmac":
                if parameters != {
                    "action": "hmac",
                    "binaryData": False,
                    "type": "SHA256",
                    "value": "={{$json.canonical}}",
                    "dataPropertyName": "signature",
                    "encoding": "hex",
                }:
                    raise N8nTemplateError("HMAC nodes must sign the reviewed canonical value with SHA-256.")
                _validate_credential(
                    node,
                    "crypto",
                    WORKBENCH_HMAC_CREDENTIAL_PLACEHOLDER,
                    require_placeholders=require_placeholders,
                )
            elif action == "hash":
                if parameters != {
                    "action": "hash",
                    "binaryData": False,
                    "type": "SHA256",
                    "value": "={{$json.request_body}}",
                    "dataPropertyName": "body_sha256",
                    "encoding": "hex",
                }:
                    raise N8nTemplateError("Hash nodes must hash the exact reviewed request body.")
                if node.get("credentials"):
                    raise N8nTemplateError("Hash nodes may not reference credentials.")
            else:
                if parameters != {
                    "action": "generate",
                    "dataPropertyName": "nonce",
                    "encodingType": "hex",
                    "stringLength": 32,
                }:
                    raise N8nTemplateError("Nonce nodes must generate a fresh reviewed 32-byte value.")
                if node.get("credentials"):
                    raise N8nTemplateError("Nonce nodes may not reference credentials.")
        elif node_type == "n8n-nodes-base.webhook":
            if (
                parameters.get("httpMethod") != "POST"
                or parameters.get("path") != GMAIL_SEND_WEBHOOK_PATH
                or parameters.get("authentication") != "headerAuth"
            ):
                raise N8nTemplateError("Only the reviewed Gmail send webhook is allowed.")
            _validate_credential(
                node,
                "httpHeaderAuth",
                WORKBENCH_WEBHOOK_CREDENTIAL_PLACEHOLDER,
                require_placeholders=require_placeholders,
            )
        elif node_type == "n8n-nodes-base.scheduleTrigger":
            if template_id != "workbench-gmail-inbound-v1":
                raise N8nTemplateError("Schedule trigger belongs only to inbound Gmail.")

    _validate_connections(connections, names)
    types = [str(node.get("type")) for node in nodes]
    if template_id == "workbench-gmail-inbound-v1":
        schedule_nodes = [
            node for node in nodes
            if node.get("type") == "n8n-nodes-base.scheduleTrigger"
        ]
        if len(schedule_nodes) != 1 or schedule_nodes[0].get("name") != "Schedule":
            raise N8nTemplateError("Inbound Gmail requires one reviewed Schedule Trigger.")
        if schedule_nodes[0].get("parameters") != {
            "rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}
        }:
            raise N8nTemplateError("Inbound Gmail polling interval must remain one minute.")
        if "n8n-nodes-base.webhook" in types:
            raise N8nTemplateError("Inbound Gmail must not use a Gmail/Webhook trigger.")

        gmail_nodes = {
            str(node.get("name")): node
            for node in nodes
            if node.get("type") == "n8n-nodes-base.gmail"
        }
        if set(gmail_nodes) != {
            "Gmail Search", "Gmail Thread Get", "Remove Workbench Label"
        }:
            raise N8nTemplateError("Inbound Gmail nodes differ from the reviewed policy.")
        if gmail_nodes["Gmail Search"].get("parameters") != {
            "resource": "message",
            "operation": "getAll",
            "returnAll": False,
            "limit": 10,
            "simple": False,
            "options": {"downloadAttachments": False},
            "filters": {"q": "label:Workbench-Agent in:inbox -in:sent"},
        }:
            raise N8nTemplateError("Inbound Gmail search policy must remain fixed.")
        if gmail_nodes["Gmail Thread Get"].get("parameters") != {
            "resource": "thread",
            "operation": "get",
            "threadId": "={{$json.threadId}}",
            "simple": False,
            "options": {"returnOnlyMessages": False},
        }:
            raise N8nTemplateError("Inbound Gmail must fetch one full reviewed thread.")
        if gmail_nodes["Remove Workbench Label"].get("parameters") != {
            "resource": "message",
            "operation": "removeLabels",
            "messageId": "={{$('Build Gmail Context').item.json.gmail_message_id}}",
            "labelIds": "={{[$('Build Gmail Context').item.json.workbench_label_id]}}",
        }:
            raise N8nTemplateError("Inbound Gmail label acknowledgement policy was changed.")

        set_nodes = {
            str(node.get("name")): node
            for node in nodes
            if node.get("type") == "n8n-nodes-base.set"
        }
        expected_set_names = {
            "Build Gmail Context", "Prepare Gmail Event",
            "Prepare Attempt 1 Auth", "Prepare Attempt 2 Auth",
            "Prepare Attempt 3 Auth", "Build Attempt 1 Canonical",
            "Build Attempt 2 Canonical", "Build Attempt 3 Canonical",
        }
        if set(set_nodes) != expected_set_names:
            raise N8nTemplateError("Inbound Gmail transform nodes differ from the reviewed policy.")
        _validate_inbound_context_node(set_nodes["Build Gmail Context"])
        event_assignments = _reviewed_set_assignments(
            set_nodes["Prepare Gmail Event"], include_other_fields=False
        )
        expected_event_body = (
            "={{JSON.stringify({event_id:'gmail-'+$json.gmail_message_id,"
            "workflow_key:'__WORKBENCH_WORKFLOW_KEY__',"
            "gmail_message_id:$json.gmail_message_id,"
            "gmail_thread_id:$json.gmail_thread_id,sender:$json.sender,"
            "subject:$json.subject,body_text:$json.body_text,"
            "labels:['INBOX','Workbench-Agent'],attachments:$json.attachments,"
            "thread_messages:$json.thread_messages})}}"
        )
        actual_event_body = event_assignments.get("request_body")
        if not require_placeholders and isinstance(actual_event_body, tuple):
            actual_event_body = (
                actual_event_body[0],
                re.sub(
                    r"workflow_key:'[A-Za-z0-9_-]{1,128}'",
                    f"workflow_key:'{WORKBENCH_WORKFLOW_KEY_PLACEHOLDER}'",
                    str(actual_event_body[1]),
                    count=1,
                ),
            )
        if (
            set(event_assignments) != {"request_body", "request_path"}
            or actual_event_body != ("string", expected_event_body)
            or event_assignments.get("request_path") != (
                "string", "/api/integrations/n8n/v1/gmail/events"
            )
        ):
            raise N8nTemplateError("Inbound Gmail event body contract was changed.")
        for attempt in range(1, 4):
            auth_assignments = _reviewed_set_assignments(
                set_nodes[f"Prepare Attempt {attempt} Auth"],
                include_other_fields=False,
            )
            if auth_assignments != {
                "request_body": (
                    "string", "={{$('Prepare Gmail Event').item.json.request_body}}"
                ),
                "request_path": (
                    "string", "={{$('Prepare Gmail Event').item.json.request_path}}"
                ),
                "timestamp": (
                    "string", "={{Math.floor(Date.now()/1000).toString()}}"
                ),
            }:
                raise N8nTemplateError(
                    "Each retry must reuse only the stable event body and create fresh auth data."
                )
            canonical_assignments = _reviewed_set_assignments(
                set_nodes[f"Build Attempt {attempt} Canonical"],
                include_other_fields=True,
            )
            if canonical_assignments != {
                "canonical": (
                    "string",
                    "={{'POST\\n'+$json.request_path+'\\n'+$json.timestamp+"
                    "'\\n'+$json.nonce+'\\n'+$json.body_sha256}}",
                )
            }:
                raise N8nTemplateError("Inbound Gmail canonical HMAC input was changed.")

        crypto_nodes = {
            str(node.get("name")): node
            for node in nodes
            if node.get("type") == "n8n-nodes-base.crypto"
        }
        expected_crypto_names = {
            *(f"Hash Attempt {attempt} Body" for attempt in range(1, 4)),
            *(f"Generate Attempt {attempt} Nonce" for attempt in range(1, 4)),
            *(f"Sign Attempt {attempt}" for attempt in range(1, 4)),
        }
        if set(crypto_nodes) != expected_crypto_names:
            raise N8nTemplateError("Inbound Gmail must use exactly three independently signed attempts.")

        http_nodes = {
            str(node.get("name")): node
            for node in nodes
            if node.get("type") == "n8n-nodes-base.httpRequest"
        }
        expected_http_names = {f"Submit Attempt {attempt}" for attempt in range(1, 4)}
        if set(http_nodes) != expected_http_names or any(
            node.get("parameters", {}).get("url") != GMAIL_INBOUND_URL
            for node in http_nodes.values()
        ):
            raise N8nTemplateError("Inbound Gmail must use exactly three reviewed callback attempts.")

        if_nodes = {
            str(node.get("name")): node
            for node in nodes
            if node.get("type") == "n8n-nodes-base.if"
        }
        expected_if_names = {
            "Accepted Attempt 1", "Accepted Attempt 2", "Accepted Attempt 3",
            "Retryable Attempt 1", "Retryable Attempt 2",
            "Workbench Label Available",
        }
        if set(if_nodes) != expected_if_names:
            raise N8nTemplateError("Inbound Gmail retry and acknowledgement guards were changed.")
        for attempt in range(1, 4):
            _validate_boolean_if(
                if_nodes[f"Accepted Attempt {attempt}"],
                "={{Number($json.statusCode||0)===202 && $json.body?.accepted===true}}",
            )
        for attempt in range(1, 3):
            _validate_boolean_if(
                if_nodes[f"Retryable Attempt {attempt}"],
                "={{Boolean($json.error)||Number($json.statusCode||0)>=500}}",
            )
        _validate_boolean_if(
            if_nodes["Workbench Label Available"],
            "={{Boolean($('Build Gmail Context').item.json.workbench_label_id)}}",
        )

        expected_connections: dict[str, list[list[str]]] = {
            "Schedule": [["Gmail Search"]],
            "Gmail Search": [["Gmail Thread Get"]],
            "Gmail Thread Get": [["Build Gmail Context"]],
            "Build Gmail Context": [["Prepare Gmail Event"]],
            "Prepare Gmail Event": [["Prepare Attempt 1 Auth"]],
            "Accepted Attempt 1": [
                ["Workbench Label Available"], ["Retryable Attempt 1"]
            ],
            "Retryable Attempt 1": [["Prepare Attempt 2 Auth"], []],
            "Accepted Attempt 2": [
                ["Workbench Label Available"], ["Retryable Attempt 2"]
            ],
            "Retryable Attempt 2": [["Prepare Attempt 3 Auth"], []],
            "Accepted Attempt 3": [["Workbench Label Available"], []],
            "Workbench Label Available": [["Remove Workbench Label"], []],
        }
        for attempt in range(1, 4):
            expected_connections.update({
                f"Prepare Attempt {attempt} Auth": [[f"Hash Attempt {attempt} Body"]],
                f"Hash Attempt {attempt} Body": [[f"Generate Attempt {attempt} Nonce"]],
                f"Generate Attempt {attempt} Nonce": [[f"Build Attempt {attempt} Canonical"]],
                f"Build Attempt {attempt} Canonical": [[f"Sign Attempt {attempt}"]],
                f"Sign Attempt {attempt}": [[f"Submit Attempt {attempt}"]],
                f"Submit Attempt {attempt}": [[f"Accepted Attempt {attempt}"]],
            })
        if _connection_target_names(connections) != expected_connections:
            raise N8nTemplateError(
                "Inbound Gmail must acknowledge only after acceptance and retry at most three times."
            )
        if require_placeholders:
            if WORKBENCH_WORKFLOW_KEY_PLACEHOLDER not in serialized:
                raise N8nTemplateError("Inbound workflow key placeholder is missing.")
        elif WORKBENCH_WORKFLOW_KEY_PLACEHOLDER in serialized:
            raise N8nTemplateError("Workflow cannot activate without a bound workflow key.")
    else:
        webhook_nodes = [
            node for node in nodes if node.get("type") == "n8n-nodes-base.webhook"
        ]
        if len(webhook_nodes) != 1:
            raise N8nTemplateError("Gmail send requires exactly one reviewed webhook.")
        if any(
            fragment in serialized
            for fragment in ("$json.body.to", "$json.body.subject", "$json.body.body")
        ):
            raise N8nTemplateError("The outbound webhook may carry only delivery identity and claim proof.")
        urls = {node.get("parameters", {}).get("url") for node in nodes}
        if not {GMAIL_SEND_CLAIM_URL, GMAIL_SEND_RESULT_URL, GMAIL_DRAFT_SEND_URL}.issubset(urls):
            raise N8nTemplateError("Gmail send claim/result endpoints are missing.")
        draft_nodes = [
            node for node in nodes
            if node.get("type") == "n8n-nodes-base.gmail"
            and node.get("parameters", {}).get("resource") == "draft"
        ]
        if len(draft_nodes) != 1 or draft_nodes[0].get("parameters", {}).get("operation") != "create":
            raise N8nTemplateError("Gmail send must create one draft before sending it.")
        gmail_api_nodes = [
            node for node in nodes
            if node.get("type") == "n8n-nodes-base.httpRequest"
            and node.get("parameters", {}).get("url") == GMAIL_DRAFT_SEND_URL
        ]
        if len(gmail_api_nodes) != 1:
            raise N8nTemplateError("Gmail send must use the one reviewed drafts/send call.")
        _require_direct_connection(connections, "Create Gmail Draft", "Send Gmail Draft")
        for source, target in (
            ("Sign Claim", "Claim Delivery"),
            ("Sign Success Result", "Report Success"),
            ("Sign Failure Result", "Report Failure"),
        ):
            _require_direct_connection(connections, source, target)
    return {
        "valid": True,
        "template_id": template_id,
        "node_count": len(nodes),
        "credential_state": "placeholders" if require_placeholders else "bound",
        "active": False,
    }


def validate_agent_workflow_template(
    workflow: Mapping[str, Any],
    *,
    require_placeholders: bool = True,
) -> dict[str, Any]:
    """Validate the two protected Agent sub-workflows against reviewed bytes.

    These workflows deliberately use a single, reviewed polling cycle.  The
    cycle lets an n8n parent wait for a tool-free model task or for a human
    decision without exposing a webhook or installing a custom node.  Exact
    reference comparison is the authority boundary: adding a node, changing
    an expression or redirecting one HTTP request fails closed.
    """

    payload = copy.deepcopy(dict(workflow))
    required_root = {
        "templateId", "name", "active", "nodes", "connections",
        "settings", "staticData", "pinData",
    }
    if set(payload) != required_root:
        raise N8nTemplateError("Protected Agent template root fields were changed.")
    template_id = payload.get("templateId")
    expected_names = {
        AGENT_BRIDGE_TEMPLATE_ID: AGENT_BRIDGE_WORKFLOW_NAME,
        APPROVAL_GATE_TEMPLATE_ID: APPROVAL_GATE_WORKFLOW_NAME,
    }
    if template_id not in expected_names or payload.get("name") != expected_names[template_id]:
        raise N8nTemplateError("Unknown protected Agent workflow template.")
    if payload.get("active") is not False:
        raise N8nTemplateError("Protected Agent workflow templates must remain inactive.")
    if payload.get("settings") != {"executionOrder": "v1"}:
        raise N8nTemplateError("Protected Agent workflow settings were changed.")
    if payload.get("staticData") is not None or payload.get("pinData") != {}:
        raise N8nTemplateError("Protected Agent templates may not contain static or pinned data.")

    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    forbidden = (
        "n8n-nodes-base.code",
        "n8n-nodes-base.executeCommand",
        "n8n-nodes-base.localFileTrigger",
        "n8n-nodes-base.readWriteFile",
        "n8n-nodes-base.ssh",
        "community",
        "$env",
        "process.env",
        "require(",
        "file://",
    )
    if any(fragment.casefold() in serialized.casefold() for fragment in forbidden):
        raise N8nTemplateError("Protected Agent template contains a forbidden capability.")

    nodes = payload.get("nodes")
    connections = payload.get("connections")
    if not isinstance(nodes, list) or not nodes or not isinstance(connections, dict):
        raise N8nTemplateError("Protected Agent nodes and connections are required.")
    names: set[str] = set()
    ids: set[str] = set()
    trigger_count = 0
    hmac_count = 0
    expected_urls = (
        {AGENT_TASK_SUBMIT_URL, AGENT_TASK_STATUS_URL}
        if template_id == AGENT_BRIDGE_TEMPLATE_ID
        else {RUNTIME_APPROVAL_SUBMIT_URL, RUNTIME_APPROVAL_STATUS_URL}
    )
    actual_urls: set[str] = set()
    allowed_node_keys = {
        "parameters", "id", "name", "type", "typeVersion", "position",
        "credentials", "onError", "webhookId",
    }
    for node in nodes:
        if not isinstance(node, dict) or not set(node).issubset(allowed_node_keys):
            raise N8nTemplateError("Protected Agent node fields were changed.")
        node_type = node.get("type")
        node_name = node.get("name")
        node_id = node.get("id")
        if node_type not in AGENT_TEMPLATE_NODE_TYPES:
            raise N8nTemplateError(f"Protected Agent node type is not allowed: {node_type}")
        if not isinstance(node_name, str) or not node_name or node_name in names:
            raise N8nTemplateError("Protected Agent node names must be unique.")
        if not isinstance(node_id, str) or not node_id or node_id in ids:
            raise N8nTemplateError("Protected Agent node IDs must be unique.")
        if not isinstance(node.get("parameters"), dict):
            raise N8nTemplateError("Protected Agent node parameters must be an object.")
        webhook_id = node.get("webhookId")
        if webhook_id is not None and (
            node_type != "n8n-nodes-base.wait"
            or not isinstance(webhook_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", webhook_id)
        ):
            raise N8nTemplateError("Protected Agent webhook identity is invalid.")
        position = node.get("position")
        if not (
            isinstance(position, list) and len(position) == 2
            and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in position)
        ):
            raise N8nTemplateError("Protected Agent node positions are invalid.")
        names.add(node_name)
        ids.add(node_id)

        if node_type == "n8n-nodes-base.executeWorkflowTrigger":
            trigger_count += 1
            if node.get("credentials") or node.get("onError"):
                raise N8nTemplateError("Protected sub-workflow trigger may not contain credentials.")
        elif node_type == "n8n-nodes-base.crypto":
            action = node["parameters"].get("action")
            if action == "hmac":
                hmac_count += 1
                if node["parameters"] != {
                    "action": "hmac", "binaryData": False, "type": "SHA256",
                    "value": "={{$json.canonical}}", "dataPropertyName": "signature",
                    "encoding": "hex",
                }:
                    raise N8nTemplateError("Protected Agent HMAC settings were changed.")
                _validate_credential(
                    node, "crypto", WORKBENCH_HMAC_CREDENTIAL_PLACEHOLDER,
                    require_placeholders=require_placeholders,
                )
            elif action == "hash":
                if node["parameters"] != {
                    "action": "hash", "binaryData": False, "type": "SHA256",
                    "value": "={{$json.request_body}}", "dataPropertyName": "body_sha256",
                    "encoding": "hex",
                } or node.get("credentials"):
                    raise N8nTemplateError("Protected Agent body hash settings were changed.")
            elif action == "generate":
                if node["parameters"] != {
                    "action": "generate", "dataPropertyName": "nonce",
                    "encodingType": "hex", "stringLength": 32,
                } or node.get("credentials"):
                    raise N8nTemplateError("Protected Agent nonce settings were changed.")
            else:
                raise N8nTemplateError("Protected Agent Crypto action is not allowed.")
        elif node_type == "n8n-nodes-base.httpRequest":
            url = node["parameters"].get("url")
            if url not in expected_urls:
                raise N8nTemplateError("Protected Agent HTTP URL is outside the signed API.")
            actual_urls.add(str(url))
            _validate_agent_signed_http_node(node)
        else:
            if node.get("credentials"):
                raise N8nTemplateError("Only reviewed HMAC nodes may reference a credential.")
    if trigger_count != 1 or hmac_count != 2 or actual_urls != expected_urls:
        raise N8nTemplateError("Protected Agent signed task boundary is incomplete.")
    _validate_connection_references(connections, names)

    reference_path = REPO_ROOT / "config" / "n8n-workflows" / f"{template_id}.json"
    reference = load_workflow_template(reference_path)
    normalized = _normalize_agent_workflow_for_reference(payload)
    if normalized != reference:
        raise N8nTemplateError("Protected Agent workflow differs from the reviewed template.")
    if require_placeholders:
        if serialized.count(WORKBENCH_HMAC_CREDENTIAL_PLACEHOLDER) != 2:
            raise N8nTemplateError("Protected Agent HMAC placeholders are missing.")
    elif WORKBENCH_HMAC_CREDENTIAL_PLACEHOLDER in serialized:
        raise N8nTemplateError("Protected Agent workflow cannot run with a placeholder credential.")
    return {
        "valid": True,
        "template_id": template_id,
        "node_count": len(nodes),
        "credential_state": "placeholders" if require_placeholders else "bound",
        "active": False,
        "protected": True,
    }


def _validate_agent_signed_http_node(node: Mapping[str, Any]) -> None:
    parameters = node.get("parameters") or {}
    if node.get("credentials"):
        raise N8nTemplateError("Signed Agent HTTP nodes may not contain an auth secret.")
    if set(parameters) != {
        "method", "url", "authentication", "sendHeaders", "headerParameters",
        "sendBody", "contentType", "rawContentType", "body", "options",
    }:
        raise N8nTemplateError("Signed Agent HTTP fields were changed.")
    if (
        parameters.get("method") != "POST"
        or parameters.get("authentication") != "none"
        or parameters.get("sendHeaders") is not True
        or parameters.get("sendBody") is not True
        or parameters.get("contentType") != "raw"
        or parameters.get("rawContentType") != "application/json"
        or parameters.get("body") != "={{$json.request_body}}"
        or node.get("onError") != "continueRegularOutput"
        or parameters.get("options") != {
            "timeout": 10000,
            "response": {"response": {
                "fullResponse": True, "neverError": True, "responseFormat": "json",
            }},
        }
    ):
        raise N8nTemplateError("Signed Agent HTTP behavior was changed.")
    rows = parameters.get("headerParameters", {}).get("parameters")
    if not isinstance(rows, list):
        raise N8nTemplateError("Signed Agent HTTP headers are missing.")
    actual = {
        str(row.get("name")): str(row.get("value"))
        for row in rows
        if isinstance(row, dict) and set(row) == {"name", "value"}
    }
    expected = {
        "X-N8N-Timestamp": "={{$json.timestamp}}",
        "X-N8N-Nonce": "={{$json.nonce}}",
        "X-N8N-Signature": "={{$json.signature}}",
        "X-N8N-Profile": AGENT_RUNTIME_PROFILE,
    }
    if actual != expected or len(rows) != len(expected):
        raise N8nTemplateError("Signed Agent HTTP headers were changed.")


def _validate_connection_references(
    connections: Mapping[str, Any], names: set[str]
) -> None:
    """Validate connection DTOs but permit the one exact reviewed polling cycle."""

    if not set(connections).issubset(names):
        raise N8nTemplateError("Protected Agent connection source is unknown.")
    for outputs in connections.values():
        if not isinstance(outputs, dict) or set(outputs) != {"main"}:
            raise N8nTemplateError("Protected Agent connections must use main outputs only.")
        branches = outputs.get("main")
        if not isinstance(branches, list):
            raise N8nTemplateError("Protected Agent connection branches are invalid.")
        for branch in branches:
            if not isinstance(branch, list):
                raise N8nTemplateError("Protected Agent connection branch is invalid.")
            for target in branch:
                if (
                    not isinstance(target, dict)
                    or set(target) != {"node", "type", "index"}
                    or target.get("node") not in names
                    or target.get("type") != "main"
                    or target.get("index") != 0
                ):
                    raise N8nTemplateError("Protected Agent connection target is invalid.")


def _normalize_agent_workflow_for_reference(
    workflow: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = copy.deepcopy(dict(workflow))
    for node in normalized.get("nodes", []):
        # n8n assigns this opaque runtime identity when a Wait node is saved;
        # it is not part of the reviewed behavior or user-authored template.
        node.pop("webhookId", None)
        credentials = node.get("credentials") or {}
        if "crypto" in credentials:
            credentials["crypto"]["id"] = WORKBENCH_HMAC_CREDENTIAL_PLACEHOLDER
    return normalized


def bind_agent_workflow_credential(
    workflow: Mapping[str, Any], *, workbench_hmac_credential_id: str
) -> dict[str, Any]:
    """Bind only the opaque HMAC credential ID; never install or activate."""

    validate_agent_workflow_template(workflow, require_placeholders=True)
    credential_id = str(workbench_hmac_credential_id or "")
    if not _CREDENTIAL_ID.fullmatch(credential_id):
        raise N8nTemplateError("HMAC credential ID must be an opaque n8n identifier.")
    bound = copy.deepcopy(dict(workflow))
    for node in bound["nodes"]:
        credentials = node.get("credentials") or {}
        if "crypto" in credentials:
            credentials["crypto"]["id"] = credential_id
    validate_agent_workflow_template(bound, require_placeholders=False)
    return bound


def _validate_signed_http_node(node: Mapping[str, Any]) -> None:
    parameters = node.get("parameters") or {}
    if node.get("credentials"):
        raise N8nTemplateError("Workbench callback HTTP nodes must not contain an auth secret.")
    if set(parameters) != {
        "method", "url", "authentication", "sendHeaders", "headerParameters",
        "sendBody", "contentType", "rawContentType", "body", "options",
    }:
        raise N8nTemplateError("Signed Workbench callback fields were changed.")
    if parameters.get("authentication") not in {None, "none"}:
        raise N8nTemplateError("Workbench callbacks use explicit HMAC headers only.")
    if (
        parameters.get("sendBody") is not True
        or parameters.get("contentType") != "raw"
        or parameters.get("rawContentType") != "application/json"
        or parameters.get("body") != "={{$json.request_body}}"
        or parameters.get("sendHeaders") is not True
    ):
        raise N8nTemplateError("Signed Workbench callback body settings were changed.")
    header_rows = parameters.get("headerParameters", {}).get("parameters")
    if not isinstance(header_rows, list):
        raise N8nTemplateError("Signed Workbench callback headers are missing.")
    actual = {
        str(row.get("name")): str(row.get("value"))
        for row in header_rows
        if isinstance(row, dict) and set(row) == {"name", "value"}
    }
    expected = {
        "X-N8N-Timestamp": "={{$json.timestamp}}",
        "X-N8N-Nonce": "={{$json.nonce}}",
        "X-N8N-Signature": "={{$json.signature}}",
        "X-N8N-Profile": "gmail",
    }
    if actual != expected or len(header_rows) != len(expected):
        raise N8nTemplateError("Signed Workbench callback headers were changed.")
    if parameters.get("url") == GMAIL_INBOUND_URL:
        if node.get("onError") != "continueRegularOutput" or parameters.get("options") != {
            "timeout": 10000,
            "response": {
                "response": {
                    "fullResponse": True,
                    "neverError": True,
                    "responseFormat": "json",
                }
            },
        }:
            raise N8nTemplateError(
                "Inbound callbacks must expose status and connection failures to reviewed retry guards."
            )


def _reviewed_set_assignments(
    node: Mapping[str, Any], *, include_other_fields: bool
) -> dict[str, tuple[str, Any]]:
    parameters = node.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) != {
        "mode", "assignments", "includeOtherFields", "options"
    }:
        raise N8nTemplateError("Reviewed Set node fields were changed.")
    if (
        parameters.get("mode") != "manual"
        or parameters.get("includeOtherFields") is not include_other_fields
        or parameters.get("options") != {}
    ):
        raise N8nTemplateError("Reviewed Set node behavior was changed.")
    container = parameters.get("assignments")
    rows = container.get("assignments") if isinstance(container, dict) else None
    if not isinstance(rows, list) or not rows:
        raise N8nTemplateError("Reviewed Set node assignments are missing.")
    result: dict[str, tuple[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "name", "type", "value"}:
            raise N8nTemplateError("Reviewed Set assignment fields were changed.")
        if not isinstance(row.get("id"), str) or not row["id"]:
            raise N8nTemplateError("Reviewed Set assignment ID is missing.")
        name = row.get("name")
        if not isinstance(name, str) or not name or name in result:
            raise N8nTemplateError("Reviewed Set assignment names must be unique.")
        result[name] = (str(row.get("type")), row.get("value"))
    return result


def _validate_inbound_context_node(node: Mapping[str, Any]) -> None:
    assignments = _reviewed_set_assignments(node, include_other_fields=False)
    expected_simple = {
        "gmail_message_id": (
            "string", "={{String($('Gmail Search').item.json.id||'').slice(0,128)}}"
        ),
        "gmail_thread_id": (
            "string", "={{String($('Gmail Search').item.json.threadId||'').slice(0,128)}}"
        ),
        "sender": (
            "string",
            "={{String($('Gmail Search').item.json.from?.value?.[0]?.address||'').slice(0,320)}}",
        ),
        "subject": (
            "string", "={{String($('Gmail Search').item.json.subject||'').slice(0,998)}}"
        ),
        "body_text": (
            "string", "={{String($('Gmail Search').item.json.text||'').slice(0,100000)}}"
        ),
    }
    if set(assignments) != {
        *expected_simple, "workbench_label_id", "attachments", "thread_messages"
    } or any(assignments.get(name) != expected for name, expected in expected_simple.items()):
        raise N8nTemplateError("Inbound current-message context fields were changed.")

    label_type, label_expression = assignments["workbench_label_id"]
    if label_type != "string" or not isinstance(label_expression, str) or not all(
        marker in label_expression
        for marker in (
            "current=($json.messages||[]).find",
            "label.name==='Workbench-Agent'",
            "?.id||''",
        )
    ):
        raise N8nTemplateError("Inbound Gmail label lookup was changed.")

    attachment_type, attachment_expression = assignments["attachments"]
    attachment_markers = (
        "current=($json.messages||[]).find",
        "metadata.length>=50",
        "part.body?.attachmentId",
        "walk(current.payload||{})",
        "attachment_id:String(part.body.attachmentId).slice(0,128)",
        "filename:String(part.filename||'').slice(0,512)",
        "mime_type:String(part.mimeType||'application/octet-stream').slice(0,255)",
        "size_bytes:",
    )
    if (
        attachment_type != "array"
        or not isinstance(attachment_expression, str)
        or not all(marker in attachment_expression for marker in attachment_markers)
        or any(
            forbidden in attachment_expression
            for forbidden in (
                "body.data", "attachmentsBinary", "downloadAttachments",
                "content:", "path:", "url:",
            )
        )
    ):
        raise N8nTemplateError("Attachments must remain current-message metadata only.")

    thread_type, thread_expression = assignments["thread_messages"]
    thread_markers = (
        "currentBody=String($('Gmail Search').item.json.text||'').slice(0,100000)",
        "remaining=100000-currentBody.length",
        ".filter(message=>String(message.id||'')!==currentId).slice(-20).reverse()",
        ".base64Decode()",
        "bodyOf(message).slice(0,Math.max(0,remaining))",
        "remaining-=text.length",
        "gmail_message_id:String(message.id||'').slice(0,128)",
        "body_text:text",
        "return newestFirst.reverse()",
    )
    if (
        thread_type != "array"
        or not isinstance(thread_expression, str)
        or not all(marker in thread_expression for marker in thread_markers)
    ):
        raise N8nTemplateError(
            "Thread context must remain the latest twenty messages within 100,000 characters."
        )


def _validate_boolean_if(node: Mapping[str, Any], expression: str) -> None:
    parameters = node.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) != {"conditions", "options"}:
        raise N8nTemplateError("Reviewed IF node fields were changed.")
    conditions = parameters.get("conditions")
    rows = conditions.get("conditions") if isinstance(conditions, dict) else None
    expected_options = {
        "caseSensitive": True,
        "leftValue": "",
        "typeValidation": "strict",
        "version": 2,
    }
    if (
        parameters.get("options") != {}
        or not isinstance(conditions, dict)
        or set(conditions) != {"options", "conditions", "combinator"}
        or conditions.get("options") != expected_options
        or conditions.get("combinator") != "and"
        or not isinstance(rows, list)
        or len(rows) != 1
    ):
        raise N8nTemplateError("Reviewed IF condition shape was changed.")
    row = rows[0]
    if (
        not isinstance(row, dict)
        or set(row) != {"id", "leftValue", "rightValue", "operator"}
        or not isinstance(row.get("id"), str)
        or not row["id"]
        or row.get("leftValue") != expression
        or row.get("rightValue") is not True
        or row.get("operator") != {
            "type": "boolean", "operation": "true", "singleValue": True
        }
    ):
        raise N8nTemplateError("Reviewed IF expression was changed.")


def _connection_target_names(
    connections: Mapping[str, Any]
) -> dict[str, list[list[str]]]:
    return {
        str(source): [
            [str(target["node"]) for target in branch]
            for branch in outputs.get("main", [])
        ]
        for source, outputs in connections.items()
    }


def _require_direct_connection(
    connections: Mapping[str, Any], source: str, target: str
) -> None:
    outputs = connections.get(source, {}).get("main", [])
    direct = any(
        isinstance(branch, list)
        and any(isinstance(item, dict) and item.get("node") == target for item in branch)
        for branch in outputs
    )
    if not direct:
        raise N8nTemplateError(f"Required reviewed connection is missing: {source} -> {target}")


def _validate_credential(
    node: Mapping[str, Any],
    credential_type: str,
    placeholder: str,
    *,
    require_placeholders: bool,
) -> None:
    credentials = node.get("credentials")
    if not isinstance(credentials, dict) or set(credentials) != {credential_type}:
        raise N8nTemplateError("Node credential reference is missing or unreviewed.")
    reference = credentials[credential_type]
    if not isinstance(reference, dict) or set(reference) != {"id", "name"}:
        raise N8nTemplateError("Credential reference has an invalid shape.")
    credential_id = reference.get("id")
    if require_placeholders:
        if credential_id != placeholder:
            raise N8nTemplateError("Template credential placeholder was replaced.")
    elif (
        not isinstance(credential_id, str)
        or credential_id in {
            WORKBENCH_HMAC_CREDENTIAL_PLACEHOLDER,
            WORKBENCH_WEBHOOK_CREDENTIAL_PLACEHOLDER,
            GMAIL_CREDENTIAL_PLACEHOLDER,
        }
        or not _CREDENTIAL_ID.fullmatch(credential_id)
    ):
        raise N8nTemplateError("Workflow cannot activate without bound credentials.")


def _validate_connections(connections: Mapping[str, Any], names: set[str]) -> None:
    edges: dict[str, set[str]] = {name: set() for name in names}
    if not set(connections).issubset(names):
        raise N8nTemplateError("Connection source references an unknown node.")
    for source, outputs in connections.items():
        if not isinstance(outputs, dict) or set(outputs) != {"main"}:
            raise N8nTemplateError("Only reviewed main connections are allowed.")
        main = outputs["main"]
        if not isinstance(main, list):
            raise N8nTemplateError("Connection output must be a list.")
        for branch in main:
            if not isinstance(branch, list):
                raise N8nTemplateError("Connection branch must be a list.")
            for target in branch:
                if (
                    not isinstance(target, dict)
                    or set(target) != {"node", "type", "index"}
                    or target.get("type") != "main"
                    or not isinstance(target.get("index"), int)
                    or target.get("index") < 0
                    or target.get("node") not in names
                ):
                    raise N8nTemplateError("Connection target is invalid.")
                edges[str(source)].add(str(target["node"]))
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise N8nTemplateError("Workflow templates may not contain cycles.")
        if name in visited:
            return
        visiting.add(name)
        for target in edges[name]:
            visit(target)
        visiting.remove(name)
        visited.add(name)

    for name in names:
        visit(name)


def bind_workflow_credentials(
    workflow: Mapping[str, Any],
    *,
    workbench_hmac_credential_id: str,
    workbench_webhook_credential_id: str,
    gmail_credential_id: str,
    workflow_key: str,
) -> dict[str, Any]:
    """Return an inactive bound copy; never publish or mutate the template."""

    validate_workflow_template(workflow, require_placeholders=True)
    for credential_id in (
        workbench_hmac_credential_id,
        workbench_webhook_credential_id,
        gmail_credential_id,
        workflow_key,
    ):
        if not isinstance(credential_id, str) or not _CREDENTIAL_ID.fullmatch(
            credential_id
        ):
            raise N8nTemplateError("Credential IDs must be opaque n8n identifiers.")
    if workbench_hmac_credential_id == workbench_webhook_credential_id:
        raise N8nTemplateError("Inbound HMAC and outbound webhook credentials must be distinct.")
    bound = copy.deepcopy(dict(workflow))
    for node in bound["nodes"]:
        credentials = node.get("credentials") or {}
        if "httpHeaderAuth" in credentials:
            credentials["httpHeaderAuth"]["id"] = workbench_webhook_credential_id
        if "crypto" in credentials:
            credentials["crypto"]["id"] = workbench_hmac_credential_id
        if "gmailOAuth2" in credentials:
            credentials["gmailOAuth2"]["id"] = gmail_credential_id
    serialized = json.dumps(bound, ensure_ascii=False)
    serialized = serialized.replace(WORKBENCH_WORKFLOW_KEY_PLACEHOLDER, workflow_key)
    bound = json.loads(serialized)
    validate_workflow_template(bound, require_placeholders=False)
    return bound


def validate_managed_policy_file(path: Path | str | None = None) -> dict[str, Any]:
    """Cross-check the reviewable JSON policy against executable constants."""

    policy_path = Path(path) if path else REPO_ROOT / "config" / "n8n-managed.json"
    payload = load_workflow_template(policy_path)
    expected = {
        "schema_version": 1,
        "n8n_version": N8N_VERSION,
        "node_version": NODE_VERSION,
        "listen_address": N8N_HOST,
        "port": N8N_PORT,
        "data_root": "runtime/n8n-data",
        "tool_root": "runtime/tools/n8n",
        "gmail_redirect_uri": "http://localhost:5678/rest/oauth2-credential/callback",
        "workflow_templates": [
            "config/n8n-workflows/workbench-gmail-inbound-v1.json",
            "config/n8n-workflows/workbench-gmail-send-v1.json",
        ],
    }
    if payload != expected:
        raise N8nConfigurationError("n8n managed policy file does not match code.")
    return payload


def inspect_gmail_workflows_readiness(
    paths: Optional[ManagedN8nPaths] = None,
    *,
    database_path: Path | str | None = None,
) -> dict[str, Any]:
    """Read-only attestation for the two installed Gmail workflows.

    The n8n database is opened with SQLite ``mode=ro`` and ``query_only``.  No
    credential data is selected or returned: only credential IDs and types
    needed to prove correct separation are inspected.
    """

    managed = paths or ManagedN8nPaths.default()
    candidate = Path(database_path) if database_path else managed.n8n_dir / "database.sqlite"
    report: dict[str, Any] = {
        "ready": False,
        "code": "gmail_workflows_not_ready",
        "blockers": [],
        "workflows": {},
        "credential_bindings": {
            "hmac_bound": False,
            "webhook_bound": False,
            "gmail_oauth_bound": False,
            "secrets_separated": False,
        },
        "checked_at": _utc_now(),
    }
    blockers: list[str] = report["blockers"]
    if not _is_relative_to(candidate, managed.n8n_dir) or not _safe_regular_file(
        candidate, managed.n8n_dir, max_bytes=8 * 1024 * 1024 * 1024
    ):
        blockers.append("n8n_database_missing_or_unsafe")
        return report

    uri = f"file:{candidate.resolve().as_posix()}?mode=ro"
    expected = {
        "workbench-gmail-inbound-v1": "Workbench Gmail Inbound v1",
        "workbench-gmail-send-v1": "Workbench Gmail Send v1",
    }
    credential_refs: dict[str, set[str]] = {
        "crypto": set(),
        "httpHeaderAuth": set(),
        "gmailOAuth2": set(),
    }
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            placeholders = ",".join("?" for _ in expected)
            rows = connection.execute(
                f"""
                SELECT w.id, w.name, w.active, w.isArchived, w.settings,
                       w.staticData, w.pinData, w.activeVersionId,
                       COALESCE(p.publishedVersionId, w.activeVersionId)
                           AS publishedVersionId,
                       h.nodes, h.connections
                FROM workflow_entity AS w
                LEFT JOIN workflow_published_version AS p ON p.workflowId = w.id
                LEFT JOIN workflow_history AS h
                       ON h.versionId = COALESCE(
                           p.publishedVersionId, w.activeVersionId
                       )
                WHERE w.name IN ({placeholders})
                """,
                tuple(expected.values()),
            ).fetchall()
            by_name: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                by_name.setdefault(str(row["name"]), []).append(row)
            for template_id, name in expected.items():
                matches = by_name.get(name, [])
                if len(matches) != 1:
                    blockers.append(
                        f"{template_id}_missing" if not matches else f"{template_id}_duplicate"
                    )
                    report["workflows"][template_id] = {
                        "present": bool(matches), "active": False, "valid": False
                    }
                    continue
                row = matches[0]
                active = int(row["active"] or 0) == 1
                published = bool(row["publishedVersionId"]) and (
                    str(row["activeVersionId"] or "")
                    == str(row["publishedVersionId"])
                )
                archived = int(row["isArchived"] or 0) == 1
                valid = False
                reason = "validated"
                try:
                    def decode_json(value: Any, *, default: Any) -> Any:
                        if value is None or value == "":
                            return copy.deepcopy(default)
                        return json.loads(str(value))

                    workflow = {
                        "templateId": template_id,
                        "name": name,
                        # Activation is attested independently; structural
                        # validation deliberately reuses the inactive template policy.
                        "active": False,
                        "nodes": decode_json(row["nodes"], default=[]),
                        "connections": decode_json(row["connections"], default={}),
                        "settings": decode_json(row["settings"], default={}),
                        "staticData": decode_json(row["staticData"], default=None),
                        "pinData": decode_json(row["pinData"], default={}),
                    }
                    validate_workflow_template(workflow, require_placeholders=False)
                    reference_path = (
                        REPO_ROOT / "config" / "n8n-workflows" / f"{template_id}.json"
                    )
                    reference = load_workflow_template(reference_path)
                    normalized = _normalize_bound_workflow_for_reference(workflow)
                    if normalized != reference:
                        raise N8nTemplateError(
                            "Installed workflow differs from the reviewed template."
                        )
                    valid = True
                    for node in workflow["nodes"]:
                        for credential_type, reference in (node.get("credentials") or {}).items():
                            if credential_type in credential_refs:
                                credential_refs[credential_type].add(str(reference.get("id") or ""))
                except (N8nTemplateError, TypeError, ValueError, json.JSONDecodeError):
                    reason = "workflow_policy_invalid"
                    blockers.append(f"{template_id}_invalid")
                if not active or archived:
                    blockers.append(f"{template_id}_inactive")
                if not published:
                    blockers.append(f"{template_id}_not_published")
                report["workflows"][template_id] = {
                    "present": True,
                    "active": active and not archived,
                    "published": published,
                    "valid": valid,
                    "reason": reason,
                }

            crypto_ids = credential_refs["crypto"] - {""}
            webhook_ids = credential_refs["httpHeaderAuth"] - {""}
            gmail_ids = credential_refs["gmailOAuth2"] - {""}
            if len(crypto_ids) != 1:
                blockers.append("hmac_credential_binding_invalid")
            if len(webhook_ids) != 1:
                blockers.append("webhook_credential_binding_invalid")
            if len(gmail_ids) != 1:
                blockers.append("gmail_credential_binding_invalid")
            if crypto_ids and webhook_ids and crypto_ids == webhook_ids:
                blockers.append("hmac_webhook_credentials_not_separated")

            all_ids = sorted(crypto_ids | webhook_ids | gmail_ids)
            credential_types: dict[str, tuple[str, bool]] = {}
            if all_ids:
                marks = ",".join("?" for _ in all_ids)
                for row in connection.execute(
                    f"""SELECT id, type,
                               CASE WHEN data IS NOT NULL AND length(data) >= 16
                                    THEN 1 ELSE 0 END AS configured
                        FROM credentials_entity WHERE id IN ({marks})""",
                    tuple(all_ids),
                ):
                    credential_types[str(row["id"])] = (
                        str(row["type"]), int(row["configured"] or 0) == 1
                    )
            hmac_ready = len(crypto_ids) == 1 and all(
                credential_types.get(item) == ("crypto", True) for item in crypto_ids
            )
            webhook_ready = len(webhook_ids) == 1 and all(
                credential_types.get(item) == ("httpHeaderAuth", True)
                for item in webhook_ids
            )
            gmail_ready = len(gmail_ids) == 1 and all(
                credential_types.get(item) == ("gmailOAuth2", True) for item in gmail_ids
            )
            if crypto_ids and not hmac_ready:
                blockers.append("hmac_credential_missing")
            if webhook_ids and not webhook_ready:
                blockers.append("webhook_credential_missing")
            if gmail_ids and not gmail_ready:
                blockers.append("gmail_oauth_credential_missing")
            separated = (
                hmac_ready and webhook_ready and next(iter(crypto_ids)) != next(iter(webhook_ids))
            )
            report["credential_bindings"] = {
                "hmac_bound": hmac_ready,
                "webhook_bound": webhook_ready,
                "gmail_oauth_bound": gmail_ready,
                "secrets_separated": separated,
            }
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError):
        blockers.append("n8n_database_read_failed")

    report["blockers"] = list(dict.fromkeys(blockers))
    report["ready"] = not report["blockers"]
    report["code"] = "ready" if report["ready"] else "gmail_workflows_not_ready"
    return report


def _normalize_bound_workflow_for_reference(
    workflow: Mapping[str, Any]
) -> dict[str, Any]:
    """Remove only reviewed opaque binding IDs for exact template comparison."""

    normalized = copy.deepcopy(dict(workflow))
    replacements = {
        "crypto": WORKBENCH_HMAC_CREDENTIAL_PLACEHOLDER,
        "httpHeaderAuth": WORKBENCH_WEBHOOK_CREDENTIAL_PLACEHOLDER,
        "gmailOAuth2": GMAIL_CREDENTIAL_PLACEHOLDER,
    }
    for node in normalized.get("nodes", []):
        for credential_type, reference in (node.get("credentials") or {}).items():
            placeholder = replacements.get(credential_type)
            if placeholder and isinstance(reference, dict):
                reference["id"] = placeholder
        if normalized.get("templateId") == "workbench-gmail-inbound-v1":
            assignments = (
                node.get("parameters", {})
                .get("assignments", {})
                .get("assignments", [])
            )
            for assignment in assignments:
                if assignment.get("name") != "request_body":
                    continue
                value = str(assignment.get("value") or "")
                assignment["value"] = re.sub(
                    r"workflow_key:'[A-Za-z0-9_-]{1,128}'",
                    f"workflow_key:'{WORKBENCH_WORKFLOW_KEY_PLACEHOLDER}'",
                    value,
                    count=1,
                )
    return normalized


def inspect_agent_bridge_workflows_readiness(
    paths: Optional[ManagedN8nPaths] = None,
    *,
    database_path: Path | str | None = None,
) -> dict[str, Any]:
    """Read-only attestation for the protected Agent and approval bridges.

    The bridge workflows must be exact, published and unarchived.

    In n8n 2.32.5, publication and the ``active`` flag are the same state:
    unpublishing also removes ``workflow_published_version``. These reviewed
    bridges therefore remain published/active, but they contain only an
    Execute Workflow Trigger, so they cannot start autonomously.
    """

    managed = paths or ManagedN8nPaths.default()
    candidate = Path(database_path) if database_path else managed.n8n_dir / "database.sqlite"
    report: dict[str, Any] = {
        "ready": False,
        "code": "agent_bridge_workflows_not_ready",
        "blockers": [],
        "workflows": {},
        "credential_bindings": {"hmac_bound": False, "hmac_configured": False},
        "checked_at": _utc_now(),
    }
    blockers: list[str] = report["blockers"]
    if not _is_relative_to(candidate, managed.n8n_dir) or not _safe_regular_file(
        candidate, managed.n8n_dir, max_bytes=8 * 1024 * 1024 * 1024
    ):
        blockers.append("n8n_database_missing_or_unsafe")
        return report

    expected = {
        AGENT_BRIDGE_TEMPLATE_ID: AGENT_BRIDGE_WORKFLOW_NAME,
        APPROVAL_GATE_TEMPLATE_ID: APPROVAL_GATE_WORKFLOW_NAME,
    }
    credential_ids: set[str] = set()
    uri = f"file:{candidate.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            placeholders = ",".join("?" for _ in expected)
            rows = connection.execute(
                f"""
                SELECT w.id, w.name, w.active, w.isArchived, w.settings,
                       w.staticData, w.pinData, w.activeVersionId,
                       COALESCE(p.publishedVersionId, w.activeVersionId)
                           AS publishedVersionId,
                       h.nodes, h.connections
                  FROM workflow_entity AS w
             LEFT JOIN workflow_published_version AS p ON p.workflowId = w.id
             LEFT JOIN workflow_history AS h
                    ON h.versionId = COALESCE(
                        p.publishedVersionId, w.activeVersionId
                    )
                 WHERE w.name IN ({placeholders})
                """,
                tuple(expected.values()),
            ).fetchall()
            by_name: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                by_name.setdefault(str(row["name"]), []).append(row)

            def decode(value: Any, default: Any) -> Any:
                if value is None or value == "":
                    return copy.deepcopy(default)
                return json.loads(str(value))

            for template_id, name in expected.items():
                matches = by_name.get(name, [])
                if len(matches) != 1:
                    blockers.append(
                        f"{template_id}_missing" if not matches else f"{template_id}_duplicate"
                    )
                    report["workflows"][template_id] = {
                        "present": bool(matches), "active": False,
                        "published": False, "valid": False, "protected": True,
                    }
                    continue
                row = matches[0]
                active = int(row["active"] or 0) == 1
                archived = int(row["isArchived"] or 0) == 1
                # n8n 2.32.5 exposes publication as activation. The exact
                # reviewed bytes are read from the published history version.
                published = bool(row["publishedVersionId"])
                workflow_id = str(row["id"] or "").strip()
                safe_workflow_id = (
                    workflow_id
                    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workflow_id)
                    else None
                )
                valid = False
                reason = "validated"
                try:
                    workflow = {
                        "templateId": template_id,
                        "name": name,
                        "active": False,
                        "nodes": decode(row["nodes"], []),
                        "connections": decode(row["connections"], {}),
                        "settings": decode(row["settings"], {}),
                        "staticData": decode(row["staticData"], None),
                        "pinData": decode(row["pinData"], {}),
                    }
                    validate_agent_workflow_template(
                        workflow, require_placeholders=False
                    )
                    valid = True
                    for node in workflow["nodes"]:
                        reference = (node.get("credentials") or {}).get("crypto")
                        if isinstance(reference, dict):
                            credential_ids.add(str(reference.get("id") or ""))
                except (N8nTemplateError, TypeError, ValueError, json.JSONDecodeError):
                    reason = "workflow_policy_invalid"
                    blockers.append(f"{template_id}_invalid")
                if not active:
                    blockers.append(f"{template_id}_must_remain_published")
                if archived:
                    blockers.append(f"{template_id}_archived")
                if not published:
                    blockers.append(f"{template_id}_not_published")
                if safe_workflow_id is None:
                    blockers.append(f"{template_id}_workflow_id_invalid")
                report["workflows"][template_id] = {
                    "workflow_id": safe_workflow_id,
                    "present": True, "active": active, "published": published,
                    "valid": valid, "protected": True, "reason": reason,
                }

            credential_ids.discard("")
            if len(credential_ids) != 1:
                blockers.append("agent_hmac_credential_binding_invalid")
            hmac_ready = False
            if len(credential_ids) == 1:
                credential_id = next(iter(credential_ids))
                row = connection.execute(
                    """SELECT type,
                              CASE WHEN data IS NOT NULL AND length(data) >= 16
                                   THEN 1 ELSE 0 END AS configured
                         FROM credentials_entity WHERE id=?""",
                    (credential_id,),
                ).fetchone()
                hmac_ready = bool(
                    row and str(row["type"]) == "crypto" and int(row["configured"] or 0) == 1
                )
            if not hmac_ready:
                blockers.append("agent_hmac_credential_missing")
            report["credential_bindings"] = {
                "hmac_bound": len(credential_ids) == 1,
                "hmac_configured": hmac_ready,
            }
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        blockers.append("n8n_database_read_failed")

    report["blockers"] = list(dict.fromkeys(blockers))
    report["ready"] = not report["blockers"]
    report["code"] = "ready" if report["ready"] else "agent_bridge_workflows_not_ready"
    return report


def agent_bridge_workflows_ready(
    paths: Optional[ManagedN8nPaths] = None,
    *,
    database_path: Path | str | None = None,
) -> bool:
    """Fail-closed convenience predicate for the protected bridge pair."""

    try:
        return inspect_agent_bridge_workflows_readiness(
            paths, database_path=database_path
        ).get("ready") is True
    except Exception:
        return False


def gmail_workflows_ready(
    paths: Optional[ManagedN8nPaths] = None,
    *,
    database_path: Path | str | None = None,
) -> bool:
    """Fail-closed convenience predicate for status and profile enable guards."""

    try:
        return inspect_gmail_workflows_readiness(
            paths, database_path=database_path
        ).get("ready") is True
    except Exception:
        return False


__all__ = [
    "AGENT_BRIDGE_TEMPLATE_ID",
    "AGENT_BRIDGE_WORKFLOW_NAME",
    "AGENT_RUNTIME_PROFILE",
    "AGENT_TEMPLATE_NODE_TYPES",
    "AGENT_TASK_STATUS_URL",
    "AGENT_TASK_SUBMIT_URL",
    "APPROVAL_GATE_TEMPLATE_ID",
    "APPROVAL_GATE_WORKFLOW_NAME",
    "GMAIL_CREDENTIAL_PLACEHOLDER",
    "GMAIL_DRAFT_SEND_URL",
    "GMAIL_INBOUND_URL",
    "GMAIL_SEND_CLAIM_URL",
    "GMAIL_SEND_RESULT_URL",
    "GMAIL_SEND_WEBHOOK_PATH",
    "ManagedN8nLifecycle",
    "ManagedN8nPaths",
    "N8N_BASE_URL",
    "N8N_PORT",
    "N8N_SERVICE_ACCOUNT",
    "N8N_VERSION",
    "NODE_VERSION",
    "RUNTIME_APPROVAL_STATUS_URL",
    "RUNTIME_APPROVAL_SUBMIT_URL",
    "N8nConfigurationError",
    "N8nLifecycleError",
    "N8nOwnershipError",
    "N8nPortConflict",
    "N8nStartupError",
    "N8nTemplateError",
    "WORKBENCH_HMAC_CREDENTIAL_PLACEHOLDER",
    "WORKBENCH_WEBHOOK_CREDENTIAL_PLACEHOLDER",
    "WORKBENCH_WORKFLOW_KEY_PLACEHOLDER",
    "WindowsRunAsLauncher",
    "WindowsRunAsProcess",
    "agent_bridge_workflows_ready",
    "bind_agent_workflow_credential",
    "bind_workflow_credentials",
    "build_managed_environment",
    "gmail_workflows_ready",
    "inspect_port",
    "inspect_isolation",
    "inspect_agent_bridge_workflows_readiness",
    "inspect_gmail_workflows_readiness",
    "inspect_stray_user_profile",
    "load_workflow_template",
    "probe_health",
    "read_lifecycle_record",
    "validate_installation",
    "validate_agent_workflow_template",
    "validate_managed_policy_file",
    "validate_runtime_layout",
    "validate_workflow_template",
    "verify_owned_process",
]
