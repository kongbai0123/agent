import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
BASIC_MODE_JS = (FRONTEND / "basic-chat-mode.js").read_text(encoding="utf-8")
INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
WORKFLOW_JS = (FRONTEND / "n8n-workflows.js").read_text(encoding="utf-8")
EXTENSION_JS = (FRONTEND / "extension-center.js").read_text(encoding="utf-8")
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
    assert 'id="workflow-extension-manage"' in INDEX_HTML
    assert 'id="workflow-extension-gate"' in INDEX_HTML
    assert 'id="workflow-enabled-content"' in INDEX_HTML
    for control_id in (
        "workflow-command-title",
        "workflow-command-detail",
        "workflow-service-start",
        "workflow-service-stop",
        "workflow-service-open",
        "workflow-service-settings",
    ):
        assert f'id="{control_id}"' in INDEX_HTML
    assert 'workflow-service-card' not in INDEX_HTML
    assert 'id="workflow-service-metrics"' not in INDEX_HTML
    assert "rail-workflows" not in BASIC_MODE_JS
    assert "window.workbenchN8nWorkflows?.init" in APP_JS
    assert "window.workbenchN8nWorkflows?.open?.()" in APP_JS
    assert "onWorkspaceOpen: () => setPrimaryWorkspace('workflows')" in APP_JS
    assert ".workflow-center[hidden]" in STYLE_CSS


def test_workflow_header_keeps_its_natural_height_and_does_not_clip_copy():
    header = _slice(STYLE_CSS, ".workflow-center-header {", ".workflow-header-main")
    assert "min-height: 0" in header
    assert "flex: 0 0 auto" in header
    assert "align-items: flex-start" in header
    body = _slice(STYLE_CSS, ".workflow-center-body {", ".workflow-enabled-content")
    assert "min-height: 0" in body
    assert "flex: 1 1 auto" in body


def test_workflow_hmi_directly_controls_the_real_managed_n8n_service():
    command_bar = _slice(WORKFLOW_JS, "function renderServiceCommandBar", "function renderService() {")
    assert "n8n 已就緒，等待啟動" in command_bar
    assert "按下「啟動 n8n」即可直接啟動本機服務" in command_bar
    assert "state.dom.serviceStart.disabled" in command_bar
    assert "service.isolation_ready === true" in command_bar
    assert "safeEditorUrl(service.editor_url)" in command_bar

    service_action = _slice(WORKFLOW_JS, "async function serviceAction", "function openEditor")
    assert "request(`/api/integrations/n8n/${action}`, { method: 'POST' })" in service_action
    assert "action === 'start'" in service_action
    assert "starting: true" in service_action

    wiring = _slice(WORKFLOW_JS, "function init(options", "window.workbenchN8nWorkflows")
    assert "serviceAction('start', state.dom.serviceStart)" in wiring
    assert "serviceAction('stop', state.dom.serviceStop)" in wiring
    assert "state.dom.serviceOpen.addEventListener('click', openEditor)" in wiring


def test_workflow_uses_chat_as_the_creation_entry_and_defers_advanced_settings():
    for copy in (
        "在聊天中建立自動化",
        "回到聊天描述需求",
        "檢查提案並批准",
        "檢查助理建議的流程",
        "需求由目前聊天帶入",
        "送出補充",
    ):
        assert copy in INDEX_HTML
    assert "想讓 n8n 幫你做什麼？" not in INDEX_HTML
    assert "送出需求" not in INDEX_HTML
    assert 'id="n8n-plan-workspace" aria-labelledby="n8n-plan-title" hidden' in INDEX_HTML
    assert 'id="n8n-plan-form" class="workflow-form n8n-plan-form" hidden' in INDEX_HTML
    assert 'id="n8n-plan-scope"' not in INDEX_HTML
    assert 'id="n8n-plan-scope-summary"' in INDEX_HTML
    assert '<details class="n8n-progressive-panel" id="n8n-advanced-settings">' in INDEX_HTML
    assert '<details class="n8n-progressive-panel" id="n8n-gmail-settings">' in INDEX_HTML
    assert 'id="n8n-advanced-settings" open' not in INDEX_HTML
    assert 'id="n8n-gmail-settings" open' not in INDEX_HTML
    assert "n8n-plan-layout:has(.n8n-plan-impact:not([hidden]))" in STYLE_CSS


