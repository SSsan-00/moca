import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import {spawn} from "node:child_process";
import {setTimeout as pause} from "node:timers/promises";

const [executable, profile, url] = process.argv.slice(2);
const chrome = spawn(executable, [
  "--headless", "--remote-debugging-port=0", "--no-first-run", "--disable-background-networking",
  "--disable-extensions", "--user-data-dir=" + profile, "about:blank",
], {stdio: "ignore"});
let socket, sequence = 0;
const pending = new Map();
async function eventually(read, label) {
  for (let index = 0; index < 100; index++) {
    const value = await read();
    if (value) return value;
    await pause(100);
  }
  throw new Error("Timed out: " + label);
}
try {
  const port = await eventually(async () => {
    try { return (await fs.readFile(path.join(profile, "DevToolsActivePort"), "utf8")).split("\n")[0]; }
    catch (error) { if (error.code !== "ENOENT") throw error; }
  }, "browser debugging port");
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  socket = new WebSocket(pages.find(page => page.type === "page").webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, {once: true});
    socket.addEventListener("error", reject, {once: true});
  });
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    pending.get(message.id)?.(message);
  });
  async function cdp(method, params = {}) {
    const id = ++sequence;
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("Browser command timed out: " + method)), 5000);
      pending.set(id, message => {
        clearTimeout(timeout);
        pending.delete(id);
        if (message.error) reject(new Error(message.error.message));
        else resolve(message.result);
      });
      socket.send(JSON.stringify({id, method, params}));
    });
  }
  async function evaluate(expression) {
    const result = await cdp("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text);
    return result.result.value;
  }
  const wait = expression => eventually(() => evaluate(expression), expression);
  const field = id => `document.getElementById(${JSON.stringify(id)})`;
  const click = id => evaluate(`${field(id)}.click()`);
  const change = (id, value, event = "change") => evaluate(`
    (element => { element.value = ${JSON.stringify(value)}; element.dispatchEvent(new Event(${JSON.stringify(event)}, {bubbles: true})); })(${field(id)})
  `);
  const value = id => evaluate(`${field(id)}.value`);
  const contextScope = "settings-context-scope", size = "settings-context-max-total";
  const folders = "settings-context-folders", limit = "settings-context-max-files";
  const save = 'document.querySelector("#workspace-config-form button[type=submit]")';
  const toml = field("workspace-config");
  await cdp("Runtime.enable");
  await cdp("Page.navigate", {url});
  await wait(`document.readyState === "complete" && !!window.harnessRuns && ${field("tab-settings")}?.offsetWidth`);
  await click("tab-settings");
  await wait(`!${field("settings-context-roots")}.matches(":disabled")`);
  await evaluate('document.querySelector("#settings-project-details > summary").click()');
  for (const width of [390, 768, 1280]) {
    await cdp("Emulation.setDeviceMetricsOverride", {width, height: 900, deviceScaleFactor: 1, mobile: false});
    assert(await evaluate(`document.documentElement.lang === "ja" && document.documentElement.scrollWidth <= ${width}`));
  }
  await evaluate(`${field(contextScope)}.focus()`);
  assert.equal(await evaluate("document.activeElement.id"), contextScope);
  await change(contextScope, "0");
  assert.equal(await value("settings-context-roots"), "default");
  assert(await evaluate(`${field(folders)}.disabled`));
  assert.equal(await value(size), "9007199254740993");
  await evaluate(`const bytes = ${field(size)}.nextElementSibling; bytes.value = "1048576"; bytes.dispatchEvent(new Event("change"))`);
  assert.equal(await value(size), "9007199254740993");
  await change("settings-context-roots", "none");
  await change(limit, "0", "input");
  await evaluate(`${field(folders)}.previousElementSibling.querySelector("input").click()`);
  await wait(`${toml}.value.includes("folders = []") && !${save}.disabled`);
  await click("settings-context-preview");
  await wait(`${field("settings-context-result")}.textContent.includes("0件")`);
  await evaluate(`${field(folders)}.previousElementSibling.querySelector("input").click()`);
  await wait(`!${toml}.value.includes("folders = []") && !${save}.disabled`);
  await click("settings-context-preview");
  await wait(`${field("settings-context-result")}.textContent.includes("上限超過")`);
  await change("settings-viewer-enabled", "false", "input");
  assert(await evaluate(`${field("settings-viewer-html")}.disabled`));
  await wait(`!${save}.disabled`);
  await evaluate(`${save}.click()`);
  await wait(`${field("workspace-config-feedback")}.textContent.includes("設定を保存しました")`);
  await click("workspace-config-reload");
  await wait(`!${field("workspace-config-reload")}.disabled`);
  await change(contextScope, "0");
  assert.equal(await value(limit), "0");
  assert.equal(await value(size), "9007199254740993");
  assert.equal(await value("settings-context-roots"), "none");
  assert(await evaluate(`${field(folders)}.disabled`));
  await change("settings-viewer-enabled", "", "input");
  assert.equal(await value("settings-viewer-html"), "viewer.html");
  assert.equal(await value("settings-anchor-reference"), "anchors.json");
  assert(!(await evaluate(`${field("settings-viewer-html")}.disabled`)));
  console.log("Browser settings: inheritance, empty list, zero, exact bytes, preview and viewer path retention");
  await cdp("Browser.close").catch(() => {});
} finally {
  socket?.close();
  if (chrome.exitCode === null) chrome.kill();
}
