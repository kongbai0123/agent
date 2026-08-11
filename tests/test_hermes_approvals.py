from __future__ import annotations

import json

import pytest

from backend.hermes_approvals import (
    ApprovalError,
    ApprovalLedger,
    ApprovalScope,
    ApprovalStateError,
    ApprovalStatus,
    AuthorizationDecision,
    CapabilityAllowlist,
    CapabilityDeniedError,
    CapabilityRule,
    HermesApprovalGate,
)


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_gate(
    *rules: CapabilityRule,
    clock: FakeClock | None = None,
) -> tuple[HermesApprovalGate, FakeClock]:
    fake_clock = clock or FakeClock()
    ids = iter(f"approval-{index}" for index in range(100))
    ledger = ApprovalLedger(clock=fake_clock, id_factory=lambda: next(ids))
    return HermesApprovalGate(CapabilityAllowlist(rules), ledger), fake_clock


def test_default_policy_denies_authorization_and_approval_request() -> None:
    gate, _ = make_gate()
    scope = ApprovalScope(project_id="project-1", session_id="session-1")

    result = gate.authorize("tools.shell", scope, actor="runtime")

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason == "capability_not_allowlisted"
    with pytest.raises(CapabilityDeniedError):
        gate.request_approval(
            "tools.shell", scope, requested_by="operator@example.test"
        )
    assert [event.event for event in gate.ledger.audit_events()] == [
        "authorization_deny",
        "authorization_deny",
    ]


def test_allowlisted_capability_requires_workbench_approval() -> None:
    gate, _ = make_gate(CapabilityRule("tools.read_file"))
    scope = ApprovalScope(session_id="session-1", resource="notes.txt")

    missing = gate.authorize("tools.read_file", scope)
    approval = gate.request_approval(
        "tools.read_file",
        scope,
        requested_by="assistant",
        ttl_seconds=60,
    )
    pending = gate.authorize(
        "tools.read_file", scope, approval_id=approval.approval_id
    )

    assert missing.decision is AuthorizationDecision.REQUIRE_APPROVAL
    assert pending.decision is AuthorizationDecision.DENY
    assert pending.reason == "approval_pending"


def test_approved_scope_is_exact_and_single_use_by_default() -> None:
    gate, _ = make_gate(CapabilityRule("tools.write_file"))
    scope = ApprovalScope(
        project_id="project-1",
        session_id="session-1",
        run_id="run-1",
        resource="X:/workbench/projects/demo/output.txt",
    )
    approval = gate.request_approval(
        "tools.write_file", scope, requested_by="assistant"
    )
    approved = gate.ledger.decide(
        approval.approval_id,
        approved=True,
        decided_by="workbench-user",
        rationale="User confirmed this one write",
    )

    wrong_scope = ApprovalScope(
        project_id="project-1",
        session_id="session-1",
        run_id="run-2",
        resource="X:/workbench/projects/demo/output.txt",
    )
    denied = gate.authorize(
        "tools.write_file", wrong_scope, approval_id=approval.approval_id
    )
    allowed = gate.authorize(
        "tools.write_file", scope, approval_id=approval.approval_id
    )
    reused = gate.authorize(
        "tools.write_file", scope, approval_id=approval.approval_id
    )

    assert approved.status is ApprovalStatus.APPROVED
    assert denied.reason == "scope_mismatch"
    assert allowed.allowed is True
    assert allowed.approval_status is ApprovalStatus.CONSUMED
    assert reused.decision is AuthorizationDecision.DENY
    assert reused.reason == "approval_consumed"


def test_capability_mismatch_does_not_consume_approval() -> None:
    gate, _ = make_gate(
        CapabilityRule("tools.read_file"),
        CapabilityRule("tools.write_file"),
    )
    scope = ApprovalScope(session_id="session-1")
    approval = gate.request_approval(
        "tools.read_file", scope, requested_by="assistant"
    )
    gate.ledger.decide(
        approval.approval_id,
        approved=True,
        decided_by="user",
        rationale="Allow one read",
    )

    mismatch = gate.authorize(
        "tools.write_file", scope, approval_id=approval.approval_id
    )
    matching = gate.authorize(
        "tools.read_file", scope, approval_id=approval.approval_id
    )

    assert mismatch.reason == "capability_mismatch"
    assert matching.allowed


