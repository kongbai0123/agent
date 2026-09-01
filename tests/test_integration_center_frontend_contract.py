from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
INTEGRATION_JS = (FRONTEND / "integration-center.js").read_text(encoding="utf-8")
STYLE_CSS = (FRONTEND / "style.css").read_text(encoding="utf-8")


def _slice(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


def test_integration_center_is_a_fixed_primary_workspace() -> None:
    assert 'id="rail-integrations"' in INDEX_HTML
    assert 'aria-controls="integration-center-workspace"' in INDEX_HTML
    marker = 'id="integration-center-workspace"'
    opening = INDEX_HTML[
        INDEX_HTML.rfind("<", 0, INDEX_HTML.index(marker)) : INDEX_HTML.index(">", INDEX_HTML.index(marker)) + 1
    ]
    assert opening.startswith("<main ")
    assert " hidden" in opening
    assert 'role="dialog"' not in opening
    assert 'aria-modal="true"' not in opening

    workspace = _slice(APP_JS, "function setPrimaryWorkspace", "// ---- Workbench 初始化")
    assert "const integrationMode = nextWorkspace === 'integrations'" in workspace
    assert "integrationCenter.hidden = !integrationMode" in workspace
    assert "integrationMode" in workspace.split("const managementMode =", 1)[1].split(";", 1)[0]
    assert "workbenchRunInspector?.setAvailable?.(!managementMode" in workspace


def test_integration_center_exposes_the_five_requested_tabs_and_six_services() -> None:
    for tab, label in (
        ("overview", "概覽"),
        ("services", "第三方服務"),
        ("api", "對外 API"),
        ("policy", "權限方案"),
        ("audit", "健康與稽核"),
    ):
        assert f'data-integration-tab="{tab}"' in INDEX_HTML
        assert f'>{label}</button>' in INDEX_HTML
        assert f'data-integration-panel="{tab}"' in INDEX_HTML

    for integration_id in ("gmail", "github", "notion", "n8n", "mcp", "external_api"):
        assert f"id: '{integration_id}'" in INTEGRATION_JS

    assert "Gmail" in INDEX_HTML
    assert "GitHub" in INDEX_HTML
    assert "Notion" in INDEX_HTML
    assert "本機 MCP" in INDEX_HTML


def test_frontend_only_calls_the_fixed_integration_center_contract() -> None:
    expected = (
        "/api/integration-center/overview?project_id=",
        "/api/integration-center/api-keys",
        "/api/integration-center/api-keys/${encodeURIComponent(key.id)}/rotate",
        "/api/integration-center/api-keys/${encodeURIComponent(key.id)}/revoke",
        "/api/integration-center/policies/${encodeURIComponent(state.selectedProjectId)}",
        "/api/integration-center/audit?project_id=",
    )
    for endpoint in expected:
        assert endpoint in INTEGRATION_JS
    assert "/api/connectors" not in INTEGRATION_JS
    assert "/api/extensions" not in INTEGRATION_JS
    assert "/api/integrations/n8n" not in INTEGRATION_JS
    assert "method: 'PUT'" in INTEGRATION_JS
    assert "data-key-action=\"toggle\"" in INTEGRATION_JS
    assert "API Key 已暫停" in INTEGRATION_JS


def test_machine_bound_api_key_secret_is_one_time_and_never_persisted() -> None:
    assert "installation.api_base_url" in INTEGRATION_JS
    assert "installation.label" in INTEGRATION_JS
    assert "payload.secret" in INTEGRATION_JS
    assert "state.oneTimeSecret" in INTEGRATION_JS
    assert "clearSecret();" in INTEGRATION_JS
    assert "integration-confirm-secret" in INDEX_HTML
    assert "只顯示這一次" in INDEX_HTML
    assert "由這台電腦的 Workbench 產生並綁定此安裝" in INDEX_HTML
    assert "localStorage" not in INTEGRATION_JS
    assert "sessionStorage" not in INTEGRATION_JS

    for scope in ("runs:create", "runs:read", "runs:cancel", "capabilities:read"):
        assert f'value="{scope}"' in INDEX_HTML


def test_project_policy_has_three_explicit_risk_levels_and_scoped_connections() -> None:
    for mode, label in (
        ("blocked", "不開放權限"),
        ("restricted", "限制權限"),
        ("open", "開放權限"),
    ):
        assert f'value="{mode}"' in INDEX_HTML
        assert label in INDEX_HTML
    assert "誤送、誤改與第三方帳號變更" in INDEX_HTML
    assert "付款、刪除、授權與系統操作仍不會被此方案放寬" in INDEX_HTML
    assert "acknowledge_open_risk" in INTEGRATION_JS
    assert "revision: Number(state.policy?.revision || 0)" in INTEGRATION_JS
    assert "data-policy-connection" in INTEGRATION_JS
    assert "必須先選擇明確連線，不能靜默使用其他帳號" in INTEGRATION_JS
    assert "data-policy-resource" in INTEGRATION_JS
    assert "payload.audits" in INTEGRATION_JS
    assert "rawState.connections" in INTEGRATION_JS


def test_layout_keeps_an_internal_scroll_surface_and_never_overlays_the_workspace() -> None:
    block = _slice(STYLE_CSS, "/* 統一整合中心", "/* MLOps 是主工作區")
    assert ".integration-workspace" in block
    assert "flex: 1 1 auto" in block
    assert "overflow: hidden" in block
    assert ".integration-content" in block
    assert "overflow-y: auto" in block
    assert "@media (max-width: 900px)" in block
    assert "@media (max-width: 640px)" in block
    assert "grid-template-columns: 1fr" in block
    assert "position: fixed" not in block
    assert "z-index:" not in block


def test_integration_center_script_is_loaded_before_app_and_parses() -> None:
    integration_tag = '<script src="integration-center.js?v=1.1.0-gmail-connector"></script>'
    app_tag = '<script src="app.js?v='
    assert integration_tag in INDEX_HTML
    assert INDEX_HTML.index(integration_tag) < INDEX_HTML.index(app_tag)

    subprocess.run(
        ["node", "--check", str(FRONTEND / "integration-center.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
