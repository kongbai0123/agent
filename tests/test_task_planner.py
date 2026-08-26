import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from task_planner import (  # noqa: E402
    DeterministicTaskPlanner,
    ExecutionOutcome,
    PlanBudgetExceeded,
    PlanDeadlineExceeded,
    PlanLimits,
    PlanProgress,
    PlanStatus,
    PlanValidationError,
    PlannerTool,
    StepKind,
    StepStatus,
    TaskPlan,
    TaskStep,
    VerificationCondition,
    VerificationKind,
    VerificationStatus,
    is_explicit_multistep_request,
)


TOOLS = (
    PlannerTool("web.search", "搜尋網頁與研究資料"),
    PlannerTool("docs.write", "撰寫文件與報告"),
    PlannerTool("data.compare", "比較資料與候選結果"),
)


def test_fallback_plan_is_deterministic_bounded_and_typed():
    limits = PlanLimits(max_steps=8, max_tool_calls=6, max_tool_calls_per_step=2)
    planner = DeterministicTaskPlanner(limits)
    request = "先搜尋市場資料，然後比較候選結果，最後撰寫報告"

    first = planner.plan(request, reversed(TOOLS))
    second = planner.plan(request, TOOLS)

    assert first.as_dict() == second.as_dict()
    assert first.planner == "deterministic_fallback_v1"
    assert len(first.steps) <= limits.max_steps
    assert sum(step.tool_budget for step in first.steps) <= limits.max_tool_calls
    assert [step.kind for step in first.steps[-2:]] == [StepKind.VERIFY, StepKind.SYNTHESIZE]
    assert any(step.allowed_tools == ("web.search",) for step in first.steps)
    assert any(step.allowed_tools == ("data.compare",) for step in first.steps)
    assert any(step.allowed_tools == ("docs.write",) for step in first.steps)
    assert all(
        step.tool_budget == limits.max_tool_calls_per_step
        for step in first.steps
        if step.kind is StepKind.TOOL
    )


def test_compact_chinese_sequence_without_punctuation_keeps_per_step_budgets():
    request = "先搜尋市場資料再比較候選結果最後撰寫報告"
    assert is_explicit_multistep_request(request) is True

    plan = DeterministicTaskPlanner().plan(request, TOOLS)
    action_steps = plan.steps[:-2]

    assert [step.instruction for step in action_steps] == [
        "搜尋市場資料",
        "比較候選結果",
        "撰寫報告",
    ]
    assert [step.allowed_tools for step in action_steps] == [
        ("web.search",),
        ("data.compare",),
        ("docs.write",),
    ]
    assert all(step.tool_budget > 0 for step in action_steps)


def test_compact_sequence_does_not_split_inside_renewable_energy_term():
    plan = DeterministicTaskPlanner().plan(
        "先搜尋再生能源市場資料再比較候選結果最後整理報告",
        TOOLS,
    )

    assert [step.instruction for step in plan.steps[:-2]] == [
        "搜尋再生能源市場資料",
        "比較候選結果",
        "整理報告",
    ]


def test_plan_wide_budget_is_distributed_across_later_tool_steps():
    plan = DeterministicTaskPlanner(
        PlanLimits(max_steps=8, max_tool_calls=24, max_tool_calls_per_step=8)
    ).plan(
        "先搜尋 A；再搜尋 B；接著搜尋 C；然後搜尋 D；最後搜尋 E",
        TOOLS,
    )
    action_steps = plan.steps[:-2]
    budgets = [step.tool_budget for step in action_steps]

    assert len(action_steps) == 5
    assert all(step.kind is StepKind.TOOL for step in action_steps)
    assert all(budget > 0 for budget in budgets)
    assert sum(budgets) == 24
    assert max(budgets) - min(budgets) <= 1


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("請說明這個錯誤", False),
        ("請開啟瀏覽器搜尋 n8n", False),
        ("1. 搜尋資料\n2. 比較結果\n3. 撰寫報告", True),
        ("先搜尋資料，然後比較結果，最後撰寫報告", True),
        ("查詢 A、比較 B、再撰寫 C", True),
        ("規劃一個需要搜尋、讀取與驗證的任務", True),
    ],
)
def test_explicit_multistep_detection_is_conservative(query, expected):
    assert is_explicit_multistep_request(query) is expected


