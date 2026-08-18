import asyncio
import hashlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from host_tools import HostToolRuntime
from tool_runtime import (
    ToolAccess,
    ToolAuditRecord,
    ToolDefinition,
    ToolDispatcher,
    ToolRegistry,
    ToolScopeState,
)


class FakeApprovals:
    def close_run(self, _run_id):
        return None


def test_host_runtime_turns_tool_audits_into_redacted_public_events():
    async def scenario():
        digest = hashlib.sha256(b"tool").hexdigest()
        definition = ToolDefinition(
            name="github.read_file",
            description="Read one file",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            access=ToolAccess.READ,
            handler=lambda _call: {},
            extension_id="connector.github",
            manifest_sha256=digest,
        )
        registry = ToolRegistry([definition])
        dispatcher = ToolDispatcher(
            registry,
            scope_resolver=lambda _definition, _call: ToolScopeState(
                installed=True,
                trusted=True,
                enabled=True,
                healthy=True,
                resource_allowed=True,
                manifest_sha256=digest,
            ),
        )
        runtime = HostToolRuntime(
            registry=registry,
            dispatcher=dispatcher,
            approval_broker=FakeApprovals(),
        )
        await runtime._audit_sink(ToolAuditRecord(
            audit_id="audit-1",
            event="tool_started",
            tool_name="github.read_file",
            call_id="call-1",
            run_id="run-1",
            project_id="project-1",
            access="read",
            risk_level="external_read",
            status="started",
            payload={"arguments": {"path": "private.txt"}},
        ))
        event, payload = await runtime.event_queue("run-1").get()
        assert event == "tool_start"
        assert payload["args"]["details_redacted"] is True
        assert "private.txt" not in str(payload)

    asyncio.run(scenario())
