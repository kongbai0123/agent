import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
EXTENSIONS = (ROOT / "frontend" / "extension-center.js").read_text(encoding="utf-8")
STYLE = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")
CONNECTORS = (ROOT / "frontend" / "connector-center.js").read_text(encoding="utf-8")
ROUTES = (ROOT / "backend" / "api" / "routes" / "extensions.py").read_text(encoding="utf-8")
SCHEMAS = (ROOT / "backend" / "api" / "schemas" / "extensions.py").read_text(encoding="utf-8")


def test_extension_center_is_a_primary_workspace_reachable_from_basic_chat():
    assert 'id="rail-extensions"' in INDEX
    assert 'id="extension-center-workspace"' in INDEX
    assert 'id="extension-center-modal"' not in INDEX
    assert "nextWorkspace === 'extensions'" in APP
    assert "setPrimaryWorkspace('extensions')" in APP
    assert ".extension-workspace[hidden]" in STYLE
    assert "workbenchBody.appendChild(workspace)" in EXTENSIONS


def test_extension_workspace_exposes_required_information_architecture():
    for tab, label in (
        ("installed", "已安裝"),
        ("available", "未安裝"),
        ("connections", "連線"),
        ("local", "私人／本機"),
    ):
        assert f'data-extension-tab="{tab}"' in INDEX
        assert label in INDEX
    assert 'id="extension-developer-toggle"' in INDEX
    assert 'data-extension-panel="developer"' in INDEX
    assert 'id="connector-center"' in INDEX
    assert 'id="extension-detail-view"' in INDEX
    assert 'id="extension-detail-back"' in INDEX
    assert 'id="extension-detail-content"' in INDEX
    assert INDEX.index('data-extension-tab="installed"') < INDEX.index('data-extension-tab="available"')
    assert 'class="extension-tab-btn is-primary active"' in INDEX
    assert 'class="extension-tab-btn is-secondary"' in INDEX
    assert 'data-extension-panel="installed">' in INDEX
    assert 'data-extension-panel="available" hidden' in INDEX
    assert "activeTab: 'installed'" in EXTENSIONS
    assert "async function open(tab = 'installed'" in EXTENSIONS
    assert "window.workbenchExtensions?.open('installed')" in APP


def test_extension_catalog_is_shallow_and_details_disclose_information_in_order():
    catalog_card = EXTENSIONS[
        EXTENSIONS.index("function createExtensionCard"):
        EXTENSIONS.index("function projectOverrideSelect")
    ]
    assert "extension-card-description" in catalog_card
    assert "extensionDocumentation(item).summary" in catalog_card
    assert "extension-card-summary-footer" in catalog_card
    assert "查看詳情" in catalog_card
    assert "extension-permissions" not in catalog_card
    assert "extension-card-controls" not in catalog_card
    assert "健康檢查" not in catalog_card
    assert "查看 Audit" not in catalog_card

    detail = EXTENSIONS[
        EXTENSIONS.index("function renderExtensionDetail"):
        EXTENSIONS.index("async function refreshN8nService")
    ]
    use = detail.index("extensionDetailStage = 'use'")
    guide = detail.index("appendUsageGuide")
    settings = detail.index("detailSection('settings'")
    features = detail.index("detailSection('features'")
    technical = detail.index("appendTechnicalDetails")
    assert use < guide < settings < features < technical
    assert "立即使用" in detail


def test_extension_details_explain_usage_data_approval_tools_and_permissions():
    for contract in (
        "這項擴充怎麼使用",
        "哪些資料會送出去",
        "系統什麼時候會詢問你",
        "目前做不到或需要注意的事",
        "documentation.tools",
        "tr('技術名稱', 'Technical name')",
        "extension-permission-detail-list",
        "extension-permission-purpose",
        "這項擴充會做什麼",
        "批准規則",
    ):
        assert contract in EXTENSIONS
    assert ".extension-permission-detail-row" in STYLE
    assert ".extension-permission-purpose" in STYLE


def test_extension_detail_exposes_project_permission_levels_with_explicit_effects():
    for contract in (
        "Agent 操作權限等級",
        "不開放權限",
        "限制權限",
        "開放權限",
        "完全不允許 Agent 使用此外掛工具",
        "輸入資料、外部寫入、系統操作或不可逆操作",
        "不可信網站內容可能誘導 Agent",
        "只套用到目前專案",
        "extensionPermissionLevel",
        "/permission`,",
        "revision: Number((item.project_permission || {}).revision || 0)",
    ):
        assert contract in EXTENSIONS
    assert ".extension-permission-level-guide" in STYLE
    assert ".extension-permission-level-option.is-open" in STYLE
    assert "window.confirm(" in EXTENSIONS


def test_extension_workspace_has_real_bilingual_runtime_switching():
    assert 'data-extension-zh="AGENT 擴充功能"' in INDEX
    assert 'data-extension-en="AGENT EXTENSIONS"' in INDEX
    assert "function applyExtensionLocale()" in EXTENSIONS
    assert "workbench:language-change" in EXTENSIONS
    assert "document.documentElement?.lang === 'en-US'" in EXTENSIONS
    assert "ui_language: settingUiLanguage?.value === 'en-US'" in APP
    assert "document.documentElement.lang = language" in APP
    assert "window.location.reload()" in APP


