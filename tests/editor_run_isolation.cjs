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
let debounce;
let editorTransport;
const context = vm.createContext({
  document, window: {monaco: {
    KeyMod: {CtrlCmd: 1}, KeyCode: {KeyS: 1}, MarkerSeverity: {Error: 8},
    editor: {createModel, create: createEditor, createDiffEditor: createEditor, EndOfLinePreference: {LF: 0}, setTheme() {},
      setModelMarkers(model, _, markers) { model.markers = markers; },
      setModelLanguage(model, language) { model.setLanguage(language); }},
  }},
  crypto: {randomUUID},
  snapshot: {run_id: "A"}, workspaceTabs: new Element(),
  workspaceRequest(path, body) {
    if (editorTransport) return editorTransport(path, body);
    return new Promise((resolve, reject) => pending.push({path, body, resolve, reject}));
  },
  setTimeout(callback) { debounce = callback; return callback; }, clearTimeout(timer) { if (debounce === timer) debounce = undefined; },
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
    createElement: () => new Element(), documentElement: new Element(), body: new Element(),
    dispatchEvent(event) { return this.emit(event.type); },
  });
  let actions;
  let proposeAnchors;
  let promptCallbacks;
  let deferredPrompt = false;
  const modelRecords = new Map();
  const modelGroup = new Element();
  const modelFields = new Map();
  modelGroup.querySelector = selector => {
    if (!modelFields.has(selector)) modelFields.set(selector, new Element());
    return modelFields.get(selector);
  };
  let exerciseModels = false, modelCounter = 0;
  let promptId = 0;
  const progressDialog = {button: new Element(), dialog: new Element(), input: new Element(), render() {},
    showError(error) { this.error = error?.message; }};
  const anchor = dashboardElement("[data-propose-anchors]");
  page.querySelectorAll = selector => selector === ".step" ? Array.from({length: 7}, () => new Element())
    : selector === "[data-propose-anchors]" ? [anchor]
    : selector === ".run-model:has([data-run-model])" && exerciseModels ? [modelGroup] : [];
  const location = new URL("http://localhost/dashboard/?run=A");
  const runtime = vm.createContext({
    document: page, Event, URL, URLSearchParams, Headers, TextEncoder, location,
    Option: class { constructor(label, value) { this.label = label; this.value = value; } },
    effortLabels: {low: "Low", high: "High"},
    history: {replaceState(_, __, url) { location.href = url.href; }},
    crypto: {randomUUID: () => ++promptId === 1 ? "11111111-1111-4111-8111-111111111111" : randomUUID()},
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
    bindProgressPromptDialog(callbacks) { promptCallbacks = callbacks; return progressDialog; },
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
      if (path === "prompts" && !options.body) return Promise.resolve({ok: true, json: async () => ({
        run_id: options.headers.get("X-Run-ID"), conversation: [], prompts: [],
      })});
      if (path === "editor" && body.kind === "conversation") {
        const receipt = {ok: true, json: async () => ({id: body.id, text: body.text, status: "received"})};
        if (!deferredPrompt) return Promise.resolve(receipt);
        deferredPrompt = false;
        return new Promise((resolve, reject) => {
          request.accept = () => resolve(receipt);
          request.lose = () => reject(Error("fixture lost successful response"));
        });
      }
      if (path === "junie-models") return Promise.resolve({ok: true, json: async () => ({models: [
        {id: "old", name: "Old", efforts: ["low"]}, {id: "new", name: "New", efforts: ["high"]},
      ]})});
      if (path === "run-model") {
        const run_id = options.headers.get("X-Run-ID");
        const saved = modelRecords.get(run_id) ?? {model: "old", effort: "low", revision: null};
        return Promise.resolve({ok: true, json: async () => ({run_id, can_select: true, ...saved})});
      }
      if (path === "editor" && body.kind === "model") {
        return new Promise((resolve, reject) => {
          request.accept = () => {
            const revision = (++modelCounter).toString(16).padStart(24, "0");
            modelRecords.set(body.run_id, {model: body.model, effort: body.effort, revision});
            resolve({ok: true, json: async () => ({run_id: body.run_id, junie_model: body.model,
              junie_effort: body.effort, junie_model_revision: revision})});
          };
          request.reject = () => reject(Error("fixture model save failed"));
        });
      }
      if (path === "comparison" || body.action === "anchors") {
        return new Promise(resolve => { request.resolve = resolve; });
      }
      return Promise.resolve({ok: true, json: async () => ({run_id: body.run_id})});
    },
  });
  runtime.window.fetch = runtime.fetch;
  vm.runInContext(fs.readFileSync(process.argv[3], "utf8"), runtime);
  const progress = (stream, runId) => stream.onmessage({data: JSON.stringify({run_id: runId,
    status: exerciseModels ? "interrupted" : "idle", ready: exerciseModels,
    resumable: exerciseModels, diff_viewer_enabled: true, worktree_path: runId})});
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

  progressDialog.dialog.open = true;
  await promptCallbacks.open();
  progressDialog.input.value = "確認してください";
  await promptCallbacks.submit(progressDialog.input.value);
  const sent = requests.find(item => item.path === "editor" && item.body.kind === "conversation");
  assert.equal(sent.body.run_id, "B");
  assert.equal(sent.body.text, "確認してください");
  assert.equal(sent.body.id, "11111111111141118111111111111111");
  assert.equal(progressDialog.input.value, "");
  progressDialog.dialog.open = false;

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

  const openPrompt = async () => {
    progressDialog.dialog.open = true;
    await promptCallbacks.open();
  };
  const writePrompt = text => {
    progressDialog.input.value = text;
    promptCallbacks.inputChanged();
  };
  const sendPrompt = () => promptCallbacks.submit(progressDialog.input.value.trim());
  const lastSend = () => requests.findLast(item => item.path === "editor" && item.body.kind === "conversation");
  await selectRun("A");
  await openPrompt();
  writePrompt("Aの成功応答は遅れて届く");
  deferredPrompt = true;
  const delayed = sendPrompt();
  const sentA = lastSend();
  await selectRun("B");
  await openPrompt();
  writePrompt("Bの未送信下書き");
  sentA.accept();
  await delayed;
  assert.equal(progressDialog.input.value, "Bの未送信下書き");
  await selectRun("A");
  await openPrompt();
  const restoredAfterSuccess = progressDialog.input.value;

  writePrompt("Aの受理済み応答を失う");
  deferredPrompt = true;
  const losing = sendPrompt();
  const lost = lastSend();
  lost.lose();
  await losing;
  assert.match(progressDialog.error, /fixture lost successful response/);
  await selectRun("B");
  await openPrompt();
  assert.equal(progressDialog.input.value, "Bの未送信下書き");
  await sendPrompt();
  assert.equal(lastSend().body.run_id, "B");
  await selectRun("A");
  await openPrompt();
  assert.equal(progressDialog.input.value, lost.body.text);
  await sendPrompt();
  const retried = lastSend();
  assert.deepEqual({restoredAfterSuccess, preservesIdAfterOtherRun: retried.body.id === lost.body.id},
    {restoredAfterSuccess: "", preservesIdAfterOtherRun: true});
  assert.equal(retried.body.run_id, "A");
  assert.equal(progressDialog.input.value, "");

  writePrompt("書き直す前の本文");
  deferredPrompt = true;
  const redrafting = sendPrompt();
  const earlier = lastSend();
  writePrompt("より新しい未送信の本文");
  await selectRun("B");
  earlier.accept();
  await redrafting;
  await selectRun("A");
  assert.equal(progressDialog.input.value, "より新しい未送信の本文");

  await openPrompt();
  writePrompt("同じ文面を書き直す");
  deferredPrompt = true;
  const sameText = sendPrompt();
  const original = lastSend();
  await selectRun("B");
  await openPrompt();
  writePrompt("Aが送信中でもBから送る");
  await sendPrompt();
  assert.equal(lastSend().body.run_id, "B");
  await selectRun("A");
  await openPrompt();
  writePrompt("途中の別文面");
  writePrompt(original.body.text);
  original.accept();
  await sameText;
  assert.equal(progressDialog.input.value, original.body.text, "a recreated draft belongs to a newer edit even with identical text");

  await openPrompt();
  let modelReady;
  runtime.window.harnessRunModelReady = () => new Promise(resolve => { modelReady = resolve; });
  const beforeModelReady = lastSend();
  const whileSavingEditor = sendPrompt();
  await selectRun("B");
  await openPrompt();
  writePrompt(original.body.text);
  modelReady(true);
  await whileSavingEditor;
  assert.equal(lastSend(), beforeModelReady, "a submit gesture cannot follow a Run switch while waiting for the editor");
  delete runtime.window.harnessRunModelReady;
  progressDialog.dialog.open = false;
  exerciseModels = true;
  const query = page.querySelector;
  page.querySelector = selector => selector === "#tab-editor" ? null : query(selector);
  vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), runtime);
  page.querySelector = query;
  const selector = modelGroup.querySelector("[data-run-model]");
  const saves = () => requests.filter(item => item.body.kind === "model");
  const resumes = () => requests.filter(item => ["resume", "resume-run"].includes(item.body.action));
  const changeModel = async model => {
    selector.value = model;
    await selector.emit("change");
    await flush();
    return saves().at(-1);
  };
  const toggle = dashboardElement("#toggle");
  await selectRun("A");
  await flush();
  assert.equal(toggle.disabled, false);
  let saving = await changeModel("new");
  const waiting = toggle.emit("click");
  await toggle.emit("click");
  await actions.execute("resume-run", {run_id: "A"});
  await flush();
  assert.equal(resumes().length, 0, "progress and list must not resume A before the model is saved");

  await actions.execute("resume-run", {run_id: "B"});
  assert.deepEqual(resumes().map(request => request.body.run_id), ["B"], "A's save must not delay B's list action");
  saving.accept();
  await waiting;
  await flush();
  assert.deepEqual(resumes().map(request => [request.body.run_id, request.body.model_revision]), [
    ["B", undefined], ["A", modelRecords.get("A").revision],
  ]);
  assert.equal(modelRecords.get("A").model, "new");

  saving = await changeModel("old");
  const listRestart = actions.execute("resume-run", {run_id: "A"});
  const rejected = assert.rejects(listRestart, /モデル設定を保存できなかった/);
  saving.reject();
  await rejected;
  assert.equal(resumes().length, 2, "failed model save must not post a resume");
  assert.match(modelGroup.querySelector(".run-model-status").textContent, /fixture model save failed/);

  saving = await changeModel("old");
  const staleResume = toggle.emit("click");
  await selectRun("B");
  await selectRun("A");
  saving.accept();
  await staleResume;
  await flush();
  assert.equal(resumes().length, 2, "switching away and back invalidates a waiting click");
  await actions.execute("resume-run", {run_id: "A"});
  assert.equal(resumes().at(-1).body.model_revision, modelRecords.get("A").revision);
  console.log("Dashboard: Run model order, per-Run isolation, failed save, and repeat clicks passed.");
  console.log("Dashboard: delayed progress receipts clear only the original Run draft; retries preserve each Run's ID.");
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

