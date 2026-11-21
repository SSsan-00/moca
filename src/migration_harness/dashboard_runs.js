"use strict";

// 実行一覧はデモと本番で共用し、進捗・エディタは選択したRunだけを表示する。
function bindRunManager({readRuns, execute, canAct, preparationChat = false}) {
  const find = selector => document.querySelector(selector);
  const form = find("#workspace-new-run");
  const options = find("#unit-options");
  const rows = find("#run-rows");
  const feedback = find("#workspace-run-feedback");
  const dryRunButton = find("#dry-run-selected");
  let dryRunId;
  let dryRunCancelling = false;
  const search = find("#run-search");
  const summary = find("#run-summary");
  const selectedUnits = new Set();
  const filters = new Map();
  let data = {units: [], runs: [], busy: false};
  let selectedRun = "";
  let filter = "all";
  let pending = false;
  let loading = false;
  let optionKey;
  let rowKey;
  let contextKey;
  const labels = {all: "すべて", active: "実行中", queued: "実行待ち", review: "要確認", paused: "停止・準備", done: "検証済み"};
  const statuses = {prepared: "準備完了", preparing: "準備中", junie_running: "移行中", editor_followup: "指示を反映中",
    repairing: "自動修正中", building: "ビルド中", checking: "チェック中", script_running: "スクリプト実行中",
    checker_succeeded: "検証済み", checker_accepted: "検証済み", build_succeeded: "検証済み",
    checker_needs_review: "確認待ち", checker_failed: "チェック失敗", build_failed: "ビルド失敗",
    junie_failed: "移行を確認", interrupted: "停止中", editor_checkpoint: "編集を確認", cancelled: "取消済み",
    preparation_failed: "準備に失敗", preparation_interrupted: "準備を中断", script_failed: "スクリプト失敗"};
  // 保存状態から実行一覧の分類を決める。
  function category(run) {
    if (run.queued) return "queued";
    if (run.awaiting_answer) return "review";
    if (!run.ready) return "active";
    if (["checker_succeeded", "checker_accepted", "build_succeeded"].includes(run.status)) return "done";
    if (["checker_needs_review", "checker_failed", "build_failed", "junie_failed", "editor_checkpoint", "preparation_failed", "script_failed"].includes(run.status) || run.error) return "review";
    return "paused";
  }
  // 実行一覧の表示文字を、HTMLとして解釈せず要素に入れる。
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  // 実行操作の結果を表示するタブにフォーカスを移す。
  function goTab(id) {
    switchHarnessTab(harnessTabs.indexOf(find(id)), true);
  }
  summary.addEventListener("click", () => goTab("#tab-runs"));
  for (const [value, label] of Object.entries(labels)) {
    const button = node("button", label);
    button.type = "button";
    button.addEventListener("click", () => { filter = value; render(); });
    find("#run-filters").append(button);
    filters.set(value, button);
  }
  for (const [index, host] of [...document.querySelectorAll("[data-run-context]")].entries()) {
    const label = node("label", "表示する実行");
    const select = node("select");
    select.id = `selected-run-${index}`;
    label.htmlFor = select.id;
    select.addEventListener("change", () => act("open-run", select.value, index ? "editor" : "progress"));
    const back = node("button", "実行一覧へ戻る", "button");
    back.type = "button";
    back.addEventListener("click", () => goTab("#tab-runs"));
    host.append(label, select, back);
  }
  // 接続状態と送信中の操作から、実行・再開ボタンを更新する。
  function sync() {
    const available = canAct() && !pending;
    for (const button of form.querySelectorAll("button")) {
      button.disabled = button === dryRunButton && dryRunId
        ? dryRunCancelling : !available || !selectedUnits.size;
    }
    dryRunButton.type = dryRunId ? "button" : "submit";
    dryRunButton.textContent = dryRunId ? "検証を中止" : "設定を検証";
    find("#run-selected").textContent = selectedUnits.size ? `選択した${selectedUnits.size}件を実行` : "選択した単位を実行";
    find("#unit-selection-label").textContent = selectedUnits.size ? [...selectedUnits].join("・") : "移行単位を選択";
    for (const input of options.querySelectorAll("input")) input.disabled = !available || input.dataset.busy === "true";
    for (const button of rows.querySelectorAll("button")) button.disabled = !available;
    for (const select of document.querySelectorAll("[data-run-context] select")) select.disabled = !available;
  }
  // 実行中の経過時間だけを保存済みの計測へ加える。
  function elapsed(run) {
    const now = Date.now() / 1000;
    let seconds = Object.values(run.stage_timings || {}).reduce((total, stage) => total + (stage.elapsed_seconds || 0)
      + (!run.ready && !run.queued && stage.started_at ? Math.max(0, now - stage.started_at) : 0), 0);
    if (!seconds && run.elapsed_seconds) seconds = run.elapsed_seconds;
    if (!seconds) return "—";
    const parts = [Math.floor(seconds / 60) % 60, Math.floor(seconds) % 60];
    if (seconds >= 3600) parts.unshift(Math.floor(seconds / 3600));
    return parts.map(value => String(value).padStart(2, "0")).join(":");
  }
  // 保存状態、待機位置、処理中の通知を実行一覧の案内へ変換する。
  function describe(run) {
    if (run.queued) {
      const position = `待機順 ${run.queue_position || "—"}`;
      return run.queue_saving ? `${position} · 待機一覧の保存を再試行中` : position;
    }
    if (run.awaiting_answer) return "Junieからの質問を確認してください";
    if (run.phase_activity && !run.ready) return run.phase_activity.message;
    if (run.context_capsules && run.status === "junie_running") return `カプセル ${run.current_capsule || 1} / ${run.context_capsules}`;
    if (category(run) === "done") return `${run.status === "build_succeeded" ? "ビルド済み" : "ビルド・チェック済み"}（台帳は進捗で確認）`;
    if (run.status === "building") return "ビルド・テストを実行";
    if (run.status === "checker_needs_review") return "チェッカーの指摘を確認";
    if (run.status === "cancelled") return "実行待ちから取り消しました";
    if (run.status === "prepared") return "移行を再開できます";
    return run.error ? "進捗で原因と次の操作を確認" : statuses[run.status] || "進捗を確認";
  }
  // フィルター、並列枠、Runの状態・操作を一覧へ反映する。
  function render() {
    const counts = Object.fromEntries(Object.keys(labels).map(key => [key, key === "all" ? data.runs.length : data.runs.filter(run => category(run) === key).length]));
    const active = data.runs.filter(run => !run.ready && !run.queued).length;
    const limit = data.parallel_limit ?? "—";
    find("#parallel-limit").textContent = limit;
    summary.textContent = `実行中 ${active} / ${limit}　待機 ${counts.queued}　要確認 ${counts.review}`;
    for (const [value, button] of filters) {
      button.textContent = `${labels[value]} ${counts[value]}`;
      button.setAttribute("aria-pressed", String(value === filter));
    }
    find("#run-count").textContent = `${data.runs.length}件`;
    const busyUnits = data.runs.filter(run => !run.ready || run.queued).map(run => run.unit_id);
    const nextOptions = JSON.stringify([data.units, busyUnits]);
    if (nextOptions !== optionKey) {
      optionKey = nextOptions;
      for (const unit of selectedUnits) if (!data.units.includes(unit) || busyUnits.includes(unit)) selectedUnits.delete(unit);
      options.replaceChildren(...data.units.map(unit => {
        const label = node("label");
        const input = node("input");
        input.type = "checkbox";
        input.value = unit;
        input.checked = selectedUnits.has(unit);
        input.dataset.busy = String(busyUnits.includes(unit));
        input.addEventListener("change", () => { if (input.checked) selectedUnits.add(unit); else selectedUnits.delete(unit); sync(); });
        label.append(input, node("span", unit));
        if (busyUnits.includes(unit)) label.append(node("small", "実行中・待機中"));
        return label;
      }));
      if (!data.units.length) options.append(node("p", "設定タブで移行単位を登録してください。"));
    }
    const opened = data.runs.filter(run => preparationChat || run.worktree_path);
    const nextContext = JSON.stringify(opened.map(run => [run.run_id, run.unit_id]));
    if (nextContext !== contextKey) {
      contextKey = nextContext;
      for (const select of document.querySelectorAll("[data-run-context] select")) {
        select.replaceChildren(new Option("実行一覧から選択", ""), ...opened.map(run => new Option(`${run.unit_id} · ${run.run_id.slice(0, 8)}`, run.run_id)));
      }
    }
    for (const select of document.querySelectorAll("[data-run-context] select")) select.value = selectedRun;
    const query = search.value.trim().toLocaleLowerCase("ja");
    const visible = data.runs.filter(run => (filter === "all" || category(run) === filter)
      && `${run.unit_id} ${run.run_id}`.toLocaleLowerCase("ja").includes(query));
    const nextRows = JSON.stringify(visible.map(run => [run.run_id, run.unit_id, run.status, run.ready, run.queued, run.queue_position, run.queue_saving,
      run.resumable, run.build_resumable, run.stop_requested, run.cancellable, run.awaiting_answer, run.worktree_path, describe(run), run.current_capsule, run.context_capsules, selectedRun === run.run_id]));
    if (nextRows !== rowKey) {
      rowKey = nextRows;
      const focused = document.activeElement;
      const focusId = focused?.closest("tr")?.dataset.runId;
      const focusAction = focused?.dataset.action;
      rows.replaceChildren(...visible.map(run => {
        const row = node("tr");
        row.dataset.runId = run.run_id;
        row.dataset.selected = String(run.run_id === selectedRun);
        const name = node("td");
        name.append(node("span", run.unit_id, "run-unit"), node("span", run.run_id, "run-id"));
        const state = node("td");
        const tone = category(run);
        const status = node("span", `${({active: "●", queued: "◷", review: "!", paused: "Ⅱ", done: "✓"})[tone]} ${run.queued ? "実行待ち" : run.awaiting_answer ? "回答待ち" : statuses[run.status] || "処理中"}`, "run-status");
        status.dataset.tone = tone;
        state.append(status);
        const current = node("td", describe(run), "run-current");
        if (run.status === "junie_running" && run.context_capsules && !run.queued) {
          const progress = node("progress");
          progress.max = run.context_capsules;
          progress.value = run.completed_capsules || 0;
          progress.setAttribute("aria-label", `完了したカプセル ${run.completed_capsules || 0} / ${run.context_capsules}`);
          current.append(progress);
        }
        const time = node("td", elapsed(run), "run-time");
        const actions = node("td");
        const buttons = node("div", undefined, "run-actions");
        // 表示中のRunに対する操作をボタンへ結び付ける。
        function button(label, action, tab, subtle = false) {
          const item = node("button", label, `button${subtle ? " run-text-action" : ""}`);
          item.type = "button";
          item.dataset.action = action + (tab || "");
          item.setAttribute("aria-label", `${run.unit_id}：${label}`);
          item.addEventListener("click", () => act(action, run.run_id, tab));
          buttons.append(item);
        }
        if (run.queued) {
          if (preparationChat) button("Junie", "open-run", "editor");
          button("待機を取消", "cancel-queued", null, true);
        } else {
          button(tone === "review" ? "進捗で確認" : "進捗", "open-run", "progress");
          if (run.worktree_path || preparationChat) button(run.worktree_path ? "エディタ" : "Junie", "open-run", "editor");
          if (!run.ready && run.cancellable) button(run.stop_requested ? "停止中" : "停止", "stop", null, true);
          if (run.ready && run.resumable) button(tone === "done" ? "再検証" : "再開", "resume-run", null, true);
          if (preparationChat && run.ready && run.build_resumable) button("ビルドのみ", "resume-build", null, true);
          if (preparationChat && run.ready && run.worktree_path) button("再開方法…", "resume-stage", null, true);
          if (run.ready) button("削除", "hide-run");
        }
        actions.append(buttons);
        row.append(name, state, current, time, actions);
        return row;
      }));
      if (focusId && focusAction) [...rows.querySelectorAll("button")].find(item => item.closest("tr").dataset.runId === focusId && item.dataset.action === focusAction)?.focus();
    }
    find("#runs-empty").hidden = !!visible.length;
    find("#runs-empty").textContent = data.runs.length ? "条件に一致する実行はありません。" : "まだ実行がありません。移行単位を選んで準備または移行を始めてください。";
    sync();
  }
  // 実行一覧の最新の要求だけを反映する。
  async function refresh() {
    if (loading) return;
    loading = true;
    try {
      data = await readRuns();
      render();
      if (feedback.dataset.readError) { feedback.textContent = ""; delete feedback.dataset.readError; }
    } catch (error) {
      feedback.dataset.readError = "true";
      feedback.textContent = `実行一覧を読み込めませんでした。接続を確認してください。\n${error.message}`;
    } finally { loading = false; }
  }
  // 実行操作を送り、選択中Runと一覧を結果に合わせて更新する。
  async function act(action, runId, tab) {
    if (pending || !canAct()) return;
    const run = data.runs.find(item => item.run_id === runId);
    if (action === "hide-run" && !window.confirm(
      `${run.unit_id}\nRun ID: ${runId}\n\nこのRunを実行一覧から削除しますか？ 作業ツリーとログは保存されます。`
    )) return;
    pending = true;
    feedback.textContent = "";
    sync();
    try {
      await execute(action, {run_id: runId, operation_id: run?.operation_id, revision: run?.revision, tab});
      if (action === "open-run") { selectedRun = runId; goTab(tab === "editor" ? "#tab-editor" : "#tab-progress"); }
      await refresh();
      if (action === "hide-run") {
        feedback.textContent = "実行一覧から削除しました。";
        search.focus();
      }
    } catch (error) { feedback.textContent = error.message; }
    finally { pending = false; sync(); }
  }
  dryRunButton.addEventListener("click", async () => {
    if (!dryRunId || dryRunCancelling) return;
    const requestId = dryRunId;
    dryRunCancelling = true;
    sync();
    try {
      await execute("dry-run-cancel", {request_id: requestId});
    } catch (error) {
      if (dryRunId !== requestId) return;
      feedback.textContent = error.message;
      dryRunCancelling = false;
      sync();
    }
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (pending || !canAct() || !selectedUnits.size) return;
    const action = event.submitter.value;
    pending = true;
    try {
      if (action === "dry-run") {
        if (selectedUnits.size !== 1) {
          feedback.textContent = "設定を検証する移行単位を1つ選んでください。";
          return;
        }
        feedback.textContent = "実行せずに設定を検証しています。";
        dryRunId = crypto.randomUUID();
        sync();
        const result = await execute("dry-run", {unit_id: [...selectedUnits][0], request_id: dryRunId});
        if (result.cancelled) {
          feedback.textContent = "設定の検証を中止しました。";
          return;
        }
        const states = {confirmed: "確認", unchecked: "未確認", error: "不備", skipped: "対象外"};
        feedback.textContent = [
          result.configuration_ok ? "確認範囲に設定上の不備はありません。実移行は未実行です。" : "設定を確認してください。",
          `移行単位: ${result.unit_id}／基準: ${result.commit ?? result.base_ref}`,
          ...result.checks.map(check => `[${states[check.state]}] ${check.phase}: ${check.detail}`
            + (check.command ? `
  ${check.command}
  cwd: ${check.cwd}` : "")
            + (check.path ? `
  ${check.path}` : "")),
        ].join("\n");
        return;
      }
      sync();
      await execute(action, {unit_ids: [...selectedUnits]});
      selectedUnits.clear();
      optionKey = undefined;
      find("#unit-picker").open = false;
      feedback.textContent = `実行待ちに追加しました。最大${data.parallel_limit}件ずつ処理します。`;
      await refresh();
    } catch (error) { feedback.textContent = error.message; }
    finally {
      dryRunId = undefined;
      dryRunCancelling = false;
      pending = false;
      sync();
    }
  });
  search.addEventListener("input", render);
  workspaceTabs.addEventListener("harnesstab", refresh);
  setInterval(() => {
    if (!document.hidden) refresh();
    for (const row of rows.children) {
      const run = data.runs.find(item => item.run_id === row.dataset.runId);
      if (run) row.querySelector(".run-time").textContent = elapsed(run);
    }
  }, 1500);
  refresh();
  return {refresh, sync, get busy() { return data.busy; },
    setSelected(runId) { selectedRun = runId || ""; render(); }};
}
