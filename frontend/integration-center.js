(() => {
    'use strict';

    const KNOWN_INTEGRATIONS = [
        {
            id: 'gmail', name: 'Gmail', icon: 'mail', kind: '郵件服務',
            description: '連接 Google 帳號後，讓 Agent 搜尋與閱讀 Gmail，並在你核准後建立或寄送草稿。',
            data: '只會使用目前專案已綁定的 Gmail 帳號；OAuth 權杖加密保存在這台電腦。',
            action: 'connections', actionLabel: '導入 Gmail 帳號',
            requiresConnection: true, resourceScopeRequired: true,
        },
        {
            id: 'github', name: 'GitHub', icon: 'github', kind: '程式協作',
            description: '讀取 Repository、Issue、PR 與 Checks；建立 Issue 或留言時依權限方案治理。',
            data: '只會存取目前 Project 明確綁定的 Repository。',
            action: 'connections', actionLabel: '管理 GitHub 連線',
            requiresConnection: true, resourceScopeRequired: true,
        },
        {
            id: 'notion', name: 'Notion', icon: 'notebook-tabs', kind: '知識與文件',
            description: '搜尋及讀取授權頁面；建立或更新內容時依權限方案治理。',
            data: '只會存取目前 Project 明確綁定的 Page 或 Database。',
            action: 'connections', actionLabel: '管理 Notion 連線',
            requiresConnection: true, resourceScopeRequired: true,
        },
        {
            id: 'n8n', name: 'n8n', icon: 'workflow', kind: '工作流程',
            description: '讓 Workbench Agent 觸發受治理的本機自動化，外部寫入仍保留政策與稽核。',
            data: '工作要求、執行狀態與核准結果；不會把 Workbench 內部權杖交給流程。',
            action: 'workflows', actionLabel: '開啟工作流程',
            requiresConnection: false, resourceScopeRequired: true,
        },
        {
            id: 'mcp', name: '本機 MCP', icon: 'terminal-square', kind: '本機工具',
            description: '以可信任的本機 stdio MCP 程序提供工具，並由相同 Project 政策限制使用。',
            data: '只傳送工具呼叫必要參數；獨立程序是故障隔離，不等同完整系統沙盒。',
            action: 'mcp', actionLabel: '管理本機 MCP',
            requiresConnection: true, resourceScopeRequired: true,
        },
        {
            id: 'external_api', name: 'Workbench 對外 API', icon: 'key-round', kind: '外部存取',
            description: '簽發綁定此電腦安裝的 API Key，讓 n8n 或其他系統建立及追蹤 Agent 工作。',
            data: '外部請求仍受金鑰 Scope、Project、預算、工具政策與稽核限制。',
            action: 'api', actionLabel: '管理 API Key',
            requiresConnection: false, resourceScopeRequired: false,
        },
    ];

    // Discovery-only entries make useful integrations visible before an
    // adapter exists. They never imply installation, connection, or consent.
    const PLANNED_INTEGRATIONS = [
        {
            id: 'google_drive', name: 'Google Drive', icon: 'hard-drive', kind: '雲端檔案',
            description: '未來可在專案授權範圍內搜尋、讀取與輸出 Drive、Docs、Sheets 及 Slides 檔案。',
            data: '尚未提供介接器，目前不會存取或傳送任何 Google Drive 資料。',
            availability: 'planned', action: null, actionLabel: '尚未提供',
        },
        {
            id: 'google_calendar', name: 'Google Calendar', icon: 'calendar-days', kind: '行事曆',
            description: '未來可讀取專案授權的行程，並在人工批准後建立或修改活動。',
            data: '尚未提供介接器，目前不會存取或傳送任何行事曆資料。',
            availability: 'planned', action: null, actionLabel: '尚未提供',
        },
        {
            id: 'slack', name: 'Slack', icon: 'message-square', kind: '團隊協作',
            description: '未來可在專案授權的頻道中讀取訊息、取得人工批准與傳送工作結果。',
            data: '尚未提供介接器，目前不會存取或傳送任何 Slack 資料。',
            availability: 'planned', action: null, actionLabel: '尚未提供',
        },
    ];

    const state = {
        deps: null,
        initialized: false,
        selectedProjectId: null,
        activeTab: 'overview',
        overview: {},
        installation: null,
        credentialRecoveryRequired: false,
        apiKeys: [],
        policy: null,
        audit: [],
        oneTimeSecret: null,
        secretTimer: null,
        requestRevision: 0,
    };

    const byId = id => document.getElementById(id);
    const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[character]);
    const asArray = value => Array.isArray(value) ? value : [];
    const cssEscape = value => globalThis.CSS?.escape
        ? globalThis.CSS.escape(String(value))
        : String(value).replace(/["\\]/g, character => `\\${character}`);
    const statusText = value => ({
        healthy: '健康', connected: '已連線', ready: '可使用', enabled: '已啟用', active: '有效',
        configured: '已設定', degraded: '服務降級', warning: '需要注意', unavailable: '無法使用',
        running: '執行中', not_configured: '尚未設定', not_connected: '尚未連線',
        disabled: '已停用', disconnected: '尚未連線', unconfigured: '尚未設定', revoked: '已撤銷',
        expired: '已到期', credential_recovery_required: '需要本機修復',
        unknown: '狀態未知', error: '發生錯誤',
    })[String(value || '').toLowerCase()] || value || '尚未設定';
    const statusClass = value => {
        const normalized = String(value || '').toLowerCase();
        if (['healthy', 'connected', 'ready', 'enabled', 'active', 'configured'].includes(normalized)) return 'is-success';
        if (['degraded', 'warning', 'rate_limited', 'unknown'].includes(normalized)) return 'is-warning';
        if (['unavailable', 'error', 'revoked', 'expired', 'auth_required', 'permission_denied', 'credential_recovery_required'].includes(normalized)) return 'is-error';
        return 'is-muted';
    };
    const formatDate = value => {
        if (!value) return '—';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        return new Intl.DateTimeFormat('zh-TW', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
    };

    function safeIcons() {
        try { state.deps?.createIcons?.(); }
        catch (_error) { try { window.lucide?.createIcons?.(); } catch (_ignored) {} }
    }

    function errorMessage(payload, response) {
        const detail = payload?.detail;
        return (typeof detail === 'object' ? detail?.message : detail)
            || payload?.message
            || `請求失敗（${response.status}）`;
    }

    async function request(path, options = {}) {
        if (!state.deps?.apiFetch) throw new Error('整合中心尚未初始化。');
        const response = await state.deps.apiFetch(`${state.deps.apiBase || ''}${path}`, options);
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload?.success === false) {
            const error = new Error(errorMessage(payload, response));
            error.status = response.status;
            error.code = payload?.detail?.code || payload?.code || null;
            throw error;
        }
        return payload;
    }

    function activeProjects() {
        return asArray(state.deps?.getProjects?.()).filter(project => !project.archived);
    }

    function projectName(projectId) {
        return activeProjects().find(project => String(project.id) === String(projectId))?.name || projectId || '未指定專案';
    }

    function syncProjects(preferredProjectId = null) {
        const select = byId('integration-project-select');
        if (!select) return;
        const projects = activeProjects();
        const preferred = preferredProjectId || state.selectedProjectId || state.deps?.getActiveProjectId?.();
        state.selectedProjectId = projects.some(project => String(project.id) === String(preferred))
            ? String(preferred)
            : (projects[0] ? String(projects[0].id) : null);
        select.innerHTML = projects.length
            ? projects.map(project => `<option value="${escapeHtml(project.id)}">${escapeHtml(project.name)}</option>`).join('')
            : '<option value="">請先建立 Project</option>';
        select.value = state.selectedProjectId || '';
        select.disabled = !projects.length;
        byId('integration-api-key-form')?.querySelectorAll('input, button').forEach(control => {
            control.disabled = !state.selectedProjectId;
        });
    }

    function integrationId(item) {
        return String(item?.integration_id || item?.id || item?.service_id || '');
    }

    function aliasesFor(id) {
        return ({
            gmail: ['gmail', 'connector.gmail'],
            github: ['github', 'connector.github'],
            notion: ['notion', 'connector.notion'],
            n8n: ['n8n', 'builtin.n8n'],
            mcp: ['mcp', 'local.mcp', 'builtin.mcp'],
            external_api: ['external_api', 'workbench.api', 'public_api'],
        })[id] || [id];
    }

    function normalizedIntegrations() {
        const source = asArray(state.overview?.integrations || state.overview?.services || state.overview?.items);
        return KNOWN_INTEGRATIONS.map(known => {
            const raw = source.find(item => aliasesFor(known.id).includes(integrationId(item))) || {};
            const rawState = raw.state && typeof raw.state === 'object' ? raw.state : {};
            const health = rawState.health && typeof rawState.health === 'object'
                ? rawState.health
                : (raw.health && typeof raw.health === 'object' ? raw.health : {});
            const stateValue = typeof rawState.state === 'string' ? rawState.state : null;
            const extension = rawState.extension && typeof rawState.extension === 'object' ? rawState.extension : {};
            const status = rawState.status || stateValue || raw.status || health.status
                || (rawState.healthy === true ? 'healthy' : null)
                || (rawState.connected === true || raw.connected === true ? 'connected' : null)
                || (rawState.enabled === true || raw.enabled === true ? 'enabled' : null)
                || (extension.enabled === true ? 'enabled' : null)
                || 'unconfigured';
            let connections = asArray(rawState.connections || raw.connections).map(connection => ({
                ...connection,
                connection_id: String(connection.connection_id || connection.id || ''),
                display_name: connection.display_name || connection.name || connection.connection_id || connection.id,
                resources: asArray(connection.resources),
            })).filter(connection => connection.connection_id);
            if (!connections.length && rawState.connection_id) {
                connections = [{
                    connection_id: String(rawState.connection_id),
                    display_name: rawState.connection_name || rawState.display_name || rawState.connection_id,
                    resources: asArray(rawState.resources),
                }];
            }
            if (known.id === 'mcp' && !connections.length) {
                connections = asArray(rawState.configured_extensions).map(extensionItem => ({
                    ...extensionItem,
                    connection_id: String(extensionItem.connection_id || extensionItem.extension_id || extensionItem.id || ''),
                    display_name: extensionItem.display_name || extensionItem.name || extensionItem.extension_id || extensionItem.id,
                    resources: asArray(extensionItem.resources || extensionItem.tools).map(resource => (
                        typeof resource === 'string'
                            ? { resource_type: 'tool', resource_id: resource, display_label: resource }
                            : resource
                    )),
                })).filter(connection => connection.connection_id);
            }
            let resources = [...asArray(rawState.resources || raw.resources)];
            if (known.id === 'n8n') {
                resources = resources.concat(asArray(rawState.workflows).map(workflow => (
                    typeof workflow === 'string'
                        ? { resource_type: 'workflow', resource_id: workflow, display_label: workflow }
                        : { resource_type: 'workflow', resource_id: workflow.workflow_id || workflow.id, display_label: workflow.name || workflow.workflow_id || workflow.id }
                )).filter(resource => resource.resource_id));
            }
            return {
                ...known,
                ...raw,
                id: known.id,
                backendId: integrationId(raw) || known.id,
                name: raw.display_name || raw.name || known.name,
                kind: known.kind,
                description: raw.description || known.description,
                data: raw.data_summary || raw.data || known.data,
                status,
                state: rawState,
                health,
                connections,
                resources,
                requiresConnection: raw.requires_connection ?? known.requiresConnection,
                resourceScopeRequired: raw.resource_scope_required_for_open ?? known.resourceScopeRequired,
                capabilityItems: asArray(raw.capabilities).map(capability => ({
                    id: typeof capability === 'string' ? capability : capability?.id || capability?.name,
                    label: typeof capability === 'string' ? capability : capability?.label || capability?.id || capability?.name,
                    risk: typeof capability === 'string' ? '' : capability?.risk || '',
                })).filter(capability => capability.id),
                capabilities: asArray(raw.capabilities).map(capability => (
                    typeof capability === 'string' ? capability : capability?.id || capability?.name
                )).filter(Boolean),
                capabilityLabels: asArray(raw.capabilities).map(capability => (
                    typeof capability === 'string' ? capability : capability?.label || capability?.id || capability?.name
                )).filter(Boolean),
            };
        });
    }

    function switchTab(tab = 'overview', { focus = false } = {}) {
        const supported = new Set(['overview', 'services', 'api', 'policy', 'audit']);
        state.activeTab = supported.has(tab) ? tab : 'overview';
        document.querySelectorAll('[data-integration-tab]').forEach(button => {
            const active = button.dataset.integrationTab === state.activeTab;
            button.classList.toggle('active', active);
            button.setAttribute('aria-selected', active ? 'true' : 'false');
            button.tabIndex = active ? 0 : -1;
            if (active && focus) button.focus();
        });
        document.querySelectorAll('[data-integration-panel]').forEach(panel => {
            panel.hidden = panel.dataset.integrationPanel !== state.activeTab;
        });
    }

    function renderSummary() {
        const integrations = normalizedIntegrations();
        const activeStates = new Set(['healthy', 'connected', 'ready', 'enabled', 'active', 'configured']);
        const problemStates = new Set(['degraded', 'warning', 'unavailable', 'error', 'auth_required', 'permission_denied']);
        const grants = asArray(state.policy?.grants);
        const summary = state.overview?.summary || {};
        const metrics = [
            [summary.connected_services ?? summary.configured ?? integrations.filter(item => activeStates.has(String(item.status).toLowerCase())).length, '已連線服務'],
            [summary.active_api_keys ?? state.apiKeys.filter(key => String(key.status).toLowerCase() === 'active').length, '有效 API Key'],
            [summary.allowed_integrations ?? grants.length, '已放行整合'],
            [summary.needs_attention ?? (summary.total !== undefined && summary.healthy !== undefined ? Math.max(0, summary.total - summary.healthy) : integrations.filter(item => problemStates.has(String(item.status).toLowerCase())).length), '需要處理'],
        ];
        byId('integration-summary-grid').innerHTML = metrics.map(([value, label]) => (
            `<article><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></article>`
        )).join('');
        byId('integration-overview-list').innerHTML = integrations.map(item => `
            <article class="integration-compact-row">
                <span class="integration-service-icon"><i data-lucide="${escapeHtml(item.icon)}"></i></span>
                <div><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.kind)}</span></div>
                <span class="integration-status ${statusClass(item.status)}">${escapeHtml(statusText(item.status))}</span>
            </article>`).join('');
    }

    function runServiceAction(action) {
        if (action === 'api') return switchTab('api', { focus: true });
        if (action === 'workflows') return state.deps?.openWorkflows?.();
        if (action === 'connections') return state.deps?.openExtensions?.('connections');
        if (action === 'mcp') return state.deps?.openExtensions?.('developer');
    }

    function renderServices() {
        const container = byId('integration-service-list');
        container.innerHTML = normalizedIntegrations().map(item => `
            <article class="integration-service-card" data-integration-service="${escapeHtml(item.id)}">
                <div class="integration-service-card-head">
                    <span class="integration-service-icon"><i data-lucide="${escapeHtml(item.icon)}"></i></span>
                    <div><span>${escapeHtml(item.kind)}</span><h2>${escapeHtml(item.name)}</h2></div>
                    <span class="integration-status ${statusClass(item.status)}">${escapeHtml(statusText(item.status))}</span>
                </div>
                <p>${escapeHtml(item.description)}</p>
                <div class="integration-data-note"><strong>資料範圍</strong><span>${escapeHtml(item.data)}</span></div>
                <button type="button" class="btn btn-secondary compact" data-service-action="${escapeHtml(item.action)}">
                    ${escapeHtml(item.actionLabel)}
                </button>
            </article>`).join('');
        container.querySelectorAll('[data-service-action]').forEach(button => {
            button.addEventListener('click', () => runServiceAction(button.dataset.serviceAction));
        });
    }

    function renderInstallation() {
        const installation = state.installation || {};
        const baseUrl = installation.api_base_url || '';
        byId('integration-installation-label').textContent = installation.label || '此電腦的 Local AI Workbench';
        byId('integration-api-base').textContent = baseUrl || '後端尚未提供對外 API 位址。';
        const copy = byId('integration-copy-api-base');
        copy.disabled = !baseUrl;
        copy.dataset.copyValue = baseUrl;
        const recovery = byId('integration-api-recovery');
        if (recovery) recovery.hidden = !state.credentialRecoveryRequired;
        const createButton = byId('integration-create-api-key');
        if (createButton) createButton.disabled = state.credentialRecoveryRequired;
    }

    function keyStatus(key) {
        return key.status || (key.revoked_at ? 'revoked' : 'active');
    }

    function armDestructiveButton(button, confirmationLabel, action) {
        if (button.dataset.confirmed !== 'true') {
            button.dataset.confirmed = 'true';
            button.dataset.originalLabel = button.textContent;
            button.textContent = confirmationLabel;
            button.classList.add('is-confirming');
            window.setTimeout(() => {
                if (!button.isConnected || button.dataset.confirmed !== 'true') return;
                button.dataset.confirmed = 'false';
                button.textContent = button.dataset.originalLabel || '取消';
                button.classList.remove('is-confirming');
            }, 6000);
            return;
        }
        button.disabled = true;
        void action();
    }

    function showSecret(secret, notice) {
        if (!secret) throw new Error('後端沒有回傳一次性金鑰，請撤銷該紀錄後重新建立。');
        clearSecret();
        state.oneTimeSecret = String(secret);
        byId('integration-secret-value').value = state.oneTimeSecret;
        byId('integration-secret-notice').textContent = notice || '離開或重新載入後，完整金鑰無法再次顯示。';
        byId('integration-secret-panel').hidden = false;
        byId('integration-secret-value').focus();
        state.secretTimer = window.setTimeout(clearSecret, 120000);
    }

    function clearSecret() {
        if (state.secretTimer) window.clearTimeout(state.secretTimer);
        state.secretTimer = null;
        state.oneTimeSecret = null;
        const input = byId('integration-secret-value');
        if (input) input.value = '';
        if (byId('integration-secret-panel')) byId('integration-secret-panel').hidden = true;
    }

    async function copyText(value, successMessage) {
        if (!value) throw new Error('目前沒有可複製的內容。');
        if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value);
        else {
            const helper = document.createElement('textarea');
            helper.value = value;
            helper.setAttribute('readonly', '');
            helper.style.position = 'fixed';
            helper.style.opacity = '0';
            document.body.appendChild(helper);
            helper.select();
            const copied = document.execCommand('copy');
            helper.remove();
            if (!copied) throw new Error('瀏覽器不允許複製，請手動選取內容。');
        }
        state.deps?.showToast?.(successMessage, 'success');
    }

    async function rotateKey(key, button) {
        try {
            const payload = await request(`/api/integration-center/api-keys/${encodeURIComponent(key.id)}/rotate`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ revision: Number(key.revision || 0) }),
            });
            showSecret(payload.secret, payload.notice || '舊金鑰已失效；請立即更新外部系統。');
            state.deps?.showToast?.('金鑰已輪替，舊金鑰已失效。', 'success');
            await loadApiKeys();
        } catch (error) { state.deps?.showToast?.(error.message, 'error'); }
        finally { if (button?.isConnected) button.disabled = false; }
    }

    async function revokeKey(key, button) {
        try {
            await request(`/api/integration-center/api-keys/${encodeURIComponent(key.id)}/revoke`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ revision: Number(key.revision || 0) }),
            });
            state.deps?.showToast?.('API Key 已撤銷，外部系統無法再使用。', 'success');
            await loadApiKeys();
        } catch (error) { state.deps?.showToast?.(error.message, 'error'); }
        finally { if (button?.isConnected) button.disabled = false; }
    }

    async function setKeyEnabled(key, enabled, button) {
        try {
            await request(`/api/integration-center/api-keys/${encodeURIComponent(key.id)}`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    revision: Number(key.revision || 0),
                    enabled,
                    scopes: asArray(key.scopes),
                    expires_at: key.expires_at || null,
                    rate_limit_per_minute: Number(key.rate_limit_per_minute),
                    request_limit_daily: Number(key.request_limit_daily),
                }),
            });
            state.deps?.showToast?.(
                enabled ? 'API Key 已重新啟用。' : 'API Key 已暫停，外部系統目前無法使用。',
                'success',
            );
            await loadApiKeys();
        } catch (error) { state.deps?.showToast?.(error.message, 'error'); }
        finally { if (button?.isConnected) button.disabled = false; }
    }

    function renderApiKeys() {
        renderInstallation();
        const projectId = state.selectedProjectId;
        const keys = state.apiKeys.filter(key => !projectId || String(key.project_id) === String(projectId));
        const container = byId('integration-api-key-list');
        if (!keys.length) {
            container.innerHTML = '<div class="integration-empty">目前專案尚未簽發 API Key。</div>';
            return;
        }
        container.innerHTML = keys.map(key => {
            const status = keyStatus(key);
            const active = String(status).toLowerCase() === 'active';
            const disabled = String(status).toLowerCase() === 'disabled';
            return `<article class="integration-key-row" data-api-key-id="${escapeHtml(key.id)}">
                <div>
                    <strong>${escapeHtml(key.name || key.prefix || '未命名金鑰')}</strong>
                    <code>${escapeHtml(key.prefix || '前綴不可用')}••••</code>
                    <span>${escapeHtml(projectName(key.project_id))} · 建立 ${escapeHtml(formatDate(key.created_at))}</span>
                    <small>到期：${escapeHtml(key.expires_at ? formatDate(key.expires_at) : '未設定')} · 最後使用：${escapeHtml(key.last_used_at ? formatDate(key.last_used_at) : '尚未使用')}</small>
                </div>
                <span class="integration-status ${statusClass(status)}">${escapeHtml(statusText(status))}</span>
                <div class="integration-key-actions">
                    <button type="button" class="btn btn-secondary compact" data-key-action="toggle" ${(active || disabled) ? '' : 'disabled'}>${active ? '暫停' : '啟用'}</button>
                    <button type="button" class="btn btn-secondary compact" data-key-action="rotate" ${active ? '' : 'disabled'}>輪替</button>
                    <button type="button" class="btn btn-secondary compact btn-danger-subtle" data-key-action="revoke" ${active ? '' : 'disabled'}>撤銷</button>
                </div>
            </article>`;
        }).join('');
        container.querySelectorAll('[data-api-key-id]').forEach(row => {
            const key = keys.find(item => String(item.id) === row.dataset.apiKeyId);
            row.querySelector('[data-key-action="toggle"]')?.addEventListener('click', event => {
                const enabled = String(keyStatus(key)).toLowerCase() === 'disabled';
                if (enabled) {
                    event.currentTarget.disabled = true;
                    void setKeyEnabled(key, true, event.currentTarget);
                } else {
                    armDestructiveButton(event.currentTarget, '再次點擊以暫停', () => setKeyEnabled(key, false, event.currentTarget));
                }
            });
            row.querySelector('[data-key-action="rotate"]')?.addEventListener('click', event => {
                armDestructiveButton(event.currentTarget, '再次點擊以輪替', () => rotateKey(key, event.currentTarget));
            });
            row.querySelector('[data-key-action="revoke"]')?.addEventListener('click', event => {
                armDestructiveButton(event.currentTarget, '再次點擊以撤銷', () => revokeKey(key, event.currentTarget));
            });
        });
    }

    function normalizePolicy(payload) {
        const policy = payload?.policy || payload;
        if (!policy || (!policy.project_id && policy.revision === undefined && !policy.permission_mode)) {
            return { project_id: state.selectedProjectId, name: '專案整合權限', permission_mode: 'restricted', grants: [], revision: 0 };
        }
        return {
            ...policy,
            name: policy.name || '專案整合權限',
            permission_mode: ['blocked', 'restricted', 'open'].includes(policy.permission_mode) ? policy.permission_mode : 'restricted',
            grants: asArray(policy.grants),
            revision: Number(policy.revision || 0),
        };
    }

    function grantMatches(grant, integration) {
        const id = String(grant?.integration_id || '');
        return id === integration.backendId || aliasesFor(integration.id).includes(id);
    }

    function renderPolicy() {
        state.policy = normalizePolicy(state.policy);
        byId('integration-policy-name').value = state.policy.name;
        byId('integration-policy-revision').textContent = `版本 ${state.policy.revision}`;
        const mode = state.policy.permission_mode;
        const modeInput = document.querySelector(`input[name="integration-permission-mode"][value="${mode}"]`);
        if (modeInput) modeInput.checked = true;
        updateOpenRisk();
        const integrations = normalizedIntegrations();
        byId('integration-policy-services').innerHTML = integrations.map(integration => {
            const existingGrant = state.policy.grants.find(grant => grantMatches(grant, integration)) || null;
            const checked = !!existingGrant;
            const selectedCapabilities = new Set(asArray(existingGrant?.capabilities));
            const selectedConnectionId = existingGrant?.connection_id
                || (integration.connections.length === 1 ? integration.connections[0].connection_id : '');
            const selectedResources = new Set(asArray(existingGrant?.resources).map(resource => `${resource.resource_type}\u001f${resource.resource_id}`));
            const connectionMarkup = integration.requiresConnection ? (integration.connections.length ? `
                <label class="integration-policy-connection">使用連線
                    <select class="settings-input" data-policy-connection="${escapeHtml(integration.id)}">
                        <option value="">請選擇明確連線</option>
                        ${integration.connections.map(connection => `<option value="${escapeHtml(connection.connection_id)}" ${connection.connection_id === selectedConnectionId ? 'selected' : ''}>${escapeHtml(connection.display_name)}</option>`).join('')}
                    </select>
                </label>` : '<span class="integration-policy-warning">尚無可用連線；請先完成連線與 Project 綁定。</span>') : '';
            const resourceGroups = integration.connections.length
                ? integration.connections.map(connection => ({ id: connection.connection_id, resources: connection.resources }))
                : [{ id: '', resources: integration.resources }];
            const resourceMarkup = resourceGroups.map(group => {
                const resources = asArray(group.resources).filter(resource => resource?.resource_type && resource?.resource_id);
                if (!resources.length) return '';
                return `<div class="integration-policy-resources" data-policy-resource-group="${escapeHtml(integration.id)}" data-connection-id="${escapeHtml(group.id)}" ${group.id !== selectedConnectionId ? 'hidden' : ''}>
                    <span>資源範圍</span>
                    ${resources.map(resource => {
                        const key = `${resource.resource_type}\u001f${resource.resource_id}`;
                        return `<label><input type="checkbox" data-policy-resource="${escapeHtml(integration.id)}" data-resource-type="${escapeHtml(resource.resource_type)}" data-resource-id="${escapeHtml(resource.resource_id)}" ${selectedResources.has(key) ? 'checked' : ''}>${escapeHtml(resource.display_label || resource.resource_id)}</label>`;
                    }).join('')}
                </div>`;
            }).join('');
            const capabilityMarkup = integration.capabilityItems.length ? `
                <fieldset class="integration-policy-capabilities">
                    <legend>允許能力（只勾選真正需要的項目）</legend>
                    ${integration.capabilityItems.map(capability => `
                        <label><input type="checkbox" data-policy-capability="${escapeHtml(integration.id)}"
                            value="${escapeHtml(capability.id)}" ${selectedCapabilities.has(capability.id) ? 'checked' : ''}>
                            <span>${escapeHtml(capability.label)}</span>
                            ${capability.risk ? `<small>${escapeHtml(capability.risk)}</small>` : ''}
                        </label>`).join('')}
                </fieldset>` : '<span class="integration-policy-warning">後端尚未提供可選能力，無法放行。</span>';
            return `<article class="integration-policy-service">
                <input type="checkbox" name="integration-policy-service" value="${escapeHtml(integration.id)}" ${checked ? 'checked' : ''}
                    aria-label="在權限方案中納入 ${escapeHtml(integration.name)}">
                <span class="integration-service-icon"><i data-lucide="${escapeHtml(integration.icon)}"></i></span>
                <span><strong>${escapeHtml(integration.name)}</strong><small>${escapeHtml(integration.description)}</small></span>
                <span class="integration-status ${statusClass(integration.status)}">${escapeHtml(statusText(integration.status))}</span>
                <div class="integration-policy-service-controls">
                    ${capabilityMarkup}
                    ${connectionMarkup}
                    ${resourceMarkup}
                </div>
            </article>`;
        }).join('');
        byId('integration-policy-services').querySelectorAll('[data-policy-connection]').forEach(select => {
            select.addEventListener('change', () => updatePolicyResourceGroups(select.dataset.policyConnection, select.value));
        });
    }

    function updatePolicyResourceGroups(integrationIdValue, connectionIdValue) {
        document.querySelectorAll(`[data-policy-resource-group="${cssEscape(integrationIdValue)}"]`).forEach(group => {
            group.hidden = group.dataset.connectionId !== connectionIdValue;
        });
    }

    function updateOpenRisk() {
        const mode = document.querySelector('input[name="integration-permission-mode"]:checked')?.value || 'restricted';
        const open = mode === 'open';
        byId('integration-open-risk').hidden = !open;
        byId('integration-open-ack-row').hidden = !open;
        if (!open) byId('integration-open-ack').checked = false;
    }

    function renderHealth() {
        const integrations = normalizedIntegrations();
        byId('integration-health-list').innerHTML = integrations.map(item => {
            const health = item.health && typeof item.health === 'object' ? item.health : {};
            const detail = item.health_detail || health.detail || item.state?.provider_error || item.reason || '尚無更多診斷資訊。';
            const checkedAt = item.last_checked_at || health.checked_at;
            return `<article class="integration-health-row">
                <span class="integration-service-icon"><i data-lucide="${escapeHtml(item.icon)}"></i></span>
                <div><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(detail)}</span><small>最後檢查：${escapeHtml(checkedAt ? formatDate(checkedAt) : '尚未檢查')}</small></div>
                <span class="integration-status ${statusClass(item.status)}">${escapeHtml(statusText(item.status))}</span>
            </article>`;
        }).join('');
        const audit = state.audit;
        byId('integration-audit-list').innerHTML = audit.length ? audit.map(event => `
            <article class="integration-audit-row">
                <span class="integration-audit-icon"><i data-lucide="file-clock"></i></span>
                <div><strong>${escapeHtml(event.action || event.event_type || event.type || '整合事件')}</strong>
                    <span>${escapeHtml(event.summary || event.message || event.reason || (event.error_code ? `錯誤：${event.error_code}` : event.details?.permission_mode ? `權限模式：${event.details.permission_mode}；整合項目：${event.details.integration_count ?? 0}` : '已記錄安全事件'))}</span>
                    <small>${escapeHtml(formatDate(event.created_at || event.occurred_at || event.timestamp))}${event.actor ? ` · ${escapeHtml(event.actor)}` : ''}</small>
                </div>
            </article>`).join('') : '<div class="integration-empty">目前專案尚無整合稽核紀錄。</div>';
    }

    function renderAll() {
        renderSummary();
        renderServices();
        renderApiKeys();
        renderPolicy();
        renderHealth();
        safeIcons();
    }

    async function loadOverview() {
        if (!state.selectedProjectId) { state.overview = {}; return null; }
        const payload = await request(`/api/integration-center/overview?project_id=${encodeURIComponent(state.selectedProjectId)}`);
        state.overview = payload?.overview || payload || {};
        if (payload?.policy || state.overview?.policy) state.policy = payload.policy || state.overview.policy;
        return payload;
    }

    async function loadApiKeys() {
        const payload = await request('/api/integration-center/api-keys');
        state.installation = payload.installation || null;
        state.credentialRecoveryRequired = payload.credential_recovery_required === true;
        state.apiKeys = asArray(payload.api_keys || payload.items);
        renderApiKeys(); renderSummary(); safeIcons();
        return payload;
    }

    async function loadPolicy() {
        if (!state.selectedProjectId) { state.policy = normalizePolicy(null); return null; }
        const payload = await request(`/api/integration-center/policies/${encodeURIComponent(state.selectedProjectId)}`);
        state.policy = normalizePolicy(payload);
        return payload;
    }

    async function loadAudit() {
        if (!state.selectedProjectId) { state.audit = []; return null; }
        const payload = await request(`/api/integration-center/audit?project_id=${encodeURIComponent(state.selectedProjectId)}&limit=50`);
        state.audit = asArray(payload.audits || payload.audit || payload.events || payload.items);
        return payload;
    }

    async function refresh({ announce = false } = {}) {
        const revision = ++state.requestRevision;
        syncProjects();
        byId('integration-status').textContent = state.selectedProjectId
            ? '正在載入整合狀態…'
            : '請先建立或選擇 Project。';
        const tasks = [loadApiKeys()];
        if (state.selectedProjectId) tasks.push(loadOverview(), loadPolicy(), loadAudit());
        const results = await Promise.allSettled(tasks);
        if (revision !== state.requestRevision) return;
        const failures = results.filter(result => result.status === 'rejected');
        if (failures.length === results.length) {
            state.overview = {};
            state.policy = normalizePolicy(null);
            state.audit = [];
            byId('integration-status').textContent = '整合中心後端尚未載入；目前顯示安全的空狀態。';
            if (announce) state.deps?.showToast?.('整合中心後端尚未載入。', 'error');
        } else if (failures.length) {
            byId('integration-status').textContent = '部分整合狀態無法取得；已保留可用資料。';
            if (announce) state.deps?.showToast?.('部分整合狀態無法取得。', 'error');
        } else {
            byId('integration-status').textContent = `已載入「${projectName(state.selectedProjectId)}」的整合與權限狀態。`;
            if (announce) state.deps?.showToast?.('整合狀態已更新。', 'success');
        }
        renderAll();
    }

    async function createApiKey(event) {
        event.preventDefault();
        if (state.credentialRecoveryRequired) return state.deps?.showToast?.('請先修復此電腦的 API 驗證資料。', 'error');
        if (!state.selectedProjectId) return state.deps?.showToast?.('請先選擇 Project。', 'error');
        const name = byId('integration-api-key-name').value.trim();
        const scopes = [...document.querySelectorAll('input[name="integration-api-scope"]:checked')].map(input => input.value);
        if (!name || !scopes.length) return state.deps?.showToast?.('請輸入名稱並至少選擇一項能力。', 'error');
        const button = byId('integration-create-api-key');
        const expiryDate = byId('integration-api-key-expiry').value;
        button.disabled = true;
        try {
            const payload = await request('/api/integration-center/api-keys', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name,
                    project_id: state.selectedProjectId,
                    scopes,
                    expires_at: expiryDate
                        ? new Date(`${expiryDate}T23:59:59`).toISOString()
                        : null,
                    rate_limit_per_minute: Number(byId('integration-api-key-rate').value),
                    request_limit_daily: Number(byId('integration-api-key-daily').value),
                }),
            });
            showSecret(payload.secret, payload.notice);
            byId('integration-api-key-name').value = '';
            state.deps?.showToast?.('API Key 已建立，請立即保存完整金鑰。', 'success');
            await loadApiKeys();
        } catch (error) { state.deps?.showToast?.(error.message, 'error'); }
        finally { button.disabled = false; }
    }

    async function resetInstallation(button) {
        try {
            const payload = await request('/api/integration-center/installation/reset', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ confirmation: 'RESET_EXTERNAL_API' }),
            });
            state.deps?.showToast?.(payload.notice || '已重設安裝身分，請重新建立 API Key。', 'success');
            await loadApiKeys();
            renderAll();
        } catch (error) {
            state.deps?.showToast?.(error.message, 'error');
        } finally {
            button.disabled = false;
            button.dataset.confirmed = 'false';
            button.textContent = button.dataset.originalLabel || '重設安裝身分';
            button.classList.remove('is-confirming');
        }
    }

    async function savePolicy(event) {
        event.preventDefault();
        if (!state.selectedProjectId) return state.deps?.showToast?.('請先選擇 Project。', 'error');
        const permissionMode = document.querySelector('input[name="integration-permission-mode"]:checked')?.value || 'restricted';
        const acknowledged = byId('integration-open-ack').checked;
        if (permissionMode === 'open' && !acknowledged) {
            byId('integration-open-ack').focus();
            return state.deps?.showToast?.('請先確認你已理解開放權限的風險與可能結果。', 'error');
        }
        const integrations = normalizedIntegrations();
        const selectedIds = new Set([...document.querySelectorAll('input[name="integration-policy-service"]:checked')].map(input => input.value));
        const existingGrants = asArray(state.policy?.grants);
        let validationError = '';
        const grants = integrations.filter(item => selectedIds.has(item.id)).map(item => {
            const existing = existingGrants.find(grant => grantMatches(grant, item)) || {};
            const grant = { integration_id: item.backendId };
            const connectionId = document.querySelector(`[data-policy-connection="${cssEscape(item.id)}"]`)?.value
                || (item.connections.length === 1 ? item.connections[0].connection_id : null)
                || existing.connection_id
                || item.connection_id;
            const capabilities = [...document.querySelectorAll(`[data-policy-capability="${cssEscape(item.id)}"]:checked`)]
                .map(input => input.value);
            const resources = [...document.querySelectorAll(`[data-policy-resource="${cssEscape(item.id)}"]:checked`)]
                .filter(input => {
                    const group = input.closest('[data-policy-resource-group]');
                    return !group || !group.hidden;
                })
                .map(input => ({ resource_type: input.dataset.resourceType, resource_id: input.dataset.resourceId }));
            if (permissionMode !== 'blocked' && item.requiresConnection && !connectionId) {
                validationError ||= `${item.name} 必須先選擇明確連線，不能靜默使用其他帳號。`;
            }
            if (permissionMode !== 'blocked' && !capabilities.length) {
                validationError ||= `${item.name} 至少要明確勾選一項能力。`;
            }
            if (permissionMode !== 'blocked' && item.resourceScopeRequired && !resources.length) {
                validationError ||= `放行 ${item.name} 前必須明確選擇資源範圍。`;
            }
            if (connectionId) grant.connection_id = connectionId;
            if (capabilities.length) grant.capabilities = capabilities;
            if (resources.length) grant.resources = resources;
            return grant;
        });
        if (validationError) return state.deps?.showToast?.(validationError, 'error');
        const button = byId('integration-save-policy');
        button.disabled = true;
        try {
            const payload = await request(`/api/integration-center/policies/${encodeURIComponent(state.selectedProjectId)}`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: byId('integration-policy-name').value.trim() || '專案整合權限',
                    permission_mode: permissionMode,
                    grants,
                    revision: Number(state.policy?.revision || 0),
                    acknowledge_open_risk: permissionMode === 'open' ? acknowledged : false,
                }),
            });
            state.policy = normalizePolicy(payload);
            state.deps?.showToast?.('整合權限方案已完整套用至目前 Project。', 'success');
            await Promise.allSettled([loadOverview(), loadAudit()]);
            renderAll();
        } catch (error) {
            if (error.status === 409) void loadPolicy().then(renderPolicy);
            state.deps?.showToast?.(error.status === 409 ? '方案已被其他操作更新，已重新載入最新版本。' : error.message, 'error');
        } finally { button.disabled = false; }
    }

    function bindEvents() {
        document.querySelectorAll('[data-integration-tab]').forEach(button => {
            button.addEventListener('click', () => switchTab(button.dataset.integrationTab));
            button.addEventListener('keydown', event => {
                const tabs = [...document.querySelectorAll('[data-integration-tab]')];
                const current = tabs.indexOf(button);
                let next = null;
                if (event.key === 'ArrowRight') next = tabs[(current + 1) % tabs.length];
                if (event.key === 'ArrowLeft') next = tabs[(current - 1 + tabs.length) % tabs.length];
                if (event.key === 'Home') next = tabs[0];
                if (event.key === 'End') next = tabs[tabs.length - 1];
                if (!next) return;
                event.preventDefault(); switchTab(next.dataset.integrationTab, { focus: true });
            });
        });
        document.querySelectorAll('[data-integration-open-tab]').forEach(button => {
            button.addEventListener('click', () => switchTab(button.dataset.integrationOpenTab, { focus: true }));
        });
        byId('integration-close').addEventListener('click', close);
        byId('integration-back-chat').addEventListener('click', close);
        byId('integration-refresh').addEventListener('click', () => refresh({ announce: true }));
        byId('integration-project-select').addEventListener('change', event => {
            state.selectedProjectId = event.target.value || null;
            clearSecret(); void refresh();
        });
        byId('integration-api-key-form').addEventListener('submit', createApiKey);
        byId('integration-policy-form').addEventListener('submit', savePolicy);
        document.querySelectorAll('input[name="integration-permission-mode"]').forEach(input => input.addEventListener('change', updateOpenRisk));
        byId('integration-copy-api-base').addEventListener('click', event => {
            void copyText(event.currentTarget.dataset.copyValue, '已複製此電腦的 API 位址。').catch(error => state.deps?.showToast?.(error.message, 'error'));
        });
        byId('integration-copy-secret').addEventListener('click', () => {
            void copyText(state.oneTimeSecret, '已複製 API Key，請保存到外部系統的秘密儲存區。').catch(error => state.deps?.showToast?.(error.message, 'error'));
        });
        byId('integration-confirm-secret').addEventListener('click', () => {
            clearSecret(); state.deps?.showToast?.('一次性金鑰已從畫面清除。', 'success');
        });
        byId('integration-reset-installation')?.addEventListener('click', event => {
            armDestructiveButton(event.currentTarget, '再次點擊：撤銷全部金鑰', () => resetInstallation(event.currentTarget));
        });
        window.addEventListener('pagehide', clearSecret);
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') clearSecret();
        });
    }

    function init(config) {
        if (state.initialized) return;
        state.deps = config;
        state.initialized = true;
        syncProjects();
        bindEvents();
        switchTab('overview');
    }

    function open(tab = 'overview') {
        syncProjects(state.deps?.getActiveProjectId?.());
        state.deps?.onWorkspaceOpen?.();
        switchTab(tab);
        byId('integration-center-title')?.focus();
        void refresh();
    }

    function close() {
        clearSecret();
        state.deps?.onWorkspaceClose?.();
    }

    window.workbenchIntegrationCenter = {
        init,
        open,
        close,
        refresh,
        syncProjects,
        catalog: () => [...KNOWN_INTEGRATIONS, ...PLANNED_INTEGRATIONS].map(item => ({ ...item })),
        openTab: tab => { state.deps?.onWorkspaceOpen?.(); switchTab(tab, { focus: true }); void refresh(); },
    };
})();
