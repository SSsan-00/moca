import fs from "node:fs/promises";
import path from "node:path";
import {spawn} from "node:child_process";
import {setTimeout as pause} from "node:timers/promises";

export let socket;
let sequence = 0;
const pending = new Map();
const started = performance.now();
let phase, phaseStarted, completed = "none", conditionMs = 0, polls = 0;
const seconds = ms => (ms / 1000).toFixed(2) + "s";

function trace(message) {
  console.log("[browser +" + seconds(performance.now() - started) + "] " + message);
}

function finishPhase() {
  if (!phase) return;
  trace("complete " + phase + " (" + seconds(performance.now() - phaseStarted)
    + "; condition waits " + seconds(conditionMs) + ", " + polls + " polls)");
  completed = phase;
  phase = null;
}

export function beginPhase(name) {
  finishPhase();
  phase = name;
  phaseStarted = performance.now();
  conditionMs = polls = 0;
  trace("start " + name + "; last completed: " + completed);
}
export async function eventually(read, label, attempts = 100) {
  const start = performance.now();
  try {
    for (let index = 0; index < attempts; index++) {
      polls++;
      const value = await read();
      if (value) return value;
      await pause(100);
    }
    throw new Error("Timed out: " + label);
  } finally {
    conditionMs += performance.now() - start;
  }
}
export async function cdp(method, params = {}, waitMs = 10_000) {
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
  await cdp("Runtime.enable", {}, Math.min(10_000, Math.max(1, deadline - performance.now())));
  await cdp("Page.enable", {}, Math.max(1, deadline - performance.now()));
}

function waitForExit(chrome, timeout) {
  if (chrome.pid === undefined || chrome.exitCode !== null || chrome.signalCode !== null) return Promise.resolve(true);
  return new Promise(resolve => {
    const exited = () => { clearTimeout(timer); resolve(true); };
    const timer = setTimeout(() => {
      chrome.removeListener("exit", exited);
      resolve(false);
    }, timeout);
    chrome.once("exit", exited);
  });
}

async function closeBrowser(chrome) {
  try {
    if (socket?.readyState === WebSocket.OPEN) {
      // Browser.close may close DevTools before replying. Wait for the process, not a CDP reply.
      try {
        socket.send(JSON.stringify({id: ++sequence, method: "Browser.close"}));
      } catch (error) {
        trace("DevTools closed during Chromium shutdown: " + error.message);
      }
    }
    if (!await waitForExit(chrome, 1_500)) {
      trace("Chromium did not exit after close; stopping the test process");
      chrome.kill();
      if (!await waitForExit(chrome, 1_500)) throw new Error("Chromium did not exit after termination");
    }
  } finally {
    socket?.close();
    socket = null;
  }
}

// Launch Chromium with a fresh profile and wait for the DevTools endpoint.
export async function startBrowser(executable, profile) {
  const failures = [];
  for (let attempt = 1; attempt <= 2; attempt++) {
    const directory = `${profile}-${attempt}`;
    const chrome = spawn(executable, [
      "--headless", "--remote-debugging-port=0", "--no-first-run", "--disable-background-networking",
      "--disable-extensions", "--enable-logging=stderr", "--user-data-dir=" + directory, "about:blank",
    ], {stdio: ["ignore", "ignore", "pipe"]});
    trace("Chromium startup attempt " + attempt);
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
            trace("Chromium DevTools ready");
            return chrome;
          }
        }
        await pause(100);
      }
      throw new Error("Timed out waiting for Chromium DevTools");
    } catch (error) {
      trace("Chromium startup attempt " + attempt + " failed: " + error.message);
      failures.push(`Attempt ${attempt}: ${error.message}\n${stderr.trim() || "(no browser stderr)"}`);
    } finally {
      if (!ready) await closeBrowser(chrome);
    }
  }
  throw new Error(`Chromium did not start:\n${failures.join("\n")}`);
}

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
export async function navigate(method, params) {
  await Promise.all([pageLoaded(), cdp(method, params)]);
}
export async function evaluate(expression, userGesture = false) {
  const result = await cdp("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true, userGesture});
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text);
  return result.result.value;
}
export const wait = (expression, attempts) => eventually(async () => {
  try {
    return await evaluate(expression);
  } catch (error) {
    // Chrome may replace the JavaScript context during a real navigation.
    if (!/Execution context was destroyed|Cannot find context|Inspected target navigated or closed/.test(error.message)) throw error;
    return false;
  }
}, expression, attempts);

export function reportBrowserFailure(error) {
  trace("failed " + phase + "; last completed: " + completed);
  console.error(error);
}

export async function finishBrowser(chrome, success) {
  if (success) finishPhase();
  phase = null;
  beginPhase("Chromium teardown");
  await closeBrowser(chrome);
  finishPhase();
}
