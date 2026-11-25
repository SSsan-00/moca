import {checkBuildReviewDrafts} from "./build_review_drafts_browser.mjs";
import {checkStoppedRepairRecovery} from "./stopped_repair_browser.mjs";
import {checkAdoptedEditorSave, checkMergedEditorSave} from "./editor_adoption_browser.mjs";
import {checkEditorSemantics} from "./editor_semantic_browser.mjs";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import {createServer} from "node:http";
import {fileURLToPath, pathToFileURL} from "node:url";
import {spawn} from "node:child_process";
import {setTimeout as pause} from "node:timers/promises";
import {checkRuleSourceEditor} from "./rule_source_browser.mjs";

const [executable, profile, url, notificationUrl, expectedRun, liveLogPath, ruleConfigPath] = process.argv.slice(2);
let socket, sequence = 0;
const pending = new Map();
async function eventually(read, label, attempts = 100) {
  for (let index = 0; index < attempts; index++) {
    const value = await read();
    if (value) return value;
    await pause(100);
  }
  throw new Error("Timed out: " + label);
}
async function cdp(method, params = {}, waitMs = 10_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pending.delete(id);
      reject(new Error("Browser command timed out: " + method));
    }, waitMs);
    pending.set(id, message => {
      clearTimeout(timeout);
      pending.delete(id);
      if (message.error) reject(new Error(message.error.message));
      else resolve(message.result);
    });
    socket.send(JSON.stringify({id, method, params}));
  });
}

async function connectPage(debuggerUrl, deadline) {
  socket = new WebSocket(debuggerUrl);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Chromium DevTools socket did not open")),
      Math.max(1, deadline - performance.now()));
    socket.addEventListener("open", () => { clearTimeout(timer); resolve(); }, {once: true});
    socket.addEventListener("error", () => {
      clearTimeout(timer);
      reject(new Error("Chromium DevTools socket failed to open"));
    }, {once: true});
  });
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (message.method === "Runtime.exceptionThrown") console.error("Browser error", message.params.exceptionDetails?.exception?.description || message.params.exceptionDetails?.text);
    pending.get(message.id)?.(message);
  });
  await cdp("Runtime.enable");
  await cdp("Page.enable", {}, Math.max(1, deadline - performance.now()));
}

// Launch Chromium with a fresh profile and wait for the DevTools endpoint.
async function startBrowser() {
  const failures = [];
  for (let attempt = 1; attempt <= 2; attempt++) {
    const directory = `${profile}-${attempt}`;
    const chrome = spawn(executable, [
      "--headless", "--remote-debugging-port=0", "--no-first-run", "--disable-background-networking",
      "--disable-extensions", "--enable-logging=stderr", "--user-data-dir=" + directory, "about:blank",
    ], {stdio: ["ignore", "ignore", "pipe"]});
    let spawnError, stderr = "";
    chrome.once("error", error => { spawnError = error; });
    chrome.stderr.setEncoding("utf8");
    chrome.stderr.on("data", chunk => { stderr = (stderr + chunk).slice(-8192); });
    let ready = false;
    try {
      const deadline = performance.now() + 25_000;
      while (performance.now() < deadline) {
        if (spawnError) throw spawnError;
        if (chrome.exitCode !== null || chrome.signalCode !== null) {
          throw new Error(`Chromium exited during startup (code=${chrome.exitCode}, signal=${chrome.signalCode})`);
        }
        const portFile = await fs.readFile(path.join(directory, "DevToolsActivePort"), "utf8").catch(error => {
          // Windows may briefly lock the file while Chromium publishes the port.
          if (error.code === "ENOENT" || error.code === "EBUSY") return "";
          throw error;
        });
        const port = portFile.split("\n")[0].trim();
        if (/^[0-9]+$/.test(port)) {
          const endpoint = `http://127.0.0.1:${port}/json/list`;
          const pages = await fetch(endpoint, {signal: AbortSignal.timeout(500)})
            .then(response => response.ok ? response.json() : null)
            .catch(() => null);
          const page = pages?.find(item => item.type === "page" && item.webSocketDebuggerUrl);
          if (page) {
            await connectPage(page.webSocketDebuggerUrl, deadline);
            ready = true;
            return chrome;
          }
        }
        await pause(100);
      }
      throw new Error("Timed out waiting for Chromium DevTools");
    } catch (error) {
      failures.push(`Attempt ${attempt}: ${error.message}\n${stderr.trim() || "(no browser stderr)"}`);
    } finally {
      if (!ready) {
        socket?.close();
        socket = null;
        if (chrome.exitCode === null && chrome.signalCode === null) chrome.kill();
      }
    }
  }
  throw new Error(`Chromium did not start:\n${failures.join("\n")}`);
}

