"""Project-scoped, read-only filesystem tools for the Hermes bridge.

This module is intentionally independent from Hermes' own ``file`` toolset.
The latter groups reads, searches, writes, and patches under one capability and
does not enforce a project-root read boundary.  Here the Workbench supplies an
authoritative project object (normally returned by ``database.get_project``),
and only two operations are exposed: ``read_file`` and ``search_files``.

The checks in this module are defense in depth, not a replacement for an
operating-system sandbox.  In particular, the production Hermes sidecar should
still run with the active project mounted read-only.  The bridge nevertheless
fails closed on path traversal, absolute paths, symbolic links and Windows
reparse points, non-regular files, secret-bearing paths, binary data, and
bounded-resource violations.
"""

from __future__ import annotations

import copy
import ctypes
import fnmatch
import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Iterator, Mapping, Optional


READ_ONLY_TOOL_NAMES = frozenset({"read_file", "search_files"})

_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_AUDIT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_SECRET_DIRECTORY_NAMES = {
    ".aws",
    ".azure",
    ".docker",
    ".git",
    ".gnupg",
    ".kube",
    ".ssh",
    "mcp-tokens",
}
_SECRET_EXACT_FILENAMES = {
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pgpass",
    ".pypirc",
    "auth.json",
    "auth.lock",
    "credentials.json",
    "credentials.toml",
    "credentials.yaml",
    "credentials.yml",
    "service-account.json",
    "service_account.json",
    "secrets.json",
    "secrets.toml",
    "secrets.yaml",
    "secrets.yml",
    "token.json",
    "tokens.json",
}
_SECRET_EXTENSIONS = {
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
}
_ENV_TEMPLATE_NAMES = {
    ".env.dist",
    ".env.example",
    ".env.sample",
    ".env.template",
}
_BINARY_EXTENSIONS = {
    ".7z",
    ".a",
    ".avi",
    ".bin",
    ".bmp",
    ".class",
    ".db",
    ".dll",
    ".dmg",
    ".doc",
    ".docx",
    ".dylib",
    ".eot",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".lockb",
    ".mov",
    ".mp3",
    ".mp4",
    ".o",
    ".otf",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".pyc",
    ".rar",
    ".so",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tif",
    ".tiff",
    ".ttf",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".xz",
    ".zip",
}

_MAX_PROJECT_PATH_CHARS = 1024
_MAX_SEARCH_PATTERN_CHARS = 512
_READ_CHUNK_BYTES = 64 * 1024
_BINARY_SAMPLE_BYTES = 8 * 1024


@dataclass(frozen=True)
class ReadOnlyToolLimits:
    """Resource ceilings for one bridge instance.

    Values may be reduced for a deployment or test, but cannot be raised above
    the audited hard ceilings in ``__post_init__``.
    """

    max_file_bytes: int = 2 * 1024 * 1024
    max_read_result_bytes: int = 100_000
    max_read_lines: int = 2_000
    max_search_results: int = 100
    max_search_offset: int = 10_000
    max_search_result_bytes: int = 100_000
    max_search_scanned_files: int = 10_000
    max_search_scanned_bytes: int = 32 * 1024 * 1024
    max_search_entries: int = 20_000
    max_search_depth: int = 32
    max_match_chars: int = 1_000

    _HARD_CEILINGS: ClassVar[dict[str, int]] = {
        "max_file_bytes": 16 * 1024 * 1024,
        "max_read_result_bytes": 256 * 1024,
        "max_read_lines": 5_000,
        "max_search_results": 500,
        "max_search_offset": 50_000,
        "max_search_result_bytes": 512 * 1024,
        "max_search_scanned_files": 50_000,
        "max_search_scanned_bytes": 256 * 1024 * 1024,
        "max_search_entries": 100_000,
        "max_search_depth": 64,
        "max_match_chars": 4_000,
    }

    def __post_init__(self) -> None:
        for name, ceiling in self._HARD_CEILINGS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value <= 0 or value > ceiling:
                raise ValueError(f"{name} must be between 1 and {ceiling}")


class HermesReadOnlyToolError(Exception):
    """A safe, typed bridge failure with JSON-serializable audit metadata."""

    def __init__(self, code: str, message: str, audit: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.audit = copy.deepcopy(dict(audit))

    def to_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {"code": self.code, "message": self.message},
            "audit": copy.deepcopy(self.audit),
        }


class HermesProjectScopeError(HermesReadOnlyToolError):
    """The supplied authoritative project cannot establish a safe root."""


class HermesReadOnlyAccessError(HermesReadOnlyToolError):
    """A request was rejected by the project read policy."""


