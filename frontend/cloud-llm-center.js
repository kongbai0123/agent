(function createCloudLlmCenter() {
    'use strict';

    const MAX_PROVIDERS = 8;
    const state = {
        deps: null,
        initialized: false,
        editingId: null
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

    function setView(view) {
        const library = byId('cloud-llm-library-view');
        const editor = byId('cloud-llm-editor-view');
        if (library) library.hidden = view !== 'library';
        if (editor) editor.hidden = view !== 'editor';
        byId('cloud-llm-modal')?.classList.toggle('is-editing', view === 'editor');
    }

    function showLibrary() {
        state.editingId = null;
        document.querySelectorAll('#model-provider-list [data-provider-card]')
            .forEach(card => { card.hidden = false; });
        setView('library');
        renderLibrary();
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
        byId('cloud-llm-modal')?.classList.add('active');
        showLibrary();
        setTimeout(() => byId('cloud-llm-search')?.focus(), 20);
    }

    async function close() {
        if (state.editingId) await state.deps?.reloadProviders?.();
        byId('cloud-llm-modal')?.classList.remove('active');
        showLibrary();
    }

    function bindEvents() {
        byId('cloud-llm-close')?.addEventListener('click', close);
        byId('cloud-llm-close-btn')?.addEventListener('click', close);
        byId('cloud-llm-back')?.addEventListener('click', discardEditor);
        byId('cloud-llm-editor-cancel')?.addEventListener('click', discardEditor);
        byId('cloud-llm-save')?.addEventListener('click', persistAndReturn);
        byId('btn-add-model-provider')?.addEventListener('click', addProvider);
        byId('btn-open-cloud-llm-settings')?.addEventListener('click', () => {
            byId('settings-modal')?.classList.remove('active');
            open();
        });
        byId('cloud-llm-modal')?.addEventListener('click', event => {
            if (event.target === byId('cloud-llm-modal')) close();
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
        bindEvents();
        renderLibrary();
    }

    window.workbenchCloudLlm = { init, open, close, render: renderLibrary };
})();
