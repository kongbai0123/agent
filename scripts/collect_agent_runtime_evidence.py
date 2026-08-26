"""Collect canonical Agent capability evidence from real Basic Chat Runs.

The deterministic smoke runner proves the evaluation contract itself.  This
collector is the production adapter: it opens the Workbench SQLite database in
read-only mode, verifies that every selected Run executed the exact suite
prompt under the captured runtime/model identity, and projects only bounded,
redacted execution facts into ``workbench-agent-run-evidence/v1``.

Collection is intentionally two-stage.  ``--init-selection`` captures the Git
tree and Runtime digest *before* evaluation Runs are executed.  The operator (or
future UI) then fills each ``run_id`` and invokes ``--selection``.  Collection
fails closed if code changed, a Run predates the capture, the prompt/model does
not match, the final assistant message is missing, or project knowledge crosses
scope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import database as workbench_database  # noqa: E402
from evaluate_agent_capabilities import (  # noqa: E402
    DEFAULT_GATE,
    DEFAULT_SUITE,
    ContractError,
    _find_secret_paths,
    canonical_digest,
    load_json,
    validate_gate,
    validate_suite,
)
from export_agent_capability_results import (  # noqa: E402
    EVIDENCE_SCHEMA,
    evidence_digest,
)


SELECTION_SCHEMA = "workbench-agent-runtime-selection/v1"
COLLECTOR_SOURCE = "workbench_basic_chat_runtime_sqlite_v1"
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_UNTRACKED_FILES = 4096
_MAX_UNTRACKED_FILE_BYTES = 16 * 1024 * 1024
_MAX_UNTRACKED_TOTAL_BYTES = 128 * 1024 * 1024
_LOCAL_DATA_PREFIXES = (
    "archive/",
    "artifacts/",
    "backend/data/",
    "data/",
    "projects/",
    "runtime/",
    "workspaces/",
)
_SENSITIVE_DATA_SUFFIXES = frozenset(
    {"", ".cfg", ".conf", ".ini", ".json", ".toml", ".txt", ".yaml", ".yml"}
)
_SENSITIVE_NAME_PARTS = frozenset(
    {
        "api_key",
        "apikey",
        "credential",
        "credentials",
        "private_key",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)
_SELECTION_FIELDS = {
    "schema_version",
    "suite_id",
    "suite_digest",
    "gate_digest",
    "subject",
    "capture_started_at",
    "git_commit",
    "git_digest",
    "git_dirty",
    "runtime",
    "model",
    "config",
    "policy",
    "trial",
    "runs",
    "capture_digest",
}
_RUNTIME_FILES = (
    "backend/app.py",
    "backend/chat/runtime.py",
    "backend/database.py",
    "backend/factual_verifier.py",
    "backend/host_tools.py",
    "backend/tool_runtime.py",
    "backend/tool_approval_broker.py",
    "backend/task_planner.py",
    "backend/project_knowledge.py",
    "backend/semantic_retrieval.py",
    "backend/model_governance.py",
    "scripts/collect_agent_runtime_evidence.py",
    "scripts/export_agent_capability_results.py",
    "scripts/evaluate_agent_capabilities.py",
)


class RuntimeEvidenceError(ValueError):
    """Raised when persisted Runtime evidence cannot be proven trustworthy."""


@dataclass(frozen=True)
class RuntimeEnvironment:
    git_commit: str
    git_digest: str
    git_dirty: bool
    runtime_digest: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any, location: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeEvidenceError(f"{location} 必須是 ISO-8601 時間") from exc
    if parsed.tzinfo is None:
        raise RuntimeEvidenceError(f"{location} 必須包含時區")
    return parsed.astimezone(timezone.utc)


def _json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _untracked_content_policy(relative: str) -> str:
    """Choose whether an untracked path is safe and relevant to hash.

    Git ignore rules are the primary boundary.  These checks are defense in
    depth for a damaged or locally-edited ignore file: mutable user Runtime
    state is irrelevant to source provenance, while a likely credential file
    must stop capture before any of its bytes are read.
    """

    normalized = relative.replace("\\", "/").lstrip("./")
    folded = normalized.casefold()
    if any(folded.startswith(prefix) for prefix in _LOCAL_DATA_PREFIXES):
        return "skip"
    name = Path(normalized).name.casefold()
    if (
        (name.startswith(".env") and name != ".env.example")
        or name == "settings.json"
        or Path(name).suffix in {".key", ".p12", ".pfx", ".pem"}
    ):
        return "reject"
    stem_parts = {
        part
        for part in re.split(r"[^a-z0-9]+", Path(name).stem)
        if part
    }
    joined_stem = "_".join(part for part in re.split(r"[^a-z0-9]+", Path(name).stem) if part)
    if Path(name).suffix in _SENSITIVE_DATA_SUFFIXES and (
        stem_parts & _SENSITIVE_NAME_PARTS
        or joined_stem in _SENSITIVE_NAME_PARTS
    ):
        return "reject"
    return "hash"


def _untracked_content_digests(raw_names: bytes) -> Dict[str, str]:
    encoded_names = sorted(item for item in raw_names.split(b"\0") if item)
    if len(encoded_names) > _MAX_UNTRACKED_FILES:
        raise RuntimeEvidenceError(
            "未追蹤檔案過多，請先提交、忽略或移出與評測無關的檔案。"
        )
    result: Dict[str, str] = {}
    total_bytes = 0
    for encoded_name in encoded_names:
        relative = encoded_name.decode("utf-8", errors="surrogateescape")
        normalized = relative.replace("\\", "/")
        if (
            not normalized
            or normalized.startswith("/")
            or re.match(r"^[A-Za-z]:/", normalized)
            or any(part in {"", ".", ".."} for part in normalized.split("/"))
        ):
            raise RuntimeEvidenceError("Git 回傳了無效的未追蹤檔案路徑。")
        policy = _untracked_content_policy(relative)
        if policy == "skip":
            continue
        if policy == "reject":
            raise RuntimeEvidenceError(
                "偵測到疑似憑證或秘密的未追蹤檔案；請先將它移出版本樹或加入忽略規則。"
            )
        candidate = REPO_ROOT / relative
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise RuntimeEvidenceError("未追蹤檔案在來源鎖定期間發生變更。") from exc
        attributes = int(getattr(info, "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if not stat.S_ISREG(info.st_mode) or attributes & reparse_flag:
            raise RuntimeEvidenceError(
                "未追蹤來源必須是一般檔案，不可使用連結或重新解析點。"
            )
        size = int(info.st_size)
        if size < 0 or size > _MAX_UNTRACKED_FILE_BYTES:
            raise RuntimeEvidenceError(
                "未追蹤檔案超過來源證明大小上限；請先提交、忽略或移出該檔案。"
            )
        total_bytes += size
        if total_bytes > _MAX_UNTRACKED_TOTAL_BYTES:
            raise RuntimeEvidenceError(
                "未追蹤檔案總量超過來源證明大小上限；請先整理工作樹。"
            )
        digest = hashlib.sha256()
        try:
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise RuntimeEvidenceError("未追蹤檔案在來源鎖定期間發生變更。") from exc
        result[normalized] = digest.hexdigest()
    return result


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
        ).stdout.replace("\r\n", "\n")
        diff = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--binary", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
        untracked_output = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
        untracked = _untracked_content_digests(untracked_output)
    except RuntimeEvidenceError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        # Formal evidence cannot safely fall back to a stable "unavailable"
        # digest: doing so would let two different worktrees compare equal.
        raise RuntimeEvidenceError("無法取得 Git 工作樹來源證明。") from exc
    return (
        commit,
        canonical_digest(
            {
                "commit": commit,
                "status": status,
                "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
                # Git's porcelain status records only the name of an untracked
                # file.  Hash its bytes as well so a two-stage capture cannot
                # silently mix different versions of a newly-added Runtime file.
                "untracked_files": untracked,
            }
        ),
        bool(status.strip()),
    )


def _runtime_digest() -> str:
    files: Dict[str, str] = {}
    for relative in _RUNTIME_FILES:
        path = REPO_ROOT / relative
        if not path.is_file():
            raise RuntimeEvidenceError(f"Runtime digest 缺少檔案：{relative}")
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return canonical_digest(files)


def current_environment() -> RuntimeEnvironment:
    commit, digest, dirty = _git_provenance()
    return RuntimeEnvironment(commit, digest, dirty, _runtime_digest())


def _selection_authority(selection: Mapping[str, Any]) -> Dict[str, Any]:
    """Return capture-time fields; run IDs are deliberately filled later."""

    return {
        key: selection.get(key)
        for key in sorted(_SELECTION_FIELDS - {"runs", "capture_digest"})
    }


def build_selection(
    suite: Mapping[str, Any],
    gate: Mapping[str, Any],
    *,
    subject_id: str,
    subject_version: str,
    model_id: str,
    model_version: str,
    runtime_id: str = "local-ai-workbench-basic-chat",
    runtime_version: str = "1",
    config: Optional[Mapping[str, Any]] = None,
    policy: Optional[Mapping[str, Any]] = None,
    trial: int = 1,
    environment: Optional[RuntimeEnvironment] = None,
    capture_started_at: Optional[str] = None,
    task_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Create a capture manifest before formal evaluation Runs begin."""

    contract_errors = validate_suite(suite) + validate_gate(gate, suite)
    if contract_errors:
        raise RuntimeEvidenceError("；".join(contract_errors))
    if trial < 1:
        raise RuntimeEvidenceError("trial 必須是正整數")
    required_strings = {
        "subject_id": subject_id,
        "subject_version": subject_version,
        "model_id": model_id,
        "model_version": model_version,
        "runtime_id": runtime_id,
        "runtime_version": runtime_version,
    }
    for name, value in required_strings.items():
        if not isinstance(value, str) or not value.strip():
            raise RuntimeEvidenceError(f"{name} 必須是非空字串")
    env = environment or current_environment()
    captured = capture_started_at or _now_iso()
    _parse_iso(captured, "capture_started_at")
    safe_config = dict(config or {})
    safe_policy = dict(policy or {})
    if _find_secret_paths(
        {"config": safe_config, "policy": safe_policy}, path="selection"
    ):
        raise RuntimeEvidenceError("config／policy 不得包含秘密或憑證")
    wanted = set(task_ids or ())
    known = {str(task["id"]) for task in suite["tasks"]}
    if wanted and not wanted <= known:
        raise RuntimeEvidenceError("task_ids 包含 suite 未定義的任務")
    model = {"id": model_id.strip(), "version": model_version.strip()}
    selection: Dict[str, Any] = {
        "schema_version": SELECTION_SCHEMA,
        "suite_id": suite["suite_id"],
        "suite_digest": canonical_digest(suite),
        "gate_digest": canonical_digest(gate),
        "subject": {"id": subject_id.strip(), "version": subject_version.strip()},
        "capture_started_at": captured,
        "git_commit": env.git_commit,
        "git_digest": env.git_digest,
        "git_dirty": env.git_dirty,
        "runtime": {
            "id": runtime_id.strip(),
            "version": runtime_version.strip(),
            "digest": env.runtime_digest,
        },
        "model": {**model, "digest": canonical_digest(model)},
        "config": safe_config,
        "policy": safe_policy,
        "trial": trial,
        "runs": [
            {"task_id": str(task["id"]), "run_id": ""}
            for task in suite["tasks"]
            if not wanted or str(task["id"]) in wanted
        ],
        "capture_digest": "sha256:" + "0" * 64,
    }
    selection["capture_digest"] = canonical_digest(_selection_authority(selection))
    return selection


