import assert from "node:assert/strict";

export async function checkStoppedRepairRecovery({evaluate, wait, field, restoredRun}) {
  async function send(path) {
    return evaluate("fetch(" + JSON.stringify(path) + ", {method:'POST'}).then(async response => {"
      + "if (!response.ok) throw Error('Repair fixture failed: ' + response.status); return response.json();})");
  }

  async function chooseVersion(choice, expectedWorktree) {
    assert.equal(await evaluate(field("recovery-draft") + ".hidden"), false);
    await evaluate(field("recovery-draft") + ".click()");
    await wait(field("recovery-dialog") + ".open && !!document.querySelector('#recovery-options details')");
    await evaluate("document.querySelector('#recovery-options summary').click()");
    await wait("document.querySelector('#recovery-options details')?.dataset.loaded === 'done'");
    const columns = await evaluate("Array.from(document.querySelectorAll('#recovery-options .recovery-previews > div'), "
      + "el => [el.querySelector('strong').textContent, el.querySelector('pre').textContent])");
    assert.deepEqual(columns[0], ["停止時の下書き", "class PartlyRepaired {}\n"]);
    assert.equal(columns[1][0], "現在の作業ツリー");
    if (expectedWorktree !== undefined) assert.equal(columns[1][1], expectedWorktree);
    else assert.notEqual(columns[1][1], columns[0][1]);
    await evaluate(`document.querySelector('#recovery-options input[value=${choice}]').click()`);
    await evaluate(field("recovery-submit") + ".click()");
    await wait("!" + field("recovery-dialog") + ".open && " + field("recovery-next")
      + ".textContent.includes('復元する版を保存しました')");
    return columns[1][1];
  }

  try {
    for (const [phase, status] of [["build", "build_failed"], ["checker", "checker_needs_review"]]) {
      const saved = await send("fixture-stopped-repair-" + phase);
      await wait("snapshot.run_id === " + JSON.stringify(saved.run_id) + " && snapshot.ready");
      const recovered = await evaluate("fetch('recovery?run_id=' + snapshot.run_id).then(response => response.json())");
      assert.equal(recovered.run_id, saved.run_id);
      assert.equal(recovered.status, status);
      assert.equal(recovered.draft, "repair");
      assert.equal(recovered.needs_process_check, false);
      await wait("!" + field("recovery-summary") + ".hidden && " + field("recovery-next")
        + ".textContent.includes('復元する版を選んでから再開')");
      assert.match(await evaluate(field("recovery-checkpoint") + ".textContent"), /修正下書きあり/);
      assert.match(await evaluate(field("recovery-next") + ".textContent"), /復元する版を選んでから再開/);
      if (phase === "build") {
        await chooseVersion("draft");
        await send("fixture-stopped-repair-stale");
        await wait("snapshot.message.includes('復元計画を取り直して')");
        assert.match(await evaluate(field("recovery-next") + ".textContent"), /比較して選び直して/);
        assert.equal(await evaluate(field("recovery-draft") + ".textContent"), "復元する版を選び直す");
        await chooseVersion("worktree", "class EditedAfterSelection {}\n");
        await chooseVersion("draft", "class EditedAfterSelection {}\n");
      } else {
        const original = await chooseVersion("worktree");
        await chooseVersion("draft", original);
      }
    }
  } finally {
    await send("fixture-stopped-repair-reset");
    await wait("snapshot.run_id === " + JSON.stringify(restoredRun) + " && snapshot.ready");
  }
  console.log("Browser recovery: reselecting either version preserves the stopped draft and previews current external edits");
}
