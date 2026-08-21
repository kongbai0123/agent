import asyncio
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from tool_approval_broker import (
    ToolApprovalBroker,
    ToolApprovalConflict,
    approval_risk_presentation,
)
from tool_runtime import (
    ApprovalStatus,
    ToolApprovalBinding,
    ToolApprovalRequest,
)


class FakeDatabase:
    def __init__(self, path: Path):
        self.path = path
        with self.get_db_conn() as conn:
            conn.execute(
                """
                CREATE TABLE capability_approvals (
                    id TEXT PRIMARY KEY, run_id TEXT, capability_name TEXT,
                    risk_level TEXT, reason TEXT, status TEXT,
                    requested_at TEXT, decided_at TEXT, decided_by TEXT
                )
                """
            )

    def get_db_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def create_capability_approval(self, approval_id, **values):
        with self.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO capability_approvals VALUES (?, ?, ?, ?, ?, 'pending', '', NULL, NULL)",
                (
                    approval_id,
                    values["run_id"],
                    values["capability_name"],
                    values["risk_level"],
                    values["reason"],
                ),
            )

    def decide_capability_approval(self, approval_id, approved, *, decided_by):
        with self.get_db_conn() as conn:
            cursor = conn.execute(
                "UPDATE capability_approvals SET status=?, decided_by=? WHERE id=? AND status='pending'",
                ("approved" if approved else "rejected", decided_by, approval_id),
            )
            return cursor.rowcount == 1

    def expire_capability_approval(self, approval_id):
        with self.get_db_conn() as conn:
            cursor = conn.execute(
                "UPDATE capability_approvals SET status='expired' WHERE id=? AND status='pending'",
                (approval_id,),
            )
            return cursor.rowcount == 1


def approval_request(approval_id="approval-1"):
    binding = ToolApprovalBinding(
        tool_name="github.create_issue",
        project_id="project-1",
        run_id="run-1",
        call_id="call-1",
        connection_id="connection-1",
        resource_id="owner/repo",
        manifest_sha256="a" * 64,
        resource_revision=3,
        arguments_sha256="b" * 64,
    )
    return ToolApprovalRequest(
        approval_id=approval_id,
        binding=binding,
        binding_sha256=binding.digest,
        summary={"risk_level": "external_write", "arguments": {"title": "secret body"}},
        reason="Create one issue",
        status=ApprovalStatus.PENDING,
        created_at=time.time(),
        expires_at=time.time() + 600,
    )


def test_broker_emits_redacted_event_and_delivers_single_decision(tmp_path):
    async def scenario():
        fake = FakeDatabase(tmp_path / "approvals.db")
        broker = ToolApprovalBroker(database_module=fake)
        request = approval_request()
        callback = asyncio.create_task(broker.approval_callback(request))
        event = await asyncio.wait_for(broker.event_queue("run-1").get(), 1)
        assert event["approval_id"] == "approval-1"
        assert event["operation_class"] == "external_write"
        assert event["risk_title"] == "外部網站操作"
        assert event["target"] == "owner/repo"
        assert event["approval_scope"].startswith("僅允許這一次")
        assert "secret body" not in str(event)

        result = await asyncio.to_thread(
            broker.decide,
            run_id="run-1",
            approval_id="approval-1",
            approved=True,
            decided_by="local_user",
        )
        assert result["approved"] is True
        assert (await callback).approved is True
        broker.mark_consumed("approval-1")
        with fake.get_db_conn() as conn:
            row = conn.execute(
                "SELECT status, arguments_sha256, summary_json FROM tool_approval_bindings"
            ).fetchone()
        assert row["status"] == "consumed"
        assert row["arguments_sha256"] == "b" * 64
        assert "secret body" not in row["summary_json"]

    asyncio.run(scenario())


def test_broker_rejects_cross_run_decision(tmp_path):
    async def scenario():
        fake = FakeDatabase(tmp_path / "approvals.db")
        broker = ToolApprovalBroker(database_module=fake)
        callback = asyncio.create_task(broker.approval_callback(approval_request()))
        await broker.event_queue("run-1").get()
        with pytest.raises(ToolApprovalConflict):
            await asyncio.to_thread(
                broker.decide,
                run_id="another-run",
                approval_id="approval-1",
                approved=True,
                decided_by="local_user",
            )
        broker.close_run("run-1")
        with pytest.raises(Exception):
            await callback

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("tool_name", "risk", "operation_class", "risk_title"),
    [
        ("mcp.browser.browser_tabs", "external_write", "low_risk", "低風險瀏覽器操作"),
        ("mcp.browser.browser_type", "external_write", "data_input", "資料輸入"),
        ("mcp.browser.browser_click", "external_write", "external_write", "外部網站操作"),
        ("danger.delete", "irreversible", "high_risk", "高風險且可能不可逆"),
        ("system.shell", "system", "system", "系統操作"),
    ],
)
def test_approval_risk_presentation_is_explicit_and_single_use(
    tool_name, risk, operation_class, risk_title
):
    request = approval_request()
    binding = ToolApprovalBinding(
        **{**request.binding.__dict__, "tool_name": tool_name}
    )
    request = ToolApprovalRequest(
        **{
            **request.__dict__,
            "binding": binding,
            "binding_sha256": binding.digest,
            "summary": {"risk_level": risk},
        }
    )
    presentation = approval_risk_presentation(request)
    assert presentation["operation_class"] == operation_class
    assert presentation["risk_title"] == risk_title
    assert presentation["operation_label"]
    assert presentation["target"]
    assert presentation["input_summary"]
    assert presentation["consequence"]
    assert presentation["data_disclosure"]
    assert presentation["reversibility"]
    assert "僅允許這一次" in presentation["approval_scope"]
