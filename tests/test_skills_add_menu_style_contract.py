from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STYLE_CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")


def test_skills_add_menu_is_anchored_and_panel_sized():
    required = (
        ".project-skills-add-wrap",
        ".project-skills-add-menu",
        "position: fixed",
        "z-index: 520",
        "width: min(320px, calc(100vw - 24px))",
        "min-width: min(280px, calc(100vw - 24px))",
        "max-width: calc(100vw - 24px)",
        "overflow-y: auto",
    )
    for contract in required:
        assert contract in STYLE_CSS


def test_skills_add_menu_options_have_clear_interaction_and_status_states():
    required = (
        ".project-skills-add-option-icon",
        ".project-skills-add-option-copy",
        ".project-skills-add-option-title",
        ".project-skills-add-option-description",
        ".project-skills-add-option-badge",
        '.project-skills-add-option:not([aria-disabled="true"]):hover',
        ".project-skills-add-option:focus-visible",
        '.project-skills-add-option[aria-disabled="true"]',
        ".project-skills-add-option.is-coming-soon",
        "cursor: not-allowed",
    )
    for contract in required:
        assert contract in STYLE_CSS

    # Titles, descriptions, and badges must respect the Workbench 12 px floor.
    assert ".project-skills-add-option-title {" in STYLE_CSS
    assert "font-size: 13px" in STYLE_CSS
    assert STYLE_CSS.count("font-size: 12px") >= 3


def test_skills_add_menu_handles_dark_narrow_and_clipped_panel_contexts():
    assert 'html[data-theme="dark"] .project-skills-add-menu' in STYLE_CSS
    assert "@media (max-width: 420px)" in STYLE_CSS
    assert "width: min(300px, calc(100vw - 24px))" in STYLE_CSS
    assert ".output-panel-card:has(.project-skills-add-menu:not([hidden]))" not in STYLE_CSS
    assert ".output-skills-mount:has(.project-skills-add-menu:not([hidden]))" not in STYLE_CSS
    assert "@media (prefers-reduced-motion: reduce)" in STYLE_CSS


def test_skill_format_guide_and_editor_layer_are_readable_above_the_output_panel():
    for selector in (
        ".project-skills-format-guide",
        ".project-skills-format-guide-head",
        ".project-skills-format-guide-title",
        ".project-skills-format-tree",
        ".project-skills-format-notes",
        "#project-skill-editor-modal { z-index: 900; }",
    ):
        assert selector in STYLE_CSS


def test_empty_skills_state_exposes_primary_and_secondary_actions():
    required = (
        ".project-skills-empty",
        ".project-skills-empty-icon",
        ".project-skills-empty-title",
        ".project-skills-empty-description",
        ".project-skills-empty-primary",
        ".project-skills-empty-secondary",
        ".project-skills-empty-primary:focus-visible",
        ".project-skills-empty-primary:disabled",
    )
    for contract in required:
        assert contract in STYLE_CSS
