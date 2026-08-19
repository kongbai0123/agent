/* M14 trusted extension center and the integration/provider UI extracted from app.js. */

function createMetric(label, value) {
    const item = document.createElement('div');
    item.className = 'runtime-metric';
    const labelElement = document.createElement('div');
    labelElement.className = 'runtime-metric-label';
    labelElement.textContent = label;
    const valueElement = document.createElement('div');
    valueElement.className = 'runtime-metric-value';
    valueElement.textContent = value == null || value === '' ? '--' : String(value);
    item.append(labelElement, valueElement);
    return item;
}

function renderN8nStatus(data) {
    if (!n8nStatusState || !n8nStatusMetrics || !n8nInstallOptions) return;
    const ready = data.installed && data.reachable;
    n8nStatusState.textContent = ready
        ? '已安裝且服務可連線'
        : data.installed
            ? '已安裝，服務尚未啟動或無法連線'
            : data.message;
    n8nStatusState.className = `runtime-health-state ${ready ? 'ok' : 'warn'}`;
    n8nStatusMetrics.replaceChildren(
        createMetric('安裝狀態', data.installed ? `已安裝（${data.installation_scope === 'workbench' ? 'Workbench' : '全域'}）` : '未安裝'),
        createMetric('n8n 版本', data.version),
        createMetric('Node.js', data.node_version),
        createMetric('服務 URL', data.url),
        createMetric('服務連線', data.reachable ? '正常' : '未連線'),
        createMetric('安裝工作', data.install?.message || '尚未執行')
    );
    n8nInstallOptions.replaceChildren();
    if (data.installed) return;
    const heading = document.createElement('div');
    heading.className = 'integration-options-heading';
    heading.textContent = '可用安裝與連線方式';
    n8nInstallOptions.appendChild(heading);
    (data.options || []).forEach(option => {
        const row = document.createElement('div');
        row.className = 'integration-option-row';
        const copy = document.createElement('div');
        copy.className = 'integration-option-copy';
        const title = document.createElement('strong');
        title.textContent = option.label + (option.recommended ? '（建議）' : '');
        const description = document.createElement('span');
        description.textContent = `${option.description} ${option.requirement}`;
        copy.append(title, description);
        row.appendChild(copy);
        if (option.id === 'local_npm') {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'btn btn-primary runtime-action';
            button.dataset.n8nInstall = option.id;
            button.disabled = !option.available || data.install?.status === 'running' || data.install?.status === 'queued';
            button.innerHTML = '<i data-lucide="download"></i>安裝';
            row.appendChild(button);
        }
        n8nInstallOptions.appendChild(row);
    });
    safeCreateIcons();
}

async function checkN8nStatus() {
    if (!btnN8nStatus) return;
    btnN8nStatus.disabled = true;
    n8nStatusState.textContent = '檢查中...';
    n8nStatusState.className = 'runtime-health-state';
    try {
        renderN8nStatus(await removedBasicFeature('n8n integration'));
    } catch (error) {
        n8nStatusState.textContent = `檢查失敗：${error.message}`;
        n8nStatusState.className = 'runtime-health-state error';
    } finally {
        btnN8nStatus.disabled = false;
    }
}

function renderCursorStatus(data) {
    if (!cursorStatusState || !cursorStatusMetrics) return;
    cursorStatusState.textContent = data.message;
    cursorStatusState.className = `runtime-health-state ${data.installed ? 'ok' : 'warn'}`;
    cursorStatusMetrics.replaceChildren(
        createMetric('CLI', data.installed ? '已安裝' : '未安裝'),
        createMetric('版本', data.version),
        createMetric('登入狀態', data.authenticated == null ? '執行時確認' : (data.authenticated ? '已登入' : '未登入'))
    );
}

async function checkCursorStatus() {
    if (!btnCursorStatus) return;
    btnCursorStatus.disabled = true;
    cursorStatusState.textContent = '檢查中...';
    cursorStatusState.className = 'runtime-health-state';
    try {
        renderCursorStatus(await removedBasicFeature('Cursor integration'));
    } catch (error) {
        cursorStatusState.textContent = `檢查失敗：${error.message}`;
        cursorStatusState.className = 'runtime-health-state error';
    } finally {
        btnCursorStatus.disabled = false;
    }
}

async function checkMcpStatus() {
    if (!btnMcpStatus || !mcpStatusState || !mcpStatusMetrics) return;
    btnMcpStatus.disabled = true;
    mcpStatusState.textContent = '檢查中...';
    mcpStatusState.className = 'runtime-health-state';
    try {
        const response = await apiFetch(`${API_BASE}/api/extensions`);
        const catalog = await response.json();
        if (!response.ok || !catalog.success) {
            throw new Error(catalog.detail?.message || catalog.message || `HTTP ${response.status}`);
        }
        const configured = (catalog.extensions || []).filter(item => item.kind === 'mcp');
        const resolvedServers = await Promise.all(configured.map(async item => {
            let current = item;
            if (item.installed && item.effective_enabled) {
                const healthResponse = await apiFetch(
                    `${API_BASE}/api/extensions/${encodeURIComponent(item.id)}/health`,
                    { method: 'POST' }
                );
                const healthPayload = await healthResponse.json();
                if (healthResponse.ok && healthPayload.success && healthPayload.extension) {
                    current = healthPayload.extension;
                }
            }
            const health = current.health || {};
            const detail = health.detail || {};
            let status = 'disabled';
            if (current.installed && current.effective_enabled && health.status === 'ready') {
                status = 'connected';
            } else if (current.installed && current.effective_enabled) {
                status = 'error';
            }
            return {
                id: current.name || current.id,
                status,
                tool_count: Number(detail.tool_count || 0),
                error: detail.error_code || detail.reason || health.status || 'unavailable'
            };
        }));
        const data = { servers: resolvedServers };
        const servers = data.servers || [];
        const connected = servers.filter(server => server.status === 'connected');
        mcpStatusState.textContent = servers.length
            ? `${connected.length}/${servers.length} 個伺服器已連線`
            : '尚未設定 MCP Server';
        mcpStatusState.className = `runtime-health-state ${servers.length && connected.length === servers.length ? 'ok' : 'warn'}`;
        mcpStatusMetrics.replaceChildren(...(
            servers.length
                ? servers.map(server => createMetric(
                    server.id,
                    server.status === 'connected'
                        ? `${server.tool_count || 0} 個工具`
                        : server.status === 'disabled' ? '已停用' : `錯誤：${server.error || '無法連線'}`
                ))
                : [createMetric('狀態', '儲存設定後會在背景連線與探索工具')]
        ));
    } catch (error) {
        mcpStatusState.textContent = `檢查失敗：${error.message}`;
        mcpStatusState.className = 'runtime-health-state error';
    } finally {
        btnMcpStatus.disabled = false;
    }
}

async function installN8n(method) {
    const button = n8nInstallOptions.querySelector(`[data-n8n-install="${method}"]`);
    if (button) button.disabled = true;
    updateTaskProgress('n8n-install', {
        label: '安裝 n8n',
        detail: '正在建立背景安裝工作',
        mode: 'indeterminate',
        value: null
    });
    try {
        const data = await removedBasicFeature('n8n installation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ method })
        });
        showToast(data.install?.message || 'n8n 安裝工作已建立。', 'info');
        updateTaskProgress('n8n-install', {
            detail: data.install?.message || '安裝工作執行中',
            mode: data.install?.progress?.mode || 'indeterminate',
            value: data.install?.progress?.value ?? null
        });
        await checkN8nStatus();
    } catch (error) {
        showToast(`無法開始安裝 n8n：${error.message}`, 'error');
        finishTaskProgress('n8n-install', 'failed', error.message);
        if (button) button.disabled = false;
    }
}

let modelProviderSecretStatus = {};
let modelProviderCatalog = {
    gemini: {
        id: 'gemini',
        label: 'Google Gemini API',
        description: 'Google 生成式 AI 模型',
        base_url: 'https://generativelanguage.googleapis.com/v1beta/openai',
        official_url: 'https://aistudio.google.com/apikey',
        endpoint_editable: false,
        source_hosts: ['aistudio.google.com', 'ai.google.dev']
    },
    nvidia: {
        id: 'nvidia',
        label: 'NVIDIA API Catalog',
        description: 'NVIDIA 免費端點、合作夥伴端點與 NIM 模型',
        base_url: 'https://integrate.api.nvidia.com/v1',
        official_url: 'https://build.nvidia.com/models',
        endpoint_editable: false,
        source_hosts: ['build.nvidia.com']
    },
    openai: {
        id: 'openai',
        label: 'OpenAI API',
        description: 'OpenAI 模型 API',
        base_url: 'https://api.openai.com/v1',
        official_url: 'https://platform.openai.com/api-keys',
        endpoint_editable: false,
        source_hosts: ['platform.openai.com']
    },
    openai_compatible: {
        id: 'openai_compatible',
        label: 'OpenAI-compatible',
        description: 'OpenRouter、LM Studio、vLLM 或自訂相容端點',
        base_url: '',
        official_url: '',
        endpoint_editable: true,
        source_hosts: []
    }
};
const removedProviderSecrets = new Set();
const legacyMcpEnabledById = new Map();

function inferProviderType(provider = {}) {
    const explicit = String(provider.provider_type || '').trim().toLowerCase();
    if (modelProviderCatalog[explicit]) return explicit;
    const endpoint = String(provider.base_url || '').toLowerCase();
    if (endpoint.includes('generativelanguage.googleapis.com')) return 'gemini';
    if (endpoint.includes('integrate.api.nvidia.com')) return 'nvidia';
    if (endpoint.includes('api.openai.com')) return 'openai';
    return 'openai_compatible';
}

function providerTypeOptions(selected) {
    return Object.values(modelProviderCatalog).map(item => `
        <option value="${escapeHtml(item.id)}" ${item.id === selected ? 'selected' : ''}>
            ${escapeHtml(item.label)}
        </option>`).join('');
}

function providerModelOptions(selectedModel) {
    if (!selectedModel) {
        return '<option value="">先測試連線以載入模型</option>';
    }
    return `<option value="${escapeHtml(selectedModel)}" selected>${escapeHtml(selectedModel)}</option>`;
}

function inferredProviderModelKind(model, explicit = '') {
    const value = String(model || '').toLowerCase();
    // A model identity takes precedence over metadata inherited from the
    // previously selected model. This prevents a new chat model from retaining
    // a stale "translation" classification (and keeps specialized models from
    // being promoted to chat).
    if (/(?:riva-|\/|-)?translat(?:e|ion)/.test(value)) return 'translation';
    if (/rerank|re-rank|ranker/.test(value)) return 'rerank';
    if (/(?:\/|-)(?:embed|embedding)|text-embedding|\/bge-|(?:^|\/)e5-/.test(value)) return 'embedding';
    if (/llama-guard|nemoguard|safety-guard|moderation|classifier/.test(value)) return 'unknown';
    if (/vision|(?:\/|-)(?:vl)(?:-|$)|llava|vila|image-to-text/.test(value)) return 'vision';
    if (/chat|instruct|assistant|llama|nemotron|qwen|mistral|mixtral|gemma|deepseek|gpt-|claude|command-r/.test(value)) {
        return 'chat';
    }
    const declared = String(explicit || '').trim().toLowerCase();
    if (['chat', 'translation', 'embedding', 'rerank', 'vision', 'unknown'].includes(declared)) {
        return declared;
    }
    return 'unknown';
}

