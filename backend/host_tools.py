"""Composition boundary between project tool providers and Basic Chat."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional

try:
    from tool_approval_broker import ToolApprovalBroker
    from tool_runtime import ToolAuditRecord, ToolDefinition, ToolDispatcher, ToolRegistry
except ImportError:  # pragma: no cover - package import compatibility
    from backend.tool_approval_broker import ToolApprovalBroker
    from backend.tool_runtime import ToolAuditRecord, ToolDefinition, ToolDispatcher, ToolRegistry


ProjectPreparer = Callable[[str], Any]
CallContextResolver = Callable[[str, ToolDefinition, Mapping[str, Any]], Any]
CapabilityStatusQuery = Callable[[str, str], Any]


@dataclass(frozen=True)
class ToolCallContext:
    connection_id: Optional[str] = None
    resource_id: Optional[str] = None


class HostToolRuntime:
    """Project-scoped registry, dispatcher, approvals and public event bridge."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        dispatcher: ToolDispatcher,
        approval_broker: ToolApprovalBroker,
        prepare_project: Optional[ProjectPreparer] = None,
        resolve_call_context: Optional[CallContextResolver] = None,
        capability_status_query: Optional[CapabilityStatusQuery] = None,
        independent_scope_id: Optional[str] = None,
    ) -> None:
        self.registry = registry
        self.dispatcher = dispatcher
        self.approval_broker = approval_broker
        self.prepare_project = prepare_project
        self.resolve_call_context_callback = resolve_call_context
        self.capability_status_query_callback = capability_status_query
        self.independent_scope_id = str(independent_scope_id or "").strip() or None
        self._event_queues: dict[str, asyncio.Queue[tuple[str, dict[str, Any]]]] = {}
        self._prior_audit_sink = dispatcher.audit_sink
        dispatcher.audit_sink = self._audit_sink

    async def _call_prior_audit(self, record: ToolAuditRecord) -> None:
        sink = self._prior_audit_sink
        if sink is None:
            return
        if inspect.iscoroutinefunction(sink):
            await sink(record)
            return
        result = await asyncio.to_thread(sink, record)
        if inspect.isawaitable(result):
            await result

    async def _audit_sink(self, record: ToolAuditRecord) -> None:
        await self._call_prior_audit(record)
        if record.event == "approval_required":
            return
        if record.event == "tool_started":
            event = "tool_start"
            payload = {
                "tool": record.tool_name,
                "tool_call_id": record.call_id,
                "run_id": record.run_id,
                "project_id": record.project_id,
                "args": {
                    "scope": "active_project",
                    "access": record.access,
                    "details_redacted": True,
                },
            }
        else:
            event = "tool_end"
            payload = {
                "tool": record.tool_name,
                "tool_call_id": record.call_id,
                "run_id": record.run_id,
                "project_id": record.project_id,
                "success": record.status == "completed",
                "result": "completed" if record.status == "completed" else "failed",
                "details_redacted": True,
                "duration_ms": int(record.duration_ms or 0),
            }
        await self.event_queue(record.run_id).put((event, payload))

    def event_queue(self, run_id: str) -> asyncio.Queue[tuple[str, dict[str, Any]]]:
        normalized = str(run_id or "").strip()
        if not normalized:
            raise ValueError("run_id is required")
        queue = self._event_queues.get(normalized)
        if queue is None:
            queue = asyncio.Queue()
            self._event_queues[normalized] = queue
        return queue

    async def definitions_for_project(self, project_id: str) -> tuple[ToolDefinition, ...]:
        if self.prepare_project is not None:
            result = self.prepare_project(project_id)
            if inspect.isawaitable(result):
                await result
        return self.registry.for_project(project_id)

    async def query_capability_status(
        self,
        project_id: str,
        query: str,
    ) -> Optional[Mapping[str, Any]]:
        """Return the authoritative read-only capability snapshot, if wired."""

        callback = self.capability_status_query_callback
        if callback is None:
            return None
        resolved = callback(project_id, query)
        if inspect.isawaitable(resolved):
            resolved = await resolved
        return dict(resolved) if isinstance(resolved, Mapping) else None

    async def resolve_call_context(
        self,
        project_id: str,
        definition: ToolDefinition,
        arguments: Mapping[str, Any],
    ) -> ToolCallContext:
        callback = self.resolve_call_context_callback
        if callback is not None:
            resolved = callback(project_id, definition, arguments)
            if inspect.isawaitable(resolved):
                resolved = await resolved
            if isinstance(resolved, ToolCallContext):
                return resolved
            if isinstance(resolved, Mapping):
                return ToolCallContext(
                    connection_id=(
                        str(resolved.get("connection_id"))
                        if resolved.get("connection_id")
                        else None
                    ),
                    resource_id=(
                        str(resolved.get("resource_id"))
                        if resolved.get("resource_id")
                        else None
                    ),
                )
        resource = (
            arguments.get("repository")
            or arguments.get("page_id")
            or arguments.get("database_id")
            or arguments.get("parent_id")
        )
        return ToolCallContext(
            connection_id=(
                str(arguments.get("connection_id"))
                if arguments.get("connection_id")
                else None
            ),
            resource_id=str(resource) if resource else None,
        )

    def close_run(self, run_id: str) -> None:
        self.approval_broker.close_run(run_id)
        self._event_queues.pop(str(run_id or "").strip(), None)


__all__ = ["HostToolRuntime", "ToolCallContext"]
