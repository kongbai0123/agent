/* Workbench-owned UI for the narrow n8n Gmail integration. */

(() => {
    'use strict';

    const TRIGGER_LABEL = 'Workbench-Agent';
    const EDITOR_URL = 'http://127.0.0.1:5678/';
    const PENDING_STATUSES = new Set(['awaiting_approval', 'waiting_approval', 'pending_approval']);
    const TERMINAL_STATUSES = new Set(['sent', 'rejected', 'failed', 'generation_failed', 'delivery_unknown', 'cancelled', 'expired', 'approval_expired', 'blocked_recipient']);

    const state = {
        initialized: false,
        profile: null,
        extension: null,
        service: null,
        runs: [],
        selectedRun: null,
        selectedRequestId: 0,
        inspectorLease: null,
        eventSource: null,
        eventRetryTimer: null,
        eventMailRevision: '',
        eventServiceSignature: '',
        refreshTimer: null,
        refreshDebounce: null,
        backgroundStarted: false,
        profileDirty: false,
        profileSaving: false,
        profileRequestId: 0,
        deps: null,
        dom: null,
    };

    const array = value => Array.isArray(value) ? value : [];
    const string = value => String(value == null ? '' : value);
    const encoded = value => encodeURIComponent(string(value));

    function workflowWorkspaceActive() {
        return state.initialized && state.dom?.center?.hidden === false;
    }

    function mailOwner(runId) {
        return `mail:${string(runId).trim()}`;
    }

    function claimMailInspector(runId) {
        if (!workflowWorkspaceActive() || !runId) return null;
        const lease = window.workbenchRunInspector?.claimContentOwner?.(mailOwner(runId)) || null;
        state.inspectorLease = lease;
        return lease;
    }

    function ownsMailInspector(lease = state.inspectorLease) {
        return !!lease
            && workflowWorkspaceActive()
            && window.workbenchRunInspector?.contentOwnerMatches?.(lease) === true;
    }

    function element(tag, className = '', text = null) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== null) node.textContent = string(text);
        return node;
    }

    function icon(name) {
        const node = element('i');
        node.dataset.lucide = name;
        node.setAttribute('aria-hidden', 'true');
        return node;
    }

    function empty(message, tone = '') {
        const node = element('div', `workflow-empty ${tone}`.trim(), message);
        node.setAttribute('role', tone === 'is-error' ? 'alert' : 'status');
        return node;
    }

    function statusLabel(value) {
        const status = string(value).toLowerCase();
        return {
            received: '已收到', queued: '等待生成', generating: '撰寫草稿中', drafting: '撰寫草稿中', awaiting_approval: '等待核准',
            waiting_approval: '等待核准', pending_approval: '等待核准', approved_queued: '已核准，等待寄送',
            sending: '寄送中', sent: '已寄送', rejected: '已拒絕', failed: '失敗', generation_failed: '草稿生成失敗',
            delivery_unknown: '寄送結果不明（高風險）',
            cancelled: '已取消', expired: '已過期', approval_expired: '核准已過期', blocked_recipient: '收件者不符',
        }[status] || status || '未知';
    }

    function statusTone(value) {
        const status = string(value).toLowerCase();
        if (['sent', 'completed'].includes(status)) return 'is-success';
        if (['failed', 'generation_failed', 'delivery_unknown', 'blocked_recipient'].includes(status)) return 'is-error';
        if (['rejected', 'cancelled', 'expired', 'approval_expired'].includes(status)) return 'is-muted';
        if (PENDING_STATUSES.has(status)) return 'is-warning';
        return 'is-running';
    }

    function apiPath(path) {
        return `${state.deps.apiBase || ''}${path}`;
    }

    async function request(path, options = {}) {
        const response = await state.deps.apiFetch(apiPath(path), options);
        let payload = {};
        try { payload = await response.json(); } catch (_error) { payload = {}; }
        if (!response.ok) {
            const detail = payload?.detail || payload || {};
            const message = detail.message || detail.error || detail.code || payload.message || `HTTP ${response.status}`;
            const error = new Error(string(message));
            error.status = response.status;
            error.code = detail.code || null;
            throw error;
        }
        return payload;
    }

    function profileFrom(payload = {}) {
        const value = payload.profile || payload.mail_profile || payload;
        return {
            projectId: string(value.project_id || value.projectId).trim(),
            instruction: string(value.instruction || value.agent_instruction || value.workflow_instruction),
            enabled: value.enabled === true,
            autoStart: value.auto_start === true || value.autoStart === true,
            defaultModel: string(value.default_model || value.defaultModel).trim(),
            triggerLabel: string(value.trigger_label || value.required_label).trim(),
            recipient: string(value.recipient || value.canary_recipient || value.fixed_recipient).trim().toLowerCase(),
            recipientConfigured: value.recipient_configured === true,
            configured: value.configured === true || Boolean(value.project_id || value.projectId),
        };
    }

    function runIdOf(value = {}) {
        return string(value.run_id || value.id).trim();
    }

    function draftOf(value = {}) {
        const draft = value.draft && typeof value.draft === 'object' ? value.draft : value;
        return {
            id: string(draft.draft_id || draft.id || value.draft_id).trim(),
            subject: string(draft.subject || value.subject),
            body: string(draft.body || draft.text || draft.content || value.body),
            revision: Number(draft.revision ?? value.draft_revision ?? value.revision ?? 0),
            sha256: string(draft.sha256 || draft.content_sha256 || draft.digest || value.draft_sha256 || value.content_sha256 || value.sha256).trim(),
        };
    }

    function runFrom(value = {}) {
        const source = value.source && typeof value.source === 'object' ? value.source : {};
        const delivery = value.delivery && typeof value.delivery === 'object' ? value.delivery : {};
        const draft = draftOf(value);
        const rawMode = string(value.mode || value.mail_mode || value.kind || value.type).toLowerCase();
        const mode = ['compose', 'new', 'new_mail', 'outbound'].includes(rawMode) ? 'compose' : 'reply';
        const recipient = string(value.recipient || value.to || value.draft?.recipient).trim().toLowerCase();
        return {
            raw: value,
            id: runIdOf(value),
            mode,
            status: string(value.status || 'received').toLowerCase(),
            projectId: string(value.project_id || value.projectId).trim(),
            projectName: string(value.project_name || value.projectName),
            createdAt: string(value.created_at || value.createdAt),
            updatedAt: string(value.updated_at || value.updatedAt),
            bindingId: string(value.binding_id || value.thread_binding_id).trim(),
            recipient,
            threadId: string(value.gmail_thread_id || source.thread_id || delivery.thread_id).trim(),
            source: {
                sender: string(source.sender || source.from || value.sender || value.from),
                subject: string(source.subject || value.source_subject),
                receivedAt: string(source.received_at || source.date || value.received_at),
                messageId: string(source.message_id || value.source_message_id).trim(),
                threadId: string(source.thread_id || value.thread_id).trim(),
            },
            attachments: array(value.attachments || source.attachments),
            skills: array(value.skills || value.used_skills),
            references: array(value.references || value.sources),
            draft,
            delivery: {
                executionId: string(delivery.execution_id || value.n8n_execution_id),
                messageId: string(delivery.message_id || value.sent_message_id),
                threadId: string(delivery.thread_id || value.sent_thread_id),
                sentAt: string(delivery.sent_at || value.sent_at),
                error: string(delivery.error || value.error_message),
            },
        };
    }

    function safeEditorUrl(value) {
        try {
            const url = new URL(string(value));
            if (url.href !== EDITOR_URL) return null;
            if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.port !== '5678') return null;
            if (url.username || url.password || url.search || url.hash || url.pathname !== '/') return null;
            return url.href;
        } catch (_error) {
            return null;
        }
    }

    function renderProjects(selected = '') {
        if (!state.initialized || !state.dom?.profileProject) return;
        const select = state.dom.profileProject;
        const projects = array(state.deps.getProjects?.());
        const fragment = document.createDocumentFragment();
        const placeholder = element('option', '', '請選擇專案');
        placeholder.value = '';
        fragment.appendChild(placeholder);
        projects.filter(project => !project.archived).forEach(project => {
            const option = element('option', '', project.name || project.id);
            option.value = string(project.id);
            fragment.appendChild(option);
        });
        if (selected && !projects.some(project => string(project.id) === selected)) {
            const unavailable = element('option', '', '原綁定專案目前無法使用');
            unavailable.value = selected;
            unavailable.disabled = true;
            fragment.appendChild(unavailable);
        }
        select.replaceChildren(fragment);
        select.value = selected || string(state.deps.getActiveProjectId?.());
    }

    function renderModelSelect(select, selected = '', placeholder = '') {
        const models = array(state.deps.getModels?.())
            .map(item => typeof item === 'string'
                ? { value: item, label: item }
                : { value: string(item?.value).trim(), label: string(item?.label || item?.value) })
            .filter(item => item.value);
        const fragment = document.createDocumentFragment();
        const emptyOption = element('option', '', placeholder);
        emptyOption.value = '';
        fragment.appendChild(emptyOption);
        models.forEach(model => {
            const option = element('option', '', model.label);
            option.value = model.value;
            fragment.appendChild(option);
        });
        if (selected && !models.some(model => model.value === selected)) {
            const unavailable = element('option', '', `${selected}（目前不可用）`);
            unavailable.value = selected;
            unavailable.disabled = true;
            fragment.appendChild(unavailable);
        }
        select.replaceChildren(fragment);
        select.value = selected;
    }

    function renderModels() {
        if (!state.initialized || !state.dom?.profileModel || !state.dom?.composeModel) return;
        renderModelSelect(state.dom.profileModel, state.profile?.defaultModel || '', '使用 Workbench 預設模型');
        renderModelSelect(state.dom.composeModel, state.dom.composeModel.value, '使用 Mail Profile 預設模型');
    }

    function renderServiceCommandBar(service = {}, extensionEnabled = false) {
        const running = service.running === true || service.reachable === true;
        const starting = service.starting === true;
        const runtimeInstalled = service.installed === true;
        const isolationReady = service.isolation_ready === true;
        const serviceError = string(service.load_error).trim();
        let title = '正在確認 n8n 狀態';
        let detail = '確認完成後，可直接從這裡啟動、停止或開啟 n8n。';
        let tone = '';

        if (!extensionEnabled) {
            title = 'n8n 擴充尚未啟用';
            detail = '請先安裝並啟用 n8n 擴充。';
            tone = 'is-warning';
        } else if (serviceError) {
            title = '無法確認 n8n 服務狀態';
            detail = serviceError;
            tone = 'is-error';
        } else if (running) {
            title = 'n8n 已連線，可以開始操作';
            detail = '可開啟 n8n 編輯器，或直接在下方說出想自動完成的事情。';
            tone = 'is-success';
        } else if (starting) {
            title = 'n8n 正在啟動';
            detail = 'Workbench 正在啟動受管服務，完成後即可開啟編輯器。';
            tone = 'is-warning is-starting';
        } else if (!runtimeInstalled) {
            title = 'n8n 執行環境尚未就緒';
            detail = '請開啟服務設定，確認受管 n8n runtime 已正確安裝。';
            tone = 'is-warning';
        } else if (!isolationReady) {
            title = 'n8n 需要完成安全隔離設定';
            detail = '完成安全隔離與擁有權檢查後，才能從 Workbench 啟動服務。';
            tone = 'is-warning';
        } else {
            title = 'n8n 已就緒，等待啟動';
            detail = '按下「啟動 n8n」即可直接啟動本機服務。';
            tone = 'is-warning';
        }

        state.dom.commandTitle.textContent = title;
        state.dom.commandDetail.textContent = detail;
        state.dom.commandSignal.className = `workflow-command-signal ${tone}`.trim();
        state.dom.commandVersion.textContent = service.version
            ? `n8n ${service.version}`
            : runtimeInstalled ? '版本讀取中' : '執行環境未安裝';
        state.dom.commandGmail.textContent = service.workflow_ready === true
            ? 'Gmail Workflow 已就緒'
            : 'Gmail Workflow 尚未就緒';

        state.dom.serviceStart.hidden = running;
        state.dom.serviceStart.disabled = !extensionEnabled || !runtimeInstalled || !isolationReady || starting;
        state.dom.serviceStartLabel.textContent = starting ? '啟動中…' : '啟動 n8n';
        state.dom.serviceStart.title = state.dom.serviceStart.disabled && !starting
            ? detail
            : '直接啟動 Workbench 管理的本機 n8n 服務';
        state.dom.serviceStop.hidden = !running && !starting;
        state.dom.serviceStop.disabled = !extensionEnabled || (!running && !starting);
        state.dom.serviceOpen.hidden = !running;
        state.dom.serviceOpen.disabled = !extensionEnabled || !running || !safeEditorUrl(service.editor_url);
    }

    function renderService() {
        const service = state.service || {};
        const extension = state.extension || {};
        const running = service.running === true || service.reachable === true;
        const runtimeInstalled = service.installed === true;
        const extensionInstalled = extension.installed === true;
        const extensionEnabled = extension.effective_enabled === true;
        const extensionError = string(extension.load_error).trim();

        renderServiceCommandBar(service, extensionEnabled);

        state.dom.extensionGate.hidden = extensionEnabled;
        state.dom.enabledContent.hidden = !extensionEnabled;
        syncComposeGate();

        if (extensionError) {
            state.dom.serviceState.textContent = '狀態無法確認';
            state.dom.serviceState.className = 'workflow-status-pill is-error';
            state.dom.serviceDetail.textContent = extensionError;
            state.dom.extensionGateTitle.textContent = '目前無法確認 n8n 擴充狀態';
            state.dom.extensionGateDetail.textContent = '重新整理後仍無法載入時，請到擴充功能檢查安裝狀態。';
            state.dom.extensionManage.textContent = '查看擴充';
            state.dom.extensionOpen.textContent = '前往擴充功能';
            return;
        }
        if (!extensionInstalled) {
            state.dom.serviceState.textContent = '尚未安裝';
            state.dom.serviceState.className = 'workflow-status-pill is-warning';
            state.dom.serviceDetail.textContent = '需要時再安裝 n8n 擴充，工作流程才會加入 Workbench。';
            state.dom.extensionGateTitle.textContent = '安裝 n8n 擴充後開始使用';
            state.dom.extensionGateDetail.textContent = '先在擴充功能認識並安裝 n8n；服務設定與技術資訊會在安裝後逐步顯示。';
            state.dom.extensionManage.textContent = '安裝擴充';
            state.dom.extensionOpen.textContent = '前往擴充功能';
            return;
        }
        if (!extensionEnabled) {
            state.dom.serviceState.textContent = '擴充已停用';
            state.dom.serviceState.className = 'workflow-status-pill is-warning';
            state.dom.serviceDetail.textContent = '重新審查並啟用 n8n 擴充後，才會開放工作流程。';
            state.dom.extensionGateTitle.textContent = '啟用 n8n 擴充以繼續';
            state.dom.extensionGateDetail.textContent = '目前安裝仍保留，但所有 n8n 執行權限都已關閉。';
            state.dom.extensionManage.textContent = '管理擴充';
            state.dom.extensionOpen.textContent = '管理 n8n 擴充';
            return;
        }

        const serviceError = string(service.load_error).trim();
        if (serviceError) {
            state.dom.serviceState.textContent = '服務狀態異常';
            state.dom.serviceState.className = 'workflow-status-pill is-error';
            state.dom.serviceDetail.textContent = serviceError;
            state.dom.extensionManage.textContent = '管理擴充';
            return;
        }

        const serviceLabel = running
            ? '已連線'
            : service.starting === true ? '啟動中' : runtimeInstalled ? '未啟動' : '執行環境未就緒';
        state.dom.serviceState.textContent = serviceLabel;
        state.dom.serviceState.className = `workflow-status-pill ${running ? 'is-success' : 'is-warning'}`;
        state.dom.serviceDetail.textContent = [
            service.version ? `n8n ${service.version}` : 'n8n',
            serviceLabel,
            service.workflow_ready === true ? 'Gmail Workflow 已就緒' : 'Gmail Workflow 尚未就緒',
        ].join(' · ');
        state.dom.extensionManage.textContent = '管理擴充';
    }

    function renderProfile() {
        const profile = state.profile || profileFrom({});
        renderProjects(profile.projectId);
        renderModels();
        state.dom.profileInstruction.value = profile.instruction;
        state.dom.profileEnabled.checked = profile.enabled;
        state.dom.profileAutoStart.checked = profile.autoStart;
        state.dom.profileLabel.value = TRIGGER_LABEL;
        const lockedRecipient = profile.recipientConfigured ? profile.recipient : '';
        state.dom.profileRecipient.value = lockedRecipient;
        state.dom.composeRecipient.value = lockedRecipient;
        state.dom.profileRecipient.placeholder = profile.recipientConfigured ? '' : '尚未在本機設定';
        state.dom.composeRecipient.placeholder = profile.recipientConfigured ? '' : '尚未在本機設定';
        state.profileDirty = false;
        updateInstructionCount();
        const contractSafe = profileContractSafe(profile);
        state.dom.profileSave.disabled = !contractSafe;
        syncComposeGate();
        if (!contractSafe) {
            state.dom.profileSaveState.textContent = '後端安全契約不符，已停止郵件操作。';
            state.dom.profileSaveState.className = 'workflow-save-state is-error';
        } else if (!profile.configured) {
            state.dom.profileSaveState.textContent = '尚未設定';
            state.dom.profileSaveState.className = 'workflow-save-state';
        } else {
            state.dom.profileSaveState.textContent = profile.enabled ? '已啟用' : '已儲存，未啟用';
            state.dom.profileSaveState.className = 'workflow-save-state is-success';
        }
    }

    function profileContractSafe(profile = state.profile) {
        return profile?.triggerLabel === TRIGGER_LABEL
            && profile?.recipientConfigured === true
            && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(profile.recipient);
    }

    function composeAllowed() {
        return n8nExtensionReady()
            && state.profile?.configured === true
            && state.profile?.enabled === true
            && profileContractSafe()
            && state.profileDirty !== true
            && state.profileSaving !== true;
    }

    function syncComposeGate() {
        state.dom.composeCreate.disabled = !composeAllowed();
    }

    function markProfileDirty() {
        state.profileDirty = true;
        syncComposeGate();
        state.dom.profileSaveState.textContent = '有尚未儲存的變更';
        state.dom.profileSaveState.className = 'workflow-save-state is-warning';
    }

    function updateInstructionCount() {
        state.dom.profileInstructionCount.textContent = string(state.dom.profileInstruction.value.length);
    }

    function updateApprovalBadges() {
        const count = state.runs.filter(run => PENDING_STATUSES.has(run.status)).length;
        [state.dom.approvalBadge, state.dom.railBadge].forEach(badge => {
            badge.hidden = count <= 0;
            badge.textContent = count > 99 ? '99+' : string(count);
        });
        const executionLabel = count > 0
            ? `執行狀態；另有 ${count} 封郵件等待核准，可從工作流程中心開啟`
            : '執行狀態';
        state.dom.executionTab.setAttribute('aria-label', executionLabel);
        state.dom.executionTab.title = executionLabel;
        state.dom.railButton.setAttribute('aria-label', count > 0
            ? `開啟工作流程中心；${count} 封郵件等待核准`
            : '開啟工作流程中心');
    }

    function renderRuns() {
        state.dom.runsCount.textContent = `${state.runs.length} 筆`;
        updateApprovalBadges();
        if (!state.runs.length) {
            state.dom.runsList.replaceChildren(empty('尚無郵件執行紀錄。'));
            return;
        }
        const fragment = document.createDocumentFragment();
        state.runs.forEach(run => {
            const row = element('article', 'mail-run-row');
            const leading = element('div', 'mail-run-icon');
            leading.appendChild(icon(run.mode === 'compose' ? 'mail-plus' : 'reply'));
            const copy = element('div', 'mail-run-copy');
            const title = element('strong', '', run.draft.subject || run.source.subject || (run.mode === 'compose' ? '新郵件草稿' : '回覆草稿'));
            const meta = element('span', '', [
                run.mode === 'compose' ? '新郵件' : 'Reply',
                run.projectName || run.projectId || '未綁定專案',
                run.createdAt,
            ].filter(Boolean).join(' · '));
            copy.append(title, meta);
            const status = element('span', `mail-run-status ${statusTone(run.status)}`, statusLabel(run.status));
            const openButton = element('button', 'btn btn-secondary compact', '檢視');
            openButton.type = 'button';
            openButton.setAttribute('aria-label', `檢視郵件：${title.textContent}`);
            openButton.addEventListener('click', () => void openRun(run.id));
            row.append(leading, copy, status, openButton);
            fragment.appendChild(row);
        });
        state.dom.runsList.replaceChildren(fragment);
        state.deps.createIcons?.();
    }

    function inspectorHeader(run, title) {
        const header = element('div', 'mail-inspector-head');
        const copy = element('div');
        copy.append(
            element('strong', '', title),
            element('span', '', `${run.mode === 'compose' ? '新郵件' : 'Reply'} · ${run.projectName || run.projectId || '未綁定專案'}`)
        );
        const back = element('button', 'run-inspector-button secondary', '返回聊天執行');
        back.type = 'button';
        back.addEventListener('click', useChatInspectorContext);
        header.append(copy, back);
        return header;
    }

    function inspectorSection(title, count = null) {
        const section = element('section', 'run-inspector-section mail-inspector-section');
        const head = element('div', 'run-inspector-section-head');
        head.appendChild(element('strong', '', title));
        if (count !== null) head.appendChild(element('span', 'run-inspector-section-count', count));
        section.appendChild(head);
        return section;
    }

    function keyValue(label, value, tone = '') {
        const row = element('div', 'run-inspector-kv');
        row.append(element('span', '', label), element('strong', tone, value || '—'));
        return row;
    }

    function attachmentMetadata(item, index) {
        const row = element('div', 'mail-metadata-row');
        row.append(
            icon('paperclip'),
            element('span', '', item.filename || item.name || `附件 ${index + 1}`),
            element('small', '', [item.mime_type || item.type, item.size_bytes != null ? `${item.size_bytes} bytes` : '', item.status].filter(Boolean).join(' · ') || '僅保存 metadata')
        );
        return row;
    }

    function hasApprovalIdentity(run) {
        return Number.isInteger(run?.draft?.revision)
            && run.draft.revision >= 1
            && /^[a-f0-9]{64}$/.test(run.draft.sha256);
    }

    function runCanEdit(run) {
        return PENDING_STATUSES.has(run.status)
            && runRecipientMatchesProfile(run)
            && hasApprovalIdentity(run);
    }

    function canResolveUnknownDelivery(run) {
        return run?.status === 'delivery_unknown'
            && runRecipientMatchesProfile(run)
            && hasApprovalIdentity(run);
    }

    function runRecipientMatchesProfile(run) {
        return profileContractSafe()
            && run?.recipient === state.profile.recipient;
    }

    function renderMailExecution() {
        const host = state.dom.mailExecution;
        const run = state.selectedRun;
        if (!run) return host.replaceChildren(empty('尚未選擇郵件執行。'));
        const fragment = document.createDocumentFragment();
        fragment.appendChild(inspectorHeader(run, '郵件草稿與核准'));

        const overview = inspectorSection('執行狀態');
        overview.append(
            keyValue('狀態', statusLabel(run.status), statusTone(run.status)),
            keyValue('Run', run.id),
            keyValue('收件者（鎖定）', run.recipient || '未回報（已停用核准）'),
            keyValue('Gmail Thread（鎖定）', run.mode === 'reply' ? (run.threadId || run.source.threadId || '等待來源') : '新郵件，不綁定既有 Thread')
        );
        if (!runRecipientMatchesProfile(run)) {
            overview.appendChild(empty('收件者不符合目前 Mail Profile 的鎖定值，Workbench 已停用草稿修改與核准。', 'is-error'));
        } else if (!hasApprovalIdentity(run)) {
            overview.appendChild(empty('草稿缺少 revision 或 SHA-256，Workbench 已停用修改與核准。', 'is-error'));
        }
        if (run.status === 'delivery_unknown') {
            overview.appendChild(empty(
                'Workbench 無法確認 Gmail 是否已寄出。禁止再次編輯或核准；請先到 Gmail 確認未寄出，才可使用下方唯一的重新生成動作。',
                'is-error'
            ));
        }
        fragment.appendChild(overview);

        if (run.mode === 'reply') {
            const source = inspectorSection('來源郵件');
            source.append(
                keyValue('寄件者', run.source.sender),
                keyValue('原始主旨', run.source.subject),
                keyValue('收到時間', run.source.receivedAt),
                keyValue('Message ID', run.source.messageId)
            );
            fragment.appendChild(source);
        }

        const attachments = inspectorSection('附件（鎖定、僅 metadata）', run.attachments.length);
        if (run.attachments.length) run.attachments.forEach((item, index) => attachments.appendChild(attachmentMetadata(item, index)));
        else attachments.appendChild(empty('沒有來源附件；Workbench 不會自動把附件加入外寄郵件。'));
        fragment.appendChild(attachments);

        const draftSection = inspectorSection('可編輯草稿');
        const form = element('form', 'mail-draft-form');
        const subjectLabel = element('label');
        subjectLabel.appendChild(element('span', '', '主旨'));
        const subject = element('input');
        subject.value = run.draft.subject;
        subject.maxLength = 240;
        subject.disabled = run.mode === 'reply' || !runCanEdit(run);
        subject.setAttribute('aria-describedby', 'mail-draft-subject-hint');
        const subjectHint = element('small', '', run.mode === 'reply'
            ? 'Reply 主旨由來源 Thread 鎖定，不能修改。'
            : '新郵件主旨可在核准前修改。');
        subjectHint.id = 'mail-draft-subject-hint';
        subjectLabel.append(subject, subjectHint);

        const bodyLabel = element('label');
        bodyLabel.appendChild(element('span', '', '正文'));
        const body = element('textarea');
        body.rows = 10;
        body.maxLength = 12000;
        body.value = run.draft.body;
        body.disabled = !runCanEdit(run);
        bodyLabel.appendChild(body);

        const revision = element('div', 'mail-draft-revision');
        revision.append(
            element('span', '', `Revision ${run.draft.revision}`),
            element('code', '', run.draft.sha256 ? `SHA-256 ${run.draft.sha256}` : '尚無核准 digest')
        );

        const dirtyNotice = element('p', 'mail-draft-dirty', '草稿有未儲存變更；儲存後才可核准。');
        dirtyNotice.hidden = true;
        const actions = element('div', 'mail-draft-actions');
        const save = element('button', 'run-inspector-button secondary', '儲存修改');
        save.type = 'submit';
        const unknownDelivery = run.status === 'delivery_unknown';
        const regenerate = element(
            'button',
            `run-inspector-button ${unknownDelivery ? 'danger' : 'secondary'}`,
            unknownDelivery ? '確認 Gmail 未寄出後重新生成' : '重新生成'
        );
        regenerate.type = 'button';
        const reject = element('button', 'run-inspector-button danger', '拒絕');
        reject.type = 'button';
        const approve = element('button', 'run-inspector-button primary', '核准並寄送');
        approve.type = 'button';
        if (unknownDelivery) actions.append(regenerate);
        else actions.append(save, regenerate, reject, approve);
        form.append(subjectLabel, bodyLabel, revision, dirtyNotice, actions);
        draftSection.appendChild(form);
        fragment.appendChild(draftSection);

        const editable = runCanEdit(run);
        save.disabled = !editable;
        regenerate.disabled = unknownDelivery ? !canResolveUnknownDelivery(run) : !editable;
        reject.disabled = !editable;
        approve.disabled = !editable;
        let dirty = false;
        const syncDirty = () => {
            dirty = body.value !== run.draft.body || (run.mode === 'compose' && subject.value !== run.draft.subject);
            dirtyNotice.hidden = !dirty;
            save.disabled = !editable || !dirty;
            approve.disabled = !editable || dirty;
            approve.title = dirty ? '請先儲存草稿修改' : '';
        };
        subject.addEventListener('input', syncDirty);
        body.addEventListener('input', syncDirty);
        syncDirty();

        form.addEventListener('submit', async event => {
            event.preventDefault();
            if (!dirty) return;
            await mutateDraft('save', save, {
                body: body.value,
                ...(run.mode === 'compose' ? { subject: subject.value } : {}),
            });
        });
        regenerate.addEventListener('click', () => void (unknownDelivery
            ? confirmUnknownDeliveryRegeneration(regenerate)
            : mutateDraft('regenerate', regenerate)));
        reject.addEventListener('click', () => void mutateDraft('reject', reject));
        approve.addEventListener('click', () => void mutateDraft('approve', approve));

        if (run.bindingId && !unknownDelivery) {
            const binding = inspectorSection('郵件對話綁定');
            binding.appendChild(element(
                'p',
                'mail-binding-description',
                '刪除只會移除 Workbench 的郵件對話綁定與後續自動回覆關聯；不會刪除 Gmail 郵件、Thread 或附件。'
            ));
            const remove = element('button', 'run-inspector-button danger', '刪除 Workbench 綁定');
            remove.type = 'button';
            remove.addEventListener('click', () => void deleteBinding(remove));
            binding.appendChild(remove);
            fragment.appendChild(binding);
        }

        host.replaceChildren(fragment);
        state.deps.createIcons?.();
    }

    function evidenceName(item, fallback) {
        if (typeof item === 'string') return item;
        return item?.name || item?.title || item?.slug || item?.path || item?.filename || fallback;
    }

    function renderMailResults() {
        const host = state.dom.mailResults;
        const run = state.selectedRun;
        if (!run) return host.replaceChildren(empty('尚未選擇郵件執行。'));
        const fragment = document.createDocumentFragment();
        fragment.appendChild(inspectorHeader(run, '郵件來源與結果'));

        const sources = inspectorSection('本輪實際使用的來源');
        sources.append(
            keyValue('來源類型', run.mode === 'reply' ? 'Gmail 郵件' : 'Workbench 新郵件表單'),
            keyValue('來源 Message ID', run.source.messageId),
            keyValue('Project', run.projectName || run.projectId),
            keyValue('收件者', run.recipient || '未回報（已停用核准）')
        );
        fragment.appendChild(sources);

        const skills = inspectorSection('固定 Project Skills', run.skills.length);
        if (run.skills.length) run.skills.forEach((item, index) => skills.appendChild(keyValue(evidenceName(item, `Skill ${index + 1}`), item.version ? `v${item.version}` : '已使用')));
        else skills.appendChild(empty('此執行未回報使用的 Project Skill。'));
        fragment.appendChild(skills);

        const references = inspectorSection('固定 Project references', run.references.length);
        if (run.references.length) run.references.forEach((item, index) => references.appendChild(keyValue(evidenceName(item, `Reference ${index + 1}`), item.kind || item.type || 'reference')));
        else references.appendChild(empty('此執行未回報使用的 reference。'));
        fragment.appendChild(references);

        const attachments = inspectorSection('來源附件 metadata', run.attachments.length);
        if (run.attachments.length) run.attachments.forEach((item, index) => attachments.appendChild(attachmentMetadata(item, index)));
        else attachments.appendChild(empty('沒有附件 metadata。'));
        fragment.appendChild(attachments);

        const delivery = inspectorSection('寄送結果');
        delivery.append(
            keyValue('狀態', statusLabel(run.status), statusTone(run.status)),
            keyValue('n8n Execution', run.delivery.executionId),
            keyValue('Gmail Message ID', run.delivery.messageId),
            keyValue('Gmail Thread ID', run.delivery.threadId),
            keyValue('寄送時間', run.delivery.sentAt)
        );
        if (run.delivery.error) delivery.appendChild(empty(run.delivery.error, 'is-error'));
        fragment.appendChild(delivery);
        host.replaceChildren(fragment);
        state.deps.createIcons?.();
    }

    function showMailInspector(lease = state.inspectorLease) {
        if (!state.selectedRun || !ownsMailInspector(lease)) return;
        state.dom.chatExecution.hidden = true;
        state.dom.chatResults.hidden = true;
        state.dom.mailExecution.hidden = false;
        state.dom.mailResults.hidden = false;
        document.getElementById('output-floating-workspace')?.classList.add('mail-inspector-active');
    }

    function useChatInspectorContext({ open = true } = {}) {
        state.selectedRun = null;
        state.inspectorLease = null;
        ++state.selectedRequestId;
        window.workbenchRunInspector?.claimContentOwner?.('chat');
        state.dom.chatExecution.hidden = false;
        state.dom.chatResults.hidden = false;
        state.dom.mailExecution.hidden = true;
        state.dom.mailResults.hidden = true;
        document.getElementById('output-floating-workspace')?.classList.remove('mail-inspector-active');
        if (open) window.workbenchRunInspector?.selectTab?.('execution');
    }

    async function confirmUnknownDeliveryRegeneration(button) {
        const run = state.selectedRun;
        if (!canResolveUnknownDelivery(run)) {
            state.deps.showToast?.('寄送結果不明的草稿缺少安全識別，已停止重新生成。', 'error');
            return;
        }
        const confirmed = window.confirm(
            '高風險：Workbench 無法確認這封郵件是否已寄出。\n\n請先到 Gmail 的「寄件備份」與原對話串確認郵件沒有寄出。若郵件其實已寄出，重新生成後再寄送可能造成重複寄信。\n\n我已確認 Gmail 未寄出，繼續重新生成？'
        );
        if (!confirmed) return;
        await mutateDraft('regenerate', button);
    }

    async function mutateDraft(action, button, changes = {}) {
        const run = state.selectedRun;
        const actionAllowed = action === 'regenerate' && run?.status === 'delivery_unknown'
            ? canResolveUnknownDelivery(run)
            : runCanEdit(run);
        if (!run?.draft.id || !actionAllowed) {
            state.deps.showToast?.('草稿安全契約不完整或收件者不符，已停止操作。', 'error');
            return;
        }
        button.disabled = true;
        const concurrency = {
            expected_revision: run.draft.revision,
            expected_sha256: run.draft.sha256,
        };
        try {
            if (action === 'save') {
                await request(`/api/integrations/n8n/mail-drafts/${encoded(run.draft.id)}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ...changes, ...concurrency }),
                });
                state.deps.showToast?.('郵件草稿已儲存；核准 digest 已更新。', 'success');
            } else {
                await request(`/api/integrations/n8n/mail-drafts/${encoded(run.draft.id)}/${action}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(concurrency),
                });
                state.deps.showToast?.({
                    approve: '草稿已核准並排入寄送。',
                    reject: '草稿已拒絕。',
                    regenerate: '已要求重新生成草稿。',
                }[action], action === 'reject' ? 'info' : 'success');
            }
            await Promise.all([loadRun(run.id, { openInspector: false }), refreshRuns({ quiet: true })]);
        } catch (error) {
            const stale = error.status === 409;
            state.deps.showToast?.(stale
                ? '草稿已被更新，正在重新載入最新版本。'
                : `郵件操作失敗：${error.message}`, stale ? 'info' : 'error');
            if (stale) await loadRun(run.id, { openInspector: false });
        } finally {
            button.disabled = false;
        }
    }

    async function deleteBinding(button) {
        const run = state.selectedRun;
        if (!run?.bindingId) return;
        const confirmed = window.confirm(
            '確定刪除 Workbench 郵件對話綁定？\n\n這不會刪除 Gmail 郵件、Thread 或附件，但之後的同 Thread 郵件不再沿用此 Workbench 關聯。'
        );
        if (!confirmed) return;
        button.disabled = true;
        try {
            await request(`/api/integrations/n8n/mail-threads/${encoded(run.bindingId)}`, { method: 'DELETE' });
            state.deps.showToast?.('Workbench 郵件對話綁定已刪除。', 'success');
            useChatInspectorContext();
            await refreshRuns({ quiet: true });
        } catch (error) {
            state.deps.showToast?.(`無法刪除綁定：${error.message}`, 'error');
            button.disabled = false;
        }
    }

    async function loadRun(runId, { openInspector = true } = {}) {
        if (!runId) return;
        const requestId = ++state.selectedRequestId;
        const lease = openInspector ? claimMailInspector(runId) : state.inspectorLease;
        if (!lease || lease.owner !== mailOwner(runId) || !ownsMailInspector(lease)) return;
        if (openInspector) {
            state.dom.mailExecution.replaceChildren(empty('載入郵件執行…'));
            state.dom.mailResults.replaceChildren(empty('載入郵件結果…'));
            state.dom.chatExecution.hidden = true;
            state.dom.chatResults.hidden = true;
            state.dom.mailExecution.hidden = false;
            state.dom.mailResults.hidden = false;
            window.workbenchRunInspector?.selectTab?.('execution');
        }
        try {
            const payload = await request(`/api/integrations/n8n/mail-runs/${encoded(runId)}`);
            if (requestId !== state.selectedRequestId || !ownsMailInspector(lease)) return;
            const selectedRun = runFrom(payload.run || payload.mail_run || payload);
            const authoritativeProject = string(state.profile?.projectId).trim();
            if (!authoritativeProject || selectedRun.projectId !== authoritativeProject) {
                useChatInspectorContext({ open: false });
                state.deps.showToast?.('郵件執行不屬於目前的 Project，已停止顯示。', 'error');
                return;
            }
            state.selectedRun = selectedRun;
            renderMailExecution();
            renderMailResults();
            showMailInspector(lease);
        } catch (error) {
            if (requestId !== state.selectedRequestId || !ownsMailInspector(lease)) return;
            state.dom.mailExecution.replaceChildren(empty(`無法載入郵件執行：${error.message}`, 'is-error'));
            state.dom.mailResults.replaceChildren(empty('此郵件結果目前無法顯示。', 'is-error'));
        }
    }

    async function openRun(runId) {
        await loadRun(runId, { openInspector: true });
    }

    async function refreshRuns({ quiet = false } = {}) {
        try {
            const payload = await request('/api/integrations/n8n/mail-runs?limit=50');
            state.runs = array(payload.runs || payload.mail_runs).map(runFrom).filter(run => run.id);
            renderRuns();
            if (state.selectedRun) {
                const summary = state.runs.find(run => run.id === state.selectedRun.id);
                if (summary && (
                    summary.status !== state.selectedRun.status
                    || (summary.updatedAt && summary.updatedAt !== state.selectedRun.updatedAt)
                    || TERMINAL_STATUSES.has(summary.status) && !TERMINAL_STATUSES.has(state.selectedRun.status)
                )) {
                    await loadRun(summary.id, { openInspector: false });
                }
            }
        } catch (error) {
            if (!quiet) state.dom.runsList.replaceChildren(empty(`無法載入郵件執行：${error.message}`, 'is-error'));
        }
    }

    async function refreshProfile({ preserveDirty = true } = {}) {
        if (state.profileSaving) return;
        const requestId = ++state.profileRequestId;
        try {
            const payload = await request('/api/integrations/n8n/mail-profile');
            if (requestId !== state.profileRequestId) return;
            state.profile = profileFrom(payload);
            if (preserveDirty && state.profileDirty) {
                syncComposeGate();
            } else {
                renderProfile();
            }
        } catch (error) {
            if (requestId !== state.profileRequestId) return;
            state.profile = profileFrom({});
            state.profileDirty = false;
            renderProfile();
            state.dom.profileSaveState.textContent = `無法載入：${error.message}`;
            state.dom.profileSaveState.className = 'workflow-save-state is-error';
        }
    }

    async function refreshExtensionState() {
        try {
            const payload = await request('/api/extensions');
            state.extension = array(payload.extensions)
                .find(item => string(item?.id) === 'builtin.n8n')
                || { id: 'builtin.n8n', installed: false, effective_enabled: false };
        } catch (error) {
            state.extension = {
                id: 'builtin.n8n',
                installed: false,
                effective_enabled: false,
                load_error: `無法取得擴充狀態：${error.message}`,
            };
        }
        renderService();
        if (!n8nExtensionReady() && state.backgroundStarted) stopBackgroundSync();
        return state.extension;
    }

    function n8nExtensionReady() {
        return state.extension?.installed === true && state.extension?.effective_enabled === true;
    }

    async function refreshService() {
        try {
            state.service = await request('/api/integrations/n8n/status');
            state.eventServiceSignature = serviceEventSignature(state.service);
        } catch (error) {
            state.service = {
                installed: false,
                reachable: false,
                load_error: `無法取得 n8n 服務狀態：${error.message}`,
            };
            state.eventServiceSignature = serviceEventSignature(state.service);
        }
        renderService();
        publishServiceState();
        return state.service;
    }

    async function refreshAll() {
        state.dom.refresh.disabled = true;
        await refreshExtensionState();
        if (n8nExtensionReady()) {
            await Promise.allSettled([refreshService(), refreshProfile(), refreshRuns()]);
        } else {
            await refreshService();
        }
        state.dom.refresh.disabled = false;
        state.deps.createIcons?.();
    }

    async function saveProfile(event) {
        event.preventDefault();
        const projectId = state.dom.profileProject.value;
        const instruction = state.dom.profileInstruction.value.trim();
        if (!projectId || !instruction) return;
        state.profileDirty = true;
        state.profileSaving = true;
        ++state.profileRequestId;
        state.dom.profileSave.disabled = true;
        syncComposeGate();
        state.dom.profileSaveState.textContent = '儲存中…';
        try {
            const payload = await request('/api/integrations/n8n/mail-profile', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project_id: projectId,
                    instruction,
                    default_model: state.dom.profileModel.value || null,
                    enabled: state.dom.profileEnabled.checked,
                    auto_start: state.dom.profileAutoStart.checked,
                }),
            });
            state.profile = profileFrom(payload);
            state.profileSaving = false;
            renderProfile();
            state.deps.showToast?.('Mail Profile 已儲存。', 'success');
        } catch (error) {
            state.profileSaving = false;
            await refreshProfile({ preserveDirty: false });
            state.dom.profileSaveState.textContent = `儲存失敗：${error.message}`;
            state.dom.profileSaveState.className = 'workflow-save-state is-error';
            state.dom.profileSave.disabled = !profileContractSafe();
        }
    }

    async function createComposeDraft({ instruction, subject = '', model = '' } = {}) {
        const content = string(instruction).trim();
        state.dom.composeInstruction.value = content;
        if (!composeAllowed()) {
            syncComposeGate();
            return {
                status: 'blocked',
                message: 'Mail Profile 尚未啟用、已變更或安全契約不完整；目前未建立或寄送郵件。',
            };
        }
        if (!content) return { status: 'blocked', message: '缺少郵件工作指示。' };

        const mentionedRecipients = (content.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi) || [])
            .map(value => value.toLowerCase());
        if (mentionedRecipients.some(value => value !== state.profile.recipient)) {
            return {
                status: 'blocked',
                message: `V1 只允許固定收件者 ${state.profile.recipient}；目前未建立或寄送郵件。`,
            };
        }

        state.dom.composeCreate.disabled = true;
        let payload;
        try {
            const composePayload = {
                instruction: content,
                ...(subject ? { subject } : {}),
                ...(model ? { model } : {}),
            };
            payload = await request('/api/integrations/n8n/mail/compose', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(composePayload),
            });
        } catch (error) {
            return { status: 'blocked', message: `無法建立郵件草稿：${error.message}` };
        } finally {
            syncComposeGate();
        }

        // The POST is the authoritative creation boundary.  A later refresh
        // or Inspector rendering failure must not invite a duplicate retry.
        state.dom.composeSubject.value = '';
        state.dom.composeInstruction.value = '';
        state.dom.composeModel.value = '';
        const runId = runIdOf(payload.run || payload.mail_run || payload);
        await refreshRuns({ quiet: true });
        if (runId) {
            try { await openRun(runId); }
            catch (error) { state.deps.showToast?.(`草稿已建立，但檢查器載入失敗：${error.message}`, 'warning'); }
        }
        return { status: 'draft_created', runId };
    }

    async function createCompose(event) {
        event.preventDefault();
        const result = await createComposeDraft({
            subject: state.dom.composeSubject.value.trim(),
            instruction: state.dom.composeInstruction.value.trim(),
            model: state.dom.composeModel.value || '',
        });
        if (result.status !== 'draft_created') {
            state.deps.showToast?.(result.message, 'error');
            return;
        }
        state.dom.composeSubject.value = '';
        state.dom.composeInstruction.value = '';
        state.dom.composeModel.value = '';
        state.deps.showToast?.('新郵件草稿已建立，請在右上檢查器核准。', 'success');
    }

    async function createComposeFromChat(options = {}) {
        const instruction = string(options.instruction).trim();
        state.dom.composeSubject.value = '';
        state.dom.composeInstruction.value = instruction;
        state.dom.composeModel.value = '';
        const result = await createComposeDraft({ instruction });
        if (result.status === 'draft_created') {
            state.dom.composeInstruction.value = '';
        }
        return result;
    }

    async function serviceAction(action, button = null) {
        if (button) button.disabled = true;
        if (action === 'start') {
            state.service = { ...(state.service || {}), starting: true };
            renderService();
        }
        try {
            state.service = await request(`/api/integrations/n8n/${action}`, { method: 'POST' });
            state.deps.showToast?.(action === 'start' ? 'n8n 啟動要求已送出。' : 'n8n 已停止。', 'success');
            await refreshService();
        } catch (error) {
            state.deps.showToast?.(`n8n 操作失敗：${error.message}`, 'error');
            await refreshService();
        } finally {
            renderService();
        }
        return state.service;
    }

    function openEditor() {
        if (!n8nExtensionReady()) {
            state.deps.showToast?.('請先啟用 n8n 擴充，再開啟編輯器。', 'warning');
            return false;
        }
        const running = state.service?.running === true || state.service?.reachable === true;
        if (!running) {
            state.deps.showToast?.('n8n 服務尚未啟動。', 'warning');
            return false;
        }
        const url = safeEditorUrl(state.service?.editor_url);
        if (!url) {
            state.deps.showToast?.('n8n 編輯器網址未通過本機安全檢查。', 'error');
            return false;
        }
        const opened = window.open(url, '_blank', 'noopener,noreferrer');
        if (opened) opened.opener = null;
        return !!opened;
    }

    function scheduleRunsRefresh() {
        window.clearTimeout(state.refreshDebounce);
        state.refreshDebounce = window.setTimeout(() => void refreshRuns({ quiet: true }), 250);
    }

    function serviceEventSignature(payload = {}) {
        return JSON.stringify([
            payload.state || '', payload.reason || '', payload.installed === true,
            payload.running === true, payload.reachable === true, payload.starting === true,
            payload.workflow_ready === true, payload.isolation_ready === true,
            payload.version || '', payload.editor_url || '',
        ]);
    }

    function publishServiceState() {
        if (typeof window.dispatchEvent !== 'function' || typeof window.CustomEvent !== 'function') return;
        window.dispatchEvent(new window.CustomEvent('workbench:n8n-service-state', {
            detail: { ...(state.service || {}) },
        }));
    }

    function handleStatusSnapshot(payload = {}) {
        const nextServiceSignature = serviceEventSignature(payload);
        if (nextServiceSignature !== state.eventServiceSignature) {
            state.eventServiceSignature = nextServiceSignature;
            state.service = payload;
            renderService();
            publishServiceState();
            void refreshProfile();
        }
        const nextMailRevision = string(payload.mail?.revision).trim();
        if (nextMailRevision && nextMailRevision !== state.eventMailRevision) {
            state.eventMailRevision = nextMailRevision;
            scheduleRunsRefresh();
        }
    }

    function connectEvents() {
        if (typeof EventSource !== 'function' || state.eventSource) return;
        try {
            const source = new EventSource(apiPath('/api/integrations/n8n/events'));
            const handleEvent = event => {
                try {
                    const payload = JSON.parse(event.data || '{}');
                    if (event.type === 'status') {
                        handleStatusSnapshot(payload);
                    } else if (payload.type || payload.run_id || payload.pending_approvals != null) {
                        scheduleRunsRefresh();
                    }
                } catch (_error) {
                    scheduleRunsRefresh();
                }
            };
            source.onmessage = handleEvent;
            source.addEventListener('status', handleEvent);
            source.onerror = () => {
                source.close();
                state.eventSource = null;
                window.clearTimeout(state.eventRetryTimer);
                state.eventRetryTimer = window.setTimeout(connectEvents, 5000);
            };
            state.eventSource = source;
        } catch (_error) {
            state.eventSource = null;
        }
    }

    async function ensureServiceForWorkspace() {
        if (!n8nExtensionReady()) return;
        await refreshService();
        const service = state.service || {};
        const running = service.running === true || service.reachable === true;
        if (running || service.starting === true || service.installed !== true) return;
        if (service.isolation_ready !== true) return;
        try {
            state.service = await request('/api/integrations/n8n/start', { method: 'POST' });
            renderService();
            state.deps.showToast?.('已按需啟動 n8n。', 'success');
        } catch (error) {
            state.deps.showToast?.(`n8n 按需啟動失敗：${error.message}`, 'error');
        }
    }

    async function prepare() {
        await refreshExtensionState();
        if (!n8nExtensionReady()) {
            await refreshService();
            state.deps.createIcons?.();
            return { ready: false, extension: state.extension, service: state.service };
        }
        startBackgroundSync();
        await ensureServiceForWorkspace();
        await Promise.allSettled([refreshProfile(), refreshRuns()]);
        state.deps.createIcons?.();
        void window.workbenchN8nGovernance?.refreshAll?.();
        return {
            ready: state.service?.running === true || state.service?.reachable === true,
            extension: state.extension,
            service: state.service,
        };
    }

    function open() {
        state.deps.onWorkspaceOpen?.();
        state.dom.center.hidden = false;
        state.dom.title.setAttribute('tabindex', '-1');
        state.dom.title.focus();
        return prepare();
    }

    function startBackgroundSync() {
        if (state.backgroundStarted) return;
        state.backgroundStarted = true;
        connectEvents();
        state.refreshTimer = window.setInterval(() => void Promise.allSettled([
            refreshService(), refreshProfile(), refreshRuns({ quiet: true }),
        ]), 30000);
    }

    function stopBackgroundSync() {
        if (state.eventSource) state.eventSource.close();
        state.eventSource = null;
        window.clearTimeout(state.eventRetryTimer);
        window.clearTimeout(state.refreshDebounce);
        window.clearInterval(state.refreshTimer);
        state.eventRetryTimer = null;
        state.refreshTimer = null;
        state.backgroundStarted = false;
    }

    async function startBackgroundSyncWhenEnabled() {
        await refreshExtensionState();
        if (n8nExtensionReady()) startBackgroundSync();
    }

    function close() {
        state.dom.center.hidden = true;
        useChatInspectorContext({ open: false });
    }

    function init(options = {}) {
        if (state.initialized) return;
        state.deps = {
            apiFetch: options.apiFetch,
            apiBase: options.apiBase || '',
            showToast: options.showToast,
            createIcons: options.createIcons,
            getProjects: options.getProjects,
            getActiveProjectId: options.getActiveProjectId,
            getModels: options.getModels,
            onWorkspaceOpen: options.onWorkspaceOpen,
        };
        if (typeof state.deps.apiFetch !== 'function') throw new Error('工作流程中心需要 apiFetch。');
        const byId = id => document.getElementById(id);
        state.dom = {
            center: byId('n8n-workflow-center'), title: byId('workflow-center-title'), refresh: byId('workflow-refresh'),
            serviceState: byId('workflow-service-state'), serviceDetail: byId('workflow-service-detail'),
            commandSignal: byId('workflow-command-signal'), commandTitle: byId('workflow-command-title'),
            commandDetail: byId('workflow-command-detail'), commandVersion: byId('workflow-command-version'),
            commandGmail: byId('workflow-command-gmail'), serviceStart: byId('workflow-service-start'),
            serviceStartLabel: byId('workflow-service-start-label'), serviceStop: byId('workflow-service-stop'),
            serviceOpen: byId('workflow-service-open'), serviceSettings: byId('workflow-service-settings'),
            extensionManage: byId('workflow-extension-manage'), extensionGate: byId('workflow-extension-gate'),
            extensionGateTitle: byId('workflow-extension-gate-title'), extensionGateDetail: byId('workflow-extension-gate-detail'),
            extensionOpen: byId('workflow-extension-open'), enabledContent: byId('workflow-enabled-content'),
            profileForm: byId('mail-profile-form'), profileLabel: byId('mail-profile-label'),
            profileRecipient: byId('mail-profile-recipient'), profileProject: byId('mail-profile-project'),
            profileInstruction: byId('mail-profile-instruction'), profileInstructionCount: byId('mail-profile-instruction-count'),
            profileModel: byId('mail-profile-model'),
            profileEnabled: byId('mail-profile-enabled'), profileAutoStart: byId('mail-profile-auto-start'),
            profileSave: byId('mail-profile-save'),
            profileSaveState: byId('mail-profile-save-state'), composeForm: byId('mail-compose-form'),
            composeRecipient: byId('mail-compose-recipient'), composeSubject: byId('mail-compose-subject'),
            composeInstruction: byId('mail-compose-instruction'), composeModel: byId('mail-compose-model'),
            composeCreate: byId('mail-compose-create'),
            runsList: byId('mail-runs-list'), runsCount: byId('mail-runs-count'), railBadge: byId('workflow-rail-badge'),
            railButton: byId('rail-workflows'), executionTab: byId('output-tab-execution'),
            approvalBadge: byId('mail-approval-badge'), chatExecution: byId('run-execution-content'),
            chatResults: byId('run-results-content'), mailExecution: byId('mail-inspector-execution'),
            mailResults: byId('mail-inspector-results'),
        };
        if (Object.values(state.dom).some(node => !node)) throw new Error('工作流程中心 DOM 不完整。');

        state.dom.refresh.addEventListener('click', () => void refreshAll());
        state.dom.profileInstruction.addEventListener('input', updateInstructionCount);
        [state.dom.profileProject, state.dom.profileInstruction, state.dom.profileModel,
            state.dom.profileEnabled, state.dom.profileAutoStart]
            .forEach(control => control.addEventListener('input', markProfileDirty));
        state.dom.profileProject.addEventListener('change', () => useChatInspectorContext({ open: false }));
        state.dom.profileForm.addEventListener('submit', saveProfile);
        state.dom.composeForm.addEventListener('submit', createCompose);
        const openExtension = () => void window.workbenchExtensions?.openExtension?.('builtin.n8n');
        state.dom.extensionManage.addEventListener('click', openExtension);
        state.dom.extensionOpen.addEventListener('click', openExtension);
        state.dom.serviceSettings.addEventListener('click', openExtension);
        state.dom.serviceStart.addEventListener('click', () => void serviceAction('start', state.dom.serviceStart));
        state.dom.serviceStop.addEventListener('click', () => void serviceAction('stop', state.dom.serviceStop));
        state.dom.serviceOpen.addEventListener('click', openEditor);
        state.profile = profileFrom({});
        renderProfile();
        renderService();
        renderRuns();
        state.initialized = true;
        // Mail badges are useful in the background, but they must not compete
        // with the first settings/projects/session render.  Start integration
        // polling only after the critical UI has had time to paint.
        window.setTimeout(() => void startBackgroundSyncWhenEnabled(), 2500);
        state.deps.createIcons?.();
    }

    window.workbenchN8nWorkflows = {
        init,
        prepare,
        open,
        close,
        openRun,
        createComposeFromChat,
        useChatInspectorContext,
        refreshAll,
        refreshService,
        refreshExtensionState,
        manageService: serviceAction,
        openEditor,
        refreshProjects: () => renderProjects(state.profile?.projectId || ''),
        refreshModels: renderModels,
        getState: () => state,
    };
})();