def test_traditional_chinese_permission_copy_hides_raw_english_descriptions():
    for permission_id, label in (
        ("network.n8n", "連接本機自動化服務"),
        ("network.ollama", "連接本機模型服務"),
        ("connector.github.repository.read", "讀取已選取的 GitHub 儲存庫"),
        ("connector.notion.content.write", "更新 Notion 內容"),
        ("process.mcp", "啟動本機工具程序"),
    ):
        assert permission_id in EXTENSIONS
        assert label in EXTENSIONS


def test_unavailable_detail_uses_localized_fallback_and_cannot_enable():
    detail = EXTENSIONS[
        EXTENSIONS.index("function renderExtensionDetail"):
        EXTENSIONS.index("async function refreshN8nService")
    ]
    assert "controlPolicy.unavailable" in detail
    assert "tr('目前無法使用', 'Unavailable')" in detail
    assert "tr('目前無法啟用', 'Cannot enable')" in detail
    assert "enable.disabled = !canEnableHere" in detail
    assert "if (canEnableHere)" in detail
    assert "cursor_adapter_not_implemented" in EXTENSIONS
    assert "此版本尚未提供 Cursor 介接器，因此目前不能使用。" in EXTENSIONS
    assert "目前不會啟動 Cursor、不會讀取專案，也不會修改任何檔案。" in EXTENSIONS

    script = r"""
global.window = {};
global.document = { documentElement: { lang: 'zh-TW' } };
require('./frontend/extension-center.js');
const documentation = window.workbenchExtensions.__testing.extensionDocumentation({
    id: 'builtin.cursor',
    description: 'Cursor adapter is not available in this release.',
    availability_reason: 'cursor_adapter_not_implemented'
});
console.log(JSON.stringify(documentation));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    documentation = json.loads(completed.stdout)
    assert documentation["summary"] == "此版本尚未提供 Cursor 介接器，因此目前不能使用。"
    assert "Cursor adapter is not available" not in json.dumps(documentation, ensure_ascii=False)


def test_extension_detail_tracks_live_n8n_status_and_preserves_keyboard_focus():
    assert "workbench:n8n-service-state" in EXTENSIONS
    assert "function captureDetailViewState" in EXTENSIONS
    assert "function restoreDetailViewState" in EXTENSIONS
    assert "snapshot.technicalOpen" in EXTENSIONS
    assert "target || byId('extension-detail-title')" in EXTENSIONS
    assert "starting ? tr('啟動中', 'Starting')" in EXTENSIONS
    assert "detailReturnExtensionId" in EXTENSIONS

    open_detail = EXTENSIONS[
        EXTENSIONS.index("async function openExtensionDetail"):
        EXTENSIONS.index("function closeExtensionDetail")
    ]
    assert open_detail.index("await refreshN8nService()") < open_detail.index(
        "byId('extension-detail-title')?.focus()"
    )


def test_extension_workspace_keeps_permission_review_as_a_modal():
    assert 'id="extension-permission-modal"' in INDEX
    assert "closePermissionReview" in EXTENSIONS
    assert "onWorkspaceOpen" in EXTENSIONS
    assert "onWorkspaceClose" in EXTENSIONS


def test_connection_workspace_supports_local_oauth_and_project_resources():
    assert "connector-center.js?v=1.0.1-extension-gate" in INDEX
    for contract in (
        "/api/connectors",
        "/auth-profile/status",
        "/oauth/start",
        "/connections/",
        "/resources",
    ):
        assert contract in CONNECTORS
    assert "callback_uri" in CONNECTORS
    assert "force_local=true" in CONNECTORS
    assert "revision: Number(current.revision || 0)" in CONNECTORS
    assert "read_write" in CONNECTORS


def test_install_review_runs_the_backend_lifecycle_in_order():
    flow = EXTENSIONS[
        EXTENSIONS.index("async function confirmPermissionReview"):
        EXTENSIONS.index("async function reviewProviderModel")
    ]
    install = flow.index("/install${scope}")
    trust = flow.index("/trust${scope}")
    enable = flow.index("/state${scope}")
    assert install < trust < enable
    assert "operation === 'install' || operation === 'enable' || operation === 'activate'" in flow
    assert "current = result.extension || current" in flow
    assert "manifest_sha256: current.manifest_sha256" in flow
    assert "operation === 'install' && current.connection_required" in flow


def test_project_mode_payload_and_extension_mutation_contracts_match_routes():
    assert "/api/projects/${encoded(projectId)}/extensions/${encoded(item.id)}" in EXTENSIONS
    assert "...(mode === 'enabled' ? { manifest_sha256: item.manifest_sha256 } : {})" in EXTENSIONS
    assert "['inherit', 'enabled', 'disabled']" in EXTENSIONS
    assert "/health${projectQuery()}" in EXTENSIONS
    assert "healthInfo(result.extension || item)" in EXTENSIONS
    assert "/audits?limit=50" in EXTENSIONS
    assert "method: 'DELETE'" in EXTENSIONS
    assert "/local/inspect${projectQuery()}" in EXTENSIONS


def test_frontend_paths_and_payload_keys_match_the_strict_backend_contract():
    for route in (
        '@router.get("/api/extensions")',
        '@router.post("/api/extensions/local/inspect")',
        '@router.post("/api/extensions/{extension_id}/install")',
        '@router.post("/api/extensions/{extension_id}/trust")',
        '@router.patch("/api/extensions/{extension_id}/state")',
        '@router.put("/api/projects/{project_id}/extensions/{extension_id}")',
        '@router.put("/api/projects/{project_id}/extensions/{extension_id}/permission")',
        '@router.post("/api/extensions/{extension_id}/health")',
        '@router.delete("/api/extensions/{extension_id}")',
    ):
        assert route in ROUTES
    assert "class ExtensionInstallRequest" in SCHEMAS
    assert "class ExtensionTrustRequest" in SCHEMAS
    assert "global_enabled: bool" in SCHEMAS
    assert 'mode: Literal["inherit", "enabled", "disabled"]' in SCHEMAS
    assert 'level: Literal["blocked", "restricted", "open"]' in SCHEMAS
    assert "manifest_sha256: current.manifest_sha256" in EXTENSIONS
    assert "global_enabled: true" in EXTENSIONS


def test_uninstalled_keeps_unavailable_entries_visible_without_overlapping_installed():
    script = r"""
