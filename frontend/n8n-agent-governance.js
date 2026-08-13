/* Project-scoped, audited Agent administration for the managed n8n instance. */
(() => {
    'use strict';

    const state = {
        initialized: false, deps: {}, dom: {}, policy: null, operations: [], workflows: [], audits: [],
        plan: null, planMessages: [], planScope: '', planBusy: false,
        requestId: 0, refreshTimer: null,
    };
    const labels = {
        off: '停用', restricted: '限制權限', full_audit: '完整管理／完全審核',
        pending: '等待核准', pending_second_approval: '等待第二次核准', approved: '已核准',
        executing: '執行中', completed: '已完成', rejected: '已拒絕', revoked: '已撤銷',
        failed: '失敗', execution_unknown: '執行結果不明', expired: '已過期',
    };

    const api = async (path, options = {}) => {
        const response = await state.deps.apiFetch(`${state.deps.apiBase || ''}${path}`, options);
        if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            throw new Error(payload?.detail?.message || payload?.detail?.error?.message || `請求失敗（${response.status}）`);
        }
        return response.status === 204 ? {} : response.json();
    };
    const node = (tag, className, text) => {
        const value = document.createElement(tag);
        if (className) value.className = className;
        if (text != null) value.textContent = String(text);
        return value;
    };
    const projectId = () => String(state.dom?.project?.value || state.deps.getActiveProjectId?.() || '').trim();
    const sessionId = () => String(state.dom?.planSession?.value || state.deps.getCurrentSessionId?.() || '').trim();
    const planScopeKey = () => `${projectId()}::${sessionId() || 'no-session'}`;
    const query = value => encodeURIComponent(String(value || ''));
    const empty = text => node('div', 'workflow-empty', text);

    const listOf = value => {
        if (Array.isArray(value)) return value;
        if (value == null || value === '') return [];
        return [value];
    };

    const plainText = value => {
        if (typeof value === 'string' || typeof value === 'number') return String(value).trim();
        if (!value || typeof value !== 'object') return '';
        return String(value.content || value.text || value.message || value.label || value.title || '').trim();
    };

    const normalizedList = value => listOf(value).map(item => plainText(item)).filter(Boolean);

    function responseMessages(payload, source) {
        const messages = listOf(source.messages || payload.messages);
        return messages.map(message => ({
            role: ['user', 'human'].includes(String(message?.role || '').toLowerCase()) ? 'user' : 'agent',
            content: plainText(message),
        })).filter(message => message.content);
    }

    function normalizedOptions(source) {
        const choices = listOf(source.options || source.choices || source.questions).map((choice, index) => {
            const label = plainText(choice);
            return {
                id: String(choice?.id || choice?.value || `option-${index + 1}`),
                label,
                message: String(choice?.message || choice?.prompt || label).trim(),
            };
        }).filter(choice => choice.label && choice.message);
        const fallbacks = [
            { id: 'clarify', label: '補充更多需求', message: '我想再補充需求，請先不要建立提案。' },
            { id: 'review-permissions', label: '先檢查權限與風險', message: '請先詳細檢查必要權限、風險、可能結果與可復原性。' },
            { id: 'pause', label: '暫停，不建立提案', message: '先暫停規劃，不要建立操作提案。' },
        ];
        fallbacks.forEach(choice => {
            if (choices.length < 2 && !choices.some(item => item.label === choice.label)) choices.push(choice);
        });
        return choices.slice(0, 3);
    }

    function normalizePlanResponse(payload = {}) {
        const source = payload.plan && typeof payload.plan === 'object' ? payload.plan : payload;
        const risk = source.risk && typeof source.risk === 'object' ? source.risk : {};
        const assistant = plainText(source.assistant_message || source.response || source.reply || source.message || payload.assistant_message);
        const status = String(source.status || payload.status || '').toLowerCase();
        const digest = String(source.digest || source.plan_digest || payload.digest || payload.plan_digest || '').trim();
        const blockers = normalizedList(source.blockers || payload.blockers);
        const risks = normalizedList(source.risk_summary || source.risks || risk.warnings || risk.items || payload.risks);
        blockers.forEach(blocker => {
            if (!risks.includes(blocker)) risks.push(blocker);
        });
        return {
            id: String(source.id || source.plan_id || payload.plan_id || '').trim(),
            digest,
            status,
            assistant,
            summary: plainText(source.summary || source.proposal_summary || payload.summary),
            blockers,
            risks,
            outcomes: normalizedList(source.expected_result || source.outcomes || source.possible_results || source.results || payload.outcomes),
            permissions: normalizedList(source.permission_requirements || source.permissions || source.required_permissions || payload.permissions),
            options: normalizedOptions(source),
            messages: responseMessages(payload, source),
            readyToPropose: blockers.length === 0 && (
                source.ready_to_propose === true
                || source.can_propose === true
                || ['ready', 'ready_to_propose', 'proposal_ready'].includes(status)
            ),
        };
    }

    function appendPlanMessage(role, content) {
        const text = String(content || '').trim();
        if (!text) return;
        state.planMessages.push({ role, content: text });
    }

    function planMessageNode(message) {
        const role = message.role === 'user' ? 'user' : message.role === 'system' ? 'system' : 'agent';
        const article = node('article', `n8n-plan-message is-${role}`);
        article.append(node('strong', '', role === 'user' ? '你' : role === 'system' ? '系統' : 'Agent'));
        article.append(node('p', '', message.content));
        return article;
    }

    function renderPlanList(container, values, fallback) {
        const items = values.length ? values : [fallback];
        container.replaceChildren(...items.map(value => node('li', '', value)));
    }

    function renderPlanner() {
        if (!state.initialized) return;
        const hasProject = Boolean(projectId());
        const hasSession = Boolean(sessionId());
        const hasScope = hasProject && hasSession;
        const plan = state.plan;
        const messages = state.planMessages.length ? state.planMessages : [{
            role: 'agent',
            content: '請描述你想完成的流程。我會先說明可行做法、可能結果、風險與需要開放的權限。',
        }];
        state.dom.planMessages.replaceChildren(...messages.map(planMessageNode));
        state.dom.planMessages.setAttribute('aria-busy', state.planBusy ? 'true' : 'false');
        state.dom.planMessages.scrollTop = state.dom.planMessages.scrollHeight;

        const choices = plan?.options || [];
        state.dom.planOptions.replaceChildren(...choices.map(choice => {
            const button = node('button', 'n8n-plan-option', choice.label);
            button.type = 'button';
            button.disabled = state.planBusy;
            button.dataset.optionId = choice.id;
            button.addEventListener('click', () => void sendPlanMessage(choice.message, choice.id));
            return button;
        }));
        state.dom.planOptions.hidden = !choices.length || state.planBusy;

        const hasImpact = Boolean(plan);
        state.dom.planImpact.hidden = !hasImpact;
        if (hasImpact) {
            renderPlanList(state.dom.planRisks, plan.risks, 'Agent 尚未指出額外風險。');
            renderPlanList(state.dom.planOutcomes, plan.outcomes, '尚未確定結果，請繼續釐清需求。');
            renderPlanList(state.dom.planPermissions, plan.permissions, '尚未要求開放額外權限。');
        }

        const proposalReady = Boolean(plan?.readyToPropose && plan?.id && plan?.digest);
        state.dom.planProposal.hidden = !proposalReady;
        state.dom.planProposalSummary.textContent = plan?.summary || '這一步只會建立可執行的待核准提案；核准後 Broker 才會依提案內容實際操作 n8n。';
        state.dom.planPropose.disabled = state.planBusy || !proposalReady || !state.dom.planProposalAck.checked;
        state.dom.planInput.disabled = state.planBusy || !hasScope;
        state.dom.planSend.disabled = state.planBusy || !hasScope;
        state.dom.planReset.disabled = state.planBusy || (!plan && state.planMessages.length === 0);
        const blocked = Boolean(plan?.blockers?.length || plan?.status === 'blocked');
        state.dom.planState.textContent = !hasProject ? '請先選擇 Project' : !hasSession ? '請先選擇 Session' : state.planBusy ? 'Agent 思考中' : blocked ? '前置條件未就緒' : proposalReady ? '等待建立提案' : plan ? '規劃中' : '尚未開始';
        state.dom.planState.className = `workflow-status-pill ${blocked ? 'is-error' : proposalReady ? 'is-warning' : plan ? 'is-success' : ''}`;
        state.deps.createIcons?.();
    }

    function resetPlanner({ announce = false } = {}) {
        state.plan = null;
        state.planMessages = [];
        state.planScope = '';
        state.planBusy = false;
        state.dom.planInput.value = '';
        state.dom.planProposalAck.checked = false;
        if (announce) appendPlanMessage('system', '已清除上一份規劃；尚未建立或執行任何 n8n 操作。');
        renderPlanner();
    }

    function applyPlanResponse(payload) {
        const response = normalizePlanResponse(payload);
        const previous = state.plan || {};
        state.plan = {
            ...previous,
            ...response,
            id: response.id || previous.id || '',
            digest: response.digest || previous.digest || '',
            summary: response.summary || previous.summary || '',
        };
        if (response.messages.length) state.planMessages = response.messages;
        else appendPlanMessage('agent', response.assistant || response.summary || '我已更新規劃，請檢查風險、結果與權限。');
        state.planScope = planScopeKey();
        state.dom.planProposalAck.checked = false;
    }

    async function sendPlanMessage(message, selectedOptionId = '') {
        const content = String(message || '').trim();
        const id = projectId();
        if (!id) return state.deps.showToast?.('請先選擇 Project。', 'warning');
        if (!sessionId()) return state.deps.showToast?.('請先選擇一個屬於此 Project 的 Session。', 'warning');
        if (!content || state.planBusy) return;
        if (state.planScope && state.planScope !== planScopeKey()) resetPlanner();
        if (state.plan?.id && !state.plan.digest) {
            resetPlanner();
            selectedOptionId = '';
            state.deps.showToast?.('舊計畫缺少版本摘要，已安全重開新計畫。', 'warning');
        }

        appendPlanMessage('user', content);
        state.planBusy = true;
        state.dom.planInput.value = '';
        state.dom.planProposalAck.checked = false;
        renderPlanner();
        const currentPlanId = state.plan?.id || '';
        const path = currentPlanId
            ? `/api/integrations/n8n/plans/${query(currentPlanId)}/messages`
            : '/api/integrations/n8n/plans';
        const body = { project_id: id, session_id: sessionId() || null, message: content };
        if (currentPlanId) body.expected_digest = state.plan.digest;
        if (selectedOptionId) body.selected_option_id = selectedOptionId;
        try {
            applyPlanResponse(await api(path, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
            }));
        } catch (error) {
            appendPlanMessage('system', `規劃未送出：${error.message}`);
            state.deps.showToast?.(error.message, 'error');
        } finally {
            state.planBusy = false;
            renderPlanner();
        }
    }

    async function submitPlanMessage(event) {
        event.preventDefault();
        await sendPlanMessage(state.dom.planInput.value);
    }

    async function proposePlan() {
        const plan = state.plan;
        if (!plan?.id || !plan.digest || !plan.readyToPropose || state.planBusy) return;
        if (!state.dom.planProposalAck.checked) return state.deps.showToast?.('請先確認已閱讀風險、結果與所需權限。', 'warning');
        if (state.planScope !== planScopeKey()) {
            resetPlanner();
            return state.deps.showToast?.('Project 或 Session 已變更，請重新規劃。', 'warning');
        }
        state.planBusy = true;
        renderPlanner();
        try {
            const payload = await api(`/api/integrations/n8n/plans/${query(plan.id)}/propose`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project_id: projectId(), session_id: sessionId() || null,
                    expected_digest: plan.digest, explicit_confirmation: true,
                }),
            });
            state.plan = { ...plan, readyToPropose: false, status: 'proposed' };
            state.dom.planProposalAck.checked = false;
            appendPlanMessage('system', '已建立可執行的待核准操作提案；目前尚未操作 n8n，核准後 Broker 才會執行。');
            await refreshAll();
            const operation = payload.operation_request && typeof payload.operation_request === 'object'
                ? payload.operation_request
                : payload.operation && typeof payload.operation === 'object'
                    ? payload.operation
                    : payload.id && payload.operation && payload.digest ? payload : null;
            if (operation?.id) showOperation(operation);
            state.deps.showToast?.('可執行提案已建立；核准後 Broker 才會操作 n8n。', 'success');
        } catch (error) {
            appendPlanMessage('system', `無法建立提案：${error.message}`);
            state.deps.showToast?.(error.message, 'error');
        } finally {
            state.planBusy = false;
            renderPlanner();
        }
    }

    function renderProjects() {
        if (!state.initialized || !state.dom?.project) return;
        const selected = projectId();
        state.dom.project.replaceChildren(new Option('請選擇 Project', ''));
        (state.deps.getProjects?.() || []).filter(item => !item.archived && !item.archived_at).forEach(project => {
            state.dom.project.appendChild(new Option(project.name || project.id, project.id));
        });
        const active = selected || state.deps.getActiveProjectId?.();
        if ([...state.dom.project.options].some(option => option.value === active)) state.dom.project.value = active;
        renderPlanSessions();
    }

    function renderPlanSessions() {
        if (!state.initialized || !state.dom?.planSession) return;
        const selected = state.dom.planSession.value;
        const current = String(state.deps.getCurrentSessionId?.() || '');
        const project = projectId();
        state.dom.planSession.replaceChildren(new Option('請先選擇此 Project 的 Session', ''));
        (state.deps.getSessions?.() || [])
            .filter(session => session.project_id === project && !session.archived && String(session.mode || 'chat') !== 'email')
            .forEach(session => state.dom.planSession.appendChild(new Option(session.title || session.id, session.id)));
        const preferred = selected || current;
        if ([...state.dom.planSession.options].some(option => option.value === preferred)) {
            state.dom.planSession.value = preferred;
        }
    }

    function renderPolicy() {
        const policy = state.policy;
        if (!policy) return;
        state.dom.mode.value = policy.mode || 'restricted';
        state.dom.duration.value = policy.elevation_policy || 'smart';
        state.dom.state.textContent = labels[policy.mode] || policy.mode;
        state.dom.state.className = `workflow-status-pill ${policy.mode === 'full_audit' ? 'is-warning' : policy.mode === 'off' ? '' : 'is-success'}`;
        state.dom.duration.disabled = policy.mode !== 'full_audit' && state.dom.mode.value !== 'full_audit';
        state.dom.ack.closest('label').hidden = state.dom.mode.value !== 'full_audit';
        const expires = policy.expires_at ? new Date(policy.expires_at).toLocaleString() : '';
        state.dom.message.textContent = [
            policy.api_key_configured ? 'API Key 已安全設定' : '尚未設定 API Key',
            policy.runtime_ready ? 'n8n Broker 已就緒' : 'n8n Broker 尚未就緒',
            expires ? `有效至 ${expires}` : '',
        ].filter(Boolean).join(' · ');
    }

    function workflowRow(workflow) {
        const row = node('article', 'mail-run-row');
        const main = node('div', 'mail-run-main');
        main.append(node('strong', '', workflow.name || workflow.id || '未命名 Workflow'));
        main.append(node('div', 'run-inspector-meta', `${workflow.node_count || 0} nodes · ${workflow.active ? '已啟用' : '未啟用'}`));
        const badge = node('span', `workflow-status-pill ${workflow.protected ? 'is-warning' : workflow.active ? 'is-success' : ''}`, workflow.protected ? '系統保護' : workflow.active ? '執行中' : '草稿');
        row.append(main, badge);
        return row;
    }

    function operationRow(operation) {
        const row = node('button', 'mail-run-row n8n-operation-row');
        row.type = 'button';
        const main = node('span', 'mail-run-main');
        main.append(node('strong', '', operation.workflow_name || operation.operation));
        main.append(node('span', 'run-inspector-meta', `${operation.operation} · ${String(operation.digest || '').slice(0, 12)}`));
        row.append(main, node('span', `workflow-status-pill ${operation.status.includes('pending') ? 'is-warning' : operation.status === 'completed' ? 'is-success' : ['failed', 'execution_unknown'].includes(operation.status) ? 'is-error' : ''}`, labels[operation.status] || operation.status));
        row.addEventListener('click', () => showOperation(operation));
        return row;
    }

    function renderLists() {
        state.dom.workflows.replaceChildren(...(state.workflows.length ? state.workflows.map(workflowRow) : [empty('尚無可管理的 Workflow，或 API Key 尚未設定。')]));
        state.dom.workflowCount.textContent = `${state.workflows.length} 筆`;
        state.dom.operations.replaceChildren(...(state.operations.length ? state.operations.map(operationRow) : [empty('尚無 Agent 操作提案。')]));
        state.dom.operationCount.textContent = `${state.operations.length} 筆`;
        const auditRows = state.audits.map(audit => {
            const row = node('article', 'mail-run-row');
            const main = node('div', 'mail-run-main');
            main.append(node('strong', '', `${audit.event_type} · ${audit.actor}`));
            main.append(node('div', 'run-inspector-meta', `${new Date(audit.created_at).toLocaleString()} · ${String(audit.digest || '').slice(0, 12)}`));
            row.appendChild(main);
            return row;
        });
        state.dom.audits.replaceChildren(...(auditRows.length ? auditRows : [empty('尚無稽核紀錄。')]));
        state.deps.createIcons?.();
    }

    function kv(label, value) {
        const row = node('div', 'run-inspector-kv');
        row.append(node('span', '', label), node('strong', '', value || '—'));
        return row;
    }

    function showOperation(operation) {
        state.dom.chatExecution.hidden = true;
        state.dom.chatResults.hidden = true;
        state.dom.inspectorExecution.hidden = false;
        state.dom.inspectorResults.hidden = false;
        document.getElementById('output-floating-workspace')?.classList.add('mail-inspector-active');
        const fragment = document.createDocumentFragment();
        const section = node('section', 'run-inspector-section');
        section.append(node('h3', '', 'n8n 操作核准'));
        section.append(kv('操作', operation.operation), kv('Workflow', operation.workflow_name || operation.workflow_id), kv('狀態', labels[operation.status] || operation.status), kv('Digest', operation.digest));
        const risk = node('div', 'workflow-risk-callout');
        risk.append(node('strong', '', `風險：${operation.risk?.level || 'unknown'}`));
        const warnings = node('ul');
        (operation.risk?.warnings || []).forEach(value => warnings.append(node('li', '', value)));
        risk.appendChild(warnings); section.appendChild(risk);
        const diff = node('pre', 'n8n-operation-diff');
        diff.textContent = JSON.stringify(operation.diff || {}, null, 2);
        section.append(node('h4', '', '伺服器權威 Before／After Diff'));
        section.append(node('p', 'run-inspector-meta', '以下內容取自伺服器鎖定的操作快照；Agent 對話不是核准依據。'), diff);
        if (['pending', 'pending_second_approval'].includes(operation.status)) {
            const actions = node('div', 'run-inspector-actions');
            const reject = node('button', 'run-inspector-button secondary', '拒絕'); reject.type = 'button';
            const approve = node('button', 'run-inspector-button primary', operation.status === 'pending_second_approval' ? '第二次核准並執行' : '核准'); approve.type = 'button';
            reject.addEventListener('click', () => decide(operation, false));
            approve.addEventListener('click', () => decide(operation, true));
            actions.append(reject, approve); section.appendChild(actions);
        }
        fragment.appendChild(section);
        state.dom.inspectorExecution.replaceChildren(fragment);
        state.dom.inspectorResults.replaceChildren(kv('稽核 digest', operation.digest), kv('錯誤碼', operation.error_code), kv('結果', operation.result ? JSON.stringify(operation.result) : '尚無'));
        window.workbenchRunInspector?.selectTab?.('execution');
    }

    async function decide(operation, approved) {
        let confirmation = null;
        if (approved && operation.risk?.irreversible) {
            confirmation = window.prompt(`此操作可能無法復原。請輸入「${operation.workflow_name || operation.workflow_id}」確認：`);
            if (confirmation == null) return;
        }
        try {
            const updated = await api(`/api/integrations/n8n/operation-requests/${query(operation.id)}/${approved ? 'approve' : 'reject'}`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ project_id: operation.project_id, expected_digest: operation.digest, confirmation }),
            });
            state.deps.showToast?.(approved ? '核准已處理。' : '操作已拒絕。', 'success');
            await refreshAll(); showOperation(updated);
        } catch (error) { state.deps.showToast?.(error.message, 'error'); }
    }

    async function refreshAll() {
        if (!state.initialized) return;
        renderProjects();
        if (state.planScope && state.planScope !== planScopeKey()) resetPlanner();
        const id = projectId();
        if (!id) { state.policy = null; state.workflows = []; state.operations = []; state.audits = []; renderLists(); return; }
        const requestId = ++state.requestId;
        const selectedSessionId = sessionId();
        const policy = await api(`/api/integrations/n8n/agent-policy?project_id=${query(id)}${selectedSessionId ? `&session_id=${query(selectedSessionId)}` : ''}`).catch(error => ({ error }));
        if (requestId !== state.requestId || projectId() !== id) return;
        if (policy.error) { state.deps.showToast?.(policy.error.message, 'error'); return; }
        state.policy = policy; renderPolicy();
        const [workflows, operations, audits] = await Promise.all([
            policy.mode === 'off' ? Promise.resolve({ workflows: [] }) : api(`/api/integrations/n8n/managed-workflows?project_id=${query(id)}${selectedSessionId ? `&session_id=${query(selectedSessionId)}` : ''}`).catch(() => ({ workflows: [] })),
            api(`/api/integrations/n8n/operation-requests?project_id=${query(id)}`).catch(() => ({ operations: [] })),
            api(`/api/integrations/n8n/audits?project_id=${query(id)}`).catch(() => ({ audits: [] })),
        ]);
        if (requestId !== state.requestId || projectId() !== id) return;
        state.workflows = workflows.workflows || []; state.operations = operations.operations || []; state.audits = audits.audits || []; renderLists();
    }

    async function savePolicy(event) {
        event.preventDefault();
        const id = projectId(); if (!id) return state.deps.showToast?.('請先選擇 Project。', 'warning');
        const mode = state.dom.mode.value;
        if (mode === 'full_audit' && !state.dom.ack.checked) return state.deps.showToast?.('請先確認完整管理風險。', 'warning');
        try {
            state.policy = await api('/api/integrations/n8n/agent-policy', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: id, mode, elevation_policy: state.dom.duration.value, session_id: sessionId() || null, explicit_ack: mode === 'full_audit' && state.dom.ack.checked }) });
            state.dom.ack.checked = false; renderPolicy(); await refreshAll(); state.deps.showToast?.('Agent n8n 權限已更新。', 'success');
        } catch (error) { state.deps.showToast?.(error.message, 'error'); }
    }

    async function saveApiKey(event) {
        event.preventDefault();
        const value = state.dom.apiKey.value;
        try {
            await api('/api/integrations/n8n/agent-api-key', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ api_key: value }) });
            state.dom.apiKey.value = ''; await refreshAll(); state.deps.showToast?.('n8n API Key 已安全儲存。', 'success');
        } catch (error) { state.deps.showToast?.(error.message, 'error'); }
    }

    function init(options = {}) {
        if (state.initialized) return;
        state.deps = options;
        const id = value => document.getElementById(value);
        state.dom = {
            form: id('n8n-agent-policy-form'), project: id('n8n-agent-project'), mode: id('n8n-agent-mode'), duration: id('n8n-agent-duration'), ack: id('n8n-agent-ack'), state: id('n8n-agent-policy-state'), message: id('n8n-agent-policy-message'),
            apiKeyForm: id('n8n-agent-api-key-form'), apiKey: id('n8n-agent-api-key'), workflows: id('n8n-managed-workflows-list'), workflowCount: id('n8n-managed-workflows-count'), operations: id('n8n-operation-requests-list'), operationCount: id('n8n-operation-requests-count'), audits: id('n8n-agent-audits-list'),
            planForm: id('n8n-plan-form'), planInput: id('n8n-plan-input'), planSend: id('n8n-plan-send'), planReset: id('n8n-plan-reset'),
            planSession: id('n8n-plan-session'),
            planState: id('n8n-plan-state'), planMessages: id('n8n-plan-messages'), planOptions: id('n8n-plan-options'), planImpact: id('n8n-plan-impact'),
            planRisks: id('n8n-plan-risks'), planOutcomes: id('n8n-plan-outcomes'), planPermissions: id('n8n-plan-permissions'),
            planProposal: id('n8n-plan-proposal-confirm'), planProposalSummary: id('n8n-plan-proposal-summary'),
            planProposalAck: id('n8n-plan-proposal-ack'), planPropose: id('n8n-plan-propose'),
            chatExecution: id('run-execution-content'), chatResults: id('run-results-content'), inspectorExecution: id('mail-inspector-execution'), inspectorResults: id('mail-inspector-results'),
        };
        if (Object.values(state.dom).some(value => !value)) throw new Error('n8n governance DOM is incomplete.');
        state.dom.form.addEventListener('submit', savePolicy); state.dom.apiKeyForm.addEventListener('submit', saveApiKey);
        state.dom.project.addEventListener('change', () => { resetPlanner(); renderPlanSessions(); void refreshAll(); });
        state.dom.planSession.addEventListener('change', () => { resetPlanner(); void refreshAll(); });
        state.dom.mode.addEventListener('change', () => { state.dom.duration.disabled = state.dom.mode.value !== 'full_audit'; state.dom.ack.closest('label').hidden = state.dom.mode.value !== 'full_audit'; });
        state.dom.planForm.addEventListener('submit', submitPlanMessage);
        state.dom.planInput.addEventListener('keydown', event => {
            if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
            event.preventDefault();
            state.dom.planForm.requestSubmit();
        });
        state.dom.planReset.addEventListener('click', () => resetPlanner({ announce: true }));
        state.dom.planProposalAck.addEventListener('change', renderPlanner);
        state.dom.planPropose.addEventListener('click', () => void proposePlan());
        // Governance data is only needed in the Workflow workspace.  The
        // existing Workflow controller calls refreshAll() when that workspace
        // opens, avoiding n8n API work on the critical chat startup path.
        state.initialized = true; renderProjects();
        state.refreshTimer = window.setInterval(() => {
            if (!document.getElementById('n8n-workflow-center')?.hidden) void refreshAll();
        }, 30000);
        renderPlanner();
    }

    window.workbenchN8nGovernance = { init, refreshAll, refreshProjects: renderProjects, getState: () => state };
})();
