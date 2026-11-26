import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

const target = "Example.cs";
const selector = id => `document.getElementById("${id}")`;
const status = selector("editor-save-status") + ".textContent";

function editorActions({evaluate, click, sourceEditor, run}) {
  return {
    save: () => click("editor-save"),
    get: query => evaluate(`fetch(${JSON.stringify("editor?" + new URLSearchParams({run_id: run, target, ...query}))}).then(r => r.json())`),
    content: () => evaluate(sourceEditor + ".getValue()"),
    set: text => evaluate(sourceEditor + ".setValue(" + JSON.stringify(text) + ")"),
    select: name => evaluate(`document.querySelector('#editor-target-buttons [data-target="${name}"]').click()`),
    fixture: name => evaluate(`fetch("fixture-editor-${name}", {method:"POST"}).then(r => r.status)`),
  };
}

function observeEditorReceipts(evaluate) {
  return evaluate("(" + function () {
    const fetchReal = window.fetch.bind(window);
    const observed = window.editorReceipts = {saves: [], polls: [], restore: () => { window.fetch = fetchReal; }};
    window.fetch = async (route, options) => {
      const response = await fetchReal(route, options);
      const body = options?.body && JSON.parse(options.body);
      if (route === "editor" && body?.kind === "save") {
        observed.saves.push({body, status: response.status, result: response.ok ? await response.clone().json() : null});
        if (response.ok && observed.dropResponse) {
          observed.dropResponse = false;
          throw new TypeError("fixture dropped successful editor response");
        }
      }
      if (typeof route === "string" && route.startsWith("editor?") && options?.cache === "no-store" && response.ok) {
        const data = await response.clone().json();
        if (data.accepted_save_id || (data.edit === null && data.content)) observed.polls.push(data);
      }
      return response;
    };
  }.toString() + ")()");
}

export async function checkAdoptedEditorSave(options) {
  const {evaluate, wait, sourceEditor} = options;
  const {save, get, content, set, fixture, select} = editorActions(options);
  const immediate = selector("editor-save-timing") + ".textContent === ''";
  const initial = await content();
  await observeEditorReceipts(evaluate);
  try {
    for (const lost of [false, true]) {
      assert.equal(await fixture("queue"), 200);
      await set("class Adopted {}\n");
      await evaluate(`editorReceipts.saves.length = editorReceipts.polls.length = 0; editorReceipts.dropResponse = ${lost}`);
      await save();
      await wait("editorReceipts.saves.at(0)?.status === 200");
      const first = await evaluate("editorReceipts.saves[0]");
      if (!lost) await wait(status + ".includes('ファイルへ反映待ち')");
      const newer = lost ? initial : "class MoreUnsavedWork {}\n";
      await set(newer);
      assert.equal(await fixture("adopt"), 200);
      await wait(`editorReceipts.polls.some(data => data.edit === null && data.accepted_save_id === ${JSON.stringify(first.body.save_id)})`);
      await wait(immediate + " && !" + selector("editor-save") + ".disabled");
      if (!lost) {
        assert.equal(await fixture("switch"), 200);
        await wait(sourceEditor + "?.getValue() === 'class OtherRun {}\\n'");
        assert.equal(await fixture("switch"), 200);
        await wait(sourceEditor + "?.getValue() === " + JSON.stringify(newer));
      }
      assert.equal(await content(), newer);
      await save();
      await wait("editorReceipts.saves.length === 2");
      const second = await evaluate("editorReceipts.saves[1]");
      assert.equal(second.status, 200);
      assert.equal(second.body.edit_id, null);
      assert.equal(second.body.base, first.body.content);
      assert.equal((await get()).content, newer);
      await wait(status + ".includes('ファイルに反映済み')");
    }

    assert.equal(await fixture("queue"), 200);
    await set("class LocallySaved {}\n");
    await save();
    await wait(status + ".includes('ファイルへ反映待ち')");
    const owned = await evaluate("editorReceipts.saves.at(-1)");
    await set("class KeepDraft {}\n");
    const outsider = {...owned.body, content: initial, edit_id: owned.result.edit.id,
      save_id: "f".repeat(32)};
    assert.equal(await evaluate(`fetch("editor", {method:"POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify(${JSON.stringify(outsider)})}).then(r => r.status)`), 200);
    assert.equal(await fixture("adopt"), 200);
    await wait(`editorReceipts.polls.some(data => data.edit === null && data.accepted_save_id === ${JSON.stringify(outsider.save_id)})`);
    await wait(immediate);
    await save();
    await wait("editorReceipts.saves.at(-1)?.status === 409");
    assert.equal(await content(), "class KeepDraft {}\n");
    assert.equal((await get()).content, initial);
    await set(owned.body.content);
    await select("Pages/Index.cshtml");
    await wait(sourceEditor + "?.getModel()?.getLanguageId() === 'razor'");
    await select(target);
    await wait(sourceEditor + ".getValue() === " + JSON.stringify(initial));
    console.log("Browser editor: acknowledged or lost queued save adopted; next draft survives Run switching; foreign adoption still conflicts");
  } finally {
    await evaluate("editorReceipts.restore()");
  }
}