class _PolicyViolation(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _is_link_or_reparse(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(info.st_mode) or bool(
        attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath(
            [os.path.normcase(str(path)), os.path.normcase(str(root))]
        )
    except (OSError, ValueError):
        return False
    return common == os.path.normcase(str(root))


def _is_secret_path(relative_path: str) -> bool:
    if relative_path == ".":
        return False
    parts = [part.casefold() for part in PurePosixPath(relative_path).parts]
    if any(part in _SECRET_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    name = parts[-1]
    if name in _SECRET_DIRECTORY_NAMES or name in _SECRET_EXACT_FILENAMES:
        return True
    if name.startswith(".env") and name not in _ENV_TEMPLATE_NAMES:
        return True
    if name in {"id_rsa", "id_ed25519", "id_ecdsa", "authorized_keys"}:
        return True
    return PurePosixPath(name).suffix.casefold() in _SECRET_EXTENSIONS


def _has_binary_extension(relative_path: str) -> bool:
    return PurePosixPath(relative_path).suffix.casefold() in _BINARY_EXTENSIONS


def _decode_text(data: bytes) -> str:
    sample = data[:_BINARY_SAMPLE_BYTES]
    if b"\x00" in sample:
        raise _PolicyViolation(
            "BINARY_FILE_DENIED", "Binary files cannot be read by this tool."
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _PolicyViolation(
            "BINARY_FILE_DENIED", "Only UTF-8 text files can be read by this tool."
        ) from exc
    if sample:
        control_count = sum(
            1 for character in text[:_BINARY_SAMPLE_BYTES] if ord(character) < 32
            and character not in "\t\n\r\f\b"
        )
        if control_count / max(1, len(text[:_BINARY_SAMPLE_BYTES])) > 0.02:
            raise _PolicyViolation(
                "BINARY_FILE_DENIED", "Binary files cannot be read by this tool."
            )
    return text


def _final_path_from_fd(fd: int, fallback: Path) -> Path:
    """Resolve the file actually opened, not merely the pre-open pathname."""

    if os.name == "nt":
        try:
            import msvcrt

            handle = msvcrt.get_osfhandle(fd)
            size = 32_768
            buffer = ctypes.create_unicode_buffer(size)
            result = ctypes.windll.kernel32.GetFinalPathNameByHandleW(  # type: ignore[attr-defined]
                ctypes.c_void_p(handle), buffer, size, 0
            )
            if 0 < result < size:
                value = buffer.value
                if value.startswith("\\\\?\\UNC\\"):
                    value = "\\\\" + value[8:]
                elif value.startswith("\\\\?\\"):
                    value = value[4:]
                return Path(value).resolve(strict=True)
        except (OSError, ValueError, AttributeError):
            pass
    else:
        for prefix in ("/proc/self/fd", "/dev/fd"):
            descriptor_path = Path(prefix) / str(fd)
            if descriptor_path.exists():
                try:
                    return descriptor_path.resolve(strict=True)
                except OSError:
                    pass

    current = fallback.resolve(strict=True)
    try:
        if not os.path.samestat(os.fstat(fd), current.stat()):
            raise _PolicyViolation(
                "PATH_RACE_DENIED", "The requested path changed while it was opened."
            )
    except OSError as exc:
        raise _PolicyViolation(
            "PATH_RACE_DENIED", "The requested path changed while it was opened."
        ) from exc
    return current


class HermesProjectReadOnlyTools:
    """Two-tool filesystem bridge bound to one authoritative project object."""

    def __init__(
        self,
        project: Mapping[str, Any],
        *,
        limits: Optional[ReadOnlyToolLimits] = None,
    ) -> None:
        if not isinstance(project, Mapping):
            raise self._scope_error("PROJECT_SCOPE_INVALID", "Project scope is required.")
        project_id = str(project.get("id") or "").strip()
        if not _PROJECT_ID_RE.fullmatch(project_id):
            raise self._scope_error(
                "PROJECT_SCOPE_INVALID", "The project identifier is invalid."
            )
        if project.get("archived") is True:
            raise self._scope_error(
                "PROJECT_SCOPE_INACTIVE", "The project is archived."
            )
        path_status = str(project.get("path_status") or "ready").strip().casefold()
        if path_status in {"invalid", "missing", "permission_denied"}:
            raise self._scope_error(
                "PROJECT_ROOT_UNAVAILABLE", "The project root is unavailable."
            )
        raw_root = project.get("root_path")
        if not isinstance(raw_root, (str, os.PathLike)) or not str(raw_root).strip():
            raise self._scope_error(
                "PROJECT_ROOT_INVALID", "The project root is invalid."
            )
        configured_root = Path(raw_root).expanduser()
        if not configured_root.is_absolute():
            raise self._scope_error(
                "PROJECT_ROOT_INVALID", "The project root must be absolute."
            )
        try:
            configured_info = os.lstat(configured_root)
            if _is_link_or_reparse(configured_info):
                raise self._scope_error(
                    "PROJECT_ROOT_LINK_DENIED",
                    "A linked or reparse-point project root is not permitted.",
                )
            canonical_root = configured_root.resolve(strict=True)
            root_info = canonical_root.stat()
        except HermesProjectScopeError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise self._scope_error(
                "PROJECT_ROOT_UNAVAILABLE", "The project root is unavailable."
            ) from exc
        if not stat.S_ISDIR(root_info.st_mode) or canonical_root.parent == canonical_root:
            raise self._scope_error(
                "PROJECT_ROOT_INVALID", "The project root must be a bounded directory."
            )

        self._project_id = project_id
        self._configured_root = configured_root
        self._root = canonical_root
        self._root_identity = (int(root_info.st_dev), int(root_info.st_ino))
        self._root_sha256 = _sha256_text(os.path.normcase(str(canonical_root)))
        self._limits = limits or ReadOnlyToolLimits()

    @staticmethod
    def _scope_error(code: str, message: str) -> HermesProjectScopeError:
        return HermesProjectScopeError(
            code,
            message,
            {
                "schema_version": 1,
                "event_id": uuid.uuid4().hex,
                "occurred_at": _utc_now(),
                "component": "workbench.hermes_readonly_tools",
                "decision": "deny",
                "reason": code,
            },
        )

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def tool_names(self) -> frozenset[str]:
        return READ_ONLY_TOOL_NAMES

    def tool_schemas(self) -> tuple[dict[str, Any], ...]:
        """Return fresh Hermes-style schemas for the only callable operations."""

        schemas = (
            {
                "name": "read_file",
                "description": (
                    "Read UTF-8 text from a project-relative file. Absolute paths, "
                    "links, secret files, binary files, and paths outside the active "
                    "project are denied."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Project-relative file path.",
                        },
                        "offset": {
                            "type": "integer",
                            "minimum": 1,
                            "default": 1,
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": self._limits.max_read_lines,
                            "default": min(500, self._limits.max_read_lines),
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "search_files",
                "description": (
                    "Search readable UTF-8 files within the active project. Content "
                    "patterns are literal text; file searches use a glob pattern."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {
                            "type": "string",
                            "description": "Project-relative directory or file.",
                            "default": ".",
                        },
                        "target": {
                            "type": "string",
                            "enum": ["content", "files"],
                            "default": "content",
                        },
                        "file_glob": {"type": "string"},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": self._limits.max_search_results,
                            "default": min(50, self._limits.max_search_results),
                        },
                        "offset": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": self._limits.max_search_offset,
                            "default": 0,
                        },
                        "case_sensitive": {"type": "boolean", "default": False},
                    },
                    "required": ["pattern"],
                },
            },
        )
        return copy.deepcopy(schemas)

    def invoke(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        audit_context: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Integration-facing dispatcher that always returns a safe result."""

        try:
            if not isinstance(arguments, Mapping):
                raise self._error(
                    tool_name,
                    _PolicyViolation(
                        "INVALID_ARGUMENTS", "Tool arguments must be an object."
                    ),
                    audit_context=audit_context,
                )
            supplied = dict(arguments)
            if tool_name == "read_file":
                allowed = {"path", "offset", "limit"}
                if set(supplied) - allowed:
                    raise self._error(
                        tool_name,
                        _PolicyViolation(
                            "INVALID_ARGUMENTS", "Unsupported read_file arguments."
                        ),
                        audit_context=audit_context,
                    )
                return self.read_file(
                    supplied.get("path"),
                    offset=supplied.get("offset", 1),
                    limit=supplied.get("limit", min(500, self._limits.max_read_lines)),
                    audit_context=audit_context,
                )
            if tool_name == "search_files":
                allowed = {
                    "pattern",
                    "path",
                    "target",
                    "file_glob",
                    "limit",
                    "offset",
                    "case_sensitive",
                }
                if set(supplied) - allowed:
                    raise self._error(
                        tool_name,
                        _PolicyViolation(
                            "INVALID_ARGUMENTS", "Unsupported search_files arguments."
                        ),
                        audit_context=audit_context,
                    )
                return self.search_files(
                    supplied.get("pattern"),
                    path=supplied.get("path", "."),
                    target=supplied.get("target", "content"),
                    file_glob=supplied.get("file_glob"),
                    limit=supplied.get(
                        "limit", min(50, self._limits.max_search_results)
                    ),
                    offset=supplied.get("offset", 0),
                    case_sensitive=supplied.get("case_sensitive", False),
                    audit_context=audit_context,
                )
            raise self._error(
                str(tool_name or "unknown"),
                _PolicyViolation("TOOL_NOT_ALLOWED", "The requested tool is not allowed."),
                audit_context=audit_context,
            )
        except HermesReadOnlyToolError as exc:
            return exc.to_result()
        except Exception:
            error = self._error(
                str(tool_name or "unknown"),
                _PolicyViolation(
                    "INTERNAL_ERROR", "The read-only tool failed safely."
                ),
                audit_context=audit_context,
            )
            return error.to_result()

    def read_file(
        self,
        path: Any,
        *,
        offset: Any = 1,
        limit: Any = 500,
        audit_context: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Read a bounded line range from one project-relative UTF-8 file."""

        try:
            normalized = self._normalize_relative_path(path, allow_dot=False)
            self._validate_integer(offset, "offset", minimum=1)
            self._validate_integer(
                limit,
                "limit",
                minimum=1,
                maximum=self._limits.max_read_lines,
            )
            relative, candidate, info = self._resolve_resource(normalized)
            if not stat.S_ISREG(info.st_mode):
                raise _PolicyViolation(
                    "NON_REGULAR_FILE_DENIED", "Only regular files can be read."
                )
            if _has_binary_extension(relative):
                raise _PolicyViolation(
                    "BINARY_FILE_DENIED", "Binary files cannot be read by this tool."
                )
            data = self._read_regular_bytes(candidate, self._limits.max_file_bytes)
            text = _decode_text(data)
            lines = text.splitlines()
            start_index = int(offset) - 1
            requested_lines = lines[start_index : start_index + int(limit)]
            returned_lines: list[str] = []
            returned_bytes = 0
            byte_truncated = False
            for line in requested_lines:
                separator_bytes = 1 if returned_lines else 0
                encoded_line = line.encode("utf-8")
                remaining = (
                    self._limits.max_read_result_bytes
                    - returned_bytes
                    - separator_bytes
                )
                if remaining < len(encoded_line):
                    if remaining > 0:
                        partial = encoded_line[:remaining].decode(
                            "utf-8", errors="ignore"
                        )
                        if partial or not returned_lines:
                            returned_lines.append(partial)
                            returned_bytes += separator_bytes + len(
                                partial.encode("utf-8")
                            )
                    byte_truncated = True
                    break
                returned_lines.append(line)
                returned_bytes += separator_bytes + len(encoded_line)
            content = "\n".join(returned_lines)
            returned_bytes = len(content.encode("utf-8"))
            lines_returned = len(returned_lines)
            has_more_lines = start_index + lines_returned < len(lines)
            truncated = bool(byte_truncated or has_more_lines)
            next_offset = int(offset) + lines_returned if has_more_lines else None
            audit = self._audit(
                "read_file",
                "allow",
                "ok",
                resource=relative,
                audit_context=audit_context,
                bytes_read=len(data),
                bytes_returned=returned_bytes,
                lines_returned=lines_returned,
                truncated=truncated,
                content_sha256=hashlib.sha256(data).hexdigest(),
            )
            return {
                "ok": True,
                "tool": "read_file",
                "path": relative,
                "content": content,
                "offset": int(offset),
                "lines_returned": lines_returned,
                "total_lines": len(lines),
                "truncated": truncated,
                "content_truncated": byte_truncated,
                "next_offset": next_offset,
                "audit": audit,
            }
        except HermesReadOnlyToolError:
            raise
        except _PolicyViolation as exc:
            raise self._error(
                "read_file", exc, raw_resource=path, audit_context=audit_context
            ) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise self._error(
                "read_file",
                _PolicyViolation("READ_FAILED", "The file could not be read safely."),
                raw_resource=path,
                audit_context=audit_context,
            ) from exc

    def search_files(
        self,
        pattern: Any,
        *,
        path: Any = ".",
        target: Any = "content",
        file_glob: Any = None,
        limit: Any = 50,
        offset: Any = 0,
        case_sensitive: Any = False,
        audit_context: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Search files without following links or leaving the project root."""

        try:
            query = self._normalize_search_pattern(pattern, target=target)
            normalized_target = str(target or "").strip().casefold()
            normalized_path = self._normalize_relative_path(path, allow_dot=True)
            normalized_glob = self._normalize_glob(file_glob)
            self._validate_integer(
                limit,
                "limit",
                minimum=1,
                maximum=self._limits.max_search_results,
            )
            self._validate_integer(
                offset,
                "offset",
                minimum=0,
                maximum=self._limits.max_search_offset,
            )
            if not isinstance(case_sensitive, bool):
                raise _PolicyViolation(
                    "INVALID_ARGUMENTS", "case_sensitive must be a boolean."
                )
            relative_root, start, start_info = self._resolve_resource(normalized_path)
            if not (
                stat.S_ISDIR(start_info.st_mode) or stat.S_ISREG(start_info.st_mode)
            ):
                raise _PolicyViolation(
                    "NON_REGULAR_FILE_DENIED",
                    "Search paths must be regular files or directories.",
                )
            if stat.S_ISREG(start_info.st_mode) and _has_binary_extension(relative_root):
                raise _PolicyViolation(
                    "BINARY_FILE_DENIED", "Binary files cannot be searched."
                )

            matches: list[dict[str, Any]] = []
            stats = {
                "entries_seen": 0,
                "files_seen": 0,
                "files_scanned": 0,
                "bytes_scanned": 0,
                "secret_paths_skipped": 0,
                "links_skipped": 0,
                "hardlinks_skipped": 0,
                "binary_files_skipped": 0,
                "oversized_files_skipped": 0,
                "unsafe_paths_skipped": 0,
                "budget_exhausted": False,
            }
            matched_seen = 0
            result_bytes = 0
            has_more = False
            query_for_match = query if case_sensitive else query.casefold()

            for candidate, relative, info in self._walk_files(
                start, relative_root, start_info, stats
            ):
                if _is_secret_path(relative):
                    stats["secret_paths_skipped"] += 1
                    continue
                if _has_binary_extension(relative):
                    stats["binary_files_skipped"] += 1
                    continue
                if normalized_glob and not self._glob_matches(
                    relative, normalized_glob, case_sensitive=case_sensitive
                ):
                    continue

                candidate_matches: Iterator[dict[str, Any]]
                if normalized_target == "files":
                    if not self._glob_matches(
                        relative, query, case_sensitive=case_sensitive
                    ):
                        continue
                    candidate_matches = iter(({"path": relative},))
                else:
                    size = int(info.st_size)
                    if size > self._limits.max_file_bytes:
                        stats["oversized_files_skipped"] += 1
                        continue
                    if stats["bytes_scanned"] + size > self._limits.max_search_scanned_bytes:
                        stats["budget_exhausted"] = True
                        break
                    try:
                        data = self._read_regular_bytes(
                            candidate, self._limits.max_file_bytes
                        )
                        text = _decode_text(data)
                    except _PolicyViolation as exc:
                        if exc.code == "BINARY_FILE_DENIED":
                            stats["binary_files_skipped"] += 1
                            continue
                        if exc.code in {"PATH_LINK_DENIED", "PATH_ESCAPE_DENIED"}:
                            stats["links_skipped"] += 1
                            continue
                        if exc.code == "HARDLINK_DENIED":
                            stats["hardlinks_skipped"] += 1
                            continue
                        raise
                    if (
                        stats["bytes_scanned"] + len(data)
                        > self._limits.max_search_scanned_bytes
                    ):
                        stats["budget_exhausted"] = True
                        break
                    stats["files_scanned"] += 1
                    stats["bytes_scanned"] += len(data)
                    candidate_matches = self._content_matches(
                        relative,
                        text,
                        query_for_match,
                        case_sensitive=case_sensitive,
                    )

                for item in candidate_matches:
                    if matched_seen < int(offset):
                        matched_seen += 1
                        continue
                    if len(matches) >= int(limit):
                        has_more = True
                        break
                    item_bytes = len(
                        json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode(
                            "utf-8"
                        )
                    )
                    if result_bytes + item_bytes > self._limits.max_search_result_bytes:
                        # Advance past an individual result that cannot fit so
                        # a caller cannot get stuck requesting the same page.
                        matched_seen += 1
                        has_more = True
                        break
                    matches.append(item)
                    matched_seen += 1
                    result_bytes += item_bytes
                if has_more:
                    break

            scan_truncated = bool(stats["budget_exhausted"])
            truncated = bool(has_more or scan_truncated)
            next_offset = matched_seen if truncated else None
            public_stats = {
                key: value for key, value in stats.items() if key != "budget_exhausted"
            }
            audit = self._audit(
                "search_files",
                "allow",
                "ok",
                resource=relative_root,
                audit_context=audit_context,
                query_sha256=_sha256_text(query),
                target=normalized_target,
                results_returned=len(matches),
                result_bytes=result_bytes,
                scan_truncated=scan_truncated,
                **public_stats,
            )
            return {
                "ok": True,
                "tool": "search_files",
                "path": relative_root,
                "target": normalized_target,
                "matches": matches,
                "count": len(matches),
                "offset": int(offset),
                "truncated": truncated,
                "scan_truncated": scan_truncated,
                "next_offset": next_offset,
                "stats": public_stats,
                "audit": audit,
            }
        except HermesReadOnlyToolError:
            raise
        except _PolicyViolation as exc:
            raise self._error(
                "search_files",
                exc,
                raw_resource=path,
                audit_context=audit_context,
                query_sha256=_sha256_text(str(pattern)),
            ) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise self._error(
                "search_files",
                _PolicyViolation(
                    "SEARCH_FAILED", "The project search failed safely."
                ),
                raw_resource=path,
                audit_context=audit_context,
                query_sha256=_sha256_text(str(pattern)),
            ) from exc

    def _validate_root(self) -> None:
        try:
            configured_info = os.lstat(self._configured_root)
            if _is_link_or_reparse(configured_info):
                raise _PolicyViolation(
                    "PROJECT_ROOT_CHANGED", "The project root changed during use."
                )
            current = self._configured_root.resolve(strict=True)
            info = current.stat()
        except _PolicyViolation:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise _PolicyViolation(
                "PROJECT_ROOT_UNAVAILABLE", "The project root is unavailable."
            ) from exc
        identity = (int(info.st_dev), int(info.st_ino))
        if current != self._root or identity != self._root_identity:
            raise _PolicyViolation(
                "PROJECT_ROOT_CHANGED", "The project root changed during use."
            )

    @staticmethod
    def _validate_integer(
        value: Any,
        name: str,
        *,
        minimum: int,
        maximum: Optional[int] = None,
    ) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _PolicyViolation("INVALID_ARGUMENTS", f"{name} must be an integer.")
        if value < minimum or (maximum is not None and value > maximum):
            raise _PolicyViolation("LIMIT_EXCEEDED", f"{name} is outside its safe limit.")

    @staticmethod
    def _normalize_relative_path(value: Any, *, allow_dot: bool) -> str:
        if not isinstance(value, str):
            raise _PolicyViolation(
                "INVALID_PATH", "A project-relative path is required."
            )
        raw = value.strip()
        if not raw or len(raw) > _MAX_PROJECT_PATH_CHARS or "\x00" in raw:
            raise _PolicyViolation("INVALID_PATH", "The requested path is invalid.")
        if any(ord(character) < 32 or ord(character) == 127 for character in raw):
            raise _PolicyViolation("INVALID_PATH", "The requested path is invalid.")
        normalized = raw.replace("\\", "/")
        if allow_dot and normalized == ".":
            return "."
        if (
            normalized.startswith("/")
            or normalized.startswith("~")
            or re.match(r"^[A-Za-z]:", normalized)
            or ":" in normalized
        ):
            raise _PolicyViolation(
                "ABSOLUTE_PATH_DENIED", "Only project-relative paths are permitted."
            )
        parts = normalized.split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise _PolicyViolation(
                "PATH_TRAVERSAL_DENIED", "Path traversal is not permitted."
            )
        for part in parts:
            if part.endswith((" ", ".")):
                raise _PolicyViolation("INVALID_PATH", "The requested path is invalid.")
            device_base = part.rstrip(" .").split(".", 1)[0].casefold()
            if device_base in _WINDOWS_RESERVED_NAMES:
                raise _PolicyViolation(
                    "DEVICE_PATH_DENIED", "Device paths are not permitted."
                )
        return PurePosixPath(*parts).as_posix()

    @staticmethod
    def _normalize_search_pattern(value: Any, *, target: Any) -> str:
        normalized_target = str(target or "").strip().casefold()
        if normalized_target not in {"content", "files"}:
            raise _PolicyViolation(
                "INVALID_ARGUMENTS", "Search target must be content or files."
            )
        if not isinstance(value, str):
            raise _PolicyViolation("INVALID_PATTERN", "A search pattern is required.")
        pattern = value.strip()
        if (
            not pattern
            or len(pattern) > _MAX_SEARCH_PATTERN_CHARS
            or "\x00" in pattern
            or any(ord(character) < 32 or ord(character) == 127 for character in pattern)
        ):
            raise _PolicyViolation("INVALID_PATTERN", "The search pattern is invalid.")
        if normalized_target == "files":
            return HermesProjectReadOnlyTools._normalize_glob(pattern) or "*"
        return pattern

    @staticmethod
    def _normalize_glob(value: Any) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str):
            raise _PolicyViolation("INVALID_PATTERN", "The file glob is invalid.")
        pattern = value.strip().replace("\\", "/")
        if (
            not pattern
            or len(pattern) > _MAX_SEARCH_PATTERN_CHARS
            or "\x00" in pattern
            or pattern.startswith("/")
            or re.match(r"^[A-Za-z]:", pattern)
            or any(part == ".." for part in pattern.split("/"))
            or any(ord(character) < 32 or ord(character) == 127 for character in pattern)
        ):
            raise _PolicyViolation("INVALID_PATTERN", "The file glob is invalid.")
        return pattern

    def _resolve_resource(self, relative: str) -> tuple[str, Path, os.stat_result]:
        self._validate_root()
        if _is_secret_path(relative):
            raise _PolicyViolation(
                "SECRET_PATH_DENIED", "Secret-bearing paths cannot be read."
            )
        candidate = self._root if relative == "." else self._root.joinpath(
            *PurePosixPath(relative).parts
        )
        if not _is_within(candidate, self._root):
            raise _PolicyViolation(
                "PATH_ESCAPE_DENIED", "The requested path is outside the project."
            )
        self._assert_no_link_components(candidate)
        try:
            canonical = candidate.resolve(strict=True)
            info = canonical.stat()
        except FileNotFoundError as exc:
            raise _PolicyViolation("FILE_NOT_FOUND", "The requested path was not found.") from exc
        except OSError as exc:
            raise _PolicyViolation("PATH_UNAVAILABLE", "The requested path is unavailable.") from exc
        if not _is_within(canonical, self._root):
            raise _PolicyViolation(
                "PATH_ESCAPE_DENIED", "The requested path is outside the project."
            )
        if _is_link_or_reparse(info):
            raise _PolicyViolation(
                "PATH_LINK_DENIED", "Linked paths are not permitted."
            )
        if stat.S_ISREG(info.st_mode) and int(getattr(info, "st_nlink", 1)) != 1:
            raise _PolicyViolation(
                "HARDLINK_DENIED", "Hard-linked files are not permitted."
            )
        return relative, canonical, info

    def _assert_no_link_components(self, candidate: Path) -> None:
        try:
            relative_parts = candidate.relative_to(self._root).parts
        except ValueError as exc:
            raise _PolicyViolation(
                "PATH_ESCAPE_DENIED", "The requested path is outside the project."
            ) from exc
        current = self._root
        for part in relative_parts:
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError as exc:
                raise _PolicyViolation(
                    "FILE_NOT_FOUND", "The requested path was not found."
                ) from exc
            except OSError as exc:
                raise _PolicyViolation(
                    "PATH_UNAVAILABLE", "The requested path is unavailable."
                ) from exc
            if _is_link_or_reparse(info):
                raise _PolicyViolation(
                    "PATH_LINK_DENIED", "Linked paths are not permitted."
                )

    def _read_regular_bytes(self, candidate: Path, maximum_bytes: int) -> bytes:
        self._assert_no_link_components(candidate)
        flags = os.O_RDONLY
        flags |= int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        try:
            fd = os.open(candidate, flags)
        except OSError as exc:
            raise _PolicyViolation("READ_FAILED", "The file could not be opened safely.") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _PolicyViolation(
                    "NON_REGULAR_FILE_DENIED", "Only regular files can be read."
                )
            if int(getattr(info, "st_nlink", 1)) != 1:
                raise _PolicyViolation(
                    "HARDLINK_DENIED", "Hard-linked files are not permitted."
                )
            if int(info.st_size) > maximum_bytes:
                raise _PolicyViolation(
                    "FILE_SIZE_LIMIT", "The file exceeds the safe read limit."
                )
            final_path = _final_path_from_fd(fd, candidate)
            if not _is_within(final_path, self._root):
                raise _PolicyViolation(
                    "PATH_ESCAPE_DENIED", "The opened file is outside the project."
                )
            self._assert_no_link_components(candidate)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(_READ_CHUNK_BYTES, maximum_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum_bytes:
                    raise _PolicyViolation(
                        "FILE_SIZE_LIMIT", "The file exceeds the safe read limit."
                    )
            return b"".join(chunks)
        finally:
            os.close(fd)

    def _walk_files(
        self,
        start: Path,
        relative_start: str,
        start_info: os.stat_result,
        stats: dict[str, Any],
    ) -> Iterator[tuple[Path, str, os.stat_result]]:
        if stat.S_ISREG(start_info.st_mode):
            stats["entries_seen"] += 1
            stats["files_seen"] += 1
            if int(getattr(start_info, "st_nlink", 1)) != 1:
                stats["hardlinks_skipped"] += 1
                return
            yield start, relative_start, start_info
            return

        stack: list[tuple[Path, str, int]] = [(start, relative_start, 0)]
        visited: set[tuple[int, int]] = set()
        while stack and not stats["budget_exhausted"]:
            directory, relative_directory, depth = stack.pop()
            if depth > self._limits.max_search_depth:
                stats["budget_exhausted"] = True
                break
            try:
                directory_info = directory.stat()
                identity = (int(directory_info.st_dev), int(directory_info.st_ino))
                if identity in visited:
                    continue
                visited.add(identity)
                entries = []
                with os.scandir(directory) as iterator:
                    for entry in iterator:
                        if stats["entries_seen"] >= self._limits.max_search_entries:
                            stats["budget_exhausted"] = True
                            break
                        stats["entries_seen"] += 1
                        entries.append(entry)
            except OSError:
                continue

            directories: list[tuple[Path, str, int]] = []
            for entry in sorted(entries, key=lambda item: (item.name.casefold(), item.name)):
                if not self._safe_discovered_name(entry.name):
                    stats["unsafe_paths_skipped"] += 1
                    continue
                relative = (
                    entry.name
                    if relative_directory == "."
                    else f"{relative_directory}/{entry.name}"
                )
                try:
                    # CPython's Windows DirEntry.stat() may report st_nlink=0
                    # even for an ordinary file. A direct lstat returns the
                    # real NTFS link count needed by the hard-link guard.
                    info = os.lstat(entry.path)
                except OSError:
                    continue
                if _is_link_or_reparse(info):
                    stats["links_skipped"] += 1
                    continue
                if _is_secret_path(relative):
                    stats["secret_paths_skipped"] += 1
                    continue
                if stat.S_ISDIR(info.st_mode):
                    directories.append((Path(entry.path), relative, depth + 1))
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                if int(getattr(info, "st_nlink", 1)) != 1:
                    stats["hardlinks_skipped"] += 1
                    continue
                if stats["files_seen"] >= self._limits.max_search_scanned_files:
                    stats["budget_exhausted"] = True
                    break
                stats["files_seen"] += 1
                yield Path(entry.path), relative, info
            stack.extend(reversed(directories))

    @staticmethod
    def _safe_discovered_name(name: str) -> bool:
        if (
            not name
            or len(name) > 255
            or ":" in name
            or name.endswith((" ", "."))
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
        ):
            return False
        return name.rstrip(" .").split(".", 1)[0].casefold() not in _WINDOWS_RESERVED_NAMES

    @staticmethod
    def _glob_matches(
        relative: str, pattern: str, *, case_sensitive: bool
    ) -> bool:
        normalized = relative.replace("\\", "/")
        if not case_sensitive:
            normalized = normalized.casefold()
            pattern = pattern.casefold()
        return fnmatch.fnmatchcase(normalized, pattern) or fnmatch.fnmatchcase(
            PurePosixPath(normalized).name, pattern
        )

    def _content_matches(
        self,
        relative: str,
        text: str,
        pattern: str,
        *,
        case_sensitive: bool,
    ) -> Iterator[dict[str, Any]]:
        for line_number, line in enumerate(text.splitlines(), start=1):
            candidate = line if case_sensitive else line.casefold()
            column = candidate.find(pattern)
            if column < 0:
                continue
            snippet = line[: self._limits.max_match_chars]
            yield {
                "path": relative,
                "line": line_number,
                "column": column + 1,
                "text": snippet,
                "text_truncated": len(snippet) < len(line),
            }

    def _error(
        self,
        tool: str,
        violation: _PolicyViolation,
        *,
        raw_resource: Any = None,
        audit_context: Optional[Mapping[str, Any]] = None,
        **details: Any,
    ) -> HermesReadOnlyAccessError:
        audit = self._audit(
            str(tool or "unknown"),
            "deny",
            violation.code,
            audit_context=audit_context,
            request_sha256=_sha256_text(str(raw_resource)),
            **details,
        )
        return HermesReadOnlyAccessError(violation.code, violation.message, audit)

    def _audit(
        self,
        tool: str,
        decision: str,
        reason: str,
        *,
        resource: Optional[str] = None,
        audit_context: Optional[Mapping[str, Any]] = None,
        **details: Any,
    ) -> dict[str, Any]:
        audit: dict[str, Any] = {
            "schema_version": 1,
            "event_id": uuid.uuid4().hex,
            "occurred_at": _utc_now(),
            "component": "workbench.hermes_readonly_tools",
            "tool": tool if tool in READ_ONLY_TOOL_NAMES else "unrecognized",
            "decision": decision,
            "reason": reason,
            "project_id": self._project_id,
            "project_root_sha256": self._root_sha256,
            "mode": "read_only",
        }
        if resource is not None and not _is_secret_path(resource):
            audit["resource"] = resource
        if isinstance(audit_context, Mapping):
            for key in ("request_id", "run_id", "session_id"):
                value = audit_context.get(key)
                if isinstance(value, str) and _AUDIT_ID_RE.fullmatch(value):
                    audit[key] = value
        for key, value in details.items():
            if isinstance(value, (bool, int, float, str)) or value is None:
                audit[key] = value
            elif isinstance(value, Mapping) and all(
                isinstance(item, (bool, int, float, str)) or item is None
                for item in value.values()
            ):
                audit[key] = dict(value)
        return audit


def build_project_readonly_tools(
    project: Mapping[str, Any],
    *,
    limits: Optional[ReadOnlyToolLimits] = None,
) -> HermesProjectReadOnlyTools:
    """Build a bridge from a project object obtained from the Workbench DB."""

    return HermesProjectReadOnlyTools(project, limits=limits)


__all__ = [
    "READ_ONLY_TOOL_NAMES",
    "HermesProjectReadOnlyTools",
    "HermesProjectScopeError",
    "HermesReadOnlyAccessError",
    "HermesReadOnlyToolError",
    "ReadOnlyToolLimits",
    "build_project_readonly_tools",
]
