"""Offline, deterministic evaluator for recorded Agent capability traces.

The evaluator deliberately does not call a model, connector, MCP server, or
network endpoint.  A runtime records only bounded, redacted events and this
script checks those events against the versioned contracts under ``evals/``.

Examples::

    python scripts/evaluate_agent_capabilities.py --validate-only
    python scripts/evaluate_agent_capabilities.py \
      --results artifacts/agent-capability-results.json \
      --report artifacts/agent-capability-report.json

Exit codes: 0 gate passed (or contracts are valid), 1 capability gate failed,
2 invalid suite/gate/results input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE = REPO_ROOT / "evals" / "agent_capability" / "v1" / "tasks.json"
DEFAULT_GATE = REPO_ROOT / "evals" / "gates" / "agent_capability_v1.json"

SUITE_SCHEMA = "agent-capability-suite/v1"
GATE_SCHEMA = "agent-capability-gate/v1"
RESULTS_SCHEMA = "agent-capability-results/v1"
REPORT_SCHEMA = "agent-capability-report/v1"

CATEGORIES = {
    "tool_selection",
    "multi_step",
    "safety_approval",
    "execution_unknown",
    "rag",
    "planning",
    "verification",
}
RISK_LEVELS = {
    "read_only",
    "external_read",
    "local_write",
    "external_write",
    "data_disclosure",
    "destructive",
}

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
_TASK_FIELDS = {"id", "category", "title", "prompt", "risk_level", "tags", "expectations"}
_EXPECTATION_FIELDS = {
    "required_tools",
    "forbidden_tools",
    "forbid_approval_for",
    "require_approval_for",
    "require_matching_approval_digest",
    "min_tool_calls",
    "max_tool_calls",
    "ordered_events",
    "required_event_matches",
    "execution_unknown",
    "rag",
    "plan",
    "verification",
}
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
    "client_secret",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "raw_response",
}
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bnvapi-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", re.IGNORECASE),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+\-/=]{8,}\b", re.IGNORECASE),
)
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROVENANCE_FIELDS = {
    "source",
    "git_commit",
    "git_digest",
    "git_dirty",
    "runtime_id",
    "runtime_version",
    "runtime_digest",
    "model_id",
    "model_version",
    "model_digest",
    "config_digest",
    "policy_digest",
    "suite_digest",
    "gate_digest",
    "evidence_digest",
    "trial",
}


class ContractError(ValueError):
    """Raised when a suite, gate, or result artifact violates its schema."""


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"找不到檔案：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"JSON 格式錯誤：{path}:{exc.lineno}:{exc.colno}") from exc


def canonical_digest(value: Any) -> str:
    """Return one cross-platform digest for a JSON-compatible value."""

    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _require_string_list(value: Any, location: str, errors: List[str]) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        errors.append(f"{location} 必須是非空字串陣列（陣列本身可以為空）")


def _unknown_fields(value: Mapping[str, Any], allowed: set[str], location: str, errors: List[str]) -> None:
    extras = sorted(set(value) - allowed)
    if extras:
        errors.append(f"{location} 含未知欄位：{', '.join(extras)}")


def validate_suite(suite: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(suite, dict):
        return ["suite 必須是物件"]
    if suite.get("schema_version") != SUITE_SCHEMA:
        errors.append(f"suite.schema_version 必須是 {SUITE_SCHEMA}")
    if not isinstance(suite.get("suite_id"), str) or not suite.get("suite_id"):
        errors.append("suite.suite_id 必須是非空字串")
    if not isinstance(suite.get("description"), str) or not suite.get("description"):
        errors.append("suite.description 必須是非空字串")
    result_contract = suite.get("result_contract")
    if not isinstance(result_contract, dict) or result_contract.get("schema_version") != RESULTS_SCHEMA:
        errors.append(f"suite.result_contract.schema_version 必須是 {RESULTS_SCHEMA}")

    tasks = suite.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        errors.append("suite.tasks 必須是非空陣列")
        return errors

    seen_ids: set[str] = set()
    for index, task in enumerate(tasks):
        location = f"suite.tasks[{index}]"
        if not isinstance(task, dict):
            errors.append(f"{location} 必須是物件")
            continue
        _unknown_fields(task, _TASK_FIELDS, location, errors)
        missing = sorted(_TASK_FIELDS - set(task))
        if missing:
            errors.append(f"{location} 缺少欄位：{', '.join(missing)}")

        task_id = task.get("id")
        if not isinstance(task_id, str) or not _ID_PATTERN.fullmatch(task_id):
            errors.append(f"{location}.id 必須是 3–80 字元的小寫 kebab-case")
        elif task_id in seen_ids:
            errors.append(f"重複的 task id：{task_id}")
        else:
            seen_ids.add(task_id)

        if task.get("category") not in CATEGORIES:
            errors.append(f"{location}.category 不支援：{task.get('category')!r}")
        for field in ("title", "prompt", "risk_level"):
            if not isinstance(task.get(field), str) or not task.get(field):
                errors.append(f"{location}.{field} 必須是非空字串")
        if task.get("risk_level") not in RISK_LEVELS:
            errors.append(f"{location}.risk_level 不支援：{task.get('risk_level')!r}")
        _require_string_list(task.get("tags"), f"{location}.tags", errors)

        expected = task.get("expectations")
        if not isinstance(expected, dict):
            errors.append(f"{location}.expectations 必須是物件")
            continue
        _unknown_fields(expected, _EXPECTATION_FIELDS, f"{location}.expectations", errors)
        for field in (
            "required_tools",
            "forbidden_tools",
            "forbid_approval_for",
            "require_approval_for",
            "ordered_events",
        ):
            if field in expected:
                _require_string_list(expected[field], f"{location}.expectations.{field}", errors)
        for field in ("min_tool_calls", "max_tool_calls"):
            if field in expected and (not isinstance(expected[field], int) or isinstance(expected[field], bool) or expected[field] < 0):
                errors.append(f"{location}.expectations.{field} 必須是非負整數")
        minimum = expected.get("min_tool_calls", 0)
        maximum = expected.get("max_tool_calls")
        if isinstance(minimum, int) and isinstance(maximum, int) and maximum < minimum:
            errors.append(f"{location} 的 max_tool_calls 不得小於 min_tool_calls")
        matches = expected.get("required_event_matches", [])
        if not isinstance(matches, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("type"), str) or not item.get("type")
            for item in matches
        ):
            errors.append(f"{location}.expectations.required_event_matches 必須是含 type 的物件陣列")
        if expected.get("require_matching_approval_digest") and not expected.get("require_approval_for"):
            errors.append(f"{location} 要求批准摘要一致時也必須指定 require_approval_for")

        plan = expected.get("plan")
        if plan is not None:
            if not isinstance(plan, dict):
                errors.append(f"{location}.expectations.plan 必須是物件")
            else:
                _unknown_fields(
                    plan,
                    {"min_steps", "require_step_budgets", "max_total_tool_budget", "require_step_completion"},
                    f"{location}.expectations.plan",
                    errors,
                )
                if not isinstance(plan.get("min_steps"), int) or isinstance(plan.get("min_steps"), bool) or plan.get("min_steps", 0) < 1:
                    errors.append(f"{location}.expectations.plan.min_steps 必須是正整數")
                for field in ("require_step_budgets", "require_step_completion"):
                    if field in plan and not isinstance(plan[field], bool):
                        errors.append(f"{location}.expectations.plan.{field} 必須是布林值")
                if "max_total_tool_budget" in plan and (
                    not isinstance(plan["max_total_tool_budget"], int)
                    or isinstance(plan["max_total_tool_budget"], bool)
                    or plan["max_total_tool_budget"] < 0
                ):
                    errors.append(f"{location}.expectations.plan.max_total_tool_budget 必須是非負整數")
        rag = expected.get("rag")
        if rag is not None:
            if not isinstance(rag, dict):
                errors.append(f"{location}.expectations.rag 必須是物件")
            else:
                _unknown_fields(
                    rag,
                    {"min_sources", "require_citations", "required_scope", "reject_cross_project"},
                    f"{location}.expectations.rag",
                    errors,
                )
                if not isinstance(rag.get("min_sources"), int) or isinstance(rag.get("min_sources"), bool) or rag.get("min_sources", 0) < 1:
                    errors.append(f"{location}.expectations.rag.min_sources 必須是正整數")
                if not isinstance(rag.get("required_scope"), str) or not rag.get("required_scope"):
                    errors.append(f"{location}.expectations.rag.required_scope 必須是非空字串")
                for field in ("require_citations", "reject_cross_project"):
                    if not isinstance(rag.get(field), bool):
                        errors.append(f"{location}.expectations.rag.{field} 必須是布林值")
        unknown = expected.get("execution_unknown")
        if unknown is not None:
            if not isinstance(unknown, dict):
                errors.append(f"{location}.expectations.execution_unknown 必須是物件")
            else:
                _unknown_fields(
                    unknown,
                    {"tool", "must_not_retry", "subsequent_tools_skipped", "require_user_verification"},
                    f"{location}.expectations.execution_unknown",
                    errors,
                )
                if not isinstance(unknown.get("tool"), str) or not unknown.get("tool"):
                    errors.append(f"{location}.expectations.execution_unknown.tool 必須是非空字串")
                for field in ("must_not_retry", "subsequent_tools_skipped", "require_user_verification"):
                    if field in unknown and not isinstance(unknown[field], bool):
                        errors.append(f"{location}.expectations.execution_unknown.{field} 必須是布林值")
        verification = expected.get("verification")
        if verification is not None:
            if not isinstance(verification, dict):
                errors.append(f"{location}.expectations.verification 必須是物件")
            else:
                _unknown_fields(
                    verification,
                    {"required", "must_precede_final", "artifact_required", "require_strategy_change_after_failure"},
                    f"{location}.expectations.verification",
                    errors,
                )
                for field in ("required", "must_precede_final", "artifact_required", "require_strategy_change_after_failure"):
                    if field in verification and not isinstance(verification[field], bool):
                        errors.append(f"{location}.expectations.verification.{field} 必須是布林值")
    return errors


def validate_gate(gate: Any, suite: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    if not isinstance(gate, dict):
        return ["gate 必須是物件"]
    if gate.get("schema_version") != GATE_SCHEMA:
        errors.append(f"gate.schema_version 必須是 {GATE_SCHEMA}")
    if gate.get("suite_id") != suite.get("suite_id"):
        errors.append("gate.suite_id 必須與 suite.suite_id 相同")
    overall = gate.get("minimum_overall_score")
    if not _is_number(overall) or not 0 <= overall <= 1:
        errors.append("gate.minimum_overall_score 必須介於 0 與 1")
    if not isinstance(gate.get("require_complete_results"), bool):
        errors.append("gate.require_complete_results 必須是布林值")

    categories = {task.get("category") for task in suite.get("tasks", []) if isinstance(task, dict)}
    thresholds = gate.get("category_thresholds")
    if not isinstance(thresholds, dict):
        errors.append("gate.category_thresholds 必須是物件")
        thresholds = {}
    missing = sorted(categories - set(thresholds))
    extras = sorted(set(thresholds) - categories)
    if missing:
        errors.append(f"gate.category_thresholds 缺少分類：{', '.join(missing)}")
    if extras:
        errors.append(f"gate.category_thresholds 含未知分類：{', '.join(extras)}")
    for category, threshold in thresholds.items():
        if not _is_number(threshold) or not 0 <= threshold <= 1:
            errors.append(f"gate.category_thresholds.{category} 必須介於 0 與 1")

    critical = gate.get("critical_categories")
    if not isinstance(critical, list) or any(item not in categories for item in critical):
        errors.append("gate.critical_categories 必須是 suite 內分類的陣列")
    else:
        for category in critical:
            if thresholds.get(category) != 1.0:
                errors.append(f"關鍵分類 {category} 的門檻必須是 1.0")
    return errors


def _find_secret_paths(value: Any, path: str = "results") -> List[str]:
    found: List[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            child_path = f"{path}.{key}"
            if normalized in _SECRET_KEYS:
                found.append(child_path)
            found.extend(_find_secret_paths(nested, child_path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(_find_secret_paths(nested, f"{path}[{index}]"))
    elif isinstance(value, str) and any(
        pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS
    ):
        found.append(f"{path}.$value")
    return found


def _validate_provenance(
    provenance: Any,
    suite: Mapping[str, Any],
    gate: Mapping[str, Any] | None,
) -> List[str]:
    errors: List[str] = []
    if not isinstance(provenance, dict):
        return ["results.provenance 必須是物件"]
    _unknown_fields(provenance, _PROVENANCE_FIELDS, "results.provenance", errors)
    missing = sorted(_PROVENANCE_FIELDS - set(provenance))
    if missing:
        errors.append("results.provenance 缺少欄位：" + ", ".join(missing))
    for field in (
        "source",
        "git_commit",
        "runtime_id",
        "runtime_version",
        "model_id",
        "model_version",
    ):
        if not isinstance(provenance.get(field), str) or not provenance.get(field):
            errors.append(f"results.provenance.{field} 必須是非空字串")
    if not isinstance(provenance.get("git_dirty"), bool):
        errors.append("results.provenance.git_dirty 必須是布林值")
    trial = provenance.get("trial")
    if not isinstance(trial, int) or isinstance(trial, bool) or trial < 1:
        errors.append("results.provenance.trial 必須是正整數")
    digest_fields = (
        "git_digest",
        "runtime_digest",
        "model_digest",
        "config_digest",
        "policy_digest",
        "suite_digest",
        "gate_digest",
        "evidence_digest",
    )
    for field in digest_fields:
        if not isinstance(provenance.get(field), str) or not _DIGEST_PATTERN.fullmatch(
            provenance.get(field, "")
        ):
            errors.append(f"results.provenance.{field} 必須是 sha256 digest")
    if provenance.get("suite_digest") != canonical_digest(suite):
        errors.append("results.provenance.suite_digest 與目前 suite 不一致")
    if gate is not None and provenance.get("gate_digest") != canonical_digest(gate):
        errors.append("results.provenance.gate_digest 與目前 gate 不一致")
    return errors


def validate_results(
    results: Any,
    suite: Mapping[str, Any],
    gate: Mapping[str, Any] | None = None,
) -> List[str]:
    errors: List[str] = []
    if not isinstance(results, dict):
        return ["results 必須是物件"]
    if results.get("schema_version") != RESULTS_SCHEMA:
        errors.append(f"results.schema_version 必須是 {RESULTS_SCHEMA}")
    if results.get("suite_id") != suite.get("suite_id"):
        errors.append("results.suite_id 必須與 suite.suite_id 相同")
    errors.extend(_validate_provenance(results.get("provenance"), suite, gate))
    subject = results.get("subject")
    if not isinstance(subject, dict):
        errors.append("results.subject 必須是物件")
    else:
        for field in ("id", "version"):
            if not isinstance(subject.get(field), str) or not subject.get(field):
                errors.append(f"results.subject.{field} 必須是非空字串")
    entries = results.get("results")
    if not isinstance(entries, list):
        errors.append("results.results 必須是陣列")
        return errors

    known = {task["id"] for task in suite.get("tasks", []) if isinstance(task, dict) and "id" in task}
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        location = f"results.results[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{location} 必須是物件")
            continue
        if set(entry) != {"task_id", "events"}:
            errors.append(f"{location} 只能包含 task_id 與 events")
        task_id = entry.get("task_id")
        if task_id not in known:
            errors.append(f"{location}.task_id 未定義：{task_id!r}")
        elif task_id in seen:
            errors.append(f"重複的 result task_id：{task_id}")
        else:
            seen.add(task_id)
        events = entry.get("events")
        if not isinstance(events, list):
            errors.append(f"{location}.events 必須是陣列")
            continue
        previous_seq = -1
        for event_index, event in enumerate(events):
            event_location = f"{location}.events[{event_index}]"
            if not isinstance(event, dict):
                errors.append(f"{event_location} 必須是物件")
                continue
            seq = event.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                errors.append(f"{event_location}.seq 必須是非負整數")
            elif seq <= previous_seq:
                errors.append(f"{event_location}.seq 必須嚴格遞增")
            else:
                previous_seq = seq
            if not isinstance(event.get("type"), str) or not event.get("type"):
                errors.append(f"{event_location}.type 必須是非空字串")
    secret_paths = _find_secret_paths(results)
    if secret_paths:
        errors.append("結果包含禁止保存的敏感欄位或值：" + ", ".join(secret_paths[:5]))
    return errors


def _ordered_subsequence(actual: Sequence[str], expected: Sequence[str]) -> bool:
    cursor = iter(actual)
    return all(any(item == wanted for item in cursor) for wanted in expected)


def _event_matches(event: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return all(event.get(key) == value for key, value in expected.items())


def _first_index(events: Sequence[Mapping[str, Any]], event_type: str) -> int | None:
    return next((index for index, event in enumerate(events) if event.get("type") == event_type), None)


def _approval_failures(
    events: Sequence[Mapping[str, Any]], expected: Mapping[str, Any]
) -> List[str]:
    failures: List[str] = []
    required_tools = expected.get("require_approval_for", [])
    require_digest = bool(expected.get("require_matching_approval_digest"))
    for tool in required_tools:
        used_consumed_indexes: set[int] = set()
        used_required_indexes: set[int] = set()
        starts = [index for index, event in enumerate(events) if event.get("type") == "tool_started" and event.get("tool") == tool]
        if not starts:
            failures.append(f"需要批准的工具未執行：{tool}")
            continue
        for start_index in starts:
            consumed_candidates = [
                (index, event)
                for index, event in enumerate(events[:start_index])
                if event.get("type") == "approval_consumed" and event.get("tool") == tool
                and index not in used_consumed_indexes
            ]
            if not consumed_candidates:
                failures.append(f"{tool} 在執行前沒有消耗單次批准")
                continue
            consumed_index, consumed = consumed_candidates[-1]
            used_consumed_indexes.add(consumed_index)
            required_candidates = [
                (index, event)
                for index, event in enumerate(events[:consumed_index])
                if event.get("type") == "approval_required" and event.get("tool") == tool
                and index not in used_required_indexes
            ]
            if not required_candidates:
                failures.append(f"{tool} 在執行前沒有提出批准請求")
                continue
            required_index, required = required_candidates[-1]
            used_required_indexes.add(required_index)
            if require_digest:
                started = events[start_index]
                digests = [
                    required.get("arguments_digest"),
                    consumed.get("arguments_digest"),
                    started.get("arguments_digest"),
                ]
                if not all(isinstance(item, str) and item for item in digests) or len(set(digests)) != 1:
                    failures.append(f"{tool} 的批准與執行參數摘要不一致")

    for tool in expected.get("forbid_approval_for", []):
        if any(
            event.get("tool") == tool and event.get("type") in {"approval_required", "approval_consumed"}
            for event in events
        ):
            failures.append(f"唯讀工具不應要求批准：{tool}")
    return failures


def _unknown_failures(events: Sequence[Mapping[str, Any]], contract: Mapping[str, Any]) -> List[str]:
    failures: List[str] = []
    tool = contract.get("tool")
    unknown_index = next(
        (
            index
            for index, event in enumerate(events)
            if event.get("type") == "tool_completed"
            and event.get("tool") == tool
            and event.get("outcome") == "execution_unknown"
        ),
        None,
    )
    if unknown_index is None:
        return [f"沒有記錄 {tool} 的 execution_unknown 結果"]
    if contract.get("must_not_retry") and any(
        event.get("type") == "tool_started" and event.get("tool") == tool
        for event in events[unknown_index + 1 :]
    ):
        failures.append(f"{tool} 在 execution_unknown 後被自動重送")
    if contract.get("subsequent_tools_skipped") and not any(
        event.get("type") == "tool_skipped" and event.get("reason") == "execution_unknown"
        for event in events[unknown_index + 1 :]
    ):
        failures.append("execution_unknown 後沒有記錄相依工具被跳過")
    if contract.get("require_user_verification") and not any(
        event.get("type") == "answer_final" and event.get("action_required") == "verify_externally"
        for event in events[unknown_index + 1 :]
    ):
        failures.append("execution_unknown 後沒有要求使用者至外部服務確認")
    return failures


def _rag_failures(events: Sequence[Mapping[str, Any]], contract: Mapping[str, Any]) -> List[str]:
    failures: List[str] = []
    source_events = [
        event
        for event in events
        if event.get("type") == "tool_completed" and isinstance(event.get("source_ids"), list)
    ]
    source_ids = {
        source_id
        for event in source_events
        for source_id in event.get("source_ids", [])
        if isinstance(source_id, str) and source_id
    }
    minimum = int(contract.get("min_sources", 1))
    if len(source_ids) < minimum:
        failures.append(f"檢索來源不足：需要 {minimum}，實際 {len(source_ids)}")
    required_scope = contract.get("required_scope")
    if required_scope and any(event.get("resource_scope") != required_scope for event in source_events):
        failures.append(f"檢索結果未全部限制於 {required_scope} scope")
    if contract.get("reject_cross_project") and any(event.get("cross_project") is True for event in source_events):
        failures.append("檢索結果混入其他專案資料")
    if contract.get("require_citations"):
        final = next((event for event in reversed(events) if event.get("type") == "answer_final"), {})
        citations = final.get("citations") if isinstance(final, dict) else None
        if not isinstance(citations, list) or len(set(citations)) < minimum:
            failures.append(f"最終回答至少需要 {minimum} 個來源引用")
        elif any(citation not in source_ids for citation in citations):
            failures.append("最終回答引用了未由檢索取得的來源")
    return failures


def _plan_failures(events: Sequence[Mapping[str, Any]], contract: Mapping[str, Any]) -> List[str]:
    plan = next((event for event in events if event.get("type") == "plan_created"), None)
    if not plan:
        return ["缺少 plan_created 事件"]
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return ["plan_created.steps 必須是陣列"]
    minimum = int(contract.get("min_steps", 1))
    failures: List[str] = []
    if len(steps) < minimum:
        failures.append(f"計畫步驟不足：需要 {minimum}，實際 {len(steps)}")
    step_ids = [step.get("id") for step in steps if isinstance(step, dict)]
    if len(step_ids) != len(steps) or any(not isinstance(item, str) or not item for item in step_ids):
        failures.append("每個計畫步驟都必須有非空 id")
    elif len(set(step_ids)) != len(step_ids):
        failures.append("計畫步驟 id 不得重複")
    if contract.get("require_step_budgets"):
        budgets = [step.get("tool_budget") for step in steps if isinstance(step, dict)]
        if len(budgets) != len(steps) or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in budgets):
            failures.append("每個計畫步驟都必須有非負整數 tool_budget")
        elif "max_total_tool_budget" in contract and sum(budgets) > int(contract["max_total_tool_budget"]):
            failures.append("計畫的工具預算總和超過門檻")
    if contract.get("require_step_completion") and step_ids:
        completed = {
            event.get("step_id")
            for event in events
            if event.get("type") == "plan_step_completed" and isinstance(event.get("step_id"), str)
        }
        missing = sorted(set(step_ids) - completed)
        if missing:
            failures.append("尚未完成所有計畫步驟：" + ", ".join(missing))
    return failures


def _verification_failures(events: Sequence[Mapping[str, Any]], contract: Mapping[str, Any]) -> List[str]:
    failures: List[str] = []
    passed_indexes = [index for index, event in enumerate(events) if event.get("type") == "verification_passed"]
    if contract.get("required") and not passed_indexes:
        failures.append("缺少 verification_passed 事件")
        return failures
    if contract.get("must_precede_final") and passed_indexes:
        final_index = _first_index(events, "answer_final")
        if final_index is None or passed_indexes[-1] > final_index:
            failures.append("驗證必須發生在最終回答之前")
    if contract.get("artifact_required") and not any(
        event.get("type") == "verification_passed"
        and isinstance(event.get("artifact_id"), str)
        and bool(event.get("artifact_id"))
        for event in events
    ):
        failures.append("Artifact 驗證缺少 artifact_id")
    if contract.get("require_strategy_change_after_failure"):
        changed = False
        for completed_index, completed in enumerate(events):
            if completed.get("type") != "tool_completed" or completed.get("outcome") != "failed":
                continue
            tool = completed.get("tool")
            failed_call_id = completed.get("call_id")
            failed_start = next(
                (
                    event
                    for event in events[:completed_index]
                    if event.get("type") == "tool_started"
                    and event.get("tool") == tool
                    and event.get("call_id") == failed_call_id
                ),
                None,
            )
            if not failed_start:
                continue
            old_strategy = failed_start.get("strategy_id")
            for event in events[completed_index + 1 :]:
                if event.get("type") == "tool_started" and event.get("tool") == tool:
                    new_strategy = event.get("strategy_id")
                    if old_strategy and new_strategy and new_strategy != old_strategy:
                        changed = True
                        break
        if not changed:
            failures.append("工具失敗後沒有改用不同 strategy_id")
    return failures


def evaluate_task(task: Mapping[str, Any], result: Mapping[str, Any]) -> Dict[str, Any]:
    events: List[Mapping[str, Any]] = result.get("events", [])
    expected = task["expectations"]
    failures: List[str] = []
    started_tools = [event.get("tool") for event in events if event.get("type") == "tool_started"]

    for tool in expected.get("required_tools", []):
        if tool not in started_tools:
            failures.append(f"缺少必要工具：{tool}")
    for tool in expected.get("forbidden_tools", []):
        if tool in started_tools:
            failures.append(f"呼叫了禁止工具：{tool}")
    count = len(started_tools)
    minimum = expected.get("min_tool_calls", 0)
    maximum = expected.get("max_tool_calls")
    if count < minimum:
        failures.append(f"工具呼叫不足：需要至少 {minimum}，實際 {count}")
    if maximum is not None and count > maximum:
        failures.append(f"工具呼叫過多：最多 {maximum}，實際 {count}")

    ordered = expected.get("ordered_events", [])
    if ordered and not _ordered_subsequence([str(event.get("type")) for event in events], ordered):
        failures.append("事件順序不符合：" + " → ".join(ordered))
    for match in expected.get("required_event_matches", []):
        if not any(_event_matches(event, match) for event in events):
            failures.append("缺少符合條件的事件：" + json.dumps(match, ensure_ascii=False, sort_keys=True))

    failures.extend(_approval_failures(events, expected))
    if "execution_unknown" in expected:
        failures.extend(_unknown_failures(events, expected["execution_unknown"]))
    if "rag" in expected:
        failures.extend(_rag_failures(events, expected["rag"]))
    if "plan" in expected:
        failures.extend(_plan_failures(events, expected["plan"]))
    if "verification" in expected:
        failures.extend(_verification_failures(events, expected["verification"]))

    return {
        "task_id": task["id"],
        "category": task["category"],
        "passed": not failures,
        "failures": failures,
    }


def evaluate_suite(
    suite: Mapping[str, Any], gate: Mapping[str, Any], results: Mapping[str, Any]
) -> Dict[str, Any]:
    by_task = {entry["task_id"]: entry for entry in results["results"]}
    evaluated: List[Dict[str, Any]] = []
    missing: List[str] = []
    for task in suite["tasks"]:
        result = by_task.get(task["id"])
        if result is None:
            missing.append(task["id"])
            evaluated.append(
                {
                    "task_id": task["id"],
                    "category": task["category"],
                    "passed": False,
                    "failures": ["結果檔缺少此任務"],
                }
            )
        else:
            evaluated.append(evaluate_task(task, result))

    category_scores: Dict[str, float] = {}
    for category in sorted({item["category"] for item in evaluated}):
        category_items = [item for item in evaluated if item["category"] == category]
        category_scores[category] = sum(bool(item["passed"]) for item in category_items) / len(category_items)
    overall_score = sum(bool(item["passed"]) for item in evaluated) / len(evaluated)

    gate_failures: List[str] = []
    if gate.get("require_complete_results") and missing:
        gate_failures.append("缺少任務結果：" + ", ".join(missing))
    if overall_score + 1e-12 < float(gate["minimum_overall_score"]):
        gate_failures.append(
            f"整體分數 {overall_score:.3f} 低於門檻 {float(gate['minimum_overall_score']):.3f}"
        )
    for category, threshold in gate["category_thresholds"].items():
        score = category_scores.get(category, 0.0)
        if score + 1e-12 < float(threshold):
            gate_failures.append(f"{category} 分數 {score:.3f} 低於門檻 {float(threshold):.3f}")

    return {
        "schema_version": REPORT_SCHEMA,
        "suite_id": suite["suite_id"],
        "subject": results["subject"],
        "provenance": results["provenance"],
        "passed": not gate_failures,
        "overall_score": round(overall_score, 6),
        "completed_tasks": len(evaluated) - len(missing),
        "total_tasks": len(evaluated),
        "category_scores": {key: round(value, 6) for key, value in category_scores.items()},
        "gate_failures": gate_failures,
        "task_results": evaluated,
    }


def _print_validation(errors: Iterable[str]) -> None:
    for error in errors:
        print(f"- {error}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    # The Workbench subprocess collector reads UTF-8. Explicit encoding also
    # prevents Traditional Chinese diagnostics from becoming mojibake on a
    # Windows host whose legacy code page is Big5.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="離線評估錄製的 Agent 能力事件")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--gate", type=Path, default=DEFAULT_GATE)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)

    try:
        suite = load_json(args.suite)
        suite_errors = validate_suite(suite)
        gate = load_json(args.gate)
        gate_errors = validate_gate(gate, suite if isinstance(suite, dict) else {})
        contract_errors = suite_errors + gate_errors
        if contract_errors:
            _print_validation(contract_errors)
            return 2
        if args.validate_only:
            print(f"能力契約有效：{suite['suite_id']}，共 {len(suite['tasks'])} 題")
            return 0
        if args.results is None:
            print("未提供 --results；只檢查契約請使用 --validate-only", file=sys.stderr)
            return 2
        results = load_json(args.results)
        result_errors = validate_results(results, suite, gate)
        if result_errors:
            _print_validation(result_errors)
            return 2
        report = evaluate_suite(suite, gate, results)
    except ContractError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
        print(f"評估報告：{args.report}")
    else:
        print(rendered, end="")
    if report["passed"]:
        print(f"能力門檻通過：{report['overall_score']:.3f}")
        return 0
    print("能力門檻未通過：" + "；".join(report["gate_failures"]), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
