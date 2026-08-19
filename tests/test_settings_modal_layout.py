import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")
JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
BACKEND = (ROOT / "backend" / "core" / "settings.py").read_text(encoding="utf-8")
SETTINGS_ROUTES = (
    ROOT / "backend" / "api" / "routes" / "settings.py"
).read_text(encoding="utf-8")


class SettingsModalLayoutTests(unittest.TestCase):
    def test_settings_center_uses_requested_default_size(self):
        block = CSS.split(".settings-modal-box {", 1)[1].split("}", 1)[0]
        self.assertIn("width: 900px !important", block)
        self.assertIn("height: 650px", block)
        self.assertIn("max-width: calc(100vw - 32px)", block)
        self.assertIn("max-height: calc(100vh - 32px)", block)

    def test_settings_sidebar_is_compact(self):
        tabs = CSS.split(".settings-tabs {", 1)[1].split("}", 1)[0]
        button = CSS.split(".settings-tab-btn {", 1)[1].split("}", 1)[0]
        self.assertIn("width: 156px", tabs)
        self.assertIn("font-size: 12.5px", button)

    def test_settings_size_is_user_resizable_and_persistent(self):
        self.assertIn('id="settings-resize-handle"', HTML)
        self.assertIn("settings-modal-size", JS)
        self.assertIn("localStorage.setItem(sizeStorageKey", JS)
        self.assertIn('/api/settings/ui-state', JS)
        self.assertIn('@router.post("/api/settings/ui-state")', SETTINGS_ROUTES)
        self.assertIn('"settings_modal_width": 900', BACKEND)
        self.assertIn('"settings_modal_height": 650', BACKEND)
        self.assertIn("setPointerCapture", JS)
        self.assertIn("pointercancel", JS)
        self.assertIn("matchMedia('(max-width: 640px)')", JS)

    def test_compact_height_keeps_settings_actions_available(self):
        self.assertIn("@media (max-height: 680px) and (min-width: 641px)", CSS)
        self.assertIn(".settings-modal-box > .modal-footer", CSS)

    def test_model_manager_is_a_bounded_workspace_and_scrolls_its_content(self):
        shell = CSS.split(".management-workspace-shell {", 1)[1].split("}", 1)[0]
        body = CSS.split(".mm-body {", 1)[1].split("}", 1)[0]
        pane = CSS.split(".mm-pane.active {", 1)[1].split("}", 1)[0]
        self.assertIn('id="model-manager-workspace"', HTML)
        self.assertNotIn('id="model-manager-modal"', HTML)
        self.assertIn("height: 100%", shell)
        self.assertIn("min-height: 0", shell)
        self.assertIn("overflow: hidden", shell)
        self.assertIn("min-height: 0", body)
        self.assertIn("overflow: hidden", body)
        self.assertIn("overflow-y: auto", pane)
        self.assertIn("overscroll-behavior: contain", pane)
        self.assertIn("scrollbar-gutter: stable", pane)


if __name__ == "__main__":
    unittest.main()
