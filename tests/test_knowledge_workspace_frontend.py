from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
BASIC = (ROOT / "frontend" / "basic-chat-mode.js").read_text(encoding="utf-8")
STYLE = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")


def _section(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end, source.index(start))]


def test_knowledge_center_is_a_primary_workspace_not_a_modal():
    workspace = _section(HTML, '<main class="knowledge-workspace"', "<!-- 彈出確認 Modal")

    assert 'id="knowledge-workspace"' in workspace
    assert 'aria-labelledby="knowledge-workspace-title"' in workspace
    assert 'role="dialog"' not in workspace
    assert 'aria-modal="true"' not in workspace
    assert 'id="kb-project-required"' in workspace
    assert '請先選擇專案' in workspace
    assert 'data-kbtab="documents"' in workspace and ">文件</button>" in workspace
    assert 'data-kbtab="retrieval"' in workspace and ">檢索測試</button>" in workspace
    assert 'data-kbtab="index"' in workspace and ">索引狀態</button>" in workspace
    assert 'id="kb-chat-retrieval-toggle"' in workspace
    assert 'id="upload-zone" role="button" tabindex="0"' in workspace
    assert 'aria-controls="file-input"' in workspace
    assert "uploadZone.addEventListener('keydown'" in APP


def test_primary_workspace_switcher_includes_knowledge_and_rail_state():
    switcher = _section(APP, "function setPrimaryWorkspace", "// ---- Workbench 初始化")

    assert "'knowledge'" in switcher
    assert "document.getElementById('knowledge-workspace')" in switcher
    assert "knowledgeCenter.hidden = !knowledgeMode" in switcher
    assert "['knowledge', railKnowledge]" in switcher
    assert "managementMode = knowledgeMode" in switcher
    assert 'aria-controls="knowledge-workspace"' in HTML
    assert "workbenchBody.appendChild(knowledgeWorkspace)" in APP


def test_every_knowledge_action_uses_the_project_scoped_api():
    knowledge = _section(APP, "function setKnowledgeProjectAvailability", "// ==========================================================================\n// 4.")

    assert "/api/knowledge/documents?project_id=${encodeURIComponent(projectId)}" in knowledge
    assert "/api/knowledge/documents/${encodeURIComponent(documentId)}?project_id=${encodeURIComponent(projectId)}" in knowledge
    assert "/api/knowledge/documents/${encodeURIComponent(documentId)}/chunks?project_id=${encodeURIComponent(projectId)}" in knowledge
    assert "formData.append('project_id', projectId)" in knowledge
    assert "/api/knowledge/documents`" in knowledge
    assert "/api/knowledge?project_id=${encodeURIComponent(projectId)}" in knowledge
    assert "removedBasicFeature('Knowledge" not in APP
    assert "Knowledge upload is not available in Basic Chat mode" not in APP
    assert "activeProjectId = currentSession?.project_id || null" in APP
    assert "activeProjectId = null;\n        loadKnowledgeRetrievalPreference(null);" in APP


def test_retrieval_test_and_status_use_real_knowledge_endpoints():
    workspace = _section(APP, "// ---- 專案知識庫工作區 ----", "// ---- First-run Setup Wizard")

    assert "/api/knowledge/status?project_id=${encodeURIComponent(projectId)}" in workspace
    assert "/api/knowledge/retrieve" in workspace
    assert "project_id: projectId" in workspace
    assert "candidate_limit: 40" in workspace
    assert "data.results" in workspace
    assert "r.citation?.source_id" in workspace
    assert "knowledgeRequestRevisions.index" in workspace
    assert "knowledgeRequestRevisions.retrieval" in workspace
    assert "max_chunk_count" in workspace
    assert "current_adapter_chunk_count" in workspace
    assert "reindex_required" in workspace
    assert "Embedding 已變更，需重新匯入／重建索引" in workspace
    assert "重排失敗時會自動改用 Embedding 相似度排序" in workspace
    assert "重排失敗時會停止本次檢索" in workspace
    assert "data.truncated" in APP
    assert "data.total_chunks" in APP


