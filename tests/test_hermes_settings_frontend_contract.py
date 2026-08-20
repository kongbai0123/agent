import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HERMES_SETTINGS_JS = (ROOT / "frontend" / "hermes-settings.js").read_text(
    encoding="utf-8"
)
INDEX_HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")


def test_module_is_independently_mountable_and_exposes_host_hooks():
    assert "async function init(options = {})" in HERMES_SETTINGS_JS
    assert "resolveContainer(options.container)" in HERMES_SETTINGS_JS
    assert "window.HermesSettings = publicApi" in HERMES_SETTINGS_JS
    assert "window.workbenchHermesSettings = publicApi" in HERMES_SETTINGS_JS
    for hook in (
        "refresh",
        "refreshStatus",
        "save",
        "rollback",
        "probe",
        "getState",
        "destroy",
    ):
        assert f"        {hook}," in HERMES_SETTINGS_JS


def test_module_uses_required_settings_status_and_probe_endpoints():
    assert "request('/api/settings')" in HERMES_SETTINGS_JS
    assert "request('/api/settings', {" in HERMES_SETTINGS_JS
    assert "request('/api/hermes/status')" in HERMES_SETTINGS_JS
    assert "request('/api/hermes/probe', {" in HERMES_SETTINGS_JS
    assert HERMES_SETTINGS_JS.count("method: 'POST'") >= 2
    assert "headers: { 'Content-Type': 'application/json' }" in HERMES_SETTINGS_JS


def test_settings_payload_preserves_other_workbench_settings_and_has_all_controls():
    assert "...state.settings," in HERMES_SETTINGS_JS
    for setting in (
        "hermes_enabled",
        "hermes_base_url",
        "hermes_api_key_env",
        "hermes_rollout_mode",
        "hermes_rollout_percentage",
        "hermes_canary_session_ids",
        "hermes_tools_enabled",
    ):
        assert setting in HERMES_SETTINGS_JS
    for mode in ("disabled", "canary", "percentage", "all"):
        assert f"['{mode}'," in HERMES_SETTINGS_JS


def test_external_api_data_uses_safe_dom_rendering_only():
    for unsafe_sink in (
        ".innerHTML",
        ".outerHTML",
        "insertAdjacentHTML",
        "document.write",
    ):
        assert unsafe_sink not in HERMES_SETTINGS_JS
    assert ".textContent" in HERMES_SETTINGS_JS
    assert ".replaceChildren" in HERMES_SETTINGS_JS
    assert "statusEntries(state.status)" in HERMES_SETTINGS_JS


def test_each_hermes_field_uses_an_accessible_bulleted_hover_help():
    for contract in (
        "function fieldTitle(id, labelText, helpContent = [])",
        "hermes-field-title-row",
        "hermes-field-help-disclosure",
        "hermes-field-help-trigger",
        "helpIcon.dataset.lucide = 'lightbulb'",
        "toggle.setAttribute('aria-describedby', helpId)",
        "help.setAttribute('role', 'tooltip')",
        "element('ul', 'hermes-field-help-list')",
        "list.appendChild(element('li', '', item))",
    ):
        assert contract in HERMES_SETTINGS_JS
    assert "toggle.addEventListener('click'" not in HERMES_SETTINGS_JS
    for portal_contract in (
        "function bindFieldHelp(toggle, help, disclosure)",
        "document.body.appendChild(help)",
        "surfaceRect.right - width - 8",
        "scrollSurface?.addEventListener('scroll', close, { once: true })",
        "closeActiveFieldHelp()",
    ):
        assert portal_contract in HERMES_SETTINGS_JS
    assert "help.hidden" not in HERMES_SETTINGS_JS
    for selector in (
        ".hermes-field-title-row",
        ".hermes-field-help-disclosure",
        ".hermes-field-help-trigger",
        ".hermes-field-help",
        ".hermes-field-help-list",
    ):
        assert selector in STYLE_CSS
    assert ".hermes-field-help-disclosure:hover .hermes-field-help" in STYLE_CSS
    assert ".hermes-field-help-disclosure:focus-within .hermes-field-help" in STYLE_CSS
    assert ".hermes-settings-field:has(.hermes-field-help-trigger:hover)" in STYLE_CSS
    assert "position: fixed" in STYLE_CSS
    assert "z-index: calc(var(--z-modal) + 1)" in STYLE_CSS
    assert '.hermes-field-help[data-open="true"]' in STYLE_CSS
    assert "visibility: hidden" in STYLE_CSS
    assert 'hermes-settings.js?v=1.1.3-tooltip-layer' in INDEX_HTML