function providerModelKindOptions(selected) {
    const labels = {
        chat: '\u5c0d\u8a71\uff0fAgent \u6a21\u578b',
        translation: '\u7ffb\u8b6f\u5de5\u5177',
        embedding: '\u5411\u91cf\u5d4c\u5165\u5de5\u5177',
        rerank: '\u6587\u4ef6\u91cd\u6392\u5de5\u5177',
        vision: '\u8996\u89ba\u5de5\u5177',
        unknown: '\u5c1a\u672a\u5206\u985e\uff08\u4e0d\u53ef\u4f5c\u70ba Agent\uff09'
    };
    return Object.entries(labels).map(([value, label]) =>
        `<option value="${value}" ${value === selected ? 'selected' : ''}>${label}</option>`
    ).join('');
}
function modelIdFromSourceUrl(value) {
    try {
        const url = new URL(String(value || '').trim());
        for (const key of ['model', 'model_id', 'modelId']) {
            const candidate = String(url.searchParams.get(key) || '').trim();
            if (candidate) return candidate;
        }
        const parts = url.pathname.split('/').filter(Boolean);
        if (url.hostname === 'build.nvidia.com' && parts.length >= 2 && parts[0].toLowerCase() !== 'models') {
            return `${parts[0]}/${parts[1]}`;
        }
        const lowered = parts.map(part => part.toLowerCase());
        for (const marker of ['models', 'model']) {
            const index = lowered.indexOf(marker);
            if (index >= 0 && parts[index + 1]) return parts.slice(index + 1).join('/');
        }
    } catch (_error) {
        return '';
    }
    return '';
}

function nextModelProviderId(prefix = 'connection') {
    const existing = new Set(collectModelProviders().map(item => item.id));
    let index = 1;
    let candidate = prefix;
    while (existing.has(candidate)) {
        index += 1;
        candidate = `${prefix}${index}`;
    }
    return candidate;
}

function modelProviderCard(provider = {}) {
    const id = String(provider.id || '').trim().toLowerCase();
    const providerType = inferProviderType(provider);
    const catalogItem = modelProviderCatalog[providerType] || modelProviderCatalog.openai_compatible;
    const status = modelProviderSecretStatus[id] || {};
    const configuredText = status.configured
        ? `已安全保存 ·••••${escapeHtml(status.last4 || '')}`
        : '尚未設定金鑰';
    const officialLink = catalogItem.official_url
        ? `<a class="model-provider-official" href="${escapeHtml(catalogItem.official_url)}"
              target="_blank" rel="noopener noreferrer">開啟官方網站 ↗</a>`
        : `<a class="model-provider-official is-hidden" target="_blank"
              rel="noopener noreferrer" aria-hidden="true">開啟官方網站 ↗</a>`;
    const endpoint = String(provider.base_url || catalogItem.base_url || '');
    const endpointLock = catalogItem.endpoint_editable ? '' : 'readonly';
    const selectedModel = String(provider.selected_model || '').trim();
    const modelKind = inferredProviderModelKind(selectedModel, provider.model_kind);
    const languagePair = String(provider.language_pair || (modelKind === 'translation' ? 'en-zh-cn' : '')).trim();
    return `
        <div class="model-provider-card" data-provider-card
             data-original-provider-id="${escapeHtml(id)}">
            <div class="model-provider-card-head">
                <input type="hidden" data-provider-field="id" value="${escapeHtml(id)}">
                <div class="model-provider-card-title">
                    <span class="model-provider-extension-note">API CONNECTION</span>
                    <strong>連線基本資料</strong>
                </div>
                <div class="model-provider-card-actions">
                    ${officialLink}
                    <button type="button" class="btn btn-secondary compact model-provider-remove"
                            data-remove-provider aria-label="移除此 API">移除</button>
                </div>
            </div>
            <div class="model-provider-identity-grid">
                <label>
                    <span>API 供應商</span>
                    <select class="settings-input" data-provider-field="provider_type"
                            aria-label="API 供應商">${providerTypeOptions(providerType)}</select>
                </label>
                <label>
                    <span>連線名稱</span>
                    <input class="settings-input" data-provider-field="label"
                           value="${escapeHtml(provider.label || catalogItem.label || '')}"
                           placeholder="例如：團隊 NVIDIA API" aria-label="連線名稱">
                </label>
            </div>
            <label class="model-provider-enable-control">
                <input type="checkbox" data-provider-field="enabled"
                       ${provider.enabled === true ? 'checked' : ''}>
                <span>
                    <strong>允許使用此 API 連線</strong>
                    <small>這是連線層開關；擴充中心的權限開關也必須啟用，任一關閉都不會送出請求。</small>
                </span>
            </label>
            <div class="model-provider-secret-grid">
                <label>
                    <span>模型或整合頁網址 <small>選填</small></span>
                    <input class="settings-input model-provider-source-url" type="url"
                           data-provider-field="source_url"
                           value="${escapeHtml(provider.source_url || '')}"
                           autocomplete="url" spellcheck="false"
                           placeholder="https://build.nvidia.com/..."
                           aria-label="模型或整合頁網址">
                </label>
                <label>
                    <span>API Key</span>
                    <input class="settings-input model-provider-api-key" type="password"
                           data-provider-field="api_key" value="" autocomplete="new-password"
                           spellcheck="false" placeholder="${status.configured ? '輸入新金鑰以更新' : '貼上 API Key'}"
                           aria-label="API Key">
                </label>
            </div>
            <div class="model-provider-connectivity-row">
                <span class="model-provider-secret-state ${status.configured ? 'configured' : ''}">${configuredText}</span>
                <button type="button" class="btn btn-primary compact model-provider-test"
                        data-test-provider>測試連線</button>
            </div>
            <div class="model-provider-description">${escapeHtml(catalogItem.description || '')}</div>
            <section class="model-provider-model-test" data-provider-model-panel
                     ${selectedModel ? '' : 'hidden'}>
                <div class="model-provider-model-row">
                    <label>
                        <span>選擇 API 模型</span>
                        <select class="settings-input" data-provider-field="selected_model"
                                aria-label="選擇 API 模型">${providerModelOptions(selectedModel)}</select>
                    </label>
                    <label>
                        <span>\u6a21\u578b\u7528\u9014</span>
                        <select class="settings-input" data-provider-field="model_kind"
                                aria-label="\u6a21\u578b\u7528\u9014">${providerModelKindOptions(modelKind)}</select>
                    </label>
                    <label>
                        <span>\u7ffb\u8b6f\u65b9\u5411</span>
                        <input class="settings-input" data-provider-test-system
                               data-provider-field="language_pair"
                               value="${escapeHtml(languagePair)}"
                               placeholder="例如 en-zh-tw">
                    </label>
                </div>
                <label class="model-provider-test-prompt">
                    <span>Sandbox 測試內容</span>
                    <textarea class="settings-input" data-provider-test-prompt
                              rows="2">Hello, this is a model connection test.</textarea>
                </label>
                <div class="model-provider-model-actions">
                    <button type="button" class="btn btn-primary compact"
                            data-test-provider-model>取得模型回覆</button>
                    <span data-provider-selected-status>尚未驗證模型回覆</span>
                </div>
                <output class="model-provider-response" data-provider-response
                        aria-live="polite"></output>
            </section>
            <details class="model-provider-advanced">
                <summary>API Endpoint 與成本設定</summary>
                <label class="model-provider-tool-capability" data-provider-tool-capability
                       ${modelKind === 'chat' ? '' : 'hidden'}>
                    <input type="checkbox" data-provider-field="supports_tools"
                           ${provider.supports_tools === true ? 'checked' : ''}>
                    <span>
                        <strong>供應商宣告此模型支援 tools</strong>
                        <small>此勾選只是能力宣告，不是驗證結果；通過實際工具呼叫驗證後，Agent 才能使用工具。</small>
                    </span>
                </label>
                <div class="model-provider-tool-verification" data-provider-tool-verification
                     ${modelKind === 'chat' ? '' : 'hidden'}>
                    <button type="button" class="btn btn-secondary compact"
                            data-test-provider-tools
                            ${provider.supports_tools === true ? '' : 'disabled'}>
                        \u9a57\u8b49\u5de5\u5177\u547c\u53eb
                    </button>
                    <div class="model-provider-attestation-state" data-provider-tool-attestation
                         data-verified="${provider.tool_attestation ? 'true' : 'false'}">
                        ${provider.tool_attestation
                            ? '\u5de5\u5177\u547c\u53eb\u5df2\u9a57\u8b49\uff0c\u8b8a\u66f4\u6a21\u578b\u3001\u7528\u9014\u3001Endpoint \u6216 tools \u5ba3\u544a\u5f8c\u9700\u91cd\u65b0\u9a57\u8b49\u3002'
                            : '\u5c1a\u672a\u5b8c\u6210\u5de5\u5177\u547c\u53eb\u9a57\u8b49\uff1b\u4e0d\u6703\u5ba3\u7a31 Agent \u5df2\u53ef\u4f7f\u7528\u5de5\u5177\u3002'}
                    </div>
                </div>
                <div class="model-provider-endpoint-row">
                    <input class="settings-input model-provider-endpoint" data-provider-field="base_url"
                           value="${escapeHtml(endpoint)}" placeholder="https://api.example.com/v1"
                           aria-label="API Endpoint" ${endpointLock}>
                    <button type="button" class="btn btn-secondary compact"
                            data-copy-provider-endpoint>複製端點</button>
                </div>
                <div class="model-provider-cost-row">
                    <input class="settings-input" type="number" min="0" step="0.0001"
                           data-provider-field="input_cost_per_million"
                           value="${Number(provider.input_cost_per_million || 0)}"
                           placeholder="輸入／百萬 Token">
                    <input class="settings-input" type="number" min="0" step="0.0001"
                           data-provider-field="output_cost_per_million"
                           value="${Number(provider.output_cost_per_million || 0)}"
                           placeholder="輸出／百萬 Token">
                    <input class="settings-input" data-provider-field="currency"
                           value="${escapeHtml(provider.currency || 'USD')}"
                           maxlength="8" placeholder="USD">
                </div>
            </details>
            <div class="model-provider-test-result" data-provider-test-result aria-live="polite"></div>
        </div>`;
}

function renderModelProviders(providers = []) {
    if (!modelProviderList) return;
    bindProviderSourceModelSync();
    modelProviderList.innerHTML = providers.length
        ? providers.map(modelProviderCard).join('')
        : '<div class="model-provider-empty">目前只使用本機 Ollama。需要雲端模型時按「新增服務」。</div>';
    modelProviderList.querySelectorAll('[data-provider-card]')
        .forEach((card, index) => {
            const attestation = providers[index]?.tool_attestation;
            card.providerToolAttestation = attestation && typeof attestation === 'object'
                ? { ...attestation }
                : null;
            syncProviderCapabilityDefaults(card);
        });
}

function collectModelProviders() {
    return [...(modelProviderList?.querySelectorAll('[data-provider-card]') || [])].map(card => {
        const record = {
            id: card.querySelector('[data-provider-field="id"]').value.trim().toLowerCase(),
            provider_type: card.querySelector('[data-provider-field="provider_type"]').value,
            label: card.querySelector('[data-provider-field="label"]').value.trim(),
            base_url: card.querySelector('[data-provider-field="base_url"]').value.trim(),
            source_url: card.querySelector('[data-provider-field="source_url"]').value.trim(),
            selected_model: card.querySelector('[data-provider-field="selected_model"]').value.trim(),
            ...providerCapabilityPayload(card),
            enabled: card.querySelector('[data-provider-field="enabled"]').checked === true,
            input_cost_per_million: parseFloat(card.querySelector('[data-provider-field="input_cost_per_million"]').value) || 0,
            output_cost_per_million: parseFloat(card.querySelector('[data-provider-field="output_cost_per_million"]').value) || 0,
            currency: card.querySelector('[data-provider-field="currency"]').value.trim().toUpperCase() || 'USD'
        };
        if (card.providerToolAttestation) {
            record.tool_attestation = { ...card.providerToolAttestation };
        }
        return record;
    });
}

function editableMcpServerSettings(servers = []) {
    legacyMcpEnabledById.clear();
    return servers.map(server => {
        if (!server || typeof server !== 'object' || Array.isArray(server)) return server;
        const id = String(server.id || '').trim();
        legacyMcpEnabledById.set(id, server.enabled === true);
        const { enabled: _legacyEnabled, ...editable } = server;
        return editable;
    });
}

function collectMcpServerSettings(servers = []) {
    return servers.map(server => {
        if (!server || typeof server !== 'object' || Array.isArray(server)) return server;
        const id = String(server.id || '').trim();
        const { enabled: _ignoredUserValue, ...configuration } = server;
        return {
            ...configuration,
            // A value typed into the JSON editor cannot change canonical
            // extension state. Existing migration data is preserved; a new
            // server starts disabled until reviewed in Extension Center.
            enabled: legacyMcpEnabledById.get(id) === true
        };
    });
}

