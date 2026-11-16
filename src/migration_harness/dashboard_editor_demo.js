"use strict";

window.harnessEditorDemo = (() => {
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
  const runId = "run-2026-11-14-001";
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

  async function request(path, action) {
    if (path.startsWith("editor-php?")) return {sources: [phpPath], source: phpPath, content: php};
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
    return structuredClone({...demo, target, content: file.content, edit: file.edit});
  }

  function update() {
    for (const file of files.values()) file.content = file.content.replace(file.before, file.after);
    updated = true;
    progress.activity.message = "Junie が注文一覧の取得処理と見出しを更新しました。";
  }

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

  let promptTimer;
  function renderPrompts() {
    promptDialog.render({timing: demo.can_prompt ? "checkpoint" : null, prompts: demo.prompts});
  }
  const promptDialog = bindProgressPromptDialog({
    open() {
      renderPrompts();
      clearInterval(promptTimer);
      promptTimer = setInterval(renderPrompts, 250);
    },
    async submit(text) {
      await request("editor", {kind: "prompt", run_id: runId, text});
      promptDialog.input.value = "";
      renderPrompts();
    },
    inputChanged: renderPrompts,
  });
  promptDialog.button.hidden = false;
  promptDialog.dialog.addEventListener("close", () => clearInterval(promptTimer));

  return {progress, request};
})();