def test_secret_is_never_collected_or_persisted_by_the_panel():
    assert "API Key 環境變數名稱" in HERMES_SETTINGS_JS
    assert "不顯示、不接收也不儲存 secret" in HERMES_SETTINGS_JS
    assert "delete payload.hermes_api_key;" in HERMES_SETTINGS_JS
    assert "delete payload.hermes_api_key_secret;" in HERMES_SETTINGS_JS
    for secret_name in (
        "hermes_api_key",
        "hermes_api_key_secret",
        "hermes_api_key_ref",
        "hermes_bearer_token",
        "hermes_password",
        "hermes_secret",
        "hermes_token",
    ):
        assert f"delete payload.{secret_name};" in HERMES_SETTINGS_JS
    assert "input('hermes-setting-api-key-env')" in HERMES_SETTINGS_JS
    assert "input('hermes-setting-api-key', 'password')" not in HERMES_SETTINGS_JS


def test_tools_are_disabled_by_default_and_require_exact_live_docker_attestation():
    assert "toolsEnabled: false" in HERMES_SETTINGS_JS
    assert "function hasVerifiedReadonlyToolPolicy(status = state.status)" in HERMES_SETTINGS_JS
    assert "status.deployment_mode === 'docker'" in HERMES_SETTINGS_JS
    assert "status.tool_policy_profile === READONLY_TOOL_POLICY_PROFILE" in HERMES_SETTINGS_JS
    assert "dockerAttestation.verified === true" in HERMES_SETTINGS_JS
    assert "const explicitCanary = persistedRolloutMode() === 'canary'" in HERMES_SETTINGS_JS
    assert "|| !explicitCanary" in HERMES_SETTINGS_JS
    assert "state.refs.toolsEnabled.dataset.hermesToolsUnavailable = String(!eligible)" in HERMES_SETTINGS_JS
    assert "if (!eligible) state.refs.toolsEnabled.checked = false" in HERMES_SETTINGS_JS
    assert "hermes_tools_enabled: false" not in HERMES_SETTINGS_JS
    assert "工具保持關閉" in HERMES_SETTINGS_JS
    assert "Docker 唯讀專案政策" in HERMES_SETTINGS_JS
    assert "toolsWarning.dataset.hermesOsIsolationWarning" in HERMES_SETTINGS_JS
    assert "dataset.hermesDockerAttestation" in HERMES_SETTINGS_JS
    assert "role', 'note'" in HERMES_SETTINGS_JS


def test_tools_payload_is_exact_readonly_capability_and_preserves_backend_project():
    assert "const READONLY_TOOL_CAPABILITY = 'hermes.project.read'" in HERMES_SETTINGS_JS
    assert "hermes_tools_enabled: toolsEnabled" in HERMES_SETTINGS_JS
    assert "hermes_allowed_capabilities: toolsEnabled" in HERMES_SETTINGS_JS
    assert "? [READONLY_TOOL_CAPABILITY]" in HERMES_SETTINGS_JS
    assert "state.settings.hermes_readonly_project_id || ''" in HERMES_SETTINGS_JS
    assert "hermes_readonly_project_id: readonlyProjectId" in HERMES_SETTINGS_JS
    assert "hermes-setting-readonly-project-id" not in HERMES_SETTINGS_JS
    assert "state.refs.readonlyProjectId" not in HERMES_SETTINGS_JS


