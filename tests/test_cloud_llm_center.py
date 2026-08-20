import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
CENTER_JS = (ROOT / "frontend" / "cloud-llm-center.js").read_text(encoding="utf-8")
PROVIDER_JS = (ROOT / "frontend" / "extension-center.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")


def test_cloud_llm_is_an_independent_primary_workspace():
    assert 'id="rail-cloud-llm"' in HTML
    assert 'id="cloud-llm-workspace"' in HTML
    assert 'id="cloud-llm-modal"' not in HTML
    assert 'id="btn-open-cloud-llm-settings"' in HTML
    assert HTML.count('id="model-provider-list"') == 1
    assert HTML.index('id="cloud-llm-workspace"') < HTML.index('id="model-provider-list"')
    assert "nextWorkspace === 'cloud'" in APP_JS
    assert "setPrimaryWorkspace('cloud')" in APP_JS
    assert "window.workbenchCloudLlm?.open()" in APP_JS


def test_controller_mounts_workspace_and_uses_navigation_callbacks():
    assert "const workspace = byId('cloud-llm-workspace')" in CENTER_JS
    assert "const workbenchBody = document.querySelector('.workbench-body')" in CENTER_JS
    assert "workbenchBody.appendChild(workspace)" in CENTER_JS
    assert "state.deps?.onWorkspaceOpen?.()" in CENTER_JS
    assert "state.deps?.onWorkspaceClose?.()" in CENTER_JS
    assert "classList.add('active')" not in CENTER_JS
    assert "classList.remove('active')" in CENTER_JS  # settings entry remains a modal


def test_deactivate_discards_an_unsaved_editor_before_hiding_workspace():
    assert "async function deactivate()" in CENTER_JS
    assert "const discardEditor = Boolean(state.editingId)" in CENTER_JS
    assert "if (discardEditor) await state.deps?.reloadProviders?.()" in CENTER_JS
    assert "if (workspace) workspace.hidden = true" in CENTER_JS
    assert "if (await deactivate()) state.deps?.onWorkspaceClose?.()" in CENTER_JS
    assert "{ init, open, openTab, close, deactivate, render: renderLibrary }" in CENTER_JS


def test_workspace_does_not_restore_modal_backdrop_close_behavior():
    assert "cloud-llm-modal" not in CENTER_JS
    assert "event.target === byId('cloud-llm-workspace')" not in CENTER_JS
    settings_entry = CENTER_JS.split("byId('btn-open-cloud-llm-settings')", 1)[1]
    assert "byId('settings-modal')?.classList.remove('active')" in settings_entry
    assert "open()" in settings_entry


def test_library_exposes_search_filters_classification_and_delete():
    required_ids = {
        "cloud-llm-search",
        "cloud-llm-provider-filter",
        "cloud-llm-kind-filter",
        "cloud-llm-status-filter",
        "cloud-llm-library-list",
    }
    for element_id in required_ids:
        assert f'id="{element_id}"' in HTML
    for marker in (
        "data-cloud-provider-id",
        "data-cloud-edit",
        "data-cloud-delete",
        "providerKind(provider)",
        "missing-key",
    ):
        assert marker in CENTER_JS


def test_new_imports_use_unique_records_and_fail_closed():
    assert "state.deps.nextProviderId()" in CENTER_JS
    assert "nextModelProviderId" in APP_JS
    assert "while (existing.has(candidate))" in PROVIDER_JS
    assert "enabled: false" in CENTER_JS
    assert "MAX_PROVIDERS = 8" in CENTER_JS
    assert "new Set(ids).size !== ids.length" in CENTER_JS


def test_library_persists_the_complete_provider_array_and_secrets_separately():
    assert "model_providers: records" in CENTER_JS
    assert "await state.deps.saveSecrets()" in CENTER_JS
    assert "/api/settings/secrets" in PROVIDER_JS
    assert "removedProviderSecrets" in PROVIDER_JS
    assert "api_key" not in CENTER_JS
    assert "status.last4" in CENTER_JS


