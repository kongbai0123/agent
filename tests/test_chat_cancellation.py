import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from chat_cancellation import (
    ChatRunCancelled,
    ChatRunControl,
    active_chat_models,
    cancel_session_chat_runs,
    cancel_chat_run,
    cancel_or_defer_chat_run,
    get_chat_run,
    register_chat_run,
    release_chat_run,
)


class FakeResponse:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class ChatCancellationTests(unittest.TestCase):
    def test_usage_summary_preserves_exact_agent_metrics(self):
        control = ChatRunControl("run-usage", "session", "turn", "model-a", "chat")
        control.record_usage(
            agent_id="planner-a",
            role="planner",
            model="model-a",
            metrics={
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "load_duration_ns": 100,
                "eval_duration_ns": 200,
            },
        )
        summary = control.usage_summary()
        self.assertEqual(summary["total_tokens"], 18)
        self.assertEqual(summary["load_duration_ns"], 100)
        self.assertEqual(summary["by_agent"][0]["agent_id"], "planner-a")

    def test_usage_summary_estimates_provider_cost_without_storing_a_key(self):
        control = ChatRunControl("run-cost", "session", "turn", "model-a", "chat")
        control.configure_billing(
            provider="openai_compatible",
            input_cost_per_million=2.0,
            output_cost_per_million=8.0,
            currency="USD",
        )
        control.record_usage(
            agent_id="primary",
            role="implementer",
            model="model-a",
            metrics={"prompt_tokens": 1_000_000, "completion_tokens": 500_000},
        )
        summary = control.usage_summary()
        self.assertEqual(summary["provider"], "openai_compatible")
        self.assertEqual(summary["estimated_cost"], 6.0)
        self.assertEqual(summary["currency"], "USD")

    def test_cancel_closes_every_attached_response(self):
        control = register_chat_run("run_canceltest", "sess", "turn", "model", "chat")
        first = FakeResponse()
        second = FakeResponse()
        control.attach(first)
        control.attach(second)
        result = cancel_chat_run("run_canceltest")
        self.assertEqual(result["closed_responses"], 2)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        with self.assertRaises(ChatRunCancelled):
            control.raise_if_cancelled()
        release_chat_run("run_canceltest", control)
        self.assertIsNone(get_chat_run("run_canceltest"))

    def test_reusing_run_id_cancels_previous_control(self):
        previous = register_chat_run("run_reusetest", "sess", "turn", "model", "chat")
        current = register_chat_run("run_reusetest", "sess", "turn2", "model", "chat")
        self.assertTrue(previous.cancelled.is_set())
        self.assertIs(get_chat_run("run_reusetest"), current)
        release_chat_run("run_reusetest", current)

    def test_cancel_before_registration_is_honored(self):
        result = cancel_or_defer_chat_run("run_pendingtest")
        self.assertTrue(result["pending_registration"])
        control = register_chat_run("run_pendingtest", "sess", "turn", "model", "chat")
        self.assertTrue(control.cancelled.is_set())
        release_chat_run("run_pendingtest", control)

    def test_late_cancel_uses_recently_released_control(self):
        control = register_chat_run("run_latecancel", "sess", "turn", "model", "chat")
        release_chat_run("run_latecancel", control)
        self.assertIsNone(get_chat_run("run_latecancel"))
        result = cancel_chat_run("run_latecancel")
        self.assertTrue(result["cancelled"])
        self.assertTrue(control.cancelled.is_set())

    def test_active_models_exclude_cancelled_and_selected_run(self):
        first = register_chat_run("run_models_a", "sess", "turn", "model-a", "chat")
        second = register_chat_run("run_models_b", "sess", "turn", "model-b", "chat")
        second.track_model("critic-model")
        self.assertEqual(active_chat_models(exclude_run_id="run_models_a"), {"model-b", "critic-model"})
        second.cancel()
        self.assertEqual(active_chat_models(exclude_run_id="run_models_a"), set())
        release_chat_run("run_models_a", first)
        release_chat_run("run_models_b", second)

    def test_new_session_turn_can_supersede_only_that_sessions_active_run(self):
        old = register_chat_run("run_session_old", "session-a", "turn-a", "model-a", "chat")
        other = register_chat_run("run_session_other", "session-b", "turn-b", "model-b", "chat")
        cancelled = cancel_session_chat_runs("session-a")
        self.assertEqual(cancelled, ["run_session_old"])
        self.assertTrue(old.cancelled.is_set())
        self.assertFalse(other.cancelled.is_set())
        release_chat_run("run_session_old", old)
        release_chat_run("run_session_other", other)


if __name__ == "__main__":
    unittest.main()
