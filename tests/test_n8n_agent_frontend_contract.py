import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "frontend" / "n8n-agent-governance.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")


def test_workflow_center_has_policy_workflows_operations_and_audits():
    for marker in (
        'id="n8n-agent-policy-form"', 'id="n8n-agent-mode"', 'id="n8n-agent-duration"',
        'id="n8n-managed-workflows-list"', 'id="n8n-operation-requests-list"',
        'id="n8n-agent-audits-list"', 'id="n8n-agent-api-key-form"',
    ):
        assert marker in HTML
    assert "n8n-agent-governance.js?v=0.9.2-plan-restore" in HTML


def test_project_scoped_credential_alias_ui_never_renders_credential_id():
    for marker in (
        'id="n8n-credential-alias-form"', 'id="n8n-credential-alias-name"',
        'type="password" id="n8n-credential-alias-id"',
        'id="n8n-credential-aliases-list"', 'id="n8n-credential-aliases-count"',
    ):
        assert marker in HTML
    assert "/api/integrations/n8n/credential-aliases" in JS
    assert "/refresh?project_id=${query(scopedProject)}" in JS
    assert "credential_id: credentialId" in JS
    assert "state.dom.credentialAliasId.value = '';" in JS
    rendered = JS[JS.index("function credentialAliasRow"):JS.index("function runtimeApprovalRow")]
    assert "credential_id" not in rendered.lower()
    assert "metadata_digest" not in rendered.lower()
    assert "credential.credential_type" in rendered
    assert "credential.display_name" in rendered


def test_runtime_approvals_are_exact_digest_locked_and_inspector_driven():
    for marker in (
        'id="n8n-runtime-approvals-title"', 'id="n8n-runtime-approvals-list"',
        'id="n8n-runtime-approvals-count"',
    ):
        assert marker in HTML
    inspector = JS[JS.index("function showRuntimeApproval"):JS.index("async function decideRuntimeApproval")]
    for exact_field in (
        "approval.workflow_id", "approval.workflow_revision", "approval.node_id",
        "approval.credential_alias", "approval.target", "approval.action",
        "approval.request_digest",
    ):
        assert exact_field in inspector
    assert "duration.value = '0'" in inspector
    assert "duration.min = '0'" in inspector
    assert "duration.max = '60'" in inspector
    assert "state.policy?.mode === 'full_audit'" in inspector
    assert "限制權限模式固定為 0" in inspector
    decision = JS[JS.index("async function decideRuntimeApproval"):JS.index("async function decide(")]
    assert "/api/integrations/n8n/runtime-approvals/" in decision
    assert "expected_digest: approval.request_digest" in decision
    assert "duration_minutes:" in decision
    assert "projectId() !== scopedProject" in decision


def test_project_switch_clears_scoped_runtime_state_and_rejects_stale_responses():
    reset = JS[JS.index("function resetProjectScopedRuntime"):JS.index("async function adoptCredentialAlias")]
    assert "state.requestId += 1" in reset
    assert "state.credentialAliases = []" in reset
    assert "state.runtimeApprovals = []" in reset
    assert "state.dom.credentialAliasId.value = ''" in reset
    assert "state.inspectorScope = ''" in reset
    assert "resetProjectScopedRuntime(); resetPlanner()" in JS
    assert "requestId !== state.requestId || projectId() !== id" in JS
    assert "credential-aliases?project_id=${query(id)}" in JS
    assert "runtime-approvals?project_id=${query(id)}&limit=100" in JS