def test_delete_failure_is_not_silently_reported_as_success():
    delete_block = PROVIDER_JS.split("for (const providerId of removedProviderSecrets)", 1)[1]
    assert "if (!response.ok)" in delete_block
    assert "throw new Error" in delete_block
    assert "removedProviderSecrets.clear()" in delete_block


def test_workspace_and_lists_are_bounded_and_scrollable():
    workspace_rule = CSS.split(".cloud-llm-workspace {", 1)[1].split("}", 1)[0]
    list_rule = CSS.split(".cloud-llm-library-list {", 1)[1].split("}", 1)[0]
    editor_rule = CSS.split(".cloud-llm-editor-list {", 1)[1].split("}", 1)[0]
    toolbar_rule = CSS.split(".cloud-llm-editor-toolbar {", 1)[1].split("}", 1)[0]
    toolbar_button_rule = CSS.split(".cloud-llm-editor-toolbar > .btn {", 1)[1].split("}", 1)[0]
    assert "height: 100%" in workspace_rule
    assert "min-width: 0" in workspace_rule
    assert "overflow: hidden" in workspace_rule
    assert "overflow-y: auto" in list_rule
    assert "overflow-y: auto" in editor_rule
    assert "grid-template-columns: auto minmax(0, 1fr)" in toolbar_rule
    assert "width: auto" in toolbar_button_rule


def test_editor_form_has_labeled_balanced_sections():
    for label in ("API 供應商", "連線名稱", "模型或整合頁網址", "API Key"):
        assert f"<span>{label}" in PROVIDER_JS
    assert "model-provider-identity-grid" in PROVIDER_JS
    assert "model-provider-secret-grid" in PROVIDER_JS
    assert "model-provider-connectivity-row" in PROVIDER_JS


def test_cloud_controller_loads_before_the_app_initializer():
    center_script = HTML.index('src="cloud-llm-center.js')
    app_script = HTML.index('src="app.js')
    assert center_script < app_script


