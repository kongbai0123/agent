"""Shared Workbench input budget for the pinned 64K Hermes model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


HERMES_CONTEXT_WINDOW_TOKENS = 64_000
HERMES_OUTPUT_RESERVE_TOKENS = 4_096
HERMES_INTERNAL_RESERVE_TOKENS = 4_096
HERMES_WORKBENCH_INPUT_BUDGET_TOKENS = (
    HERMES_CONTEXT_WINDOW_TOKENS
    - HERMES_OUTPUT_RESERVE_TOKENS
    - HERMES_INTERNAL_RESERVE_TOKENS
)
HERMES_RUN_ENVELOPE_TOKENS = 64
HERMES_MESSAGE_ENVELOPE_TOKENS = 8
HERMES_MAX_INSTRUCTIONS_CHARS = 65_536
MAX_TEMPORARY_CONTEXT_CHARS = 24_000
TEMPORARY_CONTEXT_PREFIX = "\n\nTemporary user-supplied context:\n"
TEMPORARY_CONTEXT_TRUNCATED_MARKER = (
    "\n[Temporary context truncated to fit the Hermes context budget.]"
)


class HermesContextBudgetError(ValueError):
    """Raised before submission when mandatory scoped input cannot fit."""


def estimate_text_tokens(value: object) -> int:
    """Estimate mixed-language tokens without assuming chars/4 for CJK."""

    text = str(value or "")
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    non_ascii_bytes = sum(
        len(char.encode("utf-8")) for char in text if ord(char) >= 128
    )
    return ((ascii_chars + 3) // 4) + ((non_ascii_bytes + 2) // 3)


def estimate_run_input_tokens(
    input_text: object,
    instructions: object,
    history: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    total = (
        HERMES_RUN_ENVELOPE_TOKENS
        + estimate_text_tokens(input_text)
        + estimate_text_tokens(instructions)
    )
    for message in history or ():
        total += HERMES_MESSAGE_ENVELOPE_TOKENS
        total += estimate_text_tokens(message.get("role"))
        total += estimate_text_tokens(message.get("content"))
    return total


def assert_run_context_budget(
    input_text: object,
    instructions: object,
    history: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    if len(str(instructions or "")) > HERMES_MAX_INSTRUCTIONS_CHARS:
        raise HermesContextBudgetError(
            "Hermes mandatory instructions exceed the shared instruction budget."
        )
    estimated = estimate_run_input_tokens(input_text, instructions, history)
    if estimated > HERMES_WORKBENCH_INPUT_BUDGET_TOKENS:
        raise HermesContextBudgetError(
            "Hermes mandatory input exceeds the shared 64K context budget."
        )
    return estimated


def merge_instructions(base: object, project_skills: object) -> str:
    normalized_base = str(base or "").strip()
    scoped = str(project_skills or "")
    if not scoped:
        return normalized_base
    if not normalized_base:
        return scoped
    return f"{normalized_base}\n\n{scoped}"


@dataclass(frozen=True)
class HermesBudgetedContext:
    base_instructions: str
    history: tuple[dict[str, str], ...]
    estimated_input_tokens: int
    history_messages_dropped: int
    temporary_context_chars: int
    temporary_context_truncated: bool


def _with_temporary_context(base: str, context: str, *, truncated: bool) -> str:
    if not context and not truncated:
        return base
    suffix = context
    if truncated:
        suffix = suffix.rstrip() + TEMPORARY_CONTEXT_TRUNCATED_MARKER
    return base + TEMPORARY_CONTEXT_PREFIX + suffix


def budget_hermes_context(
    *,
    user_input: str,
    fixed_instructions: str,
    project_skill_instructions: str,
    temporary_context: str,
    history: Sequence[Mapping[str, Any]],
) -> HermesBudgetedContext:
    """Trim optional buckets while preserving current user and scoped skills."""

    fixed = str(fixed_instructions or "").strip()
    scoped = str(project_skill_instructions or "")
    raw_temporary = str(temporary_context or "").strip()
    temporary = raw_temporary[:MAX_TEMPORARY_CONTEXT_CHARS]
    temporary_truncated = len(temporary) < len(raw_temporary)
    selected_history = [
        {
            "role": str(message.get("role") or ""),
            "content": str(message.get("content") or ""),
        }
        for message in history
    ]

    mandatory_instructions = merge_instructions(fixed, scoped)
    assert_run_context_budget(user_input, mandatory_instructions, ())

    base = _with_temporary_context(
        fixed,
        temporary,
        truncated=temporary_truncated,
    )
    merged = merge_instructions(base, scoped)
    original_history_count = len(selected_history)
    while (
        selected_history
        and estimate_run_input_tokens(user_input, merged, selected_history)
        > HERMES_WORKBENCH_INPUT_BUDGET_TOKENS
    ):
        remove_count = (
            2
            if len(selected_history) >= 2
            and selected_history[0]["role"] == "user"
            and selected_history[1]["role"] == "assistant"
            else 1
        )
        del selected_history[:remove_count]

    if (
        estimate_run_input_tokens(user_input, merged, selected_history)
        > HERMES_WORKBENCH_INPUT_BUDGET_TOKENS
    ):
        temporary_truncated = bool(raw_temporary)
        low, high = 0, len(temporary)
        best = ""
        while low <= high:
            middle = (low + high) // 2
            candidate = temporary[:middle]
            candidate_base = _with_temporary_context(
                fixed,
                candidate,
                truncated=temporary_truncated,
            )
            candidate_merged = merge_instructions(candidate_base, scoped)
            if (
                len(candidate_merged) <= HERMES_MAX_INSTRUCTIONS_CHARS
                and
                estimate_run_input_tokens(
                    user_input,
                    candidate_merged,
                    selected_history,
                )
                <= HERMES_WORKBENCH_INPUT_BUDGET_TOKENS
            ):
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        temporary = best
        base = _with_temporary_context(
            fixed,
            temporary,
            truncated=temporary_truncated,
        )
        merged = merge_instructions(base, scoped)

    estimated = assert_run_context_budget(user_input, merged, selected_history)
    return HermesBudgetedContext(
        base_instructions=base,
        history=tuple(selected_history),
        estimated_input_tokens=estimated,
        history_messages_dropped=original_history_count - len(selected_history),
        temporary_context_chars=len(temporary),
        temporary_context_truncated=temporary_truncated,
    )


__all__ = [
    "HERMES_CONTEXT_WINDOW_TOKENS",
    "HERMES_INTERNAL_RESERVE_TOKENS",
    "HERMES_MAX_INSTRUCTIONS_CHARS",
    "HERMES_OUTPUT_RESERVE_TOKENS",
    "HERMES_WORKBENCH_INPUT_BUDGET_TOKENS",
    "HermesBudgetedContext",
    "HermesContextBudgetError",
    "assert_run_context_budget",
    "budget_hermes_context",
    "estimate_run_input_tokens",
    "estimate_text_tokens",
    "merge_instructions",
]
