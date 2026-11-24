const assert = require("node:assert/strict");
const {randomUUID} = require("node:crypto");
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
  prepend(...children) { this.children.unshift(...children); }
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
    setLanguage(id) { language = id; },
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
    editor: {createModel, create: createEditor, createDiffEditor: createEditor, EndOfLinePreference: {LF: 0}, setTheme() {},
      setModelLanguage(model, language) { model.setLanguage(language); }},
  }},
  crypto: {randomUUID},
  snapshot: {run_id: "A"}, workspaceTabs: new Element(),
  workspaceRequest(path, body) {
    return new Promise((resolve, reject) => pending.push({path, body, resolve, reject}));
  },
  ResizeObserver: class { observe() {} }, MutationObserver: class { observe() {} },
  addEventListener() {}, setInterval(callback) { timers.push(callback); },
  URL: {createObjectURL() { return "blob:worker"; }}, Blob, URLSearchParams,
  Option: class extends Element { constructor(label, value) { super(); this.textContent = label; this.value = value; } },
});
vm.runInContext(fs.readFileSync(process.argv[4], "utf8"), context);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
const flush = () => new Promise(resolve => setImmediate(resolve));
const tick = () => timers[0]();
const model = () => editors.get(find("#editor-code")).getModel();
const response = (runId, content = `class ${runId} {}`) => ({
  run_id: runId, targets: ["Page.cs"], target: "Page.cs", content,
  edit: null, prompts: [], can_save: true, can_prompt: true, exists: true,
});
// Drive the editor with the two events emitted before and after the initial SSE update.
async function select(runId) {
  await document.emit("harnessrunselect");
  context.snapshot = {run_id: runId};
  await document.emit("harnessrunchange");
}
// Remove a pending request so each response can be delivered in the test’s chosen order.
function take(endpoint = "editor") {
  const index = pending.findIndex(item => item.path.split("?")[0] === endpoint);
  assert.notEqual(index, -1, `missing ${endpoint} request`);
  return pending.splice(index, 1)[0];
}

// Exercise the production dashboard entry point with controllable SSE, HTTP, and popup doubles.
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
    getElementById: id => dashboardNodes.get("#" + id) ?? null,
    createElement: () => new Element(), documentElement: new Element(), body: new Element(), dispatchEvent() {},
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
      addEventListener() {}, setTimeout() {},
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
      if (path === "junie-requests") return Promise.resolve({ok: true, json: async () => []});
      const body = options.body ? JSON.parse(options.body) : {};
      const request = {path, headers: options.headers, body};
      requests.push(request);
      if (path === "comparison" || body.action === "anchors") {
        return new Promise(resolve => { request.resolve = resolve; });
      }
      return Promise.resolve({ok: true, json: async () => ({run_id: body.run_id})});
    },
  });
  runtime.window.fetch = runtime.fetch;
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

