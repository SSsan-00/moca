"use strict";

window.harnessEditorDemo = (() => {
  // 編集と会話の模擬ファイルをRunごとに作る。
  function createRun(run) {
  const runId = run.run_id;
  const codeBehind = "Pages/Orders/Index.cshtml.cs";
  const razorPage = "Pages/Orders/Index.cshtml";
  const phpPath = "php/orders.php";
  const php = [
    "<?php",
    "",
    "function format_date(DateTimeInterface $date): string",
    "{",
    "    return $date->format('d/m/Y');",
    "}",
    "",
    "$orders = $repository->list();",
    "foreach ($orders as $order) {",
    "    echo format_date($order->createdAt);",
    "}",
  ].join("\n");
  const oldCall = "await _orders.ListAsync();";
  const files = new Map([[codeBehind, {
    before: oldCall,
    after: "await _orders.ListRecentAsync();",
    content: [
      "public async Task OnGetAsync()",
      "{",
      "    var orders = " + oldCall,
      "    Orders = orders.Select(order => new OrderRow(",
      "        order.Id,",
      "        FormatDate(order.CreatedAt)",
      "    )).ToList();",
      "}",
      "",
      "private static string FormatDate(DateTime date)",
      '    => date.ToString("dd/MM/yyyy");',
      "",
    ].join("\n"),
    edit: null,
  }], [razorPage, {
    before: "<h1>注文一覧</h1>",
    after: "<h1>最近の注文</h1>",
    content: [
      "@page",
      "@model IndexModel",
      "",
      "<h1>注文一覧</h1>",
      "<ul>",
      "@foreach (var order in Model.Orders)",
      "{",
      "    <li>@order.Id</li>",
      "}",
      "</ul>",
      "",
    ].join("\n"),
    edit: null,
  }]]);
  const demo = {
    run_id: runId,
    targets: [...files.keys()],
    exists: true,
    source: "agent-workspace",
    can_save: true,
    queued: true,
    can_prompt: true,
    junie_model: run.junie_model ?? null, junie_effort: run.junie_effort ?? null, junie_model_revision: null,
    prompts: [],
    capsule: 3,
    status: "junie_running",
  };
  const progress = {
    run_id: runId, status: demo.status, context_capsules: 8,
    activity: {message: "サンプルの注文履歴を移行中です。"},
  };
  let nextId = 1;
  let boundary;
  let updated = false;

  // 本番と同じ画面操作をデモRunのメモリ上の状態で再現する。
  async function request(path, action) {
    if (path === "roslyn-diagnostics") return {status: "unavailable", reason: "demo", findings: [], razor: [],
      target: action.target, version: action.version};
    if (path === "run-model") return {run_id: runId, can_select: true, model: demo.junie_model, effort: demo.junie_effort, revision: demo.junie_model_revision};
    if (action?.kind === "model") {
      if (action.model_revision !== demo.junie_model_revision) throw new Error("モデル設定が別の画面で変更されました。");
      Object.assign(demo, {junie_model: action.model || null, junie_effort: action.effort || null,
        junie_model_revision: String(nextId++)});
      return structuredClone(demo);
    }
    if (path.startsWith("editor-php?")) return {run_id: runId, sources: [phpPath], source: phpPath, content: php};
    const target = action?.target ?? new URLSearchParams(path.split("?")[1]).get("target") ?? codeBehind;
    const file = files.get(target);
    if (!file) throw new Error("設定された移行先ファイルを選んでください。");
    if (action?.kind === "save") {
      if (file.edit?.status === "conflict" && (!action.resolve || action.base !== file.content)) {
        throw new Error("差分を確認してから競合を解消してください。");
      }
      if (demo.queued) {
        file.edit = {
          id: String(nextId++), base: action.base, content: action.content, status: "queued",
        };
      } else {
        file.content = action.content;
      }
    } else if (action?.kind === "prompt") {
      if (action.id) {
        const prompt = demo.prompts.find(item => item.id === action.id && item.status === "queued");
        if (!prompt) throw new Error("この指示はすでに送信されています。");
        prompt.text = action.text;
      } else {
        demo.prompts.push({id: String(nextId++), text: action.text, status: "queued"});
      }
    }
    if (action && demo.queued && !boundary) {
      if (!updated) setTimeout(update, 850);
      boundary = setTimeout(() => {
        boundary = undefined;
        advance();
      }, 3800);
    }
    return structuredClone({...demo, save_timing: demo.queued ? "checkpoint" : "immediate",
      target, content: file.content, edit: file.edit});
  }

  // デモ対象の初期本文を模擬移行後の内容へ進める。
  function update() {
    for (const file of files.values()) file.content = file.content.replace(file.before, file.after);
    updated = true;
    progress.activity.message = "Junie が注文一覧の取得処理と見出しを更新しました。";
  }

  // 選択中のデモRunの状態に応じて、追加指示の進行を模擬する。
  function advance() {
    const merged = new Map();
    for (const file of files.values()) {
      const {edit, before, after, content} = file;
      if (!edit) continue;
      const unchanged = edit.base === content || edit.content === content;
      const separate = edit.base.includes(before) && edit.base.replace(before, after) === content
        && edit.content.includes(before);
      if (edit.status === "conflict" || (!unchanged && !separate)) {
        edit.status = "conflict";
        demo.status = "editor_checkpoint";
        progress.activity.message = "同じ箇所の編集を確認してください。";
        return;
      }
      merged.set(file, unchanged ? edit.content : edit.content.replace(before, after));
    }
    for (const [file, content] of merged) {
      file.content = content;
      file.edit = null;
    }
    const sent = demo.prompts.filter(prompt => prompt.status === "queued");
    for (const prompt of sent) prompt.status = "sending";
    if (sent.length) setTimeout(() => {
      for (const prompt of sent) prompt.status = "sent";
    }, 900);
    demo.capsule++;
    demo.status = "junie_running";
    progress.activity.message = sent.length
      ? "登録した指示を Junie に渡しました。"
      : "次の移行区間に進みました。";
    if (demo.capsule > progress.context_capsules) {
      Object.assign(demo, {capsule: progress.context_capsules, queued: false, can_prompt: false,
        source: "worktree", status: "junie_finished"});
    }
  }

  return {progress, request, update(run) {
    Object.assign(demo, {status: run.status, queued: !run.ready, can_save: !!run.worktree_path,
      can_prompt: !!run.worktree_path && !run.queued, capsule: run.current_capsule});
    Object.assign(progress, {status: run.status, context_capsules: run.context_capsules});
  }};
  }
  const sessions = new Map();
  let active;
  // Runごとのデモ編集と会話を再利用し、表示を切り替える。
  function select(run) {
    if (!run) {
      active = undefined;
      return;
    }
    if (!sessions.has(run.run_id)) sessions.set(run.run_id, createRun(run));
    active = sessions.get(run.run_id);
    active.update(run);
  }
  const idle = {status: "idle"};
  return {
    get progress() { return active?.progress ?? idle; },
    request: (...args) => active?.request(...args) ?? {targets: []},
    select,
  };
})();
