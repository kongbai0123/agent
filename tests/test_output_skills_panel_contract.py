from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
BASIC_MODE_JS = (FRONTEND / "basic-chat-mode.js").read_text(encoding="utf-8")
INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
PROJECT_SKILLS_JS = (FRONTEND / "project-skills-sidebar.js").read_text(encoding="utf-8")
RUN_INSPECTOR_JS = (FRONTEND / "run-inspector.js").read_text(encoding="utf-8")
STYLE_CSS = (FRONTEND / "style.css").read_text(encoding="utf-8")


def _function_slice(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index:source.index(end, start_index)]


def test_output_is_a_standalone_floating_panel_not_an_inspector_or_rail_item():
    assert 'id="output-floating-workspace"' in INDEX_HTML
    assert 'id="output-floating-panel"' in INDEX_HTML
    assert 'class="output-floating-panel"' in INDEX_HTML
    assert 'role="tabpanel"' in INDEX_HTML
    assert 'class="output-floating-tabs"' in INDEX_HTML
    assert 'role="tablist"' in INDEX_HTML
    assert 'aria-orientation="vertical"' in INDEX_HTML
    for name in ("skills", "execution", "results"):
        assert f'id="output-tab-{name}"' in INDEX_HTML
        assert f'id="output-pane-{name}"' in INDEX_HTML
        assert f'aria-controls="output-pane-{name}"' in INDEX_HTML
        assert f'aria-labelledby="output-tab-{name}"' in INDEX_HTML
    assert INDEX_HTML.count('role="tab"') >= 3
    assert 'id="rail-output"' not in INDEX_HTML
    assert 'id="inspector-tab-output"' not in INDEX_HTML
    assert 'id="inspector-pane-output"' not in INDEX_HTML
    assert 'id="output-panel-close"' not in INDEX_HTML
    assert INDEX_HTML.count('id="artifacts-sandbox-panel"') == 1

    inspector_start = INDEX_HTML.index('id="artifacts-sandbox-panel"')
    inspector_end = INDEX_HTML.index('</aside>', inspector_start)
    inspector = INDEX_HTML[inspector_start:inspector_end]
    assert 'output-floating-panel' not in inspector
    assert 'output-skills-mount' not in inspector


def test_floating_output_uses_its_own_vertical_tab_toggle():
    assert "openInspector('output')" not in APP_JS
    assert "rail-output" not in APP_JS
    assert "output-mode" not in APP_JS
    assert "outputPanelClose" not in APP_JS
    assert "const TAB_ORDER = ['skills', 'execution', 'results']" in RUN_INSPECTOR_JS
    assert "aria-orientation=\"vertical\"" in INDEX_HTML
    assert "state.activeTab === name && state.expanded" in RUN_INSPECTOR_JS
    assert "if (toggle && state.activeTab === name && state.expanded)" in RUN_INSPECTOR_JS
    for key in ("ArrowDown", "ArrowUp", "Home", "End", "Enter"):
        assert key in RUN_INSPECTOR_JS
    assert "app.js?v=0.8.0-n8n-graph-authoring-beta.1" in INDEX_HTML
    assert "run-inspector.js?v=1.0.0" in INDEX_HTML


def test_output_skills_own_the_project_scoped_renderer_and_cache():
    render = _function_slice(
        APP_JS,
        "function renderOutputSkillsPane",
        "function clearOutputSkillsContext",
    )
    assert "window.workbenchProjectSkills?.createProjectSection(project" in render
    assert "alwaysExpanded: true" in render
    assert "autoLoad: true" in render
    assert "/api/skills" not in render
    assert "workbenchSkills" not in render
    assert APP_JS.count("window.workbenchProjectSkills?.createProjectSection(project") == 1

    project_block = _function_slice(APP_JS, "function createProjectBlock", "function createSessionRow")
    assert "workbenchProjectSkills" not in project_block
    assert "project-skills" not in project_block

    assert "const requestId = ++record.requestId" in PROJECT_SKILLS_JS
    assert "requestId !== record.requestId" in PROJECT_SKILLS_JS
    assert "requestId !== state.session.requestId" in PROJECT_SKILLS_JS
    assert "sessionId !== state.session.sessionId" in PROJECT_SKILLS_JS
    assert "projectId !== state.session.projectId" in PROJECT_SKILLS_JS


