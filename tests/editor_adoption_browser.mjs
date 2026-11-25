import assert from "node:assert/strict";

export async function checkAdoptedEditorSave({evaluate, wait, click, sourceEditor, run}) {
  const target = "Example.cs";
  const selector = id => `document.getElementById("${id}")`;
  const status = selector("editor-save-status") + ".textContent";
  const immediate = selector("editor-save-timing") + ".textContent === ''";
  const save = () => click("editor-save");
  const get = query => evaluate(`fetch(${JSON.stringify("editor?" + new URLSearchParams({run_id: run, target, ...query}))}).then(r => r.json())`);
  const content = () => evaluate(sourceEditor + ".getValue()");
  const set = text => evaluate(sourceEditor + ".setValue(" + JSON.stringify(text) + ")");
  const fixture = name => evaluate(`fetch("fixture-editor-${name}", {method:"POST"}).then(r => r.status)`);
  const initial = await content();
  await evaluate("(" + function () {
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
        if (data.accepted_save_id) observed.polls.push(data);
      }
      return response;
    };
  }.toString() + ")()");
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
    await wait(sourceEditor + ".getValue() === " + JSON.stringify(initial));
    console.log("Browser editor: acknowledged or lost queued save adopted; next draft survives Run switching; foreign adoption still conflicts");
  } finally {
    await evaluate("editorReceipts.restore()");
  }
}
