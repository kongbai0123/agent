"""Workbench-owned integration seam for the optional Hermes Runs sidecar."""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import database
from hermes import (
    HermesConfig,
    HermesDisabledError,
    HermesError,
    HermesRunSnapshot,
    HermesRunsBridge,
    HermesUnavailableError,
)
from hermes_approval_store import (
    PersistentHermesApproval,
    PersistentHermesApprovalStore,
    approval_event_fingerprint,
    approval_summary,
)
from hermes_approvals import (
    ApprovalScope,
    ApprovalStatus,
    CapabilityAllowlist,
    CapabilityDeniedError,
    CapabilityRule,
    HermesApprovalGate,
)
from hermes_operations import (
    HealthStatus,
    HermesOperationsController,
    RoutingDecision,
)
from hermes_project_skills_bridge import (
    HermesProjectSkillsAttachment,
    HermesProjectSkillsBridge,
)
from structured_log import register_secret


REQUIRED_RUN_FEATURES = frozenset(
    {
        "run_submission",
        "run_status",
        "run_events_sse",
        "run_stop",
        "run_approval_response",
    }
)


class HermesIntegrationError(RuntimeError):
    code = "HERMES_INTEGRATION_ERROR"


class HermesSessionScopeError(HermesIntegrationError):
    code = "HERMES_SESSION_SCOPE_ERROR"


@dataclass(frozen=True)
class HermesIntegrationDecision:
    use_hermes: bool
    reason: str
    failure_class: str
    operations_decision: Optional[RoutingDecision] = None