def test_multi_use_approval_is_consumed_at_limit() -> None:
    gate, _ = make_gate(CapabilityRule("browser.navigate"))
    scope = ApprovalScope(run_id="run-42", resource="https://example.test")
    approval = gate.request_approval(
        "browser.navigate", scope, requested_by="assistant", max_uses=2
    )
    gate.ledger.decide(
        approval.approval_id,
        approved=True,
        decided_by="user",
        rationale="Allow two navigations",
    )

    first = gate.authorize(
        "browser.navigate", scope, approval_id=approval.approval_id
    )
    second = gate.authorize(
        "browser.navigate", scope, approval_id=approval.approval_id
    )

    assert first.approval_status is ApprovalStatus.APPROVED
    assert second.approval_status is ApprovalStatus.CONSUMED
    assert gate.ledger.get(approval.approval_id).uses == 2


def test_approval_expires_once_and_cannot_be_decided() -> None:
    gate, clock = make_gate(CapabilityRule("tools.shell"))
    scope = ApprovalScope(session_id="session-1")
    approval = gate.request_approval(
        "tools.shell", scope, requested_by="assistant", ttl_seconds=10
    )
    clock.advance(10)

    expired = gate.authorize(
        "tools.shell", scope, approval_id=approval.approval_id
    )
    gate.ledger.get(approval.approval_id)

    assert expired.reason == "approval_expired"
    assert gate.ledger.get(approval.approval_id).status is ApprovalStatus.EXPIRED
    assert sum(
        event.event == "approval_expired" for event in gate.ledger.audit_events()
    ) == 1
    with pytest.raises(ApprovalStateError):
        gate.ledger.decide(
            approval.approval_id,
            approved=True,
            decided_by="user",
            rationale="Too late",
        )


def test_denied_and_revoked_approvals_are_terminal() -> None:
    gate, _ = make_gate(CapabilityRule("tools.shell"))
    scope = ApprovalScope(session_id="session-1")
    denied = gate.request_approval("tools.shell", scope, requested_by="assistant")
    gate.ledger.decide(
        denied.approval_id,
        approved=False,
        decided_by="user",
        rationale="Command not approved",
    )
    with pytest.raises(ApprovalStateError):
        gate.ledger.revoke(
            denied.approval_id, revoked_by="user", rationale="Already terminal"
        )

    revoked = gate.request_approval("tools.shell", scope, requested_by="assistant")
    result = gate.ledger.revoke(
        revoked.approval_id,
        revoked_by="user",
        rationale="Request withdrawn",
    )
    assert result.status is ApprovalStatus.REVOKED
    assert (
        gate.authorize("tools.shell", scope, approval_id=revoked.approval_id).reason
        == "approval_revoked"
    )


def test_explicit_no_approval_rule_is_audited_allow() -> None:
    gate, _ = make_gate(CapabilityRule("chat.respond", approval_required=False))
    scope = ApprovalScope(session_id="session-1")

    result = gate.authorize("chat.respond", scope, actor="runtime")

    assert result.allowed
    assert result.reason == "explicit_allowlist_exemption"
    assert gate.ledger.audit_events()[-1].event == "authorization_allow"
    with pytest.raises(ApprovalError):
        gate.request_approval("chat.respond", scope, requested_by="assistant")


def test_snapshot_is_json_safe_and_audit_sequence_is_append_only() -> None:
    gate, _ = make_gate(CapabilityRule("tools.read_file"))
    scope = ApprovalScope(project_id="project-1")
    approval = gate.request_approval(
        "tools.read_file", scope, requested_by="assistant"
    )
    gate.ledger.decide(
        approval.approval_id,
        approved=True,
        decided_by="user",
        rationale="Approved",
    )
    gate.authorize("tools.read_file", scope, approval_id=approval.approval_id)

    snapshot = gate.ledger.snapshot()
    encoded = json.dumps(snapshot, sort_keys=True)

    assert "approval-0" in encoded
    assert [event["sequence"] for event in snapshot["audit"]] == [1, 2, 3]
    events = gate.ledger.audit_events()
    assert isinstance(events, tuple)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CapabilityRule("tools.*"),
        lambda: ApprovalScope(),
        lambda: ApprovalScope(session_id="*"),
        lambda: CapabilityAllowlist.from_mapping(
            {"tools.read_file": {"approval_required": True, "unknown": 1}}
        ),
    ],
)
def test_policy_and_scope_validation_rejects_broad_or_unknown_values(factory) -> None:
    with pytest.raises(ApprovalError):
        factory()


def test_module_states_application_gate_is_not_an_os_boundary() -> None:
    import backend.hermes_approvals as approvals

    documentation = (approvals.__doc__ or "").casefold()
    assert "not" in documentation
    assert "operating-system security" in documentation