async function loadModelProviderSettings(providers = []) {
    try {
        const [secretResponse, catalogResponse] = await Promise.all([
            apiFetch(`${API_BASE}/api/settings/secrets`),
            apiFetch(`${API_BASE}/api/settings/providers/catalog`)
        ]);
        const data = await secretResponse.json();
        const catalogData = await catalogResponse.json();
        modelProviderSecretStatus = Object.fromEntries(
            (data.providers || []).map(item => [item.provider_id, item])
        );
        if (catalogResponse.ok && Array.isArray(catalogData.providers)) {
            modelProviderCatalog = Object.fromEntries(
                catalogData.providers.map(item => [item.id, item])
            );
        }
    } catch (error) {
        modelProviderSecretStatus = {};
        console.warn('Unable to load provider connection metadata:', error);
    }
    renderModelProviders(providers);
}

function applyProviderType(card, providerType) {
    const item = modelProviderCatalog[providerType] || modelProviderCatalog.openai_compatible;
    const label = card.querySelector('[data-provider-field="label"]');
    const endpoint = card.querySelector('[data-provider-field="base_url"]');
    const link = card.querySelector('.model-provider-official');
    const description = card.querySelector('.model-provider-description');
    const sourceUrl = card.querySelector('[data-provider-field="source_url"]');
    const modelPanel = card.querySelector('[data-provider-model-panel]');
    label.value = item.label || label.value;
    endpoint.value = item.base_url || '';
    endpoint.readOnly = !item.endpoint_editable;
    description.textContent = item.description || '';
    sourceUrl.value = '';
    modelPanel.hidden = true;
    modelPanel.querySelector('[data-provider-field="selected_model"]').innerHTML =
        providerModelOptions('');
    if (item.official_url) {
        link.href = item.official_url;
        link.classList.remove('is-hidden');
        link.removeAttribute('aria-hidden');
    } else {
        link.removeAttribute('href');
        link.classList.add('is-hidden');
        link.setAttribute('aria-hidden', 'true');
    }
}

function bindProviderSourceModelSync() {
    if (!modelProviderList || modelProviderList.providerSourceModelSyncBound) return;
    modelProviderList.providerSourceModelSyncBound = true;
    modelProviderList.addEventListener('change', event => {
        const sourceUrl = event.target.closest('[data-provider-field="source_url"]');
        const card = sourceUrl?.closest('[data-provider-card]');
        if (!card) return;
        syncProviderSourceModel(card);
    });
}

function syncProviderSourceModel(card) {
    const sourceCandidate = modelIdFromSourceUrl(
        card.querySelector('[data-provider-field="source_url"]').value
    );
    if (!sourceCandidate) return '';
    const select = card.querySelector('[data-provider-field="selected_model"]');
    if (select.value !== sourceCandidate) {
        const option = document.createElement('option');
        option.value = sourceCandidate;
        option.textContent = sourceCandidate;
        select.replaceChildren(option);
        select.value = sourceCandidate;
        invalidateProviderToolAttestation(card);
    }
    card.querySelector('[data-provider-model-panel]').hidden = false;
    syncProviderModelDefaults(card);
    return sourceCandidate;
}

function populateProviderModels(card, models = []) {
    const select = card.querySelector('[data-provider-field="selected_model"]');
    const sourceCandidate = modelIdFromSourceUrl(
        card.querySelector('[data-provider-field="source_url"]').value
    );
    const previous = select.value;
    const uniqueModels = [...new Set(models.map(model => String(model || '').trim()).filter(Boolean))];
    select.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = uniqueModels.length ? '選擇模型' : '沒有可用模型';
    select.appendChild(placeholder);
    uniqueModels.forEach(model => {
        const option = document.createElement('option');
        option.value = model;
        option.textContent = model;
        select.appendChild(option);
    });
    select.value = uniqueModels.includes(sourceCandidate)
        ? sourceCandidate
        : uniqueModels.includes(previous) ? previous
            : uniqueModels.length === 1 ? uniqueModels[0] : '';
    if (select.value !== previous) invalidateProviderToolAttestation(card);
    card.querySelector('[data-provider-model-panel]').hidden = false;
    syncProviderModelDefaults(card);
    syncProviderCapabilityDefaults(card);
    return select.value;
}

function syncProviderModelDefaults(card) {
    const model = card.querySelector('[data-provider-field="selected_model"]').value;
    const kindSelect = card.querySelector('[data-provider-field="model_kind"]');
    const system = card.querySelector('[data-provider-test-system]');
    kindSelect.value = inferredProviderModelKind(model, '');
    if (model.includes('riva-translate') && !system.value.trim()) {
        system.value = 'en-zh-tw';
    }
    card.querySelector('[data-provider-selected-status]').textContent = model
        ? `已選擇：${model}`
        : '請選擇要驗證的模型';
    syncProviderCapabilityDefaults(card);
}

function renderProviderToolAttestation(card) {
    const state = card.querySelector('[data-provider-tool-attestation]');
    if (!state) return;
    const verified = Boolean(card.providerToolAttestation);
    state.dataset.verified = verified ? 'true' : 'false';
    state.textContent = verified
        ? '\u5de5\u5177\u547c\u53eb\u5df2\u9a57\u8b49\uff0c\u8b8a\u66f4\u6a21\u578b\u3001\u7528\u9014\u3001Endpoint \u6216 tools \u5ba3\u544a\u5f8c\u9700\u91cd\u65b0\u9a57\u8b49\u3002'
        : '\u5c1a\u672a\u5b8c\u6210\u5de5\u5177\u547c\u53eb\u9a57\u8b49\uff1b\u4e0d\u6703\u5ba3\u7a31 Agent \u5df2\u53ef\u4f7f\u7528\u5de5\u5177\u3002';
}

function invalidateProviderToolAttestation(card) {
    card.providerToolAttestation = null;
    renderProviderToolAttestation(card);
}

function syncProviderCapabilityDefaults(card) {
    const model = card.querySelector('[data-provider-field="selected_model"]').value;
    const kindSelect = card.querySelector('[data-provider-field="model_kind"]');
    const system = card.querySelector('[data-provider-test-system]');
    const inferredKind = inferredProviderModelKind(model, '');
    if (['translation', 'embedding', 'rerank', 'vision'].includes(inferredKind)) {
        kindSelect.value = inferredKind;
    }
    const kind = kindSelect.value;
    if (kind === 'translation' && !system.value.trim()) system.value = 'en-zh-cn';
    const languageLabel = system.closest('label');
    if (languageLabel) languageLabel.hidden = kind !== 'translation';
    const toolCapability = card.querySelector('[data-provider-tool-capability]');
    if (toolCapability) toolCapability.hidden = kind !== 'chat';
    const toolDeclaration = card.querySelector('[data-provider-field="supports_tools"]');
    const toolVerification = card.querySelector('[data-provider-tool-verification]');
    if (toolVerification) toolVerification.hidden = kind !== 'chat';
    const toolTestButton = card.querySelector('[data-test-provider-tools]');
    if (toolTestButton) toolTestButton.disabled = kind !== 'chat' || !toolDeclaration?.checked;
    if (kind !== 'chat') {
        if (toolDeclaration) toolDeclaration.checked = false;
        card.providerToolAttestation = null;
    }
    renderProviderToolAttestation(card);
    card.querySelector('[data-provider-selected-status]').textContent = model
        ? kind === 'chat'
            ? `\u5df2\u9078\u64c7\u5c0d\u8a71\u6a21\u578b\uff1a${model}`
            : `\u5df2\u9078\u64c7\u5c08\u7528${kind === 'translation' ? '\u7ffb\u8b6f' : ''}\u5de5\u5177\uff1a${model}\uff08\u4e0d\u6703\u51fa\u73fe\u5728 Agent \u6a21\u578b\u6e05\u55ae\uff09`
        : '\u8acb\u9078\u64c7\u8981\u9a57\u8b49\u7684\u6a21\u578b';
}

async function copyModelProviderEndpoint(card) {
    const endpoint = card.querySelector('[data-provider-field="base_url"]');
    endpoint.select();
    try {
        await navigator.clipboard.writeText(endpoint.value);
        showToast('API Endpoint 已複製。', 'success');
    } catch (_error) {
        document.execCommand('copy');
    }
}

function providerCapabilityPayload(card) {
    const modelKind = card.querySelector('[data-provider-field="model_kind"]').value;
    return {
        model_kind: modelKind,
        language_pair: modelKind === 'translation' ? card.querySelector('[data-provider-test-system]').value.trim() : '',
        supports_tools: card.querySelector('[data-provider-field="supports_tools"]')?.checked === true,
    };
}

async function testModelProviderCard(card) {
    const button = card.querySelector('[data-test-provider]');
    const result = card.querySelector('[data-provider-test-result]');
    syncProviderSourceModel(card);
    const payload = {
        provider_id: card.querySelector('[data-provider-field="id"]').value.trim().toLowerCase(),
        provider_type: card.querySelector('[data-provider-field="provider_type"]').value,
        base_url: card.querySelector('[data-provider-field="base_url"]').value.trim(),
        source_url: card.querySelector('[data-provider-field="source_url"]').value.trim(),
        ...providerCapabilityPayload(card),
        selected_model: card.querySelector('[data-provider-field="selected_model"]').value.trim()
    };
    const key = card.querySelector('[data-provider-field="api_key"]').value.trim();
    if (key) payload.api_key = key;
    button.disabled = true;
    result.className = 'model-provider-test-result testing';
    result.textContent = '正在測試連線…';
    try {
        const response = await apiFetch(`${API_BASE}/api/settings/providers/test`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.detail?.message || data.message || `HTTP ${response.status}`);
        }
        result.className = 'model-provider-test-result connected';
        result.textContent = `連線成功${Number.isFinite(data.model_count) ? `，找到 ${data.model_count} 個模型` : ''}`;
        const selected = populateProviderModels(card, data.models || []);
        if (data.model_profile?.kind) {
            card.querySelector('[data-provider-field="model_kind"]').value =
                inferredProviderModelKind(selected, data.model_profile.kind);
            if (data.model_profile.language_pair) {
                card.querySelector('[data-provider-test-system]').value = data.model_profile.language_pair;
            }
            syncProviderCapabilityDefaults(card);
        }
        if (!selected && modelIdFromSourceUrl(
            card.querySelector('[data-provider-field="source_url"]').value
        )) {
            result.className = 'model-provider-test-result failed';
            result.textContent += '；模型網址對應的模型不在可用清單中';
        }
    } catch (error) {
        result.className = 'model-provider-test-result failed';
        result.textContent = error.message;
    } finally {
        button.disabled = false;
    }
}

