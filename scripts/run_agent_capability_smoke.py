"""Run a deterministic, offline Agent Runtime smoke and record raw Run evidence.

This is a real execution harness, not a passing-result generator: scenario
operations enter a small governed dispatcher, which decides approvals, binds
argument digests, enforces EXECUTION_UNKNOWN suppression, and appends
Workbench-style Run events.  A separate exporter must validate and normalize
the resulting evidence before the capability gate can read it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
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
    validate_suite,
)
from export_agent_capability_results import EVIDENCE_SCHEMA, evidence_digest


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from project_knowledge import ProjectKnowledgeService  # noqa: E402
from task_planner import (  # noqa: E402
    StepKind,
    build_task_plan,
    is_explicit_multistep_request,
)
DEFAULT_SCENARIOS = (
    REPO_ROOT
    / "evals"
    / "agent_capability"
    / "v1"
    / "runtime_smoke_scenarios.json"
)
SCENARIO_SCHEMA = "agent-capability-runtime-smoke/v1"
APPROVAL_RISKS = {"external_write", "data_disclosure", "destructive"}


class SmokeRuntimeError(ValueError):
    """Raised when deterministic scenario input violates runtime invariants."""


class _RunRecorder:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.events: List[Dict[str, Any]] = []

    def append(self, event: str, payload: Mapping[str, Any]) -> None:
        self.events.append(
            {
                "run_id": self.run_id,
                "sequence": len(self.events),
                "event": event,
                "payload": dict(payload),
            }
        )


class DeterministicSmokeRuntime:
    """Narrow governed runtime used by CI without model, network, or writes."""

    def __init__(self, recorder: _RunRecorder, policy: Mapping[str, Any]) -> None:
        self.recorder = recorder
        self.tool_risks = {
            str(name): str(risk)
            for name, risk in dict(policy.get("tool_risks") or {}).items()
        }
        self.block_after_unknown = bool(policy.get("block_after_execution_unknown", True))
        self.execution_unknown = False

    def create_plan(self, steps: Sequence[Mapping[str, Any]]) -> None:
        normalized: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw in steps:
            step_id = str(raw.get("id") or "")
            if not step_id or step_id in seen:
                raise SmokeRuntimeError("計畫步驟 id 必須唯一且非空")
            seen.add(step_id)
            step = {"id": step_id}
            if "tool_budget" in raw:
                budget = raw["tool_budget"]
                if not isinstance(budget, int) or isinstance(budget, bool) or budget < 0:
                    raise SmokeRuntimeError("tool_budget 必須是非負整數")
                step["tool_budget"] = budget
            normalized.append(step)
        self.recorder.append("plan_created", {"steps": normalized})

    def step(self, step_id: str, status: str, *, reason: str = "") -> None:
        names = {
            "started": "plan_step_started",
            "completed": "plan_step_completed",
            "failed": "plan_step_failed",
            "skipped": "plan_step_skipped",
        }
        event = names.get(status)
        if event is None:
            raise SmokeRuntimeError(f"不支援的計畫步驟狀態：{status}")
        payload: Dict[str, Any] = {"step_id": str(step_id)}
        if reason:
            payload["reason"] = str(reason)
        self.recorder.append(event, payload)

    def call_tool(
        self,
        tool: str,
        *,
        arguments: Mapping[str, Any],
        outcome: str = "success",
        tamper_after_approval: bool = False,
        strategy_id: str = "",
        result: Mapping[str, Any] | None = None,
    ) -> None:
        name = str(tool)
        if self.execution_unknown and self.block_after_unknown:
            self.recorder.append(
                "tool_skipped",
                {"tool": name, "reason": "execution_unknown"},
            )
            return
        risk = self.tool_risks.get(name, "read_only")
        digest = canonical_digest(dict(arguments))
        if risk in APPROVAL_RISKS:
            self.recorder.append(
                "approval_required",
                {
                    "tool": name,
                    "arguments_digest": digest,
                    "risk": risk,
                    "remember_allowed": risk != "destructive",
                },
            )
            if tamper_after_approval:
                self.recorder.append(
                    "approval_rejected",
                    {
                        "tool": name,
                        "arguments_digest": canonical_digest(
                            {**dict(arguments), "tampered": True}
                        ),
                        "reason": "digest_mismatch",
                    },
                )
                return
            self.recorder.append(
                "approval_consumed",
                {"tool": name, "arguments_digest": digest},
            )
        call_id = "call-" + hashlib.sha256(
            f"{self.recorder.run_id}:{len(self.recorder.events)}:{name}".encode("utf-8")
        ).hexdigest()[:16]
        started: Dict[str, Any] = {
            "tool": name,
            "call_id": call_id,
            "arguments_digest": digest,
        }
        if strategy_id:
            started["strategy_id"] = str(strategy_id)
        self.recorder.append("tool_start", started)
        completed: Dict[str, Any] = {
            "tool": name,
            "call_id": call_id,
            "outcome": str(outcome),
        }
        for key, value in dict(result or {}).items():
            if key not in {
                "source_ids",
                "resource_scope",
                "cross_project",
                "scope_check",
            }:
                raise SmokeRuntimeError(f"工具結果欄位未列入證據契約：{key}")
            completed[key] = value
        self.recorder.append("tool_end", completed)
        if outcome == "execution_unknown":
            self.execution_unknown = True

    def verify(self, *, passed: bool, artifact_id: str = "") -> None:
        self.recorder.append("verification_started", {})
        payload: Dict[str, Any] = {}
        if artifact_id:
            payload["artifact_id"] = str(artifact_id)
        self.recorder.append(
            "verification_passed" if passed else "verification_failed",
            payload,
        )

    def answer(
        self,
        *,
        citations: Sequence[str] | None = None,
        action_required: str = "",
    ) -> None:
        payload: Dict[str, Any] = {
            "text_digest": canonical_digest(
                {
                    "run_id": self.recorder.run_id,
                    "event_count": len(self.recorder.events),
                }
            )
        }
        if citations is not None:
            payload["citations"] = [str(item) for item in citations]
        if action_required:
            payload["action_required"] = str(action_required)
        self.recorder.append("response_final", payload)


def _run_scenario(
    task: Mapping[str, Any],
    scenario: Mapping[str, Any],
    *,
    policy: Mapping[str, Any],
    config_digest: str,
    policy_digest: str,
    trial: int,
) -> Dict[str, Any]:
    task_id = str(task["id"])
    run_id = "run_eval_" + hashlib.sha256(
        f"{task_id}:{trial}".encode("utf-8")
    ).hexdigest()[:24]
    recorder = _RunRecorder(run_id)
    runtime = DeterministicSmokeRuntime(recorder, policy)
    operations = scenario.get("operations")
    if not isinstance(operations, list) or not operations:
        raise SmokeRuntimeError(f"{task_id} 沒有可執行 operations")
    for operation in operations:
        if not isinstance(operation, dict):
            raise SmokeRuntimeError(f"{task_id} operation 必須是物件")
        kind = str(operation.get("op") or "")
        if kind == "plan":
            runtime.create_plan(operation.get("steps") or [])
        elif kind == "step":
            runtime.step(
                str(operation.get("step_id") or ""),
                str(operation.get("status") or ""),
                reason=str(operation.get("reason") or ""),
            )
        elif kind == "tool":
            runtime.call_tool(
                str(operation.get("tool") or ""),
                arguments=dict(operation.get("arguments") or {}),
                outcome=str(operation.get("outcome") or "success"),
                tamper_after_approval=bool(operation.get("tamper_after_approval")),
                strategy_id=str(operation.get("strategy_id") or ""),
                result=dict(operation.get("result") or {}),
            )
        elif kind == "verify":
            runtime.verify(
                passed=bool(operation.get("passed")),
                artifact_id=str(operation.get("artifact_id") or ""),
            )
        elif kind == "answer":
            runtime.answer(
                citations=operation.get("citations"),
                action_required=str(operation.get("action_required") or ""),
            )
        else:
            raise SmokeRuntimeError(f"{task_id} 含未知 operation：{kind!r}")
    if not recorder.events or recorder.events[-1]["event"] != "response_final":
        raise SmokeRuntimeError(f"{task_id} 未產生終端 response_final")
    return {
        "run_id": run_id,
        "status": "completed",
        "input_manifest": {
            "version": 1,
            "suite_id": str(task.get("suite_id") or "agent-capability-v1"),
            "task_id": task_id,
            "prompt_sha256": canonical_digest(task["prompt"]),
            "config_digest": config_digest,
            "policy_digest": policy_digest,
            "trial": trial,
        },
        "events": recorder.events,
    }


def _git_provenance() -> tuple[str, str, bool]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
        diff = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--binary", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
        return commit, canonical_digest(
            {
                "commit": commit,
                "status": status.replace("\r\n", "\n"),
                "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
            }
        ), bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        return "unavailable", canonical_digest({"git": "unavailable"}), True


def _runtime_digest() -> str:
    paths = [
        Path(__file__).resolve(),
        Path(__file__).resolve().with_name("export_agent_capability_results.py"),
        Path(__file__).resolve().with_name("evaluate_agent_capabilities.py"),
        BACKEND_ROOT / "app.py",
        BACKEND_ROOT / "chat" / "runtime.py",
        BACKEND_ROOT / "factual_verifier.py",
        BACKEND_ROOT / "task_planner.py",
        BACKEND_ROOT / "project_knowledge.py",
        BACKEND_ROOT / "semantic_retrieval.py",
        BACKEND_ROOT / "tool_runtime.py",
        BACKEND_ROOT / "model_governance.py",
    ]
    return canonical_digest(
        {
            str(path.relative_to(REPO_ROOT)).replace("\\", "/"): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in paths
        }
    )


def _production_core_preflight(suite: Mapping[str, Any]) -> None:
    """Exercise product Planner and Project-RAG invariants before contract smoke.

    The 24 scripted traces remain a deterministic Gate-contract smoke, not a
    formal model score.  This preflight prevents that smoke from staying green
    when the product planner can no longer decompose the suite's planning
    prompts or when the real local knowledge service loses Project isolation.
    """

    planner_tools = (
        {"name": "eval.search", "description": "搜尋 查詢 查找 收集 取得來源"},
        {"name": "eval.read", "description": "讀取 閱讀 開啟 文件"},
        {"name": "eval.compare", "description": "比較 比對 排序 重排"},
        {"name": "eval.verify", "description": "驗證 檢查 格式正確"},
        {"name": "eval.write", "description": "撰寫 寫入 產生報告 整理 輸出"},
    )
    for task in suite.get("tasks") or []:
        if not isinstance(task, Mapping) or task.get("category") != "planning":
            continue
        prompt = str(task.get("prompt") or "")
        if not is_explicit_multistep_request(prompt):
            raise SmokeRuntimeError(
                f"產品 Planner 未辨識評測中的多步驟任務：{task.get('id')}"
            )
        plan = build_task_plan(prompt, planner_tools)
        action_steps = [
            step
            for step in plan.steps
            if step.kind not in {StepKind.VERIFY, StepKind.SYNTHESIZE}
        ]
        expected_plan = dict((task.get("expectations") or {}).get("plan") or {})
        minimum = int(expected_plan.get("min_steps") or 0)
        if len(action_steps) < minimum:
            raise SmokeRuntimeError(
                f"產品 Planner 對 {task.get('id')} 只產生 {len(action_steps)} 個工作步驟"
            )
        if expected_plan.get("require_step_budgets"):
            tool_steps = [step for step in action_steps if step.kind is StepKind.TOOL]
            if not tool_steps or any(step.tool_budget <= 0 for step in tool_steps):
                raise SmokeRuntimeError(
                    f"產品 Planner 未替 {task.get('id')} 的工具步驟配置獨立預算"
                )

    with tempfile.TemporaryDirectory(prefix="workbench-eval-rag-") as directory:
        service = ProjectKnowledgeService(Path(directory) / "knowledge.sqlite3")
        service.import_document(
            project_id="eval-project-a",
            source_id="a.md",
            title="A",
            content="alpha-retention-policy-unique 本專案保留政策",
        )
        service.import_document(
            project_id="eval-project-b",
            source_id="b.md",
            title="B",
            content="beta-private-deadline-unique 其他專案期限",
        )
        hits = service.retrieve(
            project_id="eval-project-a",
            query="alpha-retention-policy-unique",
            top_k=4,
            candidate_limit=20,
        )
        if not hits or any(
            item.get("citation", {}).get("project_id") != "eval-project-a"
            or item.get("citation", {}).get("source_id") == "b.md"
            for item in hits
        ):
            raise SmokeRuntimeError("產品 Project RAG 的隔離或召回前置驗證失敗")


def run_smoke(
    suite: Mapping[str, Any],
    gate: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    trial: int,
) -> Dict[str, Any]:
    if config.get("schema_version") != SCENARIO_SCHEMA:
        raise SmokeRuntimeError(f"scenario schema 必須是 {SCENARIO_SCHEMA}")
    if config.get("suite_id") != suite.get("suite_id"):
        raise SmokeRuntimeError("scenario suite_id 與 suite 不一致")
    policy = config.get("policy")
    scenarios = config.get("scenarios")
    if not isinstance(policy, dict) or not isinstance(scenarios, list):
        raise SmokeRuntimeError("scenario policy/scenarios 格式無效")
    _production_core_preflight(suite)
    scenario_by_id: Dict[str, Mapping[str, Any]] = {}
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != {"task_id", "operations"}:
            raise SmokeRuntimeError("每個 scenario 必須只包含 task_id 與 operations")
        task_id = str(scenario.get("task_id") or "")
        if not task_id or task_id in scenario_by_id:
            raise SmokeRuntimeError(f"scenario task_id 重複或空白：{task_id!r}")
        scenario_by_id[task_id] = scenario
    tasks = [task for task in suite.get("tasks", []) if isinstance(task, dict)]
    task_ids = {str(task.get("id") or "") for task in tasks}
    if set(scenario_by_id) != task_ids:
        raise SmokeRuntimeError("scenario 必須與 suite 任務一對一完整對應")
    secret_paths = _find_secret_paths(config, path="scenario")
    if secret_paths:
        raise SmokeRuntimeError("scenario 含敏感資料：" + ", ".join(secret_paths[:5]))
    config_digest = canonical_digest(config)
    policy_digest = canonical_digest(policy)
    runs: List[Dict[str, Any]] = []
    for task in tasks:
        bound_task = {**task, "suite_id": suite["suite_id"]}
        runs.append(
            _run_scenario(
                bound_task,
                scenario_by_id[str(task["id"])],
                policy=policy,
                config_digest=config_digest,
                policy_digest=policy_digest,
                trial=trial,
            )
        )
    git_commit, git_digest, git_dirty = _git_provenance()
    model_descriptor = {
        "id": "deterministic-scripted-model",
        "version": "1",
        "network": False,
        "temperature": 0,
    }
    evidence: Dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA,
        "suite_id": suite["suite_id"],
        "subject": {
            "id": "workbench-contract-smoke-with-product-preflight",
            "version": "1",
        },
        "provenance": {
            "source": "deterministic_contract_smoke_with_product_preflight",
            "git_commit": git_commit,
            "git_digest": git_digest,
            "git_dirty": git_dirty,
            "runtime_id": "workbench-governed-smoke-runtime",
            "runtime_version": "1",
            "runtime_digest": _runtime_digest(),
            "model_id": model_descriptor["id"],
            "model_version": model_descriptor["version"],
            "model_digest": canonical_digest(model_descriptor),
            "config_digest": config_digest,
            "policy_digest": policy_digest,
            "suite_digest": canonical_digest(suite),
            "gate_digest": canonical_digest(gate),
            "evidence_digest": "sha256:" + "0" * 64,
            "trial": trial,
        },
        "runs": runs,
    }
    evidence["provenance"]["evidence_digest"] = evidence_digest(evidence)
    return evidence


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
    parser = argparse.ArgumentParser(description="執行離線 Agent Runtime 能力 smoke")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--gate", type=Path, default=DEFAULT_GATE)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--trial", type=int, default=1)
    args = parser.parse_args(argv)
    if args.trial < 1:
        print("--trial 必須是正整數", file=sys.stderr)
        return 2
    try:
        suite = load_json(args.suite)
        gate = load_json(args.gate)
        contract_errors = validate_suite(suite) + validate_gate(gate, suite)
        if contract_errors:
            raise SmokeRuntimeError("；".join(contract_errors))
        evidence = run_smoke(
            suite,
            gate,
            load_json(args.scenarios),
            trial=args.trial,
        )
        _atomic_json(args.evidence, evidence)
    except (ContractError, SmokeRuntimeError, OSError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"已完成 {len(evidence['runs'])} 個離線 contract smoke Run：{args.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
