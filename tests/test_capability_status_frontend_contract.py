from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
STYLE = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")


def test_chat_consumes_authoritative_capability_status_event():
    assert "eventType === 'capability_status'" in APP
    assert "Workbench 已查詢後台狀態" in APP
    assert "目前無法驗證後台狀態" in APP
    assert "capabilityStatus = eventData" in APP


def test_repair_actions_open_existing_non_modal_workspaces():
    assert "data-capability-repair-workspace" in APP
    assert "workbenchIntegrationCenter?.open" in APP
    assert "workbenchExtensions?.open" in APP
    assert "workbenchCloudLlm?.openTab" in APP
    assert "projectSwitcherBtn?.click()" in APP


def test_capability_status_card_has_narrow_screen_layout():
    assert ".capability-status-card" in STYLE
    assert ".capability-status-row" in STYLE
    narrow = STYLE[STYLE.index("@media (max-width: 640px)"):]
    assert ".capability-status-row" in narrow
    assert "flex-direction: column" in narrow
