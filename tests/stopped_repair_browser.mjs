import assert from "node:assert/strict";

export async function checkStoppedRepairRecovery({evaluate, wait, field, restoredRun}) {
  async function send(path) {
    return evaluate("fetch(" + JSON.stringify(path) + ", {method:'POST'}).then(async response => {"
      + "if (!response.ok) throw Error('Repair fixture failed: ' + response.status); return response.json();})");
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
      assert.equal(await evaluate(field("recovery-draft") + ".hidden"), false);
      await evaluate(field("recovery-draft") + ".click()");
      await wait(field("recovery-dialog") + ".open && !!document.querySelector('#recovery-options details')");
      await evaluate("document.querySelector('#recovery-options summary').click()");
      await wait("document.querySelector('#recovery-options .recovery-previews')?.textContent.includes('class PartlyRepaired')");
      await evaluate("document.querySelector('#recovery-options input[value=draft]').click()");
      await evaluate(field("recovery-submit") + ".click()");
      await wait("!" + field("recovery-dialog") + ".open && " + field("recovery-next")
        + ".textContent.includes('復元する版を保存しました')");
    }
  } finally {
    await send("fixture-stopped-repair-reset");
    await wait("snapshot.run_id === " + JSON.stringify(restoredRun) + " && snapshot.ready");
  }
  console.log("Browser recovery: normal resume saved both stopped repair drafts; HTTP and Progress show the version-check guidance");
}
