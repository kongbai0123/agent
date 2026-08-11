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


def test_output_entry_reuses_the_existing_inspector_surface():
    assert 'id="rail-output"' in INDEX_HTML
    assert 'aria-label="開啟輸出內容"' in INDEX_HTML
    assert 'id="inspector-tab-output"' in INDEX_HTML
    assert 'data-itab="output"' in INDEX_HTML
    assert 'aria-controls="inspector-pane-output"' in INDEX_HTML
    assert 'id="inspector-pane-output"' in INDEX_HTML
    assert 'role="tabpanel"' in INDEX_HTML
    assert 'aria-labelledby="inspector-tab-output"' in INDEX_HTML
    assert INDEX_HTML.count('id="artifacts-sandbox-panel"') == 1

    assert "document.getElementById('rail-output').addEventListener('click'" in APP_JS
    assert "openInspector('output')" in APP_JS
    assert "artifactsSandboxPanel.classList.toggle('output-mode', tab === 'output')" in APP_JS


def test_output_skills_share_the_project_scoped_renderer_and_cache():
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
        ".artifacts-sandbox-panel.output-mode",
        ".output-pane",
        ".output-panel-card",
        ".output-panel-head",
        ".output-skills-mount .project-skills-section",
        ".output-panel-empty",
    ):
        assert selector in STYLE_CSS
    assert "overscroll-behavior: contain" in STYLE_CSS
    assert "@media (max-width: 900px)" in STYLE_CSS
    assert ".inspector-panel.output-mode { width: min(92vw, 400px); }" in STYLE_CSS
    assert "@media (max-width: 640px)" in STYLE_CSS
    assert ".inspector-panel.output-mode { bottom: 56px; width: 100vw; min-width: 0; height: auto; }" in STYLE_CSS
