const BASIC_CHAT_MODE = true;

function removedBasicFeature(name) {
    return Promise.reject(new Error(`${name} is not available in Basic Chat mode.`));
}

function resetBasicAgentCollaborationUi() {
    agentPanelDismissedForRun = true;
    agentCollaborationState = createAgentCollaborationState();
    agentCollaborationState.running = false;
    closeAgentCollaboration(false);
    renderAgentCollaboration();
}

function hideBasicAgentCollaborationUi(railAgents) {
    if (railAgents) railAgents.hidden = true;
    closeAgentCollaboration(false);
    renderAgentCollaboration();
}

function renderBasicSubagentStatus(plan) {
    if (plan) {
        plan.className = 'subagent-resource-plan';
        plan.innerHTML = '<div class="subagent-resource-plan-title">基本聊天模式已停用 Subagent Runtime。</div>';
    }
    return null;
}

function applyBasicChatSettingsUi() {
    settingAgentDetailedProgress.checked = false;
    settingSkillsEnabled.checked = false;
    settingAgentAutoValidate.checked = false;
    settingAgentAllowWorkspaceWrite.checked = false;
    settingSubagentEnabled.checked = false;
    [
        settingAgentDetailedProgress, settingSkillsEnabled, settingAgentMaxToolCalls,
        settingAgentMaxRepairRounds, settingAgentAutoValidate,
        settingAgentAllowWorkspaceWrite, settingAgentFinalReportDetail,
        settingSubagentEnabled,
        settingSubagentPlannerModel, settingSubagentExplorerModel,
        settingSubagentImplementerModel, settingSubagentCriticModel,
        settingSubagentCloudRouting, settingSubagentMaxParallel,
        ...Object.values(agentDisplayNameInputs)
    ].filter(Boolean).forEach(control => { control.disabled = true; });
    renderBasicSubagentStatus(subagentResourcePlan);
}

function renderBasicChatModeChip(chip, text) {
    text.textContent = '基本聊天';
    chip.classList.add('chip-ok');
    chip.classList.remove('chip-warn');
}

function configureBasicChatComposerUi() {
    ragToggle.checked = false;
    userInput.placeholder = '輸入訊息，與 AI 助手聊天…';
    [
        'rail-knowledge', 'rail-runs', 'rail-artifacts', 'rail-extensions',
        'chip-docs', 'skills-button', 'active-skills-bar',
        'task-progress-center', 'wz-kb'
    ].forEach(id => {
        const element = document.getElementById(id);
        if (element) element.hidden = true;
    });
    document.getElementById('manage-kb-btn')?.closest('.sidebar-footer')?.setAttribute('hidden', '');
    document.querySelectorAll([
        '[data-target="tab-settings-rag"]', '[data-target="tab-settings-agent"]',
        '[data-target="tab-settings-integrations"]', '[data-target="tab-settings-runtime"]',
        '[data-project-settings-tab="extensions"]', '[data-project-settings-pane="extensions"]',
        '#project-settings-open-extensions',
        '[data-itab="run"]', '[data-itab="artifact"]', '[data-itab="logs"]',
        '[data-itab="safir"]'
    ].join(', '))
        .forEach(element => { element.hidden = true; });
    document.querySelector('#inspector-pane-context .ip-section')?.setAttribute('hidden', '');
    const chip = document.getElementById('chip-rag');
    if (chip) {
        chip.classList.remove('chip-clickable');
        chip.title = '基本聊天模式';
        chip.setAttribute('aria-label', '基本聊天模式');
        chip.style.pointerEvents = 'none';
    }
}

function useBasicKnowledgeStatus() {
    kbStatus = { enabled: false, index_status: 'disabled', document_count: 0, chunk_count: 0 };
    renderKbStatusLine();
    updateDocsChip();
}

function configureBasicWelcomeDashboard(hasModel, primary, secondary) {
    primary.textContent = hasModel ? '開始聊天' : '安裝推薦模型';
    primary.onclick = hasModel ? () => userInput.focus() : () => openModelManager('recommended');
    secondary.textContent = hasModel ? '選擇聊天模型' : '連接既有 Ollama';
    secondary.onclick = hasModel
        ? () => openModelManager('installed')
        : () => document.getElementById('btn-settings-trigger').click();
}

function configureBasicWizard(hint, backendOk, ollamaOk, hasModel) {
    if (!hint) return;
    if (!backendOk) hint.textContent = '請先啟動後端服務，再按「重新檢查」。';
    else if (!ollamaOk) hint.textContent = 'Ollama 未啟動；也可改用已設定的雲端聊天模型。';
    else if (!hasModel) hint.textContent = '尚未安裝本地模型，請安裝或選擇雲端聊天模型。';
    else hint.textContent = '環境就緒，可以直接開始聊天。';
}

function basicPaletteActions(actions) {
    const allowed = new Set([
        '切換模型', '安裝模型', '管理雲端 LLM API', '開新任務（新對話）',
        '切換淺色 / 深色主題', '開啟設定中心', '執行模型測速'
    ]);
    return actions.filter(action => allowed.has(action.label));
}
