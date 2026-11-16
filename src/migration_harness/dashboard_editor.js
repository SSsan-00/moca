"use strict";

(() => {
  const find = selector => document.querySelector(selector);
  const tab = find("#tab-editor");
  if (!tab) return;
  const demo = window.harnessEditorDemo;
  const request = demo ? demo.request : workspaceRequest;
  const progress = demo ? () => demo.progress : () => snapshot;
  const assetPath = demo ? "./src/migration_harness/monaco/" : "monaco/";
  const phaseNames = demo ? {
    junie_running: "Junie で移行中", editor_checkpoint: "編集の確認待ち",
  } : statusNames;
  const layout = find("#editor-layout");
  const targetPicker = find("#editor-target");
  const notice = find("#editor-update");
  const save = find("#editor-save");
  const codeHost = find("#editor-code");
  const diffHost = find("#editor-diff");
  const diffButton = find("#editor-diff-toggle");
  const form = find("#editor-prompt-form");
  const prompt = find("#editor-prompt");
  const sendButton = find("#editor-send");
  const cancelEdit = find("#editor-cancel-prompt");
  const promptList = find("#editor-prompts");
  const documents = new Map();
  let state;
  let current;
  let diffOpen = false;
  let editor;
  let diff;
  let latest;
  let monacoLoading;
  let shownPrompts;
  let selectedTarget;
  let editPrompt;
  let newPromptDraft = "";
  let refreshing = false;
  let saving = false;
  let sending = false;
  let revision = 0;

  function loadMonaco() {
    if (monacoLoading) return monacoLoading;
    const loadScript = src => new Promise((resolve, reject) => {
      const script = Object.assign(document.createElement("script"), {src});
      script.addEventListener("load", resolve, {once: true});
      script.addEventListener("error", () => {
        script.remove();
        reject(new Error("Monaco Editor を読み込めませんでした。"));
      }, {once: true});
      document.head.append(script);
    });
    // 言語ファイルは AMD モジュールではなく、初期化前に辞書を登録する。
    const stylesheet = Object.assign(document.createElement("link"), {
      rel: "stylesheet", href: assetPath + "editor.css",
    });
    document.head.append(stylesheet);
    monacoLoading = loadScript(assetPath + "worker.js")
      .then(() => {
        const worker = new Blob([window.harnessMonacoWorker], {type: "text/javascript"});
        const url = URL.createObjectURL(worker);
        window.MonacoEnvironment = {getWorker: () => new Worker(url)};
        return loadScript(assetPath + "editor.js");
      }).then(() => window.monaco).catch(error => {
        stylesheet.remove();
        monacoLoading = undefined;
        throw error;
      });
    return monacoLoading;
  }

  function report(selector, error) {
    const target = find(selector);
    target.hidden = !error;
    target.textContent = error ? error.message || String(error) : "";
  }

  function setText(selector, text) {
    const target = find(selector);
    if (target.textContent !== text) target.textContent = text;
  }

  function readText(model) {
    return model.getValue(window.monaco.editor.EndOfLinePreference.LF);
  }

  function dirty() {
    return current && readText(current.model) !== current.saved;
  }

  addEventListener("beforeunload", event => {
    if ([...documents.values()].some(file => readText(file.model) !== file.saved)) {
      event.preventDefault();
    }
  });

  function showDiff(open) {
    diffOpen = open;
    diffButton.setAttribute("aria-pressed", String(open));
    codeHost.hidden = open;
    diffHost.hidden = !open;
    if (!editor || !current) return;
    if (open) {
      if (diff.getModel()?.modified !== current.model) diff.setModel({original: latest, modified: current.model});
      diff.layout();
    } else {
      if (editor.getModel() !== current.model) editor.setModel(current.model);
      editor.layout();
    }
  }

  layout.querySelector(".editor-source").addEventListener("keydown", event => {
    if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey || event.key.toLowerCase() !== "f") return;
    let activeEditor = editor;
    if (diffHost.contains(event.target)) {
      const original = diff.getOriginalEditor();
      activeEditor = original.getDomNode()?.contains(event.target) ? original : diff.getModifiedEditor();
    }
    const controller = activeEditor?.getContribution("editor.contrib.findController");
    if (!controller) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.repeat) return;
    if (controller.getState().isRevealed) controller.closeFindWidget();
    else activeEditor.getAction("actions.find").run();
  }, {capture: true});

  function showSaveState() {
    if (!current) return;
    const hasDiff = !state.read_error && readText(current.model) !== state.content;
    const restoreFocus = !hasDiff && (document.activeElement === diffButton || diffHost.contains(document.activeElement));
    diffButton.disabled = !hasDiff;
    if (!hasDiff && diffOpen) showDiff(false);
    if (restoreFocus) editor.focus();
    const conflict = state.edit?.status === "conflict";
    const updated = !state.read_error && state.content !== current.base;
    notice.hidden = !conflict && !updated;
    setText("#editor-update", conflict
      ? "同じ箇所が変更されました。差分を確認し、下書きに合流結果を書いて保存してから移行を再開してください。"
      : (state.source === "agent-workspace" ? "Junie の更新あり。" : "ファイルの更新あり。")
        + "編集中の下書きは保持されています。");
    save.disabled = !state.can_save || saving || (!dirty() && !conflict);
    save.textContent = conflict ? "競合を解消して保存" : "保存";
    setText("#editor-save-status", state.read_error ? "ファイルを表示できません"
      : dirty() ? "未保存の変更あり"
      : !state.exists ? "移行先ファイルの作成待ち"
      : conflict ? "競合の解消待ち"
      : state.edit ? "保存済み・ファイルへ反映待ち" : "ファイルに反映済み");
    setText("#editor-save-timing", !state.can_save ? "保存できる工程まで、入力内容は下書きに残ります。" : state.queued
      ? "Junie が現在の作業区間を終えたらファイルに反映します。" : "");
  }

  async function saveDraft() {
    if (save.disabled || !current) return;
    const resolving = state.edit?.status === "conflict";
    if (resolving && !confirm("現在の下書きを競合の解消結果として保存します。よろしいですか？")) return;
    const file = current;
    const content = readText(file.model);
    const body = {
      kind: "save", run_id: state.run_id, target: state.target,
      base: resolving ? state.content : file.base, content, edit_id: file.editId, resolve: resolving,
    };
    saving = true;
    const version = ++revision;
    showSaveState();
    report("#editor-error");
    try {
      const data = await request("editor", body);
      file.saved = content;
      file.editId = data.edit?.id ?? null;
      file.base = data.edit?.base ?? data.content;
      if (file === current) await display(data, version);
    } catch (error) {
      report("#editor-error", error);
    } finally {
      saving = false;
      showSaveState();
    }
  }

  function promptMode() {
    const active = Boolean(editPrompt);
    form.querySelector("label").textContent = active ? "送信待ちの指示を編集" : "Junie に指示";
    sendButton.querySelector("[data-send-icon]").hidden = active;
    sendButton.querySelector("[data-send-label]").hidden = !active;
    if (active) sendButton.removeAttribute("aria-label");
    else sendButton.setAttribute("aria-label", "送信");
    cancelEdit.disabled = !active || sending;
  }

  function finishPromptEdit() {
    editPrompt = undefined;
    prompt.value = newPromptDraft;
    promptMode();
    prompt.focus();
  }

  function renderPrompts() {
    const key = JSON.stringify([state.prompts, state.can_prompt]);
    if (key === shownPrompts) return;
    shownPrompts = key;
    promptList.replaceChildren(...state.prompts.map(message => {
      const item = document.createElement("li");
      const author = Object.assign(document.createElement("span"), {className: "editor-muted", textContent: "あなた"});
      const text = Object.assign(document.createElement("p"), {textContent: message.text});
      const label = {queued: "送信待ち", sending: "処理中", sent: "送信済み"}[message.status];
      const status = Object.assign(document.createElement("span"), {className: "editor-muted", textContent: label});
      item.append(author, text, status);
      if (message.status === "queued" && state.can_prompt) {
        const button = Object.assign(document.createElement("button"), {className: "button", type: "button", textContent: "編集"});
        button.addEventListener("click", () => {
          if (editPrompt) return;
          editPrompt = message.id;
          newPromptDraft = prompt.value;
          prompt.value = message.text;
          promptMode();
          prompt.focus();
        });
        item.append(button);
      }
      return item;
    }));
  }

  function updateChat() {
    renderPrompts();
    const showAgent = state.source === "agent-workspace";
    const snapshot = progress();
    const status = phaseNames[state.status] || "Junie の作業領域を表示中";
    const capsule = state.capsule && snapshot.context_capsules ? " · 区間 " + state.capsule + " / " + snapshot.context_capsules : "";
    setText("#editor-junie-status", showAgent ? status + capsule : "現在の移行結果を表示しています。");
    const activity = find("#editor-junie-activity");
    activity.textContent = showAgent && !snapshot.ready ? snapshot.activity?.message || "" : "";
    activity.hidden = !activity.textContent;
    find("#editor-prompt-timing").textContent = !state.can_prompt ? "現在の工程では指示を登録できません。"
      : state.prompt_timing === "resume" ? "同じ実行を再開したときに Junie に渡します。"
      : "現在の作業区間が終わると Junie に渡します。";
    sendButton.disabled = !state.can_prompt || sending;
    prompt.disabled = !state.can_prompt || sending;
    cancelEdit.disabled = !editPrompt || sending;
  }

  async function display(data, expectedRevision = revision) {
    if (expectedRevision !== revision) return;
    const previous = state;
    if (previous?.run_id !== data.run_id) {
      selectedTarget = undefined;
      editPrompt = undefined;
      prompt.value = "";
      promptMode();
    }
    state = data;
    if (!data.targets.length) {
      current = undefined;
      layout.hidden = true;
      find("#editor-empty").hidden = false;
      find("#editor-empty").textContent = data.run_id
        ? "この実行には C# の移行先ファイルがありません。"
        : "実行タブで移行を始めるか、保存済みの実行を開いてください。";
      return;
    }
    if (!editor) setText("#editor-empty", "C# エディタを読み込んでいます。");
    const monaco = await loadMonaco();
    if (expectedRevision !== revision || state !== data) return;
    layout.hidden = false;
    find("#editor-empty").hidden = true;
    if (!editor) {
      const theme = () => monaco.editor.setTheme(document.documentElement.dataset.theme === "dark" ? "vs-dark" : "vs");
      const options = {automaticLayout: true, minimap: {enabled: false}, bracketPairColorization: {enabled: true}};
      editor = monaco.editor.create(codeHost, {...options, model: null, ariaLabel: "C# の下書き"});
      diff = monaco.editor.createDiffEditor(diffHost, {...options, renderSideBySide: false, ignoreTrimWhitespace: false});
      latest = monaco.editor.createModel("", "csharp");
      editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, saveDraft);
      diff.getModifiedEditor().addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, saveDraft);
      new MutationObserver(theme).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
      theme();
    }
    const key = JSON.stringify([data.run_id, data.target]);
    let file = documents.get(key);
    const saved = data.edit?.content ?? (data.read_error ? file?.saved : data.content) ?? "";
    if (!file) {
      const model = monaco.editor.createModel(saved, "csharp");
      file = {model, saved, base: data.edit?.base ?? data.content, editId: data.edit?.id ?? null};
      documents.set(key, file);
      model.onDidChangeContent(() => { if (file === current) showSaveState(); });
    } else if (!data.read_error && readText(file.model) === file.saved) {
      file.saved = saved;
      file.base = data.edit?.base ?? data.content;
      file.editId = data.edit?.id ?? null;
      if (readText(file.model) !== saved) file.model.setValue(saved);
    }
    if (JSON.stringify([...targetPicker.options].map(option => option.value)) !== JSON.stringify(data.targets)) {
      targetPicker.replaceChildren(...data.targets.map(path => new Option(path, path)));
    }
    targetPicker.hidden = data.targets.length < 2;
    selectedTarget = targetPicker.value = data.target;
    if (readText(latest) !== data.content) latest.setValue(data.content);
    const different = current !== file;
    current = file;
    showDiff(!different && diffOpen);
    showSaveState();
    updateChat();
    if (data.read_error || previous?.read_error) report("#editor-error", data.read_error);
  }

  async function refresh() {
    if (tab.getAttribute("aria-selected") !== "true" || refreshing || saving || sending) return;
    refreshing = true;
    const version = revision;
    const target = state?.run_id === progress().run_id && selectedTarget;
    try {
      const data = await request(target ? "editor?target=" + encodeURIComponent(target) : "editor");
      await display(data, version);
    } catch (error) {
      if (version === revision) {
        const empty = find("#editor-empty");
        if (layout.hidden) empty.textContent = error.message;
        else {
          state.can_save = state.can_prompt = false;
          showDiff(diffOpen);
          showSaveState();
          updateChat();
          report("#editor-error", error);
        }
      }
    } finally {
      refreshing = false;
    }
  }

  diffButton.addEventListener("click", () => {
    showDiff(!diffOpen);
    showSaveState();
  });
  targetPicker.addEventListener("change", () => {
    selectedTarget = targetPicker.value;
    revision++;
    report("#editor-error");
    refresh();
  });
  save.addEventListener("click", saveDraft);
  cancelEdit.addEventListener("click", finishPromptEdit);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const text = prompt.value.trim();
    if (!text || sending || !state.can_prompt) return;
    sending = true;
    revision++;
    updateChat();
    report("#editor-prompt-error");
    try {
      const data = await request("editor", {kind: "prompt", run_id: state.run_id, id: editPrompt, text});
      state.prompts = data.prompts;
      if (editPrompt) finishPromptEdit();
      else prompt.value = "";
    } catch (error) {
      report("#editor-prompt-error", error);
    } finally {
      sending = false;
      updateChat();
      if (!prompt.disabled) prompt.focus();
    }
  });
  setInterval(refresh, 750);
  workspaceTabs.addEventListener("harnesstab", refresh);
})();