def test_many_clauses_are_merged_instead_of_silently_dropped():
    planner = DeterministicTaskPlanner(PlanLimits(max_steps=5))
    plan = planner.plan("一；二；三；四；五；六")
    action_steps = plan.steps[:-2]
    assert len(action_steps) == 3
    assert "六" in action_steps[-1].instruction


def test_topological_sort_is_stable_by_order_then_id():
    plan = TaskPlan(
        plan_id="plan-sort",
        request="排序",
        steps=(
            TaskStep("last", 3, StepKind.REASON, "末", "末", dependencies=("a", "b")),
            TaskStep("b", 2, StepKind.REASON, "乙", "乙"),
            TaskStep("a", 1, StepKind.REASON, "甲", "甲"),
        ),
    )
    assert [step.step_id for step in plan.topological_steps()] == ["a", "b", "last"]


def test_cycle_and_unknown_dependency_are_rejected():
    with pytest.raises(PlanValidationError, match="cycle"):
        TaskPlan(
            plan_id="plan-cycle",
            request="循環",
            steps=(
                TaskStep("a", 0, StepKind.REASON, "甲", "甲", dependencies=("b",)),
                TaskStep("b", 1, StepKind.REASON, "乙", "乙", dependencies=("a",)),
            ),
        )
    with pytest.raises(PlanValidationError, match="unknown dependencies"):
        TaskPlan(
            plan_id="plan-missing",
            request="缺少",
            steps=(TaskStep("a", 0, StepKind.REASON, "甲", "甲", dependencies=("missing",)),),
        )


def test_static_and_runtime_tool_budgets_are_both_enforced():
    tool_condition = VerificationCondition(
        VerificationKind.TOOL_CALLS_SUCCEEDED, "工具成功", source_step_id="a"
    )
    with pytest.raises(PlanValidationError, match="per-step"):
        TaskPlan(
            plan_id="plan-over-budget",
            request="超額",
            limits=PlanLimits(max_tool_calls=8, max_tool_calls_per_step=1),
            steps=(
                TaskStep(
                    "a", 0, StepKind.TOOL, "甲", "甲", allowed_tools=("web.search",),
                    tool_budget=2, verification=(tool_condition,),
                ),
            ),
        )

    plan = DeterministicTaskPlanner(
        PlanLimits(max_steps=4, max_tool_calls=1, max_tool_calls_per_step=1)
    ).plan("搜尋資料", TOOLS)
    progress = PlanProgress(plan)
    action = progress.ready_steps()[0]
    progress.start_step(action.step_id)
    progress.consume_tool_call(action.step_id)
    with pytest.raises(PlanBudgetExceeded):
        progress.consume_tool_call(action.step_id)


def test_successful_progress_runs_verification_and_unlocks_next_step():
    plan = DeterministicTaskPlanner().plan("搜尋資料", TOOLS)
    progress = PlanProgress(plan)
    first = progress.ready_steps()[0]
    assert first.kind is StepKind.TOOL

    progress.start_step(first.step_id)
    progress.consume_tool_call(first.step_id)
    progress.complete_step(
        first.step_id,
        ExecutionOutcome.SUCCEEDED,
        evidence={"tool_calls_succeeded": True},
    )

    state = progress.progress_for(first.step_id)
    assert state.status is StepStatus.SUCCEEDED
    assert state.verification_status is VerificationStatus.PASSED
    assert progress.ready_steps()[0].step_id == plan.steps[1].step_id