async function checkQueuedEditorHttp(address, runId) {
  const host = new URL(address);
  assert.equal(host.hostname, "127.0.0.1");
  const posted = [];
  let lose = false;
  async function actualRequest(path, body) {
    const url = new URL(path, host);
    assert.equal(url.origin, host.origin);
    const response = await fetch(url, {redirect: "error", ...(body && {
      method: "POST", headers: {"Origin": host.origin, "Content-Type": "application/json"},
      body: JSON.stringify(body),
    })});
    if (!response.ok) {
      if (body) posted.push({body, status: response.status});
      throw Error(await response.text());
    }
    const result = await response.json();
    if (body) {
      posted.push({body, status: response.status, result});
      if (lose) {
        lose = false;
        throw Error("fixture lost successful response");
      }
    }
    return result;
  }
  const button = find("#editor-save");
  const feedback = find("#editor-error");
  const status = find("#editor-save-status");
  const submit = () => button.emit("click");
  editorTransport = actualRequest;
  context.snapshot = {run_id: runId};
  await tick();

  model().setValue("class First {}\n");
  lose = true;
  await submit();
  const first = posted.at(-1);
  assert.equal(first.status, 200);
  assert.match(feedback.textContent, /lost successful response/);
  await submit();
  const repeated = posted.at(-1);
  assert.equal(repeated.body.save_id, first.body.save_id);
  assert.equal(repeated.status, 200, "the server must acknowledge the same already-saved edit");
  assert.deepEqual(repeated.result.edit, first.result.edit);
  assert.match(status.textContent, /保存済み/);

  model().setValue("class Second {}\n");
  lose = true;
  await submit();
  const second = posted.at(-1);
  assert.equal(second.status, 200);
  model().setValue("class PreparedNext {}\n");
  await tick();
  assert.equal(model().getValue(), "class PreparedNext {}\n", "acknowledgement must preserve newer unsaved text");
  assert.equal(feedback.textContent, "");
  await submit();
  const next = posted.at(-1);
  assert.equal(next.body.edit_id, second.result.edit.id);
  assert.notEqual(next.body.save_id, second.body.save_id);
  assert.equal(next.status, 200);

  model().setValue("class Adopted {}\n");
  lose = true;
  await submit();
  const adopted = posted.at(-1);
  assert.equal(adopted.status, 200);
  await actualRequest("fixture-adopt", {});
  const acknowledgement = await actualRequest("editor", adopted.body);
  assert.equal(acknowledgement.edit, null, "do not recreate an edit that the workflow already applied");
  assert.equal(acknowledgement.accepted_save_id, adopted.body.save_id);
  model().setValue("class PreparedAfterAdoption {}\n");
  await tick();
  assert.equal(model().getValue(), "class PreparedAfterAdoption {}\n");
  await submit();
  const afterAdoption = posted.at(-1);
  assert.equal(afterAdoption.body.edit_id, null, "update a draft's base after its own edit has already been applied");
  assert.equal(afterAdoption.body.base, adopted.body.content);
  assert.equal(afterAdoption.status, 200);

  model().setValue("class KeptLocally {}\n");
  const other = await actualRequest("editor", {...afterAdoption.body, content: "class AnotherTab {}\n",
    edit_id: afterAdoption.result.edit.id, save_id: randomUUID().replaceAll("-", "")});
  await tick();
  assert.equal(model().getValue(), "class KeptLocally {}\n");
  await submit();
  assert.equal(posted.at(-1).body.edit_id, afterAdoption.result.edit.id, "a receipt from another editor must not rebase the draft");
  assert.equal(posted.at(-1).status, 409);
  assert.deepEqual((await actualRequest("editor")).edit, other.edit);
  console.log("Dashboard: live HTTP queued saves recover lost receipts without losing drafts or overwriting another tab.");
}

