import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = Path(
    os.environ.get("WORKBENCH_RUNTIME_DIR", REPO_ROOT / "runtime")
).expanduser().resolve()
WORKSPACES_ROOT = Path(
    os.environ.get("WORKBENCH_WORKSPACES_DIR", REPO_ROOT / "workspaces")
).expanduser().resolve()
PROJECTS_ROOT = Path(
    os.environ.get("WORKBENCH_PROJECTS_DIR", REPO_ROOT / "projects")
).expanduser().resolve()

DB_DIR = RUNTIME_ROOT / "db"
DB_PATH = DB_DIR / "workbench.db"
CONVERSATIONS_DIR = RUNTIME_ROOT / "conversations"
KNOWLEDGE_DIR = RUNTIME_ROOT / "knowledge"
KNOWLEDGE_CHROMA_DIR = KNOWLEDGE_DIR / "chroma"
KNOWLEDGE_DOCUMENTS_DIR = KNOWLEDGE_DIR / "documents"
ATTACHMENTS_DIR = RUNTIME_ROOT / "shared-attachments"
SCREENSHOTS_DIR = RUNTIME_ROOT / "screenshots"
LOGS_DIR = RUNTIME_ROOT / "logs"
TEMP_DIR = RUNTIME_ROOT / "temp"
TRASH_DIR = RUNTIME_ROOT / "trash"
PROJECT_RUNTIME_DIR = RUNTIME_ROOT / "projects"
AUTOMATION_DIR = RUNTIME_ROOT / "automation"
AUTOMATION_RUNS_DIR = AUTOMATION_DIR / "runs"
TOOLS_DIR = RUNTIME_ROOT / "tools"
N8N_TOOL_DIR = TOOLS_DIR / "n8n"


def ensure_runtime_dirs() -> None:
    for path in (
        DB_DIR,
        KNOWLEDGE_CHROMA_DIR,
        LOGS_DIR,
        TEMP_DIR,
        TRASH_DIR,
        PROJECT_RUNTIME_DIR,
        AUTOMATION_RUNS_DIR,
        TOOLS_DIR,
        N8N_TOOL_DIR,
        WORKSPACES_ROOT,
        PROJECTS_ROOT,
    ):
        path.mkdir(parents=True, exist_ok=True)