def test_governance_inspector_uses_project_scoped_owner_leases():
    owner_helpers = JS[JS.index("function workflowWorkspaceActive"):JS.index("const listOf")]
    assert "claimContentOwner" in owner_helpers
    assert "contentOwnerMatches" in owner_helpers
    assert "state.inspectorLease = null" in owner_helpers

    operation = JS[JS.index("function showOperation"):JS.index("function showRuntimeApproval")]
    runtime = JS[JS.index("function showRuntimeApproval"):JS.index("async function decideRuntimeApproval")]
    assert "operation?.project_id !== scopedProject" in operation
    assert "inspectorOwner('operation', operation.id)" in operation
    assert "ownsInspector(activeLease)" in operation
    assert "approval.project_id !== scopedProject" in runtime
    assert "inspectorOwner('runtime', approval.approval_id)" in runtime
    assert "ownsInspector(activeLease)" in runtime

    decision = JS[JS.index("async function decideRuntimeApproval"):JS.index("function renderCatalogResults")]
    assert decision.count("ownsInspector(lease)") >= 4
    assert "showRuntimeApproval(updated, { lease })" in decision
    assert "showOperation(updated, { lease })" in decision
    assert "releaseInspectorContext(); resetProjectScopedRuntime()" in JS


def test_runtime_decision_behavior_uses_full_audit_minutes_and_drops_stale_ui_updates():
    start = JS.index("async function decideRuntimeApproval")
    end = JS.index("\n    async function decide(", start)
    function_source = JS[start:end]
    script = f"""
const results = [];
let activeProject = 'project-a';
let staleDuringRequest = false;
let refreshes = 0;
let inspectorUpdates = 0;
let toasts = 0;
const state = {{
  inspectorScope: 'runtime:project-a',
  inspectorLease: {{owner: 'runtime:approval-1', generation: 1}},
  policy: {{mode: 'full_audit'}},
  deps: {{showToast: () => {{ toasts += 1; }}}}
}};
const projectId = () => activeProject;
const ownsInspector = lease => lease === state.inspectorLease && activeProject === 'project-a';
const query = value => encodeURIComponent(String(value || ''));
const api = async (path, options) => {{
  const body = JSON.parse(options.body);
  results.push({{path, body}});
  if (staleDuringRequest) activeProject = 'project-b';
  return {{approval_id: 'approval-1', project_id: 'project-a', status: 'approved'}};
}};
const refreshAll = async () => {{ refreshes += 1; }};
const showRuntimeApproval = () => {{ inspectorUpdates += 1; }};
{function_source}
(async () => {{
  const approval = {{approval_id: 'approval-1', project_id: 'project-a', request_digest: 'a'.repeat(64)}};
  await decideRuntimeApproval(approval, true, 15);
  staleDuringRequest = true;
  activeProject = 'project-a';
  state.inspectorScope = 'runtime:project-a';
  await decideRuntimeApproval(approval, false, 0);
  process.stdout.write(JSON.stringify({{results, refreshes, inspectorUpdates, toasts}}));
}})();
"""
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, encoding="utf-8",
        capture_output=True, check=True,
    )
    result = json.loads(completed.stdout)
    assert result["results"][0]["path"].endswith("/approval-1/approve")
    assert result["results"][0]["body"] == {
        "project_id": "project-a", "expected_digest": "a" * 64, "duration_minutes": 15,
    }
    assert result["results"][1]["path"].endswith("/approval-1/reject")
    assert result["results"][1]["body"]["duration_minutes"] == 0
    assert result["refreshes"] == 1
    assert result["inspectorUpdates"] == 1
    assert result["toasts"] == 1


def test_workflow_center_has_project_scoped_agent_plan_mode():
    for marker in (
        'id="workflow-chat-start"', 'id="n8n-plan-workspace"',
        'id="n8n-plan-form"', 'id="n8n-plan-messages"', 'id="n8n-plan-options"',
        'id="n8n-plan-session"',
        'id="n8n-plan-provenance"', 'id="n8n-plan-primary-model"',
        'id="n8n-plan-structured-mode"', 'id="n8n-plan-repair-model"',
        'id="n8n-plan-risks"', 'id="n8n-plan-outcomes"', 'id="n8n-plan-permissions"',
        'id="n8n-plan-proposal-ack"', 'id="n8n-plan-propose"',
    ):
        assert marker in HTML
    assert "/api/integrations/n8n/plans" in JS
    assert "project_id: id, session_id: sessionId() || null" in JS
    assert "state.deps.getSessions?.()" in JS
    assert "String(session.project_id) === String(id || '')" in JS
    assert "expected_digest: plan.digest, explicit_confirmation: true" in JS
    assert 'id="n8n-plan-workspace" aria-labelledby="n8n-plan-title" hidden' in HTML
    assert 'id="n8n-plan-form" class="workflow-form n8n-plan-form" hidden' in HTML
    assert "檢查助理建議的流程" in HTML
    assert "需求由目前聊天帶入" in HTML
    assert "在聊天中建立自動化" in HTML
    assert "想讓 n8n 幫你做什麼？" not in HTML
    assert "送出需求" not in HTML
    assert "核准後 Broker 才會依提案內容實際操作 n8n" in HTML


