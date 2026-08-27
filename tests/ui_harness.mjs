/**
 * Runs static/analysis.js under Node against recorded API payloads.
 *
 * There is no browser in the test environment, but the risk worth covering is
 * the rendering code itself: a renamed API field or a bad property access would
 * throw at click time and show an empty panel. So this provides just enough DOM
 * for the real script to load, stubs `fetch` with payloads captured from the
 * live API, clicks each button, and reports what was rendered.
 *
 * Usage:  node tests/ui_harness.mjs <analysis.js> <payloads.json>
 * Output: a JSON report on stdout; exit code 1 if any action errored.
 */

import { readFileSync } from "node:fs";
import { runInThisContext } from "node:vm";

const [scriptPath, payloadPath] = process.argv.slice(2);
const payloads = JSON.parse(readFileSync(payloadPath, "utf8"));

// -- minimal DOM ------------------------------------------------------------

class ClassList {
  constructor() { this.items = new Set(); }
  add(...names) { names.forEach((n) => this.items.add(n)); }
  remove(...names) { names.forEach((n) => this.items.delete(n)); }
  contains(name) { return this.items.has(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.items.has(name) : Boolean(force);
    if (on) this.items.add(name); else this.items.delete(name);
    return on;
  }
  get value() { return [...this.items].join(" "); }
}

class Element {
  constructor(tag, id) {
    this.tagName = tag;
    this.id = id || "";
    this.children = [];
    this.listeners = {};
    this._text = "";
    this.classList = new ClassList();
    this.hidden = false;
    this.disabled = false;
    this.attributes = {};
    this.value = "";
    this.checked = false;
  }
  set className(value) {
    this.classList.items = new Set(String(value).split(/\s+/).filter(Boolean));
  }
  get className() { return this.classList.value; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() {
    return this._text + this.children.map((c) => c.textContent).join(" ");
  }
  set innerHTML(value) { if (!value) this.children = []; this._html = value; }
  get innerHTML() { return this._html || ""; }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelectorAll(selector) {
    if (selector === "input:checked") {
      return this.children.flatMap((c) => c.children).filter((c) => c.checked);
    }
    return [];
  }
  click() {
    return Promise.all((this.listeners.click || []).map((h) => h()));
  }
  descendantText() {
    return [this._text, ...this.children.map((c) => c.descendantText())]
      .filter(Boolean)
      .join(" ");
  }
  countTag(tag) {
    return (this.tagName === tag ? 1 : 0)
      + this.children.reduce((sum, c) => sum + c.countTag(tag), 0);
  }
}

const elements = new Map();
const ids = [
  "analysis-status", "analysis-output", "chat-panel", "analysis-panel",
  "tab-chat", "tab-analysis", "diagram-kinds",
  "btn-inventory", "btn-oop", "btn-diagrams", "btn-tools",
];
for (const id of ids) elements.set(id, new Element("div", id));

// The diagram-kind checkboxes the page ships with.
const kindsHost = elements.get("diagram-kinds");
for (const value of ["class", "inheritance", "dependency"]) {
  const label = new Element("label");
  const input = new Element("input");
  input.value = value;
  input.checked = true;
  label.appendChild(input);
  kindsHost.appendChild(label);
}

const documentListeners = {};
globalThis.document = {
  getElementById: (id) => elements.get(id) || null,
  createElement: (tag) => new Element(tag),
  addEventListener: (type, handler) => { (documentListeners[type] ||= []).push(handler); },
  dispatchEvent: (event) => {
    (documentListeners[event.type] || []).forEach((h) => h(event));
    return true;
  },
};
globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = (init || {}).detail; } };
// Node >=21 exposes a read-only `navigator`, so define the property instead
// of assigning over it.
Object.defineProperty(globalThis, "navigator", {
  value: { clipboard: { writeText: async () => {} } },
  configurable: true,
  writable: true,
});
globalThis.Node = Element;

// -- fetch stub -------------------------------------------------------------

const requests = [];
globalThis.fetch = async (path, options) => {
  requests.push({ path, method: (options && options.method) || "GET" });
  const key = Object.keys(payloads).find((candidate) => path.includes(candidate));
  if (!key) {
    return { ok: false, status: 404, statusText: "Not Found", json: async () => ({ detail: `no stub for ${path}` }) };
  }
  return { ok: true, status: 200, statusText: "OK", json: async () => payloads[key] };
};

// -- run --------------------------------------------------------------------

runInThisContext(readFileSync(scriptPath, "utf8"), { filename: scriptPath });

const report = { actions: {}, requests: [], failures: [] };

async function act(name, buttonId) {
  elements.get("analysis-output").children = [];
  elements.get("analysis-status").classList.remove("error");
  await elements.get(buttonId).click();
  // Let the mermaid import() rejection and any promise chains settle.
  for (let i = 0; i < 20; i += 1) await new Promise((r) => setImmediate(r));

  const status = elements.get("analysis-status");
  const output = elements.get("analysis-output");
  const failed = status.classList.contains("error");
  report.actions[name] = {
    status: status.textContent,
    failed,
    cards: output.children.length,
    tables: output.countTag("table"),
    text: output.descendantText().slice(0, 400),
  };
  if (failed) report.failures.push(`${name}: ${status.textContent}`);
  if (!failed && output.children.length === 0) {
    report.failures.push(`${name}: rendered nothing`);
  }
}

document.dispatchEvent(new CustomEvent("repo-selected", { detail: { repo: "demo" } }));

await act("tools", "btn-tools");
await act("inventory", "btn-inventory");
await act("oop", "btn-oop");
await act("diagrams", "btn-diagrams");

report.requests = requests;
process.stdout.write(JSON.stringify(report, null, 2));
process.exit(report.failures.length ? 1 : 0);
