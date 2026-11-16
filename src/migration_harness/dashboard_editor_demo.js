"use strict";

window.harnessEditorDemo = (() => {
  const target = "Pages/Orders/Index.cshtml.cs";
  const runId = "run-2026-11-14-001";
  const oldCall = "await _orders.ListAsync();";
  const newCall = "await _orders.ListRecentAsync();";
  const demo = {
    run_id: runId,
    targets: [target],
    target,
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

  async function request(_path, action) {
    if (action?.kind === "save") {
      if (demo.edit?.status === "conflict" && (!action.resolve || action.base !== demo.content)) {
        throw new Error("差分を確認してから競合を解消してください。");
      }
      if (demo.queued) {
        demo.edit = {
          id: String(nextId++), base: action.base, content: action.content, status: "queued",
        };
      } else {
        demo.content = action.content;
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
    return structuredClone(demo);
  }

  function update() {
    demo.content = demo.content.replace(oldCall, newCall);
    updated = true;
    progress.activity.message = "Junie が注文一覧の取得処理を更新しました。";
  }

  function advance() {
    const edit = demo.edit;
    if (edit) {
      const unchanged = edit.base === demo.content || edit.content === demo.content;
      const separate = edit.base.includes(oldCall) && edit.base.replace(oldCall, newCall) === demo.content
        && edit.content.includes(oldCall);
      if (!unchanged && !separate) {
        edit.status = "conflict";
        demo.status = "editor_checkpoint";
        progress.activity.message = "同じ箇所の編集を確認してください。";
        return;
      }
      demo.content = unchanged ? edit.content : edit.content.replace(oldCall, newCall);
      demo.edit = null;
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
