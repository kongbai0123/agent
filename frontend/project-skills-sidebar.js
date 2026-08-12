/* Project-owned, data-only Skills shown inside each project in the chat sidebar. */

(() => {
    'use strict';

    const ALLOWED_REFERENCE_EXTENSIONS = new Set([
        '.css', '.csv', '.html', '.js', '.json', '.jsx', '.md', '.py', '.rst',
        '.sql', '.toml', '.ts', '.tsv', '.tsx', '.txt', '.xml', '.yaml', '.yml',
    ]);
    const WINDOWS_RESERVED_NAMES = new Set([
        'con', 'prn', 'aux', 'nul',
        ...Array.from({ length: 9 }, (_item, index) => `com${index + 1}`),
        ...Array.from({ length: 9 }, (_item, index) => `lpt${index + 1}`),
    ]);
    const MAX_REFERENCE_FILES = 64;
    const MAX_REFERENCE_BYTES = 8 * 1024 * 1024;
    const MAX_REFERENCE_PACKAGE_BYTES = 32 * 1024 * 1024;

    const state = {
        initialized: false,
        expandedProjects: new Set(),
        projects: new Map(),
        session: {
            sessionId: null,
            projectId: null,
            status: 'idle',
            catalog: new Map(),
            error: null,
            requestId: 0,
        },
        editor: {
            open: false,
            mode: null,
            project: null,
            slug: null,
            expectedSha256: null,
            originalEnabled: true,
            references: new Map(),
            versions: [],
            versionsStatus: 'idle',
            versionsError: null,
            versionPreview: null,
            versionPreviewStatus: 'idle',
            versionPreviewError: null,
            requestId: 0,
            versionsRequestId: 0,
            versionPreviewRequestId: 0,
            generation: 0,
            saving: false,
            slugDirty: false,
            restoreFocus: null,
        },
        addMenu: {
            section: null,
            button: null,
            menu: null,
            anchor: null,
        },
        busy: new Set(),
    };

    let deps = null;
    let addMenuSequence = 0;

    const byId = id => document.getElementById(id);
    const encoded = value => encodeURIComponent(String(value || ''));

    function element(tag, className = '', text = null) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== null) node.textContent = String(text);
        return node;
    }

    function icon(name) {
        const node = document.createElement('i');
        node.dataset.lucide = name;
        node.setAttribute('aria-hidden', 'true');
        return node;
    }

    function iconButton(name, label, className) {
        const button = element('button', className);
        button.type = 'button';
        button.title = label;
        button.setAttribute('aria-label', label);
        button.appendChild(icon(name));
        return button;
    }

    function projectRecord(projectId) {
        if (!state.projects.has(projectId)) {
            state.projects.set(projectId, {
                status: 'idle',
                items: [],
                error: null,
                requestId: 0,
            });
        }
        return state.projects.get(projectId);
    }

    function skillSlug(skill) {
        const explicit = String(skill?.slug || '').trim();
        if (explicit) return explicit;
        const identifier = String(skill?.id || '').trim();
        const separator = identifier.lastIndexOf(':');
        return separator >= 0 ? identifier.slice(separator + 1) : identifier;
    }

    function skillSha256(skill) {
        return String(skill?.sha256 || skill?.skill_sha256 || '').trim();
    }

    function errorMessage(payload, status) {
        const detail = payload?.detail || payload || {};
        return String(
            detail.message || detail.error || detail.code || payload?.message || `HTTP ${status}`
        );
    }

    async function request(path, options = {}) {
        if (!deps) throw new Error('Project Skills 尚未初始化。');
        const response = await deps.apiFetch(`${deps.apiBase}${path}`, options);
        let payload = {};
        try {
            payload = await response.json();
        } catch (_error) {
            payload = {};
        }
        if (!response.ok) {
            const error = new Error(errorMessage(payload, response.status));
            error.status = response.status;
            error.payload = payload;
            throw error;
        }
        return payload;
    }

    function refreshProjectSections(projectId = null) {
        document.querySelectorAll('[data-project-skills-project-id]').forEach(section => {
            const project = section._projectSkillsProject;
            if (!project || (projectId && project.id !== projectId)) return;
            renderProjectSection(section, project, section._projectSkillsOptions || {});
        });
        deps?.createIcons?.();
    }

    function inlineState(message, kind = '') {
        const row = element('div', `project-skills-inline-state ${kind}`.trim(), message);
        row.setAttribute('role', kind === 'is-error' ? 'alert' : 'status');
        return row;
    }

    async function loadProject(projectId, { force = false } = {}) {
        const record = projectRecord(projectId);
        if (!force && ['loading', 'ready'].includes(record.status)) return record;
        const requestId = ++record.requestId;
        record.status = 'loading';
        record.error = null;
        refreshProjectSections(projectId);
        try {
            const payload = await request(`/api/projects/${encoded(projectId)}/skills`);
            if (requestId !== record.requestId) return record;
            record.items = Array.isArray(payload.skills) ? payload.skills : [];
            record.status = 'ready';
        } catch (error) {
            if (requestId !== record.requestId) return record;
            record.status = 'error';
            record.error = error.message;
        }
        refreshProjectSections(projectId);
        return record;
    }

    function currentSessionSkill(slug) {
        return state.session.catalog.get(slug) || null;
    }

    async function loadSessionCatalog({ force = false } = {}) {
        const sessionId = state.session.sessionId;
        const projectId = state.session.projectId;
        if (!sessionId || !projectId) {
            state.session.status = 'idle';
            state.session.catalog = new Map();
            state.session.error = null;
            refreshProjectSections();
            return;
        }
        if (!force && ['loading', 'ready'].includes(state.session.status)) return;
        const requestId = ++state.session.requestId;
        state.session.status = 'loading';
        state.session.error = null;
        refreshProjectSections(projectId);
        try {
            const payload = await request(`/api/sessions/${encoded(sessionId)}/skills`);
            if (
                requestId !== state.session.requestId
                || sessionId !== state.session.sessionId
                || projectId !== state.session.projectId
            ) return;
            const items = Array.isArray(payload.skills)
                ? payload.skills
                : Array.isArray(payload.catalog) ? payload.catalog : [];
            state.session.catalog = new Map(
                items.map(item => [skillSlug(item), item]).filter(([slug]) => Boolean(slug))
            );
            state.session.status = 'ready';
        } catch (error) {
            if (requestId !== state.session.requestId) return;
            state.session.status = 'error';
            state.session.error = error.message;
        }
        refreshProjectSections(projectId);
    }

    function setSessionContext({ sessionId = null, projectId = null } = {}) {
        const normalizedSession = sessionId ? String(sessionId) : null;
        const normalizedProject = projectId ? String(projectId) : null;
        if (
            normalizedSession === state.session.sessionId
            && normalizedProject === state.session.projectId
        ) {
            refreshProjectSections(normalizedProject);
            void loadSessionCatalog({ force: true });
            return;
        }
        const previousProject = state.session.projectId;
        state.session.sessionId = normalizedSession;
        state.session.projectId = normalizedProject;
        state.session.status = 'idle';
        state.session.catalog = new Map();
        state.session.error = null;
        state.session.requestId += 1;
        refreshProjectSections(previousProject);
        refreshProjectSections(normalizedProject);
        void loadSessionCatalog({ force: true });
    }

    function activationValue(skill) {
        const override = String(skill?.session_override || 'inherit').toLowerCase();
        if (override === 'enabled') {
            return String(skill?.session_scope || 'session').toLowerCase() === 'turn'
                ? 'turn'
                : 'session';
        }
        if (override === 'disabled') return 'disabled';
        return 'inherit';
    }

    function activationOption(value, label) {
        const option = element('option', '', label);
        option.value = value;
        return option;
    }

    async function updateSessionActivation(project, skill, selection, select) {
        const sessionId = state.session.sessionId;
        if (!sessionId || state.session.projectId !== project.id) return;
        const slug = skillSlug(skill);
        const sha256 = skillSha256(skill);
        const previous = select.dataset.previousValue || activationValue(currentSessionSkill(slug));
        const key = `session:${sessionId}:${slug}`;
        if (state.busy.has(key)) return;
        const modes = {
            inherit: { mode: 'inherit', scope: 'session' },
            session: { mode: 'enabled', scope: 'session' },
            turn: { mode: 'enabled', scope: 'turn' },
            disabled: { mode: 'disabled', scope: 'session' },
        };
        const body = modes[selection] || modes.inherit;
        state.busy.add(key);
        select.disabled = true;
        try {
            await request(
                `/api/sessions/${encoded(sessionId)}/skills/${encoded(slug)}`,
                {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        ...body,
                        expected_sha256: sha256,
                    }),
                }
            );
            select.dataset.previousValue = selection;
            await loadSessionCatalog({ force: true });
            deps.showToast?.(`${skill.name || slug} 的對話使用方式已更新。`, 'success');
        } catch (error) {
            select.value = previous;
            deps.showToast?.(`無法更新對話 Skill：${error.message}`, 'error');
            await Promise.all([
                loadProject(project.id, { force: true }),
                loadSessionCatalog({ force: true }),
            ]);
        } finally {
            state.busy.delete(key);
            select.disabled = false;
        }
    }

    async function toggleSkill(project, skill) {
        const slug = skillSlug(skill);
        const key = `state:${project.id}:${slug}`;
        if (state.busy.has(key) || project.archived) return;
        state.busy.add(key);
        refreshProjectSections(project.id);
        try {
            await request(
                `/api/projects/${encoded(project.id)}/skills/${encoded(slug)}/state`,
                {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        expected_sha256: skillSha256(skill),
                        enabled: skill.enabled !== true,
                    }),
                }
            );
            await Promise.all([
                loadProject(project.id, { force: true }),
                state.session.projectId === project.id
                    ? loadSessionCatalog({ force: true })
                    : Promise.resolve(),
            ]);
        } catch (error) {
            deps.showToast?.(`無法更新 Project Skill：${error.message}`, 'error');
            await loadProject(project.id, { force: true });
        } finally {
            state.busy.delete(key);
            refreshProjectSections(project.id);
        }
    }

    async function deleteSkill(project, skill) {
        const slug = skillSlug(skill);
        if (project.archived || !confirm(`確定刪除 Project Skill「${skill.name || slug}」？參考檔與對話啟用狀態也會移除。`)) return;
        const key = `delete:${project.id}:${slug}`;
        if (state.busy.has(key)) return;
        state.busy.add(key);
        refreshProjectSections(project.id);
        try {
            await request(`/api/projects/${encoded(project.id)}/skills/${encoded(slug)}`, {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ expected_sha256: skillSha256(skill) }),
            });
            closeEditor({ restoreFocus: false });
            await Promise.all([
                loadProject(project.id, { force: true }),
                state.session.projectId === project.id
                    ? loadSessionCatalog({ force: true })
                    : Promise.resolve(),
            ]);
            deps.showToast?.(`已刪除 ${skill.name || slug}。`, 'success');
        } catch (error) {
            deps.showToast?.(`無法刪除 Project Skill：${error.message}`, 'error');
            await loadProject(project.id, { force: true });
        } finally {
            state.busy.delete(key);
            refreshProjectSections(project.id);
        }
    }

    function createActivationSelect(project, skill) {
        if (!state.session.sessionId || state.session.projectId !== project.id) return null;
        const catalogSkill = currentSessionSkill(skillSlug(skill));
        const select = element('select', 'project-skill-session-select');
        select.setAttribute('aria-label', `${skill.name || skillSlug(skill)} 的對話使用方式`);
        select.append(
            activationOption('inherit', '依專案'),
            activationOption('session', '本對話'),
            activationOption('turn', '下一輪'),
            activationOption('disabled', '不使用')
        );
        const stale = catalogSkill?.activation_stale === true;
        if (stale) {
            const staleOption = activationOption('stale', '版本已更新');
            staleOption.disabled = true;
            select.prepend(staleOption);
        }
        const selected = stale ? 'stale' : activationValue(catalogSkill);
        select.value = selected;
        select.dataset.previousValue = selected;
        select.classList.toggle('is-active', catalogSkill?.active === true);
        select.classList.toggle('is-stale', stale);
        select.title = stale
            ? 'Skill 已更新；請重新選擇使用方式以確認目前版本。'
            : '選擇這個 Skill 在目前對話的使用方式。';
        select.disabled = skill.enabled !== true || state.session.status === 'loading';
        select.addEventListener('click', event => event.stopPropagation());
        select.addEventListener('change', event => {
            event.stopPropagation();
            void updateSessionActivation(project, skill, select.value, select);
        });
        return select;
    }

    function openSkillMenu(event, project, skill) {
        event.stopPropagation();
        if (!deps?.openContextMenu) {
            void openEditor(project, skill);
            return;
        }
        const readOnly = Boolean(project.archived);
        const items = [
            { label: readOnly ? '檢視 Skill' : '編輯 Skill', icon: 'pencil', run: () => openEditor(project, skill) },
            { label: '參考檔', icon: 'files', run: () => openEditor(project, skill, 'references') },
            { label: '版本紀錄', icon: 'history', run: () => openEditor(project, skill, 'versions') },
        ];
        if (!readOnly) {
            items.push(
                { separator: true },
                { label: '刪除 Skill', icon: 'trash-2', danger: true, run: () => deleteSkill(project, skill) }
            );
        }
        deps.openContextMenu(event, items);
    }

    function createSkillRow(project, skill) {
        const slug = skillSlug(skill);
        const busy = [...state.busy].some(key => key.includes(`:${project.id}:${slug}`));
        const row = element('div', `project-skill-row ${skill.enabled === true ? 'is-enabled' : 'is-disabled'}`);
        row.dataset.projectSkillSlug = slug;

        const copy = element('button', 'project-skill-copy');
        copy.type = 'button';
        copy.title = skill.description || skill.name || slug;
        const name = element('span', 'project-skill-name', skill.name || slug);
        const meta = element(
            'span',
            'project-skill-meta',
            `${slug}${skill.version ? ` · v${skill.version}` : ''}`
        );
        copy.append(name, meta);
        copy.addEventListener('click', event => {
            event.stopPropagation();
            void openEditor(project, skill);
        });

        const activation = createActivationSelect(project, skill);
        const stateButton = iconButton(
            skill.enabled === true ? 'power' : 'power-off',
            `${skill.enabled === true ? '停用' : '啟用'} Project Skill ${skill.name || slug}`,
            'project-skill-state'
        );
        stateButton.classList.toggle('is-enabled', skill.enabled === true);
        stateButton.setAttribute('aria-pressed', skill.enabled === true ? 'true' : 'false');
        stateButton.disabled = busy || Boolean(project.archived);
        stateButton.addEventListener('click', event => {
            event.stopPropagation();
            void toggleSkill(project, skill);
        });

        const menu = iconButton('ellipsis', `${skill.name || slug} 選單`, 'project-skill-menu');
        menu.addEventListener('click', event => openSkillMenu(event, project, skill));
        row.append(copy);
        if (activation) row.appendChild(activation);
        row.append(stateButton, menu);
        return row;
    }

    function closeAddMenu({ restoreFocus = false } = {}) {
        const button = state.addMenu.button;
        const menu = state.addMenu.menu;
        if (!menu) return;
        menu.hidden = true;
        menu.dataset.view = 'actions';
        menu.setAttribute('role', 'menu');
        menu.removeAttribute('aria-modal');
        menu.removeAttribute('aria-labelledby');
        menu.setAttribute('aria-label', menu.dataset.menuLabel || 'Skill 新增選單');
        if (menu._projectSkillsActions) menu._projectSkillsActions.hidden = false;
        if (menu._projectSkillsGuide) menu._projectSkillsGuide.hidden = true;
        menu.style.left = '';
        menu.style.top = '';
        menu.style.width = '';
        button?.setAttribute('aria-expanded', 'false');
        if (state.addMenu.anchor?.isConnected) state.addMenu.anchor.appendChild(menu);
        state.addMenu.section = null;
        state.addMenu.button = null;
        state.addMenu.menu = null;
        state.addMenu.anchor = null;
        if (restoreFocus && button?.isConnected && !button.disabled) button.focus();
    }

    function addMenuItems(menu) {
        return [...menu.querySelectorAll('[role="menuitem"]')];
    }

    function focusAddMenuItem(menu, position = 'first') {
        const items = addMenuItems(menu);
        const item = position === 'last' ? items.at(-1) : items[0];
        item?.focus();
    }

    function positionAddMenu(button, menu) {
        if (!document.body || typeof button.getBoundingClientRect !== 'function') return;
        document.body.appendChild(menu);
        const viewportWidth = Math.max(0, Number(window.innerWidth) || 0);
        const viewportHeight = Math.max(0, Number(window.innerHeight) || 0);
        const buttonRect = button.getBoundingClientRect();
        const width = Math.min(320, Math.max(0, viewportWidth - 24));
        menu.style.width = `${width}px`;
        const menuRect = menu.getBoundingClientRect();
        const left = Math.max(12, Math.min(
            buttonRect.right - menuRect.width,
            viewportWidth - menuRect.width - 12
        ));
        let top = buttonRect.bottom + 8;
        if (top + menuRect.height > viewportHeight - 12) {
            top = Math.max(12, buttonRect.top - menuRect.height - 8);
        }
        menu.style.left = `${left}px`;
        menu.style.top = `${top}px`;
    }

    function openAddMenu(section, button, menu, { focus = 'first' } = {}) {
        if (button.disabled) return;
        if (state.addMenu.menu && state.addMenu.menu !== menu) closeAddMenu();
        state.addMenu.section = section;
        state.addMenu.button = button;
        state.addMenu.menu = menu;
        state.addMenu.anchor = button.parentElement;
        menu.dataset.view = 'actions';
        menu.hidden = false;
        button.setAttribute('aria-expanded', 'true');
        positionAddMenu(button, menu);
        focusAddMenuItem(menu, focus);
    }

    function createAddMenuOption({ iconName, title, description, comingSoon = false, onSelect }) {
        const option = element(
            'button',
            `project-skills-add-option ${comingSoon ? 'is-coming-soon' : ''}`.trim()
        );
        option.type = 'button';
        option.tabIndex = -1;
        option.setAttribute('role', 'menuitem');
        option.appendChild(element('span', 'project-skills-add-option-icon'));
        option.firstChild.appendChild(icon(iconName));
        const copy = element('span', 'project-skills-add-option-copy');
        copy.append(
            element('span', 'project-skills-add-option-title', title),
            element('span', 'project-skills-add-option-description', description)
        );
        option.appendChild(copy);
        if (comingSoon) {
            option.setAttribute('aria-disabled', 'true');
            option.title = `${title}（即將提供）`;
            option.appendChild(element('span', 'project-skills-add-option-badge', '即將提供'));
            option.addEventListener('click', event => {
                event.preventDefault();
                event.stopPropagation();
                deps?.showToast?.(`${title}功能即將提供。`, 'info');
            });
        } else {
            option.addEventListener('click', event => {
                event.stopPropagation();
                onSelect?.();
            });
        }
        return option;
    }

    function showSkillFormatGuide(menu) {
        if (!menu?._projectSkillsActions || !menu._projectSkillsGuide) return;
        menu._projectSkillsActions.hidden = true;
        menu._projectSkillsGuide.hidden = false;
        menu.dataset.view = 'guide';
        menu.setAttribute('role', 'dialog');
        menu.setAttribute('aria-modal', 'false');
        menu.removeAttribute('aria-label');
        menu.setAttribute('aria-labelledby', menu._projectSkillsGuideTitle.id);
        positionAddMenu(state.addMenu.button, menu);
        menu._projectSkillsGuideBack?.focus();
    }

    function showAddMenuActions(menu, { focusFormat = false } = {}) {
        if (!menu?._projectSkillsActions || !menu._projectSkillsGuide) return;
        menu._projectSkillsGuide.hidden = true;
        menu._projectSkillsActions.hidden = false;
        menu.dataset.view = 'actions';
        menu.setAttribute('role', 'menu');
        menu.removeAttribute('aria-modal');
        menu.removeAttribute('aria-labelledby');
        menu.setAttribute('aria-label', menu.dataset.menuLabel || 'Skill 新增選單');
        positionAddMenu(state.addMenu.button, menu);
        if (focusFormat) menu._projectSkillsFormatOption?.focus();
    }

    function createSkillFormatGuide(menu) {
        const guide = element('div', 'project-skills-format-guide');
        guide.id = `${menu.id}-format-guide`;
        guide.hidden = true;
        guide.setAttribute('role', 'document');
        const head = element('div', 'project-skills-format-guide-head');
        const back = iconButton('arrow-left', '返回新增 Skill 選單', 'project-skills-format-guide-back');
        const title = element('strong', 'project-skills-format-guide-title', 'Skill 資料夾格式');
        title.id = `${menu.id}-format-title`;
        const close = iconButton('x', '關閉 Skill 格式說明', 'project-skills-format-guide-close');
        head.append(back, title, close);
        const tree = element(
            'pre',
            'project-skills-format-tree',
            'my-skill/\n├─ SKILL.md\n├─ references/\n├─ scripts/\n└─ assets/'
        );
        const notes = element('ul', 'project-skills-format-notes');
        [
            'SKILL.md 為必要檔案，說明用途、觸發條件與操作規則。',
            'references、scripts 與 assets 均為選用資料夾。',
            '匯入只會先檢查內容，不會自動執行 scripts。',
        ].forEach(note => notes.appendChild(element('li', '', note)));
        guide.append(
            head,
            element('p', 'project-skills-format-guide-intro', '建議使用以下結構，方便驗證、版本管理與後續匯入。'),
            tree,
            notes
        );
        back.addEventListener('click', event => {
            event.stopPropagation();
            showAddMenuActions(menu, { focusFormat: true });
        });
        close.addEventListener('click', event => {
            event.stopPropagation();
            closeAddMenu({ restoreFocus: true });
        });
        menu._projectSkillsGuide = guide;
        menu._projectSkillsGuideTitle = title;
        menu._projectSkillsGuideBack = back;
        return guide;
    }

    function createAddMenu(section, project, button) {
        const menu = element('div', 'project-skills-add-menu');
        menu.id = `project-skills-add-menu-${++addMenuSequence}`;
        menu.hidden = true;
        menu.setAttribute('role', 'menu');
        menu.dataset.menuLabel = `${project.name || '目前專案'} 的 Skill 新增選單`;
        menu.setAttribute('aria-label', menu.dataset.menuLabel);
        const actions = element('div', 'project-skills-add-actions');
        actions.setAttribute('role', 'none');
        const formatOption = createAddMenuOption({
            iconName: 'circle-help',
            title: '了解 Skill 格式',
            description: '查看標準資料夾結構與必要欄位',
            onSelect: () => showSkillFormatGuide(menu),
        });
        formatOption.setAttribute('aria-haspopup', 'dialog');
        actions.append(
            createAddMenuOption({
                iconName: 'file-plus-2',
                title: '建立空白 Skill',
                description: '從零開始編輯名稱、指令與參考檔',
                onSelect: () => {
                    closeAddMenu({ restoreFocus: true });
                    openCreateEditor(project);
                },
            }),
            createAddMenuOption({
                iconName: 'folder-input',
                title: '從本機資料夾匯入',
                description: '選擇包含 SKILL.md 的本機資料夾',
                comingSoon: true,
            }),
            createAddMenuOption({
                iconName: 'download',
                title: '從 GitHub 匯入',
                description: '貼上 Repository 或 Skill 子資料夾網址',
                comingSoon: true,
            }),
            formatOption
        );
        menu._projectSkillsActions = actions;
        menu._projectSkillsFormatOption = formatOption;
        const guide = createSkillFormatGuide(menu);
        formatOption.setAttribute('aria-controls', guide.id);
        menu.append(actions, guide);
        button.setAttribute('aria-haspopup', 'menu');
        button.setAttribute('aria-expanded', 'false');
        button.setAttribute('aria-controls', menu.id);
        button.addEventListener('click', event => {
            event.stopPropagation();
            if (state.addMenu.menu === menu && !menu.hidden) {
                closeAddMenu({ restoreFocus: true });
                return;
            }
            openAddMenu(section, button, menu);
        });
        button.addEventListener('keydown', event => {
            if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
            event.preventDefault();
            event.stopPropagation();
            openAddMenu(section, button, menu, {
                focus: event.key === 'ArrowUp' ? 'last' : 'first',
            });
        });
        menu.addEventListener('click', event => event.stopPropagation());
        menu.addEventListener('keydown', event => {
            if (menu.dataset.view === 'guide') return;
            const items = addMenuItems(menu);
            const currentIndex = items.indexOf(document.activeElement);
            let target = null;
            if (event.key === 'ArrowDown' && items.length) {
                target = items[(currentIndex + 1 + items.length) % items.length];
            } else if (event.key === 'ArrowUp' && items.length) {
                target = items[(currentIndex - 1 + items.length) % items.length];
            } else if (event.key === 'Home') {
                target = items[0];
            } else if (event.key === 'End') {
                target = items.at(-1);
            } else if (event.key === 'Escape') {
                event.preventDefault();
                event.stopPropagation();
                closeAddMenu({ restoreFocus: true });
                return;
            } else if (event.key === 'Tab') {
                closeAddMenu();
                return;
            } else if (['Enter', ' '].includes(event.key) && currentIndex >= 0) {
                event.preventDefault();
                event.stopPropagation();
                items[currentIndex].click();
                return;
            } else {
                return;
            }
            event.preventDefault();
            event.stopPropagation();
            target?.focus();
        });
        return menu;
    }

    function createEmptySkillsState(project) {
        const empty = element('div', 'project-skills-empty');
        const emptyIcon = element('div', 'project-skills-empty-icon');
        emptyIcon.appendChild(icon('sparkles'));
        const primary = element('button', 'project-skills-empty-primary', '建立第一個 Skill');
        primary.type = 'button';
        primary.addEventListener('click', event => {
            event.stopPropagation();
            openCreateEditor(project);
        });
        const secondary = element(
            'button',
            'project-skills-empty-secondary',
            '從資料夾匯入 · 即將提供'
        );
        secondary.type = 'button';
        secondary.disabled = true;
        secondary.setAttribute('aria-disabled', 'true');
        secondary.title = '從本機資料夾匯入功能即將提供';
        empty.append(
            emptyIcon,
            element('strong', 'project-skills-empty-title', '此專案尚未加入 Skill'),
            element('span', 'project-skills-empty-description', '建立專案專用指令，讓目前對話依需求使用。'),
            primary,
            secondary
        );
        return empty;
    }

    function renderProjectSection(section, project, options = {}) {
        if (state.addMenu.section === section) closeAddMenu();
        section.replaceChildren();
        const record = projectRecord(project.id);
        const alwaysExpanded = options?.alwaysExpanded === true;
        const expanded = alwaysExpanded || state.expandedProjects.has(project.id);
        section.className = `project-skills-section ${expanded ? 'is-expanded' : ''}`.trim();
        section.dataset.projectSkillsProjectId = project.id;
        section._projectSkillsProject = project;
        section._projectSkillsOptions = options;

        const header = element('div', 'project-skills-header');
        const toggle = element(alwaysExpanded ? 'div' : 'button', 'project-skills-toggle');
        if (alwaysExpanded) {
            toggle.setAttribute('aria-label', `${project.name} 的 Project Skills`);
        } else {
            toggle.type = 'button';
            toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
            toggle.setAttribute('aria-label', `${expanded ? '收合' : '展開'} ${project.name} 的 Project Skills`);
        }
        toggle.append(icon('sparkles'), element('span', '', 'Skills'));
        const count = element(
            'span',
            'project-skills-count',
            record.status === 'ready' ? record.items.length : '–'
        );
        toggle.appendChild(count);
        if (!alwaysExpanded) {
            toggle.addEventListener('click', event => {
                event.stopPropagation();
                if (expanded) state.expandedProjects.delete(project.id);
                else state.expandedProjects.add(project.id);
                renderProjectSection(section, project, options);
                deps?.createIcons?.();
                if (!expanded) void loadProject(project.id);
            });
        }
        header.appendChild(toggle);

        const addDisabledReason = !project?.id
            ? '請先選擇專案後再新增 Skill'
            : project.archived ? '封存專案無法新增 Skill' : '';
        const addLabel = addDisabledReason || `新增或匯入 ${project.name} 的 Project Skill`;
        const addWrap = element('div', 'project-skills-add-wrap');
        const add = iconButton('plus', addLabel, 'project-skills-add');
        add.disabled = Boolean(addDisabledReason);
        const addMenu = createAddMenu(section, project, add);
        addWrap.append(add, addMenu);
        header.appendChild(addWrap);
        section.appendChild(header);

        if (!expanded) return;
        const list = element('div', 'project-skills-list');
        list.setAttribute('aria-live', 'polite');
        if (record.status === 'idle' || record.status === 'loading') {
            list.appendChild(inlineState('載入 Project Skills…', 'is-loading'));
        } else if (record.status === 'error') {
            const failure = inlineState(record.error || '無法載入 Project Skills。', 'is-error');
            const retry = element('button', 'project-skills-retry', '重試');
            retry.type = 'button';
            retry.addEventListener('click', event => {
                event.stopPropagation();
                void loadProject(project.id, { force: true });
            });
            failure.appendChild(retry);
            list.appendChild(failure);
        } else if (!record.items.length) {
            list.appendChild(
                project.archived
                    ? inlineState('此專案沒有 Skills。')
                    : createEmptySkillsState(project)
            );
        } else {
            record.items.forEach(skill => list.appendChild(createSkillRow(project, skill)));
        }
        section.appendChild(list);
    }

    function createProjectSection(project, options = {}) {
        if (!project?.id) return null;
        const section = element('section', 'project-skills-section');
        renderProjectSection(section, project, options);
        if (options?.autoLoad === true) void loadProject(project.id);
        return section;
    }

    function normalizeReferencePath(value) {
        const raw = String(value || '');
        const path = raw.trim();
        const parts = path.split('/');
        if (
            !path || raw !== path || path.startsWith('/') || path.includes('\\') || path.includes(':')
            || parts.some(part => !part || part === '.' || part === '..')
            || path.length > 240
        ) throw new Error(`不安全的參考檔路徑：${value}`);
        for (const part of parts) {
            const stem = part.split('.', 1)[0].toLowerCase();
            if (
                part.startsWith('.') || part.endsWith('.') || part.endsWith(' ')
                || part.length > 100 || /[\x00-\x1f]/.test(part)
                || WINDOWS_RESERVED_NAMES.has(stem)
            ) throw new Error(`不安全的參考檔路徑：${value}`);
        }
        const filename = parts.at(-1).toLowerCase();
        const dot = filename.lastIndexOf('.');
        const extension = dot >= 0 ? filename.slice(dot) : '';
        if (!ALLOWED_REFERENCE_EXTENSIONS.has(extension)) {
            throw new Error(`不支援的參考檔類型：${value}`);
        }
        return path;
    }

    function normalizeReferences(value) {
        const references = new Map();
        if (value && typeof value === 'object' && !Array.isArray(value)) {
            Object.entries(value).forEach(([path, content]) => {
                if (typeof content === 'string') references.set(normalizeReferencePath(path), content);
            });
        } else if (Array.isArray(value)) {
            value.forEach(item => {
                const path = item?.path || item?.name;
                const content = item?.content ?? item?.text;
                if (path && typeof content === 'string') references.set(normalizeReferencePath(path), content);
            });
        }
        return references;
    }

    function referenceObject() {
        return Object.fromEntries(
            [...state.editor.references.entries()].sort(([left], [right]) => left.localeCompare(right))
        );
    }

    function setEditorStatus(message, kind = '') {
        const status = byId('project-skill-editor-status');
        if (!status) return;
        status.textContent = message || '';
        status.className = `project-skill-editor-status ${kind}`.trim();
    }

    function setEditorControlsDisabled(disabled) {
        [
            'project-skill-name', 'project-skill-slug', 'project-skill-description',
            'project-skill-version', 'project-skill-instructions', 'project-skill-enabled',
            'project-skill-reference-input', 'project-skill-reference-add', 'project-skill-editor-save',
        ].forEach(id => {
            const control = byId(id);
            if (control) control.disabled = Boolean(disabled);
        });
        document.querySelectorAll('#project-skill-reference-list .project-skill-reference-remove')
            .forEach(button => { button.disabled = Boolean(disabled); });
        if (state.editor.mode === 'edit') byId('project-skill-slug').readOnly = true;
    }

    function setEditorDismissDisabled(disabled) {
        ['project-skill-editor-close', 'project-skill-editor-cancel'].forEach(id => {
            const control = byId(id);
            if (control) control.disabled = Boolean(disabled);
        });
    }

    function resetEditor() {
        state.editor.generation += 1;
        byId('project-skill-editor-form')?.reset();
        if (byId('project-skill-version')) byId('project-skill-version').value = '1.0.0';
        if (byId('project-skill-enabled')) byId('project-skill-enabled').checked = true;
        state.editor.references = new Map();
        state.editor.versions = [];
        state.editor.versionsStatus = 'idle';
        state.editor.versionsError = null;
        state.editor.versionPreview = null;
        state.editor.versionPreviewStatus = 'idle';
        state.editor.versionPreviewError = null;
        state.editor.expectedSha256 = null;
        state.editor.originalEnabled = true;
        state.editor.slugDirty = false;
        state.editor.mode = null;
        renderReferences();
        renderVersions();
        renderVersionPreview();
        setEditorStatus('');
    }

    function editorTitle(text) {
        const title = byId('project-skill-editor-title');
        if (title) title.lastChild.textContent = text;
    }

    function showEditor() {
        const modal = byId('project-skill-editor-modal');
        if (!modal) return;
        modal.hidden = false;
        modal.inert = false;
        modal.classList.add('active');
        modal.setAttribute('aria-hidden', 'false');
        state.editor.open = true;
        deps?.createIcons?.();
        setTimeout(() => byId('project-skill-editor-close')?.focus(), 0);
    }

    function closeEditor({ restoreFocus = true, force = false } = {}) {
        if (state.editor.saving && !force) {
            setEditorStatus('正在儲存，請稍候。', 'is-loading');
            return false;
        }
        const modal = byId('project-skill-editor-modal');
        modal?.classList.remove('active');
        modal?.setAttribute('aria-hidden', 'true');
        if (modal) {
            modal.hidden = true;
            modal.inert = true;
        }
        const focus = state.editor.restoreFocus;
        state.editor.open = false;
        state.editor.generation += 1;
        state.editor.requestId += 1;
        state.editor.versionsRequestId += 1;
        state.editor.versionPreviewRequestId += 1;
        state.editor.project = null;
        state.editor.slug = null;
        state.editor.restoreFocus = null;
        if (restoreFocus && focus?.isConnected) focus.focus();
        return true;
    }

    function slugify(value) {
        return String(value || '')
            .normalize('NFKD')
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, '-')
            .replace(/^-+|-+$/g, '')
            .slice(0, 63);
    }

    function openCreateEditor(project) {
        if (!project?.id || project.archived || state.editor.saving) return;
        resetEditor();
        state.editor.mode = 'create';
        state.editor.project = { ...project };
        state.editor.slug = null;
        state.editor.restoreFocus = document.activeElement;
        editorTitle(`新增 Project Skill · ${project.name}`);
        setEditorControlsDisabled(false);
        byId('project-skill-slug').readOnly = false;
        byId('project-skill-reference-input').disabled = false;
        byId('project-skill-reference-section').classList.remove('is-create-mode');
        byId('project-skill-version-section').hidden = true;
        renderReferences();
        setEditorStatus('可加入多個 UTF-8 文字參考檔，建立時會一起儲存。');
        showEditor();
        setTimeout(() => byId('project-skill-name')?.focus(), 20);
    }

    function applySkillToEditor(skill) {
        byId('project-skill-name').value = skill.name || '';
        byId('project-skill-slug').value = skillSlug(skill);
        byId('project-skill-description').value = skill.description || '';
        byId('project-skill-version').value = skill.version || '1.0.0';
        byId('project-skill-instructions').value = skill.instructions || '';
        byId('project-skill-enabled').checked = skill.enabled === true;
        state.editor.slug = skillSlug(skill);
        state.editor.expectedSha256 = skillSha256(skill);
        state.editor.originalEnabled = skill.enabled === true;
        state.editor.references = normalizeReferences(skill.references);
        renderReferences();
    }

    async function openEditor(project, summary, focusSection = '') {
        if (!project?.id || !summary || state.editor.saving) return;
        resetEditor();
        state.editor.mode = 'edit';
        state.editor.project = { ...project };
        state.editor.slug = skillSlug(summary);
        state.editor.restoreFocus = document.activeElement;
        editorTitle(`${project.archived ? '檢視' : '編輯'} Project Skill · ${summary.name || state.editor.slug}`);
        byId('project-skill-slug').readOnly = true;
        byId('project-skill-reference-section').classList.remove('is-create-mode');
        byId('project-skill-version-section').hidden = false;
        showEditor();
        setEditorControlsDisabled(true);
        setEditorStatus('載入 Skill…', 'is-loading');
        const requestId = ++state.editor.requestId;
        try {
            const payload = await request(
                `/api/projects/${encoded(project.id)}/skills/${encoded(state.editor.slug)}`
            );
            if (requestId !== state.editor.requestId || !state.editor.open) return;
            const skill = payload.skill || payload;
            applySkillToEditor(skill);
            setEditorControlsDisabled(Boolean(project.archived));
            byId('project-skill-slug').readOnly = true;
            setEditorStatus(project.archived ? '封存專案中的 Skill 為唯讀。' : '');
            void loadVersions();
            setTimeout(() => {
                if (focusSection === 'references') {
                    byId('project-skill-reference-section')?.scrollIntoView({ block: 'nearest' });
                    byId('project-skill-reference-add')?.focus();
                } else if (focusSection === 'versions') {
                    byId('project-skill-version-section')?.scrollIntoView({ block: 'nearest' });
                    byId('project-skill-version-refresh')?.focus();
                } else {
                    byId('project-skill-name')?.focus();
                }
            }, 20);
        } catch (error) {
            if (requestId !== state.editor.requestId) return;
            setEditorStatus(`無法載入 Skill：${error.message}`, 'is-error');
            setEditorControlsDisabled(true);
        }
    }

    function renderReferences() {
        const list = byId('project-skill-reference-list');
        if (!list) return;
        list.replaceChildren();
        const entries = [...state.editor.references.entries()].sort(([left], [right]) => left.localeCompare(right));
        if (!entries.length) {
            list.appendChild(inlineState('尚無參考檔。'));
            return;
        }
        entries.forEach(([path, content]) => {
            const row = element('div', 'project-skill-reference-row');
            const copy = element('div', 'project-skill-reference-copy');
            copy.append(
                element('strong', '', path),
                element('small', '', `${new Blob([content]).size.toLocaleString()} bytes · UTF-8 text`)
            );
            const remove = iconButton('x', `移除參考檔 ${path}`, 'project-skill-reference-remove');
            remove.disabled = Boolean(state.editor.project?.archived || state.editor.saving);
            remove.addEventListener('click', () => {
                state.editor.references.delete(path);
                renderReferences();
                setEditorStatus('參考檔變更會在儲存 Skill 時一起送出。');
            });
            row.append(copy, remove);
            list.appendChild(row);
        });
        deps?.createIcons?.();
    }

    async function addReferenceFiles(fileList) {
        if (!['create', 'edit'].includes(state.editor.mode) || state.editor.project?.archived) return;
        const files = [...(fileList || [])];
        if (!files.length) return;
        const generation = state.editor.generation;
        const projectId = state.editor.project?.id;
        const editorSlug = state.editor.slug;
        const decoder = new TextDecoder('utf-8', { fatal: true });
        const encoder = new TextEncoder();
        const failures = [];
        const additions = [];
        for (const file of files) {
            try {
                const path = normalizeReferencePath(file.webkitRelativePath || file.name);
                if (file.size > MAX_REFERENCE_BYTES) {
                    throw new Error(`單一檔案不可超過 ${MAX_REFERENCE_BYTES.toLocaleString()} bytes`);
                }
                const content = decoder.decode(await file.arrayBuffer())
                    .replaceAll('\r\n', '\n').replaceAll('\r', '\n');
                if (
                    generation !== state.editor.generation
                    || projectId !== state.editor.project?.id
                    || editorSlug !== state.editor.slug
                ) return;
                if (content.includes('\0') || encoder.encode(content).length > MAX_REFERENCE_BYTES) {
                    throw new Error('檔案不是安全的 UTF-8 文字，或正規化後超過大小限制');
                }
                additions.push([path, content]);
            } catch (error) {
                failures.push(`${file.name}: ${error.message}`);
            }
        }
        if (
            generation !== state.editor.generation
            || projectId !== state.editor.project?.id
            || editorSlug !== state.editor.slug
        ) return;
        const merged = new Map(state.editor.references);
        const foldedPaths = new Map(
            [...merged.keys()].map(path => [path.toLowerCase(), path])
        );
        let totalBytes = [...merged.values()]
            .reduce((total, content) => total + encoder.encode(content).length, 0);
        additions.forEach(([path, content]) => {
            const folded = path.toLowerCase();
            const existingPath = foldedPaths.get(folded);
            if (existingPath && existingPath !== path) {
                failures.push(`${path}: 路徑只差英文大小寫，會造成碰撞`);
                return;
            }
            const previous = merged.get(path);
            const nextBytes = encoder.encode(content).length;
            const previousBytes = previous === undefined ? 0 : encoder.encode(previous).length;
            if (previous === undefined && merged.size >= MAX_REFERENCE_FILES) {
                failures.push(`${path}: 最多只能加入 ${MAX_REFERENCE_FILES} 個參考檔`);
                return;
            }
            if (totalBytes - previousBytes + nextBytes > MAX_REFERENCE_PACKAGE_BYTES) {
                failures.push(`${path}: 參考檔總大小不可超過 ${MAX_REFERENCE_PACKAGE_BYTES.toLocaleString()} bytes`);
                return;
            }
            merged.set(path, content);
            foldedPaths.set(folded, path);
            totalBytes = totalBytes - previousBytes + nextBytes;
        });
        state.editor.references = merged;
        byId('project-skill-reference-input').value = '';
        renderReferences();
        if (failures.length) {
            setEditorStatus(`部分檔案無法加入：${failures.join('；')}`, 'is-error');
        } else {
            setEditorStatus('參考檔已加入草稿；按「儲存 Skill」後才會生效。');
        }
    }

    function versionItems(payload) {
        if (Array.isArray(payload?.versions)) return payload.versions;
        if (Array.isArray(payload?.history)) return payload.history;
        return Array.isArray(payload) ? payload : [];
    }

    function renderVersionPreview() {
        const panel = byId('project-skill-version-preview');
        if (!panel) return;
        panel.replaceChildren();
        panel.hidden = state.editor.versionPreviewStatus === 'idle';
        if (state.editor.versionPreviewStatus === 'idle') return;
        if (state.editor.versionPreviewStatus === 'loading') {
            panel.appendChild(inlineState('載入版本內容…', 'is-loading'));
            return;
        }
        if (state.editor.versionPreviewStatus === 'error') {
            panel.appendChild(inlineState(
                state.editor.versionPreviewError || '無法載入版本內容。',
                'is-error'
            ));
            return;
        }
        const snapshot = state.editor.versionPreview;
        if (!snapshot) return;
        const head = element('div', 'project-skill-version-preview-head');
        const copy = element('div', 'project-skill-version-preview-copy');
        copy.append(
            element('strong', '', `${snapshot.name || state.editor.slug} · v${snapshot.version || ''}`),
            element('small', '', `SHA-256 ${String(snapshot.sha256 || '').slice(0, 16)}…`)
        );
        const close = iconButton('x', '關閉版本內容', 'project-skill-version-preview-close');
        close.addEventListener('click', () => {
            state.editor.versionPreviewRequestId += 1;
            state.editor.versionPreview = null;
            state.editor.versionPreviewStatus = 'idle';
            state.editor.versionPreviewError = null;
            renderVersionPreview();
        });
        head.append(copy, close);
        const instructions = String(snapshot.instructions || '');
        const previewLimit = 20_000;
        const pre = element(
            'pre',
            'project-skill-version-preview-instructions',
            instructions.slice(0, previewLimit)
        );
        const references = Array.isArray(snapshot.references) ? snapshot.references : [];
        const referencesList = element('ul', 'project-skill-version-preview-references');
        references.forEach(reference => {
            referencesList.appendChild(element(
                'li',
                '',
                `${reference.path || ''} · ${Number(reference.size_bytes || 0).toLocaleString()} bytes`
            ));
        });
        panel.append(
            head,
            element('h5', '', 'Instructions 快照'),
            pre,
            instructions.length > previewLimit
                ? element('small', 'project-skill-version-preview-note', '畫面僅預覽前 20,000 字元；儲存的快照內容不受影響。')
                : document.createTextNode(''),
            element('h5', '', `References 快照（${references.length}）`),
            referencesList
        );
        deps?.createIcons?.();
    }

    async function loadVersionSnapshot(version) {
        const project = state.editor.project;
        const slug = state.editor.slug;
        const digest = skillSha256(version);
        if (!project?.id || !slug || !digest || version.snapshot_available !== true) return;
        const requestId = ++state.editor.versionPreviewRequestId;
        state.editor.versionPreview = null;
        state.editor.versionPreviewStatus = 'loading';
        state.editor.versionPreviewError = null;
        renderVersionPreview();
        try {
            const payload = await request(
                `/api/projects/${encoded(project.id)}/skills/${encoded(slug)}/versions/${encoded(digest)}`
            );
            if (requestId !== state.editor.versionPreviewRequestId || !state.editor.open) return;
            state.editor.versionPreview = payload.snapshot || payload.version || payload;
            state.editor.versionPreviewStatus = 'ready';
        } catch (error) {
            if (requestId !== state.editor.versionPreviewRequestId) return;
            state.editor.versionPreviewStatus = 'error';
            state.editor.versionPreviewError = error.message;
        }
        renderVersionPreview();
    }

    function renderVersions() {
        const list = byId('project-skill-version-list');
        if (!list) return;
        list.replaceChildren();
        if (state.editor.versionsStatus === 'loading') {
            list.appendChild(inlineState('載入版本紀錄…', 'is-loading'));
            return;
        }
        if (state.editor.versionsStatus === 'error') {
            list.appendChild(inlineState(state.editor.versionsError || '無法載入版本紀錄。', 'is-error'));
            return;
        }
        if (!state.editor.versions.length) {
            list.appendChild(inlineState('尚無版本紀錄。'));
            return;
        }
        state.editor.versions.forEach((version, index) => {
            const row = element('div', 'project-skill-version-row');
            const copy = element('div', 'project-skill-version-copy');
            const label = version.version || version.label || version.revision || `版本 ${index + 1}`;
            const timestamp = version.recorded_at || version.created_at || version.updated_at || version.saved_at || '';
            const digest = skillSha256(version);
            copy.append(
                element('strong', '', String(label)),
                element('small', '', [timestamp, digest ? `SHA-256 ${digest.slice(0, 12)}…` : ''].filter(Boolean).join(' · '))
            );
            row.append(copy);
            if (version.snapshot_available === true && digest) {
                const view = iconButton('eye', `查看版本 ${label} 的內容`, 'project-skill-version-view');
                view.addEventListener('click', () => { void loadVersionSnapshot(version); });
                row.appendChild(view);
            } else {
                row.appendChild(element('small', 'project-skill-version-summary-only', '僅有摘要'));
            }
            list.appendChild(row);
        });
        renderVersionPreview();
    }

    async function loadVersions() {
        const project = state.editor.project;
        const slug = state.editor.slug;
        if (!project?.id || !slug || state.editor.mode !== 'edit') return;
        const requestId = ++state.editor.versionsRequestId;
        state.editor.versionsStatus = 'loading';
        state.editor.versionsError = null;
        renderVersions();
        try {
            const payload = await request(
                `/api/projects/${encoded(project.id)}/skills/${encoded(slug)}/versions`
            );
            if (requestId !== state.editor.versionsRequestId || !state.editor.open) return;
            state.editor.versions = versionItems(payload);
            state.editor.versionsStatus = 'ready';
        } catch (error) {
            if (requestId !== state.editor.versionsRequestId) return;
            state.editor.versionsStatus = 'error';
            state.editor.versionsError = error.message;
        }
        renderVersions();
    }

    function editorPayload() {
        return {
            slug: byId('project-skill-slug').value.trim(),
            name: byId('project-skill-name').value.trim(),
            description: byId('project-skill-description').value.trim(),
            version: byId('project-skill-version').value.trim(),
            instructions: byId('project-skill-instructions').value,
            enabled: byId('project-skill-enabled').checked,
        };
    }

    async function saveEditor(event) {
        event?.preventDefault();
        const project = state.editor.project;
        if (!project?.id || project.archived || state.editor.saving) return;
        const draft = editorPayload();
        if (!draft.slug || !draft.name || !draft.version || !draft.instructions.trim()) {
            setEditorStatus('名稱、slug、版本與 Instructions 都是必填欄位。', 'is-error');
            return;
        }
        state.editor.saving = true;
        const generation = state.editor.generation;
        setEditorControlsDisabled(true);
        setEditorDismissDisabled(true);
        setEditorStatus('儲存 Project Skill…', 'is-loading');
        try {
            if (state.editor.mode === 'create') {
                await request(`/api/projects/${encoded(project.id)}/skills`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        slug: draft.slug,
                        name: draft.name,
                        description: draft.description,
                        version: draft.version,
                        instructions: draft.instructions,
                        enabled: draft.enabled,
                        references: referenceObject(),
                    }),
                });
            } else {
                await request(
                    `/api/projects/${encoded(project.id)}/skills/${encoded(state.editor.slug)}`,
                    {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            expected_sha256: state.editor.expectedSha256,
                            name: draft.name,
                            description: draft.description,
                            version: draft.version,
                            instructions: draft.instructions,
                            enabled: draft.enabled,
                            references: referenceObject(),
                        }),
                    }
                );
            }
            if (generation !== state.editor.generation || !state.editor.open) return;
            state.editor.saving = false;
            closeEditor({ restoreFocus: true });
            await Promise.all([
                loadProject(project.id, { force: true }),
                state.session.projectId === project.id
                    ? loadSessionCatalog({ force: true })
                    : Promise.resolve(),
            ]);
            deps.showToast?.(`已儲存 ${draft.name}。`, 'success');
        } catch (error) {
            if (generation !== state.editor.generation || !state.editor.open) return;
            setEditorStatus(`儲存失敗：${error.message}`, 'is-error');
            setEditorControlsDisabled(false);
            byId('project-skill-slug').readOnly = state.editor.mode === 'edit';
        } finally {
            state.editor.saving = false;
            setEditorDismissDisabled(false);
            if (
                generation === state.editor.generation
                && state.editor.open
                && !project.archived
            ) setEditorControlsDisabled(false);
        }
    }

    function initEditorDom() {
        byId('project-skill-editor-close')?.addEventListener('click', () => closeEditor());
        byId('project-skill-editor-cancel')?.addEventListener('click', () => closeEditor());
        byId('project-skill-editor-form')?.addEventListener('submit', saveEditor);
        byId('project-skill-editor-modal')?.addEventListener('click', event => {
            if (event.target === byId('project-skill-editor-modal')) closeEditor();
        });
        byId('project-skill-name')?.addEventListener('input', event => {
            if (state.editor.mode !== 'create' || state.editor.slugDirty) return;
            byId('project-skill-slug').value = slugify(event.target.value);
        });
        byId('project-skill-slug')?.addEventListener('input', () => {
            if (state.editor.mode === 'create') state.editor.slugDirty = true;
        });
        byId('project-skill-reference-input')?.addEventListener('change', event => {
            void addReferenceFiles(event.target.files);
        });
        byId('project-skill-reference-add')?.addEventListener('click', () => {
            byId('project-skill-reference-input')?.click();
        });
        byId('project-skill-version-refresh')?.addEventListener('click', () => {
            void loadVersions();
        });
        document.addEventListener('keydown', event => {
            if (
                event.key !== 'Escape'
                || !byId('project-skill-editor-modal')?.classList.contains('active')
            ) return;
            event.preventDefault();
            event.stopPropagation();
            if (state.editor.saving) {
                setEditorStatus('正在儲存，請稍候。', 'is-loading');
                return;
            }
            closeEditor();
        }, true);
    }

    function initAddMenuDom() {
        document.addEventListener('click', event => {
            const menu = state.addMenu.menu;
            if (
                !menu || menu.hidden
                || menu.contains(event.target)
                || state.addMenu.button?.contains(event.target)
            ) return;
            closeAddMenu();
        });
        document.addEventListener('keydown', event => {
            if (event.key !== 'Escape' || !state.addMenu.menu || state.addMenu.menu.hidden) return;
            event.preventDefault();
            event.stopImmediatePropagation();
            closeAddMenu({ restoreFocus: true });
        }, true);
        window.addEventListener('resize', () => closeAddMenu());
        document.addEventListener('scroll', event => {
            if (state.addMenu.menu?.contains(event.target)) return;
            closeAddMenu();
        }, true);
    }

    function init(options) {
        if (state.initialized) return;
        deps = {
            apiFetch: options?.apiFetch,
            apiBase: options?.apiBase || '',
            showToast: options?.showToast,
            createIcons: options?.createIcons,
            openContextMenu: options?.openContextMenu,
        };
        if (typeof deps.apiFetch !== 'function') {
            throw new Error('Project Skills 需要 apiFetch。');
        }
        state.initialized = true;
        initEditorDom();
        initAddMenuDom();
    }

    window.workbenchProjectSkills = {
        init,
        createProjectSection,
        setSessionContext,
        refreshProject: projectId => loadProject(projectId, { force: true }),
        refreshSession: () => loadSessionCatalog({ force: true }),
    };
})();