let staticServer;
const chrome = await startBrowser();
try {
  function pageLoaded() {
    return new Promise((resolve, reject) => {
      const receive = event => {
        if (JSON.parse(event.data).method !== "Page.loadEventFired") return;
        clearTimeout(timer);
        socket.removeEventListener("message", receive);
        resolve();
      };
      const timer = setTimeout(() => {
        socket.removeEventListener("message", receive);
        reject(new Error("Browser page did not finish loading"));
      }, 15_000);
      socket.addEventListener("message", receive);
    });
  }
  async function navigate(method, params) {
    await Promise.all([pageLoaded(), cdp(method, params)]);
  }
  async function evaluate(expression, userGesture = false) {
    const result = await cdp("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true, userGesture});
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text);
    return result.result.value;
  }
  const wait = (expression, attempts) => eventually(async () => {
    try {
      return await evaluate(expression);
    } catch (error) {
      // Chrome may replace the JavaScript context during a real navigation.
      if (!/Execution context was destroyed|Cannot find context|Inspected target navigated or closed/.test(error.message)) throw error;
      return false;
    }
  }, expression, attempts);
  const field = id => `document.getElementById(${JSON.stringify(id)})`;
  const click = id => evaluate(`${field(id)}.click()`);
  const change = (id, value, event = "change") => evaluate(`
    (element => { element.value = ${JSON.stringify(value)}; element.dispatchEvent(new Event(${JSON.stringify(event)}, {bubbles: true})); })(${field(id)})
  `);
  const value = id => evaluate(`${field(id)}.value`);
  const folders = "settings-context-folders";
  const save = 'document.querySelector("#workspace-config-form button[type=submit]")';
  const toml = field("workspace-config");
  console.log("Browser:", (await cdp("Browser.getVersion")).product);
  await cdp("Page.addScriptToEvaluateOnNewDocument", {source: "(" + function () {
    if (!new URL(location.href).searchParams.has("notice")) return;
    const NativeEventSource = window.EventSource;
    window.EventSource = class extends NativeEventSource {
      constructor(url) {
        super(url);
        this.onerror = null;
        // Report a reconnectable error before the first real event.
        this.bootstrapError = true;
      }
      set onerror(handler) {
        super.onerror = handler;
        if (handler && this.bootstrapError) {
          this.bootstrapError = false;
          handler(new Event("error"));
        }
      }
      get onerror() { return super.onerror; }
    };
  }.toString() + ")()"});
  await navigate("Page.navigate", {url: notificationUrl});
  const notificationState = `({
    progressSelected: ${field("tab-progress")}?.getAttribute("aria-selected") === "true",
    run: document.querySelector(".run-detail")?.innerText ?? "",
    state: ${field("state-label")}?.innerText ?? "",
    feedback: ${field("workspace-run-feedback")}?.textContent ?? "",
  })`;
  let observed;
  try {
    await eventually(async () => {
      observed = await evaluate(notificationState);
      return observed.progressSelected && observed.run.includes(expectedRun) && observed.run.includes("Example.cs")
        && observed.state.includes("スクリプトで停止");
    }, "notification opens the failed Run in Progress");
  } catch (error) {
    throw new Error(`${error.message}; last dashboard state: ${JSON.stringify(observed)}`, {cause: error});
  }
  console.log("Browser notification: opaque activation showed the failed Run in Progress");
  const liveLog = "junieLog.windows.get(" + JSON.stringify(expectedRun) + ")?.popup.document";
  const logRow = index => liveLog + "?.querySelector('[data-log-entries] li:nth-child(" + index + ")')";
  const logAt = "2026-11-14T01:42:03Z";
  const fence = String.fromCharCode(96).repeat(3);
  const afterUpdate = "<SOURCE>構造化されていない追記</SOURCE>";
  const update = [
    "<UPDATE><PREVIOUS_STEP>",
    "注文の移行を確認。",
    '<UNKNOWN name="literal">原文</UNKNOWN>',
    "<em>XMLタグは文字として表示</em>",
    fence + "xml",
    '<UPDATE><DATA priority="1">コード内のXML</DATA></UPDATE>',
    fence,
    "</PREVIOUS_STEP>",
    "<EXTERNAL>確認結果をまとめています。</EXTERNAL>",
    "</UPDATE>",
    afterUpdate,
  ].join("\r\n");
  const formattedBody = update.slice(update.indexOf("\r\n") + 2, update.lastIndexOf("\r\n</PREVIOUS_STEP>"));
  const code = fence + "xml\n<UPDATE><PREVIOUS_STEP>XMLの例</PREVIOUS_STEP></UPDATE>\n" + fence;
  const unfinished = "<UPDATE><PREVIOUS_STEP>閉じていない更新";
  const literal = "<UPDATE><UNKNOWN>仕様外の要素</UNKNOWN></UPDATE>";
  const records = ["通常のログ", update, code, unfinished, literal];
  const jsonLine = thought => JSON.stringify({at: logAt, thought}) + "\n";
  await fs.writeFile(liveLogPath, jsonLine(records[0]), "utf8");
  await evaluate("(" + function () {
    const request = window.fetch;
    window.receivedLogPolls = 0;
    window.fetch = async (...args) => {
      const response = await request(...args);
      if (String(args[0]).includes("/junie-thoughts?")) window.receivedLogPolls++;
      return response;
    };
  }.toString() + ")()");
  await evaluate("document.querySelector('.junie-log-trigger').click()", true);
  await wait(logRow(1) + "?.textContent.includes('通常のログ')");
  const json = jsonLine(update);
  const split = json.indexOf("PREVIOUS_STEP") + 5;
  await fs.appendFile(liveLogPath, json.slice(0, split));
  const polls = await evaluate("window.receivedLogPolls");
  await wait("window.receivedLogPolls > " + polls);
  assert.equal(await evaluate("!!" + logRow(2)), false, "partial records must not reach the DOM");
  const rest = json.slice(split) + records.slice(2).map(jsonLine).join("");
  await fs.appendFile(liveLogPath, rest);
  await wait(logRow(5) + "?.textContent.includes('仕様外の要素')");
  const structured = logRow(2);
  const sections = await evaluate("Array.from(" + structured + ".querySelectorAll('section'), node => [node.querySelector('h3').textContent, node.querySelector('p').textContent])");
  assert.deepEqual(sections, [["前の工程", formattedBody], ["利用者向けの進捗", "確認結果をまとめています。"]]);
  assert.equal(await evaluate(structured + ".querySelector('section + p').textContent"), "\r\n" + afterUpdate);
  assert.equal(await evaluate(structured + ".innerText.includes('<PREVIOUS_STEP>')"), false);
  assert.equal(await evaluate(structured + ".querySelector('em,unknown,source') === null"), true, "model text must never become DOM markup");
  assert.equal(await evaluate(structured + ".querySelector('details').open"), false);
  assert.equal(await evaluate(structured + ".querySelector('details pre').textContent"), update);
  await evaluate(structured + ".querySelector('details summary').click()");
  assert((await evaluate(structured + ".innerText")).includes("<UPDATE><PREVIOUS_STEP>"));
  for (const [offset, original] of records.slice(2).entries()) {
    assert.equal(await evaluate(logRow(3 + offset) + ".querySelector('p').textContent"), original);
  }
  assert.equal(await fs.readFile(liveLogPath, "utf8"), records.map(jsonLine).join(""));
  await evaluate(liveLog + ".defaultView.close()");
  console.log("Browser Junie log: complete updates have headings; partial, unknown, and code remain literal and storage unchanged");
  await navigate("Page.navigate", {url});
  await wait(`document.readyState === "complete" && document.documentElement.dataset.liveReady === "true" && typeof window.harnessEditorCanSwitch === "function" && !!window.harnessRuns && ${field("tab-settings")}?.offsetWidth`);
  await click("tab-progress");
  const checkerNote = "a previous checker finding is still present";
  const checkerPresentation = async reason => evaluate(`(() => {
    renderCheckerStatus(${JSON.stringify({ready: true, status: "checker_needs_review",
      checker_review_reason: reason, error: checkerNote, resumable: true})});
    return [${field("checker-review-reason")}.textContent, ${field("checker-review-technical")}.textContent];
  })()`);
  assert.match((await checkerPresentation("finding_repeated"))[0], /同じチェッカーの指摘/);
  assert.deepEqual(await checkerPresentation(null), ["チェッカーの指摘が残っているため、確認が必要です。", checkerNote]);
  await wait(`!${field("recovery-summary")}.hidden && ${field("recovery-next")}.textContent.includes("実行結果不明")`);
  assert(await evaluate(`${field("recovery-checkpoint")}.textContent.includes("1 / 3")`));
  assert(await evaluate(`${field("recovery-pending")}.textContent.includes("指示 1 件")`));
  assert(await evaluate(`${field("recovery-next")}.textContent.includes("before_migration.log")`));
  await click("recovery-editor");
  assert(await evaluate(`${field("tab-editor")}.getAttribute("aria-selected") === "true"`));
  await checkBuildReviewDrafts({url, evaluate, click, change, value, wait, run: expectedRun,
    tree: path.join(path.dirname(ruleConfigPath), "trees", expectedRun)});
  // Reopen the same Run in a new document instead of relying on Chrome's reload scheduling.
  const recoveryUrl = new URL(url);
  recoveryUrl.searchParams.set("run", expectedRun);
  recoveryUrl.searchParams.set("reopen", "1");
  await navigate("Page.navigate", {url: recoveryUrl.href});
  await wait(`document.documentElement.dataset.liveReady === "true"
    && new URL(location.href).searchParams.get("reopen") === "1"`);
  await click("tab-progress");
  await wait(`!${field("recovery-summary")}.hidden && ${field("recovery-next")}.textContent.includes("実行結果不明")`);
  console.log("Browser recovery: checkpoint, pending instruction, blocked hook and reopen");
  await checkStoppedRepairRecovery({evaluate, wait, field, restoredRun: expectedRun});
  await navigate("Page.navigate", {url});
  await wait("document.documentElement.dataset.liveReady === 'true' && !!window.harnessRuns");
  await click("tab-runs");
  await evaluate(`(() => {
    const original = window.fetch;
    window.restoreDryRunFetch = () => { window.fetch = original; };
    window.dryRunRequests = [];
    window.validationButton = document.querySelector("#dry-run-selected");
    window.fetch = (endpoint, options) => {
      if (endpoint === "workspace") return Promise.resolve(Response.json({units: ["例"], runs: [], parallel_limit: 5}));
      const request = endpoint === "control" && options?.body && JSON.parse(options.body);
      if (request?.action === "dry-run") {
        window.dryRunRequests.push(request);
        return new Promise(resolve => { window.finishDryRun = result => resolve(Response.json(result)); });
      }
      if (request?.action === "dry-run-cancel") {
        window.dryRunRequests.push(request);
        return Promise.resolve(Response.json({}));
      }
      return original(endpoint, options);
    };
    window.harnessRuns.refresh();
  })()`);
  await wait("Boolean(document.querySelector('#unit-options input:not(:disabled)'))");
  await evaluate("document.querySelector('#unit-options input:not(:disabled)').click()");
  const validationIdle = "validationButton.type === 'submit' && validationButton.textContent === '設定を検証' && !validationButton.disabled";
  const validationRunning = "validationButton.type === 'submit' && validationButton.textContent === '検証を中止'";
  await cdp("Page.bringToFront");
  await wait(validationIdle + " && validationButton.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})"
    + " && !validationButton.closest('[inert]')");
  await evaluate("validationButton.focus()");
  assert(await evaluate("document.activeElement === validationButton"), "the visible validation button must accept keyboard focus");
  await click("dry-run-selected");
  await wait(validationRunning + " && !validationButton.disabled && dryRunRequests.length === 1");
  assert(await evaluate("document.activeElement === validationButton"),
    "the validation button must keep keyboard focus when it becomes the cancel action");
  assert(await evaluate("validationButton === document.querySelector('#dry-run-selected')"), "the same button stays in place");
  assert(await evaluate("document.querySelector('#run-selected').disabled"), "migration must be disabled during validation");
  assert(await evaluate("document.querySelector('#dry-run-cancel') === null"));
  await click("dry-run-selected");
  await wait(validationRunning + " && validationButton.disabled && dryRunRequests.length === 2");
  assert(await evaluate("dryRunRequests[1].action === 'dry-run-cancel' && dryRunRequests[1].request_id === dryRunRequests[0].request_id"));
  await evaluate("finishDryRun({cancelled: true})");
  await wait(validationIdle);
  assert(await evaluate("document.querySelector('#workspace-run-feedback').textContent.includes('検証を中止しました')"));
  await click("dry-run-selected");
  await wait(validationRunning + " && dryRunRequests.length === 3");
  await evaluate("finishDryRun({configuration_ok: true, unit_id: '例', base_ref: 'HEAD', checks: []})");
  await wait(validationIdle);
  assert(await evaluate("document.querySelector('#workspace-run-feedback').textContent.includes('実移行は未実行')"));
  await evaluate("restoreDryRunFetch()");
  console.log("Browser validation: one button starts, cancels and resets; migration is not started");

  await click("tab-settings");
  await wait(`!${field("settings-context-roots")}.matches(":disabled")`);
  assert(await evaluate("!document.querySelector('#settings-unit-checker-command') && !!document.querySelector('#settings-checker-command')"));
  await evaluate('document.querySelector("#settings-project-details > summary").click()');
  for (const [id, title, options] of [
    ["settings-junie-log-language", "言語", ["日本語", "英語"]],
    ["settings-junie-log-tone", "口調", ["標準", "丁寧", "簡潔", "フレンドリー", "カスタム"]],
  ]) {
    assert.equal(await evaluate(field(id) + ".labels[0].textContent"), title);
    assert.deepEqual(await evaluate("Array.from(" + field(id) + ".options, option => option.text)"), options);
  }
  assert(await evaluate(field("settings-junie-log-custom") + ".hidden"));
  await change("settings-junie-log-tone", "custom", "input");
  assert(!(await evaluate(field("settings-junie-log-custom") + ".hidden")));
  const toneExample = field("settings-junie-log-example");
  const exampleAnimation = `getComputedStyle(${toneExample}.firstElementChild).animationName`;
  const exampleVisibility = `getComputedStyle(${toneExample}).visibility`;
  const motion = value => cdp("Emulation.setEmulatedMedia", {features: [{name: "prefers-reduced-motion", value}]});
  await motion("no-preference");
  assert.equal(await evaluate(exampleAnimation), "prompt-type");
  await wait(exampleVisibility + " === 'visible'");
  await motion("reduce");
  assert.equal(await evaluate(exampleAnimation), "none");
  assert.equal(await evaluate(exampleVisibility), "visible");
  await motion("no-preference");
  await change("settings-junie-log-instruction", "関西弁で短く", "input");
  await wait(exampleVisibility + " === 'hidden'");
  await wait(toml + ".value.includes('関西弁で短く')");
  await change("settings-junie-log-tone", "friendly", "input");
  assert(await evaluate(field("settings-junie-log-custom") + ".hidden"));
  const model = "settings-junie-model";
  const effort = "settings-junie-effort";
  const effortStatus = "settings-junie-effort-status";
  await wait("Array.from(document.getElementById('settings-junie-model').options).some(option => option.value === 'fixture-luna')");
  await change(model, "fixture-luna", "input");
  const choices = () => evaluate("Array.from(document.getElementById('settings-junie-effort').options).filter(option => !option.disabled).map(option => option.value)");
  assert.deepEqual(await choices(), ["", "low", "medium"]);
  await change(effort, "medium", "input");
  await change(model, "fixture-lite", "input");
  assert.deepEqual(await choices(), ["", "none"]);
  assert.equal(await value(effort), "medium");
  assert(await evaluate("document.getElementById('settings-junie-effort').selectedOptions[0].disabled"));
  assert(!(await evaluate(field(effortStatus) + ".hidden")));
  assert(await evaluate("document.getElementById('settings-junie-effort').closest('form').querySelector('button[type=submit]').disabled"));
  await change(effort, "none", "input");
  assert(await evaluate(field(effortStatus) + ".hidden"));
  await change(model, "fixture-unknown", "input");
  assert.deepEqual(await choices(), [""]);
  await change(model, "", "input");
  assert.equal(await value(effort), "");
  console.log("Browser settings: supported Junie efforts only; prior unsupported value is not silently changed");
  for (const width of [390, 768, 1280]) {
    await cdp("Emulation.setDeviceMetricsOverride", {width, height: 900, deviceScaleFactor: 1, mobile: false});
    const layout = await evaluate('JSON.stringify({viewport: innerWidth, width: document.documentElement.scrollWidth, tabs: document.querySelector(".workspace-tabs").getBoundingClientRect().right, overflow: [...document.querySelectorAll("body *")].filter(e => e.getClientRects().length && e.getBoundingClientRect().right > innerWidth + 1).slice(0, 16).map(e => [e.id || e.className, e.getBoundingClientRect().right])})');
    assert(await evaluate(`document.documentElement.lang === "ja" && document.documentElement.scrollWidth <= ${width}`), layout);
  }
  for (const id of ["settings-context-scope", "settings-context-exclude", "settings-context-recursive",
    "settings-context-max-files", "settings-context-max-total", "settings-context-max-file",
    "settings-context-preview-actions", "settings-input-warning", "settings-input-limit"]) {
    assert.equal(await evaluate(`${field(id)} === null`), true, `${id} should not be shown`);
  }
  await change("settings-context-roots", "default");
  await change(folders, "Shared\nExtra", "input");
  await wait(`${toml}.value.includes('"Extra"') && !${save}.disabled`);
  await change("settings-viewer-enabled", "false", "input");
  assert(await evaluate(`${field("settings-viewer-html")}.disabled`));
  await wait(`!${save}.disabled`);
  await evaluate(`${save}.click()`);
  await wait(`${field("workspace-config-feedback")}.textContent.includes("設定を保存しました")`);
  await click("workspace-config-reload");
  await wait(`!${field("workspace-config-reload")}.disabled`);
  assert.equal(await value(folders), "Shared\nExtra");
  assert.equal(await value("settings-context-roots"), "default");
  await change("settings-viewer-enabled", "", "input");
  assert.equal(await value("settings-viewer-html"), "viewer.html");
  assert.equal(await value("settings-anchor-reference"), "anchors.json");
  assert(!(await evaluate(`${field("settings-viewer-html")}.disabled`)));
  console.log("Browser settings: simplified controls, saved values and viewer path retention");
  await evaluate(`(() => {
    const original = window.fetch;
    window.fixturePermissionQueue = [
      {run_id: "different-run", unit_id: "Different unit", operation_id: "op-one", id: "one",
        title: "Junie の操作を確認", detail: "Read a file", options: [{id: "allow", label: "読む", kind: "allow_once"}]},
      {run_id: "third-run", unit_id: "Third unit", operation_id: "op-two", id: "two",
        title: "Junie の操作を確認", detail: "Other request", options: [{id: "allow", label: "読む", kind: "allow_once"}]},
    ];
    window.fetch = (endpoint, options) => {
      if (endpoint !== "junie-requests") return original(endpoint, options);
      if (options?.method === "POST") {
        window.fixturePermissionAnswer = JSON.parse(options.body);
        window.fixturePermissionQueue = window.fixturePermissionQueue.filter(row => row.id !== window.fixturePermissionAnswer.request_id);
        return Promise.resolve(Response.json({}, {status: 202}));
      }
      return Promise.resolve(Response.json(window.fixturePermissionQueue));
    };
    const input = ${field(folders)};
    input.focus();
    input.setSelectionRange(2, 4);
  })()`);
  const juniePopup = field("junie-confirmation");
  await wait(`${juniePopup}.open && ${field("junie-confirmation-run")}.textContent.includes("different-run")`);
  assert.equal(await evaluate(`${field("tab-settings")}.getAttribute("aria-selected")`), "true");
  await evaluate(`${juniePopup}.querySelector("[data-confirmation-later]").click()`);
  await wait(`${juniePopup}.open && ${field("junie-confirmation-run")}.textContent.includes("third-run")`);
  await evaluate(`${juniePopup}.querySelector("[data-confirmation-later]").click()`);
  await wait(`!${juniePopup}.open && document.activeElement === ${field(folders)}`);
  assert.deepEqual(await evaluate(`[${field(folders)}.selectionStart, ${field(folders)}.selectionEnd]`), [2, 4]);
  assert.equal(await value(folders), "Shared\nExtra");
  await evaluate('document.querySelector("[aria-controls=junie-confirmation]").click()');
  await evaluate(`${juniePopup}.querySelector(".junie-choices button").click()`);
  await evaluate(`${juniePopup}.querySelector(".junie-choices button").click()`);
  await wait(`window.fixturePermissionAnswer && !${juniePopup}.open`);
  assert.deepEqual(await evaluate("window.fixturePermissionAnswer"), {
    run_id: "different-run", operation_id: "op-one", request_id: "one", option_id: "allow",
  });
  console.log("Browser Junie checks: cross-Run queue, exact answer, active tab and editor selection retained");
  const rulePanel = field("workspace-rule-workspace");
  async function showRuleWorkspace() {
    assert(await evaluate(field("tab-settings") + ".nextElementSibling === " + field("tab-rule-workspace")));
    await click("tab-rule-workspace");
    await wait("!" + rulePanel + ".hidden");
  }
  const ruleViewport = "document.body.scrollHeight <= innerHeight + 1 && "
    + "document.documentElement.scrollWidth <= innerWidth && "
    + "document.querySelector('.rule-workspace').getBoundingClientRect().bottom <= innerHeight + 1";
  await cdp("Emulation.setDeviceMetricsOverride", {width: 1280, height: 650, deviceScaleFactor: 1, mobile: false});
  await showRuleWorkspace();
  assert(await evaluate(ruleViewport), "live rules fit the available viewport");
  const registered = field("rules-registered");
  const sourceValue = field("rules-sources");
  const markdown = field("rules-text");
  const draftPicker = field("rules-drafts");
  const baseRuleConfig = await fs.readFile(ruleConfigPath, "utf8");
  const fixtureProject = path.join(path.dirname(ruleConfigPath), "app");
  const ruleText = "# 先頭のルール\r\n<sample>字面のまま</sample>\r\n";
  await fs.writeFile(path.join(fixtureProject, "z-first.md"), ruleText);
  await fs.writeFile(path.join(fixtureProject, "a-second.md"), "# 二番目\n");
  const configureRules = (...names) => fs.writeFile(ruleConfigPath, baseRuleConfig +
    names.map(name => "\n[[project.migration_rules]]\npath = " + JSON.stringify(name) + "\n").join(""));
  const readRegistered = () => evaluate("fetch('rule-workspace?registered=1').then(r => r.json()).then(data => data.registered)");
  async function revisitRules() {
    await click("tab-settings");
    await showRuleWorkspace();
  }
  assert.equal(await readRegistered(), null);
  assert(await evaluate(registered + ".hidden"));

  try {
    await configureRules("z-first.md");
    await revisitRules();
    const name = field("rules-registered-name") + ".textContent";
    await wait("!" + registered + ".hidden && " + name + " === 'z-first.md'");
    assert.equal(await evaluate(field("rules-registered-text") + ".textContent"), ruleText);
    assert.equal(await evaluate(field("rules-registered-text") + ".querySelector('sample')"), null);
    assert.equal(await evaluate("fetch('rule-workspace').then(r => r.json()).then(data => data.drafts.length)"), 0);

    await configureRules("missing.md", "z-first.md", "a-second.md");
    await revisitRules();
    assert.equal((await readRegistered()).name, "z-first.md", "skip an unreadable entry without sorting readable ones");

    await configureRules("z-first.md", "a-second.md");
    await revisitRules();
    await click("rules-start");
    await wait(sourceValue + ".selectedOptions[0]?.textContent === 'z-first.md' && " +
      markdown + ".value.includes('字面のまま')");
    assert(await evaluate(registered + ".hidden"));
    await wait(draftPicker + ".value !== ''");
    const firstDraft = await value("rules-drafts");

    await checkRuleSourceEditor({evaluate, wait, field, change, click, value}, ruleConfigPath, firstDraft);
    await change("rules-model", "fixture-luna", "change");
    await wait(field("rules-model") + ".value === 'fixture-luna' && !" + field("rules-model") + ".disabled");
    await change("rules-effort", "medium", "change");
    await wait(field("rules-effort") + ".value === 'medium' && !" + field("rules-effort") + ".disabled");
    const savedRuleModel = await evaluate("fetch('rule-workspace?id=" + firstDraft + "').then(r => r.json()).then(d => [d.junie_model, d.junie_effort])");
    assert.deepEqual(savedRuleModel, ["fixture-luna", "medium"]);
    await click("rules-preview");
    await wait("!" + field("rules-review") + ".hidden");
    assert(await evaluate("!!document.querySelector('#rules-text-diff .rule-diff-line[data-kind=add]')"));
    assert(await evaluate("!!document.querySelector('#rules-text-diff .rule-diff-line[data-kind=remove]')"));
    await change("rules-text", "未保存の本文", "input");
    await revisitRules();
    assert.equal(await value("rules-text"), "未保存の本文");

    await change("rules-sources", "new");
    await wait(markdown + ".value === ''");
    await click("rules-start");
    await wait(sourceValue + ".selectedOptions[0]?.textContent === 'z-first.md' && " +
      draftPicker + ".value !== " + JSON.stringify(firstDraft));
    await change("rules-drafts", firstDraft);
    await wait(sourceValue + ".value === 'new'");
    await change("rules-drafts", "");
    await wait("!" + registered + ".hidden && " + field("rules-draft") + ".hidden");
  } finally {
    await fs.writeFile(ruleConfigPath, baseRuleConfig);
  }
  console.log("Browser RuleWorkspace: first configured Markdown previews without a draft, skips unavailable files, and preserves source and unsaved edits");
  await evaluate("(" + function () {
    const original = window.fetch;
    window.stopRuleChatFixture = () => { window.fetch = original; };
    const fixture = {id: "browser-rules-fixture", revision: 0, messages: [], unresolved: [], files: []};
    window.ruleChatActions = [];
    window.fetch = (url, options) => {
      if (!String(url).startsWith("rule-workspace")) return original(url, options);
      if (!options?.body) return Promise.resolve(Response.json(String(url).includes("id=") ?
        fixture : {drafts: [{id: fixture.id, files: 0}]}));
      const action = JSON.parse(options.body);
      window.ruleChatActions.push(action);
      fixture.revision++;
      if (action.action === "message") {
        fixture.messages.push({role: "operator", text: action.text});
        fixture.live = {status: "running", text: "参照資料を確認しています。", permission: {
          id: "browser-permission", detail: "サンプル資料を開く", options: [{id: "allow", label: "一度だけ許可"}],
        }};
      } else if (action.action === "answer") {
        fixture.live = {status: "running", text: "確認を続けています。"};
      } else if (action.action === "stop") {
        fixture.live = {status: "error", text: "Junieとの整理を中断しました。"};
      }
      return Promise.resolve(Response.json(fixture));
    };
  }.toString() + ")()");
  await click("rules-start");
  await wait("!" + field("rules-draft") + ".hidden");
  await change("rules-instruction", "このルールについて相談します。", "input");
  await click("rules-message");
  await wait("!" + field("rules-live-turn") + ".hidden && !" + field("rules-permission") + ".hidden");
  assert.equal(await evaluate(field("rules-live") + ".textContent"), "参照資料を確認しています。");
  await evaluate(field("rules-permission") + ".querySelector('button').click()");
  await wait(field("rules-permission") + ".hidden");
  await change("rules-instruction", "続けて相談する下書き", "input");
  assert(await evaluate(field("rules-message") + ".disabled && " + field("rules-optimize") + ".disabled"));
  await click("rules-stop");
  await wait(field("rules-live-turn") + ".hidden && " + field("rules-stop") + ".hidden");
  assert.equal(await value("rules-instruction"), "続けて相談する下書き");
  assert.equal(await evaluate(field("rules-status") + ".textContent"), "Junieとの整理を中断しました。");
  assert.deepEqual(await evaluate("window.ruleChatActions.slice(1).map(({action, request, option}) => [action, request || '', option || ''])"),
    [["message", "", ""], ["answer", "browser-permission", "allow"], ["stop", "", ""]]);
  await evaluate("stopRuleChatFixture()");
  console.log("Browser RuleWorkspace live actions: streaming text, permission, cancellation, and unsent draft remain distinct");
  await click("tab-editor");
  const sourceEditor = "window.monaco?.editor.getEditors().find(e => document.getElementById('editor-code').contains(e.getDomNode()))";
  await wait(sourceEditor + "?.getModel()?.getLanguageId() === 'csharp'");
  const savedCode = await evaluate(sourceEditor + ".getValue()");
  await evaluate(sourceEditor + ".setValue('class Draft {\\n int n = ;\\n}')");
  await wait("document.getElementById('editor-diagnostic-list').textContent.includes('CS1525')", 250);
  assert.deepEqual(await evaluate("monaco.editor.getModelMarkers({owner: 'syntax'}).map(m => [m.code,m.startLineNumber,m.startColumn])"),
    [["CS1525", 2, 10]]);
  await evaluate("document.querySelector('#editor-diagnostics summary').click()");
  await wait("!!document.querySelector('#editor-code .squiggly-error') && document.querySelector('#editor-diagnostic-list button').offsetWidth > 0");
  await evaluate("document.querySelector('#editor-diagnostic-list button').click()");
  assert.deepEqual(await evaluate(sourceEditor + ".getPosition()"), {lineNumber: 2, column: 10});
  assert.equal(await evaluate("fetch('editor').then(r => r.json()).then(d => d.content)"), savedCode);
  await evaluate(sourceEditor + ".setValue(" + JSON.stringify(savedCode) + ")");
  await wait("document.getElementById('editor-diagnostic-status').textContent.includes('指摘なし')");
  assert.equal(await evaluate("monaco.editor.getModelMarkers({owner:'syntax'}).length"), 0);
  console.log("Browser editor: unsaved C# syntax, real compiler marker and click location, saved text unchanged");
  await checkEditorSemantics({evaluate, wait, sourceEditor, config: ruleConfigPath, run: expectedRun});
  await checkAdoptedEditorSave({evaluate, wait, click, sourceEditor, run: expectedRun});
  const runModel = "document.querySelector('#editor-prompt-form [data-run-model]')";
  const runEffort = "document.querySelector('#editor-prompt-form [data-run-effort]')";
  await wait(runModel + " && !" + runModel + ".disabled");
  await evaluate(runModel + ".value = 'fixture-luna'; " + runModel + ".dispatchEvent(new Event('change', {bubbles:true}))");
  await wait(runModel + ".value === 'fixture-luna' && !" + runModel + ".disabled");
  await evaluate(runEffort + ".value = 'medium'; " + runEffort + ".dispatchEvent(new Event('change', {bubbles:true}))");
  await wait(runEffort + ".value === 'medium' && !" + runEffort + ".disabled");
  const runSelection = await evaluate("fetch('run-model').then(r => r.json()).then(d => [d.model, d.effort])");
  assert.deepEqual(runSelection, ["fixture-luna", "medium"]);
  await evaluate("(" + function () {
    const fetchOriginal = window.fetch.bind(window);
    const control = window.resumeModelFixture = {attempts: [], saved: false};
    window.fetch = async (resource, options) => {
      const body = options?.body && JSON.parse(options.body);
      if (resource === "editor" && body?.kind === "model") {
        control.pending = true;
        const send = await new Promise(resolve => { control.release = resolve; });
        control.pending = false;
        if (!send) return new Response("fixture model save failed", {status: 409});
        const reply = await fetchOriginal(resource, options);
        control.saved = reply.ok;
        return reply;
      }
      if (resource === "control" && body?.action === "resume") {
        const response = await fetchOriginal(resource, options);
        control.attempts.push({saved: control.saved, status: response.status, revision: body.model_revision});
        return response;
      }
      return fetchOriginal(resource, options);
    };
  }.toString() + ")()");
  assert.equal(await evaluate("fetch('fixture-resumable', {method: 'POST'}).then(r => r.status)"), 200);
  await click("tab-progress");
  await wait(field("toggle") + "?.matches(':enabled')");
  const failedChange = runModel + ".value = 'fixture-lite'; " + runModel + ".dispatchEvent(new Event('change', {bubbles:true}))";
  await evaluate(failedChange);
  await wait("resumeModelFixture.pending");
  await click("toggle");
  await evaluate("resumeModelFixture.release(false)");
  await wait(field("workspace-progress-feedback") + ".textContent.includes('モデル設定を保存できなかった')");
  assert.deepEqual(await evaluate("resumeModelFixture.attempts"), []);
  assert.equal(await evaluate("fetch('run-model').then(r => r.json()).then(d => d.model)"), "fixture-luna");
  await evaluate(failedChange);
  await wait("resumeModelFixture.pending");
  await click("toggle");
  await click("toggle");
  assert.deepEqual(await evaluate("resumeModelFixture.attempts"), []);
  await evaluate("resumeModelFixture.release(true)");
  await wait("resumeModelFixture.attempts.length === 1");
  const [attempt] = await evaluate("resumeModelFixture.attempts");
  assert.deepEqual({saved: attempt.saved, status: attempt.status}, {saved: true, status: 202});
  assert.match(attempt.revision, /^[0-9a-f]{24}$/);
  assert.deepEqual(await evaluate("fetch('run-model').then(r => r.json()).then(d => [d.model, d.revision])"),
    ["fixture-lite", attempt.revision]);
  console.log("Browser Run model: a failed save blocks resume; a delayed valid save is used by one accepted resume");
  const liveTabs = await evaluate("Array.from(document.querySelectorAll('.workspace-tabs [role=tab]:not([hidden])'), b => b.textContent)");
  const liveSteps = await evaluate("Array.from(document.querySelectorAll('.step-title'), h => h.textContent)");
  assert.equal(liveTabs.length, 6); assert.equal(liveSteps.length, 7);
  const demoUrl = pathToFileURL(path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "progress-demo.html"));
  await navigate("Page.navigate", {url: demoUrl.href});
  const ready = 'document.documentElement.dataset.liveReady === "true" && document.querySelector(".run-detail").textContent.includes("run-2026-11-14-001")';
  await wait(ready);
  const nested = "/php-to-csharp-migration-harness/";
  const files = path.dirname(fileURLToPath(demoUrl));
  const network = [];
  await cdp("Network.enable");
  const onRequest = event => {
    const m = JSON.parse(event.data);
    if (m.method === "Network.requestWillBeSent") network.push(m.params.request);
  };
  socket.addEventListener("message", onRequest);
  staticServer = createServer(async (req, res) => {
    const asset = new URL(req.url, "http://localhost").pathname.slice(nested.length);
    if (!req.url.startsWith(nested) || req.method !== "GET" ||
        !(asset === "progress-demo.html" || /^src\/migration_harness\/web\/[\w.\/-]+$/.test(asset)) ||
        asset.split("/").includes("..")) {
      res.writeHead(404).end(); return;
    }
    try {
      const bytes = await fs.readFile(path.join(files, asset));
      res.writeHead(200, {"Content-Type": asset.endsWith(".html") ? "text/html; charset=utf-8" :
        asset.endsWith(".css") ? "text/css" : "application/javascript"}).end(bytes);
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(resolve => staticServer.listen(0, "127.0.0.1", resolve));
  const staticOrigin = "http://127.0.0.1:" + staticServer.address().port;
  await navigate("Page.navigate", {url: staticOrigin + nested + "progress-demo.html"});
  await wait(ready);
  assert.deepEqual(await evaluate("Array.from(document.querySelectorAll('.workspace-tabs [role=tab]:not([hidden])'), b => b.textContent)"), liveTabs);
  assert.deepEqual(await evaluate("Array.from(document.querySelectorAll('.step-title'), h => h.textContent)"), liveSteps);
  await click("stop");
  await wait(field("state-label") + ".textContent === '中断'");
  await wait("!" + field("recovery-summary") + ".hidden");
  await click("open-migration-ledger");
  await wait(field("migration-ledger-dialog") + ".open && !" + field("migration-ledger-compare") + ".hidden");
  await wait("document.querySelector('#unresolved-junie-memos li')?.textContent");
  const demoMemo = await evaluate("document.querySelector('#unresolved-junie-memos li').textContent");
  await evaluate(field("migration-ledger-dialog") + ".close()");
  await click("tab-editor");
  await wait("!" + field("workspace-editor") + ".hidden");
  async function captureChat(name) {
    const directory = process.env.HARNESS_CHAT_SCREENSHOTS;
    if (!directory) return;
    await fs.mkdir(directory, {recursive: true});
    await evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))");
    await evaluate("Promise.all(document.getAnimations().filter(a => Number.isFinite(a.effect?.getComputedTiming().endTime))"
      + ".map(a => a.finished.catch(() => {}))).then(() => true)");
    const {data} = await cdp("Page.captureScreenshot", {format: "png"});
    await fs.writeFile(path.join(directory, name + ".png"), Buffer.from(data, "base64"));
  }
  async function sendChat(input, send, history, text, checkIME = false) {
    await change(input, text, "input");
    await wait(field(send) + ".matches(':enabled')");
    const enter = options => evaluate(field(input) + ".dispatchEvent(new KeyboardEvent('keydown', " +
      JSON.stringify({key: "Enter", bubbles: true, cancelable: true, ...options}) + "))");
    if (checkIME) {
      assert(await enter({}), "plain Enter remains a newline");
      await evaluate(field(input) + ".dispatchEvent(new CompositionEvent('compositionstart'))");
      assert(await enter({ctrlKey: true, isComposing: true}), "IME Enter must not submit");
      assert(await enter({ctrlKey: true}), "unflagged Enter during composition must not submit");
      assert.equal(await value(input), text);
      assert(!(await evaluate(field(history) + ".textContent")).includes(text));
      await evaluate(field(input) + ".dispatchEvent(new CompositionEvent('compositionend'))");
    }
    assert.equal(await enter({ctrlKey: true}), false, "Ctrl+Enter submits the composer");
    await wait(field(history) + ".textContent.includes(" + JSON.stringify(text) + ")");
  }
  const editorSample = "注文履歴の日付表記を元の画面と揃えてください。\n表示順は変更しないでください。";
  await sendChat("editor-prompt", "editor-send", "editor-prompts", editorSample, true);
  const editorBubble = "document.querySelector('#editor-prompts .junie-chat-bubble[data-side=outgoing]')";
  assert.equal(await evaluate(editorBubble + ".querySelector('p').textContent"), editorSample);
  await wait("!!document.querySelector('#editor-prompts .junie-chat-bubble[data-side=incoming]')");
  const editorStyles = await evaluate(editorBubble + ".className");
  assert.equal(await evaluate(editorBubble + ".querySelector('button')"), null, "queued instructions have no edit button");
  assert.equal(await evaluate("getComputedStyle(" + editorBubble + ".querySelector('.junie-chat-sender')).position"), "absolute");
  if (process.env.HARNESS_CHAT_SCREENSHOTS) {
    await cdp("Emulation.setDeviceMetricsOverride", {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false});
    await wait(editorBubble + ".offsetWidth > 0");
    await evaluate(editorBubble + ".closest('.junie-chat-feed').scrollTop = 0");
  }
  await captureChat("editor-light-1280");
  await cdp("Emulation.setDeviceMetricsOverride", {width: 390, height: 690, deviceScaleFactor: 1, mobile: false});
  await click("theme-toggle");
  await wait("document.documentElement.dataset.theme === 'dark' && !document.documentElement.dataset.wipe");
  await evaluate(field("editor-chat-feed") + ".closest('.junie-chat').scrollIntoView({block: 'end'})");
  assert(await evaluate("document.documentElement.scrollWidth <= innerWidth && " +
    field("editor-send") + ".getBoundingClientRect().right <= innerWidth && " +
    field("editor-send") + ".getBoundingClientRect().bottom <= innerHeight"));
  await wait(editorBubble + ".offsetWidth > 0");
  await captureChat("editor-dark-390");
  await click("theme-toggle");
  await wait("document.documentElement.dataset.theme === 'light' && !document.documentElement.dataset.wipe");
  await cdp("Emulation.setDeviceMetricsOverride", {width: 1280, height: 650, deviceScaleFactor: 1, mobile: false});
  await click("tab-runs");
  await wait("!" + field("workspace-runs") + ".hidden");
  await click("tab-tokens");
  await wait("!" + field("workspace-tokens") + ".hidden");
  await wait(field("token-input") + ".textContent === '18,420'");
  const demoInputs = await evaluate("Array.from(document.querySelectorAll('#token-input-breakdown span'), n => n.textContent)");
  assert.deepEqual({demoMemo, demoInputs}, {
    demoMemo: "1:1:1–2：日付の表示形式を原文と照合する。",
    demoInputs: ["通常入力 14,740", "キャッシュ読み込み 3,200", "キャッシュ作成 480"],
  });
  await click("tab-progress");
  const demoDialog = field("demo-notice-dialog");
  await wait(`!!${field("demo-notice")}?.offsetWidth`);
  await click("demo-notice");
  assert.equal(await evaluate(`${demoDialog}.open && ${field("demo-notice-answer")}.hidden`), true);
  await click("demo-notice-open");
  assert.match(await evaluate(`${field("demo-notice-state")}.textContent`), /再通知を停止.*未回答/);
  assert.equal(await evaluate(`${field("demo-notice-answer")}.hidden`), false);
  await click("demo-notice-answer");
  assert.match(await evaluate(`${field("demo-notice-state")}.textContent`), /回答を記録/);
  console.log("Browser demo: opening suppresses reminders without answering the confirmation");
  await click("tab-settings");
  await wait(field("settings-junie-log-tone") + ".matches(':disabled')");
  await click("tab-runs");
  const activeAction = "document.querySelector('tr [data-action=cancel-queued], tr [data-action=stop]')";
  while (await evaluate(activeAction + " !== null")) {
    const row = await evaluate(activeAction + ".closest('tr').dataset.runId");
    await evaluate(activeAction + ".click()");
    await wait("!Array.from(document.querySelectorAll('tr [data-action=cancel-queued],tr [data-action=stop]')).some(b => b.closest('tr').dataset.runId === " + JSON.stringify(row) + ")");
  }
  await click("tab-settings");
  await wait(field("settings-junie-log-tone") + ".matches(':enabled')");
  await evaluate('document.querySelector("#settings-project-details").open = true');
  await change("settings-junie-log-tone", "custom", "input");
  await wait(`${toneExample}.offsetWidth > 0 && ${exampleVisibility} === 'visible'`);
  assert.equal(await evaluate(exampleAnimation), "prompt-type");

  await click("tab-progress");
  const logButton = "document.querySelector('.junie-log-trigger')";
  await wait(logButton + ".matches(':enabled')");
  await evaluate(logButton + ".click()", true);
  const log = "junieLog.windows.values().next().value?.popup.document";
  const logText = selector => log + "?.querySelector(" + JSON.stringify(selector) + ")?.textContent";
  const logStatus = logText("[data-log-status]");
  const rawLog = `Array.from(${log}.querySelectorAll("[data-log-entries] li"), row => [
    row.querySelector("h2").textContent, row.querySelector("p").textContent, row.querySelector("time").dateTime,
  ])`;
  await wait(logText("[data-log-entries] li p"));
  assert.equal(await evaluate(log + ".documentElement.lang"), "ja");
  assert.equal(await evaluate(logStatus), "表示中のログ：1 件。");
  const originalEntries = await evaluate(rawLog);
  assert.deepEqual(originalEntries, [["入力を確認", "移行元のPHPと仮C#を確認しました。", "2026-11-14T01:42:03.000Z"]]);

  await click("tab-settings");
  await change("settings-junie-log-language", "en", "input");
  await change("settings-junie-log-tone", "polite", "input");
  await wait(save + ".matches(':enabled')");
  await evaluate(save + ".click()");
  await wait(`${field("workspace-config-feedback")}.textContent.includes("設定を保存しました")`);
  const englishPoliteLog = logStatus + " === 'Showing 1 entry.'";
  await wait(englishPoliteLog);
  assert.equal(await evaluate(log + ".documentElement.lang"), "en");
  assert.equal(await evaluate(logText("#junie-log-title")), "Junie detailed log");
  assert.match(await evaluate(logText("[data-log-description]")), /new progress updates and reports/);
  assert.deepEqual(await evaluate(rawLog), originalEntries);
  for (const [tone, expected] of [["concise", "1 entry"], ["friendly", "You’ve got 1 entry so far."]]) {
    await change("settings-junie-log-tone", tone, "input");
    await wait(save + ".matches(':enabled')");
    await evaluate(save + ".click()");
    await wait(logStatus + " === " + JSON.stringify(expected));
    assert.deepEqual(await evaluate(rawLog), originalEntries);
  }
  await evaluate(log + ".defaultView.close()");
  await click("tab-progress");
  await evaluate(logButton + ".click()", true);
  await wait(logStatus + " === 'You’ve got 1 entry so far.'");
  assert.deepEqual(await evaluate(rawLog), originalEntries);
  await evaluate(log + ".defaultView.close()");
  console.log("Browser demo: saved language and tone change log framing without changing original entries");

  await showRuleWorkspace();
  await wait("!" + field("rules-draft") + ".hidden && " + field("rules-name") + ".value === 'orders.md'");
  assert.equal(await evaluate(field("rules-import-label") + ".hidden"), true);
  await change("rules-model", "gpt", "change");
  await wait(field("rules-model") + ".value === 'gpt' && !" + field("rules-model") + ".disabled");
  await change("rules-effort", "medium", "change");
  await wait(field("rules-effort") + ".value === 'medium' && !" + field("rules-effort") + ".disabled");
  const instructionExample = field("rules-instruction-example");
  assert.equal(await evaluate("getComputedStyle(" + instructionExample + ").visibility"), "visible");
  await cdp("Emulation.setEmulatedMedia", {features: [{name: "prefers-reduced-motion", value: "no-preference"}]});
  assert.equal(await evaluate("getComputedStyle(" + instructionExample + ".firstElementChild).animationName"), "prompt-type");
  await cdp("Emulation.setEmulatedMedia", {features: [{name: "prefers-reduced-motion", value: "reduce"}]});
  assert.equal(await evaluate("getComputedStyle(" + instructionExample + ".firstElementChild).animationName"), "none");
  const ruleInstruction = field("rules-instruction");
  await evaluate(ruleInstruction + ".value = '確認テスト'");
  await wait("getComputedStyle(" + instructionExample + ").visibility === 'hidden'");
  const rulesSample = "確定済みの注文は取消不可ですか？\n決済前の例外を確認してください。";
  await sendChat("rules-instruction", "rules-message", "rules-history", rulesSample, true);
  await wait("!" + field("rules-proposal") + ".hidden");
  const rulesBubble = "document.querySelector('#rules-history .junie-chat-bubble[data-side=outgoing]')";
  assert.equal(await evaluate(rulesBubble + ".className"), editorStyles, "both panels use the same visual components");
  assert.equal(await evaluate(rulesBubble + ".querySelector('p').textContent"), rulesSample);
  await wait("!!document.querySelector('#rules-history .junie-chat-bubble[data-side=incoming]')");
  if (process.env.HARNESS_CHAT_SCREENSHOTS) {
    await cdp("Emulation.setDeviceMetricsOverride", {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false});
    await wait(rulesBubble + ".offsetWidth > 0");
    await evaluate(rulesBubble + ".closest('.junie-chat-feed').scrollTop = 0");
  }
  await captureChat("consultation-light-1280");
  await cdp("Emulation.setDeviceMetricsOverride", {width: 390, height: 690, deviceScaleFactor: 1, mobile: false});
  await click("theme-toggle");
  await wait("document.documentElement.dataset.theme === 'dark' && !document.documentElement.dataset.wipe");
  await evaluate(field("rules-chat-feed") + ".closest('.junie-chat').scrollIntoView({block: 'end'})");
  assert(await evaluate("document.documentElement.scrollWidth <= innerWidth && " +
    field("rules-message") + ".getBoundingClientRect().right <= innerWidth && " +
    field("rules-message") + ".getBoundingClientRect().bottom <= innerHeight"));
  await captureChat("consultation-dark-390");
  await click("theme-toggle");
  await wait("document.documentElement.dataset.theme === 'light' && !document.documentElement.dataset.wipe");
  await cdp("Emulation.setDeviceMetricsOverride", {width: 1280, height: 650, deviceScaleFactor: 1, mobile: false});

  const ruleFeed = field("rules-chat-feed");
  for (let turn = 0; turn < 3; turn++) {
    await sendChat("rules-instruction", "rules-message", "rules-history", "適用条件を確認 " + turn + "\n" + "照合してください。\n".repeat(12) + "以上。");
  }
  await evaluate(ruleFeed + ".scrollTop = 0");
  await wait("!" + field("rules-chat-latest") + ".hidden");
  await sendChat("rules-instruction", "rules-message", "rules-history", "古い履歴を読みながら新しい返信を確認。");
  assert.equal(await evaluate(ruleFeed + ".scrollTop"), 0, "new replies must not move someone reading older messages");
  await click("rules-chat-latest");
  assert(await evaluate(ruleFeed + ".scrollHeight - " + ruleFeed + ".clientHeight - " + ruleFeed + ".scrollTop <= 1"));
  assert.equal(await evaluate(field("rules-chat-latest") + ".hidden"), true);
  await click("rules-optimize");
  await wait("!" + field("rules-proposal") + ".hidden");
  console.log("Browser Junie chats: same components, IME-safe shortcut and manually followed history");
  await click("rules-preview");
  await wait("!" + field("rules-review") + ".hidden");
  assert.equal(await evaluate("document.querySelector('#rules-confirm')"), null);
  assert.equal(await evaluate("document.querySelector('#rules-toml-diff')"), null);
  for (const [width, height] of [[1280, 650], [390, 650], [1280, 440]]) {
    await cdp("Emulation.setDeviceMetricsOverride", {width, height, deviceScaleFactor: 1, mobile: false});
    assert(await evaluate(ruleViewport), "rules demo fits " + width + "×" + height);
    const scrollable = width <= 950 || height <= 500
      ? "document.querySelector('.rule-workspace')"
      : "document.querySelector('.rule-workspace-columns > section')";
    assert(await evaluate(scrollable + ".scrollHeight > " + scrollable + ".clientHeight"),
      "long rule controls can scroll at " + width + "×" + height);
  }
  console.log("Browser rules: settings tab adjacency and bounded desktop/compact layouts");
  const reviewLayout = [
    "(() => {",
    "const dialog = document.getElementById('review-dialog');",
    "if (!dialog.open) dialog.showModal();",
    "const panel = dialog.querySelector('.checker-review-scroll');",
    "const input = dialog.querySelector('#checker-instruction');",
    "const action = dialog.querySelector('#review-fix');",
    "const heading = dialog.querySelector('#review-title');",
    "const top = element => element.getBoundingClientRect().top;",
    "panel.scrollTop = 0;",
    "const inputBefore = top(input), headingBefore = top(heading);",
    "panel.scrollTop = panel.scrollHeight;",
    "const result = panel.scrollTop > 0 && top(heading) < headingBefore && Math.abs(top(input) - inputBefore) < 1",
    "  && top(input) >= 0 && action.getBoundingClientRect().bottom <= innerHeight + 1;",
    "dialog.close();",
    "return result;",
    "})()",
  ].join("\n");
  await cdp("Emulation.setEmulatedMedia", {features: [{name: "prefers-reduced-motion", value: "reduce"}]});
  for (const [width, height] of [[760, 520], [390, 480]]) {
    await cdp("Emulation.setDeviceMetricsOverride", {width, height, deviceScaleFactor: 1, mobile: false});
    await evaluate("document.getElementById('checker-review-output').textContent = 'fixture output\\n'.repeat(40)");
    assert(await evaluate(reviewLayout), "checker instructions stay visible while findings scroll at " + width + "×" + height);
  }
  console.log("Browser review: findings scroll while instructions and actions stay visible");

  const selectedDemo = "document.querySelector('.run-detail').textContent";
  const runAction = (id, action) => "document.querySelector(" +
    JSON.stringify('#run-rows tr[data-run-id="' + id + '"] [data-action="' + action + '"]') + ")";
  async function openDemoRun(id) {
    await click("tab-runs");
    const button = runAction(id, "open-runprogress");
    await wait(button + "?.matches(':enabled')");
    await evaluate(button + ".click()");
    await wait(selectedDemo + ".includes(" + JSON.stringify(id) + ")");
  }
  async function removeDemoRun(id) {
    await click("tab-runs");
    const button = runAction(id, "hide-run");
    await wait(button + "?.matches(':enabled')");
    await evaluate(button + ".click()");
    await wait("!" + button + " && !" + selectedDemo + ".includes(" + JSON.stringify(id) + ")");
  }

  await click("tab-runs");
  const waitingRun = "run-2026-11-14-001", otherRun = "demo-customer";
  const waitingResume = runAction(waitingRun, "resume-run");
  const otherProgress = runAction(otherRun, "open-runprogress");
  await wait(waitingResume + "?.matches(':enabled') && " + otherProgress + "?.matches(':enabled')");
  await evaluate("(" + function (run) {
    const state = window.listModelWait = {started: 0};
    const previous = window.harnessRunModelReady;
    const transport = window.harnessDemoTransport;
    const nativeFetch = transport.fetch;
    window.harnessRunModelReady = id => id === run ? new Promise(resolve => { state.release = resolve; }) : previous(id);
    transport.fetch = (path, request) => {
      if (path === "control" && request?.body && JSON.parse(request.body).action === "resume-run") state.started++;
      return nativeFetch(path, request);
    };
    state.restore = () => { window.harnessRunModelReady = previous; transport.fetch = nativeFetch; };
  }.toString() + ")(" + JSON.stringify(waitingRun) + ")");
  await evaluate(waitingResume + ".click()");
  await wait("!!listModelWait.release");
  assert(await evaluate(waitingResume + ".disabled"), "waiting resume locks its own row");
  assert(await evaluate(otherProgress + ".matches(':enabled')"), "waiting resume must not lock another Run's row");
  await evaluate(otherProgress + ".click()");
  await wait(selectedDemo + ".includes(" + JSON.stringify(otherRun) + ")");
  await evaluate("listModelWait.release(true)");
  await click("tab-runs");
  await wait(waitingResume + "?.matches(':enabled')");
  assert.equal(await evaluate("listModelWait.started"), 0, "switching via the actual run list cancels the waiting resume");
  await evaluate("listModelWait.restore()");
  console.log("Browser run list: another Run can be selected while a model save is pending");

  const designRun = "run-2026-11-14-001";
  await click("tab-runs");
  await wait(runAction(designRun, "resume-stage") + "?.matches(':enabled')");
  const placements = [];
  const actionMetrics = id => evaluate("(" + function (run) {
    const row = document.querySelector('tr[data-run-id="' + run + '"]');
    return [...(row?.querySelectorAll(".run-actions button") ?? [])].map(button => ({
      label: button.textContent, width: button.getBoundingClientRect().width,
      fits: button.scrollWidth <= button.clientWidth + 1,
    }));
  }.toString() + ")(" + JSON.stringify(id) + ")");
  function verifyWidths(buttons, width, expected) {
    assert(buttons.length > 1 && buttons.every(button => button.fits && button.width > 0), "labels fit at " + width);
    const fixed = expected ?? buttons[0].width;
    assert(buttons.every(button => Math.abs(button.width - fixed) <= 1), "action widths at " + width + ": " + JSON.stringify(buttons));
    return fixed;
  }
  async function viewport(width, height) {
    await cdp("Emulation.setDeviceMetricsOverride", {width, height, deviceScaleFactor: 1, mobile: false});
    await wait("innerWidth === " + width);
    await evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
  }
  for (const [width, height] of [[320, 540], [360, 540], [580, 620], [780, 640], [1280, 650]]) {
    await viewport(width, height);
    const layout = await evaluate("(" + function (run) {
      const row = document.querySelector('tr[data-run-id="' + run + '"]');
      const actions = row.querySelector(".run-actions");
      const navigation = actions.querySelector(".run-view-actions");
      const execution = actions.querySelector(".run-execution-actions");
      const remove = actions.querySelector(":scope > .run-remove-action");
      if (!navigation || !execution || !remove) return null;
      const names = group => [...group.querySelectorAll("button")].map(button => button.dataset.action);
      const buttons = [...execution.querySelectorAll("button")];
      const rowBox = actions.getBoundingClientRect();
      const viewport = row.closest(".runs-table-wrap").getBoundingClientRect();
      const within = [...actions.querySelectorAll("button")].every(button => {
        const rect = button.getBoundingClientRect();
        return rect.left >= rowBox.left - 1 && rect.right <= rowBox.right + 1;
      });
      const top = element => Math.round(element.getBoundingClientRect().top);
      const middle = element => {
        const bounds = element.getBoundingClientRect();
        return bounds.top + bounds.height / 2;
      };
      const navButtons = [...navigation.querySelectorAll("button")];
      return {
        navigation: names(navigation), execution: names(execution), remove: remove.dataset.action,
        allFit: within && actions.scrollWidth <= actions.clientWidth + 1 && rowBox.right <= viewport.right + 1,
        primary: buttons[0].classList.contains("primary"),
        executeTogether: Math.abs(top(buttons[0]) - top(buttons[1])) <= 1,
        navigationTogether: Math.abs(top(navButtons[0]) - top(navButtons[1])) <= 1,
        viewAndExecuteTogether: Math.abs(middle(navigation) - middle(execution)) <= 1,
        gridAreas: getComputedStyle(actions).gridTemplateAreas,
      };
    }.toString() + ")(" + JSON.stringify(designRun) + ")");
    const buttons = (await Promise.all([designRun, "demo-search", "demo-partners"].map(actionMetrics))).flat();
    for (const label of ["進捗", "進捗で確認", "エディタ", "再開", "再検証", "再開方法…", "削除"]) {
      assert(buttons.some(button => button.label === label), "fixture includes " + label);
    }
    placements.push({width, layout, buttonWidth: verifyWidths(buttons, width)});

  }
  await evaluate("(" + function () {
    const transport = window.harnessDemoTransport;
    const previous = transport.fetch;
    const requests = window.resumeDesignRequests = [];
    transport.fetch = async (resource, options) => {
      const url = new URL(resource, location.href);
      const data = options?.body && JSON.parse(options.body);
      if (url.pathname.endsWith("/resume-plan")) {
        const entry = {kind: "plan", data: Object.fromEntries(url.searchParams)};
        requests.push(entry);
        try {
          const response = await previous(resource, options);
          entry.status = response.status;
          try {
            if (response.ok) {
              entry.response = await response.clone().json();
              entry.data.version = entry.response.version;
            } else entry.error = await response.clone().text();
          } catch (error) { entry.error = error.message; }
          return response;
        } catch (error) {
          entry.error = error.message;
          throw error;
        }
      }
      const response = await previous(resource, options);
      if (resource === "control" && data?.action === "resume-run") requests.push({kind: "start", data});
      return response;
    };
    window.restoreResumeDesignTransport = () => { transport.fetch = previous; };
  }.toString() + ")()");
  const dialog = field("resume-stage-dialog"), startButton = field("resume-stage-start");
  const demoRun = run => "harnessDemoTransport.fetch('workspace').then(r => r.json()).then(d => d.runs.find(x => x.run_id === "
    + JSON.stringify(run) + "))";
  const demoRunStatus = run => demoRun(run) + ".then(run => run?.status)";
  async function waitForConfirmedPlan(run, stage) {
    try {
      await wait(dialog + ".open && " + startButton + "?.matches(':enabled')");
    } catch (error) {
      const state = await evaluate("(" + function (run, stage) {
        const field = id => document.getElementById(id);
        const plans = window.resumeDesignRequests.filter(entry =>
          entry.kind === "plan" && entry.data.run_id === run && entry.data.start_stage === stage);
        return {open: field("resume-stage-dialog").open, stage: field("resume-stage").value,
          startDisabled: field("resume-stage-start").disabled, previewDisabled: field("resume-stage-preview").disabled,
          summary: field("resume-stage-summary").textContent, plans};
      }.toString() + ")(" + JSON.stringify(run) + "," + JSON.stringify(stage) + ")");
      throw new Error(`Resume plan was not accepted: ${JSON.stringify({run, stage, fixture: await evaluate(demoRun(run)), ...state})}`,
        {cause: error});
    }
  }
  async function choosePlan(run, stage) {
    const fixture = await evaluate(demoRun(run));
    assert(fixture?.ready && fixture.worktree_path, "resume fixture not ready: " + JSON.stringify(fixture));
    await wait(runAction(run, "resume-stage") + "?.matches(':enabled')");
    await evaluate(runAction(run, "resume-stage") + ".click()");
    await wait(dialog + ".open");
    if (stage !== "migration") await change("resume-stage", stage);
    assert(await evaluate(startButton + ".disabled"), "start requires a confirmed plan");
    await click("resume-stage-preview");
    await waitForConfirmedPlan(run, stage);
  }
  async function stopDemoStage(run) {
    const body = JSON.stringify({action: "stop", run_id: run});
    assert.equal(await evaluate("harnessDemoTransport.fetch('control', {method:'POST', body:" + JSON.stringify(body)
      + "}).then(r => r.ok)"), true);
    await wait("(" + demoRunStatus(run) + ").then(status => status === 'interrupted')");
  }
  const stoppedRange = await evaluate("harnessDemoTransport.fetch('workspace').then(r => r.json()).then(d => {"
    + "const run = d.runs.find(x => x.run_id === " + JSON.stringify(designRun) + ");"
    + "return (run.completed_capsules + 1) + '〜' + run.context_capsules;})");
  await viewport(360, 440);
  await choosePlan(designRun, "migration");
  assert.deepEqual(await evaluate("Array.from(" + field("resume-stage") + ".options, option => [option.value,option.textContent])"),
    [["migration", "移行から"], ["build-through", "ビルドから"], ["checker", "チェッカーから"]]);
  assert.equal(await evaluate(dialog + ".querySelectorAll('input[type=number], #resume-rewind, #resume-through, #resume-stage-hint').length"), 0);
  assert(!await evaluate(dialog + ".textContent.includes('保存済みの続きから')"), "static migration hint is not displayed");
  await wait("!" + dialog + ".getAnimations().some(a => a.playState === 'running')");
  assert(await evaluate("(" + function () {
    const modal = document.getElementById("resume-stage-dialog");
    const bounds = modal.getBoundingClientRect();
    return modal.scrollWidth <= modal.clientWidth + 1 && bounds.left >= 0 && bounds.right <= innerWidth &&
      [...modal.querySelectorAll("button")].every(button => {
        const rect = button.getBoundingClientRect();
        return rect.left >= bounds.left && rect.right <= bounds.right;
      });
  }.toString() + ")()"), "resume dialog has no horizontal clipping at 360 px");
  assert((await evaluate(field("resume-stage-summary") + ".textContent")).includes(stoppedRange));
  await click("resume-stage-start");
  await wait("!" + dialog + ".open");
  await wait("(" + demoRunStatus(designRun) + ").then(status => status === 'junie_running')");
  await stopDemoStage(designRun);

  const buildRun = "demo-partners";
  await evaluate(runAction(designRun, "resume-stage") + ".click()");
  await wait(dialog + ".open");
  await wait(runAction(buildRun, "resume-stage") + "?.matches(':enabled')");
  const reopenedPlan = await evaluate("(" + async function (otherRun) {
    const dialog = document.getElementById("resume-stage-dialog");
    const stage = document.getElementById("resume-stage");
    const transport = window.harnessDemoTransport;
    const fetch = transport.fetch;
    const closed = new Promise(resolve => dialog.addEventListener("close", () => resolve(dialog.open), {once: true}));
    let deliver;
    const plan = new Promise(resolve => { deliver = resolve; });
    transport.fetch = async (resource, options) => {
      const response = await fetch(resource, options);
      const url = new URL(resource, location.href);
      if (url.pathname.endsWith("/resume-plan")) {
        // Release the valid plan after the browser delivers the previous opening's close event.
        deliver({reopened: await closed, status: response.status, run: url.searchParams.get("run_id")});
      }
      return response;
    };
    try {
      dialog.close();
      otherRun.click();
      stage.value = "build-through";
      stage.dispatchEvent(new Event("change", {bubbles: true}));
      document.getElementById("resume-stage-preview").click();
      return await plan;
    } finally { transport.fetch = fetch; }
  }.toString() + ")(" + runAction(buildRun, "resume-stage") + ")");
  assert.deepEqual(reopenedPlan, {reopened: true, status: 200, run: buildRun});
  await waitForConfirmedPlan(buildRun, "build-through");
  assert.match(await evaluate(field("resume-stage-summary") + ".textContent"), /ビルド → チェッカー/);
  await click("resume-stage-start");
  await wait("!" + dialog + ".open");
  await wait("(" + demoRunStatus(buildRun) + ").then(status => status === 'building')");
  await wait(runAction(buildRun, "stop") + " != null");
  for (const width of [320, 1280]) {
    await viewport(width, width === 320 ? 540 : 650);
    const changed = await actionMetrics(buildRun);
    assert(changed.some(button => button.label === "停止") && !changed.some(button => button.label === "再検証"));
    verifyWidths(changed, width, placements.find(place => place.width === width).buttonWidth);
  }
  await stopDemoStage(buildRun);

  const checkerRun = "demo-search";
  await choosePlan(checkerRun, "build-through");
  await change("resume-stage", "checker");
  assert.equal(await evaluate(startButton + ".disabled"), true);
  await click("resume-stage-preview");
  await waitForConfirmedPlan(checkerRun, "checker");
  await click("resume-stage-start");
  await wait("!" + dialog + ".open");
  await wait("(" + demoRunStatus(checkerRun) + ").then(status => status === 'checking')");
  await stopDemoStage(checkerRun);
  const resumeRequests = await evaluate("resumeDesignRequests.map(({kind, data}) => [kind, data.run_id, data.start_stage, data.version ?? data.plan_version, 'through_capsule' in data, 'rewind_to' in data])");
  assert.deepEqual(resumeRequests.map(([kind, run, stage, , through, rewind]) => [kind, run, stage, through, rewind]), [
    ["plan", designRun, "migration", false, false], ["start", designRun, "migration", false, false],
    ["plan", buildRun, "build-through", false, false], ["start", buildRun, "build-through", false, false],
    ["plan", checkerRun, "build-through", false, false], ["plan", checkerRun, "checker", false, false],
    ["start", checkerRun, "checker", false, false],
  ]);
  for (const [plan, start] of [[0, 1], [2, 3], [5, 6]]) {
    assert(resumeRequests[plan][3].startsWith("sample:"));
    assert.equal(resumeRequests[start][3], resumeRequests[plan][3]);
  }
  for (const {width, layout} of placements) {
    const desktop = width >= 1080;
    assert.deepEqual(layout, {
      navigation: ["open-runprogress", "open-runeditor"], execution: ["resume-run", "resume-stage"], remove: "hide-run",
      allFit: true, primary: true, executeTogether: true, navigationTogether: true, viewAndExecuteTogether: desktop,
      gridAreas: desktop ? '"view view execute execute remove"' : '"view view remove" "execute execute ."',
    }, "action groups and non-wrapping controls at width " + width);
  }
  await evaluate("restoreResumeDesignTransport()");
  console.log("Browser resume: same-width actions across runs and status changes at 320-1280 px; migration, build and checker start at the confirmed phase");
  await cdp("Emulation.setDeviceMetricsOverride", {width: 390, height: 480, deviceScaleFactor: 1, mobile: false});

  // All sample runs were stopped or cancelled before the settings test.
  await evaluate("window.confirm = () => true");
  await openDemoRun("run-2026-11-14-001");
  await removeDemoRun("run-2026-11-14-001");
  await pause(3100);
  assert(!(await evaluate(selectedDemo)).includes("run-2026-11-14-001"), "the demo timer must not resurrect a deleted Run");
  await openDemoRun("demo-customer");
  await removeDemoRun("demo-customer");
  await openDemoRun("demo-search");
  await click("tab-runs");
  const remaining = await evaluate("Array.from(document.querySelectorAll('#run-rows tr'), row => row.dataset.runId)");
  assert.equal(remaining.length, 6);
  await removeDemoRun("demo-partners");
  assert((await evaluate(selectedDemo)).includes("demo-search"), "deleting another Run must keep the current selection");
  for (const id of ["demo-search", ...remaining.filter(id => !["demo-search", "demo-partners"].includes(id))]) await removeDemoRun(id);
  await wait("!" + field("runs-empty") + ".hidden && " + selectedDemo + " === ''");
  await click("tab-editor");
  await wait("!" + field("editor-empty") + ".hidden");
  await click("tab-runs");
  await wait("!!document.querySelector('#unit-options input:not(:disabled)')");
  await evaluate("document.querySelector('#unit-picker').open = true; document.querySelector('#unit-options input:not(:disabled)').click()");
  await wait(field("run-selected") + ".matches(':enabled')");
  await click("run-selected");
  const newRow = "document.querySelector('#run-rows tr[data-run-id^=demo-run-]')";
  await wait("!!" + newRow);
  await openDemoRun(await evaluate(newRow + ".dataset.runId"));
  console.log("Browser demo: deletion does not resurrect Runs; empty state can start and open a new mock Run");

  assert.equal(await evaluate("harnessDemoTransport.fetch('unimplemented-operation', {method: 'POST'}).then(r => r.status)"), 404);
  assert.equal(await evaluate("harnessDemoTransport.fetch('https://example.invalid/outside').then(r => r.status)"), 404);
  socket.removeEventListener("message", onRequest);
  const unexpected = network.filter(({url, method}) => {
    if (/^(data|blob):/.test(url)) return false;
    const location = new URL(url);
    return method !== "GET" || location.origin !== staticOrigin ||
      !(location.pathname === nested + "progress-demo.html" || location.pathname === "/favicon.ico" ||
        location.pathname.startsWith(nested + "src/migration_harness/web/"));
  });
  assert.deepEqual(unexpected.map(({method, url}) => [method, url]), []);
  console.log("Browser demo: file and Pages subpath use live tabs, progress, stop, recovery and ledger without API traffic");
  await navigate("Page.navigate", {url: recoveryUrl.href});
  await wait("document.documentElement.dataset.liveReady === 'true'");
  await click("tab-editor");
  await wait(sourceEditor + "?.getModel()?.getLanguageId() === 'csharp'");
  await checkMergedEditorSave({evaluate, wait, click, sourceEditor, run: expectedRun,
    root: path.dirname(ruleConfigPath)});
  await cdp("Browser.close").catch(() => {});
} finally {
  staticServer?.close();
  socket?.close();
  if (chrome.exitCode === null) chrome.kill();
}
