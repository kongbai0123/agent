import json
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Literal, Optional

from paths import PROJECT_RUNTIME_DIR, REPO_ROOT, RUNTIME_ROOT, WORKSPACES_ROOT


PermissionMode = Literal["read_only", "confirm_write", "workspace_write"]
RootKind = Literal["managed", "linked"]


@dataclass(frozen=True)
class WorkspaceContext:
    project_id: Optional[str]
    root_path: Path
    working_dir: Path
    runtime_dir: Path
    permission_mode: PermissionMode
    path_status: str = "ready"


_workspace_context: ContextVar[Optional[WorkspaceContext]] = ContextVar("workspace_context", default=None)


def normalize_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve(strict=False)


def path_key(value: str | Path) -> str:
    return os.path.normcase(str(normalize_path(value)))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_project_path(value: str | Path, *, require_existing: bool = False) -> Path:
    path = normalize_path(value)
    blocked = [RUNTIME_ROOT]
    if os.name == "nt":
        windows_dir = os.environ.get("WINDIR")
        if windows_dir:
            blocked.append(normalize_path(windows_dir))
    if path.parent == path or any(path == item or _is_within(path, item) for item in blocked):
        raise ValueError("此路徑不可作為專案工作區。")
    if require_existing and not path.is_dir():
        raise ValueError("指定的專案資料夾不存在。")
    if path.exists() and not path.is_dir():
        raise ValueError("專案路徑必須是資料夾。")
    return path


def managed_project_path(name: str, project_id: str) -> Path:
    # Managed source files live inside the project deletion boundary.
    return PROJECT_RUNTIME_DIR / project_id / "workspace"


def path_status(value: str | Path) -> str:
    path = normalize_path(value)
    if not path.exists():
        return "missing"
    if not path.is_dir():
        return "invalid"
    if not os.access(path, os.R_OK):
        return "permission_denied"
    if not os.access(path, os.W_OK):
        return "read_only"
    return "ready"


def project_runtime_path(project_id: str, *, create: bool = True) -> Path:
    path = PROJECT_RUNTIME_DIR / project_id
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def write_project_manifest(project: dict) -> None:
    runtime_dir = project_runtime_path(project["id"])
    payload = {
        "project_id": project["id"],
        "name": project["name"],
        "root_path": project["root_path"],
        "root_kind": project.get("root_kind", "linked"),
        "permission_mode": project.get("permission_mode", "read_only"),
    }
    (runtime_dir / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def context_for_project(project: Optional[dict]) -> WorkspaceContext:
    if not project:
        root = REPO_ROOT
        return WorkspaceContext(None, root, root, PROJECT_RUNTIME_DIR / "_independent", "read_only", "ready")
    root = normalize_path(project["root_path"])
    status = path_status(root)
    return WorkspaceContext(
        project["id"],
        root,
        root,
        project_runtime_path(project["id"]),
        project.get("permission_mode") or "read_only",
        status,
    )


def current_workspace() -> WorkspaceContext:
    context = _workspace_context.get()
    return context or context_for_project(None)


@contextmanager
def workspace_scope(context: WorkspaceContext) -> Iterator[WorkspaceContext]:
    token = _workspace_context.set(context)
    try:
        yield context
    finally:
        _workspace_context.reset(token)


def context_payload(context: WorkspaceContext) -> dict:
    payload = asdict(context)
    for key in ("root_path", "working_dir", "runtime_dir"):
        payload[key] = str(payload[key])
    return payload
