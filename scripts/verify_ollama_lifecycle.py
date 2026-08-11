"""Run a small, real-Ollama lifecycle matrix against production helpers.

This is intentionally separate from the versioned scenario quality suite. It verifies
model loading, same-model multi-Run retention, final-holder unload, user
cancellation, absolute deadline teardown, and a different-model slot switch.
The JSON evidence is runtime-only by default.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import app as workbench_app  # noqa: E402
from chat_cancellation import (  # noqa: E402
    ChatRunDeadlineExceeded,
    register_chat_run,
    release_chat_run,
)
from ollama_cleanup import loaded_models_snapshot  # noqa: E402


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def wait_loaded(ollama_url: str, model: str, expected: bool, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = loaded_models_snapshot(ollama_url)
        if snapshot is not None and (model in snapshot) is expected:
            return True
        time.sleep(0.1)
    return False


def create_control(model: str, label: str):
    suffix = uuid.uuid4().hex[:8]
    run_id = f"lifecycle-{label}-{suffix}"
    return register_chat_run(run_id, f"session-{suffix}", f"turn-{suffix}", model, "chat")


def run_minimal_call(
    *,
    settings: Dict[str, Any],
    model: str,
    control,
    num_predict: int = 1,
    prompt: str = "Reply with OK.",
) -> Dict[str, Any]:
    status, message, error = asyncio.run(
        workbench_app.scheduled_ollama_chat(
            settings["ollama_url"],
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "options": {"temperature": 0, "num_predict": num_predict},
            },
            control,
            agent_id=f"lifecycle-{control.run_id}",
            timeout=120,
            phase="generation",
        )
    )
    if status != 200:
        raise RuntimeError(f"Ollama HTTP {status}: {error[:300]}")
    return {
        "content": str(message.get("content") or ""),
        "metrics": dict(message.get("_metrics") or {}),
    }


def unload_if_present(settings: Dict[str, Any], model: str) -> Dict[str, Any]:
    return workbench_app.unload_model_now(settings, model)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary-model", default="gemma4:latest")
    parser.add_argument("--secondary-model", default="gemma4-hermes:latest")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    output = args.output or (
        ROOT
        / "runtime"
        / "evals"
        / "ollama-lifecycle"
        / datetime.now().strftime("%Y%m%d-%H%M%S")
        / "matrix.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    settings = {
        **workbench_app.load_settings(),
        "cancel_release_grace_seconds": 8.0,
        "cancel_release_poll_seconds": 0.2,
    }
    ollama_url = settings["ollama_url"]
    primary = args.primary_model
    secondary = args.secondary_model
    evidence: Dict[str, Any] = {
        "schema_version": 1,
        "started_at": now_iso(),
        "ollama_url": ollama_url,
        "primary_model": primary,
        "secondary_model": secondary,
        "cases": {},
    }
    controls = []

    workbench_app.GLOBAL_MODEL_HOLDERS.reset_for_tests()
    workbench_app.GLOBAL_MODEL_SLOT.reset_for_tests()
    try:
        unload_if_present(settings, primary)
        unload_if_present(settings, secondary)

        # Normal + two Runs using the same model.
        first = create_control(primary, "same-a")
        second = create_control(primary, "same-b")
        controls.extend([first, second])
        first_call = run_minimal_call(settings=settings, model=primary, control=first)
        second_call = run_minimal_call(settings=settings, model=primary, control=second)
        first_release = workbench_app.release_run_model_leases(
            first,
            settings,
            closing=True,
            reason="normal",
        )
        retained_after_first = wait_loaded(ollama_url, primary, True, timeout=5)
        second_release = workbench_app.release_run_model_leases(
            second,
            settings,
            closing=True,
            reason="normal",
        )
        unloaded_after_last = wait_loaded(ollama_url, primary, False, timeout=15)
        first_load_ms = round(
            int(first_call["metrics"].get("load_duration_ns") or 0) / 1_000_000,
            3,
        )
        second_load_ms = round(
            int(second_call["metrics"].get("load_duration_ns") or 0) / 1_000_000,
            3,
        )
        same_passed = all([
            first_load_ms > 0,
            second_load_ms < max(100.0, first_load_ms * 0.1),
            first_release[primary]["state"] == "retained_shared",
            retained_after_first,
            bool(second_release[primary].get("released")),
            unloaded_after_last,
        ])
        evidence["cases"]["normal_same_model"] = {
            "passed": same_passed,
            "first_load_ms": first_load_ms,
            "second_load_ms": second_load_ms,
            "first_release": first_release[primary],
            "second_release": second_release[primary],
            "retained_after_first_release": retained_after_first,
            "unloaded_after_last_release": unloaded_after_last,
        }

        # User cancellation while a real stream is active.
        cancel_control = create_control(primary, "cancel")
        controls.append(cancel_control)
        cancel_result: Dict[str, Any] = {}

        def cancel_worker() -> None:
            try:
                cancel_result["call"] = run_minimal_call(
                    settings=settings,
                    model=primary,
                    control=cancel_control,
                    num_predict=256,
                    prompt="Count upward one number per line until 256.",
                )
            except Exception as exc:
                cancel_result["exception"] = f"{type(exc).__name__}: {exc}"

        cancel_thread = threading.Thread(target=cancel_worker, daemon=True)
        cancel_thread.start()
        became_loaded = wait_loaded(ollama_url, primary, True, timeout=60)
        cancel_control.cancel()
        cancel_thread.join(timeout=30)
        cancel_release = workbench_app.release_run_model_leases(
            cancel_control,
            settings,
            closing=True,
            reason="cancel",
        )
        cancel_unloaded = wait_loaded(ollama_url, primary, False, timeout=15)
        cancel_passed = all([
            became_loaded,
            not cancel_thread.is_alive(),
            cancel_release[primary]["reason"] == "cancel",
            bool(cancel_release[primary].get("released")),
            cancel_unloaded,
        ])
        evidence["cases"]["cancel"] = {
            "passed": cancel_passed,
            "became_loaded": became_loaded,
            "stream_stopped": not cancel_thread.is_alive(),
            "worker_exception": cancel_result.get("exception", ""),
            "release": cancel_release[primary],
            "unloaded": cancel_unloaded,
        }

        # Absolute deadline uses the same final-holder release decision.
        deadline_control = create_control(primary, "deadline")
        controls.append(deadline_control)
        deadline_warm = run_minimal_call(
            settings=settings,
            model=primary,
            control=deadline_control,
        )
        deadline_control.start_deadline(0.5)
        deadline_exception = ""
        try:
            run_minimal_call(
                settings=settings,
                model=primary,
                control=deadline_control,
                num_predict=256,
                prompt="Write a long numbered list with 256 detailed entries.",
            )
        except ChatRunDeadlineExceeded as exc:
            deadline_exception = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            deadline_exception = f"unexpected {type(exc).__name__}: {exc}"
        deadline_release = workbench_app.release_run_model_leases(
            deadline_control,
            settings,
            closing=True,
            reason="deadline",
        )
        deadline_unloaded = wait_loaded(ollama_url, primary, False, timeout=15)
        deadline_passed = all([
            deadline_control.deadline_exceeded(),
            bool(deadline_exception),
            deadline_release[primary]["reason"] == "deadline",
            bool(deadline_release[primary].get("released")),
            deadline_unloaded,
        ])
        evidence["cases"]["deadline"] = {
            "passed": deadline_passed,
            "warm_load_ms": round(
                int(deadline_warm["metrics"].get("load_duration_ns") or 0) / 1_000_000,
                3,
            ),
            "exception": deadline_exception,
            "deadline": deadline_control.deadline_report(),
            "release": deadline_release[primary],
            "unloaded": deadline_unloaded,
        }

        # A different model must evict all idle holders once, then load itself.
        old_control = create_control(primary, "switch-old")
        new_control = create_control(secondary, "switch-new")
        controls.extend([old_control, new_control])
        old_call = run_minimal_call(settings=settings, model=primary, control=old_control)
        new_call = run_minimal_call(settings=settings, model=secondary, control=new_control)
        primary_evicted = wait_loaded(ollama_url, primary, False, timeout=10)
        secondary_loaded = wait_loaded(ollama_url, secondary, True, timeout=10)
        old_holders_cleared = not workbench_app.GLOBAL_MODEL_HOLDERS.holders(primary)
        new_release = workbench_app.release_run_model_leases(
            new_control,
            settings,
            closing=True,
            reason="normal",
        )
        secondary_unloaded = wait_loaded(ollama_url, secondary, False, timeout=15)
        switch_passed = all([
            int(old_call["metrics"].get("load_duration_ns") or 0) > 0,
            int(new_call["metrics"].get("load_duration_ns") or 0) > 0,
            primary_evicted,
            secondary_loaded,
            old_holders_cleared,
            bool(new_release[secondary].get("released")),
            secondary_unloaded,
        ])
        evidence["cases"]["different_model_switch"] = {
            "passed": switch_passed,
            "primary_load_ms": round(
                int(old_call["metrics"].get("load_duration_ns") or 0) / 1_000_000,
                3,
            ),
            "secondary_load_ms": round(
                int(new_call["metrics"].get("load_duration_ns") or 0) / 1_000_000,
                3,
            ),
            "primary_evicted": primary_evicted,
            "secondary_loaded": secondary_loaded,
            "old_holders_cleared": old_holders_cleared,
            "secondary_release": new_release[secondary],
            "secondary_unloaded": secondary_unloaded,
        }
    finally:
        for control in controls:
            try:
                workbench_app.release_run_model_leases(
                    control,
                    settings,
                    closing=True,
                    reason="matrix_cleanup",
                )
            except Exception:
                pass
            release_chat_run(control.run_id, control)
        for model in {primary, secondary}:
            try:
                unload_if_present(settings, model)
            except Exception:
                pass
        evidence["finished_at"] = now_iso()
        expected_cases = {
            "normal_same_model",
            "cancel",
            "deadline",
            "different_model_switch",
        }
        evidence["passed"] = set(evidence["cases"]) == expected_cases and all(
            bool(case.get("passed")) for case in evidence["cases"].values()
        )
        output.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(f"Evidence: {output}")

    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
