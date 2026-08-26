"""End-to-end tests for deterministic Runtime evidence and export."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from evaluate_agent_capabilities import (  # noqa: E402
    DEFAULT_GATE,
    DEFAULT_SUITE,
    evaluate_suite,
    load_json,
    validate_results,
)
from export_agent_capability_results import (  # noqa: E402
    EvidenceError,
    evidence_digest,
    export_evidence,
)
from run_agent_capability_smoke import (  # noqa: E402
    DEFAULT_SCENARIOS,
    SmokeRuntimeError,
    run_smoke,
)


def _evidence_chain():
    suite = load_json(DEFAULT_SUITE)
    gate = load_json(DEFAULT_GATE)
    config = load_json(DEFAULT_SCENARIOS)
    evidence = run_smoke(suite, gate, config, trial=1)
    results = export_evidence(evidence, suite, gate)
    return suite, gate, config, evidence, results


def _raw_events(evidence: dict, task_id: str) -> list[dict]:
    run = next(
        item
        for item in evidence["runs"]
        if item["input_manifest"]["task_id"] == task_id
    )
    return run["events"]


def test_contract_smoke_evidence_exports_and_passes_the_real_gate():
    suite, gate, _config, evidence, results = _evidence_chain()

    assert len(evidence["runs"]) == len(suite["tasks"]) == 24
    assert validate_results(results, suite, gate) == []
    report = evaluate_suite(suite, gate, results)
    assert report["passed"] is True
    assert report["overall_score"] == 1.0
    assert report["provenance"] == results["provenance"]
    assert results["provenance"]["source"] == (
        "deterministic_contract_smoke_with_product_preflight"
    )
    for field in (
        "git_digest",
        "runtime_digest",
        "model_digest",
        "config_digest",
        "policy_digest",
        "suite_digest",
        "gate_digest",
        "evidence_digest",
    ):
        assert results["provenance"][field].startswith("sha256:")


def test_runtime_policy_generates_approval_and_unknown_suppression_events():
    _suite, _gate, _config, evidence, _results = _evidence_chain()

    read_names = [
        item["event"]
        for item in _raw_events(evidence, "safety-read-without-approval")
    ]
    write_events = _raw_events(evidence, "safety-external-write-approval")
    unknown_events = _raw_events(evidence, "unknown-skips-following-tools")

    assert "approval_required" not in read_names
    assert [item["event"] for item in write_events][:3] == [
        "approval_required",
        "approval_consumed",
        "tool_start",
    ]
    required_digest = write_events[0]["payload"]["arguments_digest"]
    assert write_events[1]["payload"]["arguments_digest"] == required_digest
    assert write_events[2]["payload"]["arguments_digest"] == required_digest
    skipped = next(item for item in unknown_events if item["event"] == "tool_skipped")
    assert skipped["payload"] == {
        "tool": "notion.blocks.append",
        "reason": "execution_unknown",
    }


def test_runtime_events_are_not_synthesized_from_gate_expectations():
    suite = load_json(DEFAULT_SUITE)
    gate = load_json(DEFAULT_GATE)
    config = load_json(DEFAULT_SCENARIOS)
    baseline = run_smoke(suite, gate, config, trial=1)
    altered_contract = copy.deepcopy(suite)
    for task in altered_contract["tasks"]:
        task["expectations"] = {
            "forbidden_tools": ["every.tool"],
            "max_tool_calls": 0,
        }

    altered = run_smoke(altered_contract, gate, config, trial=1)

    # Suite provenance changes, but the Runtime actions and Run events do not:
    # the harness never reads expectations to manufacture a passing trace.
    assert altered["provenance"]["suite_digest"] != baseline["provenance"]["suite_digest"]
    assert altered["runs"] == baseline["runs"]


def test_contract_smoke_fails_when_product_planner_cannot_recognize_suite_prompt():
    suite = copy.deepcopy(load_json(DEFAULT_SUITE))
    gate = load_json(DEFAULT_GATE)
    config = load_json(DEFAULT_SCENARIOS)
    planning_task = next(
        task for task in suite["tasks"] if task["category"] == "planning"
    )
    planning_task["prompt"] = "請只回答一個簡短問題"

    with pytest.raises(SmokeRuntimeError, match="產品 Planner 未辨識"):
        run_smoke(suite, gate, config, trial=1)


def test_exporter_rejects_tampered_authoritative_run_evidence():
    suite, gate, _config, evidence, _results = _evidence_chain()
    tampered = copy.deepcopy(evidence)
    tampered["runs"][0]["events"][0]["payload"]["tool"] = "shell.execute"

    with pytest.raises(EvidenceError, match="evidence digest 不一致"):
        export_evidence(tampered, suite, gate)


def test_exporter_recursively_rejects_secret_values_even_with_new_digest():
    suite, gate, _config, evidence, _results = _evidence_chain()
    leaked = copy.deepcopy(evidence)
    leaked["runs"][0]["events"][0]["payload"]["reason"] = (
        "Bearer abcdefghijklmnop"
    )
    leaked["provenance"]["evidence_digest"] = evidence_digest(leaked)

    with pytest.raises(EvidenceError, match="敏感欄位或值"):
        export_evidence(leaked, suite, gate)


def test_runtime_smoke_cli_exporter_and_gate_are_deterministic(tmp_path: Path):
    runner = ROOT / "scripts" / "run_agent_capability_smoke.py"
    exporter = ROOT / "scripts" / "export_agent_capability_results.py"
    evaluator = ROOT / "scripts" / "evaluate_agent_capabilities.py"
    evidence_one = tmp_path / "evidence-one.json"
    evidence_two = tmp_path / "evidence-two.json"
    results = tmp_path / "results.json"
    report = tmp_path / "report.json"

    for evidence_path in (evidence_one, evidence_two):
        completed = subprocess.run(
            [sys.executable, str(runner), "--evidence", str(evidence_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
    assert evidence_one.read_bytes() == evidence_two.read_bytes()

    exported = subprocess.run(
        [
            sys.executable,
            str(exporter),
            "--evidence",
            str(evidence_one),
            "--output",
            str(results),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert exported.returncode == 0, exported.stderr
    gated = subprocess.run(
        [
            sys.executable,
            str(evaluator),
            "--results",
            str(results),
            "--report",
            str(report),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert gated.returncode == 0, gated.stderr
    assert json.loads(report.read_text(encoding="utf-8"))["passed"] is True
