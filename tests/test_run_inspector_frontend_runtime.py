import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_run_inspector_workspace_availability_escape_focus_and_aria_state():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const ids = new Map();

class ClassList {
    constructor(owner) { this.owner = owner; }
    values() { return new Set(String(this.owner.className || '').split(/\s+/).filter(Boolean)); }
    write(values) { this.owner.className = [...values].join(' '); }
    toggle(name, force) {
        const values = this.values();
        const enabled = force === undefined ? !values.has(name) : Boolean(force);
        enabled ? values.add(name) : values.delete(name);
        this.write(values);
        return enabled;
    }
    contains(name) { return this.values().has(name); }
}

class Element {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.className = '';
        this.classList = new ClassList(this);
        this.attributes = {};
        this.dataset = {};
        this.listeners = {};
        this.hidden = false;
        this.tabIndex = 0;
        this.textContent = '';
        this.disabled = false;
    }
    set id(value) { this._id = String(value); ids.set(this._id, this); }
    get id() { return this._id || ''; }
    get childNodes() { return this.children; }
    append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
    appendChild(node) { node.parentNode = this; this.children.push(node); return node; }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    contains(node) {
        for (let current = node; current; current = current.parentNode) {
            if (current === this) return true;
        }
        return false;
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    emit(name, event = {}) {
        event.currentTarget ||= this;
        event.preventDefault ||= () => {};
        for (const fn of this.listeners[name] || []) fn(event);
    }
    click() { this.emit('click'); }
    focus() { document.activeElement = this; }
}

global.document = {
    activeElement: null,
    documentElement: new Element('html'),
    createElement: tag => new Element(tag),
    createDocumentFragment: () => new Element('#fragment'),
    getElementById: id => ids.get(id) || null,
};
global.window = { matchMedia: () => ({ matches: true }) };
let beforeOpenCalls = 0;

const create = (id, tag = 'div') => { const node = new Element(tag); node.id = id; return node; };
const workspace = create('output-floating-workspace');
const panel = create('output-floating-panel', 'aside');
workspace.appendChild(panel);
['output-panel-title', 'output-panel-project', 'run-skills-used', 'run-skills-used-count', 'run-execution-content', 'run-results-content'].forEach(id => panel.appendChild(create(id)));
for (const name of ['skills', 'execution', 'results']) {
    const tab = create(`output-tab-${name}`, 'button');
    const pane = create(`output-pane-${name}`, 'section');
    workspace.appendChild(tab);
    panel.appendChild(pane);
    create(`output-tab-${name}-badge`);
}

vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'), { filename: process.argv[1] });
window.workbenchRunInspector.init({
    apiFetch: async () => { throw new Error('unexpected request'); },
    createIcons() {},
    showToast() {},
    beforeOpen() { beforeOpenCalls += 1; },
});

const api = window.workbenchRunInspector;
const state = api.getState();
const skillsPane = ids.get('output-pane-skills');
const skillsTab = ids.get('output-tab-skills');
const initial = {
    open: api.isOpen(),
    panelHidden: panel.hidden,
    panelAriaHidden: panel.getAttribute('aria-hidden'),
    skillsPaneHidden: skillsPane.hidden,
    rootOpen: document.documentElement.classList.contains('run-inspector-open'),
    beforeOpenCalls,
};

const paneButton = new Element('button');
skillsPane.appendChild(paneButton);
paneButton.focus();
skillsTab.click();
const collapsed = {
    open: api.isOpen(),
    panelHidden: panel.hidden,
    panelAriaHidden: panel.getAttribute('aria-hidden'),
    skillsPaneHidden: skillsPane.hidden,
    focusReturned: document.activeElement === skillsTab,
    rootOpen: document.documentElement.classList.contains('run-inspector-open'),
};

api.setAvailable(false);
api.selectTab('results');
const unavailable = {
    workspaceHidden: workspace.hidden,
    open: api.isOpen(),
    expanded: state.expanded,
    activeTab: state.activeTab,
    panelHidden: panel.hidden,
};

api.setAvailable(true);
const restoredWorkspace = {
    workspaceHidden: workspace.hidden,
    open: api.isOpen(),
    expanded: state.expanded,
};
ids.get('output-tab-results').emit('keydown', { key: 'ArrowUp' });
const keyboardOpen = {
    open: api.isOpen(),
    activeTab: state.activeTab,
    focusedExecution: document.activeElement === ids.get('output-tab-execution'),
    executionExpanded: ids.get('output-tab-execution').getAttribute('aria-expanded'),
    executionPaneHidden: ids.get('output-pane-execution').hidden,
};