def test_workflow_can_prepare_in_background_without_switching_workspace():
    prepare = _slice(WORKFLOW_JS, "async function prepare()", "function open()")
    assert "await refreshExtensionState()" in prepare
    assert "await ensureServiceForWorkspace()" in prepare
    assert "refreshProfile()" in prepare
    assert "refreshRuns()" in prepare
    open_workspace = _slice(WORKFLOW_JS, "function open()", "function startBackgroundSync()")
    assert "state.deps.onWorkspaceOpen?.()" in open_workspace
    assert "return prepare()" in open_workspace
    assert "prepare," in WORKFLOW_JS[WORKFLOW_JS.index("window.workbenchN8nWorkflows ="):]


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
    open_editor = _slice(WORKFLOW_JS, "function openEditor", "function scheduleRunsRefresh")
    assert "if (!n8nExtensionReady())" in open_editor
    assert "state.service?.running === true || state.service?.reachable === true" in open_editor
    assert "window.open(url, '_blank', 'noopener,noreferrer')" in open_editor
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
    assert "/api/extensions" in WORKFLOW_JS
    assert "manageService: serviceAction" in WORKFLOW_JS
    assert "openEditor," in WORKFLOW_JS
    assert "runN8nServiceAction('start'" in EXTENSION_JS
    assert "runN8nServiceAction('stop'" in EXTENSION_JS
    assert "new EventSource(apiPath('/api/integrations/n8n/events'))" in WORKFLOW_JS
    assert "source.addEventListener('status', handleEvent)" in WORKFLOW_JS
    assert "state.eventRetryTimer = window.setTimeout(connectEvents, 5000)" in WORKFLOW_JS
    assert "/open" not in WORKFLOW_JS


def test_workflow_workspace_starts_n8n_on_demand_only_after_safe_status_probe():
    ensure = _slice(WORKFLOW_JS, "async function ensureServiceForWorkspace", "function open()")
    assert "if (!n8nExtensionReady()) return" in ensure
    assert "await refreshService()" in ensure
    assert "service.installed !== true" in ensure
    assert "service.isolation_ready !== true" in ensure
    assert "request('/api/integrations/n8n/start', { method: 'POST' })" in ensure
    prepare = _slice(WORKFLOW_JS, "async function prepare()", "function open()")
    assert "await refreshExtensionState()" in prepare
    assert "if (!n8nExtensionReady())" in prepare
    assert "await ensureServiceForWorkspace()" in prepare
    open_workspace = _slice(WORKFLOW_JS, "function open()", "function startBackgroundSync")
    assert "state.deps.onWorkspaceOpen?.()" in open_workspace
    assert "return prepare()" in open_workspace


def test_workflow_header_uses_extension_state_while_details_keep_core_service_controls():
    renderer = _slice(WORKFLOW_JS, "function renderService", "function renderProfile")
    assert "extension.installed === true" in renderer
    assert "extension.effective_enabled === true" in renderer
    assert "state.dom.extensionGate.hidden = extensionEnabled" in renderer
    assert "state.dom.enabledContent.hidden = !extensionEnabled" in renderer
    assert "service.load_error" in renderer
    assert "workflow-status-pill is-error" in renderer
    assert "Gmail Workflow 已就緒" in renderer
    for label in ("本機 n8n 服務", "工作流程管理", "Gmail 工作流程", "在瀏覽器開啟", "技術資訊"):
        assert label in EXTENSION_JS


def test_service_status_updates_are_published_for_the_open_extension_detail():
    assert "workbench:n8n-service-state" in WORKFLOW_JS
    refresh = _slice(WORKFLOW_JS, "async function refreshService", "async function refreshAll")
    assert "publishServiceState()" in refresh
    events = _slice(WORKFLOW_JS, "function handleStatusSnapshot", "function connectEvents")
    assert "publishServiceState()" in events


def test_chat_email_handoff_creates_only_a_fixed_recipient_draft_before_approval():
    compose_gate = _slice(WORKFLOW_JS, "function composeAllowed", "function syncComposeGate")
    assert "n8nExtensionReady()" in compose_gate
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


def test_mail_inspector_async_render_requires_current_workspace_owner_and_project():
    helpers = _slice(WORKFLOW_JS, "function workflowWorkspaceActive", "function element")
    assert "claimContentOwner" in helpers
    assert "contentOwnerMatches" in helpers
    assert "mail:${string(runId).trim()}" in helpers

    load_run = _slice(WORKFLOW_JS, "async function loadRun", "async function openRun")
    assert "openInspector ? claimMailInspector(runId) : state.inspectorLease" in load_run
    assert "lease.owner !== mailOwner(runId)" in load_run
    assert load_run.count("!ownsMailInspector(lease)") >= 3
    assert "selectedRun.projectId !== authoritativeProject" in load_run
    assert "useChatInspectorContext({ open: false })" in load_run

    context = _slice(WORKFLOW_JS, "function useChatInspectorContext", "async function confirmUnknown")
    assert "state.inspectorLease = null" in context
    assert "claimContentOwner?.('chat')" in context
    close = _slice(WORKFLOW_JS, "function close()", "function init(options")
    assert "useChatInspectorContext({ open: false })" in close