async function checkConversationFileSwitch() {
  const run = "RazorRun", code = "Page.cshtml.cs", markup = "Page.cshtml";
  const composer = find("#editor-prompt"), submit = find("#editor-prompt-form");
  const send = find("#editor-send"), turns = find("#editor-prompts"), error = find("#editor-prompt-error");
  const selectFile = target => find("#editor-target-buttons").emit("click", {target: {dataset: {target}}});
  const razor = (target, conversation = []) => ({
    ...response(run, target === code ? "class Page {}" : "@page"),
    targets: [code, markup], target, conversation,
  });
  const enter = async text => { composer.value = text; await composer.emit("input"); };
  const reply = ({body}) => ({id: body.id, text: body.text, status: "received"});
  const readFile = async (target, history) => {
    const read = pending.length ? undefined : tick();
    const request = take();
    assert.equal(request.body, undefined);
    assert.equal(new URLSearchParams(request.path.split("?")[1]).get("target"), target);
    request.resolve(razor(target, history));
    await read;
    await flush();
  };

  await select(run);
  const firstRead = take();
  firstRead.resolve(razor(code));
  await flush();
  const verify = "保存した版でビルドして";
  await enter(` ${verify} `);
  const sending = submit.emit("submit");
  const first = take();
  assert.equal(first.body.run_id, run);
  assert.equal(first.body.text, verify);
  await selectFile(markup);
  const unexpectedDuplicate = submit.emit("submit");
  await unexpectedDuplicate;
  assert.equal(pending.filter(item => item.body).length, 0);
  const firstReceipt = reply(first);
  first.resolve(firstReceipt);
  await sending;
  assert.equal(composer.value, "", "a receipt for the same Run must clear its submitted draft after a file switch");
  assert.equal(send.disabled, true);
  assert.equal(turns.children.length, 1, "show the receipt without waiting for polling");
  await submit.emit("submit");
  assert.equal(pending.filter(item => item.body).length, 0);
  await readFile(markup, [firstReceipt]);
  assert.equal(model().getValue(), "@page");
  assert.equal(turns.children.length, 1);

  const followup = "チェッカーをもう一度";
  await enter(followup);
  const failing = submit.emit("submit");
  const lost = take();
  await selectFile(code);
  lost.reject(new Error("simulated response lost after acceptance"));
  await failing;
  assert.equal(composer.value, followup);
  assert.match(error.textContent, /simulated response lost/);
  assert.equal(send.disabled, false);
  const accepted = reply(lost);
  await readFile(code, [firstReceipt, accepted]);
  const retrying = submit.emit("submit");
  const retry = take();
  assert.equal(retry.body.id, lost.body.id, "a lost response must reuse the original request ID");
  await selectFile(markup);
  await enter("後から入力した指示");
  retry.resolve(accepted);
  await retrying;
  assert.equal(composer.value, "後から入力した指示", "a receipt must not clear a newer draft");
  assert.equal(turns.children.length, 2, "a retry receipt must update the existing turn rather than duplicate it");
  await readFile(markup, [firstReceipt, accepted]);
  assert.equal(composer.value, "後から入力した指示");

  const abandonedSend = submit.emit("submit");
  const abandoned = take();
  assert.equal(context.window.harnessEditorCanSwitch(), false);
  await select("B");
  abandoned.resolve(reply(abandoned));
  await abandonedSend;
  if (!pending.length) tick();
  take().resolve(response("B"));
  await flush();
  assert.equal(composer.value, "", "a prior Run's receipt never updates the new Run");
  assert.equal(turns.children.length, 0);
  await select(run);
  take().resolve(razor(code, [firstReceipt, accepted, reply(abandoned)]));
  await flush();
  assert.equal(composer.value, abandoned.body.text, "preserve the draft when its success cannot be applied");
  const recover = submit.emit("submit");
  const recovered = take();
  assert.equal(recovered.body.id, abandoned.body.id, "an unapplied success must not discard the retry ID");
  recovered.resolve(reply(abandoned));
  await recover;
  assert.equal(composer.value, "");
  assert.equal(turns.children.length, 3);
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
  assert.match(saved.body.save_id, /^[0-9a-f]{32}$/);
  saved.reject(new Error("simulated response loss"));
  await saving;
  const retrySave = find("#editor-save").emit("click");
  const retried = take();
  assert.equal(retried.body.save_id, saved.body.save_id, "retry a lost save response with the same operation ID");
  retried.reject(new Error("simulated response loss"));
  await retrySave;
  model().setValue("class B { /* changed again */ }");
  model().setValue(saved.body.content);
  const editedSave = find("#editor-save").emit("click");
  const freshSave = take();
  assert.notEqual(freshSave.body.save_id, saved.body.save_id, "a new edit must not reuse a rejected save ID");
  freshSave.resolve(response("B", freshSave.body.content));
  await editedSave;

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
  await checkConversationFileSwitch();
  console.log("Dashboard: Razor target changes preserve Run chat receipts, unsent drafts, and retry IDs.");
  console.log("Dashboard: same-file run isolation, delayed responses, drafts, saves, and recovery passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
