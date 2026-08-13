from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "frontend" / "n8n-agent-governance.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")


def test_workflow_center_has_policy_workflows_operations_and_audits():
    for marker in (
        'id="n8n-agent-policy-form"', 'id="n8n-agent-mode"', 'id="n8n-agent-duration"',
        'id="n8n-managed-workflows-list"', 'id="n8n-operation-requests-list"',
        'id="n8n-agent-audits-list"', 'id="n8n-agent-api-key-form"',
    ):
        assert marker in HTML
    assert "n8n-agent-governance.js?v=0.7.0-n8n-agent-governance-beta.1" in HTML


def test_workflow_center_has_project_scoped_agent_plan_mode():
    for marker in (
        'id="n8n-plan-form"', 'id="n8n-plan-messages"', 'id="n8n-plan-options"',
        'id="n8n-plan-session"',
        'id="n8n-plan-risks"', 'id="n8n-plan-outcomes"', 'id="n8n-plan-permissions"',
        'id="n8n-plan-proposal-ack"', 'id="n8n-plan-propose"',
    ):
        assert marker in HTML
    assert "/api/integrations/n8n/plans" in JS
    assert "project_id: id, session_id: sessionId() || null" in JS
    assert "state.deps.getSessions?.()" in JS
    assert "session.project_id === project" in JS
    assert "expected_digest: plan.digest, explicit_confirmation: true" in JS
    assert "Agent n8n 操作助理" in HTML
    assert "規劃與問題是執行前的安全層" in HTML
    assert "核准後 Broker 才會依提案內容實際操作 n8n" in HTML


def test_plan_mode_requires_explicit_confirmation_and_only_two_or_three_choices():
    assert "choices.slice(0, 3)" in JS
    assert "choices.length < 2" in JS
    assert "!state.dom.planProposalAck.checked" in JS
    assert "核准後 Broker 才會執行" in JS
    assert "textContent" in JS
    assert ".innerHTML" not in JS


def test_plan_followups_are_digest_locked_and_missing_digest_restarts_safely():
    assert "if (currentPlanId) body.expected_digest = state.plan.digest;" in JS
    assert "if (state.plan?.id && !state.plan.digest)" in JS
    assert "selectedOptionId = '';" in JS
    assert "舊計畫缺少版本摘要，已安全重開新計畫" in JS
    assert "伺服器權威 Before／After Diff" in JS
    assert "伺服器鎖定的操作快照" in JS


def test_project_refresh_is_safe_before_governance_initializes():
    assert "state.dom?.project?.value" in JS
    assert "if (!state.initialized || !state.dom?.project) return;" in JS
    assert "async function refreshAll() {\n        if (!state.initialized) return;" in JS


def test_frontend_is_fail_closed_and_uses_safe_dom():
    assert "textContent" in JS
    assert ".innerHTML" not in JS
    assert "expected_digest" in JS
    assert "explicit_ack" in JS
    assert "pending_second_approval" in JS
    assert "window.prompt" in JS
    assert "api_key" in JS
    assert "Secret 永遠不會交給 Agent" in HTML
    assert "const blockers = normalizedList(source.blockers || payload.blockers);" in JS
    assert "readyToPropose: blockers.length === 0" in JS
    assert "plan?.status === 'blocked'" in JS
    assert "n8n Broker 尚未就緒" in JS
    assert "const selectedSessionId = sessionId();" in JS
    assert "managed-workflows?project_id=${query(id)}${selectedSessionId" in JS
    assert "execution_unknown: '執行結果不明'" in JS


def test_styles_keep_twelve_pixel_minimum_and_responsive_layout():
    assert ".workflow-risk-callout" in CSS
    assert ".n8n-operation-diff" in CSS
    assert "font-size: 12px" in CSS
    assert ".n8n-agent-policy-grid { grid-template-columns: 1fr; }" in CSS
    assert ".n8n-plan-layout" in CSS
    assert ".n8n-plan-options" in CSS
    assert ".n8n-plan-message" in CSS
