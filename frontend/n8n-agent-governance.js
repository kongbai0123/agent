/* Project-scoped, audited Agent administration for the managed n8n instance. */
(() => {
    'use strict';

    const state = {
        initialized: false, deps: {}, dom: {}, policy: null, operations: [], workflows: [], audits: [],
        credentialAliases: [], runtimeApprovals: [], inspectorScope: '', inspectorLease: null,
        plan: null, planMessages: [], planScope: '', planBusy: false, planBusyAction: '', scopeBusy: false,
        planWorkspaceVisible: false, planRestoreRequestId: 0,
        catalogResults: [], catalogDigest: '', adoptionPreview: null,
        requestId: 0, refreshTimer: null,
    };
    const labels = {
        off: '只規劃', restricted: '安全模式', full_audit: '進階管理',
        pending: '等待核准', pending_second_approval: '等待第二次核准', approved: '已核准',
        executing: '執行中', completed: '已完成', rejected: '已拒絕', revoked: '已撤銷',
        failed: '失敗', execution_unknown: '執行結果不明', expired: '已過期',
    };

    const api = async (path, options = {}) => {
        const response = await state.deps.apiFetch(`${state.deps.apiBase || ''}${path}`, options);
        if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            const detail = payload?.detail?.error || payload?.detail || {};
            const error = new Error(detail?.message || `Request failed (${response.status})`);
            error.code = String(detail?.code || 'REQUEST_FAILED');
            error.status = response.status;
            error.recoverable = detail?.recoverable === true;
            throw error;
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
    const liveProjects = () => (state.deps.getProjects?.() || [])
        .filter(item => !item.archived && !item.archived_at);
    const liveSessionsFor = id => (state.deps.getSessions?.() || [])
        .filter(session => String(session.project_id) === String(id || '')
            && !session.archived && String(session.mode || 'chat') !== 'email');
    const canAutoProvisionScope = () => {
        const projects = liveProjects();
        if (!projectId()) return projects.length === 0;
        return !sessionId() && liveSessionsFor(projectId()).length === 0;
    };
    const planScopeKey = () => `${projectId()}::${sessionId() || 'no-session'}`;
    const query = value => encodeURIComponent(String(value || ''));
    const empty = text => node('div', 'workflow-empty', text);

    function workflowWorkspaceActive() {
        return state.initialized
            && document.getElementById('n8n-workflow-center')?.hidden === false;
    }

    function inspectorOwner(kind, id) {
        return `${kind}:${String(id || '').trim()}`;
    }

    function claimInspectorOwner(owner) {
        if (!workflowWorkspaceActive() || !owner) return null;
        const lease = window.workbenchRunInspector?.claimContentOwner?.(owner) || null;
        state.inspectorLease = lease;
        return lease;
    }

    function ownsInspector(lease = state.inspectorLease) {
        return !!lease
            && workflowWorkspaceActive()
            && window.workbenchRunInspector?.contentOwnerMatches?.(lease) === true;
    }

    function releaseInspectorContext() {
        state.inspectorScope = '';
        state.inspectorLease = null;
        if (state.dom?.inspectorExecution) {
            state.dom.inspectorExecution.hidden = true;
            state.dom.inspectorResults.hidden = true;
            state.dom.chatExecution.hidden = false;
            state.dom.chatResults.hidden = false;
            document.getElementById('output-floating-workspace')?.classList.remove('mail-inspector-active');
        }
        window.workbenchRunInspector?.claimContentOwner?.('chat');
    }

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

    const normalizedIssueList = value => listOf(value).map(item => {
        if (typeof item === 'string') return { code: '', message: item, severity: '', node: '', path: '' };
        if (!item || typeof item !== 'object') return null;
        return {
            code: String(item.code || '').slice(0, 128),
            message: plainText(item.message || item),
            severity: String(item.severity || '').slice(0, 32),
            node: String(item.node || '').slice(0, 255),
            path: String(item.path || '').slice(0, 255),
        };
    }).filter(item => item?.message || item?.code);

    const validationValue = value => {
        if (typeof value === 'string') return value.toLowerCase();
        if (!value || typeof value !== 'object') return '';
        if (value.valid === true) return 'ready';
        return String(value.status || value.state || '').toLowerCase();
    };

    function responseMessages(payload, source) {
        const messages = listOf(source.messages || payload.messages);
        return messages.map(message => ({
            role: ['user', 'human'].includes(String(message?.role || '').toLowerCase()) ? 'user' : 'agent',
            content: plainText(message),
        })).filter(message => message.content);
    }

    function normalizedArchitectureOptions(source) {
        const rawChoices = listOf(source.options || source.choices || source.questions);
        if (rawChoices.length < 2 || rawChoices.length > 3) return [];
        const choices = rawChoices.map(choice => {
            const label = plainText(choice);
            const architecture = choice?.architecture && typeof choice.architecture === 'object'
                ? choice.architecture : null;
            return {
                id: String(choice?.id || '').trim(),
                label,
                message: String(choice?.message || choice?.prompt || label).trim(),
                description: String(choice?.description || '').trim(),
                recommended: choice?.recommended === true,
                operation: String(choice?.operation || '').trim(),
                expectedResult: String(choice?.expected_result || '').trim(),
                risks: normalizedList(choice?.risks),
                permissions: normalizedList(choice?.permissions),
                architecture,
            };
        }).filter(choice => choice.id && choice.label && choice.message && choice.architecture);
        return choices.length === rawChoices.length ? choices : [];
    }

    function normalizePlanResponse(payload = {}) {
        const source = payload.plan && typeof payload.plan === 'object' ? payload.plan : payload;
        const risk = source.risk && typeof source.risk === 'object' ? source.risk : {};
        const materialization = source.materialization && typeof source.materialization === 'object'
            ? source.materialization
            : payload.materialization && typeof payload.materialization === 'object'
                ? payload.materialization
                : {};
        const assistant = plainText(source.assistant_message || source.response || source.reply || source.message || payload.assistant_message);
        const status = String(source.status || payload.status || '').toLowerCase();
        const digest = String(source.digest || source.plan_digest || payload.digest || payload.plan_digest || '').trim();
        const blockers = normalizedList(source.blockers || payload.blockers);
        const risks = normalizedList(source.risk_summary || source.risks || risk.warnings || risk.items || payload.risks);
        const graphPreview = source.graph_preview && typeof source.graph_preview === 'object'
            ? source.graph_preview
            : materialization.graph_preview && typeof materialization.graph_preview === 'object'
                ? materialization.graph_preview
                : null;
        const validationStatus = validationValue(source.validation_status || materialization.validation_status);
        const graphDigest = String(source.graph_digest || materialization.graph_digest || '').trim();
        const catalogDigest = String(source.catalog_digest || materialization.catalog_digest || '').trim();
        const selectedOptionId = String(source.selected_option_id || '').trim();
        const issues = normalizedIssueList(materialization.issues || source.issues);
        const questions = normalizedIssueList(materialization.questions || source.questions);
        const options = normalizedArchitectureOptions(source);
        const planSchema = String(source.plan_schema || '').trim();
        const provenanceSource = source.generation_provenance && typeof source.generation_provenance === 'object'
            ? source.generation_provenance : {};
        const structuredMode = String(provenanceSource.structured_mode || 'unknown').trim();
        const generationProvenance = {
            primaryModel: String(provenanceSource.primary_model || '').trim(),
            structuredMode: ['json_schema', 'guided_json', 'json_object', 'ollama_schema', 'prompt_only'].includes(structuredMode)
                ? structuredMode : 'unknown',
            formatRepaired: provenanceSource.format_repaired === true,
            repairModel: provenanceSource.format_repaired === true
                ? String(provenanceSource.repair_model || '').trim() : '',
            repairCount: Math.max(0, Math.min(1, Number(provenanceSource.repair_count || 0))),
        };
        if (status === 'architecture_ready' && options.length < 2) {
            blockers.push('規劃結果不完整，未收到 2–3 個有效架構；請重新規劃。');
        }
        if (source.id && planSchema !== 'workbench.n8n.two-stage.v1') {
            blockers.push('此規劃使用舊版契約，請重新規劃。');
        }
        blockers.forEach(blocker => {
            if (!risks.includes(blocker)) risks.push(blocker);
        });
        return {
            id: String(source.id || source.plan_id || payload.plan_id || '').trim(),
            projectId: String(source.project_id || '').trim(),
            sessionId: String(source.session_id || '').trim(),
            planSchema,
            digest,
            status,
            assistant,
            summary: plainText(source.summary || source.proposal_summary || payload.summary),
            blockers,
            risks,
            outcomes: normalizedList(source.expected_result || source.outcomes || source.possible_results || source.results || payload.outcomes),
            permissions: normalizedList(source.permission_requirements || source.permissions || source.required_permissions || payload.permissions),
            options,
            messages: responseMessages(payload, source),
            selectedOptionId,
            graphPreview,
            validationStatus,
            catalogDigest,
            graphDigest,
            graphDiff: materialization.diff && typeof materialization.diff === 'object' ? materialization.diff : {},
            issues,
            questions,
            generationProvenance,
            operationId: String(source.operation_id || '').trim(),
            createdAt: String(source.created_at || '').trim(),
            updatedAt: String(source.updated_at || '').trim(),
            expiresAt: String(source.expires_at || '').trim(),
            readyToMaterialize: blockers.length === 0 && Boolean(selectedOptionId) && ['selected', 'needs_input'].includes(status),
            readyToPropose: blockers.length === 0 && status === 'graph_ready'
                && validationStatus === 'ready' && /^[a-f0-9]{64}$/.test(graphDigest),
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

    const shortDigest = value => {
        const digest = String(value || '').trim();
        return /^[a-f0-9]{64}$/.test(digest) ? `${digest.slice(0, 12)}…${digest.slice(-6)}` : '—';
    };

    function graphBranchLabel(preview, edge) {
        const source = listOf(preview?.nodes).find(item => item?.name === edge?.from);
        const type = String(source?.type || '').toLowerCase();
        const index = Number(edge?.output_index || 0);
        if (type.endsWith('.if') || type === 'if') return index === 0 ? 'true' : index === 1 ? 'false' : `分支 ${index + 1}`;
        if (type.endsWith('.switch') || type === 'switch') return `分支 ${index + 1}`;
        return index > 0 ? `輸出 ${index + 1}` : '';
    }

    function graphNodeText(item) {
        const name = String(item?.name || '未命名節點');
        const type = String(item?.type || '未知類型').replace(/^n8n-nodes-base\./, '');
        const version = item?.type_version ?? item?.typeVersion;
        const aliases = normalizedList(item?.credential_aliases);
        return `${name} · ${type}${version != null ? ` v${version}` : ''}${aliases.length ? ` · Credential：${aliases.join('、')}` : ''}`;
    }

    function graphEdgeText(preview, edge) {
        const branch = graphBranchLabel(preview, edge);
        const ports = Number(edge?.input_index || 0) > 0 ? ` → 輸入 ${Number(edge.input_index) + 1}` : '';
        return `${String(edge?.from || '未知來源')} → ${String(edge?.to || '未知目標')}${branch ? `（${branch}${ports}）` : ports}`;
    }

    function issueText(item) {
        const location = [item?.node, item?.path].filter(Boolean).join(' · ');
        return `${item?.code ? `[${item.code}] ` : ''}${item?.message || '未提供說明'}${location ? `（${location}）` : ''}`;
    }

    function renderPlanGraph(plan) {
        const preview = plan?.graphPreview;
        const hasPreview = Boolean(preview && typeof preview === 'object');
        state.dom.planGraphPreview.hidden = !hasPreview && !plan?.issues?.length && !plan?.questions?.length;
        state.dom.planValidationStatus.textContent = plan?.validationStatus === 'ready' ? '通過' : plan?.validationStatus === 'needs_input' ? '需要補充' : plan?.validationStatus === 'blocked' ? '已阻擋' : '尚未驗證';
        state.dom.planNodeCount.textContent = String(Number(preview?.node_count || listOf(preview?.nodes).length || 0));
        state.dom.planEdgeCount.textContent = String(Number(preview?.edge_count || listOf(preview?.edges).length || 0));
        state.dom.planGraphNodes.replaceChildren(...(listOf(preview?.nodes).length
            ? listOf(preview.nodes).map(item => node('li', '', graphNodeText(item)))
            : [node('li', 'is-muted', '尚無可顯示的節點。')]));
        state.dom.planGraphEdges.replaceChildren(...(listOf(preview?.edges).length
            ? listOf(preview.edges).map(item => node('li', '', graphEdgeText(preview, item)))
            : [node('li', 'is-muted', '尚無可顯示的連線。')]));
        state.dom.planQuestionsWrap.hidden = !plan?.questions?.length;
        state.dom.planQuestions.replaceChildren(...listOf(plan?.questions).map(item => node('li', '', issueText(item))));
        const questionKeys = new Set(listOf(plan?.questions).map(item => `${item.code}|${item.message}|${item.node}|${item.path}`));
        const otherIssues = listOf(plan?.issues).filter(item => !questionKeys.has(`${item.code}|${item.message}|${item.node}|${item.path}`));
        state.dom.planIssuesWrap.hidden = !otherIssues.length;
        state.dom.planIssues.replaceChildren(...otherIssues.map(item => node('li', '', issueText(item))));
        state.dom.planCatalogDigest.textContent = shortDigest(plan?.catalogDigest);
        state.dom.planGraphDigest.textContent = shortDigest(plan?.graphDigest);
    }

    function renderPlanner() {
        if (!state.initialized) return;
        state.dom.planWorkspace.hidden = !state.planWorkspaceVisible;
        const hasProject = Boolean(projectId());
        const hasSession = Boolean(sessionId());
        const hasScope = hasProject && hasSession;
        const plan = state.plan;
        const provenance = plan?.generationProvenance;
        state.dom.planProvenance.hidden = !provenance?.primaryModel;
        if (provenance?.primaryModel) {
            state.dom.planPrimaryModel.textContent = `規劃模型：${provenance.primaryModel}`;
            const modeLabels = {
                json_schema: 'NVIDIA JSON Schema', guided_json: 'NVIDIA Guided JSON',
                json_object: 'JSON Object', ollama_schema: 'Ollama JSON Schema', prompt_only: 'Prompt contract',
            };
            state.dom.planStructuredMode.textContent = `結構：約束 ${modeLabels[provenance.structuredMode] || '未知'}`;
            state.dom.planRepairModel.hidden = !provenance.formatRepaired;
            state.dom.planRepairModel.textContent = provenance.formatRepaired
                ? `格式修復：${provenance.repairModel || '本機模型'}` : '';
        }
        const messages = state.planMessages.length ? state.planMessages : [{
            role: 'agent',
            content: '告訴我你希望自動完成什麼。我會先整理做法；真正寫入、寄送或刪除前才會請你確認。',
        }];
        state.dom.planMessages.replaceChildren(...messages.map(planMessageNode));
        state.dom.planMessages.setAttribute('aria-busy', state.planBusy || state.scopeBusy ? 'true' : 'false');
        state.dom.planMessages.scrollTop = state.dom.planMessages.scrollHeight;

        const choices = ['selected', 'needs_input', 'graph_ready', 'proposed'].includes(plan?.status) ? [] : plan?.options || [];
        state.dom.planOptions.replaceChildren(...choices.map(choice => {
            const button = node('button', 'n8n-plan-option');
            button.type = 'button';
            button.disabled = state.planBusy;
            button.dataset.optionId = choice.id;
            const heading = node('span', 'n8n-plan-option-title', choice.label);
            if (choice.recommended) heading.append(node('span', 'n8n-plan-option-badge', '推薦'));
            button.append(heading);
            if (choice.description) button.append(node('span', 'n8n-plan-option-description', choice.description));
            const steps = listOf(choice.architecture?.steps)
                .map(step => String(step?.capability || step?.purpose || '').trim()).filter(Boolean);
            if (steps.length) button.append(node('span', 'n8n-plan-option-flow', steps.join(' → ')));
            if (choice.expectedResult) button.append(node('span', 'n8n-plan-option-result', choice.expectedResult));
            if (choice.risks.length) button.append(node('span', 'n8n-plan-option-risk', `風險：${choice.risks.join('；')}`));
            if (choice.permissions.length) button.append(node('span', 'n8n-plan-option-permission', `權限：${choice.permissions.join('；')}`));
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

        const graphStageVisible = Boolean(plan?.id && plan?.digest && (plan?.selectedOptionId || plan?.graphPreview || plan?.status === 'needs_input'));
        state.dom.planGraphStage.hidden = !graphStageVisible;
        state.dom.planMaterialize.hidden = !plan?.readyToMaterialize;
        state.dom.planMaterialize.disabled = state.planBusy || !plan?.readyToMaterialize;
        // The next legacy assignment is retained for compatibility; this label
        // is set again after graph copy is rendered below.
        state.dom.planMaterialize.textContent = plan?.status === 'needs_input' ? '重新驗證節點圖' : '驗證並產生節點圖';
        state.dom.planGraphMessage.textContent = plan?.status === 'needs_input'
            ? '節點圖尚缺必要資訊。請依下方問題在對話中補充，再重新選定架構並驗證。'
            : plan?.status === 'graph_ready'
                ? '伺服器已配對並驗證節點、參數與連線；目前尚未建立 n8n Workflow。'
                : '選定架構後，由伺服器配對節點、參數與連線；這一步不會建立或執行 n8n Workflow。';
        renderPlanGraph(plan);
        state.dom.planMaterialize.textContent = '產生並驗證唯一節點圖';

        const proposalReady = Boolean(plan?.readyToPropose && plan?.id && plan?.digest);
        state.dom.planProposal.hidden = !proposalReady;
        state.dom.planProposalSummary.textContent = plan?.summary || '這一步只會建立可執行的待核准提案；核准後 Broker 才會依提案內容實際操作 n8n。';
        state.dom.planPropose.disabled = state.planBusy || !proposalReady || !state.dom.planProposalAck.checked;
        const acceptsFollowup = Boolean(plan && ['needs_input', 'selected', 'blocked', 'proposal_failed'].includes(plan.status));
        state.dom.planForm.hidden = !acceptsFollowup;
        const scopeCanBePrepared = canAutoProvisionScope();
        state.dom.planInput.disabled = state.planBusy || state.scopeBusy || (!hasScope && !scopeCanBePrepared);
        state.dom.planSend.disabled = state.planBusy || state.scopeBusy || (!hasScope && !scopeCanBePrepared);
        state.dom.planInput.placeholder = plan?.status === 'needs_input'
            ? '補充助理詢問的必要資訊…' : '補充或調整這份提案…';
        state.dom.planSend.textContent = '送出補充';
        state.dom.planReset.disabled = state.planBusy || state.scopeBusy || (!plan && state.planMessages.length === 0);
        const blocked = Boolean(plan?.blockers?.length || plan?.status === 'blocked');
        const busyLabel = state.planBusyAction === 'materializing' ? '正在產生唯一節點圖'
            : state.planBusyAction === 'proposing' ? '正在建立待核准提案'
                : state.planBusyAction === 'selecting' ? '正在保存架構選擇' : '正在產生架構選項';
        syncPlanScopeDisclosure();
        state.dom.planState.textContent = state.scopeBusy ? '正在準備個人工作區' : !hasProject ? (scopeCanBePrepared ? '可以直接開始' : '需要選擇 Project') : !hasSession ? (scopeCanBePrepared ? '可以直接開始' : '需要選擇對話') : state.planBusy ? busyLabel : blocked ? '前置條件未就緒' : plan?.status === 'needs_input' ? '需要補充資訊' : proposalReady ? '節點圖已驗證' : plan?.readyToMaterialize ? '等待驗證節點圖' : plan ? '規劃中' : '可以開始描述需求';
        state.dom.planState.className = `workflow-status-pill ${blocked || plan?.status === 'needs_input' ? 'is-error' : proposalReady || plan?.readyToMaterialize ? 'is-warning' : plan ? 'is-success' : ''}`;
        state.deps.createIcons?.();
    }

    function resetPlanner({ announce = false, keepVisible = false } = {}) {
        state.plan = null;
        state.planMessages = [];
        state.planScope = '';
        state.planBusy = false;
        state.planBusyAction = '';
        state.planWorkspaceVisible = keepVisible;
        state.dom.planInput.value = '';
        state.dom.planProposalAck.checked = false;
        if (announce) appendPlanMessage('system', '已清除上一份規劃；尚未建立或執行任何 n8n 操作。');
        renderPlanner();
    }

    function handlePlanError(error) {
        const restartCodes = new Set([
            'N8N_PLAN_STALE', 'N8N_PLAN_EXPIRED', 'N8N_PLAN_SCHEMA_STALE',
            'N8N_PLAN_GRAPH_STALE', 'N8N_PLAN_MODEL_STALE',
        ]);
        if (!restartCodes.has(String(error?.code || ''))) return false;
        resetPlanner({ announce: true, keepVisible: true });
        state.deps.showToast?.('規劃快照已失效，請重新規劃。', 'warning');
        return true;
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
        if (!content || state.planBusy || state.scopeBusy) return;
        if ((!projectId() || !sessionId()) && !await ensurePersonalScope()) return;
        const id = projectId();
        if (!id) return state.deps.showToast?.('請先選擇 Project。', 'warning');
        if (!sessionId()) return state.deps.showToast?.('請先選擇一個屬於此 Project 的 Session。', 'warning');
        if (state.planScope && state.planScope !== planScopeKey()) resetPlanner();
        if (state.plan?.id && !state.plan.digest) {
            resetPlanner();
            selectedOptionId = '';
            state.deps.showToast?.('舊計畫缺少版本摘要，已安全重開新計畫。', 'warning');
        }

        appendPlanMessage('user', content);
        state.planBusy = true;
        state.planBusyAction = selectedOptionId ? 'selecting' : 'planning';
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
            return { ok: true, plan: state.plan };
        } catch (error) {
            if (handlePlanError(error)) return { ok: false, error };
            appendPlanMessage('system', `規劃未送出：${error.message}`);
            state.deps.showToast?.(error.message, 'error');
            return { ok: false, error };
        } finally {
            state.planBusy = false;
            state.planBusyAction = '';
            renderPlanner();
        }
    }

    async function submitPlanMessage(event) {
        event.preventDefault();
        await sendPlanMessage(state.dom.planInput.value);
    }

    async function startPlanFromChat(options = {}) {
        if (!state.initialized) throw new Error('n8n 操作助理尚未初始化。');
        const content = String(options.message || '').trim();
        if (!content) throw new Error('缺少要規劃的 n8n 操作需求。');

        state.planRestoreRequestId += 1;
        resetPlanner();
        state.planWorkspaceVisible = true;
        renderProjects();
        renderPlanner();

        const requestedSession = String(options.sessionId || '').trim();
        // activeProjectId can briefly be empty while the sidebar is restoring.
        // Recover only from the already-loaded Session record; the server still
        // verifies that this Session belongs to the Project before creating a
        // plan, so this is convenience rather than an authority decision.
        const requestedSessionRecord = (state.deps.getSessions?.() || [])
            .find(session => String(session?.id || '') === requestedSession);
        const requestedProject = String(
            options.projectId || requestedSessionRecord?.project_id || state.dom.project.value || ''
        ).trim();
        const projectAvailable = requestedProject
            && [...state.dom.project.options].some(option => option.value === requestedProject);
        if (projectAvailable) {
            state.dom.project.value = requestedProject;
            renderPlanSessions();
        } else {
            state.dom.project.value = '';
            renderPlanSessions();
        }
        const preferredSession = requestedSession || state.dom.planSession.value;
        let sessionAvailable = preferredSession
            && [...state.dom.planSession.options].some(option => option.value === preferredSession);
        state.dom.planSession.value = sessionAvailable ? preferredSession : '';

        state.dom.planInput.value = content;
        const blockers = [];
        if (options.hasAttachments === true) {
            blockers.push('n8n 操作規劃不會接收聊天圖片或附件；請移除附件後再送出。');
        }
        if (!blockers.length && (!projectAvailable || !sessionAvailable) && canAutoProvisionScope()) {
            await ensurePersonalScope();
            sessionAvailable = Boolean(sessionId());
        }
        if (!projectId()) blockers.push('請先在左側選擇一個未封存的專案。');
        if (!sessionAvailable) blockers.push('請先回到該專案的一般聊天再提出需求。');
        if (blockers.length) {
            state.planMessages = [
                { role: 'agent', content: '我已辨識這是 n8n 操作要求，並轉到受治理的操作助理。' },
                { role: 'system', content: `尚未送出規劃，也未操作 n8n：${blockers.join(' ')}` },
            ];
            renderPlanner();
            return { status: 'blocked', message: blockers.join(' '), plan: null };
        }

        const sent = await sendPlanMessage(content);
        if (!sent?.ok) {
            return {
                status: 'blocked',
                message: sent?.error?.message || 'n8n 操作規劃未送出，且未執行任何操作。',
                plan: state.plan ? { ...state.plan } : null,
            };
        }
        if (state.plan?.blockers?.length) {
            return {
                status: 'blocked',
                message: state.plan.blockers.join(' '),
                planId: state.plan.id || '',
                digest: state.plan.digest || '',
                plan: { ...state.plan },
            };
        }
        return {
            status: state.plan?.status || 'planning',
            planId: state.plan?.id || '',
            digest: state.plan?.digest || '',
            plan: state.plan ? { ...state.plan } : null,
        };
    }

    async function restorePlanForScope(options = {}) {
        if (!state.initialized) return { status: 'unavailable', plan: null };
        const requestedProject = String(options.projectId || '').trim();
        const requestedSession = String(options.sessionId || '').trim();
        const requestId = ++state.planRestoreRequestId;
        resetPlanner();
        renderProjects();

        const projectAvailable = requestedProject
            && [...state.dom.project.options].some(option => option.value === requestedProject);
        if (!projectAvailable) return { status: 'scope_unavailable', plan: null };
        state.dom.project.value = requestedProject;
        renderPlanSessions();
        const sessionAvailable = requestedSession
            && [...state.dom.planSession.options].some(option => option.value === requestedSession);
        if (!sessionAvailable) return { status: 'scope_unavailable', plan: null };
        state.dom.planSession.value = requestedSession;

        try {
            const payload = await api(
                `/api/integrations/n8n/plans/current?project_id=${query(requestedProject)}&session_id=${query(requestedSession)}`,
                { cache: 'no-store' }
            );
            const liveProject = String(state.deps.getActiveProjectId?.() || '').trim();
            const liveSession = String(state.deps.getCurrentSessionId?.() || '').trim();
            if (requestId !== state.planRestoreRequestId
                || liveProject !== requestedProject || liveSession !== requestedSession) {
                return { status: 'stale', plan: null };
            }
            if (!payload?.plan) {
                resetPlanner();
                return { status: 'empty', plan: null };
            }
            applyPlanResponse(payload.plan);
            state.planWorkspaceVisible = true;
            renderPlanner();
            return { status: 'restored', plan: { ...state.plan } };
        } catch (error) {
            if (requestId === state.planRestoreRequestId) resetPlanner();
            return {
                status: 'unavailable',
                plan: null,
                message: error?.message || '無法恢復 n8n 提案。',
            };
        }
    }

    async function materializePlan() {
        const plan = state.plan;
        if (!plan?.id || !plan.digest || !plan.readyToMaterialize || state.planBusy) return;
        if (state.planScope !== planScopeKey()) {
            resetPlanner();
            return state.deps.showToast?.('Project 或 Session 已變更，請重新規劃。', 'warning');
        }
        state.planBusy = true;
        state.planBusyAction = 'materializing';
        renderPlanner();
        try {
            const payload = await api(`/api/integrations/n8n/plans/${query(plan.id)}/materialize`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project_id: projectId(), session_id: sessionId(), expected_digest: plan.digest,
                }),
            });
            applyPlanResponse(payload);
            if (state.plan?.status === 'graph_ready') {
                appendPlanMessage('system', '節點、參數與連線已由伺服器驗證；目前尚未建立或執行 n8n Workflow。');
                state.deps.showToast?.('節點圖驗證通過，可以建立待核准提案。', 'success');
            } else if (state.plan?.status === 'needs_input') {
                appendPlanMessage('system', '節點圖仍缺必要資訊。請依「需要你補充」的問題回覆；尚未建立 n8n Workflow。');
                state.deps.showToast?.('節點圖需要補充資訊。', 'warning');
            } else {
                appendPlanMessage('system', '節點圖未通過安全驗證，未建立 n8n Workflow。');
                state.deps.showToast?.('節點圖已被安全阻擋。', 'error');
            }
        } catch (error) {
            if (handlePlanError(error)) return;
            appendPlanMessage('system', `無法驗證節點圖：${error.message}`);
            state.deps.showToast?.(error.message, 'error');
        } finally {
            state.planBusy = false;
            state.planBusyAction = '';
            renderPlanner();
        }
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
        state.planBusyAction = 'proposing';
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
            if (handlePlanError(error)) return;
            appendPlanMessage('system', `無法建立提案：${error.message}`);
            state.deps.showToast?.(error.message, 'error');
        } finally {
            state.planBusy = false;
            state.planBusyAction = '';
            renderPlanner();
        }
    }

    async function ensurePersonalScope() {
        if (projectId() && sessionId()) return true;
        if (!canAutoProvisionScope()) {
            syncPlanScopeDisclosure();
            state.deps.showToast?.('有多個工作範圍可用，請選擇這次要使用的 Project 與對話。', 'warning');
            return false;
        }
        state.scopeBusy = true;
        renderPlanner();
        try {
            let selectedProjectId = projectId();
            if (!selectedProjectId) {
                const result = await api('/api/projects', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        name: '個人自動化',
                        root_kind: 'managed',
                        permission_mode: 'read_only',
                    }),
                });
                selectedProjectId = String(result.project?.id || '');
                if (!selectedProjectId) throw new Error('個人工作區建立後未回報 Project ID。');
                await state.deps.refreshWorkspaceScope?.();
                renderProjects();
                state.dom.project.value = selectedProjectId;
                renderPlanSessions();
            }
            if (!sessionId()) {
                const result = await api('/api/sessions', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ title: 'n8n 自動化', project_id: selectedProjectId }),
                });
                const createdSessionId = String(result.session_id || result.id || '');
                if (!createdSessionId) throw new Error('個人工作區建立後未回報 Session ID。');
                await state.deps.refreshWorkspaceScope?.();
                renderProjects();
                state.dom.project.value = selectedProjectId;
                renderPlanSessions();
                state.dom.planSession.value = createdSessionId;
            }
            syncPlanScopeDisclosure();
            return Boolean(projectId() && sessionId());
        } catch (error) {
            appendPlanMessage('system', `無法準備個人工作區：${error.message}`);
            state.deps.showToast?.(`無法準備個人工作區：${error.message}`, 'error');
            return false;
        } finally {
            state.scopeBusy = false;
            renderPlanner();
        }
    }

    function syncPlanScopeDisclosure() {
        if (!state.dom?.planScopeSummary) return;
        const projectOption = state.dom.project?.selectedOptions?.[0];
        const sessionOption = state.dom.planSession?.selectedOptions?.[0];
        const hasProject = Boolean(projectId());
        const hasSession = Boolean(sessionId());
        state.dom.planScopeSummary.textContent = hasProject && hasSession
            ? `自動沿用：${projectOption?.textContent || '目前專案'} · ${sessionOption?.textContent || '目前對話'}`
            : canAutoProvisionScope() ? '首次送出時會自動準備個人工作區'
                : !hasProject ? '請先在左側選擇專案' : '請先回到聊天選擇一個對話';
    }

    function renderProjects() {
        if (!state.initialized || !state.dom?.project) return;
        const selected = String(state.dom.project.value || '');
        state.dom.project.replaceChildren(new Option('請選擇專案', ''));
        const projects = liveProjects();
        projects.forEach(project => {
            state.dom.project.appendChild(new Option(project.name || project.id, project.id));
        });
        const active = String(state.deps.getActiveProjectId?.() || '');
        const requested = active || selected;
        const preferred = projects.some(project => String(project.id) === requested)
            ? requested
            : projects.length === 1 ? String(projects[0].id) : '';
        state.dom.project.value = preferred;
        renderPlanSessions();
    }

    function renderPlanSessions() {
        if (!state.initialized || !state.dom?.planSession) return;
        const selected = state.dom.planSession.value;
        const current = String(state.deps.getCurrentSessionId?.() || '');
        const project = projectId();
        state.dom.planSession.replaceChildren(new Option('請先選擇此專案的對話', ''));
        const sessions = liveSessionsFor(project);
        sessions.forEach(session => state.dom.planSession.appendChild(new Option(session.title || session.id, session.id)));
        const requested = String(current || selected || '');
        const preferred = sessions.some(session => String(session.id) === requested)
            ? requested
            : sessions.length === 1 ? String(sessions[0].id) : '';
        state.dom.planSession.value = preferred;
        syncPlanScopeDisclosure();
    }

    function renderPolicy() {
        const policy = state.policy;
        if (!policy) return;
        state.dom.mode.value = policy.mode || 'restricted';
        state.dom.duration.value = policy.elevation_policy || 'smart';
        state.dom.state.textContent = labels[policy.mode] || policy.mode;
        state.dom.state.className = `workflow-status-pill ${policy.mode === 'full_audit' ? 'is-warning' : policy.mode === 'off' ? '' : 'is-success'}`;
        const advanced = state.dom.mode.value === 'full_audit';
        state.dom.duration.disabled = !advanced;
        state.dom.duration.closest('label').hidden = !advanced;
        state.dom.ack.closest('label').hidden = !advanced;
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

    function credentialAliasRow(credential) {
        const row = node('article', 'mail-run-row n8n-credential-alias-row');
        const main = node('div', 'mail-run-main');
        main.append(node('strong', '', credential.alias || '未命名別名'));
        main.append(node('div', 'run-inspector-meta', `${credential.credential_type || '未知類型'} · ${credential.display_name || '未命名 Credential'} · revision ${credential.revision || 1}`));
        const controls = node('div', 'n8n-credential-alias-actions');
        const status = node('span', `workflow-status-pill ${credential.status === 'ready' ? 'is-success' : credential.status === 'revoked' ? 'is-error' : 'is-warning'}`, credential.status || 'unknown');
        const refresh = node('button', 'btn btn-secondary', '重新驗證');
        refresh.type = 'button';
        refresh.disabled = credential.status === 'revoked';
        refresh.addEventListener('click', () => void refreshCredentialAlias(credential.alias));
        const revoke = node('button', 'btn btn-danger', '撤銷');
        revoke.type = 'button';
        revoke.disabled = credential.status === 'revoked';
        revoke.addEventListener('click', () => void revokeCredentialAlias(credential.alias));
        controls.append(status, refresh, revoke);
        row.append(main, controls);
        return row;
    }

    function runtimeApprovalRow(approval) {
        const row = node('button', 'mail-run-row n8n-operation-row');
        row.type = 'button';
        const main = node('span', 'mail-run-main');
        main.append(node('strong', '', `${approval.action || '外部動作'} · ${approval.target || '未提供目標'}`));
        main.append(node('span', 'run-inspector-meta', `${approval.workflow_id || '未知 Workflow'} · node ${approval.node_id || '—'} · ${shortDigest(approval.request_digest)}`));
        const pending = approval.status === 'pending';
        row.append(main, node('span', `workflow-status-pill ${pending ? 'is-warning' : approval.status === 'approved' || approval.status === 'approved_by_grant' ? 'is-success' : ['rejected', 'expired', 'revoked'].includes(approval.status) ? 'is-error' : ''}`, approval.status || 'unknown'));
        row.addEventListener('click', () => showRuntimeApproval(approval));
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
        state.dom.credentialAliases.replaceChildren(...(state.credentialAliases.length
            ? state.credentialAliases.map(credentialAliasRow)
            : [empty('尚無 Credential 別名。')]));
        state.dom.credentialAliasCount.textContent = `${state.credentialAliases.length} 筆`;
        state.dom.runtimeApprovals.replaceChildren(...(state.runtimeApprovals.length
            ? state.runtimeApprovals.map(runtimeApprovalRow)
            : [empty('尚無執行時核准。')]));
        state.dom.runtimeApprovalCount.textContent = `${state.runtimeApprovals.filter(item => item.status === 'pending').length} 待處理`;
        state.deps.createIcons?.();
    }

    function kv(label, value) {
        const row = node('div', 'run-inspector-kv');
        row.append(node('span', '', label), node('strong', '', value || '—'));
        return row;
    }

    function safeLoopbackEditorUrl(value) {
        try {
            const parsed = new URL(String(value || ''));
            const loopback = ['127.0.0.1', 'localhost', '[::1]'].includes(parsed.hostname.toLowerCase());
            if (parsed.protocol !== 'http:' || !loopback || parsed.port !== '5678') return '';
            if (!/^\/workflow\/[A-Za-z0-9_-]+\/?$/.test(parsed.pathname)) return '';
            if (parsed.username || parsed.password || parsed.search || parsed.hash) return '';
            return parsed.href;
        } catch (_error) {
            return '';
        }
    }

    function diffListSection(title, values, formatter) {
        const section = node('section', 'n8n-operation-diff-section');
        section.append(node('h5', '', `${title}（${values.length}）`));
        const list = node('ul', 'n8n-operation-diff-list');
        list.append(...(values.length ? values.map(value => node('li', '', formatter(value))) : [node('li', 'is-muted', '無變更')]));
        section.appendChild(list);
        return section;
    }

    function nodeFactText(item) {
        const fact = item?.after || item?.before || item || {};
        const base = `${String(fact.name || item?.name || '未命名節點')} · ${String(fact.type || '未知類型').replace(/^n8n-nodes-base\./, '')}`;
        const parameterKeys = normalizedList(fact.parameter_keys);
        return `${base}${parameterKeys.length ? ` · 參數欄位：${parameterKeys.join('、')}` : ''}${fact.parameter_digest ? ` · 參數摘要 ${shortDigest(fact.parameter_digest)}` : ''}`;
    }

    function changedNodeText(item) {
        if (item?.before || item?.after) {
            const before = item.before || {};
            const after = item.after || {};
            const parts = [];
            if (before.name !== after.name) parts.push(`名稱 ${before.name || '—'} → ${after.name || '—'}`);
            if (before.type !== after.type) parts.push(`類型 ${before.type || '—'} → ${after.type || '—'}`);
            if (before.parameter_digest !== after.parameter_digest) {
                parts.push(`參數欄位 ${normalizedList(after.parameter_keys).join('、') || '無'}；摘要 ${shortDigest(before.parameter_digest)} → ${shortDigest(after.parameter_digest)}`);
            }
            return `${String(after.name || before.name || '未命名節點')}：${parts.join('；') || '節點摘要已變更'}`;
        }
        const changes = item?.changes && typeof item.changes === 'object' ? item.changes : {};
        const labels = Object.keys(changes).map(field => field === 'parameters' ? '參數摘要' : field === 'credential_aliases' ? 'Credential 別名' : field);
        return `${String(item?.name || '未命名節點')}：${labels.join('、') || '節點摘要已變更'}`;
    }

    function renderAuthoritativeDiff(operation) {
        const diff = operation?.diff && typeof operation.diff === 'object' ? operation.diff : {};
        const nodeChanges = diff.nodes && typeof diff.nodes === 'object' ? diff.nodes : {};
        const connectionChanges = diff.connections && typeof diff.connections === 'object' ? diff.connections : {};
        const container = node('div', 'n8n-operation-diff');
        const source = node('p', 'run-inspector-meta', diff.source === 'server' ? '來源：伺服器權威快照' : '來源：伺服器鎖定的編譯結果');
        container.appendChild(source);
        const before = diff.before && typeof diff.before === 'object' ? diff.before : null;
        const after = diff.after && typeof diff.after === 'object' ? diff.after : null;
        container.append(kv('Workflow 狀態', `${before?.active ? '啟用' : before ? '停用' : '不存在'} → ${after?.active ? '啟用' : after ? '停用' : '刪除'}`));
        container.append(
            diffListSection('新增節點', listOf(nodeChanges.added), nodeFactText),
            diffListSection('刪除節點', listOf(nodeChanges.removed), nodeFactText),
            diffListSection('變更節點／參數', listOf(nodeChanges.changed), changedNodeText),
            diffListSection('新增連線', listOf(connectionChanges.added), item => graphEdgeText(operation.graph_preview || operation.graphPreview, item)),
            diffListSection('刪除連線', listOf(connectionChanges.removed), item => graphEdgeText(operation.graph_preview || operation.graphPreview, item)),
        );
        const targetChange = diff.external_targets && typeof diff.external_targets === 'object' ? diff.external_targets : {};
        const credentialChange = diff.credential_aliases && typeof diff.credential_aliases === 'object' ? diff.credential_aliases : {};
        container.append(
            kv('外部目標（變更後）', normalizedList(targetChange.after).join('、') || '無'),
            kv('Credential 別名（變更後）', normalizedList(credentialChange.after).join('、') || '無'),
            kv('Catalog digest', shortDigest(operation.catalog_digest || operation.catalogDigest)),
            kv('Graph digest', shortDigest(operation.graph_digest || operation.graphDigest)),
            kv('Base digest', shortDigest(operation.base_digest || operation.baseDigest)),
        );
        return container;
    }

    function renderSafeOperationResult(operation) {
        const result = operation?.result && typeof operation.result === 'object' ? operation.result : {};
        const fragment = document.createDocumentFragment();
        fragment.append(
            kv('稽核 digest', operation.digest),
            kv('錯誤碼', operation.error_code),
            kv('Workflow ID', result.id || operation.workflow_id),
            kv('Graph digest', shortDigest(result.graph_digest || operation.graph_digest)),
            kv('狀態', result.active === true ? '已啟用' : operation.status === 'completed' ? '已建立未啟用草稿' : '尚無結果'),
        );
        const editorUrl = safeLoopbackEditorUrl(result.editor_url);
        if (editorUrl) {
            const actions = node('div', 'run-inspector-actions');
            const open = node('button', 'run-inspector-button primary', '在 n8n 畫布檢視');
            open.type = 'button';
            open.addEventListener('click', () => window.open(editorUrl, '_blank', 'noopener,noreferrer'));
            actions.appendChild(open);
            fragment.appendChild(actions);
        }
        return fragment;
    }

    function showOperation(operation, { lease = null } = {}) {
        const scopedProject = projectId();
        if (!workflowWorkspaceActive() || !scopedProject || operation?.project_id !== scopedProject) return;
        const owner = inspectorOwner('operation', operation.id);
        const activeLease = lease || claimInspectorOwner(owner);
        if (!activeLease || activeLease.owner !== owner || !ownsInspector(activeLease)) return;
        state.inspectorLease = activeLease;
        state.inspectorScope = `operation:${operation.project_id || projectId()}`;
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
        section.append(node('h4', '', '伺服器權威 Before／After Diff'));
        section.append(node('p', 'run-inspector-meta', '以下只顯示伺服器鎖定的操作快照：節點、參數摘要、連線、目標與 Credential 別名；Agent 對話不是核准依據。'));
        if (operation.graph_preview && typeof operation.graph_preview === 'object') {
            section.append(diffListSection('編譯後節點', listOf(operation.graph_preview.nodes), graphNodeText));
            section.append(diffListSection('編譯後連線與分支', listOf(operation.graph_preview.edges), item => graphEdgeText(operation.graph_preview, item)));
        }
        section.append(renderAuthoritativeDiff(operation));
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
        state.dom.inspectorResults.replaceChildren(renderSafeOperationResult(operation));
        window.workbenchRunInspector?.selectTab?.('execution');
    }

    function showRuntimeApproval(approval, { lease = null } = {}) {
        const scopedProject = projectId();
        if (!workflowWorkspaceActive() || !scopedProject || approval.project_id !== scopedProject) return;
        const owner = inspectorOwner('runtime', approval.approval_id);
        const activeLease = lease || claimInspectorOwner(owner);
        if (!activeLease || activeLease.owner !== owner || !ownsInspector(activeLease)) return;
        state.inspectorLease = activeLease;
        state.inspectorScope = `runtime:${scopedProject}`;
        state.dom.chatExecution.hidden = true;
        state.dom.chatResults.hidden = true;
        state.dom.inspectorExecution.hidden = false;
        state.dom.inspectorResults.hidden = false;
        document.getElementById('output-floating-workspace')?.classList.add('mail-inspector-active');

        const section = node('section', 'run-inspector-section n8n-runtime-approval-inspector');
        section.append(node('h3', '', 'n8n 執行時核准'));
        section.append(
            kv('動作', approval.action),
            kv('Workflow ID', approval.workflow_id),
            kv('Workflow revision', approval.workflow_revision),
            kv('節點 ID', approval.node_id),
            kv('Credential 別名', approval.credential_alias),
            kv('外部目標類型', approval.target_kind),
            kv('外部目標', approval.target),
            kv('狀態', approval.status),
            kv('操作 digest', approval.request_digest),
        );
        const warning = node('div', 'workflow-risk-callout');
        warning.append(node('strong', '', '執行後可能改變外部系統'));
        const risks = node('ul');
        risks.append(
            node('li', '', '核准只對上方精確 Workflow revision、節點、Credential 別名、目標、動作與 digest 有效。'),
            node('li', '', '修改 Workflow、權限降級、n8n 停止或 Workbench 重啟會撤銷尚未使用的授權。'),
            node('li', '', 'Credential Secret 與 n8n Credential ID 不會顯示，也不會交給 Agent。'),
        );
        warning.appendChild(risks);
        section.appendChild(warning);

        if (approval.status === 'pending') {
            const duration = node('input', 'n8n-runtime-duration-input');
            duration.type = 'number';
            duration.min = '0';
            duration.max = '60';
            duration.step = '1';
            duration.value = '0';
            duration.inputMode = 'numeric';
            const fullAudit = state.policy?.mode === 'full_audit';
            duration.disabled = !fullAudit;
            const durationLabel = node('label', 'n8n-runtime-duration');
            durationLabel.append(node('span', '', '限時授權（分鐘）'), duration);
            durationLabel.append(node('small', '', fullAudit
                ? '預設 0 代表只核准本次；1–60 會允許相同 revision、節點、Credential 別名、目標與動作在期限內重用，風險較高。'
                : '限制權限模式固定為 0：每個外部動作都必須逐次核准。'));
            section.appendChild(durationLabel);
            const actions = node('div', 'run-inspector-actions');
            const reject = node('button', 'run-inspector-button secondary', '拒絕');
            reject.type = 'button';
            const approve = node('button', 'run-inspector-button primary', '核准本次動作');
            approve.type = 'button';
            reject.addEventListener('click', () => void decideRuntimeApproval(approval, false, 0));
            approve.addEventListener('click', () => {
                const minutes = fullAudit ? Number(duration.value || 0) : 0;
                if (!Number.isInteger(minutes) || minutes < 0 || minutes > 60) {
                    state.deps.showToast?.('限時授權必須是 0 到 60 的整數分鐘。', 'warning');
                    return;
                }
                void decideRuntimeApproval(approval, true, minutes);
            });
            actions.append(reject, approve);
            section.appendChild(actions);
        }
        state.dom.inspectorExecution.replaceChildren(section);
        state.dom.inspectorResults.replaceChildren(
            kv('有效期限', approval.expires_at ? new Date(approval.expires_at).toLocaleString() : '—'),
            kv('核准方式', approval.grant_id ? '由限時授權核准' : '逐次人工核准'),
        );
        window.workbenchRunInspector?.selectTab?.('execution');
    }

    async function decideRuntimeApproval(approval, approved, durationMinutes = 0) {
        const scopedProject = projectId();
        if (!scopedProject || approval.project_id !== scopedProject || state.inspectorScope !== `runtime:${scopedProject}`) return;
        const lease = state.inspectorLease;
        if (!ownsInspector(lease)) return;
        try {
            const updated = await api(`/api/integrations/n8n/runtime-approvals/${query(approval.approval_id)}/${approved ? 'approve' : 'reject'}`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project_id: scopedProject,
                    expected_digest: approval.request_digest,
                    duration_minutes: approved && state.policy?.mode === 'full_audit' ? durationMinutes : 0,
                }),
            });
            if (projectId() !== scopedProject) return;
            state.deps.showToast?.(approved ? '執行時核准已處理。' : '執行時動作已拒絕。', 'success');
            await refreshAll();
            if (projectId() === scopedProject && ownsInspector(lease)) {
                showRuntimeApproval(updated, { lease });
            }
        } catch (error) {
            if (projectId() === scopedProject) state.deps.showToast?.(error.message, 'error');
        }
    }

    async function decide(operation, approved) {
        const scopedProject = projectId();
        const lease = state.inspectorLease;
        if (operation?.project_id !== scopedProject || !ownsInspector(lease)) return;
        let confirmation = null;
        if (approved && operation.risk?.irreversible) {
            confirmation = window.prompt(`此操作可能無法復原。請輸入「${operation.workflow_name || operation.workflow_id}」確認：`);
            if (confirmation == null) return;
        }
        try {
            const updated = await api(`/api/integrations/n8n/operation-requests/${query(operation.id)}/${approved ? 'approve' : 'reject'}`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project_id: operation.project_id,
                    session_id: operation.session_id || null,
                    expected_digest: operation.digest,
                    confirmation,
                }),
            });
            state.deps.showToast?.(approved ? '核准已處理。' : '操作已拒絕。', 'success');
            await refreshAll();
            if (projectId() === scopedProject && ownsInspector(lease)) {
                showOperation(updated, { lease });
            }
        } catch (error) { state.deps.showToast?.(error.message, 'error'); }
    }

    function renderCatalogResults() {
        const rows = state.catalogResults.map(item => {
            const article = node('article', 'n8n-node-catalog-row');
            const heading = node('strong', '', String(item.display_name || item.displayName || item.name || item.type || '未命名節點'));
            const type = String(item.type || item.name || '').replace(/^n8n-nodes-base\./, '');
            const versions = listOf(item.versions);
            const latest = item.latest_version ?? item.latestVersion ?? versions.at(-1);
            article.append(heading, node('span', 'run-inspector-meta', `${type}${latest != null ? ` · v${latest}` : ''}`));
            const description = plainText(item.description);
            if (description) article.append(node('p', '', description));
            const credentialTypes = normalizedList(item.credential_types || item.credentials);
            if (credentialTypes.length) article.append(node('small', '', `需要的 Credential 類型：${credentialTypes.join('、')}`));
            return article;
        });
        state.dom.catalogResults.replaceChildren(...(rows.length ? rows : [empty('尚無搜尋結果。')]));
        state.dom.catalogMeta.textContent = state.catalogDigest
            ? `${state.catalogResults.length} 個結果 · Catalog ${shortDigest(state.catalogDigest)} · 不包含 Community／Custom Node`
            : '只搜尋固定版本的官方內建節點；不包含 Community 或 Custom Node。';
    }

    async function searchNodeCatalog(event) {
        event.preventDefault();
        const id = projectId();
        if (!id) return state.deps.showToast?.('請先選擇 Project。', 'warning');
        const search = String(state.dom.catalogQuery.value || '').trim();
        state.dom.catalogMeta.textContent = '正在搜尋固定版本節點目錄…';
        try {
            const payload = await api(`/api/integrations/n8n/node-catalog?project_id=${query(id)}${sessionId() ? `&session_id=${query(sessionId())}` : ''}&query=${query(search)}&limit=24`);
            state.catalogResults = listOf(payload.nodes).slice(0, 24);
            state.catalogDigest = String(payload.catalog_digest || '');
            renderCatalogResults();
        } catch (error) {
            state.catalogResults = [];
            state.catalogDigest = '';
            renderCatalogResults();
            state.dom.catalogMeta.textContent = `無法讀取節點目錄：${error.message}`;
            state.deps.showToast?.(error.message, 'error');
        }
    }

    function resetAdoptionPreview() {
        state.adoptionPreview = null;
        state.dom.adoptPreview.hidden = true;
        state.dom.adoptSummary.textContent = '';
        state.dom.adoptIssues.replaceChildren();
        state.dom.adoptConfirmation.value = '';
        state.dom.adoptConfirm.disabled = true;
    }

    function renderAdoptionPreview(preview) {
        const graph = preview?.graph_preview && typeof preview.graph_preview === 'object' ? preview.graph_preview : {};
        const validation = validationValue(preview?.validation_status) || String(preview?.status || '');
        const issues = normalizedIssueList(preview?.issues);
        state.dom.adoptPreview.hidden = false;
        state.dom.adoptSummary.textContent = `${String(preview?.workflow_name || '未命名 Workflow')} · ${Number(graph.node_count || listOf(graph.nodes).length || 0)} 個節點 · ${Number(graph.edge_count || listOf(graph.edges).length || 0)} 條連線 · ${preview?.active ? '目前已啟用' : '目前未啟用'} · 驗證 ${validation === 'ready' || preview?.status === 'graph_ready' ? '通過' : '未通過'}`;
        state.dom.adoptIssues.replaceChildren(...(issues.length
            ? issues.map(item => node('li', '', issueText(item)))
            : [node('li', 'is-muted', '未發現阻擋項目。')]));
        state.dom.adoptConfirmation.value = '';
        state.dom.adoptConfirmation.placeholder = `請輸入：${String(preview?.workflow_name || '')}`;
        state.dom.adoptConfirm.disabled = true;
    }

    async function previewAdoption(event) {
        event.preventDefault();
        const id = projectId();
        const workflowId = String(state.dom.adoptWorkflowId.value || '').trim();
        if (!id) return state.deps.showToast?.('請先選擇 Project。', 'warning');
        if (!workflowId) return;
        resetAdoptionPreview();
        try {
            const preview = await api(`/api/integrations/n8n/managed-workflows/${query(workflowId)}/adoption-preview?project_id=${query(id)}${sessionId() ? `&session_id=${query(sessionId())}` : ''}`);
            state.adoptionPreview = preview;
            renderAdoptionPreview(preview);
        } catch (error) {
            state.deps.showToast?.(`無法預覽採用：${error.message}`, 'error');
        }
    }

    function updateAdoptionConfirmation() {
        const preview = state.adoptionPreview;
        const exact = String(state.dom.adoptConfirmation.value || '');
        state.dom.adoptConfirm.disabled = !preview || preview.status !== 'graph_ready'
            || exact !== String(preview.workflow_name || '');
    }

    async function confirmAdoption(event) {
        event.preventDefault();
        const preview = state.adoptionPreview;
        if (!preview || state.dom.adoptConfirm.disabled) return;
        try {
            await api(`/api/integrations/n8n/managed-workflows/${query(preview.workflow_id)}/adopt`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project_id: projectId(), session_id: sessionId() || null,
                    expected_digest: preview.expected_digest,
                    confirmation: state.dom.adoptConfirmation.value,
                }),
            });
            state.deps.showToast?.('Workflow 已採用並綁定目前 Project。', 'success');
            resetAdoptionPreview();
            state.dom.adoptWorkflowId.value = '';
            await refreshAll();
        } catch (error) {
            state.deps.showToast?.(`無法採用 Workflow：${error.message}`, 'error');
        }
    }

    function resetProjectScopedRuntime() {
        state.requestId += 1;
        state.policy = null;
        state.workflows = [];
        state.operations = [];
        state.audits = [];
        state.credentialAliases = [];
        state.runtimeApprovals = [];
        state.dom.credentialAliasName.value = '';
        state.dom.credentialAliasId.value = '';
        if (state.inspectorScope) {
            state.inspectorScope = '';
            state.dom.inspectorExecution.replaceChildren();
            state.dom.inspectorResults.replaceChildren();
            state.dom.inspectorExecution.hidden = true;
            state.dom.inspectorResults.hidden = true;
            state.dom.chatExecution.hidden = false;
            state.dom.chatResults.hidden = false;
        }
        renderLists();
    }

    async function adoptCredentialAlias(event) {
        event.preventDefault();
        const scopedProject = projectId();
        const alias = String(state.dom.credentialAliasName.value || '').trim();
        const credentialId = String(state.dom.credentialAliasId.value || '');
        if (!scopedProject) return state.deps.showToast?.('請先選擇 Project。', 'warning');
        if (!/^[a-z][a-z0-9._-]{0,62}$/.test(alias) || !credentialId) return;
        state.dom.credentialAliasId.value = '';
        try {
            await api('/api/integrations/n8n/credential-aliases', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ project_id: scopedProject, alias, credential_id: credentialId }),
            });
            if (projectId() !== scopedProject) return;
            state.dom.credentialAliasName.value = '';
            state.deps.showToast?.('Credential 別名已安全採用。', 'success');
            await refreshAll();
        } catch (error) {
            if (projectId() === scopedProject) state.deps.showToast?.(error.message, 'error');
        }
    }

    async function refreshCredentialAlias(alias) {
        const scopedProject = projectId();
        if (!scopedProject) return;
        try {
            await api(`/api/integrations/n8n/credential-aliases/${query(alias)}/refresh?project_id=${query(scopedProject)}`, { method: 'POST' });
            if (projectId() !== scopedProject) return;
            state.deps.showToast?.('Credential 別名已重新驗證。', 'success');
            await refreshAll();
        } catch (error) {
            if (projectId() === scopedProject) state.deps.showToast?.(error.message, 'error');
        }
    }

    async function revokeCredentialAlias(alias) {
        const scopedProject = projectId();
        if (!scopedProject) return;
        if (!window.confirm(`撤銷 Credential 別名「${alias}」？現有 Workflow 綁定不會刪除，但新動作將無法使用此別名。`)) return;
        try {
            await api(`/api/integrations/n8n/credential-aliases/${query(alias)}?project_id=${query(scopedProject)}`, { method: 'DELETE' });
            if (projectId() !== scopedProject) return;
            state.deps.showToast?.('Credential 別名已撤銷。', 'success');
            await refreshAll();
        } catch (error) {
            if (projectId() === scopedProject) state.deps.showToast?.(error.message, 'error');
        }
    }

    async function refreshAll() {
        if (!state.initialized) return;
        renderProjects();
        if (state.planScope && state.planScope !== planScopeKey()) resetPlanner();
        const id = projectId();
        if (!id) {
            state.policy = null; state.workflows = []; state.operations = []; state.audits = [];
            state.credentialAliases = []; state.runtimeApprovals = [];
            state.catalogResults = []; state.catalogDigest = ''; renderCatalogResults(); resetAdoptionPreview(); renderLists(); return;
        }
        const requestId = ++state.requestId;
        const selectedSessionId = sessionId();
        const policy = await api(`/api/integrations/n8n/agent-policy?project_id=${query(id)}${selectedSessionId ? `&session_id=${query(selectedSessionId)}` : ''}`).catch(error => ({ error }));
        if (requestId !== state.requestId || projectId() !== id) return;
        if (policy.error) { state.deps.showToast?.(policy.error.message, 'error'); return; }
        state.policy = policy; renderPolicy();
        const [workflows, operations, audits, credentials, runtimeApprovals] = await Promise.all([
            policy.mode === 'off' ? Promise.resolve({ workflows: [] }) : api(`/api/integrations/n8n/managed-workflows?project_id=${query(id)}${selectedSessionId ? `&session_id=${query(selectedSessionId)}` : ''}`).catch(() => ({ workflows: [] })),
            api(`/api/integrations/n8n/operation-requests?project_id=${query(id)}`).catch(() => ({ operations: [] })),
            api(`/api/integrations/n8n/audits?project_id=${query(id)}`).catch(() => ({ audits: [] })),
            api(`/api/integrations/n8n/credential-aliases?project_id=${query(id)}`).catch(() => ({ credentials: [] })),
            api(`/api/integrations/n8n/runtime-approvals?project_id=${query(id)}&limit=100`).catch(() => ({ approvals: [] })),
        ]);
        if (requestId !== state.requestId || projectId() !== id) return;
        state.workflows = workflows.workflows || [];
        state.operations = operations.operations || [];
        state.audits = audits.audits || [];
        state.credentialAliases = credentials.credentials || [];
        state.runtimeApprovals = runtimeApprovals.approvals || [];
        renderLists();
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
            chatStart: id('workflow-chat-start'), planWorkspace: id('n8n-plan-workspace'),
            form: id('n8n-agent-policy-form'), project: id('n8n-agent-project'), mode: id('n8n-agent-mode'), duration: id('n8n-agent-duration'), ack: id('n8n-agent-ack'), state: id('n8n-agent-policy-state'), message: id('n8n-agent-policy-message'),
            apiKeyForm: id('n8n-agent-api-key-form'), apiKey: id('n8n-agent-api-key'), workflows: id('n8n-managed-workflows-list'), workflowCount: id('n8n-managed-workflows-count'), operations: id('n8n-operation-requests-list'), operationCount: id('n8n-operation-requests-count'), audits: id('n8n-agent-audits-list'),
            credentialAliasForm: id('n8n-credential-alias-form'), credentialAliasName: id('n8n-credential-alias-name'), credentialAliasId: id('n8n-credential-alias-id'),
            credentialAliases: id('n8n-credential-aliases-list'), credentialAliasCount: id('n8n-credential-aliases-count'),
            runtimeApprovals: id('n8n-runtime-approvals-list'), runtimeApprovalCount: id('n8n-runtime-approvals-count'),
            planForm: id('n8n-plan-form'), planInput: id('n8n-plan-input'), planSend: id('n8n-plan-send'), planReset: id('n8n-plan-reset'),
            planSession: id('n8n-plan-session'), planScopeSummary: id('n8n-plan-scope-summary'),
            planState: id('n8n-plan-state'), planMessages: id('n8n-plan-messages'), planOptions: id('n8n-plan-options'), planImpact: id('n8n-plan-impact'),
            planProvenance: id('n8n-plan-provenance'), planPrimaryModel: id('n8n-plan-primary-model'),
            planStructuredMode: id('n8n-plan-structured-mode'), planRepairModel: id('n8n-plan-repair-model'),
            planRisks: id('n8n-plan-risks'), planOutcomes: id('n8n-plan-outcomes'), planPermissions: id('n8n-plan-permissions'),
            planGraphStage: id('n8n-plan-graph-stage'), planGraphMessage: id('n8n-plan-graph-message'), planMaterialize: id('n8n-plan-materialize'),
            planGraphPreview: id('n8n-plan-graph-preview'), planValidationStatus: id('n8n-plan-validation-status'),
            planNodeCount: id('n8n-plan-node-count'), planEdgeCount: id('n8n-plan-edge-count'), planGraphNodes: id('n8n-plan-graph-nodes'), planGraphEdges: id('n8n-plan-graph-edges'),
            planQuestionsWrap: id('n8n-plan-graph-questions-wrap'), planQuestions: id('n8n-plan-graph-questions'),
            planIssuesWrap: id('n8n-plan-graph-issues-wrap'), planIssues: id('n8n-plan-graph-issues'),
            planCatalogDigest: id('n8n-plan-catalog-digest'), planGraphDigest: id('n8n-plan-graph-digest'),
            planProposal: id('n8n-plan-proposal-confirm'), planProposalSummary: id('n8n-plan-proposal-summary'),
            planProposalAck: id('n8n-plan-proposal-ack'), planPropose: id('n8n-plan-propose'),
            catalogForm: id('n8n-node-catalog-form'), catalogQuery: id('n8n-node-catalog-query'), catalogMeta: id('n8n-node-catalog-meta'), catalogResults: id('n8n-node-catalog-results'),
            adoptPreviewForm: id('n8n-workflow-adopt-preview-form'), adoptWorkflowId: id('n8n-workflow-adopt-id'), adoptPreview: id('n8n-workflow-adopt-preview'),
            adoptSummary: id('n8n-workflow-adopt-summary'), adoptIssues: id('n8n-workflow-adopt-issues'), adoptConfirmForm: id('n8n-workflow-adopt-confirm-form'),
            adoptConfirmation: id('n8n-workflow-adopt-confirmation'), adoptConfirm: id('n8n-workflow-adopt-confirm'),
            chatExecution: id('run-execution-content'), chatResults: id('run-results-content'), inspectorExecution: id('mail-inspector-execution'), inspectorResults: id('mail-inspector-results'),
        };
        if (Object.values(state.dom).some(value => !value)) throw new Error('n8n governance DOM is incomplete.');
        state.dom.chatStart.addEventListener('click', () => state.deps.openChatComposer?.());
        state.dom.form.addEventListener('submit', savePolicy); state.dom.apiKeyForm.addEventListener('submit', saveApiKey);
        state.dom.credentialAliasForm.addEventListener('submit', adoptCredentialAlias);
        state.dom.project.addEventListener('change', () => { state.planRestoreRequestId += 1; releaseInspectorContext(); resetProjectScopedRuntime(); resetPlanner(); resetAdoptionPreview(); renderPlanSessions(); void refreshAll(); });
        state.dom.planSession.addEventListener('change', () => { state.planRestoreRequestId += 1; resetPlanner(); resetAdoptionPreview(); void refreshAll(); });
        state.dom.mode.addEventListener('change', () => {
            const advanced = state.dom.mode.value === 'full_audit';
            state.dom.duration.disabled = !advanced;
            state.dom.duration.closest('label').hidden = !advanced;
            state.dom.ack.closest('label').hidden = !advanced;
        });
        state.dom.planForm.addEventListener('submit', submitPlanMessage);
        state.dom.planInput.addEventListener('keydown', event => {
            if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
            event.preventDefault();
            state.dom.planForm.requestSubmit();
        });
        state.dom.planReset.addEventListener('click', () => resetPlanner());
        state.dom.planMaterialize.addEventListener('click', () => void materializePlan());
        state.dom.planProposalAck.addEventListener('change', renderPlanner);
        state.dom.planPropose.addEventListener('click', () => void proposePlan());
        state.dom.catalogForm.addEventListener('submit', searchNodeCatalog);
        state.dom.adoptPreviewForm.addEventListener('submit', previewAdoption);
        state.dom.adoptConfirmForm.addEventListener('submit', confirmAdoption);
        state.dom.adoptConfirmation.addEventListener('input', updateAdoptionConfirmation);
        // Governance data is only needed in the Workflow workspace.  The
        // existing Workflow controller calls refreshAll() when that workspace
        // opens, avoiding n8n API work on the critical chat startup path.
        state.initialized = true; renderProjects(); renderCatalogResults(); resetAdoptionPreview();
        state.refreshTimer = window.setInterval(() => {
            if (!document.getElementById('n8n-workflow-center')?.hidden) void refreshAll();
        }, 30000);
        renderPlanner();
    }

    window.workbenchN8nGovernance = {
        init,
        refreshAll,
        refreshProjects: renderProjects,
        releaseInspectorContext,
        startPlanFromChat,
        restorePlanForScope,
        getState: () => state,
    };
})();
