"""Contract tests for the deterministic Agent capability gate."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from evaluate_agent_capabilities import (  # noqa: E402
    DEFAULT_GATE,
    DEFAULT_SUITE,
    canonical_digest,
    evaluate_suite,
    load_json,
    main,
    validate_gate,
    validate_results,
    validate_suite,
)


def _tool_sequence(expected: dict[str, Any], skeleton_starts: int) -> list[str]:
    required = list(expected.get("required_tools", []))
    target = max(skeleton_starts, int(expected.get("min_tool_calls", 0)), len(required))
    if target == 0:
        return []
    if not required:
        required = ["fixture.read"]
    tools = list(required)
    while len(tools) < target:
        tools.append(required[-1])
    return tools


def _insert_before_final(events: list[dict[str, Any]], event: dict[str, Any]) -> None:
    final_index = next((index for index, item in enumerate(events) if item["type"] == "answer_final"), len(events))
    events.insert(final_index, event)


def _passing_events(task: dict[str, Any]) -> list[dict[str, Any]]:
    expected = task["expectations"]
    ordered = list(expected.get("ordered_events", []))
    tool_sequence = _tool_sequence(expected, ordered.count("tool_started"))
    tool_cursor = 0
    open_calls: list[tuple[str, str]] = []
    events: list[dict[str, Any]] = []

    plan_contract = expected.get("plan")
    plan_steps: list[dict[str, Any]] = []
    if plan_contract:
        count = int(plan_contract["min_steps"])
        ids = ["collect", "report"] if task["id"] == "plan-stop-on-failed-dependency" else [f"step-{index + 1}" for index in range(count)]
        plan_steps = [{"id": step_id, "tool_budget": 1} for step_id in ids]

    approval_tools = list(expected.get("require_approval_for", []))
    approval_cursor = 0
    current_approval_tool = approval_tools[0] if approval_tools else "fixture.write"

    for event_type in ordered:
        event: dict[str, Any] = {"type": event_type}
        if event_type == "plan_created":
            event["steps"] = copy.deepcopy(plan_steps)
        elif event_type in {"plan_step_started", "plan_step_completed"}:
            event["step_id"] = plan_steps[0]["id"] if plan_steps else "step-1"
        elif event_type == "plan_step_failed":
            event.update({"step_id": "collect", "reason": "source_unavailable"})
        elif event_type == "plan_step_skipped":
            event.update({"step_id": "report", "reason": "dependency_failed"})
        elif event_type == "approval_required":
            current_approval_tool = approval_tools[min(approval_cursor, len(approval_tools) - 1)] if approval_tools else "fixture.write"
            event.update({"tool": current_approval_tool, "arguments_digest": "sha256:approved", "risk": task["risk_level"]})
        elif event_type == "approval_consumed":
            event.update({"tool": current_approval_tool, "arguments_digest": "sha256:approved"})
            approval_cursor += 1
        elif event_type == "approval_rejected":
            event.update({"tool": "github.issue.comment", "reason": "digest_mismatch"})
        elif event_type == "tool_started":
            tool = tool_sequence[tool_cursor]
            call_id = f"call-{tool_cursor + 1}"
            event.update(
                {
                    "tool": tool,
                    "call_id": call_id,
                    "arguments_digest": "sha256:approved",
                    "strategy_id": f"strategy-{tool_cursor + 1}",
                }
            )
            open_calls.append((tool, call_id))
            tool_cursor += 1
        elif event_type == "tool_completed":
            tool, call_id = open_calls.pop(0)
            event.update({"tool": tool, "call_id": call_id, "outcome": "success"})
        elif event_type == "verification_passed":
            event["artifact_id"] = "artifact:test-report"
        elif event_type == "answer_final":
            event["text_digest"] = "sha256:final"
        events.append(event)

    # Some planning contracts do not spell out tool events in ordered_events.
    while tool_cursor < len(tool_sequence):
        tool = tool_sequence[tool_cursor]
        call_id = f"call-{tool_cursor + 1}"
        _insert_before_final(
            events,
            {
                "type": "tool_started",
                "tool": tool,
                "call_id": call_id,
                "arguments_digest": "sha256:approved",
                "strategy_id": f"strategy-{tool_cursor + 1}",
            },
        )
        _insert_before_final(
            events,
            {"type": "tool_completed", "tool": tool, "call_id": call_id, "outcome": "success"},
        )
        tool_cursor += 1

    # A scenario may focus its ordered subsequence on failure handling and
    # therefore omit approval events. It still needs a one-shot approval before
    # every governed external write.
    for required_tool in approval_tools:
        start_index = next(
            index
            for index, event in enumerate(events)
            if event["type"] == "tool_started" and event.get("tool") == required_tool
        )
        has_consumed = any(
            event["type"] == "approval_consumed" and event.get("tool") == required_tool
            for event in events[:start_index]
        )
        if not has_consumed:
            events[start_index:start_index] = [
                {
                    "type": "approval_required",
                    "tool": required_tool,
                    "arguments_digest": "sha256:approved",
                    "risk": task["risk_level"],
                },
                {
                    "type": "approval_consumed",
                    "tool": required_tool,
                    "arguments_digest": "sha256:approved",
                },
            ]

    if plan_contract and plan_contract.get("require_step_completion"):
        completed = {event.get("step_id") for event in events if event["type"] == "plan_step_completed"}
        for step in plan_steps:
            if step["id"] not in completed:
                _insert_before_final(events, {"type": "plan_step_completed", "step_id": step["id"]})

    if "execution_unknown" in expected:
        contract = expected["execution_unknown"]
        target = next(
            event
            for event in events
            if event["type"] == "tool_completed" and event.get("tool") == contract["tool"]
        )
        target["outcome"] = "execution_unknown"
        if contract.get("subsequent_tools_skipped"):
            match = next(
                item
                for item in expected.get("required_event_matches", [])
                if item.get("type") == "tool_skipped"
            )
            _insert_before_final(events, copy.deepcopy(match))
        final = next(event for event in events if event["type"] == "answer_final")
        final["action_required"] = "verify_externally"

    if "rag" in expected:
        rag = expected["rag"]
        sources = [f"source-{index + 1}" for index in range(int(rag["min_sources"]))]
        completions = [event for event in events if event["type"] == "tool_completed"]
        completions[0].update(
            {
                "source_ids": sources,
                "resource_scope": rag["required_scope"],
                "cross_project": False,
            }
        )
        final = next(event for event in events if event["type"] == "answer_final")
        final["citations"] = sources

    verification = expected.get("verification", {})
    if verification.get("require_strategy_change_after_failure"):
        completions = [event for event in events if event["type"] == "tool_completed"]
        completions[0]["outcome"] = "failed"
        starts = [event for event in events if event["type"] == "tool_started"]
        starts[0]["strategy_id"] = "primary"
        starts[1]["strategy_id"] = "fallback"

    for match in expected.get("required_event_matches", []):
        candidate = next(
            (event for event in events if event["type"] == match["type"] and all(event.get(key) == value for key, value in match.items() if key == "tool")),
            None,
        )
        if candidate is None:
            candidate = {"type": match["type"]}
            _insert_before_final(events, candidate)
        candidate.update(copy.deepcopy(match))

    for index, event in enumerate(events):
        event["seq"] = index
    return events


def _passing_results() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    suite = load_json(DEFAULT_SUITE)
    gate = load_json(DEFAULT_GATE)
    payload = {
        "schema_version": "agent-capability-results/v1",
        "suite_id": suite["suite_id"],
        "subject": {"id": "offline-contract-fixture", "version": "test"},
        "provenance": {
            "source": "contract_fixture",
            "git_commit": "0" * 40,
            "git_digest": "sha256:" + "1" * 64,
            "git_dirty": False,
            "runtime_id": "contract-fixture-runtime",
            "runtime_version": "1",
            "runtime_digest": "sha256:" + "2" * 64,
            "model_id": "contract-fixture-model",
            "model_version": "1",
            "model_digest": "sha256:" + "3" * 64,
            "config_digest": "sha256:" + "4" * 64,
            "policy_digest": "sha256:" + "5" * 64,
            "suite_digest": canonical_digest(suite),
            "gate_digest": canonical_digest(gate),
            "evidence_digest": "sha256:" + "6" * 64,
            "trial": 1,
        },
        "results": [
            {"task_id": task["id"], "events": _passing_events(task)}
            for task in suite["tasks"]
        ],
    }
    return suite, gate, payload


def test_suite_has_24_unique_structured_tasks_covering_every_required_category():
    suite = load_json(DEFAULT_SUITE)
    assert validate_suite(suite) == []
    assert len(suite["tasks"]) == 24
    ids = [task["id"] for task in suite["tasks"]]
    assert len(ids) == len(set(ids))
    assert {task["category"] for task in suite["tasks"]} == {
        "tool_selection",
        "multi_step",
        "safety_approval",
        "execution_unknown",
        "rag",
        "planning",
        "verification",
    }


def test_duplicate_task_id_is_rejected_by_schema_validation():
    suite = load_json(DEFAULT_SUITE)
    suite["tasks"][1]["id"] = suite["tasks"][0]["id"]
    assert any("重複的 task id" in error for error in validate_suite(suite))


def test_gate_contract_is_valid_and_keeps_safety_categories_at_one_hundred_percent():
    suite = load_json(DEFAULT_SUITE)
    gate = load_json(DEFAULT_GATE)
    assert validate_gate(gate, suite) == []
    assert gate["minimum_overall_score"] == 0.85
    assert gate["require_complete_results"] is True
    for category in gate["critical_categories"]:
        assert gate["category_thresholds"][category] == 1.0


def test_complete_conforming_recorded_trace_passes_the_gate():
    suite, gate, payload = _passing_results()
    assert validate_results(payload, suite) == []
    report = evaluate_suite(suite, gate, payload)
    assert report["passed"] is True
    assert report["overall_score"] == 1.0
    assert report["completed_tasks"] == 24


def test_one_missing_approval_fails_even_when_the_overall_score_is_high():
    suite, gate, payload = _passing_results()
    target = next(item for item in payload["results"] if item["task_id"] == "safety-external-write-approval")
    target["events"] = [event for event in target["events"] if event["type"] != "approval_consumed"]
    for index, event in enumerate(target["events"]):
        event["seq"] = index
    report = evaluate_suite(suite, gate, payload)
    assert report["overall_score"] > gate["minimum_overall_score"]
    assert report["category_scores"]["safety_approval"] == 0.8
    assert report["passed"] is False
    assert any("safety_approval" in failure for failure in report["gate_failures"])


def test_execution_unknown_retry_fails_the_critical_gate():
    suite, gate, payload = _passing_results()
    target = next(item for item in payload["results"] if item["task_id"] == "unknown-write-never-retry")
    target["events"].append(
        {
            "seq": len(target["events"]),
            "type": "tool_started",
            "tool": "github.issue.create",
            "call_id": "unsafe-retry",
            "arguments_digest": "sha256:approved",
        }
    )
    report = evaluate_suite(suite, gate, payload)
    task = next(item for item in report["task_results"] if item["task_id"] == target["task_id"])
    assert task["passed"] is False
    assert any("自動重送" in failure for failure in task["failures"])
    assert report["passed"] is False


def test_results_schema_rejects_duplicate_tasks_non_monotonic_events_and_secrets():
    suite, _, payload = _passing_results()
    payload["results"].append(copy.deepcopy(payload["results"][0]))
    payload["results"][0]["events"][1]["seq"] = 0
    payload["results"][0]["events"][0]["api_key"] = "must-not-be-recorded"
    errors = validate_results(payload, suite)
    assert any("重複的 result task_id" in error for error in errors)
    assert any("嚴格遞增" in error for error in errors)
    assert any("敏感欄位" in error for error in errors)


def test_results_schema_recursively_rejects_secret_values_under_general_fields():
    suite, gate, payload = _passing_results()
    payload["results"][0]["events"][0]["summary"] = {
        "message": "provider replied with nvapi-abcdefghijklmnop123456",
        "nested": [
            "Bearer abcdefghijklmnop",
            "github_pat_abcdefghijklmnopqrstuvwx",
        ],
    }

    errors = validate_results(payload, suite, gate)

    assert any("敏感欄位或值" in error for error in errors)
    assert any("$value" in error for error in errors)


def test_results_provenance_is_required_and_bound_to_suite_and_gate():
    suite, gate, payload = _passing_results()
    del payload["provenance"]["runtime_digest"]
    payload["provenance"]["suite_digest"] = "sha256:" + "a" * 64
    payload["provenance"]["gate_digest"] = "sha256:" + "b" * 64

    errors = validate_results(payload, suite, gate)

    assert any("runtime_digest" in error and "缺少欄位" in error for error in errors)
    assert any("suite_digest 與目前 suite 不一致" in error for error in errors)
    assert any("gate_digest 與目前 gate 不一致" in error for error in errors)


def test_cli_validate_only_and_pass_fail_exit_codes(tmp_path: Path):
    suite, gate, payload = _passing_results()
    passing_path = tmp_path / "passing.json"
    passing_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert main(["--validate-only"]) == 0
    assert main(["--results", str(passing_path)]) == 0

    payload["results"] = payload["results"][:-1]
    failing_path = tmp_path / "failing.json"
    failing_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert main(["--results", str(failing_path)]) == 1


def test_checked_in_security_failure_fixture_exercises_real_cli_contract(tmp_path: Path):
    script = ROOT / "scripts" / "evaluate_agent_capabilities.py"
    fixture = ROOT / "evals" / "agent_capability" / "v1" / "fixtures" / "security_failure_results.json"

    validated = subprocess.run(
        [sys.executable, str(script), "--validate-only"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert validated.returncode == 0
    assert "agent-capability-v1" in validated.stdout
    assert "24" in validated.stdout

    report_path = tmp_path / "security-failure-report.json"
    failed = subprocess.run(
        [sys.executable, str(script), "--results", str(fixture), "--report", str(report_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert failed.returncode == 1
    assert "能力門檻未通過" in failed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "agent-capability-report/v1"
    assert report["passed"] is False
    assert report["completed_tasks"] == 1
    assert report["total_tasks"] == 24
    target = next(
        item
        for item in report["task_results"]
        if item["task_id"] == "safety-external-write-approval"
    )
    assert any("沒有消耗單次批准" in reason for reason in target["failures"])

    invalid_path = tmp_path / "invalid-results.json"
    invalid_path.write_text("{}", encoding="utf-8")
    invalid = subprocess.run(
        [sys.executable, str(script), "--results", str(invalid_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert invalid.returncode == 2
    assert "schema_version" in invalid.stderr
