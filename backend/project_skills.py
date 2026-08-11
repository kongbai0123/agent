"""Project-owned, data-only instruction skills.

Every skill is stored below its owning project's runtime directory.  The
store deliberately derives project scope from the caller (or from a session)
and never performs a global slug lookup.
"""

from __future__ import annotations

import hashlib
import gzip
import io
import json
import os
import re
import shutil
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional

import database as database_module
from project_storage import project_dir


SKILL_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SKILL_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
ALLOWED_REFERENCE_EXTENSIONS = {
    ".css",
    ".csv",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".py",
    ".rst",
    ".sql",
    ".toml",
    ".ts",
    ".tsv",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
MAX_INSTRUCTIONS_BYTES = 128 * 1024
MAX_REFERENCE_FILES = 64
# References are stored as UTF-8 files, not copied into the model prompt in
# full.  The runtime selects bounded, query-relevant chunks, so a Project
# Skill can safely retain multi-megabyte source documents without creating a
# multi-megabyte prompt.
MAX_REFERENCE_BYTES = 8 * 1024 * 1024
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_REFERENCE_PATH_LENGTH = 240
MAX_HISTORY_ENTRIES = 1000
MAX_HISTORY_STORAGE_BYTES = 512 * 1024 * 1024
MAX_HISTORY_SNAPSHOT_BYTES = MAX_PACKAGE_BYTES + (4 * 1024 * 1024)
# JSON can escape valid control characters as six bytes (for example
# ``\u0001``).  Keep a separate decompressed bound so every package accepted
# by the store can also be represented by an immutable history snapshot.
MAX_HISTORY_SNAPSHOT_UNCOMPRESSED_BYTES = (MAX_PACKAGE_BYTES * 6) + (2 * 1024 * 1024)
SCHEMA_VERSION = 2
HISTORY_SNAPSHOT_SCHEMA_VERSION = 1
METADATA_FILENAME = ".project-skill.json"
INSTRUCTIONS_FILENAME = "SKILL.md"
REFERENCES_DIR = "references"
HISTORY_DIR = ".versions"
HISTORY_SNAPSHOT_SUFFIX = ".json.gz"
NAME_RESERVATIONS_DIR = ".name-reservations"
MUTATION_LOCKS_DIR = ".mutation-locks"
_UNSET = object()


class ProjectSkillError(Exception):
    code = "PROJECT_SKILL_ERROR"
    status_code = 500


class ProjectSkillValidationError(ProjectSkillError):
    code = "INVALID_PROJECT_SKILL"
    status_code = 400


class ProjectSkillNotFound(ProjectSkillError):
    code = "PROJECT_SKILL_NOT_FOUND"
    status_code = 404


class ProjectSkillProjectNotFound(ProjectSkillError):
    code = "PROJECT_NOT_FOUND"
    status_code = 404


class ProjectSkillSessionNotFound(ProjectSkillError):
    code = "SESSION_NOT_FOUND"
    status_code = 404


class ProjectSkillConflict(ProjectSkillError):
    code = "PROJECT_SKILL_NAME_CONFLICT"
    status_code = 409


class ProjectSkillVersionConflict(ProjectSkillError):
    code = "PROJECT_SKILL_VERSION_CONFLICT"
    status_code = 409


class ProjectSkillVersionNotFound(ProjectSkillError):
    code = "PROJECT_SKILL_VERSION_NOT_FOUND"
    status_code = 404


class ProjectSkillScopeError(ProjectSkillError):
    code = "PROJECT_SKILL_SCOPE_REQUIRED"
    status_code = 409


class ProjectSkillIntegrityError(ProjectSkillError):
    code = "PROJECT_SKILL_INTEGRITY_FAILED"
    status_code = 409


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(info.st_mode) or bool(
        attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


class ProjectSkillStore:
    """Filesystem store whose namespace boundary is the owning project."""

    def __init__(
        self,
        database: Any = database_module,
        project_dir_factory: Callable[..., Path] = project_dir,
    ) -> None:
        self.database = database
        self.project_dir_factory = project_dir_factory
        self._lock = threading.RLock()

    @staticmethod
    def normalize_slug(value: str) -> str:
        slug = str(value or "").strip()
        if not SKILL_SLUG_PATTERN.fullmatch(slug):
            raise ProjectSkillValidationError(
                "Skill slug must be 1-63 lowercase letters, numbers, or hyphens."
            )
        if slug in WINDOWS_RESERVED_NAMES:
            raise ProjectSkillValidationError(
                "Skill slug is reserved by the operating system."
            )
        return slug

    @staticmethod
    def _normalize_name(value: str) -> str:
        name = str(value or "").strip()
        if not name or len(name) > 80 or "\x00" in name:
            raise ProjectSkillValidationError(
                "Skill name must contain 1-80 visible characters."
            )
        return name

    @staticmethod
    def _normalize_description(value: str) -> str:
        description = str(value or "").strip()
        if len(description) > 500 or "\x00" in description:
            raise ProjectSkillValidationError(
                "Skill description must be at most 500 characters."
            )
        return description

    @staticmethod
    def _normalize_version(value: str) -> str:
        version = str(value or "").strip()
        if not SKILL_VERSION_PATTERN.fullmatch(version):
            raise ProjectSkillValidationError("Skill version is invalid.")
        return version

    @staticmethod
    def _normalize_instructions(value: str) -> str:
        instructions = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        encoded = instructions.encode("utf-8")
        if not instructions:
            raise ProjectSkillValidationError("Skill instructions cannot be empty.")
        if "\x00" in instructions:
            raise ProjectSkillValidationError("Skill instructions cannot contain null bytes.")
        if len(encoded) > MAX_INSTRUCTIONS_BYTES:
            raise ProjectSkillValidationError(
                f"Skill instructions exceed {MAX_INSTRUCTIONS_BYTES} bytes."
            )
        return instructions

    @staticmethod
    def _normalize_reference_path(value: Any) -> str:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ProjectSkillValidationError("Reference paths must be non-empty relative paths.")
        if len(value) > MAX_REFERENCE_PATH_LENGTH or "\x00" in value:
            raise ProjectSkillValidationError("Reference path is too long or invalid.")
        if "\\" in value or ":" in value or value.startswith("/"):
            raise ProjectSkillValidationError("Reference paths must use safe relative '/' paths.")
        raw_parts = value.split("/")
        if any(part in {"", ".", ".."} for part in raw_parts):
            raise ProjectSkillValidationError("Reference path contains an unsafe segment.")
        path = PurePosixPath(value)
        parts = path.parts
        if not parts or any(
            part in {"", ".", ".."}
            or part.startswith(".")
            or part.endswith((".", " "))
            or len(part) > 100
            or any(ord(character) < 32 for character in part)
            or part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES
            for part in parts
        ):
            raise ProjectSkillValidationError("Reference path contains an unsafe segment.")
        if path.suffix.casefold() not in ALLOWED_REFERENCE_EXTENSIONS:
            raise ProjectSkillValidationError("Reference file type is not allowed.")
        return path.as_posix()

    @classmethod
    def _normalize_references(cls, value: Any) -> dict[str, str]:
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ProjectSkillValidationError("Skill references must be a path-to-text object.")
        if len(value) > MAX_REFERENCE_FILES:
            raise ProjectSkillValidationError(
                f"A skill can contain at most {MAX_REFERENCE_FILES} reference files."
            )
        normalized: dict[str, str] = {}
        casefold_paths: set[str] = set()
        total_bytes = 0
        for raw_path, raw_content in value.items():
            path = cls._normalize_reference_path(raw_path)
            folded = path.casefold()
            if folded in casefold_paths:
                raise ProjectSkillValidationError(
                    "Reference paths must be unique without regard to letter case."
                )
            if not isinstance(raw_content, str):
                raise ProjectSkillValidationError("Reference file content must be text.")
            content = raw_content.replace("\r\n", "\n").replace("\r", "\n")
            if "\x00" in content:
                raise ProjectSkillValidationError("Reference files cannot contain null bytes.")
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > MAX_REFERENCE_BYTES:
                raise ProjectSkillValidationError(
                    f"A reference file cannot exceed {MAX_REFERENCE_BYTES} bytes."
                )
            total_bytes += len(content_bytes)
            if total_bytes > MAX_PACKAGE_BYTES:
                raise ProjectSkillValidationError("Skill reference files exceed the package limit.")
            casefold_paths.add(folded)
            normalized[path] = content
        return dict(sorted(normalized.items(), key=lambda item: item[0].casefold()))

    @staticmethod
    def _normalize_expected_sha256(value: str) -> str:
        digest = str(value or "").strip()
        if not SHA256_PATTERN.fullmatch(digest):
            raise ProjectSkillValidationError("expected_sha256 must be a lowercase SHA-256 value.")
        return digest

    @staticmethod
    def _package_digest(
        *,
        slug: str,
        name: str,
        description: str,
        version: str,
        instructions: str,
        references: Mapping[str, str],
    ) -> str:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "manifest": {
                "slug": slug,
                "name": name,
                "description": description,
                "version": version,
            },
            "instructions": instructions,
            "references": [
                {"path": path, "content": references[path]}
                for path in sorted(references, key=str.casefold)
            ],
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _sha256(canonical)

    @staticmethod
    def _reference_metadata(references: Mapping[str, str]) -> list[dict[str, Any]]:
        return [
            {
                "path": path,
                "sha256": _sha256(references[path].encode("utf-8")),
                "size_bytes": len(references[path].encode("utf-8")),
            }
            for path in sorted(references, key=str.casefold)
        ]

    @staticmethod
    def _history_entry(
        *,
        sha256: str,
        name: str,
        description: str,
        version: str,
        recorded_at: str,
        snapshot_sha256: Optional[str] = None,
        snapshot_size_bytes: Optional[int] = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "sha256": sha256,
            "name": name,
            "description": description,
            "version": version,
            "recorded_at": recorded_at,
            "snapshot_available": snapshot_sha256 is not None,
        }
        if snapshot_sha256 is not None:
            entry["snapshot_sha256"] = snapshot_sha256
            entry["snapshot_size_bytes"] = int(snapshot_size_bytes or 0)
        return entry

    @classmethod
    def _history_snapshot_bytes(
        cls,
        *,
        sha256: str,
        slug: str,
        name: str,
        description: str,
        version: str,
        instructions: str,
        references: Mapping[str, str],
    ) -> bytes:
        payload = {
            "snapshot_schema_version": HISTORY_SNAPSHOT_SCHEMA_VERSION,
            "package_sha256": sha256,
            "slug": slug,
            "name": name,
            "description": description,
            "version": version,
            "instructions": instructions,
            "references": [
                {"path": path, "content": references[path]}
                for path in sorted(references, key=str.casefold)
            ],
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(canonical) > MAX_HISTORY_SNAPSHOT_UNCOMPRESSED_BYTES:
            raise ProjectSkillValidationError("Skill version snapshot exceeds the safe limit.")
        compressed = gzip.compress(canonical, compresslevel=6, mtime=0)
        if len(compressed) > MAX_HISTORY_SNAPSHOT_BYTES:
            raise ProjectSkillValidationError("Compressed Skill version snapshot exceeds the safe limit.")
        return compressed

    @staticmethod
    def _history_snapshot_filename(package_sha256: str) -> str:
        return f"{package_sha256}{HISTORY_SNAPSHOT_SUFFIX}"

    def _require_project(self, project_id: str) -> dict[str, Any]:
        project = self.database.get_project(str(project_id or ""))
        if not project:
            raise ProjectSkillProjectNotFound(f"Project was not found: {project_id}")
        return project

    def _require_mutable_project(self, project_id: str) -> dict[str, Any]:
        project = self._require_project(project_id)
        if project.get("archived"):
            raise ProjectSkillScopeError("Archived projects cannot modify project-scoped skills.")
        return project

    def _skills_root(self, project_id: str, *, create: bool) -> Path:
        try:
            project_root = self.project_dir_factory(project_id, create=create)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProjectSkillIntegrityError("Project Skill storage path is invalid.") from exc
        if _is_link_or_reparse_point(project_root):
            raise ProjectSkillIntegrityError("Project Skill storage cannot use a linked project root.")
        root = project_root / "skills"
        if create:
            root.mkdir(parents=True, exist_ok=True)
        if root.exists() and _is_link_or_reparse_point(root):
            raise ProjectSkillIntegrityError("Project Skill storage cannot use a linked skills root.")
        resolved_project_root = project_root.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
        if not _is_relative_to(resolved_root, resolved_project_root):
            raise ProjectSkillIntegrityError("Skill root escapes its project boundary.")
        return root

    def _skill_dir(self, project_id: str, slug: str, *, create_root: bool) -> Path:
        root = self._skills_root(project_id, create=create_root)
        candidate = root / slug
        resolved_root = root.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
        if not _is_relative_to(resolved_candidate, resolved_root):
            raise ProjectSkillIntegrityError("Skill path escapes its project boundary.")
        if candidate.exists() and _is_link_or_reparse_point(candidate):
            raise ProjectSkillIntegrityError("Project Skill directories cannot be links.")
        return candidate

    def _reservation_path(self, project_id: str, name: str) -> Path:
        root = self._skills_root(project_id, create=True)
        reservation_root = root / NAME_RESERVATIONS_DIR
        reservation_root.mkdir(exist_ok=True)
        if _is_link_or_reparse_point(reservation_root):
            raise ProjectSkillIntegrityError("Skill name reservations cannot use links.")
        key = _sha256(name.strip().casefold().encode("utf-8"))
        return reservation_root / key

    def _claim_name(self, project_id: str, name: str, slug: str) -> tuple[Path, bool]:
        reservation = self._reservation_path(project_id, name)
        try:
            descriptor = os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if _is_link_or_reparse_point(reservation):
                raise ProjectSkillIntegrityError("Skill name reservation is unsafe.")
            try:
                owner = reservation.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as exc:
                raise ProjectSkillIntegrityError("Skill name reservation is unreadable.") from exc
            if owner == slug:
                return reservation, False
            raise ProjectSkillConflict(f"Skill name already exists in this project: {name}")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(slug + "\n")
        except Exception:
            reservation.unlink(missing_ok=True)
            raise
        return reservation, True

    def _release_name(self, project_id: str, name: str, slug: str) -> None:
        reservation = self._reservation_path(project_id, name)
        if not reservation.exists():
            return
        if _is_link_or_reparse_point(reservation):
            raise ProjectSkillIntegrityError("Skill name reservation is unsafe.")
        try:
            owner = reservation.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise ProjectSkillIntegrityError("Skill name reservation is unreadable.") from exc
        if owner == slug:
            reservation.unlink(missing_ok=True)

    @contextmanager
    def _mutation_guard(self, project_id: str, slug: str) -> Iterator[None]:
        root = self._skills_root(project_id, create=True)
        lock_root = root / MUTATION_LOCKS_DIR
        lock_root.mkdir(exist_ok=True)
        if _is_link_or_reparse_point(lock_root):
            raise ProjectSkillIntegrityError("Skill mutation locks cannot use links.")
        lock_path = lock_root / _sha256(slug.encode("utf-8"))
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ProjectSkillVersionConflict(
                "This skill is already being modified; refresh and try again."
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}\n")
            yield
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _safe_existing_file(path: Path, skill_root: Path) -> Path:
        try:
            resolved_root = skill_root.resolve(strict=True)
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ProjectSkillIntegrityError("Skill files are incomplete.") from exc
        if (
            _is_link_or_reparse_point(path)
            or not resolved.is_file()
            or not _is_relative_to(resolved, resolved_root)
        ):
            raise ProjectSkillIntegrityError("Skill file escapes its project boundary.")
        return resolved

    def _read_references(self, skill_path: Path) -> dict[str, str]:
        references_root = skill_path / REFERENCES_DIR
        if not references_root.exists():
            if _is_link_or_reparse_point(references_root):
                raise ProjectSkillIntegrityError("Skill references directory is unsafe.")
            return {}
        if _is_link_or_reparse_point(references_root) or not references_root.is_dir():
            raise ProjectSkillIntegrityError("Skill references directory is unsafe.")
        references: dict[str, str] = {}
        folded_paths: set[str] = set()
        total_bytes = 0

        def visit(directory: Path) -> None:
            nonlocal total_bytes
            try:
                children = list(directory.iterdir())
            except OSError as exc:
                raise ProjectSkillIntegrityError("Skill references could not be listed.") from exc
            for child in children:
                if _is_link_or_reparse_point(child):
                    raise ProjectSkillIntegrityError("Skill references cannot contain links.")
                if child.is_dir():
                    visit(child)
                    continue
                if not child.is_file():
                    raise ProjectSkillIntegrityError("Skill references contain an unsafe entry.")
                try:
                    relative = child.relative_to(references_root).as_posix()
                    normalized_path = self._normalize_reference_path(relative)
                    content_bytes = child.read_bytes()
                    content = content_bytes.decode("utf-8")
                    normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")
                except (OSError, UnicodeError, ValueError, ProjectSkillValidationError) as exc:
                    raise ProjectSkillIntegrityError("Skill reference file is invalid.") from exc
                if normalized_content.encode("utf-8") != content_bytes:
                    raise ProjectSkillIntegrityError("Skill reference file is not canonically encoded.")
                if len(content_bytes) > MAX_REFERENCE_BYTES or "\x00" in content:
                    raise ProjectSkillIntegrityError("Skill reference file exceeds safe limits.")
                folded = normalized_path.casefold()
                if folded in folded_paths:
                    raise ProjectSkillIntegrityError("Skill reference paths collide by letter case.")
                folded_paths.add(folded)
                total_bytes += len(content_bytes)
                if len(references) >= MAX_REFERENCE_FILES or total_bytes > MAX_PACKAGE_BYTES:
                    raise ProjectSkillIntegrityError("Skill references exceed safe package limits.")
                references[normalized_path] = content

        visit(references_root)
        return dict(sorted(references.items(), key=lambda item: item[0].casefold()))

    def _read_history_snapshots(
        self,
        skill_path: Path,
        history: Iterable[Mapping[str, Any]],
        *,
        allow_untracked: bool = False,
    ) -> dict[str, bytes]:
        expected: dict[str, tuple[str, int]] = {}
        for entry in history:
            if not entry.get("snapshot_available"):
                continue
            package_sha256 = self._normalize_expected_sha256(entry.get("sha256"))
            snapshot_sha256 = self._normalize_expected_sha256(entry.get("snapshot_sha256"))
            snapshot_size = entry.get("snapshot_size_bytes")
            if type(snapshot_size) is not int or snapshot_size <= 0:
                raise ProjectSkillIntegrityError("Skill version snapshot size is invalid.")
            previous = expected.get(package_sha256)
            descriptor = (snapshot_sha256, snapshot_size)
            if previous is not None and previous != descriptor:
                raise ProjectSkillIntegrityError("Duplicate Skill version snapshots disagree.")
            expected[package_sha256] = descriptor

        snapshots_root = skill_path / HISTORY_DIR
        if not expected:
            if snapshots_root.exists():
                if not allow_untracked:
                    raise ProjectSkillIntegrityError("Skill has untracked version snapshots.")
                if (
                    not snapshots_root.is_dir()
                    or _is_link_or_reparse_point(snapshots_root)
                ):
                    raise ProjectSkillIntegrityError("Legacy Skill snapshot directory is unsafe.")
                total_bytes = 0
                try:
                    children = list(snapshots_root.iterdir())
                except OSError as exc:
                    raise ProjectSkillIntegrityError(
                        "Legacy Skill snapshots could not be listed."
                    ) from exc
                for child in children:
                    stem = child.name.removesuffix(HISTORY_SNAPSHOT_SUFFIX)
                    if (
                        not child.is_file()
                        or _is_link_or_reparse_point(child)
                        or not child.name.endswith(HISTORY_SNAPSHOT_SUFFIX)
                        or not SHA256_PATTERN.fullmatch(stem)
                    ):
                        raise ProjectSkillIntegrityError("Legacy Skill snapshot entry is unsafe.")
                    total_bytes += child.stat().st_size
                    if total_bytes > MAX_HISTORY_STORAGE_BYTES:
                        raise ProjectSkillIntegrityError("Legacy Skill snapshots exceed safe limits.")
            return {}
        if (
            not snapshots_root.is_dir()
            or _is_link_or_reparse_point(snapshots_root)
        ):
            raise ProjectSkillIntegrityError("Skill version snapshot directory is unsafe.")
        try:
            children = {child.name: child for child in snapshots_root.iterdir()}
        except OSError as exc:
            raise ProjectSkillIntegrityError("Skill version snapshots could not be listed.") from exc
        expected_names = {
            self._history_snapshot_filename(package_sha256)
            for package_sha256 in expected
        }
        if set(children) != expected_names:
            raise ProjectSkillIntegrityError("Skill version snapshots do not match history.")

        snapshots: dict[str, bytes] = {}
        total_bytes = 0
        for package_sha256, (snapshot_sha256, snapshot_size) in expected.items():
            path = children[self._history_snapshot_filename(package_sha256)]
            self._safe_existing_file(path, skill_path)
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise ProjectSkillIntegrityError("Skill version snapshot is unreadable.") from exc
            if (
                len(data) != snapshot_size
                or len(data) > MAX_HISTORY_SNAPSHOT_BYTES
                or _sha256(data) != snapshot_sha256
            ):
                raise ProjectSkillIntegrityError("Skill version snapshot failed validation.")
            total_bytes += len(data)
            if total_bytes > MAX_HISTORY_STORAGE_BYTES:
                raise ProjectSkillIntegrityError("Skill version history exceeds its storage limit.")
            snapshots[package_sha256] = data
        return snapshots

    def _decode_history_snapshot(
        self,
        *,
        slug: str,
        entry: Mapping[str, Any],
        compressed: bytes,
    ) -> dict[str, Any]:
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as handle:
                raw = handle.read(MAX_HISTORY_SNAPSHOT_UNCOMPRESSED_BYTES + 1)
            if len(raw) > MAX_HISTORY_SNAPSHOT_UNCOMPRESSED_BYTES:
                raise ProjectSkillIntegrityError("Skill version snapshot expands beyond its limit.")
            payload = json.loads(raw.decode("utf-8"))
        except ProjectSkillIntegrityError:
            raise
        except (OSError, EOFError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectSkillIntegrityError("Skill version snapshot could not be decoded.") from exc
        if not isinstance(payload, dict):
            raise ProjectSkillIntegrityError("Skill version snapshot payload is invalid.")
        if payload.get("snapshot_schema_version") != HISTORY_SNAPSHOT_SCHEMA_VERSION:
            raise ProjectSkillIntegrityError("Skill version snapshot schema is unsupported.")
        try:
            package_sha256 = self._normalize_expected_sha256(payload.get("package_sha256"))
            snapshot_slug = self.normalize_slug(payload.get("slug"))
            name = self._normalize_name(payload.get("name"))
            description = self._normalize_description(payload.get("description"))
            version = self._normalize_version(payload.get("version"))
            instructions = self._normalize_instructions(payload.get("instructions"))
            raw_references = payload.get("references")
            if not isinstance(raw_references, list):
                raise ProjectSkillValidationError("Snapshot references must be a list.")
            reference_map: dict[str, str] = {}
            for reference in raw_references:
                if not isinstance(reference, dict) or set(reference) != {"path", "content"}:
                    raise ProjectSkillValidationError("Snapshot reference entry is invalid.")
                path = self._normalize_reference_path(reference.get("path"))
                if path in reference_map:
                    raise ProjectSkillValidationError("Snapshot references contain duplicates.")
                reference_map[path] = reference.get("content")
            references = self._normalize_references(reference_map)
        except ProjectSkillValidationError as exc:
            raise ProjectSkillIntegrityError("Skill version snapshot content is invalid.") from exc
        if snapshot_slug != slug or package_sha256 != entry.get("sha256"):
            raise ProjectSkillIntegrityError("Skill version snapshot identity does not match history.")
        digest = self._package_digest(
            slug=slug,
            name=name,
            description=description,
            version=version,
            instructions=instructions,
            references=references,
        )
        if digest != package_sha256:
            raise ProjectSkillIntegrityError("Skill version snapshot package digest is invalid.")
        return {
            "sha256": package_sha256,
            "slug": slug,
            "name": name,
            "description": description,
            "version": version,
            "recorded_at": entry.get("recorded_at"),
            "snapshot_available": True,
            "instructions": instructions,
            "references": [
                {**item, "content": references[item["path"]]}
                for item in self._reference_metadata(references)
            ],
        }

    def _validated_history(
        self,
        metadata: Mapping[str, Any],
        *,
        current: Mapping[str, Any],
        legacy: bool,
    ) -> list[dict[str, Any]]:
        if legacy:
            return [
                self._history_entry(
                    sha256=current["sha256"],
                    name=current["name"],
                    description=current["description"],
                    version=current["version"],
                    recorded_at=str(metadata.get("updated_at") or metadata.get("created_at") or _now()),
                )
            ]
        raw_history = metadata.get("history")
        if not isinstance(raw_history, list) or not raw_history or len(raw_history) > MAX_HISTORY_ENTRIES:
            raise ProjectSkillIntegrityError("Skill version history is invalid.")
        history: list[dict[str, Any]] = []
        for raw in raw_history:
            if not isinstance(raw, dict):
                raise ProjectSkillIntegrityError("Skill version history is invalid.")
            try:
                snapshot_available = raw.get(
                    "snapshot_available",
                    raw.get("snapshot_sha256") is not None,
                )
                if not isinstance(snapshot_available, bool):
                    raise ProjectSkillValidationError("Snapshot availability must be boolean.")
                snapshot_sha256 = None
                snapshot_size_bytes = None
                if snapshot_available:
                    snapshot_sha256 = self._normalize_expected_sha256(
                        raw.get("snapshot_sha256")
                    )
                    snapshot_size_bytes = raw.get("snapshot_size_bytes")
                    if type(snapshot_size_bytes) is not int or snapshot_size_bytes <= 0:
                        raise ProjectSkillValidationError("Snapshot size is invalid.")
                elif raw.get("snapshot_sha256") is not None or raw.get("snapshot_size_bytes") is not None:
                    raise ProjectSkillValidationError("Unavailable snapshot has snapshot metadata.")
                entry = self._history_entry(
                    sha256=self._normalize_expected_sha256(raw.get("sha256")),
                    name=self._normalize_name(raw.get("name")),
                    description=self._normalize_description(raw.get("description")),
                    version=self._normalize_version(raw.get("version")),
                    recorded_at=str(raw.get("recorded_at") or ""),
                    snapshot_sha256=snapshot_sha256,
                    snapshot_size_bytes=snapshot_size_bytes,
                )
            except ProjectSkillValidationError as exc:
                raise ProjectSkillIntegrityError("Skill version history failed validation.") from exc
            if not entry["recorded_at"]:
                raise ProjectSkillIntegrityError("Skill version history has no timestamp.")
            history.append(entry)
        if history[-1]["sha256"] != current["sha256"]:
            raise ProjectSkillIntegrityError("Skill version history does not match its package.")
        return history

    def _read_state(
        self,
        project_id: str,
        slug: str,
        *,
        include_instructions: bool,
    ) -> dict[str, Any]:
        skill_path = self._skill_dir(project_id, slug, create_root=False)
        if not skill_path.is_dir():
            raise ProjectSkillNotFound(f"Project skill was not found: {project_id}/{slug}")
        try:
            root_entries = {entry.name: entry for entry in skill_path.iterdir()}
        except OSError as exc:
            raise ProjectSkillIntegrityError("Skill directory could not be listed.") from exc
        allowed_entries = {
            METADATA_FILENAME,
            INSTRUCTIONS_FILENAME,
            REFERENCES_DIR,
            HISTORY_DIR,
        }
        if set(root_entries) - allowed_entries:
            raise ProjectSkillIntegrityError("Skill directory contains unexpected files.")
        metadata_path = self._safe_existing_file(skill_path / METADATA_FILENAME, skill_path)
        instructions_path = self._safe_existing_file(skill_path / INSTRUCTIONS_FILENAME, skill_path)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            instructions_bytes = instructions_path.read_bytes()
            instructions = instructions_bytes.decode("utf-8")
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectSkillIntegrityError("Skill files could not be read safely.") from exc
        if not isinstance(metadata, dict):
            raise ProjectSkillIntegrityError("Skill metadata is invalid.")
        if metadata.get("project_id") != project_id or metadata.get("slug") != slug:
            raise ProjectSkillIntegrityError(
                "Skill ownership metadata does not match its project directory."
            )
        if not isinstance(metadata.get("enabled"), bool):
            raise ProjectSkillIntegrityError("Skill enabled state is invalid.")
        try:
            name = self._normalize_name(metadata.get("name"))
            description = self._normalize_description(metadata.get("description"))
            version = self._normalize_version(metadata.get("version"))
            normalized_instructions = self._normalize_instructions(instructions)
        except ProjectSkillValidationError as exc:
            raise ProjectSkillIntegrityError("Skill metadata or instructions failed validation.") from exc
        if normalized_instructions.encode("utf-8") != instructions_bytes:
            raise ProjectSkillIntegrityError("Skill instructions are not canonically encoded.")
        references = self._read_references(skill_path)
        if len(instructions_bytes) + sum(
            len(content.encode("utf-8")) for content in references.values()
        ) > MAX_PACKAGE_BYTES:
            raise ProjectSkillIntegrityError("Skill package exceeds the safe size limit.")

        schema_version = metadata.get("schema_version", 1)
        legacy = schema_version == 1
        if legacy:
            if references:
                raise ProjectSkillIntegrityError("Legacy skills cannot contain reference files.")
            digest = _sha256(instructions_bytes)
        elif schema_version == SCHEMA_VERSION:
            expected_references = self._reference_metadata(references)
            if metadata.get("references") != expected_references:
                raise ProjectSkillIntegrityError("Skill reference metadata failed validation.")
            digest = self._package_digest(
                slug=slug,
                name=name,
                description=description,
                version=version,
                instructions=normalized_instructions,
                references=references,
            )
        else:
            raise ProjectSkillIntegrityError("Skill metadata schema is not supported.")
        if metadata.get("sha256") != digest:
            raise ProjectSkillIntegrityError("Skill package failed the integrity check.")

        item: dict[str, Any] = {
            "id": f"{project_id}:{slug}",
            "project_id": project_id,
            "slug": slug,
            "name": name,
            "description": description,
            "version": version,
            "enabled": metadata["enabled"],
            "sha256": digest,
            "created_at": metadata.get("created_at"),
            "updated_at": metadata.get("updated_at"),
            "storage_path": f"skills/{slug}",
            "references": self._reference_metadata(references),
        }
        history = self._validated_history(metadata, current=item, legacy=legacy)
        history_snapshots = self._read_history_snapshots(
            skill_path,
            history,
            allow_untracked=legacy,
        )
        item["history_count"] = len(history)
        if include_instructions:
            item["instructions"] = normalized_instructions
            item["references"] = [
                {**entry, "content": references[entry["path"]]}
                for entry in item["references"]
            ]
        return {
            "item": item,
            "metadata": metadata,
            "instructions": normalized_instructions,
            "references": references,
            "history": history,
            "history_snapshots": history_snapshots,
            "legacy": legacy,
            "path": skill_path,
        }

    def _read(self, project_id: str, slug: str, *, include_instructions: bool) -> dict[str, Any]:
        return self._read_state(
            project_id,
            slug,
            include_instructions=include_instructions,
        )["item"]

    @staticmethod
    def _write_metadata(path: Path, metadata: Mapping[str, Any]) -> None:
        path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_package_directory(
        self,
        directory: Path,
        *,
        metadata: Mapping[str, Any],
        instructions: str,
        references: Mapping[str, str],
        history_snapshots: Optional[Mapping[str, bytes]] = None,
    ) -> None:
        directory.mkdir(exist_ok=False)
        try:
            (directory / INSTRUCTIONS_FILENAME).write_bytes(instructions.encode("utf-8"))
            if references:
                references_root = directory / REFERENCES_DIR
                references_root.mkdir()
                for relative, content in references.items():
                    target = references_root.joinpath(*PurePosixPath(relative).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content.encode("utf-8"))
            if history_snapshots:
                total_snapshot_bytes = sum(len(data) for data in history_snapshots.values())
                if total_snapshot_bytes > MAX_HISTORY_STORAGE_BYTES:
                    raise ProjectSkillValidationError(
                        "Skill version history exceeds its storage limit."
                    )
                history_root = directory / HISTORY_DIR
                history_root.mkdir()
                for package_sha256, data in sorted(history_snapshots.items()):
                    normalized_sha256 = self._normalize_expected_sha256(package_sha256)
                    if not isinstance(data, bytes) or not data or len(data) > MAX_HISTORY_SNAPSHOT_BYTES:
                        raise ProjectSkillValidationError("Skill version snapshot is invalid.")
                    (history_root / self._history_snapshot_filename(normalized_sha256)).write_bytes(data)
            self._write_metadata(directory / METADATA_FILENAME, metadata)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def _replace_package(
        self,
        skill_path: Path,
        *,
        metadata: Mapping[str, Any],
        instructions: str,
        references: Mapping[str, str],
        history_snapshots: Mapping[str, bytes],
    ) -> None:
        root = skill_path.parent
        staging = root / f".{skill_path.name}.{uuid.uuid4().hex}.staging"
        backup = root / f".{skill_path.name}.{uuid.uuid4().hex}.backup"
        self._write_package_directory(
            staging,
            metadata=metadata,
            instructions=instructions,
            references=references,
            history_snapshots=history_snapshots,
        )
        moved_original = False
        try:
            os.replace(skill_path, backup)
            moved_original = True
            os.replace(staging, skill_path)
        except Exception:
            if moved_original and backup.exists() and not skill_path.exists():
                os.replace(backup, skill_path)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        shutil.rmtree(backup, ignore_errors=True)

    def list(self, project_id: str, *, include_instructions: bool = False) -> list[dict[str, Any]]:
        self._require_project(project_id)
        root = self._skills_root(project_id, create=True)
        items: list[dict[str, Any]] = []
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            if path.name.startswith(".") or not SKILL_SLUG_PATTERN.fullmatch(path.name):
                continue
            items.append(self._read(project_id, path.name, include_instructions=include_instructions))
        names = [item["name"].strip().casefold() for item in items]
        if len(names) != len(set(names)):
            raise ProjectSkillIntegrityError("Two skills in this project have the same display name.")
        return items

    def get(self, project_id: str, slug: str) -> dict[str, Any]:
        self._require_project(project_id)
        return self._read(project_id, self.normalize_slug(slug), include_instructions=True)

    def create(
        self,
        project_id: str,
        *,
        slug: str,
        name: str,
        instructions: str,
        description: str = "",
        version: str = "1.0.0",
        enabled: bool = True,
        references: Optional[Mapping[str, str]] = None,
    ) -> dict[str, Any]:
        self._require_mutable_project(project_id)
        normalized_slug = self.normalize_slug(slug)
        normalized_name = self._normalize_name(name)
        normalized_description = self._normalize_description(description)
        normalized_version = self._normalize_version(version)
        if not isinstance(enabled, bool):
            raise ProjectSkillValidationError("Skill enabled state must be true or false.")
        normalized_instructions = self._normalize_instructions(instructions)
        normalized_references = self._normalize_references(references)
        if len(normalized_instructions.encode("utf-8")) + sum(
            len(content.encode("utf-8")) for content in normalized_references.values()
        ) > MAX_PACKAGE_BYTES:
            raise ProjectSkillValidationError("Skill package exceeds the safe size limit.")
        now = _now()
        digest = self._package_digest(
            slug=normalized_slug,
            name=normalized_name,
            description=normalized_description,
            version=normalized_version,
            instructions=normalized_instructions,
            references=normalized_references,
        )
        history_snapshot = self._history_snapshot_bytes(
            sha256=digest,
            slug=normalized_slug,
            name=normalized_name,
            description=normalized_description,
            version=normalized_version,
            instructions=normalized_instructions,
            references=normalized_references,
        )
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "slug": normalized_slug,
            "name": normalized_name,
            "description": normalized_description,
            "version": normalized_version,
            "enabled": enabled,
            "sha256": digest,
            "references": self._reference_metadata(normalized_references),
            "history": [
                self._history_entry(
                    sha256=digest,
                    name=normalized_name,
                    description=normalized_description,
                    version=normalized_version,
                    recorded_at=now,
                    snapshot_sha256=_sha256(history_snapshot),
                    snapshot_size_bytes=len(history_snapshot),
                )
            ],
            "created_at": now,
            "updated_at": now,
        }

        with self._lock, self._mutation_guard(project_id, normalized_slug):
            existing = self.list(project_id)
            if any(item["slug"] == normalized_slug for item in existing):
                raise ProjectSkillConflict(
                    f"Skill slug already exists in this project: {normalized_slug}"
                )
            if any(item["name"].casefold() == normalized_name.casefold() for item in existing):
                raise ProjectSkillConflict(
                    f"Skill name already exists in this project: {normalized_name}"
                )
            skill_path = self._skill_dir(project_id, normalized_slug, create_root=True)
            reservation, newly_claimed = self._claim_name(
                project_id,
                normalized_name,
                normalized_slug,
            )
            staging = skill_path.parent / f".{normalized_slug}.{uuid.uuid4().hex}.staging"
            try:
                self._write_package_directory(
                    staging,
                    metadata=metadata,
                    instructions=normalized_instructions,
                    references=normalized_references,
                    history_snapshots={digest: history_snapshot},
                )
                try:
                    os.rename(staging, skill_path)
                except FileExistsError as exc:
                    raise ProjectSkillConflict(
                        f"Skill slug already exists in this project: {normalized_slug}"
                    ) from exc
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                if newly_claimed:
                    try:
                        reservation.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
        return self.get(project_id, normalized_slug)

    def update(
        self,
        project_id: str,
        slug: str,
        *,
        expected_sha256: str,
        name: Any = _UNSET,
        description: Any = _UNSET,
        version: Any = _UNSET,
        instructions: Any = _UNSET,
        references: Any = _UNSET,
        enabled: Any = _UNSET,
    ) -> dict[str, Any]:
        self._require_mutable_project(project_id)
        normalized_slug = self.normalize_slug(slug)
        expected = self._normalize_expected_sha256(expected_sha256)
        with self._lock, self._mutation_guard(project_id, normalized_slug):
            state = self._read_state(project_id, normalized_slug, include_instructions=False)
            current = state["item"]
            if current["sha256"] != expected:
                raise ProjectSkillVersionConflict(
                    "The skill changed after it was loaded; refresh before updating."
                )
            next_name = current["name"] if name is _UNSET else self._normalize_name(name)
            next_description = (
                current["description"]
                if description is _UNSET
                else self._normalize_description(description)
            )
            next_version = (
                current["version"] if version is _UNSET else self._normalize_version(version)
            )
            next_instructions = (
                state["instructions"]
                if instructions is _UNSET
                else self._normalize_instructions(instructions)
            )
            next_references = (
                state["references"]
                if references is _UNSET
                else self._normalize_references(references)
            )
            if enabled is not _UNSET and not isinstance(enabled, bool):
                raise ProjectSkillValidationError("Skill enabled state must be true or false.")
            next_enabled = current["enabled"] if enabled is _UNSET else enabled
            if len(next_instructions.encode("utf-8")) + sum(
                len(content.encode("utf-8")) for content in next_references.values()
            ) > MAX_PACKAGE_BYTES:
                raise ProjectSkillValidationError("Skill package exceeds the safe size limit.")
            next_digest = self._package_digest(
                slug=normalized_slug,
                name=next_name,
                description=next_description,
                version=next_version,
                instructions=next_instructions,
                references=next_references,
            )
            package_changed = next_digest != current["sha256"] or state["legacy"]
            state_changed = next_enabled != current["enabled"]
            if not package_changed and not state_changed:
                return self.get(project_id, normalized_slug)

            if next_name.casefold() != current["name"].casefold():
                for item in self.list(project_id):
                    if item["slug"] != normalized_slug and item["name"].casefold() == next_name.casefold():
                        raise ProjectSkillConflict(
                            f"Skill name already exists in this project: {next_name}"
                        )
            new_reservation: Optional[Path] = None
            newly_claimed = False
            if next_name.casefold() != current["name"].casefold():
                new_reservation, newly_claimed = self._claim_name(
                    project_id,
                    next_name,
                    normalized_slug,
                )
            now = _now()
            history = list(state["history"])
            history_snapshots = dict(state["history_snapshots"])
            if package_changed:
                if (
                    history
                    and not history[-1].get("snapshot_available")
                    and not state["legacy"]
                    and history[-1].get("sha256") == current["sha256"]
                ):
                    current_snapshot = self._history_snapshot_bytes(
                        sha256=current["sha256"],
                        slug=normalized_slug,
                        name=current["name"],
                        description=current["description"],
                        version=current["version"],
                        instructions=state["instructions"],
                        references=state["references"],
                    )
                    history_snapshots[current["sha256"]] = current_snapshot
                    history[-1] = self._history_entry(
                        sha256=current["sha256"],
                        name=current["name"],
                        description=current["description"],
                        version=current["version"],
                        recorded_at=str(history[-1].get("recorded_at") or now),
                        snapshot_sha256=_sha256(current_snapshot),
                        snapshot_size_bytes=len(current_snapshot),
                    )
                next_snapshot = self._history_snapshot_bytes(
                    sha256=next_digest,
                    slug=normalized_slug,
                    name=next_name,
                    description=next_description,
                    version=next_version,
                    instructions=next_instructions,
                    references=next_references,
                )
                existing_snapshot = history_snapshots.get(next_digest)
                if existing_snapshot is not None and existing_snapshot != next_snapshot:
                    raise ProjectSkillIntegrityError(
                        "A stored Skill version snapshot does not match its package."
                    )
                history_snapshots[next_digest] = next_snapshot
                history.append(
                    self._history_entry(
                        sha256=next_digest,
                        name=next_name,
                        description=next_description,
                        version=next_version,
                        recorded_at=now,
                        snapshot_sha256=_sha256(next_snapshot),
                        snapshot_size_bytes=len(next_snapshot),
                    )
                )
            if len(history) > MAX_HISTORY_ENTRIES:
                if new_reservation is not None and newly_claimed:
                    new_reservation.unlink(missing_ok=True)
                raise ProjectSkillValidationError("Skill version history reached its safe limit.")
            if sum(len(data) for data in history_snapshots.values()) > MAX_HISTORY_STORAGE_BYTES:
                if new_reservation is not None and newly_claimed:
                    new_reservation.unlink(missing_ok=True)
                raise ProjectSkillValidationError("Skill version history exceeds its storage limit.")
            metadata = {
                "schema_version": SCHEMA_VERSION,
                "project_id": project_id,
                "slug": normalized_slug,
                "name": next_name,
                "description": next_description,
                "version": next_version,
                "enabled": next_enabled,
                "sha256": next_digest,
                "references": self._reference_metadata(next_references),
                "history": history,
                "created_at": current["created_at"],
                "updated_at": now,
            }
            try:
                self._replace_package(
                    state["path"],
                    metadata=metadata,
                    instructions=next_instructions,
                    references=next_references,
                    history_snapshots=history_snapshots,
                )
            except Exception:
                if new_reservation is not None and newly_claimed:
                    try:
                        new_reservation.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
            if next_name.casefold() != current["name"].casefold():
                self._release_name(project_id, current["name"], normalized_slug)
        return self.get(project_id, normalized_slug)

    def set_enabled(
        self,
        project_id: str,
        slug: str,
        *,
        enabled: bool,
        expected_sha256: str,
    ) -> dict[str, Any]:
        self._require_mutable_project(project_id)
        normalized_slug = self.normalize_slug(slug)
        expected = self._normalize_expected_sha256(expected_sha256)
        if not isinstance(enabled, bool):
            raise ProjectSkillValidationError("Skill enabled state must be true or false.")
        with self._lock, self._mutation_guard(project_id, normalized_slug):
            state = self._read_state(project_id, normalized_slug, include_instructions=False)
            current = state["item"]
            if current["sha256"] != expected:
                raise ProjectSkillVersionConflict(
                    "The skill changed after it was loaded; refresh before changing its state."
                )
            if current["enabled"] == enabled:
                return self.get(project_id, normalized_slug)
            metadata = dict(state["metadata"])
            metadata["enabled"] = enabled
            metadata["updated_at"] = _now()
            temporary = state["path"] / f".{METADATA_FILENAME}.{uuid.uuid4().hex}.tmp"
            try:
                self._write_metadata(temporary, metadata)
                os.replace(temporary, state["path"] / METADATA_FILENAME)
            finally:
                temporary.unlink(missing_ok=True)
        return self.get(project_id, normalized_slug)

    def versions(self, project_id: str, slug: str) -> list[dict[str, Any]]:
        self._require_project(project_id)
        normalized_slug = self.normalize_slug(slug)
        state = self._read_state(project_id, normalized_slug, include_instructions=False)
        return [dict(entry) for entry in reversed(state["history"])]

    def get_version(
        self,
        project_id: str,
        slug: str,
        sha256: str,
    ) -> dict[str, Any]:
        """Return an immutable historical package snapshot when one is available."""

        self._require_project(project_id)
        normalized_slug = self.normalize_slug(slug)
        normalized_sha256 = self._normalize_expected_sha256(sha256)
        state = self._read_state(project_id, normalized_slug, include_instructions=False)
        entry = next(
            (
                item
                for item in reversed(state["history"])
                if item.get("sha256") == normalized_sha256
            ),
            None,
        )
        if not entry or not entry.get("snapshot_available"):
            raise ProjectSkillVersionNotFound(
                f"Project Skill version snapshot was not found: {normalized_slug}/{normalized_sha256}"
            )
        compressed = state["history_snapshots"].get(normalized_sha256)
        if not isinstance(compressed, bytes):
            raise ProjectSkillIntegrityError("Skill version snapshot is missing.")
        return self._decode_history_snapshot(
            slug=normalized_slug,
            entry=entry,
            compressed=compressed,
        )

    def delete(
        self,
        project_id: str,
        slug: str,
        *,
        expected_sha256: str,
    ) -> dict[str, Any]:
        self._require_project(project_id)
        normalized_slug = self.normalize_slug(slug)
        expected = self._normalize_expected_sha256(expected_sha256)
        with self._lock, self._mutation_guard(project_id, normalized_slug):
            state = self._read_state(project_id, normalized_slug, include_instructions=False)
            current = state["item"]
            if current["sha256"] != expected:
                raise ProjectSkillVersionConflict(
                    "The skill changed after it was loaded; refresh before deleting it."
                )
            skill_path = state["path"]
            root = self._skills_root(project_id, create=False).resolve(strict=True)
            resolved_skill = skill_path.resolve(strict=True)
            if not _is_relative_to(resolved_skill, root) or resolved_skill.parent != root:
                raise ProjectSkillIntegrityError("Skill deletion target escaped its project boundary.")
            shutil.rmtree(resolved_skill)
            self._release_name(project_id, current["name"], normalized_slug)
        return {
            "id": current["id"],
            "project_id": project_id,
            "slug": normalized_slug,
            "sha256": current["sha256"],
            "deleted": True,
        }

    def load_for_session(
        self,
        session_id: str,
        slugs: Optional[Iterable[str]] = None,
    ) -> dict[str, Any]:
        """Load only skills owned by the session's database-bound project."""

        session = self.database.get_session(session_id)
        if not session:
            raise ProjectSkillSessionNotFound(f"Session was not found: {session_id}")
        project_id = session.get("project_id")
        if not project_id:
            raise ProjectSkillScopeError("Independent sessions cannot load project-scoped skills.")
        project = self._require_project(project_id)
        if project.get("archived"):
            raise ProjectSkillScopeError("Archived projects cannot load project-scoped skills.")
        if slugs is None:
            skills = self.list(project_id, include_instructions=True)
        else:
            normalized = list(dict.fromkeys(self.normalize_slug(slug) for slug in slugs))
            skills = [self.get(project_id, slug) for slug in normalized]
        return {
            "session_id": session_id,
            "project_id": project_id,
            "skills": [item for item in skills if item["enabled"]],
        }