def test_chat_first_mode_keeps_scope_internal_and_auto_selects_current_context():
    assert 'id="n8n-plan-scope"' not in HTML
    assert 'id="n8n-plan-scope-summary"' in HTML
    assert '<div hidden aria-hidden="true">' in HTML
    assert HTML.index('id="n8n-agent-project"') < HTML.index('id="n8n-plan-session"')
    assert "projects.length === 1 ? String(projects[0].id) : ''" in JS
    assert "sessions.length === 1 ? String(sessions[0].id) : ''" in JS
    assert "const requested = active || selected" in JS
    assert "const requested = String(current || selected || '')" in JS
    assert "state.dom.planScope.open" not in JS
    assert "自動沿用：" in JS
    assert "請先在左側選擇專案" in JS
    assert "請先回到聊天選擇一個對話" in JS


def test_empty_single_user_workspace_is_prepared_on_first_request_without_weakening_permissions():
    assert "function ensurePersonalScope()" in JS
    assert "if ((!projectId() || !sessionId()) && !await ensurePersonalScope()) return" in JS
    assert "scopeCanBePrepared = canAutoProvisionScope()" in JS
    assert "(!hasScope && !scopeCanBePrepared)" in JS
    assert "api('/api/projects'" in JS
    assert "name: '個人自動化'" in JS
    assert "root_kind: 'managed'" in JS
    assert "permission_mode: 'read_only'" in JS
    assert "api('/api/sessions'" in JS
    assert "title: 'n8n 自動化'" in JS
    handoff = JS[JS.index("async function startPlanFromChat"):JS.index("async function materializePlan")]
    assert "canAutoProvisionScope()" in handoff
    assert "await ensurePersonalScope()" in handoff
    assert "refreshWorkspaceScope: () => loadSessions" in APP_JS
    assert '<select id="n8n-agent-project" required' not in HTML
    assert '<select id="n8n-plan-session" required' not in HTML


def test_advanced_permission_and_gmail_controls_stay_available_but_collapsed():
    assert '<details class="n8n-progressive-panel" id="n8n-advanced-settings">' in HTML
    assert '<details class="n8n-progressive-panel" id="n8n-gmail-settings">' in HTML
    assert 'id="n8n-advanced-settings" open' not in HTML
    assert 'id="n8n-gmail-settings" open' not in HTML
    for marker in (
        'id="n8n-agent-policy-form"', 'id="n8n-agent-api-key-form"',
        'id="n8n-credential-alias-form"', 'id="n8n-runtime-approvals-list"',
        'id="n8n-managed-workflows-list"', 'id="n8n-operation-requests-list"',
        'id="n8n-agent-audits-list"', 'id="mail-profile-form"',
        'id="mail-compose-form"', 'id="mail-runs-list"',
    ):
        assert HTML.count(marker) == 1
    assert "安全模式（敏感動作逐次確認）" in HTML
    assert 'id="n8n-agent-duration-field" hidden' in HTML
    assert "state.dom.duration.closest('label').hidden = !advanced" in JS


def test_plan_mode_requires_explicit_confirmation_and_only_two_or_three_choices():
    assert "rawChoices.length < 2 || rawChoices.length > 3" in JS
    assert "return choices.length === rawChoices.length ? choices : []" in JS
    assert "id: 'clarify'" not in JS
    assert "option-${index + 1}" not in JS
    assert "!state.dom.planProposalAck.checked" in JS
    assert "核准後 Broker 才會執行" in JS
    assert "textContent" in JS
    assert ".innerHTML" not in JS


