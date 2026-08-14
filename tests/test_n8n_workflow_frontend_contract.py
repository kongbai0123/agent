from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
BASIC_MODE_JS = (FRONTEND / "basic-chat-mode.js").read_text(encoding="utf-8")
INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
WORKFLOW_JS = (FRONTEND / "n8n-workflows.js").read_text(encoding="utf-8")
STYLE_CSS = (FRONTEND / "style.css").read_text(encoding="utf-8")


def _slice(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index:source.index(end, start_index)]


def test_basic_chat_exposes_a_dedicated_workflow_workspace():
    assert 'id="rail-workflows"' in INDEX_HTML
    assert 'id="n8n-workflow-center"' in INDEX_HTML
    assert 'id="mail-profile-form"' in INDEX_HTML
    assert 'id="mail-compose-form"' in INDEX_HTML
    assert 'id="mail-profile-model"' in INDEX_HTML
    assert 'id="mail-compose-model"' in INDEX_HTML
    assert 'id="mail-runs-list"' in INDEX_HTML
    assert "rail-workflows" not in BASIC_MODE_JS
    assert "window.workbenchN8nWorkflows?.init" in APP_JS
    assert "window.workbenchN8nWorkflows?.open?.()" in APP_JS
    assert "onWorkspaceOpen: () => setPrimaryWorkspace('workflows')" in APP_JS
    assert ".workflow-center[hidden]" in STYLE_CSS


def test_single_profile_has_fixed_label_recipient_and_project_sources():
    assert INDEX_HTML.count('id="mail-profile-form"') == 1
    assert 'value="Workbench-Agent" readonly' in INDEX_HTML
    assert 'placeholder="由本機安全設定提供" readonly' in INDEX_HTML
    assert 'id="mail-profile-project"' in INDEX_HTML
    assert "const TRIGGER_LABEL = 'Workbench-Agent'" in WORKFLOW_JS
    assert "recipientConfigured: value.recipient_configured === true" in WORKFLOW_JS

    save_profile = _slice(WORKFLOW_JS, "async function saveProfile", "async function createCompose")
    assert "project_id: projectId" in save_profile
    assert "instruction," in save_profile
    assert "default_model: state.dom.profileModel.value || null" in save_profile
    assert "enabled: state.dom.profileEnabled.checked" in save_profile
    assert "auto_start: state.dom.profileAutoStart.checked" in save_profile
    assert 'id="mail-profile-auto-start"' in INDEX_HTML
    assert "預設關閉" in INDEX_HTML
    assert "mode:" not in save_profile
    assert "recipient:" not in save_profile
    assert "trigger_label:" not in save_profile
    assert "getModels: () => Array.from(modelSelect.options)" in APP_JS
    assert "getModels: options.getModels" in WORKFLOW_JS
    assert "window.workbenchN8nWorkflows?.refreshModels?.()" in APP_JS


def test_project_and_model_refresh_are_safe_before_workflow_ui_initializes():
    project_renderer = _slice(WORKFLOW_JS, "function renderProjects", "function renderModelSelect")
    model_renderer = _slice(WORKFLOW_JS, "function renderModels", "function renderService")
    assert "if (!state.initialized || !state.dom?.profileProject) return;" in project_renderer
    assert "if (!state.initialized || !state.dom?.profileModel || !state.dom?.composeModel) return;" in model_renderer


def test_profile_and_recipient_contracts_fail_closed_when_fields_are_missing():
    profile_parser = _slice(WORKFLOW_JS, "function profileFrom", "function runIdOf")
    run_parser = _slice(WORKFLOW_JS, "function runFrom", "function safeEditorUrl")
    assert "value.fixed_recipient).trim().toLowerCase()" in profile_parser
    assert "value.fixed_recipient || LOCKED_RECIPIENT" not in profile_parser
    assert "value.draft?.recipient).trim().toLowerCase()" in run_parser
    assert "value.draft?.recipient || LOCKED_RECIPIENT" not in run_parser
    assert "未回報（已停用核准）" in WORKFLOW_JS


def test_reply_and_compose_editing_respect_locked_fields():
    assert "const mode = ['compose', 'new', 'new_mail', 'outbound'].includes(rawMode) ? 'compose' : 'reply'" in WORKFLOW_JS
    assert "subject.disabled = run.mode === 'reply' || !runCanEdit(run)" in WORKFLOW_JS
    assert "body.disabled = !runCanEdit(run)" in WORKFLOW_JS
    assert "runRecipientMatchesProfile(run)" in WORKFLOW_JS
    assert "附件（鎖定、僅 metadata）" in WORKFLOW_JS
    assert "Workbench 不會自動把附件加入外寄郵件" in WORKFLOW_JS

    compose = _slice(WORKFLOW_JS, "async function createCompose", "async function serviceAction")
    assert "const composePayload = {" in compose
    assert "...(subject ? { subject } : {})" in compose
    assert "...(model ? { model } : {})" in compose
    assert "body: JSON.stringify(composePayload)" in compose
    assert "if (!content) return { status: 'blocked'" in compose
    assert "recipient:" not in compose
    assert "project_id" not in compose
    assert "thread" not in compose
    assert "attachments" not in compose


