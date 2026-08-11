"""Workbench-owned, fail-closed Hermes project read-only tools.

This module is injected through a read-only ``PYTHONPATH`` bind mount into the
digest-pinned Hermes container.  It deliberately registers two new tool names
instead of enabling Hermes' stock ``file`` toolset, because that stock bundle
also contains write and patch operations.

The Docker launcher and live attestation are the primary security boundary:
only one project is mounted at ``/workspace/project`` and that mount is read
only.  The checks below are a second, independent containment layer.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


POLICY_PROFILE = "project-readonly-v1"
PROJECT_ROOT = "/workspace/project"
TOOLSET_NAME = "workbench-readonly"
MAX_PATH_CHARS = 4096
MAX_FILE_BYTES = 1_048_576
MAX_READ_LINES = 400
MAX_READ_RESULT_CHARS = 65_536
MAX_SEARCH_PATTERN_CHARS = 256
MAX_SEARCH_RESULTS = 50
MAX_SEARCH_FILES = 2_000
MAX_SEARCH_BYTES = 16 * 1_048_576
MAX_SNIPPET_CHARS = 400

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DENIED_COMPONENTS = {
    ".aws",
    ".azure",
    ".git",
    ".gnupg",
    ".ssh",
    ".terraform",
    "credentials",
    "secrets",
}
_DENIED_FILENAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    "auth.json",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
}
_DENIED_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}


class PolicyDenied(ValueError):
    """A stable policy denial safe to return to the model."""


def _result(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _error(code: str) -> str:
    return _result({"ok": False, "error": code})


def _root() -> Path:
    if os.environ.get("WORKBENCH_POLICY_PROFILE") != POLICY_PROFILE:
        raise PolicyDenied("policy_profile_invalid")
    if os.environ.get("WORKBENCH_PROJECT_ROOT") != PROJECT_ROOT:
        raise PolicyDenied("project_root_invalid")
    root = Path(PROJECT_ROOT)
    if root.is_symlink() or not root.is_dir():
        raise PolicyDenied("project_root_invalid")
    resolved = root.resolve(strict=True)
    if resolved != root:
        raise PolicyDenied("project_root_invalid")
    return resolved


def _is_denied(relative: PurePosixPath) -> bool:
    folded = [part.casefold() for part in relative.parts]
    if any(part in _DENIED_COMPONENTS for part in folded):
        return True
    filename = folded[-1] if folded else ""
    if filename in _DENIED_FILENAMES or filename.startswith(".env."):
        return True
    return any(filename.endswith(suffix) for suffix in _DENIED_SUFFIXES)


def _relative_path(value: Any, *, allow_root: bool) -> PurePosixPath:
    if not isinstance(value, str):
        raise PolicyDenied("path_invalid")
    text = value.strip()
    if not text or len(text) > MAX_PATH_CHARS or _CONTROL_RE.search(text):
        raise PolicyDenied("path_invalid")
    if "\\" in text or text.startswith("~"):
        raise PolicyDenied("path_invalid")
    relative = PurePosixPath(text)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise PolicyDenied("path_outside_project")
    normalized = PurePosixPath(*[part for part in relative.parts if part not in {"", "."}])
    if not normalized.parts:
        if allow_root:
            return PurePosixPath(".")
        raise PolicyDenied("path_invalid")
    if _is_denied(normalized):
        raise PolicyDenied("sensitive_path_denied")
    return normalized


def _resolve(value: Any, *, allow_directory: bool = False) -> tuple[Path, PurePosixPath]:
    relative = _relative_path(value, allow_root=allow_directory)
    root = _root()
    candidate = root if str(relative) == "." else root.joinpath(*relative.parts)

    current = root
    for component in (() if candidate == root else relative.parts):
        current = current / component
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise PolicyDenied("path_not_found") from exc
        if stat.S_ISLNK(mode):
            raise PolicyDenied("link_path_denied")

    try:
        resolved = candidate.resolve(strict=True)
        if os.path.commonpath((str(root), str(resolved))) != str(root):
            raise PolicyDenied("path_outside_project")
    except PolicyDenied:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise PolicyDenied("path_invalid") from exc
    try:
        resolved_stat = resolved.lstat()
    except OSError as exc:
        raise PolicyDenied("path_not_found") from exc
    if stat.S_ISLNK(resolved_stat.st_mode):
        raise PolicyDenied("link_path_denied")
    if stat.S_ISREG(resolved_stat.st_mode) and resolved_stat.st_nlink != 1:
        # A hard link keeps the lexical path inside the project while sharing
        # an inode with a file outside it.  Canonical-path containment cannot
        # detect that alias, so fail closed for every multiply-linked file.
        raise PolicyDenied("hardlink_path_denied")
    if allow_directory:
        if not (stat.S_ISDIR(resolved_stat.st_mode) or stat.S_ISREG(resolved_stat.st_mode)):
            raise PolicyDenied("unsupported_path_type")
    elif not stat.S_ISREG(resolved_stat.st_mode):
        raise PolicyDenied("not_a_regular_file")
    return resolved, relative


def _read_utf8(path: Path) -> str:
    try:
        size = path.stat().st_size
        if size < 0 or size > MAX_FILE_BYTES:
            raise PolicyDenied("file_too_large")
        data = path.read_bytes()
    except PolicyDenied:
        raise
    except OSError as exc:
        raise PolicyDenied("read_failed") from exc
    if b"\x00" in data:
        raise PolicyDenied("binary_file_denied")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyDenied("non_utf8_file_denied") from exc


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int, code: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise PolicyDenied(code)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise PolicyDenied(code) from exc
    if result < minimum or result > maximum:
        raise PolicyDenied(code)
    return result


def project_read_file(args: dict[str, Any], **_: Any) -> str:
    try:
        if not isinstance(args, dict):
            raise PolicyDenied("arguments_invalid")
        path, relative = _resolve(args.get("path"), allow_directory=False)
        offset = _bounded_int(
            args.get("offset"), default=1, minimum=1, maximum=10_000_000, code="offset_invalid"
        )
        limit = _bounded_int(
            args.get("limit"), default=200, minimum=1, maximum=MAX_READ_LINES, code="limit_invalid"
        )
        lines = _read_utf8(path).splitlines()
        start = offset - 1
        selected: list[dict[str, Any]] = []
        used = 0
        for index, line in enumerate(lines[start : start + limit], start=offset):
            remaining = MAX_READ_RESULT_CHARS - used
            if remaining <= 0:
                break
            clipped = line[:remaining]
            selected.append({"line": index, "text": clipped})
            used += len(clipped)
        consumed = len(selected)
        next_offset = offset + consumed if start + consumed < len(lines) else None
        return _result(
            {
                "ok": True,
                "path": relative.as_posix(),
                "lines": selected,
                "next_offset": next_offset,
                "truncated": next_offset is not None,
            }
        )
    except PolicyDenied as exc:
        return _error(str(exc))


def _iter_files(base: Path) -> Iterable[Path]:
    if base.is_file():
        yield base
        return
    for current, directory_names, file_names in os.walk(base, followlinks=False):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            entry = current_path / name
            relative = PurePosixPath(entry.relative_to(_root()).as_posix())
            try:
                if entry.is_symlink() or _is_denied(relative):
                    continue
            except OSError:
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in sorted(file_names):
            entry = current_path / name
            relative = PurePosixPath(entry.relative_to(_root()).as_posix())
            try:
                entry_stat = entry.lstat()
                if (
                    stat.S_ISLNK(entry_stat.st_mode)
                    or not stat.S_ISREG(entry_stat.st_mode)
                    or entry_stat.st_nlink != 1
                    or _is_denied(relative)
                ):
                    continue
            except OSError:
                continue
            yield entry


def project_search_files(args: dict[str, Any], **_: Any) -> str:
    try:
        if not isinstance(args, dict):
            raise PolicyDenied("arguments_invalid")
        raw_pattern = args.get("pattern")
        if (
            not isinstance(raw_pattern, str)
            or not raw_pattern
            or len(raw_pattern) > MAX_SEARCH_PATTERN_CHARS
            or _CONTROL_RE.search(raw_pattern)
        ):
            raise PolicyDenied("pattern_invalid")
        mode = str(args.get("mode") or "content").strip().casefold()
        if mode not in {"content", "files"}:
            raise PolicyDenied("mode_invalid")
        raw_glob = args.get("file_glob") or "*"
        if (
            not isinstance(raw_glob, str)
            or not raw_glob
            or len(raw_glob) > 128
            or _CONTROL_RE.search(raw_glob)
        ):
            raise PolicyDenied("file_glob_invalid")
        limit = _bounded_int(
            args.get("limit"), default=20, minimum=1, maximum=MAX_SEARCH_RESULTS, code="limit_invalid"
        )
        offset = _bounded_int(
            args.get("offset"), default=0, minimum=0, maximum=10_000, code="offset_invalid"
        )
        base, _ = _resolve(args.get("path") or ".", allow_directory=True)
        root = _root()
        needle = raw_pattern.casefold()
        results: list[dict[str, Any]] = []
        matched = 0
        scanned_files = 0
        scanned_bytes = 0
        exhausted = False
        for file_path in _iter_files(base):
            scanned_files += 1
            if scanned_files > MAX_SEARCH_FILES:
                exhausted = True
                break
            relative = file_path.relative_to(root).as_posix()
            if not fnmatch.fnmatch(relative.casefold(), raw_glob.casefold()) and not fnmatch.fnmatch(
                file_path.name.casefold(), raw_glob.casefold()
            ):
                continue
            if mode == "files":
                if needle not in relative.casefold():
                    continue
                if matched >= offset:
                    results.append({"path": relative})
                matched += 1
                if len(results) >= limit:
                    break
                continue

            try:
                size = file_path.stat().st_size
            except OSError:
                continue
            if size < 0 or size > MAX_FILE_BYTES:
                continue
            scanned_bytes += size
            if scanned_bytes > MAX_SEARCH_BYTES:
                exhausted = True
                break
            try:
                text = _read_utf8(file_path)
            except PolicyDenied:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if needle not in line.casefold():
                    continue
                if matched >= offset:
                    results.append(
                        {"path": relative, "line": line_number, "text": line[:MAX_SNIPPET_CHARS]}
                    )
                matched += 1
                if len(results) >= limit:
                    break
            if len(results) >= limit:
                break
        return _result(
            {
                "ok": True,
                "mode": mode,
                "results": results,
                "next_offset": offset + len(results) if len(results) == limit else None,
                "scanned_files": min(scanned_files, MAX_SEARCH_FILES),
                "scan_limit_reached": exhausted,
            }
        )
    except PolicyDenied as exc:
        return _error(str(exc))


READ_SCHEMA = {
    "name": "project_read_file",
    "description": (
        "Read a UTF-8 text file from the current Workbench project. The path must be "
        "relative to the project root. This tool is read-only and cannot access secrets, "
        "links, files outside the project, or files larger than 1 MiB."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Project-relative file path"},
            "offset": {"type": "integer", "minimum": 1, "default": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_READ_LINES, "default": 200},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}

SEARCH_SCHEMA = {
    "name": "project_search_files",
    "description": (
        "Search UTF-8 text or file names inside the current Workbench project using a "
        "case-insensitive literal pattern. This tool is read-only and never follows links."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Literal text or filename fragment"},
            "mode": {"type": "string", "enum": ["content", "files"], "default": "content"},
            "path": {"type": "string", "default": ".", "description": "Project-relative file or directory"},
            "file_glob": {"type": "string", "default": "*"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS, "default": 20},
            "offset": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 0},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
}


def _activate() -> None:
    # Fail during import if the mount/profile is malformed. Python reports a
    # sitecustomize error and continues, but the launcher then requires the
    # exact custom toolset from /v1/toolsets and tears the container down.
    _root()
    from tools.registry import registry
    from toolsets import create_custom_toolset

    registry.register(
        name="project_read_file",
        toolset=TOOLSET_NAME,
        schema=READ_SCHEMA,
        handler=project_read_file,
        emoji="read",
        max_result_size_chars=MAX_READ_RESULT_CHARS,
    )
    registry.register(
        name="project_search_files",
        toolset=TOOLSET_NAME,
        schema=SEARCH_SCHEMA,
        handler=project_search_files,
        emoji="search",
        max_result_size_chars=MAX_READ_RESULT_CHARS,
    )
    create_custom_toolset(
        TOOLSET_NAME,
        "Workbench current-project read-only file access",
        tools=["project_read_file", "project_search_files"],
    )

    # The API endpoint enumerates configurable toolsets rather than every
    # runtime custom toolset, so add this one deterministic entry for live
    # attestation. No UI configurator is exposed by Workbench.
    from hermes_cli import tools_config

    entry = (TOOLSET_NAME, "Workbench Project Read Only", "project_read_file, project_search_files")
    if entry not in tools_config.CONFIGURABLE_TOOLSETS:
        tools_config.CONFIGURABLE_TOOLSETS.append(entry)


if os.environ.get("WORKBENCH_POLICY_PROFILE") == POLICY_PROFILE:
    _activate()
