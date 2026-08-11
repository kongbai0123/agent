"""Workbench policy and approval coordination for Hermes capabilities.

This module is deliberately an application-level gate.  It records intent,
scope, decisions, use, and audit events so the Workbench can make a
fail-closed routing decision.  It is **not** an operating-system security
boundary, a sandbox, or a substitute for process/container isolation.

The implementation performs no tool execution and no I/O.  A caller may
persist :meth:`ApprovalLedger.snapshot` and the audit events in its own store.
"""

from __future__ import annotations

import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Optional


_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SCOPE_FIELDS = ("project_id", "session_id", "run_id", "resource")


class ApprovalError(ValueError):
    """Base class for invalid approval configuration or operations."""


class ApprovalStateError(ApprovalError):
    """Raised when an approval transition is not valid from its state."""


class CapabilityDeniedError(PermissionError):
    """Raised when an approval is requested for an unlisted capability."""


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"


class AuthorizationDecision(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


def normalize_capability(value: str) -> str:
    capability = str(value or "").strip().lower()
    if not _CAPABILITY_RE.fullmatch(capability):
        raise ApprovalError(
            "capability must be an exact lowercase name containing only "
            "letters, digits, '.', ':', '_' or '-'"
        )
    if "*" in capability:
        raise ApprovalError("wildcard capabilities are not supported")
    return capability


def _required_text(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ApprovalError(f"{field} is required")
    if len(text) > 512:
        raise ApprovalError(f"{field} is too long")
    return text


def _scope_text(value: Optional[str], field: str) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ApprovalError(f"{field} cannot be blank")
    if "*" in text:
        raise ApprovalError(f"{field} cannot use a wildcard")
    if len(text) > 512:
        raise ApprovalError(f"{field} is too long")
    return text


@dataclass(frozen=True)
class ApprovalScope:
    """Exact context to which an approval applies.

    Equality is intentionally strict.  An approval created for a session and
    resource cannot be reused for a different run, a broader scope, or a
    context with additional identifiers.
    """

    project_id: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    resource: Optional[str] = None

    def __post_init__(self) -> None:
        for field in _SCOPE_FIELDS:
            object.__setattr__(self, field, _scope_text(getattr(self, field), field))
        if not any(getattr(self, field) is not None for field in _SCOPE_FIELDS):
            raise ApprovalError("an approval scope must include at least one identifier")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ApprovalScope":
        if not isinstance(value, Mapping):
            raise ApprovalError("approval scope must be an object")
        unknown = set(value) - set(_SCOPE_FIELDS)
        if unknown:
            raise ApprovalError(f"unknown approval scope fields: {sorted(unknown)}")
        return cls(**{field: value.get(field) for field in _SCOPE_FIELDS})

    def as_dict(self) -> dict[str, str]:
        return {
            field: value
            for field in _SCOPE_FIELDS
            if (value := getattr(self, field)) is not None
        }


@dataclass(frozen=True)
class CapabilityRule:
    capability: str
    approval_required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability", normalize_capability(self.capability))
        if type(self.approval_required) is not bool:
            raise ApprovalError("approval_required must be a boolean")


class CapabilityAllowlist:
    """Exact-match capability policy; an empty policy denies everything."""

    def __init__(self, rules: Iterable[CapabilityRule] = ()) -> None:
        indexed: dict[str, CapabilityRule] = {}
        for rule in rules:
            if not isinstance(rule, CapabilityRule):
                raise ApprovalError("allowlist entries must be CapabilityRule values")
            if rule.capability in indexed:
                raise ApprovalError(f"duplicate capability rule: {rule.capability}")
            indexed[rule.capability] = rule
        self._rules = indexed

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CapabilityAllowlist":
        if not isinstance(value, Mapping):
            raise ApprovalError("capability allowlist must be an object")
        rules: list[CapabilityRule] = []
        for capability, raw_rule in value.items():
            if isinstance(raw_rule, bool):
                approval_required = raw_rule
            elif isinstance(raw_rule, Mapping):
                unknown = set(raw_rule) - {"approval_required"}
                if unknown:
                    raise ApprovalError(
                        f"unknown fields for {capability}: {sorted(unknown)}"
                    )
                if "approval_required" not in raw_rule:
                    raise ApprovalError(
                        f"approval_required is required for {capability}"
                    )
                approval_required = raw_rule["approval_required"]
            else:
                raise ApprovalError(
                    f"allowlist rule for {capability} must be a boolean or object"
                )
            rules.append(CapabilityRule(str(capability), approval_required))
        return cls(rules)

    def rule_for(self, capability: str) -> Optional[CapabilityRule]:
        return self._rules.get(normalize_capability(capability))

    def as_dict(self) -> dict[str, dict[str, bool]]:
        return {
            name: {"approval_required": rule.approval_required}
            for name, rule in sorted(self._rules.items())
        }


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    capability: str
    scope: ApprovalScope
    requested_by: str
    status: ApprovalStatus
    created_at: float
    expires_at: float
    max_uses: int
    uses: int = 0
    decided_at: Optional[float] = None
    decided_by: Optional[str] = None
    rationale: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "capability": self.capability,
            "scope": self.scope.as_dict(),
            "requested_by": self.requested_by,
            "status": self.status.value,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "max_uses": self.max_uses,
            "uses": self.uses,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class ApprovalAuditEvent:
    sequence: int
    event: str
    actor: str
    timestamp: float
    approval_id: Optional[str]
    capability: str
    scope: ApprovalScope
    details: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event": self.event,
            "actor": self.actor,
            "timestamp": self.timestamp,
            "approval_id": self.approval_id,
            "capability": self.capability,
            "scope": self.scope.as_dict(),
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class AuthorizationResult:
    decision: AuthorizationDecision
    reason: str
    capability: str
    approval_id: Optional[str] = None
    approval_status: Optional[ApprovalStatus] = None

    @property
    def allowed(self) -> bool:
        return self.decision is AuthorizationDecision.ALLOW

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "capability": self.capability,
            "approval_id": self.approval_id,
            "approval_status": (
                self.approval_status.value if self.approval_status else None
            ),
        }


class ApprovalLedger:
    """Thread-safe, in-memory approval records and append-only audit events."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory
        self._records: dict[str, ApprovalRecord] = {}
        self._events: list[ApprovalAuditEvent] = []
        self._lock = threading.RLock()

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value) or value < 0:
            raise ApprovalError("clock returned an invalid timestamp")
        return value

    def _audit(
        self,
        event: str,
        *,
        actor: str,
        capability: str,
        scope: ApprovalScope,
        approval_id: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
        timestamp: Optional[float] = None,
    ) -> ApprovalAuditEvent:
        event_record = ApprovalAuditEvent(
            sequence=len(self._events) + 1,
            event=_required_text(event, "event"),
            actor=_required_text(actor, "actor"),
            timestamp=self._now() if timestamp is None else timestamp,
            approval_id=approval_id,
            capability=normalize_capability(capability),
            scope=scope,
            details=tuple(
                sorted((str(key), str(value)) for key, value in (details or {}).items())
            ),
        )
        self._events.append(event_record)
        return event_record

    def request(
        self,
        capability: str,
        scope: ApprovalScope,
        *,
        requested_by: str,
        ttl_seconds: float = 300.0,
        max_uses: int = 1,
    ) -> ApprovalRecord:
        capability = normalize_capability(capability)
        actor = _required_text(requested_by, "requested_by")
        if not isinstance(scope, ApprovalScope):
            raise ApprovalError("scope must be an ApprovalScope")
        ttl = float(ttl_seconds)
        if not math.isfinite(ttl) or ttl <= 0 or ttl > 86_400:
            raise ApprovalError("ttl_seconds must be greater than 0 and at most 86400")
        if isinstance(max_uses, bool) or not isinstance(max_uses, int) or max_uses < 1:
            raise ApprovalError("max_uses must be a positive integer")

        with self._lock:
            now = self._now()
            approval_id = _required_text(self._id_factory(), "approval_id")
            if approval_id in self._records:
                raise ApprovalError(f"duplicate approval_id: {approval_id}")
            record = ApprovalRecord(
                approval_id=approval_id,
                capability=capability,
                scope=scope,
                requested_by=actor,
                status=ApprovalStatus.PENDING,
                created_at=now,
                expires_at=now + ttl,
                max_uses=max_uses,
            )
            self._records[approval_id] = record
            self._audit(
                "approval_requested",
                actor=actor,
                approval_id=approval_id,
                capability=capability,
                scope=scope,
                details={"max_uses": max_uses, "ttl_seconds": ttl},
                timestamp=now,
            )
            return record

    def _expire_if_needed(self, record: ApprovalRecord, now: float) -> ApprovalRecord:
        if record.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}:
            if now >= record.expires_at:
                expired = replace(record, status=ApprovalStatus.EXPIRED)
                self._records[record.approval_id] = expired
                self._audit(
                    "approval_expired",
                    actor="system",
                    approval_id=record.approval_id,
                    capability=record.capability,
                    scope=record.scope,
                    timestamp=now,
                )
                return expired
        return record

    def get(self, approval_id: str, *, refresh_expiration: bool = True) -> Optional[ApprovalRecord]:
        approval_id = _required_text(approval_id, "approval_id")
        with self._lock:
            record = self._records.get(approval_id)
            if record is not None and refresh_expiration:
                record = self._expire_if_needed(record, self._now())
            return record

    def decide(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
        rationale: str,
    ) -> ApprovalRecord:
        if type(approved) is not bool:
            raise ApprovalError("approved must be a boolean")
        actor = _required_text(decided_by, "decided_by")
        reason = _required_text(rationale, "rationale")
        approval_id = _required_text(approval_id, "approval_id")
        with self._lock:
            record = self._records.get(approval_id)
            if record is None:
                raise KeyError(approval_id)
            now = self._now()
            record = self._expire_if_needed(record, now)
            if record.status is not ApprovalStatus.PENDING:
                raise ApprovalStateError(
                    f"cannot decide approval in state {record.status.value}"
                )
            status = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
            updated = replace(
                record,
                status=status,
                decided_at=now,
                decided_by=actor,
                rationale=reason,
            )
            self._records[approval_id] = updated
            self._audit(
                "approval_approved" if approved else "approval_denied",
                actor=actor,
                approval_id=approval_id,
                capability=record.capability,
                scope=record.scope,
                details={"rationale": reason},
                timestamp=now,
            )
            return updated

    def revoke(
        self,
        approval_id: str,
        *,
        revoked_by: str,
        rationale: str,
    ) -> ApprovalRecord:
        actor = _required_text(revoked_by, "revoked_by")
        reason = _required_text(rationale, "rationale")
        approval_id = _required_text(approval_id, "approval_id")
        with self._lock:
            record = self._records.get(approval_id)
            if record is None:
                raise KeyError(approval_id)
            now = self._now()
            record = self._expire_if_needed(record, now)
            if record.status not in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}:
                raise ApprovalStateError(
                    f"cannot revoke approval in state {record.status.value}"
                )
            updated = replace(
                record,
                status=ApprovalStatus.REVOKED,
                decided_at=now,
                decided_by=actor,
                rationale=reason,
            )
            self._records[approval_id] = updated
            self._audit(
                "approval_revoked",
                actor=actor,
                approval_id=approval_id,
                capability=record.capability,
                scope=record.scope,
                details={"rationale": reason},
                timestamp=now,
            )
            return updated

    def authorize(
        self,
        capability: str,
        scope: ApprovalScope,
        approval_id: str,
        *,
        actor: str = "workbench",
        consume: bool = True,
    ) -> AuthorizationResult:
        capability = normalize_capability(capability)
        actor = _required_text(actor, "actor")
        approval_id = _required_text(approval_id, "approval_id")
        if not isinstance(scope, ApprovalScope):
            raise ApprovalError("scope must be an ApprovalScope")
        if type(consume) is not bool:
            raise ApprovalError("consume must be a boolean")

        with self._lock:
            record = self._records.get(approval_id)
            now = self._now()
            reason: str
            if record is None:
                reason = "approval_not_found"
            else:
                record = self._expire_if_needed(record, now)
                if record.capability != capability:
                    reason = "capability_mismatch"
                elif record.scope != scope:
                    reason = "scope_mismatch"
                elif record.status is not ApprovalStatus.APPROVED:
                    reason = f"approval_{record.status.value}"
                elif record.uses >= record.max_uses:
                    reason = "approval_consumed"
                else:
                    uses = record.uses + (1 if consume else 0)
                    status = (
                        ApprovalStatus.CONSUMED
                        if consume and uses >= record.max_uses
                        else ApprovalStatus.APPROVED
                    )
                    if consume:
                        record = replace(record, uses=uses, status=status)
                        self._records[approval_id] = record
                    self._audit(
                        "authorization_allowed",
                        actor=actor,
                        approval_id=approval_id,
                        capability=capability,
                        scope=scope,
                        details={"consumed": consume, "uses": uses},
                        timestamp=now,
                    )
                    return AuthorizationResult(
                        AuthorizationDecision.ALLOW,
                        "approval_valid",
                        capability,
                        approval_id,
                        record.status,
                    )

            self._audit(
                "authorization_denied",
                actor=actor,
                approval_id=approval_id,
                capability=capability,
                scope=scope,
                details={"reason": reason},
                timestamp=now,
            )
            return AuthorizationResult(
                AuthorizationDecision.DENY,
                reason,
                capability,
                approval_id,
                record.status if record is not None else None,
            )

    def record_policy_result(
        self,
        *,
        decision: AuthorizationDecision,
        reason: str,
        capability: str,
        scope: ApprovalScope,
        actor: str,
    ) -> None:
        if not isinstance(decision, AuthorizationDecision):
            raise ApprovalError("decision must be an AuthorizationDecision")
        if not isinstance(scope, ApprovalScope):
            raise ApprovalError("scope must be an ApprovalScope")
        with self._lock:
            self._audit(
                f"authorization_{decision.value}",
                actor=actor,
                capability=capability,
                scope=scope,
                details={"reason": _required_text(reason, "reason")},
            )

    def records(self, *, refresh_expiration: bool = True) -> tuple[ApprovalRecord, ...]:
        with self._lock:
            if refresh_expiration:
                now = self._now()
                for record in tuple(self._records.values()):
                    self._expire_if_needed(record, now)
            return tuple(self._records[key] for key in sorted(self._records))

    def audit_events(self) -> tuple[ApprovalAuditEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe point-in-time view for persistence/observability."""
        with self._lock:
            records = self.records(refresh_expiration=True)
            return {
                "records": [record.as_dict() for record in records],
                "audit": [event.as_dict() for event in self._events],
            }


class HermesApprovalGate:
    """Combine exact capability policy with scoped approval state."""

    def __init__(
        self,
        allowlist: Optional[CapabilityAllowlist] = None,
        ledger: Optional[ApprovalLedger] = None,
    ) -> None:
        self.allowlist = allowlist or CapabilityAllowlist()
        self.ledger = ledger or ApprovalLedger()

    def request_approval(
        self,
        capability: str,
        scope: ApprovalScope,
        *,
        requested_by: str,
        ttl_seconds: float = 300.0,
        max_uses: int = 1,
    ) -> ApprovalRecord:
        capability = normalize_capability(capability)
        if not isinstance(scope, ApprovalScope):
            raise ApprovalError("scope must be an ApprovalScope")
        rule = self.allowlist.rule_for(capability)
        if rule is None:
            self.ledger.record_policy_result(
                decision=AuthorizationDecision.DENY,
                reason="capability_not_allowlisted",
                capability=capability,
                scope=scope,
                actor=requested_by,
            )
            raise CapabilityDeniedError(
                f"capability is not allowlisted: {capability}"
            )
        if not rule.approval_required:
            raise ApprovalError(
                "this capability is explicitly configured not to require approval"
            )
        return self.ledger.request(
            capability,
            scope,
            requested_by=requested_by,
            ttl_seconds=ttl_seconds,
            max_uses=max_uses,
        )

    def authorize(
        self,
        capability: str,
        scope: ApprovalScope,
        *,
        approval_id: Optional[str] = None,
        actor: str = "workbench",
        consume: bool = True,
    ) -> AuthorizationResult:
        capability = normalize_capability(capability)
        if not isinstance(scope, ApprovalScope):
            raise ApprovalError("scope must be an ApprovalScope")
        rule = self.allowlist.rule_for(capability)
        if rule is None:
            result = AuthorizationResult(
                AuthorizationDecision.DENY,
                "capability_not_allowlisted",
                capability,
            )
            self.ledger.record_policy_result(
                decision=result.decision,
                reason=result.reason,
                capability=capability,
                scope=scope,
                actor=actor,
            )
            return result
        if not rule.approval_required:
            result = AuthorizationResult(
                AuthorizationDecision.ALLOW,
                "explicit_allowlist_exemption",
                capability,
            )
            self.ledger.record_policy_result(
                decision=result.decision,
                reason=result.reason,
                capability=capability,
                scope=scope,
                actor=actor,
            )
            return result
        if not approval_id:
            result = AuthorizationResult(
                AuthorizationDecision.REQUIRE_APPROVAL,
                "approval_required",
                capability,
            )
            self.ledger.record_policy_result(
                decision=result.decision,
                reason=result.reason,
                capability=capability,
                scope=scope,
                actor=actor,
            )
            return result
        return self.ledger.authorize(
            capability,
            scope,
            approval_id,
            actor=actor,
            consume=consume,
        )


__all__ = [
    "ApprovalAuditEvent",
    "ApprovalError",
    "ApprovalLedger",
    "ApprovalRecord",
    "ApprovalScope",
    "ApprovalStateError",
    "ApprovalStatus",
    "AuthorizationDecision",
    "AuthorizationResult",
    "CapabilityAllowlist",
    "CapabilityDeniedError",
    "CapabilityRule",
    "HermesApprovalGate",
    "normalize_capability",
]
