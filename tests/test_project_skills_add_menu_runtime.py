import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_project_skills_add_menu_runtime_interactions():
    script = r"""
const fs = require('fs');
const vm = require('vm');

const ids = new Map();
const documentListeners = new Map();
const windowListeners = new Map();

class FakeClassList {
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

function eventFor(target, key = '') {
    return {
        target,
        key,
        defaultPrevented: false,
        immediateStopped: false,
        preventDefault() { this.defaultPrevented = true; },
        stopPropagation() {},
        stopImmediatePropagation() { this.immediateStopped = true; },
    };
}

class FakeElement {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.children = [];
        this.parentElement = null;
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this.listeners = {};
        this.className = '';
        this.classList = new FakeClassList(this);
        this.textContent = '';
        this.hidden = false;
        this.disabled = false;
        this.tabIndex = 0;
        this.type = '';
    }
    set id(value) { this._id = String(value); if (this._id) ids.set(this._id, this); }
    get id() { return this._id || ''; }
    get firstChild() { return this.children[0] || null; }
    get firstElementChild() { return this.firstChild; }
    get isConnected() {
        let current = this;
        while (current) {
            if (current === document.body) return true;
            current = current.parentElement;
        }
        return false;
    }
    _adopt(node, prepend = false) {
        if (node.parentElement) {
            node.parentElement.children = node.parentElement.children.filter(child => child !== node);
        }
        node.parentElement = this;
        prepend ? this.children.unshift(node) : this.children.push(node);
    }
    append(...nodes) { nodes.forEach(node => this._adopt(node)); }
    appendChild(node) { this._adopt(node); return node; }
    prepend(node) { this._adopt(node, true); }
    replaceChildren(...nodes) {
        this.children.forEach(node => { node.parentElement = null; });
        this.children = [];
        this.append(...nodes);
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return Object.hasOwn(this.attributes, name) ? this.attributes[name] : null; }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(name, listener) { (this.listeners[name] ||= []).push(listener); }
    contains(target) {
        if (target === this) return true;
        return this.children.some(child => child.contains(target));
    }
    querySelectorAll(selector) {
        const matches = [];
        const visit = node => {
            node.children.forEach(child => {
                if (selector === '[role="menuitem"]' && child.getAttribute('role') === 'menuitem') matches.push(child);
                if (selector === '[data-project-skills-project-id]' && child.dataset.projectSkillsProjectId) matches.push(child);
                visit(child);
            });
        };
        visit(this);
        return matches;
    }
    focus() { document.activeElement = this; }
    click() { this.emit('click', eventFor(this)); }
    emit(name, event) {
        for (const listener of this.listeners[name] || []) {
            listener(event);
            if (event.immediateStopped) break;
        }
    }
    getBoundingClientRect() {
        if (this.classList.contains('project-skills-add')) {
            return { x: 900, y: 100, left: 900, right: 932, top: 100, bottom: 132, width: 32, height: 32 };
        }
        if (this.classList.contains('project-skills-add-menu')) {
            return { x: 612, y: 140, left: 612, right: 932, top: 140, bottom: 440, width: 320, height: 300 };
        }
        return { x: 0, y: 0, left: 0, right: 100, top: 0, bottom: 30, width: 100, height: 30 };
    }
}

global.document = {
    body: new FakeElement('body'),
    activeElement: null,
    createElement: tag => new FakeElement(tag),
    getElementById: id => ids.get(id) || null,
    querySelectorAll: selector => document.body.querySelectorAll(selector),
    addEventListener(name, listener) { (documentListeners.get(name) || documentListeners.set(name, []).get(name)).push(listener); },
};
global.window = {
    innerWidth: 1024,
    innerHeight: 768,
    addEventListener(name, listener) { (windowListeners.get(name) || windowListeners.set(name, []).get(name)).push(listener); },
};
global.confirm = () => true;

vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'), { filename: process.argv[1] });

const toasts = [];
window.workbenchProjectSkills.init({
    apiFetch: async () => { throw new Error('unexpected API request'); },
    showToast: (message, kind) => toasts.push({ message, kind }),
});

const findClass = (root, name) => {
    if (root.classList?.contains(name)) return root;
    for (const child of root.children || []) {
        const match = findClass(child, name);
        if (match) return match;
    }
    return null;
};
const fireKey = (target, key) => target.emit('keydown', eventFor(target, key));

const firstSection = window.workbenchProjectSkills.createProjectSection(
    { id: 'project-a', name: 'Project A', archived: false },
    { alwaysExpanded: true },
);
document.body.appendChild(firstSection);
const firstAdd = findClass(firstSection, 'project-skills-add');
firstAdd.click();
const firstMenu = ids.get(firstAdd.getAttribute('aria-controls'));
const firstItems = firstMenu.querySelectorAll('[role="menuitem"]');
const opened = {
    expanded: firstAdd.getAttribute('aria-expanded'),
    hidden: firstMenu.hidden,
    parent: firstMenu.parentElement.tagName,
    focused: document.activeElement.textContent,
    uniqueId: firstMenu.id,
};

fireKey(firstMenu, 'ArrowDown');
const comingSoonFocus = {
    text: document.activeElement.textContent,
    ariaDisabled: document.activeElement.getAttribute('aria-disabled'),
    nativeDisabled: document.activeElement.disabled,
};
document.activeElement.click();
const comingSoonFeedback = { menuOpen: !firstMenu.hidden, toast: toasts.at(-1) };

fireKey(firstMenu, 'End');
fireKey(firstMenu, 'Enter');
const guide = {
    role: firstMenu.getAttribute('role'),
    view: firstMenu.dataset.view,
    visible: !firstMenu._projectSkillsGuide.hidden,
    focus: document.activeElement.getAttribute('aria-label'),
};

const escapeEvent = eventFor(document.activeElement, 'Escape');
for (const listener of documentListeners.get('keydown') || []) {
    listener(escapeEvent);
    if (escapeEvent.immediateStopped) break;
}
const escaped = {
    hidden: firstMenu.hidden,
    expanded: firstAdd.getAttribute('aria-expanded'),
    focusReturned: document.activeElement === firstAdd,
    immediateStopped: escapeEvent.immediateStopped,
};

firstAdd.click();
const secondSection = window.workbenchProjectSkills.createProjectSection(
    { id: 'project-b', name: 'Project B', archived: false },
    { alwaysExpanded: true },
);
document.body.appendChild(secondSection);
const secondAdd = findClass(secondSection, 'project-skills-add');
secondAdd.click();
const secondMenu = ids.get(secondAdd.getAttribute('aria-controls'));
const singleOpen = {
    firstHidden: firstMenu.hidden,
    secondHidden: secondMenu.hidden,
    distinctIds: firstMenu.id !== secondMenu.id,
};

const internalScrollEvent = eventFor(secondMenu);
for (const listener of documentListeners.get('scroll') || []) listener(internalScrollEvent);
const internalScrollKeptOpen = !secondMenu.hidden;

const outside = new FakeElement('div');
document.body.appendChild(outside);
const outsideEvent = eventFor(outside);
for (const listener of documentListeners.get('click') || []) listener(outsideEvent);

const archivedSection = window.workbenchProjectSkills.createProjectSection(
    { id: 'project-c', name: 'Archived', archived: true },
    { alwaysExpanded: true },
);
document.body.appendChild(archivedSection);
const archivedAdd = findClass(archivedSection, 'project-skills-add');

console.log(JSON.stringify({
    opened,
    comingSoonFocus,
    comingSoonFeedback,
    guide,
    escaped,
    singleOpen,
    internalScrollKeptOpen,
    outsideClosed: secondMenu.hidden && secondAdd.getAttribute('aria-expanded') === 'false',
    archived: { disabled: archivedAdd.disabled, label: archivedAdd.getAttribute('aria-label') },
    itemCount: firstItems.length,
}));
"""
    completed = subprocess.run(
        ["node", "-e", script, str(ROOT / "frontend" / "project-skills-sidebar.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)

    assert result["opened"] == {
        "expanded": "true",
        "hidden": False,
        "parent": "BODY",
        "focused": "",
        "uniqueId": "project-skills-add-menu-1",
    }
    assert result["itemCount"] == 4
    assert result["comingSoonFocus"]["ariaDisabled"] == "true"
    assert result["comingSoonFocus"]["nativeDisabled"] is False
    assert result["comingSoonFeedback"]["menuOpen"] is True
    assert result["comingSoonFeedback"]["toast"]["kind"] == "info"
    assert result["guide"] == {
        "role": "dialog",
        "view": "guide",
        "visible": True,
        "focus": "返回新增 Skill 選單",
    }
    assert result["escaped"] == {
        "hidden": True,
        "expanded": "false",
        "focusReturned": True,
        "immediateStopped": True,
    }
    assert result["singleOpen"] == {
        "firstHidden": True,
        "secondHidden": False,
        "distinctIds": True,
    }
    assert result["internalScrollKeptOpen"] is True
    assert result["outsideClosed"] is True
    assert result["archived"] == {"disabled": True, "label": "封存專案無法新增 Skill"}
