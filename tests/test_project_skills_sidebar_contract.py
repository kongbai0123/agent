from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
BASIC_MODE_JS = (FRONTEND / "basic-chat-mode.js").read_text(encoding="utf-8")
INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
PROJECT_SKILLS_JS = (FRONTEND / "project-skills-sidebar.js").read_text(encoding="utf-8")
STYLE_CSS = (FRONTEND / "style.css").read_text(encoding="utf-8")


def test_project_skills_module_is_loaded_before_app_and_wired_into_sidebar():
    module_tag = '<script src="project-skills-sidebar.js'
    app_tag = '<script src="app.js'
    assert module_tag in INDEX_HTML
    assert INDEX_HTML.index(module_tag) < INDEX_HTML.index(app_tag)
    assert "window.workbenchProjectSkills?.init({" in APP_JS
    assert "window.workbenchProjectSkills?.setSessionContext({" in APP_JS
    assert "window.workbenchProjectSkills?.createProjectSection(project" in APP_JS

    row_anchor = "block.appendChild(row);"
    skills_anchor = "window.workbenchProjectSkills?.createProjectSection(project"
    tasks_anchor = "const matching = sessions.filter(session => session.project_id === project.id"
    project_block = APP_JS[APP_JS.index("function createProjectBlock"):APP_JS.index("function createSessionRow")]
    assert project_block.index(row_anchor) < project_block.index(skills_anchor) < project_block.index(tasks_anchor)


def test_project_skills_use_only_project_and_session_scoped_apis():
    expected_paths = (
        "/api/projects/${encoded(projectId)}/skills",
        "/api/projects/${encoded(project.id)}/skills/${encoded(slug)}",
        "/api/projects/${encoded(project.id)}/skills/${encoded(slug)}/state",
        "/api/projects/${encoded(project.id)}/skills/${encoded(slug)}/versions",
        "/api/projects/${encoded(project.id)}/skills/${encoded(slug)}/versions/${encoded(digest)}",
        "/api/sessions/${encoded(sessionId)}/skills",
        "/api/sessions/${encoded(sessionId)}/skills/${encoded(slug)}",
    )
    for path in expected_paths:
        assert path in PROJECT_SKILLS_JS
    for method in ("method: 'POST'", "method: 'PATCH'", "method: 'PUT'", "method: 'DELETE'"):
        assert method in PROJECT_SKILLS_JS
    assert "expected_sha256" in PROJECT_SKILLS_JS
    assert "references: referenceObject()" in PROJECT_SKILLS_JS

    # The reset UI must not reopen the removed global catalog or its unscoped APIs.
    assert "/api/skills" not in PROJECT_SKILLS_JS
    assert "workbenchSkills" not in PROJECT_SKILLS_JS
    assert "skill-center-modal" not in PROJECT_SKILLS_JS
    assert "skills-button" not in PROJECT_SKILLS_JS


def test_project_section_supports_fixed_expansion_and_cached_auto_load():
    render_start = PROJECT_SKILLS_JS.index("function renderProjectSection")
    create_start = PROJECT_SKILLS_JS.index("function createProjectSection")
    render_block = PROJECT_SKILLS_JS[render_start:create_start]
    create_end = PROJECT_SKILLS_JS.index("function normalizeReferencePath")
    create_block = PROJECT_SKILLS_JS[create_start:create_end]

    assert "const alwaysExpanded = options?.alwaysExpanded === true;" in render_block
    assert "const expanded = alwaysExpanded || state.expandedProjects.has(project.id);" in render_block
    assert "element(alwaysExpanded ? 'div' : 'button', 'project-skills-toggle')" in render_block
    assert "if (!alwaysExpanded) {" in render_block
    assert "state.expandedProjects.delete(project.id);" in render_block
    assert "state.expandedProjects.add(project.id);" in render_block

    assert "if (options?.autoLoad === true) void loadProject(project.id);" in create_block
    assert "force" not in create_block
    assert "/api/" not in create_block