def test_nvidia_ocr_frontend_switches_provider_isolates_secret_and_uses_image_adapter():
    script = r"""
const fs = require('fs');
const vm = require('vm');

const classList = () => ({ add() {}, remove() {} });
const field = (value = '') => ({
    value, hidden: false, disabled: false, checked: false, textContent: '',
    className: '', dataset: {}, classList: classList(), files: [],
    placeholder: '', innerHTML: '', children: [],
    closest(selector) { return selector === 'label' ? this.label : null; },
    replaceChildren(...children) { this.children = children; },
    appendChild(child) { this.children.push(child); return child; },
    removeAttribute() {}, setAttribute() {}
});

const source = field('https://build.nvidia.com/nvidia/nemotron-ocr-v2?snippet_tab=Python');
const providerType = field('gemini');
const providerId = field('gemini');
const label = field('Google Gemini API');
const endpoint = field('https://generativelanguage.googleapis.com/v1beta/openai');
const apiKey = field('');
const enabled = field(''); enabled.checked = true;
const selected = field('');
const kind = field('chat');
const language = field(''); language.label = field('');
const selectedStatus = field('');
const prompt = field('Hello.');
const responseOutput = field('');
const adapterStatus = field(''); adapterStatus.hidden = true;
const modelButton = field('');
const connectionButton = field('');
const connectionResult = field('');
const secretState = field(''); secretState.classList = classList();
const chatControl = field('');
const ocrControl = field(''); ocrControl.hidden = true;
const ocrFile = field('');
const ocrFileStatus = field('');
const supportsTools = field('');
const modelPanel = field(''); modelPanel.querySelector = () => selected;
const officialLink = field('');
const description = field('');
const costs = [field('0'), field('0')];
const currency = field('USD');

const selectors = new Map([
    ['[data-provider-field="source_url"]', source],
    ['[data-provider-field="provider_type"]', providerType],
    ['[data-provider-field="id"]', providerId],
    ['[data-provider-field="label"]', label],
    ['[data-provider-field="base_url"]', endpoint],
    ['[data-provider-field="api_key"]', apiKey],
    ['[data-provider-field="enabled"]', enabled],
    ['[data-provider-field="selected_model"]', selected],
    ['[data-provider-field="model_kind"]', kind],
    ['[data-provider-field="supports_tools"]', supportsTools],
    ['[data-provider-field="input_cost_per_million"]', costs[0]],
    ['[data-provider-field="output_cost_per_million"]', costs[1]],
    ['[data-provider-field="currency"]', currency],
    ['[data-provider-test-system]', language],
    ['[data-provider-test-prompt]', prompt],
    ['[data-provider-selected-status]', selectedStatus],
    ['[data-provider-response]', responseOutput],
    ['[data-provider-adapter-status]', adapterStatus],
    ['[data-test-provider-model]', modelButton],
    ['[data-test-provider]', connectionButton],
    ['[data-provider-test-result]', connectionResult],
    ['[data-provider-ocr-file]', ocrFile],
    ['[data-provider-ocr-file-status]', ocrFileStatus],
    ['[data-provider-model-panel]', modelPanel],
    ['.model-provider-official', officialLink],
    ['.model-provider-description', description],
    ['.model-provider-secret-state', secretState]
]);
const card = {
    dataset: { currentProviderType: 'gemini', currentModelIdentity: '' },
    providerToolAttestation: null,
    querySelector(selector) { return selectors.get(selector) || null; },
    querySelectorAll(selector) {
        if (selector === '[data-provider-chat-test-control]') return [chatControl];
        if (selector === '[data-provider-ocr-test-control]') return [ocrControl];
        return [];
    }
};

let requestCount = 0;
let modelPayload = null;
const context = {
    window: {}, console, URL, encodeURIComponent, setTimeout, clearTimeout,
    API_BASE: '', modelProviderList: { querySelectorAll: () => [card] },
    document: { createElement: () => field('') },
    FileReader: class {
        readAsDataURL(file) {
            this.result = `data:${file.type};base64,QUJD`;
            this.onload();
        }
    },
    apiFetch: async (_url, options) => {
        requestCount += 1;
        modelPayload = JSON.parse(options.body);
        return {
            ok: true,
            json: async () => ({
                success: true, selected_model: 'nvidia/nemotron-ocr-v2',
                response: 'recognized text', model_profile: { kind: 'vision' }
            })
        };
    }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/extension-center.js', 'utf8'), context);

(async () => {
    vm.runInContext("modelProviderSecretStatus = {gemini: {configured: true, last4: '1234'}}", context);
    vm.runInContext('syncProviderSourceModel(globalThis.card)', Object.assign(context, { card }));
    await vm.runInContext('testModelProviderCard(globalThis.card)', context);
    const requestsWithoutNewKey = requestCount;

    apiKey.value = 'nvapi-new-key';
    ocrFile.files = [{ name: 'sample.png', type: 'image/png', size: 1024 }];
    const firstOcrRequest = vm.runInContext('testProviderModelCard(globalThis.card)', context);
    const duplicateOcrRequest = vm.runInContext('testProviderModelCard(globalThis.card)', context);
    await Promise.all([firstOcrRequest, duplicateOcrRequest]);

    const pure = vm.runInContext(`({
        ocrKind: inferredProviderModelKind('nvidia/nemotron-ocr-v2'),
        genericVisionBlocked: providerModelBlocksTest('vision', 'qwen/qwen-vl'),
        ocrBlocked: providerModelBlocksTest('vision', 'nvidia/nemotron-ocr-v2'),
        pngValid: providerOcrFileValidation({name:'x.png', type:'image/png', size:131072}).valid,
        webpValid: providerOcrFileValidation({name:'x.webp', type:'image/webp', size:100}).valid,
        oversizedValid: providerOcrFileValidation({name:'x.jpg', type:'image/jpeg', size:131073}).valid
    })`, context);
    const ocrUiBeforeModelChange = {
        kind: kind.value,
        chatHidden: chatControl.hidden,
        ocrVisible: !ocrControl.hidden,
        output: responseOutput.textContent
    };
    ocrFile.value = 'selected-image.png';
    selected.value = 'nvidia/other-vision-model';
    vm.runInContext('syncProviderModelDefaults(globalThis.card)', context);
    console.log(JSON.stringify({
        providerType: providerType.value,
        endpoint: endpoint.value,
        providerId: providerId.value,
        enabled: enabled.checked,
        credentialResetRequired: card.providerCredentialResetRequired,
        requestsWithoutNewKey,
        ocrRequestCount: requestCount,
        connectionMessage: connectionResult.textContent,
        kind: ocrUiBeforeModelChange.kind,
        chatHidden: ocrUiBeforeModelChange.chatHidden,
        ocrVisible: ocrUiBeforeModelChange.ocrVisible,
        ocrFileClearedAfterModelChange: ocrFile.value === '',
        modelPayload,
        output: ocrUiBeforeModelChange.output,
        pure
    }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result["providerType"] == "nvidia"
    assert result["endpoint"] == "https://integrate.api.nvidia.com/v1"
    assert result["providerId"].startswith("nvidia-connection")
    assert result["providerId"] != "gemini"
    assert result["enabled"] is False
    assert result["credentialResetRequired"] is True
    assert result["requestsWithoutNewKey"] == 0
    assert result["ocrRequestCount"] == 1
    assert "API Key" in result["connectionMessage"]
    assert result["kind"] == "vision"
    assert result["chatHidden"] is True
    assert result["ocrVisible"] is True
    assert result["ocrFileClearedAfterModelChange"] is True
    assert result["modelPayload"]["image_data_url"] == "data:image/png;base64,QUJD"
    assert "prompt" not in result["modelPayload"]
    assert result["output"] == "recognized text"
    assert result["pure"] == {
        "ocrKind": "vision",
        "genericVisionBlocked": True,
        "ocrBlocked": False,
        "pngValid": True,
        "webpValid": False,
        "oversizedValid": False,
    }


def test_provider_requests_explain_network_timeout_and_backend_json_errors():
    script = r"""