def test_tools_toggle_and_payload_follow_verified_status_at_runtime():
    script = r"""
const fs = require('fs');
const vm = require('vm');

const elements = new Map();
class FakeElement {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.children = [];
        this.dataset = {};
        this.attributes = {};
        this.listeners = {};
        this.className = '';
        this.textContent = '';
        this.value = '';
        this.checked = false;
        this.disabled = false;
        this.hidden = false;
        this.style = {
            setProperty(name, value) { this[name] = value; },
            removeProperty(name) { delete this[name]; },
        };
    }
    set id(value) {
        this._id = String(value);
        if (this._id) elements.set(this._id, this);
    }
    get id() { return this._id || ''; }
    append(...nodes) {
        nodes.forEach(node => { node.parentElement = this; });
        this.children.push(...nodes);
    }
    appendChild(node) {
        node.parentElement = this;
        this.children.push(node);
        return node;
    }
    replaceChildren(...nodes) {
        nodes.forEach(node => { node.parentElement = this; });
        this.children = [...nodes];
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    addEventListener(name, listener) { this.listeners[name] = listener; }
    getBoundingClientRect() {
        if (this.className === 'hermes-field-help') {
            return { left: 0, right: 320, top: 0, bottom: 110, width: 320, height: 110 };
        }
        return { left: 344, right: 372, top: 190, bottom: 218, width: 28, height: 28 };
    }
}

const fakeBody = new FakeElement('body');
global.document = {
    body: fakeBody,
    createElement: tag => new FakeElement(tag),
    querySelector: selector => selector.startsWith('#')
        ? elements.get(selector.slice(1)) || null
        : null,
};
global.window = {
    API_BASE: '',
    innerWidth: 750,
    innerHeight: 516,
    addEventListener() {},
    removeEventListener() {},
};
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'), {
    filename: process.argv[1],
});

function response(payload) {
    return {
        ok: true,
        status: 200,
        async json() { return payload; },
    };
}

const verified = {
    deployment_mode: 'docker',
    tool_policy_profile: 'project-readonly-v1',
    docker_attestation: { verified: true },
};
const invalidStatuses = [
    { ...verified, deployment_mode: 'native' },
    { ...verified, tool_policy_profile: 'other-profile' },
    { ...verified, docker_attestation: { verified: false } },
];

(async () => {
    let settings = {
        hermes_enabled: true,
        hermes_base_url: 'http://127.0.0.1:8642',
        hermes_api_key_env: 'HERMES_API_SERVER_KEY',
        hermes_rollout_mode: 'canary',
        hermes_rollout_percentage: 0,
        hermes_canary_session_ids: ['session-canary'],
        hermes_tools_enabled: false,
        hermes_allowed_capabilities: ['untrusted.capability'],
        hermes_readonly_project_id: 'project-canary',
    };
    let activeStatus = verified;
    let savedPayload = null;
    let rollbackCalls = 0;
    let confirmationCopy = '';
    const apiFetch = async (url, options = {}) => {
        if (url.endsWith('/api/hermes/rollout/rollback') && options.method === 'POST') {
            rollbackCalls += 1;
            settings = {
                ...settings,
                hermes_rollout_mode: 'disabled',
                hermes_rollout_percentage: 0,
                hermes_canary_session_ids: [],
                hermes_tools_enabled: false,
                hermes_allowed_capabilities: [],
                hermes_readonly_project_id: '',
            };
            return response({ success: true });
        }
        if (url.endsWith('/api/settings') && options.method === 'POST') {
            savedPayload = JSON.parse(options.body);
            settings = { ...savedPayload };
            return response({ settings });
        }
        if (url.endsWith('/api/settings')) return response({ settings });
        if (url.endsWith('/api/hermes/status')) return response({ status: activeStatus });
        throw new Error(`Unexpected request: ${url}`);
    };

    const container = new FakeElement('div');
    await window.HermesSettings.init({
        container,
        apiFetch,
        confirmAction: message => {
            confirmationCopy = String(message);
            return true;
        },
    });
    const toggle = elements.get('hermes-setting-tools-enabled');
    const rolloutMode = elements.get('hermes-setting-rollout-mode');
    const rolloutPercentage = elements.get('hermes-setting-rollout-percentage');
    const rolloutHelpTrigger = elements.get('hermes-setting-rollout-mode-help-trigger');
    const rolloutHelp = elements.get('hermes-setting-rollout-mode-help');
    const optionFor = (control, value) => control.children.find(
        option => option.value === String(value),
    );
    if (!rolloutHelpTrigger || !rolloutHelp) {
        throw new Error('rollout hover help must exist beside its title');
    }
    const rolloutHelpList = rolloutHelp.children[0];
    if (rolloutHelpList?.tagName !== 'UL' || rolloutHelpList.children.length !== 3
        || !rolloutHelpList.children.every(item => item.tagName === 'LI')) {
        throw new Error('rollout help must use a three-item bulleted list');
    }
    if (rolloutHelp.attributes.role !== 'tooltip'
        || rolloutHelpTrigger.attributes['aria-describedby'] !== rolloutHelp.id
        || rolloutHelpTrigger.listeners.click) {
        throw new Error('rollout help must be hover/focus driven without click state');
    }
    if (rolloutHelp.parentElement !== rolloutHelpTrigger.parentElement
        || rolloutHelp.parentElement?.className !== 'hermes-field-help-disclosure') {
        throw new Error('rollout help must remain inside its hovered disclosure');
    }
    const rolloutDisclosure = rolloutHelp.parentElement;
    rolloutHelpTrigger.listeners.mouseenter();
    if (rolloutHelp.parentElement !== fakeBody || rolloutHelp.dataset.open !== 'true') {
        throw new Error('opened help must enter the body portal above modal clipping');
    }
    const portalLeft = Number.parseInt(rolloutHelp.style.left, 10);
    const portalWidth = Number.parseInt(rolloutHelp.style.width, 10);
    if (portalLeft < 12 || portalLeft + portalWidth > 738) {
        throw new Error('portal help must stay inside viewport bounds');
    }
    rolloutHelpTrigger.listeners.mouseleave();
    if (rolloutHelp.parentElement !== rolloutDisclosure || rolloutHelp.dataset.open) {
        throw new Error('closed help must return to its disclosure without stale layer state');
    }
    if (!toggle || toggle.disabled) throw new Error('verified toggle must be enabled');
    if (JSON.stringify(rolloutPercentage.children.map(option => option.value))
        !== JSON.stringify(['5', '25', '50'])) {
        throw new Error('percentage presets were not exact');
    }
    if (optionFor(rolloutMode, 'percentage').disabled
        || !optionFor(rolloutMode, 'all').disabled
        || optionFor(rolloutPercentage, 5).disabled
        || !optionFor(rolloutPercentage, 25).disabled
        || !optionFor(rolloutPercentage, 50).disabled) {
        throw new Error('canary must allow only the next 5 percent stage');
    }
    toggle.checked = true;
    toggle.listeners.change();
    if (!optionFor(rolloutMode, 'percentage').disabled) {
        throw new Error('active project tools must block text expansion');
    }
    const saved = await window.HermesSettings.save();
    if (!saved) throw new Error('verified settings save failed');
    if (savedPayload.hermes_tools_enabled !== true) throw new Error('tools were not enabled');
    if (JSON.stringify(savedPayload.hermes_allowed_capabilities) !== JSON.stringify(['hermes.project.read'])) {
        throw new Error('capability allowlist was not exact');
    }
    if (savedPayload.hermes_readonly_project_id !== 'project-canary') {
        throw new Error('backend project id was changed');
    }
    if (elements.has('hermes-setting-readonly-project-id')) {
        throw new Error('project id must not be user-editable');
    }

    toggle.checked = false;
    toggle.listeners.change();
    if (!optionFor(rolloutMode, 'percentage').disabled) {
        throw new Error('tools must be stopped and saved before promotion');
    }
    if (!await window.HermesSettings.save()) throw new Error('tool stop save failed');
    if (optionFor(rolloutMode, 'percentage').disabled) {
        throw new Error('5 percent must unlock after tools are saved off');
    }

    rolloutMode.value = 'percentage';
    rolloutMode.listeners.change();
    if (rolloutPercentage.value !== '5') throw new Error('first percentage must be 5');
    if (!await window.HermesSettings.save()) throw new Error('5 percent save failed');
    if (optionFor(rolloutPercentage, 25).disabled
        || !optionFor(rolloutPercentage, 50).disabled
        || !optionFor(rolloutMode, 'all').disabled) {
        throw new Error('5 percent must unlock only 25 percent');
    }

    rolloutPercentage.value = '25';
    rolloutPercentage.listeners.change();
    if (!await window.HermesSettings.save()) throw new Error('25 percent save failed');
    if (optionFor(rolloutPercentage, 50).disabled
        || !optionFor(rolloutMode, 'all').disabled) {
        throw new Error('25 percent must unlock only 50 percent');
    }

    rolloutPercentage.value = '50';
    rolloutPercentage.listeners.change();
    if (!await window.HermesSettings.save()) throw new Error('50 percent save failed');
    if (optionFor(rolloutMode, 'all').disabled) {
        throw new Error('50 percent must unlock all');
    }

    rolloutMode.value = 'all';
    rolloutMode.listeners.change();
    if (!await window.HermesSettings.save()) throw new Error('all save failed');
    if (!await window.HermesSettings.rollback()) throw new Error('emergency rollback failed');
    if (rollbackCalls !== 1 || settings.hermes_rollout_mode !== 'disabled') {
        throw new Error('rollback endpoint did not reach disabled');
    }
    if (!confirmationCopy.includes('不會刪除任何')) {
        throw new Error('rollback confirmation did not explain data retention');
    }
    window.HermesSettings.destroy();

    for (const status of invalidStatuses) {
        activeStatus = status;
        settings.hermes_rollout_mode = 'canary';
        settings.hermes_rollout_percentage = 0;
        settings.hermes_canary_session_ids = ['session-canary'];
        settings.hermes_tools_enabled = true;
        const nextContainer = new FakeElement('div');
        await window.HermesSettings.init({ container: nextContainer, apiFetch });
        const lockedToggle = elements.get('hermes-setting-tools-enabled');
        if (!lockedToggle.disabled || lockedToggle.checked) {
            throw new Error('unverified policy must fail closed');
        }
        window.HermesSettings.destroy();
    }
})().catch(error => {
    console.error(error.stack || error);
    process.exitCode = 1;
});
"""
    completed = subprocess.run(
        ["node", "-e", script, str(ROOT / "frontend" / "hermes-settings.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_loopback_rollout_and_environment_name_are_validated_client_side():
    assert "function validateLoopbackUrl(value)" in HERMES_SETTINGS_JS
    assert "host !== 'localhost' && !ipv4Loopback && !ipv6Loopback" in HERMES_SETTINGS_JS
    assert "parsed.username || parsed.password || parsed.search || parsed.hash" in HERMES_SETTINGS_JS
    assert "ENV_NAME_PATTERN.test(apiKeyEnv)" in HERMES_SETTINGS_JS
    assert "const PERCENTAGE_LADDER = Object.freeze([5, 25, 50])" in HERMES_SETTINGS_JS
    assert "!PERCENTAGE_LADDER.includes(percentage)" in HERMES_SETTINGS_JS
    assert "rolloutPercentage = document.createElement('select')" in HERMES_SETTINGS_JS
    assert "rollout expansion must advance" not in HERMES_SETTINGS_JS
    assert "Canary 模式至少需要一個 Session ID" in HERMES_SETTINGS_JS


def test_rollout_status_shows_safe_operational_and_promotion_summary():
    assert "status.rollout_control" in HERMES_SETTINGS_JS
    assert "status.supervisor" in HERMES_SETTINGS_JS
    for label in (
        "監控狀態",
        "目前階段",
        "下一階段",
        "可晉升",
        "晉升阻擋",
    ):
        assert label in HERMES_SETTINGS_JS
    assert "firstRolloutBlocker.message || firstRolloutBlocker.code" in HERMES_SETTINGS_JS
    assert "metrics_cohort" not in HERMES_SETTINGS_JS
    assert "subject_hash" not in HERMES_SETTINGS_JS


def test_rollout_controls_are_fixed_staged_and_require_tools_to_stop_first():
    assert "ROLLOUT_STAGE_LABELS" in HERMES_SETTINGS_JS
    assert "targetStage > currentStage + 1" in HERMES_SETTINGS_JS
    assert "targetStage <= currentStage" in HERMES_SETTINGS_JS
    assert "persistedTools || Boolean(state.refs.toolsEnabled?.checked)" in HERMES_SETTINGS_JS
    assert "請先關閉專案唯讀工具並儲存" in HERMES_SETTINGS_JS
    assert "rolloutStageNotice.dataset.hermesRolloutGuidance = 'true'" in HERMES_SETTINGS_JS


def test_emergency_basic_chat_rollback_is_confirmed_and_refreshes_state():
    assert "立即回復 Basic Chat" in HERMES_SETTINGS_JS
    assert "這不會刪除任何對話、專案或設定資料" in HERMES_SETTINGS_JS
    assert "request('/api/hermes/rollout/rollback', {" in HERMES_SETTINGS_JS
    assert "const refreshed = await refresh()" in HERMES_SETTINGS_JS
    assert "rollbackButton.addEventListener('click'" in HERMES_SETTINGS_JS


def test_workbench_loads_and_mounts_the_hermes_panel_without_replacing_ui():
    module_index = INDEX_HTML.index('src="hermes-settings.js')
    app_index = INDEX_HTML.index('src="app.js')
    assert module_index < app_index
    assert 'data-target="tab-settings-hermes"' in INDEX_HTML
    assert 'id="tab-settings-hermes"' in INDEX_HTML
    assert 'id="hermes-settings-container"' in INDEX_HTML
    assert "window.workbenchHermesSettings?.init({" in APP_JS
    assert "container: hermesSettingsContainer" in APP_JS


def test_loading_error_and_accessibility_states_are_rendered():
    assert "aria-busy" in HERMES_SETTINGS_JS
    assert "aria-live" in HERMES_SETTINGS_JS
    assert "role', kind === 'error' ? 'alert' : 'status'" in HERMES_SETTINGS_JS
    assert "state.settingsLoading || state.settingsSaving" in HERMES_SETTINGS_JS
    assert "state.statusLoading || state.statusProbing" in HERMES_SETTINGS_JS
    assert "無法載入 Hermes 設定" in HERMES_SETTINGS_JS
    assert "Hermes 健康檢查失敗" in HERMES_SETTINGS_JS
