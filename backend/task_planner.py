"""Deterministic host-side task planning and progress contracts.

This module deliberately does not call a model and does not execute tools.  It
turns a user request plus the currently available tool descriptions into a
small, bounded dependency graph.  A host runtime can then use ``PlanProgress``
to enforce the graph, wall-clock budget, per-step tool-call budgets and
fail-safe handling of indeterminate external side effects.

The planner is intentionally conservative.  Its fallback is deterministic so
the Agent still has an auditable plan when a planning model is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LIST_PREFIX = re.compile(r"^\s*(?:[-*•]|\d{1,3}[.)、])\s*")
_CLAUSE_SEPARATOR = re.compile(
    r"(?:\r?\n)+|\s*[;；]\s*|(?<=[,，])\s*(?=(?:再|接著|然後|最後|並且|and\s+then\b|then\b))",
    re.IGNORECASE,
)
_LEADING_SEQUENCE_WORD = re.compile(
    r"^(?:先|再|接著|然後|最後|並且|and\s+then|then)\s*", re.IGNORECASE
)
_COMPACT_SEQUENCE_START = re.compile(r"^\s*先")
_COMPACT_SEQUENCE_SPLIT = re.compile(
    r"(?<!不)(?=(?:再|接著|然後|最後)\s*(?:搜尋|查詢|查找|讀取|閱讀|開啟|比較|"
    r"分析|整理|撰寫|寫入|建立|更新|修改|驗證|產生|輸出|search|find|read|open|"
    r"compare|analy[sz]e|summari[sz]e|write|create|update|verify))",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]*|[\u3400-\u9fff]{2,}", re.IGNORECASE)
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
_SEQUENCE_MARKER = re.compile(
    r"(?:^|[\s,，;；])(?:先|再|接著|然後|最後|第一|第二|第三)"
    r"|\b(?:first|second|third|next|finally|then|and\s+then)\b",
    re.IGNORECASE,
)
_ACTION_HINT = re.compile(
    r"(?:搜尋|查詢|查找|讀取|比較|分析|整理|撰寫|寫入|建立|更新|修改|驗證|"
    r"search|find|read|compare|analy[sz]e|summari[sz]e|write|create|update|verify)",
    re.IGNORECASE,
)
_CAPABILITY_ALIASES = (
    frozenset({"search", "find", "query", "搜尋", "查詢", "查找"}),
    frozenset({"read", "open", "讀取", "閱讀", "開啟"}),
    frozenset({"write", "create", "update", "edit", "撰寫", "建立", "更新", "修改"}),
    frozenset({"compare", "rank", "比較", "排序", "重排"}),
    frozenset({"browser", "navigate", "chrome", "瀏覽器", "網頁", "導覽"}),
    frozenset({"file", "document", "docs", "檔案", "文件"}),
)


class PlannerError(RuntimeError):
    """Base error with a stable machine-readable code."""

    code = "TASK_PLANNER_ERROR"

    def __init__(self, message: str, *, details: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": self.details}


class PlanValidationError(PlannerError, ValueError):
    code = "TASK_PLAN_INVALID"


class PlanStateError(PlannerError):
    code = "TASK_PLAN_STATE_INVALID"


class PlanBudgetExceeded(PlannerError):
    code = "TASK_PLAN_BUDGET_EXCEEDED"


class PlanDeadlineExceeded(PlannerError, TimeoutError):
    code = "TASK_PLAN_DEADLINE_EXCEEDED"


class StepKind(str, Enum):
    REASON = "reason"
    TOOL = "tool"
    VERIFY = "verify"
    SYNTHESIZE = "synthesize"


class VerificationKind(str, Enum):
    OUTPUT_NONEMPTY = "output_nonempty"
    TOOL_CALLS_SUCCEEDED = "tool_calls_succeeded"
    DEPENDENCIES_SUCCEEDED = "dependencies_succeeded"
    NO_EXECUTION_UNKNOWN = "no_execution_unknown"
    EVIDENCE_FLAG = "evidence_flag"


class StepStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    EXECUTION_UNKNOWN = "execution_unknown"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class VerificationStatus(str, Enum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class PlanStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXECUTION_UNKNOWN = "execution_unknown"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class ExecutionOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXECUTION_UNKNOWN = "execution_unknown"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class PlanLimits:
    """Hard limits copied into every plan and enforced again at runtime."""

    max_steps: int = 12
    max_tool_calls: int = 24
    max_tool_calls_per_step: int = 4
    max_wall_seconds: float = 900.0
    max_request_chars: int = 16_000
    max_tools_per_step: int = 4

    def __post_init__(self) -> None:
        integer_limits = {
            "max_steps": (self.max_steps, 3, 64),
            "max_tool_calls": (self.max_tool_calls, 0, 256),
            "max_tool_calls_per_step": (self.max_tool_calls_per_step, 0, 32),
            "max_request_chars": (self.max_request_chars, 1, 100_000),
            "max_tools_per_step": (self.max_tools_per_step, 0, 16),
        }
        for name, (value, minimum, maximum) in integer_limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise PlanValidationError(
                    f"{name} must be between {minimum} and {maximum}",
                    details={"field": name},
                )
        wall_seconds = float(self.max_wall_seconds)
        if not math.isfinite(wall_seconds) or not 1.0 <= wall_seconds <= 86_400.0:
            raise PlanValidationError(
                "max_wall_seconds must be between 1 and 86400",
                details={"field": "max_wall_seconds"},
            )
        object.__setattr__(self, "max_wall_seconds", wall_seconds)

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_tool_calls_per_step": self.max_tool_calls_per_step,
            "max_wall_seconds": self.max_wall_seconds,
            "max_request_chars": self.max_request_chars,
            "max_tools_per_step": self.max_tools_per_step,
        }


@dataclass(frozen=True)
class PlannerTool:
    name: str
    description: str

    def __post_init__(self) -> None:
        name = str(self.name or "").strip().casefold()
        description = " ".join(str(self.description or "").split())
        if len(name) > 160 or not _TOOL_NAME.fullmatch(name):
            raise PlanValidationError("tool name is invalid", details={"tool": name[:160]})
        if not description or len(description) > 1_000:
            raise PlanValidationError("tool description is invalid", details={"tool": name})
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)


@dataclass(frozen=True)
class VerificationCondition:
    kind: VerificationKind
    description: str
    source_step_id: Optional[str] = None
    evidence_key: Optional[str] = None

    def __post_init__(self) -> None:
        try:
            kind = self.kind if isinstance(self.kind, VerificationKind) else VerificationKind(str(self.kind))
        except ValueError as error:
            raise PlanValidationError("verification kind is invalid") from error
        description = " ".join(str(self.description or "").split())
        if not description or len(description) > 500:
            raise PlanValidationError("verification description is invalid")
        source = str(self.source_step_id or "").strip() or None
        key = str(self.evidence_key or "").strip() or None
        if kind is VerificationKind.EVIDENCE_FLAG and not key:
            raise PlanValidationError("evidence_flag requires evidence_key")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "source_step_id", source)
        object.__setattr__(self, "evidence_key", key)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "description": self.description,
            "source_step_id": self.source_step_id,
            "evidence_key": self.evidence_key,
        }


@dataclass(frozen=True)
class TaskStep:
    step_id: str
    order: int
    kind: StepKind
    title: str
    instruction: str
    dependencies: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    tool_budget: int = 0
    verification: tuple[VerificationCondition, ...] = ()

    def __post_init__(self) -> None:
        step_id = str(self.step_id or "").strip()
        if not step_id or len(step_id) > 80 or _CONTROL_CHARACTERS.search(step_id):
            raise PlanValidationError("step_id is invalid")
        if isinstance(self.order, bool) or not isinstance(self.order, int) or self.order < 0:
            raise PlanValidationError("step order must be a non-negative integer")
        try:
            kind = self.kind if isinstance(self.kind, StepKind) else StepKind(str(self.kind))
        except ValueError as error:
            raise PlanValidationError("step kind is invalid", details={"step_id": step_id}) from error
        title = " ".join(str(self.title or "").split())
        instruction = " ".join(str(self.instruction or "").split())
        if not title or len(title) > 200:
            raise PlanValidationError("step title is invalid", details={"step_id": step_id})
        if not instruction or len(instruction) > 4_000:
            raise PlanValidationError("step instruction is invalid", details={"step_id": step_id})
        dependencies = tuple(dict.fromkeys(str(item or "").strip() for item in self.dependencies))
        allowed_tools = tuple(dict.fromkeys(str(item or "").strip().casefold() for item in self.allowed_tools))
        if any(not item for item in dependencies) or any(not _TOOL_NAME.fullmatch(item) for item in allowed_tools):
            raise PlanValidationError("step dependencies or tools are invalid", details={"step_id": step_id})
        if isinstance(self.tool_budget, bool) or not isinstance(self.tool_budget, int) or self.tool_budget < 0:
            raise PlanValidationError("step tool budget must be a non-negative integer")
        if self.tool_budget and not allowed_tools:
            raise PlanValidationError("a tool budget requires at least one allowed tool", details={"step_id": step_id})
        if not self.tool_budget and allowed_tools:
            raise PlanValidationError("allowed tools require a positive tool budget", details={"step_id": step_id})
        object.__setattr__(self, "step_id", step_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "instruction", instruction)
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "allowed_tools", allowed_tools)
        object.__setattr__(self, "verification", tuple(self.verification))

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "order": self.order,
            "kind": self.kind.value,
            "title": self.title,
            "instruction": self.instruction,
            "dependencies": list(self.dependencies),
            "allowed_tools": list(self.allowed_tools),
            "tool_budget": self.tool_budget,
            "verification": [condition.as_dict() for condition in self.verification],
        }


@dataclass(frozen=True)
class TaskPlan:
    plan_id: str
    request: str
    steps: tuple[TaskStep, ...]
    limits: PlanLimits = field(default_factory=PlanLimits)
    planner: str = "deterministic_fallback_v1"

    def __post_init__(self) -> None:
        plan_id = str(self.plan_id or "").strip()
        request = " ".join(str(self.request or "").split())
        if not plan_id or len(plan_id) > 96:
            raise PlanValidationError("plan_id is invalid")
        if not request or len(request) > self.limits.max_request_chars:
            raise PlanValidationError("request is empty or exceeds the plan limit")
        steps = tuple(self.steps)
        if not steps or len(steps) > self.limits.max_steps:
            raise PlanValidationError("step count exceeds the plan limit")
        ids = [step.step_id for step in steps]
        if len(ids) != len(set(ids)):
            raise PlanValidationError("step ids must be unique")
        orders = [step.order for step in steps]
        if len(orders) != len(set(orders)):
            raise PlanValidationError("step order values must be unique")
        known = set(ids)
        allowed_tool_count = 0
        for step in steps:
            if step.tool_budget > self.limits.max_tool_calls_per_step:
                raise PlanValidationError(
                    "step tool budget exceeds the per-step limit",
                    details={"step_id": step.step_id},
                )
            if len(step.allowed_tools) > self.limits.max_tools_per_step:
                raise PlanValidationError(
                    "step exposes too many tools", details={"step_id": step.step_id}
                )
            unknown = sorted(set(step.dependencies) - known)
            if unknown:
                raise PlanValidationError(
                    "step has unknown dependencies",
                    details={"step_id": step.step_id, "dependencies": unknown},
                )
            if step.step_id in step.dependencies:
                raise PlanValidationError("step cannot depend on itself", details={"step_id": step.step_id})
            for condition in step.verification:
                if condition.source_step_id and condition.source_step_id not in known:
                    raise PlanValidationError(
                        "verification references an unknown step",
                        details={"step_id": step.step_id, "source_step_id": condition.source_step_id},
                    )
            allowed_tool_count += step.tool_budget
        if allowed_tool_count > self.limits.max_tool_calls:
            raise PlanValidationError("plan tool budget exceeds the total limit")
        object.__setattr__(self, "plan_id", plan_id)
        object.__setattr__(self, "request", request)
        object.__setattr__(self, "steps", steps)
        # Calling this during construction rejects cycles before a plan can run.
        self.topological_steps()

    def step(self, step_id: str) -> TaskStep:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise KeyError(step_id)

    def topological_steps(self) -> tuple[TaskStep, ...]:
        by_id = {step.step_id: step for step in self.steps}
        indegree = {step.step_id: len(step.dependencies) for step in self.steps}
        children: dict[str, list[str]] = {step.step_id: [] for step in self.steps}
        for step in self.steps:
            for dependency in step.dependencies:
                children[dependency].append(step.step_id)
        ready = sorted(
            (by_id[step_id] for step_id, count in indegree.items() if count == 0),
            key=lambda item: (item.order, item.step_id),
        )
        ordered: list[TaskStep] = []
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for child_id in children[current.step_id]:
                indegree[child_id] -= 1
                if indegree[child_id] == 0:
                    ready.append(by_id[child_id])
                    ready.sort(key=lambda item: (item.order, item.step_id))
        if len(ordered) != len(self.steps):
            cyclic = sorted(step_id for step_id, count in indegree.items() if count > 0)
            raise PlanValidationError("plan dependency graph contains a cycle", details={"steps": cyclic})
        return tuple(ordered)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "request": self.request,
            "planner": self.planner,
            "limits": self.limits.as_dict(),
            "steps": [step.as_dict() for step in self.topological_steps()],
        }


def _normalize_tools(tools: Iterable[Any]) -> tuple[PlannerTool, ...]:
    normalized: dict[str, PlannerTool] = {}
    for raw in tools:
        if isinstance(raw, PlannerTool):
            tool = raw
        elif isinstance(raw, Mapping):
            function = raw.get("function") if isinstance(raw.get("function"), Mapping) else raw
            tool = PlannerTool(
                name=str(function.get("name") or raw.get("name") or ""),
                description=str(function.get("description") or raw.get("description") or ""),
            )
        else:
            tool = PlannerTool(
                name=str(getattr(raw, "name", "")),
                description=str(getattr(raw, "description", "")),
            )
        existing = normalized.get(tool.name)
        if existing is not None and existing.description != tool.description:
            raise PlanValidationError("duplicate tool definitions disagree", details={"tool": tool.name})
        normalized[tool.name] = tool
    return tuple(normalized[name] for name in sorted(normalized))


def _request_clauses(request: str, maximum: int) -> tuple[str, ...]:
    parts = _CLAUSE_SEPARATOR.split(request)
    # 中文步驟常不使用標點，例如「先搜尋再讀取最後整理」。複雜任務
    # 偵測器會正確辨識這種句型，因此切句器也必須在後續順序詞前切開，
    # 否則整份計畫只會得到一個步驟，失去逐步工具預算與驗證。
    if len(parts) == 1 and _COMPACT_SEQUENCE_START.search(request):
        compact_parts = _COMPACT_SEQUENCE_SPLIT.split(request)
        if len(compact_parts) >= 2:
            parts = compact_parts
    comma_parts = [item.strip() for item in re.split(r"[,，、]", request) if item.strip()]
    if len(comma_parts) >= 3 and sum(bool(_ACTION_HINT.search(item)) for item in comma_parts) >= 2:
        parts = comma_parts
    clauses: list[str] = []
    for raw in parts:
        clause = _LEADING_SEQUENCE_WORD.sub("", _LIST_PREFIX.sub("", raw)).strip(" ,，。")
        if clause:
            clauses.append(" ".join(clause.split()))
    if not clauses:
        clauses = [" ".join(request.split())]
    if len(clauses) > maximum:
        clauses = clauses[: maximum - 1] + ["；".join(clauses[maximum - 1 :])]
    return tuple(clauses)


def _tokens(text: str) -> set[str]:
    result: set[str] = set()
    for token in _WORD.findall(text.casefold()):
        result.add(token)
        if re.fullmatch(r"[\u3400-\u9fff]{3,}", token):
            result.update(token[index : index + 2] for index in range(len(token) - 1))
    return result


def _rank_tools(clause: str, tools: Sequence[PlannerTool], maximum: int) -> tuple[str, ...]:
    if maximum <= 0:
        return ()
    clause_folded = clause.casefold()
    clause_tokens = _tokens(clause)
    clause_aliases = {
        member
        for group in _CAPABILITY_ALIASES
        if any(member in clause_folded for member in group)
        for member in group
    }
    ranked: list[tuple[int, str]] = []
    for tool in tools:
        name_parts = {part for part in re.split(r"[._-]+", tool.name) if len(part) >= 2}
        description_tokens = _tokens(tool.description)
        tool_text = f"{tool.name} {tool.description}".casefold()
        score = 0
        if tool.name in clause_folded:
            score += 100
        score += 12 * sum(1 for part in name_parts if part in clause_folded)
        overlap = clause_tokens & (name_parts | description_tokens)
        score += sum(min(len(token), 8) for token in overlap)
        for group in _CAPABILITY_ALIASES:
            if clause_aliases & group and any(member in tool_text for member in group):
                score += 8
        # A single generic two-character overlap (for example 「資料」) is too
        # weak to grant a tool budget.  Exact names/name parts still score well,
        # while natural-language matches need at least two useful characters
        # twice (or one sufficiently specific English term).
        if score >= 4:
            ranked.append((-score, tool.name))
    ranked.sort()
    return tuple(name for _, name in ranked[:maximum])


class DeterministicTaskPlanner:
    """Create a bounded plan without relying on a second model."""

    def __init__(self, limits: Optional[PlanLimits] = None) -> None:
        self.limits = limits or PlanLimits()

    def plan(self, request: str, tools: Iterable[Any] = ()) -> TaskPlan:
        normalized_request = " ".join(str(request or "").split())
        if not normalized_request or _CONTROL_CHARACTERS.search(normalized_request):
            raise PlanValidationError("request is empty or contains control characters")
        if len(normalized_request) > self.limits.max_request_chars:
            raise PlanValidationError("request exceeds the plan limit")
        normalized_tools = _normalize_tools(tools)
        # Reserve one verification step and one synthesis step.
        clauses = _request_clauses(str(request), self.limits.max_steps - 2)
        ranked_tools = [
            _rank_tools(clause, normalized_tools, self.limits.max_tools_per_step)
            for clause in clauses
        ]
        budgets = [0 for _ in clauses]
        tool_step_indexes = [
            index for index, candidates in enumerate(ranked_tools) if candidates
        ]
        # Distribute the plan-wide budget round-robin. Greedy allocation made
        # early steps consume the entire cap and silently downgraded later tool
        # steps to reasoning-only work. Every candidate step now receives one
        # call before any step receives its next call.
        remaining_tools = self.limits.max_tool_calls
        while remaining_tools > 0:
            allocated = False
            for index in tool_step_indexes:
                if budgets[index] >= self.limits.max_tool_calls_per_step:
                    continue
                budgets[index] += 1
                remaining_tools -= 1
                allocated = True
                if remaining_tools <= 0:
                    break
            if not allocated:
                break
        steps: list[TaskStep] = []
        previous: Optional[str] = None
        for index, clause in enumerate(clauses, start=1):
            step_id = f"step-{index:02d}"
            candidates = ranked_tools[index - 1]
            budget = budgets[index - 1]
            if not budget:
                candidates = ()
            kind = StepKind.TOOL if candidates else StepKind.REASON
            condition = VerificationCondition(
                kind=(
                    VerificationKind.TOOL_CALLS_SUCCEEDED
                    if kind is StepKind.TOOL
                    else VerificationKind.OUTPUT_NONEMPTY
                ),
                description=(
                    "所有已執行工具皆成功並產生可供下一步使用的結果。"
                    if kind is StepKind.TOOL
                    else "步驟產生非空白且可供下一步使用的結果。"
                ),
                source_step_id=step_id,
            )
            steps.append(
                TaskStep(
                    step_id=step_id,
                    order=len(steps),
                    kind=kind,
                    title=f"執行需求 {index}",
                    instruction=clause,
                    dependencies=(previous,) if previous else (),
                    allowed_tools=candidates,
                    tool_budget=budget,
                    verification=(condition,),
                )
            )
            previous = step_id

        verify_id = f"step-{len(steps) + 1:02d}"
        steps.append(
            TaskStep(
                step_id=verify_id,
                order=len(steps),
                kind=StepKind.VERIFY,
                title="驗證執行結果",
                instruction="確認所有必要步驟成功，且不存在結果不確定的外部操作。",
                dependencies=(previous,) if previous else (),
                verification=(
                    VerificationCondition(
                        kind=VerificationKind.DEPENDENCIES_SUCCEEDED,
                        description="所有相依步驟皆已成功。",
                    ),
                    VerificationCondition(
                        kind=VerificationKind.NO_EXECUTION_UNKNOWN,
                        description="本次執行沒有結果不確定的外部操作。",
                    ),
                ),
            )
        )
        synthesis_id = f"step-{len(steps) + 1:02d}"
        steps.append(
            TaskStep(
                step_id=synthesis_id,
                order=len(steps),
                kind=StepKind.SYNTHESIZE,
                title="整理最終回覆",
                instruction="只使用已完成且通過驗證的結果，整理成符合使用者要求的回覆。",
                dependencies=(verify_id,),
                verification=(
                    VerificationCondition(
                        kind=VerificationKind.OUTPUT_NONEMPTY,
                        description="最終回覆不可為空白。",
                        source_step_id=synthesis_id,
                    ),
                ),
            )
        )
        digest_payload = {
            "request": normalized_request,
            "tools": [(tool.name, tool.description) for tool in normalized_tools],
            "limits": self.limits.as_dict(),
            "steps": [step.as_dict() for step in steps],
        }
        digest = hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return TaskPlan(
            plan_id=f"plan_{digest[:24]}",
            request=normalized_request,
            steps=tuple(steps),
            limits=self.limits,
        )


@dataclass(frozen=True)
class VerificationResult:
    kind: VerificationKind
    passed: Optional[bool]
    description: str
    source_step_id: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "passed": self.passed,
            "description": self.description,
            "source_step_id": self.source_step_id,
        }


@dataclass
class StepProgress:
    status: StepStatus = StepStatus.PENDING
    tool_calls_used: int = 0
    verification_status: VerificationStatus = VerificationStatus.NOT_RUN
    verification_results: tuple[VerificationResult, ...] = ()
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def as_dict(self, step: TaskStep) -> dict[str, Any]:
        return {
            "step_id": step.step_id,
            "kind": step.kind.value,
            "status": self.status.value,
            "tool_calls_used": self.tool_calls_used,
            "tool_call_limit": step.tool_budget,
            "verification_status": self.verification_status.value,
            "verification_results": [result.as_dict() for result in self.verification_results],
            "error": (
                {"code": self.error_code, "message": self.error_message}
                if self.error_code
                else None
            ),
        }


_TERMINAL_STEPS = {
    StepStatus.SUCCEEDED,
    StepStatus.FAILED,
    StepStatus.BLOCKED,
    StepStatus.SKIPPED,
    StepStatus.EXECUTION_UNKNOWN,
    StepStatus.TIMED_OUT,
    StepStatus.CANCELLED,
}
_FAILED_DEPENDENCIES = _TERMINAL_STEPS - {StepStatus.SUCCEEDED}


class PlanProgress:
    """Mutable execution state for one immutable :class:`TaskPlan`."""

    def __init__(
        self,
        plan: TaskPlan,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.plan = plan
        self._clock = clock
        self._started_at = float(clock())
        self._states = {step.step_id: StepProgress() for step in plan.steps}
        self._total_tool_calls = 0
        self._stop_status: Optional[PlanStatus] = None
        self._stop_reason: Optional[str] = None
        self._refresh()

    @property
    def total_tool_calls_used(self) -> int:
        return self._total_tool_calls

    @property
    def stop_reason(self) -> Optional[str]:
        return self._stop_reason

    def progress_for(self, step_id: str) -> StepProgress:
        try:
            return self._states[step_id]
        except KeyError as error:
            raise PlanStateError("unknown plan step", details={"step_id": step_id}) from error

    def _check_deadline(self) -> None:
        if self._stop_status is not None:
            return
        if float(self._clock()) - self._started_at < self.plan.limits.max_wall_seconds:
            return
        for state in self._states.values():
            if state.status is StepStatus.RUNNING:
                state.status = StepStatus.TIMED_OUT
                state.error_code = "TASK_PLAN_DEADLINE_EXCEEDED"
                state.error_message = "步驟超過整體執行時間上限。"
            elif state.status not in _TERMINAL_STEPS:
                state.status = StepStatus.SKIPPED
                state.error_code = "TASK_PLAN_STOPPED"
                state.error_message = "整體執行時間已用盡。"
        self._stop_status = PlanStatus.TIMED_OUT
        self._stop_reason = "TASK_PLAN_DEADLINE_EXCEEDED"

    def _refresh(self) -> None:
        if self._stop_status is not None:
            return
        changed = True
        while changed:
            changed = False
            for step in self.plan.topological_steps():
                state = self._states[step.step_id]
                if state.status not in {StepStatus.PENDING, StepStatus.READY}:
                    continue
                dependency_states = [self._states[item].status for item in step.dependencies]
                if any(item in _FAILED_DEPENDENCIES for item in dependency_states):
                    state.status = StepStatus.BLOCKED
                    state.error_code = "TASK_PLAN_DEPENDENCY_FAILED"
                    state.error_message = "必要的前置步驟未成功。"
                    changed = True
                elif all(item is StepStatus.SUCCEEDED for item in dependency_states):
                    if state.status is not StepStatus.READY:
                        state.status = StepStatus.READY
                        changed = True
                elif state.status is StepStatus.READY:
                    state.status = StepStatus.PENDING
                    changed = True

    def ready_steps(self) -> tuple[TaskStep, ...]:
        self._check_deadline()
        self._refresh()
        return tuple(
            step
            for step in self.plan.topological_steps()
            if self._states[step.step_id].status is StepStatus.READY
        )

    def start_step(self, step_id: str) -> None:
        self._check_deadline()
        if self._stop_status is PlanStatus.TIMED_OUT:
            raise PlanDeadlineExceeded("plan wall-clock budget has expired")
        if self._stop_status is not None:
            raise PlanStateError("plan has already stopped", details={"status": self._stop_status.value})
        if any(state.status is StepStatus.RUNNING for state in self._states.values()):
            raise PlanStateError("only one plan step may run at a time")
        state = self.progress_for(step_id)
        if state.status is not StepStatus.READY:
            raise PlanStateError(
                "step is not ready", details={"step_id": step_id, "status": state.status.value}
            )
        state.status = StepStatus.RUNNING

    def consume_tool_call(self, step_id: str, count: int = 1) -> None:
        self._check_deadline()
        if self._stop_status is PlanStatus.TIMED_OUT:
            raise PlanDeadlineExceeded("plan wall-clock budget has expired")
        state = self.progress_for(step_id)
        step = self.plan.step(step_id)
        if state.status is not StepStatus.RUNNING:
            raise PlanStateError("tool calls are only allowed while a step is running")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise PlanStateError("tool call count must be a positive integer")
        if state.tool_calls_used + count > step.tool_budget:
            raise PlanBudgetExceeded(
                "step tool-call budget exceeded",
                details={"step_id": step_id, "limit": step.tool_budget},
            )
        if self._total_tool_calls + count > self.plan.limits.max_tool_calls:
            raise PlanBudgetExceeded(
                "plan tool-call budget exceeded",
                details={"limit": self.plan.limits.max_tool_calls},
            )
        state.tool_calls_used += count
        self._total_tool_calls += count

    def _verification_results(
        self,
        step: TaskStep,
        evidence: Mapping[str, Any],
    ) -> tuple[VerificationResult, ...]:
        results: list[VerificationResult] = []
        flags = evidence.get("checks") if isinstance(evidence.get("checks"), Mapping) else {}
        for condition in step.verification:
            if condition.kind is VerificationKind.OUTPUT_NONEMPTY:
                output = evidence.get("output")
                passed = bool(str(output).strip()) if output is not None else False
            elif condition.kind is VerificationKind.TOOL_CALLS_SUCCEEDED:
                # A model cannot satisfy a tool step merely by claiming that
                # tools succeeded; the host-side counter must prove that at
                # least one budgeted call actually occurred.
                passed = (
                    evidence.get("tool_calls_succeeded") is True
                    and self._states[step.step_id].tool_calls_used > 0
                )
            elif condition.kind is VerificationKind.DEPENDENCIES_SUCCEEDED:
                passed = all(
                    self._states[dependency].status is StepStatus.SUCCEEDED
                    for dependency in step.dependencies
                )
            elif condition.kind is VerificationKind.NO_EXECUTION_UNKNOWN:
                passed = not any(
                    state.status is StepStatus.EXECUTION_UNKNOWN for state in self._states.values()
                )
            else:
                passed = flags.get(condition.evidence_key) is True
            results.append(
                VerificationResult(
                    kind=condition.kind,
                    passed=passed,
                    description=condition.description,
                    source_step_id=condition.source_step_id,
                )
            )
        return tuple(results)

    def complete_step(
        self,
        step_id: str,
        outcome: ExecutionOutcome | str,
        *,
        evidence: Optional[Mapping[str, Any]] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        self._check_deadline()
        state = self.progress_for(step_id)
        if state.status is not StepStatus.RUNNING:
            raise PlanStateError("only a running step can be completed")
        try:
            normalized_outcome = outcome if isinstance(outcome, ExecutionOutcome) else ExecutionOutcome(str(outcome))
        except ValueError as error:
            raise PlanStateError("execution outcome is invalid") from error
        if normalized_outcome is ExecutionOutcome.EXECUTION_UNKNOWN:
            state.status = StepStatus.EXECUTION_UNKNOWN
            state.verification_status = VerificationStatus.UNKNOWN
            state.error_code = "EXECUTION_UNKNOWN"
            state.error_message = error_message or "外部操作的結果無法確認，禁止自動重試。"
            for other_id, other in self._states.items():
                if other_id != step_id and other.status not in _TERMINAL_STEPS:
                    other.status = StepStatus.SKIPPED
                    other.error_code = "TOOL_SKIPPED_AFTER_EXECUTION_UNKNOWN"
                    other.error_message = "先前外部操作的結果不確定，已停止後續步驟。"
            self._stop_status = PlanStatus.EXECUTION_UNKNOWN
            self._stop_reason = "EXECUTION_UNKNOWN"
            return
        if normalized_outcome is ExecutionOutcome.CANCELLED:
            state.status = StepStatus.CANCELLED
            state.error_code = error_code or "TASK_PLAN_CANCELLED"
            state.error_message = error_message or "步驟已取消。"
            self._stop_status = PlanStatus.CANCELLED
            self._stop_reason = state.error_code
            for other_id, other in self._states.items():
                if other_id != step_id and other.status not in _TERMINAL_STEPS:
                    other.status = StepStatus.SKIPPED
                    other.error_code = "TASK_PLAN_STOPPED"
                    other.error_message = "計畫已取消。"
            return
        if normalized_outcome is ExecutionOutcome.FAILED:
            state.status = StepStatus.FAILED
            state.verification_status = VerificationStatus.FAILED
            state.error_code = str(error_code or "TASK_STEP_FAILED")
            state.error_message = str(error_message or "步驟執行失敗。")
            self._refresh()
            return

        results = self._verification_results(self.plan.step(step_id), dict(evidence or {}))
        state.verification_results = results
        if all(result.passed is True for result in results):
            state.status = StepStatus.SUCCEEDED
            state.verification_status = VerificationStatus.PASSED
        else:
            state.status = StepStatus.FAILED
            state.verification_status = VerificationStatus.FAILED
            state.error_code = "TASK_STEP_VERIFICATION_FAILED"
            state.error_message = "步驟結果未通過驗證。"
        self._refresh()

    @property
    def status(self) -> PlanStatus:
        self._check_deadline()
        if self._stop_status is not None:
            return self._stop_status
        states = [state.status for state in self._states.values()]
        if all(state is StepStatus.SUCCEEDED for state in states):
            return PlanStatus.SUCCEEDED
        if any(state is StepStatus.RUNNING for state in states) or any(
            state is StepStatus.SUCCEEDED for state in states
        ):
            return PlanStatus.RUNNING
        if any(state is StepStatus.READY for state in states) and any(
            state in _TERMINAL_STEPS for state in states
        ):
            return PlanStatus.RUNNING
        if all(state in _TERMINAL_STEPS for state in states) and any(
            state in {StepStatus.FAILED, StepStatus.BLOCKED} for state in states
        ):
            return PlanStatus.FAILED
        return PlanStatus.PENDING

    def snapshot(self) -> dict[str, Any]:
        status = self.status
        elapsed = max(0.0, float(self._clock()) - self._started_at)
        return {
            "plan_id": self.plan.plan_id,
            "status": status.value,
            "stop_reason": self._stop_reason,
            "elapsed_seconds": round(elapsed, 3),
            "wall_time_limit_seconds": self.plan.limits.max_wall_seconds,
            "tool_calls_used": self._total_tool_calls,
            "tool_call_limit": self.plan.limits.max_tool_calls,
            "steps": [
                self._states[step.step_id].as_dict(step)
                for step in self.plan.topological_steps()
            ],
        }


def build_task_plan(
    request: str,
    tools: Iterable[Any] = (),
    *,
    limits: Optional[PlanLimits] = None,
) -> TaskPlan:
    """Convenience boundary for callers that do not need a planner instance."""

    return DeterministicTaskPlanner(limits).plan(request, tools)


def is_explicit_multistep_request(request: str) -> bool:
    """Return true only when the user visibly expressed multiple ordered tasks.

    This intentionally favours false negatives: ordinary prose containing an
    ``and`` or a single comma must remain on the legacy direct-chat path.
    """

    text = str(request or "").strip()
    if not text or _CONTROL_CHARACTERS.search(text):
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    explicit_list_items = sum(
        bool(re.match(r"^(?:[-*•]|\d{1,3}[.)、])\s*\S", line)) for line in lines
    )
    if explicit_list_items >= 2:
        return True
    semicolon_parts = [item for item in re.split(r"[;；]", text) if item.strip()]
    if len(semicolon_parts) >= 3:
        return True
    if len(_SEQUENCE_MARKER.findall(text)) >= 2:
        return True
    if _COMPACT_SEQUENCE_START.search(text) and len(
        _COMPACT_SEQUENCE_SPLIT.split(text)
    ) >= 2:
        return True
    if re.search(r"(?:規劃|計畫|plan)", text, re.IGNORECASE) and len(
        _ACTION_HINT.findall(text)
    ) >= 2:
        return True
    comma_parts = [item.strip() for item in re.split(r"[,，、]", text) if item.strip()]
    return len(comma_parts) >= 3 and sum(
        bool(_ACTION_HINT.search(item)) for item in comma_parts
    ) >= 2


__all__ = [
    "DeterministicTaskPlanner",
    "ExecutionOutcome",
    "PlanBudgetExceeded",
    "PlanDeadlineExceeded",
    "PlanLimits",
    "PlanProgress",
    "PlanStateError",
    "PlanStatus",
    "PlanValidationError",
    "PlannerError",
    "PlannerTool",
    "StepKind",
    "StepProgress",
    "StepStatus",
    "TaskPlan",
    "TaskStep",
    "VerificationCondition",
    "VerificationKind",
    "VerificationResult",
    "VerificationStatus",
    "build_task_plan",
    "is_explicit_multistep_request",
]