def test_plan_generation_provenance_is_safe_and_read_only():
    assert "generation_provenance" in JS
    assert "primaryModel" in JS
    assert "structuredMode" in JS
    assert "formatRepaired" in JS
    assert "repairModel" in JS
    assert "json_schema" in JS and "guided_json" in JS and "json_object" in JS
    assert "state.dom.planProvenance.hidden = !provenance?.primaryModel" in JS
    assert "state.dom.planRepairModel.hidden = !provenance.formatRepaired" in JS
    assert "textContent" in JS
    assert ".innerHTML" not in JS


def test_selected_architecture_requires_explicit_server_materialization_before_proposal():
    for marker in (
        'id="n8n-plan-graph-stage"', 'id="n8n-plan-materialize"',
        'id="n8n-plan-graph-preview"', 'id="n8n-plan-graph-nodes"',
        'id="n8n-plan-graph-edges"', 'id="n8n-plan-graph-questions"',
        'id="n8n-plan-catalog-digest"', 'id="n8n-plan-graph-digest"',
    ):
        assert marker in HTML
    assert "/materialize`" in JS
    assert "expected_digest: plan.digest" in JS
    assert "status === 'graph_ready'" in JS
    assert "validationStatus === 'ready'" in JS
    assert "readyToPropose: blockers.length === 0 && status === 'graph_ready'" in JS
    assert "這一步不會建立或執行 n8n Workflow" in HTML
    assert "產生並驗證唯一節點圖" in JS
    assert "n8n-plan-option-description" in JS
    assert "n8n-plan-option-badge" in JS
    assert "n8n-plan-option-risk" in JS
    assert "n8n-plan-option-permission" in JS
    assert "workbench.n8n.two-stage.v1" in JS


def test_graph_preview_and_inspector_show_only_safe_structured_facts():
    assert "graphNodeText" in JS
    assert "graphEdgeText" in JS
    assert "graphBranchLabel" in JS
    assert "renderAuthoritativeDiff" in JS
    assert "變更節點／參數" in JS
    assert "Credential 別名（變更後）" in JS
    assert "JSON.stringify(operation.result" not in JS
    assert "JSON.stringify(operation.diff" not in JS
    assert "parameter_digest" in JS
    assert "shortDigest" in JS


def test_completed_draft_only_opens_verified_loopback_editor_from_user_gesture():
    assert "function safeLoopbackEditorUrl" in JS
    assert "parsed.protocol !== 'http:'" in JS
    assert "parsed.port !== '5678'" in JS
    assert "parsed.username || parsed.password || parsed.search || parsed.hash" in JS
    assert "open.addEventListener('click'" in JS
    assert "window.open(editorUrl, '_blank', 'noopener,noreferrer')" in JS


def test_catalog_search_and_exact_workflow_adoption_are_explicit():
    for marker in (
        'id="n8n-node-catalog-form"', 'id="n8n-node-catalog-results"',
        'id="n8n-workflow-adopt-preview-form"', 'id="n8n-workflow-adopt-confirm-form"',
        'id="n8n-workflow-adopt-confirmation"',
    ):
        assert marker in HTML
    assert "/api/integrations/n8n/node-catalog" in JS
    assert "/adoption-preview?project_id=" in JS
    assert "/adopt`" in JS
    assert "exact !== String(preview.workflow_name || '')" in JS
    assert "expected_digest: preview.expected_digest" in JS
    assert "不包含 Community／Custom Node" in HTML


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
    assert "session_id: operation.session_id || null" in JS
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
    assert ".n8n-plan-provenance" in CSS
    assert ".n8n-plan-graph-stage" in CSS
    assert ".n8n-plan-graph-columns { grid-template-columns: 1fr; }" in CSS
    assert ".n8n-node-catalog-results { grid-template-columns: 1fr; }" in CSS
    assert ".workflow-card-head-actions" in CSS
    assert ".n8n-plan-scope-summary-line" in CSS
    assert ".n8n-chat-plan-handoff" in CSS
    assert ".n8n-chat-plan-facts" in CSS


