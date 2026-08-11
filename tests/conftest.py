"""Keep deterministic pytest runs away from the user's live Workbench state."""

from __future__ import annotations

import os
import atexit
import shutil
import tempfile
from pathlib import Path


_ROOT = Path(tempfile.mkdtemp(prefix="workbench-pytest-"))


@atexit.register
def _cleanup_test_root() -> None:
    # Chroma may release its SQLite handle in another atexit callback. Cleanup
    # is best-effort so a Windows handle-order race never turns a passing test
    # run into a misleading shutdown traceback.
    shutil.rmtree(_ROOT, ignore_errors=True)

# These variables are set before pytest imports any test module (and therefore
# before app/paths/database are imported). TestClient lifespans, extension sync,
# session-token publication, settings writes, and runtime databases all remain
# inside the disposable test root.
os.environ["WORKBENCH_RUNTIME_DIR"] = str(_ROOT / "runtime")
os.environ["WORKBENCH_WORKSPACES_DIR"] = str(_ROOT / "workspaces")
os.environ["WORKBENCH_PROJECTS_DIR"] = str(_ROOT / "projects")
os.environ["WORKBENCH_SETTINGS_PATH"] = str(_ROOT / "settings.json")
# Deterministic tests must never wait on or mutate remote model caches during
# module collection. The Workbench test venv already carries the local models
# required by app imports; missing fixtures should fail immediately.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
