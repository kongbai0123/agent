from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
CLOUD_JS = (FRONTEND / "cloud-llm-center.js").read_text(encoding="utf-8")


WORKSPACES = {
    "chat": ("chat", "rail-chat"),
    "workflows": ("n8n-workflow-center", "rail-workflows"),
    "extensions": ("extension-center-workspace", "rail-extensions"),
    "models": ("model-manager-workspace", "rail-models"),
    "cloud": ("cloud-llm-workspace", "rail-cloud-llm"),
}


def _slice(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


def test_models_cloud_and_extensions_are_management_workspaces_not_modals():
    for workspace_id in (
        "extension-center-workspace",
        "model-manager-workspace",
        "cloud-llm-workspace",
    ):
        marker = f'id="{workspace_id}"'
        assert marker in INDEX_HTML
        opening_tag = INDEX_HTML[INDEX_HTML.rfind("<", 0, INDEX_HTML.index(marker)) : INDEX_HTML.index(">", INDEX_HTML.index(marker)) + 1]
        assert opening_tag.startswith("<main ")
        assert " hidden" in opening_tag
        assert 'role="dialog"' not in opening_tag
        assert 'aria-modal="true"' not in opening_tag

    assert 'id="model-manager-modal"' not in INDEX_HTML
    assert 'id="cloud-llm-modal"' not in INDEX_HTML


def test_primary_workspace_switch_has_five_exclusive_states_and_policy_gates():
    workspace = _slice(APP_JS, "function setPrimaryWorkspace", "// ---- Workbench 初始化")
    assert "new Set(['chat', 'workflows', 'extensions', 'models', 'cloud'])" in workspace
    assert "const managementMode = extensionMode || modelMode || cloudMode" in workspace
    assert "workbenchRunInspector?.setAvailable?.(!managementMode" in workspace
    assert "drawer.hidden = nextWorkspace !== 'chat'" in workspace
    assert "['workflows', railWorkflows]" in workspace

    for state, (main_id, rail_id) in WORKSPACES.items():
        if state != "chat":
            assert main_id in workspace
        assert f"['{state}', { {'chat': 'railChat', 'workflows': 'railWorkflows', 'extensions': 'railExtensions', 'models': 'railModels', 'cloud': 'railCloud'}[state]}]" in workspace
        assert rail_id in INDEX_HTML

    # Workflows deliberately remain run-inspector capable; only the three
    # management workspaces are included in the unavailable gate.
    assert "workflowMode" not in workspace.split("const managementMode =", 1)[1].split(";", 1)[0]


def test_switching_each_primary_workspace_leaves_one_main_and_one_active_rail():
    script = textwrap.dedent(
        r"""
        const fs = require('fs');
        const source = fs.readFileSync(process.argv[1], 'utf8');
        const start = source.indexOf('let primaryWorkspace');
        const end = source.indexOf('// ---- Workbench 初始化', start);
        if (start < 0 || end < 0) throw new Error('primary workspace state machine not found');

        class ClassList {
          constructor() { this.values = new Set(); }
          toggle(name, force) {
            if (force === undefined) force = !this.values.has(name);
            if (force) this.values.add(name); else this.values.delete(name);
          }
          add(name) { this.values.add(name); }
          remove(name) { this.values.delete(name); }
          contains(name) { return this.values.has(name); }
        }
        class Node {
          constructor(id) {
            this.id = id;
            this.hidden = false;
            this.classList = new ClassList();
            this.attributes = new Map();
          }
          setAttribute(name, value) { this.attributes.set(name, String(value)); }
          getAttribute(name) { return this.attributes.get(name) ?? null; }
        }

        const nodes = new Map();
        const make = id => { const node = new Node(id); nodes.set(id, node); return node; };
        const chat = make('chat');
        const mains = ['n8n-workflow-center', 'extension-center-workspace', 'model-manager-workspace', 'cloud-llm-workspace'].map(make);
        const rails = ['rail-chat', 'rail-workflows', 'rail-extensions', 'rail-models', 'rail-cloud-llm'].map(make);
        const drawer = make('chat-drawer');
        let inspectorAvailable = null;
        let cloudDeactivations = 0;

        global.window = {
          matchMedia: () => ({ matches: false }),
          workbenchRunInspector: {
            setAvailable(value) { inspectorAvailable = value; },
            isOpen() { return false; },
          },
          workbenchCloudLlm: { deactivate() { cloudDeactivations += 1; } },
          workbenchN8nWorkflows: { close() {}, useChatInspectorContext() {} },
          workbenchN8nGovernance: { releaseInspectorContext() {} },
        };
        global.document = {
          querySelector(selector) { return selector === 'main.chat-container' ? chat : null; },
          getElementById(id) { return nodes.get(id) || null; },
        };
        global.syncChatDrawerA11y = () => {};
        global.setOutputFloatingPanelOpen = () => {};
        global.setTaskProgressCollapsed = () => {};
        global.closeInspectorPanel = () => {};
        global.closeAgentCollaboration = () => {};

        const observations = [];
        const stateToMain = {
          chat: 'chat', workflows: 'n8n-workflow-center', extensions: 'extension-center-workspace',
          models: 'model-manager-workspace', cloud: 'cloud-llm-workspace',
        };
        const stateToRail = {
          chat: 'rail-chat', workflows: 'rail-workflows', extensions: 'rail-extensions',
          models: 'rail-models', cloud: 'rail-cloud-llm',
        };
        const fragment = source.slice(start, end) + `
          for (const state of ['chat', 'workflows', 'extensions', 'models', 'cloud']) {
            setPrimaryWorkspace(state);
            const allMains = [chat, ...mains];
            observations.push({
              state,
              visible: allMains.filter(node => !node.hidden).map(node => node.id),
              active: rails.filter(node => node.classList.contains('active')).map(node => node.id),
              current: rails.filter(node => node.getAttribute('aria-current') === 'page').map(node => node.id),
              inspectorAvailable,
              expectedMain: stateToMain[state],
              expectedRail: stateToRail[state],
            });
          }
          setPrimaryWorkspace('models');
          globalThis.__result = { observations, cloudDeactivations };
        `;
        eval(fragment);
        console.log(JSON.stringify(globalThis.__result));
        """
    )
    completed = subprocess.run(
        ["node", "-e", script, str(FRONTEND / "app.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    result = json.loads(completed.stdout)

    for observation in result["observations"]:
        assert observation["visible"] == [observation["expectedMain"]]
        assert observation["active"] == [observation["expectedRail"]]
        assert observation["current"] == [observation["expectedRail"]]
        assert observation["inspectorAvailable"] is (observation["state"] in {"chat", "workflows"})
    assert result["cloudDeactivations"] >= 1


def test_escape_and_cloud_deactivation_route_through_workspace_controllers():
    a11y = _slice(APP_JS, "function initA11y", "let primaryWorkspace")
    assert "primaryWorkspace === 'extensions'" in a11y
    assert "workbenchExtensions?.close?.()" in a11y
    assert "primaryWorkspace === 'models'" in a11y
    assert "closeModelManager()" in a11y
    assert "primaryWorkspace === 'cloud'" in a11y
    assert "workbenchCloudLlm?.close?.()" in a11y

    workspace = _slice(APP_JS, "function setPrimaryWorkspace", "// ---- Workbench 初始化")
    assert "previousWorkspace === 'cloud' && nextWorkspace !== 'cloud'" in workspace
    assert "workbenchCloudLlm?.deactivate?.()" in workspace

    cloud_init = _slice(APP_JS, "window.workbenchCloudLlm?.init", "window.workbenchN8nWorkflows?.init")
    assert "onWorkspaceOpen: () => setPrimaryWorkspace('cloud')" in cloud_init
    assert "onWorkspaceClose:" in cloud_init
    assert "setPrimaryWorkspace('chat')" in cloud_init

    deactivate = _slice(CLOUD_JS, "async function deactivate", "function bindEvents")
    assert "lifecycleRevision" in deactivate
    assert "if (await deactivate()) state.deps?.onWorkspaceClose?.()" in deactivate
    assert "window.workbenchCloudLlm = { init, open, close, deactivate" in CLOUD_JS


def test_auxiliary_panels_cannot_reclaim_a_management_workspace():
    inspector = _slice(APP_JS, "function openInspector", "function renderContextPane")
    collaboration = _slice(
        APP_JS,
        "function openAgentCollaboration",
        "function closeAgentCollaboration",
    )
    helper = _slice(
        APP_JS,
        "function activateChatForAuxiliaryPanel",
        "function setPrimaryWorkspace",
    )
    assert "if (primaryWorkspace !== 'chat') return false" in inspector
    assert "if (primaryWorkspace !== 'chat') return false" in collaboration
    assert "setPrimaryWorkspace('chat')" in helper
    assert "activateChatForAuxiliaryPanel();\n        openInspector('run');" in APP_JS
    assert "activateChatForAuxiliaryPanel();\n            openInspector('artifact');" in APP_JS