const fallback = new Element('button');
ids.get('output-tab-execution').focus();
api.setAvailable(false, { focusTarget: fallback });
const suspendedOpen = {
    open: api.isOpen(),
    expanded: state.expanded,
    focusReturned: document.activeElement === fallback,
};
api.selectTab('skills');
api.setAvailable(true);
const restoredOpen = {
    open: api.isOpen(),
    expanded: state.expanded,
    activeTab: state.activeTab,
    skillsPaneHidden: skillsPane.hidden,
};
paneButton.focus();
api.selectTab('results');
const programmaticSwitch = {
    activeTab: state.activeTab,
    focusedResults: document.activeElement === ids.get('output-tab-results'),
    skillsPaneHidden: skillsPane.hidden,
};
const mailLease = api.claimContentOwner('mail:mail-1');
const operationLease = api.claimContentOwner('operation:operation-1');
const ownerIsolation = {
    staleMailRejected: !api.contentOwnerMatches(mailLease),
    currentOperationAccepted: api.contentOwnerMatches(operationLease),
    released: api.releaseContentOwner('operation:operation-1'),
    ownerAfterRelease: api.getContentOwner().owner,
    generationAdvanced: api.getContentOwner().generation > operationLease.generation,
};

console.log(JSON.stringify({
    initial, collapsed, unavailable, restoredWorkspace, keyboardOpen,
    suspendedOpen, restoredOpen, programmaticSwitch, ownerIsolation,
}));
"""
    completed = subprocess.run(
        ["node", "-e", script, str(ROOT / "frontend" / "run-inspector.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {
        "initial": {
            "open": True,
            "panelHidden": False,
            "panelAriaHidden": "false",
            "skillsPaneHidden": False,
            "rootOpen": True,
            "beforeOpenCalls": 1,
        },
        "collapsed": {
            "open": False,
            "panelHidden": True,
            "panelAriaHidden": "true",
            "skillsPaneHidden": True,
            "focusReturned": True,
            "rootOpen": False,
        },
        "unavailable": {
            "workspaceHidden": True,
            "open": False,
            "expanded": False,
            "activeTab": "results",
            "panelHidden": True,
        },
        "restoredWorkspace": {
            "workspaceHidden": False,
            "open": False,
            "expanded": False,
        },
        "keyboardOpen": {
            "open": True,
            "activeTab": "execution",
            "focusedExecution": True,
            "executionExpanded": "true",
            "executionPaneHidden": False,
        },
        "suspendedOpen": {
            "open": False,
            "expanded": False,
            "focusReturned": True,
        },
        "restoredOpen": {
            "open": True,
            "expanded": True,
            "activeTab": "skills",
            "skillsPaneHidden": False,
        },
        "programmaticSwitch": {
            "activeTab": "results",
            "focusedResults": True,
            "skillsPaneHidden": True,
        },
        "ownerIsolation": {
            "staleMailRejected": True,
            "currentOperationAccepted": True,
            "released": True,
            "ownerAfterRelease": "chat",
            "generationAdvanced": True,
        },
    }


def test_compact_viewport_reconciles_open_drawer_output_and_progress_surfaces():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const ids = new Map();
let viewportWidth = 1200;
const collapsedProgress = [];

class ClassList {
    constructor(...values) { this.values = new Set(values); }
    add(...values) { values.forEach(value => this.values.add(value)); }
    remove(...values) { values.forEach(value => this.values.delete(value)); }
    contains(value) { return this.values.has(value); }
}

class Element {
    constructor(id, ...classes) {
        this.id = id;
        this.hidden = false;
        this.inert = false;
        this.classList = new ClassList(...classes);
        this.attributes = {};
        this.parentNode = null;
        ids.set(id, this);
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    removeAttribute(name) { delete this.attributes[name]; }
    contains(node) {
        for (let current = node; current; current = current.parentNode) {
            if (current === this) return true;
        }
        return false;
    }
    focus() { document.activeElement = this; }
}

const drawer = new Element('chat-drawer');
const drawerControl = new Element('drawer-control');
drawerControl.parentNode = drawer;
const rail = new Element('rail-chat', 'active');
const executionTab = new Element('output-tab-execution');
global.document = {
    activeElement: drawerControl,
    getElementById: id => ids.get(id) || null,
};
global.window = {
    matchMedia(query) {
        const limit = Number(query.match(/max-width:\s*(\d+)px/)?.[1] || 0);
        return { matches: viewportWidth <= limit };
    },
    workbenchRunInspector: {
        isOpen: () => true,
        getState: () => ({ activeTab: 'execution' }),
    },
};
global.taskProgressCenter = { hidden: false };
global.agentCollaborationPanel = { hidden: true };
global.artifactsSandboxPanel = { classList: new ClassList() };
global.btnSandboxToggle = null;
global.setTaskProgressCollapsed = value => collapsedProgress.push(value);

function take(start, end) {
    const at = source.indexOf(start);
    if (at < 0) throw new Error(`missing ${start}`);
    return source.slice(at, source.indexOf(end, at));
}
vm.runInThisContext(
    take('function syncChatDrawerA11y', 'function collapseCompactChatDrawer')
    + take('function collapseCompactChatDrawer', 'function syncRightSidebarForViewport')
    + take('function syncRightSidebarForViewport', 'function prepareRunInspectorOpen')
);

syncRightSidebarForViewport();
const wide = {
    drawerCollapsed: drawer.classList.contains('collapsed'),
    progressCollapses: collapsedProgress.length,
};

viewportWidth = 900;
syncRightSidebarForViewport();
const compact = {
    drawerCollapsed: drawer.classList.contains('collapsed'),
    drawerAriaHidden: drawer.getAttribute('aria-hidden'),
    drawerInert: drawer.inert,
    inertAttribute: Object.prototype.hasOwnProperty.call(drawer.attributes, 'inert'),
    railActive: rail.classList.contains('active'),
    railExpanded: rail.getAttribute('aria-expanded'),
    focusMovedToInspector: document.activeElement === executionTab,
    progressCollapses: collapsedProgress.length,
};

drawer.classList.remove('collapsed');
syncChatDrawerA11y(drawer);
const reopened = {
    drawerAriaHidden: drawer.getAttribute('aria-hidden'),
    drawerInert: drawer.inert,
    inertAttribute: Object.prototype.hasOwnProperty.call(drawer.attributes, 'inert'),
};

console.log(JSON.stringify({ wide, compact, reopened }));
"""
    completed = subprocess.run(
        ["node", "-e", script, str(ROOT / "frontend" / "app.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "wide": {
            "drawerCollapsed": False,
            "progressCollapses": 0,
        },
        "compact": {
            "drawerCollapsed": True,
            "drawerAriaHidden": "true",
            "drawerInert": True,
            "inertAttribute": True,
            "railActive": True,
            "railExpanded": "false",
            "focusMovedToInspector": True,
            "progressCollapses": 1,
        },
        "reopened": {
            "drawerAriaHidden": "false",
            "drawerInert": False,
            "inertAttribute": False,
        },
    }


def test_run_inspector_render_preserves_keyed_interaction_and_resets_cross_context_scroll():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const ids = new Map();

class ClassList {
    constructor(owner) { this.owner = owner; }
    values() { return new Set(String(this.owner.className || '').split(/\s+/).filter(Boolean)); }
    write(values) { this.owner.className = [...values].join(' '); }
    toggle(name, force) {
        const values = this.values();
        const enabled = force === undefined ? !values.has(name) : Boolean(force);
        enabled ? values.add(name) : values.delete(name);
        this.write(values);
        return enabled;
    }
    contains(name) { return this.values().has(name); }
}

class Element {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.className = '';
        this.classList = new ClassList(this);
        this.attributes = {};
        this.dataset = {};
        this.listeners = {};
        this.hidden = false;
        this.disabled = false;
        this.tabIndex = 0;
        this.textContent = '';
        this.scrollTop = 0;
        this.open = false;
    }
    set id(value) { this._id = String(value); ids.set(this._id, this); }
    get id() { return this._id || ''; }
    get childNodes() { return this.children; }
    append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
    appendChild(node) {
        if (!node || typeof node !== 'object') return node;
        node.parentNode = this;
        this.children.push(node);
        return node;
    }
    replaceChildren(...nodes) {
        this.children.forEach(node => { if (node?.parentNode === this) node.parentNode = null; });
        this.children = [];
        this.append(...nodes);
    }
    contains(node) {
        for (let current = node; current; current = current.parentNode) {
            if (current === this) return true;
        }
        return false;
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    removeEventListener(name, fn) { this.listeners[name] = (this.listeners[name] || []).filter(item => item !== fn); }
    focus() { document.activeElement = this; }
}

global.document = {
    activeElement: null,
    documentElement: new Element('html'),
    createElement: tag => new Element(tag),
    createDocumentFragment: () => new Element('#fragment'),
    getElementById: id => ids.get(id) || null,
};
global.window = { matchMedia: () => ({ matches: false }) };

const create = (id, tag = 'div') => { const node = new Element(tag); node.id = id; return node; };
const workspace = create('output-floating-workspace');
const panel = create('output-floating-panel', 'aside');
workspace.appendChild(panel);
const title = create('output-panel-title');
const project = create('output-panel-project');
panel.append(title, project);
const panes = {};
for (const name of ['skills', 'execution', 'results']) {
    const tab = create(`output-tab-${name}`, 'button');
    const pane = create(`output-pane-${name}`, 'section');
    const badge = create(`output-tab-${name}-badge`);
    panes[name] = pane;
    workspace.append(tab, badge);
    panel.appendChild(pane);
}
const count = create('run-skills-used-count');
const skills = create('run-skills-used');
const execution = create('run-execution-content');
const results = create('run-results-content');
panes.skills.append(count, skills);
panes.execution.appendChild(execution);
panes.results.appendChild(results);

const response = body => ({ ok: true, status: 200, json: async () => body });
const apiFetch = async url => {
    if (url.includes('/sessions/')) return response({ success: true, runs: [] });
    if (url.includes('/projects/project-a/vcs/status')) {
        return response({ success: true, project_id: 'project-a', vcs: { available: true, changes: [] } });
    }
    if (url.includes('/projects/project-b/vcs/status')) {
        return response({ success: true, project_id: 'project-b', vcs: { available: true, changes: [] } });
    }
    throw new Error(`unexpected request ${url}`);
};
const all = root => {
    const found = [];
    const visit = node => (node?.children || []).forEach(child => { found.push(child); visit(child); });
    visit(root);
    return found;
};
const findState = fragment => all(panel).find(node => String(node.dataset?.inspectorStateKey || '').includes(fragment));
const findFocus = fragment => all(panel).find(node => String(node.dataset?.inspectorFocusKey || '').endsWith(fragment));

vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'), { filename: process.argv[1] });
const toasts = [];
const api = window.workbenchRunInspector;
api.init({
    apiFetch,
    createIcons() {},
    showToast(message, tone) { toasts.push({ message, tone }); },
});

(async () => {
    await api.setContext({ sessionId: 'session-a', projectId: 'project-a', projectName: 'A' });
    const source = { runId: 'run-a', sessionId: 'session-a', projectId: 'project-a' };
    api.beginRun(source);
    api.handleEvent('artifact_created', {
        run_id: 'run-a',
        artifact: { artifact_id: 'artifact-a', files: [{ path: 'report.txt' }] },
    }, source);
    api.selectTab('results');
    const originalDetails = findState(':file:report.txt');
    const originalSummary = all(originalDetails).find(node => node.tagName === 'SUMMARY');
    const originalPreview = all(originalDetails).find(node => node.tagName === 'PRE');
    originalDetails.open = true;
    originalDetails.dataset.loaded = 'true';
    originalPreview.textContent = 'cached safe preview';
    originalSummary.focus();
    panes.results.scrollTop = 137;

    api.handleEvent('validation', { run_id: 'run-a', name: 'pytest', passed: true }, source);
    const restoredDetails = findState(':file:report.txt');
    const restoredSummary = all(restoredDetails).find(node => node.tagName === 'SUMMARY');
    const restoredPreview = all(restoredDetails).find(node => node.tagName === 'PRE');
    const resultInteraction = {
        replaced: restoredDetails !== originalDetails,
        open: restoredDetails.open,
        loaded: restoredDetails.dataset.loaded,
        preview: restoredPreview.textContent,
        focusRestored: document.activeElement === restoredSummary,
        scrollTop: panes.results.scrollTop,
    };

    api.handleEvent('approval_required', {
        run_id: 'run-a', approval_id: 'approval-a', capability: 'github.issue.write',
    }, source);
    const originalApprove = findFocus(':approve');
    originalApprove.focus();
    panes.execution.scrollTop = 88;
    api.handleEvent('progress', { run_id: 'run-a', message: 'still running' }, source);
    const restoredApprove = findFocus(':approve');
    const approvalInteraction = {
        replaced: restoredApprove !== originalApprove,
        focusRestored: document.activeElement === restoredApprove,
        scrollTop: panes.execution.scrollTop,
    };

    api.setAvailable(false);
    api.handleEvent('approval_required', {
        run_id: 'run-a', approval_id: 'approval-hidden', capability: 'notion.content.write',
    }, source);
    const hiddenApproval = {
        tone: toasts.at(-1)?.tone,
        guided: toasts.at(-1)?.message.includes('回到「聊天」')
            && toasts.at(-1)?.message.includes('「執行」'),
        remainsClosed: !api.isOpen(),
    };
    api.setAvailable(true);

    panes.skills.scrollTop = 21;
    panes.execution.scrollTop = 22;
    panes.results.scrollTop = 23;
    findFocus(':approve').focus();
    await api.setContext({ sessionId: 'session-b', projectId: 'project-b', projectName: 'B' });
    const crossContext = {
        scroll: [panes.skills.scrollTop, panes.execution.scrollTop, panes.results.scrollTop],
        focusHandedToTab: document.activeElement === ids.get('output-tab-execution'),
        staleDetailsAbsent: !findState(':file:report.txt'),
    };

    console.log(JSON.stringify({ resultInteraction, approvalInteraction, hiddenApproval, crossContext }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", script, str(ROOT / "frontend" / "run-inspector.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "resultInteraction": {
            "replaced": True,
            "open": True,
            "loaded": "true",
            "preview": "cached safe preview",
            "focusRestored": True,
            "scrollTop": 137,
        },
        "approvalInteraction": {
            "replaced": True,
            "focusRestored": True,
            "scrollTop": 88,
        },
        "hiddenApproval": {
            "tone": "warning",
            "guided": True,
            "remainsClosed": True,
        },
        "crossContext": {
            "scroll": [0, 0, 0],
            "focusHandedToTab": True,
            "staleDetailsAbsent": True,
        },
    }


def test_run_inspector_tabs_hydration_and_live_events():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const ids = new Map();

class ClassList {
    constructor(owner) { this.owner = owner; }
    values() { return new Set(String(this.owner.className || '').split(/\s+/).filter(Boolean)); }
    write(values) { this.owner.className = [...values].join(' '); }
    add(...names) { const values = this.values(); names.forEach(name => values.add(name)); this.write(values); }
    remove(...names) { const values = this.values(); names.forEach(name => values.delete(name)); this.write(values); }
    contains(name) { return this.values().has(name); }
    toggle(name, force) {
        const values = this.values();
        const enabled = force === undefined ? !values.has(name) : Boolean(force);
        enabled ? values.add(name) : values.delete(name);
        this.write(values);
        return enabled;
    }
}

class Element {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.children = [];
        this.className = '';
        this.classList = new ClassList(this);
        this.attributes = {};
        this.dataset = {};
        this.listeners = {};
        this.hidden = false;
        this.tabIndex = 0;
        this.textContent = '';
        this.disabled = false;
    }
    set id(value) { this._id = String(value); ids.set(this._id, this); }
    get id() { return this._id || ''; }
    get childNodes() { return this.children; }
    append(...nodes) { nodes.forEach(node => this.children.push(node)); }
    appendChild(node) { this.children.push(node); return node; }
    replaceChildren(...nodes) { this.children = nodes; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    emit(name, event = {}) {
        event.currentTarget ||= this;
        event.preventDefault ||= () => {};
        for (const fn of this.listeners[name] || []) fn(event);
    }
    click() { this.emit('click'); }
    focus() { document.activeElement = this; }
}

global.document = {
    activeElement: null,
    createElement: tag => new Element(tag),
    createDocumentFragment: () => new Element('#fragment'),
    getElementById: id => ids.get(id) || null,
};
global.window = {};

const create = id => { const node = new Element('div'); node.id = id; return node; };
[
    'output-floating-panel', 'output-panel-title', 'output-panel-project',
    'run-skills-used', 'run-skills-used-count', 'run-execution-content', 'run-results-content',
].forEach(create);
for (const name of ['skills', 'execution', 'results']) {
    create(`output-tab-${name}`);
    create(`output-pane-${name}`);
    create(`output-tab-${name}-badge`);
}

const response = body => ({ ok: true, status: 200, json: async () => body });
const calls = [];
const apiFetch = async url => {
    calls.push(url);
    if (url.includes('/sessions/session-b/runs')) return response({ success: true, session_id: 'session-b', runs: [] });
    if (url.includes('/sessions/session-a/runs')) return response({
        success: true, session_id: 'session-a',
        runs: [{ run_id: 'run-a', session_id: 'session-a', project_id: 'project-a', status: 'completed', model: 'model-a' }],
    });
    if (url.endsWith('/runs/run-a/execution')) return response({
        success: true, run_id: 'run-a', session_id: 'session-a', project_id: 'project-a', status: 'completed',
        tasks: [{ id: 'one', title: 'Check', status: 'completed' }],
        events: [{ event: 'tool_end', created_at: '10:00', payload: { tool: 'read_file', status: 'completed' } }],
        retry: { allowed: false, reason: 'run_completed' },
    });
    if (url.endsWith('/runs/run-a/results')) return response({
        success: true, run_id: 'run-a', session_id: 'session-a', project_id: 'project-a', status: 'completed',
        artifacts: [], changes: [{ path: 'frontend/app.js', action: 'modified' }],
        validations: [{ name: 'pytest', passed: true }], sources: [{ name: 'notes.md', kind: 'attachment' }],
        vcs: { run_evidence: { committed_this_run: false, pushed_this_run: false } },
    });
    if (url.endsWith('/runs/run-a/skills')) return response({
        success: true, run_id: 'run-a', session_id: 'session-a', project_id: 'project-a',
        skills: [{ skill_slug: 'review', name: 'Review', version: '1.0.0', trigger_mode: 'session' }],
    });
    if (url.endsWith('/runs/run-live/execution')) return response({
        success: true, run_id: 'run-live', session_id: 'session-a', project_id: 'project-a', status: 'running',
        tasks: [{ id: 'persisted', title: 'Persisted phase', status: 'in_progress' }], events: [], retry: { allowed: false },
    });
    if (url.endsWith('/runs/run-live/skills')) return response({
        success: true, run_id: 'run-live', session_id: 'session-a', project_id: 'project-a',
        skills: [{ skill_slug: 'live-review', name: 'Live Review', version: '2.0.0', trigger_mode: 'session' }],
    });
    if (url.endsWith('/projects/project-a/vcs/status')) return response({
        success: true, project_id: 'project-a', vcs: { available: true, branch: 'main', commit: 'abc1234', dirty: true, ahead: 0, behind: 0 },
    });
    if (url.endsWith('/projects/project-b/vcs/status')) return response({
        success: true, project_id: 'project-b', vcs: { available: false, changes: [] },
    });
    throw new Error(`unexpected request ${url}`);
};

vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'), { filename: process.argv[1] });
window.workbenchRunInspector.init({ apiFetch, apiBase: '', createIcons() {}, showToast() {} });

(async () => {
    await window.workbenchRunInspector.setContext({ sessionId: 'session-a', projectId: 'project-a', projectName: 'Project A' });
    let state = window.workbenchRunInspector.getState();
    const hydrated = {
        runId: state.run.runId,
        skill: state.usedSkills.items[0].skill_slug,
        task: state.execution.tasks[0].title,
        event: state.execution.events[0].tool,
        change: state.results.changes[0].path,
        branch: state.workspaceVcs.value.branch,
    };

    ids.get('output-tab-execution').click();
    const executionOpen = !ids.get('output-floating-panel').hidden && state.activeTab === 'execution';
    ids.get('output-tab-execution').click();
    const collapsed = ids.get('output-floating-panel').hidden;
    ids.get('output-tab-skills').emit('keydown', { key: 'ArrowDown' });
    const keyboardSelected = state.activeTab;

    window.workbenchRunInspector.beginRun({ runId: 'run-live', sessionId: 'session-a', projectId: 'project-a', model: 'model-a' });
    const liveSkillsLoading = state.usedSkills.status === 'loading';
    window.workbenchRunInspector.handleEvent('meta', { run_id: 'run-live', session_id: 'session-a', project_id: 'project-a' });
    window.workbenchRunInspector.handleEvent('plan', { run_id: 'run-live', tasks: [{ id: 'live', title: 'Live task', status: 'in_progress' }] });
    window.workbenchRunInspector.handleEvent('file_change', { run_id: 'run-live', relative_path: 'frontend/live.js', change_type: 'modified' });
    window.workbenchRunInspector.handleEvent('validation', { run_id: 'another-run', name: 'ignored', passed: false });
    await new Promise(resolve => setTimeout(resolve, 0));
    state = window.workbenchRunInspector.getState();
    console.log(JSON.stringify({
        hydrated,
        executionOpen,
        collapsed,
        keyboardSelected,
        liveTask: state.execution.tasks.find(item => item.id === 'live')?.title,
        hydratedRunningTask: state.execution.tasks.some(item => item.title === 'Persisted phase'),
        liveSkillsLoading,
        liveSkill: state.usedSkills.items[0]?.skill_slug,
        normalizedLiveChange: state.results.changes[0],
        ignoredForeignRun: state.results.validations.length === 0,
        calls,
    }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", script, str(ROOT / "frontend" / "run-inspector.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["hydrated"] == {
        "runId": "run-a",
        "skill": "review",
        "task": "Check",
        "event": "read_file",
        "change": "frontend/app.js",
        "branch": "main",
    }
    assert result["executionOpen"] is True
    assert result["collapsed"] is True
    assert result["keyboardSelected"] == "execution"
    assert result["liveTask"] == "Live task"
    assert result["hydratedRunningTask"] is True
    assert result["liveSkillsLoading"] is True
    assert result["liveSkill"] == "live-review"
    assert result["normalizedLiveChange"]["path"] == "frontend/live.js"
    assert result["normalizedLiveChange"]["action"] == "modified"
    assert result["ignoredForeignRun"] is True
    assert any("/sessions/session-a/runs?limit=1" in call for call in result["calls"])


def test_run_inspector_rejects_stale_stream_cancels_approval_and_uses_authoritative_retry():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const ids = new Map();
class ClassList {
    constructor(owner) { this.owner = owner; }
    values() { return new Set(String(this.owner.className || '').split(/\s+/).filter(Boolean)); }
    write(values) { this.owner.className = [...values].join(' '); }
    toggle(name, force) { const values = this.values(); const on = force === undefined ? !values.has(name) : !!force; on ? values.add(name) : values.delete(name); this.write(values); return on; }
}
class Element {
    constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.className = ''; this.classList = new ClassList(this); this.attributes = {}; this.dataset = {}; this.listeners = {}; this.hidden = false; this.tabIndex = 0; this.textContent = ''; this.disabled = false; }
    set id(value) { this._id = String(value); ids.set(this._id, this); }
    get id() { return this._id || ''; }
    get childNodes() { return this.children; }
    append(...nodes) { this.children.push(...nodes); }
    appendChild(node) { this.children.push(node); return node; }
    replaceChildren(...nodes) { this.children = nodes; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    removeEventListener(name, fn) { this.listeners[name] = (this.listeners[name] || []).filter(item => item !== fn); }
    focus() { document.activeElement = this; }
}
global.document = { activeElement: null, createElement: tag => new Element(tag), createDocumentFragment: () => new Element('#fragment'), getElementById: id => ids.get(id) || null };
global.window = {};
const create = id => { const node = new Element('div'); node.id = id; return node; };
['output-floating-panel', 'output-panel-title', 'output-panel-project', 'run-skills-used', 'run-skills-used-count', 'run-execution-content', 'run-results-content'].forEach(create);
for (const name of ['skills', 'execution', 'results']) { create(`output-tab-${name}`); create(`output-pane-${name}`); create(`output-tab-${name}-badge`); }
const response = body => ({ ok: true, status: 200, json: async () => body });
const calls = [];
let resolveSessionBLatest;
const sessionBLatest = new Promise(resolve => { resolveSessionBLatest = resolve; });
const apiFetch = async (url, options = {}) => {
    calls.push(url);
    if (url.includes('/sessions/session-a/runs')) return response({ success: true, session_id: 'session-a', runs: [{ run_id: 'run-failed', session_id: 'session-a', project_id: 'project-a', status: 'failed', model: 'model-a' }] });
    if (url.includes('/sessions/session-b/runs')) return sessionBLatest;
    if (url.endsWith('/runs/run-failed/execution')) return response({ success: true, run_id: 'run-failed', session_id: 'session-a', project_id: 'project-a', status: 'failed', tasks: [], events: [], error: { message: 'failed' }, retry: { allowed: false, reason: 'error_not_recoverable' } });
    if (url.endsWith('/runs/run-failed/results')) return response({ success: true, run_id: 'run-failed', session_id: 'session-a', project_id: 'project-a', artifacts: [], changes: [], validations: [], sources: [], vcs: { run_evidence: { commits: [{ commit: 'deadbeef', success: false }], pushes: [{ commit: 'deadbeef', success: false }], committed_this_run: false, pushed_this_run: false } } });
    if (url.endsWith('/runs/run-failed/skills')) return response({ success: true, run_id: 'run-failed', session_id: 'session-a', project_id: 'project-a', skills: [] });
    if (url.endsWith('/runs/run-terminal/execution')) return response({ success: true, run_id: 'run-terminal', session_id: 'session-b', project_id: 'project-b', status: 'failed', tasks: [{ id: 'persisted', title: 'Saved failure', status: 'failed' }], events: [], error: { message: 'recoverable' }, retry: { allowed: true } });
    if (url.endsWith('/runs/run-terminal/results')) return response({ success: true, run_id: 'run-terminal', session_id: 'session-b', project_id: 'project-b', artifacts: [], changes: [], validations: [], sources: [], vcs: null });
    if (url.endsWith('/runs/run-terminal/skills')) return response({ success: true, run_id: 'run-terminal', session_id: 'session-b', project_id: 'project-b', skills: [] });
    if (url.includes('/projects/project-a/vcs/status')) return response({ success: true, project_id: 'project-a', vcs: { available: true, branch: 'main', dirty: false, changes: [] } });
    if (url.includes('/projects/project-b/vcs/status')) return response({ success: true, project_id: 'project-b', vcs: { available: true, branch: 'main', dirty: false, changes: [] } });
    if (url.includes('/api/chat/runs/')) return response({ success: true });
    throw new Error(`unexpected request ${url}`);
};
const allText = node => [node?.textContent || '', ...(node?.children || []).map(allText)].join(' ');
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'), { filename: process.argv[1] });
window.workbenchRunInspector.init({ apiFetch, apiBase: '', createIcons() {}, showToast() {}, retryRun() {} });
(async () => {
    await window.workbenchRunInspector.setContext({ sessionId: 'session-a', projectId: 'project-a' });
    const initialExecutionText = allText(ids.get('run-execution-content'));
    const initialResultsText = allText(ids.get('run-results-content'));
    const sourceA = { runId: 'run-failed', sessionId: 'session-a', projectId: 'project-a' };
    const pending = window.workbenchRunInspector.handleApproval({ approval_id: 'approval-a', run_id: 'run-failed', capability: 'read' }, sourceA)
        .then(() => 'resolved', error => error.name);
    const switching = window.workbenchRunInspector.setContext({ sessionId: 'session-b', projectId: 'project-b' });
    const approvalOutcome = await pending;
    const staleAccepted = window.workbenchRunInspector.handleEvent('plan', { run_id: 'run-failed', tasks: [{ id: 'stale' }] }, sourceA);
    const sourceB = { runId: 'run-terminal', sessionId: 'session-b', projectId: 'project-b' };
    window.workbenchRunInspector.beginRun(sourceB);
    window.workbenchRunInspector.handleEvent('error', { run_id: 'run-terminal', message: 'temporary failure' }, sourceB);
    const retryBeforeSnapshot = window.workbenchRunInspector.getState().execution.retry?.allowed;
    resolveSessionBLatest(response({
        success: true,
        session_id: 'session-b',
        runs: [{ run_id: 'run-stale-latest', session_id: 'session-b', project_id: 'project-b', status: 'completed' }],
    }));
    await switching;
    await new Promise(resolve => setTimeout(resolve, 0));
    const state = window.workbenchRunInspector.getState();
    console.log(JSON.stringify({
        approvalOutcome,
        staleAccepted,
        contextSession: state.context.sessionId,
        runId: state.run?.runId,
        retryBeforeSnapshot,
        retryAfterSnapshot: state.execution.retry?.allowed,
        taskAfterSnapshot: state.execution.tasks[0]?.title,
        retryHiddenWhenDenied: !initialExecutionText.includes('重新執行本輪'),
        failedGitShown: (initialResultsText.match(/失敗/g) || []).length >= 2,
        falsePushNotShown: !initialResultsText.includes('已推送'),
        terminalHydrated: calls.some(url => url.endsWith('/runs/run-terminal/results')) && calls.some(url => url.endsWith('/runs/run-terminal/skills')),
    }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", script, str(ROOT / "frontend" / "run-inspector.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {
        "approvalOutcome": "AbortError",
        "staleAccepted": False,
        "contextSession": "session-b",
        "runId": "run-terminal",
        "retryBeforeSnapshot": False,
        "retryAfterSnapshot": True,
        "taskAfterSnapshot": "Saved failure",
        "retryHiddenWhenDenied": True,
        "failedGitShown": True,
        "falsePushNotShown": True,
        "terminalHydrated": True,
    }
