/* Local Skill lifecycle, slash shortcuts, and active-skill chips. */

(() => {
    const state = {
        catalog: [],
        sections: {},
        tab: 'installed',
        runActive: [],
        sessionActive: new Map(),
        inspected: null,
        enabled: true,
        suggestions: [],
        lastRunId: '',
    };

    const byId = id => document.getElementById(id);
    const projectQuery = () => activeProjectId ? `?project_id=${encodeURIComponent(activeProjectId)}` : '';

    async function request(path, options = {}) {
        const response = await apiFetch(`${API_BASE}${path}`, options);
        let payload = {};
        try { payload = await response.json(); } catch (_error) { /* empty */ }
        if (!response.ok) {
            const detail = payload.detail || payload;
            throw new Error(detail.message || detail.code || `HTTP ${response.status}`);
        }
        return payload;
    }

    function close() {
        byId('skill-center-modal')?.classList.remove('active');
    }

    async function open(tab = 'installed') {
        state.tab = tab;
        byId('skill-center-modal')?.classList.add('active');
        await refresh();
    }

    function dynamicSlashItems() {
        const menu = byId('slash-commands-menu');
        if (!menu) return;
        menu.querySelectorAll('[data-skill-dynamic]').forEach(item => item.remove());
        if (!state.enabled) return;
        const management = document.createElement('div');
        management.className = 'slash-command-item';
        management.dataset.command = '/skills';
        management.dataset.skillDynamic = 'true';
        management.innerHTML = '<span class="slash-command-name">/skills</span><span class="slash-command-desc">開啟 Skill 管理與選擇</span>';
        menu.appendChild(management);
        state.catalog.filter(item => item.installed && item.trusted && item.project_override !== 'disabled').forEach(skill => {
            const row = document.createElement('div');
            row.className = 'slash-command-item';
            row.dataset.command = `/skill ${skill.id}`;
            row.dataset.skillDynamic = 'true';
            const name = document.createElement('span');
            name.className = 'slash-command-name';
            name.textContent = `/skill ${skill.id}`;
            const description = document.createElement('span');
            description.className = 'slash-command-desc';
            description.textContent = `${skill.name} · ${skill.description}`;
            row.append(name, description);
            menu.appendChild(row);
        });
        state.sessionActive.forEach(skill => {
            const row = document.createElement('div');
            row.className = 'slash-command-item';
            row.dataset.command = `/skill-off ${skill.id}`;
            row.dataset.skillDynamic = 'true';
            const name = document.createElement('span');
            name.className = 'slash-command-name';
            name.textContent = `/skill-off ${skill.id}`;
            const description = document.createElement('span');
            description.className = 'slash-command-desc';
            description.textContent = `停止使用 ${skill.name}`;
            row.append(name, description);
            menu.appendChild(row);
        });
    }

    async function refresh() {
        const payload = await request(`/api/skills${projectQuery()}`);
        state.catalog = payload.skills || [];
        state.sections = payload.sections || {};
        state.enabled = payload.feature_enabled !== false;
        const button = byId('skills-button');
        if (button) button.disabled = !state.enabled;
        const status = byId('skill-center-status');
        if (status && !state.enabled) status.textContent = 'Agent Skills 功能旗標目前已關閉；已安裝資料仍保留。';
        dynamicSlashItems();
        render();
        return payload;
    }

    function button(label, action, primary = false) {
        const element = document.createElement('button');
        element.type = 'button';
        element.className = `btn ${primary ? 'btn-primary' : 'btn-secondary'} compact`;
        element.textContent = label;
        element.dataset.skillAction = action;
        return element;
    }

    function skillCard(skill) {
        const card = document.createElement('article');
        card.className = 'skill-card';
        card.dataset.skillId = skill.id;
        const head = document.createElement('div');
        head.className = 'skill-card-head';
        const title = document.createElement('div');
        title.className = 'skill-card-title';
        const strong = document.createElement('strong');
        strong.textContent = skill.name;
        const version = document.createElement('span');
        version.textContent = `${skill.id} · v${skill.version} · ${skill.publisher}`;
        title.append(strong, version);
        const status = document.createElement('span');
        status.className = `extension-badge ${skill.effective_enabled ? 'is-healthy' : ''}`;
        status.textContent = skill.effective_enabled ? '使用中' : skill.trusted ? '已信任' : skill.installed ? '待信任' : '未安裝';
        head.append(title, status);

        const description = document.createElement('div');
        description.className = 'skill-card-description';
        description.textContent = skill.description;
        const meta = document.createElement('div');
        meta.className = 'skill-card-meta';
        const digest = document.createElement('span');
        digest.className = 'skill-digest';
        digest.textContent = `SHA-256 ${String(skill.skill_sha256 || '').slice(0, 16)}…`;
        const resources = document.createElement('span');
        resources.textContent = `${(skill.resources || []).length} references · ${(skill.assets || []).length} assets`;
        const source = document.createElement('span');
        source.textContent = `來源：${skill.source || skill.origin || 'local'}`;
        meta.append(digest, resources, source);
        if (skill.last_used_at) {
            const used = document.createElement('span');
            used.textContent = `最近使用：${new Date(skill.last_used_at).toLocaleString()}`;
            meta.appendChild(used);
        }
        if (skill.contains_scripts) {
            const risk = document.createElement('span');
            risk.className = 'skill-risk';
            risk.textContent = `含 ${(skill.scripts || []).length} 個 script（不自動執行）`;
            meta.appendChild(risk);
        }
        if ((skill.missing_dependencies || []).length) {
            const missing = document.createElement('span');
            missing.className = 'skill-risk';
            missing.textContent = `缺少：${skill.missing_dependencies.join(', ')}`;
            meta.appendChild(missing);
        }
        const actions = document.createElement('div');
        actions.className = 'skill-card-actions';
        actions.append(button('預覽', 'preview'));
        if ((skill.missing_dependencies || []).length) actions.append(button('查看擴充中心', 'extensions'));
        if (!skill.installed) actions.append(button('檢查並安裝', 'install', true));
        else if (!skill.trusted) actions.append(button('審查並信任', 'trust', true));
        else {
            actions.append(button('本輪使用', 'use-turn', true));
            actions.append(button('本對話使用', 'use-session'));
            actions.append(button(skill.global_enabled ? '全域停用' : '全域啟用', skill.global_enabled ? 'disable' : 'enable'));
            if (activeProjectId) {
                if (skill.project_override === 'enabled') {
                    actions.append(button('取消專案預設', 'project-inherit'));
                } else if (skill.project_override === 'disabled') {
                    actions.append(button('專案恢復繼承', 'project-inherit'));
                } else {
                    actions.append(button('設為專案預設', 'project-enable'));
                    actions.append(button('此專案停用', 'project-disable'));
                }
            }
            actions.append(button('Audit', 'audit'));
            actions.append(button('移除', 'remove'));
        }
        card.append(head, description, meta, actions);
        return card;
    }

    function render() {
        const list = byId('skill-list');
        if (!list) return;
        document.querySelectorAll('[data-skill-tab]').forEach(tab => {
            const active = tab.dataset.skillTab === state.tab;
            tab.classList.toggle('active', active);
            tab.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        byId('skill-local-picker').hidden = state.tab !== 'local';
        const query = (byId('skill-search')?.value || '').trim().toLowerCase();
        let items = state.sections[state.tab] || [];
        if (state.tab === 'local') items = state.catalog;
        items = items.filter(skill => !query || `${skill.id} ${skill.name} ${skill.description} ${skill.publisher}`.toLowerCase().includes(query));
        list.replaceChildren();
        if (!items.length) {
            const empty = document.createElement('div');
            empty.className = 'extension-state';
            empty.textContent = state.tab === 'local' ? '輸入本機資料夾或 ZIP basename 進行檢查。' : '沒有符合的 Skill。';
            list.appendChild(empty);
        } else {
            items.forEach(skill => list.appendChild(skillCard(skill)));
        }
        safeCreateIcons();
    }

    async function activate(skill, scope) {
        await ensureSession();
        await request(`/api/sessions/${encodeURIComponent(currentSessionId)}/skills/activate`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                skill_id: skill.id, scope, skill_sha256: skill.skill_sha256,
                project_id: activeProjectId || null,
            })
        });
        state.sessionActive.set(skill.id, { ...skill, trigger_mode: scope });
        renderChips();
        showToast(`${skill.name} 已設為${scope === 'turn' ? '下一輪' : '本對話'}使用。`, 'success');
    }

    async function deactivate(skillId) {
        if (!currentSessionId) return;
        await request(`/api/sessions/${encodeURIComponent(currentSessionId)}/skills/${encodeURIComponent(skillId)}`, { method: 'DELETE' });
        state.sessionActive.delete(skillId);
        state.runActive = state.runActive.filter(item => item.id !== skillId);
        renderChips();
    }

    async function inspectSource() {
        const source = byId('skill-local-source').value.trim();
        if (!source) return;
        const payload = await request(`/api/skills/local/inspect${projectQuery()}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source })
        });
        state.inspected = payload.skill;
        const existing = state.catalog.findIndex(item => item.id === payload.skill.id);
        if (existing >= 0) state.catalog[existing] = payload.skill;
        else state.catalog.push(payload.skill);
        state.sections.local = [payload.skill];
        render();
        byId('skill-center-status').textContent = `已檢查 ${payload.skill.name}；digest ${payload.skill.skill_sha256.slice(0, 16)}…。尚未安裝或信任。`;
    }

    async function act(skill, action) {
        if (action === 'preview') {
            const current = skill.installed
                ? (await request(`/api/skills/${encodeURIComponent(skill.id)}${projectQuery()}`)).skill
                : skill;
            alert(`${current.name} v${current.version}\n發布者：${current.publisher}\n來源：${current.source}\nSHA-256：${current.skill_sha256}\n\n${current.instructions_preview || '沒有可顯示的指令摘要。'}\n\nReferences:\n${(current.resources || []).join('\n') || 'none'}\n\nScripts（唯讀、不自動執行）：\n${(current.scripts || []).join('\n') || 'none'}`);
            return;
        } else if (action === 'extensions') {
            close();
            await window.workbenchExtensions?.open('installed');
            return;
        } else if (action === 'install') {
            const scripts = (skill.scripts || []).length;
            const okay = confirm(`安裝 ${skill.name} v${skill.version}？\n來源：${skill.source}\nSHA-256：${skill.skill_sha256}\nReferences：${(skill.resources || []).length}\nScripts：${scripts}（不會自動執行）`);
            if (!okay) return;
            await request(`/api/skills/${encodeURIComponent(skill.id)}/install${projectQuery()}`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ skill_sha256: skill.skill_sha256, allow_downgrade: false })
            });
        } else if (action === 'trust') {
            if (!confirm(`信任目前 Skill digest？\n${skill.skill_sha256}\n內容變更後會自動撤銷信任。`)) return;
            await request(`/api/skills/${encodeURIComponent(skill.id)}/trust${projectQuery()}`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ skill_sha256: skill.skill_sha256 })
            });
        } else if (action === 'enable' || action === 'disable') {
            await request(`/api/skills/${encodeURIComponent(skill.id)}/state${projectQuery()}`, {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ global_enabled: action === 'enable', skill_sha256: action === 'enable' ? skill.skill_sha256 : null })
            });
        } else if (action === 'project-enable' || action === 'project-disable' || action === 'project-inherit') {
            const mode = action === 'project-enable' ? 'enabled' : action === 'project-disable' ? 'disabled' : 'inherit';
            await request(`/api/projects/${encodeURIComponent(activeProjectId)}/skills/${encodeURIComponent(skill.id)}`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode, skill_sha256: mode === 'enabled' ? skill.skill_sha256 : null })
            });
        } else if (action === 'use-turn' || action === 'use-session') {
            await activate(skill, action === 'use-turn' ? 'turn' : 'session');
        } else if (action === 'audit') {
            const payload = await request(`/api/skills/${encodeURIComponent(skill.id)}/audits`);
            alert((payload.audits || []).slice(0, 10).map(row => `${row.created_at} · ${row.action} · ${row.status}`).join('\n') || '尚無 Audit');
        } else if (action === 'remove') {
            if (!confirm(`移除 ${skill.name}？信任與專案狀態會一併清除。`)) return;
            await request(`/api/skills/${encodeURIComponent(skill.id)}`, { method: 'DELETE' });
            state.sessionActive.delete(skill.id);
        }
        await refresh();
        renderChips();
    }

    function renderChips(active = null) {
        if (Array.isArray(active)) state.runActive = active;
        const bar = byId('active-skills-bar');
        if (!bar) return;
        const merged = new Map(state.sessionActive);
        state.runActive.forEach(item => merged.set(item.id, item));
        bar.replaceChildren();
        merged.forEach(skill => {
            const chip = document.createElement('span');
            chip.className = 'active-skill-chip';
            const label = document.createElement('span');
            label.textContent = `${skill.name} v${skill.version} · ${skill.trigger_mode || 'active'}`;
            const remove = document.createElement('button');
            remove.type = 'button'; remove.textContent = '×'; remove.title = '停止使用 Skill';
            remove.addEventListener('click', () => {
                deactivate(skill.id).then(() => {
                    if (skill.trigger_mode !== 'auto' || !state.lastRunId) return;
                    return request(`/api/runs/${encodeURIComponent(state.lastRunId)}/skills/${encodeURIComponent(skill.id)}/feedback`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ event: 'false_trigger' })
                    }).catch(error => console.warn('[Skills feedback]', error));
                }).catch(error => showToast(error.message, 'error'));
            });
            chip.append(label, remove); bar.appendChild(chip);
        });
        state.suggestions.forEach(suggestion => {
            const chip = document.createElement('span');
            chip.className = 'active-skill-chip skill-suggestion-chip';
            const label = document.createElement('span');
            label.textContent = `建議：${suggestion.name}`;
            const accept = document.createElement('button');
            accept.type = 'button'; accept.textContent = '下輪套用';
            accept.addEventListener('click', () => {
                const skill = state.catalog.find(item => item.id === suggestion.id);
                if (!skill) return;
                activate(skill, 'turn').then(() => request(
                    `/api/runs/${encodeURIComponent(state.lastRunId)}/skills/${encodeURIComponent(skill.id)}/feedback`,
                    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ event: 'auto_suggestion_accepted' }) }
                )).then(() => {
                    state.suggestions = state.suggestions.filter(item => item.id !== suggestion.id);
                    renderChips();
                }).catch(error => showToast(error.message, 'error'));
            });
            chip.append(label, accept); bar.appendChild(chip);
        });
        bar.hidden = merged.size === 0 && state.suggestions.length === 0;
    }

    async function prepareSubmission(question) {
        if (!state.enabled && /^\/skills?(?:-|\s|$)/i.test(String(question || ''))) {
            throw new Error('Agent Skills 功能目前已關閉。');
        }
        const match = String(question || '').match(/^\/(skills|skill-off|skill)(?:\s+([^\s]+))?(?:\s+([\s\S]+))?$/i);
        if (!match) return { message: question, skillIds: [] };
        const command = match[1].toLowerCase();
        const skillId = match[2] || '';
        const task = (match[3] || '').trim();
        if (command === 'skills' || (command === 'skill' && !skillId)) {
            await open('installed');
            return null;
        }
        const normalized = skillId.toLowerCase();
        const skill = state.catalog.find(item => item.id === normalized || String(item.name || '').toLowerCase() === normalized);
        if (!skill) throw new Error(`找不到 Skill：${skillId}`);
        if (command === 'skill-off') {
            await deactivate(skill.id);
            return null;
        }
        if (!skill.installed || !skill.trusted || skill.project_override === 'disabled') {
            throw new Error(`Skill 尚未安裝、信任，或已在此專案停用：${skillId}`);
        }
        if (!task) {
            await activate(skill, 'session');
            return null;
        }
        await activate(skill, 'turn');
        return { message: task, skillIds: [skill.id] };
    }

    function handleRunEvent(payload) {
        state.lastRunId = String(payload.run_id || '');
        state.suggestions = Array.isArray(payload.suggestions) ? payload.suggestions : [];
        renderChips(payload.active || []);
    }

    function initSlashCommands({ input, menu, clearHistory }) {
        let selected = 0;
        const visible = () => [...menu.querySelectorAll('.slash-command-item')].filter(item => !item.hidden);
        const update = () => visible().forEach((item, index) => item.classList.toggle('selected', index === selected));
        const execute = command => {
            if (command === '/clean') {
                input.value = ''; clearHistory();
            } else if (command === '/skills') {
                open('installed').catch(error => showToast(error.message, 'error'));
            } else {
                input.value = `${command} `; input.focus();
            }
            menu.classList.remove('active');
        };
        input.addEventListener('input', () => {
            const value = input.value;
            if (!value.startsWith('/')) { menu.classList.remove('active'); return; }
            const query = value.trim().toLowerCase();
            menu.querySelectorAll('.slash-command-item').forEach(item => {
                const command = String(item.dataset.command || '').toLowerCase();
                item.hidden = Boolean(query && !command.startsWith(query)
                    && !item.textContent.toLowerCase().includes(query.replace(/^\//, '')));
            });
            selected = 0; menu.classList.add('active'); update();
        });
        input.addEventListener('keydown', event => {
            if (!menu.classList.contains('active')) return;
            const items = visible();
            if (!items.length) return;
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                selected = (selected + (event.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length;
                update();
            } else if (event.key === 'Enter') {
                event.preventDefault(); execute(items[selected].dataset.command);
            } else if (event.key === 'Escape') {
                event.preventDefault(); menu.classList.remove('active');
            }
        });
        menu.addEventListener('click', event => {
            const item = event.target.closest('.slash-command-item');
            if (item) execute(item.dataset.command);
        });
    }

    function init() {
        if (typeof BASIC_CHAT_MODE !== 'undefined' && BASIC_CHAT_MODE) return;
        byId('skills-button')?.addEventListener('click', () => open('installed').catch(error => showToast(error.message, 'error')));
        byId('skills-close')?.addEventListener('click', close);
        byId('skills-done')?.addEventListener('click', close);
        byId('skill-refresh')?.addEventListener('click', () => refresh().catch(error => showToast(error.message, 'error')));
        byId('skill-search')?.addEventListener('input', render);
        byId('skill-local-source')?.addEventListener('input', event => { byId('skill-local-inspect').disabled = !event.target.value.trim(); });
        byId('skill-local-inspect')?.addEventListener('click', () => inspectSource().catch(error => showToast(error.message, 'error')));
        document.querySelectorAll('[data-skill-tab]').forEach(tab => tab.addEventListener('click', () => { state.tab = tab.dataset.skillTab; render(); }));
        byId('skill-list')?.addEventListener('click', event => {
            const action = event.target.closest('[data-skill-action]');
            const card = event.target.closest('[data-skill-id]');
            if (!action || !card) return;
            const skill = state.catalog.find(item => item.id === card.dataset.skillId);
            if (skill) act(skill, action.dataset.skillAction).catch(error => showToast(error.message, 'error'));
        });
        byId('skill-center-modal')?.addEventListener('click', event => { if (event.target === byId('skill-center-modal')) close(); });
        refresh().catch(error => console.warn('[Skills]', error));
    }

    window.workbenchSkills = { open, refresh, prepareSubmission, handleRunEvent, renderChips, initSlashCommands, init };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
