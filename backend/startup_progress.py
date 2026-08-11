import argparse
import json
import os
import statistics
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from paths import RUNTIME_ROOT


STARTUP_DIR = RUNTIME_ROOT / "startup"
STATUS_PATH = STARTUP_DIR / "status.json"
HISTORY_PATH = STARTUP_DIR / "history.json"
_LOCK = threading.RLock()


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _run_id() -> str:
    return str(os.environ.get("WORKBENCH_STARTUP_RUN_ID") or f"startup_{uuid.uuid4().hex}")


def _enabled() -> bool:
    return bool(os.environ.get("WORKBENCH_STARTUP_RUN_ID"))


def begin_startup(run_id: Optional[str] = None) -> Dict[str, Any]:
    now = time.time()
    state = {
        "run_id": run_id or _run_id(),
        "status": "running",
        "stage": "launcher",
        "message": "正在建立本機啟動環境。",
        "detail": "準備操作介面與背景服務",
        "progress_percent": 8,
        "current_documents": None,
        "total_documents": None,
        "started_at": now,
        "stage_started_at": now,
        "stage_durations": {},
        "updated_at": now,
    }
    with _LOCK:
        _write_json(STATUS_PATH, state)
    return state


def update_startup(
    stage: str,
    message: str,
    *,
    detail: str = "",
    progress_percent: int = 10,
    current_documents: Optional[int] = None,
    total_documents: Optional[int] = None,
) -> Dict[str, Any]:
    if not _enabled():
        return _read_json(STATUS_PATH, {})
    with _LOCK:
        state = _read_json(STATUS_PATH, {})
        expected_run = _run_id()
        if not state or state.get("run_id") != expected_run:
            state = begin_startup(expected_run)
        now = time.time()
        previous_stage = str(state.get("stage") or "")
        if previous_stage and previous_stage != stage:
            stage_started = float(state.get("stage_started_at") or state.get("started_at") or now)
            durations = dict(state.get("stage_durations") or {})
            durations[previous_stage] = round(max(0.0, now - stage_started), 3)
            state["stage_durations"] = durations
            state["stage_started_at"] = now
        state.update({
            "status": "running",
            "stage": stage,
            "message": message,
            "detail": detail,
            "progress_percent": max(int(state.get("progress_percent") or 0), min(99, int(progress_percent))),
            "current_documents": current_documents,
            "total_documents": total_documents,
            "updated_at": now,
        })
        _write_json(STATUS_PATH, state)
        return state


def _history_estimate(history: list) -> Optional[float]:
    values = [float(item.get("total_seconds")) for item in history[-20:] if float(item.get("total_seconds") or 0) > 0]
    return round(statistics.median(values), 1) if values else None


def complete_startup() -> Dict[str, Any]:
    if not _enabled():
        return _read_json(STATUS_PATH, {})
    with _LOCK:
        state = _read_json(STATUS_PATH, {}) or begin_startup()
        now = time.time()
        previous_stage = str(state.get("stage") or "")
        durations = dict(state.get("stage_durations") or {})
        if previous_stage:
            durations[previous_stage] = round(max(0.0, now - float(state.get("stage_started_at") or now)), 3)
        total_seconds = round(max(0.0, now - float(state.get("started_at") or now)), 3)
        state.update({
            "status": "ready",
            "stage": "ready",
            "message": "啟動完成，正在進入工作區。",
            "detail": "後端、索引與工作區已就緒",
            "progress_percent": 100,
            "stage_durations": durations,
            "total_seconds": total_seconds,
            "updated_at": now,
        })
        history = _read_json(HISTORY_PATH, [])
        if not isinstance(history, list):
            history = []
        if os.environ.get("WORKBENCH_STARTUP_RECORD_HISTORY", "1") != "0":
            history.append({
                "completed_at": now,
                "total_seconds": total_seconds,
                "stage_durations": durations,
            })
            history = history[-20:]
            _write_json(HISTORY_PATH, history)
        state["history_samples"] = len(history)
        state["estimated_total_seconds"] = _history_estimate(history)
        _write_json(STATUS_PATH, state)
        return state


def fail_startup(message: str) -> Dict[str, Any]:
    if not _enabled():
        return _read_json(STATUS_PATH, {})
    with _LOCK:
        state = _read_json(STATUS_PATH, {}) or begin_startup()
        state.update({
            "status": "failed",
            "stage": "failed",
            "message": "啟動未完成。",
            "detail": str(message)[:500],
            "updated_at": time.time(),
        })
        _write_json(STATUS_PATH, state)
        return state


def read_startup_status() -> Dict[str, Any]:
    state = _read_json(STATUS_PATH, {})
    history = _read_json(HISTORY_PATH, [])
    if not state:
        return {"status": "waiting", "stage": "launcher", "message": "等待啟動器回報狀態。", "progress_percent": 5}
    now = time.time()
    elapsed = max(0.0, now - float(state.get("started_at") or now))
    estimate = _history_estimate(history if isinstance(history, list) else [])
    return {
        **state,
        "elapsed_seconds": round(elapsed, 1),
        "estimated_total_seconds": estimate,
        "eta_seconds": round(max(0.0, estimate - elapsed), 1) if estimate is not None and state.get("status") == "running" else None,
        "history_samples": len(history) if isinstance(history, list) else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("begin", "fail", "read"))
    parser.add_argument("--message", default="")
    args = parser.parse_args()
    if args.action == "begin":
        payload = begin_startup()
    elif args.action == "fail":
        payload = fail_startup(args.message)
    else:
        payload = read_startup_status()
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
