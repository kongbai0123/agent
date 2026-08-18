(() => {
    'use strict';

    const state = {
        deps: null,
        initialized: false,
        connectors: [],
        connections: [],
        profiles: new Map(),
        extensionStates: new Map(),
        extensionCatalogReady: false,
        selectedProjectId: null,
        polling: null,
    };

    const byId = id => document.getElementById(id);

    function safeIcons() {
        try { window.lucide?.createIcons?.(); } catch (_error) { /* decorative only */ }
    }

    function errorMessage(payload, fallback) {
        return payload?.detail?.message || payload?.message || payload?.detail || fallback;
    }

    async function request(path, options = {}) {
        if (!state.deps?.apiFetch) throw new Error('連接器中心尚未初始化');
        const response = await state.deps.apiFetch(`${state.deps.apiBase || ''}${path}`, options);
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.success === false) {
            const error = new Error(errorMessage(payload, `HTTP ${response.status}`));
            error.status = response.status;
            error.code = payload?.detail?.code || payload?.code || null;
            error.payload = payload;
            throw error;
        }
        return payload;
    }

    function button(label, icon, className = 'btn btn-secondary compact') {
        const element = document.createElement('button');
        element.type = 'button';
        element.className = className;
        const iconNode = document.createElement('i');
        iconNode.dataset.lucide = icon;
        element.append(iconNode, document.createTextNode(label));
        return element;
    }

    function callbackUrl(connector) {
        return `${window.location.origin}${connector.callback_path || `/oauth/callback/${connector.id}`}`;
    }

    function activeProjects() {
        return (state.deps?.getProjects?.() || []).filter(project => !project.archived);
    }

    function ensureSelectedProject() {
        const projects = activeProjects();
        const preferred = state.selectedProjectId || state.deps?.getActiveProjectId?.();
        state.selectedProjectId = projects.some(project => String(project.id) === String(preferred))
            ? String(preferred)
            : (projects[0] ? String(projects[0].id) : null);
    }

    function setProject(projectId) {
        state.selectedProjectId = projectId ? String(projectId) : null;
        ensureSelectedProject();
        return state.selectedProjectId;
    }

    function renderHeading(root) {
        const heading = document.createElement('div');
        heading.className = 'connector-center-head';
        const copy = document.createElement('div');
        const title = document.createElement('h3');
        title.textContent = '帳號連線與 Project 範圍';
        const note = document.createElement('p');
        note.textContent = '憑證只保存在本機加密儲存區；Agent 只能使用此 Project 明確選取的資源。';
        copy.append(title, note);
        const label = document.createElement('label');
        label.className = 'extension-scope';
        const text = document.createElement('span');
        text.textContent = 'Project';
        const select = document.createElement('select');
        select.className = 'settings-input';
        select.setAttribute('aria-label', '連接器作用 Project');
        const projects = activeProjects();
        if (!projects.length) {
            const option = document.createElement('option');
            option.value = '';
            option.textContent = '請先建立 Project';
            select.append(option);
            select.disabled = true;
        } else {
            projects.forEach(project => {
                const option = document.createElement('option');
                option.value = String(project.id);
                option.textContent = project.name;
                select.append(option);
            });
            select.value = state.selectedProjectId || '';
        }
        select.addEventListener('change', async () => {
            setProject(select.value || null);
            window.dispatchEvent(new CustomEvent('workbench:connector-project-change', {
                detail: { projectId: state.selectedProjectId },
            }));
            await refresh();
        });
        label.append(text, select);
        heading.append(copy, label);
        root.append(heading);
    }

    function profileForm(connector, profile) {
        const form = document.createElement('form');
        form.className = 'connector-form';

        const callbackLabel = document.createElement('label');
        callbackLabel.append(document.createTextNode('OAuth Callback URL'));
        const callbackRow = document.createElement('div');
        callbackRow.className = 'connector-callback';
        const callback = document.createElement('input');
        callback.className = 'settings-input';
        callback.readOnly = true;
        callback.value = callbackUrl(connector);
        const copy = button('複製', 'copy');
        copy.addEventListener('click', async () => {
            await navigator.clipboard.writeText(callback.value);
            state.deps?.showToast?.('已複製 Callback URL', 'success');
        });
        callbackRow.append(callback, copy);
        callbackLabel.append(callbackRow);
        if (profile?.callback_uri && profile.callback_uri !== callback.value) {
            const changed = document.createElement('small');
            changed.className = 'extension-state is-warning';
            changed.textContent = 'Workbench 位址已變更。請先在第三方 OAuth App 更新為上方 URL，再儲存此設定並重新連線。';
            callbackLabel.append(changed);
        }

        const clientIdLabel = document.createElement('label');
        clientIdLabel.append(document.createTextNode('Client ID'));
        const clientId = document.createElement('input');
        clientId.className = 'settings-input';
        clientId.required = true;
        clientId.maxLength = 255;
        clientId.autocomplete = 'off';
        clientId.value = profile?.client_id || '';
        clientIdLabel.append(clientId);

        const secretLabel = document.createElement('label');
        secretLabel.append(document.createTextNode(profile?.configured ? 'Client Secret（留白代表不變）' : 'Client Secret'));
        const secret = document.createElement('input');
        secret.className = 'settings-input';
        secret.type = 'password';
        secret.autocomplete = 'new-password';
        secret.required = !profile?.configured;
        secret.maxLength = 2048;
        secretLabel.append(secret);

        const actions = document.createElement('div');
        actions.className = 'connector-card-actions';
        const save = button('安全儲存 OAuth 設定', 'shield-check', 'btn btn-primary compact');
        save.type = 'submit';
        actions.append(save);
        if (profile?.configured) {
            const remove = button('移除設定', 'trash-2');
            remove.addEventListener('click', async () => {
                if (!window.confirm(`移除 ${connector.name} 的本機 OAuth 設定？現有連線必須先中斷。`)) return;
                try {
                    await request(`/api/connectors/${encodeURIComponent(connector.id)}/auth-profile`, { method: 'DELETE' });
                    state.deps?.showToast?.('OAuth 設定已移除', 'success');
                    await refresh();
                } catch (error) {
                    state.deps?.showToast?.(error.message, 'error');
                }
            });
            actions.append(remove);
        }

        form.append(callbackLabel, clientIdLabel, secretLabel, actions);
        form.addEventListener('submit', async event => {
            event.preventDefault();
            save.disabled = true;
            try {
                const body = {
                    client_id: clientId.value.trim(),
                    callback_uri: callback.value,
                };
                if (secret.value) body.client_secret = secret.value;
                await request(`/api/connectors/${encodeURIComponent(connector.id)}/auth-profile`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                secret.value = '';
                state.deps?.showToast?.(`${connector.name} OAuth 設定已安全儲存`, 'success');
                await refresh();
            } catch (error) {
                state.deps?.showToast?.(error.message, 'error');
            } finally {
                save.disabled = false;
            }
        });
        return form;
    }

    function normalizeConnectionLabel(connection) {
        return connection.display_name || connection.display_label || connection.account_login
            || connection.workspace_name || connection.account_id || connectionId(connection);
    }

    function connectionId(connection) {
        return String(connection?.connection_id || connection?.id || '');
    }

    function projectBinding(connection) {
        return connection.project_binding || connection.binding || null;
    }

    async function bindConnection(connection, mode) {
        if (!state.selectedProjectId) throw new Error('請先選擇 Project');
        return request(`/api/projects/${encodeURIComponent(state.selectedProjectId)}/connections/${encodeURIComponent(connectionId(connection))}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: true, mode }),
        });
    }

    function normalizeResource(item, fallbackType) {
        return {
            resource_type: item.resource_type || item.type || fallbackType,
            resource_id: String(item.resource_id || item.id || ''),
            parent_id: item.parent_id || null,
            display_label: item.display_label || item.name || item.full_name || item.title || item.id,
            metadata: item.metadata || {},
        };
    }

    async function fetchDiscoverableResources(connector, connection) {
        const types = connector.id === 'github' ? ['repository'] : ['page', 'database'];
        const batches = await Promise.all(types.map(async type => {
            const result = await request(`/api/connectors/connections/${encodeURIComponent(connectionId(connection))}/resources?type=${encodeURIComponent(type)}`);
            const items = result.resources || result.items || [];
            return items.map(item => normalizeResource(item, type));
        }));
        const unique = new Map();
        batches.flat().forEach(item => {
            if (item.resource_id) unique.set(`${item.resource_type}:${item.resource_id}`, item);
        });
        return [...unique.values()];
    }

    async function renderResourcePicker(container, connector, connection) {
        container.replaceChildren();
        const loading = document.createElement('div');
        loading.className = 'extension-state is-loading';
        loading.textContent = '正在載入可授權資源…';
        container.append(loading);
        try {
            if (!state.selectedProjectId) throw new Error('請先選擇 Project');
            const initialBinding = await bindConnection(
                connection,
                projectBinding(connection)?.mode || 'read_only',
            );
            if (initialBinding.binding) connection.binding = initialBinding.binding;
            const [discoverable, current] = await Promise.all([
                fetchDiscoverableResources(connector, connection),
                request(`/api/projects/${encodeURIComponent(state.selectedProjectId)}/connections/${encodeURIComponent(connectionId(connection))}/resources`),
            ]);
            const selected = new Set((current.resources || []).map(item => `${item.resource_type}:${item.resource_id}`));
            const availableByKey = new Map(discoverable.map(item => [
                `${item.resource_type}:${item.resource_id}`,
                item,
            ]));
            (current.resources || []).forEach(item => {
                const normalized = normalizeResource(item, item.resource_type);
                const key = `${normalized.resource_type}:${normalized.resource_id}`;
                if (!availableByKey.has(key)) {
                    availableByKey.set(key, { ...normalized, noLongerDiscoverable: true });
                }
            });
            const available = [...availableByKey.values()];
            const wrapper = document.createElement('div');
            wrapper.className = 'connector-resources';
            const head = document.createElement('div');
            head.className = 'connector-resource-head';
            const heading = document.createElement('strong');
            heading.textContent = '允許 Agent 使用的資源';
            const mode = document.createElement('select');
            mode.className = 'settings-input';
            mode.innerHTML = '<option value="read_only">僅讀取</option><option value="read_write">讀取＋逐次核准寫入</option>';
            mode.value = current.mode || projectBinding(connection)?.mode || 'read_only';
            head.append(heading, mode);
            const list = document.createElement('div');
            list.className = 'connector-resource-list';
            if (!available.length) {
                const empty = document.createElement('div');
                empty.className = 'extension-state';
                empty.textContent = '此帳號沒有可選取的資源，或第三方服務尚未授權任何根資源。';
                list.append(empty);
            }
            available.forEach(item => {
                const label = document.createElement('label');
                label.className = 'connector-resource-row';
                const checkbox = document.createElement('input');
                checkbox.type = 'checkbox';
                checkbox.checked = selected.has(`${item.resource_type}:${item.resource_id}`);
                checkbox._resource = item;
                const copy = document.createElement('span');
                const title = document.createElement('strong');
                title.textContent = item.display_label;
                const detail = document.createElement('small');
                detail.textContent = item.noLongerDiscoverable
                    ? `${item.resource_type} · 已無法從服務取得；取消選取後儲存即可移除`
                    : `${item.resource_type} · ${item.resource_id}`;
                copy.append(title, detail);
                label.append(checkbox, copy);
                list.append(label);
            });
            const save = button('儲存 Project 資源範圍', 'save', 'btn btn-primary compact');
            save.addEventListener('click', async () => {
                save.disabled = true;
                try {
                    const bindingUpdate = await bindConnection(connection, mode.value);
                    if (bindingUpdate.binding) connection.binding = bindingUpdate.binding;
                    const resources = [...list.querySelectorAll('input[type="checkbox"]:checked')]
                        .map(input => input._resource);
                    const updated = await request(`/api/projects/${encodeURIComponent(state.selectedProjectId)}/connections/${encodeURIComponent(connectionId(connection))}/resources`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ revision: Number(current.revision || 0), resources }),
                    });
                    current.revision = Number(updated.revision || current.revision || 0);
                    state.deps?.showToast?.('Project 資源範圍已更新', 'success');
                    await refresh();
                } catch (error) {
                    state.deps?.showToast?.(error.message, 'error');
                    if (error.code === 'RESOURCE_BINDING_REVISION_CONFLICT') {
                        await renderResourcePicker(container, connector, connection);
                    } else {
                        save.disabled = false;
                    }
                }
            });
            wrapper.append(head, list, save);
            container.replaceChildren(wrapper);
            safeIcons();
        } catch (error) {
            loading.className = 'extension-state is-error';
            loading.textContent = error.message;
        }
    }

    function connectionRow(connector, connection, operational = true) {
        const row = document.createElement('div');
        row.className = 'connector-connection';
        const head = document.createElement('div');
        head.className = 'connector-card-head';
        const copy = document.createElement('div');
        const title = document.createElement('strong');
        title.textContent = normalizeConnectionLabel(connection);
        const detail = document.createElement('small');
        detail.textContent = `狀態：${connection.status || 'unknown'}${connection.token_expires_at ? ` · 到期 ${connection.token_expires_at}` : ''}`;
        copy.append(title, detail);
        const actions = document.createElement('div');
        actions.className = 'connector-card-actions';
        const health = button('測試', 'activity');
        health.disabled = !operational;
        if (!operational) health.title = '請先安裝並啟用對應的外掛程式';
        health.addEventListener('click', async () => {
            health.disabled = true;
            try {
                await request(`/api/connectors/connections/${encodeURIComponent(connectionId(connection))}/health`, { method: 'POST' });
                state.deps?.showToast?.('連線測試完成', 'success');
                await refresh();
            } catch (error) {
                state.deps?.showToast?.(error.message, 'error');
                health.disabled = false;
            }
        });
        const resources = button('選擇資源', 'folder-key');
        resources.disabled = !operational;
        if (!operational) resources.title = '請先安裝並啟用對應的外掛程式';
        const disconnect = button('中斷', 'unlink');
        disconnect.addEventListener('click', async () => {
            if (!window.confirm(`中斷 ${normalizeConnectionLabel(connection)}？`)) return;
            try {
                await request(`/api/connectors/connections/${encodeURIComponent(connectionId(connection))}`, { method: 'DELETE' });
                state.deps?.showToast?.('帳號連線已中斷', 'success');
                await refresh();
            } catch (error) {
                if (error.code === 'CONNECTOR_REVOKE_FAILED' && window.confirm(`${error.message}\n\n是否只移除本機憑證？`)) {
                    await request(`/api/connectors/connections/${encodeURIComponent(connectionId(connection))}?force_local=true`, { method: 'DELETE' });
                    await refresh();
                } else {
                    state.deps?.showToast?.(error.message, 'error');
                }
            }
        });
        actions.append(health, resources, disconnect);
        head.append(copy, actions);
        const resourceHost = document.createElement('div');
        resourceHost.hidden = true;
        resources.addEventListener('click', async () => {
            resourceHost.hidden = !resourceHost.hidden;
            if (!resourceHost.hidden) await renderResourcePicker(resourceHost, connector, connection);
        });
        row.append(head, resourceHost);
        return row;
    }

    function stopOAuthPolling() {
        if (!state.polling) return;
        state.polling.cancelled = true;
        if (state.polling.timer) clearTimeout(state.polling.timer);
        state.polling = null;
    }

    function connectionVersion(connection) {
        return `${connection.status || ''}:${connection.updated_at || ''}`;
    }

    function startOAuthPolling(connectorId, baseline, expiresAt) {
        stopOAuthPolling();
        const parsedExpiry = Date.parse(expiresAt || '');
        const deadline = Number.isFinite(parsedExpiry) && parsedExpiry > Date.now()
            ? parsedExpiry
            : Date.now() + 10 * 60 * 1000;
        const poll = { cancelled: false, timer: null };
        state.polling = poll;

        const check = async () => {
            if (poll.cancelled || state.polling !== poll) return;
            if (Date.now() >= deadline) {
                stopOAuthPolling();
                state.deps?.showToast?.('OAuth 授權等待逾時，請重新嘗試。', 'warning');
                await refresh();
                return;
            }
            try {
                const payload = await request(`/api/connectors/connections?connector_id=${encodeURIComponent(connectorId)}`);
                const current = payload.connections || [];
                const completed = current.some(connection => {
                    if (connection.status !== 'connected') return false;
                    const id = connectionId(connection);
                    return !baseline.has(id) || baseline.get(id) !== connectionVersion(connection);
                });
                if (completed) {
                    stopOAuthPolling();
                    await refresh();
                    state.deps?.showToast?.('OAuth 連線完成', 'success');
                    return;
                }
            } catch (_error) { /* transient local error; retry until the flow expires */ }
            if (!poll.cancelled && state.polling === poll) {
                poll.timer = setTimeout(check, 1800);
            }
        };
        poll.timer = setTimeout(check, 600);
    }

    function connectorCard(connector) {
        const profile = state.profiles.get(connector.id) || null;
        const connections = state.connections.filter(item => item.connector_id === connector.id);
        const extension = state.extensionStates.get(`connector.${connector.id}`) || null;
        const extensionReady = !!(
            state.extensionCatalogReady
            && extension?.installed
            && extension?.trusted
            && extension?.runtime_available !== false
            && extension?.effective_enabled
        );
        const card = document.createElement('article');
        card.className = `connector-card${extensionReady ? '' : ' is-disabled'}`;
        const head = document.createElement('div');
        head.className = 'connector-card-head';
        const title = document.createElement('h3');
        const icon = document.createElement('i');
        icon.dataset.lucide = connector.id === 'github' ? 'github' : 'notebook-text';
        title.append(icon, document.createTextNode(connector.name || connector.id));
        const status = document.createElement('span');
        const connected = connections.some(item => item.status === 'connected');
        status.className = `connector-status-pill${connected && extensionReady ? ' is-connected' : ''}`;
        status.textContent = !extensionReady
            ? (connected ? '已連線 · 外掛停用' : '外掛未啟用')
            : (connected ? '已連線' : (profile?.configured ? '待連線' : '尚未設定'));
        head.append(title, status);
        const description = document.createElement('p');
        description.textContent = connector.description || '';
        card.append(head, description);

        if (!extensionReady) {
            const notice = document.createElement('div');
            notice.className = 'extension-state is-warning';
            notice.textContent = state.extensionCatalogReady
                ? '請先安裝、信任並在目前 Project 啟用對應外掛；已有帳號仍可斷開。'
                : '無法確認外掛啟用狀態，已暫停新增連線。';
            const manage = button(
                extension?.installed ? '前往啟用外掛' : '前往安裝外掛',
                'puzzle',
                'btn btn-secondary compact'
            );
            manage.addEventListener('click', () => {
                window.workbenchExtensions?.open?.(
                    extension?.installed ? 'installed' : 'available',
                    state.selectedProjectId
                );
            });
            card.append(notice, manage);
        } else {
            card.append(profileForm(connector, profile));
        }

        if (extensionReady && profile?.configured) {
            const connect = button(connected ? '重新連線／新增帳號' : '連接帳號', 'external-link', 'btn btn-primary compact');
            connect.addEventListener('click', async () => {
                connect.disabled = true;
                try {
                    const baseline = new Map(connections.map(connection => [
                        connectionId(connection),
                        connectionVersion(connection),
                    ]));
                    const result = await request(`/api/connectors/${encodeURIComponent(connector.id)}/oauth/start`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
                    });
                    const target = new URL(result.authorization_url);
                    window.open(target.href, '_blank', 'noopener,noreferrer');
                    startOAuthPolling(connector.id, baseline, result.expires_at);
                } catch (error) {
                    state.deps?.showToast?.(error.message, 'error');
                    connect.disabled = false;
                }
            });
            card.append(connect);
        }

        if (connections.length) {
            const list = document.createElement('div');
            list.className = 'connector-form';
            connections.forEach(connection => list.append(connectionRow(connector, connection, extensionReady)));
            card.append(list);
        }
        return card;
    }

    function render() {
        const root = byId('connector-center');
        if (!root) return;
        root.replaceChildren();
        renderHeading(root);
        if (!state.connectors.length) {
            const empty = document.createElement('div');
            empty.className = 'extension-state';
            empty.textContent = '尚無可用 Connector。';
            root.append(empty);
        } else {
            state.connectors.forEach(connector => root.append(connectorCard(connector)));
        }
        safeIcons();
    }

    async function refresh(options = {}) {
        if (!state.initialized) return;
        if (typeof options === 'string') {
            setProject(options);
        } else if (options && Object.prototype.hasOwnProperty.call(options, 'projectId')) {
            setProject(options.projectId);
        }
        ensureSelectedProject();
        const projectQuery = state.selectedProjectId
            ? `?project_id=${encodeURIComponent(state.selectedProjectId)}`
            : '';
        try {
            const catalog = await request('/api/connectors');
            state.connectors = catalog.connectors || [];
            const [profilePairs, connections, extensionCatalog] = await Promise.all([
                Promise.all(state.connectors.map(async connector => {
                    try {
                        const result = await request(`/api/connectors/${encodeURIComponent(connector.id)}/auth-profile/status`);
                        return [connector.id, result.profile || null];
                    } catch (_error) {
                        return [connector.id, null];
                    }
                })),
                request(`/api/connectors/connections${projectQuery}`),
                request(`/api/extensions${projectQuery}`).catch(() => null),
            ]);
            state.profiles = new Map(profilePairs);
            state.connections = connections.connections || [];
            state.extensionCatalogReady = !!extensionCatalog;
            state.extensionStates = new Map(
                (extensionCatalog?.extensions || [])
                    .filter(item => item?.kind === 'connector' || String(item?.id || '').startsWith('connector.'))
                    .map(item => [String(item.id), item])
            );
            render();
        } catch (error) {
            const root = byId('connector-center');
            if (root) {
                const failed = document.createElement('div');
                failed.className = 'extension-state is-error';
                failed.textContent = `Connector 載入失敗：${error.message}`;
                root.replaceChildren(failed);
            }
        }
    }

    function init(dependencies = {}) {
        state.deps = dependencies;
        if (state.initialized) return;
        state.initialized = true;
        ensureSelectedProject();
    }

    window.workbenchConnectors = { init, refresh, setProject };
})();