def test_failed_verification_blocks_dependent_steps():
    plan = DeterministicTaskPlanner().plan("搜尋資料", TOOLS)
    progress = PlanProgress(plan)
    first = progress.ready_steps()[0]
    progress.start_step(first.step_id)
    progress.complete_step(
        first.step_id,
        ExecutionOutcome.SUCCEEDED,
        evidence={"tool_calls_succeeded": False},
    )

    assert progress.progress_for(first.step_id).status is StepStatus.FAILED
    assert all(
        progress.progress_for(step.step_id).status is StepStatus.BLOCKED
        for step in plan.steps[1:]
    )
    assert progress.status is PlanStatus.FAILED


def test_tool_success_claim_without_a_recorded_call_fails_closed():
    plan = DeterministicTaskPlanner().plan("搜尋資料", TOOLS)
    progress = PlanProgress(plan)
    first = progress.ready_steps()[0]
    progress.start_step(first.step_id)
    progress.complete_step(
        first.step_id,
        ExecutionOutcome.SUCCEEDED,
        evidence={"tool_calls_succeeded": True},
    )
    assert progress.progress_for(first.step_id).status is StepStatus.FAILED
    assert progress.progress_for(first.step_id).error_code == "TASK_STEP_VERIFICATION_FAILED"


def test_execution_unknown_stops_every_later_step_and_cannot_consume_again():
    plan = DeterministicTaskPlanner().plan("搜尋資料，然後撰寫報告", TOOLS)
    progress = PlanProgress(plan)
    first = progress.ready_steps()[0]
    progress.start_step(first.step_id)
    progress.consume_tool_call(first.step_id)
    progress.complete_step(first.step_id, ExecutionOutcome.EXECUTION_UNKNOWN)

    assert progress.status is PlanStatus.EXECUTION_UNKNOWN
    assert progress.stop_reason == "EXECUTION_UNKNOWN"
    assert progress.progress_for(first.step_id).verification_status is VerificationStatus.UNKNOWN
    assert all(
        progress.progress_for(step.step_id).status is StepStatus.SKIPPED
        for step in plan.steps[1:]
    )
    assert progress.ready_steps() == ()
    with pytest.raises(Exception):
        progress.consume_tool_call(first.step_id)
    assert progress.snapshot()["stop_reason"] == "EXECUTION_UNKNOWN"


def test_wall_clock_limit_is_absolute_and_visible_in_snapshot():
    now = [100.0]
    plan = DeterministicTaskPlanner(PlanLimits(max_wall_seconds=5)).plan("整理內容")
    progress = PlanProgress(plan, clock=lambda: now[0])
    first = progress.ready_steps()[0]
    progress.start_step(first.step_id)
    now[0] = 105.0

    with pytest.raises(PlanDeadlineExceeded):
        progress.consume_tool_call(first.step_id)
    snapshot = progress.snapshot()
    assert snapshot["status"] == "timed_out"
    assert snapshot["stop_reason"] == "TASK_PLAN_DEADLINE_EXCEEDED"
    assert snapshot["steps"][0]["status"] == "timed_out"


def test_custom_evidence_flag_is_typed_and_fail_closed():
    condition = VerificationCondition(
        VerificationKind.EVIDENCE_FLAG,
        "輸出格式符合契約",
        source_step_id="a",
        evidence_key="schema_valid",
    )
    plan = TaskPlan(
        plan_id="plan-custom-verify",
        request="驗證",
        steps=(
            TaskStep("a", 0, StepKind.VERIFY, "驗證", "驗證", verification=(condition,)),
        ),
    )
    progress = PlanProgress(plan)
    progress.start_step("a")
    progress.complete_step("a", ExecutionOutcome.SUCCEEDED, evidence={"checks": {}})
    assert progress.progress_for("a").verification_status is VerificationStatus.FAILED
    assert progress.status is PlanStatus.FAILED
