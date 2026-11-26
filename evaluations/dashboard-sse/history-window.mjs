import assert from "node:assert/strict";
import {beginPhase, cdp, evaluate, finishBrowser, navigate, reportBrowserFailure,
  startBrowser, wait} from "../../tests/chromium_browser.mjs";

const [, , binary, profile, base, runA, runB] = process.argv;
const chrome = await startBrowser(binary, profile);
const probe = "window.reconnectProbe";
const id = JSON.stringify;
const editor = "window.monaco?.editor.getEditors().find(e => document.querySelector('#editor-code').contains(e.getDomNode()))";
const visible = run => "connected && snapshot.run_id === " + id(run);
const postState = () => evaluate("fetch('bench-window-controls').then(response => response.json())");
const currentEdit = () => evaluate("[" + editor + ".getValue(), " + editor + ".getSelection()]");
async function switchEditor(run, marker) {
  const selector = "#run-rows tr[data-run-id='" + run + "'] [data-action=open-runeditor]";
  await evaluate("document.querySelector(" + id(selector) + ").click()");
  await wait(visible(run) + " && " + editor + "?.getValue().includes(" + id(marker) + ")");
}

let success = false;
try {
  await cdp("Page.addScriptToEvaluateOnNewDocument", {source: "(" + (() => {
    const probe = window.reconnectProbe = {events: [], disconnects: [], streams: new Map()};
    const NativeEventSource = window.EventSource;
    window.EventSource = class extends NativeEventSource {
      constructor(...args) {
        super(...args);
        probe.streams.set(new URL(this.url).searchParams.get("run_id"), this);
        this.addEventListener("message", event => {
          const {run_id: run, operation_id: operation, stop_requested: pending, cancellable} = JSON.parse(event.data);
          probe.events.push({run, revision: Number(event.lastEventId), operation, pending, cancellable});
        });
        this.addEventListener("error", () => setTimeout(() => probe.disconnects.push({
          run: snapshot.run_id, connected, stopDisabled: document.querySelector("#stop").disabled,
        }), 0));
      }
    };
  }).toString() + ")();"});
  beginPhase("Open real dashboard, switch A to B, and keep an unsaved draft");
  await navigate("Page.navigate", {url: new URL("?run=" + runA, base).href});
  await wait(visible(runA) + " && document.querySelectorAll('#run-rows tr').length === 5");
  const oldRun = await evaluate("snapshot");
  await switchEditor(runB, "class Bench2");
  const staleEventRejected = await evaluate(`(() => {
    const before = JSON.stringify([snapshot, dashboardRevision]);
    ${probe}.streams.get(${id(runA)}).dispatchEvent(new MessageEvent("message", {
      data: ${id(JSON.stringify(oldRun))}, lastEventId: "999",
    }));
    return before === JSON.stringify([snapshot, dashboardRevision]);
  })()`);
  assert.equal(staleEventRejected, true, "a delayed event from the old Run must not overwrite the current Run");
  await evaluate(editor + ".setValue('class UnsavedWindowProbe {}\\n')");
  await evaluate(editor + ".setSelection({startLineNumber: 1, startColumn: 7, endLineNumber: 1, endColumn: 19})");
  const draft = await currentEdit();

  beginPhase("Submit a synthetic Stop through the real dashboard route");
  const stopRevision = await evaluate("dashboardRevision");
  await evaluate("document.querySelector('#stop').click()");
  await wait("snapshot.stop_requested && !submitting && document.querySelector('#stop').disabled");
  assert.deepEqual((await postState()).stops, [{run_id: runB, operation_id: "operation-2", revision: stopRevision}]);

  const results = [];
  for (const count of [20, 100, 137]) {
    beginPhase("Reconnect after " + count + " revisions without losing selected Run or draft");
    const before = await evaluate("dashboardRevision");
    const response = await evaluate("fetch('bench-window?run=" + runB + "&count=" + count + "').then(r => r.json())");
    assert.equal(response.before, before);
    assert.equal(response.last, before + count);
    assert.ok(response.retained <= 100);
    await wait(visible(runB) + " && dashboardRevision === " + response.last);
    const events = await evaluate(probe + ".events.filter(e => e.run === " + id(runB) + " && e.revision > " + before + ")");
    const delivered = Math.min(100, count);
    assert.deepEqual(events.map(e => e.revision), Array.from({length: delivered}, (_, i) => response.last - delivered + i + 1));
    assert.equal(events[0].revision, Math.max(response.first, before + 1));
    assert.deepEqual(await currentEdit(), draft);
    const action = await evaluate("({pending: snapshot.stop_requested, operation: snapshot.operation_id, disabled: document.querySelector('#stop').disabled, pendingLocal: stopPendingOperation})");
    if (count <= 100) {
      assert.deepEqual(action, {pending: true, operation: "operation-2", disabled: true, pendingLocal: "operation-2"});
      assert.ok(events.every(e => e.pending && !e.cancellable));
    } else {
      assert.deepEqual(action, {pending: false, operation: "operation-2-new", disabled: false});
      assert.ok(events.slice(0, -1).every(e => e.pending && !e.cancellable));
    }
    assert.equal((await postState()).stops.length, 1, "reconnection must not replay a POST");
    results.push({count, cursor: before, retained: response.retained, delivered,
      firstDelivered: events[0].revision, lastDelivered: events.at(-1).revision, operation: action.operation,
      pendingStop: action.pending, stopDisabled: action.disabled, missingBeforeBuffer: count - delivered});
  }
  const counts = await evaluate("fetch('bench-counts').then(response => response.json())");
  assert.deepEqual(counts.reconnects.filter(([run]) => run === runB), results.map(result => [runB, result.cursor]));
  assert.deepEqual(await evaluate(probe + ".disconnects"), results.map(() => ({
    run: runB, connected: false, stopDisabled: true,
  })));

  beginPhase("Switch to A and back; Stop stays scoped to B's current operation");
  await switchEditor(runA, "class Bench1");
  assert.equal(await evaluate("snapshot.stop_requested || document.querySelector('#stop').disabled"), false);
  await switchEditor(runB, "class UnsavedWindowProbe");
  const switched = await currentEdit();
  assert.equal(switched[0], draft[0]);
  const newRevision = await evaluate("dashboardRevision");
  await evaluate("document.querySelector('#stop').click()");
  await wait("snapshot.stop_requested && !submitting && document.querySelector('#stop').disabled");
  const controls = await postState();
  assert.deepEqual(controls.stops, [
    {run_id: runB, operation_id: "operation-2", revision: stopRevision},
    {run_id: runB, operation_id: "operation-2-new", revision: newRevision},
  ]);
  assert.equal(controls.cancelled[runA], false);
  assert.equal(controls.cancelled[runB], true);
  console.log(JSON.stringify({results, reconnects: counts.reconnects.filter(([run]) => run === runB),
    staleEventRejected, noStopReplay: true, nextOperationStopAccepted: true, otherRunUnaffected: true,
    unsavedContentsAndSelectionAfterReconnect: true,
    draftContentAfterSwitch: true, selectionStayedAfterSwitch: JSON.stringify(switched[1]) === JSON.stringify(draft[1])}));
  success = true;
} catch (error) {
  reportBrowserFailure(error);
  process.exitCode = 1;
} finally {
  await finishBrowser(chrome, success);
}
