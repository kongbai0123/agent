from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
BASIC_MODE_JS = (FRONTEND / "basic-chat-mode.js").read_text(encoding="utf-8")
INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
PROJECT_SKILLS_JS = (FRONTEND / "project-skills-sidebar.js").read_text(encoding="utf-8")
STYLE_CSS = (FRONTEND / "style.css").read_text(encoding="utf-8")


def _function_slice(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index:source.index(end, start_index)]


def test_output_is_a_standalone_floating_panel_not_an_inspector_or_rail_item():
    assert 'id="output-floating-workspace"' in INDEX_HTML
    assert 'id="output-floating-panel"' in INDEX_HTML
    assert 'class="output-floating-panel"' in INDEX_HTML
    assert 'role="tabpanel"' in INDEX_HTML
    assert 'aria-labelledby="output-floating-tab"' in INDEX_HTML
    assert 'class="output-floating-tabs"' in INDEX_HTML
    assert 'role="tablist"' in INDEX_HTML
    assert 'aria-orientation="vertical"' in INDEX_HTML
    assert 'id="output-floating-tab"' in INDEX_HTML
    assert 'role="tab"' in INDEX_HTML
    assert 'aria-controls="output-floating-panel"' in INDEX_HTML
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
    toggle = _function_slice(APP_JS, "function setOutputFloatingPanelOpen", "async function loadSessions")
    assert "outputFloatingPanel.hidden = !expanded" in toggle
    assert "outputFloatingTab.classList.toggle('active', expanded)" in toggle
    assert "outputFloatingTab.setAttribute('aria-selected'" in toggle
    assert "outputFloatingTab.setAttribute('aria-expanded'" in toggle
    assert "outputFloatingTab.addEventListener('click'" in APP_JS
    assert "setOutputFloatingPanelOpen(outputFloatingPanel.hidden)" in APP_JS
    assert "app.js?v=5.14.7-typography" in INDEX_HTML


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
    ):
        assert selector in STYLE_CSS
    assert "overscroll-behavior: contain" in STYLE_CSS
    floating = STYLE_CSS[STYLE_CSS.index(".output-floating-workspace {"):STYLE_CSS.index(".output-floating-panel {")]
    assert "position: fixed" in floating
    assert "top: 86px" in floating
    assert "right: 0" in floating
    assert "z-index: 190" in floating
    tabs = STYLE_CSS[STYLE_CSS.index(".output-floating-tabs {"):STYLE_CSS.index(".output-floating-tab {")]
    assert "flex-direction: column" in tabs
    assert "@media (max-width: 900px)" in STYLE_CSS
    assert ".output-floating-workspace { top: 72px; }" in STYLE_CSS
    assert ".output-floating-panel { width: min(380px, calc(100vw - 58px));" in STYLE_CSS
    assert "@media (max-width: 640px)" in STYLE_CSS
    assert ".output-floating-workspace { top: 64px; }" in STYLE_CSS
    assert ".output-floating-panel { width: calc(100vw - 54px);" in STYLE_CSS
