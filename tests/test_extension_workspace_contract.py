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
    assert "workspace === 'extensions'" in APP
    assert "setPrimaryWorkspace('extensions')" in APP
    assert ".extension-workspace[hidden]" in STYLE
    assert "workbenchBody.appendChild(workspace)" in EXTENSIONS


def test_extension_workspace_exposes_required_information_architecture():
    for tab, label in (
        ("available", "探索"),
        ("installed", "已安裝"),
        ("connections", "連線"),
        ("local", "私人／本機"),
    ):
        assert f'data-extension-tab="{tab}"' in INDEX
        assert label in INDEX
    assert 'id="extension-developer-toggle"' in INDEX
    assert 'data-extension-panel="developer"' in INDEX
    assert 'id="connector-center"' in INDEX


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
        '@router.post("/api/extensions/{extension_id}/health")',
        '@router.delete("/api/extensions/{extension_id}")',
    ):
        assert route in ROUTES
    assert "class ExtensionInstallRequest" in SCHEMAS
    assert "class ExtensionTrustRequest" in SCHEMAS
    assert "global_enabled: bool" in SCHEMAS
    assert 'mode: Literal["inherit", "enabled", "disabled"]' in SCHEMAS
    assert "manifest_sha256: current.manifest_sha256" in EXTENSIONS
    assert "global_enabled: true" in EXTENSIONS


def test_explore_keeps_unavailable_catalog_entries_visible_but_not_installable():
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
        enable: action('enable') ? {
            disabled: action('enable').disabled,
            title: action('enable').title
        } : null,
        toggle: { disabled: toggle.disabled, title: toggle.title },
        policy: testing.extensionControlPolicy(item)
    };
};
console.log(JSON.stringify({
    exploreIds: explore.map(item => item.id),
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

    assert result["exploreIds"] == ["builtin.cursor", "builtin.excel"]
    assert result["cursor"]["install"]["disabled"] is True
    assert "Cursor adapter" in result["cursor"]["install"]["title"]
    assert result["cursor"]["toggle"]["disabled"] is True
    assert result["cursor"]["policy"]["canInstall"] is False
    assert result["excel"]["enable"]["disabled"] is True
    assert "Excel adapter" in result["excel"]["enable"]["title"]
    assert result["excel"]["toggle"]["disabled"] is True
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
