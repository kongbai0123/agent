"""Bounded, session-derived Project Skills attachments for Hermes runs.

This adapter deliberately has no filesystem or project lookup API.  The
ProjectSkillRuntime remains the single authority that resolves a Workbench
session to its project and determines which skills are active for that turn.
Only the already-bounded prompt context and non-secret provenance metadata are
passed across the Hermes boundary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Optional
from urllib.parse import quote

from project_skill_runtime import (
    MAX_ACTIVE_SKILLS,
    MAX_PROJECT_SKILL_CONTEXT_CHARS,
    ProjectSkillRuntime,
)


# ProjectSkillRuntime budgets blocks at 32k characters; joining up to eight
# blocks can add a small separator overhead.  The remainder covers a compact
# source manifest for those skills and their selected reference metadata.  The
# adapter fails closed if a custom runtime violates either boundary.
MAX_HERMES_RESOLVED_PROJECT_SKILL_CONTEXT_CHARS = (
    MAX_PROJECT_SKILL_CONTEXT_CHARS + (2 * (MAX_ACTIVE_SKILLS - 1))
)
MAX_HERMES_PROJECT_SKILL_INSTRUCTIONS_CHARS = 64_000
SOURCE_KIND = "workbench_project_skill"


class HermesProjectSkillBridgeError(ValueError):
    """Raised when a runtime result cannot safely cross the Hermes boundary."""


def _safe_reference_path(value: Any) -> str:
    path = str(value or "")
    parts = path.split("/")
    parsed = PurePosixPath(path)
    if (
        not path
        or path != path.strip()
        or "\\" in path
        or ":" in path
        or path.startswith("/")
        or parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise HermesProjectSkillBridgeError(
            "Project Skill reference provenance contains an unsafe path."
        )
    return path


def _source_id(project_id: str, slug: str) -> str:
    canonical = json.dumps(
        [project_id, slug],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"project-skill-{hashlib.sha256(canonical).hexdigest()}"


@dataclass(frozen=True)
class HermesProjectSkillReference:
    """Reference metadata only; reference contents stay in bounded context."""

    path: str
    sha256: Optional[str]
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class HermesProjectSkillSource:
    """Stable provenance for one project-qualified skill source."""

    source_id: str
    source_uri: str
    project_id: str
    slug: str
    version: str
    sha256: str
    trigger_mode: str
    references: tuple[HermesProjectSkillReference, ...]
    context_chars: int
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": SOURCE_KIND,
            "source_id": self.source_id,
            "source_uri": self.source_uri,
            "project_id": self.project_id,
            "slug": self.slug,
            "version": self.version,
            "sha256": self.sha256,
            "trigger_mode": self.trigger_mode,
            "references": [item.as_dict() for item in self.references],
            "context_chars": self.context_chars,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class HermesProjectSkillsAttachment:
    """Prepared Project Skills data suitable for a Hermes Runs client hook."""

    session_id: str
    project_id: Optional[str]
    workbench_run_id: Optional[str]
    instructions: str
    sources: tuple[HermesProjectSkillSource, ...]
    truncated: bool

    @property
    def has_skills(self) -> bool:
        return bool(self.sources)

    @property
    def provenance(self) -> list[dict[str, Any]]:
        return [source.as_dict() for source in self.sources]

    def merge_instructions(self, base_instructions: Optional[str] = None) -> str:
        """Append the scoped attachment to caller-owned Hermes instructions."""

        base = str(base_instructions or "").strip()
        if not self.instructions:
            return base
        if not base:
            return self.instructions
        return f"{base}\n\n{self.instructions}"

    def as_run_kwargs(
        self,
        base_instructions: Optional[str] = None,
    ) -> dict[str, Optional[str]]:
        """Arguments accepted by HermesRunsBridge.create_run()."""

        merged = self.merge_instructions(base_instructions)
        return {"instructions": merged or None}


class HermesProjectSkillsBridge:
    """Create Hermes attachments exclusively from a Workbench session id."""

    def __init__(self, runtime: ProjectSkillRuntime) -> None:
        self.runtime = runtime

    @staticmethod
    def _source(
        project_id: str,
        raw: Mapping[str, Any],
    ) -> HermesProjectSkillSource:
        slug = str(raw.get("slug") or "").strip()
        version = str(raw.get("version") or "").strip()
        digest = str(raw.get("sha256") or "").strip()
        trigger_mode = str(raw.get("trigger_mode") or "").strip()
        if not slug or not version or not digest or not trigger_mode:
            raise HermesProjectSkillBridgeError(
                "Project Skill provenance is missing a required identity field."
            )

        references = []
        for value in raw.get("references") or []:
            if not isinstance(value, Mapping):
                raise HermesProjectSkillBridgeError(
                    "Project Skill reference provenance is invalid."
                )
            references.append(
                HermesProjectSkillReference(
                    path=_safe_reference_path(value.get("path")),
                    sha256=(
                        str(value["sha256"])
                        if value.get("sha256") is not None
                        else None
                    ),
                    truncated=bool(value.get("truncated")),
                )
            )

        return HermesProjectSkillSource(
            source_id=_source_id(project_id, slug),
            source_uri=(
                "workbench-project-skill://"
                f"{quote(project_id, safe='')}/{quote(slug, safe='')}"
            ),
            project_id=project_id,
            slug=slug,
            version=version,
            sha256=digest,
            trigger_mode=trigger_mode,
            references=tuple(references),
            context_chars=int(raw.get("context_chars") or 0),
            truncated=bool(raw.get("truncated")),
        )

    @staticmethod
    def _instructions(
        context: str,
        sources: tuple[HermesProjectSkillSource, ...],
    ) -> str:
        if not sources:
            if context:
                raise HermesProjectSkillBridgeError(
                    "Project Skill context has no matching provenance."
                )
            return ""
        if not context:
            raise HermesProjectSkillBridgeError(
                "Project Skill provenance has no matching bounded context."
            )
        if len(context) > MAX_HERMES_RESOLVED_PROJECT_SKILL_CONTEXT_CHARS:
            raise HermesProjectSkillBridgeError(
                "Project Skill runtime context exceeded its safe boundary."
            )

        manifest = json.dumps(
            [source.as_dict() for source in sources],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        attachment = "\n".join(
            (
                "--- BEGIN WORKBENCH PROJECT SKILLS ATTACHMENT ---",
                "Use only the session-scoped skills resolved below. Do not scan for "
                "or infer any other skill or project location.",
                f"SOURCE_MANIFEST={manifest}",
                context,
                "--- END WORKBENCH PROJECT SKILLS ATTACHMENT ---",
            )
        )
        if len(attachment) > MAX_HERMES_PROJECT_SKILL_INSTRUCTIONS_CHARS:
            raise HermesProjectSkillBridgeError(
                "Project Skill attachment exceeded the Hermes instruction boundary."
            )
        return attachment

    def prepare(
        self,
        session_id: str,
        user_query: str,
        *,
        run_id: Optional[str] = None,
        consume_turn: bool = False,
    ) -> HermesProjectSkillsAttachment:
        """Resolve and package active skills without accepting a project/path input."""

        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            raise HermesProjectSkillBridgeError("A Workbench session id is required.")
        if not isinstance(user_query, str):
            raise HermesProjectSkillBridgeError("The Project Skill query must be text.")

        resolved = self.runtime.build_prompt_context(
            normalized_session_id,
            user_query,
            run_id=run_id,
            consume_turn=consume_turn,
        )
        if not isinstance(resolved, Mapping):
            raise HermesProjectSkillBridgeError(
                "Project Skill runtime returned an invalid attachment."
            )

        raw_project_id = resolved.get("project_id")
        project_id = str(raw_project_id) if raw_project_id is not None else None
        raw_skills = resolved.get("skills") or []
        if not isinstance(raw_skills, list):
            raise HermesProjectSkillBridgeError(
                "Project Skill runtime returned invalid provenance."
            )
        if raw_skills and not project_id:
            raise HermesProjectSkillBridgeError(
                "Project Skill runtime returned sources without a project scope."
            )

        sources = tuple(
            self._source(project_id, raw)
            for raw in raw_skills
            if isinstance(raw, Mapping) and project_id is not None
        )
        if len(sources) != len(raw_skills):
            raise HermesProjectSkillBridgeError(
                "Project Skill runtime returned malformed source provenance."
            )
        if len({source.source_id for source in sources}) != len(sources):
            raise HermesProjectSkillBridgeError(
                "Project Skill runtime returned duplicate sources."
            )

        context = str(resolved.get("context") or "")
        instructions = self._instructions(context, sources)
        return HermesProjectSkillsAttachment(
            session_id=normalized_session_id,
            project_id=project_id,
            workbench_run_id=run_id,
            instructions=instructions,
            sources=sources,
            truncated=bool(resolved.get("truncated")),
        )


__all__ = [
    "HermesProjectSkillBridgeError",
    "HermesProjectSkillReference",
    "HermesProjectSkillSource",
    "HermesProjectSkillsAttachment",
    "HermesProjectSkillsBridge",
    "MAX_HERMES_PROJECT_SKILL_INSTRUCTIONS_CHARS",
    "MAX_HERMES_RESOLVED_PROJECT_SKILL_CONTEXT_CHARS",
    "SOURCE_KIND",
]