def _validate_selection(
    selection: Any,
    suite: Mapping[str, Any],
    gate: Mapping[str, Any],
    environment: RuntimeEnvironment,
) -> None:
    if not isinstance(selection, dict):
        raise RuntimeEvidenceError("selection 必須是物件")
    missing = sorted(_SELECTION_FIELDS - set(selection))
    extras = sorted(set(selection) - _SELECTION_FIELDS)
    if missing or extras:
        raise RuntimeEvidenceError(
            "selection 欄位不符："
            + (f"缺少 {', '.join(missing)}" if missing else "")
            + (f"；未知 {', '.join(extras)}" if extras else "")
        )
    if selection.get("schema_version") != SELECTION_SCHEMA:
        raise RuntimeEvidenceError(f"selection schema 必須是 {SELECTION_SCHEMA}")
    if selection.get("suite_id") != suite.get("suite_id"):
        raise RuntimeEvidenceError("selection suite_id 與 suite 不一致")
    if selection.get("suite_digest") != canonical_digest(suite):
        raise RuntimeEvidenceError("selection suite digest 與目前 suite 不一致")
    if selection.get("gate_digest") != canonical_digest(gate):
        raise RuntimeEvidenceError("selection gate digest 與目前 gate 不一致")
    if selection.get("capture_digest") != canonical_digest(
        _selection_authority(selection)
    ):
        raise RuntimeEvidenceError("selection capture digest 不一致")
    if selection.get("git_commit") != environment.git_commit:
        raise RuntimeEvidenceError("評估期間 Git commit 已改變")
    if selection.get("git_digest") != environment.git_digest:
        raise RuntimeEvidenceError("評估期間 Git 工作樹已改變")
    if selection.get("git_dirty") is not environment.git_dirty:
        raise RuntimeEvidenceError("評估期間 Git dirty 狀態已改變")
    runtime = selection.get("runtime")
    model = selection.get("model")
    subject = selection.get("subject")
    if not isinstance(subject, dict) or set(subject) != {"id", "version"}:
        raise RuntimeEvidenceError("selection.subject 格式無效")
    if not isinstance(runtime, dict) or set(runtime) != {"id", "version", "digest"}:
        raise RuntimeEvidenceError("selection.runtime 格式無效")
    if runtime.get("digest") != environment.runtime_digest:
        raise RuntimeEvidenceError("評估期間 Runtime digest 已改變")
    if not isinstance(model, dict) or set(model) != {"id", "version", "digest"}:
        raise RuntimeEvidenceError("selection.model 格式無效")
    if model.get("digest") != canonical_digest(
        {"id": model.get("id"), "version": model.get("version")}
    ):
        raise RuntimeEvidenceError("selection.model digest 不一致")
    if not all(
        isinstance(item.get(key), str) and item.get(key)
        for item in (subject, runtime, model)
        if isinstance(item, dict)
        for key in ("id", "version")
    ):
        raise RuntimeEvidenceError("subject／runtime／model 必須提供 id 與 version")
    trial = selection.get("trial")
    if not isinstance(trial, int) or isinstance(trial, bool) or trial < 1:
        raise RuntimeEvidenceError("selection.trial 必須是正整數")
    _parse_iso(selection.get("capture_started_at"), "selection.capture_started_at")
    secret_paths = _find_secret_paths(selection, path="selection")
    if secret_paths:
        raise RuntimeEvidenceError(
            "selection 含秘密或憑證：" + ", ".join(secret_paths[:5])
        )
    known = {str(task["id"]) for task in suite["tasks"]}
    mappings = selection.get("runs")
    if not isinstance(mappings, list) or not mappings:
        raise RuntimeEvidenceError("selection.runs 必須是非空陣列")
    seen_tasks: set[str] = set()
    seen_runs: set[str] = set()
    for item in mappings:
        if not isinstance(item, dict) or set(item) != {"task_id", "run_id"}:
            raise RuntimeEvidenceError("selection.runs[] 只能包含 task_id 與 run_id")
        task_id = str(item.get("task_id") or "")
        run_id = str(item.get("run_id") or "")
        if task_id not in known or task_id in seen_tasks:
            raise RuntimeEvidenceError(f"selection task_id 未知或重複：{task_id!r}")
        if not run_id or run_id in seen_runs:
            raise RuntimeEvidenceError(f"selection run_id 必須唯一且非空：{run_id!r}")
        seen_tasks.add(task_id)
        seen_runs.add(run_id)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.resolve(strict=True)
    uri = resolved.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("BEGIN")
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _approval_rows(
    connection: sqlite3.Connection, run_id: str
) -> List[Dict[str, Any]]:
    if "tool_approval_bindings" not in _tables(connection):
        return []
    rows = connection.execute(
        """
        SELECT approval_id, run_id, project_id, call_id, tool_name,
               arguments_sha256, summary_json, status, created_at,
               decided_at, consumed_at
        FROM tool_approval_bindings
        WHERE run_id = ?
        ORDER BY created_at, approval_id
        """,
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _source_id(source: Mapping[str, Any]) -> str:
    document = str(source.get("document_id") or "").strip()
    chunk = str(source.get("chunk_id") or "").strip()
    return f"{document}:{chunk}" if document and chunk else ""


class _CanonicalRecorder:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.events: List[Dict[str, Any]] = []

    def append(self, event: str, payload: Optional[Mapping[str, Any]] = None) -> None:
        self.events.append(
            {
                "run_id": self.run_id,
                "sequence": len(self.events),
                "event": event,
                "payload": dict(payload or {}),
            }
        )


def _safe_digest(value: Any) -> Optional[str]:
    digest = str(value or "").strip().casefold()
    if _HEX_SHA256.fullmatch(digest):
        return "sha256:" + digest
    if _SHA256.fullmatch(digest):
        return digest
    return None


def _collect_one_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    task: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> Dict[str, Any]:
    row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise RuntimeEvidenceError(f"找不到 Runtime Run：{run_id}")
    run = dict(row)
    run_status = str(run.get("status") or "")
    if run_status not in {"completed", "failed", "cancelled"} or run.get("mode") != "chat":
        raise RuntimeEvidenceError(f"{run_id} 不是終端 Basic Chat Run")
    if str(run.get("model") or "") != str(selection["model"]["id"]):
        raise RuntimeEvidenceError(f"{run_id} 使用的模型與 selection 不一致")
    if not run.get("completed_at"):
        raise RuntimeEvidenceError(f"{run_id} 缺少 completed_at")
    started = _parse_iso(run.get("created_at"), f"{run_id}.created_at")
    completed = _parse_iso(run.get("completed_at"), f"{run_id}.completed_at")
    capture_started = _parse_iso(
        selection.get("capture_started_at"), "selection.capture_started_at"
    )
    if started < capture_started or completed < started:
        raise RuntimeEvidenceError(f"{run_id} 不屬於這次 capture 時窗")

    private_manifest = _json(run.get("input_manifest_json"), {})
    if not isinstance(private_manifest, dict) or int(private_manifest.get("version") or 0) < 2:
        raise RuntimeEvidenceError(f"{run_id} 缺少 Basic Chat v2 input manifest")
    prompt = str(task["prompt"]).strip()
    if str(private_manifest.get("user_message") or "").strip() != prompt:
        raise RuntimeEvidenceError(f"{run_id} 的實際 prompt 與 suite 不一致")
    raw_prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    if str(private_manifest.get("prompt_sha256") or "").casefold() != raw_prompt_sha:
        raise RuntimeEvidenceError(f"{run_id} 的 Runtime prompt digest 不一致")
    if private_manifest.get("project_id") != run.get("project_id"):
        raise RuntimeEvidenceError(f"{run_id} 的 project 綁定不一致")

    user_message_id = private_manifest.get("user_message_id")
    if not isinstance(user_message_id, int) or isinstance(user_message_id, bool):
        raise RuntimeEvidenceError(f"{run_id} 缺少 user_message_id")
    user_row = connection.execute(
        "SELECT * FROM messages WHERE id = ?", (user_message_id,)
    ).fetchone()
    if (
        user_row is None
        or user_row["role"] != "user"
        or user_row["session_id"] != run["session_id"]
        or str(user_row["content"] or "").strip() != prompt
    ):
        raise RuntimeEvidenceError(f"{run_id} 的持久化使用者訊息綁定不一致")
    assistant = connection.execute(
        """
        SELECT * FROM messages
        WHERE session_id = ? AND role = 'assistant' AND turn_id = ?
              AND parent_message_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (run["session_id"], run["turn_id"], user_message_id),
    ).fetchone()
    if run_status == "completed" and (
        assistant is None or not str(assistant["content"] or "").strip()
    ):
        raise RuntimeEvidenceError(f"{run_id} 缺少已持久化的最終回答")

    raw_events = _json(run.get("events_json"), [])
    public_events = workbench_database.public_run_events(
        raw_events,
        run_id=run_id,
        session_id=str(run["session_id"]),
        project_id=run.get("project_id"),
    )
    approvals = _approval_rows(connection, run_id)
    approval_by_id = {str(item["approval_id"]): item for item in approvals}
    approval_by_call = {str(item["call_id"]): item for item in approvals}
    allowed_projects = {str(run.get("project_id") or ""), "__independent_chat__"}
    for approval in approvals:
        if str(approval.get("project_id") or "") not in allowed_projects:
            raise RuntimeEvidenceError(f"{run_id} 的批准紀錄跨越專案範圍")
        if not _safe_digest(approval.get("arguments_sha256")):
            raise RuntimeEvidenceError(f"{run_id} 的批准參數 digest 無效")

    sources = _json(run.get("sources_json"), [])
    knowledge_sources = [
        item
        for item in sources
        if isinstance(item, dict) and item.get("kind") == "project_knowledge"
    ]
    source_ids: List[str] = []
    for source in knowledge_sources:
        if str(source.get("project_id") or "") != str(run.get("project_id") or ""):
            raise RuntimeEvidenceError(f"{run_id} 的知識來源跨越專案範圍")
        identifier = _source_id(source)
        if not identifier:
            raise RuntimeEvidenceError(f"{run_id} 的知識來源缺少 document/chunk ID")
        source_ids.append(identifier)
    source_ids = list(dict.fromkeys(source_ids))
    private_sources = private_manifest.get("knowledge_sources")
    if source_ids:
        if private_manifest.get("knowledge_used") is not True:
            raise RuntimeEvidenceError(f"{run_id} 的 RAG 來源缺少 knowledge_used 綁定")
        private_ids = {
            _source_id(item)
            for item in (private_sources or [])
            if isinstance(item, Mapping)
        }
        if set(source_ids) != private_ids:
            raise RuntimeEvidenceError(f"{run_id} 的私有與公開 RAG 來源不一致")
        if assistant is not None:
            assistant_sources = _json(assistant["sources_json"], [])
            assistant_source_ids = {
                _source_id(item)
                for item in assistant_sources
                if isinstance(item, Mapping)
                and item.get("kind") == "project_knowledge"
            }
            if set(source_ids) != assistant_source_ids:
                raise RuntimeEvidenceError(f"{run_id} 的最終回答未綁定相同 RAG 引用")

    recorder = _CanonicalRecorder(run_id)
    if source_ids:
        rag_call = "host-rag-" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
        recorder.append(
            "tool_start", {"tool": "knowledge.retrieve", "call_id": rag_call}
        )
        recorder.append(
            "tool_end",
            {
                "tool": "knowledge.retrieve",
                "call_id": rag_call,
                "outcome": "success",
                "source_ids": source_ids,
                "resource_scope": "project",
                "cross_project": False,
                "scope_check": "passed",
            },
        )

    artifact_ids = [
        str(item.get("artifact_id") or item.get("id") or "")
        for item in _json(run.get("artifacts_json"), [])
        if isinstance(item, Mapping)
    ]
    artifact_id = next((item for item in artifact_ids if item), "")
    plan_steps: Dict[str, Dict[str, Any]] = {}
    for event in public_events:
        if event.get("event") == "plan":
            for item in event.get("payload", {}).get("tasks") or []:
                if isinstance(item, Mapping) and item.get("id"):
                    plan_steps[str(item["id"])] = {"id": str(item["id"]), "tool_budget": 0}
        if event.get("event") == "task_update":
            payload = event.get("payload") or {}
            step_id = str(payload.get("task_id") or "")
            if step_id:
                step = plan_steps.setdefault(step_id, {"id": step_id, "tool_budget": 0})
                limit = payload.get("tool_call_limit")
                if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0:
                    step["tool_budget"] = limit

    emitted_approvals: set[str] = set()
    active_step = ""
    plan_id = ""
    repair_rounds: Dict[str, int] = {}
    execution_unknown = False

    def emit_approval(approval: Mapping[str, Any]) -> None:
        approval_id = str(approval.get("approval_id") or "")
        if not approval_id or approval_id in emitted_approvals:
            return
        emitted_approvals.add(approval_id)
        digest = _safe_digest(approval.get("arguments_sha256"))
        summary = _json(approval.get("summary_json"), {})
        risk = str(summary.get("risk_level") or "external_write")
        tool = str(approval.get("tool_name") or "")
        recorder.append(
            "approval_required",
            {
                "tool": tool,
                "arguments_digest": digest,
                "risk": risk,
                "remember_allowed": False,
            },
        )
        status = str(approval.get("status") or "")
        if status == "consumed" and approval.get("consumed_at"):
            recorder.append(
                "approval_consumed",
                {"tool": tool, "arguments_digest": digest},
            )
        elif status in {"denied", "expired"}:
            recorder.append(
                "approval_rejected",
                {
                    "tool": tool,
                    "arguments_digest": digest,
                    "reason": "user_denied" if status == "denied" else "expired",
                },
            )

    for event in public_events:
        name = str(event.get("event") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        if name == "plan":
            plan_id = str(payload.get("plan_id") or "")
            recorder.append("plan_created", {"steps": list(plan_steps.values())})
        elif name == "task_update":
            step_id = str(payload.get("task_id") or "")
            status = str(payload.get("status") or "")
            if status in {"running", "in_progress"}:
                active_step = step_id
                recorder.append("plan_step_started", {"step_id": step_id})
            elif status == "completed":
                recorder.append("plan_step_completed", {"step_id": step_id})
                if active_step == step_id:
                    active_step = ""
            elif status == "failed":
                recorder.append("plan_step_failed", {"step_id": step_id})
                if active_step == step_id:
                    active_step = ""
            elif status == "skipped":
                recorder.append(
                    "plan_step_skipped",
                    {"step_id": step_id, "reason": "dependency_failed"},
                )
        elif name == "repair":
            step_id = str(payload.get("task_id") or active_step)
            repair_rounds[step_id] = max(
                repair_rounds.get(step_id, 0), int(payload.get("round") or 0)
            )
        elif name == "approval_required":
            approval = approval_by_id.get(str(payload.get("approval_id") or ""))
            if approval is not None:
                emit_approval(approval)
        elif name == "tool_start":
            call_id = str(payload.get("tool_call_id") or "")
            approval = approval_by_call.get(call_id)
            if approval is not None:
                emit_approval(approval)
            projected: Dict[str, Any] = {
                "tool": str(payload.get("tool") or ""),
                "call_id": call_id,
            }
            if approval is not None:
                projected["arguments_digest"] = _safe_digest(
                    approval.get("arguments_sha256")
                )
            if active_step:
                projected["strategy_id"] = (
                    f"{plan_id}:{active_step}:repair-{repair_rounds.get(active_step, 0)}"
                )
            recorder.append("tool_start", projected)
        elif name == "tool_end":
            tool = str(payload.get("tool") or "")
            result = str(payload.get("result") or "").casefold()
            if "tool_skipped_after_execution_unknown" in result:
                recorder.append(
                    "tool_skipped", {"tool": tool, "reason": "execution_unknown"}
                )
                continue
            if "execution_unknown" in result:
                outcome = "execution_unknown"
                execution_unknown = True
            else:
                outcome = "success" if payload.get("success") is True else "failed"
            recorder.append(
                "tool_end",
                {
                    "tool": tool,
                    "call_id": str(payload.get("tool_call_id") or ""),
                    "outcome": outcome,
                },
            )
        elif name == "validation":
            recorder.append("verification_started", {})
            passed = payload.get("passed") is True and payload.get("status") == "passed"
            validation: Dict[str, Any] = {}
            if passed and artifact_id:
                validation["artifact_id"] = artifact_id
            recorder.append(
                "verification_passed" if passed else "verification_failed",
                validation,
            )

    for approval in approvals:
        emit_approval(approval)

    if assistant is not None and str(assistant["content"] or "").strip():
        final: Dict[str, Any] = {
            "text_digest": canonical_digest(str(assistant["content"])),
        }
        if source_ids:
            final["citations"] = source_ids
        if execution_unknown:
            final["action_required"] = "verify_externally"
        recorder.append("response_final", final)
    return {
        "run_id": run_id,
        "status": run_status,
        "input_manifest": {
            "version": 1,
            "suite_id": selection["suite_id"],
            "task_id": task["id"],
            "prompt_sha256": canonical_digest(task["prompt"]),
            "config_digest": canonical_digest(selection["config"]),
            "policy_digest": canonical_digest(selection["policy"]),
            "trial": selection["trial"],
        },
        "events": recorder.events,
    }


def collect_runtime_evidence(
    database_path: Path,
    selection: Mapping[str, Any],
    suite: Mapping[str, Any],
    gate: Mapping[str, Any],
    *,
    environment: Optional[RuntimeEnvironment] = None,
) -> Dict[str, Any]:
    """Collect one immutable, exporter-compatible snapshot from real Runs."""

    contract_errors = validate_suite(suite) + validate_gate(gate, suite)
    if contract_errors:
        raise RuntimeEvidenceError("；".join(contract_errors))
    env = environment or current_environment()
    _validate_selection(selection, suite, gate, env)
    tasks = {str(task["id"]): task for task in suite["tasks"]}
    connection = _readonly_connection(database_path)
    try:
        required_tables = {"runs", "messages"}
        missing = required_tables - _tables(connection)
        if missing:
            raise RuntimeEvidenceError(
                "Workbench database 缺少資料表：" + ", ".join(sorted(missing))
            )
        runs = [
            _collect_one_run(
                connection,
                run_id=str(mapping["run_id"]),
                task=tasks[str(mapping["task_id"])],
                selection=selection,
            )
            for mapping in selection["runs"]
        ]
    finally:
        connection.rollback()
        connection.close()
    provenance = {
        "source": COLLECTOR_SOURCE,
        "git_commit": env.git_commit,
        "git_digest": env.git_digest,
        "git_dirty": env.git_dirty,
        "runtime_id": selection["runtime"]["id"],
        "runtime_version": selection["runtime"]["version"],
        "runtime_digest": env.runtime_digest,
        "model_id": selection["model"]["id"],
        "model_version": selection["model"]["version"],
        "model_digest": selection["model"]["digest"],
        "config_digest": canonical_digest(selection["config"]),
        "policy_digest": canonical_digest(selection["policy"]),
        "suite_digest": canonical_digest(suite),
        "gate_digest": canonical_digest(gate),
        "evidence_digest": "sha256:" + "0" * 64,
        "trial": selection["trial"],
    }
    evidence: Dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA,
        "suite_id": suite["suite_id"],
        "subject": dict(selection["subject"]),
        "provenance": provenance,
        "runs": runs,
    }
    evidence["provenance"]["evidence_digest"] = evidence_digest(evidence)
    secrets = _find_secret_paths(evidence, path="evidence")
    if secrets:
        raise RuntimeEvidenceError(
            "收集結果含秘密或憑證：" + ", ".join(secrets[:5])
        )
    return evidence


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="收集正式 Basic Chat Runtime 評估證據")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--init-selection", type=Path)
    mode.add_argument("--selection", type=Path)
    parser.add_argument("--database", type=Path, default=Path(workbench_database.DB_PATH))
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--gate", type=Path, default=DEFAULT_GATE)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--subject-id", default="local-ai-workbench-basic-chat")
    parser.add_argument("--subject-version", default="1")
    parser.add_argument("--runtime-id", default="local-ai-workbench-basic-chat")
    parser.add_argument("--runtime-version", default="1")
    parser.add_argument("--model-id")
    parser.add_argument("--model-version", default="unknown")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--trial", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        suite = load_json(args.suite)
        gate = load_json(args.gate)
        if args.init_selection is not None:
            if not args.model_id:
                raise RuntimeEvidenceError("--init-selection 必須提供 --model-id")
            selection = build_selection(
                suite,
                gate,
                subject_id=args.subject_id,
                subject_version=args.subject_version,
                model_id=args.model_id,
                model_version=args.model_version,
                runtime_id=args.runtime_id,
                runtime_version=args.runtime_version,
                config=load_json(args.config) if args.config else {},
                policy=load_json(args.policy) if args.policy else {},
                trial=args.trial,
            )
            _atomic_json(args.init_selection, selection)
            print(
                f"已建立 {len(selection['runs'])} 題正式 Runtime capture："
                f"{args.init_selection}；完成每題後填入 run_id。"
            )
            return 0
        if args.evidence is None:
            raise RuntimeEvidenceError("--selection 必須同時提供 --evidence")
        evidence = collect_runtime_evidence(
            args.database,
            load_json(args.selection),
            suite,
            gate,
        )
        _atomic_json(args.evidence, evidence)
    except (
        ContractError,
        RuntimeEvidenceError,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"已收集 {len(evidence['runs'])} 個正式 Runtime Run：{args.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
