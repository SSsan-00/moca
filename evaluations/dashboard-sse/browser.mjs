import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {beginPhase, cdp, evaluate, finishBrowser, navigate, reportBrowserFailure,
  startBrowser, wait} from "../../tests/browser/chromium_browser.mjs";

const [, , binary, profile, base, runA, runB] = process.argv;
const chrome = await startBrowser(binary, profile);
let success = false;
try {
  await cdp("Performance.enable");
  await cdp("Page.addScriptToEvaluateOnNewDocument", {source: "(" + (() => {
    const encoder = new TextEncoder();
    const probe = window.bench = {events: [], bytes: 0, listWrites: 0};
    const NativeEventSource = window.EventSource;
    window.EventSource = class extends NativeEventSource {
      constructor(...args) {
        super(...args);
        this.addEventListener("message", event => {
          const {run_id: run, status, stop_requested: stop, activity} = JSON.parse(event.data);
          probe.events.push({run, id: Number(event.lastEventId), status, stop: !!stop, sequence: activity?.sequence});
          probe.bytes += encoder.encode("id: " + event.lastEventId + "\ndata: " + event.data + "\n\n").length;
        });
      }
    };
    new MutationObserver(records => {
      probe.listWrites += records.filter(record => record.target.id === "run-summary").length;
    }).observe(document, {childList: true, subtree: true});
  }).toString() + ")();"});
  beginPhase("Open dashboard and select synthetic run");
  await navigate("Page.navigate", {url: new URL("?run=" + runA, base).href});
  await wait("document.querySelectorAll('#run-rows tr').length === 5 && connected && snapshot.run_id === " + JSON.stringify(runA));
  async function metric() {
    return Object.fromEntries((await cdp("Performance.getMetrics")).metrics.map(({name, value}) => [name, value]));
  }
  async function burst(start, count) {
    const latest = await evaluate("fetch('bench-burst?start=" + start + "&count=" + count + "').then(r => r.json())");
    await wait("dashboardRevision === " + latest[await evaluate("snapshot.run_id")]);
    return latest;
  }
  beginPhase("Five runs × 64 ordered events");
  const before = await metric();
  const writes = await evaluate("bench.listWrites");
  const tick = performance.now();
  await burst(1, 64);
  const elapsedMs = performance.now() - tick;
  const after = await metric();
  const listWrites = (await evaluate("bench.listWrites")) - writes;
  const aEvents = await evaluate("bench.events");
  assert.equal(aEvents.length, 65);
  assert.deepEqual(aEvents.slice(1).map(e => e.sequence), Array.from({length: 64}, (_, i) => i + 1));
  assert.equal(aEvents[59].stop, true);
  assert.equal(aEvents[60].stop, false);
  const aLog = await evaluate("[...document.querySelectorAll('#activity [data-event-kind=activity] strong')].map(e => e.parentElement.textContent)");
  assert.equal(aLog.length, 64);
  assert.match(aLog.at(-1), /unit-1 064/);
  assert.equal(await evaluate("document.querySelector('#selected-run-0').value"), runA);

  beginPhase("Switch Run and inspect its editor");
  const switchStart = performance.now();
  await evaluate("document.querySelector(" + JSON.stringify("#run-rows tr[data-run-id='" + runB + "'] [data-action=open-runeditor]") + ").click()");
  await wait("snapshot.run_id === " + JSON.stringify(runB) + " && window.monaco?.editor.getModels().some(m => m.getValue().includes('class Bench2'))");
  const switchMs = performance.now() - switchStart;
  assert.equal(await evaluate("document.querySelector('#selected-run-1').value"), runB);
  const editor = "window.monaco.editor.getEditors().find(e => document.querySelector('#editor-code').contains(e.getDomNode()))";
  await evaluate(editor + ".setValue('class UnsavedBench2 {}\\n')");
  await evaluate(editor + ".setSelection({startLineNumber: 1, startColumn: 7, endLineNumber: 1, endColumn: 18})");
  const editBefore = await evaluate("[" + editor + ".getValue(), " + editor + ".getSelection()]");

  beginPhase("Disconnect, replay, and preserve the unsaved editor");
  const last = await evaluate("dashboardRevision");
  await evaluate("fetch('bench-drop?run=" + runB + "')");
  await wait("!connected");
  await burst(65, 20);
  const editAfter = await evaluate("[" + editor + ".getValue(), " + editor + ".getSelection()]");
  assert.deepEqual(editAfter, editBefore);
  const reconnect = await evaluate("fetch('bench-counts').then(r => r.json())");
  assert.deepEqual(reconnect.reconnects.filter(([id]) => id === runB), [[runB, last]]);
  const bEvents = (await evaluate("bench.events")).filter(e => e.run === runB);
  assert.deepEqual(bEvents.slice(1).map(e => e.sequence), Array.from({length: 84}, (_, i) => i + 1));
  const bLog = await evaluate("[...document.querySelectorAll('#activity [data-event-kind=activity] strong')].map(e => e.parentElement.textContent)");
  assert.equal(bLog.length, 84);
  assert.match(bLog.at(-1), /unit-2 084/);
  assert.equal(await evaluate("snapshot.current_capsule"), 6);
  assert.equal(await evaluate("connected && document.querySelector('#selected-run-1').value === snapshot.run_id"), true);
  console.log(JSON.stringify({
    elapsedMs, switchMs, listWrites, requests: reconnect.requests, sseEvents: await evaluate("bench.events.length"),
    sseBytes: await evaluate("bench.bytes"), layoutCount: after.LayoutCount - before.LayoutCount,
    chromiumTaskMs: (after.TaskDuration - before.TaskDuration) * 1000,
    scriptMs: (after.ScriptDuration - before.ScriptDuration) * 1000, jsHeapBytes: after.JSHeapUsedSize,
    visibleDigest: createHash("sha256").update(JSON.stringify([aLog, bLog, editAfter,
      await evaluate("document.querySelector('#state-label').textContent"),
      await evaluate("document.querySelector('#checked-count').textContent")])).digest("hex"),
  }));
  success = true;
} catch (error) {
  reportBrowserFailure(error);
  process.exitCode = 1;
} finally {
  await finishBrowser(chrome, success);
}