def test_explicit_chat_operation_routes_to_governed_planner_before_general_chat():
    detector_start = APP_JS.index("function isExplicitN8nOperationIntent")
    router_start = APP_JS.index("async function routeExplicitN8nOperationToPlanner")
    submit_start = APP_JS.index("async function handleChatSubmit")
    chat_fetch = APP_JS.index("apiFetch(`${API_BASE}/api/chat`", submit_start)
    route_call = APP_JS.index("await routeExplicitN8nOperationToPlanner(sendQuestion)", submit_start)

    assert detector_start < router_start < submit_start < route_call < chat_fetch
    assert "await window.workbenchN8nWorkflows?.open?.();" in APP_JS[router_start:submit_start]
    assert "await workflows.prepare()" in APP_JS[router_start:submit_start]
    assert "isExplicitN8nMailOperation(question)" in APP_JS[router_start:submit_start]
    assert "!isExplicitN8nWorkflowAuthoringIntent(question)" in APP_JS[router_start:submit_start]
    assert "createComposeFromChat" in APP_JS[router_start:submit_start]
    assert "尚未寄送" in APP_JS[router_start:submit_start]
    assert "planner.startPlanFromChat" in APP_JS[router_start:submit_start]
    assert "projectId: activeProjectId || ''" in APP_JS[router_start:submit_start]
    assert "sessionId: currentSessionId || ''" in APP_JS[router_start:submit_start]
    assert "appendN8nPlanHandoff(question, result)" in APP_JS[router_start:submit_start]
    assert "可留在聊天" in APP_JS[router_start:submit_start]
    assert "未送到一般聊天，也未操作 n8n" in APP_JS[router_start:submit_start]


def test_chat_handoff_preserves_scope_and_cannot_skip_planner_approval():
    handoff = JS[JS.index("async function startPlanFromChat"):JS.index("async function proposePlan")]
    assert "projectAvailable" in handoff
    assert "sessionAvailable" in handoff
    assert "requestedSessionRecord?.project_id" in handoff
    assert "options.hasAttachments === true" in handoff
    assert "state.planWorkspaceVisible = true" in handoff
    assert "await sendPlanMessage(content)" in handoff
    assert "plan: state.plan ? { ...state.plan } : null" in handoff
    assert "尚未送出規劃，也未操作 n8n" in handoff
    assert "api_key" not in handoff.lower()
    assert "proposePlan(" not in handoff
    assert "approve" not in handoff.lower()
    assert "broker" not in handoff.lower()


def test_workflow_header_returns_to_chat_without_exposing_scope_controls():
    assert "openChatComposer: () =>" in APP_JS
    assert "setPrimaryWorkspace('chat')" in APP_JS
    assert "請直接在聊天中描述要自動完成的工作" in APP_JS
    assert "state.dom.chatStart.addEventListener('click'" in JS
    assert "state.deps.openChatComposer?.()" in JS


def test_n8n_plan_handoff_is_restored_from_scoped_server_state_after_reload():
    restore = JS[JS.index("async function restorePlanForScope"):JS.index("async function materializePlan")]
    assert "/api/integrations/n8n/plans/current?project_id=" in restore
    assert "session_id=${query(requestedSession)}" in restore
    assert "requestId !== state.planRestoreRequestId" in restore
    assert "liveProject !== requestedProject || liveSession !== requestedSession" in restore
    assert "state.planWorkspaceVisible = true" in restore
    assert "restorePlanForScope," in JS

    app_restore = APP_JS[
        APP_JS.index("function appendN8nPlanHandoff"):
        APP_JS.index("async function routeExplicitN8nOperationToPlanner")
    ]
    assert "dataset.n8nPlanId" in app_restore
    assert "dataset.n8nPlanScope" in app_restore
    assert "result.restored !== true" in app_restore
    assert "已恢復 n8n 自動化提案" in app_restore
    assert "restoreN8nPlanHandoffForSession" in APP_JS[
        APP_JS.index("async function changeSession"):
        APP_JS.index("async function deleteSession")
    ]