def test_api_data_is_rendered_without_html_injection_sinks():
    assert ".innerHTML" not in PROJECT_SKILLS_JS
    assert "insertAdjacentHTML" not in PROJECT_SKILLS_JS
    assert "document.write" not in PROJECT_SKILLS_JS
    assert "textContent" in PROJECT_SKILLS_JS
    assert "normalizeReferencePath" in PROJECT_SKILLS_JS
    assert "TextDecoder('utf-8', { fatal: true })" in PROJECT_SKILLS_JS


def test_editor_dom_has_labeled_fields_references_versions_and_status():
    required_ids = (
        "project-skill-editor-modal",
        "project-skill-editor-title",
        "project-skill-editor-form",
        "project-skill-name",
        "project-skill-slug",
        "project-skill-description",
        "project-skill-version",
        "project-skill-instructions",
        "project-skill-enabled",
        "project-skill-reference-input",
        "project-skill-reference-add",
        "project-skill-reference-list",
        "project-skill-version-refresh",
        "project-skill-version-list",
        "project-skill-version-preview",
        "project-skill-editor-status",
        "project-skill-editor-save",
    )
    for element_id in required_ids:
        assert f'id="{element_id}"' in INDEX_HTML
    for field_id in (
        "project-skill-name",
        "project-skill-slug",
        "project-skill-description",
        "project-skill-version",
        "project-skill-instructions",
        "project-skill-enabled",
    ):
        assert f'for="{field_id}"' in INDEX_HTML
    assert 'aria-labelledby="project-skill-editor-title"' in INDEX_HTML
    assert 'aria-live="polite"' in INDEX_HTML
    assert 'multiple' in INDEX_HTML[INDEX_HTML.index('id="project-skill-reference-input"') - 60:INDEX_HTML.index('id="project-skill-reference-input"') + 160]


def test_sidebar_and_editor_styles_cover_focus_disabled_error_and_mobile_states():
    required_selectors = (
        ".project-skills-section",
        ".project-skills-header",
        ".project-skills-list",
        ".project-skill-row",
        ".project-skill-name",
        ".project-skill-session-select",
        ".project-skill-editor-box",
        ".project-skill-reference-list",
        ".project-skill-version-list",
        ".project-skill-version-preview",
        ".project-skill-editor-status.is-error",
    )
    for selector in required_selectors:
        assert selector in STYLE_CSS
    assert ":focus-visible" in STYLE_CSS
    assert ":disabled" in STYLE_CSS
    assert "text-overflow: ellipsis" in STYLE_CSS
    assert "@media (max-width: 760px)" in STYLE_CSS


def test_basic_mode_keeps_global_center_hidden_but_not_project_skills():
    for legacy_id in ("skills-button", "active-skills-bar"):
        assert legacy_id in BASIC_MODE_JS
    assert "project-skills-section" not in BASIC_MODE_JS
    assert "project-skill-editor-modal" not in BASIC_MODE_JS
    assert "typeof BASIC_CHAT_MODE !== 'undefined' && BASIC_CHAT_MODE" in (
        FRONTEND / "skill-center.js"
    ).read_text(encoding="utf-8")


def test_session_activation_supports_all_backend_modes_and_stale_state():
    for value in ("inherit", "session", "turn", "disabled"):
        assert f"{value}:" in PROJECT_SKILLS_JS or f"'{value}'" in PROJECT_SKILLS_JS
    assert "session_override" in PROJECT_SKILLS_JS
    assert "session_scope" in PROJECT_SKILLS_JS
    assert "activation_stale" in PROJECT_SKILLS_JS
    assert "state.session.projectId !== project.id" in PROJECT_SKILLS_JS


def test_large_reference_limits_and_version_snapshot_preview_are_wired():
    assert "const MAX_REFERENCE_BYTES = 8 * 1024 * 1024" in PROJECT_SKILLS_JS
    assert "const MAX_REFERENCE_PACKAGE_BYTES = 32 * 1024 * 1024" in PROJECT_SKILLS_JS
    assert "version.snapshot_available === true" in PROJECT_SKILLS_JS
    assert "loadVersionSnapshot(version)" in PROJECT_SKILLS_JS
    assert "Instructions 快照" in PROJECT_SKILLS_JS
    assert "單檔 8 MiB、整包 32 MiB" in INDEX_HTML
