const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// Exercise the shipped editor with delayed HTTP responses and lightweight DOM/Monaco doubles.
class Element {
  constructor() {
    this.listeners = new Map();
    this.children = [];
    this.attributes = new Map();
    this.dataset = {};
    this.value = this.textContent = "";
    this.hidden = false;
    this.classList = {toggle() {}, contains() { return false; }};
  }
  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(callback);
  }
  async emit(name, fields = {}) {
    await Promise.all((this.listeners.get(name) || []).map(callback => callback({
      target: this, preventDefault() {}, ...fields,
    })));
  }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name); }
  removeAttribute(name) { this.attributes.delete(name); }
  append(...children) {
    this.children.push(...children);
    for (const child of children) if (child.src) queueMicrotask(() => child.emit("load"));
  }
  replaceChildren(...children) { this.children = children; }
  querySelector(selector) { return find(selector); }
  contains() { return false; }
  focus() {}
}
const nodes = new Map();
function find(selector) {
  if (!nodes.has(selector)) nodes.set(selector, new Element());
  return nodes.get(selector);
}
const document = new Element();
Object.assign(document, {querySelector: find, createElement: () => new Element(), head: new Element(), documentElement: new Element()});
find("#tab-editor").setAttribute("aria-selected", "true");
const editors = new Map();
function createModel(value, language) {
  let changed = () => {};
  return {
    getValue: () => value, setValue(text) { value = text; changed(); },
    getLanguageId: () => language, onDidChangeContent(callback) { changed = callback; },
  };
}
function createEditor(host, options = {}) {
  let model = options.model;
  const editor = {
    getModel: () => model, setModel(value) { model = value; },
    setValue(value) { if (model) model.setValue(value); else model = createModel(value); },
    getModifiedEditor: () => editor,
    layout() {}, addCommand() {}, focus() {},
  };
  editors.set(host, editor);
  return editor;
}
const pending = [];
const timers = [];
const context = vm.createContext({
  document, window: {monaco: {
    KeyMod: {CtrlCmd: 1}, KeyCode: {KeyS: 1},
    editor: {createModel, create: createEditor, createDiffEditor: createEditor, EndOfLinePreference: {LF: 0}, setTheme() {}},
  }},
  snapshot: {run_id: "A"}, workspaceTabs: new Element(),
  workspaceRequest(path, body) {
    return new Promise((resolve, reject) => pending.push({path, body, resolve, reject}));
  },
  ResizeObserver: class { observe() {} }, MutationObserver: class { observe() {} },
  addEventListener() {}, setInterval(callback) { timers.push(callback); },
  URL: {createObjectURL() { return "blob:worker"; }}, Blob, URLSearchParams,
  Option: class extends Element { constructor(label, value) { super(); this.textContent = label; this.value = value; } },
});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
const flush = () => new Promise(resolve => setImmediate(resolve));
const tick = () => timers[0]();
const model = () => editors.get(find("#editor-code")).getModel();
const response = (runId, content = `class ${runId} {}`) => ({
  run_id: runId, targets: ["Page.cs"], target: "Page.cs", content,
  edit: null, prompts: [], can_save: true, can_prompt: true, exists: true,
});
async function select(runId) {
  await document.emit("harnessrunselect");
  context.snapshot = {run_id: runId};
  await document.emit("harnessrunchange");
}
function take(endpoint = "editor") {
  const index = pending.findIndex(item => item.path.split("?")[0] === endpoint);
  assert.notEqual(index, -1, `missing ${endpoint} request`);
  return pending.splice(index, 1)[0];
}

async function checkProgressSelection() {
  const streams = [];
  const source = fs.readFileSync(process.argv[3], "utf8").replace(/\r\n/g, "\n");
  const start = source.indexOf("let stream;\n");
  const end = source.indexOf("function receiveProgress(event)");
  assert.ok(start >= 0 && end > start, "missing dashboard progress connection code");
  const runtime = vm.createContext({
    window: {}, document: {dispatchEvent() {}}, Event, URL,
    location: {href: "http://localhost/dashboard/"}, history: {replaceState() {}},
    connected: false, selectedRunId: "", snapshot: {}, connection: {},
    syncActions() {}, receiveProgress(event) { runtime.snapshot = JSON.parse(event.data); },
    EventSource: class {
      constructor() { streams.push(this); }
      close() {}
    },
  });
  vm.runInContext(source.slice(start, end), runtime);
  const a = runtime.connectRun("A");
  streams[0].onopen();
  streams[0].onmessage({data: JSON.stringify({run_id: "A"})});
  await a;
  const b = runtime.connectRun("B");
  streams[1].onopen();
  assert.equal(runtime.connected, false, "controls must stay disabled until the selected run's first event");
  assert.equal(runtime.snapshot.run_id, "A");
  streams[1].onmessage({data: JSON.stringify({run_id: "B"})});
  await b;
  assert.equal(runtime.connected, true);
  streams[0].onmessage({data: JSON.stringify({run_id: "A"})});
  assert.equal(runtime.snapshot.run_id, "B", "events from the old connection must be ignored");

  const requests = [];
  const bindStart = source.indexOf("function runFetch(");
  const bindEnd = source.indexOf("const tokenRuns", bindStart);
  assert.ok(bindStart >= 0 && bindEnd > bindStart, "missing dashboard workspace actions");
  Object.assign(runtime, {
    Headers, dashboardRevision: 0,
    bindHarnessWorkspace(actions) { runtime.actions = actions; },
    fetch(path, options) {
      const body = options.body ? JSON.parse(options.body) : {};
      requests.push({path, headers: options.headers, body});
      return Promise.resolve({ok: true, json: async () => ({run_id: body.run_id})});
    },
  });
  vm.runInContext(source.slice(bindStart, bindEnd), runtime);
  await runtime.actions.execute("hide-run", {run_id: "A"});
  assert.equal(runtime.selectedRunId, "B", "deleting a different run must preserve the selection");
  assert.equal(streams.length, 2);
  const deleting = runtime.actions.execute("hide-run", {run_id: "B"});
  await flush();
  assert.equal(runtime.selectedRunId, "");
  assert.equal(runtime.snapshot.run_id, "B", "the previous snapshot may still be visible while reconnecting");
  await runtime.runFetch("control");
  assert.equal(requests.at(-1).headers.get("X-Run-ID"), null);
  streams[2].onmessage({data: JSON.stringify({status: "idle"})});
  await deleting;
  await runtime.actions.execute("prepare", {unit_ids: ["new-unit"]});
  assert.equal(requests.at(-1).headers.get("X-Run-ID"), null, "new runs must omit the deleted run's ID");
}