def test_approval_mutations_require_revision_and_sha_digest():
    assert "expected_revision: run.draft.revision" in WORKFLOW_JS
    assert "expected_sha256: run.draft.sha256" in WORKFLOW_JS
    assert "Number.isInteger(run?.draft?.revision)" in WORKFLOW_JS
    assert "/^[a-f0-9]{64}$/.test(run.draft.sha256)" in WORKFLOW_JS
    assert "draft.content_sha256" in WORKFLOW_JS
    assert "approval_expired" in WORKFLOW_JS
    assert "const PENDING_STATUSES = new Set(['awaiting_approval', 'waiting_approval', 'pending_approval'])" in WORKFLOW_JS
    assert "approved_queued" not in _slice(WORKFLOW_JS, "function runCanEdit", "function renderMailExecution")
    for action in ("approve", "reject", "regenerate"):
        assert f"mutateDraft('{action}'" in WORKFLOW_JS
    assert "method: 'PATCH'" in WORKFLOW_JS
    assert "草稿安全契約不完整或收件者不符" in WORKFLOW_JS


def test_n8n_editor_is_external_and_exactly_allowlisted():
    assert "const EDITOR_URL = 'http://127.0.0.1:5678/'" in WORKFLOW_JS
    safe_url = _slice(WORKFLOW_JS, "function safeEditorUrl", "function renderProjects")
    assert "url.href !== EDITOR_URL" in safe_url
    assert "url.protocol !== 'http:'" in safe_url
    assert "url.hostname !== '127.0.0.1'" in safe_url
    assert "url.port !== '5678'" in safe_url
    assert "url.username || url.password || url.search || url.hash || url.pathname !== '/'" in safe_url
    assert "window.open(url, '_blank', 'noopener,noreferrer')" in WORKFLOW_JS
    assert "iframe" not in WORKFLOW_JS.lower()


def test_frontend_uses_only_the_narrow_mail_and_service_apis():
    for route in (
        "/api/integrations/n8n/status",
        "/api/integrations/n8n/mail-profile",
        "/api/integrations/n8n/mail/compose",
        "/api/integrations/n8n/mail-runs",
        "/api/integrations/n8n/mail-drafts/",
        "/api/integrations/n8n/mail-threads/",
        "/api/integrations/n8n/events",
    ):
        assert route in WORKFLOW_JS
    assert "serviceAction('start'" in WORKFLOW_JS
    assert "serviceAction('stop'" in WORKFLOW_JS
    assert "new EventSource(apiPath('/api/integrations/n8n/events'))" in WORKFLOW_JS
    assert "source.addEventListener('status', handleEvent)" in WORKFLOW_JS
    assert "state.eventRetryTimer = window.setTimeout(connectEvents, 5000)" in WORKFLOW_JS
    assert "/open" not in WORKFLOW_JS


def test_workflow_workspace_starts_n8n_on_demand_only_after_safe_status_probe():
    ensure = _slice(WORKFLOW_JS, "async function ensureServiceForWorkspace", "function open()")
    assert "await refreshService()" in ensure
    assert "service.installed !== true" in ensure
    assert "service.isolation_ready !== true" in ensure
    assert "request('/api/integrations/n8n/start', { method: 'POST' })" in ensure
    open_workspace = _slice(WORKFLOW_JS, "function open()", "function startBackgroundSync")
    assert "await ensureServiceForWorkspace()" in open_workspace
    assert "const ready = (async () =>" in open_workspace
    assert "return ready" in open_workspace


def test_chat_email_handoff_creates_only_a_fixed_recipient_draft_before_approval():
    compose = _slice(WORKFLOW_JS, "async function createComposeDraft", "async function serviceAction")
    assert "mentionedRecipients" in compose
    assert "value !== state.profile.recipient" in compose
    assert "request('/api/integrations/n8n/mail/compose'" in compose
    assert "recipient:" not in compose
    assert "status: 'draft_created'" in compose
    assert "send" not in compose.lower()
    assert "approve" not in compose.lower()
    assert "createComposeFromChat" in WORKFLOW_JS


def test_background_events_only_refresh_badges_and_never_hijack_chat():
    connect_events = _slice(WORKFLOW_JS, "function connectEvents", "function open()")
    assert "scheduleRunsRefresh()" in connect_events
    assert "openRun(" not in connect_events
    assert "selectTab(" not in connect_events
    assert 'id="mail-approval-badge"' in INDEX_HTML
    assert 'id="output-tab-execution-badge"' in INDEX_HTML
    assert "approvalBadge: byId('mail-approval-badge')" in WORKFLOW_JS
    assert "chatExecution: byId('run-execution-content')" in WORKFLOW_JS
    assert "mailExecution: byId('mail-inspector-execution')" in WORKFLOW_JS


