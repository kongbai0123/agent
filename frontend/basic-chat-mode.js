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
    settingAgentAllowWorkspaceWrite.checked = false;
    settingSubagentEnabled.checked = false;
    [
        settingAgentDetailedProgress, settingSkillsEnabled,
        settingAgentAllowWorkspaceWrite, settingAgentFinalReportDetail,
        settingSubagentEnabled,
        settingSubagentPlannerModel, settingSubagentExplorerModel,
        settingSubagentImplementerModel, settingSubagentCriticModel,
        settingSubagentCloudRouting, settingSubagentMaxParallel,
        ...Object.values(agentDisplayNameInputs)
    ].filter(Boolean).forEach(control => { control.disabled = true; });
    document.querySelector('.subagent-settings-card')?.setAttribute('hidden', '');
    renderBasicSubagentStatus(subagentResourcePlan);
}

function renderBasicChatModeChip(chip, text) {
    const hasProject = !!activeProjectId;
    const enabled = hasProject && !!ragToggle?.checked;
    text.textContent = hasProject ? `專案知識：${enabled ? '開' : '關'}` : '專案知識：未選專案';
    chip.classList.toggle('chip-ok', enabled);
    chip.classList.toggle('chip-warn', !enabled);
}

function configureBasicChatComposerUi() {
    loadKnowledgeRetrievalPreference(activeProjectId);
    userInput.placeholder = '輸入訊息；需要時會使用目前專案的知識與工具…';
    [
        'rail-runs', 'rail-artifacts',
        'chip-docs', 'skills-button', 'active-skills-bar',
        'task-progress-center', 'wz-kb'
    ].forEach(id => {
        const element = document.getElementById(id);
        if (element) element.hidden = true;
    });
    document.querySelectorAll([
        '[data-target="tab-settings-agent"]', '[data-target="tab-settings-integrations"]', '[data-target="tab-settings-runtime"]',
        '[data-itab="run"]', '[data-itab="artifact"]', '[data-itab="logs"]'
    ].join(', '))
        .forEach(element => { element.hidden = true; });
    document.querySelector('#inspector-pane-context .ip-section')?.setAttribute('hidden', '');
    const chip = document.getElementById('chip-rag');
    if (chip) chip.style.pointerEvents = '';
}

function useBasicKnowledgeStatus() {
    return kbStatus;
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
        '上傳文件（知識庫）', '開啟知識庫工作區', '執行檢索測試',
        '切換模型', '安裝模型', '管理雲端 LLM API', '開新任務（新對話）',
        '開啟整合中心',
        '切換淺色 / 深色主題', '開啟設定中心', '執行模型測速'
    ]);
    return actions.filter(action => allowed.has(action.label));
}