def test_delayed_mail_response_cannot_reclaim_an_inspector_owned_by_another_controller():
    load_run = _slice(WORKFLOW_JS, "async function loadRun", "async function openRun")
    script = r"""
let generation = 0;
let currentOwner = {owner: 'chat', generation: 0};
const manager = {
  claim(owner) { currentOwner = {owner, generation: ++generation}; return {...currentOwner}; },
  matches(lease) { return lease?.owner === currentOwner.owner && lease?.generation === currentOwner.generation; },
};
const state = {
  initialized: true,
  selectedRun: null,
  selectedRequestId: 0,
  inspectorLease: null,
  profile: {projectId: 'project-a'},
  deps: {showToast: () => { toasts += 1; }},
  dom: {
    center: {hidden: false},
    mailExecution: {replaceChildren() {}}, mailResults: {replaceChildren() {}},
    chatExecution: {hidden: false}, chatResults: {hidden: false},
  },
};
global.window = {workbenchRunInspector: {selectTab() {}}};
const string = value => String(value == null ? '' : value);
const encoded = value => encodeURIComponent(string(value));
const mailOwner = runId => `mail:${string(runId).trim()}`;
const workflowWorkspaceActive = () => state.initialized && state.dom.center.hidden === false;
const claimMailInspector = runId => {
  if (!workflowWorkspaceActive() || !runId) return null;
  state.inspectorLease = manager.claim(mailOwner(runId));
  return state.inspectorLease;
};
const ownsMailInspector = lease => !!lease && workflowWorkspaceActive() && manager.matches(lease);
const empty = message => ({message});
const runFrom = value => ({id: value.id, projectId: value.project_id});
let requestCount = 0;
let resolvers = [];
const request = () => { requestCount += 1; return new Promise(resolve => resolvers.push(resolve)); };
let executionRenders = 0;
let resultRenders = 0;
let shows = 0;
let toasts = 0;
let chatRestores = 0;
const renderMailExecution = () => { executionRenders += 1; };
const renderMailResults = () => { resultRenders += 1; };
const showMailInspector = lease => { if (ownsMailInspector(lease)) shows += 1; };
const useChatInspectorContext = () => {
  chatRestores += 1;
  state.selectedRun = null;
  state.inspectorLease = null;
  manager.claim('chat');
};
""" + load_run + r"""
(async () => {
  const stale = loadRun('mail-1', {openInspector: true});
  manager.claim('operation:operation-1');
  resolvers.shift()({id: 'mail-1', project_id: 'project-a'});
  await stale;

  const wrongProject = loadRun('mail-1', {openInspector: true});
  resolvers.shift()({id: 'mail-1', project_id: 'project-b'});
  await wrongProject;

  state.inspectorLease = manager.claim('operation:operation-2');
  await loadRun('mail-1', {openInspector: false});

  const current = loadRun('mail-1', {openInspector: true});
  resolvers.shift()({id: 'mail-1', project_id: 'project-a'});
  await current;
  process.stdout.write(JSON.stringify({
    requestCount, executionRenders, resultRenders, shows, toasts, chatRestores,
    selectedProject: state.selectedRun?.projectId || null,
  }));
})();
"""
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, encoding="utf-8",
        capture_output=True, check=True,
    )
    assert json.loads(completed.stdout) == {
        "requestCount": 3,
        "executionRenders": 1,
        "resultRenders": 1,
        "shows": 1,
        "toasts": 1,
        "chatRestores": 1,
        "selectedProject": "project-a",
    }


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


def test_pending_mail_never_hijacks_the_project_scoped_execution_tab():
    assert 'id="mail-approval-badge" aria-hidden="true"' in INDEX_HTML
    assert "state.dom.executionTab.setAttribute('aria-label', executionLabel)" in WORKFLOW_JS
    assert "可從工作流程中心開啟" in WORKFLOW_JS
    assert "openPendingFromExecution" not in WORKFLOW_JS
    assert "executionTab.addEventListener" not in WORKFLOW_JS
    assert "stopImmediatePropagation" not in WORKFLOW_JS


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