(async () => {
  if (process.argv[5]) {
    await checkQueuedEditorHttp(process.argv[5], process.argv[6]);
    return;
  }
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
  await find("#editor-target-buttons").emit("click", {target: {dataset: {target: "Page.cs"}}});
  const b = model();
  b.setValue("class Draft {");
  const previousDiagnosis = debounce();
  const older = take("roslyn-diagnostics");
  assert.equal(older.body.content, "class Draft {");
  b.setValue("class Draft {}");
  const freshDiagnosis = debounce();
  const newer = take("roslyn-diagnostics");
  const responseFor = ({body}, findings) => ({target: body.target, version: body.version, status: findings.length ? "errors" : "clear", findings, razor: []});
  newer.resolve(responseFor(newer, []));
  await freshDiagnosis;
  const fictional = [{start: 0, length: 2, line: 1, column: 1, code: "CS1513", message: "}"}];
  older.resolve(responseFor(older, fictional));
  await previousDiagnosis;
  assert.deepEqual([...b.markers], [], "an obsolete result must not add markers to the corrected draft");
  b.setValue("class NewDraft {");
  const switchedDiagnosis = debounce();
  const oldRun = take("roslyn-diagnostics");
  await select("A");
  take().resolve(response("A"));
  await flush();
  oldRun.resolve(responseFor(oldRun, fictional));
  await switchedDiagnosis;
  assert.equal(find("#editor-diagnostic-list").children.length, 0, "same target name in another Run is independent");
  await checkConversationFileSwitch();
  console.log("Dashboard: Razor target changes preserve Run chat receipts, unsent drafts, and retry IDs.");
  console.log("Dashboard: same-file run isolation, delayed responses, drafts, saves, and recovery passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