(async () => {
  await checkProgressSelection();
  const initial = tick();
  take().resolve(response("A"));
  await initial;
  assert.equal(find("#editor-error").textContent, "");
  const a = model();
  a.setValue("class A { /* unsaved */ }");
  find("#editor-prompt").value = "instruction for A";
  await find("#editor-php-toggle").emit("click");
  const oldPhp = take("editor-php");
  const oldRefresh = tick();
  const oldRequest = take();

  await select("B");
  assert.equal(find("#editor-layout").hidden, true, "old run content must disappear immediately");
  assert.equal(find("#editor-save").disabled, true);
  // The selected run must load even while the previous run's request is still pending.
  const bRequest = take();
  assert.equal(new URLSearchParams(bRequest.path.split("?")[1]).get("run_id"), "B");
  oldPhp.resolve({run_id: "A", sources: ["source.php"], source: "source.php", content: "old A PHP"});
  await flush();
  assert.notEqual(editors.get(find("#editor-php"))?.getModel()?.getValue(), "old A PHP");
  bRequest.resolve(response("B"));
  await flush();
  assert.equal(model().getValue(), "class B {}");
  assert.equal(find("#editor-layout").hidden, false);
  assert.equal(find("#editor-prompt").value, "");
  oldRequest.resolve(response("A", "late A response"));
  await oldRefresh;
  assert.equal(model().getValue(), "class B {}");

  await find("#editor-php-toggle").emit("click");
  take("editor-php").resolve({run_id: "B", sources: ["source.php"], source: "source.php", content: "B PHP"});
  await flush();
  assert.equal(editors.get(find("#editor-php")).getModel().getValue(), "B PHP");
  await find("#editor-target-buttons").emit("click", {target: {dataset: {target: "Page.cs"}}});

  model().setValue("class B { /* edit */ }");
  const saving = find("#editor-save").emit("click");
  const saved = take();
  assert.equal(saved.body.run_id, "B");
  assert.equal(saved.body.base, "class B {}");
  assert.equal(saved.body.content, "class B { /* edit */ }");
  saved.resolve(response("B", saved.body.content));
  await saving;

  await select("A");
  take().resolve(response("A"));
  await flush();
  assert.equal(model(), a);
  assert.equal(model().getValue(), "class A { /* unsaved */ }");
  assert.equal(find("#editor-prompt").value, "instruction for A");

  await select("B");
  take().reject(new Error("network failure"));
  await flush();
  assert.equal(find("#editor-layout").hidden, true, "a failed switch must not show the previous run");
  assert.match(find("#editor-empty").textContent, /network failure/);
  const retry = tick();
  take().resolve(response("A", "wrong run response"));
  await retry;
  assert.equal(find("#editor-layout").hidden, true, "a response for another run must be ignored");
  const recovery = tick();
  take().resolve(response("B", "class B { /* edit */ }"));
  await recovery;
  assert.equal(find("#editor-layout").hidden, false);
  assert.equal(model().getValue(), "class B { /* edit */ }");
  await find("#editor-php-toggle").emit("click");
  const previousPhp = take("editor-php");
  await select("A");
  const abandoned = take();
  await select("B");
  take().resolve(response("B", "class B { /* edit */ }"));
  await flush();
  const freshPhp = take("editor-php");
  previousPhp.resolve({run_id: "B", sources: ["source.php"], source: "source.php", content: "stale B PHP"});
  abandoned.resolve(response("A"));
  freshPhp.resolve({run_id: "B", sources: ["source.php"], source: "source.php", content: "fresh B PHP"});
  await flush();
  assert.equal(editors.get(find("#editor-php")).getModel().getValue(), "fresh B PHP");
  assert.equal(model().getValue(), "class B { /* edit */ }");
  console.log("Dashboard: same-file run isolation, delayed responses, drafts, saves, and recovery passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