const fs = require('fs');
const vm = require('vm');

const context = {
    window: {}, document: {}, console, setTimeout, clearTimeout, AbortController,
    API_BASE: '', apiFetch: async () => ({ ok: true, status: 200, json: async () => ({ success: true }) })
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/extension-center.js', 'utf8'), context);

async function captured(expression) {
    try {
        await vm.runInContext(expression, context);
        return { message: '', code: '' };
    } catch (error) {
        return { message: error.message, code: error.code || '' };
    }
}

(async () => {
    context.apiFetch = async () => { throw new TypeError('Failed to fetch'); };
    const network = await captured("providerJsonRequest('/api/test')");

    context.apiFetch = async (_url, options) => new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => {
            const error = new Error('aborted');
            error.name = 'AbortError';
            reject(error);
        });
    });
    const timeout = await captured("providerJsonRequest('/api/test', {}, 5)");

    context.apiFetch = async () => ({
        ok: false,
        status: 422,
        json: async () => ({
            success: false,
            detail: { code: 'PROVIDER_IMAGE_INVALID', message: '後端圖片驗證說明' }
        })
    });
    const backend = await captured("providerJsonRequest('/api/test')");

    context.apiFetch = async () => ({
        ok: true,
        status: 200,
        json: async () => { throw new SyntaxError('Unexpected token'); }
    });
    const invalidJson = await captured("providerJsonRequest('/api/test')");

    console.log(JSON.stringify({ network, timeout, backend, invalidJson }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result["network"]["code"] == "LOCAL_API_UNREACHABLE"
    assert "Agent API" in result["network"]["message"]
    assert "Failed to fetch" not in result["network"]["message"]
    assert result["timeout"]["code"] == "LOCAL_API_TIMEOUT"
    assert "逾時" in result["timeout"]["message"]
    assert result["backend"] == {
        "message": "後端圖片驗證說明",
        "code": "PROVIDER_IMAGE_INVALID",
    }
    assert result["invalidJson"]["code"] == "LOCAL_API_INVALID_JSON"
    assert "無法解析" in result["invalidJson"]["message"]