export async function checkMergedEditorSave(options) {
  const {evaluate, wait, sourceEditor, run, root} = options;
  const {save, get, content, set, fixture} = editorActions(options);
  const tree = path.join(root, "trees", run);
  const runPath = path.join(root, "runs", run);
  const agent = path.join(runPath, "agent-workspace");
  const file = path.join(tree, target);
  const example = (first, last) => `class Stable {
    int First = ${first};
    int P1;
    int P2;
    int P3;
    int P4;
    int P5;
    int P6;
    int Last = ${last};
}
`;
  const resetReads = text => wait(sourceEditor + "?.getValue() === " + JSON.stringify(text));
  const prepare = async ({lost = false, conflict = false, reference = false} = {}) => {
    await fs.writeFile(file, example(1, 1));
    await resetReads(example(1, 1));
    const proposal = reference ? path.join(runPath, "reference-proposal-fixture") : agent;
    for (const destination of new Set([agent, proposal])) {
      for (const name of (await get()).targets) {
        const copy = path.join(destination, name);
        await fs.mkdir(path.dirname(copy), {recursive: true});
        await fs.copyFile(path.join(tree, name), copy);
      }
    }
    if (reference) assert.equal(await fixture("pin-reference"), 200);
    assert.equal(await fixture("queue"), 200);
    await set(example(2, 1));
    await evaluate(`editorReceipts.saves.length = editorReceipts.polls.length = 0; editorReceipts.dropResponse = ${lost}`);
    await save();
    await wait("editorReceipts.saves.at(0)?.status === 200");
    const first = await evaluate("editorReceipts.saves[0]");
    if (!lost) await wait(status + ".includes('ファイルへ反映待ち')");
    else await wait(selector("editor-error") + ".textContent.includes('dropped successful')");
    await set(example(3, conflict ? 3 : 1));
    await fs.writeFile(path.join(proposal, target), example(1, 2));
    assert.equal(await fixture(reference ? "refresh-reference" : "merge"), 200);
    return first;
  };
  await observeEditorReceipts(evaluate);
  try {
    for (const variant of [{}, {lost: true}, {reference: true}]) {
      const first = await prepare(variant);
      await wait(`editorReceipts.polls.some(data => data.content === ${JSON.stringify(example(2, 2))})`);
      await wait("!" + selector("editor-update") + ".hidden");
      const adopted = await get();
      const bytes = await fs.readFile(file);
      assert.equal(await content(), example(3, 1));
      await save();
      await wait("editorReceipts.saves.length === 2");
      const second = await evaluate("editorReceipts.saves[1]");
      assert.equal(second.status, 200);
      assert.equal(adopted.content, example(2, 2));
      assert.equal(adopted.accepted_save_id, first.body.save_id);
      assert.equal(adopted.accepted_edit_id, first.result.edit.id);
      assert.equal(adopted.accepted_version, createHash("sha256").update(bytes).digest("hex"));
      assert.equal(second.body.edit_id, null);
      assert.equal(second.body.base, example(2, 1));
      assert.equal(second.body.adoption.version, adopted.accepted_version);
      await wait(status + ".includes('ファイルへ反映待ち')");
      assert.equal(await fixture("merge"), 200);
      await resetReads(example(3, 2));
      assert.equal((await get()).content, example(3, 2));
      if (variant.reference) console.log("Browser editor: reference refresh merges a queued save and accepts later edits without reverting the proposal");
    }

    await prepare({conflict: true});
    await wait(selector("editor-save-timing") + ".textContent.includes('追加の編集を次の反映時に合流')");
    await save();
    await wait("editorReceipts.saves.at(-1)?.status === 200 && editorReceipts.saves.length === 2");
    assert.equal(await fixture("merge"), 409);
    const conflict = await get();
    assert.equal(conflict.edit?.status, "conflict");
    assert.equal(conflict.content, example(2, 2));
    assert.equal(await content(), example(3, 3));
    console.log("Browser editor: the real Junie-output merge preserves First=3 and Last=2 and keeps same-line conflicts visible");
  } finally {
    await evaluate("editorReceipts.restore()");
  }
}
