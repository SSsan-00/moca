"use strict";

// サーバーを使わず、同じ一覧UIで並列数・停止・再開・待機順を試せるデモ。
function createDemoRuns(initialId, units, show, junie = () => ({})) {
  const limit = 5;
  let selected = initialId;
  let sequence = 8;
  const pending = [];
  const hidden = new Set();
  const make = (id, unit, status, ready, tick = 0) => ({run_id: id, unit_id: unit, status, ready, tick,
    junie_model: junie().model || null, junie_effort: junie().effort || null,
    created_at: new Date().toISOString(), context_capsules: 5, current_capsule: 1, completed_capsules: 0,
    worktree_path: status === "queued" ? null : `demo/${id}`, queued: status === "queued",
    resumable: ready, cancellable: !ready && status !== "queued", elapsed_seconds: tick * 3});
  const runs = [make(initialId, "注文履歴", "junie_running", false, 8),
    make("demo-customer", "顧客詳細", "building", false, 22),
    make("demo-sales", "売上集計", "junie_running", false, 12), make("demo-stock", "在庫照会", "junie_running", false, 4),
    make("demo-invoices", "請求一覧", "checking", false, 25), make("demo-payments", "入金照合", "queued", false),
    make("demo-search", "商品検索", "checker_needs_review", true, 30),
    make("demo-partners", "取引先一覧", "checker_succeeded", true, 30)];
  pending.push(...runs.filter(run => run.queued));
  function enqueue(run) {
    run.queued = true;
    run.ready = false;
    pending.push(run);
  }
  function publish() {
    for (const run of runs) {
      run.queue_position = run.queued ? pending.indexOf(run) + 1 : undefined;
      run.current_capsule = Math.min(5, Math.max(1, Math.floor(run.tick / 4) + 1));
      run.completed_capsules = run.tick >= 20 ? 5 : run.current_capsule - 1;
      run.cancellable = !run.ready && !run.queued;
      run.resumable = run.ready && !!run.worktree_path && !runs.some(item => item !== run && item.unit_id === run.unit_id && !item.ready);
    }
    const run = runs.find(item => item.run_id === selected);
    if (run) {
      window.harnessDemoSelectedRun = run;
      window.harnessEditorDemo?.select(run);
      show(run);
    }
  }
  function schedule() {
    let active = runs.filter(run => !run.ready && !run.queued).length;
    while (active < limit && pending.length) {
      const run = pending.shift();
      Object.assign(run, {queued: false, status: "preparing", worktree_path: `demo/${run.run_id}`});
      active++;
    }
    publish();
  }
  setInterval(() => {
    for (const run of runs) if (!run.ready && !run.queued) {
      run.tick++;
      run.elapsed_seconds += 3;
      if (run.prepareOnly && run.tick >= 2) Object.assign(run, {status: "prepared", ready: true});
      else if (run.tick >= 29) Object.assign(run, {status: "checker_succeeded", ready: true});
      else run.status = run.tick < 2 ? "preparing" : run.tick < 20 ? "junie_running" : run.tick < 25 ? "building" : "checking";
    }
    schedule();
  }, 3000);
  return {
    read: async () => {
      return {units: units(), runs: structuredClone(runs.filter(run => !hidden.has(run.run_id)).map(run => ({...run, queue_position: run.queued ? pending.indexOf(run) + 1 : undefined}))),
        parallel_limit: limit, busy: runs.some(run => !run.ready)};
    },
    async execute(action, data) {
      if (action === "run" || action === "prepare") {
        const selectedUnits = data.unit_ids || [data.unit_id];
        if (selectedUnits.some(unit => runs.some(run => run.unit_id === unit && !run.ready))) throw new Error("選択した移行単位は実行中または待機中です。");
        for (const unit of selectedUnits) {
          const run = make(`demo-run-${++sequence}`, unit, "queued", false);
          run.prepareOnly = action === "prepare";
          runs.push(run);
          enqueue(run);
        }
      } else {
        const run = runs.find(item => item.run_id === data.run_id);
        if (!run) throw new Error("実行一覧からRunを選び直してください。");
        if (action === "open-run") {
          if (window.harnessEditorCanSwitch && !window.harnessEditorCanSwitch()) throw new Error("保存または送信が終わってから切り替えてください。");
          document.dispatchEvent(new Event("harnessrunselect"));
          selected = run.run_id;
          publish();
          document.dispatchEvent(new Event("harnessrunchange"));
        } else if (action === "hide-run") {
          if (!run.ready || run.queued) throw new Error("このRunは処理中です。終了してから操作してください。");
          hidden.add(run.run_id);
        } else if (action === "stop") Object.assign(run, {ready: true, status: "interrupted"});
        else if (action === "cancel-queued") {
          if (!run.queued) throw new Error("このRunはすでに実行されています。一覧を更新して停止してください。");
          pending.splice(pending.indexOf(run), 1);
          Object.assign(run, {queued: false, ready: true, status: "cancelled"});
        } else if (action === "resume-run") {
          if (!run.ready || !run.worktree_path) throw new Error("このRunは再開できません。実行一覧を確認してください。");
          if (runs.some(item => item !== run && item.unit_id === run.unit_id && !item.ready)) throw new Error("この移行単位は別のRunで実行中または待機中です。");
          Object.assign(run, {prepareOnly: false, tick: Math.min(run.tick, 20)});
          enqueue(run);
        }
      }
      schedule();
    },
    select: runId => { selected = runId; publish(); },
    get selected() { return selected; },
  };
}