global.window = {};
require('./frontend/extension-center.js');

class FakeElement {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.children = [];
        this.dataset = {};
        this.className = '';
        this.disabled = false;
        this.hidden = false;
        this.checked = false;
        this.textContent = '';
        this.title = '';
    }
    appendChild(child) { this.children.push(child); return child; }
    append(...children) { this.children.push(...children); }
    addEventListener() {}
}

global.document = {
    createElement: tag => new FakeElement(tag),
    createTextNode: text => ({ nodeType: 3, textContent: String(text), children: [], dataset: {} })
};

const cursor = {
    id: 'builtin.cursor', name: 'Cursor Agent', origin: 'builtin', version: '1.0.0',
    installed: false, available: false, trusted: true, global_enabled: false,
    effective_enabled: false, runtime_available: false,
    availability_reason: 'cursor_adapter_not_implemented',
    permissions: [], health: { status: 'unavailable' }
};
const excel = {
    id: 'builtin.excel', name: 'Microsoft Excel', origin: 'builtin', version: '1.0.0',
    installed: true, available: false, trusted: true, global_enabled: false,
    effective_enabled: false, runtime_available: false,
    availability_reason: 'excel_adapter_not_implemented',
    permissions: [], health: { status: 'unavailable' }
};
const testing = window.workbenchExtensions.__testing;
const explore = testing.catalogSectionItems({
    extensions: [cursor, excel],
    sections: { available: [], unavailable: [cursor, excel] }
}, 'available');
const installed = testing.catalogSectionItems({
    extensions: [cursor, excel],
    sections: { available: [], unavailable: [cursor, excel] }
}, 'installed');
const walk = root => [root, ...(root.children || []).flatMap(walk)];
    const controls = item => {
        const nodes = walk(testing.createExtensionCard(item));
        const action = name => nodes.find(node => node.dataset?.extensionAction === name);
        const toggle = nodes.find(node => node.dataset?.extensionGlobalToggle === item.id);
        return {
        install: action('install') ? {
            disabled: action('install').disabled,
            title: action('install').title
        } : null,
            open: action('open') ? { disabled: action('open').disabled } : null,
            hasToggle: Boolean(toggle),
            policy: testing.extensionControlPolicy(item)
        };
};
console.log(JSON.stringify({
    exploreIds: explore.map(item => item.id),
    installedIds: installed.map(item => item.id),
    cursor: controls(cursor),
    excel: controls(excel)
}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result["exploreIds"] == ["builtin.cursor"]
    assert result["installedIds"] == ["builtin.excel"]
    assert result["cursor"]["install"]["disabled"] is True
    assert "Cursor" in result["cursor"]["install"]["title"]
    assert "介接器" in result["cursor"]["install"]["title"]
    assert result["cursor"]["hasToggle"] is False
    assert result["cursor"]["policy"]["canInstall"] is False
    assert result["excel"]["open"]["disabled"] is False
    assert result["excel"]["hasToggle"] is False
    assert result["excel"]["policy"]["canEnable"] is False


def test_connection_tab_shares_project_scope_and_obeys_extension_state():
    assert "ensureConnectionProjectScope" in EXTENSIONS
    assert "workbenchConnectors?.setProject?.(state.projectId)" in EXTENSIONS
    assert "refresh?.({ projectId: state.projectId })" in EXTENSIONS
    assert "workbench:connector-project-change" in EXTENSIONS
    assert "request(`/api/extensions${projectQuery}`)" in CONNECTORS
    assert "state.extensionCatalogReady" in CONNECTORS
    assert "extension?.effective_enabled" in CONNECTORS
    assert "window.workbenchConnectors = { init, refresh, setProject }" in CONNECTORS
