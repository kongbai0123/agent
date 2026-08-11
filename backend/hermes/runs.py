"""Hermes Runs/Sessions bridge with stable Workbench identities."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence
from urllib.parse import quote

from .chat import normalize_text_messages
from .client import HermesSidecarClient, SSEEvent
from .config import validate_header_value
from .context_budget import assert_run_context_budget
from .errors import HermesConflictError, HermesError, HermesProtocolError
from .mapping import HermesRunMapping, HermesRunMappingStore


def _bounded_text(value: object, *, label: str, maximum: int, required: bool = False) -> str:
    text = str(value or "")
    if required and not text.strip():
        raise ValueError(f"{label} is required.")
    if len(text) > maximum:
        raise ValueError(f"{label} exceeded the size limit.")
    return text


@dataclass(frozen=True)
class HermesRunSnapshot:
    workbench_run_id: str
    workbench_session_id: str
    hermes_run_id: str
    status: str
    response: Mapping[str, Any]


class HermesRunsBridge:
    def __init__(
        self,
        client: HermesSidecarClient,
        mappings: Optional[HermesRunMappingStore] = None,
    ) -> None:
        self.client = client
        self.mappings = mappings or HermesRunMappingStore()

    @staticmethod
    def _snapshot(mapping: HermesRunMapping, response: Mapping[str, Any]) -> HermesRunSnapshot:
        return HermesRunSnapshot(
            workbench_run_id=mapping.workbench_run_id,
            workbench_session_id=mapping.workbench_session_id,
            hermes_run_id=mapping.hermes_run_id,
            status=mapping.status,
            response=response,
        )

    def create_run(
        self,
        workbench_run_id: str,
        workbench_session_id: str,
        input_text: str,
        *,
        instructions: Optional[str] = None,
        history: Optional[Sequence[Mapping[str, Any]]] = None,
        previous_response_id: Optional[str] = None,
        session_scope: Optional[str] = None,
        model: Optional[str] = None,
    ) -> HermesRunSnapshot:
        user_input = _bounded_text(
            input_text, label="Hermes run input", maximum=1_048_576, required=True
        )
        instruction_text = _bounded_text(
            instructions or "", label="Hermes run instructions", maximum=65_536
        )
        previous = _bounded_text(
            previous_response_id or "",
            label="Hermes previous response ID",
            maximum=256,
        ).strip()
        normalized_history = normalize_text_messages(history) if history else None
        selected_model = _bounded_text(
            model or "", label="Hermes run model", maximum=256
        ).strip()
        # Protect non-chat callers too. This check precedes every mapping write,
        # so an oversized request cannot leave an ambiguous local run identity.
        assert_run_context_budget(
            user_input,
            instruction_text,
            normalized_history or (),
        )
        session = self.mappings.get_or_create_session(
            workbench_session_id,
            workbench_scope=session_scope,
        )
        reserved = self.mappings.reserve_run(
            workbench_run_id,
            workbench_session_id,
            previous_response_id=previous,
        )
        if reserved.hermes_run_id:
            # An already-bound Workbench run is idempotent locally and must not
            # produce a second upstream run.
            return self._snapshot(reserved, {"id": reserved.hermes_run_id, "status": reserved.status})
        if reserved.status != "creating":
            raise HermesConflictError("Workbench run cannot be submitted again.")

        payload: Dict[str, Any] = {
            "input": user_input,
            "session_id": session.hermes_session_id,
        }
        if selected_model:
            payload["model"] = selected_model
        if instruction_text:
            payload["instructions"] = instruction_text
        if normalized_history:
            # Hermes' public Runs API names this field conversation_history.
            # Keeping the wire name exact matters because older prototypes
            # silently ignored a generic `history` key.
            payload["conversation_history"] = normalized_history
        if previous:
            payload["previous_response_id"] = previous
        try:
            response = self.client.request_json(
                "POST",
                "/v1/runs",
                payload=payload,
                headers={
                    "Idempotency-Key": validate_header_value(
                        workbench_run_id, label="Workbench run ID"
                    ),
                    "X-Hermes-Session-Key": session.hermes_session_key,
                },
            )
        except HermesError:
            # A timeout can occur after Hermes accepted the request. Refuse an
            # automatic resubmission with the same Workbench run ID; the caller
            # may reconcile status or deliberately create a new run instead.
            self.mappings.update_status(workbench_run_id, "submission_unknown")
            raise
        upstream_id = str(response.get("id") or response.get("run_id") or "").strip()
        if not upstream_id:
            self.mappings.update_status(workbench_run_id, "protocol_error")
            raise HermesProtocolError("Hermes run response is missing its ID.")
        status = str(response.get("status") or "queued").strip() or "queued"
        try:
            mapping = self.mappings.bind_run(
                workbench_run_id, upstream_id, status=status
            )
        except (HermesConflictError, ValueError) as exc:
            self.mappings.update_status(workbench_run_id, "protocol_error")
            raise HermesProtocolError("Hermes run identity could not be mapped.") from exc
        return self._snapshot(mapping, response)

    def _bound(self, workbench_run_id: str) -> HermesRunMapping:
        mapping = self.mappings.get_run(workbench_run_id)
        if mapping is None or not mapping.hermes_run_id:
            raise KeyError(str(workbench_run_id))
        return mapping

    @staticmethod
    def _run_path(mapping: HermesRunMapping, suffix: str = "") -> str:
        upstream_id = quote(mapping.hermes_run_id, safe="")
        return f"/v1/runs/{upstream_id}{suffix}"

    def status(self, workbench_run_id: str) -> HermesRunSnapshot:
        mapping = self._bound(workbench_run_id)
        response = self.client.request_json("GET", self._run_path(mapping))
        status = str(response.get("status") or mapping.status).strip() or mapping.status
        updated = self.mappings.update_status(workbench_run_id, status)
        return self._snapshot(updated, response)

    def stop(self, workbench_run_id: str) -> HermesRunSnapshot:
        mapping = self._bound(workbench_run_id)
        response = self.client.request_json("POST", self._run_path(mapping, "/stop"), payload={})
        status = str(response.get("status") or "stopping").strip() or "stopping"
        updated = self.mappings.update_status(workbench_run_id, status)
        return self._snapshot(updated, response)

    def resolve_approval(
        self,
        workbench_run_id: str,
        *,
        choice: str,
    ) -> HermesRunSnapshot:
        """Resolve exactly one pending Hermes approval; broad grants stay disabled."""

        normalized_choice = str(choice or "").strip().casefold()
        if normalized_choice not in {"once", "deny"}:
            raise ValueError("Workbench permits only once or deny for Hermes approvals.")
        mapping = self._bound(workbench_run_id)
        response = self.client.request_json(
            "POST",
            self._run_path(mapping, "/approval"),
            payload={"choice": normalized_choice, "resolve_all": False},
        )
        status = "running" if normalized_choice == "once" else "approval_denied"
        updated = self.mappings.update_status(workbench_run_id, status)
        return self._snapshot(updated, response)

    @contextmanager
    def open_events(
        self,
        workbench_run_id: str,
        *,
        after_event_id: str = "",
    ) -> Iterator[Iterator[SSEEvent]]:
        mapping = self._bound(workbench_run_id)
        headers: Dict[str, str] = {}
        if after_event_id:
            headers["Last-Event-ID"] = validate_header_value(
                after_event_id, label="Hermes event ID"
            )
        with self.client.open_sse(
            self._run_path(mapping, "/events"), headers=headers
        ) as events:
            yield events

    def events(
        self,
        workbench_run_id: str,
        *,
        after_event_id: str = "",
    ) -> Iterator[SSEEvent]:
        with self.open_events(
            workbench_run_id, after_event_id=after_event_id
        ) as events:
            for event in events:
                yield event