class HermesIntegrationManager:
    """Compose secure routing, Runs, Project Skills, and durable approvals."""

    def __init__(
        self,
        *,
        config: HermesConfig,
        runs: HermesRunsBridge,
        project_skills: HermesProjectSkillsBridge,
        operations: HermesOperationsController,
        tools_enabled: bool = True,
        tool_project_id: Optional[str] = None,
        tool_capability: str = "hermes.tool",
        deployment_mode: str = "",
        tool_policy_profile: str = "no-tools-v1",
        docker_attestation: Optional[Mapping[str, Any]] = None,
        fallback_enabled: bool = True,
        approval_gate: Optional[HermesApprovalGate] = None,
        approval_store: Optional[PersistentHermesApprovalStore] = None,
    ) -> None:
        self.config = config
        self.runs = runs
        self.project_skills = project_skills
        self.operations = operations
        self._tools_enabled = bool(tools_enabled)
        self._tool_project_id = str(tool_project_id or "").strip() or None
        self._tool_capability = str(tool_capability or "").strip().casefold()
        self._deployment_mode = str(deployment_mode or "").strip().casefold()
        self._tool_policy_profile = str(tool_policy_profile or "no-tools-v1").strip()
        self._docker_attestation = dict(docker_attestation or {})
        if self._tools_enabled and self._tool_policy_profile == "project-readonly-v1" and (
            not self._tool_project_id or self._tool_capability != "hermes.project.read"
        ):
            raise ValueError("Hermes read-only tools require one project and one exact capability.")
        self._fallback_enabled = bool(fallback_enabled)
        self.approval_gate = approval_gate or HermesApprovalGate(
            CapabilityAllowlist((CapabilityRule(self._tool_capability, True),))
        )
        self.approval_store = approval_store or PersistentHermesApprovalStore()
        self._status_lock = threading.RLock()
        self._last_features = {
            name: False for name in sorted(REQUIRED_RUN_FEATURES)
        }
        if config.api_key:
            register_secret(config.api_key)

    def prepare_project_skills(
        self,
        session_id: str,
        user_query: str,
        *,
        run_id: str,
        consume_turn: bool = True,
    ) -> HermesProjectSkillsAttachment:
        return self.project_skills.prepare(
            session_id,
            user_query,
            run_id=run_id,
            consume_turn=consume_turn,
        )

    def _session_scope(
        self,
        session_id: str,
        attachment: HermesProjectSkillsAttachment,
    ) -> str:
        session = database.get_session(session_id)
        if session is None:
            raise HermesSessionScopeError("Workbench session does not exist.")
        project_id = str(session.get("project_id") or "").strip() or None
        if attachment.session_id != session_id:
            raise HermesSessionScopeError("Project Skills belong to another session.")
        if attachment.project_id != project_id:
            raise HermesSessionScopeError(
                "Project Skills no longer match the Workbench session project."
            )
        if self._tools_enabled and self._tool_project_id is not None and project_id != self._tool_project_id:
            raise HermesSessionScopeError(
                "Hermes read-only tools belong to another Workbench project."
            )
        return project_id or "unscoped"

    def start_run(
        self,
        *,
        workbench_run_id: str,
        workbench_session_id: str,
        input_text: str,
        attachment: HermesProjectSkillsAttachment,
        base_instructions: str = "",
        history: Optional[Sequence[Mapping[str, Any]]] = None,
        previous_response_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> HermesRunSnapshot:
        scope = self._session_scope(workbench_session_id, attachment)
        return self.runs.create_run(
            workbench_run_id,
            workbench_session_id,
            input_text,
            history=history,
            previous_response_id=previous_response_id,
            session_scope=scope,
            model=model or self.config.default_model,
            **attachment.as_run_kwargs(base_instructions),
        )

    @staticmethod
    def _features(payload: Mapping[str, Any]) -> dict[str, bool]:
        raw = payload.get("features")
        if not isinstance(raw, Mapping):
            return {name: False for name in sorted(REQUIRED_RUN_FEATURES)}
        return {
            name: raw.get(name) is True
            for name in sorted(REQUIRED_RUN_FEATURES)
        }

    def probe(self) -> dict[str, Any]:
        """Perform one authenticated probe and return only bounded public state."""

        started = time.monotonic()
        if not self.config.enabled:
            self.operations.health.record_probe(
                HealthStatus.UNHEALTHY,
                reason="integration_disabled",
            )
            self.operations.record_probe_failure()
            result = {
                "success": False,
                "reason": "integration_disabled",
                "features": {name: False for name in sorted(REQUIRED_RUN_FEATURES)},
                "health": self.operations.health.snapshot(),
            }
            with self._status_lock:
                self._last_features = dict(result["features"])
            return result
        try:
            health = self.runs.client.health()
            capabilities = self.runs.client.capabilities()
            features = self._features(capabilities)
            healthy = (
                str(health.get("status") or "").strip().casefold() == "ok"
                and all(features.values())
            )
            reason = "probe_ok" if healthy else "capabilities_incomplete"
            self.operations.health.record_probe(
                HealthStatus.HEALTHY if healthy else HealthStatus.DEGRADED,
                latency_ms=(time.monotonic() - started) * 1000,
                reason=reason,
            )
            if not healthy:
                self.operations.record_probe_failure()
            result = {
                "success": healthy,
                "reason": reason,
                "features": features,
                "health": self.operations.health.snapshot(),
            }
            with self._status_lock:
                self._last_features = dict(features)
            return result
        except HermesError as exc:
            reason = str(exc.code).casefold()
            self.operations.health.record_probe(
                HealthStatus.UNHEALTHY,
                latency_ms=(time.monotonic() - started) * 1000,
                reason=reason,
            )
            self.operations.record_probe_failure()
            result = {
                "success": False,
                "reason": reason,
                "features": {name: False for name in sorted(REQUIRED_RUN_FEATURES)},
                "health": self.operations.health.snapshot(),
            }
            with self._status_lock:
                self._last_features = dict(result["features"])
            return result

    def decide(self, session_id: str) -> HermesIntegrationDecision:
        if not self.config.enabled:
            return HermesIntegrationDecision(False, "integration_disabled", "disabled")
        if self._tools_enabled and self._tool_project_id is not None:
            session = database.get_session(session_id)
            project_id = str((session or {}).get("project_id") or "").strip() or None
            if project_id != self._tool_project_id:
                return HermesIntegrationDecision(
                    False, "readonly_project_mismatch", "disabled"
                )
        health = self.operations.health.snapshot()
        if health.get("status") in {"unknown", "stale"}:
            self.probe()
        decision = self.operations.decide(session_id)
        if decision.use_hermes:
            return HermesIntegrationDecision(
                True, decision.reason, "", operations_decision=decision
            )
        failure_class = (
            "disabled" if str(decision.reason).startswith("rollout_") else "unavailable"
        )
        return HermesIntegrationDecision(
            False,
            decision.reason,
            failure_class,
            operations_decision=decision,
        )

    def complete(
        self,
        decision: HermesIntegrationDecision,
        *,
        success: bool,
        failure_kind: str = "sidecar_request_failed",
    ) -> None:
        if decision.operations_decision is None or not decision.use_hermes:
            return
        self.operations.complete(
            decision.operations_decision,
            success=success,
            failure_kind=failure_kind,
            record_fallback=False,
        )

    def abandon(
        self,
        decision: HermesIntegrationDecision,
        *,
        reason: str = "cancelled",
    ) -> None:
        """Release a routing permit without recording cancellation as failure."""

        if decision.operations_decision is None or not decision.use_hermes:
            return
        self.operations.abandon(decision.operations_decision, reason=reason)
        if reason == "context_budget":
            self.operations.record_fallback()

    def fallback_allowed(
        self,
        workbench_run_id: str,
        exc: BaseException,
        *,
        token_emitted: bool,
    ) -> bool:
        """Fallback only when no upstream run could possibly have been accepted."""

        if not self._fallback_enabled or token_emitted or not isinstance(
            exc, (HermesDisabledError, HermesUnavailableError)
        ):
            return False
        try:
            mapping = self.runs.mappings.get_run(workbench_run_id)
        except Exception:
            # A failed idempotency lookup cannot prove that Hermes never
            # accepted the run, so replaying it through basic chat is unsafe.
            return False
        # `submission_unknown` means the POST may have succeeded before a
        # timeout. Any persisted mapping therefore blocks automatic replay in
        # basic chat; only a pre-submission failure is safely fallback-ready.
        allowed = mapping is None
        if allowed:
            self.operations.record_fallback()
        return allowed

    def register_approval(
        self,
        *,
        workbench_run_id: str,
        workbench_session_id: str,
        project_id: Optional[str],
        event: Mapping[str, Any],
    ) -> PersistentHermesApproval:
        fingerprint = approval_event_fingerprint(workbench_run_id, event)
        existing = self.approval_store.find_event(fingerprint)
        if existing is not None:
            return existing
        command = str(event.get("command") or event.get("tool") or "hermes-tool")
        resource = "sha256:" + hashlib.sha256(
            command.encode("utf-8", errors="replace")
        ).hexdigest()
        scope = ApprovalScope(
            project_id=project_id,
            session_id=workbench_session_id,
            run_id=workbench_run_id,
            resource=resource,
        )
        capability = self._tool_capability
        raw_choices = event.get("choices")
        available_choices = (
            tuple(
                item.strip().casefold()
                for item in raw_choices
                if isinstance(item, str) and item.strip()
            )
            if isinstance(raw_choices, (list, tuple))
            else ("once", "deny")
        )
        choices = tuple(
            value
            for value in ("once", "deny")
            if value in available_choices
        ) or ("deny",)
        summary = approval_summary(event).replace("\r", " ").replace("\n", " ")
        try:
            if not self._tools_enabled:
                raise CapabilityDeniedError("Hermes tools are disabled.")
            gate_record = self.approval_gate.request_approval(
                capability,
                scope,
                requested_by="hermes-sidecar",
                ttl_seconds=3600,
                max_uses=1,
            )
            approval_id = gate_record.approval_id
            status = "pending"
        except CapabilityDeniedError:
            approval_id = f"denied-{uuid.uuid4().hex}"
            status = "denied_policy"
            self.operations.record_tool_policy_denial()
        record = self.approval_store.create(
            approval_id=approval_id,
            event_fingerprint=fingerprint,
            workbench_run_id=workbench_run_id,
            workbench_session_id=workbench_session_id,
            project_id=project_id,
            capability=capability,
            resource=resource,
            summary=summary,
            status=status,
            choices=choices,
        )
        if status == "denied_policy":
            self.runs.resolve_approval(workbench_run_id, choice="deny")
        return record

    def resolve_approval(
        self,
        approval_id: str,
        *,
        choice: str,
        rationale: str,
    ) -> PersistentHermesApproval:
        normalized_choice = str(choice or "").strip().casefold()
        if normalized_choice not in {"once", "deny"}:
            raise ValueError("Hermes approval choice must be once or deny.")
        claimed = self.approval_store.claim(
            approval_id, choice=normalized_choice, rationale=rationale
        )
        live = self.approval_gate.ledger.get(approval_id)
        if normalized_choice == "once" and (
            live is None or live.status is not ApprovalStatus.PENDING
        ):
            # A process restart loses the in-memory, one-use authorization
            # nonce. Never turn a durable UI record into an approval without
            # that nonce; explicitly deny the upstream request instead.
            try:
                self.runs.resolve_approval(claimed.workbench_run_id, choice="deny")
            except Exception:
                self.approval_store.finish(
                    approval_id, status="resolution_unknown"
                )
                raise
            return self.approval_store.finish(
                approval_id, status="denied_missing_live_grant"
            )
        if live is not None and live.status is ApprovalStatus.PENDING:
            approved = normalized_choice == "once"
            self.approval_gate.ledger.decide(
                approval_id,
                approved=approved,
                decided_by="workbench-user",
                rationale=rationale or ("Approved once." if approved else "Denied."),
            )
            if approved:
                scope = ApprovalScope(
                    project_id=claimed.project_id,
                    session_id=claimed.workbench_session_id,
                    run_id=claimed.workbench_run_id,
                    resource=claimed.resource,
                )
                self.approval_gate.authorize(
                    claimed.capability,
                    scope,
                    approval_id=approval_id,
                    actor="workbench-user",
                    consume=True,
                )
        try:
            self.runs.resolve_approval(
                claimed.workbench_run_id,
                choice=normalized_choice,
            )
        except Exception:
            self.approval_store.finish(
                approval_id, status="resolution_unknown"
            )
            raise
        return self.approval_store.finish(
            approval_id,
            status="approved_once" if normalized_choice == "once" else "denied",
        )

    def cancel(self, workbench_run_id: str) -> dict[str, Any]:
        try:
            snapshot = self.runs.stop(workbench_run_id)
        finally:
            self.approval_store.expire_run(workbench_run_id)
        return {
            "run_id": snapshot.workbench_run_id,
            "status": snapshot.status,
            "cancelled": True,
        }

    def run_status(self, workbench_run_id: str) -> dict[str, Any]:
        snapshot = self.runs.status(workbench_run_id)
        return {
            "run_id": snapshot.workbench_run_id,
            "session_id": snapshot.workbench_session_id,
            "status": snapshot.status,
        }

    def status(self) -> dict[str, Any]:
        operations = dict(self.operations.status())
        # Routing history is useful inside the controller's focused tests but
        # is not part of the production status contract: even a short hash can
        # become a cross-request correlation identifier.
        operations.pop("recent_decisions", None)
        with self._status_lock:
            features = dict(self._last_features)
        health = dict(operations.get("health") or {})
        rollout = dict(operations.get("rollout") or {})
        health_gate = dict(operations.get("health_gate") or {})
        metrics = dict(operations.get("metrics") or {})
        return {
            "enabled": self.config.enabled,
            "configured": bool(self.config.enabled and self.config.api_key),
            "model": self.config.default_model,
            "base_url": self.config.base_url,
            "api_key_configured": bool(self.config.api_key),
            "health": health,
            "health_gate": health_gate,
            "rollout": rollout,
            "metrics": metrics,
            "features": features,
            "tools_enabled": bool(
                self.config.enabled
                and self._tools_enabled
                and health.get("status") == "healthy"
                and health_gate.get("allowed") is True
                and features.get("run_approval_response") is True
            ),
            "deployment_mode": self._deployment_mode,
            "tool_policy_profile": self._tool_policy_profile,
            # The project binding is enforced internally.  Public production
            # status exposes only whether a binding exists, never its ID.
            "tool_project_scoped": self._tool_project_id is not None,
            "docker_attestation": dict(self._docker_attestation),
            "fallback_enabled": self._fallback_enabled,
            "operations": operations,
            "pending_approval_count": len(self.approval_store.list_pending(limit=500)),
        }


__all__ = [
    "HermesIntegrationDecision",
    "HermesIntegrationError",
    "HermesIntegrationManager",
    "HermesSessionScopeError",
    "REQUIRED_RUN_FEATURES",
]