def test_cloud_semantic_knowledge_operations_require_explicit_project_consent():
    knowledge = _section(
        APP,
        "async function knowledgeResponse",
        "function formatKnowledgeBytes",
    )
    upload = _section(APP, "async function handleFilesSelect", "async function clearRagIndex")
    retrieval = _section(APP, "async function runRetrievalTest", "// ---- First-run Setup Wizard")

    assert "MODEL_DATA_CONSENT_REQUIRED" in knowledge
    assert "requestModelDataConsent" in knowledge
    assert "remember_project: choice === 'remember'" in knowledge
    assert "尚未傳送文件" in knowledge
    assert "formData.append('run_id', knowledgeRunId)" in upload
    assert "formData.append('consent_proposal_id', consentProposalId)" in upload
    assert "consentProposalId = await approveKnowledgeDataConsent(error)" in upload
    assert "run_id: knowledgeRunId" in retrieval
    assert "consent_proposal_id: consentProposalId" in retrieval


def test_basic_chat_chip_reports_project_knowledge_and_keeps_a_toggle():
    renderer = _section(BASIC, "function renderBasicChatModeChip", "function configureBasicChatComposerUi")

    assert "專案知識：" in renderer
    assert "'開'" in renderer and "'關'" in renderer
    assert "專案知識：未選專案" in renderer
    assert "基本聊天" not in renderer
    assert "kbChatRetrievalToggle?.addEventListener('change'" in APP
    assert "saveKnowledgeRetrievalPreference" in APP
    assert "workbench-project-knowledge:" in APP
    assert "use_rag: !!activeProjectId && ragToggle.checked" in APP


def test_basic_chat_command_palette_keeps_project_knowledge_actions():
    palette = _section(BASIC, "function basicPaletteActions", "}")

    assert "上傳文件（知識庫）" in palette
    assert "開啟知識庫工作區" in palette
    assert "執行檢索測試" in palette


def test_knowledge_workspace_has_bounded_responsive_layout_css():
    assert ".knowledge-workspace[hidden]" in STYLE
    assert ".knowledge-workspace," in STYLE
    assert ".knowledge-workspace-body" in STYLE
    assert "overflow-y: auto" in STYLE
    assert ".knowledge-project-required[hidden]" in STYLE
    assert ".knowledge-runtime-toggle" in STYLE
    assert "@media (max-width: 720px)" in STYLE


def test_management_workspaces_hide_global_floating_progress():
    renderer = _section(APP, "function renderTaskProgress", "function setTaskProgressCollapsed")
    switcher = _section(APP, "function setPrimaryWorkspace", "// ---- Workbench 初始化")

    assert "managementWorkspaceOpen" in renderer
    assert "items.length === 0 || managementWorkspaceOpen" in renderer
    assert "renderTaskProgress();" in switcher


def test_knowledge_and_sidebar_requests_ignore_stale_responses():
    sessions = _section(APP, "async function loadSessions", "function matchesSidebarSearch")
    knowledge = _section(APP, "async function loadRagStatus", "function renderKbStatusLine")

    assert "++sessionLoadRevision" in sessions
    assert "requestRevision !== sessionLoadRevision" in sessions
    assert "++knowledgeRequestRevisions.status" in knowledge
    assert "requestRevision !== knowledgeRequestRevisions.status" in knowledge


def test_visible_knowledge_copy_is_traditional_chinese():
    workspace = _section(HTML, '<main class="knowledge-workspace"', "<!-- 彈出確認 Modal")

    assert "Knowledge Center" not in workspace
    assert ">Documents<" not in workspace
    assert ">Retrieval Test<" not in workspace
    assert ">Index Settings<" not in workspace
    assert "Chunks 預覽" not in workspace
    assert "清空整個知識庫" not in workspace
    assert "目前 Embedding 可用片段" in APP
    assert "索引需重建" in APP