def test_project_switch_clears_old_skills_before_loading_the_next_context():
    assert "clearOutputSkillsContext();" in APP_JS
    assert "setSessionContext({ sessionId: null, projectId: null })" in APP_JS
    load_sessions = _function_slice(APP_JS, "async function loadSessions", "function matchesSidebarSearch")
    assert "projectId: currentSession?.project_id || null" in load_sessions
    assert "renderOutputSkillsPane(currentSession?.project_id || null)" in load_sessions
    assert "syncRunInspectorContext(currentSession?.project_id || null" in load_sessions


def test_run_inspector_hydrates_authoritative_run_scoped_data_fail_closed():
    assert "/api/sessions/${encoded(sessionId)}/runs?limit=1" in RUN_INSPECTOR_JS
    assert "/api/runs/${encoded(runId)}/execution" in RUN_INSPECTOR_JS
    assert "/api/runs/${encoded(runId)}/results" in RUN_INSPECTOR_JS
    assert "/api/runs/${encoded(runId)}/skills" in RUN_INSPECTOR_JS
    assert "String(latest.session_id || '') !== String(state.context.sessionId || '')" in RUN_INSPECTOR_JS
    assert "String(latest.project_id || '') !== expectedProject" in RUN_INSPECTOR_JS
    assert "Skill 紀錄不屬於目前的 Project／Session／Run" in RUN_INSPECTOR_JS
    assert "requestId !== state.runRequestId" in RUN_INSPECTOR_JS
    latest = _function_slice(RUN_INSPECTOR_JS, "async function hydrateLatestRun", "async function hydrateWorkspaceVcs")
    assert "const expectedRunRequestId = state.runRequestId" in latest
    assert "expectedRunRequestId !== state.runRequestId" in latest
    assert "|| state.run" in latest
    assert "void hydrateSkills(state.run.runId)" in RUN_INSPECTOR_JS
    begin_run = _function_slice(RUN_INSPECTOR_JS, "function beginRun", "function normalizeEvent")
    assert "usedSkills = { status: 'loading'" in begin_run


def test_run_inspector_integrates_sse_approval_and_retry_without_raw_tool_arguments():
    assert "workbenchRunInspector?.handleEvent(eventType, eventData, streamIdentity)" in APP_JS
    assert "workbenchRunInspector.handleApproval(" in APP_JS
    assert "retry_of_run_id: retryOfRunId" in APP_JS
    assert "if (!retryOfRunId) {\n            addLLMMessage('user', sendQuestion" in APP_JS
    assert "args" not in _function_slice(RUN_INSPECTOR_JS, "function normalizeEvent", "function upsertAgent")
    assert "/api/chat/runs/${encoded(runId)}/approval" in RUN_INSPECTOR_JS
    assert "state.execution.retry?.allowed === true" in RUN_INSPECTOR_JS
    assert "cancelPendingApprovals" in APP_JS
    submit = _function_slice(APP_JS, "async function handleChatSubmit", "function appendMessage")
    assert "let userMessageAddedToConversation = false" in submit
    assert "userMessageAddedToConversation = true" in submit
    assert "userMessageAddedToConversation\n                && conversationState.length" in submit


def test_chat_stream_is_bound_to_the_session_project_and_run_that_started_it():
    assert "function streamEventMatches(identity, data = {})" in APP_JS
    assert "if (!streamEventMatches(streamIdentity, eventData)) continue;" in APP_JS
    assert "currentSessionId = eventData.session_id" not in APP_JS
    assert "if (isGenerating) await cancelActiveChatRun();" in _function_slice(
        APP_JS, "async function changeSession", "async function deleteSession"
    )


def test_shared_skill_menus_portal_above_floating_panel_and_are_mutually_exclusive():
    context_menu = _function_slice(APP_JS, "function openContextMenu", "function closeContextMenu")
    assert "document.body.appendChild(sidebarContextMenu)" in context_menu
    assert "workbenchProjectSkills?.closeMenus()" in context_menu
    assert "deps?.closeContextMenu?.()" in PROJECT_SKILLS_JS
    assert "document.body.appendChild(menu)" in PROJECT_SKILLS_JS


def test_basic_chat_keeps_output_visible_while_legacy_skill_controls_stay_hidden():
    configure = _function_slice(
        BASIC_MODE_JS,
        "function configureBasicChatComposerUi",
        "function useBasicKnowledgeStatus",
    )
    assert "'skills-button'" in configure
    assert "'active-skills-bar'" in configure
    assert "rail-output" not in configure
    assert '[data-itab="output"]' not in configure


