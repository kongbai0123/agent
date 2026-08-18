from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from hook_audit_store import HookAuditStore
from hook_runtime import HookAuditRecord


class _Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def get_db_conn(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


def test_hook_audit_store_persists_only_record_metadata(tmp_path):
    store = HookAuditStore(database_module=_Database(tmp_path / "audit.db"))
    store.record(
        HookAuditRecord(
            event="run.before_start",
            event_id="event-1",
            mode="guard",
            hook_id="guard",
            extension_id="builtin.policy",
            extension_version="1",
            manifest_sha256="a" * 64,
            status="failed",
            duration_ms=3,
            error_code="HOOK_GUARD_UNAVAILABLE",
            error_type="TimeoutError",
            error="safe bounded detail",
        )
    )

    rows = store.list(extension_id="builtin.policy")
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["error"] == "safe bounded detail"
    assert "metadata" not in rows[0]
