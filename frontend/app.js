// 主介面由 FastAPI 在同一個 origin 提供。這讓瀏覽器只需使用 HttpOnly cookie，
// 不再讓 JavaScript、sessionStorage、URL 或跨埠 CORS 接觸工作階段憑證。
const API_BASE = location.origin;
const WORKBENCH_CSP_NONCE =
    document.querySelector('meta[name="csp-nonce"]')?.getAttribute('content') || '';

/** 帶上同源 HttpOnly 工作階段 cookie 的 fetch。 */
async function apiFetch(url, options = {}) {
    const requestOptions = { ...options, credentials: 'same-origin' };
    let response = await fetch(url, requestOptions);
    if (response.status === 401) {
        // 後端重啟會輪替 token；以同源 bootstrap 更新不可讀取的 cookie 後重試一次。
        const bootstrap = await fetch('/session/bootstrap', {
            method: 'POST',
            credentials: 'same-origin',
            cache: 'no-store'
        });
        if (bootstrap.ok) response = await fetch(url, requestOptions);
    }
    return response;
}

/** EventSource 與 window.open 同樣會自動攜帶同源 cookie。 */
function apiUrl(url) {
    return url;
}

let isGenerating = false;
let isCancellingGeneration = false;
let currentSessionId = null;
let activeProjectId = null;
let currentImages = []; // 儲存當前待上傳圖片的 Base64 列表

// 第十階段高階功能變數
let temporaryContextText = '';
let speechRecognition = null;
let isRecording = false;
let activeArtifactCode = '';
let activeArtifactTitle = '';
let activeArtifactExt = 'html';

// ==========================================================================
// P0-1：對話狀態（LLM 記憶）與 UI 顯示分離
// LLM 上下文一律取自 conversationState，絕不從 DOM innerText 收集，
// 避免「執行過程」「任務清單」「引用來源」等 UI 文字污染下一輪 messages。
// ==========================================================================
let conversationState = [];

function addLLMMessage(role, content, meta = {}) {
    conversationState.push({ role, content, meta, timestamp: Date.now() });
}

function getLLMMessages() {
    return conversationState.map(m => ({ role: m.role, content: m.content }));
}

function resetConversationState(messages = []) {
    conversationState = messages.map(m => ({
        role: m.role,
        content: m.content,
        meta: {},
        timestamp: Date.now()
    }));
}

// 清洗助理回覆：剝離 thought / 工具 JSON，只留正式回答（與後端入庫邏輯一致）
function cleanAssistantText(text) {
    if (!text) return '';
    let clean = text;
    clean = clean.replace(/<thought>[\s\S]*?<\/thought>/g, '');
    clean = clean.replace(/<tool>[\s\S]*?<\/tool>/g, '');
    clean = clean.replace(/```json[\s\S]*?```/g, '');
    clean = clean.replace(/\bjson\s*\{[\s\S]*?\}/g, '');
    const thoughtIdx = clean.indexOf('<thought>');
    if (thoughtIdx !== -1) clean = clean.slice(0, thoughtIdx);
    return clean.trim();
}

// ==========================================================================
// P0-2：session 一律由後端建立與回發，前端不自行生成 session_id
// ==========================================================================
async function ensureSession() {
    if (currentSessionId) return currentSessionId;
    clearOutputSkillsContext('正在建立新對話…');
    const res = await apiFetch(`${API_BASE}/api/sessions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: '新對話', project_id: activeProjectId })
    });
    if (!res.ok) throw new Error(`建立會話失敗 (HTTP ${res.status})`);
    const data = await res.json();
    currentSessionId = data.session_id;
    window.workbenchProjectSkills?.setSessionContext({
        sessionId: currentSessionId,
        projectId: activeProjectId,
    });
    renderOutputSkillsPane(activeProjectId);
    syncRunInspectorContext(activeProjectId);
    return currentSessionId;
}

// DOM 元素
const systemStatusEl = document.getElementById('system-status');
const statusIndicator = systemStatusEl.querySelector('.status-indicator');
const statusText = systemStatusEl.querySelector('.status-text');
const modelSelect = document.getElementById('model-select');
const activeModelName = document.getElementById('active-model-name');
const ragToggle = document.getElementById('rag-toggle');
const chatMessages = document.getElementById('chat-messages');
const welcomeCard = document.getElementById('welcome-card');
const userInput = document.getElementById('user-input');
const chatForm = document.getElementById('chat-form');
const sendBtn = document.getElementById('send-btn');

// KaTeX 安全公式渲染函數
function safeRenderMath(element) {
    const isDefined = typeof renderMathInElement !== 'undefined';
    console.log('[MathRender] safeRenderMath called, isDefined:', isDefined, 'elementId:', element ? element.id : 'null');
    if (isDefined && element) {
        try {
            renderMathInElement(element, {
                delimiters: [
                    {left: "$$", right: "$$", display: true},
                    {left: "$", right: "$", display: false},
                    {left: "\\(", right: "\\)", display: false},
                    {left: "\\[", right: "\\]", display: true}
                ],
                throwOnError: false
            });
            console.log('[MathRender] renderMathInElement executed successfully');
        } catch (e) {
            console.warn('[MathRender] KaTeX render failed:', e);
        }
    }
}

function protectMathForMarkdown(text) {
    const source = String(text || '');
    const segments = [];
    let output = '';
    let index = 0;
    let fencedCode = false;
    let inlineCode = false;
    const escapedAt = position => {
        let slashes = 0;
        for (let cursor = position - 1; cursor >= 0 && source[cursor] === '\\'; cursor -= 1) slashes += 1;
        return slashes % 2 === 1;
    };
    const reserve = value => {
        const token = `MATHSEGMENTTOKEN${segments.length}END`;
        segments.push(value);
        output += token;
    };
    while (index < source.length) {
        if (source.startsWith('```', index)) {
            fencedCode = !fencedCode;
            output += '```';
            index += 3;
            continue;
        }
        if (!fencedCode && source[index] === '`' && !escapedAt(index)) {
            inlineCode = !inlineCode;
            output += source[index++];
            continue;
        }
        if (!fencedCode && !inlineCode) {
            const candidates = [
                { left: '$$', right: '$$' },
                { left: '\\[', right: '\\]' },
                { left: '\\(', right: '\\)' },
                { left: '$', right: '$' }
            ];
            const delimiter = candidates.find(item => source.startsWith(item.left, index));
            if (delimiter && !escapedAt(index)) {
                let end = index + delimiter.left.length;
                while (end < source.length) {
                    if (source.startsWith(delimiter.right, end) && !escapedAt(end)) break;
                    end += 1;
                }
                if (end < source.length) {
                    const value = source.slice(index, end + delimiter.right.length);
                    const body = value.slice(delimiter.left.length, -delimiter.right.length).trim();
                    if (body) {
                        reserve(value);
                        index = end + delimiter.right.length;
                        continue;
                    }
                }
            }
        }
        output += source[index++];
    }
    return { text: output, segments };
}

function restoreMathAfterMarkdown(html, segments) {
    let restored = String(html || '');
    segments.forEach((segment, index) => {
        const token = `MATHSEGMENTTOKEN${index}END`;
        restored = restored.split(token).join(escapeHtml(segment));
    });
    return restored;
}

// 會話歷史 DOM 元素
const sessionList = document.getElementById('session-list');
const newChatBtn = document.getElementById('new-chat-btn');
const newProjectBtn = document.getElementById('new-project-btn');
const searchSessionsInput = document.getElementById('search-sessions-input');
const sidebarContextMenu = document.getElementById('sidebar-context-menu');
const sidebarDialog = document.getElementById('sidebar-dialog');
const sidebarDialogForm = document.getElementById('sidebar-dialog-form');
const sidebarDialogTitle = document.getElementById('sidebar-dialog-title');
const sidebarDialogLabel = document.getElementById('sidebar-dialog-label');
const sidebarDialogInput = document.getElementById('sidebar-dialog-input');
const sidebarDialogSelect = document.getElementById('sidebar-dialog-select');
const sidebarDialogConfirm = document.getElementById('sidebar-dialog-confirm');
const projectDialogFields = document.getElementById('project-dialog-fields');
const projectRootPath = document.getElementById('project-root-path');
const projectBrowseButton = document.getElementById('project-browse-button');
const folderBrowserDialog = document.getElementById('folder-browser-dialog');
const folderBrowserPath = document.getElementById('folder-browser-path');
const folderBrowserList = document.getElementById('folder-browser-list');
const folderBrowserStatus = document.getElementById('folder-browser-status');
const folderBrowserUp = document.getElementById('folder-browser-up');
const folderBrowserSelect = document.getElementById('folder-browser-select');
const projectSwitcherBtn = document.getElementById('project-switcher-btn');
const projectSwitcherLabel = document.getElementById('project-switcher-label');
const projectSwitcherPopover = document.getElementById('project-switcher-popover');
const projectSwitcherSearch = document.getElementById('project-switcher-search');
const projectSwitcherList = document.getElementById('project-switcher-list');
let sidebarProjects = [];
let sidebarSessions = [];
let sidebarSearch = '';
const expandedTaskLists = new Set();
let sidebarPointerDrag = null;
let sidebarPointerTarget = null;
let sidebarPointerPlacement = 'inside';
let sidebarProjectDrag = null;
let sidebarProjectTarget = null;
let sidebarProjectPlacement = 'before';
let folderBrowserCurrentPath = null;
let folderBrowserParentPath = null;
let folderBrowserResolver = null;

// 圖片上傳/預覽 DOM 元素
const imgUploadBtn = document.getElementById('img-upload-btn');
const imgFileInput = document.getElementById('img-file-input');
const imagePreviewContainer = document.getElementById('image-preview-container');

// 知識庫管理 Modal DOM 元素
const manageKbBtn = document.getElementById('manage-kb-btn');
const kbManagerModal = document.getElementById('kb-manager-modal');
const kbModalClose = document.getElementById('kb-modal-close');
const kbModalCloseBtn = document.getElementById('kb-modal-close-btn');
const kbFileList = document.getElementById('kb-file-list');
const clearAllKbBtn = document.getElementById('clear-all-kb-btn');
const uploadZone = document.getElementById('upload-zone');
const fileInput = document.getElementById('file-input');
const progressContainer = document.getElementById('progress-container');
const progressFilename = progressContainer.querySelector('.progress-filename');
const progressPercent = progressContainer.querySelector('.progress-percent');
const progressFill = document.getElementById('progress-fill');

// Chunks 預覽區域
const chunksPreviewSection = document.getElementById('chunks-preview-section');
const chunksPreviewTitle = document.getElementById('chunks-preview-title');
const chunksList = document.getElementById('chunks-list');
const closeChunksBtn = document.getElementById('close-chunks-btn');

// 第十階段高階功能 DOM 引用
const dragOverlayMask = document.getElementById('drag-overlay-mask');
const tempContextBar = document.getElementById('temp-context-bar');
const tempContextFilename = document.getElementById('temp-context-filename');
const btnRemoveTempContext = document.getElementById('btn-remove-temp-context');
const slashCommandsMenu = document.getElementById('slash-commands-menu');
const voiceInputBtn = document.getElementById('voice-input-btn');
const artifactsSandboxPanel = document.getElementById('artifacts-sandbox-panel');
const sandboxTitle = document.getElementById('sandbox-title');
const sandboxSubtitle = document.getElementById('sandbox-subtitle');
const btnSandboxDownload = document.getElementById('btn-sandbox-download');
const btnSandboxClose = document.getElementById('btn-sandbox-close');
const outputFloatingPanel = document.getElementById('output-floating-panel');
const outputPanelProject = document.getElementById('output-panel-project');
const outputSkillsMount = document.getElementById('output-skills-mount');
const tabSandboxPreview = document.getElementById('tab-sandbox-preview');
const tabSandboxWorkspace = document.getElementById('tab-sandbox-workspace');
const sandboxIframe = document.getElementById('sandbox-iframe');
const sandboxCodeEditor = document.getElementById('sandbox-code-editor');
const sandboxStructureView = document.getElementById('sandbox-structure-view');
const sandboxStashList = document.getElementById('sandbox-stash-list');
const sandboxResizerMain = document.getElementById('sandbox-resizer-main');
const sandboxResizerInner = document.getElementById('sandbox-resizer-inner');
const sandboxStashSidebar = document.getElementById('sandbox-stash-sidebar');
const thoughtChainVisualizer = document.getElementById('thought-chain-visualizer');
const thoughtDetail = document.getElementById('thought-detail');

// 第十二階段：代碼暫存虛擬專案工作區全域變數 (Virtual VFS)
let virtualProjectFiles = {
    "index.html": { code: "", ext: "html", title: "HTML 互動網頁原型", timestamp: "" },
    "css/style.css": { code: "/* 專案客製化 CSS 樣式表 */\n.clock {\n    box-shadow: 0 0 30px #00FFFF;\n}", ext: "css", title: "樣式表配置", timestamp: "" },
    "js/app.js": { code: "// 專案動態交互邏輯\nconsole.log('App initialized.');", ext: "js", title: "JavaScript 邏輯", timestamp: "" },
    "app.py": { code: "# Python FastAPI 服務\nfrom fastapi import FastAPI\napp = FastAPI()\n", ext: "py", title: "Python 後端服務", timestamp: "" },
    "README.md": { code: "# 本地設計代碼暫存專案\n這是由 Agent 設計之專案代碼暫存工作區。", ext: "md", title: "專案說明文件", timestamp: "" },
    "version.json": { code: "{\n  \"version\": \"1.0.0\",\n  \"status\": \"staged\"\n}", ext: "json", title: "版本描述檔", timestamp: "" }
};
let activeVirtualFilePath = "index.html"; // 當前作用檔案
let codeEditDebounceTimer = null;

// P0-7：多檔案合併預覽 —— 記錄各檔初始樣板，僅在使用者/模型實際修改後才注入
const VFS_DEFAULTS = {
    "css/style.css": virtualProjectFiles["css/style.css"].code,
    "js/app.js": virtualProjectFiles["js/app.js"].code
};

// srcdoc 會繼承主文件 CSP；為使用者沙盒中的 inline script 附上本次回應 nonce。
// iframe 沒有 allow-same-origin，因此沙盒程式仍無法讀取主頁 DOM 或 session cookie。
function prepareSandboxHtml(html) {
    const source = String(html || '');
    if (!WORKBENCH_CSP_NONCE) return source;
    return source.replace(
        /<script\b(?![^>]*\bnonce\s*=)/gi,
        `<script nonce="${WORKBENCH_CSP_NONCE}"`
    );
}

// 將 index.html + css/style.css + js/app.js 合併為單一可預覽 HTML
function buildPreviewHtml() {
    const html = virtualProjectFiles["index.html"]?.code || "";
    const cssRaw = virtualProjectFiles["css/style.css"]?.code || "";
    const jsRaw = virtualProjectFiles["js/app.js"]?.code || "";

    // 樣板未被修改時不注入，避免預設示例污染生成頁面
    const css = cssRaw !== VFS_DEFAULTS["css/style.css"] ? cssRaw : "";
    const js = jsRaw !== VFS_DEFAULTS["js/app.js"] ? jsRaw : "";

    let output = html || `<!DOCTYPE html>\n<html>\n<head></head>\n<body></body>\n</html>`;

    if (css) {
        if (output.includes("</head>")) {
            output = output.replace("</head>", `<style>${css}</style></head>`);
        } else {
            output = `<style>${css}</style>` + output;
        }
    }
    if (js) {
        if (output.includes("</body>")) {
            output = output.replace("</body>", `<script>${js}<\/script></body>`);
        } else {
            output += `<script>${js}<\/script>`;
        }
    }
    return output;
}

function refreshSandboxPreview() {
    if (activeArtifactExt === 'html') {
        sandboxIframe.srcdoc = prepareSandboxHtml(buildPreviewHtml());
    }
}
const sandboxFileFilter = document.getElementById('sandbox-file-filter');

// 二次確認清空 Modal
const confirmModal = document.getElementById('confirm-modal');
const modalClose2 = confirmModal.querySelector('#modal-close');
const modalCancel = document.getElementById('modal-cancel');
const modalConfirm = document.getElementById('modal-confirm');

// 第十一階段設定中心 DOM 引用
const btnSandboxToggle = document.getElementById('btn-sandbox-toggle');
const btnSettingsTrigger = document.getElementById('btn-settings-trigger');
const settingsModal = document.getElementById('settings-modal');
const settingsModalBox = settingsModal?.querySelector('.settings-modal-box');
const settingsResizeHandle = document.getElementById('settings-resize-handle');
const btnSettingsClose = document.getElementById('btn-settings-close');
const btnSettingsCancel = document.getElementById('btn-settings-cancel');
const btnSettingsSave = document.getElementById('btn-settings-save');
const btnClearRagDb = document.getElementById('btn-clear-rag-db');
const btnRuntimeHealth = document.getElementById('btn-runtime-health');
const btnRuntimeExport = document.getElementById('btn-runtime-export');
const btnRuntimeRebuildPreview = document.getElementById('btn-runtime-rebuild-preview');
const btnRuntimeRebuildApply = document.getElementById('btn-runtime-rebuild-apply');
const runtimeHealthState = document.getElementById('runtime-health-state');
const runtimeHealthMetrics = document.getElementById('runtime-health-metrics');
const runtimeUsageState = document.getElementById('runtime-usage-state');
const runtimeUsageMetrics = document.getElementById('runtime-usage-metrics');
const runtimeExportSession = document.getElementById('runtime-export-session');
const runtimeRebuildReport = document.getElementById('runtime-rebuild-report');
const runtimeRebuildConfirm = document.getElementById('runtime-rebuild-confirm');
const btnN8nStatus = document.getElementById('btn-n8n-status');
const n8nStatusState = document.getElementById('n8n-status-state');
const n8nStatusMetrics = document.getElementById('n8n-status-metrics');
const n8nInstallOptions = document.getElementById('n8n-install-options');
const btnCursorStatus = document.getElementById('btn-cursor-status');
const cursorStatusState = document.getElementById('cursor-status-state');
const cursorStatusMetrics = document.getElementById('cursor-status-metrics');
const btnMcpStatus = document.getElementById('btn-mcp-status');
const mcpStatusState = document.getElementById('mcp-status-state');
const mcpStatusMetrics = document.getElementById('mcp-status-metrics');
const settingMcpServers = document.getElementById('setting-mcp-servers');
const taskProgressCenter = document.getElementById('task-progress-center');
const taskProgressList = document.getElementById('task-progress-list');
const taskProgressCount = document.getElementById('task-progress-count');
const taskProgressToggle = document.getElementById('task-progress-toggle');
const taskProgressItems = new Map();
let taskProgressClockTimer = null;
const agentCollaborationPanel = document.getElementById('agent-collaboration-panel');
const agentCollaborationResizer = document.getElementById('agent-collaboration-resizer');
const agentCollaborationClose = document.getElementById('agent-collaboration-close');
const agentCollaborationStop = document.getElementById('agent-collaboration-stop');
const agentRoster = document.getElementById('agent-roster');
const agentResourceMonitor = document.getElementById('agent-resource-monitor');
const agentResourceSummary = document.getElementById('agent-resource-summary');
const agentResourceDetails = document.getElementById('agent-resource-details');
const agentResourceLabel = document.getElementById('agent-resource-label');
const agentResourceRam = document.getElementById('agent-resource-ram');
const agentResourceVram = document.getElementById('agent-resource-vram');
const agentResourceMargin = document.getElementById('agent-resource-margin');
const agentResourceState = document.getElementById('agent-resource-state');
const agentResourceEstimated = document.getElementById('agent-resource-estimated');
const agentResourceActual = document.getElementById('agent-resource-actual');
const agentResourceCalibration = document.getElementById('agent-resource-calibration');
const agentResourceTokens = document.getElementById('agent-resource-tokens');
const agentExternalModelIndicator = document.getElementById('agent-external-model-indicator');
const agentExternalModelDetails = document.getElementById('agent-external-model-details');
const agentGraphSummary = document.getElementById('agent-graph-summary');
const agentParallelState = document.getElementById('agent-parallel-state');
const agentConvergenceState = document.getElementById('agent-convergence-state');
const agentHandoffList = document.getElementById('agent-handoff-list');
const agentConversation = document.getElementById('agent-conversation');
const agentActiveCount = document.getElementById('agent-active-count');
const agentDisputeCard = document.getElementById('agent-dispute-card');
const agentDisputeTitle = document.getElementById('agent-dispute-title');
const agentDisputeDetail = document.getElementById('agent-dispute-detail');
const agentDisputeState = document.getElementById('agent-dispute-state');

const settingOllamaUrl = document.getElementById('setting-ollama-url');
const modelProviderList = document.getElementById('model-provider-list');
// Provider renderer lives in extension-center.js; data-provider-field="api_key" persists through /api/settings/secrets.
const settingModelInputCost = document.getElementById('setting-model-input-cost');
const settingModelOutputCost = document.getElementById('setting-model-output-cost');
const settingModelCostCurrency = document.getElementById('setting-model-cost-currency');
const settingN8nUrl = document.getElementById('setting-n8n-url');
const settingChatModel = document.getElementById('setting-chat-model');
const settingVisionModel = document.getElementById('setting-vision-model');
const settingRagK = document.getElementById('setting-rag-k');
const valRagK = document.getElementById('val-rag-k');
const settingRagThreshold = document.getElementById('setting-rag-threshold');
const valRagThreshold = document.getElementById('val-rag-threshold');
const settingChunkSize = document.getElementById('setting-chunk-size');
const settingChunkOverlap = document.getElementById('setting-chunk-overlap');
const settingBrowserHeadful = document.getElementById('setting-browser-headful');
const settingNetworkProxy = document.getElementById('setting-network-proxy');
const settingAgentDetailedProgress = document.getElementById('setting-agent-detailed-progress');
const settingSkillsEnabled = document.getElementById('setting-skills-enabled');
const settingAgentMaxToolCalls = document.getElementById('setting-agent-max-tool-calls');
const settingAgentMaxRepairRounds = document.getElementById('setting-agent-max-repair-rounds');
const settingAgentAutoValidate = document.getElementById('setting-agent-auto-validate');
const settingAgentAllowWorkspaceWrite = document.getElementById('setting-agent-allow-workspace-write');
const settingAgentFinalReportDetail = document.getElementById('setting-agent-final-report-detail');
const settingSubagentEnabled = document.getElementById('setting-subagent-enabled');
const settingSubagentPlannerModel = document.getElementById('setting-subagent-planner-model');
const settingSubagentExplorerModel = document.getElementById('setting-subagent-explorer-model');
const settingSubagentImplementerModel = document.getElementById('setting-subagent-implementer-model');
const settingSubagentCriticModel = document.getElementById('setting-subagent-critic-model');
const settingSubagentCloudRouting = document.getElementById('setting-subagent-cloud-routing'), settingSubagentMaxParallel = document.getElementById('setting-subagent-max-parallel');
const subagentResourcePlan = document.getElementById('subagent-resource-plan');
// SAFIR controls are absent in basic-chat builds.  Keep the optional
// reference defined so legacy settings handlers cannot abort UI startup.
const safirModelStatus = document.getElementById('safir-model-status');
const agentDisplayNameInputs = {
    planner: document.getElementById('setting-agent-name-planner'),
    explorer: document.getElementById('setting-agent-name-explorer'),
    implementer: document.getElementById('setting-agent-name-implementer'),
    critic: document.getElementById('setting-agent-name-critic'),
    verifier: document.getElementById('setting-agent-name-verifier')
};

// P1-2：知識庫狀態（側欄摘要顯示用）
let kbStatus = {
    document_count: 0,
    chunk_count: 0,
    index_status: 'empty',
    embedding_model: '',
    updated_at: null
};

const AUTOMATION_PHASE_LABELS = {
    pending: '等待執行',
    waiting_approval: '等待批准',
    running: '執行中',
    verifying: '驗證結果',
    retrying: '準備重試',
    completed: '已完成',
    failed: '執行失敗',
    cancelled: '已取消'
};

function formatProgressElapsed(startedAt) {
    const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    if (seconds < 60) return `${seconds} 秒`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes} 分 ${String(seconds % 60).padStart(2, '0')} 秒`;
}

function taskProgressValueText(item) {
    if (Number.isFinite(item.value)) return `${Math.round(item.value)}%`;
    if (item.status === 'failed') return '失敗';
    if (item.status === 'cancelled') return '取消';
    return formatProgressElapsed(item.startedAt);
}

function refreshTaskProgressClock() {
    if (!taskProgressList) return;
    taskProgressList.querySelectorAll('.task-progress-item').forEach(card => {
        const item = taskProgressItems.get(card.dataset.progressId);
        const value = card.querySelector('.task-progress-value');
        if (item && value && !Number.isFinite(item.value)) value.textContent = taskProgressValueText(item);
    });
}

function renderTaskProgress() {
    if (!taskProgressCenter || !taskProgressList) return;
    taskProgressList.replaceChildren();
    const items = [...taskProgressItems.values()].sort((a, b) => b.updatedAt - a.updatedAt);
    taskProgressCount.textContent = String(items.length);
    taskProgressCenter.hidden = items.length === 0;
    if (
        items.length > 0
        && window.matchMedia('(max-width: 1180px)').matches
        && window.workbenchRunInspector?.isOpen?.()
    ) {
        setTaskProgressCollapsed(true);
    }
    items.forEach(item => {
        const card = document.createElement('article');
        card.className = `task-progress-item ${item.mode === 'indeterminate' ? 'indeterminate' : ''} ${item.status || 'running'}`;
        card.dataset.progressId = item.id;

        const row = document.createElement('div');
        row.className = 'task-progress-item-row';
        const label = document.createElement('div');
        label.className = 'task-progress-label';
        label.textContent = item.label;
        const value = document.createElement('div');
        value.className = 'task-progress-value';
        value.textContent = taskProgressValueText(item);
        row.append(label, value);

        const detail = document.createElement('div');
        detail.className = 'task-progress-detail';
        detail.textContent = item.detail || AUTOMATION_PHASE_LABELS[item.phase] || '處理中';

        const track = document.createElement('div');
        track.className = 'task-progress-track';
        track.setAttribute('role', 'progressbar');
        track.setAttribute('aria-label', item.label);
        track.setAttribute('aria-valuemin', '0');
        track.setAttribute('aria-valuemax', '100');
        if (Number.isFinite(item.value)) track.setAttribute('aria-valuenow', String(Math.round(item.value)));
        const fill = document.createElement('div');
        fill.className = 'task-progress-fill';
        fill.style.width = Number.isFinite(item.value) ? `${Math.max(0, Math.min(100, item.value))}%` : '38%';
        track.appendChild(fill);
        card.append(row, detail, track);
        taskProgressList.appendChild(card);
    });
}

function setTaskProgressCollapsed(collapsed) {
    if (!taskProgressCenter || !taskProgressToggle) return;
    taskProgressCenter.classList.toggle('collapsed', collapsed === true);
    taskProgressToggle.setAttribute('aria-expanded', String(collapsed !== true));
    taskProgressToggle.setAttribute('aria-label', collapsed ? '展開執行進度' : '收合執行進度');
    taskProgressToggle.title = collapsed ? '展開執行進度' : '收合執行進度';
}

function syncChatDrawerA11y(drawer = document.getElementById('chat-drawer')) {
    if (!drawer) return false;
    const expanded = !drawer.hidden && !drawer.classList.contains('collapsed');
    drawer.setAttribute('aria-hidden', String(!expanded));
    drawer.inert = !expanded;
    if (expanded) drawer.removeAttribute('inert');
    else drawer.setAttribute('inert', '');
    document.getElementById('rail-chat')?.setAttribute('aria-expanded', String(expanded));
    return expanded;
}

function collapseCompactChatDrawer({ focusTarget = null } = {}) {
    if (!window.matchMedia('(max-width: 900px)').matches) return false;
    const drawer = document.getElementById('chat-drawer');
    if (!drawer || drawer.hidden || drawer.classList.contains('collapsed')) return false;
    const focusWasInsideDrawer = drawer.contains(document.activeElement);
    drawer.classList.add('collapsed');
    syncChatDrawerA11y(drawer);
    if (focusWasInsideDrawer) {
        (focusTarget || document.getElementById('rail-chat'))?.focus?.();
    }
    return true;
}

function syncRightSidebarForViewport() {
    if (window.matchMedia('(max-width: 900px)').matches) {
        const inspector = window.workbenchRunInspector;
        const outputOpen = inspector?.isOpen?.() === true;
        const agentOpen = agentCollaborationPanel && !agentCollaborationPanel.hidden;
        const artifactOpen = artifactsSandboxPanel?.classList.contains('active') === true;
        if (outputOpen || agentOpen || artifactOpen) {
            const activeTab = inspector?.getState?.().activeTab || 'skills';
            const focusTarget = outputOpen
                ? document.getElementById(`output-tab-${activeTab}`)
                : agentOpen
                    ? document.getElementById('rail-agents')
                    : (btnSandboxToggle || document.getElementById('rail-artifacts'));
            collapseCompactChatDrawer({ focusTarget });
        }
    }
    if (
        window.matchMedia('(max-width: 1180px)').matches
        && !taskProgressCenter?.hidden
        && window.workbenchRunInspector?.isOpen?.()
    ) {
        setTaskProgressCollapsed(true);
    }
}

function prepareRunInspectorOpen() {
    const activeTab = window.workbenchRunInspector?.getState?.().activeTab || 'skills';
    const focusTarget = document.getElementById(`output-tab-${activeTab}`);
    closeInspectorPanel({ focusTarget });
    closeAgentCollaboration(true, { focusTarget });
    collapseCompactChatDrawer();
    if (window.matchMedia('(max-width: 1180px)').matches && !taskProgressCenter?.hidden) {
        setTaskProgressCollapsed(true);
    }
}

function updateTaskProgress(id, changes = {}) {
    if (!id) return;
    // 聊天回覆已有自己的 Tasks／事件抽屜；不要再複製到全域浮動進度中心，
    // 聊天進度留在回答區內；全域中心只保留背景工作。
    if (isInlineChatProgress(id)) return;
    const previous = taskProgressItems.get(id);
    if (previous?.removeTimer) clearTimeout(previous.removeTimer);
    taskProgressItems.set(id, {
        id,
        label: changes.label || previous?.label || '背景工作',
        detail: changes.detail ?? previous?.detail ?? '',
        phase: changes.phase ?? previous?.phase ?? 'running',
        mode: changes.mode || previous?.mode || 'indeterminate',
        value: Number.isFinite(changes.value) ? Math.max(0, Math.min(100, Number(changes.value))) : (changes.value === null ? null : previous?.value ?? null),
        status: changes.status || previous?.status || 'running',
        startedAt: previous?.startedAt || changes.startedAt || Date.now(),
        updatedAt: Date.now(),
        removeTimer: null
    });
    renderTaskProgress();
}

function isInlineChatProgress(id) {
    return String(id || '').startsWith('chat-generation-');
}

function finishTaskProgress(id, status = 'completed', detail = '') {
    const previous = taskProgressItems.get(id);
    if (!previous) return;
    if (['completed', 'failed', 'cancelled'].includes(previous.status)) return;
    const completed = status === 'completed';
    updateTaskProgress(id, {
        status,
        phase: status,
        mode: completed ? 'determinate' : 'indeterminate',
        value: completed ? 100 : null,
        detail: detail || (completed ? '工作已完成' : status === 'cancelled' ? '工作已取消' : '工作未完成')
    });
    const current = taskProgressItems.get(id);
    current.removeTimer = setTimeout(() => {
        taskProgressItems.delete(id);
        renderTaskProgress();
    }, completed ? 4500 : 8000);
}

function initTaskProgress() {
    if (!taskProgressCenter || !taskProgressToggle) return;
    taskProgressToggle.addEventListener('click', () => {
        const expanding = taskProgressCenter.classList.contains('collapsed');
        if (
            expanding
            && window.matchMedia('(max-width: 1180px)').matches
            && window.workbenchRunInspector?.isOpen?.()
        ) {
            setOutputFloatingPanelOpen(false);
        }
        setTaskProgressCollapsed(!expanding);
    });
    window.addEventListener('resize', syncRightSidebarForViewport, { passive: true });
    taskProgressClockTimer = setInterval(refreshTaskProgressClock, 1000);
}

window.WorkbenchProgress = {
    start: (id, options) => updateTaskProgress(id, options),
    update: updateTaskProgress,
    finish: finishTaskProgress
};

// ==========================================================================
// Agent 協作側欄
// 只呈現後台實際送出的狀態與公開摘要；不顯示或推測模型隱藏思考鏈。
// 同時相容既有 phase/tool/validation 事件與未來原生 agent_* 事件。
// ==========================================================================
const AGENT_ROLE_META = {
    planner: { label: 'Planner', short: 'P' },
    explorer: { label: 'Explorer', short: 'E' },
    implementer: { label: 'Implementer', short: 'I' },
    critic: { label: 'Critic', short: 'C' },
    verifier: { label: 'Verifier', short: 'V' }
};
const DEFAULT_AGENT_DISPLAY_NAMES = Object.fromEntries(
    Object.entries(AGENT_ROLE_META).map(([role, meta]) => [role, meta.label])
);
let agentDisplayNames = { ...DEFAULT_AGENT_DISPLAY_NAMES };
const AGENT_ACTIVE_STATES = new Set(['queued', 'pending', 'planning', 'waiting_for_model', 'connecting_provider', 'loading_model', 'running', 'working', 'saving_handoff', 'unloading_model', 'verifying_release', 'closing_provider', 'reviewing', 'revising', 'challenged']);
const AGENT_TERMINAL_STATES = new Set(['completed', 'verified', 'failed', 'blocked', 'cancelled']);
let agentPanelDismissedForRun = false;
let agentCollaborationState = createAgentCollaborationState();
function agentTaskRole(taskId) {
    const id = String(taskId || '').toLowerCase();
    if (id.includes('retrieve') || id.includes('inspect') || id.includes('research')) return 'explorer';
    if (id.includes('validate') || id.includes('test') || id.includes('verify')) return 'verifier';
    if (id.includes('execute') || id.includes('implement') || id.includes('fix')) return 'implementer';
    return 'planner';
}

function collaborationTime(value) {
    const parsed = value ? new Date(value) : new Date();
    return Number.isNaN(parsed.getTime()) ? new Date().toTimeString().slice(0, 5) : parsed.toTimeString().slice(0, 5);
}

function setAgentStatus(agentIdOrRole, status, details = {}) {
    const raw = String(agentIdOrRole || details.role || 'planner'), role = normalizeAgentRole(details.role || raw);
    const sameRole = Object.values(agentCollaborationState.agents).reverse().find(item => item.role === role);
    const id = AGENT_ROLE_META[raw] ? (sameRole?.id || defaultAgentExecutor(role)) : raw;
    const realAgent = details.realAgent === true || !!String(details.agent_id || details.agentId || '').trim();
    const existing = agentCollaborationState.agents[id];
    if (existing && AGENT_TERMINAL_STATES.has(existing.status) && !AGENT_TERMINAL_STATES.has(status)) return existing;
    const agent = ensureCollaborationAgent(agentCollaborationState, { ...details, agent_id: id, role, status, realAgent });
    agent.status = status || 'idle';
    const timestamp = String(details.createdAt || details.created_at || '');
    if (AGENT_ACTIVE_STATES.has(agent.status) && !agent.startedAt) agent.startedAt = timestamp;
    if (AGENT_TERMINAL_STATES.has(agent.status)) agent.completedAt = timestamp;
    updateCollaborationParallel(agentCollaborationState);
    return agent;
}

function defaultAgentExecutor(role) {
    const normalized = normalizeAgentRole(role);
    const runSuffix = agentCollaborationState.runId || 'pending';
    if (normalized === 'planner') return `orchestrator-${runSuffix}`;
    return `${normalized}-${runSuffix}`;
}

function normalizeAgentDisplayName(value, fallback) {
    const normalized = String(value || fallback || '')
        .replace(/[\u0000-\u001f\u007f]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .slice(0, 24);
    return normalized || fallback;
}

function applyAgentDisplayNames(value = {}) {
    agentDisplayNames = Object.fromEntries(Object.keys(AGENT_ROLE_META).map(role => [
        role,
        normalizeAgentDisplayName(value?.[role], DEFAULT_AGENT_DISPLAY_NAMES[role])
    ]));
}

function agentAliasSuffix(ordinal) {
    let value = Math.max(1, Number(ordinal) || 1);
    let suffix = '';
    while (value > 0) {
        value -= 1;
        suffix = String.fromCharCode(65 + (value % 26)) + suffix;
        value = Math.floor(value / 26);
    }
    return suffix;
}

function friendlyAgentExecutor(role, technicalId) {
    const normalized = normalizeAgentRole(role);
    const executor = String(technicalId || defaultAgentExecutor(normalized)).trim();
    const key = `${normalized}\u0000${executor}`;
    if (!agentCollaborationState.aliasOrdinals[key]) {
        agentCollaborationState.aliasCounters[normalized] = (agentCollaborationState.aliasCounters[normalized] || 0) + 1;
        agentCollaborationState.aliasOrdinals[key] = agentCollaborationState.aliasCounters[normalized];
    }
    return `${agentDisplayNames[normalized] || DEFAULT_AGENT_DISPLAY_NAMES[normalized]} ${agentAliasSuffix(agentCollaborationState.aliasOrdinals[key])}`;
}

function addAgentCollaborationMessage(role, tag, text, options = {}) {
    const cleanText = String(text || '')
        .replace(/\r\n?/g, '\n')
        .split('\n')
        .map(line => line.replace(/[ \t]+/g, ' ').trim())
        .join('\n')
        .replace(/\n{3,}/g, '\n\n')
        .trim();
    if (!cleanText) return;
    const normalized = normalizeAgentRole(role);
    const agentId = String(options.agentId || defaultAgentExecutor(normalized)).trim();
    ensureCollaborationAgent(agentCollaborationState, { ...options, agent_id: agentId, role: normalized, status: undefined, realAgent: options.realAgent === true });
    const previous = agentCollaborationState.messages[agentCollaborationState.messages.length - 1];
    if (previous && previous.role === normalized && previous.tag === tag && previous.text === cleanText) return;
    agentCollaborationState.messages.push({
        role: normalized,
        agentId,
        model: String(options.model || '').trim(),
        tag: String(tag || '進度'),
        text: cleanText.slice(0, 1200),
        time: collaborationTime(options.createdAt),
        reply: !!options.reply,
        tone: options.tone || '',
        evidence: Array.isArray(options.evidence) ? options.evidence.map(String).slice(0, 5) : []
    });
    if (agentCollaborationState.messages.length > 80) agentCollaborationState.messages.splice(0, agentCollaborationState.messages.length - 80);
}

function setAgentCollaborationStep(step) {
    agentCollaborationState.step = Math.max(agentCollaborationState.step, Math.min(5, Number(step) || 0));
}

function renderAgentCollaboration() {
    if (!agentCollaborationPanel || !agentRoster || !agentConversation) return;
    const allAgents = Object.values(agentCollaborationState.agents);
    const agents = allAgents.filter(agent => agent.realAgent);
    const active = agents.filter(isActiveCollaborationAgent).length;
    const independentAgents = agents.filter(agent => Number.isSafeInteger(agent.workerPid) && agent.workerPid > 0);
    const orchestrationOnlyCount = allAgents.length - agents.length;
    const distinctWorkerPids = new Set(independentAgents.map(agent => agent.workerPid));
    agentActiveCount.textContent = `${active} 位工作中`;
    agentActiveCount.closest('.agent-active-count')?.classList.toggle('idle', active === 0);
    agentCollaborationStop.disabled = !agentCollaborationState.running || !isGenerating;

    const shortId = value => String(value || '').slice(-8) || '--';
    const processBoundary = independentAgents.length
        ? `<div class="agent-process-boundary verified">已證明 ${independentAgents.length} 個獨立子代理程序；沒有 PID 的卡片表示已指派但尚未收到程序啟動證據。</div>`
        : agents.length
            ? `<div class="agent-process-boundary unverified">已指派 ${agents.length} 個子代理，但尚未收到獨立程序 PID；目前不可宣稱已開始多 Agent 執行。</div>`
            : '<div class="agent-process-boundary unverified">尚未啟動獨立子代理；流程角色不會冒充 Agent instance。</div>';
    const plannedModels = ['planner', 'explorer', 'implementer', 'critic']
        .filter(role => agentCollaborationState.roleModels[role])
        .map(role => `<span><strong>${escapeHtml(AGENT_ROLE_META[role].label)}</strong> ${escapeHtml(agentCollaborationState.roleModels[role])}</span>`)
        .join('');
    const modelPlan = plannedModels
        ? `<div class="agent-model-plan"><div>本輪子代理模型分配</div>${plannedModels}</div>`
        : '';
    agentRoster.innerHTML = processBoundary + modelPlan + (agents.length ? agents.map(agent => {
        const meta = AGENT_ROLE_META[agent.role] || AGENT_ROLE_META.planner;
        const start = agent.startedAt ? collaborationTime(agent.startedAt) : '--:--';
        const end = agent.completedAt ? collaborationTime(agent.completedAt) : AGENT_TERMINAL_STATES.has(agent.status) ? '已終止' : '進行中';
        const toolState = agent.currentTool
            ? ` · 工具循環 ${agent.toolCallCount}：${agent.currentTool}`
            : agent.toolCallCount ? ` · 工具循環 ${agent.toolCallCount}：${agent.lastTool || '已完成'}` : '';
        const processIdentity = Number.isSafeInteger(agent.workerPid) && agent.workerPid > 0
            ? `<span class="agent-process-proof">獨立程序 PID ${escapeHtml(String(agent.workerPid))}</span>`
            : '<span class="agent-process-unverified">程序 PID 未提供</span>';
        const contextIdentity = agent.contextId
            ? `<span class="agent-context-proof">私有 context #${escapeHtml(shortId(agent.contextId))}</span>`
            : '<span class="agent-context-unverified">context 未提供</span>';
        return `<article class="agent-instance-card agent-role-${agent.role} ${escapeHtml(agent.status)}" data-agent-id="${escapeHtml(agent.id)}" data-parent-id="${escapeHtml(agent.parentId)}"${agent.workerPid ? ` data-worker-pid="${escapeHtml(String(agent.workerPid))}"` : ''}>
            <div class="agent-instance-head"><span class="agent-roster-avatar">${escapeHtml(meta.short)}</span><strong>${escapeHtml(friendlyAgentExecutor(agent.role, agent.id))}</strong><span class="agent-instance-status">${escapeHtml(agent.status)}</span></div>
            <div class="agent-instance-model">${escapeHtml(meta.label)} · ${escapeHtml(agent.model || '模型資料缺失（舊紀錄）')} · ${agent.modelRequestCount ? `已呼叫 ${agent.modelRequestCount} 次` : '已指派，等待呼叫'}</div>
            <div class="agent-instance-identity">${processIdentity}${contextIdentity}</div>
            <div class="agent-instance-task">${escapeHtml(agent.task || '等待任務')}</div>
            <div class="agent-instance-trace">agent #${escapeHtml(shortId(agent.id))}${agent.workerId ? ` · worker #${escapeHtml(shortId(agent.workerId))}` : ''}${agent.parentId ? ` · ↳ parent #${escapeHtml(shortId(agent.parentId))}` : ''}${escapeHtml(toolState)} · ${escapeHtml(start)} → ${escapeHtml(end)}</div>
        </article>`;
    }).join('') : '<div class="agent-graph-empty">尚未建立獨立 Agent instance。</div>');
    const reasonLabels = { accepted: '已接受', stalled: '無進展停止', max_rounds: '達輪次上限', budget: '預算用盡', budget_exhausted: '預算用盡', deadline: '到達期限', cancelled: '已取消' };
    const graphEdges = agentCollaborationState.graph?.dependencies || [];
    const readyEdges = graphEdges.filter(edge => edge.ready).length;
    const processSummary = distinctWorkerPids.size
        ? `獨立程序 PID ${distinctWorkerPids.size} 個`
        : '尚無獨立程序 PID 證據';
    const agentScopeSummary = `${independentAgents.length} 獨立子代理 · ${orchestrationOnlyCount} 流程角色／舊事件`;
    const graphPrefix = graphEdges.length
        ? `執行圖 ${agentScopeSummary} · ${processSummary} · 交接 ${readyEdges}/${graphEdges.length}`
        : `執行圖 ${agentScopeSummary} · ${processSummary}`;
    agentParallelState.textContent = agentCollaborationState.graph?.invalidReason
        ? `執行圖無效：${agentCollaborationState.graph.invalidReason}`
        : agentCollaborationState.parallel.overlap
            ? `${graphPrefix} · 並行峰值 ${agentCollaborationState.parallel.peak}`
            : `${graphPrefix} · 目前 ${active} 位執行`;
    agentConvergenceState.textContent = agentCollaborationState.convergence.terminationReason ? `終止：${reasonLabels[agentCollaborationState.convergence.terminationReason] || agentCollaborationState.convergence.terminationReason}` : '等待收斂判定';
    agentGraphSummary.className = `agent-graph-summary convergence-${escapeHtml(agentCollaborationState.convergence.terminationReason || 'running')}`;
    agentHandoffList.hidden = agentCollaborationState.handoffs.length === 0;
    agentHandoffList.innerHTML = agentCollaborationState.handoffs.map(item => {
        const verification = item.contractValid
            ? `已驗證 · ${item.evidenceCount} 證據${item.consumedArtifactIds.length ? ` · 使用 ${item.consumedArtifactIds.length} 上游產物` : ''}`
            : '系統交接';
        return `<span class="${item.contractValid ? 'contract-valid' : 'system-handoff'} ${item.superseded ? 'superseded' : ''}" title="${escapeHtml(item.sha256 || item.artifactId)}">${escapeHtml(shortId(item.producerAgentId))} → ${escapeHtml(item.consumerAgentIds.map(shortId).join(', ') || 'Planner')} · ${escapeHtml(shortId(item.artifactId))} · ${escapeHtml(verification)}</span>`;
    }).join('');

    const resource = agentCollaborationState.resource;
    const formatGb = value => Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)}G` : '--';
    const resourceState = resource.state || 'idle';
    agentResourceMonitor.className = `agent-resource-monitor ${escapeHtml(resourceState)}`;
    agentResourceLabel.textContent = resourceState === 'idle' ? '資源待命' : resourceState === 'running' ? '資源監測' : resourceState === 'danger' ? '資源危險' : resourceState === 'caution' ? '資源注意' : '資源安全';
    agentResourceRam.textContent = `RAM ${formatGb(resource.ramPeak)}`;
    agentResourceVram.textContent = `VRAM ${formatGb(resource.vramPeak)}`;
    agentResourceMargin.textContent = `餘裕 ${formatGb(resource.margin)}`;
    agentResourceState.textContent = resourceState === 'danger' ? '危險' : resourceState === 'caution' ? '注意' : resourceState === 'idle' ? '待命' : resourceState === 'running' ? '監測中' : '安全';
    agentResourceEstimated.textContent = formatGb(resource.estimatedPeak);
    agentResourceActual.textContent = formatGb(resource.actualPeak);
    agentResourceCalibration.textContent = resource.calibrationSamples >= 3
        ? `${resource.calibrationSamples} 次 · ${Math.round((resource.calibrationFactor - 1) * 100)}%`
        : `${resource.calibrationSamples || 0} / 3 次`;

    if (agentCollaborationState.messages.length === 0) {
        agentConversation.innerHTML = '<div class="agent-collaboration-empty">開始執行任務後，這裡會顯示派工、發現、質疑、修正與驗證事件。</div>';
    } else {
        agentConversation.innerHTML = agentCollaborationState.messages.map(message => {
            const meta = AGENT_ROLE_META[message.role] || AGENT_ROLE_META.planner;
            const executor = message.agentId || defaultAgentExecutor(message.role);
            const executorLabel = `${meta.label} (${friendlyAgentExecutor(message.role, executor)})`;
            const executorTitle = `技術 ID：${executor}${message.model ? `｜模型：${message.model}` : ''}`;
            const evidence = message.evidence.length
                ? `<div class="agent-evidence-row">${message.evidence.map(item => `<span class="agent-evidence-chip">${escapeHtml(item)}</span>`).join('')}</div>`
                : '';
            return `<article class="agent-message agent-role-${message.role} ${message.reply ? 'reply' : ''} ${escapeHtml(message.tone)}">
                <span class="agent-message-avatar" aria-hidden="true">${escapeHtml(meta.short)}</span>
                <div class="agent-message-card">
                    <div class="agent-message-head"><span class="agent-message-name" title="${escapeHtml(executorTitle)}">${escapeHtml(executorLabel)}</span><span class="agent-message-time">${escapeHtml(message.time)}</span><span class="agent-message-tag">${escapeHtml(message.tag)}</span></div>
                    <div class="agent-message-text">${escapeHtml(message.text)}</div>${evidence}
                </div>
            </article>`;
        }).join('');
    }

    const externalModels = agentCollaborationState.externalModels || [];
    agentExternalModelIndicator.hidden = externalModels.length === 0;
    if (externalModels.length) {
        agentExternalModelIndicator.querySelector('span').textContent = `外部模型占用 ${externalModels.length}`;
        agentExternalModelDetails.textContent = `外部模型正在占用資源：${externalModels.join('、')}。系統不會自動卸載來源不明的模型。`;
    } else {
        agentExternalModelIndicator.setAttribute('aria-expanded', 'false');
        agentExternalModelDetails.hidden = true;
    }

    const dispute = agentCollaborationState.disputes[agentCollaborationState.activeDisputeId];
    if (dispute) {
        agentDisputeCard.hidden = false;
        agentDisputeCard.classList.toggle('resolved', dispute.resolved);
        agentDisputeTitle.textContent = dispute.resolved ? '爭議已解決' : '待解決爭議';
        agentDisputeTitle.textContent += ` ? #${shortId(dispute.id)}`;
        agentDisputeDetail.textContent = dispute.detail || '';
        agentDisputeState.textContent = dispute.resolved ? '已裁決' : '等待裁決';
    } else {
        agentDisputeCard.hidden = true;
    }

    safeCreateIcons();
    requestAnimationFrame(() => { agentConversation.scrollTop = agentConversation.scrollHeight; });
}

function openAgentCollaboration(force = false) {
    if (primaryWorkspace !== 'chat') return false;
    if (!agentCollaborationPanel || (!force && agentPanelDismissedForRun)) return;
    setOutputFloatingPanelOpen(false);
    closeInspectorPanel();
    collapseCompactChatDrawer();
    agentCollaborationPanel.hidden = false;
    document.getElementById('rail-agents')?.classList.add('active');
    renderAgentCollaboration();
    return true;
}

function closeAgentCollaboration(dismissForRun = true, { focusTarget = null } = {}) {
    if (!agentCollaborationPanel) return;
    const focusWasInside = agentCollaborationPanel.contains(document.activeElement);
    agentCollaborationPanel.hidden = true;
    document.getElementById('rail-agents')?.classList.remove('active');
    if (dismissForRun) agentPanelDismissedForRun = true;
    if (focusWasInside) (focusTarget || document.getElementById('rail-agents'))?.focus?.();
}

function resetAgentCollaboration() {
    if (BASIC_CHAT_MODE) return resetBasicAgentCollaborationUi();
    agentPanelDismissedForRun = false;
    agentCollaborationState = createAgentCollaborationState();
    agentCollaborationState.running = true;
    setAgentStatus('orchestrator-ui', 'working', {
        role: 'planner',
        model: modelSelect.value || currentSettings.default_chat_model || ''
    });
    addAgentCollaborationMessage('planner', '受理', '正在分析要求並建立可驗證的工作計畫。', { agentId: 'orchestrator-ui' });
    openAgentCollaboration(true);
}

function restoreAgentCollaboration(events) {
    agentPanelDismissedForRun = false;
    agentCollaborationState = createAgentCollaborationState();
    (Array.isArray(events) ? events : []).forEach(item => handleAgentCollaborationEvent(item.type, item, false));
    agentCollaborationState.running = false;
    Object.values(agentCollaborationState.agents).forEach(agent => {
        if (AGENT_ACTIVE_STATES.has(agent.status)) setAgentStatus(agent.id, 'completed', { role: agent.role });
    });
    renderAgentCollaboration();
}

function handleAgentCollaborationEvent(eventType, data = {}, shouldRender = true) {
    const createdAt = data.created_at;
    reduceAgentCollaborationState(agentCollaborationState, eventType, data);
    if (eventType === 'meta') {
        agentCollaborationState.runId = String(data.run_id || agentCollaborationState.runId || '').trim();
    } else if (eventType === 'agent_worker_started') {
        const role = normalizeAgentRole(data.role || data.name || data.agent_type);
        const workerPid = Number(data.worker_pid);
        const workerIdentity = Number.isSafeInteger(workerPid) && workerPid > 0
            ? `獨立程序 PID ${workerPid}`
            : 'worker（程序 PID 未提供）';
        const workerStartDetail = data.message || `開始執行 ${data.task || data.task_id || '工作'}。`;
        addAgentCollaborationMessage(role, '啟動', `${workerIdentity} · ${workerStartDetail}`, {
            createdAt, agentId: data.agent_id || '', model: data.model || '', realAgent: !!data.agent_id
        });
    } else if (eventType === 'agent_spawned') {
        const role = normalizeAgentRole(data.role || data.name || data.agent_type);
        setAgentStatus(data.agent_id || role, 'queued', { ...data, createdAt });
        const checklist = Array.isArray(data.checklist) && data.checklist.length
            ? `\n${data.checklist.map((item, index) => `${index + 1}. ${item}`).join('\n')}`
            : '';
        const acceptance = data.acceptance ? `\n完成條件：${data.acceptance}` : '';
        const assignmentText = data.message || `${data.task || `已將任務交給 ${AGENT_ROLE_META[role].label}。`}${checklist}${acceptance}`;
        addAgentCollaborationMessage('planner', '派工', assignmentText, {
            createdAt, reply: role === 'planner',
            agentId: data.assigned_by || 'orchestrator', model: data.model || ''
        });
    } else if (eventType === 'agent_status') {
        setAgentStatus(data.agent_id || data.role || data.name || data.agent_type, data.status || 'working', { ...data, createdAt });
    } else if (eventType === 'planner_decision') {
        setAgentStatus(data.agent_id || 'planner', 'working', { ...data, createdAt });
        addAgentCollaborationMessage(
            'planner',
            data.fallback ? '安全回退' : '決策',
            data.summary || 'Planner 已完成派工決策。',
            { createdAt, tone: 'decision', agentId: data.agent_id || defaultAgentExecutor('planner'), model: data.model || '' }
        );
    } else if (eventType === 'critic_review') {
        const disputes = Array.isArray(data.disputes) ? data.disputes : [];
        const detail = disputes.length
            ? disputes.map((item, index) => `${index + 1}. ${item.issue}；建議：${item.recommendation}`).join('\n')
            : (data.summary || 'Critic 未發現需要阻止交付的矛盾。');
        setAgentStatus(data.agent_id || 'critic', disputes.length ? 'challenged' : 'completed', { ...data, createdAt });
        addAgentCollaborationMessage(
            'critic',
            disputes.length ? '質疑' : '審查',
            detail,
            { createdAt, tone: disputes.length ? 'challenge' : '', agentId: data.agent_id || defaultAgentExecutor('critic'), model: data.model || '' }
        );
    } else if (eventType === 'agent_execution_state') {
        const role = normalizeAgentRole(data.role || data.name || data.agent_type);
        const stateLabels = {
            waiting_for_model: '等待模型插槽',
            loading_model: `正在載入 ${data.model || '角色模型'}`,
            connecting_provider: `正在連接 ${data.model || 'API 模型'}`,
            running: '正在執行工作',
            saving_handoff: '正在保存交接資料',
            unloading_model: `正在卸載 ${data.model || '角色模型'}`,
            verifying_release: '正在確認記憶體已釋放',
            closing_provider: 'API 回覆已完成，正在關閉連線',
            cancelled: '工作已取消'
        };
        if (stateLabels[data.state]) {
            addAgentCollaborationMessage(role, '模型排程', stateLabels[data.state], {
                createdAt, agentId: data.agent_id || defaultAgentExecutor(role), model: data.model || ''
            });
        }
    } else if (eventType === 'agent_message') {
        const role = normalizeAgentRole(data.role || data.name || data.agent_type);
        addAgentCollaborationMessage(role, data.tag || data.kind || '回報', data.message || data.summary, {
            createdAt, reply: !!data.reply_to, tone: data.kind === 'challenge' ? 'challenge' : data.kind === 'decision' ? 'decision' : '', evidence: data.evidence_ids || [],
            agentId: data.agent_id || '', model: data.model || ''
        });
    } else if (eventType === 'collaboration_dependency_ready') {
        const consumer = agentCollaborationState.agents[String(data.consumer_agent_id || '')];
        const producer = agentCollaborationState.agents[String(data.logical_producer_agent_id || data.producer_agent_id || '')];
        addAgentCollaborationMessage(
            consumer?.role || 'implementer',
            '接收交接',
            `${producer ? friendlyAgentExecutor(producer.role, producer.id) : '上游 Agent'} 的已驗證產物已送入本 Agent 私有上下文。`,
            { createdAt, agentId: data.consumer_agent_id || '', model: consumer?.model || '', evidence: [data.artifact_id || ''] }
        );
    } else if (eventType === 'handoff_superseded') {
        addAgentCollaborationMessage(
            'critic',
            '取代產物',
            `修復 Agent 已用新版產物取代被質疑版本（${String(data.old_artifact_id || '').slice(-8)} → ${String(data.new_artifact_id || '').slice(-8)}）。`,
            { createdAt, agentId: data.repair_agent_id || '', evidence: data.dispute_ids || [] }
        );    } else if (eventType === 'agent_completed' || eventType === 'agent_failed') {
        const role = normalizeAgentRole(data.role || data.name || data.agent_type);
        addAgentCollaborationMessage(role, eventType === 'agent_completed' ? '完成' : '阻塞', data.message || data.summary || (eventType === 'agent_completed' ? '子任務已完成。' : '子任務未完成。'), { createdAt, agentId: data.agent_id || '', model: data.model || '' });
        if (data.model_release?.state === 'protected' && data.model_release?.model) {
            agentCollaborationState.externalModels = [...new Set([...agentCollaborationState.externalModels, data.model_release.model])];
        }
    } else if (eventType === 'external_model_occupancy') {
        agentCollaborationState.externalModels = Array.isArray(data.models) ? data.models.map(String) : [];
    } else if (eventType === 'plan') {
        const tasks = Array.isArray(data.tasks) ? data.tasks : [];
        setAgentStatus('planner', 'completed');
        setAgentStatus('explorer', tasks.some(task => ['retrieve', 'inspect'].includes(task.id)) ? 'working' : 'idle');
        setAgentStatus('implementer', tasks.some(task => task.id === 'execute') ? 'queued' : 'idle');
        setAgentStatus('verifier', 'queued');
        addAgentCollaborationMessage('planner', '派工', `已建立 ${tasks.length} 項工作，依相依順序交給檢查、執行與驗證角色。`, {
            createdAt, agentId: data.agent_id || defaultAgentExecutor('planner')
        });
        setAgentCollaborationStep(1);
    } else if (eventType === 'task_update') {
        const role = agentTaskRole(data.task_id);
        const status = data.status === 'completed' ? 'completed' : data.status === 'failed' ? 'blocked' : 'working';
        setAgentStatus(role, status);
        addAgentCollaborationMessage(role, status === 'completed' ? '完成' : status === 'blocked' ? '阻塞' : '進度', data.message || `${data.task_id || '工作'}：${data.status || '更新'}`, {
            createdAt, agentId: data.agent_id || defaultAgentExecutor(role)
        });
        if (role === 'implementer') setAgentCollaborationStep(2);
        if (role === 'verifier') setAgentCollaborationStep(3);
    } else if (eventType === 'approval_required') {
        addAgentCollaborationMessage(
            'planner',
            '等待批准',
            data.message || `系統級能力 ${data.capability || ''} 等待使用者批准。`,
            { tone: 'challenge' }
        );
    } else if (eventType === 'approval_decided') {
        addAgentCollaborationMessage(
            'planner',
            data.approved ? '已批准' : '已拒絕',
            `${data.capability || '系統級能力'}：${data.approved ? '允許執行' : '不執行'}`,
            { tone: data.approved ? 'support' : 'challenge' }
        );
    } else if (eventType === 'tool_denied') {
        addAgentCollaborationMessage(
            'planner',
            '立即拒絕',
            data.message || `${data.capability || data.tool || '工具'}：本次請求不允許執行，未請求批准。`,
            { tone: 'challenge' }
        );
    } else if (eventType === 'repair_skipped') {
        addAgentCollaborationMessage(
            'critic',
            '不重試',
            data.message || `${data.tool || '工具'} 已被拒絕，屬終局結果，不進入修復輪次。`,
            { tone: 'challenge' }
        );
    } else if (eventType === 'deadline_exceeded') {
        addAgentCollaborationMessage(
            'planner',
            '逾時中止',
            data.message || '本次執行已超過絕對時間預算，已中止並釋放模型。',
            { tone: 'challenge' }
        );
    } else if (eventType === 'tool_start' || eventType === 'agent_tool_start') {
        const role = normalizeAgentRole(data.role || 'implementer');
        const tool = data.tool || data.tool_name || '工具';
        addAgentCollaborationMessage(role, '執行', `正在使用 ${tool} 取得可驗證結果。`, {
            createdAt, agentId: data.agent_id || defaultAgentExecutor(role), model: data.model || '', realAgent: !!data.agent_id
        });
        setAgentCollaborationStep(2);
    } else if (eventType === 'tool_end' || eventType === 'agent_tool_end') {
        const role = normalizeAgentRole(data.role || 'implementer');
        const tool = data.tool || data.tool_name || '工具';
        const success = data.success !== false && !String(data.result || '').toLowerCase().startsWith('error');
        addAgentCollaborationMessage(role, success ? '回報' : '失敗', `${tool}${success ? '已完成' : '未成功，等待診斷'}。`, {
            createdAt, agentId: data.agent_id || defaultAgentExecutor(role), model: data.model || '', realAgent: !!data.agent_id,
            evidence: success ? [`工具證據 ${data.sequence || data.tool_call_id || ''}`.trim()] : []
        });
    } else if (eventType === 'commentary' || eventType === 'progress') {
        const role = normalizeAgentRole(data.role || data.phase || (data.tool ? 'implementer' : 'planner'));
        addAgentCollaborationMessage(role, data.phase?.includes('after_tool') ? '回應' : '說明', data.message, {
            createdAt, reply: data.phase?.includes('after_tool'), agentId: data.agent_id || defaultAgentExecutor(role)
        });
    } else if (eventType === 'phase') {
        const role = normalizeAgentRole(data.phase);
        const phaseTag = data.phase === 'diagnose' ? '質疑' : data.phase === 'fix' ? '修正' : data.phase === 'finalize' ? '統整' : '進度';
        setAgentStatus(role, data.phase === 'diagnose' ? 'reviewing' : data.phase === 'fix' ? 'revising' : 'working');
        addAgentCollaborationMessage(role, phaseTag, data.message || data.phase, {
            createdAt, tone: data.phase === 'diagnose' ? 'challenge' : '', agentId: data.agent_id || defaultAgentExecutor(role)
        });
    } else if (eventType === 'repair') {
        setAgentStatus('critic', 'challenged');
        setAgentStatus('implementer', 'revising');
        const detail = data.reason || data.message || `第 ${data.round || '?'} 輪驗證未通過`;
        addAgentCollaborationMessage('critic', '質疑', detail, {
            createdAt, tone: 'challenge', agentId: data.critic_agent_id || defaultAgentExecutor('critic')
        });
        addAgentCollaborationMessage('implementer', '修正', `依第 ${data.round || '?'} 輪驗證意見進行最小修正。`, {
            createdAt, reply: true, agentId: data.agent_id || defaultAgentExecutor('implementer')
        });
    } else if (eventType === 'validation') {
        const passed = !!data.passed;
        setAgentStatus('verifier', passed ? 'verified' : 'challenged');
        addAgentCollaborationMessage('verifier', passed ? '通過' : '質疑', data.details || (passed ? '必要驗證已通過。' : '驗證未通過，要求修正。'), {
            createdAt, tone: passed ? '' : 'challenge', agentId: data.agent_id || defaultAgentExecutor('verifier')
        });
        setAgentCollaborationStep(3);
    } else if (eventType === 'context' || eventType === 'sources') {
        const sources = Array.isArray(data.sources) ? data.sources : [];
        if (sources.length) {
            setAgentStatus('explorer', 'completed');
            addAgentCollaborationMessage('explorer', '發現', `已取得 ${sources.length} 項可追溯的檢索來源。`, {
                createdAt, agentId: data.agent_id || defaultAgentExecutor('explorer'), evidence: [`來源 ${sources.length}`]
            });
        }
    } else if (eventType === 'resource_guard') {
        agentCollaborationState.resource = {
            ...agentCollaborationState.resource,
            state: 'running',
            estimatedPeak: data.effective_peak_gb,
            margin: Number(data.hardware?.safe_capacity_gb || 0),
            calibrationFactor: Number(data.calibration?.factor || 1),
            calibrationSamples: Number(data.calibration?.sample_count || 0)
        };
    } else if (eventType === 'resource_usage' || eventType === 'resource_summary') {
        const margin = Number(data.safety_margin_gb);
        const finalEvent = eventType === 'resource_summary';
        agentCollaborationState.resource = {
            ...agentCollaborationState.resource,
            state: margin < 0 ? 'danger' : margin < 2 ? 'caution' : finalEvent ? 'safe' : 'running',
            ramPeak: Number(data.ram_peak_delta_gb || 0),
            vramPeak: Number(data.vram_peak_delta_gb || 0),
            margin: Number.isFinite(margin) ? margin : null,
            estimatedPeak: Number(data.estimated_peak_gb || agentCollaborationState.resource.estimatedPeak || 0),
            actualPeak: Number(data.actual_peak_gb || 0),
            calibrationFactor: Number(data.calibration_factor_used || 1),
            calibrationSamples: Number(data.history_sample_count_after_run ?? data.calibration_sample_count ?? 0)
        };
    } else if (eventType === 'metrics') {
        const totalTokens = Number(data.usage?.total_tokens || 0);
        if (agentResourceTokens) {
            agentResourceTokens.textContent = totalTokens > 0
                ? totalTokens.toLocaleString()
                : '無精確資料';
        }
    } else if (eventType === 'token_budget') {
        addAgentCollaborationMessage(
            'planner',
            data.dispatch_blocked ? '預算停止派工' : 'Token 提醒',
            data.message || 'Token 預算狀態已更新。',
            { createdAt, tone: data.dispatch_blocked ? 'challenge' : '', agentId: data.agent_id || defaultAgentExecutor('planner') }
        );
    } else if (eventType === 'final') {
        setAgentStatus('planner', 'completed');
        addAgentCollaborationMessage('planner', '裁決', data.validation_passed === false ? '最終結果仍有未通過項目，已在報告中保留限制。' : '已根據工作紀錄、驗證與風險完成最終統整。', {
            createdAt, tone: 'decision', agentId: data.agent_id || defaultAgentExecutor('planner')
        });
        setAgentCollaborationStep(5);
    } else if (eventType === 'done') {
        agentCollaborationState.running = false;
        Object.values(agentCollaborationState.agents).forEach(agent => {
            if (AGENT_ACTIVE_STATES.has(agent.status) || agent.status === 'queued') setAgentStatus(agent.id, 'completed', { role: agent.role });
        });
        setAgentCollaborationStep(5);
    }
    if (shouldRender) {
        openAgentCollaboration(false);
        renderAgentCollaboration();
    }
}

function initAgentCollaboration() {
    if (!agentCollaborationPanel) return;
    try {
        const saved = Number(localStorage.getItem('agent-collaboration-width'));
        if (Number.isFinite(saved) && saved >= 300) agentCollaborationPanel.style.width = `${saved}px`;
    } catch (error) { /* localStorage 不可用時使用 CSS 預設寬度 */ }
    agentCollaborationClose?.addEventListener('click', () => closeAgentCollaboration(true, {
        focusTarget: document.getElementById('rail-agents'),
    }));
    agentResourceSummary?.addEventListener('click', () => {
        const expanded = agentResourceSummary.getAttribute('aria-expanded') === 'true';
        agentResourceSummary.setAttribute('aria-expanded', String(!expanded));
        agentResourceDetails.hidden = expanded;
    });
    agentExternalModelIndicator?.addEventListener('click', () => {
        const expanded = agentExternalModelIndicator.getAttribute('aria-expanded') === 'true';
        agentExternalModelIndicator.setAttribute('aria-expanded', String(!expanded));
        agentExternalModelDetails.hidden = expanded;
    });
    agentCollaborationStop?.addEventListener('click', () => {
        if (isGenerating) cancelActiveChatRun();
    });
    const railAgents = document.getElementById('rail-agents');
    if (BASIC_CHAT_MODE) return hideBasicAgentCollaborationUi(railAgents);
    railAgents?.addEventListener('click', () => {
        if (agentCollaborationPanel.hidden) {
            activateChatForAuxiliaryPanel();
            agentPanelDismissedForRun = false;
            openAgentCollaboration(true);
        } else {
            closeAgentCollaboration(true);
        }
    });
    const resizeTo = clientX => {
        const min = 300;
        const max = Math.min(620, window.innerWidth * 0.55);
        const width = Math.max(min, Math.min(max, window.innerWidth - clientX));
        agentCollaborationPanel.style.width = `${width}px`;
        return width;
    };
    agentCollaborationResizer?.addEventListener('pointerdown', event => {
        if (window.matchMedia('(max-width: 900px)').matches) return;
        event.preventDefault();
        agentCollaborationPanel.classList.add('resizing');
        agentCollaborationResizer.classList.add('resizing');
        agentCollaborationResizer.setPointerCapture?.(event.pointerId);
        const onMove = moveEvent => resizeTo(moveEvent.clientX);
        const onUp = upEvent => {
            const width = resizeTo(upEvent.clientX);
            agentCollaborationPanel.classList.remove('resizing');
            agentCollaborationResizer.classList.remove('resizing');
            agentCollaborationResizer.releasePointerCapture?.(event.pointerId);
            window.removeEventListener('pointermove', onMove);
            window.removeEventListener('pointerup', onUp);
            try { localStorage.setItem('agent-collaboration-width', String(Math.round(width))); } catch (error) {}
        };
        window.addEventListener('pointermove', onMove);
        window.addEventListener('pointerup', onUp, { once: true });
    });
    agentCollaborationResizer?.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        event.preventDefault();
        const current = agentCollaborationPanel.getBoundingClientRect().width;
        const next = Math.max(300, Math.min(Math.min(620, window.innerWidth * 0.55), current + (event.key === 'ArrowLeft' ? 20 : -20)));
        agentCollaborationPanel.style.width = `${next}px`;
        try { localStorage.setItem('agent-collaboration-width', String(Math.round(next))); } catch (error) {}
    });
    renderAgentCollaboration();
}

async function loadRagStatus() {
    if (BASIC_CHAT_MODE) return useBasicKnowledgeStatus();
    try {
        // 優先使用正式狀態 API（後端尚未提供時自動走 fallback）
        const res = await removedBasicFeature('Knowledge retrieval');
        if (res.ok) {
            const data = await res.json();
            kbStatus = { ...kbStatus, ...data };
            renderKbStatusLine();
            return;
        }
    } catch (e) { /* fallthrough */ }
    try {
        // Fallback：由 /api/documents 推導文件數與 chunk 數
        const res = await removedBasicFeature('Knowledge documents');
        const data = await res.json();
        const docs = data.documents || [];
        kbStatus.document_count = docs.length;
        kbStatus.chunk_count = docs.reduce((sum, d) => sum + (d.chunk_count || 0), 0);
        kbStatus.index_status = docs.length > 0 ? 'ready' : 'empty';
        renderKbStatusLine();
    } catch (e) {
        console.warn('[KB] 無法取得知識庫狀態:', e);
    }
}

function renderKbStatusLine() {
    const el = document.getElementById('kb-status-line');
    if (el) {
        el.textContent = kbStatus.index_status === 'ready'
            ? `${kbStatus.document_count} 文件 · ${kbStatus.chunk_count} chunks · 索引就緒`
            : '尚未匯入文件';
    }
    // Workbench：同步 Top Bar chip 與 Start Dashboard
    updateDocsChip();
    updateWelcomeDashboard();
}

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    initApp();
    setupEventListeners();
});

// 初始化 App
async function initApp() {
    initThemeToggle();
    initTaskProgress();
    initAgentCollaboration();
    window.workbenchProjectSkills?.init({
        apiFetch,
        apiBase: API_BASE,
        showToast,
        createIcons: safeCreateIcons,
        openContextMenu,
        closeContextMenu
    });
    window.workbenchRunInspector?.init({
        apiFetch,
        apiBase: API_BASE,
        showToast,
        createIcons: safeCreateIcons,
        retryRun: retryRunFromInspector,
        beforeOpen: prepareRunInspectorOpen,
    });
    const hermesSettingsContainer = document.getElementById('hermes-settings-container');
    if (hermesSettingsContainer) {
        void window.workbenchHermesSettings?.init({
            container: hermesSettingsContainer,
            apiFetch,
            apiBase: API_BASE,
            showToast,
            createIcons: safeCreateIcons
        });
    }
    safeCreateIcons();

    // 1. 系統狀態與專案資料互不相依，並行載入以縮短首屏等待。
    const statusPromise = checkSystemStatus();
    const sessionsPromise = loadSessions();
    const status = await statusPromise;
    if (getOllamaConnectionStatus(status) === 'connected') {
        await loadModels();
    } else {
        showSystemWarning('Ollama 未連線');
    }

    // 2. 確保側欄資料完成，再啟用完整 Workbench 控制器。
    await sessionsPromise;
    loadRagStatus(); // P1-2

    // 3. 載入第十階段高階功能控制器
    initSpeechRecognition();
    initDragAndDrop();
    initSlashCommands();
    initArtifactsControls();
    initSettingsControls();

    // 4. Workbench Shell（Rail / Chips / Dashboard / Wizard / Palette / Inspector）
    initWorkbench(status);
}

function getOllamaConnectionStatus(status) {
    if (!status) return 'disconnected';
    if (typeof status.ollama === 'string') return status.ollama;
    if (status.ollama && typeof status.ollama.status === 'string') return status.ollama.status;
    return status.ollama_legacy || 'disconnected';
}

// ===== 主題切換（暖紙淺色 / 深色玻璃） =====
function applyTheme(theme) {
    if (theme === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
    } else {
        document.documentElement.removeAttribute('data-theme'); // 預設即暖紙主題
    }
    // 更新切換鈕圖示：淺色顯示月亮（切去深色）、深色顯示太陽（切回淺色）
    const btn = document.getElementById('btn-theme-toggle');
    if (btn) {
        btn.innerHTML = `<i data-lucide="${theme === 'dark' ? 'sun' : 'moon'}" style="width: 16px; height: 16px;"></i>`;
        safeCreateIcons();
    }
}

function initThemeToggle() {
    let saved = 'paper';
    try { saved = localStorage.getItem('ui-theme') || 'paper'; } catch (e) {}
    applyTheme(saved);
    const btn = document.getElementById('btn-theme-toggle');
    if (btn) {
        btn.addEventListener('click', () => {
            const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
            const next = isDark ? 'paper' : 'dark';
            try { localStorage.setItem('ui-theme', next); } catch (e) {}
            applyTheme(next);
        });
    }
}

// 監聽器設定
function setupEventListeners() {
    // 輸入框高度自動調整與 Enter 送出
    userInput.addEventListener('input', () => {
        userInput.style.height = 'auto';
        userInput.style.height = userInput.scrollHeight + 'px';
    });

    userInput.addEventListener('keydown', (e) => {
        // 如果斜線選單 active，讓斜線選單的 Enter 處理，這裡直接 return
        if (slashCommandsMenu.classList.contains('active')) return;

        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            if (!isGenerating && (userInput.value.trim() || currentImages.length > 0) && !sendBtn.disabled) {
                chatForm.dispatchEvent(new Event('submit'));
            }
        }
    });

    // 對話送出
    chatForm.addEventListener('submit', handleChatSubmit);
    // 生成期間按鈕會改成 type=button，避免空白 required 輸入框攔截停止事件。
    sendBtn.addEventListener('click', async event => {
        if (!isGenerating) return;
        event.preventDefault();
        event.stopPropagation();
        await cancelActiveChatRun();
    });

    // 新增對話會話
    newChatBtn.addEventListener('click', () => createNewSession(activeProjectId));
    newProjectBtn.addEventListener('click', createNewProject);
    projectSwitcherBtn.addEventListener('click', (event) => {
        event.stopPropagation();
        projectSwitcherPopover.hidden = !projectSwitcherPopover.hidden;
        projectSwitcherBtn.setAttribute('aria-expanded', String(!projectSwitcherPopover.hidden));
        if (!projectSwitcherPopover.hidden) {
            projectSwitcherSearch.value = '';
            renderProjectSwitcher();
            projectSwitcherSearch.focus();
        }
    });
    projectSwitcherSearch.addEventListener('input', renderProjectSwitcher);
    document.getElementById('project-switcher-new').addEventListener('click', async () => {
        projectSwitcherPopover.hidden = true;
        await createNewProject();
    });
    document.getElementById('project-switcher-independent').addEventListener('click', () => selectProjectWorkspace(null));
    projectBrowseButton.addEventListener('click', browseProjectFolder);
    folderBrowserUp.addEventListener('click', () => folderBrowserParentPath && loadFolderDirectory(folderBrowserParentPath));
    folderBrowserSelect.addEventListener('click', () => closeFolderBrowser(folderBrowserCurrentPath));
    document.getElementById('folder-browser-close').addEventListener('click', () => closeFolderBrowser(null));
    document.getElementById('folder-browser-cancel').addEventListener('click', () => closeFolderBrowser(null));
    folderBrowserDialog.addEventListener('cancel', event => {
        event.preventDefault();
        closeFolderBrowser(null);
    });

    // 搜尋歷史會話
    searchSessionsInput.addEventListener('input', (e) => {
        loadSessions(e.target.value.trim());
    });

    document.getElementById('sidebar-dialog-close').addEventListener('click', () => sidebarDialog.close('cancel'));
    document.getElementById('sidebar-dialog-cancel').addEventListener('click', () => sidebarDialog.close('cancel'));
    document.addEventListener('click', (e) => {
        if (!sidebarContextMenu.hidden && !e.target.closest('#sidebar-context-menu') && !e.target.closest('.sidebar-menu-btn')) {
            sidebarContextMenu.hidden = true;
        }
        if (!projectSwitcherPopover.hidden && !e.target.closest('.project-switcher')) {
            projectSwitcherPopover.hidden = true;
            projectSwitcherBtn.setAttribute('aria-expanded', 'false');
        }
    });
    document.addEventListener('pointermove', handleSidebarPointerMove);
    document.addEventListener('pointerup', finishSidebarPointerDrag);
    document.addEventListener('pointercancel', cancelSidebarPointerDrag);
    document.addEventListener('pointermove', handleSidebarProjectPointerMove);
    document.addEventListener('pointerup', finishSidebarProjectPointerDrag);
    document.addEventListener('pointercancel', cancelSidebarProjectPointerDrag);

    // 圖片📎按鈕點擊上傳
    imgUploadBtn.addEventListener('click', () => imgFileInput.click());
    imgFileInput.addEventListener('change', handleImageUploadSelect);

    // 語音輸入按鈕
    voiceInputBtn.addEventListener('click', () => {
        if (isRecording) {
            stopRecording();
        } else {
            if (speechRecognition) {
                speechRecognition.start();
            } else {
                showToast('您的瀏覽器不支援 Web Speech 語音辨識輸入。');
            }
        }
    });

    // 移除臨時 context 按鈕
    btnRemoveTempContext.addEventListener('click', () => {
        clearTemporaryContext();
    });

    // 剪貼簿 paste 貼上圖片監聽
    userInput.addEventListener('paste', handleImagePaste);

    // 知識庫管理 Modal 切換控制
    manageKbBtn.addEventListener('click', () => {
        kbManagerModal.classList.add('active');
        loadKBFiles();
    });
    
    const closeKBModal = () => {
        kbManagerModal.classList.remove('active');
        chunksPreviewSection.style.display = 'none';
    };
    kbModalClose.addEventListener('click', closeKBModal);
    kbModalCloseBtn.addEventListener('click', closeKBModal);

    // 知識庫拖曳上傳
    uploadZone.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', (e) => handleFilesSelect(e.target.files));

    ['dragenter', 'dragover'].forEach(eventName => {
        uploadZone.addEventListener(eventName, (e) => {
            e.preventDefault();
            uploadZone.classList.add('dragover');
        }, false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
        uploadZone.addEventListener(eventName, (e) => {
            e.preventDefault();
            uploadZone.classList.remove('dragover');
        }, false);
    });

    uploadZone.addEventListener('drop', (e) => {
        const dt = e.dataTransfer;
        handleFilesSelect(dt.files);
    });

    // 清空整個知識庫 Modal 交互
    clearAllKbBtn.addEventListener('click', () => {
        confirmModal.classList.add('active');
    });
    modalClose2.addEventListener('click', () => confirmModal.classList.remove('active'));
    modalCancel.addEventListener('click', () => confirmModal.classList.remove('active'));
    modalConfirm.addEventListener('click', handleClearDatabase);

    // 關閉 Chunk 預覽
    closeChunksBtn.addEventListener('click', () => {
        chunksPreviewSection.style.display = 'none';
    });

    // 模型切換同步標題
    modelSelect.addEventListener('change', () => {
        if (modelSelect.value) {
            activeModelName.textContent = modelSelect.value;
        }
    });
}

// 系統狀態檢查
async function checkSystemStatus() {
    try {
        const res = await apiFetch(`${API_BASE}/api/status`);
        const data = await res.json();
        
        if (data.status === 'ok') {
            statusIndicator.className = 'status-indicator ok';
            statusText.textContent = '系統就緒';
            sendBtn.disabled = false;
        } else {
            showSystemWarning('Ollama 未啟動');
        }
        return data;
    } catch (e) {
        showSystemWarning('後端未啟動');
        return { status: 'error', ollama: 'disconnected' };
    }
}

function showSystemWarning(text) {
    statusIndicator.className = 'status-indicator warning';
    statusText.textContent = text;
    sendBtn.disabled = true;
}

// 載入模型清單
function specializedModelKindFromName(modelName = '') {
    const name = String(modelName || '').trim().toLowerCase();
    if (/(?:riva-|\/|-)?translat(?:e|ion)/.test(name)) return 'translation';
    if (/rerank|re-rank|ranker/.test(name)) return 'rerank';
    if (/(?:\/|-)(?:embed|embedding)|text-embedding|\/bge-|(?:^|\/)e5-/.test(name)) return 'embedding';
    return '';
}
function modelEligibleForChat(entry, modelName = '') {
    // Fail closed for recognizable specialized endpoints even if metadata is
    // missing or stale. This is a UI guard; the backend remains authoritative.
    if (specializedModelKindFromName(modelName || entry?.name)) return false;
    if (!entry) return true;
    if (entry.eligible_for_chat === false) return false;
    return !entry.model_kind || String(entry.model_kind).toLowerCase() === 'chat';
}
function modelEligibleForRole(entry, role, modelName = '') {
    if (!modelEligibleForChat(entry, modelName)) return false;
    const roles = entry?.eligible_roles;
    return !Array.isArray(roles) || roles.length === 0 || roles.includes(role);
}
async function loadModels() {
    try {
        const res = await apiFetch(`${API_BASE}/api/models`);
        const data = await res.json();
        const configured = Array.isArray(data.configured_models) ? data.configured_models : [];
        const configuredByName = new Map(configured.map(item => [item.name, item]));
        const models = (Array.isArray(data.models) ? data.models : [])
            .filter(name => modelEligibleForChat(configuredByName.get(name), name));
        modelSelect.innerHTML = '';
        if (models.length > 0) {
            models.forEach(model => {
                const opt = document.createElement('option');
                opt.value = model;
                opt.textContent = model;
                modelSelect.appendChild(opt);
            });
            if (models.includes(currentSettings.default_chat_model)) {
                modelSelect.value = currentSettings.default_chat_model;
            }
            activeModelName.textContent = modelSelect.value;
            sendBtn.disabled = false;
            updateWelcomeDashboard();
        } else {
            const opt = document.createElement('option');
            opt.value = '';
            opt.textContent = configured.length ? 'API 模型尚未啟用' : '尚未安裝模型';
            modelSelect.appendChild(opt);
            activeModelName.textContent = opt.textContent;
            sendBtn.disabled = true;
            updateWelcomeDashboard();
        }
        window.workbenchN8nWorkflows?.refreshModels?.();
        return data;
    } catch (e) {
        console.error('Failed to load models:', e);
        return { models: [], configured_models: [] };
    }
}

function createClientRunId() {
    const raw = globalThis.crypto?.randomUUID?.().replace(/-/g, '') || `${Date.now()}_${Math.random().toString(16).slice(2)}`;
    return `run_${raw}`;
}

async function cancelActiveChatRun() {
    if (!isGenerating || isCancellingGeneration) return;
    isCancellingGeneration = true;
    window.workbenchRunInspector?.cancelPendingApprovals?.('目前執行已停止。');
    setGeneratingUI(true, true);
    const runId = currentChatRunId;
    const activeAbort = chatAbort;
    const cancelRequest = runId
        ? apiFetch(`${API_BASE}/api/chat/runs/${encodeURIComponent(runId)}/cancel`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
            keepalive: true
        }).then(async response => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.json();
        }).catch(error => {
            console.warn('Failed to notify backend cancellation:', error);
            return null;
        })
        : Promise.resolve(null);
    agentCollaborationState.running = false;
    addAgentCollaborationMessage('planner', '停止', runId ? '正在停止後端 Agent 與 Ollama 請求。' : '正在停止生成。');
    renderAgentCollaboration();
    // 先給取消 API 一個短暫機會關閉後端 Ollama / Subagent 連線，
    // 再中止前端串流；即使後端暫時無回應，UI 仍能立即結束。
    await Promise.race([
        cancelRequest,
        new Promise(resolve => setTimeout(resolve, 250))
    ]);
    if (activeAbort) activeAbort.abort();
    const cancelResult = await cancelRequest;
    const cleanup = cancelResult?.cleanup;
    if (cleanup) {
        const cleanupFailed = cleanup.state === 'warning' || cleanup.state === 'unavailable';
        agentCollaborationState.resource.state = cleanupFailed
            ? 'danger'
            : cleanup.timed_out ? 'caution' : 'safe';
        renderAgentCollaboration();
    }
    if (cleanup?.warning) {
        const cleanupFailed = cleanup.state === 'warning' || cleanup.state === 'unavailable';
        const tag = cleanup.cleanup_performed ? '受控清理' : '資源警告';
        addAgentCollaborationMessage('planner', tag, cleanup.message || 'Ollama 停止後的資源釋放需要注意。', {
            tone: cleanupFailed ? 'challenge' : 'decision'
        });
        renderAgentCollaboration();
        showToast(cleanup.message || 'Ollama 停止後的資源釋放需要注意。', cleanupFailed ? 'error' : 'warning');
    }
}

async function loadSubagentModelOptions(selectedModels = {}) {
    const roleSelects = {
        planner: settingSubagentPlannerModel,
        explorer: settingSubagentExplorerModel,
        implementer: settingSubagentImplementerModel,
        critic: settingSubagentCriticModel
    };
    if (Object.values(roleSelects).some(select => !select)) return;
    let models = [], configuredByName = new Map();
    try {
        const response = await apiFetch(`${API_BASE}/api/models`);
        const data = await response.json();
        models = Array.isArray(data.models) ? data.models : [];
        configuredByName = new Map((Array.isArray(data.configured_models) ? data.configured_models : []).map(item => [item.name, item]));
    } catch (error) {
        console.warn('Failed to load Subagent models:', error);
    }
    Object.entries(roleSelects).forEach(([role, select]) => {
        const selected = String(selectedModels[role] || '');
        select.innerHTML = '';
        const inherited = document.createElement('option');
        const eligibleModels = models.filter(model => modelEligibleForRole(configuredByName.get(model), role, model));
        const selectedAllowed = modelEligibleForRole(configuredByName.get(selected), role, selected);
        inherited.value = '';
        const primaryModel = settingChatModel.value.trim() || modelSelect.value || '尚未選擇';
        inherited.textContent = `沿用主模型：${primaryModel}（建議）`;
        select.appendChild(inherited);
        eligibleModels.forEach(model => {
            const option = document.createElement('option');
            option.value = model;
            option.textContent = model;
            select.appendChild(option);
        });
        if (selected && selectedAllowed && !eligibleModels.includes(selected)) {
            const unavailable = document.createElement('option');
            unavailable.value = selected;
            unavailable.textContent = `${selected}（目前未偵測到）`;
            select.appendChild(unavailable);
        }
        select.value = selectedAllowed ? selected : '';
    });
    syncSubagentSettingsEnabled();
    await updateSubagentResourcePlan();
}

function syncSubagentSettingsEnabled() {
    const enabled = settingSubagentEnabled?.checked !== false;
    [settingSubagentPlannerModel, settingSubagentExplorerModel, settingSubagentImplementerModel, settingSubagentCriticModel, settingSubagentCloudRouting, settingSubagentMaxParallel]
        .filter(Boolean)
        .forEach(control => { control.disabled = !enabled; });
}

async function updateSubagentResourcePlan(showAdjustmentToast = false) {
    if (BASIC_CHAT_MODE) return renderBasicSubagentStatus(subagentResourcePlan);
    if (!subagentResourcePlan || !settingSubagentEnabled?.checked) {
        if (subagentResourcePlan) {
            subagentResourcePlan.className = 'subagent-resource-plan';
            subagentResourcePlan.innerHTML = '<div class="subagent-resource-plan-title">Subagent Runtime 已停用，不會載入角色模型。</div>';
        }
        return null;
    }
    subagentResourcePlan.className = 'subagent-resource-plan is-loading';
    subagentResourcePlan.innerHTML = '<div class="subagent-resource-plan-title">正在估算角色模型記憶體需求…</div>';
    const requestedParallel = parseInt(settingSubagentMaxParallel.value) || 1;
    try {
        const response = await removedBasicFeature('Subagent resource planning', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                primary_model: settingChatModel.value.trim() || modelSelect.value || '',
                max_parallel: requestedParallel, allow_cloud_routing: !!settingSubagentCloudRouting?.checked,
                models: {
                    planner: settingSubagentPlannerModel.value || '',
                    explorer: settingSubagentExplorerModel.value || '',
                    implementer: settingSubagentImplementerModel.value || '',
                    critic: settingSubagentCriticModel.value || ''
                }
            })
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const plan = await response.json();
        const safeParallel = Math.max(1, parseInt(plan.recommended_max_parallel) || 1);
        Array.from(settingSubagentMaxParallel.options).forEach(option => {
            option.disabled = Number(option.value) > safeParallel;
        });
        if (requestedParallel > safeParallel) {
            settingSubagentMaxParallel.value = String(safeParallel);
            if (showAdjustmentToast) showToast(`為避免記憶體不足，Subagent 並行數已調整為 ${safeParallel}。`, 'warning');
        }
        const roles = (plan.roles || []).map(role => `
            <div class="subagent-resource-role">
                <strong>${escapeHtml(role.role)}</strong>
                <span title="${escapeHtml(role.model || '未選擇')}">${escapeHtml(role.model || '未選擇')}</span>
                <span>檔案 ${Number(role.size_gb || 0).toFixed(1)}GB · 估計 ${Number(role.estimated_memory_gb || 0).toFixed(1)}GB</span>
            </div>
        `).join('');
        const conflicts = (plan.conflicts || []).map(item => `<li>${escapeHtml(item)}</li>`).join(''), localCapacity = Number(plan.local_parallel_capacity || 0), remoteCapacity = Number(plan.remote_parallel_capacity || 0), routingPreview = Object.values(plan.routing_preview || {}).filter(item => item && item.model).map(item => `${item.execution_class === 'remote' ? '雲端' : '本機'} ${item.model}`).join('、');
        subagentResourcePlan.className = `subagent-resource-plan is-${plan.risk || 'caution'}`;
        subagentResourcePlan.innerHTML = `
            <div class="subagent-resource-plan-title">${escapeHtml(plan.message || '資源估算完成')}</div>
            <div class="subagent-resource-role-list">${roles}</div>
            <div>預估峰值 ${Number(plan.effective_peak_gb || 0).toFixed(1)}GB／安全額度 ${Number(plan.hardware?.safe_capacity_gb || 0).toFixed(1)}GB；本機通道 ${localCapacity}、遠端通道 ${remoteCapacity}。</div>${routingPreview ? `<div>智慧改派預覽：${escapeHtml(routingPreview)}</div>` : ''}
            ${conflicts ? `<ul class="subagent-resource-conflicts">${conflicts}</ul>` : ''}
        `;
        return plan;
    } catch (error) {
        subagentResourcePlan.className = 'subagent-resource-plan is-caution';
        subagentResourcePlan.innerHTML = '<div class="subagent-resource-plan-title">目前無法取得硬體估算，並行數將維持最安全的 1。</div>';
        settingSubagentMaxParallel.value = '1';
        Array.from(settingSubagentMaxParallel.options).forEach(option => { option.disabled = Number(option.value) > 1; });
        return null;
    }
}

// ==========================================================================
// 1. 會話歷史持久化邏輯 (Sessions)
// ==========================================================================

function renderOutputSkillsState(message) {
    if (!outputSkillsMount) return;
    const state = document.createElement('div');
    state.className = 'output-panel-empty';
    state.textContent = message;
    outputSkillsMount.replaceChildren(state);
}

function renderOutputSkillsPane(projectId = activeProjectId, message = '') {
    if (!outputSkillsMount || !outputPanelProject) return;
    if (message) {
        outputPanelProject.textContent = '專案切換中';
        renderOutputSkillsState(message);
        return;
    }
    const project = projectId
        ? sidebarProjects.find(item => item.id === projectId)
        : null;
    if (!project) {
        outputPanelProject.textContent = '尚未選擇專案';
        renderOutputSkillsState('請先選擇專案');
        return;
    }
    outputPanelProject.textContent = project.name || '目前專案';
    const section = window.workbenchProjectSkills?.createProjectSection(project, {
        surface: 'output',
        alwaysExpanded: true,
        autoLoad: true,
    });
    if (!section) {
        renderOutputSkillsState('目前無法顯示 Skills。');
        return;
    }
    outputSkillsMount.replaceChildren(section);
    safeCreateIcons();
}

function syncRunInspectorContext(projectId = activeProjectId, projectName = '') {
    const project = projectId ? sidebarProjects.find(item => item.id === projectId) : null;
    void window.workbenchRunInspector?.setContext({
        sessionId: currentSessionId,
        projectId: project?.id || projectId || null,
        projectName: projectName || project?.name || '',
    });
}

function clearOutputSkillsContext(message = '正在切換專案…') {
    window.workbenchProjectSkills?.setSessionContext({ sessionId: null, projectId: null });
    void window.workbenchRunInspector?.setContext({ sessionId: null, projectId: null, projectName: '' });
    renderOutputSkillsPane(null, message);
}

function setOutputFloatingPanelOpen(open, { restoreFocus = false } = {}) {
    if (open === true && !window.workbenchRunInspector?.isOpen()) {
        window.workbenchRunInspector?.selectTab('skills');
    } else if (open !== true && window.workbenchRunInspector?.isOpen()) {
        window.workbenchRunInspector?.selectTab(
            window.workbenchRunInspector.getState().activeTab,
            { toggle: true, focus: restoreFocus }
        );
    }
}

async function loadSessions(searchVal = '') {
    try {
        sidebarSearch = searchVal.toLocaleLowerCase();
        const [sessionsRes, projectsRes] = await Promise.all([
            apiFetch(`${API_BASE}/api/sessions`),
            apiFetch(`${API_BASE}/api/projects`)
        ]);
        if (!sessionsRes.ok || !projectsRes.ok) throw new Error('Sidebar data request failed');
        sidebarSessions = (await sessionsRes.json()).sessions || [];
        sidebarProjects = (await projectsRes.json()).projects || [];
        const currentSession = sidebarSessions.find(session => session.id === currentSessionId);
        if (currentSession) activeProjectId = currentSession.project_id || null;
        window.workbenchProjectSkills?.setSessionContext({
            sessionId: currentSessionId,
            projectId: currentSession?.project_id || null
        });
        renderOutputSkillsPane(currentSession?.project_id || null);
        syncRunInspectorContext(currentSession?.project_id || null, currentSession?.project_name || '');
        renderSidebar();
        renderProjectSwitcher();
        window.workbenchN8nWorkflows?.refreshProjects?.();
        window.workbenchN8nGovernance?.refreshProjects?.();
        safeCreateIcons();
    } catch (e) {
        console.error('Failed to load sessions:', e);
        sessionList.innerHTML = `<div class="empty-sessions">無法載入工作區</div>`;
        outputPanelProject.textContent = '載入失敗';
        renderOutputSkillsState('無法載入目前專案的 Skills。');
    }
}

function matchesSidebarSearch(session) {
    if (!sidebarSearch) return true;
    return `${session.title || ''} ${session.project_name || ''}`.toLocaleLowerCase().includes(sidebarSearch);
}

function matchesProjectSearch(project, sessions) {
    if (!sidebarSearch) return true;
    return project.name.toLocaleLowerCase().includes(sidebarSearch)
        || sessions.some(session => session.project_id === project.id);
}

function renderSidebar() {
    sessionList.innerHTML = '';
    const allActiveProjects = sidebarProjects.filter(project => !project.archived);
    const allArchivedProjects = sidebarProjects.filter(project => project.archived);
    const archivedProjectIds = new Set(allArchivedProjects.map(project => project.id));
    const activeSessions = sidebarSessions.filter(session =>
        !session.archived && !archivedProjectIds.has(session.project_id) && matchesSidebarSearch(session)
    );
    const archivedSessions = sidebarSessions.filter(session =>
        (session.archived || archivedProjectIds.has(session.project_id)) && matchesSidebarSearch(session)
    );
    const activeProjects = allActiveProjects.filter(project => matchesProjectSearch(project, activeSessions));
    const archivedProjects = allArchivedProjects.filter(project => matchesProjectSearch(project, archivedSessions));

    sessionList.appendChild(createSidebarSection('專案', 'projects', activeProjects, activeSessions));
    sessionList.appendChild(createSidebarSection('獨立任務', 'independent', [], activeSessions.filter(session => !session.project_id)));
    sessionList.appendChild(createArchiveSection(archivedProjects, archivedSessions));
    safeCreateIcons();
}

function renderProjectSwitcher() {
    const active = sidebarProjects.find(project => project.id === activeProjectId);
    projectSwitcherLabel.textContent = active ? active.name : '不在專案中工作';
    const query = (projectSwitcherSearch.value || '').trim().toLocaleLowerCase();
    const visible = sidebarProjects.filter(project => !project.archived && (!query || `${project.name} ${project.root_path}`.toLocaleLowerCase().includes(query)));
    projectSwitcherList.innerHTML = visible.map(project => {
        const selected = project.id === activeProjectId;
        const unhealthy = !['ready', 'read_only'].includes(project.path_status);
        return `<button type="button" data-switch-project="${escapeHtml(project.id)}" title="${escapeHtml(project.root_path)}">
            <i data-lucide="folder${unhealthy ? '-x' : ''}" class="${unhealthy ? 'path-warning' : ''}"></i>
            <span>${project.pinned ? '<i data-lucide="pin" class="project-switcher-pin" aria-label="已釘選"></i>' : ''}${escapeHtml(project.name)}</span>
            ${selected ? '<i data-lucide="check"></i>' : '<span></span>'}
            <span class="project-path ${unhealthy ? 'path-warning' : ''}">${escapeHtml(unhealthy ? '路徑無法使用' : project.root_path)}</span>
        </button>`;
    }).join('') || '<div class="sidebar-empty-row">沒有符合的專案</div>';
    projectSwitcherList.querySelectorAll('[data-switch-project]').forEach(button => {
        button.addEventListener('click', () => selectProjectWorkspace(button.dataset.switchProject));
    });
    safeCreateIcons();
}

async function selectProjectWorkspace(projectId) {
    activeProjectId = projectId;
    projectSwitcherPopover.hidden = true;
    projectSwitcherBtn.setAttribute('aria-expanded', 'false');
    renderProjectSwitcher();
    await createNewSession(projectId);
}

function createSidebarSection(title, type, projects, sessions) {
    const section = document.createElement('section');
    section.className = 'sidebar-group';
    section.dataset.sidebarSection = type;
    if (type === 'independent') {
        section.dataset.dropProjectId = '';
    } else if (type === 'projects') {
        section.dataset.projectReorderZone = '';
    }
    const heading = document.createElement('div');
    heading.className = 'sidebar-group-heading';
    heading.innerHTML = `<span>${title}</span>${type === 'projects' ? '<button class="sidebar-icon-btn sidebar-group-add" title="新增專案" aria-label="新增專案"><i data-lucide="plus"></i></button>' : ''}`;
    if (type === 'projects') heading.querySelector('button').addEventListener('click', createNewProject);
    section.appendChild(heading);

    if (type === 'projects') {
        projects.forEach(project => section.appendChild(createProjectBlock(project, sessions)));
        if (!projects.length) {
            const empty = emptySidebarRow(sidebarSearch ? '沒有符合的專案' : '尚無專案');
            if (!sidebarSearch) empty.dataset.createProjectDrop = 'true';
            section.appendChild(empty);
        }
    } else {
        const independent = sessions.filter(session => !session.project_id);
        independent.forEach(session => section.appendChild(createSessionRow(session, true)));
        if (!independent.length) section.appendChild(emptySidebarRow(sidebarSearch ? '沒有符合的任務' : '尚無獨立任務'));
    }
    return section;
}

function createProjectBlock(project, sessions) {
    const block = document.createElement('div');
    block.className = 'sidebar-project';
    block.dataset.projectBlockId = project.id;
    block.dataset.taskDropProjectId = project.id;
    const row = document.createElement('div');
    row.className = `project-row ${project.pinned ? 'is-pinned' : ''}`.trim();
    row.dataset.projectId = project.id;
    row.title = `${project.name}\n${project.root_path}`;
    if (!sidebarSearch) row.classList.add('draggable-project');
    row.innerHTML = `
        <button class="project-toggle" aria-label="${project.expanded ? '收合' : '展開'} ${escapeHtml(project.name)}"><i data-lucide="chevron-right"></i></button>
        <i data-lucide="folder" class="project-icon"></i>
        ${project.pinned ? '<i data-lucide="pin" class="project-pin" aria-label="已釘選"></i>' : ''}
        <span class="project-name">${escapeHtml(project.name)}</span>
        ${!['ready', 'read_only'].includes(project.path_status) ? '<i data-lucide="triangle-alert" class="project-path-status" aria-label="專案路徑無法使用"></i>' : ''}
        <span class="project-count">${project.active_task_count || 0}</span>
        <button class="sidebar-menu-btn" aria-label="${escapeHtml(project.name)} 選單"><i data-lucide="ellipsis"></i></button>`;
    row.querySelector('.project-toggle').classList.toggle('expanded', !!project.expanded);
    row.querySelector('.project-toggle').addEventListener('click', async (event) => {
        event.stopPropagation();
        await patchProject(project.id, { expanded: !project.expanded });
    });
    row.querySelector('.sidebar-menu-btn').addEventListener('click', event => openProjectMenu(event, project));
    row.addEventListener('dblclick', () => createNewSession(project.id));
    if (!sidebarSearch) {
        row.addEventListener('pointerdown', event => {
            if (event.button !== 0 || event.pointerType !== 'mouse' || event.target.closest('button')) return;
            sidebarProjectDrag = { projectId: project.id, row, block, startX: event.clientX, startY: event.clientY, moved: false };
            row.setPointerCapture?.(event.pointerId);
        });
    }
    block.appendChild(row);

    if (project.expanded || sidebarSearch) {
        const matching = sessions.filter(session => session.project_id === project.id && matchesSidebarSearch(session));
        const visible = (expandedTaskLists.has(project.id) || sidebarSearch) ? matching : matching.slice(0, 5);
        const taskList = document.createElement('div');
        taskList.className = 'project-task-list';
        visible.forEach(session => taskList.appendChild(createSessionRow(session)));
        if (!matching.length) taskList.appendChild(emptySidebarRow(sidebarSearch ? '沒有符合的任務' : '尚無任務'));
        if (matching.length > 5 && !sidebarSearch) {
            const showMore = document.createElement('button');
            showMore.className = 'sidebar-show-more';
            showMore.textContent = expandedTaskLists.has(project.id) ? '顯示較少' : `顯示更多（${matching.length - 5}）`;
            showMore.addEventListener('click', () => {
                expandedTaskLists.has(project.id) ? expandedTaskLists.delete(project.id) : expandedTaskLists.add(project.id);
                renderSidebar();
            });
            taskList.appendChild(showMore);
        }
        block.appendChild(taskList);
    }
    return block;
}

function createSessionRow(session, independent = false) {
    const row = document.createElement('div');
    const status = ['running', 'waiting', 'failed', 'generating', 'completed'].includes(session.status) ? session.status : 'idle';
    const statusLabels = {
        running: '執行中',
        waiting: '等待 Planner 決策',
        failed: '執行失敗',
        generating: '正在生成',
        completed: '已完成'
    };
    const statusContent = status === 'completed' ? '<i data-lucide="check"></i>' : '';
    row.className = `session-item ${session.id === currentSessionId ? 'active' : ''}`;
    row.dataset.sessionId = session.id;
    row.title = session.title;
    if (!session.archived && !sidebarSearch) row.classList.add('draggable-task');
    row.innerHTML = `
        <span class="task-status ${status}" ${status === 'idle' ? 'aria-hidden="true"' : `aria-label="${statusLabels[status]}"`}>${statusContent}</span>
        <span class="session-title-wrap">
            <span class="session-item-title">${escapeHtml(session.title)}</span>
            ${sidebarSearch && session.project_name ? `<span class="session-project-label">${escapeHtml(session.project_name)}</span>` : ''}
        </span>
        ${session.pinned ? '<i data-lucide="pin" class="task-pin" aria-label="已釘選"></i>' : ''}
        <button class="sidebar-menu-btn" aria-label="${escapeHtml(session.title)} 選單"><i data-lucide="ellipsis"></i></button>`;
    if (!independent) row.classList.add('project-task');
    row.querySelector('.sidebar-menu-btn').addEventListener('click', event => openSessionMenu(event, session));
    row.addEventListener('click', event => {
        if (row.dataset.suppressClick === 'true') {
            event.preventDefault();
            delete row.dataset.suppressClick;
            return;
        }
        if (!event.target.closest('.sidebar-menu-btn')) changeSession(session.id);
    });
    if (!session.archived && !sidebarSearch) {
        row.addEventListener('pointerdown', event => {
            if (event.button !== 0 || event.pointerType !== 'mouse' || event.target.closest('button')) return;
            sidebarPointerDrag = { sessionId: session.id, row, startX: event.clientX, startY: event.clientY, moved: false };
            row.setPointerCapture?.(event.pointerId);
        });
    }
    return row;
}

function findSidebarDropTarget(x, y) {
    const hit = document.elementFromPoint(x, y);
    const direct = hit?.closest('.session-item, [data-create-project-drop], [data-task-drop-project-id], [data-drop-project-id]');
    if (direct) return direct;
    const projectZone = hit?.closest('[data-project-reorder-zone]');
    const projectBlocks = projectZone ? [...projectZone.querySelectorAll('.sidebar-project:not(.archived-project)')] : [];
    if (!projectBlocks.length) return null;
    return projectBlocks.reduce((nearest, block) => {
        const distance = Math.abs(y - (block.getBoundingClientRect().top + block.getBoundingClientRect().height / 2));
        return !nearest || distance < nearest.distance ? { block, distance } : nearest;
    }, null).block;
}

function scrollSidebarDuringDrag(pointerY) {
    const bounds = sessionList.getBoundingClientRect();
    const edge = 36;
    if (pointerY < bounds.top + edge) sessionList.scrollTop -= 14;
    if (pointerY > bounds.bottom - edge) sessionList.scrollTop += 14;
}

function clearSidebarTaskTarget(target) {
    target?.classList.remove('session-drop-target', 'session-drop-before', 'session-drop-after');
}

function setSidebarPointerTarget(target, placement) {
    if (sidebarPointerTarget === target && sidebarPointerPlacement === placement) return;
    clearSidebarTaskTarget(sidebarPointerTarget);
    sidebarPointerTarget = target;
    sidebarPointerPlacement = placement;
    if (!target) return;
    target.classList.add(placement === 'inside' ? 'session-drop-target' : `session-drop-${placement}`);
}

function handleSidebarPointerMove(event) {
    if (!sidebarPointerDrag) return;
    const distance = Math.hypot(event.clientX - sidebarPointerDrag.startX, event.clientY - sidebarPointerDrag.startY);
    if (!sidebarPointerDrag.moved && distance < 6) return;
    sidebarPointerDrag.moved = true;
    sidebarPointerDrag.row.classList.add('dragging');
    scrollSidebarDuringDrag(event.clientY);
    const target = findSidebarDropTarget(event.clientX, event.clientY);
    const placement = target?.classList.contains('session-item')
        ? (event.clientY < target.getBoundingClientRect().top + target.getBoundingClientRect().height / 2 ? 'before' : 'after')
        : 'inside';
    setSidebarPointerTarget(target, placement);
}

async function finishSidebarPointerDrag(event) {
    if (!sidebarPointerDrag) return;
    const drag = sidebarPointerDrag;
    const target = sidebarPointerTarget || findSidebarDropTarget(event.clientX, event.clientY);
    const placement = sidebarPointerPlacement;
    cancelSidebarPointerDrag();
    if (!drag.moved || !target) return;
    drag.row.dataset.suppressClick = 'true';
    setTimeout(() => delete drag.row.dataset.suppressClick, 0);
    const session = sidebarSessions.find(item => item.id === drag.sessionId);
    if (!session) return;
    if (target.dataset.createProjectDrop === 'true') {
        await createProjectForSession(drag.sessionId);
        return;
    }
    const targetSession = target.classList.contains('session-item')
        ? sidebarSessions.find(item => item.id === target.dataset.sessionId)
        : null;
    const projectId = targetSession
        ? (targetSession.project_id || null)
        : (target.dataset.taskDropProjectId || target.dataset.dropProjectId || null);
    const orderedIds = sidebarSessions
        .filter(item => !item.archived && (item.project_id || null) === projectId && item.id !== drag.sessionId)
        .map(item => item.id);
    if (targetSession && targetSession.id !== drag.sessionId) {
        let targetIndex = orderedIds.indexOf(targetSession.id);
        if (placement === 'after') targetIndex += 1;
        orderedIds.splice(targetIndex, 0, drag.sessionId);
    } else if (!targetSession) {
        orderedIds.unshift(drag.sessionId);
    } else {
        return;
    }
    await reorderSessions(projectId, orderedIds);
}

function cancelSidebarPointerDrag() {
    sidebarPointerDrag?.row.classList.remove('dragging');
    clearSidebarTaskTarget(sidebarPointerTarget);
    sidebarPointerDrag = null;
    sidebarPointerTarget = null;
    sidebarPointerPlacement = 'inside';
}

function clearSidebarProjectTarget(target) {
    target?.classList.remove('project-drop-before', 'project-drop-after');
}

function handleSidebarProjectPointerMove(event) {
    if (!sidebarProjectDrag) return;
    const distance = Math.hypot(event.clientX - sidebarProjectDrag.startX, event.clientY - sidebarProjectDrag.startY);
    if (!sidebarProjectDrag.moved && distance < 6) return;
    sidebarProjectDrag.moved = true;
    sidebarProjectDrag.block.classList.add('dragging');
    scrollSidebarDuringDrag(event.clientY);
    const hit = document.elementFromPoint(event.clientX, event.clientY);
    const target = hit?.closest('.sidebar-project:not(.archived-project)') || hit?.closest('[data-project-reorder-zone]') || null;
    const placement = target?.classList.contains('sidebar-project') && event.clientY < target.getBoundingClientRect().top + target.getBoundingClientRect().height / 2
        ? 'before'
        : 'after';
    if (sidebarProjectTarget === target && sidebarProjectPlacement === placement) return;
    clearSidebarProjectTarget(sidebarProjectTarget);
    sidebarProjectTarget = target;
    sidebarProjectPlacement = placement;
    if (target && target.dataset.projectBlockId !== sidebarProjectDrag.projectId) target.classList.add(`project-drop-${placement}`);
}

async function finishSidebarProjectPointerDrag() {
    if (!sidebarProjectDrag) return;
    const drag = sidebarProjectDrag;
    const target = sidebarProjectTarget;
    const placement = sidebarProjectPlacement;
    cancelSidebarProjectPointerDrag();
    if (!drag.moved || !target || target.dataset.projectBlockId === drag.projectId) return;
    const orderedIds = sidebarProjects.filter(project => !project.archived && project.id !== drag.projectId).map(project => project.id);
    let targetIndex = target.dataset.projectBlockId ? orderedIds.indexOf(target.dataset.projectBlockId) : orderedIds.length;
    if (target.dataset.projectBlockId && placement === 'after') targetIndex += 1;
    orderedIds.splice(targetIndex, 0, drag.projectId);
    await reorderProjects(orderedIds);
}

function cancelSidebarProjectPointerDrag() {
    sidebarProjectDrag?.block.classList.remove('dragging');
    clearSidebarProjectTarget(sidebarProjectTarget);
    sidebarProjectDrag = null;
    sidebarProjectTarget = null;
    sidebarProjectPlacement = 'before';
}

function createArchiveSection(projects, sessions) {
    const section = document.createElement('section');
    section.className = 'sidebar-group archive-group';
    const total = projects.length + sessions.length;
    const archivedProjectIds = new Set(projects.map(project => project.id));
    const toggle = document.createElement('button');
    toggle.className = 'archive-toggle';
    toggle.innerHTML = `<i data-lucide="archive"></i><span>封存</span><span class="project-count">${total}</span><i data-lucide="chevron-right" class="archive-chevron"></i>`;
    const content = document.createElement('div');
    content.className = 'archive-content';
    content.hidden = true;
    toggle.addEventListener('click', () => {
        content.hidden = !content.hidden;
        toggle.classList.toggle('expanded', !content.hidden);
        safeCreateIcons();
    });
    projects.forEach(project => {
        const block = document.createElement('div');
        block.className = 'sidebar-project archived-project';
        block.appendChild(createArchivedProjectRow(project));
        const taskList = document.createElement('div');
        taskList.className = 'project-task-list';
        sessions
            .filter(session => session.project_id === project.id)
            .forEach(session => taskList.appendChild(createSessionRow(session)));
        block.appendChild(taskList);
        content.appendChild(block);
    });
    sessions
        .filter(session => !session.project_id || !archivedProjectIds.has(session.project_id))
        .forEach(session => content.appendChild(createSessionRow(session, !session.project_id)));
    if (!total) content.appendChild(emptySidebarRow('尚無封存項目'));
    section.append(toggle, content);
    return section;
}

function createArchivedProjectRow(project) {
    const row = document.createElement('div');
    row.className = `project-row archived ${project.pinned ? 'is-pinned' : ''}`.trim();
    row.innerHTML = `<i data-lucide="folder-archive" class="project-icon"></i>${project.pinned ? '<i data-lucide="pin" class="project-pin" aria-label="已釘選"></i>' : ''}<span class="project-name">${escapeHtml(project.name)}</span><button class="sidebar-menu-btn" aria-label="${escapeHtml(project.name)} 選單"><i data-lucide="ellipsis"></i></button>`;
    row.querySelector('.sidebar-menu-btn').addEventListener('click', event => openProjectMenu(event, project));
    return row;
}

function emptySidebarRow(text) {
    const row = document.createElement('div');
    row.className = 'sidebar-empty-row';
    row.textContent = text;
    return row;
}

function openContextMenu(event, items) {
    event.stopPropagation();
    window.workbenchProjectSkills?.closeMenus();
    if (sidebarContextMenu.parentElement !== document.body) {
        document.body.appendChild(sidebarContextMenu);
    }
    sidebarContextMenu.innerHTML = '';
    items.forEach(item => {
        if (item.separator) {
            sidebarContextMenu.appendChild(document.createElement('hr'));
            return;
        }
        const button = document.createElement('button');
        button.className = item.danger ? 'danger' : '';
        const menuIcon = document.createElement('i');
        menuIcon.dataset.lucide = String(item.icon || 'circle');
        menuIcon.setAttribute('aria-hidden', 'true');
        const menuLabel = document.createElement('span');
        menuLabel.textContent = String(item.label || '');
        button.append(menuIcon, menuLabel);
        button.addEventListener('click', async () => {
            sidebarContextMenu.hidden = true;
            await item.run();
        });
        sidebarContextMenu.appendChild(button);
    });
    sidebarContextMenu.hidden = false;
    const rect = event.currentTarget.getBoundingClientRect();
    const width = 190;
    const left = Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8));
    const availableBelow = window.innerHeight - rect.bottom - 8;
    const top = availableBelow >= sidebarContextMenu.offsetHeight
        ? rect.bottom + 4
        : Math.max(8, rect.top - sidebarContextMenu.offsetHeight - 4);
    sidebarContextMenu.style.left = `${left}px`;
    sidebarContextMenu.style.top = `${top}px`;
    safeCreateIcons();
}

function closeContextMenu() {
    if (sidebarContextMenu) sidebarContextMenu.hidden = true;
}

function openProjectMenu(event, project) {
    openContextMenu(event, [
        { label: project.pinned ? '取消釘選' : '釘選專案', icon: 'pin', run: () => patchProject(project.id, { pinned: !project.pinned }) },
        { separator: true },
        { label: '新增任務', icon: 'plus', run: () => createNewSession(project.id) },
        { label: '重新命名', icon: 'pencil', run: () => renameProject(project) },
        { label: '專案設定', icon: 'settings', run: () => editProjectSettings(project) },
        { label: '重新連結資料夾', icon: 'folder-sync', run: () => relinkProject(project) },
        { label: '開啟資料夾', icon: 'folder-open', run: () => openProjectFolder(project.id) },
        { separator: true },
        { label: project.archived ? '取消封存' : '封存專案', icon: 'archive-restore', run: () => patchProject(project.id, { archived: !project.archived }) },
        { label: '刪除專案', icon: 'trash-2', danger: true, run: () => deleteProject(project) }
    ]);
}

function openSessionMenu(event, session) {
    openContextMenu(event, [
        { label: session.pinned ? '取消釘選' : '釘選任務', icon: 'pin', run: () => patchSession(session.id, { pinned: !session.pinned }) },
        { label: '重新命名', icon: 'pencil', run: () => renameSession(session) },
        { label: '移動到專案', icon: 'folder-input', run: () => moveSession(session) },
        { label: '匯出對話', icon: 'download', run: () => window.open(apiUrl(`${API_BASE}/api/sessions/${session.id}/export.zip`), '_blank') },
        { separator: true },
        { label: session.archived ? '取消封存' : '封存任務', icon: 'archive', run: () => patchSession(session.id, { archived: !session.archived }) },
        { label: '永久刪除', icon: 'trash-2', danger: true, run: () => confirm('確定要永久刪除此對話？') && deleteSession(session.id) }
    ]);
}

function openSidebarDialog({ title, label, value = '', options = null, confirmLabel = '確認' }) {
    return new Promise(resolve => {
        sidebarDialogTitle.textContent = title;
        sidebarDialogLabel.textContent = label;
        sidebarDialogConfirm.textContent = confirmLabel;
        projectDialogFields.hidden = true;
        sidebarDialogInput.disabled = false;
        sidebarDialogInput.hidden = !!options;
        sidebarDialogSelect.hidden = !options;
        if (options) {
            sidebarDialogSelect.innerHTML = options.map(option => `<option value="${escapeHtml(option.value)}" ${option.value === value ? 'selected' : ''}>${escapeHtml(option.label)}</option>`).join('');
        } else {
            sidebarDialogInput.value = value;
        }
        const closeHandler = () => {
            sidebarDialog.removeEventListener('close', closeHandler);
            sidebarDialogForm.removeEventListener('submit', submitHandler);
            resolve(null);
        };
        const submitHandler = event => {
            event.preventDefault();
            const result = options ? sidebarDialogSelect.value : sidebarDialogInput.value.trim();
            if (!result && !options) return;
            sidebarDialog.removeEventListener('close', closeHandler);
            sidebarDialogForm.removeEventListener('submit', submitHandler);
            sidebarDialog.close('confirm');
            resolve(result);
        };
        sidebarDialog.addEventListener('close', closeHandler, { once: true });
        sidebarDialogForm.addEventListener('submit', submitHandler);
        sidebarDialog.showModal();
        (options ? sidebarDialogSelect : sidebarDialogInput).focus();
    });
}

async function browseProjectFolder() {
    const selectedPath = await openFolderBrowser(projectRootPath.value.trim() || null);
    if (selectedPath && selectedPath !== '__roots__') {
        projectRootPath.value = selectedPath;
    }
}

function openFolderBrowser(initialPath = null) {
    if (folderBrowserResolver) return Promise.resolve(null);
    folderBrowserDialog.showModal();
    safeCreateIcons();
    return new Promise(resolve => {
        folderBrowserResolver = resolve;
        loadFolderDirectory(initialPath).catch(() => {});
    });
}

function closeFolderBrowser(selectedPath) {
    const resolve = folderBrowserResolver;
    folderBrowserResolver = null;
    folderBrowserDialog.close();
    if (resolve) resolve(selectedPath);
}

async function loadFolderDirectory(path) {
    folderBrowserStatus.className = 'folder-browser-status';
    folderBrowserStatus.textContent = '載入資料夾中...';
    folderBrowserList.innerHTML = '';
    folderBrowserSelect.disabled = true;
    folderBrowserUp.disabled = true;
    try {
        const res = await apiFetch(`${API_BASE}/api/projects/browse-directories`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail?.message || '無法讀取資料夾。');
        folderBrowserCurrentPath = data.current_path;
        folderBrowserParentPath = data.parent_path;
        folderBrowserPath.value = data.display_path;
        folderBrowserUp.disabled = !folderBrowserParentPath;
        folderBrowserSelect.disabled = folderBrowserCurrentPath === '__roots__';
        folderBrowserList.innerHTML = (data.directories || []).map(directory => `
            <button type="button" data-folder-path="${escapeHtml(directory.path)}" title="${escapeHtml(directory.path)}">
                <i data-lucide="folder"></i><span>${escapeHtml(directory.name)}</span>
            </button>`).join('') || '<div class="sidebar-empty-row">此資料夾沒有子資料夾</div>';
        folderBrowserList.querySelectorAll('[data-folder-path]').forEach(button => {
            button.addEventListener('click', () => loadFolderDirectory(button.dataset.folderPath));
        });
        folderBrowserStatus.textContent = `${(data.directories || []).length} 個子資料夾`;
        safeCreateIcons();
    } catch (error) {
        folderBrowserStatus.className = 'folder-browser-status error';
        folderBrowserStatus.textContent = error.message || '無法讀取資料夾。';
    }
}

function openProjectDialog() {
    return new Promise(resolve => {
        sidebarDialogTitle.textContent = '新增專案';
        sidebarDialogLabel.textContent = '專案名稱';
        sidebarDialogConfirm.textContent = '建立專案';
        sidebarDialogInput.hidden = false;
        sidebarDialogSelect.hidden = true;
        sidebarDialogInput.disabled = false;
        sidebarDialogInput.value = '';
        projectDialogFields.hidden = false;
        projectRootPath.value = '';
        projectRootPath.readOnly = false;
        projectBrowseButton.disabled = false;
        const closeHandler = () => {
            cleanup();
            resolve(null);
        };
        const submitHandler = event => {
            event.preventDefault();
            const name = sidebarDialogInput.value.trim();
            if (!name || !projectRootPath.value.trim()) return;
            const result = {
                name,
                root_path: projectRootPath.value.trim() || null,
                root_kind: 'linked',
                permission_mode: 'read_only'
            };
            cleanup();
            sidebarDialog.close('confirm');
            resolve(result);
        };
        const cleanup = () => {
            sidebarDialog.removeEventListener('close', closeHandler);
            sidebarDialogForm.removeEventListener('submit', submitHandler);
            projectRootPath.readOnly = false;
            projectDialogFields.hidden = true;
        };
        sidebarDialog.addEventListener('close', closeHandler, { once: true });
        sidebarDialogForm.addEventListener('submit', submitHandler);
        sidebarDialog.showModal();
        sidebarDialogInput.focus();
    });
}

async function createNewProject() {
    const values = await openProjectDialog();
    if (!values) return;
    const project = await createProjectRecord(values);
    if (project) await loadSessions(searchSessionsInput.value.trim());
}

async function createProjectRecord(values) {
    const res = await apiFetch(`${API_BASE}/api/projects`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(values) });
    if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        alert(data.detail?.message || '無法建立專案，請確認名稱或路徑。');
        return null;
    }
    return (await res.json()).project || null;
}

async function createProjectForSession(sessionId) {
    const values = await openProjectDialog();
    if (!values) return;
    const project = await createProjectRecord(values);
    if (!project) return;
    await reorderSessions(project.id, [sessionId]);
}

async function renameProject(project) {
    const name = await openSidebarDialog({ title: '重新命名專案', label: '專案名稱', value: project.name, confirmLabel: '儲存' });
    if (name && name !== project.name) await patchProject(project.id, { name });
}

async function editProjectSettings(project) {
    window.workbenchExtensions?.openProjectSettings(project);
}

async function relinkProject(project) {
    const rootPath = await openSidebarDialog({ title: '重新連結資料夾', label: '新的專案路徑', value: project.root_path, confirmLabel: '重新連結' });
    if (!rootPath || rootPath === project.root_path) return;
    await relinkProjectToPath(project.id, rootPath);
}

async function relinkProjectToPath(projectId, rootPath) {
    const res = await apiFetch(`${API_BASE}/api/projects/${projectId}/relink`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ root_path: rootPath })
    });
    if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        return alert(data.detail?.message || '重新連結專案資料夾失敗。');
    }
    await loadSessions(searchSessionsInput.value.trim());
}

async function renameSession(session) {
    const title = await openSidebarDialog({ title: '重新命名任務', label: '任務名稱', value: session.title, confirmLabel: '儲存' });
    if (title && title !== session.title) await patchSession(session.id, { title });
}

async function moveSession(session) {
    const options = [{ value: '__independent__', label: '獨立任務' }, ...sidebarProjects.filter(project => !project.archived).map(project => ({ value: project.id, label: project.name }))];
    const target = await openSidebarDialog({ title: '移動任務', label: '目的位置', value: session.project_id || '__independent__', options, confirmLabel: '移動' });
    const projectId = target === '__independent__' ? null : target;
    if (target && (session.project_id || null) !== projectId) await patchSession(session.id, { project_id: projectId });
}

async function patchProject(projectId, changes) {
    const res = await apiFetch(`${API_BASE}/api/projects/${projectId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes) });
    if (!res.ok) return alert('專案更新失敗。');
    await loadSessions(searchSessionsInput.value.trim());
}

async function patchSession(sessionId, changes) {
    const res = await apiFetch(`${API_BASE}/api/sessions/${sessionId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes) });
    if (!res.ok) return alert('任務更新失敗。');
    await loadSessions(searchSessionsInput.value.trim());
}

async function reorderProjects(projectIds) {
    const res = await apiFetch(`${API_BASE}/api/projects/reorder`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_ids: projectIds })
    });
    if (!res.ok) return alert('專案排序失敗。');
    await loadSessions(searchSessionsInput.value.trim());
}

async function reorderSessions(projectId, sessionIds) {
    const res = await apiFetch(`${API_BASE}/api/sessions/reorder`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: projectId, session_ids: sessionIds })
    });
    if (!res.ok) return alert('任務移動或排序失敗。');
    await loadSessions(searchSessionsInput.value.trim());
}

async function openProjectFolder(projectId) {
    const res = await apiFetch(`${API_BASE}/api/projects/${projectId}/open-folder`, { method: 'POST' });
    if (!res.ok) alert('無法開啟專案資料夾。');
}

async function deleteProject(project) {
    if (!confirm(`確定永久刪除專案「${project.name}」？這會刪除專案內所有任務、訊息、附件與 Workbench 專案資料，且無法復原；已連結的外部資料夾本身不會被刪除。`)) return;
    const res = await apiFetch(`${API_BASE}/api/projects/${project.id}`, { method: 'DELETE' });
    if (!res.ok) return alert('刪除專案失敗。');
    await loadSessions(searchSessionsInput.value.trim());
}

async function createNewSession(projectId = null) {
    if (isGenerating) await cancelActiveChatRun();
    clearOutputSkillsContext('正在建立新對話…');
    try {
        const res = await apiFetch(`${API_BASE}/api/sessions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: '新任務', project_id: projectId })
        });
        const data = await res.json();
        currentSessionId = data.session_id;
        resetConversationState(); // P0-1：新會話 → 清空 LLM 記憶
        agentCollaborationState = createAgentCollaborationState();
        closeAgentCollaboration(false);

        // 重置 UI
        chatMessages.innerHTML = '';
        chatMessages.appendChild(welcomeCard);
        welcomeCard.style.display = 'block';
        
        // 收合沙盒，重設專案代碼狀態
        if (btnSandboxToggle) {
            btnSandboxToggle.classList.remove('active');
        }
        closeInspectorPanel();
        activeArtifactCode = '';
        
        await loadSessions(searchSessionsInput.value.trim());
    } catch (e) {
        console.error('Create session failed:', e);
    }
}

async function changeSession(sessionId) {
    if (sessionId === currentSessionId) return;
    if (isGenerating) await cancelActiveChatRun();
    currentSessionId = sessionId;
    clearOutputSkillsContext();
    chatMessages.innerHTML = '';
    welcomeCard.style.display = 'none';
    
    // 預設收合沙盒並清除高亮
    if (btnSandboxToggle) {
        btnSandboxToggle.classList.remove('active');
    }
    closeInspectorPanel();
    activeArtifactCode = '';
    
    // 更新側邊欄 Active 狀態
    const items = sessionList.querySelectorAll('.session-item');
    items.forEach(item => item.classList.remove('active'));
    await loadSessions(); // 刷新列表高亮 active

    try {
        const res = await apiFetch(`${API_BASE}/api/sessions/${sessionId}/messages`);
        const data = await res.json();
        
        if (data.messages && data.messages.length > 0) {
            resetConversationState(data.messages); // P0-1：以 DB 乾淨訊息重建 LLM 記憶
            const renderedMessages = data.messages.map(msg => appendHistoricalMessage(msg));
            // 💡 歷史消息載入完畢，全面執行 KaTeX 公式排版渲染 💡
            safeRenderMath(chatMessages);
            
            // 💡 歷史消息載入完畢，尋找最後一條助理消息以預加載 Artifacts 💡
            const lastAssistantIndex = data.messages.map(msg => msg.role).lastIndexOf('assistant');
            const lastAssistantMsg = lastAssistantIndex >= 0 ? data.messages[lastAssistantIndex] : null;
            if (lastAssistantMsg) {
                const lastAssistantBubble = renderedMessages[lastAssistantIndex]?.querySelector('.message-bubble');
                if (lastAssistantBubble && !lastAssistantBubble.querySelector('.answer-actions')) {
                    appendAnswerFooter(lastAssistantBubble, {
                        text: lastAssistantMsg.content || '',
                        metrics: {},
                        artifactProduced: false,
                        showMetrics: false
                    });
                }
                parseAndLoadArtifacts(lastAssistantMsg.content, true);
                restoreAgentCollaboration(lastAssistantMsg.process_events || []);
            }
        } else {
            resetConversationState();
            agentCollaborationState = createAgentCollaborationState();
            renderAgentCollaboration();
            chatMessages.appendChild(welcomeCard);
            welcomeCard.style.display = 'block';
        }
    } catch (e) {
        console.error('Failed to fetch session messages:', e);
    }
}

async function deleteSession(sessionId) {
    try {
        const res = await apiFetch(`${API_BASE}/api/sessions/${sessionId}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.success) {
            if (currentSessionId === sessionId) {
                currentSessionId = null;
                clearOutputSkillsContext('請先選擇專案');
                resetConversationState(); // P0-1
                chatMessages.innerHTML = '';
                chatMessages.appendChild(welcomeCard);
                welcomeCard.style.display = 'block';
            }
            await loadSessions();
            if (!currentSessionId && sessionList.querySelector('.session-item')) {
                // 自動切換到第一個可用會話
                const firstSess = sessionList.querySelector('.session-item');
                firstSess.click();
            }
        }
    } catch (e) {
        console.error('Delete session failed:', e);
    }
}

// ==========================================================================
// 2. 多模態圖片貼上與上傳
// ==========================================================================

function handleImagePaste(e) {
    const items = (e.clipboardData || e.originalEvent.clipboardData).items;
    for (const item of items) {
        if (item.type.indexOf('image') === 0) {
            const blob = item.getAsFile();
            const reader = new FileReader();
            reader.onload = function(event) {
                addImagePreview(event.target.result);
            };
            reader.readAsDataURL(blob);
            e.preventDefault();
        }
    }
}

function handleImageUploadSelect(e) {
    const files = e.target.files;
    for (const file of files) {
        const reader = new FileReader();
        reader.onload = function(event) {
            addImagePreview(event.target.result);
        };
        reader.readAsDataURL(file);
    }
    imgFileInput.value = ''; // 重置
}

function addImagePreview(base64Data) {
    currentImages.push(base64Data);
    
    // 渲染預覽卡片
    imagePreviewContainer.style.display = 'flex';
    const card = document.createElement('div');
    card.className = 'image-preview-card';
    card.innerHTML = `
        <img src="${base64Data}" alt="預覽圖片">
        <button type="button" class="remove-btn">&times;</button>
    `;
    
    card.querySelector('.remove-btn').addEventListener('click', () => {
        const idx = currentImages.indexOf(base64Data);
        if (idx !== -1) {
            currentImages.splice(idx, 1);
        }
        card.remove();
        if (currentImages.length === 0) {
            imagePreviewContainer.style.display = 'none';
        }
    });
    
    imagePreviewContainer.appendChild(card);
    scrollToBottom();
}

// ==========================================================================
// 3. 知識庫與 Chunks 預覽管理 (Modal)
// ==========================================================================

async function loadKBFiles() {
    try {
        const res = await removedBasicFeature('Knowledge documents');
        const data = await res.json();
        
        kbFileList.innerHTML = '';
        if (data.documents && data.documents.length > 0) {
            data.documents.forEach(doc => {
                const item = document.createElement('div');
                item.className = 'kb-file-item';
                item.innerHTML = `
                    <div class="kb-file-info">
                        <span class="kb-file-name" title="${escapeHtml(doc.filename)}">${escapeHtml(doc.filename)}</span>
                        <span class="kb-file-meta">路徑: ${escapeHtml(doc.filepath)} | 共 ${doc.chunk_count} 個 Chunks</span>
                    </div>
                    <div class="kb-file-actions">
                        <button class="btn btn-secondary btn-xs btn-preview-chunks" data-path="${escapeHtml(doc.filepath)}" data-name="${escapeHtml(doc.filename)}">
                            <i data-lucide="eye" style="width: 12px; height: 12px; margin-right: 4px;"></i>預覽 Chunks
                        </button>
                        <button class="btn btn-danger btn-xs btn-delete-doc" data-path="${escapeHtml(doc.filepath)}" style="padding: 6px;">
                            <i data-lucide="trash-2" style="width: 12px; height: 12px;"></i>
                        </button>
                    </div>
                `;
                
                // 預覽 Chunks 綁定
                item.querySelector('.btn-preview-chunks').addEventListener('click', (e) => {
                    const filepath = e.currentTarget.getAttribute('data-path');
                    const filename = e.currentTarget.getAttribute('data-name');
                    previewDocChunks(filepath, filename);
                });
                
                // 單獨刪除文件綁定
                item.querySelector('.btn-delete-doc').addEventListener('click', (e) => {
                    const filepath = e.currentTarget.getAttribute('data-path');
                    if (confirm('確認從向量庫與本機徹底刪除此文件？')) {
                        deleteKBDocument(filepath);
                    }
                });

                kbFileList.appendChild(item);
            });
        } else {
            kbFileList.innerHTML = `<div class="empty-files" style="padding: 20px 0;"><span style="color: var(--text-muted);">知識庫為空，請上傳檔案</span></div>`;
        }
        safeCreateIcons();
    } catch (e) {
        console.error('Failed to load KB files:', e);
    }
}

async function deleteKBDocument(filepath) {
    try {
        const res = await removedBasicFeature('Knowledge documents', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_path: filepath })
        });
        const data = await res.json();
        if (data.success) {
            await loadKBFiles();
            loadRagStatus(); // P1-2
            chunksPreviewSection.style.display = 'none';
        } else {
            showToast('刪除失敗');
        }
    } catch (e) {
        console.error('Delete doc failed:', e);
    }
}

async function previewDocChunks(filepath, filename) {
    try {
        chunksPreviewSection.style.display = 'block';
        chunksPreviewTitle.innerHTML = `<i data-lucide="file-text" style="width: 14px; height: 14px; margin-right: 6px;"></i> 📄 ${escapeHtml(filename)} Chunks 預覽`;
        chunksList.innerHTML = `<div style="text-align:center; padding:20px; color:var(--text-muted);">載入中...</div>`;
        safeCreateIcons();
        
        const res = await removedBasicFeature('Knowledge chunks');
        const data = await res.json();
        
        chunksList.innerHTML = '';
        if (data.chunks && data.chunks.length > 0) {
            data.chunks.forEach((chunk, index) => {
                const card = document.createElement('div');
                card.className = 'chunk-card';
                card.innerHTML = `<strong style="color: var(--primary-color);">[Chunk ${index + 1}]</strong>\n${escapeHtml(chunk)}`;
                chunksList.appendChild(card);
            });
        } else {
            chunksList.innerHTML = `<div style="text-align:center; padding:20px; color:var(--text-muted);">無可用片段</div>`;
        }
        // 滾動定位到預覽區域
        chunksPreviewSection.scrollIntoView({ behavior: 'smooth' });
    } catch (e) {
        console.error('Preview chunks failed:', e);
    }
}

// 檔案選擇與上傳
async function handleFilesSelect(files) {
    if (!files || files.length === 0) return;
    const progressId = `document-upload-${Date.now()}`;
    
    progressContainer.style.display = 'block';
    progressContainer.classList.remove('is-indeterminate');
    progressFill.style.width = '0%';
    
    const formData = new FormData();
    for (let i = 0; i < files.length; i++) {
        formData.append('files', files[i]);
    }
    if (currentSessionId) formData.append('session_id', currentSessionId);
    
    progressFilename.textContent = files.length === 1 ? `正在匯入: ${files[0].name}` : `正在匯入 ${files.length} 個檔案...`;
    progressPercent.textContent = '10%';
    progressFill.style.width = '10%';
    updateTaskProgress(progressId, {
        label: files.length === 1 ? `匯入文件：${files[0].name}` : `匯入 ${files.length} 份文件`,
        detail: '正在上傳檔案',
        mode: 'determinate',
        value: 10
    });

    try {
        const xhr = new XMLHttpRequest();
        throw new Error('Knowledge upload is not available in Basic Chat mode.');
        
        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
                const percent = Math.round((e.loaded / e.total) * 80) + 10;
                progressPercent.textContent = `${percent}%`;
                progressFill.style.width = `${percent}%`;
                updateTaskProgress(progressId, { detail: '正在上傳檔案', mode: 'determinate', value: percent });
            }
        };

        xhr.upload.onload = () => {
            progressContainer.classList.add('is-indeterminate');
            progressPercent.textContent = '建立索引';
            updateTaskProgress(progressId, { detail: '上傳完成，正在解析並建立索引', mode: 'indeterminate', value: null });
        };

        xhr.onload = async () => {
            if (xhr.status === 200) {
                progressContainer.classList.remove('is-indeterminate');
                progressPercent.textContent = '100%';
                progressFill.style.width = '100%';
                finishTaskProgress(progressId, 'completed', '文件已完成索引');
                
                setTimeout(() => {
                    progressContainer.style.display = 'none';
                }, 1000);

                await loadKBFiles();
                loadRagStatus(); // P1-2
            } else {
                showToast('檔案上傳失敗：' + xhr.statusText);
                progressContainer.classList.remove('is-indeterminate');
                progressContainer.style.display = 'none';
                finishTaskProgress(progressId, 'failed', `文件匯入失敗：HTTP ${xhr.status}`);
            }
        };

        xhr.onerror = () => {
            showToast('連線失敗，無法上傳檔案。');
            progressContainer.classList.remove('is-indeterminate');
            progressContainer.style.display = 'none';
            finishTaskProgress(progressId, 'failed', '無法連接後端服務');
        };

        xhr.send(formData);
    } catch (e) {
        console.error('Upload failed:', e);
        progressContainer.classList.remove('is-indeterminate');
        progressContainer.style.display = 'none';
        finishTaskProgress(progressId, 'failed', e.message || '文件匯入失敗');
    }
}

// P1-9：清空知識庫唯一入口（全前端統一呼叫此函式；語意 = 清空索引與已上傳暫存檔）
async function clearRagIndex() {
    const res = await removedBasicFeature('Knowledge index reset');
    const data = await res.json();
    if (!data.success) throw new Error(data.detail || '清空失敗');
    await loadKBFiles();
    await loadRagStatus();
}

// 清空整個知識庫
async function handleClearDatabase() {
    confirmModal.classList.remove('active');
    const progressId = `rag-clear-${Date.now()}`;
    updateTaskProgress(progressId, { label: '清除知識庫索引', detail: '正在移除向量索引與暫存文件', mode: 'indeterminate', value: null });
    try {
        await clearRagIndex(); // P1-9：統一入口
        chunksPreviewSection.style.display = 'none';
        showToast('知識庫已全部清空！');
        finishTaskProgress(progressId, 'completed', '知識庫索引已清除');
    } catch (e) {
        console.error('Clear db failed:', e);
        finishTaskProgress(progressId, 'failed', e.message || '清除知識庫失敗');
    }
}

// ==========================================================================
// 4. 對話送出與串流處理 (過濾 / 剔除思考區與工具卡片)
// ==========================================================================

function isExplicitN8nOperationIntent(value) {
    const text = String(value || '').trim();
    if (!/\bn8n\b/i.test(text)) return false;
    if (/```/.test(text)) return false;

    // Questions about capabilities or troubleshooting stay in ordinary chat.
    // Routing is intentionally conservative because the governed planner is
    // an operation boundary, not a general n8n knowledge assistant.
    const informational = /(?:請問|想了解|幫我了解|為何|為什麼|是否|能否|可否|可不可以|怎麼|如何|解釋|說明|what\b|why\b|how\b)/i;
    if (informational.test(text)) return false;

    const operation = /(?:建立|新增|修改|更新|編輯|發布|啟用|啟動|停用|停止|刪除|移除|執行|運行|操作|控制|管理|設定|配置|串接|連接|寄送|發送|回覆|觸發|排程|匯入|測試|create|add|update|edit|publish|activate|deactivate|delete|remove|execute|run|manage|configure|connect|send|reply|trigger|schedule|import|test)/i;
    if (!operation.test(text)) return false;

    const explicitRequest = /(?:請(?!問)|幫我|替我|為我|我要|我想要|我希望|現在|立即|立刻|直接|please\b|^\s*(?:用|讓|叫|n8n\b|use\b|have\b|make\b|create\b|update\b|publish\b|activate\b|deactivate\b|delete\b|run\b|execute\b))/i;
    return explicitRequest.test(text);
}

function isExplicitN8nMailOperation(value) {
    const text = String(value || '');
    return /\bn8n\b/i.test(text)
        && /(?:寄信|寄送|發信|發送(?:電子)?郵件|回覆(?:郵件|信件)|email|e-mail|gmail|send\s+(?:an?\s+)?(?:email|mail)|reply\s+(?:to\s+)?(?:an?\s+)?(?:email|mail))/i.test(text);
}

function isExplicitN8nWorkflowAuthoringIntent(value) {
    const text = String(value || '');
    if (!/\bn8n\b/i.test(text)) return false;
    return /(?:workflow|work\s*flow|node|nodes|canvas|graph|pipeline|automation|流程|工作流|節點|拉節點|配對節點|串接節點|建立流程|修改流程|新增流程|自動化流程)/i.test(text)
        && /(?:create|add|build|design|update|edit|connect|wire|deploy|建立|新增|設計|修改|連接|串接|配對|部署|拉)/i.test(text);
}

async function routeExplicitN8nOperationToPlanner(question) {
    if (!isExplicitN8nOperationIntent(question)) return false;

    // Opening the dedicated workspace does not grant authority. The planner
    // remains tool-free and the existing digest-bound proposal plus separate
    // human approval are still required before the Broker can mutate n8n.
    // Wait for the managed n8n lifecycle check/start attempt to settle before
    // asking the Planner for its live readiness snapshot.  This avoids a
    // false "runtime not ready" result during the on-demand startup race.
    await window.workbenchN8nWorkflows?.open?.();
    if (isExplicitN8nMailOperation(question) && !isExplicitN8nWorkflowAuthoringIntent(question)) {
        const mailResult = await window.workbenchN8nWorkflows?.createComposeFromChat?.({
            instruction: question,
        });
        if (mailResult?.status === 'draft_created') {
            showToast('郵件草稿已建立；尚未寄送，請在右上檢查器確認並核准。', 'success');
        } else {
            showToast(
                mailResult?.message || '請先完成 Gmail Profile、OAuth 與固定收件者設定；目前未寄送郵件。',
                'warning'
            );
        }
        userInput.value = '';
        userInput.style.height = 'auto';
        return true;
    }
    const planner = window.workbenchN8nGovernance;
    if (typeof planner?.startPlanFromChat !== 'function') {
        showToast('n8n 操作助理尚未就緒；本次要求未送到一般聊天，也未操作 n8n。', 'warning');
        return true;
    }

    try {
        const result = await planner.startPlanFromChat({
            message: question,
            projectId: activeProjectId || '',
            sessionId: currentSessionId || '',
            hasAttachments: currentImages.length > 0,
        });
        if (result?.status === 'blocked') {
            showToast(result.message || '請先完成 n8n 規劃所需的 Project 與 Session。', 'warning');
        } else {
            showToast('已轉入 n8n 操作助理；目前尚未操作 n8n。', 'success');
        }
        userInput.value = '';
        userInput.style.height = 'auto';
    } catch (error) {
        showToast(error?.message || 'n8n 操作規劃無法開始；本次未操作 n8n。', 'error');
    }
    return true;
}

async function retryRunFromInspector(runId, run = {}) {
    if (!runId) throw new Error('缺少要重新執行的 Run。');
    if (isGenerating) throw new Error('目前已有一輪正在執行。');
    return handleChatSubmit({
        preventDefault() {},
        retryOfRunId: runId,
        retryModel: run.model || null,
    });
}

function streamEventMatches(identity, data = {}) {
    if (!identity || currentSessionId !== identity.sessionId || activeProjectId !== identity.projectId) return false;
    if (data.run_id && String(data.run_id) !== identity.runId) return false;
    if (data.session_id && String(data.session_id) !== String(identity.sessionId || '')) return false;
    if (Object.prototype.hasOwnProperty.call(data, 'project_id')) {
        if (String(data.project_id || '') !== String(identity.projectId || '')) return false;
    }
    return true;
}

async function handleChatSubmit(e) {
    e.preventDefault();
    // 生成中：送出鈕作為「停止」（中止串流讀取，保留已生成內容）
    if (isGenerating) {
        await cancelActiveChatRun();
        return;
    }

    const retryOfRunId = String(e?.retryOfRunId || '').trim() || null;
    const retryModel = String(e?.retryModel || '').trim() || null;
    const retryInput = retryOfRunId ? runRetryInputs.get(retryOfRunId) : null;
    let userMessageAddedToConversation = false;
    let question = retryInput?.question || userInput.value.trim();
    if (!retryOfRunId && !question && currentImages.length === 0) return;
    let explicitSkillIds = [];
    if (!retryOfRunId && !BASIC_CHAT_MODE && question.startsWith('/skill') && window.workbenchSkills) {
        try {
            const prepared = await window.workbenchSkills.prepareSubmission(question);
            if (!prepared) {
                userInput.value = '';
                return;
            }
            question = prepared.message;
            explicitSkillIds = prepared.skillIds || [];
        } catch (error) {
            showToast(error.message || '無法套用 Skill', 'error');
            return;
        }
    }
    // 原始問題直接送交後端，由 Orchestrator 自動判斷處理流程。
    const sendQuestion = question;

    // 明確的 n8n 操作要求必須走既有的受治理 Planner。不要先送到
    // /api/chat，否則一般聊天只能如實回答它沒有外部操作工具。
    if (
        !retryOfRunId
        && explicitSkillIds.length === 0
        && currentImages.length === 0
        && !temporaryContextText
        && await routeExplicitN8nOperationToPlanner(sendQuestion)
    ) return;
    
    // 隱藏歡迎卡片
    if (welcomeCard.style.display !== 'none') {
        welcomeCard.style.display = 'none';
    }
    
    // 複製當前的待發送圖片，並清空預覽容器
    const imagesToSend = retryOfRunId ? [] : [...currentImages];
    if (!retryOfRunId) {
        currentImages = [];
        imagePreviewContainer.innerHTML = '';
        imagePreviewContainer.style.display = 'none';
    }
    
    // 1. 將使用者訊息渲染到對話歷史中 (帶有上傳的圖片)
    if (!retryOfRunId) appendMessage('user', question, imagesToSend);
    
    // 2. 重置輸入框
    if (!retryOfRunId) {
        userInput.value = '';
        userInput.style.height = 'auto';
    }
    
    // 3. 建立助理回覆的 placeholder
    const assistantMsgEl = createAssistantMessagePlaceholder();
    const bubbleEl = assistantMsgEl.querySelector('.message-bubble');
    const thinkingEl = assistantMsgEl.querySelector('.assistant-thinking');
    
    isGenerating = true;
    setGeneratingUI(true);
    chatAbort = new AbortController();
    currentChatRunId = null;
    resetAgentCollaboration();
    const runStart = performance.now();
    const progressId = `chat-generation-${Date.now()}`;
    let streamIdentity = null;
    let chatProgressStatus = 'completed';
    let firstTokenAt = 0;
    let approxTokens = 0;
    const artifactAtStart = activeArtifactCode;
    updateTaskProgress(progressId, {
        label: '生成 Agent 回覆',
        detail: '正在準備對話與檢索內容',
        mode: 'indeterminate',
        value: null
    });

    try {
        // P0-2：session 由後端建立（既有會話直接沿用）
        await ensureSession();
        if (isCancellingGeneration) throw new DOMException('Chat run cancelled before dispatch', 'AbortError');
        currentChatRunId = createClientRunId();
        streamIdentity = Object.freeze({
            runId: currentChatRunId,
            sessionId: currentSessionId,
            projectId: activeProjectId || null,
        });
        window.workbenchRunInspector?.beginRun({
            ...streamIdentity,
            model: retryModel || modelSelect.value,
            retryOfRunId,
        });

        // P0-1：LLM 上下文只來自 conversationState，本輪問題只加入一次
        if (!retryOfRunId) {
            addLLMMessage('user', sendQuestion, {
                images: imagesToSend.length,
                temporary_context_enabled: !!temporaryContextText
            });
            userMessageAddedToConversation = true;
        }

        const payload = {
            model: retryModel || modelSelect.value,
            messages: getLLMMessages(),
            use_rag: BASIC_CHAT_MODE ? false : ragToggle.checked,
            session_id: currentSessionId,
            images: imagesToSend,
            temporary_context: temporaryContextText || "",
            run_id: currentChatRunId,
            retry_of_run_id: retryOfRunId,
            skill_ids: explicitSkillIds,
            skill_auto: true
        };
        runRetryInputs.set(currentChatRunId, {
            question: sendQuestion,
            temporaryContextEnabled: !!temporaryContextText,
        });
        const response = await apiFetch(`${API_BASE}/api/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            signal: chatAbort.signal
        });
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8');
        
        let replyText = '';
        let currentTurnText = '';
        let accumulatedFinalAnswer = ''; // 用於累加前幾輪產生的正式回復，防範多輪 ReAct 狀態機混亂
        let currentEventType = null;
        let buffer = '';
        
        // 💡 資料驅動的 Agent 執行面板狀態（可觀察工作流） 💡
        // 事件相容：既有 tool_start / tool_end，並支援後端未來的 plan / task_update / progress / validation / final
        let executedTools = []; // 工具項（與 execLog 共享同一物件參照）: { kind:'tool', name, desc, completed, time }
        let execLog = [];       // 執行紀錄時間軸：tool / progress / validation 混排
        let runTasks = [];      // plan 事件的任務清單: { id, title, status }
        let finalSummary = '';  // final 事件的最終報告
        let showFinalReport = false;
        let finalValidationPassed = true;
        let authoritativeMetrics = null; // 後端模型 eval 指標；避免把整輪牆鐘時間誤當生成速度
        let runInProgress = true;
        let isDrawerActive = true; // 記錄折疊面板展開/收合狀態，預設為展開
        let sources = [];          // P0-3：本輪 RAG 檢索來源（context/sources 事件）
        const nowHM = () => new Date().toTimeString().slice(0, 5);
        
        // 💡 統合式更新泡泡 HTML 渲染器 💡
        // P0-5：heavy=false 時為串流輕量渲染（跳過 KaTeX / Artifacts 全文掃描 / 部分圖示重建）
        function updateBubble(heavy = true) {
            // 🛡️ 強力安全過濾：將當前這一輪的 replyText 進行無死角過濾，防漏各種 thought、tool 標記與裸 JSON 🛡️
            let cleanReplyText = replyText;
            
            // 1. 移除已閉合的標籤與代碼區塊
            cleanReplyText = cleanReplyText.replace(/<thought>[\s\S]*?<\/thought>/g, '');
            cleanReplyText = cleanReplyText.replace(/<tool>[\s\S]*?<\/tool>/g, '');
            cleanReplyText = cleanReplyText.replace(/```json[\s\S]*?```/g, '');
            cleanReplyText = cleanReplyText.replace(/\bjson\s*\{[\s\S]*?\}/g, '');
            
            // 2. 切除流式輸出中尚未閉合的標籤 (防漏 token)
            const thoughtIdx = cleanReplyText.indexOf('<thought>');
            if (thoughtIdx !== -1) cleanReplyText = cleanReplyText.slice(0, thoughtIdx);
            
            const toolIdx = cleanReplyText.indexOf('<tool>');
            if (toolIdx !== -1) cleanReplyText = cleanReplyText.slice(0, toolIdx);
            
            const jsonIdx = cleanReplyText.indexOf('```json');
            if (jsonIdx !== -1) cleanReplyText = cleanReplyText.slice(0, jsonIdx);
            
            // 3. 處理未閉合的裸 JSON `{ "tool": ... }`
            const braceIdx = cleanReplyText.indexOf('{');
            if (braceIdx !== -1) {
                const suffix = cleanReplyText.slice(braceIdx);
                if (suffix.includes('"tool"')) {
                    cleanReplyText = cleanReplyText.slice(0, braceIdx);
                }
            }
            
            // 正式回答文字拼接
            let cleanDisplay = accumulatedFinalAnswer + cleanReplyText;
            
            // 切除尾部任何殘留未閉合的代碼殘留
            cleanDisplay = cleanDisplay.replace(/```json[\s\S]*$/g, '');
            
            const parsedContentHTML = renderMarkdownSafe(cleanDisplay);
            
            // 如果沒有任何 Agent 執行資訊，直接渲染 markdown
            if (executedTools.length === 0 && execLog.length === 0 && runTasks.length === 0 && !finalSummary) {
                bubbleEl.innerHTML = `<div class="assistant-answer-content">${parsedContentHTML}</div>`;
                if (heavy) {
                    safeCreateIcons();
                    safeRenderMath(bubbleEl);
                }
                maybeParseArtifacts(cleanDisplay, heavy);
                return;
            }
            
            // 3. 構造 Agent 執行面板 HTML（任務清單 + 執行紀錄時間軸）
            // 3-1. 任務清單（plan / task_update 事件）
            let taskListHTML = '';
            if (runTasks.length > 0) {
                const statusMark = { pending: '○', in_progress: '◐', completed: '✓', failed: '✗' };
                let items = '';
                for (const t of runTasks) {
                    const st = statusMark[t.status] ? t.status : 'pending';
                    items += `
                        <div class="agent-task-item ${st}">
                            <span class="agent-task-status">${statusMark[st]}</span>
                            <span class="agent-task-title">${escapeHtml(t.title || '')}</span>
                        </div>
                    `;
                }
                taskListHTML = `
                    <div class="agent-task-list">
                        <div class="agent-task-list-title">任務清單 Tasks</div>
                        ${items}
                    </div>
                `;
            }

            // 3-2. 執行紀錄時間軸（tool / progress / validation 依發生順序混排）
            let stepsHTML = '';
            for (const e of execLog) {
                const timeHTML = `<span class="agent-log-time">${escapeHtml(e.time || '')}</span>`;
                if (e.kind === 'tool') {
                    const icon = e.completed
                        ? `<i data-lucide="check-circle" class="drawer-step-icon" style="color: #10b981;"></i>`
                        : `<i data-lucide="loader" class="status-spinner drawer-step-icon"></i>`;
                    const descText = e.desc || `執行工具: ${e.name}`;
                    const text = e.completed
                        ? `${descText.replace('執行中', '已執行')} (已完成)`
                        : `${descText}...`;
                    stepsHTML += `
                        <li class="drawer-step-item ${e.completed ? 'completed' : 'pending'}">
                            ${timeHTML}
                            ${icon}
                            <span class="step-text">${escapeHtml(text)}</span>
                        </li>
                    `;
                } else if (e.kind === 'validation') {
                    stepsHTML += `
                        <li class="drawer-step-item completed">
                            ${timeHTML}
                            <div class="agent-log-line ${e.passed ? 'validation-pass' : 'validation-fail'}">
                                <span class="agent-log-text">${e.passed ? '✓' : '⚠'} ${escapeHtml(e.text || '')}</span>
                            </div>
                        </li>
                    `;
                } else {
                    stepsHTML += `
                        <li class="drawer-step-item completed">
                            ${timeHTML}
                            <div class="agent-log-line"><span class="agent-log-text">${escapeHtml(e.text || '')}</span></div>
                        </li>
                    `;
                }
            }

            // 3-3. 面板頭部：進行中顯示呼吸燈與目前動作，結束後顯示統計
            const actionCount = execLog.length;
            let headStatus = `執行過程 · ${actionCount} 個動作`;
            if (runInProgress && execLog.length > 0) {
                const last = execLog[execLog.length - 1];
                const lastText = last.kind === 'tool' ? (last.desc || last.name) : (last.text || '');
                headStatus = lastText.length > 42 ? lastText.slice(0, 42) + '…' : lastText;
            }
            const headIcon = runInProgress
                ? `<span class="agent-breathe-dot"></span>`
                : `<i data-lucide="terminal" class="drawer-icon"></i>`;

            const drawerHTML = `
                <div class="agent-tool-drawer">
                    <div class="drawer-trigger ${isDrawerActive ? 'active' : ''} ${runInProgress ? 'running' : ''}">
                        ${headIcon}
                        <span class="drawer-status-text">${escapeHtml(headStatus)}</span>
                        <i data-lucide="chevron-down" class="drawer-chevron"></i>
                    </div>
                    <div class="drawer-content ${isDrawerActive ? 'active' : ''}">
                        ${taskListHTML}
                        <ul class="drawer-steps-list">
                            ${stepsHTML}
                        </ul>
                    </div>
                </div>
            `;

            // 3-4. 最終報告（final 事件）
            const finalHTML = renderAgentWorkReport(finalSummary, showFinalReport, finalValidationPassed);
            
            // 4. 面板、正式文字與最終報告拼接
            bubbleEl.innerHTML = drawerHTML
                + `<div class="assistant-answer-content">${parsedContentHTML}</div>` + finalHTML;
            
            // 5. 重新裝配 Lucide 圖示與折疊面板點擊事件
            safeCreateIcons();
            
            const trigger = bubbleEl.querySelector('.drawer-trigger');
            const content = bubbleEl.querySelector('.drawer-content');
            if (trigger && content) {
                trigger.addEventListener('click', (e) => {
                    e.stopPropagation();
                    isDrawerActive = !isDrawerActive;
                    trigger.classList.toggle('active', isDrawerActive);
                    content.classList.toggle('active', isDrawerActive);
                    scrollToBottom();
                });
            }
            
            // 💡 渲染完畢，重要節點才做 KaTeX 與 Artifacts 解析 💡
            if (heavy) safeRenderMath(bubbleEl);
            maybeParseArtifacts(cleanDisplay, heavy);
        }

        // ==================================================================
        // P0-5：三層渲染 —— token 只排程；輕量渲染最多每 150ms 一次；
        // KaTeX / Artifacts 全文掃描 / 圖示重建留到工具事件與 done。
        // ==================================================================
        let renderTimer = null;
        let lastFenceCount = 0;

        // 串流中僅在「新的完整 code fence 出現」時才掃描 Artifacts，避免逐 token 掃全文
        function maybeParseArtifacts(cleanDisplay, heavy) {
            const fenceCount = (cleanDisplay.match(/```/g) || []).length;
            const hasNewCompleteBlock = fenceCount !== lastFenceCount && fenceCount % 2 === 0;
            if (heavy || (hasNewCompleteBlock && /```(html|xml)/.test(cleanDisplay))) {
                lastFenceCount = fenceCount;
                parseAndLoadArtifacts(cleanDisplay, false);
            }
        }

        function scheduleBubbleUpdate() {
            if (renderTimer) return;
            renderTimer = setTimeout(() => {
                renderTimer = null;
                updateBubble(false);
                scrollToBottom();
            }, 150);
        }

        function finalizeBubble() {
            if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
            updateBubble(true);
        }

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop(); // 緩衝不完整行
            
            for (const line of lines) {
                const trimmedLine = line.trim();
                if (trimmedLine.startsWith('event: ')) {
                    currentEventType = trimmedLine.slice(7).trim();
                } else if (trimmedLine.startsWith('data: ')) {
                    const dataStr = trimmedLine.slice(6).trim();
                    if (!dataStr) continue;
                    
                    try {
                        const eventData = JSON.parse(dataStr);
                        const eventType = currentEventType || eventData.type || 'token';
                        if (!streamEventMatches(streamIdentity, eventData)) continue;
                        pushSseLog(eventType, dataStr);
                        if (eventType !== 'token') handleAgentCollaborationEvent(eventType, eventData);
                        if (eventType !== 'token' && eventType !== 'approval_required') {
                            window.workbenchRunInspector?.handleEvent(eventType, eventData, streamIdentity);
                        }
                        
                        if (eventType === 'meta') {
                            if (eventData.run_id && String(eventData.run_id) !== streamIdentity.runId) continue;
                        } else if (eventType === 'skills') {
                            window.workbenchSkills?.handleRunEvent(eventData);
                        } else if (eventType === 'cancelled') {
                            chatProgressStatus = 'cancelled';
                            setAssistantResponsePhase(assistantMsgEl, 'clear');
                            agentCollaborationState.running = false;
                            addAgentCollaborationMessage('planner', '停止', eventData.message || '後端已停止 Agent Runtime。');
                        } else if (eventType === 'approval_required') {
                            const capabilityName = eventData.capability || '未命名系統能力';
                            let approved = false;
                            if (window.workbenchRunInspector?.handleApproval) {
                                approved = await window.workbenchRunInspector.handleApproval(
                                    eventData,
                                    streamIdentity,
                                    chatAbort?.signal
                                );
                            } else {
                                approved = window.confirm(
                                    `${eventData.message || 'Agent 要求執行系統級能力。'}\n\n`
                                    + `能力：${capabilityName}\n風險：${riskLabel(eventData.risk)}\n\n`
                                    + '只有在你了解這項操作時才按「確定」。'
                                );
                                const approvalRunId = eventData.run_id || currentChatRunId;
                                const approvalResponse = await apiFetch(
                                    `${API_BASE}/api/chat/runs/${encodeURIComponent(approvalRunId)}/approval`,
                                    {
                                        method: 'POST',
                                        headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({
                                            approval_id: eventData.approval_id,
                                            approved,
                                            decided_by: 'local_user'
                                        })
                                    }
                                );
                                if (!approvalResponse.ok) throw new Error('能力批准已逾時或無法送達後端。');
                            }
                            execLog.push({
                                kind: 'validation',
                                passed: approved,
                                text: `${capabilityName}：${approved ? '使用者已批准' : '使用者已拒絕'}`,
                                time: nowHM()
                            });
                            updateBubble();
                        } else if (eventType === 'tool_denied') {
                            execLog.push({
                                kind: 'validation',
                                passed: false,
                                text: eventData.message || `${eventData.tool || '工具'}：已立即拒絕，未請求批准`,
                                time: nowHM()
                            });
                            updateBubble();
                        } else if (eventType === 'deadline_exceeded') {
                            execLog.push({
                                kind: 'validation',
                                passed: false,
                                text: eventData.message || '本次執行已超過絕對時間預算，已中止。',
                                time: nowHM()
                            });
                            updateBubble();
                        } else if (eventType === 'answer_replacement') {
                            setAssistantResponsePhase(assistantMsgEl, 'answering');
                            accumulatedFinalAnswer = '';
                            currentTurnText = cleanAssistantText(eventData.content || '');
                            replyText = currentTurnText;
                            approxTokens = Math.max(0, Math.round(currentTurnText.length / 3));
                            updateBubble();
                            scrollToBottom();
                        } else if (eventType === 'token') {
                            const token = eventData.content;
                            const isFirstToken = !firstTokenAt && Boolean(token);
                            if (isFirstToken) {
                                firstTokenAt = performance.now();
                                setAssistantResponsePhase(assistantMsgEl, 'answering');
                            }
                            approxTokens += Math.max(1, Math.round((token || '').length / 3));
                            updateTaskProgress(progressId, {
                                detail: `正在串流生成 · 約 ${approxTokens} tokens`,
                                mode: 'indeterminate',
                                value: null
                            });
                            updateGenState(approxTokens, runStart, firstTokenAt);
                            currentTurnText += token;
                            replyText = currentTurnText; // 直接賦值，所有防漏過濾交給 updateBubble 統一處理
                            
                            // 🔍 P0-4：不顯示 raw thought 原文，只顯示通用執行狀態 🔍
                            // 進度描述一律以後端明確送出的 progress 事件為準
                            if (currentTurnText.includes('<thought>') && !currentTurnText.includes('</thought>')) {
                                thoughtChainVisualizer.classList.add('active');
                                thoughtDetail.textContent = '正在進行內部規劃...';
                            }
                            if (token.includes('</thought>')) {
                                thoughtChainVisualizer.classList.remove('active');
                            }

                            // P0-5：token 只排程節流渲染（Artifacts 偵測已整合進 maybeParseArtifacts）
                            if (isFirstToken) {
                                updateBubble(false);
                                scrollToBottom();
                            } else {
                                scheduleBubbleUpdate();
                            }
                        } else if (eventType === 'tool_start') {
                            // 💡 當工具開始執行時，說明當前這一輪生成已經結束。我們鎖定並累加這一輪已生成的正式內容 💡
                            let turnFinalText = replyText;
                            
                            // 進行強力過濾
                            turnFinalText = turnFinalText.replace(/<thought>[\s\S]*?<\/thought>/g, '');
                            turnFinalText = turnFinalText.replace(/<tool>[\s\S]*?<\/tool>/g, '');
                            turnFinalText = turnFinalText.replace(/```json[\s\S]*?```/g, '');
                            turnFinalText = turnFinalText.replace(/\bjson\s*\{[\s\S]*?\}/g, '');
                            
                            const thoughtIdx = turnFinalText.indexOf('<thought>');
                            if (thoughtIdx !== -1) turnFinalText = turnFinalText.slice(0, thoughtIdx);
                            const toolIdx = turnFinalText.indexOf('<tool>');
                            if (toolIdx !== -1) turnFinalText = turnFinalText.slice(0, toolIdx);
                            const jsonIdx = turnFinalText.indexOf('```json');
                            if (jsonIdx !== -1) turnFinalText = turnFinalText.slice(0, jsonIdx);
                            const braceIdx = turnFinalText.indexOf('{');
                            if (braceIdx !== -1 && turnFinalText.slice(braceIdx).includes('"tool"')) {
                                turnFinalText = turnFinalText.slice(0, braceIdx);
                            }
                            
                            accumulatedFinalAnswer += turnFinalText;
                            currentTurnText = ''; // 重置，迎接下一輪
                            replyText = '';
                            
                            // 生成指令中文描述
                            let toolDesc = `執行中: ${eventData.tool}`;
                            if (eventData.tool === 'web_browser_navigate') toolDesc = `執行中 開啟網頁: ${eventData.args.url || ''}`;
                            else if (eventData.tool === 'web_browser_screenshot') toolDesc = `執行中 對網頁進行截圖`;
                            else if (eventData.tool === 'execute_terminal_command') toolDesc = `執行中 系統指令: ${eventData.args.command || ''}`;
                            else if (eventData.tool === 'execute_python_code') toolDesc = `執行中 Python 代碼`;
                            else if (eventData.tool === 'web_search') toolDesc = `執行中 網路搜尋: ${eventData.args.query || ''}`;
                            
                            const toolEntry = { kind: 'tool', name: eventData.tool, desc: toolDesc, completed: false, time: nowHM() };
                            executedTools.push(toolEntry);
                            execLog.push(toolEntry); // 共享參照：tool_end 標記 completed 時，時間軸同步更新
                            
                            // 🔍 更新思維鏈狀態 🔍
                            thoughtChainVisualizer.classList.add('active');
                            thoughtDetail.textContent = toolDesc;
                            updateTaskProgress(progressId, { detail: toolDesc, mode: 'indeterminate', value: null });
                            
                            updateBubble();
                            scrollToBottom();
                        } else if (eventType === 'tool_end') {
                            currentTurnText = ''; // 工具結束時同樣重置
                            
                            // 🔍 更新思維鏈狀態 🔍
                            thoughtDetail.textContent = '整理並整合工具分析結果...';
                            updateTaskProgress(progressId, { detail: `工具完成：${eventData.tool}`, mode: 'indeterminate', value: null });
                            
                            // 將資料陣列中最後一個未完成項目置為完成狀態
                            const pendingTool = executedTools.findLast(t => !t.completed);
                            if (pendingTool) {
                                pendingTool.completed = true;
                            }
                            
                            updateBubble();
                            scrollToBottom();
                            
                            // 💡 自動在對話泡泡內渲染剛生成的網頁截圖 💡
                            if (eventData.tool === 'web_browser_screenshot' && eventData.result && eventData.result.includes('Success')) {
                                const fileMatch = eventData.result.match(/screenshot_\d+\.png/);
                                if (fileMatch) {
                                    const screenshotFilename = fileMatch[0];
                                    const screenshotUrl = '';
                                    
                                    const imgEl = document.createElement('img');
                                    imgEl.src = screenshotUrl;
                                    imgEl.className = 'chat-bubble-image';
                                    imgEl.style.marginTop = '12px';
                                    imgEl.alt = '網頁截圖';
                                    bubbleEl.appendChild(imgEl);
                                    scrollToBottom();
                                }
                            }
                        } else if (eventType === 'plan') {
                            // 🗺 Agent 顯式任務計畫（後端 Planner 事件）
                            runTasks = (eventData.tasks || []).map(t => ({
                                id: t.id,
                                title: t.title || '',
                                status: t.status || 'pending'
                            }));
                            execLog.push({ kind: 'progress', text: `已建立任務清單（${runTasks.length} 項）`, time: nowHM() });
                            thoughtChainVisualizer.classList.add('active');
                            thoughtDetail.textContent = '已規劃任務清單，開始執行...';
                            const firstPending = runTasks.find(task => task.status === 'in_progress' || task.status === 'pending');
                            updateTaskProgress(progressId, {
                                label: `Agent 計畫 · ${runTasks.length} 個步驟`,
                                detail: firstPending?.title || '任務計畫已建立',
                                mode: 'indeterminate', value: null
                            });
                            updateBubble();
                            scrollToBottom();
                        } else if (eventType === 'task_update') {
                            const t = runTasks.find(x => x.id === eventData.task_id);
                            if (t && eventData.status) t.status = eventData.status;
                            if (eventData.message) {
                                execLog.push({ kind: 'progress', text: eventData.message, time: nowHM() });
                                updateTaskProgress(progressId, { detail: eventData.message, mode: 'indeterminate', value: null });
                            }
                            updateBubble();
                            scrollToBottom();
                        } else if (eventType === 'agent_spawned') {
                            const role = eventData.role || 'agent';
                            execLog.push({ kind: 'progress', text: `${role}：已接受獨立工作`, time: nowHM() });
                            updateBubble();
                            scrollToBottom();
                        } else if (eventType === 'agent_message') {
                            const role = eventData.role || 'agent';
                            const tag = eventData.tag ? `／${eventData.tag}` : '';
                            const msg = eventData.message || eventData.summary || '';
                            if (msg) execLog.push({ kind: 'progress', text: `${role}${tag}：${msg}`, time: nowHM() });
                            updateBubble();
                            scrollToBottom();
                        } else if (eventType === 'agent_completed' || eventType === 'agent_failed') {
                            const role = eventData.role || 'agent';
                            const state = eventType === 'agent_completed' ? '完成' : '未完成';
                            execLog.push({
                                kind: 'validation',
                                passed: eventType === 'agent_completed',
                                text: `${role}：${eventData.message || state}`,
                                time: nowHM()
                            });
                            updateBubble();
                            scrollToBottom();
                        } else if (eventType === 'progress') {
                            // 人話進度摘要（非思維鏈原文）
                            const msg = eventData.message || '';
                            execLog.push({ kind: 'progress', text: msg, time: nowHM() });
                            thoughtChainVisualizer.classList.add('active');
                            if (msg) thoughtDetail.textContent = msg;
                            if (msg) updateTaskProgress(progressId, { detail: msg, mode: 'indeterminate', value: null });
                            updateBubble();
                            scrollToBottom();
                        } else if (eventType === 'commentary') {
                            const msg = eventData.message || '';
                            execLog.push({ kind: 'progress', text: msg, time: nowHM() });
                            thoughtChainVisualizer.classList.add('active');
                            if (msg) thoughtDetail.textContent = msg;
                            if (msg) updateTaskProgress(progressId, { detail: msg, mode: 'indeterminate', value: null });
                            updateBubble();
                            scrollToBottom();
                        } else if (eventType === 'phase') {
                            const msg = eventData.message || eventData.phase || '';
                            execLog.push({ kind: 'progress', text: msg, time: nowHM() });
                            thoughtChainVisualizer.classList.add('active');
                            if (msg) thoughtDetail.textContent = msg;
                            if (msg) updateTaskProgress(progressId, { label: `Agent · ${eventData.phase || '執行中'}`, detail: msg, mode: 'indeterminate', value: null });
                            updateBubble();
                            scrollToBottom();
                        } else if (eventType === 'repair') {
                            const msg = `第 ${eventData.round || '?'} 輪自動修正：${eventData.reason || '依驗證結果調整'}`;
                            execLog.push({ kind: 'progress', text: msg, time: nowHM() });
                            thoughtChainVisualizer.classList.add('active');
                            thoughtDetail.textContent = msg;
                            updateTaskProgress(progressId, { label: `Agent 自動修正 · 第 ${eventData.round || '?'} 輪`, detail: msg, mode: 'indeterminate', value: null });
                            updateBubble();
                            scrollToBottom();
                        } else if (eventType === 'validation') {
                            execLog.push({
                                kind: 'validation',
                                passed: !!eventData.passed,
                                text: eventData.details || (eventData.passed ? '驗證通過' : '驗證未通過'),
                                time: nowHM()
                            });
                            updateBubble();
                            scrollToBottom();
                        } else if (eventType === 'metrics') {
                            authoritativeMetrics = eventData;
                        } else if (eventType === 'final') {
                            finalSummary = eventData.summary || '';
                            finalValidationPassed = eventData.validation_passed !== false;
                            showFinalReport = typeof eventData.show_report === 'boolean'
                                ? eventData.show_report
                                : !finalValidationPassed || executedTools.some(item => REPORTABLE_WORK_TOOLS.has(item.name));
                            runInProgress = false;
                            updateBubble();
                            scrollToBottom();
                        } else if (eventType === 'context' || eventType === 'sources') {
                            // 📚 P0-3：RAG 檢索來源事件（相容後端現行 context 與未來 sources 命名）
                            sources = eventData.sources || [];
                            renderSources(assistantMsgEl, sources);
                            scrollToBottom();
                        } else if (eventType === 'done') {
                            // 🔍 隱藏思維鏈 🔍
                            thoughtChainVisualizer.classList.remove('active');
                            setAssistantResponsePhase(assistantMsgEl, 'clear');

                            // P0-5：終局重渲染（完整 Markdown + KaTeX + 圖示一次到位）
                            finalizeBubble();

                            // P0-1：把「乾淨正式回答」寫回 conversationState（UI 顯示與 LLM 記憶分離）
                            const finalAssistantText = cleanAssistantText(accumulatedFinalAnswer + replyText);
                            if (finalAssistantText) {
                                addLLMMessage('assistant', finalAssistantText, {
                                    sources: sources.map(s => ({
                                        source: s.source,
                                        page: s.page,
                                        rank: s.rank,
                                        score: s.score,
                                        score_source: s.score_source
                                    })),
                                    tool_events: execLog.length
                                });
                            }

                            // ✅ 執行結束：面板頭部停止呼吸燈並自動收合（DOM 層級調整，避免重繪清掉已附加的截圖）
                            runInProgress = false;
                            const doneTrigger = bubbleEl.querySelector('.drawer-trigger');
                            const doneContent = bubbleEl.querySelector('.drawer-content');
                            if (doneTrigger) {
                                doneTrigger.classList.remove('running');
                                const dot = doneTrigger.querySelector('.agent-breathe-dot');
                                if (dot) dot.outerHTML = '<i data-lucide="terminal" class="drawer-icon"></i>';
                                const statusTextEl = doneTrigger.querySelector('.drawer-status-text');
                                if (statusTextEl) statusTextEl.textContent = `執行過程 · ${execLog.length} 個動作`;
                                if (doneContent) {
                                    isDrawerActive = false;
                                    doneTrigger.classList.remove('active');
                                    doneContent.classList.remove('active');
                                }
                                safeCreateIcons();
                            }
                            // 💡 生成結束，深度解析並加載任何產生的 Artifacts 💡
                            parseAndLoadArtifacts(accumulatedFinalAnswer + replyText, true);

                            await loadSessions();

                            // Workbench：run 紀錄、答案卡動作/指標列、chips 更新
                            const runMetrics = computeRunMetrics(runStart, firstTokenAt, approxTokens);
                            if (authoritativeMetrics) {
                                const serverTokps = authoritativeMetrics.tokens_per_second;
                                const completionTokens = Number(authoritativeMetrics.usage?.completion_tokens || 0);
                                runMetrics.elapsed = Number(authoritativeMetrics.elapsed_ms || 0) / 1000 || runMetrics.elapsed;
                                runMetrics.ttft = authoritativeMetrics.first_token_ms == null
                                    ? runMetrics.ttft
                                    : Number(authoritativeMetrics.first_token_ms) / 1000;
                                runMetrics.tokps = serverTokps == null || !Number.isFinite(Number(serverTokps))
                                    ? null
                                    : Number(serverTokps);
                                runMetrics.tokens = completionTokens > 0 ? completionTokens : runMetrics.tokens;
                                runMetrics.tokpsBasis = authoritativeMetrics.tokens_per_second_basis || 'not_available';
                                lastMetrics = { ...runMetrics };
                            }
                            recordRun({
                                model: payload.model,
                                metrics: runMetrics,
                                events: execLog.slice(),
                                tasks: runTasks.slice(),
                                sources: sources.slice()
                            });
                            appendAnswerFooter(bubbleEl, {
                                text: finalAssistantText,
                                metrics: runMetrics,
                                usedRag: payload.use_rag,
                                sourceCount: sources.length,
                                artifactProduced: !!activeArtifactCode && activeArtifactCode !== artifactAtStart
                            });
                            updateCtxChip();

                            break;
                        } else if (eventType === 'error') {
                            chatProgressStatus = 'failed';
                            setAssistantResponsePhase(assistantMsgEl, 'clear');
                            const errorMessage = eventData.content || eventData.message || '未知錯誤';
                            bubbleEl.innerHTML = `<span style="color: var(--danger-color)">錯誤: ${escapeHtml(errorMessage)}</span>`;
                        }
                    } catch (e) {
                        console.error('Failed to parse SSE JSON:', e);
                    } finally {
                        currentEventType = null;
                    }
                }
            }
        }
    } catch (error) {
        if (error && error.name === 'AbortError') {
            chatProgressStatus = 'cancelled';
            window.workbenchRunInspector?.handleEvent('cancelled', {
                run_id: currentChatRunId,
                message: '使用者已停止目前執行。',
            }, streamIdentity || {});
            agentCollaborationState.running = false;
            addAgentCollaborationMessage('planner', '停止', '使用者已停止目前 Agent 工作。');
            Object.values(agentCollaborationState.agents).forEach(agent => {
                if (AGENT_ACTIVE_STATES.has(agent.status)) setAgentStatus(agent.id, 'idle', { role: agent.role });
            });
            // 使用者手動停止：保留已生成的部分內容，不回滾 user 訊息（後端已收到）
            if (thinkingEl) thinkingEl.remove();
            setAssistantResponsePhase(assistantMsgEl, 'clear');
            thoughtChainVisualizer.classList.remove('active');
            bubbleEl.insertAdjacentHTML('beforeend', '<div class="answer-ragnote">已停止生成（本輪回覆未完整）。</div>');
        } else {
            chatProgressStatus = 'failed';
            window.workbenchRunInspector?.markError(error, { retryAllowed: false });
            agentCollaborationState.running = false;
            setAgentStatus('critic', 'blocked');
            addAgentCollaborationMessage('critic', '阻塞', `執行中斷：${error?.message || '未知錯誤'}`, { tone: 'challenge' });
            console.error('Chat failed:', error);
            if (thinkingEl) thinkingEl.remove();
            setAssistantResponsePhase(assistantMsgEl, 'clear');
            // P0-1：請求失敗時回滾本輪 user 訊息，避免使用者重試後上下文出現重複問題
            if (
                userMessageAddedToConversation
                && conversationState.length > 0
                && conversationState[conversationState.length - 1].role === 'user'
            ) {
                conversationState.pop();
            }
            bubbleEl.innerHTML = renderConnectionErrorCard();
            bubbleEl.querySelector('[data-recovery-action="reload"]')
                ?.addEventListener('click', () => location.reload());
            bubbleEl.querySelector('[data-recovery-action="settings"]')
                ?.addEventListener('click', () => document.getElementById('btn-settings-trigger')?.click());
        }
    } finally {
        finishTaskProgress(
            progressId,
            chatProgressStatus,
            chatProgressStatus === 'completed' ? '回覆生成完成' : chatProgressStatus === 'cancelled' ? '已停止生成' : '回覆生成失敗'
        );
        isGenerating = false;
        setAssistantResponsePhase(assistantMsgEl, 'clear');
        setGeneratingUI(false);
        chatAbort = null;
        currentChatRunId = null;
        renderAgentCollaboration();
        void window.workbenchProjectSkills?.refreshSession?.();
        scrollToBottom();
    }
}

// 建立 HTML 對話訊息 (支持多模態圖片渲染)
function appendMessage(role, text, images = []) {
    const msgEl = document.createElement('div');
    msgEl.className = `message ${role}`;
    if (role === 'assistant') msgEl.setAttribute('aria-label', 'Agent 回答');
    
    const avatarIcon = role === 'user' ? 'user' : 'bot';
    const renderedContent = role === 'user' ? escapeHtml(text) : renderMarkdownSafe(text);
    const content = role === 'assistant'
        ? `<div class="assistant-answer-content">${renderedContent}</div>`
        : renderedContent;
    
    // 渲染訊息文字內容
    msgEl.innerHTML = `
        <div class="avatar">
            <i data-lucide="${avatarIcon}"></i>
        </div>
        <div class="message-content-wrapper">
            <div class="message-bubble">${content}</div>
        </div>
    `;
    
    // 如果含有上傳圖片，在泡泡內部渲染圖片縮圖
    if (images && images.length > 0) {
        const bubble = msgEl.querySelector('.message-bubble');
        images.forEach(imgBase64 => {
            const img = document.createElement('img');
            img.src = imgBase64;
            img.className = 'chat-bubble-image';
            img.alt = '上傳的圖片';
            bubble.appendChild(img);
        });
    }
    
    chatMessages.appendChild(msgEl);
    safeCreateIcons();
    scrollToBottom();
    return msgEl;
}

// 還原已保存的 Agent 執行過程；只呈現可公開的規劃、操作與驗證摘要。
function appendHistoricalMessage(message) {
    const msgEl = appendMessage(message.role, message.content || '', message.images || []);
    const events = Array.isArray(message.process_events) ? message.process_events : [];
    if (message.role !== 'assistant' || events.length === 0) return msgEl;

    const tasks = [];
    const log = [];
    let finalSummary = '';
    let showFinalReport = false;
    let finalValidationPassed = true;
    let hasReportableWork = false;
    const toolEntries = new Map();
    const eventTime = (item) => {
        const date = new Date(item.created_at || '');
        return Number.isNaN(date.getTime()) ? '' : date.toTimeString().slice(0, 5);
    };

    for (const item of events) {
        if (item.type === 'plan') {
            tasks.splice(0, tasks.length, ...(item.tasks || []).map(task => ({ ...task })));
        } else if (item.type === 'task_update') {
            const task = tasks.find(entry => entry.id === item.task_id);
            if (task) task.status = item.status || task.status;
            if (item.message) log.push({ kind: 'progress', text: item.message, time: eventTime(item) });
        } else if (item.type === 'commentary' || item.type === 'progress' || item.type === 'repair' || item.type === 'phase') {
            const repairPrefix = item.type === 'repair' ? `第 ${item.round || '?'} 輪自動修正：` : '';
            log.push({ kind: 'progress', text: repairPrefix + (item.message || item.reason || ''), time: eventTime(item) });
        } else if (item.type === 'agent_spawned') {
            log.push({ kind: 'progress', text: `${item.role || 'agent'}：已接受獨立工作`, time: eventTime(item) });
        } else if (item.type === 'agent_message') {
            const tag = item.tag ? `／${item.tag}` : '';
            log.push({ kind: 'progress', text: `${item.role || 'agent'}${tag}：${item.message || item.summary || ''}`, time: eventTime(item) });
        } else if (item.type === 'agent_completed' || item.type === 'agent_failed') {
            log.push({
                kind: 'validation',
                passed: item.type === 'agent_completed',
                text: `${item.role || 'agent'}：${item.message || (item.type === 'agent_completed' ? '完成' : '未完成')}`,
                time: eventTime(item)
            });
        } else if (item.type === 'tool_start') {
            const entry = { kind: 'tool', name: item.tool, desc: `執行工具：${item.tool}`, completed: false, time: eventTime(item) };
            if (REPORTABLE_WORK_TOOLS.has(item.tool)) hasReportableWork = true;
            toolEntries.set(item.sequence || `${item.tool}-${log.length}`, entry);
            log.push(entry);
        } else if (item.type === 'tool_end') {
            const entry = toolEntries.get(item.sequence || `${item.tool}-${log.length}`) || [...toolEntries.values()].reverse().find(value => value.name === item.tool && !value.completed);
            if (entry) entry.completed = true;
        } else if (item.type === 'validation') {
            log.push({ kind: 'validation', passed: !!item.passed, text: item.details || '', time: eventTime(item) });
        } else if (item.type === 'final') {
            finalSummary = item.summary || '';
            finalValidationPassed = item.validation_passed !== false;
            showFinalReport = typeof item.show_report === 'boolean'
                ? item.show_report
                : !finalValidationPassed || hasReportableWork;
        }
    }

    const statusMark = { pending: '○', in_progress: '◐', completed: '✓', failed: '✗' };
    const taskHTML = tasks.length ? `
        <div class="agent-task-list">
            <div class="agent-task-list-title">任務清單 Tasks</div>
            ${tasks.map(task => {
                const status = statusMark[task.status] ? task.status : 'pending';
                return `<div class="agent-task-item ${status}"><span class="agent-task-status">${statusMark[status]}</span><span class="agent-task-title">${escapeHtml(task.title || '')}</span></div>`;
            }).join('')}
        </div>` : '';
    const logHTML = log.map(entry => {
        const time = `<span class="agent-log-time">${escapeHtml(entry.time || '')}</span>`;
        if (entry.kind === 'tool') {
            return `<li class="drawer-step-item completed">${time}<i data-lucide="check-circle" class="drawer-step-icon" style="color:#10b981;"></i><span class="step-text">${escapeHtml(entry.desc)}（${entry.completed ? '已完成' : '未完成'}）</span></li>`;
        }
        const mark = entry.kind === 'validation' ? (entry.passed ? '✓ ' : '⚠ ') : '';
        const stateClass = entry.kind === 'validation' ? (entry.passed ? 'validation-pass' : 'validation-fail') : '';
        return `<li class="drawer-step-item completed">${time}<div class="agent-log-line ${stateClass}"><span class="agent-log-text">${mark}${escapeHtml(entry.text || '')}</span></div></li>`;
    }).join('');

    const bubble = msgEl.querySelector('.message-bubble');
    const contentHTML = renderMarkdownSafe(message.content || '');
    bubble.innerHTML = `
        <div class="agent-tool-drawer">
            <div class="drawer-trigger"><i data-lucide="terminal" class="drawer-icon"></i><span class="drawer-status-text">執行過程 · ${log.length} 個動作</span><i data-lucide="chevron-down" class="drawer-chevron"></i></div>
            <div class="drawer-content">${taskHTML}<ul class="drawer-steps-list">${logHTML}</ul></div>
        </div>
        <div class="assistant-answer-content">${contentHTML}</div>
        ${renderAgentWorkReport(finalSummary, showFinalReport, finalValidationPassed)}`;

    const trigger = bubble.querySelector('.drawer-trigger');
    const drawer = bubble.querySelector('.drawer-content');
    trigger?.addEventListener('click', () => {
        trigger.classList.toggle('active');
        drawer?.classList.toggle('active');
    });
    safeCreateIcons();
    return msgEl;
}

const REPORTABLE_WORK_TOOLS = new Set(['write_file', 'execute_terminal_command', 'execute_python_code']);

function compactFinalReportMarkdown(summary) {
    const keepSections = [
        { match: /files changed|變更檔案/i, title: 'Files Changed / 變更檔案' },
        { match: /validation|驗證/i, title: 'Validation / 驗證' },
        { match: /risks?|風險/i, title: 'Risks / 風險' },
        { match: /next step|下一步/i, title: 'Next Step / 下一步' }
    ];
    const output = [];
    let activeSection = null;
    for (const line of String(summary || '').split(/\r?\n/)) {
        const heading = line.match(/^#{2,4}\s+(.+?)\s*$/);
        if (heading) {
            activeSection = keepSections.find(section => section.match.test(heading[1])) || null;
            if (activeSection) output.push(`### ${activeSection.title}`);
            continue;
        }
        if (activeSection) output.push(line);
    }
    return output.join('\n').trim();
}

function renderAgentWorkReport(summary, showReport, validationPassed = true) {
    if (!summary || !showReport) return '';
    const compact = compactFinalReportMarkdown(summary);
    if (!compact) return '';
    return `<details class="agent-final-block">
        <summary class="agent-final-summary">
            <span>工作報告</span>
            <span class="agent-final-state ${validationPassed ? 'passed' : 'warning'}">${validationPassed ? '已完成' : '需注意'}</span>
        </summary>
        <div class="agent-final-content">${renderMarkdownSafe(compact)}</div>
    </details>`;
}

function setAssistantResponsePhase(messageEl, phase) {
    if (!messageEl) return;
    const active = phase === 'thinking' || phase === 'answering';
    messageEl.classList.toggle('is-thinking', phase === 'thinking');
    messageEl.classList.toggle('is-streaming', phase === 'answering');
    messageEl.setAttribute('aria-busy', active ? 'true' : 'false');
}

// 建立助理 placeholder；只顯示公開活動狀態，不顯示模型內部推理內容。
function createAssistantMessagePlaceholder() {
    const msgEl = document.createElement('div');
    msgEl.className = 'message assistant is-thinking';
    msgEl.setAttribute('aria-label', 'Agent 回答');
    msgEl.setAttribute('aria-busy', 'true');
    
    msgEl.innerHTML = `
        <div class="avatar" aria-hidden="true">
            <i data-lucide="bot"></i>
        </div>
        <div class="message-content-wrapper">
            <div class="message-bubble">
                <div class="assistant-thinking" role="status" aria-live="polite">
                    <span class="assistant-thinking-mark" aria-hidden="true"><i data-lucide="sparkles"></i></span>
                    <span class="assistant-thinking-copy">
                        <strong>Agent 正在思考</strong>
                        <small>正在理解你的訊息並組織回答</small>
                    </span>
                    <span class="assistant-thinking-dots" aria-hidden="true">
                        <i></i><i></i><i></i>
                    </span>
                </div>
            </div>
        </div>
    `;
    
    chatMessages.appendChild(msgEl);
    safeCreateIcons();
    scrollToBottom();
    return msgEl;
}

// 渲染參考來源折疊區
function formatSourceScore(source, suffix = ' 關聯度') {
    const value = Number(source?.score);
    if (source?.score === null || source?.score === undefined || !Number.isFinite(value)) {
        return '僅依排序';
    }
    return `${Math.round(value * 100)}%${suffix}`;
}

function renderSources(messageEl, sources) {
    if (!sources || sources.length === 0) return;
    
    const wrapper = messageEl.querySelector('.message-content-wrapper');
    const bubbleEl = messageEl.querySelector('.message-bubble');
    
    const sourcesEl = document.createElement('div');
    sourcesEl.className = 'rag-sources'; // P1-1：對齊 CSS 既有 class
    
    let sourcesListHtml = '';
    sources.forEach((source, index) => {
        const scoreLabel = formatSourceScore(source);
        const pageText = source.page ? ` (第 ${source.page} 頁)` : '';
        sourcesListHtml += `
            <div class="source-item">
                <div class="source-item-header">
                    <span class="source-num">${index + 1}</span>
                    <span class="source-name" title="${escapeHtml(source.source)}">${escapeHtml(source.source)}${pageText}</span>
                    <span class="source-score">${scoreLabel}</span>
                </div>
                <div class="source-item-content">${escapeHtml(source.content)}</div>
            </div>
        `;
    });
    
    sourcesEl.innerHTML = `
        <div class="sources-trigger">
            <i data-lucide="book-open"></i>
            <span>已檢索出 ${sources.length} 個相關知識庫片段</span>
            <i data-lucide="chevron-down" class="chevron" style="margin-left:auto;"></i>
        </div>
        <div class="sources-content">
            ${sourcesListHtml}
        </div>
    `;
    
    // 插入在回覆 bubble 之前
    wrapper.insertBefore(sourcesEl, bubbleEl);
    safeCreateIcons();
    
    const trigger = sourcesEl.querySelector('.sources-trigger');
    const content = sourcesEl.querySelector('.sources-content');
    trigger.addEventListener('click', () => {
        trigger.classList.toggle('active');
        content.classList.toggle('active');
        scrollToBottom();
    });
}

// ==========================================================================
// 5. 基礎工具函數
// ==========================================================================

// 安全建立 Lucide Icons，避免對不存在的元素調用
function safeCreateIcons() {
    try {
        if (typeof lucide !== 'undefined' && lucide.createIcons) {
            lucide.createIcons();
        }
    } catch (e) {
        console.warn('Lucide icons creation failed:', e);
    }
}

// ==========================================================================
// P0-6：安全 Markdown 渲染（唯一入口）
// 優先使用 marked（GFM）+ DOMPurify sanitize；若 CDN 載入失敗則退回內建 parseMarkdown。
// 所有訊息 HTML 一律經由 renderMarkdownSafe 產生，不允許各功能自行拼 innerHTML。
// ==========================================================================
function renderMarkdownSafe(text) {
    if (!text) return '';
    const protectedMath = protectMathForMarkdown(text);
    try {
        if (typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
            const rawHtml = marked.parse(protectedMath.text, { breaks: true, gfm: true });
            const sanitized = DOMPurify.sanitize(rawHtml, { USE_PROFILES: { html: true } });
            return restoreMathAfterMarkdown(sanitized, protectedMath.segments);
        }
    } catch (e) {
        console.warn('[Markdown] marked/DOMPurify 渲染失敗，退回內建 parser:', e);
    }
    return restoreMathAfterMarkdown(
        parseMarkdown(protectedMath.text), protectedMath.segments
    ); // 內建 fallback（已先 escapeHtml，注入安全）
}

// 簡單的 Markdown 解析器，支援粗體、程式碼框、分段、清單與斜體
function parseMarkdown(text) {
    if (!text) return '';
    let html = text;
    
    // 轉義 HTML 標籤防注入
    html = escapeHtml(html);
    
    // 1. 程式碼區塊 (Code blocks)
    html = html.replace(/```([\s\S]*?)```/g, (match, code) => {
        // 還原 escape 掉的換行
        return `<pre><code>${code.trim()}</code></pre>`;
    });
    
    // 2. 行內程式碼 (Inline code)
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    
    // 3. 粗體 (Bold)
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    
    // 4. 斜體 (Italic)
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    
    // 5. 無序清單 (Lists)
    html = html.replace(/^\*\s+(.+)$/gm, '<li>$1</li>');
    html = html.replace(/(<li>.*<\/li>)/gs, '<ul>$1</ul>');
    
    // 6. 分段 (Newlines to paragraphs)
    html = html.split(/\n{2,}/).map(p => {
        if (p.trim().startsWith('<pre>') || p.trim().startsWith('<ul>')) {
            return p;
        }
        return `<p>${p.replace(/\n/g, '<br>')}</p>`;
    }).join('');
    
    return html;
}

// HTML 轉義
function escapeHtml(text) {
    if (!text) return '';
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return text.replace(/[&<>"']/g, function(m) { return map[m]; });
}

// 滾動到底部
function scrollToBottom() {
    setTimeout(() => {
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }, 50);
}

/* ==========================================================================
   第十階段：高階核心功能 (語音輸入、拖放上傳、斜線指令、Artifacts 沙盒) 實現
   ========================================================================== */

// 1. 語音輸入模組
function initSpeechRecognition() {
    const SpeechLib = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechLib) {
        voiceInputBtn.style.display = 'none';
        return;
    }
    speechRecognition = new SpeechLib();
    speechRecognition.continuous = false;
    speechRecognition.interimResults = false;
    speechRecognition.lang = 'zh-TW';
    
    speechRecognition.onstart = () => {
        isRecording = true;
        voiceInputBtn.classList.add('recording');
        voiceInputBtn.title = '正在錄音...再次點擊停止';
    };
    
    speechRecognition.onresult = (e) => {
        const resultText = e.results[0][0].transcript;
        if (resultText) {
            userInput.value += resultText;
            userInput.dispatchEvent(new Event('input'));
        }
    };
    
    speechRecognition.onerror = (e) => {
        console.error('Speech recognition error:', e);
        stopRecording();
    };
    
    speechRecognition.onend = () => {
        stopRecording();
    };
}

function stopRecording() {
    isRecording = false;
    voiceInputBtn.classList.remove('recording');
    voiceInputBtn.title = '語音輸入';
    if (speechRecognition) {
        try { speechRecognition.stop(); } catch(e) {}
    }
}

// 2. 拖放上傳/臨時知識庫模組
function initDragAndDrop() {
    const chatContainer = document.querySelector('.chat-container');
    if (!chatContainer) return;
    
    chatContainer.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.stopPropagation();
        dragOverlayMask.classList.add('active');
    });
    
    chatContainer.addEventListener('dragleave', (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (e.relatedTarget === null || !chatContainer.contains(e.relatedTarget)) {
            dragOverlayMask.classList.remove('active');
        }
    });
    
    chatContainer.addEventListener('drop', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        dragOverlayMask.classList.remove('active');
        
        const files = e.dataTransfer.files;
        if (files.length === 0) return;
        
        const file = files[0];
        const ext = file.name.split('.').pop().toLowerCase();
        
        if (!['pdf', 'txt', 'md', 'py', 'js', 'html', 'css', 'json'].includes(ext)) {
            showToast('不支援的檔案格式，請上傳 PDF 或普通文字文檔！');
            return;
        }
        
        showTempContextLoading(file.name);
        
        if (ext === 'pdf') {
            const formData = new FormData();
            formData.append('file', file);
            if (currentSessionId) formData.append('session_id', currentSessionId);
            
            try {
                const response = await apiFetch(`${API_BASE}/api/documents/parse-temp`, {
                    method: 'POST',
                    body: formData
                });
                const data = await response.json();
                if (data.success) {
                    setTemporaryContext(data.filename, data.text);
                } else {
                    throw new Error(data.detail || '解析失敗');
                }
            } catch (err) {
                showToast(`檔案解析錯誤: ${err.message}`);
                clearTemporaryContext();
            }
        } else {
            const reader = new FileReader();
            reader.onload = (evt) => {
                setTemporaryContext(file.name, evt.target.result);
            };
            reader.onerror = () => {
                showToast('檔案讀取失敗！');
                clearTemporaryContext();
            };
            reader.readAsText(file);
        }
    });
}

function showTempContextLoading(filename) {
    tempContextBar.classList.add('active');
    tempContextFilename.textContent = `正在解析 ${filename}...`;
    btnRemoveTempContext.style.display = 'none';
}

function setTemporaryContext(filename, text) {
    temporaryContextText = text;
    tempContextBar.classList.add('active');
    tempContextFilename.textContent = filename;
    btnRemoveTempContext.style.display = 'flex';
    safeCreateIcons();
    console.log('[TempContext] Successfully loaded document:', filename, 'Length:', text.length);
}

function clearTemporaryContext() {
    temporaryContextText = '';
    tempContextBar.classList.remove('active');
    tempContextFilename.textContent = '';
}

// 3. 斜線指令模組
function initSlashCommands() {
    if (BASIC_CHAT_MODE) return;
    window.workbenchSkills?.initSlashCommands({
        input: userInput, menu: slashCommandsMenu, clearHistory: clearChatHistory
    });
}

async function clearChatHistory() {
    if (confirm('確定要清空目前會話嗎？這將不會影響您的全局知識庫。')) {
        if (currentSessionId) {
            try {
                await apiFetch(`${API_BASE}/api/sessions/${currentSessionId}`, { method: 'DELETE' });
                currentSessionId = null;
                clearOutputSkillsContext('請先選擇專案');
                resetConversationState(); // P0-1
                chatMessages.innerHTML = '';
                welcomeCard.style.display = 'flex';
                loadSessions();
            } catch (e) {
                console.error('Failed to delete session:', e);
            }
        }
    }
}

// 4. Artifacts 沙盒渲染模組
function parseAndLoadArtifacts(text, forceRenderIframe = false) {
    if (!text) return;
    
    // 支援流式未閉合代碼塊
    const htmlMatch = text.match(/```html([\s\S]*?)(?:```|$)/);
    const svgMatch = text.match(/```xml([\s\S]*?<svg[\s\S]*?)(?:```|$)/) || text.match(/(<svg[\s\S]*?<\/svg>)/);
    
    console.log('[Artifacts] parseAndLoadArtifacts called. textLength:', text.length, 'htmlMatch:', !!htmlMatch, 'svgMatch:', !!svgMatch, 'force:', forceRenderIframe);
    
    if (htmlMatch) {
        const code = htmlMatch[1].trim();
        activeArtifactCode = code;
        activeArtifactTitle = 'HTML 互動網頁原型';
        activeArtifactExt = 'html';
        showArtifactsPanel(forceRenderIframe);
    } else if (svgMatch) {
        const code = svgMatch[1].trim();
        activeArtifactCode = code;
        activeArtifactTitle = 'SVG 向量圖預覽';
        activeArtifactExt = 'svg';
        showArtifactsPanel(forceRenderIframe);
    } else {
        // 裸 HTML / DOCTYPE / SVG 檢測容錯
        const lowerText = text.toLowerCase();
        if (lowerText.includes('<!doctype html>') || lowerText.includes('<html') || lowerText.includes('<body') || lowerText.includes('<div class="clock"')) {
            activeArtifactCode = text.trim();
            activeArtifactTitle = 'HTML 互動網頁原型 (自動識別)';
            activeArtifactExt = 'html';
            showArtifactsPanel(forceRenderIframe);
        } else if (lowerText.includes('<svg')) {
            activeArtifactCode = text.trim();
            activeArtifactTitle = 'SVG 向量圖預覽 (自動識別)';
            activeArtifactExt = 'svg';
            showArtifactsPanel(forceRenderIframe);
        }
    }
}

function showArtifactsPanel(forceRenderIframe = false) {
    const opened = openInspector('artifact');
    if (opened && btnSandboxToggle) {
        btnSandboxToggle.classList.add('active');
    }
    sandboxTitle.textContent = activeArtifactTitle;
    
    // 將代碼存入虛擬專案中的 index.html
    if (virtualProjectFiles["index.html"]) {
        virtualProjectFiles["index.html"].code = activeArtifactCode;
        virtualProjectFiles["index.html"].timestamp = new Date().toLocaleTimeString('zh-TW', { hour12: false });
    }
    
    // 更新編輯器內容
    sandboxCodeEditor.value = virtualProjectFiles[activeVirtualFilePath].code;
    
    // 渲染左側專案虛擬目錄樹
    renderVirtualFileTree();
    
    // 渲染預覽 iframe (如果當前是 index.html)
    if (forceRenderIframe || !sandboxIframe.srcdoc) {
        if (activeArtifactExt === 'html') {
            refreshSandboxPreview(); // P0-7：合併 index.html + css + js
        } else if (activeArtifactExt === 'svg') {
            const svgHtml = `
                <!DOCTYPE html>
                <html>
                <head>
                    <style>
                        html, body { margin:0; padding:0; width:100%; height:100%; display:flex; align-items:center; justify-content:center; background:${document.documentElement.getAttribute('data-theme') === 'dark' ? '#0f0f15' : '#f6f5f2'}; overflow:hidden; }
                        svg { max-width:90%; max-height:90%; }
                    </style>
                </head>
                <body>${activeArtifactCode}</body>
                </html>
            `;
            sandboxIframe.srcdoc = prepareSandboxHtml(svgHtml);
        }
    }
    
    safeCreateIcons();
}

// 遞迴渲染 VFS 檔案目錄樹
function renderVirtualFileTree(filterQuery = "") {
    sandboxStashList.innerHTML = '';
    
    // 建立樹狀階層結構
    const root = { name: "root", type: "folder", children: {}, path: "" };
    
    Object.keys(virtualProjectFiles).forEach(filePath => {
        // 如果有搜尋過濾，且路徑與過濾條件不匹配，直接跳過
        if (filterQuery && !filePath.toLowerCase().includes(filterQuery.toLowerCase())) {
            return;
        }
        
        const parts = filePath.split('/');
        let current = root;
        let currentPath = "";
        
        parts.forEach((part, index) => {
            currentPath = currentPath ? `${currentPath}/${part}` : part;
            const isLast = index === parts.length - 1;
            
            if (isLast) {
                current.children[part] = {
                    name: part,
                    type: "file",
                    path: currentPath,
                    ext: virtualProjectFiles[filePath].ext
                };
            } else {
                if (!current.children[part]) {
                    current.children[part] = {
                        name: part,
                        type: "folder",
                        path: currentPath,
                        children: {}
                    };
                }
                current = current.children[part];
            }
        });
    });

    // 遞迴渲染節點 DOM
    function buildTreeNodeDOM(node, depth = 0) {
        if (node.type === "file") {
            const row = document.createElement('div');
            row.className = `stash-item-row ${node.path === activeVirtualFilePath ? 'active' : ''}`;
            row.style.paddingLeft = `${depth * 12 + 8}px`;

            row.innerHTML = `
                <span class="stash-node-arrow empty"></span>
                <span class="stash-node-icon ${node.ext}">
                    <i data-lucide="${getFileIconName(node.ext)}" style="width: 13px; height: 13px;"></i>
                </span>
                <span class="stash-node-name">${node.name}</span>
            `;

            row.addEventListener('click', () => {
                activeVirtualFilePath = node.path;
                activeArtifactCode = virtualProjectFiles[node.path].code;
                activeArtifactExt = virtualProjectFiles[node.path].ext;
                
                // 同步更新編輯器
                sandboxCodeEditor.value = activeArtifactCode;
                
                // 同步更新預覽 (如果選取了 index.html)
                if (node.path === 'index.html') {
                    sandboxIframe.srcdoc = prepareSandboxHtml(activeArtifactCode);
                }
                
                // 重新渲染高亮
                renderVirtualFileTree(filterQuery);
            });

            return row;
        } else {
            // 資料夾節點
            const folderContainer = document.createElement('div');
            folderContainer.className = 'stash-tree-node';

            const row = document.createElement('div');
            row.className = 'stash-item-row';
            row.style.paddingLeft = `${depth * 12 + 8}px`;

            row.innerHTML = `
                <span class="stash-node-arrow">
                    <i data-lucide="chevron-down" style="width: 12px; height: 12px;"></i>
                </span>
                <span class="stash-node-icon folder">
                    <i data-lucide="folder-open" style="width: 13px; height: 13px;"></i>
                </span>
                <span class="stash-node-name">${node.name}</span>
            `;

            const childrenBox = document.createElement('div');
            childrenBox.className = 'stash-tree-children';

            let hasVisibleChild = false;
            Object.keys(node.children).forEach(key => {
                const childDOM = buildTreeNodeDOM(node.children[key], depth + 1);
                if (childDOM) {
                    childrenBox.appendChild(childDOM);
                    hasVisibleChild = true;
                }
            });

            // 搜尋時，若無匹配子項目，隱藏該目錄
            if (filterQuery && !hasVisibleChild) {
                return null;
            }

            folderContainer.appendChild(row);
            folderContainer.appendChild(childrenBox);

            // 摺疊/展開事件
            row.addEventListener('click', (e) => {
                e.stopPropagation();
                const children = folderContainer.querySelector('.stash-tree-children');
                const arrow = row.querySelector('.stash-node-arrow');
                const isCollapsed = children.classList.toggle('collapsed');
                
                const iconBox = row.querySelector('.stash-node-icon');
                if (isCollapsed) {
                    arrow.classList.add('collapsed');
                    iconBox.innerHTML = '<i data-lucide="folder" style="width: 13px; height: 13px;"></i>';
                } else {
                    arrow.classList.remove('collapsed');
                    iconBox.innerHTML = '<i data-lucide="folder-open" style="width: 13px; height: 13px;"></i>';
                }
                safeCreateIcons();
            });

            return folderContainer;
        }
    }

    function getFileIconName(ext) {
        switch (ext) {
            case 'html': return 'file-code';
            case 'css': return 'palette';
            case 'js': return 'braces';
            case 'py': return 'file-terminal';
            case 'md': return 'file-text';
            case 'json': return 'braces';
            default: return 'file';
        }
    }

    // 渲染根節點的子節點
    Object.keys(root.children).forEach(key => {
        const childDOM = buildTreeNodeDOM(root.children[key], 0);
        if (childDOM) {
            sandboxStashList.appendChild(childDOM);
        }
    });

    safeCreateIcons();
}

// ==========================================================
// 專案工作區已經改為直接整合編輯器，故移除 DOM 樹渲染器

function initArtifactsControls() {
    btnSandboxClose.addEventListener('click', () => closeInspectorPanel({
        focusTarget: btnSandboxToggle || document.getElementById('rail-artifacts'),
    }));

    if (btnSandboxToggle) {
        btnSandboxToggle.addEventListener('click', () => {
            const isNowActive = artifactsSandboxPanel.classList.toggle('active');
            btnSandboxToggle.classList.toggle('active', isNowActive);
            if (isNowActive) {
                setOutputFloatingPanelOpen(false);
                closeAgentCollaboration(false);
                collapseCompactChatDrawer();
            }
            
            // 防呆引導：如果點擊展開沙盒但目前還沒有代碼
            if (isNowActive && !activeArtifactCode) {
                sandboxTitle.textContent = "尚無專案";
                sandboxCodeEditor.value = "/* 尚未建立專案。\n\n在對話中生成 HTML/CSS/JS 代碼後，專案虛擬目錄樹與程式碼將在此自動載入並支援即時修改。 */";
                // 空狀態頁面依目前主題配色（暖紙 / 深色）
                const isDarkTheme = document.documentElement.getAttribute('data-theme') === 'dark';
                const emptyBg = isDarkTheme ? '#09090d' : '#f6f5f2';
                const emptyText = isDarkTheme ? '#718096' : '#8b877c';
                const emptyMuted = isDarkTheme ? '#a0aec0' : '#6d685c';
                const emptyAccent = isDarkTheme ? '#8b5cf6' : '#5b5bd6';
                sandboxIframe.srcdoc = prepareSandboxHtml(`
                    <!DOCTYPE html>
                    <html>
                    <head>
                        <meta charset="UTF-8">
                        <style>
                            body {
                                background: ${emptyBg};
                                color: ${emptyMuted};
                                font-family: 'Times New Roman', 'Microsoft JhengHei UI', 'Microsoft JhengHei', serif;
                                display: flex;
                                flex-direction: column;
                                align-items: center;
                                justify-content: center;
                                height: 100vh;
                                margin: 0;
                                text-align: center;
                                padding: 20px;
                            }
                            h3 { color: ${emptyAccent}; margin-bottom: 8px; font-weight: 600; }
                            p { font-size: 14px; max-width: 320px; line-height: 1.6; color: ${emptyText}; }
                        </style>
                    </head>
                    <body>
                        <h3>📦 專案沙盒預覽</h3>
                        <p>尚無可展示的網頁原型。<br>請在左側對話中發送需求，例如「寫一個網頁時鐘」，Agent 將自動為您產生專案並在此即時預覽與互動！</p>
                    </body>
                    </html>
                `);
                sandboxStashList.innerHTML = '<div style="color: var(--text-muted); padding: 16px; font-size: 12px; text-align: center;">尚未載入檔案</div>';
            }
        });
    }
    
    // 預覽與工作區雙分頁切換
    tabSandboxPreview.addEventListener('click', () => {
        tabSandboxPreview.classList.add('active');
        tabSandboxWorkspace.classList.remove('active');
        sandboxIframe.classList.add('active');
        sandboxStructureView.classList.remove('active');
    });
    
    tabSandboxWorkspace.addEventListener('click', () => {
        tabSandboxWorkspace.classList.add('active');
        tabSandboxPreview.classList.remove('active');
        sandboxStructureView.classList.add('active');
        sandboxIframe.classList.remove('active');
    });

    // 原始碼即時編輯與雙向熱重載
    sandboxCodeEditor.addEventListener('input', () => {
        clearTimeout(codeEditDebounceTimer);
        codeEditDebounceTimer = setTimeout(() => {
            const currentCode = sandboxCodeEditor.value;
            activeArtifactCode = currentCode;
            
            // 更新目前選取的暫存代碼檔案
            if (virtualProjectFiles[activeVirtualFilePath]) {
                virtualProjectFiles[activeVirtualFilePath].code = currentCode;
            }
            
            // P0-7：任何檔案（html / css / js）修改都刷新合併預覽，不再只限 index.html
            refreshSandboxPreview();

            console.log('[Editor] Hot-reload complete.');
        }, 300);
    });

    // 專案檔案樹模糊篩選器
    sandboxFileFilter.addEventListener('input', () => {
        const filterQuery = sandboxFileFilter.value.trim();
        renderVirtualFileTree(filterQuery);
    });
    
    btnSandboxDownload.addEventListener('click', () => {
        const blob = new Blob([activeArtifactCode], { type: 'text/plain;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `artifact_${Date.now()}.${activeArtifactExt}`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    });

    // ==========================================
    // 雙重拖曳 Resizer 滑動縮放邏輯實作
    // ==========================================

    // 1. 主拖曳：調整整個沙盒的寬度
    sandboxResizerMain.addEventListener('mousedown', (mouseDownEvent) => {
        mouseDownEvent.preventDefault();
        sandboxResizerMain.classList.add('resizing');
        sandboxIframe.style.pointerEvents = 'none'; // 避免 iframe 攔截滑鼠移動

        const handleMouseMoveMain = (e) => {
            // 計算滑鼠到視窗右側的距離作為沙盒的寬度
            let width = window.innerWidth - e.clientX;
            
            // 限制縮放寬度在 25% 到 85% 之間
            const minWidth = window.innerWidth * 0.25;
            const maxWidth = window.innerWidth * 0.85;
            
            if (width < minWidth) width = minWidth;
            if (width > maxWidth) width = maxWidth;
            
            artifactsSandboxPanel.style.width = width + 'px';
        };

        const handleMouseUpMain = () => {
            sandboxResizerMain.classList.remove('resizing');
            sandboxIframe.style.pointerEvents = 'auto';
            window.removeEventListener('mousemove', handleMouseMoveMain);
            window.removeEventListener('mouseup', handleMouseUpMain);
        };

        window.addEventListener('mousemove', handleMouseMoveMain);
        window.addEventListener('mouseup', handleMouseUpMain);
    });

    // 2. 內部拖曳：調整左側目錄樹與右側編輯器的比例
    sandboxResizerInner.addEventListener('mousedown', (mouseDownEvent) => {
        mouseDownEvent.preventDefault();
        sandboxResizerInner.classList.add('resizing');
        sandboxIframe.style.pointerEvents = 'none';

        const sidebarRect = sandboxStashSidebar.getBoundingClientRect();

        const handleMouseMoveInner = (e) => {
            // 計算滑鼠相對於目錄樹左側邊界的距離
            let width = e.clientX - sidebarRect.left;
            
            // 限制寬度在 140px 到 500px 之間
            if (width < 140) width = 140;
            if (width > 500) width = 500;
            
            sandboxStashSidebar.style.width = width + 'px';
            sandboxStashSidebar.style.minWidth = width + 'px';
        };

        const handleMouseUpInner = () => {
            sandboxResizerInner.classList.remove('resizing');
            sandboxIframe.style.pointerEvents = 'auto';
            window.removeEventListener('mousemove', handleMouseMoveInner);
            window.removeEventListener('mouseup', handleMouseUpInner);
        };

        window.addEventListener('mousemove', handleMouseMoveInner);
        window.addEventListener('mouseup', handleMouseUpInner);
    });
}

// ==========================================================
// 第十一階段：設定中心 (Settings Center) 控制模組
// ==========================================================

function formatRuntimeBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes < 0) return '--';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = bytes;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
        value /= 1024;
        index += 1;
    }
    return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

async function runtimeJson(url, options = {}) {
    // 必須用 apiFetch：這個 helper 服務 n8n 狀態、Cursor 狀態、Runtime 健康、
    // 重建索引等 6 個端點，用裸 fetch 會少帶工作階段憑證而全部回 401。
    const response = await apiFetch(url, options);
    const data = await response.json();
    if (!response.ok) {
        const detail = data.detail?.message || data.detail?.content || data.detail || `HTTP ${response.status}`;
        throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    return data;
}

function updateRuntimeSessionLabel() {
    if (!runtimeExportSession || !btnRuntimeExport) return;
    runtimeExportSession.textContent = currentSessionId || '尚未選擇對話';
    btnRuntimeExport.disabled = !currentSessionId;
}

function renderRuntimeHealth(data) {
    if (!runtimeHealthState || !runtimeHealthMetrics) return;
    runtimeHealthState.textContent = data.healthy ? '正常' : '需要檢查';
    runtimeHealthState.className = `runtime-health-state ${data.healthy ? 'ok' : 'warn'}`;
    runtimeHealthMetrics.replaceChildren();
    const metrics = [
        ['資料庫', data.database_integrity],
        ['對話', `${data.counts.sessions} / ${data.conversation_folders} folders`],
        ['訊息', data.counts.messages],
        ['Runs', data.counts.runs],
        ['自動化', `${data.counts.automation_runs || 0} Runs`],
        ['待批准', data.counts.pending_approvals || 0],
        ['孤立資料', data.orphan_messages + data.orphan_runs],
        ['可用空間', formatRuntimeBytes(data.disk.free_bytes)]
    ];
    metrics.forEach(([label, value]) => {
        const item = document.createElement('div');
        item.className = 'runtime-metric';
        const labelElement = document.createElement('div');
        labelElement.className = 'runtime-metric-label';
        labelElement.textContent = label;
        const valueElement = document.createElement('div');
        valueElement.className = 'runtime-metric-value';
        valueElement.textContent = String(value);
        item.append(labelElement, valueElement);
        runtimeHealthMetrics.appendChild(item);
    });
}

function renderUsageLedger(data) {
    if (!runtimeUsageState || !runtimeUsageMetrics) return;
    const totals = data.totals || {};
    const currencies = data.cost_by_currency || {};
    const costText = Object.entries(currencies).length
        ? Object.entries(currencies).map(([currency, value]) => `${currency} ${Number(value).toFixed(4)}`).join(' · ')
        : '本機／未設定費率';
    runtimeUsageState.textContent = `${Number(totals.runs || 0)} 次執行`;
    runtimeUsageState.className = 'runtime-health-state ok';
    runtimeUsageMetrics.replaceChildren(
        createMetric('總 Token', Number(totals.total_tokens || 0).toLocaleString()),
        createMetric('輸入', Number(totals.prompt_tokens || 0).toLocaleString()),
        createMetric('輸出', Number(totals.completion_tokens || 0).toLocaleString()),
        createMetric('估算成本', costText)
    );
}

async function checkUsageLedger() {
    if (!runtimeUsageState || !runtimeUsageMetrics) return;
    runtimeUsageState.textContent = '載入中...';
    try {
        renderUsageLedger(await removedBasicFeature('Usage ledger'));
    } catch (error) {
        runtimeUsageState.textContent = `載入失敗：${error.message}`;
        runtimeUsageState.className = 'runtime-health-state error';
    }
}

async function checkRuntimeHealth() {
    if (!btnRuntimeHealth) return;
    btnRuntimeHealth.disabled = true;
    runtimeHealthState.textContent = '檢查中...';
    runtimeHealthState.className = 'runtime-health-state';
    try {
        renderRuntimeHealth(await removedBasicFeature('Legacy runtime health'));
    } catch (error) {
        runtimeHealthState.textContent = `檢查失敗：${error.message}`;
        runtimeHealthState.className = 'runtime-health-state error';
        showToast(`Runtime 健康檢查失敗：${error.message}`, 'error');
    } finally {
        btnRuntimeHealth.disabled = false;
    }
}

async function exportCurrentConversation() {
    if (!currentSessionId) {
        showToast('請先選擇要匯出的對話。', 'info');
        return;
    }
    const progressId = `conversation-export-${currentSessionId}`;
    btnRuntimeExport.disabled = true;
    updateTaskProgress(progressId, { label: '匯出目前對話', detail: '正在封裝對話與附件', mode: 'indeterminate', value: null });
    try {
        const response = await apiFetch(`${API_BASE}/api/sessions/${encodeURIComponent(currentSessionId)}/export.zip`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const url = URL.createObjectURL(await response.blob());
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = `conversation-${currentSessionId}.zip`;
        anchor.click();
        URL.revokeObjectURL(url);
        showToast('對話匯出完成。', 'success');
        finishTaskProgress(progressId, 'completed', 'ZIP 匯出完成');
    } catch (error) {
        showToast(`對話匯出失敗：${error.message}`, 'error');
        finishTaskProgress(progressId, 'failed', error.message);
    } finally {
        btnRuntimeExport.disabled = false;
    }
}

function renderRebuildReport(data) {
    const status = data.valid ? '驗證通過' : '驗證失敗';
    runtimeRebuildReport.textContent = [
        status,
        `Sessions: ${data.sessions}`,
        `Messages: ${data.messages}`,
        `Runs: ${data.runs}`,
        `SAFIR: ${data.safir_analyses}`,
        `Errors: ${data.errors?.length || 0}`,
        data.applied ? `Backup: ${data.backup_path}` : '目前僅為預覽'
    ].join('\n');
    runtimeRebuildReport.classList.toggle('error', !data.valid);
}

async function runRuntimeRebuild(apply) {
    const button = apply ? btnRuntimeRebuildApply : btnRuntimeRebuildPreview;
    const progressId = apply ? 'runtime-rebuild-apply' : 'runtime-rebuild-preview';
    button.disabled = true;
    runtimeRebuildReport.textContent = apply ? '正在建立備份並套用重建...' : '正在建立重建預覽...';
    updateTaskProgress(progressId, {
        label: apply ? '重建 Runtime 索引' : '預覽 Runtime 索引重建',
        detail: apply ? '正在建立備份、掃描資料夾並重建索引' : '正在掃描資料夾並驗證重建結果',
        mode: 'indeterminate',
        value: null
    });
    try {
        const data = await removedBasicFeature('Legacy runtime rebuild', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ apply, confirm: apply ? runtimeRebuildConfirm.value.trim() : null })
        });
        renderRebuildReport(data);
        showToast(apply ? 'Runtime 索引重建完成。' : '重建預覽已完成。', 'success');
        finishTaskProgress(progressId, 'completed', apply ? '備份與索引重建完成' : '重建預覽完成');
        if (apply) {
            runtimeRebuildConfirm.value = '';
            btnRuntimeRebuildApply.disabled = true;
            await checkRuntimeHealth();
            await loadSessions();
        }
    } catch (error) {
        runtimeRebuildReport.textContent = `重建失敗：${error.message}`;
        runtimeRebuildReport.classList.add('error');
        showToast(`Runtime 索引重建失敗：${error.message}`, 'error');
        finishTaskProgress(progressId, 'failed', error.message);
    } finally {
        if (!apply) button.disabled = false;
        else btnRuntimeRebuildApply.disabled = runtimeRebuildConfirm.value.trim() !== 'REBUILD';
    }
}

function initSettingsControls() {
    const sizeStorageKey = 'settings-modal-size';
    const defaultSize = { width: 900, height: 650 };
    const getSizeBounds = () => {
        const maxWidth = Math.max(320, window.innerWidth - 32);
        const maxHeight = Math.max(320, window.innerHeight - 32);
        return {
            minWidth: Math.min(620, maxWidth),
            minHeight: Math.min(420, maxHeight),
            maxWidth,
            maxHeight
        };
    };
    const clampSize = size => {
        const bounds = getSizeBounds();
        const width = Number(size?.width);
        const height = Number(size?.height);
        return {
            width: Math.round(Math.min(bounds.maxWidth, Math.max(bounds.minWidth, Number.isFinite(width) ? width : defaultSize.width))),
            height: Math.round(Math.min(bounds.maxHeight, Math.max(bounds.minHeight, Number.isFinite(height) ? height : defaultSize.height)))
        };
    };
    const readSavedSize = () => {
        try {
            return clampSize(JSON.parse(localStorage.getItem(sizeStorageKey) || 'null') || defaultSize);
        } catch (error) {
            return clampSize(defaultSize);
        }
    };
    const hasSavedLocalSize = () => {
        try { return localStorage.getItem(sizeStorageKey) !== null; } catch (error) { return false; }
    };
    const applySize = size => {
        if (!settingsModalBox || window.matchMedia('(max-width: 640px)').matches) {
            settingsModalBox?.style.removeProperty('width');
            settingsModalBox?.style.removeProperty('height');
            return clampSize(size || defaultSize);
        }
        const next = clampSize(size);
        settingsModalBox.style.setProperty('width', `${next.width}px`, 'important');
        settingsModalBox.style.setProperty('height', `${next.height}px`);
        return next;
    };
    const saveSize = size => {
        const next = clampSize(size);
        try { localStorage.setItem(sizeStorageKey, JSON.stringify(next)); } catch (error) {}
        apiFetch(`${API_BASE}/api/settings/ui-state`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            keepalive: true,
            body: JSON.stringify({
                settings_modal_width: next.width,
                settings_modal_height: next.height
            })
        }).catch(error => console.warn('Failed to persist settings modal size:', error));
        return next;
    };
    let currentSettingsSize = applySize(readSavedSize());

    if (settingsResizeHandle && settingsModalBox) {
        settingsResizeHandle.addEventListener('pointerdown', event => {
            if (event.button !== 0 || window.matchMedia('(max-width: 640px)').matches) return;
            event.preventDefault();
            event.stopPropagation();
            const startRect = settingsModalBox.getBoundingClientRect();
            const startX = event.clientX;
            const startY = event.clientY;
            settingsModalBox.classList.add('is-resizing');
            settingsResizeHandle.setPointerCapture?.(event.pointerId);

            const onMove = moveEvent => {
                currentSettingsSize = applySize({
                    width: startRect.width + moveEvent.clientX - startX,
                    height: startRect.height + moveEvent.clientY - startY
                });
            };
            const onEnd = endEvent => {
                settingsResizeHandle.removeEventListener('pointermove', onMove);
                settingsResizeHandle.removeEventListener('pointerup', onEnd);
                settingsResizeHandle.removeEventListener('pointercancel', onEnd);
                settingsResizeHandle.releasePointerCapture?.(endEvent.pointerId);
                settingsModalBox.classList.remove('is-resizing');
                currentSettingsSize = saveSize(currentSettingsSize);
            };
            settingsResizeHandle.addEventListener('pointermove', onMove);
            settingsResizeHandle.addEventListener('pointerup', onEnd);
            settingsResizeHandle.addEventListener('pointercancel', onEnd);
        });

        settingsResizeHandle.addEventListener('keydown', event => {
            const step = event.shiftKey ? 40 : 10;
            const delta = {
                ArrowLeft: { width: -step, height: 0 },
                ArrowRight: { width: step, height: 0 },
                ArrowUp: { width: 0, height: -step },
                ArrowDown: { width: 0, height: step }
            }[event.key];
            if (!delta) return;
            event.preventDefault();
            currentSettingsSize = applySize({
                width: currentSettingsSize.width + delta.width,
                height: currentSettingsSize.height + delta.height
            });
            currentSettingsSize = saveSize(currentSettingsSize);
        });
    }

    window.addEventListener('resize', () => {
        if (settingsModal?.classList.contains('active')) {
            currentSettingsSize = applySize(currentSettingsSize);
        }
    });

    // 1. 開關 Modal 邏輯
    btnSettingsTrigger.addEventListener('click', async () => {
        await loadSettingsFromServer();
        const serverSize = {
            width: Number(currentSettings.settings_modal_width),
            height: Number(currentSettings.settings_modal_height)
        };
        const hasServerSize = Number.isFinite(serverSize.width) && serverSize.width > 0
            && Number.isFinite(serverSize.height) && serverSize.height > 0;
        const hadLocalSize = hasSavedLocalSize();
        const localSize = readSavedSize();
        const serverUsesDefault = serverSize.width === defaultSize.width && serverSize.height === defaultSize.height;
        const preferredSize = hadLocalSize && serverUsesDefault ? localSize : (hasServerSize ? serverSize : localSize);
        currentSettingsSize = applySize(preferredSize);
        try { localStorage.setItem(sizeStorageKey, JSON.stringify(currentSettingsSize)); } catch (error) {}
        if (hadLocalSize && serverUsesDefault
            && (localSize.width !== defaultSize.width || localSize.height !== defaultSize.height)) {
            currentSettingsSize = saveSize(currentSettingsSize);
        }
        updateRuntimeSessionLabel();
        settingsModal.classList.add('active');
    });

    btnSettingsClose.addEventListener('click', () => {
        settingsModal.classList.remove('active');
    });

    btnSettingsCancel.addEventListener('click', () => {
        settingsModal.classList.remove('active');
    });

    // 點擊 Modal 外部亦可關閉
    settingsModal.addEventListener('click', (e) => {
        if (e.target === settingsModal) {
            settingsModal.classList.remove('active');
        }
    });

    // 2. 分頁切換邏輯
    const tabBtns = settingsModal.querySelectorAll('.settings-tab-btn');
    const panes = settingsModal.querySelectorAll('.settings-pane');

    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const targetId = btn.getAttribute('data-target');
            
            tabBtns.forEach(b => b.classList.remove('active'));
            panes.forEach(p => p.classList.remove('active'));
            
            btn.classList.add('active');
            document.getElementById(targetId).classList.add('active');
            if (targetId === 'tab-settings-runtime') {
                updateRuntimeSessionLabel();
                checkRuntimeHealth();
                checkUsageLedger();
            }
            if (targetId === 'tab-settings-integrations') {
                checkN8nStatus();
                checkCursorStatus();
            }
            if (targetId === 'tab-settings-agent') refreshSafirStatus();
        });
    });

    btnRuntimeHealth.addEventListener('click', checkRuntimeHealth);
    btnRuntimeExport.addEventListener('click', exportCurrentConversation);
    btnRuntimeRebuildPreview.addEventListener('click', () => runRuntimeRebuild(false));
    btnRuntimeRebuildApply.addEventListener('click', () => runRuntimeRebuild(true));
    runtimeRebuildConfirm.addEventListener('input', () => {
        btnRuntimeRebuildApply.disabled = runtimeRebuildConfirm.value.trim() !== 'REBUILD';
    });
    btnN8nStatus.addEventListener('click', checkN8nStatus);
    btnCursorStatus.addEventListener('click', checkCursorStatus);
    btnMcpStatus?.addEventListener('click', checkMcpStatus);
    n8nInstallOptions.addEventListener('click', event => {
        const button = event.target.closest('[data-n8n-install]');
        if (button) installN8n(button.dataset.n8nInstall);
    });

    // 3. 滑桿數值即時更新
    settingRagK.addEventListener('input', () => {
        valRagK.textContent = settingRagK.value;
    });

    settingRagThreshold.addEventListener('input', () => {
        valRagThreshold.textContent = parseFloat(settingRagThreshold.value).toFixed(2);
    });
    settingSubagentEnabled?.addEventListener('change', syncSubagentSettingsEnabled);
    settingSubagentEnabled?.addEventListener('change', () => updateSubagentResourcePlan());
    [settingSubagentPlannerModel, settingSubagentExplorerModel, settingSubagentImplementerModel, settingSubagentCriticModel, settingSubagentCloudRouting, settingSubagentMaxParallel]
        .filter(Boolean)
        .forEach(control => control.addEventListener('change', () => updateSubagentResourcePlan(true)));
    modelProviderList?.addEventListener('click', event => {
        const copyButton = event.target.closest('[data-copy-provider-endpoint]');
        if (copyButton) {
            copyModelProviderEndpoint(copyButton.closest('[data-provider-card]'));
            return;
        }
        const toolTestButton = event.target.closest('[data-test-provider-tools]');
        if (toolTestButton) {
            testProviderToolCapability(toolTestButton.closest('[data-provider-card]'));
            return;
        }
        const modelTestButton = event.target.closest('[data-test-provider-model]');
        if (modelTestButton) {
            testProviderModelCard(modelTestButton.closest('[data-provider-card]'));
            return;
        }
        const testButton = event.target.closest('[data-test-provider]');
        if (testButton) {
            testModelProviderCard(testButton.closest('[data-provider-card]'));
            return;
        }
        const button = event.target.closest('[data-remove-provider]');
        if (!button) return;
        const card = button.closest('[data-provider-card]');
        const originalId = card?.dataset.originalProviderId;
        if (originalId && modelProviderSecretStatus[originalId]?.configured) {
            removedProviderSecrets.add(originalId);
        }
        card?.remove();
        if (!modelProviderList.querySelector('[data-provider-card]')) renderModelProviders([]);
    });
    modelProviderList?.addEventListener('change', event => {
        const card = event.target.closest('[data-provider-card]');
        const providerType = event.target.closest('[data-provider-field="provider_type"]');
        if (providerType) {
            invalidateProviderToolAttestation(card);
            applyProviderType(card, providerType.value);
            return;
        }
        const selectedModel = event.target.closest('[data-provider-field="selected_model"]');
        if (selectedModel) {
            invalidateProviderToolAttestation(card);
            syncProviderModelDefaults(card);
            return;
        }
        const modelKind = event.target.closest('[data-provider-field="model_kind"]');
        if (modelKind) {
            invalidateProviderToolAttestation(card);
            syncProviderCapabilityDefaults(card);
            return;
        }
        if (event.target.closest('[data-provider-field="supports_tools"]')) {
            invalidateProviderToolAttestation(card);
            syncProviderCapabilityDefaults(card);
            return;
        }
        if (event.target.closest('[data-provider-field="base_url"], [data-provider-field="source_url"]')) {
            invalidateProviderToolAttestation(card);
        }
    });
    safirModelStatus?.addEventListener('click', async event => {
        if (!event.target.closest('[data-safir-evaluate]')) return;
        const button = event.target.closest('[data-safir-evaluate]');
        button.disabled = true;
        safirModelStatus.className = 'safir-model-status is-loading';
        safirModelStatus.textContent = '正在執行中文 SAFIR 評測並校正門檻…';
        try {
            const response = await removedBasicFeature('SAFIR evaluation');
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.detail?.message || `HTTP ${response.status}`);
            showToast(`SAFIR 評測完成：NLI ${Math.round((data.calibration?.nli_accuracy || 0) * 100)}%`, data.calibration?.all_passed ? 'success' : 'warning');
        } catch (error) {
            showToast(`SAFIR 評測失敗：${error.message}`, 'error');
        } finally {
            refreshSafirStatus();
        }
    });

    // 4. 儲存設定邏輯
    btnSettingsSave.addEventListener('click', async () => {
        await updateSubagentResourcePlan(true);
        let mcpServers = [];
        try {
            const editedMcpServers = JSON.parse(settingMcpServers?.value?.trim() || '[]');
            if (!Array.isArray(editedMcpServers)) throw new Error('最外層必須是陣列');
            mcpServers = collectMcpServerSettings(editedMcpServers);
        } catch (error) {
            showToast(`MCP JSON 格式錯誤：${error.message}`, 'error');
            return;
        }
        const payload = {
            ollama_url: settingOllamaUrl.value.trim() || 'http://127.0.0.1:11434',
            model_provider: 'ollama',
            model_providers: collectModelProviders(),
            model_input_cost_per_million: parseFloat(settingModelInputCost.value) || 0,
            model_output_cost_per_million: parseFloat(settingModelOutputCost.value) || 0,
            model_cost_currency: settingModelCostCurrency.value.trim().toUpperCase() || 'USD',
            n8n_url: settingN8nUrl.value.trim() || 'http://127.0.0.1:5678',
            default_chat_model: settingChatModel.value.trim() || 'gemma4-hermes:latest',
            default_vision_model: settingVisionModel.value.trim() || 'gemma4-hermes:latest',
            rag_k: parseInt(settingRagK.value),
            rag_rerank_threshold: parseFloat(settingRagThreshold.value),
            chunk_size: parseInt(settingChunkSize.value) || 600,
            chunk_overlap: parseInt(settingChunkOverlap.value) || 120,
            browser_headless: !settingBrowserHeadful.checked,
            network_proxy: settingNetworkProxy.value.trim(),
            tts_auto_play: false,
            tts_rate: 1.0,
            agent_detailed_progress: BASIC_CHAT_MODE ? false : settingAgentDetailedProgress.checked,
            skills_enabled: BASIC_CHAT_MODE ? false : settingSkillsEnabled.checked,
            agent_max_tool_calls: parseInt(settingAgentMaxToolCalls.value) || 8,
            agent_max_repair_rounds: BASIC_CHAT_MODE ? 0 : (Number.isFinite(parseInt(settingAgentMaxRepairRounds.value)) ? parseInt(settingAgentMaxRepairRounds.value) : 3),
            agent_auto_validate: BASIC_CHAT_MODE ? false : settingAgentAutoValidate.checked,
            agent_allow_workspace_write: BASIC_CHAT_MODE ? false : settingAgentAllowWorkspaceWrite.checked,
            agent_final_report_detail: settingAgentFinalReportDetail.value || 'standard',
            settings_modal_width: currentSettingsSize.width,
            settings_modal_height: currentSettingsSize.height,
            safir_semantic_entailment_enabled: BASIC_CHAT_MODE ? false : settingSafirNliEnabled.checked,
            safir_max_search_rounds: BASIC_CHAT_MODE ? 0 : (parseInt(settingSafirSearchRounds.value) || 0),
            safir_max_external_sources: BASIC_CHAT_MODE ? 2 : (parseInt(settingSafirExternalSources.value) || 2),
            safir_retrieval_timeout_seconds: BASIC_CHAT_MODE ? 20 : (parseInt(settingSafirTimeout.value) || 20),
            safir_critic_max_output_tokens: BASIC_CHAT_MODE ? 512 : (parseInt(settingSafirCriticTokens.value) || 512),
            subagent_enabled: BASIC_CHAT_MODE ? false : settingSubagentEnabled.checked, subagent_allow_planner_cloud_routing: BASIC_CHAT_MODE ? false : !!settingSubagentCloudRouting?.checked,
            subagent_max_parallel: parseInt(settingSubagentMaxParallel.value) || 1,
            subagent_models: BASIC_CHAT_MODE ? {} : {
                planner: settingSubagentPlannerModel.value || '',
                explorer: settingSubagentExplorerModel.value || '',
                implementer: settingSubagentImplementerModel.value || '',
                critic: settingSubagentCriticModel.value || ''
            },
            mcp_servers: mcpServers,
            agent_display_names: Object.fromEntries(Object.entries(agentDisplayNameInputs).map(([role, input]) => [
                role,
                normalizeAgentDisplayName(input?.value, DEFAULT_AGENT_DISPLAY_NAMES[role])
            ]))
        };

        try {
            if (!BASIC_CHAT_MODE) await validateExternalCollaborationModels(payload.subagent_models);
            const res = await apiFetch(`${API_BASE}/api/settings`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (data.success) {
                await saveModelProviderSecrets();
                // P1-8：僅在 Ollama 端點或預設模型變更時刷新模型下拉選單
                const oldOllamaUrl = currentSettings.ollama_url;
                const oldChatModel = currentSettings.default_chat_model;
                if (
                    payload.ollama_url !== oldOllamaUrl
                    || payload.default_chat_model !== oldChatModel
                    || JSON.stringify(payload.model_providers) !== JSON.stringify(currentSettings.model_providers || [])
                ) {
                    await loadModels();
                }
                currentSettings = { ...currentSettings, ...payload };
                applyAgentDisplayNames(payload.agent_display_names);
                renderAgentCollaboration();

                showToast('設定儲存成功，且已即時熱加載生效！');
                settingsModal.classList.remove('active');
            } else {
                throw new Error('後端回傳儲存失敗');
            }
        } catch (err) {
            showToast(`儲存設定失敗: ${err.message}`);
        }
    });

    // 5. 危險區域：清空 RAG 向量資料庫
    btnClearRagDb.addEventListener('click', async () => {
        if (confirm('警告：此動作將清空所有已向量化的本地文件索引。確認要清空整個 RAG 數據庫嗎？此動作無法還原！')) {
            try {
                btnClearRagDb.disabled = true;
                btnClearRagDb.innerHTML = '<i class="status-spinner" data-lucide="loader"></i>正在清空...';
                safeCreateIcons();

                await clearRagIndex(); // P1-9：統一入口（含刷新文件清單與狀態）
                showToast('知識庫索引已成功清空！');
            } catch (err) {
                showToast(`清空 RAG 數據庫失敗: ${err.message}`);
            } finally {
                btnClearRagDb.disabled = false;
                btnClearRagDb.innerHTML = '<i data-lucide="trash-2"></i>清空 RAG 向量數據庫';
                safeCreateIcons();
            }
        }
    });
    
    // 初始化時即時同步設定
    loadSettingsFromServer();
}

// 從後端拉取設定更新 UI
let currentSettings = {}; // P1-8：保存後端設定快照，供儲存時比對變更

async function loadSettingsFromServer() {
    try {
        const res = await apiFetch(`${API_BASE}/api/settings`);
        const data = await res.json();
        currentSettings = data || {};

        settingOllamaUrl.value = data.ollama_url || '';
        await loadModelProviderSettings(data.model_providers || []);
        settingModelInputCost.value = data.model_input_cost_per_million ?? 0;
        settingModelOutputCost.value = data.model_output_cost_per_million ?? 0;
        settingModelCostCurrency.value = data.model_cost_currency || 'USD';
        settingN8nUrl.value = data.n8n_url || 'http://127.0.0.1:5678';
        if (settingMcpServers) {
            settingMcpServers.value = JSON.stringify(
                editableMcpServerSettings(data.mcp_servers || []),
                null,
                2
            );
        }
        settingChatModel.value = data.default_chat_model || '';
        settingVisionModel.value = data.default_vision_model || '';
        
        settingRagK.value = data.rag_k || 4;
        valRagK.textContent = settingRagK.value;
        
        settingRagThreshold.value = data.rag_rerank_threshold || 0.2;
        valRagThreshold.textContent = parseFloat(settingRagThreshold.value).toFixed(2);
        
        settingChunkSize.value = data.chunk_size || 600;
        settingChunkOverlap.value = data.chunk_overlap || 120;
        
        settingBrowserHeadful.checked = !data.browser_headless;
        settingNetworkProxy.value = data.network_proxy || '';
        settingAgentDetailedProgress.checked = data.agent_detailed_progress !== false;
        settingSkillsEnabled.checked = data.skills_enabled !== false;
        settingAgentMaxToolCalls.value = data.agent_max_tool_calls ?? 8;
        settingAgentMaxRepairRounds.value = data.agent_max_repair_rounds ?? 3;
        settingAgentAutoValidate.checked = data.agent_auto_validate !== false;
        settingAgentAllowWorkspaceWrite.checked = data.agent_allow_workspace_write !== false;
        settingAgentFinalReportDetail.value = data.agent_final_report_detail || 'standard';
        if (!BASIC_CHAT_MODE) {
            settingSafirNliEnabled.checked = data.safir_semantic_entailment_enabled !== false;
            settingSafirSearchRounds.value = data.safir_max_search_rounds ?? 1;
            settingSafirExternalSources.value = data.safir_max_external_sources ?? 2;
            settingSafirTimeout.value = data.safir_retrieval_timeout_seconds ?? 20;
            settingSafirCriticTokens.value = data.safir_critic_max_output_tokens ?? 512;
        }
        settingSubagentEnabled.checked = data.subagent_enabled !== false; settingSubagentCloudRouting.checked = !!data.subagent_allow_planner_cloud_routing;
        settingSubagentMaxParallel.value = String(data.subagent_max_parallel || 1);
        applyAgentDisplayNames(data.agent_display_names || {});
        Object.entries(agentDisplayNameInputs).forEach(([role, input]) => {
            if (input) input.value = agentDisplayNames[role];
        });
        if (BASIC_CHAT_MODE) applyBasicChatSettingsUi();
        else { await loadSubagentModelOptions(data.subagent_models || {}); refreshSafirStatus(); }
        if (data.default_chat_model && [...modelSelect.options].some(option => option.value === data.default_chat_model)) {
            modelSelect.value = data.default_chat_model;
            activeModelName.textContent = data.default_chat_model;
            sendBtn.disabled = false;
            updateWelcomeDashboard();
        }
        renderAgentCollaboration();
    } catch (err) {
        console.error('Failed to load settings from server:', err);
    }
}

async function refreshSafirStatus() {
    if (!safirModelStatus) return;
    safirModelStatus.className = 'safir-model-status is-loading';
    safirModelStatus.innerHTML = '<span class="status-spinner"></span><span>正在檢查 NLI 模型與來源快取…</span>';
    try {
        const response = await removedBasicFeature('SAFIR status');
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.detail?.message || `HTTP ${response.status}`);
        const nli = data.nli || {};
        const cache = data.source_cache || {};
        const calibration = data.calibration || {};
        const state = !nli.enabled ? '停用' : nli.available ? '可用' : '不可用';
        safirModelStatus.className = `safir-model-status ${nli.available ? 'is-ready' : nli.enabled ? 'is-error' : 'is-disabled'}`;
        const calibrationText = calibration.evaluated_at
            ? `校正 NLI ${Math.round((calibration.nli_accuracy || 0) * 100)}% · E ${calibration.entailment_threshold} / C ${calibration.contradiction_threshold} · 來源 ≥ ${calibration.source_reliability_threshold ?? '--'} · 信心 ≥ ${calibration.confidence_threshold ?? '--'}`
            : '尚未執行中文評測';
        safirModelStatus.innerHTML = `<strong>NLI ${escapeHtml(state)}</strong><span>${escapeHtml(nli.model || '--')}</span><span>來源快取 ${cache.fresh_count || 0}/${cache.source_count || 0} · ${cache.version_count || 0} 版本</span><span>${escapeHtml(calibrationText)}</span><button type="button" class="btn btn-secondary safir-evaluate-btn" data-safir-evaluate>重新評測與校正</button>${nli.error ? `<small>${escapeHtml(nli.error)}</small>` : ''}`;
    } catch (error) {
        safirModelStatus.className = 'safir-model-status is-error';
        safirModelStatus.textContent = `無法取得 SAFIR 狀態：${error.message}`;
    }
}

/* ==========================================================================
   WORKBENCH SHELL 模組（Fable5 UX 執行文件）
   Top Bar chips / Icon Rail / Drawer / Inspector / Wizard / Model Manager /
   Knowledge Center / Command Palette / Toast / Metrics
   ========================================================================== */

// ---- 全域狀態 ----
let chatAbort = null;                 // 生成中止控制器（送出鈕 = 停止）
let currentChatRunId = null;          // 後端 Run ID，用於真正取消 Ollama 工作
const runRetryInputs = new Map();      // 僅供同一頁面的「重新執行本輪」恢復正式輸入
let runHistory = [];                  // Agent Run 紀錄（Inspector Run tab）
let sseLogBuffer = [];                // 原始 SSE 事件（Inspector Logs tab）
let lastMetrics = null;               // 最近一次生成指標
let latestSafirAnalysis = null;       // 最近一輪 SAFIR 語義驗證結果
const CTX_WINDOW_TOKENS = 32 * 1024;  // 前端估算用 context window（無後端回報時的預設）

// ---- Toast（P13：取代 alert）----
function showToast(message, type = 'info', actions = null) {
    const container = document.getElementById('toast-container');
    if (!container) { console.log('[Toast]', message); return; }
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = String(message);
    if (Array.isArray(actions) && actions.length) {
        const row = document.createElement('div');
        row.className = 'toast-actions';
        actions.forEach(a => {
            const b = document.createElement('button');
            b.textContent = a.label;
            b.addEventListener('click', () => { try { a.onClick(); } finally { el.remove(); } });
            row.appendChild(b);
        });
        el.appendChild(row);
    }
    container.appendChild(el);
    setTimeout(() => el.remove(), actions ? 12000 : 5000);
    while (container.children.length > 4) container.firstChild.remove();
}

// 連線失敗 Recovery Card（P13：錯誤必須包含下一步）
function renderConnectionErrorCard() {
    return `
        <div>
            <div style="font-weight:600; color: var(--danger-color); margin-bottom:6px;">無法連線至本地 LLM 後端服務</div>
            <div style="font-size:12.5px; color: var(--text-light); line-height:1.7;">可能原因：FastAPI 後端或 Ollama 尚未啟動。</div>
            <div class="toast-actions" style="margin-top:10px; display:flex; gap:8px;">
                <button data-recovery-action="reload" style="border:1px solid var(--panel-border); background:var(--ink-03); color:var(--text-primary); font-family:inherit; font-size:12px; padding:5px 10px; border-radius:7px; cursor:pointer;">重新檢查</button>
                <button data-recovery-action="settings" style="border:1px solid var(--panel-border); background:var(--ink-03); color:var(--text-primary); font-family:inherit; font-size:12px; padding:5px 10px; border-radius:7px; cursor:pointer;">開啟設定</button>
            </div>
        </div>`;
}

// ---- 生成狀態 UI（送出鈕 ↔ 停止鈕、genstate 列）----
function setGeneratingUI(on, stopping = false) {
    const genstate = document.getElementById('composer-genstate');
    if (on) {
        sendBtn.type = 'button';
        sendBtn.disabled = stopping;
        sendBtn.classList.toggle('is-stop', !stopping);
        sendBtn.classList.toggle('is-stopping', stopping);
        sendBtn.dataset.state = stopping ? 'stopping' : 'stop';
        sendBtn.innerHTML = '<i data-lucide="square"></i>';
        sendBtn.title = stopping ? '正在停止' : '停止生成';
        sendBtn.setAttribute('aria-label', stopping ? '正在停止生成' : '停止生成');
        if (genstate) genstate.textContent = stopping ? '正在停止回答...' : '正在生成回答...';
    } else {
        isCancellingGeneration = false;
        sendBtn.type = 'submit';
        sendBtn.disabled = !modelSelect.value;
        sendBtn.classList.remove('is-stop', 'is-stopping');
        sendBtn.dataset.state = 'send';
        sendBtn.innerHTML = '<i data-lucide="send"></i>';
        sendBtn.title = '送出';
        sendBtn.setAttribute('aria-label', '送出訊息');
        if (genstate) genstate.textContent = '';
    }
    safeCreateIcons();
}

function updateGenState(tokens, runStart, firstTokenAt) {
    const genstate = document.getElementById('composer-genstate');
    if (!genstate || !firstTokenAt) return;
    const secs = (performance.now() - firstTokenAt) / 1000;
    const tokps = secs > 0.3 ? Math.round(tokens / secs) : 0;
    if (tokps > 0) {
        genstate.textContent = `正在生成回答... ${tokps} tok/s`;
        const chip = document.getElementById('chip-speed-text');
        if (chip) chip.textContent = `${tokps} tok/s`;
    }
}

function computeRunMetrics(runStart, firstTokenAt, tokens) {
    const now = performance.now();
    const elapsed = (now - runStart) / 1000;
    const ttft = firstTokenAt ? (firstTokenAt - runStart) / 1000 : null;
    const genSecs = firstTokenAt ? (now - firstTokenAt) / 1000 : elapsed;
    const tokps = genSecs > 0.3 && tokens > 0 ? Math.round(tokens / genSecs) : null;
    lastMetrics = { elapsed, ttft, tokps, tokens };
    const chip = document.getElementById('chip-speed-text');
    if (chip && tokps) chip.textContent = `${tokps} tok/s`;
    return lastMetrics;
}

function recordRun(run) {
    runHistory.push({ time: new Date().toLocaleTimeString('zh-TW', { hour12: false }), ...run });
    if (runHistory.length > 20) runHistory.shift();
    renderRunPane();
}

function pushSseLog(eventType, dataStr) {
    if (eventType === 'token') return; // token 太多，Logs 只留結構事件
    sseLogBuffer.push(`[${new Date().toLocaleTimeString('zh-TW', { hour12: false })}] ${eventType}  ${String(dataStr).slice(0, 300)}`);
    if (sseLogBuffer.length > 200) sseLogBuffer.shift();
}

// ---- 上下文估算（chip-ctx / Context tab）----
function estimateCtxTokens() {
    let chars = 0;
    conversationState.forEach(m => { chars += (m.content || '').length; });
    chars += (temporaryContextText || '').length;
    return Math.round(chars / 3);
}
function updateCtxChip() {
    const used = estimateCtxTokens();
    const el = document.getElementById('chip-ctx-text');
    if (el) el.textContent = `${(used / 1000).toFixed(1)}k / ${CTX_WINDOW_TOKENS / 1024}k ctx`;
}

// ---- Composer 模式（P7.2）----
function updateRagChip() {
    const chip = document.getElementById('chip-rag');
    const txt = document.getElementById('chip-rag-text');
    if (!chip || !txt) return;
    if (BASIC_CHAT_MODE) return renderBasicChatModeChip(chip, txt);
    if (ragToggle.checked) {
        txt.textContent = 'RAG ON';
        chip.classList.add('chip-ok'); chip.classList.remove('chip-warn');
    } else {
        txt.textContent = '一般對話';
        chip.classList.remove('chip-ok');
    }
    const dsRag = document.getElementById('ds-rag');
    if (dsRag) dsRag.textContent = ragToggle.checked ? '已啟用' : '關閉（一般對話）';
}
function updateDocsChip() {
    const el = document.getElementById('chip-docs-text');
    if (el) el.textContent = kbStatus.index_status === 'ready'
        ? `${kbStatus.document_count} docs · ${kbStatus.chunk_count} chunks`
        : '0 docs';
    const dsKb = document.getElementById('ds-kb');
    if (dsKb) dsKb.textContent = kbStatus.index_status === 'ready'
        ? `${kbStatus.document_count} 文件 · ${kbStatus.chunk_count} chunks`
        : '空白（尚未匯入文件）';
}

// ---- Start Dashboard（P6）----
function updateWelcomeDashboard() {
    const dsModel = document.getElementById('ds-model');
    const dsBackend = document.getElementById('ds-backend');
    const ctaPrimary = document.getElementById('cta-primary');
    const ctaSecondary = document.getElementById('cta-secondary');
    if (!dsModel || !ctaPrimary) return;

    const hasModel = !!modelSelect.value;
    dsModel.textContent = hasModel ? modelSelect.value : '尚未安裝模型';
    dsModel.className = 'ds-value ' + (hasModel ? 'ok' : 'warn');

    const st = statusText ? statusText.textContent : '';
    const backendOk = statusIndicator && statusIndicator.classList.contains('ok');
    if (dsBackend) {
        dsBackend.textContent = backendOk ? '正常' : (st || '未連線');
        dsBackend.className = 'ds-value ' + (backendOk ? 'ok' : 'err');
    }

    const kbReady = kbStatus.index_status === 'ready';
    if (BASIC_CHAT_MODE) return configureBasicWelcomeDashboard(hasModel, ctaPrimary, ctaSecondary);
    // 三種空狀態 CTA
    if (!hasModel) {
        ctaPrimary.textContent = '安裝推薦模型';
        ctaPrimary.onclick = () => openModelManager('recommended');
        ctaSecondary.textContent = '連接既有 Ollama';
        ctaSecondary.onclick = () => document.getElementById('btn-settings-trigger').click();
    } else if (!kbReady) {
        ctaPrimary.textContent = '上傳文件建立知識庫';
        ctaPrimary.onclick = () => openKnowledgeCenter('documents');
        ctaSecondary.textContent = '直接開始一般對話';
        ctaSecondary.onclick = () => { userInput.focus(); };
    } else {
        ctaPrimary.textContent = '開始詢問知識庫';
        ctaPrimary.onclick = () => {
            ragToggle.checked = true;
            updateRagChip();
            userInput.focus();
        };
        ctaSecondary.textContent = '上傳更多文件';
        ctaSecondary.onclick = () => openKnowledgeCenter('documents');
    }
    const ctaT = document.getElementById('cta-tertiary');
    if (ctaT) ctaT.onclick = () => document.getElementById('btn-settings-trigger').click();
}

// ---- 答案卡動作 / 指標列（P8）----
function appendAnswerFooter(bubbleEl, opts) {
    // Artifact chip（P8.2 / 驗收 5）
    if (opts.artifactProduced) {
        const chip = document.createElement('button');
        chip.className = 'artifact-chip';
        chip.innerHTML = `<i data-lucide="code-xml" style="width:14px;height:14px;"></i><span>Artifact：${escapeHtml(activeArtifactTitle || 'HTML 原型')}</span><span style="color:var(--primary-color); font-weight:600;">打開工作區</span>`;
        chip.addEventListener('click', () => { showArtifactsPanel(true); openInspector('artifact'); });
        bubbleEl.appendChild(chip);
    }

    // 動作列 + 指標
    const bar = document.createElement('div');
    bar.className = 'answer-actions';
    const mkBtn = (label, icon, fn) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'act-btn';
        b.setAttribute('aria-label', label);
        b.title = label;
        b.innerHTML = `<i data-lucide="${icon}" aria-hidden="true"></i><span>${label}</span>`;
        b.addEventListener('click', fn);
        return b;
    };
    bar.appendChild(mkBtn('複製', 'copy', async () => {
        try { await navigator.clipboard.writeText(opts.text || ''); showToast('已複製回答內容', 'success'); }
        catch (e) { showToast('複製失敗：' + e.message, 'error'); }
    }));
    bar.appendChild(mkBtn('重新生成', 'refresh-cw', () => regenerateLastAnswer()));
    if (!BASIC_CHAT_MODE) bar.appendChild(mkBtn('查看 Run', 'list-checks', () => { openInspector('run'); }));

    if (opts.showMetrics !== false) {
        const m = opts.metrics || {};
        const metricsEl = document.createElement('span');
        metricsEl.className = 'answer-metrics';
        metricsEl.textContent = m.elapsed ? `生成 ${m.elapsed.toFixed(1)} 秒` : '生成完成';
        const details = [];
        if (m.tokps) details.push(`${m.tokps} tok/s`);
        details.push(`${(estimateCtxTokens() / 1000).toFixed(1)}k / ${CTX_WINDOW_TOKENS / 1024}k context`);
        metricsEl.title = details.join(' · ');
        bar.appendChild(metricsEl);
    }
    bubbleEl.appendChild(bar);
    safeCreateIcons();
}

function regenerateLastAnswer() {
    if (isGenerating) { showToast('生成中，請先停止或等待完成', 'info'); return; }
    const lastUser = [...conversationState].reverse().find(m => m.role === 'user');
    if (!lastUser) { showToast('沒有可重新生成的問題', 'info'); return; }
    // 移除最後一組 user/assistant，交由送出流程重新加入
    if (conversationState.length && conversationState[conversationState.length - 1].role === 'assistant') conversationState.pop();
    if (conversationState.length && conversationState[conversationState.length - 1].role === 'user') conversationState.pop();
    userInput.value = lastUser.content.replace(/\n\n【輸出要求】[\s\S]*$/, '').replace(/^\/code /, '');
    chatForm.dispatchEvent(new Event('submit'));
}

// ---- Inspector Panel（P10）----
function closeInspectorPanel({ focusTarget = null } = {}) {
    const focusWasInside = artifactsSandboxPanel.contains(document.activeElement);
    artifactsSandboxPanel.classList.remove('active');
    artifactsSandboxPanel.setAttribute('aria-label', 'Inspector 面板');
    if (btnSandboxToggle) btnSandboxToggle.classList.remove('active');
    if (focusWasInside) {
        (focusTarget || btnSandboxToggle || document.getElementById('rail-artifacts'))?.focus?.();
    }
}

function openInspector(tab) {
    if (primaryWorkspace !== 'chat') return false;
    const pane = document.getElementById(`inspector-pane-${tab}`);
    if (!pane) return false;
    setOutputFloatingPanelOpen(false);
    closeAgentCollaboration(false);
    collapseCompactChatDrawer();
    artifactsSandboxPanel.classList.add('active');
    artifactsSandboxPanel.setAttribute('aria-label', 'Inspector 面板');
    document.querySelectorAll('.inspector-tab').forEach(inspectorTab => {
        const selected = inspectorTab.dataset.itab === tab;
        inspectorTab.classList.toggle('active', selected);
        inspectorTab.setAttribute('aria-selected', selected ? 'true' : 'false');
        inspectorTab.tabIndex = selected ? 0 : -1;
    });
    document.querySelectorAll('.inspector-pane').forEach(inspectorPane => {
        const selected = inspectorPane === pane;
        inspectorPane.classList.toggle('active', selected);
        inspectorPane.hidden = !selected;
    });
    if (tab === 'context') renderContextPane();
    if (tab === 'run') renderRunPane();
    if (tab === 'safir') renderSafirPane();
    if (tab === 'models') renderModelsPane();
    if (tab === 'logs') renderLogsPane();
    return true;
}

function renderContextPane() {
    const s = document.getElementById('ip-context-sources');
    const t = document.getElementById('ip-context-temp');
    const c = document.getElementById('ip-context-conv');
    const u = document.getElementById('ip-context-usage');
    if (!s) return;
    const lastRun = runHistory[runHistory.length - 1];
    if (lastRun && lastRun.sources && lastRun.sources.length) {
        s.innerHTML = lastRun.sources.map((x, i) =>
            `<div style="padding:4px 0;">${i + 1}. ${escapeHtml(x.source || '')}${x.page ? ` p.${x.page}` : ''} · ${formatSourceScore(x, '')}</div>`).join('');
    } else {
        s.textContent = '本輪尚無檢索來源。';
    }
    t.textContent = temporaryContextText
        ? `已載入臨時文件（約 ${(temporaryContextText.length / 1000).toFixed(1)}k 字元）`
        : '無臨時文件。';
    c.textContent = `${conversationState.length} 則訊息（user ${conversationState.filter(m => m.role === 'user').length} / assistant ${conversationState.filter(m => m.role === 'assistant').length}）`;
    const used = estimateCtxTokens();
    u.textContent = `約 ${(used / 1000).toFixed(1)}k / ${CTX_WINDOW_TOKENS / 1024}k tokens（前端估算）`;
}

function renderRunPane() {
    const el = document.getElementById('ip-run-timeline');
    if (!el) return;
    if (!runHistory.length) { el.textContent = '尚無執行紀錄。'; return; }
    el.innerHTML = runHistory.slice().reverse().map(r => {
        const evts = (r.events || []).map(e => {
            if (e.kind === 'tool') return `<div>✓ ${escapeHtml((e.desc || e.name || '').replace('執行中', '已執行'))}</div>`;
            if (e.kind === 'validation') return `<div style="color:${e.passed ? 'var(--success-color)' : 'var(--warning-color)'};">${e.passed ? '✓' : '⚠'} ${escapeHtml(e.text || '')}</div>`;
            return `<div>· ${escapeHtml(e.text || '')}</div>`;
        }).join('');
        const m = r.metrics || {};
        return `<div style="border:1px solid var(--panel-border); border-radius:9px; padding:10px 12px; margin-bottom:8px; background:var(--surface-subtle);">
            <div style="font-weight:600; margin-bottom:4px;">${r.time} · ${escapeHtml(r.model || '')}</div>
            ${evts || '<div style="color:var(--text-muted);">（無工具步驟）</div>'}
            <div style="color:var(--text-muted); margin-top:6px; font-size:12px;">${m.elapsed ? `用時 ${m.elapsed.toFixed(1)}s` : ''}${m.tokps ? ` · ${m.tokps} tok/s` : ''}${r.sources && r.sources.length ? ` · ${r.sources.length} 來源` : ''}</div>
        </div>`;
    }).join('');
}

function renderSafirPane() {
    const status = document.getElementById('ip-safir-status');
    const graph = document.getElementById('ip-safir-graph');
    const risk = document.getElementById('ip-safir-risk');
    if (!status || !graph || !risk) return;
    const data = latestSafirAnalysis;
    if (!data) {
        status.textContent = '本輪尚無 SAFIR 分析。';
        graph.textContent = '--';
        risk.textContent = '--';
        return;
    }
    const claims = data.claims || [];
    const evidence = data.evidence || [];
    const verified = claims.filter(c => c.status === 'verified').length;
    const quarantined = claims.filter(c => c.status === 'quarantined').length;
    const semanticClaims = claims.filter(c => c.verification_method === 'multilingual_nli').length;
    const qualities = evidence.map(item => Number(item.source_reliability)).filter(Number.isFinite);
    const averageQuality = qualities.length ? Math.round((qualities.reduce((sum, value) => sum + value, 0) / qualities.length) * 100) : 0;
    const ttlLabels = [...new Set(evidence.map(item => item.ttl).filter(Boolean))];
    const safirStateLabel = data.delivery_action === 'repair' ? '補查中' : data.delivery_action === 'advisory' ? '有缺口／不阻擋' : '通過';
    status.textContent = `${data.mode} · ${safirStateLabel} · 證據覆蓋率 ${Math.round((data.evidence_coverage || 0) * 100)}%`;
    graph.textContent = `Claims ${claims.length}（verified ${verified} / 待確認 ${quarantined}） · Evidence ${evidence.length} · NLI ${semanticClaims} · 品質 ${averageQuality || '--'}% · TTL ${ttlLabels.join('/') || '--'}`;
    risk.textContent = data.breaker_reasons?.length ? data.breaker_reasons.join('、') : '目前沒有未解決的查證提示';
}

function renderModelsPane() {
    const a = document.getElementById('ip-models-active');
    const t = document.getElementById('ip-models-ttft');
    const k = document.getElementById('ip-models-tokps');
    const h = document.getElementById('ip-models-hw');
    if (!a) return;
    a.textContent = modelSelect.value || '尚未選擇';
    t.textContent = lastMetrics && lastMetrics.ttft != null ? `${lastMetrics.ttft.toFixed(2)}s` : '尚未量測';
    k.textContent = lastMetrics && lastMetrics.tokps ? `${lastMetrics.tokps} tok/s` : '尚未量測';
    h.textContent = detectHardwareString();
}

function renderLogsPane() {
    const el = document.getElementById('ip-logs-body');
    if (el) el.textContent = sseLogBuffer.length ? sseLogBuffer.join('\n') : '尚無事件。';
}

// ---- 硬體偵測（前端可得的近似資訊）----
function detectHardwareString() {
    const parts = [];
    if (navigator.deviceMemory) parts.push(`RAM ≥ ${navigator.deviceMemory}GB（瀏覽器回報上限 8GB）`);
    if (navigator.hardwareConcurrency) parts.push(`${navigator.hardwareConcurrency} 執行緒`);
    try {
        const gl = document.createElement('canvas').getContext('webgl');
        const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
        if (ext) parts.push(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL));
    } catch (e) {}
    return parts.join(' · ') || '無法取得硬體資訊';
}

// ---- Model Manager（P5）----
// 型錄的唯一來源是後端 /api/models/catalog：它同時知道已安裝清單與本機硬體。
// 前端再放一份硬編碼清單，只會在兩邊的模型數、名稱或硬體建議不一致時誤導使用者。
let modelCatalogCache = null;

// 風險等級對使用者的說法。批准對話框只顯示英文代碼時，使用者無從判斷該不該按。
const RISK_LABELS = {
    read: '讀取工作區',
    external_read: '讀取外部資訊',
    verify: '執行專案驗證',
    write: '寫入工作區檔案',
    external_write: '變更外部狀態（瀏覽器／應用程式）',
    system: '系統層操作',
    irreversible: '無法復原的操作'
};
function riskLabel(risk) {
    const key = String(risk || 'system');
    return RISK_LABELS[key] ? `${RISK_LABELS[key]}（${key}）` : key;
}

const MODEL_PURPOSE_LABELS = {
    chat: '一般對話', code: '程式開發', rag: '知識庫', vision: '圖片理解',
    tools: '工具操作', reasoning: '推理', math: '數學', multilingual: '多語言',
    edge: '輕量裝置', research: '開放研究', documents: '文件理解'
};

function formatCatalogSize(sizeGb) {
    const value = Number(sizeGb || 0);
    if (!value) return '大小未知';
    return value < 1 ? `約 ${Math.round(value * 1000)}MB` : `約 ${value}GB`;
}

function formatCatalogContext(context) {
    const value = Number(context || 0);
    if (!value) return '未知';
    if (value >= 1_048_576) return `${Math.round(value / 1_048_576 * 10) / 10}M`;
    if (value >= 1_024) return `${Math.round(value / 1_024)}K`;
    return String(value);
}

function normalizeCatalogEntry(item) {
    const compatibility = item.compatibility || {};
    const level = compatibility.level || compatibility.fit || 'unknown';
    const purposes = item.purposes || item.category || [];
    const sizeGb = item.size_gb ?? item.size_gb_estimated;
    const recRam = item.recommended_ram_gb;
    const recVram = item.rec_vram_gb ?? item.recommended_vram_gb;
    const publisher = item.publisher || '';
    const license = item.license || '';
    const displayName = item.display_name || item.name;
    const purposeLabels = purposes.map(purpose => MODEL_PURPOSE_LABELS[purpose] || purpose);
    return {
        name: item.name,
        display_name: displayName,
        installed: !!item.installed,
        installed_as: item.installed_as || '',
        purposes,
        use: purposeLabels.join(' / ') || '一般用途',
        size: formatCatalogSize(sizeGb),
        need: [recRam ? `建議 ${recRam}GB RAM` : '', recVram ? `${recVram}GB VRAM` : '']
            .filter(Boolean).join(' / ') || '硬體需求未知',
        fit: level === 'good' ? 'good' : (level === 'ok' ? 'slow' : 'bad'),
        fitLabel: compatibility.label || (level === 'good' ? '相容性：良好' : level === 'ok' ? '可運行但偏慢' : '相容性未知'),
        fitReason: compatibility.reason || '',
        context: item.context ?? item.context_window,
        contextLabel: formatCatalogContext(item.context ?? item.context_window),
        publisher,
        license,
        source_url: item.source_url || '',
        description: item.description || '',
        searchText: [item.name, displayName, publisher, license, purposeLabels.join(' '), item.description || '']
            .join(' ').toLowerCase()
    };
}

async function loadModelCatalog(force = false) {
    if (modelCatalogCache && !force) return modelCatalogCache;
    const res = await apiFetch(`${API_BASE}/api/models/catalog`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const items = (data.catalog || data.models || []).map(normalizeCatalogEntry);
    const byName = new Map(items.map(m => [m.name, m]));
    const recommended = (data.recommended || [])
        .map(r => byName.get(r.name) || normalizeCatalogEntry(r))
        .filter(Boolean);
    modelCatalogCache = {
        items,
        recommended: recommended.length ? recommended : items.filter(m => !m.installed && m.fit !== 'bad'),
        hardware: data.hardware || null
    };
    return modelCatalogCache;
}

function hardwareSummaryText(hardware) {
    if (!hardware) return detectHardwareString();
    const ram = hardware.ram_total_gb ? `${hardware.ram_total_gb}GB RAM` : '';
    const gpuList = hardware.gpu || [];
    const gpu = gpuList.length
        ? `${gpuList[0].name || 'GPU'}${gpuList[0].vram_total_gb ? ` · ${gpuList[0].vram_total_gb}GB VRAM` : ''}`
        : '未偵測到獨立 GPU';
    return [ram, gpu].filter(Boolean).join(' · ') || detectHardwareString();
}

let pendingSwitchModel = null;
const validatedExternalModels = new Set();
const modelInstallJobs = new Map();
const modelInstallStreams = new Map();
const customCatalogModels = new Map();
const MODEL_INSTALL_ACTIVE = new Set(['queued', 'starting', 'downloading', 'cancelling']);
const OLLAMA_MODEL_REFERENCE = /^[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?$/;

function isSafeOllamaModelReference(value) {
    const model = String(value || '').trim();
    if (!model || model.length > 200 || !OLLAMA_MODEL_REFERENCE.test(model) || model.includes('//')) return false;
    const repository = model.split(':', 1)[0];
    return repository.split('/').every(segment => segment && segment !== '.' && segment !== '..');
}

function customCatalogEntry(name) {
    return {
        name,
        display_name: name,
        installed: false,
        installed_as: '',
        purposes: ['chat'],
        use: '自訂 Ollama 模型',
        size: '由 Ollama 回報',
        need: '請先查看模型頁面的硬體需求',
        fit: 'slow',
        fitLabel: '尚未評估',
        fitReason: '自訂標籤不在 Workbench 已驗證型錄中。',
        context: null,
        contextLabel: '未知',
        publisher: 'Ollama Library',
        license: '請查看模型頁面',
        source_url: 'https://ollama.com/library',
        description: '',
        searchText: `${name} 自訂 ollama 模型`.toLowerCase(),
        custom: true
    };
}

function openModelManager(tab = 'installed') {
    setPrimaryWorkspace('models');
    switchMmTab(tab);
    renderMmInstalled();
    renderMmRecommended();
    renderMmAvailable(
        document.getElementById('mm-search')?.value || '',
        document.getElementById('mm-category-filter')?.value || ''
    );
    syncModelInstallJobs();
    window.requestAnimationFrame(() => document.getElementById('model-manager-title')?.focus({ preventScroll: true }));
}

function closeModelManager({ restoreFocus = true } = {}) {
    setPrimaryWorkspace('chat');
    if (restoreFocus) document.getElementById('rail-chat')?.focus();
}

function switchMmTab(tab) {
    document.querySelectorAll('.mm-tab[data-mmtab]').forEach(button => {
        const active = button.dataset.mmtab === tab;
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', active ? 'true' : 'false');
        button.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll('.mm-pane').forEach(pane => {
        const active = pane.id === `mm-pane-${tab}`;
        pane.classList.toggle('active', active);
        pane.hidden = !active;
    });
}
function mmCard({ title, meta, badge, actions, progress }) {
    const card = document.createElement('div');
    card.className = 'mm-card';
    const info = document.createElement('div');
    info.className = 'mm-card-info';
    info.innerHTML = `<div class="mm-card-name">${escapeHtml(title)}${badge || ''}</div><div class="mm-card-meta">${meta}</div>`;
    const act = document.createElement('div');
    act.className = 'mm-card-actions';
    (actions || []).forEach(a => {
        const b = document.createElement('button');
        b.className = `btn ${a.danger ? 'btn-danger' : (a.primary ? 'btn-primary' : 'btn-secondary')}`;
        b.textContent = a.label;
        b.disabled = !!a.disabled;
        b.addEventListener('click', a.onClick);
        act.appendChild(b);
    });
    if (progress) info.appendChild(progress);
    card.appendChild(info); card.appendChild(act);
    return card;
}

function formatModelInstallBytes(value) {
    const bytes = Number(value || 0);
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    return `${(bytes / Math.pow(1024, index)).toFixed(index >= 3 ? 2 : 1)} ${units[index]}`;
}

function modelInstallProgress(job) {
    if (!job) return null;
    const progress = Math.max(0, Math.min(100, Number(job.progress ?? job.percent ?? 0)));
    const downloaded = Number(job.downloaded_bytes ?? job.completed ?? 0);
    const total = Number(job.total_bytes ?? job.total ?? 0);
    const terminalLabel = job.status === 'ready' ? '安裝完成' : job.status === 'cancelled' ? '已停止' : job.status === 'failed' ? '安裝失敗' : job.status === 'cancelling' ? '正在停止…' : job.message || '下載中';
    const detail = total > 0 ? `${formatModelInstallBytes(downloaded)} / ${formatModelInstallBytes(total)}` : formatModelInstallBytes(downloaded);
    const wrap = document.createElement('div');
    wrap.className = `mm-install-progress is-${job.status || 'queued'}`;
    wrap.innerHTML = `<div class="mm-install-progress-head"><span>${escapeHtml(terminalLabel)}</span><span>${progress}% · ${escapeHtml(detail)}</span></div><div class="mm-install-progress-track" role="progressbar" aria-label="${escapeHtml(job.model || '模型')} 安裝進度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><div class="mm-install-progress-fill" style="width:${progress}%"></div></div>`;
    return wrap;
}

function modelInstallActions(model) {
    const job = modelInstallJobs.get(model.name);
    if (job && MODEL_INSTALL_ACTIVE.has(job.status)) {
        return [{
            label: job.status === 'cancelling' ? '停止中…' : '停止安裝',
            danger: true,
            disabled: job.status === 'cancelling',
            onClick: () => stopModelInstall(job.job_id)
        }];
    }
    if (model.installed) return [{ label: '已安裝', disabled: true }];
    if (job?.status === 'ready') return [];
    return [{ label: job?.status === 'failed' || job?.status === 'cancelled' ? '重新安裝' : '開始安裝', primary: true, onClick: () => startModelInstall(model.name) }];
}

function refreshModelInstallCards() {
    renderMmRecommended();
    renderMmAvailable(
        document.getElementById('mm-search')?.value || '',
        document.getElementById('mm-category-filter')?.value || ''
    );
}

async function syncModelInstallJobs() {
    try {
        const response = await apiFetch(`${API_BASE}/api/models/install`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        (data.jobs || []).forEach(job => {
            if (!modelInstallJobs.has(job.model)) modelInstallJobs.set(job.model, job);
            if (MODEL_INSTALL_ACTIVE.has(job.status)) monitorModelInstall(job.job_id);
        });
        refreshModelInstallCards();
    } catch (error) {
        console.debug('[Model Install] 無法同步安裝工作:', error);
    }
}

async function startModelInstall(model) {
    try {
        const response = await apiFetch(`${API_BASE}/api/models/install`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail?.message || data.message || `HTTP ${response.status}`);
        modelInstallJobs.set(model, { ...data, progress: 0, downloaded_bytes: 0, total_bytes: 0 });
        refreshModelInstallCards();
        monitorModelInstall(data.job_id);
        showToast(`已開始安裝 ${model}`, 'info');
        return true;
    } catch (error) {
        showToast(`無法開始安裝：${error.message}`, 'error');
        return false;
    }
}

async function installCustomOllamaModel() {
    const input = document.getElementById('mm-custom-model');
    const model = String(input?.value || '').trim();
    if (!isSafeOllamaModelReference(model)) {
        showToast('請輸入有效的 Ollama 模型標籤，例如 qwen3.5:4b。', 'error');
        input?.focus();
        return;
    }
    try {
        const catalog = await loadModelCatalog();
        if (catalog.items.some(item => item.name === model || item.installed_as === model)) {
            const known = catalog.items.find(item => item.name === model || item.installed_as === model);
            if (known?.installed) {
                showToast(`${model} 已經安裝`, 'info');
                return;
            }
        } else {
            customCatalogModels.set(model, customCatalogEntry(model));
        }
    } catch (error) {
        // The install endpoint will provide the authoritative connectivity
        // error; keep the requested tag visible so its progress can render.
        customCatalogModels.set(model, customCatalogEntry(model));
    }
    const search = document.getElementById('mm-search');
    const category = document.getElementById('mm-category-filter');
    if (search) search.value = model;
    if (category) category.value = '';
    renderMmAvailable(model, '');
    const started = await startModelInstall(model);
    if (!started) customCatalogModels.delete(model);
    if (started && input) input.value = '';
}

async function stopModelInstall(jobId) {
    const job = [...modelInstallJobs.values()].find(item => item.job_id === jobId);
    if (!job) return;
    modelInstallJobs.set(job.model, { ...job, status: 'cancelling', message: '正在停止安裝…' });
    refreshModelInstallCards();
    try {
        const response = await apiFetch(`${API_BASE}/api/models/install/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail?.message || data.message || `HTTP ${response.status}`);
        modelInstallJobs.set(job.model, { ...job, status: data.status, message: data.status === 'cancelled' ? '已停止安裝' : '正在停止安裝…' });
        refreshModelInstallCards();
        showToast(`${job.model} 的下載已要求停止`, 'success');
    } catch (error) {
        modelInstallJobs.set(job.model, job);
        refreshModelInstallCards();
        showToast(`停止安裝失敗：${error.message}`, 'error');
    }
}

function monitorModelInstall(jobId) {
    if (!jobId || modelInstallStreams.has(jobId)) return;
    const stream = new EventSource(apiUrl(`${API_BASE}/api/models/install/${encodeURIComponent(jobId)}/events`));
    modelInstallStreams.set(jobId, stream);
    stream.addEventListener('model_install_progress', event => {
        const job = JSON.parse(event.data);
        modelInstallJobs.set(job.model, job);
        refreshModelInstallCards();
    });
    stream.addEventListener('done', async event => {
        const result = JSON.parse(event.data);
        const finishedJob = [...modelInstallJobs.values()].find(item => item.job_id === jobId);
        stream.close();
        modelInstallStreams.delete(jobId);
        await syncModelInstallJobs();
        if (result.status === 'ready') {
            if (finishedJob?.model) customCatalogModels.delete(finishedJob.model);
            await loadModels();
            await loadModelCatalog(true);
            renderMmInstalled();
            renderMmRecommended();
            renderMmAvailable(
                document.getElementById('mm-search')?.value || '',
                document.getElementById('mm-category-filter')?.value || ''
            );
            showToast('模型安裝完成', 'success');
        }
    });
    stream.addEventListener('error', () => {
        stream.close();
        modelInstallStreams.delete(jobId);
        setTimeout(syncModelInstallJobs, 1000);
    });
}
async function renderMmInstalled() {
    const list = document.getElementById('mm-installed-list');
    if (!list) return;
    list.textContent = '載入中...';
    try {
        const res = await apiFetch(`${API_BASE}/api/models`);
        const data = await res.json();
        const ready = new Set(Array.isArray(data.models) ? data.models : []);
        const configured = Array.isArray(data.configured_models) ? data.configured_models : [];
        const configuredByName = new Map(configured.map(item => [item.name, item]));
        const entries = [...ready].map(name => {
            const provider = name.includes('::') ? name.split('::', 1)[0] : 'ollama';
            const metadata = configuredByName.get(name) || {};
            return { ...metadata, name, provider, provider_label: metadata.provider_label || (provider === 'ollama' ? 'Ollama' : provider), ready: true };
        });
        configured.filter(item => !ready.has(item.name)).forEach(item => entries.push({ ...item, ready: false }));
        list.innerHTML = '';
        if (!entries.length) {
            list.innerHTML = `<div class="mm-note">尚未安裝本地模型或連接 API 模型。<br>本地模型可由「Recommended」安裝；API 模型可由「雲端 LLM」匯入。</div>`;
            return;
        }
        entries.forEach(entry => {
            const name = entry.name;
            const isActive = name === modelSelect.value;
            const isDefault = currentSettings && currentSettings.default_chat_model === name;
            const chatEligible = modelEligibleForChat(entry, name);
            const stateText = !chatEligible ? `${entry.model_kind || '專用'}模型` : entry.ready ? '可用' : '已連接，待權限啟用';
            const actions = !chatEligible ? [{ label: '專用工具', disabled: true }] : entry.ready ? [
                { label: isActive ? '使用中' : '切換使用', primary: !isActive, onClick: () => { if (!isActive) openModelSwitch(name); } }, { label: '測速', onClick: () => { switchMmTab('benchmark'); runBenchmark(name); } }] : [{ label: '啟用並切換', primary: true, onClick: () => activateConfiguredModel(entry) }];
            list.appendChild(mmCard({
                title: name,
                badge: isActive ? '<span class="mm-badge good">使用中</span>' : (isDefault ? '<span class="mm-badge slow">預設</span>' : ''),
                meta: `來源：${escapeHtml(entry.provider_label)} · 狀態：${stateText}${isDefault ? ' · 預設模型' : ''}`,
                actions
            }));
        });
    } catch (e) {
        list.innerHTML = `<div class="mm-note">無法載入模型清單：後端未連線。</div>`;
    }
}

async function activateConfiguredModel(entry) {
    try {
        await window.workbenchExtensions.reviewProviderModel(entry.extension_id, async () => {
            await validateExternalModelForSwitch(entry.name);
            await loadModels();
            if ([...modelSelect.options].some(option => option.value === entry.name)) {
                modelSelect.value = entry.name;
                activeModelName.textContent = entry.name;
                sendBtn.disabled = false;
                updateWelcomeDashboard();
                showToast(`已啟用並切換至 ${entry.name}`, 'success');
            } else {
                showToast(`${entry.provider_label} 已啟用，但目前無法取得模型清單，請檢查 API 額度或連線。`, 'error');
            }
            await renderMmInstalled();
        });
    } catch (error) {
        showToast(`無法啟用 API 模型：${error.message}`, 'error');
    }
}
function mmCatalogCard(m, meta) {
    const job = modelInstallJobs.get(m.name);
    const badge = `<span class="mm-badge ${m.fit}" title="${escapeHtml(m.fitReason)}">${escapeHtml(m.fitLabel)}</span>`;
    return mmCard({
        title: m.display_name || m.name,
        badge,
        meta,
        actions: modelInstallActions(m),
        progress: modelInstallProgress(job)
    });
}
async function renderMmRecommended() {
    const hw = document.getElementById('mm-hw-card');
    const list = document.getElementById('mm-recommended-list');
    if (!hw || !list) return;
    list.innerHTML = '<div class="mm-note">載入模型型錄中...</div>';
    try {
        const catalog = await loadModelCatalog();
        hw.innerHTML = `<strong>你的硬體（後端偵測）</strong><br>${escapeHtml(hardwareSummaryText(catalog.hardware))}`;
        list.innerHTML = '';
        catalog.recommended.slice(0, 4).forEach(m => {
            list.appendChild(mmCatalogCard(m,
                `模型標籤：${escapeHtml(m.name)} · 用途：${escapeHtml(m.use)} · ${escapeHtml(m.size)} · ${escapeHtml(m.need)}`
            ));
        });
        if (!list.children.length) list.innerHTML = '<div class="mm-note">目前沒有適合這台機器且尚未安裝的模型。</div>';
    } catch (e) {
        hw.innerHTML = `<strong>你的硬體（瀏覽器偵測，近似值）</strong><br>${escapeHtml(detectHardwareString())}`;
        list.innerHTML = '<div class="mm-note">無法載入模型型錄：後端未連線。</div>';
    }
}
async function renderMmAvailable(filter = '', category = '') {
    const list = document.getElementById('mm-available-list');
    const summary = document.getElementById('mm-catalog-summary');
    if (!list) return;
    list.innerHTML = '<div class="mm-note">載入模型型錄中...</div>';
    const q = filter.trim().toLowerCase();
    const selectedCategory = String(category || '').trim().toLowerCase();
    try {
        const catalog = await loadModelCatalog();
        const customItems = [...customCatalogModels.values()]
            .filter(item => !catalog.items.some(known => known.name === item.name));
        const available = [...catalog.items, ...customItems].filter(m => !m.installed);
        const matches = available.filter(m => {
            const matchesText = !q || m.searchText.includes(q);
            const matchesCategory = !selectedCategory || m.purposes.includes(selectedCategory);
            return matchesText && matchesCategory;
        });
        list.innerHTML = '';
        matches.forEach(m => {
            const provenance = [m.publisher, m.license].filter(Boolean).map(escapeHtml).join(' · ');
            list.appendChild(mmCatalogCard(m,
                `模型標籤：${escapeHtml(m.name)} · 下載：${escapeHtml(m.size)} · Context：${escapeHtml(m.contextLabel)} · 需求：${escapeHtml(m.need)} · 用途：${escapeHtml(m.use)}${provenance ? ` · ${provenance}` : ''}`
            ));
        });
        if (summary) {
            summary.textContent = `已驗證 ${catalog.items.length} 個 Ollama 官方生成模型；目前有 ${available.length} 個尚未安裝，顯示 ${matches.length} 個。`;
        }
        if (!list.children.length) list.innerHTML = '<div class="mm-note">找不到符合的模型。</div>';
    } catch (e) {
        list.innerHTML = '<div class="mm-note">無法載入模型型錄：後端未連線。</div>';
        if (summary) summary.textContent = '暫時無法取得模型型錄。';
    }
}
async function runBenchmark(modelName) {
    const out = document.getElementById('mm-benchmark-result');
    const model = modelName || modelSelect.value;
    if (!model) { out.textContent = '請先選擇模型。'; return; }
    const progressId = `model-benchmark-${model}`;
    out.textContent = `正在對 ${model} 執行測速（短提示）...`;
    updateTaskProgress(progressId, { label: `模型測速：${model}`, detail: '正在等待第一個 token', mode: 'indeterminate', value: null });
    const start = performance.now();
    let first = 0, tokens = 0;
    try {
        const res = await apiFetch(`${API_BASE}/api/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model, messages: [{ role: 'user', content: '請用一句話介紹你自己。' }], use_rag: false, session_id: null, images: [], temporary_context: '' })
        });
        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buf = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            const lines = buf.split('\n'); buf = lines.pop();
            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    try {
                        const d = JSON.parse(line.slice(6));
                        if (d.content) {
                            if (!first) first = performance.now();
                            tokens += Math.max(1, Math.round(d.content.length / 3));
                            updateTaskProgress(progressId, { detail: `正在量測生成速度 · 約 ${tokens} tokens`, mode: 'indeterminate', value: null });
                        }
                    } catch (e) {}
                }
            }
        }
        const ttft = first ? ((first - start) / 1000).toFixed(2) : '--';
        const secs = first ? (performance.now() - first) / 1000 : 1;
        const tokps = Math.round(tokens / Math.max(secs, 0.2));
        out.innerHTML = `<div class="mm-card"><div class="mm-card-info"><div class="mm-card-name">${escapeHtml(model)}</div><div class="mm-card-meta">TTFT：${ttft}s · 速度：約 ${tokps} tok/s · 樣本 tokens：${tokens}</div></div></div>`;
        lastMetrics = { ...(lastMetrics || {}), ttft: first ? (first - start) / 1000 : null, tokps };
        const chip = document.getElementById('chip-speed-text');
        if (chip) chip.textContent = `${tokps} tok/s`;
        finishTaskProgress(progressId, 'completed', `測速完成：約 ${tokps} tok/s`);
    } catch (e) {
        out.textContent = '測速失敗：後端未連線或模型無法回應。';
        finishTaskProgress(progressId, 'failed', e.message || '模型無法回應');
    }
}
function openModelSwitch(target) {
    pendingSwitchModel = target;
    const externalNotice = target.includes('::')
        ? '<br><span class="mm-note">套用前會送出一則最小測試訊息，確認此 API 帳戶真的能使用該模型。</span>'
        : '';
    document.getElementById('ms-desc').innerHTML = `目前對話使用：<strong>${escapeHtml(modelSelect.value || '--')}</strong><br>即將切換至：<strong>${escapeHtml(target)}</strong>${externalNotice}`;
    document.getElementById('model-switch-modal').classList.add('active');
}

async function validateExternalModelForSwitch(reference) {
    if (!reference.includes('::') || validatedExternalModels.has(reference)) return;
    const separator = reference.indexOf('::');
    const providerId = reference.slice(0, separator).toLowerCase();
    const model = reference.slice(separator + 2);
    const provider = (currentSettings.model_providers || []).find(item => String(item.id || '').toLowerCase() === providerId);
    if (!provider) throw new Error(`找不到 ${providerId} 的 API 連線設定`);
    const response = await apiFetch(`${API_BASE}/api/settings/providers/model-test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            provider_id: providerId,
            provider_type: provider.provider_type || 'openai_compatible',
            base_url: provider.base_url,
            model,
            model_kind: provider.model_kind || '',
            supports_tools: provider.supports_tools === true,
            language_pair: provider.language_pair || '',
            system_prompt: 'Return only the requested token.',
            prompt: 'Reply with exactly: READY'
        })
    });
    const data = await response.json();
    if (!response.ok || !data.success) {
        throw new Error(data.detail?.message || data.message || `HTTP ${response.status}`);
    }
    validatedExternalModels.add(reference);
}

async function validateExternalCollaborationModels(roleModels) {
    const references = [...new Set(Object.values(roleModels || {}).filter(value => String(value).includes('::')))];
    for (const reference of references) {
        await validateExternalModelForSwitch(String(reference));
    }
}

async function applyModelSwitch(setDefault) {
    if (!pendingSwitchModel) return;
    const target = pendingSwitchModel;
    const buttons = ['ms-apply-session', 'ms-apply-default', 'ms-cancel'].map(id => document.getElementById(id));
    buttons.forEach(button => { button.disabled = true; });
    try {
        await validateExternalModelForSwitch(target);
        modelSelect.value = target;
        activeModelName.textContent = target;
        if (setDefault) {
            const payload = { ...currentSettings, default_chat_model: target };
            const response = await apiFetch(`${API_BASE}/api/settings`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            currentSettings.default_chat_model = target;
            showToast(`已驗證並將 ${target} 設為預設模型`, 'success');
        } else {
            showToast(`已驗證並切換至 ${target}`, 'success');
        }
        document.getElementById('model-switch-modal').classList.remove('active');
        pendingSwitchModel = null;
        renderMmInstalled();
        updateWelcomeDashboard();
    } catch (error) {
        showToast(`無法切換模型：${error.message}`, 'error');
    } finally {
        buttons.forEach(button => { button.disabled = false; });
    }
}

// ---- Knowledge Center（P11）----
function openKnowledgeCenter(tab = 'documents') {
    kbManagerModal.classList.add('active');
    document.querySelectorAll('.mm-tab[data-kbtab]').forEach(t => t.classList.toggle('active', t.dataset.kbtab === tab));
    document.querySelectorAll('.kb-pane').forEach(p => p.classList.remove('active'));
    const pane = document.getElementById(`kb-pane-${tab}`);
    if (pane) pane.classList.add('active');
    if (tab === 'index') renderKbIndexSettings();
    loadKBFiles();
    loadRagStatus();
}
async function renderKbIndexSettings() {
    const el = document.getElementById('kb-index-settings');
    if (!el) return;
    try {
        const res = await apiFetch(`${API_BASE}/api/settings`);
        const d = await res.json();
        el.innerHTML = `
            <div class="mm-card"><div class="mm-card-info"><div class="mm-card-meta" style="font-size:12.5px; line-height:2;">
                Embedding model：本地 HuggingFace（後端設定）<br>
                Chunk size：${d.chunk_size ?? 600}<br>
                Overlap：${d.chunk_overlap ?? 120}<br>
                Top K（rag_k）：${d.rag_k ?? 4}<br>
                Rerank threshold：${d.rag_rerank_threshold ?? 0.2}
            </div></div></div>`;
    } catch (e) {
        el.innerHTML = '<div class="mm-note">無法載入設定（後端未連線）。</div>';
    }
}
async function runRetrievalTest() {
    const q = document.getElementById('kb-test-query').value.trim();
    const out = document.getElementById('kb-test-results');
    if (!q) { showToast('請先輸入測試問題', 'info'); return; }
    const progressId = `retrieval-test-${Date.now()}`;
    out.innerHTML = '<div class="mm-note">檢索中...</div>';
    updateTaskProgress(progressId, { label: '測試知識庫檢索', detail: '正在向量化問題並搜尋索引', mode: 'indeterminate', value: null });
    try {
        const res = await removedBasicFeature('Knowledge retrieval test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: q, top_k: 5, rerank: true })
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const results = data.results || data.sources || [];
        if (!results.length) {
            out.innerHTML = '<div class="mm-note">沒有檢索到相關片段。</div>';
            finishTaskProgress(progressId, 'completed', '檢索完成，沒有相符片段');
            return;
        }
        out.innerHTML = results.map((r, i) => `
            <div class="kb-source-hit">
                <div class="hit-head"><span>${i + 1}. ${escapeHtml(r.source || '')}${r.page ? ` p.${r.page}` : ''}</span><span class="hit-score">${formatSourceScore(r, '')}</span></div>
                <div class="hit-text">${escapeHtml((r.content || '').slice(0, 200))}...</div>
            </div>`).join('');
        finishTaskProgress(progressId, 'completed', `檢索完成，共 ${results.length} 個結果`);
    } catch (e) {
        out.textContent = `檢索測試不可用：${e.message || 'Basic Chat mode'}`;
        finishTaskProgress(progressId, 'failed', e.message || '檢索測試失敗');
    }
}

// ---- First-run Setup Wizard（P4）----
async function evaluateFirstRun(status) {
    let firstRunDone = false;
    try { firstRunDone = localStorage.getItem('wb-first-run-done') === '1'; } catch (e) {}
    const backendOk = !!(status && (
        status.status === 'ok' ||
        status.backend === 'running' ||
        (status.backend && status.backend.status === 'ok')
    ));
    const ollamaOk = getOllamaConnectionStatus(status) === 'connected';
    const hasModel = !!modelSelect.value;
    if (!firstRunDone || !backendOk || !ollamaOk || !hasModel) {
        openWizard();
    }
}
function setWizardCheck(id, state, text) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = `wizard-check ${state}`;
    el.querySelector('.wz-mark').textContent = state === 'ok' ? '✓' : (state === 'warn' ? '⚠' : (state === 'err' ? '✗' : '○'));
    el.querySelector('span:last-child').textContent = text;
}
async function refreshWizardChecks() {
    const hint = document.getElementById('wizard-hint');
    let backendOk = false, ollamaOk = false;
    try {
        const res = await apiFetch(`${API_BASE}/api/status`);
        const d = await res.json();
        backendOk = true;
        ollamaOk = getOllamaConnectionStatus(d) === 'connected';
    } catch (e) {}
    setWizardCheck('wz-backend', backendOk ? 'ok' : 'err', `後端服務：${backendOk ? '正常' : '未連線（請啟動 FastAPI 後端）'}`);
    setWizardCheck('wz-ollama', ollamaOk ? 'ok' : 'warn', `Ollama：${ollamaOk ? '已連線' : '未連線'}`);
    let hasModel = false;
    if (ollamaOk) {
        try {
            const res = await apiFetch(`${API_BASE}/api/models`);
            const d = await res.json();
            hasModel = !!(d.models && d.models.length);
            setWizardCheck('wz-models', hasModel ? 'ok' : 'warn', hasModel ? `模型：已安裝 ${d.models.length} 個` : '模型：尚未安裝');
        } catch (e) { setWizardCheck('wz-models', 'warn', '模型：無法檢查'); }
    } else {
        setWizardCheck('wz-models', 'warn', '模型：待 Ollama 連線後檢查');
    }
    if (BASIC_CHAT_MODE) return configureBasicWizard(hint, backendOk, ollamaOk, hasModel);
    setWizardCheck('wz-kb', kbStatus.index_status === 'ready' ? 'ok' : 'warn',
        kbStatus.index_status === 'ready' ? `知識庫：${kbStatus.document_count} 文件已索引` : '知識庫：空白（可稍後上傳）');
    if (hint) {
        if (!backendOk) hint.textContent = '請先在終端機啟動後端（uvicorn app:app）與 Ollama，然後按「重新檢查」。';
        else if (!ollamaOk) hint.textContent = 'Ollama 未啟動：請執行 ollama serve，或在「連接既有 Ollama」設定遠端位址。';
        else if (!hasModel) hint.textContent = '尚未安裝本地模型。按「安裝推薦模型」，系統會根據你的硬體推薦可運行模型。';
        else hint.textContent = '環境就緒！可以直接開始對話，或上傳文件建立知識庫。';
    }
}
function openWizard() {
    document.getElementById('setup-wizard-modal').classList.add('active');
    refreshWizardChecks();
}
function closeWizard() {
    document.getElementById('setup-wizard-modal').classList.remove('active');
    try { localStorage.setItem('wb-first-run-done', '1'); } catch (e) {}
}

// ---- Command Palette（P12）----
const PALETTE_ACTIONS = [
    { label: '上傳文件（知識庫）', icon: 'upload-cloud', run: () => openKnowledgeCenter('documents') },
    { label: '切換模型', icon: 'box', run: () => openModelManager('installed') },
    { label: '安裝模型', icon: 'download', run: () => openModelManager('recommended') },
    { label: '管理雲端 LLM API', icon: 'cloud-cog', run: () => window.workbenchCloudLlm?.open() },
    { label: '開啟 Knowledge Center', icon: 'book-open', run: () => openKnowledgeCenter('documents') },
    { label: '執行檢索測試', icon: 'search', run: () => openKnowledgeCenter('retrieval') },
    { label: '開啟 Artifact 工作區', icon: 'code-xml', run: () => { activateChatForAuxiliaryPanel(); openInspector('artifact'); } },
    { label: '開新任務（新對話）', icon: 'plus', run: () => createNewSession() },
    { label: '清空知識庫', icon: 'trash-2', run: () => confirmModal.classList.add('active') },
    { label: '切換淺色 / 深色主題', icon: 'moon', run: () => document.getElementById('btn-theme-toggle').click() },
    { label: '開啟擴充中心', icon: 'puzzle', run: () => window.workbenchExtensions?.open('installed') },
    { label: '開啟設定中心', icon: 'sliders', run: () => document.getElementById('btn-settings-trigger').click() },
    { label: '執行模型測速', icon: 'gauge', run: () => { openModelManager('benchmark'); } }
];
let paletteIndex = 0;
function openPalette() {
    const overlay = document.getElementById('command-palette');
    overlay.classList.add('active');
    const input = document.getElementById('palette-input');
    input.value = '';
    renderPalette('');
    setTimeout(() => input.focus(), 30);
}
function closePalette() { document.getElementById('command-palette').classList.remove('active'); }
function renderPalette(q) {
    const list = document.getElementById('palette-list');
    const actions = BASIC_CHAT_MODE ? basicPaletteActions(PALETTE_ACTIONS) : PALETTE_ACTIONS;
    const items = actions.filter(a => !q || a.label.toLowerCase().includes(q.toLowerCase()));
    paletteIndex = 0;
    list.innerHTML = '';
    if (!items.length) { list.innerHTML = '<div class="palette-empty">找不到指令</div>'; return; }
    items.forEach((a, i) => {
        const el = document.createElement('div');
        el.className = `palette-item ${i === 0 ? 'selected' : ''}`;
        el.innerHTML = `<i data-lucide="${a.icon}"></i><span>${a.label}</span>`;
        el.addEventListener('click', () => { closePalette(); a.run(); });
        list.appendChild(el);
    });
    safeCreateIcons();
    list._items = items;
}

// ---- A11y：ESC 關閉 + Modal focus trap（P14）----
function topmostOverlay() {
    if (document.getElementById('command-palette').classList.contains('active')) return document.getElementById('command-palette');
    const overlays = [...document.querySelectorAll('.modal-overlay.active')];
    return overlays.length ? overlays[overlays.length - 1] : null;
}
function initA11y() {
    document.addEventListener('keydown', (e) => {
        // Ctrl/Cmd + K：Command Palette
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
            e.preventDefault();
            window.workbenchExtensions?.closePermissionReview?.({ restoreFocus: false });
            openPalette();
            return;
        }
        const top = topmostOverlay();
        if (e.key === 'Escape') {
            if (top) {
                e.preventDefault();
                if (top.id === 'command-palette') closePalette();
                else if (top.id === 'setup-wizard-modal') closeWizard();
                else if (top.id === 'extension-permission-modal') {
                    window.workbenchExtensions?.closePermissionReview?.();
                }
                else top.classList.remove('active');
            } else if (primaryWorkspace === 'extensions') {
                e.preventDefault();
                window.workbenchExtensions?.close?.();
            } else if (primaryWorkspace === 'models') {
                e.preventDefault();
                closeModelManager();
            } else if (primaryWorkspace === 'cloud') {
                e.preventDefault();
                window.workbenchCloudLlm?.close?.();
            } else if (window.workbenchRunInspector?.isOpen?.()) {
                e.preventDefault();
                setOutputFloatingPanelOpen(false, { restoreFocus: true });
            } else if (agentCollaborationPanel && !agentCollaborationPanel.hidden) {
                e.preventDefault();
                closeAgentCollaboration(true);
                document.getElementById('rail-agents')?.focus();
            } else if (artifactsSandboxPanel.classList.contains('active')) {
                e.preventDefault();
                closeInspectorPanel();
                (btnSandboxToggle || document.getElementById('rail-artifacts'))?.focus?.();
            } else if (
                window.matchMedia('(max-width: 900px)').matches
                && !document.getElementById('chat-drawer')?.classList.contains('collapsed')
            ) {
                e.preventDefault();
                collapseCompactChatDrawer({ focusTarget: document.getElementById('rail-chat') });
            }
            return;
        }
        // 簡易 focus trap：Tab 循環於最上層 modal 內
        if (e.key === 'Tab' && top) {
            const focusables = [...top.querySelectorAll(
                'button, input, textarea, select, [tabindex]:not([tabindex="-1"])'
            )].filter(element => (
                !element.disabled
                && !element.hidden
                && element.getAttribute('aria-hidden') !== 'true'
                && element.getClientRects().length > 0
            ));
            if (!focusables.length) return;
            const first = focusables[0], last = focusables[focusables.length - 1];
            if (!focusables.includes(document.activeElement)) {
                e.preventDefault();
                (e.shiftKey ? last : first).focus();
            } else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
            else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
        // Palette 鍵盤導覽
        if (document.getElementById('command-palette').classList.contains('active')) {
            const list = document.getElementById('palette-list');
            const items = list.querySelectorAll('.palette-item');
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault();
                paletteIndex = (paletteIndex + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length;
                items.forEach((it, i) => it.classList.toggle('selected', i === paletteIndex));
            } else if (e.key === 'Enter' && list._items && list._items[paletteIndex]) {
                e.preventDefault();
                const act = list._items[paletteIndex];
                closePalette(); act.run();
            }
        }
    });
}

let primaryWorkspace = 'chat';
let runInspectorSuspendedWorkspace = null;

function activateChatForAuxiliaryPanel() {
    if (primaryWorkspace !== 'chat') setPrimaryWorkspace('chat');
}

function setPrimaryWorkspace(workspace = 'chat') {
    const supportedWorkspaces = new Set(['chat', 'workflows', 'extensions', 'models', 'cloud']);
    const nextWorkspace = supportedWorkspaces.has(workspace) ? workspace : 'chat';
    const workflowMode = nextWorkspace === 'workflows';
    const extensionMode = nextWorkspace === 'extensions';
    const modelMode = nextWorkspace === 'models';
    const cloudMode = nextWorkspace === 'cloud';
    const managementMode = extensionMode || modelMode || cloudMode;
    const chatWorkspace = document.querySelector('main.chat-container');
    const workflowCenter = document.getElementById('n8n-workflow-center');
    const extensionCenter = document.getElementById('extension-center-workspace');
    const modelCenter = document.getElementById('model-manager-workspace');
    const cloudCenter = document.getElementById('cloud-llm-workspace');
    const drawer = document.getElementById('chat-drawer');
    const railChat = document.getElementById('rail-chat');
    const railWorkflows = document.getElementById('rail-workflows');
    const railExtensions = document.getElementById('rail-extensions');
    const railModels = document.getElementById('rail-models');
    const railCloud = document.getElementById('rail-cloud-llm');
    if (!chatWorkspace || !workflowCenter || !extensionCenter || !modelCenter || !cloudCenter || !drawer
        || !railChat || !railWorkflows || !railExtensions || !railModels || !railCloud) return;

    const previousWorkspace = primaryWorkspace;
    const previousManagementMode = ['extensions', 'models', 'cloud'].includes(previousWorkspace);
    if (previousWorkspace === 'cloud' && nextWorkspace !== 'cloud' && !cloudCenter.hidden) {
        void window.workbenchCloudLlm?.deactivate?.();
    }
    primaryWorkspace = nextWorkspace;
    if (managementMode && !previousManagementMode) {
        runInspectorSuspendedWorkspace = previousWorkspace;
    }
    const activeManagementRail = extensionMode ? railExtensions : (modelMode ? railModels : railCloud);
    window.workbenchRunInspector?.setAvailable?.(!managementMode, {
        focusTarget: managementMode ? activeManagementRail : null,
    });
    const returningToSuspendedWorkspace = previousManagementMode
        && runInspectorSuspendedWorkspace === primaryWorkspace;
    if (workflowMode && !returningToSuspendedWorkspace) setOutputFloatingPanelOpen(false);
    if (!managementMode) runInspectorSuspendedWorkspace = null;
    if (managementMode) setTaskProgressCollapsed(true);

    chatWorkspace.hidden = nextWorkspace !== 'chat';
    workflowCenter.hidden = !workflowMode;
    extensionCenter.hidden = !extensionMode;
    modelCenter.hidden = !modelMode;
    cloudCenter.hidden = !cloudMode;
    drawer.hidden = nextWorkspace !== 'chat';
    syncChatDrawerA11y(drawer);
    const workspaceRails = new Map([
        ['chat', railChat],
        ['workflows', railWorkflows],
        ['extensions', railExtensions],
        ['models', railModels],
        ['cloud', railCloud],
    ]);
    workspaceRails.forEach((rail, name) => {
        const active = name === nextWorkspace;
        rail.classList.toggle('active', active);
        rail.setAttribute('aria-current', active ? 'page' : 'false');
    });
    if (window.matchMedia('(max-width: 640px)').matches) {
        workspaceRails.get(nextWorkspace)?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
    }

    if (nextWorkspace !== 'chat') {
        closeInspectorPanel();
        closeAgentCollaboration(true);
        if (managementMode) {
            window.workbenchN8nWorkflows?.close?.();
            window.workbenchN8nGovernance?.releaseInspectorContext?.();
            window.workbenchN8nWorkflows?.useChatInspectorContext?.({ open: false });
        }
        return;
    }

    if (
        window.matchMedia('(max-width: 900px)').matches
        && !drawer.classList.contains('collapsed')
        && window.workbenchRunInspector?.isOpen?.()
    ) {
        setOutputFloatingPanelOpen(false);
    }

    window.workbenchN8nWorkflows?.close?.();
    window.workbenchN8nGovernance?.releaseInspectorContext?.();
    window.workbenchN8nWorkflows?.useChatInspectorContext?.({ open: false });
}

// ---- Workbench 初始化 ----
function initWorkbench(status) {
    const workbenchBody = document.querySelector('.workbench-body');
    const modelWorkspace = document.getElementById('model-manager-workspace');
    if (workbenchBody && modelWorkspace && modelWorkspace.parentElement !== workbenchBody) {
        workbenchBody.appendChild(modelWorkspace);
    }
    window.workbenchExtensions?.init({
        apiFetch,
        apiBase: API_BASE,
        showToast,
        openFolderBrowser,
        getProjects: () => sidebarProjects,
        getActiveProjectId: () => activeProjectId,
        reloadProject: () => loadSessions(searchSessionsInput.value.trim()),
        onWorkspaceOpen: () => setPrimaryWorkspace('extensions'),
        onWorkspaceClose: () => {
            setPrimaryWorkspace('chat');
            document.getElementById('rail-chat')?.focus();
        }
    });
    window.workbenchConnectors?.init({
        apiFetch,
        apiBase: API_BASE,
        showToast,
        getProjects: () => sidebarProjects,
        getActiveProjectId: () => activeProjectId
    });
    window.workbenchCloudLlm?.init({
        collectProviders: collectModelProviders, providerCard: modelProviderCard,
        nextProviderId: nextModelProviderId, apiFetch, apiBase: API_BASE,
        getSettings: () => currentSettings,
        setSettings: settings => { currentSettings = settings; },
        saveSecrets: saveModelProviderSecrets, loadProviders: loadModelProviderSettings,
        refreshModels: loadModels, showToast,
        reloadProviders: () => loadModelProviderSettings(currentSettings.model_providers || []),
        getSecretStatus: () => modelProviderSecretStatus,
        inferModelKind: inferredProviderModelKind,
        createIcons: safeCreateIcons,
        onWorkspaceOpen: () => setPrimaryWorkspace('cloud'),
        onWorkspaceClose: () => {
            setPrimaryWorkspace('chat');
            document.getElementById('rail-chat')?.focus();
        }
    });
    window.workbenchN8nWorkflows?.init({
        apiFetch,
        apiBase: API_BASE,
        showToast,
        createIcons: safeCreateIcons,
        getProjects: () => sidebarProjects,
        getActiveProjectId: () => activeProjectId,
        getModels: () => Array.from(modelSelect.options)
            .filter(option => option.value)
            .map(option => ({ value: option.value, label: option.textContent || option.value })),
        onWorkspaceOpen: () => setPrimaryWorkspace('workflows')
    });
    window.workbenchN8nGovernance?.init({
        apiFetch,
        apiBase: API_BASE,
        showToast,
        createIcons: safeCreateIcons,
        getProjects: () => sidebarProjects,
        getSessions: () => sidebarSessions,
        getActiveProjectId: () => activeProjectId,
        getCurrentSessionId: () => currentSessionId,
        refreshWorkspaceScope: () => loadSessions(searchSessionsInput.value.trim()),
    });
    // Rail 導覽
    const drawer = document.getElementById('chat-drawer');
    document.getElementById('rail-chat').addEventListener('click', () => {
        setPrimaryWorkspace('chat');
        if (window.matchMedia('(max-width: 900px)').matches) {
            setOutputFloatingPanelOpen(false);
            closeInspectorPanel();
            closeAgentCollaboration(true);
        }
        drawer.classList.remove('collapsed');
        syncChatDrawerA11y(drawer);
        document.getElementById('rail-chat').classList.add('active');
    });
    document.getElementById('rail-workflows').addEventListener('click', () => window.workbenchN8nWorkflows?.open?.());
    document.getElementById('chat-drawer-close').addEventListener('click', () => {
        if (window.matchMedia('(max-width: 900px)').matches) {
            drawer.classList.add('collapsed');
            syncChatDrawerA11y(drawer);
            return;
        }
        drawer.classList.remove('collapsed');
        syncChatDrawerA11y(drawer);
        document.getElementById('rail-chat').classList.add('active');
    });
    document.getElementById('rail-knowledge').addEventListener('click', () => openKnowledgeCenter('documents'));
    document.getElementById('rail-runs').addEventListener('click', () => {
        activateChatForAuxiliaryPanel();
        openInspector('run');
    });
    document.getElementById('rail-artifacts').addEventListener('click', () => {
        if (artifactsSandboxPanel.classList.contains('active') && document.getElementById('inspector-pane-artifact').classList.contains('active')) {
            closeInspectorPanel();
        } else {
            activateChatForAuxiliaryPanel();
            openInspector('artifact');
        }
    });
    document.getElementById('rail-models').addEventListener('click', () => openModelManager('installed'));
    document.getElementById('rail-cloud-llm').addEventListener('click', () => window.workbenchCloudLlm?.open());
    document.getElementById('rail-extensions').addEventListener('click', () => window.workbenchExtensions?.open('installed'));
    document.getElementById('rail-settings').addEventListener('click', () => document.getElementById('btn-settings-trigger').click());
    setPrimaryWorkspace('chat');

    // Top Bar chips 點擊行為（P3.3）
    document.getElementById('chip-model').addEventListener('click', () => openModelManager('installed'));
    document.getElementById('chip-rag').addEventListener('click', () => openKnowledgeCenter('documents'));
    document.getElementById('chip-docs').addEventListener('click', () => openKnowledgeCenter('documents'));
    document.getElementById('chip-speed').addEventListener('click', () => { activateChatForAuxiliaryPanel(); openInspector('models'); });
    document.getElementById('chip-ctx').addEventListener('click', () => { activateChatForAuxiliaryPanel(); openInspector('context'); });
    const setupWizardTrigger = document.getElementById('btn-setup-wizard-trigger');
    if (setupWizardTrigger) setupWizardTrigger.addEventListener('click', openWizard);

    // Inspector tabs
    document.querySelectorAll('.inspector-tab').forEach(t =>
        t.addEventListener('click', () => openInspector(t.dataset.itab)));

    if (BASIC_CHAT_MODE) configureBasicChatComposerUi();
    else { ragToggle.checked = true; userInput.placeholder = '詢問文件內容，或輸入一般問題...（系統會自動判斷處理方式）'; }
    updateRagChip();

    // Model Manager 事件
    document.getElementById('mm-close').addEventListener('click', () => closeModelManager());
    document.getElementById('mm-close-btn').addEventListener('click', () => closeModelManager());
    document.getElementById('mm-refresh').addEventListener('click', async () => {
        await Promise.all([loadModels(), loadModelCatalog(true), syncModelInstallJobs()]);
        renderMmInstalled();
        renderMmRecommended();
        renderMmAvailable(
            document.getElementById('mm-search')?.value || '',
            document.getElementById('mm-category-filter')?.value || ''
        );
        showToast('已重新整理模型清單', 'success');
    });
    const modelTabs = [...document.querySelectorAll('.mm-tab[data-mmtab]')];
    modelTabs.forEach(tab => {
        tab.addEventListener('click', () => switchMmTab(tab.dataset.mmtab));
        tab.addEventListener('keydown', event => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault();
            const current = modelTabs.indexOf(tab);
            const next = event.key === 'Home' ? 0
                : event.key === 'End' ? modelTabs.length - 1
                    : (current + (event.key === 'ArrowRight' ? 1 : modelTabs.length - 1)) % modelTabs.length;
            switchMmTab(modelTabs[next].dataset.mmtab);
            modelTabs[next].focus();
        });
    });
    document.getElementById('mm-search').addEventListener('input', (event) => renderMmAvailable(
        event.target.value,
        document.getElementById('mm-category-filter')?.value || ''
    ));
    document.getElementById('mm-category-filter').addEventListener('change', (event) => renderMmAvailable(
        document.getElementById('mm-search')?.value || '',
        event.target.value
    ));
    document.getElementById('mm-custom-install-btn').addEventListener('click', installCustomOllamaModel);
    document.getElementById('mm-custom-model').addEventListener('keydown', event => {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        void installCustomOllamaModel();
    });
    document.getElementById('mm-run-benchmark').addEventListener('click', () => runBenchmark());
    // 模型切換確認
    document.getElementById('ms-apply-session').addEventListener('click', () => applyModelSwitch(false));
    document.getElementById('ms-apply-default').addEventListener('click', () => applyModelSwitch(true));
    document.getElementById('ms-cancel').addEventListener('click', () => { document.getElementById('model-switch-modal').classList.remove('active'); pendingSwitchModel = null; });
    document.getElementById('ms-close').addEventListener('click', () => { document.getElementById('model-switch-modal').classList.remove('active'); pendingSwitchModel = null; });

    // Knowledge Center tabs
    document.querySelectorAll('.mm-tab[data-kbtab]').forEach(t => t.addEventListener('click', () => {
        document.querySelectorAll('.mm-tab[data-kbtab]').forEach(x => x.classList.toggle('active', x === t));
        document.querySelectorAll('.kb-pane').forEach(p => p.classList.remove('active'));
        document.getElementById(`kb-pane-${t.dataset.kbtab}`).classList.add('active');
        if (t.dataset.kbtab === 'index') renderKbIndexSettings();
    }));
    document.getElementById('kb-test-run').addEventListener('click', runRetrievalTest);
    document.getElementById('kb-test-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') runRetrievalTest(); });

    // Wizard 事件
    document.getElementById('wizard-close').addEventListener('click', closeWizard);
    document.getElementById('wizard-later').addEventListener('click', closeWizard);
    document.getElementById('wizard-recheck').addEventListener('click', refreshWizardChecks);
    document.getElementById('wizard-open-settings').addEventListener('click', () => { closeWizard(); document.getElementById('btn-settings-trigger').click(); });
    document.getElementById('wizard-install-model').addEventListener('click', () => { closeWizard(); openModelManager('recommended'); });

    // Command Palette
    document.getElementById('palette-input').addEventListener('input', (e) => renderPalette(e.target.value));
    document.getElementById('command-palette').addEventListener('click', (e) => { if (e.target.id === 'command-palette') closePalette(); });

    // RAG 開關同步（隱藏 checkbox 仍是單一真相來源）
    ragToggle.addEventListener('change', updateRagChip);

    initA11y();
    updateRagChip();
    updateCtxChip();
    updateDocsChip();
    updateWelcomeDashboard();

    // First-run 檢查（P4.1）
    evaluateFirstRun(status);
}
