const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

let tick;
const createDemoRuns = vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8") + "\ncreateDemoRuns;", {
  structuredClone, window: {}, setInterval: callback => { tick = callback; },
});
const units = ["注文履歴", "顧客詳細", "売上集計", "在庫照会", "請求一覧", "入金照合", "商品検索", "取引先一覧"];
let junie = {model: "sonnet", effort: "high"};
const demo = createDemoRuns("orders", () => units, () => {}, () => junie);
const active = data => data.runs.filter(run => !run.ready && !run.queued);
const queued = data => data.runs.filter(run => run.queued).sort((a, b) => a.queue_position - b.queue_position);

(async () => {
  let data = await demo.read();
  assert.equal(data.parallel_limit, 5);
  assert.equal(active(data).length, 5);
  assert.deepEqual(queued(data).map(run => run.run_id), ["demo-payments"]);
  await demo.execute("run", {unit_ids: ["商品検索", "取引先一覧"]});
  const waiting = queued(await demo.read()).map(run => run.run_id);
  assert.equal(waiting.length, 3);
  await demo.execute("cancel-queued", {run_id: "demo-payments"});
  await demo.execute("stop", {run_id: "orders"});
  data = await demo.read();
  assert.equal(active(data).length, 5);
  assert(active(data).some(run => run.run_id === waiting[1]));
  await demo.execute("resume-run", {run_id: "orders"});
  junie = {model: "opus", effort: "low"};
  const resumed = (await demo.read()).runs.find(run => run.run_id === "orders");
  assert.equal(resumed.junie_model, "sonnet");
  assert.equal(resumed.junie_effort, "high");
  assert.deepEqual(queued(await demo.read()).map(run => run.run_id), [waiting[2], "orders"]);
  await assert.rejects(demo.execute("resume-run", {run_id: "orders"}));
  await demo.execute("stop", {run_id: "demo-customer"});
  assert(active(await demo.read()).some(run => run.run_id === waiting[2]));
  for (let i = 0; i < 70; i++) {
    tick();
    assert(active(await demo.read()).length <= 5);
  }
  assert.equal((await demo.read()).busy, false);
  await demo.execute("prepare", {unit_id: "商品検索"});
  const prepared = (await demo.read()).runs.at(-1);
  assert.equal(prepared.junie_model, "opus");
  assert.equal(prepared.junie_effort, "low");
  console.log("Demo: five workers, FIFO resume, cancellation, and completion passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
