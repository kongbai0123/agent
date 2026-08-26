from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
BASIC_MODE_JS = (ROOT / "frontend" / "basic-chat-mode.js").read_text(encoding="utf-8")
SKILL_CENTER_JS = (ROOT / "frontend" / "skill-center.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")


def test_only_unsupported_sidebar_surfaces_are_hidden_in_basic_chat_mode():
    for element_id in (
        "rail-knowledge",
        "rail-runs",
        "rail-artifacts",
        "manage-kb-btn",
    ):
        assert element_id in BASIC_MODE_JS
    assert "'rail-extensions'" not in BASIC_MODE_JS
    assert '[data-project-settings-tab="extensions"]' not in BASIC_MODE_JS
    assert '[data-project-settings-pane="extensions"]' not in BASIC_MODE_JS
    assert "BASIC_CHAT_MODE ? basicPaletteActions(PALETTE_ACTIONS)" in APP_JS
    assert "[hidden]" in STYLE_CSS and "display: none !important" in STYLE_CSS


def test_basic_mode_skips_removed_skill_center():
    assert "typeof BASIC_CHAT_MODE !== 'undefined' && BASIC_CHAT_MODE" in SKILL_CENTER_JS