def test_profile_changes_and_stale_state_disable_compose_until_authoritative_refresh():
    assert "function composeAllowed()" in WORKFLOW_JS
    assert "state.profileDirty !== true" in WORKFLOW_JS
    assert "state.profileSaving !== true" in WORKFLOW_JS
    assert "function markProfileDirty()" in WORKFLOW_JS
    assert "if (!composeAllowed())" in WORKFLOW_JS
    assert "if (state.profileSaving) return" in WORKFLOW_JS
    assert "requestId !== state.profileRequestId" in WORKFLOW_JS
    assert "await refreshProfile({ preserveDirty: false })" in WORKFLOW_JS
    assert "refreshService(), refreshProfile(), refreshRuns({ quiet: true })" in WORKFLOW_JS


def test_pending_mail_is_keyboard_accessible_through_the_execution_tab():
    assert 'id="mail-approval-badge" aria-hidden="true"' in INDEX_HTML
    assert "state.dom.executionTab.setAttribute('aria-label', executionLabel)" in WORKFLOW_JS
    assert "state.dom.executionTab.addEventListener('click', openPendingFromExecution" in WORKFLOW_JS
    assert "state.dom.executionTab.addEventListener('keydown', openPendingFromExecution" in WORKFLOW_JS
    assert "!['Enter', ' '].includes(event.key)" in WORKFLOW_JS
    assert "event.stopImmediatePropagation()" in WORKFLOW_JS
    assert "void openRun(pending.id)" in WORKFLOW_JS


def test_sse_refreshes_only_when_service_or_mail_revision_changes():
    assert "function serviceEventSignature(payload = {})" in WORKFLOW_JS
    assert "function handleStatusSnapshot(payload = {})" in WORKFLOW_JS
    assert "nextServiceSignature !== state.eventServiceSignature" in WORKFLOW_JS
    assert "nextMailRevision !== state.eventMailRevision" in WORKFLOW_JS
    status_branch = _slice(WORKFLOW_JS, "if (event.type === 'status')", "} else if (payload.type")
    assert "handleStatusSnapshot(payload)" in status_branch
    assert "refreshService()" not in status_branch
    assert "scheduleRunsRefresh()" not in status_branch


def test_generation_failure_is_a_terminal_error_state():
    assert "generation_failed: '草稿生成失敗'" in WORKFLOW_JS
    assert "['failed', 'generation_failed', 'delivery_unknown', 'blocked_recipient']" in WORKFLOW_JS
    terminal_line = next(line for line in WORKFLOW_JS.splitlines() if "const TERMINAL_STATUSES" in line)
    assert "generation_failed" in terminal_line


def test_unknown_delivery_is_terminal_high_risk_with_one_confirmed_recovery_action():
    terminal_line = next(line for line in WORKFLOW_JS.splitlines() if "const TERMINAL_STATUSES" in line)
    assert "delivery_unknown" in terminal_line
    assert "delivery_unknown: '寄送結果不明（高風險）'" in WORKFLOW_JS
    assert "['failed', 'generation_failed', 'delivery_unknown', 'blocked_recipient']" in WORKFLOW_JS
    assert "function canResolveUnknownDelivery(run)" in WORKFLOW_JS
    assert "run?.status === 'delivery_unknown'" in WORKFLOW_JS
    assert "unknownDelivery ? '確認 Gmail 未寄出後重新生成' : '重新生成'" in WORKFLOW_JS
    assert "if (unknownDelivery) actions.append(regenerate)" in WORKFLOW_JS
    assert "else actions.append(save, regenerate, reject, approve)" in WORKFLOW_JS
    assert "if (run.bindingId && !unknownDelivery)" in WORKFLOW_JS
    assert "function confirmUnknownDeliveryRegeneration(button)" in WORKFLOW_JS
    assert "const confirmed = window.confirm(" in WORKFLOW_JS
    assert "我已確認 Gmail 未寄出，繼續重新生成？" in WORKFLOW_JS
    assert "await mutateDraft('regenerate', button)" in WORKFLOW_JS
    assert "action === 'regenerate' && run?.status === 'delivery_unknown'" in WORKFLOW_JS


def test_untrusted_mail_content_is_rendered_with_safe_dom_operations():
    assert ".innerHTML" not in WORKFLOW_JS
    assert "insertAdjacentHTML" not in WORKFLOW_JS
    assert "document.write" not in WORKFLOW_JS
    assert "node.textContent = string(text)" in WORKFLOW_JS
    assert "replaceChildren" in WORKFLOW_JS


def test_binding_delete_copy_explains_its_non_destructive_scope():
    assert "不會刪除 Gmail 郵件、Thread 或附件" in WORKFLOW_JS
    assert "不再沿用此 Workbench 關聯" in WORKFLOW_JS
    assert "method: 'DELETE'" in WORKFLOW_JS


def test_workflow_ui_preserves_typography_responsive_and_accessibility_contracts():
    assert "font-size: 12px" in _slice(STYLE_CSS, ".workflow-center {", ".mail-inspector-head")
    assert "@media (max-width: 760px)" in STYLE_CSS
    assert "@media (max-width: 520px)" in STYLE_CSS
    assert 'aria-live="polite"' in INDEX_HTML
    assert 'aria-current' in APP_JS
    assert "state.dom.title.focus()" in WORKFLOW_JS