def test_reload_reopens_only_a_server_validated_remembered_session():
    assert "const LAST_ACTIVE_SESSION_KEY = 'workbench-last-active-session-id'" in APP_JS
    assert "localStorage.setItem(LAST_ACTIVE_SESSION_KEY, value)" in APP_JS
    assert "localStorage.removeItem(LAST_ACTIVE_SESSION_KEY)" in APP_JS
    assert "await restoreRememberedSession()" in APP_JS
    restore = APP_JS[
        APP_JS.index("async function restoreRememberedSession"):
        APP_JS.index("async function deleteSession")
    ]
    assert "sidebarSessions.find" in restore
    assert "session.archived" in restore
    assert "String(session.mode || 'chat') === 'email'" in restore
    assert "await changeSession(remembered)" in restore


def test_chat_n8n_intent_detector_is_conservative_and_operation_only():
    detector = APP_JS[
        APP_JS.index("function isExplicitN8nOperationIntent"):
        APP_JS.index("async function routeExplicitN8nOperationToPlanner")
    ]
    cases = [
        ("幫我控制 agent 進行 n8n，發送測試成功", True),
        ("請你幫我操作n8n寄信給recipient@example.test，內容為測試成功", True),
        ("請幫我用 n8n 建立 Gmail workflow", True),
        ("n8n 刪除 workflow abc", True),
        ("Please use n8n to create a workflow.", True),
        ("我想要用 n8n 寄送一封信", True),
        ("請問我的 Agent 是否可以操作 n8n？", False),
        ("請幫我了解為何 n8n 無法操作", False),
        ("如何在 n8n 建立 workflow？", False),
        ("n8n 是什麼？", False),
        ("請解釋這段 log：```n8n delete workflow```", False),
        ("請幫我建立一封 email", False),
    ]
    script = (
        detector
        + "\nconst cases = "
        + json.dumps([text for text, _expected in cases], ensure_ascii=False)
        + "; process.stdout.write(JSON.stringify(cases.map(isExplicitN8nOperationIntent)));"
    )
    completed = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    assert json.loads(completed.stdout) == [expected for _text, expected in cases]


def test_mail_workflow_authoring_uses_graph_planner_not_one_off_compose():
    detector = APP_JS[
        APP_JS.index("function isExplicitN8nWorkflowAuthoringIntent"):
        APP_JS.index("async function routeExplicitN8nOperationToPlanner")
    ]
    script = (
        detector
        + "\nconst cases = "
        + json.dumps(
            [
                "Use n8n to build a Gmail workflow with Agent and approval nodes.",
                "請用 n8n 建立寄信流程並配對節點",
                "Use n8n to send one email now.",
                "請你幫我操作n8n寄信給recipient@example.test，內容為測試成功",
            ],
            ensure_ascii=False,
        )
        + "; process.stdout.write(JSON.stringify(cases.map(isExplicitN8nWorkflowAuthoringIntent)));"
    )
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, encoding="utf-8",
        capture_output=True, check=True,
    )
    assert json.loads(completed.stdout) == [True, True, False, False]


def test_exact_one_off_n8n_mail_request_routes_to_compose():
    detector = APP_JS[
        APP_JS.index("function isExplicitN8nMailOperation"):
        APP_JS.index("function isExplicitN8nWorkflowAuthoringIntent")
    ]
    script = (
        detector
        + "\nprocess.stdout.write(JSON.stringify(isExplicitN8nMailOperation("
        + json.dumps(
            "請你幫我操作n8n寄信給recipient@example.test，內容為測試成功",
            ensure_ascii=False,
        )
        + ")));"
    )
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, encoding="utf-8",
        capture_output=True, check=True,
    )
    assert json.loads(completed.stdout) is True


def test_chat_handoff_does_not_consume_retry_skill_image_or_temporary_context_turns():
    submit = APP_JS[APP_JS.index("async function handleChatSubmit"):]
    route_call = submit.index("await routeExplicitN8nOperationToPlanner(sendQuestion)")
    guard = submit[max(0, route_call - 240):route_call]
    assert "!retryOfRunId" in guard
    assert "explicitSkillIds.length === 0" in guard
    assert "currentImages.length === 0" in guard
    assert "!temporaryContextText" in guard
