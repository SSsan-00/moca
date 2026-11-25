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
        + ".textContent.startsWith('進捗とエディタで内容を確認してから再開してください。')");
      assert.match(await evaluate(field("recovery-checkpoint") + ".textContent"), /修正下書きあり/);
      assert.match(await evaluate(field("recovery-next") + ".textContent"), /ファイルごとに復元する版を選ぶ必要がある場合は repair-draft/);
    }
  } finally {
    await send("fixture-stopped-repair-reset");
    await wait("snapshot.run_id === " + JSON.stringify(restoredRun) + " && snapshot.ready");
  }
  console.log("Browser recovery: normal resume saved both stopped repair drafts; HTTP and Progress show the version-check guidance");
}