async function testProviderToolCapability(card) {
    const button = card.querySelector('[data-test-provider-tools]');
    const state = card.querySelector('[data-provider-tool-attestation]');
    const model = card.querySelector('[data-provider-field="selected_model"]').value.trim();
    const declared = card.querySelector('[data-provider-field="supports_tools"]')?.checked === true;
    if (!model || !declared) {
        invalidateProviderToolAttestation(card);
        state.textContent = !model
            ? '\u8acb\u5148\u9078\u64c7\u5c0d\u8a71\u6a21\u578b\u3002'
            : '\u8acb\u5148\u52fe\u9078\u300c\u4f9b\u61c9\u5546\u5ba3\u544a\u6b64\u6a21\u578b\u652f\u63f4 tools\u300d\u3002';
        return;
    }
    const payload = {
        provider_id: card.querySelector('[data-provider-field="id"]').value.trim().toLowerCase(),
        provider_type: card.querySelector('[data-provider-field="provider_type"]').value,
        base_url: card.querySelector('[data-provider-field="base_url"]').value.trim(),
        source_url: card.querySelector('[data-provider-field="source_url"]').value.trim(),
        selected_model: model,
        model,
        ...providerCapabilityPayload(card)
    };
    const key = card.querySelector('[data-provider-field="api_key"]').value.trim();
    if (key) payload.api_key = key;
    button.disabled = true;
    state.dataset.verified = 'false';
    state.textContent = '\u6b63\u5728\u9a57\u8b49\u771f\u5be6\u5de5\u5177\u547c\u53eb\u2026';
    try {
        const response = await apiFetch(`${API_BASE}/api/settings/providers/tool-test`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!response.ok || !data.success || !data.tool_attestation || typeof data.tool_attestation !== 'object') {
            throw new Error(data.detail?.message || data.message || `HTTP ${response.status}`);
        }
        card.providerToolAttestation = { ...data.tool_attestation };
        renderProviderToolAttestation(card);
    } catch (error) {
        invalidateProviderToolAttestation(card);
        state.textContent = `\u5de5\u5177\u547c\u53eb\u9a57\u8b49\u5931\u6557\uff1a${error.message}`;
    } finally {
        button.disabled = !card.querySelector('[data-provider-field="supports_tools"]')?.checked;
    }
}
async function testProviderModelCard(card) {
    const button = card.querySelector('[data-test-provider-model]');
    const output = card.querySelector('[data-provider-response]');
    const status = card.querySelector('[data-provider-selected-status]');
    syncProviderSourceModel(card);
    const model = card.querySelector('[data-provider-field="selected_model"]').value.trim();
    if (!model) {
        output.textContent = '請先測試連線並選擇模型。';
        return;
    }
    const payload = {
        provider_id: card.querySelector('[data-provider-field="id"]').value.trim().toLowerCase(),
        provider_type: card.querySelector('[data-provider-field="provider_type"]').value,
        base_url: card.querySelector('[data-provider-field="base_url"]').value.trim(),
        ...providerCapabilityPayload(card),
        model,
        system_prompt: card.querySelector('[data-provider-test-system]').value.trim(),
        prompt: card.querySelector('[data-provider-test-prompt]').value.trim()
    };
    const key = card.querySelector('[data-provider-field="api_key"]').value.trim();
    if (key) payload.api_key = key;
    button.disabled = true;
    output.className = 'model-provider-response testing';
    output.textContent = '正在等待指定模型回覆…';
    try {
        const response = await apiFetch(`${API_BASE}/api/settings/providers/model-test`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.detail?.message || data.message || `HTTP ${response.status}`);
        }
        output.className = 'model-provider-response connected';
        output.textContent = data.response;
        // A normal text response never grants tool capability. Only the
        // dedicated tool-test route may return an accepted attestation.
        status.textContent = `已由 ${data.selected_model} 實際回覆；儲存後可在 Model Manager 切換`;
        const kind = data.model_profile?.kind || payload.model_kind;
        status.textContent = kind === 'chat'
            ? `\u5df2\u7531 ${data.selected_model} \u5be6\u969b\u56de\u8986\uff1b\u5132\u5b58\u5f8c\u53ef\u5728 Model Manager \u5207\u63db`
            : `\u5df2\u7531 ${data.selected_model} \u5b8c\u6210\u5c08\u7528${kind === 'translation' ? '\u7ffb\u8b6f' : ''}\u6e2c\u8a66\uff1b\u4e0d\u6703\u5217\u5165 Agent \u6a21\u578b`;
    } catch (error) {
        output.className = 'model-provider-response failed';
        output.textContent = error.message;
        status.textContent = '模型回覆驗證失敗';
    } finally {
        button.disabled = false;
    }
}

async function saveModelProviderSecrets() {
    const cards = [...(modelProviderList?.querySelectorAll('[data-provider-card]') || [])];
    for (const card of cards) {
        const providerId = card.querySelector('[data-provider-field="id"]').value.trim().toLowerCase();
        const apiKeyInput = card.querySelector('[data-provider-field="api_key"]');
        const apiKey = apiKeyInput.value.trim();
        if (!apiKey) continue;
        const response = await apiFetch(`${API_BASE}/api/settings/secrets`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider_id: providerId, api_key: apiKey })
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.detail?.message || data.message || `無法保存 ${providerId} 金鑰`);
        }
        apiKeyInput.value = '';
    }
    for (const providerId of removedProviderSecrets) {
        const response = await apiFetch(`${API_BASE}/api/settings/secrets/${encodeURIComponent(providerId)}`, {
            method: 'DELETE'
        });
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            throw new Error(data.detail?.message || data.message || `無法刪除 ${providerId} 金鑰`);
        }
    }
    removedProviderSecrets.clear();
}

