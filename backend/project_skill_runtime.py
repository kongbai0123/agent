"""Session-scoped Project Skill activation, prompt context, and provenance."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import database as database_module
from project_skills import (
    ProjectSkillScopeError,
    ProjectSkillSessionNotFound,
    ProjectSkillValidationError,
)


MAX_ACTIVE_SKILLS = 8
MAX_PROJECT_SKILL_CONTEXT_CHARS = 32_000
MAX_INSTRUCTIONS_PER_SKILL_CHARS = 10_000
MAX_REFERENCE_FILES_PER_SKILL = 3
MAX_REFERENCE_CHARS_PER_SKILL = 12_000
REFERENCE_CHUNK_CHARS = 4_000
REFERENCE_CHUNK_OVERLAP_CHARS = 400
MAX_REFERENCE_CHUNKS_PER_SKILL = 6


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _query_terms(value: str) -> set[str]:
    lowered = str(value or "").casefold()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_.-]{1,}", lowered))
    for sequence in re.findall(r"[\u3400-\u9fff]+", lowered):
        if len(sequence) == 1:
            terms.add(sequence)
        else:
            terms.update(sequence[index:index + 2] for index in range(len(sequence) - 1))
    return terms


class ProjectSkillRuntime:
    """Resolve effective skills from a session without accepting project input."""

    def __init__(self, store: Any, database: Any = database_module) -> None:
        self.store = store
        self.database = database
        self._schema_key: Optional[str] = None
        self._schema_lock = threading.Lock()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        schema_key = str(getattr(self.database, "DB_PATH", "default"))
        if self._schema_key == schema_key:
            return
        with self._schema_lock:
            if self._schema_key == schema_key:
                return
            with self.database.get_db_conn() as conn:
                conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS project_skill_session_state (
                    session_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    skill_slug TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK (mode IN ('enabled', 'disabled')),
                    scope TEXT NOT NULL CHECK (scope IN ('session', 'turn')),
                    approved_sha256 TEXT NOT NULL,
                    activated_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, project_id, skill_slug)
                );
                CREATE INDEX IF NOT EXISTS idx_project_skill_session_state_session
                    ON project_skill_session_state(session_id, project_id);

                CREATE TABLE IF NOT EXISTS project_skill_run_provenance (
                    run_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    skill_slug TEXT NOT NULL,
                    version TEXT NOT NULL,
                    skill_sha256 TEXT NOT NULL,
                    trigger_mode TEXT NOT NULL,
                    references_json TEXT NOT NULL DEFAULT '[]',
                    context_chars INTEGER NOT NULL DEFAULT 0,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    loaded_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, project_id, skill_slug)
                );
                CREATE INDEX IF NOT EXISTS idx_project_skill_provenance_session
                    ON project_skill_run_provenance(session_id, loaded_at DESC);

                CREATE TRIGGER IF NOT EXISTS cleanup_project_skill_state_after_session_delete
                AFTER DELETE ON sessions BEGIN
                    DELETE FROM project_skill_session_state WHERE session_id = OLD.id;
                    DELETE FROM project_skill_run_provenance WHERE session_id = OLD.id;
                END;

                CREATE TRIGGER IF NOT EXISTS cleanup_project_skill_state_after_session_move
                AFTER UPDATE OF project_id ON sessions
                WHEN OLD.project_id IS NOT NEW.project_id BEGIN
                    DELETE FROM project_skill_session_state WHERE session_id = OLD.id;
                END;

                CREATE TRIGGER IF NOT EXISTS cleanup_project_skill_state_after_project_delete
                AFTER DELETE ON projects BEGIN
                    DELETE FROM project_skill_session_state WHERE project_id = OLD.id;
                END;

                CREATE TRIGGER IF NOT EXISTS cleanup_project_skill_provenance_after_run_delete
                AFTER DELETE ON runs BEGIN
                    DELETE FROM project_skill_run_provenance WHERE run_id = OLD.id;
                END;
                """
                )
            self._schema_key = schema_key

    def _session_scope(self, session_id: str) -> tuple[dict[str, Any], Optional[str]]:
        session = self.database.get_session(session_id)
        if not session:
            raise ProjectSkillSessionNotFound(f"Session was not found: {session_id}")
        project_id = session.get("project_id")
        if not project_id:
            return session, None
        project = self.database.get_project(project_id)
        if not project or project.get("archived"):
            raise ProjectSkillScopeError(
                "The session does not belong to an active project."
            )
        return session, str(project_id)

    def _state_rows(self, session_id: str, project_id: str) -> dict[str, dict[str, Any]]:
        with self.database.get_db_conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM project_skill_session_state
                WHERE session_id=? AND project_id=?
                """,
                (session_id, project_id),
            ).fetchall()
        return {str(row["skill_slug"]): dict(row) for row in rows}

    def catalog_for_session(self, session_id: str) -> dict[str, Any]:
        self.ensure_schema()
        _session, project_id = self._session_scope(session_id)
        if not project_id:
            return {"session_id": session_id, "project_id": None, "skills": []}
        states = self._state_rows(session_id, project_id)
        catalog = []
        for item in self.store.list(project_id):
            skill = dict(item)
            state = states.get(str(skill["slug"]))
            stale = bool(
                state
                and state.get("mode") == "enabled"
                and state.get("approved_sha256") != skill.get("sha256")
            )
            if skill.get("enabled") is not True:
                active = False
                trigger_mode = "project_disabled"
            elif state:
                active = state.get("mode") == "enabled" and not stale
                trigger_mode = state.get("scope") if active else "disabled"
            else:
                active = skill.get("enabled") is True
                trigger_mode = "project_default" if active else None
            skill.update(
                {
                    "active": active,
                    "trigger_mode": trigger_mode,
                    "activation_stale": stale,
                    "session_override": state.get("mode") if state else "inherit",
                    "session_scope": state.get("scope") if state else None,
                }
            )
            catalog.append(skill)
        return {"session_id": session_id, "project_id": project_id, "skills": catalog}

    def set_session_state(
        self,
        session_id: str,
        skill_slug: str,
        *,
        mode: str,
        scope: str = "session",
        expected_sha256: Optional[str] = None,
    ) -> dict[str, Any]:
        self.ensure_schema()
        if mode not in {"enabled", "disabled", "inherit"}:
            raise ProjectSkillValidationError("Skill session mode is invalid.")
        if scope not in {"session", "turn"}:
            raise ProjectSkillValidationError("Skill session scope is invalid.")
        if mode == "disabled" and scope == "turn":
            raise ProjectSkillValidationError("Turn scope only supports enabling a skill.")
        _session, project_id = self._session_scope(session_id)
        if not project_id:
            raise ProjectSkillScopeError(
                "Independent sessions cannot activate project-scoped skills."
            )
        skill = self.store.get(project_id, skill_slug)
        if mode == "enabled" and skill.get("enabled") is not True:
            raise ProjectSkillScopeError(
                "A disabled Project Skill cannot be activated for a session."
            )
        if expected_sha256 and expected_sha256 != skill.get("sha256"):
            raise ProjectSkillValidationError(
                "Skill content changed; refresh before changing its session state."
            )
        with self.database.get_db_conn() as conn:
            if mode == "inherit":
                conn.execute(
                    """
                    DELETE FROM project_skill_session_state
                    WHERE session_id=? AND project_id=? AND skill_slug=?
                    """,
                    (session_id, project_id, skill["slug"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO project_skill_session_state(
                        session_id, project_id, skill_slug, mode, scope,
                        approved_sha256, activated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, project_id, skill_slug) DO UPDATE SET
                        mode=excluded.mode,
                        scope=excluded.scope,
                        approved_sha256=excluded.approved_sha256,
                        activated_at=excluded.activated_at
                    """,
                    (
                        session_id,
                        project_id,
                        skill["slug"],
                        mode,
                        scope,
                        str(skill["sha256"]),
                        _now(),
                    ),
                )
        return self.catalog_for_session(session_id)

    def purge_skill_state(self, project_id: str, skill_slug: str) -> None:
        """Forget live activation overrides after a skill package is deleted."""

        self.ensure_schema()
        normalized_slug = self.store.normalize_slug(skill_slug)
        with self.database.get_db_conn() as conn:
            conn.execute(
                """
                DELETE FROM project_skill_session_state
                WHERE project_id=? AND skill_slug=?
                """,
                (project_id, normalized_slug),
            )

    def _effective_skills(
        self,
        session_id: str,
        *,
        consume_turn: bool,
    ) -> tuple[Optional[str], list[dict[str, Any]]]:
        catalog = self.catalog_for_session(session_id)
        project_id = catalog["project_id"]
        active = [item for item in catalog["skills"] if item.get("active")]
        if len(active) > MAX_ACTIVE_SKILLS:
            raise ProjectSkillScopeError(
                f"At most {MAX_ACTIVE_SKILLS} Project Skills may be active for one session."
            )
        loaded = [self.store.get(project_id, item["slug"]) | {
            "trigger_mode": item["trigger_mode"]
        } for item in active]
        if consume_turn and project_id:
            turn_slugs = [
                item["slug"] for item in active if item.get("trigger_mode") == "turn"
            ]
            if turn_slugs:
                placeholders = ",".join("?" for _ in turn_slugs)
                with self.database.get_db_conn() as conn:
                    conn.execute(
                        f"""
                        DELETE FROM project_skill_session_state
                        WHERE session_id=? AND project_id=? AND scope='turn'
                          AND skill_slug IN ({placeholders})
                        """,
                        (session_id, project_id, *turn_slugs),
                    )
        return project_id, loaded

    def _reference_content(self, project_id: str, skill_slug: str, reference: Any) -> str:
        if isinstance(reference, dict) and isinstance(reference.get("content"), str):
            return reference["content"]
        path = reference.get("path") if isinstance(reference, dict) else str(reference)
        reader = getattr(self.store, "read_reference", None)
        if not path or not callable(reader):
            return ""
        loaded = reader(project_id, skill_slug, str(path))
        if isinstance(loaded, dict):
            return str(loaded.get("content") or "")
        return str(loaded or "")

    def _selected_references(
        self,
        project_id: str,
        skill: dict[str, Any],
        user_query: str,
    ) -> list[dict[str, Any]]:
        terms = _query_terms(user_query)
        ranked: list[tuple[int, str, int, dict[str, Any], str, int, str]] = []
        for raw in skill.get("references") or []:
            reference = dict(raw) if isinstance(raw, dict) else {"path": str(raw)}
            path = str(reference.get("path") or "")
            content = self._reference_content(project_id, skill["slug"], reference)
            if not content:
                continue
            path_folded = path.casefold()
            path_score = sum(4 for term in terms if term in path_folded)
            step = max(1, REFERENCE_CHUNK_CHARS - REFERENCE_CHUNK_OVERLAP_CHARS)
            starts = range(0, len(content), step) if terms else (0,)
            for start in starts:
                chunk = content[start:start + REFERENCE_CHUNK_CHARS]
                if not chunk:
                    continue
                folded = chunk.casefold()
                content_score = sum(1 for term in terms if term in folded)
                score = path_score + content_score
                if path_score and start == 0:
                    score += 1
                ranked.append(
                    (score, path_folded, start, reference, content, start, chunk)
                )
        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
        selected_chunks: list[dict[str, Any]] = []
        remaining = MAX_REFERENCE_CHARS_PER_SKILL
        selected_paths: set[str] = set()
        chunks_per_path: dict[str, int] = {}
        for score, path_key, _sort_offset, reference, content, start, chunk in ranked:
            if len(selected_chunks) >= MAX_REFERENCE_CHUNKS_PER_SKILL:
                break
            if path_key not in selected_paths and len(selected_paths) >= MAX_REFERENCE_FILES_PER_SKILL:
                continue
            if chunks_per_path.get(path_key, 0) >= 2:
                continue
            if terms and score <= 0 and selected_chunks:
                continue
            clipped = chunk[:remaining]
            if not clipped:
                break
            selected_paths.add(path_key)
            chunks_per_path[path_key] = chunks_per_path.get(path_key, 0) + 1
            selected_chunks.append(
                {
                    "path": str(reference.get("path") or ""),
                    "sha256": reference.get("sha256"),
                    "content": clipped,
                    "document_chars": len(content),
                    "start": start,
                    "end": start + len(clipped),
                    "chunk_truncated": len(chunk) > len(clipped),
                }
            )
            remaining -= len(clipped)
            if remaining <= 0:
                break

        grouped: dict[str, dict[str, Any]] = {}
        for chunk in selected_chunks:
            path = chunk["path"]
            group = grouped.setdefault(
                path,
                {
                    "path": path,
                    "sha256": chunk.get("sha256"),
                    "document_chars": chunk["document_chars"],
                    "chunks": [],
                },
            )
            group["chunks"].append(chunk)

        selected: list[dict[str, Any]] = []
        for group in grouped.values():
            chunks = sorted(group["chunks"], key=lambda item: item["start"])
            parts = [
                f"[CHUNK chars {chunk['start']}-{chunk['end']}]\n{chunk['content']}"
                for chunk in chunks
            ]
            covered = 0
            covered_until = 0
            for chunk in chunks:
                if chunk["end"] > covered_until:
                    covered += chunk["end"] - max(chunk["start"], covered_until)
                    covered_until = chunk["end"]
            selected.append(
                {
                    "path": group["path"],
                    "sha256": group.get("sha256"),
                    "content": "\n\n".join(parts),
                    "segments": [
                        {"start": chunk["start"], "end": chunk["end"]}
                        for chunk in chunks
                    ],
                    "truncated": (
                        covered < group["document_chars"]
                        or any(chunk["chunk_truncated"] for chunk in chunks)
                    ),
                }
            )
        return selected

    def build_prompt_context(
        self,
        session_id: str,
        user_query: str,
        *,
        run_id: Optional[str] = None,
        consume_turn: bool = False,
    ) -> dict[str, Any]:
        project_id, skills = self._effective_skills(
            session_id,
            consume_turn=consume_turn,
        )
        if not project_id or not skills:
            return {
                "project_id": project_id,
                "context": "",
                "skills": [],
                "truncated": False,
            }

        blocks = []
        evidence = []
        remaining = MAX_PROJECT_SKILL_CONTEXT_CHARS
        overall_truncated = False
        for skill in skills:
            instructions = str(skill.get("instructions") or "")
            instruction_text = instructions[:MAX_INSTRUCTIONS_PER_SKILL_CHARS]
            skill_truncated = len(instructions) > len(instruction_text)
            references = self._selected_references(project_id, skill, user_query)
            header = json.dumps(
                {
                    "project_id": project_id,
                    "slug": skill["slug"],
                    "name": skill.get("name"),
                    "version": skill.get("version"),
                    "sha256": skill.get("sha256"),
                    "trigger_mode": skill.get("trigger_mode"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            block_parts = [
                f"--- BEGIN PROJECT SKILL {header} ---",
                instruction_text,
            ]
            for reference in references:
                block_parts.extend(
                    [
                        f"[REFERENCE {reference['path']}]",
                        reference["content"],
                        "[END REFERENCE]",
                    ]
                )
                skill_truncated = skill_truncated or bool(reference["truncated"])
            block_parts.append("--- END PROJECT SKILL ---")
            block = "\n".join(block_parts)
            clipped = block[:remaining]
            if not clipped:
                overall_truncated = True
                break
            if len(clipped) < len(block):
                skill_truncated = True
                overall_truncated = True
            blocks.append(clipped)
            evidence.append(
                {
                    "slug": skill["slug"],
                    "version": skill.get("version"),
                    "sha256": skill.get("sha256"),
                    "trigger_mode": skill.get("trigger_mode"),
                    "references": [
                        {
                            "path": reference["path"],
                            "sha256": reference.get("sha256"),
                            "segments": reference.get("segments") or [],
                            "truncated": reference["truncated"],
                        }
                        for reference in references
                    ],
                    "context_chars": len(clipped),
                    "truncated": skill_truncated,
                }
            )
            remaining -= len(clipped)
            if remaining <= 0:
                break

        context = "\n\n".join(blocks)
        if run_id:
            self.record_provenance(
                run_id,
                session_id,
                project_id,
                evidence,
            )
        return {
            "project_id": project_id,
            "context": context,
            "skills": evidence,
            "truncated": overall_truncated or any(item["truncated"] for item in evidence),
        }

    def record_provenance(
        self,
        run_id: str,
        session_id: str,
        project_id: str,
        evidence: Iterable[dict[str, Any]],
    ) -> None:
        self.ensure_schema()
        now = _now()
        with self.database.get_db_conn() as conn:
            for item in evidence:
                conn.execute(
                    """
                    INSERT INTO project_skill_run_provenance(
                        run_id, session_id, project_id, skill_slug, version,
                        skill_sha256, trigger_mode, references_json,
                        context_chars, truncated, loaded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, project_id, skill_slug) DO UPDATE SET
                        version=excluded.version,
                        skill_sha256=excluded.skill_sha256,
                        trigger_mode=excluded.trigger_mode,
                        references_json=excluded.references_json,
                        context_chars=excluded.context_chars,
                        truncated=excluded.truncated,
                        loaded_at=excluded.loaded_at
                    """,
                    (
                        run_id,
                        session_id,
                        project_id,
                        item["slug"],
                        str(item.get("version") or ""),
                        str(item.get("sha256") or ""),
                        str(item.get("trigger_mode") or ""),
                        json.dumps(item.get("references") or [], ensure_ascii=False),
                        int(item.get("context_chars") or 0),
                        int(bool(item.get("truncated"))),
                        now,
                    ),
                )

    def run_provenance(self, run_id: str) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self.database.get_db_conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM project_skill_run_provenance
                WHERE run_id=? ORDER BY loaded_at, skill_slug
                """,
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["references"] = _loads(item.pop("references_json", None), [])
            item["truncated"] = bool(item.get("truncated"))
            result.append(item)
        return result
