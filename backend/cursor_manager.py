import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

from workspace import WorkspaceContext
from subprocess_env import agent_subprocess_env


MAX_OUTPUT_CHARS = 200_000


def cursor_executable() -> Optional[str]:
    return shutil.which("cursor-agent")


def _run_metadata(executable: str, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
            env=agent_subprocess_env(),
        )
        value = (result.stdout or result.stderr).strip()
        return value.splitlines()[0] if value else None
    except Exception:
        return None


def status() -> Dict[str, Any]:
    executable = cursor_executable()
    return {
        "installed": bool(executable),
        "executable": executable,
        "version": _run_metadata(executable, "--version") if executable else None,
        "authenticated": None,
        "message": (
            "已偵測到 Cursor Agent CLI；帳戶狀態會在建立任務後由 CLI 回報。"
            if executable
            else "尚未偵測到 Cursor Agent CLI。Workbench 不會自動安裝或登入 Cursor。"
        ),
    }


def _parse_output(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        parsed = []
        for line in raw.splitlines():
            try:
                parsed.append(json.loads(line))
            except ValueError:
                continue
        return parsed or None


def run_cursor_task(
    prompt: str,
    context: WorkspaceContext,
    *,
    write: bool = False,
    model: Optional[str] = None,
    resume: Optional[str] = None,
    timeout_seconds: int = 900,
) -> Dict[str, Any]:
    executable = cursor_executable()
    if not executable:
        raise RuntimeError("找不到 cursor-agent，請先安裝並完成 Cursor CLI 登入。")
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("Cursor 任務必須提供 prompt。")
    if context.path_status not in {"ready", "read_only"}:
        raise PermissionError(f"專案路徑無法使用：{context.path_status}")
    if write and context.permission_mode != "workspace_write":
        raise PermissionError("專案沒有 Cursor 寫入權限。")
    argv = [executable, "-p", "--output-format", "json"]
    if write:
        argv.append("--force")
    if model:
        argv.extend(["--model", str(model)])
    if resume:
        argv.extend(["--resume", str(resume)])
    argv.append(prompt)
    started = time.monotonic()
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            argv,
            cwd=str(context.working_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(30, min(int(timeout_seconds), 1800)),
            shell=False,
            env=agent_subprocess_env({"PYTHONUTF8": "1"}),
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "exit_code": None,
            "stdout": (exc.stdout or "")[:MAX_OUTPUT_CHARS] if isinstance(exc.stdout, str) else "",
            "stderr": "Cursor CLI 執行逾時。",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "write": write,
        }
    stdout = (completed.stdout or "")[:MAX_OUTPUT_CHARS]
    stderr = (completed.stderr or "")[:MAX_OUTPUT_CHARS]
    return {
        "success": completed.returncode == 0,
        "exit_code": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "parsed": _parse_output(stdout),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "write": write,
        "truncated": len(completed.stdout or "") > MAX_OUTPUT_CHARS or len(completed.stderr or "") > MAX_OUTPUT_CHARS,
    }
