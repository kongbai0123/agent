"""Export authoritative Workbench-style Run evidence into capability results.

The input is intentionally not the gate result schema.  It contains terminal
Runs, private-but-content-free input manifests, and append-only Run events.
This exporter verifies their bindings and digests, projects a strict event
allowlist, and only then emits ``agent-capability-results/v1``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from evaluate_agent_capabilities import (
    DEFAULT_GATE,
    DEFAULT_SUITE,
    ContractError,
    _find_secret_paths,
    canonical_digest,
    load_json,
    validate_gate,
    validate_results,
    validate_suite,
)


EVIDENCE_SCHEMA = "workbench-agent-run-evidence/v1"
RESULTS_SCHEMA = "agent-capability-results/v1"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "suite_id",
    "subject",
    "provenance",
    "runs",
}
_RUN_FIELDS = {"run_id", "status", "input_manifest", "events"}
_MANIFEST_FIELDS = {
    "version",
    "suite_id",
    "task_id",
    "prompt_sha256",
    "config_digest",
    "policy_digest",
    "trial",
}
_EVENT_FIELDS = {"run_id", "sequence", "event", "payload"}
_PAYLOAD_FIELDS = {
    "tool",
    "call_id",
    "arguments_digest",
    "risk",
    "remember_allowed",
    "outcome",
    "status",
    "reason",
    "action_required",
    "source_ids",
    "resource_scope",
    "cross_project",
    "citations",
    "steps",
    "step_id",
    "artifact_id",
    "strategy_id",
    "scope_check",
    "text_digest",
}
_EVENT_NAMES = {
    "tool_start": "tool_started",
    "tool_end": "tool_completed",
    "response_final": "answer_final",
    "plan_created": "plan_created",
    "plan_step_started": "plan_step_started",
    "plan_step_completed": "plan_step_completed",
    "plan_step_failed": "plan_step_failed",
    "plan_step_skipped": "plan_step_skipped",
    "approval_required": "approval_required",
    "approval_consumed": "approval_consumed",
    "approval_rejected": "approval_rejected",
    "tool_skipped": "tool_skipped",
    "verification_started": "verification_started",
    "verification_passed": "verification_passed",
    "verification_failed": "verification_failed",
}
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}


class EvidenceError(ValueError):
    """Raised when raw Run evidence cannot be trusted or normalized."""


def evidence_digest(evidence: Mapping[str, Any]) -> str:
    """Digest the authority-bearing portion without circular provenance data."""

    return canonical_digest(
        {
            "schema_version": evidence.get("schema_version"),
            "suite_id": evidence.get("suite_id"),
            "subject": evidence.get("subject"),
            "runs": evidence.get("runs"),
        }
    )


def _require_exact_fields(
    value: Mapping[str, Any], expected: set[str], location: str
) -> None:
    missing = sorted(expected - set(value))
    extras = sorted(set(value) - expected)
    if missing:
        raise EvidenceError(f"{location} 缺少欄位：{', '.join(missing)}")
    if extras:
        raise EvidenceError(f"{location} 含未知欄位：{', '.join(extras)}")


def _normalize_event(raw: Mapping[str, Any], *, run_id: str) -> Dict[str, Any]:
    _require_exact_fields(raw, _EVENT_FIELDS, "run.events[]")
    if raw.get("run_id") != run_id:
        raise EvidenceError("Run event 的 run_id 與所屬 Run 不一致")
    sequence = raw.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise EvidenceError("Run event sequence 必須是非負整數")
    raw_name = str(raw.get("event") or "")
    event_type = _EVENT_NAMES.get(raw_name)
    if event_type is None:
        raise EvidenceError(f"Run evidence 含不支援的事件：{raw_name!r}")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise EvidenceError("Run event payload 必須是物件")
    extras = sorted(set(payload) - _PAYLOAD_FIELDS)
    if extras:
        raise EvidenceError(
            "Run event payload 含不可匯出的欄位：" + ", ".join(extras)
        )
    projected = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    if event_type == "tool_completed" and "outcome" not in projected:
        status = str(projected.pop("status", "") or "")
        projected["outcome"] = "success" if status == "completed" else status
    else:
        projected.pop("status", None)
    return {"seq": sequence, "type": event_type, **projected}


def export_evidence(
    evidence: Any,
    suite: Mapping[str, Any],
    gate: Mapping[str, Any],
) -> Dict[str, Any]:
    if not isinstance(evidence, dict):
        raise EvidenceError("evidence 必須是物件")
    _require_exact_fields(evidence, _TOP_LEVEL_FIELDS, "evidence")
    if evidence.get("schema_version") != EVIDENCE_SCHEMA:
        raise EvidenceError(f"evidence.schema_version 必須是 {EVIDENCE_SCHEMA}")
    if evidence.get("suite_id") != suite.get("suite_id"):
        raise EvidenceError("evidence.suite_id 與 suite 不一致")
    subject = evidence.get("subject")
    if not isinstance(subject, dict) or set(subject) != {"id", "version"}:
        raise EvidenceError("evidence.subject 必須只包含 id 與 version")
    if any(not isinstance(subject.get(key), str) or not subject.get(key) for key in subject):
        raise EvidenceError("evidence.subject.id/version 必須是非空字串")
    provenance = evidence.get("provenance")
    if not isinstance(provenance, dict):
        raise EvidenceError("evidence.provenance 必須是物件")
    if provenance.get("suite_digest") != canonical_digest(suite):
        raise EvidenceError("evidence provenance 的 suite digest 不一致")
    if provenance.get("gate_digest") != canonical_digest(gate):
        raise EvidenceError("evidence provenance 的 gate digest 不一致")
    if provenance.get("evidence_digest") != evidence_digest(evidence):
        raise EvidenceError("evidence digest 不一致，Run evidence 可能遭到修改")
    secret_paths = _find_secret_paths(evidence, path="evidence")
    if secret_paths:
        raise EvidenceError(
            "Run evidence 包含禁止保存的敏感欄位或值："
            + ", ".join(secret_paths[:5])
        )

    tasks = {
        str(task["id"]): task
        for task in suite.get("tasks", [])
        if isinstance(task, dict) and task.get("id")
    }
    runs = evidence.get("runs")
    if not isinstance(runs, list):
        raise EvidenceError("evidence.runs 必須是陣列")
    results: List[Dict[str, Any]] = []
    seen_tasks: set[str] = set()
    seen_runs: set[str] = set()
    for index, raw_run in enumerate(runs):
        location = f"evidence.runs[{index}]"
        if not isinstance(raw_run, dict):
            raise EvidenceError(f"{location} 必須是物件")
        _require_exact_fields(raw_run, _RUN_FIELDS, location)
        run_id = str(raw_run.get("run_id") or "")
        if not run_id or run_id in seen_runs:
            raise EvidenceError(f"{location}.run_id 必須唯一且非空")
        seen_runs.add(run_id)
        if raw_run.get("status") not in _TERMINAL_RUN_STATUSES:
            raise EvidenceError(
                f"{location} 必須是 completed／failed／cancelled 的終端 Run"
            )
        manifest = raw_run.get("input_manifest")
        if not isinstance(manifest, dict):
            raise EvidenceError(f"{location}.input_manifest 必須是物件")
        _require_exact_fields(manifest, _MANIFEST_FIELDS, f"{location}.input_manifest")
        if manifest.get("version") != 1:
            raise EvidenceError(f"{location}.input_manifest.version 必須是 1")
        if manifest.get("suite_id") != suite.get("suite_id"):
            raise EvidenceError(f"{location} 的 manifest suite 綁定不一致")
        task_id = str(manifest.get("task_id") or "")
        if task_id not in tasks or task_id in seen_tasks:
            raise EvidenceError(f"{location} 的 task_id 未知或重複：{task_id!r}")
        seen_tasks.add(task_id)
        expected_prompt_digest = canonical_digest(tasks[task_id]["prompt"])
        if manifest.get("prompt_sha256") != expected_prompt_digest:
            raise EvidenceError(f"{location} 的 prompt digest 與 suite 不一致")
        if manifest.get("config_digest") != provenance.get("config_digest"):
            raise EvidenceError(f"{location} 的 config digest 與 provenance 不一致")
        if manifest.get("policy_digest") != provenance.get("policy_digest"):
            raise EvidenceError(f"{location} 的 policy digest 與 provenance 不一致")
        if manifest.get("trial") != provenance.get("trial"):
            raise EvidenceError(f"{location} 的 trial 與 provenance 不一致")
        raw_events = raw_run.get("events")
        if not isinstance(raw_events, list):
            raise EvidenceError(f"{location}.events 必須是陣列")
        normalized = [_normalize_event(event, run_id=run_id) for event in raw_events]
        sequences = [event["seq"] for event in normalized]
        if sequences != list(range(len(sequences))):
            raise EvidenceError(f"{location}.events sequence 必須從 0 連續遞增")
        results.append({"task_id": task_id, "events": normalized})

    exported = {
        "schema_version": RESULTS_SCHEMA,
        "suite_id": evidence["suite_id"],
        "subject": dict(subject),
        "provenance": dict(provenance),
        "results": results,
    }
    result_errors = validate_results(exported, suite, gate)
    if result_errors:
        raise EvidenceError("；".join(result_errors))
    return exported


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="匯出 Workbench Agent Runtime 能力證據")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--gate", type=Path, default=DEFAULT_GATE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        suite = load_json(args.suite)
        gate = load_json(args.gate)
        contract_errors = validate_suite(suite) + validate_gate(gate, suite)
        if contract_errors:
            raise EvidenceError("；".join(contract_errors))
        exported = export_evidence(load_json(args.evidence), suite, gate)
        _atomic_json(args.output, exported)
    except (ContractError, EvidenceError, OSError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"已匯出 {len(exported['results'])} 個已驗證 evidence Run：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
