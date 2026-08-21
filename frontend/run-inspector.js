/* Run-scoped inspector for Skills, execution status, approvals, and results. */

(() => {
    'use strict';

    const TAB_ORDER = ['skills', 'execution', 'results'];
    const TAB_TITLES = {
        skills: 'Skills',
        execution: '執行狀態',
        results: '結果與變更',
    };

    const state = {
        initialized: false,
        available: true,
        expanded: true,
        expandedBeforeUnavailable: true,
        activeTab: 'skills',
        contentOwner: 'chat',
        contentGeneration: 0,
        context: { sessionId: null, projectId: null, projectName: '' },
        contextRequestId: 0,
        runRequestId: 0,
        liveRevision: 0,
        run: null,
        usedSkills: { status: 'idle', items: [], error: null },
        execution: { status: 'idle', tasks: [], events: [], agents: [], approvals: [], error: null, retry: null },
        results: { status: 'idle', artifacts: [], changes: [], validations: [], sources: [], attachments: [], vcs: null, error: null },
        workspaceVcs: { status: 'idle', value: null, error: null },
    };

    let deps = null;
    let dom = null;
    const approvalWaiters = new Map();

    const encoded = value => encodeURIComponent(String(value || ''));
    const array = value => Array.isArray(value) ? value : [];

    function interactionKey(kind, ...parts) {
        return [kind, ...parts].map(part => encodeURIComponent(String(part ?? ''))).join(':');
    }

    function markInteraction(node, key, { focus = false } = {}) {
        if (!node || !key) return node;
        node.dataset.inspectorStateKey = key;
        if (focus) node.dataset.inspectorFocusKey = key;
        return node;
    }

    function descendantElements(root) {
        const result = [];
        const visit = node => {
            const children = Array.from(node?.children || node?.childNodes || []);
            children.forEach(child => {
                if (!child || typeof child !== 'object') return;
                result.push(child);
                visit(child);
            });
        };
        visit(root);
        return result;
    }

    function containsNode(root, node) {
        if (!root || !node) return false;
        if (typeof root.contains === 'function') return root.contains(node);
        return root === node || descendantElements(root).includes(node);
    }

    function currentContextKey() {
        return interactionKey('context', state.context.projectId || '', state.context.sessionId || '');
    }

    function captureInteractionState() {
        const snapshot = {
            contextKey: currentContextKey(),
            scrollTop: Object.fromEntries(TAB_ORDER.map(name => [name, Number(dom.panes[name]?.scrollTop || 0)])),
            details: new Map(),
            focusKey: null,
            focusWasInsideRenderedContent: false,
        };
        descendantElements(dom.panel).forEach(node => {
            const stateKey = node.dataset?.inspectorStateKey;
            if (node.tagName !== 'DETAILS' || !stateKey) return;
            const preview = descendantElements(node).find(child => child.tagName === 'PRE');
            const loaded = ['true', 'error'].includes(String(node.dataset.loaded || ''))
                ? String(node.dataset.loaded)
                : null;
            snapshot.details.set(stateKey, {
                open: node.open === true,
                loaded,
                content: loaded ? String(preview?.textContent || '') : null,
            });
        });

        const active = document.activeElement;
        const renderedHosts = [dom.usedSkills, dom.execution, dom.results];
        snapshot.focusWasInsideRenderedContent = renderedHosts.some(host => containsNode(host, active));
        if (snapshot.focusWasInsideRenderedContent) {
            const keyed = descendantElements(dom.panel).find(node => (
                node.dataset?.inspectorFocusKey
                && containsNode(node, active)
            ));
            snapshot.focusKey = keyed?.dataset?.inspectorFocusKey || null;
        }
        return snapshot;
    }

    function resetPaneScroll() {
        TAB_ORDER.forEach(name => {
            if (dom.panes[name]) dom.panes[name].scrollTop = 0;
        });
    }

    function restoreInteractionState(snapshot, { reset = false } = {}) {
        if (reset || !snapshot || snapshot.contextKey !== currentContextKey()) {
            resetPaneScroll();
            if (
                snapshot?.focusWasInsideRenderedContent
                && state.available
                && state.expanded
            ) {
                dom.tabs[state.activeTab]?.focus?.();
            }
            return;
        }

        const descendants = descendantElements(dom.panel);
        descendants.forEach(node => {
            const saved = snapshot.details.get(node.dataset?.inspectorStateKey);
            if (node.tagName !== 'DETAILS' || !saved) return;
            if (saved.loaded) {
                const preview = descendantElements(node).find(child => child.tagName === 'PRE');
                node.dataset.loaded = saved.loaded;
                if (preview) preview.textContent = saved.content || '';
            }
            node.open = saved.open;
        });
        TAB_ORDER.forEach(name => {
            if (dom.panes[name]) dom.panes[name].scrollTop = snapshot.scrollTop[name] || 0;
        });

        if (!snapshot.focusWasInsideRenderedContent || !state.available || !state.expanded) return;
        const focusTarget = descendants.find(node => (
            node.dataset?.inspectorFocusKey === snapshot.focusKey
            && node.hidden !== true
            && node.disabled !== true
            && typeof node.focus === 'function'
        ));
        if (focusTarget) focusTarget.focus();
        else dom.tabs[state.activeTab]?.focus?.();
    }

    function claimContentOwner(owner) {
        const normalized = String(owner || '').trim();
        if (!normalized) throw new Error('Inspector content owner is required.');
        state.contentOwner = normalized;
        state.contentGeneration += 1;
        return Object.freeze({ owner: normalized, generation: state.contentGeneration });
    }

    function contentOwnerMatches(leaseOrOwner, generation = null) {
        const owner = typeof leaseOrOwner === 'object'
            ? String(leaseOrOwner?.owner || '')
            : String(leaseOrOwner || '');
        const expectedGeneration = typeof leaseOrOwner === 'object'
            ? Number(leaseOrOwner?.generation)
            : (generation == null ? null : Number(generation));
        return owner === state.contentOwner
            && (expectedGeneration == null || expectedGeneration === state.contentGeneration);
    }

    function releaseContentOwner(owner = null, fallback = 'chat') {
        if (owner && !contentOwnerMatches(owner)) return false;
        claimContentOwner(fallback);
        return true;
    }

    function getContentOwner() {
        return { owner: state.contentOwner, generation: state.contentGeneration };
    }

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

    function empty(message, kind = '') {
        const node = element('div', `output-panel-empty ${kind}`.trim(), message);
        node.setAttribute('role', kind === 'is-error' ? 'alert' : 'status');
        return node;
    }

    function apiPath(path) {
        return `${deps?.apiBase || ''}${path}`;
    }

    async function request(path, options = {}) {
        const response = await deps.apiFetch(apiPath(path), options);
        let payload = {};
        try { payload = await response.json(); } catch (_error) { payload = {}; }
        if (!response.ok) {
            const detail = payload?.detail || payload || {};
            const message = detail.message || detail.error || detail.code || payload.message || `HTTP ${response.status}`;
            const error = new Error(String(message));
            error.status = response.status;
            throw error;
        }
        return payload;
    }

    function runIdOf(value) {
        return String(value?.run_id || value?.id || '').trim();
    }

    function identityMatches(payload, { requireProject = false } = {}) {
        if (!payload || typeof payload !== 'object' || !state.run) return false;
        if (runIdOf(payload) !== state.run.runId) return false;
        if (String(payload.session_id || '') !== String(state.context.sessionId || '')) return false;
        const expectedProject = String(state.context.projectId || '');
        const actualProject = String(payload.project_id || '');
        if (requireProject || expectedProject || actualProject) return expectedProject === actualProject;
        return true;
    }

    function normalizedIdentity(value = {}) {
        return {
            runId: String(value.runId || value.run_id || '').trim(),
            sessionId: String(value.sessionId || value.session_id || '').trim(),
            projectId: value.projectId ?? value.project_id ?? null,
        };
    }

    function contextMatches(identity) {
        if (!identity?.sessionId || identity.sessionId !== String(state.context.sessionId || '')) return false;
        return String(identity.projectId || '') === String(state.context.projectId || '');
    }

    function eventIdentityMatches(data = {}, source = {}) {
        if (!state.run || !contextMatches(state.run)) return false;
        const supplied = normalizedIdentity(source);
        if (supplied.sessionId && !contextMatches(supplied)) return false;
        if (supplied.runId && supplied.runId !== state.run.runId) return false;
        const eventRunId = String(data.run_id || supplied.runId || '').trim();
        if (!eventRunId || eventRunId !== state.run.runId) return false;
        if (data.session_id && String(data.session_id) !== String(state.run.sessionId || '')) return false;
        if (Object.prototype.hasOwnProperty.call(data, 'project_id')) {
            if (String(data.project_id || '') !== String(state.run.projectId || '')) return false;
        }
        return true;
    }

    function setBadge(name, value, tone = '') {
        const badge = dom.badges[name];
        if (!badge) return;
        const count = Number(value) || 0;
        badge.hidden = count <= 0;
        badge.textContent = count > 99 ? '99+' : String(count);
        badge.className = `output-tab-badge ${tone}`.trim();
    }

    function syncTabs() {
        const visible = state.available && state.expanded;
        if (dom.workspace) dom.workspace.hidden = !state.available;
        dom.panel.hidden = !visible;
        dom.panel.setAttribute('aria-hidden', visible ? 'false' : 'true');
        document.documentElement?.classList.toggle('run-inspector-open', visible);
        TAB_ORDER.forEach(name => {
            const selected = state.activeTab === name;
            const tab = dom.tabs[name];
            const pane = dom.panes[name];
            tab.classList.toggle('active', selected && visible);
            tab.setAttribute('aria-selected', selected ? 'true' : 'false');
            tab.setAttribute('aria-expanded', selected && visible ? 'true' : 'false');
            tab.tabIndex = selected ? 0 : -1;
            pane.hidden = !selected || !visible;
            pane.classList.toggle('active', selected && visible);
        });
        dom.title.textContent = TAB_TITLES[state.activeTab];
    }

    function selectTab(name, { focus = false, toggle = false } = {}) {
        if (!TAB_ORDER.includes(name)) return;
        const previousTab = state.activeTab;
        const focusedInsidePanel = typeof dom.panel.contains === 'function'
            && dom.panel.contains(document.activeElement);
        if (toggle && state.activeTab === name && state.expanded) {
            state.expanded = false;
        } else {
            state.activeTab = name;
            if (state.available) {
                deps?.beforeOpen?.();
                state.expanded = true;
            } else {
                state.expanded = false;
            }
        }
        syncTabs();
        const movedFromFocusedPane = focusedInsidePanel && previousTab !== state.activeTab;
        if (focus || movedFromFocusedPane || (!state.expanded && focusedInsidePanel && state.available)) {
            dom.tabs[name].focus();
        }
    }

    function setAvailable(available, { focusTarget = null } = {}) {
        const focusedInsideWorkspace = typeof dom.workspace?.contains === 'function'
            && dom.workspace.contains(document.activeElement);
        const nextAvailable = available === true;
        const wasAvailable = state.available;
        if (!nextAvailable && wasAvailable) {
            state.expandedBeforeUnavailable = state.expanded;
            state.expanded = false;
        } else if (nextAvailable && !wasAvailable) {
            state.expanded = state.expandedBeforeUnavailable;
        }
        state.available = nextAvailable;
        syncTabs();
        if (!state.available && focusedInsideWorkspace && typeof focusTarget?.focus === 'function') {
            focusTarget.focus();
        }
    }

    function onTabKeydown(event) {
        const current = TAB_ORDER.findIndex(name => dom.tabs[name] === event.currentTarget);
        let next = null;
        if (event.key === 'ArrowDown') next = (current + 1) % TAB_ORDER.length;
        else if (event.key === 'ArrowUp') next = (current - 1 + TAB_ORDER.length) % TAB_ORDER.length;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = TAB_ORDER.length - 1;
        else if (['Enter', ' '].includes(event.key)) {
            event.preventDefault();
            selectTab(TAB_ORDER[current], { toggle: true });
            return;
        } else return;
        event.preventDefault();
        selectTab(TAB_ORDER[next], { focus: true });
    }

    function statusLabel(status) {
        const labels = {
            pending: '等待中', queued: '等待中', in_progress: '處理中', running: '處理中',
            completed: '已完成', complete: '已完成', succeeded: '已完成', failed: '失敗',
            error: '失敗', cancelled: '已停止', denied: '已拒絕', waiting_approval: '等待核准',
        };
        return labels[String(status || '').toLowerCase()] || String(status || '未知');
    }

    function itemStatus(value) {
        return String(value?.status || value?.state || 'pending').toLowerCase();
    }

    function section(title, count = null) {
        const node = element('section', 'run-inspector-section');
        const head = element('div', 'run-inspector-section-head');
        head.appendChild(element('strong', '', title));
        if (count !== null) head.appendChild(element('span', 'run-inspector-section-count', count));
        node.appendChild(head);
        return node;
    }

    function keyValue(label, value, tone = '') {
        const row = element('div', 'run-inspector-kv');
        row.append(element('span', '', label), element('strong', tone, value));
        return row;
    }

    function renderUsedSkills() {
        const host = dom.usedSkills;
        const items = state.usedSkills.items;
        dom.usedSkillsCount.textContent = state.usedSkills.status === 'ready' ? String(items.length) : '–';
        if (state.usedSkills.status === 'loading') return host.replaceChildren(empty('載入本輪 Skill 紀錄…'));
        if (state.usedSkills.status === 'error') return host.replaceChildren(empty(state.usedSkills.error || '無法載入本輪 Skill 紀錄。', 'is-error'));
        if (!state.run) return host.replaceChildren(empty('尚無執行紀錄'));
        if (!items.length) return host.replaceChildren(empty('本輪未使用 Project Skill'));
        const list = element('div', 'run-inspector-list');
        items.forEach(skill => {
            const row = element('article', 'run-inspector-item');
            const top = element('div', 'run-inspector-item-title');
            top.append(
                element('strong', '', skill.name || skill.skill_slug || skill.slug || '未命名 Skill'),
                element('span', 'run-inspector-status is-completed', `v${skill.version || '–'}`)
            );
            const slug = skill.skill_slug || skill.slug || '';
            const trigger = skill.trigger_mode || skill.activation_mode || '未記錄';
            const references = array(skill.references);
            row.append(top, element('div', 'run-inspector-meta', `${slug} · ${trigger}`));
            row.appendChild(element(
                'div',
                'run-inspector-meta',
                `${references.length} 個 references${skill.truncated ? ' · 已截斷' : ''}`
            ));
            list.appendChild(row);
        });
        host.replaceChildren(list);
    }

    function renderApproval(approval, index = 0) {
        const approvalKey = interactionKey(
            'approval',
            approval.approval_id || approval.capability || approval.tool || index
        );
        const card = element('article', 'run-inspector-approval');
        const title = element('div', 'run-inspector-item-title');
        title.append(
            element('strong', '', approval.capability || approval.tool || '系統能力'),
            element('span', 'run-inspector-status is-warning', approval.deciding ? '送出中' : '等待核准')
        );
        const approvalDescription = approval.risk_title
            ? `Agent 準備執行「${approval.operation_label || approval.capability || '受控操作'}」。請先確認下列風險與後果。`
            : (approval.message || approval.summary || '此操作需要你的核准。');
        card.append(title, element('p', 'run-inspector-description', approvalDescription));
        if (approval.risk_title) {
            card.appendChild(element('strong', 'run-inspector-approval-risk-title', approval.risk_title));
        }
        if (approval.target) {
            card.appendChild(element('div', 'run-inspector-meta', `操作目標：${approval.target}`));
        }
        if (approval.input_summary) {
            card.appendChild(element('div', 'run-inspector-meta', `輸入摘要：${approval.input_summary}`));
        }
        if (approval.consequence) {
            card.appendChild(element('div', 'run-inspector-approval-warning', `可能後果：${approval.consequence}`));
        }
        if (approval.data_disclosure) {
            card.appendChild(element('div', 'run-inspector-meta', `資料範圍：${approval.data_disclosure}`));
        }
        if (approval.reversibility) {
            card.appendChild(element('div', 'run-inspector-meta', `能否復原：${approval.reversibility}`));
        }
        if (approval.approval_scope) {
            card.appendChild(element('div', 'run-inspector-meta', `本次授權：${approval.approval_scope}`));
        }
        if (approval.risk && !approval.risk_title) card.appendChild(element('div', 'run-inspector-meta', `風險：${approval.risk}`));
        const actions = element('div', 'run-inspector-actions');
        const reject = element('button', 'run-inspector-button secondary', '拒絕');
        const approve = element('button', 'run-inspector-button primary', '僅允許本次');
        markInteraction(reject, interactionKey(approvalKey, 'reject'), { focus: true });
        markInteraction(approve, interactionKey(approvalKey, 'approve'), { focus: true });
        reject.type = approve.type = 'button';
        reject.disabled = approve.disabled = approval.deciding === true;
        reject.addEventListener('click', () => void decideApproval(approval, false));
        approve.addEventListener('click', () => void decideApproval(approval, true));
        actions.append(reject, approve);
        card.appendChild(actions);
        return card;
    }

    function renderExecution() {
        const host = dom.execution;
        if (state.execution.status === 'loading') return host.replaceChildren(empty('載入執行狀態…'));
        if (!state.run && state.execution.status !== 'error') return host.replaceChildren(empty('尚無執行紀錄'));
        const fragment = document.createDocumentFragment();
        const status = state.run?.status || state.execution.status;
        const overview = section('本輪狀態');
        overview.append(
            keyValue('狀態', statusLabel(status), ['failed', 'error'].includes(status) ? 'is-error' : ''),
            keyValue('Run', state.run?.runId || '尚未建立')
        );
        fragment.appendChild(overview);

        const approvals = state.execution.approvals.filter(item => item.status === 'pending');
        if (approvals.length) {
            const approvalSection = section('等待核准', approvals.length);
            approvals.forEach((item, index) => approvalSection.appendChild(renderApproval(item, index)));
            fragment.appendChild(approvalSection);
        }

        const tasks = state.execution.tasks;
        if (tasks.length) {
            const completed = tasks.filter(task => ['completed', 'complete', 'succeeded'].includes(itemStatus(task))).length;
            const taskSection = section('Agent 正在處理的步驟', `${completed}/${tasks.length}`);
            const list = element('div', 'run-inspector-list');
            tasks.forEach(task => {
                const statusName = itemStatus(task);
                const row = element('div', 'run-inspector-step');
                row.append(
                    icon(['completed', 'complete', 'succeeded'].includes(statusName) ? 'check-circle-2' : statusName === 'failed' ? 'circle-x' : 'loader-circle'),
                    element('span', '', task.title || task.label || task.message || '未命名步驟'),
                    element('span', `run-inspector-status is-${statusName}`, statusLabel(statusName))
                );
                list.appendChild(row);
            });
            taskSection.appendChild(list);
            fragment.appendChild(taskSection);
        }

        if (state.execution.agents.length) {
            const agentSection = section('子代理', state.execution.agents.length);
            const list = element('div', 'run-inspector-list');
            state.execution.agents.forEach(agent => {
                list.appendChild(keyValue(agent.role || agent.name || 'Agent', statusLabel(itemStatus(agent))));
            });
            agentSection.appendChild(list);
            fragment.appendChild(agentSection);
        }

        const events = state.execution.events.slice(-30);
        if (events.length) {
            const eventSection = section('工具與執行紀錄', events.length);
            const list = element('div', 'run-inspector-timeline');
            events.forEach(event => {
                const row = element('div', 'run-inspector-event');
                row.append(
                    element('time', '', event.time || ''),
                    element('span', '', event.label || event.message || event.tool || event.type || '執行事件')
                );
                list.appendChild(row);
            });
            eventSection.appendChild(list);
            fragment.appendChild(eventSection);
        }

        if (state.execution.error) {
            const errorSection = section('錯誤與重試');
            errorSection.appendChild(empty(state.execution.error.message || String(state.execution.error), 'is-error'));
            if (state.execution.retry?.allowed === true && deps.retryRun && state.run?.runId) {
                const retry = element('button', 'run-inspector-button secondary run-inspector-retry', '重新執行本輪');
                markInteraction(retry, interactionKey('retry', state.run.runId), { focus: true });
                retry.type = 'button';
                retry.addEventListener('click', async () => {
                    retry.disabled = true;
                    try { await deps.retryRun(state.run.runId, { model: state.run.model }); }
                    catch (error) { deps.showToast?.(error.message || '無法重新執行本輪。', 'error'); }
                    finally { retry.disabled = false; }
                });
                errorSection.appendChild(retry);
            } else if (state.execution.retry?.reason) {
                errorSection.appendChild(element('p', 'run-inspector-meta', state.execution.retry.reason));
            }
            fragment.appendChild(errorSection);
        }

        if (fragment.childNodes.length === 1 && !tasks.length && !events.length && !approvals.length) {
            fragment.appendChild(empty('本輪沒有工具、子代理或核准事件。'));
        }
        host.replaceChildren(fragment);
        deps.createIcons?.();
    }

    function renderArtifact(artifact, index = 0) {
        const artifactKey = interactionKey(
            'artifact',
            artifact.artifact_id || artifact.id || artifact.path || artifact.filename || index
        );
        const row = element('article', 'run-inspector-item');
        const title = element('div', 'run-inspector-item-title');
        title.append(icon('file'), element('strong', '', artifact.title || artifact.name || artifact.filename || artifact.path || '產出檔案'));
        row.appendChild(title);
        if (artifact.mime_type || artifact.size != null) {
            row.appendChild(element('div', 'run-inspector-meta', [artifact.mime_type, artifact.size == null ? '' : `${artifact.size} bytes`].filter(Boolean).join(' · ')));
        }
        const preview = artifact.preview ?? artifact.content_preview;
        if (typeof preview === 'string' && preview) {
            const details = element('details', 'run-inspector-preview');
            const detailsKey = interactionKey(artifactKey, 'inline-preview');
            const summary = element('summary', '', '安全預覽');
            markInteraction(details, detailsKey);
            markInteraction(summary, interactionKey(detailsKey, 'summary'), { focus: true });
            details.append(summary, element('pre', '', preview.slice(0, 12000)));
            row.appendChild(details);
        }
        array(artifact.files).forEach(file => {
            const details = element('details', 'run-inspector-preview');
            const detailsKey = interactionKey(artifactKey, 'file', file.path || '');
            const summary = element('summary', '', file.path || '預覽檔案');
            const body = element('pre', '', '展開後載入…');
            markInteraction(details, detailsKey);
            markInteraction(summary, interactionKey(detailsKey, 'summary'), { focus: true });
            details.append(summary, body);
            details.addEventListener('toggle', () => {
                if (details.open && !details.dataset.loaded) void loadArtifactPreview(artifact, file, details, body);
            });
            row.appendChild(details);
        });
        return row;
    }

    function normalizeChange(value = {}) {
        const rawPath = String(value.path || value.relative_path || '').replace(/\\/g, '/').trim();
        const unsafe = !rawPath
            || rawPath.startsWith('/')
            || /^[a-zA-Z]:/.test(rawPath)
            || rawPath.split('/').some(part => !part || part === '.' || part === '..');
        if (unsafe) return null;
        return {
            ...value,
            path: rawPath,
            action: value.action || value.change_type || value.status || 'modified',
        };
    }

    async function loadArtifactPreview(artifact, file, details, body) {
        details.dataset.loaded = 'loading';
        try {
            const payload = await request(
                `/api/runs/${encoded(state.run?.runId)}/artifacts/${encoded(artifact.artifact_id || artifact.id)}/preview?path=${encoded(file.path)}`
            );
            body.textContent = String(payload.preview || payload.content || '此檔案沒有可顯示的預覽。').slice(0, 64000);
            details.dataset.loaded = 'true';
        } catch (error) {
            body.textContent = `無法載入預覽：${error.message}`;
            details.dataset.loaded = 'error';
        }
    }

    function renderChange(change, index = 0, scope = 'run') {
        change = normalizeChange(change);
        if (!change) return empty('此變更沒有安全的相對路徑。', 'is-error');
        const row = element('article', 'run-inspector-item run-inspector-change');
        const top = element('div', 'run-inspector-item-title');
        top.append(
            icon('file-diff'),
            element('strong', '', change.path || change.name || '未命名變更'),
            element('span', 'run-inspector-status', change.status || change.action || change.change_type || 'modified')
        );
        row.appendChild(top);
        if (change.additions != null || change.deletions != null) {
            row.appendChild(element('div', 'run-inspector-meta', `+${change.additions || 0} / −${change.deletions || 0}`));
        }
        if (change.path && state.context.projectId) {
            const details = element('details', 'run-inspector-preview');
            const detailsKey = interactionKey('change', scope, change.path || index);
            const summary = element('summary', '', '查看 Diff');
            const body = element('pre', '', '展開後載入…');
            markInteraction(details, detailsKey);
            markInteraction(summary, interactionKey(detailsKey, 'summary'), { focus: true });
            details.append(summary, body);
            details.addEventListener('toggle', () => {
                if (details.open && !details.dataset.loaded) void loadDiff(change.path, details, body);
            });
            row.appendChild(details);
        }
        return row;
    }

    async function loadDiff(path, details, body) {
        details.dataset.loaded = 'loading';
        try {
            const payload = await request(`/api/projects/${encoded(state.context.projectId)}/vcs/diff?path=${encoded(path)}`);
            body.textContent = String(payload.diff || payload.patch || '此檔案沒有可顯示的 Diff。').slice(0, 40000);
            details.dataset.loaded = 'true';
        } catch (error) {
            body.textContent = error.status === 404 ? 'Diff 端點尚未提供。' : `無法載入 Diff：${error.message}`;
            details.dataset.loaded = 'error';
        }
    }

    function renderResults() {
        const host = dom.results;
        if (state.results.status === 'loading') return host.replaceChildren(empty('載入結果與變更…'));
        if (!state.run && state.results.status !== 'error') return host.replaceChildren(empty('尚無結果'));
        const fragment = document.createDocumentFragment();
        const artifacts = state.results.artifacts;
        const artifactSection = section('生成的檔案與預覽', artifacts.length);
        if (artifacts.length) artifacts.forEach((item, index) => artifactSection.appendChild(renderArtifact(item, index)));
        else artifactSection.appendChild(empty('本輪沒有可驗證的生成檔案。'));
        fragment.appendChild(artifactSection);

        const changes = state.results.changes;
        const changeSection = section('本輪修改與 Diff', changes.length);
        if (changes.length) changes.forEach((item, index) => changeSection.appendChild(renderChange(item, index, 'run')));
        else changeSection.appendChild(empty('本輪沒有可歸屬的檔案變更。'));
        fragment.appendChild(changeSection);

        const validations = state.results.validations;
        const validationSection = section('測試結果', validations.length);
        if (validations.length) {
            validations.forEach(item => {
                const status = itemStatus(item);
                validationSection.appendChild(keyValue(
                    item.name || item.label || item.details || '驗證',
                    item.passed === true ? '通過' : item.passed === false ? '未通過' : statusLabel(status),
                    item.passed === false || status === 'failed' ? 'is-error' : ''
                ));
            });
        } else validationSection.appendChild(empty('本輪沒有結構化測試紀錄。'));
        fragment.appendChild(validationSection);

        const vcsSection = section('Git 狀態');
        const runVcs = state.results.vcs?.run_evidence || state.results.vcs;
        if (runVcs) {
            const commits = array(runVcs.commits);
            const pushes = array(runVcs.pushes);
            const lastCommit = commits.at(-1);
            const lastPush = pushes.at(-1);
            const evidenceSucceeded = item => item?.success === true
                || ['success', 'completed', 'pushed'].includes(String(item?.status || '').toLowerCase());
            const evidenceFailed = item => item?.success === false
                || ['failed', 'error'].includes(String(item?.status || '').toLowerCase());
            const committed = runVcs.committed_this_run === true || commits.some(evidenceSucceeded);
            const pushed = runVcs.pushed_this_run === true || pushes.some(evidenceSucceeded);
            const commitLabel = committed
                ? (lastCommit?.commit || lastCommit?.commit_sha || lastCommit?.short_sha || runVcs.commit || runVcs.commit_sha || '已完成')
                : (lastCommit && evidenceFailed(lastCommit) ? '失敗' : '未執行');
            const pushLabel = pushed
                ? '已推送'
                : (lastPush && evidenceFailed(lastPush) ? '失敗' : (runVcs.push_status || '未執行'));
            vcsSection.append(
                keyValue('本輪 Commit', commitLabel, commitLabel === '失敗' ? 'is-error' : ''),
                keyValue('本輪 Push', pushLabel, pushLabel === '失敗' ? 'is-error' : '')
            );
        } else vcsSection.appendChild(empty('本輪沒有可信的 Git 操作紀錄。'));
        if (state.workspaceVcs.status === 'ready' && state.workspaceVcs.value) {
            const vcs = state.workspaceVcs.value;
            const workspace = element('div', 'run-inspector-subsection');
            workspace.append(
                element('strong', '', '目前工作區（可能包含外部變更）'),
                keyValue('分支', vcs.branch || '未偵測'),
                keyValue('HEAD', vcs.head || vcs.commit || '未偵測'),
                keyValue('工作區', vcs.dirty ? '有未提交變更' : '乾淨'),
                keyValue(
                    '同步',
                    vcs.ahead == null || vcs.behind == null
                        ? (vcs.upstream ? '狀態未知' : '未設定 upstream')
                        : `ahead ${vcs.ahead} · behind ${vcs.behind}`
                )
            );
            const workspaceChanges = array(vcs.changes);
            if (workspaceChanges.length) {
                const label = element('div', 'run-inspector-meta', `目前工作區變更 · ${workspaceChanges.length}`);
                workspace.appendChild(label);
                workspaceChanges.forEach((change, index) => workspace.appendChild(renderChange(change, index, 'workspace')));
            }
            vcsSection.appendChild(workspace);
        } else if (state.workspaceVcs.status === 'error') {
            vcsSection.appendChild(empty(state.workspaceVcs.error || '無法讀取目前工作區 Git 狀態。', 'is-error'));
        }
        fragment.appendChild(vcsSection);

        const evidence = [...state.results.sources, ...state.results.attachments];
        const sourceSection = section('本輪實際使用的來源與附件', evidence.length);
        if (evidence.length) {
            evidence.forEach(item => sourceSection.appendChild(keyValue(
                item.name || item.filename || item.title || item.source || '來源',
                item.kind || item.type || (item.filename ? '附件' : '來源')
            )));
        } else sourceSection.appendChild(empty('本輪沒有可驗證的來源或附件紀錄。'));
        fragment.appendChild(sourceSection);

        if (state.results.error) fragment.appendChild(empty(state.results.error, 'is-error'));
        host.replaceChildren(fragment);
        deps.createIcons?.();
    }

    function renderAll({ resetInteraction = false } = {}) {
        const interaction = captureInteractionState();
        dom.project.textContent = state.context.projectName || (state.context.projectId ? '目前專案' : '尚未選擇專案');
        renderUsedSkills();
        renderExecution();
        renderResults();
        const pending = state.execution.approvals.filter(item => item.status === 'pending').length;
        const running = state.execution.tasks.filter(item => ['pending', 'queued', 'in_progress', 'running'].includes(itemStatus(item))).length;
        setBadge('skills', state.usedSkills.items.length);
        setBadge('execution', pending || running, pending ? 'is-warning' : '');
        setBadge('results', state.results.artifacts.length + state.results.changes.length);
        restoreInteractionState(interaction, { reset: resetInteraction });
    }

    function resetRunState() {
        state.run = null;
        state.liveRevision = 0;
        state.usedSkills = { status: 'idle', items: [], error: null };
        state.execution = { status: 'idle', tasks: [], events: [], agents: [], approvals: [], error: null, retry: null };
        state.results = { status: 'idle', artifacts: [], changes: [], validations: [], sources: [], attachments: [], vcs: null, error: null };
    }

    async function setContext(context = {}) {
        const sessionId = context.sessionId || null;
        const projectId = context.projectId || null;
        const changed = sessionId !== state.context.sessionId || projectId !== state.context.projectId;
        state.context = { sessionId, projectId, projectName: context.projectName || '' };
        if (!changed) {
            renderAll();
            return;
        }
        claimContentOwner('chat');
        cancelPendingApprovals('對話或專案已切換。');
        const requestId = ++state.contextRequestId;
        ++state.runRequestId;
        resetRunState();
        state.workspaceVcs = { status: projectId ? 'loading' : 'idle', value: null, error: null };
        renderAll({ resetInteraction: true });
        const jobs = [];
        if (sessionId) jobs.push(hydrateLatestRun(requestId, sessionId));
        if (projectId) jobs.push(hydrateWorkspaceVcs(requestId, projectId));
        await Promise.allSettled(jobs);
    }

    async function hydrateLatestRun(contextRequestId, sessionId) {
        const expectedRunRequestId = state.runRequestId;
        try {
            const payload = await request(`/api/sessions/${encoded(sessionId)}/runs?limit=1`);
            if (
                contextRequestId !== state.contextRequestId
                || expectedRunRequestId !== state.runRequestId
                || sessionId !== state.context.sessionId
                || state.run
            ) return;
            const latest = array(payload.runs)[0];
            if (!latest) return renderAll();
            if (String(latest.session_id || '') !== String(state.context.sessionId || '')) return;
            const expectedProject = String(state.context.projectId || '');
            if (String(latest.project_id || '') !== expectedProject) return;
            state.run = {
                runId: runIdOf(latest),
                sessionId: latest.session_id,
                projectId: latest.project_id || null,
                status: latest.status || 'completed',
                model: latest.model || '',
                retryOfRunId: latest.retry_of_run_id || null,
            };
            await hydrateRun(state.run.runId);
        } catch (error) {
            if (
                contextRequestId !== state.contextRequestId
                || expectedRunRequestId !== state.runRequestId
                || state.run
            ) return;
            state.execution = { ...state.execution, status: 'error', error: { message: `無法載入最近一輪：${error.message}` } };
            renderAll();
        }
    }

    async function hydrateWorkspaceVcs(contextRequestId, projectId) {
        try {
            const payload = await request(`/api/projects/${encoded(projectId)}/vcs/status`);
            if (contextRequestId !== state.contextRequestId || projectId !== state.context.projectId) return;
            if (String(payload.project_id || '') !== String(projectId)) throw new Error('Git 狀態的專案識別不一致。');
            state.workspaceVcs = { status: 'ready', value: payload.vcs || null, error: null };
        } catch (error) {
            if (contextRequestId !== state.contextRequestId) return;
            state.workspaceVcs = { status: 'error', value: null, error: error.message };
        }
        renderAll();
    }

    function mergeByIdentity(snapshotItems, liveItems, identity) {
        const merged = new Map();
        array(snapshotItems).forEach((item, index) => merged.set(identity(item, index), { ...item }));
        array(liveItems).forEach((item, index) => {
            const key = identity(item, index);
            merged.set(key, { ...(merged.get(key) || {}), ...item });
        });
        return [...merged.values()];
    }

    function applyExecutionSnapshot(payload, { preserveLive = false } = {}) {
        if (!identityMatches(payload)) throw new Error('執行狀態不屬於目前的 Project／Session／Run。');
        const liveTasks = preserveLive ? state.execution.tasks : [];
        const liveEvents = preserveLive ? state.execution.events : [];
        const liveAgents = preserveLive ? state.execution.agents : [];
        const liveApprovals = preserveLive ? state.execution.approvals : [];
        state.execution.status = payload.status || 'ready';
        state.execution.tasks = mergeByIdentity(payload.tasks, liveTasks, (item, index) => String(item?.id || `task-${index}`));
        const rawEvents = array(payload.events);
        state.execution.events = rawEvents.map(normalizeEvent);
        if (preserveLive) {
            state.execution.events = mergeByIdentity(
                state.execution.events,
                liveEvents,
                (item, index) => String(item?.correlationId || `${item?.type || 'event'}:${item?.time || ''}:${item?.label || ''}:${index}`)
            );
        }
        state.execution.agents = mergeByIdentity(payload.agents, liveAgents, (item, index) => String(item?.id || item?.agent_id || item?.role || `agent-${index}`));
        state.execution.approvals = mergeByIdentity(payload.approvals, liveApprovals, (item, index) => String(item?.approval_id || `approval-${index}`));
        rawEvents.forEach(raw => {
            const type = raw?.event || raw?.type || '';
            const value = raw?.payload && typeof raw.payload === 'object' ? raw.payload : raw;
            if (type === 'approval_required') {
                const approvalId = String(value.approval_id || '');
                if (!state.execution.approvals.some(item => String(item.approval_id) === approvalId)) {
                    state.execution.approvals.push({ ...value, status: value.status || 'pending' });
                }
            } else if (type === 'agent_spawned') upsertAgent(value, value.status || 'running');
            else if (type === 'agent_completed') upsertAgent(value, 'completed');
            else if (type === 'agent_failed') upsertAgent(value, 'failed');
        });
        state.execution.error = payload.error || null;
        state.execution.retry = payload.retry || null;
        if (state.run) state.run.status = payload.status || state.run.status;
    }

    function applyResultsSnapshot(payload) {
        if (!identityMatches(payload)) throw new Error('結果不屬於目前的 Project／Session／Run。');
        const workspace = payload.vcs?.workspace;
        if (workspace && state.workspaceVcs.status !== 'ready') {
            state.workspaceVcs = { status: 'ready', value: workspace, error: null };
        }
        state.results = {
            status: 'ready',
            artifacts: array(payload.artifacts),
            changes: array(payload.changes).map(normalizeChange).filter(Boolean),
            validations: array(payload.validations),
            sources: array(payload.sources),
            attachments: array(payload.attachments),
            vcs: payload.vcs || null,
            error: null,
        };
    }

    function applySkillsSnapshot(payload) {
        if (!identityMatches(payload, { requireProject: true })) {
            throw new Error('Skill 紀錄不屬於目前的 Project／Session／Run。');
        }
        state.usedSkills = { status: 'ready', items: array(payload.skills), error: null };
    }

    async function hydrateRun(runId) {
        if (!runId || !state.run || runId !== state.run.runId) return;
        const requestId = ++state.runRequestId;
        state.usedSkills = { status: 'loading', items: [], error: null };
        state.execution = { ...state.execution, status: 'loading', error: null };
        state.results = { ...state.results, status: 'loading', error: null };
        renderAll();
        const endpoints = [
            ['execution', `/api/runs/${encoded(runId)}/execution`],
            ['results', `/api/runs/${encoded(runId)}/results`],
            ['skills', `/api/runs/${encoded(runId)}/skills`],
        ];
        const settled = await Promise.allSettled(endpoints.map(([, path]) => request(path)));
        if (requestId !== state.runRequestId || !state.run || state.run.runId !== runId) return;
        settled.forEach((outcome, index) => {
            const kind = endpoints[index][0];
            try {
                if (outcome.status === 'rejected') throw outcome.reason;
                if (kind === 'execution') applyExecutionSnapshot(outcome.value);
                else if (kind === 'results') applyResultsSnapshot(outcome.value);
                else applySkillsSnapshot(outcome.value);
            } catch (error) {
                if (kind === 'execution') state.execution = { ...state.execution, status: 'error', error: { message: error.message } };
                else if (kind === 'results') state.results = { ...state.results, status: 'error', error: error.message };
                else state.usedSkills = { status: 'error', items: [], error: error.message };
            }
        });
        renderAll();
    }

    async function hydrateExecution(runId) {
        if (!runId || !state.run || runId !== state.run.runId || !contextMatches(state.run)) return;
        const requestId = state.runRequestId;
        const revisionAtRequest = state.liveRevision;
        try {
            const payload = await request(`/api/runs/${encoded(runId)}/execution`);
            if (
                requestId !== state.runRequestId
                || !state.run
                || state.run.runId !== runId
                || !contextMatches(state.run)
            ) return;
            applyExecutionSnapshot(payload, { preserveLive: state.liveRevision !== revisionAtRequest });
            renderAll();
        } catch (error) {
            if (requestId !== state.runRequestId || !state.run || state.run.runId !== runId) return;
            state.execution = { ...state.execution, status: 'error', error: { message: error.message } };
            renderAll();
        }
    }

    async function hydrateSkills(runId) {
        if (!runId || !state.run || runId !== state.run.runId || !contextMatches(state.run)) return;
        const requestId = state.runRequestId;
        state.usedSkills = { status: 'loading', items: [], error: null };
        renderUsedSkills();
        try {
            const payload = await request(`/api/runs/${encoded(runId)}/skills`);
            if (
                requestId !== state.runRequestId
                || !state.run
                || state.run.runId !== runId
                || !contextMatches(state.run)
            ) return;
            applySkillsSnapshot(payload);
        } catch (error) {
            if (requestId !== state.runRequestId || !state.run || state.run.runId !== runId) return;
            state.usedSkills = { status: 'error', items: [], error: error.message };
        }
        renderAll();
    }

    function beginRun(run = {}) {
        const identity = normalizedIdentity(run);
        if (!identity.runId || !contextMatches(identity)) return false;
        cancelPendingApprovals('新的執行已開始。');
        ++state.runRequestId;
        state.run = {
            runId: identity.runId,
            sessionId: identity.sessionId,
            projectId: identity.projectId,
            status: 'running',
            model: run.model || '',
            retryOfRunId: run.retryOfRunId || run.retry_of_run_id || null,
        };
        state.liveRevision = 0;
        state.usedSkills = { status: 'loading', items: [], error: null };
        state.execution = { status: 'running', tasks: [], events: [], agents: [], approvals: [], error: null, retry: null };
        state.results = { status: 'running', artifacts: [], changes: [], validations: [], sources: [], attachments: [], vcs: null, error: null };
        renderAll();
        return true;
    }

    function normalizeEvent(event) {
        const type = event?.type || event?.kind || 'progress';
        const payload = event?.payload && typeof event.payload === 'object' ? event.payload : event;
        const eventType = event?.event || type;
        return {
            type: eventType,
            tool: payload?.tool || payload?.name || '',
            label: payload?.label || payload?.message || payload?.details || (payload?.tool ? `工具：${payload.tool}` : eventType),
            time: event?.time || event?.created_at || payload?.created_at || new Date().toTimeString().slice(0, 5),
            status: payload?.status || '',
            correlationId: payload?.correlation_id || payload?.tool_call_id || '',
        };
    }

    function upsertAgent(data, status) {
        const id = String(data.agent_id || data.id || data.role || `agent-${state.execution.agents.length + 1}`);
        const current = state.execution.agents.find(item => String(item.id || item.agent_id || item.role) === id);
        if (current) Object.assign(current, { ...data, status });
        else state.execution.agents.push({ ...data, id, status });
    }

    function handleEvent(type, data = {}, source = {}) {
        if (!state.initialized || type === 'token') return false;
        const supplied = normalizedIdentity(source);
        if (!state.run && type === 'meta') {
            const identity = {
                runId: String(data.run_id || supplied.runId || '').trim(),
                sessionId: String(data.session_id || supplied.sessionId || '').trim(),
                projectId: Object.prototype.hasOwnProperty.call(data, 'project_id') ? data.project_id : supplied.projectId,
            };
            if (!beginRun(identity)) return false;
        }
        if (!eventIdentityMatches(data, source)) return false;
        state.liveRevision += 1;
        if (type === 'meta') {
            if (data.model) state.run.model = String(data.model);
            void hydrateExecution(state.run.runId);
            void hydrateSkills(state.run.runId);
        } else if (type === 'skills') {
            void hydrateSkills(state.run.runId);
        } else if (type === 'plan') {
            state.execution.tasks = array(data.tasks);
        } else if (type === 'task_update') {
            const task = state.execution.tasks.find(item => String(item.id) === String(data.task_id));
            if (task) Object.assign(task, { status: data.status || task.status, message: data.message || task.message });
        } else if (type === 'agent_spawned') upsertAgent(data, data.status || 'running');
        else if (type === 'agent_completed') upsertAgent(data, 'completed');
        else if (type === 'agent_failed') upsertAgent(data, 'failed');
        else if (type === 'approval_required') {
            const approvalId = String(data.approval_id || '');
            let added = false;
            if (!state.execution.approvals.some(item => String(item.approval_id) === approvalId)) {
                state.execution.approvals.push({ ...data, approval_id: approvalId, status: 'pending' });
                added = true;
            }
            if (added && !state.available) {
                deps.showToast?.(
                    '此執行正在等待批准。請回到「聊天」或「流程」，再開啟右側「執行」處理。',
                    'warning'
                );
            }
            selectTab('execution');
        } else if (type === 'approval_decided') {
            const approval = state.execution.approvals.find(item => String(item.approval_id) === String(data.approval_id));
            if (approval) Object.assign(approval, { status: data.approved ? 'approved' : 'denied', deciding: false });
        } else if (type === 'validation') {
            state.results.validations.push({ ...data, status: data.passed ? 'completed' : 'failed' });
        } else if (type === 'context' || type === 'sources') {
            state.results.sources = array(data.sources);
        } else if (type === 'artifact' || type === 'artifact_created') {
            state.results.artifacts.push(data.artifact || data);
        } else if (type === 'file_change' || type === 'change') {
            const change = normalizeChange(data.change || data);
            if (change) state.results.changes.push(change);
        } else if (type === 'error' || type === 'deadline_exceeded' || type === 'tool_denied') {
            state.execution.status = 'error';
            state.execution.error = { message: data.message || data.content || '執行失敗' };
            state.execution.retry = { allowed: false };
            if (state.run) state.run.status = 'failed';
            selectTab('execution');
        } else if (type === 'cancelled') {
            state.execution.status = 'cancelled';
            if (state.run) state.run.status = 'cancelled';
        } else if (type === 'done' || type === 'final') {
            state.execution.status = type === 'done' ? 'completed' : state.execution.status;
            if (state.run && type === 'done') state.run.status = 'completed';
        }
        if (['tool_start', 'tool_end', 'progress', 'commentary', 'phase', 'repair', 'tool_denied', 'deadline_exceeded', 'validation', 'agent_message'].includes(type)) {
            state.execution.events.push(normalizeEvent({ ...data, type }));
        }
        renderAll();
        if (['done', 'error', 'cancelled', 'deadline_exceeded', 'tool_denied'].includes(type) && state.run?.runId) {
            void hydrateRun(state.run.runId);
        }
        return true;
    }

    async function decideApproval(approval, approved) {
        if (approval.deciding || approval.status !== 'pending') return;
        approval.deciding = true;
        renderAll();
        try {
            const runId = approval.run_id || state.run?.runId;
            await request(`/api/chat/runs/${encoded(runId)}/approval`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ approval_id: approval.approval_id, approved, decided_by: 'local_user' }),
            });
            approval.status = approved ? 'approved' : 'denied';
            approval.deciding = false;
            settleApprovalWaiter(String(approval.approval_id), 'resolve', approved);
            deps.showToast?.(approved ? '已核准本次能力。' : '已拒絕本次能力。', approved ? 'success' : 'info');
        } catch (error) {
            approval.deciding = false;
            settleApprovalWaiter(String(approval.approval_id), 'reject', error);
            deps.showToast?.(error.message || '核准決定無法送達。', 'error');
        }
        renderAll();
    }

    function settleApprovalWaiter(approvalId, action, value) {
        const waiter = approvalWaiters.get(String(approvalId || ''));
        if (!waiter) return false;
        approvalWaiters.delete(String(approvalId || ''));
        if (waiter.signal && waiter.abortHandler) {
            waiter.signal.removeEventListener('abort', waiter.abortHandler);
        }
        waiter[action](value);
        return true;
    }

    function cancelPendingApprovals(reason = '核准等待已取消。') {
        const error = new DOMException(reason, 'AbortError');
        [...approvalWaiters.keys()].forEach(approvalId => {
            settleApprovalWaiter(approvalId, 'reject', error);
        });
    }

    function handleApproval(data, source = {}, signal = null) {
        const approvalId = String(data?.approval_id || '').trim();
        if (!approvalId || !handleEvent('approval_required', data, source)) {
            return Promise.reject(new DOMException('核准要求已不屬於目前執行。', 'AbortError'));
        }
        settleApprovalWaiter(
            approvalId,
            'reject',
            new DOMException('同一核准要求已被新的事件取代。', 'AbortError')
        );
        return new Promise((resolve, reject) => {
            const abortHandler = () => settleApprovalWaiter(
                approvalId,
                'reject',
                new DOMException('核准等待已取消。', 'AbortError')
            );
            if (signal?.aborted) {
                reject(new DOMException('核准等待已取消。', 'AbortError'));
                return;
            }
            approvalWaiters.set(approvalId, { resolve, reject, signal, abortHandler });
            signal?.addEventListener('abort', abortHandler, { once: true });
        });
    }

    function markError(error, { retryAllowed = false } = {}) {
        state.execution.status = 'error';
        state.execution.error = { message: error?.message || String(error || '執行失敗') };
        state.execution.retry = { allowed: retryAllowed === true };
        if (state.run) state.run.status = 'failed';
        selectTab('execution');
        renderAll();
    }

    function init(options = {}) {
        if (state.initialized) return;
        deps = {
            apiFetch: options.apiFetch,
            apiBase: options.apiBase || '',
            createIcons: options.createIcons,
            showToast: options.showToast,
            retryRun: options.retryRun,
            beforeOpen: options.beforeOpen,
        };
        if (typeof deps.apiFetch !== 'function') throw new Error('Run Inspector 需要 apiFetch。');
        dom = {
            workspace: document.getElementById('output-floating-workspace'),
            panel: document.getElementById('output-floating-panel'),
            title: document.getElementById('output-panel-title'),
            project: document.getElementById('output-panel-project'),
            usedSkills: document.getElementById('run-skills-used'),
            usedSkillsCount: document.getElementById('run-skills-used-count'),
            execution: document.getElementById('run-execution-content'),
            results: document.getElementById('run-results-content'),
            tabs: Object.fromEntries(TAB_ORDER.map(name => [name, document.getElementById(`output-tab-${name}`)])),
            panes: Object.fromEntries(TAB_ORDER.map(name => [name, document.getElementById(`output-pane-${name}`)])),
            badges: Object.fromEntries(TAB_ORDER.map(name => [name, document.getElementById(`output-tab-${name}-badge`)])),
        };
        if (!dom.panel || TAB_ORDER.some(name => !dom.tabs[name] || !dom.panes[name])) {
            throw new Error('Run Inspector DOM 不完整。');
        }
        TAB_ORDER.forEach(name => {
            dom.tabs[name].addEventListener('click', () => selectTab(name, { toggle: true }));
            dom.tabs[name].addEventListener('keydown', onTabKeydown);
        });
        state.initialized = true;
        if (state.expanded && window.matchMedia?.('(max-width: 900px)').matches) {
            deps.beforeOpen?.();
        }
        syncTabs();
        renderAll();
        deps.createIcons?.();
    }

    window.workbenchRunInspector = {
        init,
        setContext,
        beginRun,
        handleEvent,
        handleApproval,
        cancelPendingApprovals,
        hydrateRun,
        hydrateExecution,
        hydrateSkills,
        markError,
        selectTab,
        setAvailable,
        claimContentOwner,
        contentOwnerMatches,
        releaseContentOwner,
        getContentOwner,
        isOpen: () => state.initialized && state.available && state.expanded,
        getState: () => state,
    };
})();
