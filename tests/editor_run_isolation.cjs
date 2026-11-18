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
    this.style = {};
    this.options = [];
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
  get parentElement() { return this; }
  get childNodes() { return this.children.map(child => typeof child === "string" ? {cloneNode: () => child} : child); }
  cloneNode() { return this; }
  querySelectorAll() { return []; }
  toggleAttribute() {}
  hasAttribute(name) { return this.attributes.has(name); }
  before() {}
  after() {}
  close() { this.open = false; }
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

async function checkDashboard() {
  const streams = [];
  const requests = [];
  const popups = [];
  const opened = [];
  const dashboardNodes = new Map();
  function dashboardElement(selector) {
    if (!dashboardNodes.has(selector)) dashboardNodes.set(selector, new Element());
    return dashboardNodes.get(selector);
  }
  const page = new Element();
  Object.assign(page, {
    querySelector: dashboardElement,
    createElement: () => new Element(), documentElement: new Element(), dispatchEvent() {},
  });
  let actions;
  let proposeAnchors;
  const anchor = dashboardElement("[data-propose-anchors]");
  page.querySelectorAll = selector => selector === ".step" ? Array.from({length: 7}, () => new Element())
    : selector === "[data-propose-anchors]" ? [anchor] : [];
  const location = new URL("http://localhost/dashboard/?run=A");
  const runtime = vm.createContext({
    document: page, Event, URL, URLSearchParams, Headers, TextEncoder, location,
    history: {replaceState(_, __, url) { location.href = url.href; }},
    window: {
      addEventListener() {},
      open() {
        const popup = {
          closed: false, close() { this.closed = true; },
          document: {body: {}}, location: {replace(url) { opened.push(url); }},
        };
        popups.push(popup);
        return popup;
      },
    },
    EventSource: class {
      constructor(path) { this.path = path; streams.push(this); }
      close() { this.closed = true; }
    },
    bindJunieLog: () => ({button: new Element()}),
    bindComparisonOptions(button, callback) { proposeAnchors = callback; },
    bindProgressPromptDialog: () => ({button: new Element(), dialog: new Element(), input: new Element(), render() {}}),
    bindHarnessWorkspace(value) { actions = value; },
    bindTokenRuns: () => ({}),
    migrationLedgerButton: new Element(), migrationLedgerDialog: new Element(),
    workspaceTabs: new Element(), workspaceControls() {}, setInterval() {},
    renderRunTokenSummary() {}, updateMigrationLedgerStatuses() {},
    fetch(path, options) {
      const body = options.body ? JSON.parse(options.body) : {};
      const request = {path, headers: options.headers, body};
      requests.push(request);
      if (path === "comparison" || body.action === "anchors") {
        return new Promise(resolve => { request.resolve = resolve; });
      }
      return Promise.resolve({ok: true, json: async () => ({run_id: body.run_id})});
    },
  });
  vm.runInContext(fs.readFileSync(process.argv[3], "utf8"), runtime);
  const progress = (stream, runId) => stream.onmessage({data: JSON.stringify({run_id: runId, status: "idle", ready: false, diff_viewer_enabled: true, worktree_path: runId})});
  progress(streams[0], "A");
  const switching = actions.execute("open-run", {run_id: "B"});
  streams[1].onopen();
  assert.equal(actions.canAct(), false, "controls must stay disabled until the selected run's first event");
  assert.match(dashboardElement(".run-detail").textContent, /Run ID A/);
  progress(streams[1], "B");
  await switching;
  assert.equal(actions.canAct(), true);
  progress(streams[0], "A");
  assert.match(dashboardElement(".run-detail").textContent, /Run ID B/, "ignore events from the old connection");

  await actions.execute("hide-run", {run_id: "A"});
  assert.equal(location.searchParams.get("run"), "B", "deleting a different run must preserve the selection");
  assert.equal(streams.length, 2);
  const deleting = actions.execute("hide-run", {run_id: "B"});
  await flush();
  assert.equal(location.searchParams.get("run"), null);
  assert.match(dashboardElement(".run-detail").textContent, /Run ID B/, "previous snapshot is visible while reconnecting");
  await actions.execute("prepare", {unit_ids: ["new-unit"]});
  assert.equal(requests.at(-1).headers.get("X-Run-ID"), null);
  progress(streams[2]);
  await deleting;
  await actions.execute("prepare", {unit_ids: ["new-unit"]});
  assert.equal(requests.at(-1).headers.get("X-Run-ID"), null, "new runs must omit the deleted run's ID");

  const selectRun = async runId => {
    const selecting = actions.execute("open-run", {run_id: runId});
    progress(streams.at(-1), runId);
    await selecting;
  };
  const comparisonButton = dashboardElement(".runbar [data-compare]");
  await selectRun("A");
  assert.equal(comparisonButton.disabled, false);
  const cancelled = proposeAnchors();
  assert.equal(comparisonButton.disabled, true);
  requests.at(-1).resolve({ok: false, text: async () => "取り消しました"});
  await cancelled;
  assert.equal(popups[0].closed, true);
  assert.equal(comparisonButton.disabled, false);

  const plain = comparisonButton.emit("click");
  assert.equal(requests.at(-1).path, "comparison");
  requests.at(-1).resolve({ok: true, json: async () => ({run_id: "A", id: "plain"})});
  await plain;
  const stale = proposeAnchors();
  const staleRequest = requests.at(-1);
  await selectRun("B");
  staleRequest.resolve({ok: true, json: async () => ({run_id: "A", id: "stale"})});
  await stale;
  assert.equal(popups[2].closed, true);
  const retry = proposeAnchors();
  assert.equal(requests.at(-1).path, "control");
  requests.at(-1).resolve({ok: true, json: async () => ({run_id: "B", id: "anchors"})});
  await retry;
  assert.deepEqual(opened, ["review/A/plain.html", "review/B/anchors.html"]);
}

(async () => {
  await checkDashboard();
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