(function createTrustedExtensionCenter() {
    'use strict';

    const N8N_EXTENSION_ID = 'builtin.n8n';
    const N8N_EDITOR_URL = 'http://127.0.0.1:5678/';

    const state = {
        deps: null,
        activeTab: 'installed',
        projectId: null,
        response: { extensions: [], sections: {} },
        selectedExtensionId: null,
        detailReturnExtensionId: null,
        detailReturnTab: 'installed',
        n8nService: null,
        n8nServiceLoading: false,
        pendingReview: null,
        settingsProject: null,
        initialized: false
    };

    const byId = id => document.getElementById(id);
    const encoded = value => encodeURIComponent(String(value || ''));
    const iconFor = item => item?.id === N8N_EXTENSION_ID ? 'workflow' : ({
        workflow: 'workflow',
        integration: 'workflow',
        mcp: 'plug',
        provider: 'cloud',
        model_provider: 'cloud',
        excel: 'table-2',
        application: 'app-window',
        cli: 'terminal',
        builtin: 'puzzle',
        local: 'package-open'
    }[item.kind] || (item.origin === 'local' ? 'package-open' : 'puzzle'));

    function messageFrom(data, fallback) {
        const detail = data?.detail;
        return detail?.message || (typeof detail === 'string' ? detail : '') || data?.message || fallback;
    }

    async function request(path, options = {}) {
        if (!state.deps?.apiFetch) throw new Error('擴充中心尚未初始化');
        const response = await state.deps.apiFetch(`${state.deps.apiBase || ''}${path}`, options);
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.success === false) {
            const error = new Error(messageFrom(data, `HTTP ${response.status}`));
            error.status = response.status;
            error.code = data?.detail?.code || data?.code || null;
            error.payload = data;
            throw error;
        }
        return data;
    }

    function projectQuery(projectId = state.projectId) {
        return projectId ? `?project_id=${encoded(projectId)}` : '';
    }

    function projectOptions(preferredProjectId = null) {
        const select = byId('extension-scope-select');
        if (!select) return;
        const prior = preferredProjectId || state.projectId || '';
        select.replaceChildren();
        const globalOption = document.createElement('option');
        globalOption.value = 'global';
        globalOption.textContent = '所有專案（全域）';
        select.appendChild(globalOption);
        const projects = state.deps?.getProjects?.() || [];
        projects.filter(project => !project.archived).forEach(project => {
            const option = document.createElement('option');
            option.value = String(project.id);
            option.textContent = `專案：${project.name}`;
            select.appendChild(option);
        });
        select.value = prior && projects.some(project => String(project.id) === String(prior))
            ? String(prior)
            : 'global';
        state.projectId = select.value === 'global' ? null : select.value;
    }

    function setTab(tab) {
        state.activeTab = ['installed', 'available', 'connections', 'local', 'developer'].includes(tab)
            ? tab
            : 'installed';
        document.querySelectorAll('.extension-tab-btn[data-extension-tab]').forEach(button => {
            const active = button.dataset.extensionTab === state.activeTab;
            button.classList.toggle('active', active);
            button.setAttribute('aria-selected', active ? 'true' : 'false');
            button.tabIndex = active ? 0 : -1;
        });
        document.querySelectorAll('.extension-tab-panel[data-extension-panel]').forEach(panel => {
            const active = panel.dataset.extensionPanel === state.activeTab;
            panel.classList.toggle('active', active);
            panel.hidden = !active;
        });
        const search = byId('extension-search');
        const searchable = ['installed', 'available', 'local'].includes(state.activeTab);
        if (search?.closest('.extension-search')) search.closest('.extension-search').hidden = !searchable;
        const globalOption = byId('extension-scope-select')?.querySelector('option[value="global"]');
        if (globalOption && state.activeTab !== 'connections') globalOption.disabled = false;
    }

    function ensureConnectionProjectScope() {
        const select = byId('extension-scope-select');
        const projects = (state.deps?.getProjects?.() || []).filter(project => !project.archived);
        const active = state.deps?.getActiveProjectId?.();
        const preferred = state.projectId || active || projects[0]?.id || null;
        const valid = preferred && projects.some(project => String(project.id) === String(preferred));
        const nextProjectId = valid ? String(preferred) : null;
        const changed = state.projectId !== nextProjectId;
        state.projectId = nextProjectId;
        if (select) {
            const globalOption = select.querySelector('option[value="global"]');
            if (globalOption) globalOption.disabled = !!nextProjectId;
            select.value = nextProjectId || 'global';
        }
        return changed;
    }

    async function refreshConnections() {
        ensureConnectionProjectScope();
        window.workbenchConnectors?.setProject?.(state.projectId);
        await window.workbenchConnectors?.refresh?.({ projectId: state.projectId });
    }

    async function selectWorkspaceTab(tab) {
        setTab(tab);
        if (state.activeTab !== 'connections') return;
        const scopeChanged = ensureConnectionProjectScope();
        if (scopeChanged) await loadCatalog().catch(() => {});
        await refreshConnections().catch(() => {});
    }

    async function refreshActiveTab() {
        await loadCatalog();
        if (state.activeTab === 'connections') await refreshConnections();
    }

    function healthInfo(item) {
        const raw = item.health;
        if (typeof raw === 'string') return { status: raw, message: raw };
        const detail = raw?.detail || {};
        return {
            status: raw?.status || 'unknown',
            message: raw?.message || raw?.error || detail.message || detail.reason || detail.error || '',
            checkedAt: raw?.checked_at || raw?.checkedAt || ''
        };
    }

    function permissionInfo(permission) {
        if (typeof permission === 'string') return { name: permission, risk: '' };
        return {
            name: permission?.name || permission?.id || permission?.capability || '未命名權限',
            risk: permission?.risk || permission?.risk_level || '',
            description: permission?.description || ''
        };
    }

    function catalogSectionItems(response, section) {
        const all = response?.extensions || [];
        const lookup = new Map(all.map(item => [String(item.id), item]));
        const normalize = entries => (Array.isArray(entries) ? entries : [])
            .map(item => typeof item === 'string' ? lookup.get(item) : item)
            .filter(Boolean);
        const explicit = normalize(response?.sections?.[section]);
        if (section === 'available') {
            // Keep unavailable catalog entries discoverable even if an older
            // release recorded them as installed. Their controls remain
            // fail-closed through extensionControlPolicy().
            const unavailable = normalize(response?.sections?.unavailable);
            const unique = new Map(
                [...explicit, ...unavailable]
                    .filter(item => item.installed !== true)
                    .map(item => [String(item.id), item])
            );
            if (unique.size) return [...unique.values()];
        }
        if (explicit.length) return explicit;
        if (section === 'installed') return all.filter(item => item.installed);
        if (section === 'available') {
            return all.filter(item => item.installed !== true && (item.available || item.runtime_available === false));
        }
        return all.filter(item => item.origin === 'local');
    }

    function sectionItems(section) {
        return catalogSectionItems(state.response, section);
    }

    function unavailableExplanation(item) {
        if (item?.runtime_available !== false) return '';
        const reason = String(item.availability_reason || 'adapter_unavailable');
        return ({
            cursor_adapter_not_implemented: '此版本尚未提供 Cursor adapter。',
            excel_adapter_not_implemented: '此版本尚未提供 Excel adapter。'
        })[reason] || `此版本暫時無法使用此擴充（${reason}）。`;
    }

    function extensionControlPolicy(item) {
        const unavailable = item?.runtime_available === false;
        const globallyEnabled = item?.installed === true && item?.global_enabled === true;
        return {
            unavailable,
            explanation: unavailableExplanation(item),
            canInstall: item?.installed !== true && item?.available === true && !unavailable,
            // An already-enabled unavailable extension may still be switched
            // off, but an unavailable extension can never be switched on.
            canToggleGlobal: item?.installed === true && (!unavailable || globallyEnabled),
            canEnable: item?.installed === true && !globallyEnabled && !unavailable
        };
    }

    function stateBlock(text, className = '') {
        const block = document.createElement('div');
        block.className = `extension-state ${className}`.trim();
        block.textContent = text;
        return block;
    }

    function actionButton(label, action, icon, className = 'btn btn-secondary compact') {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = className;
        button.dataset.extensionAction = action;
        if (icon) {
            const iconElement = document.createElement('i');
            iconElement.dataset.lucide = icon;
            button.appendChild(iconElement);
        }
        button.appendChild(document.createTextNode(label));
        return button;
    }

    function badge(text, className = '') {
        const element = document.createElement('span');
        element.className = `extension-badge ${className}`.trim();
        element.textContent = text;
        return element;
    }

    function createExtensionCard(item) {
        const card = document.createElement('article');
        card.className = `extension-card extension-discovery-card ${item.runtime_available === false ? 'is-disabled' : ''}`.trim();
        card.dataset.extensionId = String(item.id);
        const controlPolicy = extensionControlPolicy(item);

        const head = document.createElement('div');
        head.className = 'extension-card-head';
        const icon = document.createElement('span');
        icon.className = 'extension-card-icon';
        const iconElement = document.createElement('i');
        iconElement.dataset.lucide = iconFor(item);
        icon.appendChild(iconElement);
        const copy = document.createElement('div');
        copy.className = 'extension-card-copy';
        const title = document.createElement('div');
        title.className = 'extension-card-title';
        const titleText = document.createElement('span');
        titleText.textContent = item.name || item.id;
        title.appendChild(titleText);
        const meta = document.createElement('div');
        meta.className = 'extension-card-meta';
        meta.textContent = [
            item.category || item.kind || '擴充功能',
            item.publisher || (item.origin === 'builtin' ? 'Workbench' : '本機')
        ].filter(Boolean).join(' · ');
        const description = document.createElement('div');
        description.className = 'extension-card-description';
        description.textContent = item.id === N8N_EXTENSION_ID
            ? '建立及執行本機自動化工作流程，並以 Workbench 治理 Gmail 草稿與核准。'
            : item.description || '沒有提供說明。';
        copy.append(title, meta, description);
        head.append(icon, copy);

        const footer = document.createElement('div');
        footer.className = 'extension-card-summary-footer';
        const stateLabel = document.createElement('span');
        stateLabel.className = `extension-card-state ${item.effective_enabled ? 'is-active' : ''}`.trim();
        stateLabel.textContent = item.runtime_available === false
            ? '目前無法使用'
            : item.installed ? (item.effective_enabled ? '已啟用' : '已安裝') : '選用擴充';
        const actions = document.createElement('div');
        actions.className = 'extension-card-actions extension-card-primary-action';
        if (!item.installed && (item.available || controlPolicy.unavailable)) {
            const install = actionButton(
                controlPolicy.unavailable
                    ? '目前不可安裝'
                    : '安裝',
                'install',
                'download',
                'btn btn-primary compact'
            );
            install.disabled = !controlPolicy.canInstall;
            if (controlPolicy.unavailable) install.title = controlPolicy.explanation;
            if (controlPolicy.canInstall) {
                install.addEventListener('click', () => openPermissionReview(item, 'install', {
                    onComplete: () => openExtensionDetail(item.id),
                }));
            }
            actions.appendChild(install);
        }
        if (item.installed) {
            const openButton = actionButton('查看詳情', 'open', 'chevron-right', 'btn btn-secondary compact');
            openButton.addEventListener('click', () => openExtensionDetail(item.id));
            actions.appendChild(openButton);
        }

        footer.append(stateLabel, actions);
        card.append(head, footer);
        return card;
    }

    function projectOverrideSelect(item, projectId = state.projectId) {
        const select = document.createElement('select');
        select.className = 'settings-input extension-project-override';
        select.dataset.extensionProjectOverride = String(item.id);
        const canEnable = !!(
            item.installed
            && item.trusted
            && item.runtime_available !== false
            && item.configuration_enabled !== false
            && item.global_enabled === true
            && item.global_approval_current === true
        );
        [
            ['inherit', '繼承全域'],
            ['enabled', '此專案啟用'],
            ['disabled', '此專案停用']
        ].forEach(([value, label]) => {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = label;
            if (value === 'enabled') option.disabled = !canEnable;
            select.appendChild(option);
        });
        select.value = ['inherit', 'enabled', 'disabled'].includes(item.project_override)
            ? item.project_override
            : 'inherit';
        select.dataset.previousMode = select.value;
        select.title = canEnable
            ? '設定此專案的擴充覆寫'
            : '可繼承或停用；如要在此專案啟用，請先完成全域信任與啟用';
        select.addEventListener('change', () => {
            const mode = select.value;
            const previous = select.dataset.previousMode || 'inherit';
            if (mode === 'enabled' && previous !== 'enabled') {
                select.value = previous;
                openPermissionReview(item, 'project_enable', { projectId, mode, select });
                return;
            }
            mutateProjectOverride(item, projectId, mode, select);
        });
        return select;
    }

    function detailElement(tag, className = '', text = null) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== null) node.textContent = String(text);
        return node;
    }

    function currentDetailItem() {
        return (state.response.extensions || [])
            .find(item => String(item.id) === String(state.selectedExtensionId)) || null;
    }

    function detailSection(stage, eyebrow, title, description = '') {
        const section = detailElement('section', 'extension-detail-section');
        section.dataset.extensionDetailStage = stage;
        const header = detailElement('div', 'extension-detail-section-head');
        const copy = detailElement('div');
        if (eyebrow) copy.appendChild(detailElement('span', 'workflow-eyebrow', eyebrow));
        copy.appendChild(detailElement('h2', '', title));
        if (description) copy.appendChild(detailElement('p', '', description));
        header.appendChild(copy);
        const body = detailElement('div', 'extension-detail-section-body');
        section.append(header, body);
        return { section, header, body };
    }

    function detailSettingRow(title, description = '') {
        const row = detailElement('div', 'extension-detail-setting-row');
        const copy = detailElement('div', 'extension-detail-setting-copy');
        copy.appendChild(detailElement('strong', '', title));
        if (description) copy.appendChild(detailElement('span', '', description));
        const actions = detailElement('div', 'extension-detail-setting-actions');
        row.append(copy, actions);
        return { row, copy, actions };
    }

    function capabilityLabel(value) {
        return ({
            workflow_automation: ['工作流程管理', '建立、檢視並執行受治理的本機 Workflow。'],
            gmail_governed_drafts: ['Gmail Workflow', '接收標籤事件並回到 Workbench 編輯與核准草稿。'],
            local_models: ['本機模型', '在 Workbench 使用本機推論服務。'],
            repositories: ['Repositories', '在核准範圍內存取程式碼庫。'],
            issues: ['Issues', '讀取與管理議題。'],
            pull_requests: ['Pull Requests', '檢視與管理合併請求。'],
            checks: ['Checks', '讀取自動化檢查結果。'],
            pages: ['Pages', '讀取與管理頁面內容。'],
            databases: ['Databases', '存取已授權的資料庫。'],
            blocks: ['Blocks', '讀取與更新內容區塊。'],
        })[String(value)] || [String(value).replaceAll('_', ' '), '由此擴充提供的功能。'];
    }

    function appendExtensionAuthorization(body, item) {
        const controlPolicy = extensionControlPolicy(item);
        const globalEnabled = item.global_enabled === true;
        const authorization = detailSettingRow(
            'Workbench 擴充權限',
            globalEnabled ? '已通過全域審查；可再依專案覆寫。' : '目前不會向 Agent 或工作流程提供能力。'
        );
        const toggle = detailElement('label', 'extension-detail-toggle');
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = globalEnabled;
        checkbox.disabled = !controlPolicy.canToggleGlobal;
        checkbox.dataset.extensionGlobalToggle = String(item.id);
        toggle.append(checkbox, document.createTextNode(globalEnabled ? '已啟用' : '已停用'));
        checkbox.addEventListener('change', () => {
            if (checkbox.checked) {
                checkbox.checked = false;
                openPermissionReview(item, 'enable', {
                    onComplete: () => openExtensionDetail(item.id),
                });
            } else {
                mutateGlobalState(item, false, checkbox);
            }
        });
        authorization.actions.appendChild(toggle);
        body.appendChild(authorization.row);

        if (state.projectId) {
            const project = detailSettingRow('專案作用範圍', '覆寫目前專案是否能使用此擴充。');
            project.actions.appendChild(projectOverrideSelect(item));
            body.appendChild(project.row);
        }

        const maintenance = detailSettingRow('檢查與管理', '健康狀態與稽核紀錄只在需要時查看。');
        const healthButton = actionButton('健康檢查', 'health', 'activity');
        healthButton.addEventListener('click', () => refreshHealth(item, healthButton));
        const auditButton = actionButton('查看 Audit', 'audit', 'scroll-text');
        auditButton.addEventListener('click', () => openAudit(item));
        maintenance.actions.append(healthButton, auditButton);
        if (item.removable) {
            const remove = actionButton('移除擴充', 'remove', 'trash-2', 'btn btn-danger compact');
            remove.addEventListener('click', () => removeExtension(item, remove));
            maintenance.actions.appendChild(remove);
        }
        body.appendChild(maintenance.row);
    }

    function appendN8nRuntimeSetting(body, item) {
        const service = state.n8nService || {};
        const running = service.running === true || service.reachable === true;
        const starting = service.starting === true;
        const runtimeInstalled = service.installed === true;
        const runtime = detailSettingRow(
            '本機 n8n 服務',
            state.n8nServiceLoading
                ? '正在檢查服務狀態…'
                : service.load_error || service.message || (running ? '服務已可使用。' : '服務目前未啟動。')
        );
        const status = badge(
            state.n8nServiceLoading ? '檢查中'
                : running ? '已啟動' : starting ? '啟動中' : runtimeInstalled ? '未啟動' : '執行環境未就緒',
            running ? 'is-healthy' : service.load_error ? 'is-error' : 'is-warning'
        );
        const start = actionButton('啟動', 'n8n-start', 'play', 'btn btn-primary compact');
        const stop = actionButton('停止', 'n8n-stop', 'square');
        start.disabled = state.n8nServiceLoading || !item.effective_enabled || !runtimeInstalled || running || service.starting === true;
        stop.disabled = state.n8nServiceLoading || !item.effective_enabled || !running;
        start.addEventListener('click', () => runN8nServiceAction('start', start));
        stop.addEventListener('click', () => runN8nServiceAction('stop', stop));
        runtime.actions.append(status, start, stop);
        body.appendChild(runtime.row);
    }

    function appendN8nFeatures(body, item) {
        const service = state.n8nService || {};
        const running = service.running === true || service.reachable === true;
        const workflow = detailSettingRow('Workflow 管理', '在 Workbench 規劃、核准並管理 n8n 工作流程。');
        const use = actionButton('立即使用', 'n8n-use', 'arrow-up-right', 'btn btn-primary compact');
        use.disabled = !item.effective_enabled;
        use.addEventListener('click', openN8nWorkspace);
        workflow.actions.appendChild(use);
        body.appendChild(workflow.row);

        const gmailReady = service.workflow_ready === true;
        const gmail = detailSettingRow(
            'Gmail Workflow',
            gmailReady ? '受治理的 Gmail 工作流程已完成驗證。' : '尚未通過 Workflow 與 Credential readiness 檢查。'
        );
        gmail.actions.appendChild(badge(gmailReady ? '已就緒' : '尚未就緒', gmailReady ? 'is-healthy' : 'is-warning'));
        body.appendChild(gmail.row);

        const editor = detailSettingRow('n8n 編輯器', '在外部瀏覽器開啟固定的本機端點；不會內嵌到 Workbench。');
        const open = actionButton('在瀏覽器開啟', 'n8n-open', 'external-link');
        open.disabled = !item.effective_enabled || !running || !service.editor_url;
        open.addEventListener('click', () => window.workbenchN8nWorkflows?.openEditor?.());
        editor.actions.appendChild(open);
        body.appendChild(editor.row);
    }

    function appendTechnicalDetails(container, item) {
        const service = item.id === N8N_EXTENSION_ID ? state.n8nService || {} : {};
        const details = detailElement('details', 'extension-technical-details');
        details.dataset.extensionDetailStage = 'technical';
        details.appendChild(detailElement('summary', '', '技術資訊'));
        const metrics = detailElement('dl', 'extension-detail-metrics');
        const values = item.id === N8N_EXTENSION_ID ? [
            ['n8n 版本', service.version || '—'],
            ['本機端點', service.editor_url || N8N_EDITOR_URL],
            ['Gmail Workflow', service.workflow_ready === true ? '已就緒' : '尚未就緒'],
            ['擴充版本', item.version || '—'],
        ] : [
            ['版本', item.version || '—'],
            ['來源', item.publisher || item.origin || '—'],
            ['類型', item.kind || '—'],
        ];
        values.forEach(([label, value]) => {
            const row = detailElement('div');
            row.append(detailElement('dt', '', label), detailElement('dd', '', value));
            metrics.appendChild(row);
        });
        details.appendChild(metrics);

        const permissions = detailElement('div', 'extension-detail-permissions');
        permissions.appendChild(detailElement('strong', '', '權限與安全'));
        const chips = detailElement('div', 'extension-permissions');
        const permissionItems = (item.permissions || []).map(permissionInfo);
        (permissionItems.length ? permissionItems : [{ name: '未要求額外權限', risk: '' }]).forEach(permission => {
            const chip = detailElement(
                'span',
                `extension-permission-chip ${['system', 'irreversible', 'external_write'].includes(permission.risk) ? 'is-danger' : ''}`.trim(),
                permission.risk ? `${permission.name} · ${permission.risk}` : permission.name
            );
            chip.title = permission.description || chip.textContent;
            chips.appendChild(chip);
        });
        permissions.appendChild(chips);
        const digest = detailElement('code', 'extension-detail-digest', item.manifest_sha256 || '未提供 manifest digest');
        permissions.appendChild(digest);
        details.appendChild(permissions);
        container.appendChild(details);
    }

    function captureDetailViewState(container) {
        const snapshot = {
            technicalOpen: container.querySelector('.extension-technical-details')?.open === true,
            focus: null,
        };
        const active = document.activeElement;
        if (!active || !container.contains(active)) return snapshot;
        if (active.id) {
            snapshot.focus = { type: 'id', value: active.id };
            return snapshot;
        }
        for (const key of ['extensionAction', 'extensionGlobalToggle', 'extensionProjectOverride']) {
            const value = active.dataset?.[key];
            if (value) {
                snapshot.focus = { type: key, value };
                return snapshot;
            }
        }
        if (active.tagName === 'SUMMARY') snapshot.focus = { type: 'summary', value: '' };
        return snapshot;
    }

    function restoreDetailViewState(container, snapshot) {
        const technical = container.querySelector('.extension-technical-details');
        if (technical) technical.open = snapshot.technicalOpen;
        if (!snapshot.focus) return;
        let target = null;
        if (snapshot.focus.type === 'id') {
            const candidate = document.getElementById(snapshot.focus.value);
            if (candidate && container.contains(candidate)) target = candidate;
        } else if (snapshot.focus.type === 'summary') {
            target = technical?.querySelector('summary') || null;
        } else {
            target = [...container.querySelectorAll('[data-extension-action], [data-extension-global-toggle], [data-extension-project-override]')]
                .find(node => node.dataset?.[snapshot.focus.type] === snapshot.focus.value) || null;
        }
        if (target?.disabled) target = null;
        (target || byId('extension-detail-title'))?.focus?.({ preventScroll: true });
    }

    function renderExtensionDetail() {
        const container = byId('extension-detail-content');
        const item = currentDetailItem();
        if (!container || !item) return;
        const viewState = captureDetailViewState(container);
        container.replaceChildren();

        const hero = detailElement('article', 'extension-detail-hero');
        hero.dataset.extensionDetailStage = 'use';
        const identity = detailElement('div', 'extension-detail-identity');
        const icon = detailElement('span', 'extension-detail-icon');
        const iconNode = detailElement('i');
        iconNode.dataset.lucide = iconFor(item);
        icon.appendChild(iconNode);
        const copy = detailElement('div');
        copy.appendChild(detailElement('span', 'workflow-eyebrow', item.category || 'EXTENSION'));
        const title = detailElement('h1', '', item.name || item.id);
        title.id = 'extension-detail-title';
        title.tabIndex = -1;
        copy.appendChild(title);
        copy.appendChild(detailElement(
            'p', '',
            item.id === N8N_EXTENSION_ID
                ? '將本機自動化工作流程連接至 Workbench，保留人工核准與安全邊界。'
                : item.description || '由 Workbench 管理的選用擴充功能。'
        ));
        identity.append(icon, copy);
        const heroActions = detailElement('div', 'extension-detail-hero-actions');
        heroActions.appendChild(badge(
            item.effective_enabled ? '可使用' : item.installed ? '已安裝，未啟用' : '尚未安裝',
            item.effective_enabled ? 'is-healthy' : 'is-warning'
        ));
        if (!item.installed) {
            const install = actionButton('安裝', 'install', 'download', 'btn btn-primary');
            install.disabled = !extensionControlPolicy(item).canInstall;
            install.addEventListener('click', () => openPermissionReview(item, 'install', {
                onComplete: () => openExtensionDetail(item.id),
            }));
            heroActions.appendChild(install);
        } else if (!item.effective_enabled) {
            const enable = actionButton('審查並啟用', 'enable', 'power', 'btn btn-primary');
            enable.addEventListener('click', () => openPermissionReview(
                item,
                state.projectId && item.global_enabled ? 'project_enable' : 'enable',
                state.projectId && item.global_enabled
                    ? { projectId: state.projectId, onComplete: () => openExtensionDetail(item.id) }
                    : { onComplete: () => openExtensionDetail(item.id) }
            ));
            heroActions.appendChild(enable);
        } else if (item.id === N8N_EXTENSION_ID) {
            const use = actionButton('立即使用', 'n8n-use', 'arrow-up-right', 'btn btn-primary');
            use.addEventListener('click', openN8nWorkspace);
            heroActions.appendChild(use);
        } else if (item.connection_required) {
            const connect = actionButton('設定連線', 'connections', 'link', 'btn btn-primary');
            connect.addEventListener('click', () => {
                closeExtensionDetail();
                void selectWorkspaceTab('connections');
            });
            heroActions.appendChild(connect);
        }
        hero.append(identity, heroActions);
        container.appendChild(hero);

        if (item.installed) {
            const settings = detailSection('settings', 'SETTINGS', '設定', '先管理服務與擴充權限，再視需要查看其他能力。');
            if (item.id === N8N_EXTENSION_ID) appendN8nRuntimeSetting(settings.body, item);
            appendExtensionAuthorization(settings.body, item);
            container.appendChild(settings.section);

            const features = detailSection('features', 'FEATURES', '功能', '安裝後可使用的主要能力。');
            if (item.id === N8N_EXTENSION_ID) {
                appendN8nFeatures(features.body, item);
            } else {
                const capabilities = Array.isArray(item.capabilities) ? item.capabilities : [];
                (capabilities.length ? capabilities : ['extension_capability']).forEach(capability => {
                    const [label, description] = capabilityLabel(capability);
                    features.body.appendChild(detailSettingRow(label, description).row);
                });
            }
            container.appendChild(features.section);
            appendTechnicalDetails(container, item);
        }
        restoreDetailViewState(container, viewState);
        safeCreateIcons();
    }

    async function refreshN8nService() {
        state.n8nServiceLoading = true;
        if (state.selectedExtensionId === N8N_EXTENSION_ID) renderExtensionDetail();
        try {
            const workflows = window.workbenchN8nWorkflows;
            state.n8nService = typeof workflows?.refreshService === 'function'
                ? await workflows.refreshService()
                : await request('/api/integrations/n8n/status');
        } catch (error) {
            state.n8nService = { load_error: `無法取得 n8n 狀態：${error.message}` };
        } finally {
            state.n8nServiceLoading = false;
            if (state.selectedExtensionId === N8N_EXTENSION_ID) renderExtensionDetail();
        }
        return state.n8nService;
    }

    async function runN8nServiceAction(action, button) {
        button.disabled = true;
        try {
            if (typeof window.workbenchN8nWorkflows?.manageService === 'function') {
                state.n8nService = await window.workbenchN8nWorkflows.manageService(action, button);
            } else {
                state.n8nService = await request(`/api/integrations/n8n/${action}`, { method: 'POST' });
            }
            await refreshN8nService();
        } catch (error) {
            state.deps?.showToast?.(`n8n 操作失敗：${error.message}`, 'error');
            button.disabled = false;
        }
    }

    function openN8nWorkspace() {
        close();
        void window.workbenchN8nWorkflows?.open?.();
    }

    async function openExtensionDetail(extensionId) {
        let item = (state.response.extensions || []).find(entry => String(entry.id) === String(extensionId));
        if (!item) {
            await loadCatalog().catch(() => {});
            item = (state.response.extensions || []).find(entry => String(entry.id) === String(extensionId));
        }
        if (!item) return;
        state.selectedExtensionId = String(item.id);
        state.detailReturnExtensionId = String(item.id);
        state.detailReturnTab = item.installed ? 'installed' : 'available';
        byId('extension-center-workspace')?.classList.add('is-detail');
        document.querySelectorAll('.extension-tab-panel[data-extension-panel]').forEach(panel => {
            panel.hidden = true;
            panel.classList.remove('active');
        });
        byId('extension-detail-view').hidden = false;
        renderExtensionDetail();
        if (item.id === N8N_EXTENSION_ID) await refreshN8nService();
        setTimeout(() => byId('extension-detail-title')?.focus(), 20);
    }

    function closeExtensionDetail() {
        const returnExtensionId = state.detailReturnExtensionId;
        state.selectedExtensionId = null;
        state.detailReturnExtensionId = null;
        state.n8nService = null;
        state.n8nServiceLoading = false;
        byId('extension-detail-view').hidden = true;
        byId('extension-center-workspace')?.classList.remove('is-detail');
        setTab(state.detailReturnTab || state.activeTab);
        setTimeout(() => {
            const list = byId(`extension-${state.activeTab}-list`);
            const card = [...(list?.querySelectorAll?.('[data-extension-id]') || [])]
                .find(node => String(node.dataset.extensionId) === String(returnExtensionId));
            card?.querySelector?.('[data-extension-action="open"], [data-extension-action="install"]')?.focus?.();
        }, 0);
    }

    function renderLists() {
        const query = (byId('extension-search')?.value || '').trim().toLocaleLowerCase();
        ['installed', 'available'].forEach(section => {
            const count = sectionItems(section).length;
            const node = byId(`extension-${section}-count`);
            if (!node) return;
            node.textContent = String(count);
            node.setAttribute('aria-label', `${count} 個${section === 'installed' ? '已安裝' : '未安裝'}`);
        });
        ['installed', 'available', 'local'].forEach(section => {
            const list = byId(`extension-${section}-list`);
            if (!list) return;
            const items = sectionItems(section).filter(item => !query || [
                item.name,
                item.id,
                item.kind,
                item.publisher,
                item.description
            ].some(value => String(value || '').toLocaleLowerCase().includes(query)));
            list.replaceChildren();
            if (!items.length) {
                list.appendChild(stateBlock(query ? '找不到符合的擴充。' : {
                    installed: '尚未安裝任何擴充。',
                    available: '目前沒有未安裝的擴充。',
                    local: '尚未註冊本機受信任擴充。'
                }[section]));
                return;
            }
            items.forEach(item => list.appendChild(createExtensionCard(item)));
        });
        safeCreateIcons();
    }

    function loading() {
        ['installed', 'available'].forEach(section => {
            byId(`extension-${section}-list`)?.replaceChildren(stateBlock('載入擴充中…', 'is-loading'));
        });
        if (state.activeTab === 'local' && !byId('extension-local-path')?.value) {
            byId('extension-local-list')?.replaceChildren(stateBlock('載入本機擴充中…', 'is-loading'));
        }
    }

    function renderError(error) {
        ['installed', 'available', 'local'].forEach(section => {
            byId(`extension-${section}-list`)?.replaceChildren(
                stateBlock(`無法載入擴充：${error.message}`, 'is-error')
            );
        });
    }

    async function loadCatalog() {
        loading();
        const suffix = state.projectId ? `?project_id=${encoded(state.projectId)}` : '';
        try {
            const data = await request(`/api/extensions${suffix}`);
            state.response = {
                extensions: Array.isArray(data.extensions) ? data.extensions : [],
                sections: data.sections || {}
            };
            renderLists();
            if (state.selectedExtensionId) renderExtensionDetail();
            return state.response;
        } catch (error) {
            renderError(error);
            throw error;
        }
    }

    async function mutateGlobalState(item, enabled, control) {
        control.disabled = true;
        try {
            await request(`/api/extensions/${encoded(item.id)}/state${projectQuery()}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    global_enabled: !!enabled,
                    manifest_sha256: enabled ? item.manifest_sha256 : null
                })
            });
            state.deps?.showToast?.(`${item.name || item.id} 已${enabled ? '啟用' : '停用'}。`, 'success');
            await loadCatalog();
            if (item.id === N8N_EXTENSION_ID) {
                await window.workbenchN8nWorkflows?.refreshExtensionState?.();
                await refreshN8nService();
            }
            await state.deps?.reloadProject?.();
        } catch (error) {
            state.deps?.showToast?.(`更新擴充失敗：${error.message}`, 'error');
            // A failed cleanup response may still have committed an
            // authority-reducing disable. Reload the server snapshot instead
            // of restoring a stale client-side enabled control.
            await loadCatalog().catch(() => {
                control.disabled = false;
                renderLists();
            });
        }
    }

    async function mutateProjectOverride(item, projectId, mode, control) {
        if (!projectId) return;
        control.disabled = true;
        try {
            await request(`/api/projects/${encoded(projectId)}/extensions/${encoded(item.id)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    mode,
                    ...(mode === 'enabled' ? { manifest_sha256: item.manifest_sha256 } : {})
                })
            });
            control.dataset.previousMode = mode;
            state.deps?.showToast?.(`${item.name || item.id} 的專案設定已更新。`, 'success');
            if (!byId('extension-center-workspace')?.hidden) await loadCatalog();
            if (item.id === N8N_EXTENSION_ID) {
                await window.workbenchN8nWorkflows?.refreshExtensionState?.();
            }
            await renderProjectAssignments(byId('project-settings-extension-list'), projectId);
        } catch (error) {
            state.deps?.showToast?.(`更新專案擴充失敗：${error.message}`, 'error');
            // Project disable is also fail-closed when runtime cleanup fails.
            // Re-read the persisted mode before deciding which option to show.
            await loadCatalog().catch(() => {
                control.disabled = false;
                control.value = item.project_override || 'inherit';
            });
            await renderProjectAssignments(
                byId('project-settings-extension-list'),
                projectId
            ).catch(() => {});
        }
    }

    function reviewDescription(operation, item) {
        if (operation === 'trust') return `信任擴充「${item.name || item.id}」的目前 manifest`;
        if (operation === 'install') return `安裝、信任並啟用「${item.name || item.id}」的下列能力`;
        if (operation === 'project_enable') return `允許「${item.name || item.id}」在指定專案生效`;
        if (operation === 'activate') return `允許並啟用「${item.name || item.id}」作為聊天模型`;
        return `重新啟用「${item.name || item.id}」及下列能力`;
    }

    function openPermissionReview(item, operation, context = {}) {
        if (
            item?.runtime_available === false
            && ['install', 'enable', 'activate', 'project_enable'].includes(operation)
        ) {
            state.deps?.showToast?.(unavailableExplanation(item), 'warning');
            return;
        }
        const modal = byId('extension-permission-modal');
        const summary = byId('extension-permission-summary');
        const trust = byId('extension-trust-confirm');
        const confirm = byId('extension-permission-confirm');
        const returnFocus = document.activeElement;
        closePermissionReview({ restoreFocus: false });
        state.pendingReview = { item, operation, context, returnFocus };
        summary.replaceChildren();

        const identity = document.createElement('div');
        identity.className = 'extension-permission-identity';
        const name = document.createElement('strong');
        name.textContent = reviewDescription(operation, item);
        const source = document.createElement('span');
        source.textContent = `來源：${item.origin || 'unknown'} · 版本：${item.version || '--'}`;
        const digest = document.createElement('span');
        digest.textContent = `Manifest SHA-256：${item.manifest_sha256 || '未提供'}`;
        identity.append(name, source, digest);
        summary.appendChild(identity);

        const list = document.createElement('div');
        list.className = 'extension-permission-list';
        const permissions = (item.permissions || []).map(permissionInfo);
        (permissions.length ? permissions : [{ name: '未要求額外能力', risk: '', description: '仍受 Workbench 能力閘門與 audit 約束。' }])
            .forEach(permission => {
                const row = document.createElement('div');
                row.className = 'extension-permission-row';
                row.textContent = [permission.name, permission.risk, permission.description].filter(Boolean).join(' · ');
                list.appendChild(row);
            });
        summary.appendChild(list);

        const trustCopy = byId('extension-trust-confirm')?.closest('label')?.querySelector('span');
        if (trustCopy) {
            trustCopy.textContent = item.origin === 'local'
                ? '我已確認本機來源、manifest digest 與上述權限，並信任此擴充。'
                : '我已閱讀上述權限，確認允許此內建擴充在選定範圍生效。';
        }
        trust.checked = false;
        confirm.disabled = true;
        modal.classList.add('active');
        setTimeout(() => trust.focus(), 20);
    }

    function closePermissionReview({ restoreFocus = true } = {}) {
        const pending = state.pendingReview;
        const modal = byId('extension-permission-modal');
        const trust = byId('extension-trust-confirm');
        const confirm = byId('extension-permission-confirm');
        modal?.classList.remove('active');
        if (trust) trust.checked = false;
        if (confirm) confirm.disabled = true;
        state.pendingReview = null;
        if (modal?.contains(document.activeElement)) document.activeElement.blur();
        if (!restoreFocus) return;
        setTimeout(() => {
            if (pending?.returnFocus?.isConnected) {
                pending.returnFocus.focus();
                return;
            }
            if (!byId('extension-center-workspace')?.hidden) {
                byId('extension-search')?.focus();
            }
        }, 0);
    }

    async function confirmPermissionReview() {
        const pending = state.pendingReview;
        if (!pending || !byId('extension-trust-confirm').checked) return;
        const { item, operation, context } = pending;
        const confirm = byId('extension-permission-confirm');
        confirm.disabled = true;
        try {
            let current = item;
            const scope = projectQuery(context.projectId || state.projectId);
            if (operation === 'install') {
                const result = await request(`/api/extensions/${encoded(current.id)}/install${scope}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ manifest_sha256: current.manifest_sha256 })
                });
                current = result.extension || current;
            }
            if (['trust', 'install', 'enable', 'activate', 'project_enable'].includes(operation) && !current.trusted) {
                const result = await request(`/api/extensions/${encoded(current.id)}/trust${scope}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ manifest_sha256: current.manifest_sha256 })
                });
                current = result.extension || current;
            }
            if (operation === 'install' || operation === 'enable' || operation === 'activate') {
                const result = await request(`/api/extensions/${encoded(current.id)}/state${scope}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        global_enabled: true,
                        manifest_sha256: current.manifest_sha256
                    })
                });
                current = result.extension || current;
            } else if (operation === 'project_enable') {
                const result = await request(`/api/projects/${encoded(context.projectId)}/extensions/${encoded(current.id)}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        mode: 'enabled',
                        manifest_sha256: current.manifest_sha256
                    })
                });
                current = result.extension || current;
            }
            state.deps?.showToast?.(`${item.name || item.id} 的權限操作已完成。`, 'success');
            closePermissionReview();
            if (!byId('extension-center-workspace')?.hidden) await loadCatalog();
            if (item.id === N8N_EXTENSION_ID) {
                await window.workbenchN8nWorkflows?.refreshExtensionState?.();
            }
            if (context.projectId) {
                await renderProjectAssignments(byId('project-settings-extension-list'), context.projectId);
            }
            await state.deps?.reloadProject?.();
            if (typeof context.onComplete === 'function') await context.onComplete();
            if (operation === 'install' && current.connection_required) {
                await selectWorkspaceTab('connections');
            }
        } catch (error) {
            state.deps?.showToast?.(`權限操作失敗：${error.message}`, 'error');
            confirm.disabled = false;
            if (!byId('extension-center-workspace')?.hidden) await loadCatalog().catch(() => {});
        }
    }

    async function reviewProviderModel(extensionId, onComplete) {
        const catalog = await loadCatalog();
        const item = catalog.extensions.find(entry => entry.id === extensionId);
        if (!item) throw new Error(`找不到 API 模型權限項目：${extensionId}`);
        if (item.effective_enabled) {
            await onComplete?.();
            return;
        }
        openPermissionReview(item, 'activate', { onComplete });
    }

    async function refreshHealth(item, button) {
        button.disabled = true;
        try {
            const result = await request(`/api/extensions/${encoded(item.id)}/health${projectQuery()}`, { method: 'POST' });
            const health = healthInfo(result.extension || item);
            const healthy = ['healthy', 'ready', 'connected', 'ok'].includes(health.status);
            const failed = ['error', 'failed', 'unavailable'].includes(health.status);
            const detail = health.message ? `：${health.message}` : '';
            state.deps?.showToast?.(
                `${item.name || item.id} 健康狀態：${health.status}${detail}`,
                healthy ? 'success' : (failed ? 'error' : 'warning')
            );
            await loadCatalog();
        } catch (error) {
            state.deps?.showToast?.(`健康檢查失敗：${error.message}`, 'error');
            button.disabled = false;
        }
    }

    async function openAudit(item) {
        const panel = byId('extension-audit-panel');
        const list = byId('extension-audit-list');
        byId('extension-audit-subtitle').textContent = item.name || item.id;
        list.replaceChildren(stateBlock('載入 audit 中…', 'is-loading'));
        panel.hidden = false;
        try {
            const data = await request(`/api/extensions/${encoded(item.id)}/audits?limit=50`);
            const audits = Array.isArray(data.audits) ? data.audits : [];
            list.replaceChildren();
            if (!audits.length) {
                list.appendChild(stateBlock('尚無執行紀錄。'));
                return;
            }
            audits.forEach(audit => {
                const row = document.createElement('div');
                row.className = 'extension-audit-row';
                const title = document.createElement('strong');
                title.textContent = audit.capability_name || audit.action || audit.event || '擴充操作';
                const meta = document.createElement('span');
                meta.textContent = [
                    audit.status || audit.outcome,
                    audit.risk_level || audit.risk,
                    audit.project_id ? `project ${audit.project_id}` : '',
                    audit.created_at || audit.timestamp
                ].filter(Boolean).join(' · ');
                row.append(title, meta);
                list.appendChild(row);
            });
        } catch (error) {
            list.replaceChildren(stateBlock(`無法載入 audit：${error.message}`, 'is-error'));
        }
    }

    async function removeExtension(item, button) {
        if (!item.removable) return;
        const retentionNote = item.origin === 'local'
            ? '來源資料夾不會被刪除。'
            : item.connection_required
                ? '已保存的帳號憑證不會一併刪除；如不再使用，請先在「連線」中斷開帳號。'
                : '內建目錄項目仍會保留，之後可重新安裝。';
        if (!window.confirm(`確定移除擴充「${item.name || item.id}」？${retentionNote}`)) return;
        button.disabled = true;
        try {
            await request(`/api/extensions/${encoded(item.id)}`, { method: 'DELETE' });
            state.deps?.showToast?.(`${item.name || item.id} 已移除註冊。`, 'success');
            if (state.selectedExtensionId === item.id) closeExtensionDetail();
            await loadCatalog();
            if (item.id === N8N_EXTENSION_ID) {
                await window.workbenchN8nWorkflows?.refreshExtensionState?.();
            }
            await state.deps?.reloadProject?.();
        } catch (error) {
            state.deps?.showToast?.(`移除擴充失敗：${error.message}`, 'error');
            button.disabled = false;
        }
    }

    async function inspectLocal() {
        const filename = byId('extension-local-path')?.value?.trim();
        const button = byId('extension-local-inspect');
        if (!filename || !button) return;
        button.disabled = true;
        const list = byId('extension-local-list');
        list.replaceChildren(stateBlock('正在驗證本機 manifest…', 'is-loading'));
        try {
            const data = await request(`/api/extensions/local/inspect${projectQuery()}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filename })
            });
            const item = data.extension || data.inspection || data.manifest || data;
            if (!item?.id) throw new Error('後端未回傳可識別的 extension manifest');
            list.replaceChildren(createExtensionCard(item));
            safeCreateIcons();
            if (!item.installed) {
                openPermissionReview(item, 'install');
            } else if (!item.trusted) {
                openPermissionReview(item, 'trust');
            } else {
                state.deps?.showToast?.(`${item.name || item.id} 的目前 manifest 已安裝並信任。`, 'success');
            }
        } catch (error) {
            list.replaceChildren(stateBlock(`Manifest 驗證失敗：${error.message}`, 'is-error'));
        } finally {
            button.disabled = false;
        }
    }

    async function renderProjectAssignments(container, projectId) {
        if (!container || !projectId) return;
        container.replaceChildren(stateBlock('載入專案擴充中…', 'is-loading'));
        try {
            const data = await request(`/api/extensions?project_id=${encoded(projectId)}`);
            const items = (data.extensions || []).filter(item => item.installed);
            container.replaceChildren();
            if (!items.length) {
                container.appendChild(stateBlock('尚未安裝可分配給此專案的擴充。'));
                return;
            }
            items.forEach(item => {
                const row = document.createElement('div');
                row.className = 'project-extension-row';
                const copy = document.createElement('div');
                const title = document.createElement('strong');
                title.textContent = item.name || item.id;
                const meta = document.createElement('small');
                meta.textContent = item.global_enabled === false
                    ? '全域已停用，專案無法啟用'
                    : `${item.publisher || item.origin || 'Workbench'} · ${item.version || '--'}`;
                copy.append(title, meta);
                row.append(copy, projectOverrideSelect(item, projectId));
                container.appendChild(row);
            });
        } catch (error) {
            container.replaceChildren(stateBlock(`無法載入專案擴充：${error.message}`, 'is-error'));
        }
    }

    function setProjectSettingsTab(tab) {
        const selected = ['basic', 'permissions', 'extensions'].includes(tab) ? tab : 'basic';
        document.querySelectorAll('.project-settings-tab[data-project-settings-tab]').forEach(button => {
            const active = button.dataset.projectSettingsTab === selected;
            button.classList.toggle('active', active);
            button.setAttribute('aria-selected', active ? 'true' : 'false');
            button.tabIndex = active ? 0 : -1;
        });
        document.querySelectorAll('.project-settings-pane[data-project-settings-pane]').forEach(panel => {
            const active = panel.dataset.projectSettingsPane === selected;
            panel.classList.toggle('active', active);
            panel.hidden = !active;
        });
        if (selected === 'extensions' && state.settingsProject?.id) {
            renderProjectAssignments(
                byId('project-settings-extension-list'),
                state.settingsProject.id
            );
        }
    }

    function closeProjectSettings() {
        closePermissionReview({ restoreFocus: false });
        byId('project-settings-modal')?.classList.remove('active');
        state.settingsProject = null;
    }

    async function openProjectSettings(project) {
        if (!project?.id || !state.initialized) return;
        closePermissionReview({ restoreFocus: false });
        state.settingsProject = { ...project };
        byId('project-settings-title').lastChild.textContent = `專案設定 · ${project.name}`;
        byId('project-settings-name').value = project.name || '';
        byId('project-settings-root-path').value = project.root_path || '';
        byId('project-settings-permission-mode').value = [
            'read_only',
            'confirm_write',
            'workspace_write'
        ].includes(project.permission_mode) ? project.permission_mode : 'read_only';
        setProjectSettingsTab('basic');
        byId('project-settings-modal').classList.add('active');
        safeCreateIcons();
        setTimeout(() => byId('project-settings-name')?.focus(), 20);
    }

    async function browseProjectSettingsFolder() {
        const selected = await state.deps?.openFolderBrowser?.(
            byId('project-settings-root-path')?.value?.trim() || null
        );
        if (selected && selected !== '__roots__') {
            byId('project-settings-root-path').value = selected;
        }
    }

    async function saveProjectSettings() {
        const project = state.settingsProject;
        if (!project?.id) return;
        const name = byId('project-settings-name').value.trim();
        const rootPath = byId('project-settings-root-path').value.trim();
        const permissionMode = byId('project-settings-permission-mode').value;
        if (!name || !rootPath) {
            state.deps?.showToast?.('專案名稱與資料夾不可留空。', 'error');
            return;
        }
        const save = byId('project-settings-save');
        save.disabled = true;
        try {
            await request(`/api/projects/${encoded(project.id)}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, permission_mode: permissionMode })
            });
            if (rootPath !== project.root_path) {
                await request(`/api/projects/${encoded(project.id)}/relink`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ root_path: rootPath })
                });
            }
            state.deps?.showToast?.('專案設定已儲存。', 'success');
            closeProjectSettings();
            await state.deps?.reloadProject?.();
        } catch (error) {
            state.deps?.showToast?.(`儲存專案設定失敗：${error.message}`, 'error');
        } finally {
            save.disabled = false;
        }
    }

    function initProjectSettings() {
        byId('project-settings-close')?.addEventListener('click', closeProjectSettings);
        byId('project-settings-cancel')?.addEventListener('click', closeProjectSettings);
        byId('project-settings-save')?.addEventListener('click', saveProjectSettings);
        byId('project-settings-browse')?.addEventListener('click', browseProjectSettingsFolder);
        byId('project-settings-open-extensions')?.addEventListener('click', () => {
            const projectId = state.settingsProject?.id;
            if (!projectId) return;
            closeProjectSettings();
            open('installed', projectId);
        });
        document.querySelectorAll('.project-settings-tab[data-project-settings-tab]').forEach(button => {
            button.addEventListener('click', () => setProjectSettingsTab(button.dataset.projectSettingsTab));
        });
        byId('project-settings-modal')?.addEventListener('click', event => {
            if (event.target === byId('project-settings-modal')) closeProjectSettings();
        });
    }

    async function open(tab = 'installed', projectId = null) {
        if (!state.initialized) return;
        closePermissionReview({ restoreFocus: false });
        state.selectedExtensionId = null;
        state.n8nService = null;
        state.n8nServiceLoading = false;
        byId('extension-detail-view').hidden = true;
        byId('extension-center-workspace')?.classList.remove('is-detail');
        const selectedProject = projectId || state.deps?.getActiveProjectId?.() || null;
        projectOptions(selectedProject);
        if (!projectId && tab !== 'connections') {
            byId('extension-scope-select').value = 'global';
            state.projectId = null;
        }
        if (tab === 'connections') ensureConnectionProjectScope();
        byId('extension-search').value = '';
        byId('extension-audit-panel').hidden = true;
        setTab(tab);
        byId('extension-center-workspace').hidden = false;
        state.deps?.onWorkspaceOpen?.();
        safeCreateIcons();
        await loadCatalog().catch(() => {});
        if (tab === 'connections') await refreshConnections().catch(() => {});
        setTimeout(() => {
            if (['installed', 'available', 'local'].includes(state.activeTab)) {
                byId('extension-search')?.focus();
            } else {
                byId('extension-scope-select')?.focus();
            }
        }, 20);
    }

    async function openExtension(extensionId, projectId = null) {
        await open('available', projectId);
        const item = (state.response.extensions || [])
            .find(entry => String(entry.id) === String(extensionId));
        if (!item) {
            state.deps?.showToast?.('找不到指定的擴充功能。', 'warning');
            return;
        }
        if (item.installed) {
            await openExtensionDetail(item.id);
            return;
        }
        setTab('available');
        renderLists();
        setTimeout(() => {
            const card = byId('extension-available-list')?.querySelector?.(`[data-extension-id="${item.id}"]`);
            card?.scrollIntoView?.({ block: 'center', behavior: 'smooth' });
            card?.querySelector?.('[data-extension-action="install"]')?.focus?.();
        }, 20);
    }

    function close() {
        closePermissionReview({ restoreFocus: false });
        state.selectedExtensionId = null;
        state.n8nService = null;
        state.n8nServiceLoading = false;
        byId('extension-detail-view').hidden = true;
        byId('extension-center-workspace')?.classList.remove('is-detail');
        if (byId('extension-center-workspace')) byId('extension-center-workspace').hidden = true;
        byId('extension-audit-panel').hidden = true;
        state.deps?.onWorkspaceClose?.();
    }

    function init(dependencies = {}) {
        state.deps = dependencies;
        if (state.initialized) return;
        state.initialized = true;
        const workspace = byId('extension-center-workspace');
        const workbenchBody = document.querySelector('.workbench-body');
        if (workspace && workbenchBody && workspace.parentElement !== workbenchBody) {
            workbenchBody.appendChild(workspace);
        }
        byId('extensions-close')?.addEventListener('click', close);
        byId('extensions-close-btn')?.addEventListener('click', close);
        byId('extension-detail-back')?.addEventListener('click', closeExtensionDetail);
        byId('extension-refresh')?.addEventListener('click', () => refreshActiveTab().catch(() => {}));
        byId('extension-search')?.addEventListener('input', renderLists);
        byId('extension-scope-select')?.addEventListener('change', async event => {
            state.projectId = event.target.value === 'global' ? null : event.target.value;
            if (state.activeTab === 'connections') ensureConnectionProjectScope();
            await loadCatalog().catch(() => {});
            if (state.activeTab === 'connections') await refreshConnections().catch(() => {});
        });
        document.querySelectorAll('.extension-tab-btn[data-extension-tab]').forEach(button => {
            button.addEventListener('click', () => selectWorkspaceTab(button.dataset.extensionTab));
        });
        byId('extension-developer-toggle')?.addEventListener('click', () => setTab('developer'));
        window.addEventListener('workbench:connector-project-change', event => {
            if (state.activeTab !== 'connections') return;
            const projectId = event.detail?.projectId ? String(event.detail.projectId) : null;
            const select = byId('extension-scope-select');
            if (!projectId || ![...(select?.options || [])].some(option => option.value === projectId)) return;
            state.projectId = projectId;
            select.value = projectId;
            loadCatalog().catch(() => {});
        });
        window.addEventListener('workbench:n8n-service-state', event => {
            if (!event.detail || typeof event.detail !== 'object') return;
            state.n8nService = { ...event.detail };
            if (state.selectedExtensionId === N8N_EXTENSION_ID) renderExtensionDetail();
        });
        byId('extension-local-path')?.addEventListener('input', event => {
            const filename = event.target.value.trim();
            byId('extension-local-inspect').disabled = !/^[^/\\]+\.json$/i.test(filename)
                || filename.includes('..');
        });
        byId('extension-local-inspect')?.addEventListener('click', inspectLocal);
        byId('extension-audit-close')?.addEventListener('click', () => {
            byId('extension-audit-panel').hidden = true;
        });
        byId('extension-permission-close')?.addEventListener('click', closePermissionReview);
        byId('extension-permission-cancel')?.addEventListener('click', closePermissionReview);
        byId('extension-trust-confirm')?.addEventListener('change', event => {
            byId('extension-permission-confirm').disabled = !event.target.checked;
        });
        byId('extension-permission-confirm')?.addEventListener('click', confirmPermissionReview);
        byId('extension-permission-modal')?.addEventListener('click', event => {
            if (event.target === byId('extension-permission-modal')) closePermissionReview();
        });
        initProjectSettings();
        projectOptions();
    }

    window.workbenchExtensions = {
        init,
        open,
        openExtension,
        close,
        closePermissionReview,
        refresh: loadCatalog,
        reviewProviderModel,
        renderProjectAssignments,
        openProjectSettings,
        __testing: Object.freeze({
            catalogSectionItems,
            extensionControlPolicy,
            createExtensionCard,
            renderExtensionDetail
        })
    };
}());
