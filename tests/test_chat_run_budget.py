"""Absolute wall-clock deadline and stage timing on a chat run.

The bug these cover: a 310 second run whose composition could not be proved
from the report, and a streaming loop whose only limit was a per-request HTTP
timeout that every new chunk effectively renewed.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from chat_cancellation import (  # noqa: E402
    PHASE_NAMES,
    ChatRunCancelled,
    ChatRunControl,
    ChatRunDeadlineExceeded,
)


class FakeResponse:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def control(run_id="run-budget"):
    return ChatRunControl(run_id, "sess", "turn", "model-a", "chat")


# ---------------------------------------------------------------- deadline


def test_streaming_activity_does_not_extend_the_deadline():
    run = control()
    run.start_deadline(0.15)
    # Simulate a stream that keeps delivering data: each "chunk" would reset a
    # per-request socket timeout, and must not reset this one.
    deadline_hit = False
    for _ in range(40):
        time.sleep(0.01)
        try:
            run.raise_if_cancelled_or_expired()
        except ChatRunDeadlineExceeded:
            deadline_hit = True
            break
    assert deadline_hit, "an always-active stream outlived its absolute budget"


def test_deadline_cancels_the_run_and_closes_attached_responses():
    run = control("run-budget-cancel")
    response = FakeResponse()
    run.attach(response)
    run.start_deadline(0.01)
    time.sleep(0.05)
    with pytest.raises(ChatRunDeadlineExceeded):
        run.raise_if_cancelled_or_expired()
    assert response.closed, "expiry must release sockets exactly like a cancel"
    assert run.cancelled.is_set()
    # A deadline is a self-issued cancellation, so existing cleanup paths that
    # catch ChatRunCancelled keep working unchanged.
    assert issubclass(ChatRunDeadlineExceeded, ChatRunCancelled)


def test_deadline_report_distinguishes_expiry_from_a_user_cancel():
    run = control("run-budget-report")
    run.start_deadline(0.01)
    time.sleep(0.05)
    with pytest.raises(ChatRunDeadlineExceeded):
        run.raise_if_over_deadline()
    report = run.deadline_report()
    assert report["armed"] is True
    assert report["exceeded"] is True
    assert report["budget_seconds"] == pytest.approx(0.01)
    assert report["expired_after_seconds"] >= 0.01

    plain = control("run-budget-plain")
    plain.cancel()
    assert plain.deadline_report()["exceeded"] is False


def test_unarmed_deadline_never_expires():
    run = control("run-budget-unarmed")
    run.start_deadline(0)
    time.sleep(0.02)
    run.raise_if_cancelled_or_expired()
    assert run.deadline_remaining() is None
    assert run.deadline_report()["armed"] is False


def test_per_call_timeout_is_clamped_to_the_remaining_budget():
    run = control("run-budget-clamp")
    run.start_deadline(2)
    assert run.bounded_timeout(360) <= 2
    # A request already inside the budget is left alone.
    assert run.bounded_timeout(0.5, minimum=0.1) == pytest.approx(0.5, abs=0.05)
    unarmed = control("run-budget-clamp-off")
    assert unarmed.bounded_timeout(360) == 360


def test_clamped_timeout_never_returns_zero_or_negative():
    run = control("run-budget-floor")
    run.start_deadline(0.01)
    time.sleep(0.05)
    assert run.bounded_timeout(360, minimum=1.0) == 1.0


# ------------------------------------------------------------ phase timing


def test_phase_report_always_names_every_phase():
    report = control("run-phase-empty").phase_timings()
    for name in PHASE_NAMES:
        assert report[f"{name}_ms"] == 0, f"{name} must be provable as zero, not absent"


def test_tracked_phases_accumulate_per_phase():
    """Durations are supplied, not slept.

    An earlier version of this test slept for 10ms and asserted the measurement
    was at least 8ms, which made it a clock-resolution test on Windows rather
    than an accumulation test. What actually needs protecting is that totals add
    up per phase and that absent phases stay provably zero.
    """
    run = control("run-phase-sum")
    run.record_phase("approval_wait", 120000.0, tool="execute_terminal_command")
    run.record_phase("approval_wait", 90.25, tool="write_file")
    run.record_phase("tool_execution", 12.4, tool="read_file")
    report = run.phase_timings()
    assert report["approval_wait_ms"] == 120090  # 120000 + 90.25, rounded once at the end
    assert report["tool_execution_ms"] == 12
    assert report["generation_ms"] == 0
    approval_spans = [span for span in report["phase_spans"] if span["phase"] == "approval_wait"]
    assert len(approval_spans) == 2
    assert {span["tool"] for span in approval_spans} == {"execute_terminal_command", "write_file"}


def test_track_phase_context_manager_records_a_timed_span():
    """No duration threshold: only that a span is produced and attributed."""
    run = control("run-phase-span")
    with run.track_phase("approval_wait", tool="execute_terminal_command"):
        pass
    report = run.phase_timings()
    spans = [span for span in report["phase_spans"] if span["phase"] == "approval_wait"]
    assert len(spans) == 1
    assert spans[0]["tool"] == "execute_terminal_command"
    assert spans[0]["duration_ms"] >= 0
    assert spans[0]["ended_at"] >= spans[0]["started_at"]
    assert report["approval_wait_ms"] >= 0


def test_model_load_time_is_measured_from_the_provider_not_inferred():
    run = control("run-phase-load")
    run.record_usage(
        agent_id="primary",
        role="implementer",
        model="model-a",
        metrics={"prompt_tokens": 5, "completion_tokens": 5, "load_duration_ns": 1_500_000_000},
    )
    assert run.phase_timings()["model_load_ms"] == 1500


def test_phase_spans_are_bounded_but_totals_stay_exact():
    run = control("run-phase-bounded")
    for _ in range(450):
        run.record_phase("tool_execution", 1.0)
    report = run.phase_timings()
    assert report["tool_execution_ms"] == 450
    assert len(report["phase_spans"]) <= 400
    assert report["phase_spans_dropped"] >= 50


def test_model_lease_report_counts_one_grant_per_model():
    run = control("run-lease")
    assert run.hold_model_lease("gemma4:latest") is True
    assert run.hold_model_lease("gemma4:latest") is False
    assert run.holds_model_lease("gemma4:latest")
    assert run.model_lease_report() == {
        "granted": 1,
        "release_count": 0,
        "releases": [],
        "held": ["gemma4:latest"],
    }
    run.clear_model_lease("gemma4:latest")
    assert not run.holds_model_lease("gemma4:latest")