def test_artifact_opening_leaves_output_mode_and_selects_artifact():
    show_artifact = _function_slice(
        APP_JS,
        "function showArtifactsPanel",
        "function renderVirtualFileTree",
    )
    assert "openInspector('artifact')" in show_artifact
    open_inspector = _function_slice(APP_JS, "function openInspector", "function renderContextPane")
    assert "inspectorPane.hidden = !selected" in open_inspector
    assert "inspectorTab.setAttribute('aria-selected'" in open_inspector
    assert "inspectorTab.tabIndex = selected ? 0 : -1" in open_inspector


def test_output_panel_has_compact_and_responsive_layout_contracts():
    for selector in (
        ".output-floating-workspace",
        ".output-floating-panel",
        ".output-floating-tabs",
        ".output-floating-tab",
        ".output-panel-card",
        ".output-panel-head",
        ".output-skills-mount .project-skills-section",
        ".output-panel-empty",
        ".output-panel-pane",
        ".run-inspector-section",
        ".output-tab-badge",
    ):
        assert selector in STYLE_CSS
    assert "overscroll-behavior: contain" in STYLE_CSS
    floating = STYLE_CSS[STYLE_CSS.index(".output-floating-workspace {"):STYLE_CSS.index(".output-floating-panel {")]
    assert "position: fixed" in floating
    assert "top: 86px" in floating
    assert "right: 0" in floating
    assert "z-index: 230" in floating
    tabs = STYLE_CSS[STYLE_CSS.index(".output-floating-tabs {"):STYLE_CSS.index(".output-floating-tab {")]
    assert "flex-direction: column" in tabs
    assert "@media (max-width: 900px)" in STYLE_CSS
    assert ".output-floating-workspace { top: 72px; }" in STYLE_CSS
    assert ".output-floating-panel { width: min(380px, calc(100vw - 58px));" in STYLE_CSS
    assert "@media (max-width: 640px)" in STYLE_CSS
    assert ".output-floating-workspace { top: 64px; }" in STYLE_CSS
    assert ".output-floating-panel { width: calc(100vw - 54px);" in STYLE_CSS
    assert "html.run-inspector-open .task-progress-center" in STYLE_CSS
    assert "html.run-inspector-open .output-floating-panel" in STYLE_CSS
    assert "beforeOpen: prepareRunInspectorOpen" in APP_JS
    assert "setOutputFloatingPanelOpen(false);" in _function_slice(
        APP_JS, "function openAgentCollaboration", "function closeAgentCollaboration"
    )
    assert "setOutputFloatingPanelOpen(false);" in _function_slice(
        APP_JS, "function openInspector", "function renderContextPane"
    )


def test_open_output_panel_reserves_chat_without_widening_the_reading_column():
    desktop = STYLE_CSS[
        STYLE_CSS.index("/* Run Inspector: reserve the reading surface instead of covering chat."):
        STYLE_CSS.index("/* A side-by-side inspector would leave an unusably narrow reading column")
    ]
    assert "@media (min-width: 901px)" in desktop
    assert "main.chat-container:not([hidden])" in desktop
    assert "min-width: 0" in desktop
    assert "margin-right: 62px" in desktop
    assert "margin-right: calc(clamp(320px, 24vw, 380px) + 62px)" in desktop

    # The inspector may reduce available space, but must not enlarge the
    # established answer or composer reading widths on wide screens.
    assert "body.basic-chat-mode .message {\n    max-width: 820px" in STYLE_CSS
    assert "body.basic-chat-mode .message.assistant .message-content-wrapper {\n    width: calc(100% - 46px);\n    max-width: 760px" in STYLE_CSS
    assert ".chat-form {\n    max-width: 800px" in STYLE_CSS


def test_compact_output_panel_docks_above_chat_instead_of_covering_it():
    compact = STYLE_CSS[
        STYLE_CSS.index("/* A side-by-side inspector would leave an unusably narrow reading column"):
        STYLE_CSS.index("/* --- Project-organized task sidebar --- */")
    ]
    assert "@media (max-width: 900px)" in compact
    assert "margin-right: 54px" in compact
    assert "margin-top: calc(clamp(216px, 42vh, 360px) + 40px)" in compact
    assert "height: clamp(216px, 42vh, 360px)" in compact
    assert "max-height: none" in compact
    assert "@media (max-width: 640px)" in compact
    assert "margin-top: calc(clamp(216px, 40vh, 300px) + 32px)" in compact
    assert "height: clamp(216px, 40vh, 300px)" in compact
