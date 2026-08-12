"""Bounded, project-scoped Git inspection for the floating inspector.

The project row supplied by the database is the only authority for the
workspace root.  Callers never supply a cwd, repository root, or absolute file
path.  Git is executed with argv (never a shell), a short timeout, bounded
output, and configuration that disables external diff/text-conversion hooks.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional

import structured_log
from subprocess_env import agent_subprocess_env


MAX_GIT_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_DIFF_BYTES = 160 * 1024
MAX_STATUS_FILES = 2_000
GIT_TIMEOUT_SECONDS = 8.0

_SECRET_DIRECTORIES = {
    ".aws", ".azure", ".docker", ".git", ".gnupg", ".kube", ".ssh",
    "mcp-tokens",
}
_SECRET_FILES = {
    ".git-credentials", ".netrc", ".npmrc", ".pgpass", ".pypirc",
    "auth.json", "auth.lock", "credentials.json", "credentials.toml",
    "credentials.yaml", "credentials.yml", "service-account.json",
    "service_account.json", "secrets.json", "secrets.toml", "secrets.yaml",
    "secrets.yml", "token.json", "token.toml", "token.yaml", "token.yml",
    "tokens.json", "tokens.toml", "tokens.yaml", "tokens.yml",
}
_SECRET_EXTENSIONS = {".jks", ".key", ".keystore", ".p12", ".pem", ".pfx"}
_ENV_TEMPLATES = {".env.dist", ".env.example", ".env.sample", ".env.template"}
_SECRET_CONFIG_STEMS = {
    "auth", "credential", "credentials", "key", "keys", "private-key",
    "private_key", "secret", "secrets", "token", "tokens",
}
_SECRET_CONFIG_EXTENSIONS = {"", ".conf", ".config", ".ini", ".json", ".toml", ".yaml", ".yml"}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}\b", re.IGNORECASE),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|password|secret)"
        r"(\s*[:=]\s*)([^\s,;]{6,})"
    ),
)
_WINDOWS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:\\|\\\\)[^\r\n\t\"'<>|]+"
)
_POSIX_PRIVATE_PATH_RE = re.compile(
    r"(?<![:A-Za-z0-9_])/(?:Users|home|root|tmp|var|etc|mnt|media|workspace)"
    r"(?:/[^\s\"'<>|]+)+"
)


class ProjectVcsError(Exception):
    """Safe typed failure that can be translated to an API response."""

    def __init__(self, code: str, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.not_found = not_found


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    overflowed: bool = False


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


def _unresolved_absolute(path: Path) -> Path:
    """Normalize lexical components without resolving filesystem links."""

    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or not absolute.anchor:
        raise OSError("path is not absolute")
    return absolute


def _chain_contains_link_or_reparse(
    path: Path,
    *,
    allow_missing_tail: bool = False,
) -> bool:
    """Inspect every existing unresolved path component, including the leaf."""

    absolute = _unresolved_absolute(path)
    current = Path(absolute.anchor)
    components = [current]
    for part in absolute.parts[1:]:
        current = current / part
        components.append(current)
    for component in components:
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            if allow_missing_tail:
                return False
            raise
        if _is_link_or_reparse(info):
            return True
    return False


def _project_root(project: Mapping[str, Any]) -> Path:
    if project.get("archived") is True:
        raise ProjectVcsError("PROJECT_INACTIVE", "Project is archived.")
    path_status = str(project.get("path_status") or "ready").strip().casefold()
    if path_status in {"invalid", "missing", "permission_denied"}:
        raise ProjectVcsError("PROJECT_ROOT_UNAVAILABLE", "Project root is unavailable.")
    raw = project.get("root_path")
    if not isinstance(raw, (str, os.PathLike)) or not str(raw).strip():
        raise ProjectVcsError("PROJECT_ROOT_INVALID", "Project root is invalid.")
    configured = Path(raw).expanduser()
    if not configured.is_absolute():
        raise ProjectVcsError("PROJECT_ROOT_INVALID", "Project root must be absolute.")
    if any(part == ".." for part in configured.parts):
        raise ProjectVcsError("PROJECT_ROOT_INVALID", "Project root is invalid.")
    try:
        configured = _unresolved_absolute(configured)
        if _chain_contains_link_or_reparse(configured):
            raise ProjectVcsError(
                "PROJECT_ROOT_LINK_DENIED",
                "Project roots with linked or reparse-point path components cannot be inspected.",
            )
        root = configured.resolve(strict=True)
    except ProjectVcsError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ProjectVcsError(
            "PROJECT_ROOT_UNAVAILABLE", "Project root is unavailable."
        ) from exc
    if not root.is_dir():
        raise ProjectVcsError("PROJECT_ROOT_INVALID", "Project root is not a directory.")
    return root


def normalize_relative_path(value: Any) -> str:
    """Return a portable relative path or fail closed.

    This is used both for paths reported by Git and for a later diff request.
    A detailed diff is permitted only when this value also appears in a fresh
    status snapshot.
    """

    raw = str(value or "").replace("\\", "/")
    if raw.startswith("/"):
        raise ProjectVcsError("VCS_PATH_NOT_FOUND", "Changed file was not found.", not_found=True)
    text = raw.rstrip("/")
    raw_parts = text.split("/")
    if (
        not text
        or len(text) > 1_024
        or _CONTROL_RE.search(text)
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise ProjectVcsError("VCS_PATH_NOT_FOUND", "Changed file was not found.", not_found=True)
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProjectVcsError("VCS_PATH_NOT_FOUND", "Changed file was not found.", not_found=True)
    if any(":" in part for part in path.parts):
        raise ProjectVcsError("VCS_PATH_NOT_FOUND", "Changed file was not found.", not_found=True)
    return path.as_posix()


def is_secret_relative_path(relative_path: str) -> bool:
    parts = [part.casefold() for part in PurePosixPath(relative_path).parts]
    if not parts:
        return True
    if any(part in _SECRET_DIRECTORIES for part in parts[:-1]):
        return True
    name = parts[-1]
    if name in _SECRET_DIRECTORIES or name in _SECRET_FILES:
        return True
    if name.startswith(".env") and name not in _ENV_TEMPLATES:
        return True
    suffix = PurePosixPath(name).suffix.casefold()
    stem = name[: -len(suffix)] if suffix else name
    if stem in _SECRET_CONFIG_STEMS and suffix in _SECRET_CONFIG_EXTENSIONS:
        return True
    return suffix in _SECRET_EXTENSIONS


def redact_public_text(value: Any, *, max_chars: int = MAX_DIFF_BYTES) -> tuple[str, int]:
    """Redact common credentials and private absolute filesystem paths."""

    text = str(value or "")[: max(0, int(max_chars))]
    count = 0
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)"):
            text, found = pattern.subn(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", text)
        else:
            text, found = pattern.subn("[redacted]", text)
        count += found
    text, found = _WINDOWS_PATH_RE.subn("[path-redacted]", text)
    count += found
    text, found = _POSIX_PRIVATE_PATH_RE.subn("[path-redacted]", text)
    count += found
    # Structured logging owns the live literal-secret registry (provider keys,
    # local session tokens, and other values learned at runtime).  Its internal
    # text primitive is used deliberately here because the public object helper
    # caps strings at 4 KiB, while a bounded diff may safely be larger.
    runtime_redactor = getattr(structured_log, "_redact_text", None)
    if callable(runtime_redactor):
        before = text
        text = str(runtime_redactor(text))
        if text != before:
            count += 1
    return text, count


def _clip_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _git_environment() -> dict[str, str]:
    environment = agent_subprocess_env()
    for name in (
        "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_DIR", "GIT_WORK_TREE",
        "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT",
    ):
        environment.pop(name, None)
    for name in list(environment):
        if name.startswith("GIT_CONFIG_KEY_") or name.startswith("GIT_CONFIG_VALUE_"):
            environment.pop(name, None)
    # Keep normal Git configuration semantics such as core.autocrlf.  Changing
    # those for inspection can make a clean Windows worktree appear dirty.
    # Commands still override executable features (pager/fsmonitor/ext-diff).
    environment.update(
        {"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "LC_ALL": "C"}
    )
    return environment


def _run_git(
    root: Path,
    arguments: list[str],
    *,
    max_output_bytes: int = MAX_GIT_OUTPUT_BYTES,
    timeout: float = GIT_TIMEOUT_SECONDS,
    allow_truncated: bool = False,
) -> _CommandResult:
    argv = [
        "git", "-c", "core.fsmonitor=false", "-c", "core.pager=cat",
        "-C", str(root), *arguments,
    ]
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=_git_environment(),
            creationflags=creationflags,
        )
    except (OSError, ValueError) as exc:
        raise ProjectVcsError("GIT_UNAVAILABLE", "Git is unavailable.") from exc

    output: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()

    def _drain(name: str, stream: Any) -> None:
        try:
            while True:
                chunk = stream.read(16 * 1024)
                if not chunk:
                    break
                target = output[name]
                remaining = max_output_bytes - len(target)
                if remaining > 0:
                    target.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    threads = [
        threading.Thread(target=_drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=_drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        returncode = process.wait(timeout=max(0.1, float(timeout)))
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait(timeout=2)
        raise ProjectVcsError("GIT_TIMEOUT", "Git inspection timed out.") from exc
    finally:
        for thread in threads:
            thread.join(timeout=2)
    if overflow.is_set() and not allow_truncated:
        raise ProjectVcsError("GIT_OUTPUT_LIMIT", "Git inspection exceeded its output limit.")
    return _CommandResult(
        returncode,
        bytes(output["stdout"]),
        bytes(output["stderr"]),
        overflow.is_set(),
    )


def _decode(result: _CommandResult) -> str:
    return result.stdout.decode("utf-8", errors="replace").strip()


def _assert_repository_root(root: Path) -> None:
    result = _run_git(root, ["rev-parse", "--show-toplevel"], max_output_bytes=8_192)
    if result.returncode != 0:
        raise ProjectVcsError("NOT_GIT_REPOSITORY", "Project is not a Git repository.")
    reported = Path(_decode(result)).resolve(strict=False)
    if os.path.normcase(str(reported)) != os.path.normcase(str(root)):
        # A project nested inside some other repository would allow Git to
        # observe sibling paths outside the authorized project root.
        raise ProjectVcsError(
            "GIT_ROOT_OUTSIDE_PROJECT",
            "Project root must also be the Git repository root.",
        )


def _status_entries(root: Path) -> tuple[list[dict[str, Any]], int]:
    result = _run_git(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    if result.returncode != 0:
        raise ProjectVcsError("GIT_STATUS_FAILED", "Git status is unavailable.")
    tokens = result.stdout.split(b"\0")
    entries: list[dict[str, Any]] = []
    redacted_count = 0
    index = 0
    while index < len(tokens):
        raw = tokens[index]
        index += 1
        if not raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        if len(text) < 4 or text[2] != " ":
            continue
        code = text[:2]
        try:
            path = normalize_relative_path(text[3:])
        except ProjectVcsError:
            redacted_count += 1
            continue
        old_path: Optional[str] = None
        if "R" in code or "C" in code:
            if index < len(tokens):
                raw_old = tokens[index]
                index += 1
                try:
                    old_path = normalize_relative_path(
                        raw_old.decode("utf-8", errors="replace")
                    )
                except ProjectVcsError:
                    old_path = None
        if is_secret_relative_path(path) or (old_path and is_secret_relative_path(old_path)):
            redacted_count += 1
            continue
        if len(entries) >= MAX_STATUS_FILES:
            redacted_count += 1
            continue
        entries.append(
            {
                "path": path,
                "old_path": old_path,
                "index_status": code[0],
                "worktree_status": code[1],
                "staged": code[0] not in {" ", "?", "!"},
                "unstaged": code[1] not in {" ", "!"} or code == "??",
                "untracked": code == "??",
                "conflicted": "U" in code or code in {"AA", "DD"},
            }
        )
    return entries, redacted_count


def _optional_git_text(root: Path, arguments: list[str]) -> Optional[str]:
    result = _run_git(root, arguments, max_output_bytes=16_384)
    return _decode(result) if result.returncode == 0 and _decode(result) else None


def inspect_project_vcs(project: Mapping[str, Any]) -> dict[str, Any]:
    """Return current workspace state without attributing it to a run."""

    root = _project_root(project)
    _assert_repository_root(root)
    entries, redacted_count = _status_entries(root)
    branch = _optional_git_text(root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    commit = _optional_git_text(root, ["rev-parse", "--verify", "HEAD"])
    upstream = _optional_git_text(
        root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]
    )
    ahead = behind = None
    if upstream:
        result = _run_git(root, ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], max_output_bytes=512)
        if result.returncode == 0:
            pieces = _decode(result).split()
            if len(pieces) == 2 and all(piece.isdigit() for piece in pieces):
                ahead, behind = int(pieces[0]), int(pieces[1])
    return {
        "available": True,
        "repository": True,
        "branch": branch,
        "detached": branch is None and commit is not None,
        "commit": commit,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "synced": bool(upstream and ahead == 0 and behind == 0),
        "dirty": bool(entries),
        "changes": entries,
        "change_count": len(entries),
        "redacted_change_count": redacted_count,
        # Workspace synchronization is not evidence that this run pushed.
        "pushed_this_run": None,
        "scope": "workspace",
    }


def unavailable_vcs(error: ProjectVcsError) -> dict[str, Any]:
    return {
        "available": False,
        "repository": False,
        "reason": error.code.casefold(),
        "changes": [],
        "change_count": 0,
        "redacted_change_count": 0,
        "pushed_this_run": None,
        "scope": "workspace",
    }


def _bounded_diff(root: Path, arguments: list[str]) -> tuple[str, bool, int]:
    result = _run_git(
        root,
        arguments,
        max_output_bytes=MAX_DIFF_BYTES + 1,
        allow_truncated=True,
    )
    # git diff returns 0 for a normal patch; --no-index may return 1 when files differ.
    if result.returncode not in {0, 1} and not result.overflowed:
        raise ProjectVcsError("GIT_DIFF_FAILED", "Git diff is unavailable.")
    original_bytes = len(result.stdout)
    truncated = result.overflowed or original_bytes > MAX_DIFF_BYTES
    raw = result.stdout[:MAX_DIFF_BYTES].decode("utf-8", errors="replace")
    public, redactions = redact_public_text(raw, max_chars=MAX_DIFF_BYTES)
    public, public_clipped = _clip_utf8(public, MAX_DIFF_BYTES)
    truncated = truncated or public_clipped
    return public, truncated, redactions


def inspect_project_diff(project: Mapping[str, Any], requested_path: str) -> dict[str, Any]:
    """Return a bounded patch for one path in a fresh Git status snapshot."""

    root = _project_root(project)
    _assert_repository_root(root)
    requested = normalize_relative_path(requested_path)
    entries, _ = _status_entries(root)
    entry = next((item for item in entries if item["path"] == requested), None)
    if entry is None:
        raise ProjectVcsError("VCS_PATH_NOT_FOUND", "Changed file was not found.", not_found=True)

    raw_candidate = root / Path(*PurePosixPath(requested).parts)
    try:
        if _chain_contains_link_or_reparse(
            raw_candidate,
            allow_missing_tail=True,
        ):
            raise ProjectVcsError(
                "VCS_PATH_NOT_FOUND",
                "Changed file was not found.",
                not_found=True,
            )
        candidate = raw_candidate.resolve(strict=False)
    except ProjectVcsError:
        raise
    except (OSError, RuntimeError):
        raise ProjectVcsError(
            "VCS_PATH_NOT_FOUND",
            "Changed file was not found.",
            not_found=True,
        ) from None
    if not _is_within(candidate, root) or is_secret_relative_path(requested):
        raise ProjectVcsError("VCS_PATH_NOT_FOUND", "Changed file was not found.", not_found=True)

    sections: list[dict[str, Any]] = []
    total_redactions = 0
    truncated = False
    if entry["staged"]:
        patch, clipped, redactions = _bounded_diff(
            root,
            ["diff", "--cached", "--no-ext-diff", "--no-textconv", "--unified=3", "--", requested],
        )
        if patch:
            sections.append({"kind": "staged", "diff": patch})
        truncated = truncated or clipped
        total_redactions += redactions
    if entry["unstaged"] and not entry["untracked"]:
        patch, clipped, redactions = _bounded_diff(
            root,
            ["diff", "--no-ext-diff", "--no-textconv", "--unified=3", "--", requested],
        )
        if patch:
            sections.append({"kind": "unstaged", "diff": patch})
        truncated = truncated or clipped
        total_redactions += redactions
    if entry["untracked"]:
        try:
            info = os.lstat(raw_candidate)
            if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
                raise OSError("not a regular file")
            data = raw_candidate.read_bytes()[: MAX_DIFF_BYTES + 1]
            if b"\x00" in data[:8_192]:
                raise UnicodeError("binary")
            content = data[:MAX_DIFF_BYTES].decode("utf-8")
            public, redactions = redact_public_text(content, max_chars=MAX_DIFF_BYTES)
            quoted = "\n".join(f"+{line}" for line in public.splitlines())
            patch = (
                f"diff --git a/{requested} b/{requested}\n"
                "new file\n--- /dev/null\n"
                f"+++ b/{requested}\n@@ -0,0 +1,{len(public.splitlines())} @@\n{quoted}\n"
            )
            patch, patch_clipped = _clip_utf8(patch, MAX_DIFF_BYTES)
            sections.append({"kind": "untracked", "diff": patch})
            truncated = truncated or len(data) > MAX_DIFF_BYTES or patch_clipped
            total_redactions += redactions
        except (OSError, UnicodeError, UnicodeDecodeError):
            sections.append({"kind": "untracked", "diff": None, "reason": "preview_unavailable"})

    patches = [
        section["diff"] for section in sections if isinstance(section.get("diff"), str)
    ]
    combined, combined_clipped = _clip_utf8("\n".join(patches), MAX_DIFF_BYTES)
    truncated = truncated or combined_clipped
    section_summaries = [
        {
            "kind": section["kind"],
            "available": isinstance(section.get("diff"), str),
            "reason": section.get("reason"),
            "characters": len(section.get("diff") or ""),
        }
        for section in sections
    ]
    return {
        "path": requested,
        "status": entry,
        "diff": combined or None,
        "sections": section_summaries,
        "truncated": truncated,
        "redactions": total_redactions,
        "max_bytes": MAX_DIFF_BYTES,
    }
