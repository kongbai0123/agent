(function createCloudLlmCenter() {
    'use strict';

    const MAX_PROVIDERS = 8;
    const state = {
        deps: null,
        initialized: false,
        editingId: null,
        lifecycleRevision: 0,
        governance: null,
        governanceTab: 'connections'
    };

    const byId = id => document.getElementById(id);
    const escapeHtml = value => String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');

    function providers() {
        return state.deps?.collectProviders?.() || [];
    }

    function secretStatus(providerId) {
        return state.deps?.getSecretStatus?.()?.[providerId] || {};
    }

    function providerKind(provider) {
        return state.deps?.inferModelKind?.(provider.selected_model, provider.model_kind)
            || provider.model_kind
            || 'unknown';
    }

    function kindLabel(kind) {
        return {
            chat: '對話 / Agent',
            translation: '翻譯',
            embedding: '嵌入',
            rerank: '重排序',
            vision: '視覺',
            unknown: '未分類'
        }[kind] || '未分類';
    }

    function providerLabel(type) {
        return {
            gemini: 'Google Gemini',
            nvidia: 'NVIDIA',
            openai: 'OpenAI',
            openai_compatible: '相容 API'
        }[type] || type || '其他';
    }

    function currentFilters() {
        return {
            query: byId('cloud-llm-search')?.value.trim().toLowerCase() || '',
            provider: byId('cloud-llm-provider-filter')?.value || 'all',
            kind: byId('cloud-llm-kind-filter')?.value || 'all',
            status: byId('cloud-llm-status-filter')?.value || 'all'
        };
    }

    function isVisible(provider, filters) {
        const status = secretStatus(provider.id);
        const haystack = [
            provider.label,
            provider.id,
            provider.provider_type,
            provider.selected_model,
            provider.base_url
        ].join(' ').toLowerCase();
        if (filters.query && !haystack.includes(filters.query)) return false;
        if (filters.provider !== 'all' && provider.provider_type !== filters.provider) return false;
        if (filters.kind !== 'all' && providerKind(provider) !== filters.kind) return false;
        if (filters.status === 'enabled' && provider.enabled !== true) return false;
        if (filters.status === 'disabled' && provider.enabled === true) return false;
        if (filters.status === 'configured' && status.configured !== true) return false;
        if (filters.status === 'missing-key' && status.configured === true) return false;
        return true;
    }

    function summaryCard(provider) {
        const status = secretStatus(provider.id);
        const kind = providerKind(provider);
        const keyText = status.configured
            ? `金鑰 ••••${escapeHtml(status.last4 || '')}`
            : '尚未設定金鑰';
        return `
            <article class="cloud-llm-item" data-cloud-provider-id="${escapeHtml(provider.id)}">
                <div class="cloud-llm-item-icon" aria-hidden="true">
                    <i data-lucide="${provider.provider_type === 'nvidia' ? 'cpu' : 'cloud'}"></i>
                </div>
                <div class="cloud-llm-item-main">
                    <div class="cloud-llm-item-title">
                        <strong>${escapeHtml(provider.label || provider.id)}</strong>
                        <span class="cloud-llm-badge">${escapeHtml(providerLabel(provider.provider_type))}</span>
                        <span class="cloud-llm-badge kind">${escapeHtml(kindLabel(kind))}</span>
                        <span class="cloud-llm-status ${provider.enabled ? 'enabled' : 'disabled'}">
                            ${provider.enabled ? '已啟用' : '已停用'}
                        </span>
                    </div>
                    <div class="cloud-llm-item-model">${escapeHtml(provider.selected_model || '尚未選擇模型')}</div>
                    <div class="cloud-llm-item-meta">
                        <span>ID：${escapeHtml(provider.id)}</span>
                        <span class="${status.configured ? 'configured' : 'missing'}">${keyText}</span>
                    </div>
                </div>
                <div class="cloud-llm-item-actions">
                    <button type="button" class="btn btn-secondary compact" data-cloud-edit="${escapeHtml(provider.id)}">
                        編輯
                    </button>
                    <button type="button" class="btn btn-danger-subtle compact" data-cloud-delete="${escapeHtml(provider.id)}">
                        刪除
                    </button>
                </div>
            </article>`;
    }

    function updateCount(allProviders, visibleProviders) {
        const count = byId('cloud-llm-count');
        if (count) count.textContent = `${visibleProviders.length} / ${allProviders.length} 筆`;
        const add = byId('btn-add-model-provider');
        if (add) {
            add.disabled = allProviders.length >= MAX_PROVIDERS;
            add.title = add.disabled ? `最多可導入 ${MAX_PROVIDERS} 筆 API` : '';
        }
    }

    function renderLibrary() {
        const list = byId('cloud-llm-library-list');
        if (!list) return;
        const allProviders = providers();
        const visible = allProviders.filter(item => isVisible(item, currentFilters()));
        updateCount(allProviders, visible);
        if (!allProviders.length) {
            list.innerHTML = `
                <div class="cloud-llm-empty">
                    <i data-lucide="cloud"></i>
                    <strong>尚未導入雲端 LLM API</strong>
                    <span>新增後會保留為獨立連線，不會覆蓋既有 API。</span>
                </div>`;
        } else if (!visible.length) {
            list.innerHTML = '<div class="cloud-llm-empty compact">沒有符合目前搜尋或篩選條件的連線。</div>';
        } else {
            list.innerHTML = visible.map(summaryCard).join('');
        }
        state.deps?.createIcons?.();
    }

    async function governanceJson(path, options = {}) {
        const response = await state.deps.apiFetch(`${state.deps.apiBase}${path}`, options);
        let data = {};
        try { data = await response.json(); } catch (_) { /* handled below */ }
        if (!response.ok || data.success === false) {
            throw new Error(data.detail?.message || data.message || `HTTP ${response.status}`);
        }
        return data;
    }

    function selectedProjectId() {
        return String(state.deps?.getProjectId?.() || '').trim();
    }

    function expiryLabel(credential) {
        if (credential?.never_expires) return '永不到期（使用者聲明）';
        if (!credential?.expires_at) return '到期日未知';
        const days = credential.remaining_days;
        return `${new Date(credential.expires_at).toLocaleString()}${Number.isFinite(days) ? ` · 剩餘 ${days} 天` : ''}`;
    }

    function renderGovernanceHealth() {
        const data = state.governance || {};
        const totals = data.usage?.totals || {};
        const historical = data.usage?.historical_runs?.totals || {};
        const costs = Object.entries(data.usage?.cost_by_currency || {});
        const metrics = byId('cloud-governance-usage-summary');
        if (metrics) metrics.innerHTML = [
            ['本機觀測請求', Number(totals.requests || 0).toLocaleString()],
            ['本機觀測 Token', Number(totals.total_tokens || 0).toLocaleString()],
            ['OCR 圖片量', `${Number(totals.image_megabytes || 0).toFixed(3)} MiB`],
            ['估算成本', costs.length ? costs.map(([currency, amount]) => `${currency} ${Number(amount).toFixed(4)}`).join(' · ') : '尚無可估算費率'],
            ['歷史 Run（唯讀）', `${Number(historical.runs || 0).toLocaleString()} · ${Number(historical.total_tokens || 0).toLocaleString()} Token（不計入預算）`]
        ].map(([label, value]) => `<div class="governance-metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('');
        const list = byId('cloud-governance-provider-health');
        if (!list) return;
        list.innerHTML = (data.providers || []).map(item => {
            const operational = item.operational || {};
            const credential = item.credential || {};
            const stateName = operational.state || 'unknown';
            const retry = operational.retry_at ? ` · 可重試 ${new Date(operational.retry_at).toLocaleString()}` : '';
            const expiryDate = credential.expires_at ? String(credential.expires_at).slice(0, 10) : '';
            return `<article class="governance-provider" data-governance-provider="${escapeHtml(item.provider_id)}">
                <div><strong>${escapeHtml(item.provider_id)}</strong><span>${escapeHtml(item.model_id || '尚未選擇模型')}</span></div>
                <div class="governance-provider-state ${escapeHtml(stateName)}">${escapeHtml(stateName)}${escapeHtml(retry)}</div>
                <div class="governance-expiry-row">
                    <span>${escapeHtml(expiryLabel(credential))}</span>
                    <input type="date" class="settings-input" data-governance-expiry value="${escapeHtml(expiryDate)}" aria-label="API Key 到期日">
                    <label><input type="checkbox" data-governance-never ${credential.never_expires ? 'checked' : ''}>永不到期</label>
                    <button type="button" class="btn btn-secondary compact" data-save-credential-metadata>儲存到期資訊</button>
                </div>
                <small>最後驗證：${escapeHtml(credential.last_verified_at ? new Date(credential.last_verified_at).toLocaleString() : '尚未完成')}</small>
            </article>`;
        }).join('') || '<div class="cloud-llm-empty compact">尚未導入供應商。</div>';
    }

    function budgetInput(scope, period, metric, value) {
        const labels = { requests: '請求數', tokens: 'Token', cost: '成本' };
        return `<label>${scope === 'global' ? '全域' : '專案'} ${period === 'daily' ? '每日' : '每月'} ${labels[metric]}
            <input class="settings-input" type="number" min="0" step="${metric === 'cost' ? '0.0001' : '1'}"
                   data-budget-scope="${scope}" data-budget-period="${period}" data-budget-metric="${metric}"
                   value="${escapeHtml(value || '')}" placeholder="未設定"></label>`;
    }

    function renderGovernanceBudgets() {
        const data = state.governance || {};
        const fields = byId('cloud-governance-budget-fields');
        if (fields) {
            const rows = [];
            for (const [scope, record] of [['global', data.global_budget], ['project', data.project_budget]]) {
                if (!record) continue;
                for (const period of ['daily', 'monthly']) {
                    for (const metric of ['requests', 'tokens', 'cost']) rows.push(budgetInput(scope, period, metric, record.policy?.[period]?.[metric]));
                }
            }
            fields.innerHTML = rows.join('') || '<p>請先選擇專案以設定專案預算。</p>';
        }
        const policy = data.routing_policy;
        const card = byId('cloud-governance-routing-card');
        if (card) card.hidden = !policy;
        if (!policy) return;
        byId('cloud-routing-mode').value = policy.mode || 'ask';
        byId('cloud-consent-text').checked = policy.data_consent?.text === true;
        byId('cloud-consent-images').checked = policy.data_consent?.images === true;
        byId('cloud-consent-documents').checked = policy.data_consent?.documents === true;
        const providerBox = byId('cloud-routing-providers');
        if (providerBox) providerBox.innerHTML = providers().map(provider => `<label><input type="checkbox" data-routing-provider="${escapeHtml(provider.id)}" ${(policy.allowed_providers || []).includes(provider.id) ? 'checked' : ''}>允許 ${escapeHtml(provider.label || provider.id)}</label>`).join('');
    }

    async function loadGovernance() {
        const projectId = selectedProjectId();
        const path = projectId
            ? `/api/model-governance/overview?project_id=${encodeURIComponent(projectId)}`
            : '/api/model-governance/overview';
        state.governance = await governanceJson(path);
        renderGovernanceHealth();
        renderGovernanceBudgets();
    }

    async function showGovernanceTab(tab) {
        state.governanceTab = tab;
        document.querySelectorAll('[data-cloud-governance-tab]').forEach(button => {
            const active = button.dataset.cloudGovernanceTab === tab;
            button.classList.toggle('active', active);
            button.setAttribute('aria-selected', String(active));
        });
        for (const name of ['connections', 'health', 'budgets']) {
            const panel = byId(`cloud-governance-${name}-panel`);
            if (panel) panel.hidden = name !== tab;
        }
        if (tab !== 'connections') {
            try { await loadGovernance(); }
            catch (error) { state.deps?.showToast?.(`治理資料載入失敗：${error.message}`, 'error'); }
        }
    }

    async function saveCredentialMetadata(button) {
        const card = button.closest('[data-governance-provider]');
        const providerId = card?.dataset.governanceProvider;
        if (!providerId) return;
        const never = card.querySelector('[data-governance-never]').checked;
        const date = card.querySelector('[data-governance-expiry]').value;
        const expiresAt = !never && date ? new Date(`${date}T23:59:59`).toISOString() : null;
        await governanceJson(`/api/model-governance/providers/${encodeURIComponent(providerId)}/credential-metadata`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ expires_at: expiresAt, expiry_source: date && !never ? 'user_declared' : 'unknown', never_expires: never })
        });
        await loadGovernance();
        state.deps?.showToast?.('API Key 到期資訊已更新', 'success');
    }

    function collectBudget(scope, existing) {
        const policy = { daily: {}, monthly: {} };
        document.querySelectorAll(`[data-budget-scope="${scope}"]`).forEach(input => {
            const value = Number(input.value || 0);
            if (value > 0) policy[input.dataset.budgetPeriod][input.dataset.budgetMetric] = value;
        });
        policy.daily.currency = existing?.policy?.daily?.currency || 'USD';
        policy.monthly.currency = existing?.policy?.monthly?.currency || 'USD';
        return policy;
    }

    async function saveBudgets() {
        const data = state.governance || {};
        const operations = [['global', 'global', data.global_budget]];
        if (data.project_budget && selectedProjectId()) operations.push(['project', selectedProjectId(), data.project_budget]);
        for (const [scope, id, record] of operations) {
            await governanceJson(`/api/model-governance/budgets/${scope}/${encodeURIComponent(id)}`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ revision: record.revision || 0, timezone: record.timezone || 'Asia/Taipei', policy: collectBudget(scope, record) })
            });
        }
        await loadGovernance();
        state.deps?.showToast?.('模型預算已儲存', 'success');
    }

    async function saveRouting() {
        const projectId = selectedProjectId();
        const policy = state.governance?.routing_policy;
        if (!projectId || !policy) return;
        const allowed = [...document.querySelectorAll('[data-routing-provider]:checked')].map(input => input.dataset.routingProvider);
        await governanceJson(`/api/projects/${encodeURIComponent(projectId)}/model-routing-policy`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ revision: policy.revision || 0, mode: byId('cloud-routing-mode').value, allowed_providers: allowed, data_consent: { text: byId('cloud-consent-text').checked, images: byId('cloud-consent-images').checked, documents: byId('cloud-consent-documents').checked }, preferred_models: policy.preferred_models || [] })
        });
        await loadGovernance();
        state.deps?.showToast?.('專案選模與資料同意政策已儲存', 'success');
    }

    function setView(view) {
        const library = byId('cloud-llm-library-view');
        const editor = byId('cloud-llm-editor-view');
        if (library) library.hidden = view !== 'library';
        if (editor) editor.hidden = view !== 'editor';
        byId('cloud-llm-workspace')?.classList.toggle('is-editing', view === 'editor');
    }

    function showLibrary() {
        state.editingId = null;
        document.querySelectorAll('#model-provider-list [data-provider-card]')
            .forEach(card => { card.hidden = false; });
        setView('library');
        renderLibrary();
        showGovernanceTab(state.governanceTab);
    }

    async function discardEditor() {
        if (state.editingId) await state.deps?.reloadProviders?.();
        showLibrary();
    }

    function showEditor(providerId) {
        let selected = null;
        document.querySelectorAll('#model-provider-list [data-provider-card]').forEach(card => {
            const id = card.querySelector('[data-provider-field="id"]')?.value;
            card.hidden = id !== providerId;
            if (id === providerId) selected = card;
        });
        if (!selected) return;
        state.editingId = providerId;
        const title = byId('cloud-llm-editor-title');
        if (title) title.textContent = providerId.startsWith('connection') ? 'API 連線設定' : '編輯 API 連線';
        setView('editor');
        setTimeout(() => selected.querySelector('[data-provider-field="label"]')?.focus(), 20);
    }

    function addProvider() {
        if (providers().length >= MAX_PROVIDERS) {
            state.deps?.showToast?.(`最多可導入 ${MAX_PROVIDERS} 筆 API`, 'error');
            return;
        }
        const list = byId('model-provider-list');
        list?.querySelector('.model-provider-empty')?.remove();
        list?.insertAdjacentHTML('beforeend', state.deps.providerCard({
            id: state.deps.nextProviderId(),
            provider_type: 'gemini',
            label: 'Google Gemini API',
            base_url: 'https://generativelanguage.googleapis.com/v1beta/openai',
            enabled: false,
            currency: 'USD'
        }));
        const added = providers().at(-1);
        if (added) showEditor(added.id);
    }

    async function saveProviders() {
        const records = providers();
        const ids = records.map(provider => provider.id);
        if (ids.some(id => !id) || new Set(ids).size !== ids.length) {
            throw new Error('每筆 API 連線都必須有唯一識別碼');
        }
        const payload = {
            ...state.deps.getSettings(),
            model_provider: 'ollama',
            model_providers: records
        };
        const response = await state.deps.apiFetch(`${state.deps.apiBase}/api/settings`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.detail?.message || data.message || '後端拒絕儲存 API 設定');
        }
        state.deps.setSettings(payload);
        await state.deps.saveSecrets();
        await state.deps.loadProviders(records);
        await state.deps.refreshModels();
        return records;
    }

    async function persistAndReturn() {
        const save = byId('cloud-llm-save');
        if (save) save.disabled = true;
        try {
            await saveProviders();
            showLibrary();
            state.deps?.showToast?.('雲端 LLM API 已儲存', 'success');
        } catch (error) {
            state.deps?.showToast?.(`API 儲存失敗：${error.message}`, 'error');
        } finally {
            if (save) save.disabled = false;
        }
    }

    async function deleteProvider(providerId) {
        const provider = providers().find(item => item.id === providerId);
        if (!provider) return;
        const confirmed = window.confirm(`確定刪除「${provider.label || provider.id}」？已保存的 API 金鑰也會一併刪除。`);
        if (!confirmed) return;
        const card = [...document.querySelectorAll('#model-provider-list [data-provider-card]')]
            .find(item => item.querySelector('[data-provider-field="id"]')?.value === providerId);
        card?.querySelector('[data-remove-provider]')?.click();
        try {
            await saveProviders();
            renderLibrary();
            state.deps?.showToast?.('API 連線與金鑰已刪除', 'success');
        } catch (error) {
            await state.deps?.reloadProviders?.();
            renderLibrary();
            state.deps?.showToast?.(`刪除失敗：${error.message}`, 'error');
        }
    }

    function open() {
        state.lifecycleRevision += 1;
        state.deps?.onWorkspaceOpen?.();
        const workspace = byId('cloud-llm-workspace');
        if (workspace) workspace.hidden = false;
        showLibrary();
        setTimeout(() => byId('cloud-llm-search')?.focus(), 20);
    }

    async function deactivate() {
        const revision = ++state.lifecycleRevision;
        const discardEditor = Boolean(state.editingId);
        try {
            if (discardEditor) await state.deps?.reloadProviders?.();
        } catch (error) {
            state.deps?.showToast?.(`無法重新載入 API 設定：${error.message}`, 'error');
        }
        // A rapid re-open supersedes this close. The settings reload still
        // discarded the stale form, so refresh the visible library without
        // hiding the newly opened workspace.
        if (revision !== state.lifecycleRevision) {
            renderLibrary();
            return false;
        }
        const workspace = byId('cloud-llm-workspace');
        if (workspace) workspace.hidden = true;
        showLibrary();
        return true;
    }

    async function close() {
        if (await deactivate()) state.deps?.onWorkspaceClose?.();
    }

    function bindEvents() {
        byId('cloud-llm-close')?.addEventListener('click', close);
        byId('cloud-llm-close-btn')?.addEventListener('click', close);
        byId('cloud-llm-back')?.addEventListener('click', discardEditor);
        byId('cloud-llm-editor-cancel')?.addEventListener('click', discardEditor);
        byId('cloud-llm-save')?.addEventListener('click', persistAndReturn);
        byId('btn-add-model-provider')?.addEventListener('click', addProvider);
        document.querySelectorAll('[data-cloud-governance-tab]').forEach(button => button.addEventListener('click', () => showGovernanceTab(button.dataset.cloudGovernanceTab)));
        byId('cloud-governance-refresh')?.addEventListener('click', loadGovernance);
        byId('cloud-governance-provider-health')?.addEventListener('click', event => {
            const button = event.target.closest('[data-save-credential-metadata]');
            if (button) saveCredentialMetadata(button).catch(error => state.deps?.showToast?.(`到期資訊儲存失敗：${error.message}`, 'error'));
        });
        byId('cloud-governance-save-budgets')?.addEventListener('click', () => saveBudgets().catch(error => state.deps?.showToast?.(`預算儲存失敗：${error.message}`, 'error')));
        byId('cloud-governance-save-routing')?.addEventListener('click', () => saveRouting().catch(error => state.deps?.showToast?.(`選模政策儲存失敗：${error.message}`, 'error')));
        byId('btn-open-cloud-llm-settings')?.addEventListener('click', () => {
            byId('settings-modal')?.classList.remove('active');
            open();
        });
        ['cloud-llm-search', 'cloud-llm-provider-filter', 'cloud-llm-kind-filter', 'cloud-llm-status-filter']
            .forEach(id => byId(id)?.addEventListener(id === 'cloud-llm-search' ? 'input' : 'change', renderLibrary));
        byId('cloud-llm-library-list')?.addEventListener('click', event => {
            const edit = event.target.closest('[data-cloud-edit]');
            if (edit) return showEditor(edit.dataset.cloudEdit);
            const remove = event.target.closest('[data-cloud-delete]');
            if (remove) deleteProvider(remove.dataset.cloudDelete);
        });
    }

    function init(deps) {
        if (state.initialized) return;
        state.deps = deps;
        state.initialized = true;
        const workspace = byId('cloud-llm-workspace');
        const workbenchBody = document.querySelector('.workbench-body');
        if (workspace && workbenchBody && workspace.parentElement !== workbenchBody) {
            workbenchBody.appendChild(workspace);
        }
        bindEvents();
        renderLibrary();
    }

    function openTab(tab = 'connections') {
        state.governanceTab = ['connections', 'health', 'budgets'].includes(tab) ? tab : 'connections';
        open();
    }

    window.workbenchCloudLlm = { init, open, openTab, close, deactivate, render: renderLibrary };
})();
