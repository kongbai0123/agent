import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
