"use strict";

(() => {
  const find = selector => document.querySelector(selector);
  const tab = find("#tab-editor");
  if (!tab) return;
  const demo = window.harnessEditorDemo;
  const request = demo ? demo.request : workspaceRequest;
  const progress = demo ? () => demo.progress : () => snapshot;
  const assetPath = demo ? "./src/migration_harness/monaco/" : "monaco/";
  const layout = find("#editor-layout");
  const splitter = layout.querySelector(".editor-splitter");
  const editorPane = find("#editor-pane");
  const targetButtons = find("#editor-target-buttons");
  const phpPicker = find("#editor-php-source");
  const phpButton = find("#editor-php-toggle");
  const notice = find("#editor-update");
  const save = find("#editor-save");
  const codeHost = find("#editor-code");
  const diffHost = find("#editor-diff");
  const phpHost = find("#editor-php");
  const diffButton = find("#editor-diff-toggle");
  const form = find("#editor-prompt-form");
  const prompt = find("#editor-prompt");
  const sendButton = find("#editor-send");
  const cancelEdit = find("#editor-cancel-prompt");
  const promptList = find("#editor-prompts");
  const documents = new Map();
  let state;
  let current;
  let language = "target";
  let phpSource;
  let phpEditor;
  let phpRequest = 0;
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

  function splitBounds() {
    const style = getComputedStyle(layout);
    const width = layout.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const min = parseFloat(style.getPropertyValue("--editor-min-width"));
    const max = width - splitter.offsetWidth - parseFloat(style.getPropertyValue("--junie-min-width"));
    return {width, min, max};
  }

  function resizeEditor(width) {
    const bounds = splitBounds();
    const clamped = Math.max(bounds.min, Math.min(bounds.max, width));
    layout.style.setProperty("--editor-width", `${clamped / bounds.width * 100}%`);
  }

  const splitObserver = new ResizeObserver(() => {
    if (!splitter.offsetWidth) return;
    const {width, min, max} = splitBounds();
    splitter.setAttribute("aria-valuemin", String(Math.round(min / width * 100)));
    splitter.setAttribute("aria-valuemax", String(Math.round(max / width * 100)));
    splitter.setAttribute("aria-valuenow", String(Math.round(editorPane.offsetWidth / width * 100)));
  });
  splitObserver.observe(layout);
  splitObserver.observe(editorPane);

  let drag;
  splitter.addEventListener("pointerdown", event => {
    if (event.button !== 0 || !event.isPrimary) return;
    event.preventDefault();
    splitter.focus();
    splitter.setPointerCapture(event.pointerId);
    splitter.classList.add("is-dragging");
    drag = {x: event.clientX, width: editorPane.getBoundingClientRect().width};
  });
  splitter.addEventListener("pointermove", event => {
    if (drag && splitter.hasPointerCapture(event.pointerId)) {
      resizeEditor(drag.width + event.clientX - drag.x);
    }
  });
  splitter.addEventListener("lostpointercapture", () => {
    drag = undefined;
    splitter.classList.remove("is-dragging");
  });
  splitter.addEventListener("keydown", event => {
    const width = editorPane.getBoundingClientRect().width;
    const {min, max} = splitBounds();
    const sizes = {ArrowLeft: width - 24, ArrowRight: width + 24, Home: min, End: max};
    if (!Object.hasOwn(sizes, event.key)) return;
    event.preventDefault();
    resizeEditor(sizes[event.key]);
  });

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

  const progressBadge = find("#run-state");
  const editorBadge = find("#editor-junie-status");
  const progressReview = find("#toggle");
  function syncPhaseBadge() {
    setText("#editor-junie-phase", find("#state-label").textContent);
    editorBadge.dataset.state = progressBadge.dataset.state;
    editorBadge.classList.toggle("is-running", progressBadge.classList.contains("is-running"));
    const canReview = progressReview.getAttribute("aria-controls") === "review-dialog"
      && !progressReview.hidden && !progressReview.disabled;
    editorBadge.disabled = !canReview;
    if (canReview) editorBadge.setAttribute("aria-haspopup", "dialog");
    else editorBadge.removeAttribute("aria-haspopup");
  }
  const phaseObserver = new MutationObserver(syncPhaseBadge);
  phaseObserver.observe(progressBadge, {
    attributes: true, attributeFilter: ["class", "data-state"], subtree: true, childList: true, characterData: true,
  });
  phaseObserver.observe(progressReview, {attributeFilter: ["aria-controls", "disabled", "hidden"]});
  editorBadge.addEventListener("click", () => progressReview.click());
  syncPhaseBadge();

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

  function showEditor(open) {
    diffOpen = open;
    const showingPhp = language === "php";
    for (const button of targetButtons.children) {
      button.setAttribute("aria-pressed", String(!showingPhp && button.dataset.target === state.target));
    }
    phpButton.setAttribute("aria-pressed", String(showingPhp));
    diffButton.setAttribute("aria-pressed", String(open && !showingPhp));
    phpPicker.hidden = !showingPhp || (phpSource?.sources.length ?? 0) < 2;
    codeHost.hidden = showingPhp || open;
    diffHost.hidden = showingPhp || !open;
    phpHost.hidden = !showingPhp;
    if (showingPhp) {
      phpEditor?.layout();
      return;
    }
    if (!editor || !current) return;
    if (open) {
      if (diff.getModel()?.modified !== current.model) diff.setModel({original: latest, modified: current.model});
      diff.layout();
    } else {
      if (editor.getModel() !== current.model) editor.setModel(current.model);
      editor.layout();
    }
  }

  function showTargets(targets) {
    const shown = [...targetButtons.children].map(button => button.dataset.target);
    if (shown.length === targets.length && targets.every((path, index) => path === shown[index])) return;
    const extensions = targets.map(path => path.toLowerCase().match(/(?:\.cshtml)?\.cs$|\.cshtml$/)[0]);
    targetButtons.replaceChildren(...targets.map((path, index) => {
      const extension = extensions[index];
      const repeated = extensions.indexOf(extension) !== extensions.lastIndexOf(extension);
      const number = extensions.slice(0, index + 1).filter(value => value === extension).length;
      const button = Object.assign(document.createElement("button"), {
        className: "button", type: "button", title: path,
        textContent: repeated ? `${extension} ${number}` : extension,
      });
      button.dataset.target = path;
      if (repeated) button.setAttribute("aria-label", `${button.textContent} ${path}`);
      return button;
    }));
  }

  layout.querySelector(".editor-source").addEventListener("keydown", event => {
    if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey || event.key.toLowerCase() !== "f") return;
    let activeEditor = language === "php" ? phpEditor : editor;
    if (diffHost.contains(event.target) && diff) {
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
    const hasDiff = current && !state.read_error && readText(current.model) !== state.content;
    const restoreFocus = !hasDiff && (document.activeElement === diffButton || diffHost.contains(document.activeElement));
    diffButton.disabled = !hasDiff || language === "php";
    if (!hasDiff && diffOpen) showEditor(false);
    if (restoreFocus) editor.focus();
    if (language === "php") {
      save.disabled = notice.hidden = true;
      save.textContent = "保存";
      setText("#editor-save-status", "移行元 PHP（読み取り専用）");
      setText("#editor-save-timing", phpSource?.loading ? "読み込み中"
        : phpSource?.error || phpSource?.source || "この実行に移行元 PHP がありません。");
      return;
    }
    if (!current) return;
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
      : conflict ? "競合の解消待ち"
      : state.edit ? "保存済み・ファイルへ反映待ち"
      : !state.exists ? "移行先ファイルは未作成です" : "ファイルに反映済み");
    setText("#editor-save-timing", !state.can_save ? "保存先を読み込めません。接続とエラー内容を確認してください。"
      : state.save_timing === "repair_restart" ? "編集を保存して追加指示を送ると、修正へ反映し、対応後に再ビルドします。"
      : state.save_timing === "checkpoint" ? "編集は保存できます。現在の作業区間が終わるとファイルに反映します。"
      : state.save_timing === "resume" ? "編集は保存できます。同じ実行を再開するとファイルに反映します。" : "");
  }

  async function loadPhp(source) {
    const version = ++phpRequest;
    const query = new URLSearchParams({run_id: state.run_id});
    if (source) query.set("source", source);
    phpSource = {...phpSource, sources: phpSource?.sources ?? [], loading: true};
    phpEditor?.setValue("");
    showSaveState();
    try {
      const data = await request("editor-php?" + query);
      if (version !== phpRequest) return;
      phpSource = data;
      if (!phpEditor) {
        phpEditor = window.monaco.editor.create(phpHost, {
          language: "php", readOnly: true, automaticLayout: true, minimap: {enabled: false},
          ariaLabel: "移行元 PHP（読み取り専用）",
        });
      }
      phpEditor.setValue(data.content);
      phpPicker.replaceChildren(...data.sources.map(name => new Option(name, name)));
      phpPicker.value = data.source ?? "";
    } catch (error) {
      if (version !== phpRequest) return;
      phpSource = {...phpSource, loading: false, source, error: error.message};
      phpEditor?.setValue("");
    }
    showEditor(diffOpen);
    showSaveState();
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

  function updateSendButton() {
    sendButton.disabled = !state?.can_prompt || sending || !prompt.value.trim();
  }

  function promptMode() {
    const active = Boolean(editPrompt);
    prompt.setAttribute("aria-label", active ? "送信待ちの指示を編集" : "Junie に指示");
    sendButton.querySelector("[data-send-icon]").hidden = active;
    sendButton.querySelector("[data-send-label]").hidden = !active;
    if (active) sendButton.removeAttribute("aria-label");
    else sendButton.setAttribute("aria-label", "送信");
    cancelEdit.disabled = !active || sending;
    updateSendButton();
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
      const label = {queued: "送信待ち", steering: "Junie に送信・処理中", applying: "反映確認中", sending: "処理中", sent: "送信済み"}[message.status];
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
    const question = state.operator_question;
    setText("#editor-operator-question", question ? "Junie からの確認\n" + question
      + "\n回答を Junie に送ってから、進捗タブで実行を再開してください。" : "");
    find("#editor-operator-question").hidden = !question;
    const showAgent = state.source === "agent-workspace";
    const snapshot = progress();
    const activity = find("#editor-junie-activity");
    activity.textContent = showAgent && !snapshot.ready ? snapshot.activity?.message || "" : "";
    activity.hidden = !activity.textContent;
    find("#editor-prompt-timing").textContent = !state.can_prompt ? "現在の工程では指示を登録できません。"
      : state.prompt_timing === "repair_restart" ? "保存した編集を反映して Junie に指示を渡し直し、対応後に再ビルドします。"
      : state.prompt_timing === "immediate" ? "実行中の Junie にすぐ渡します。反映の完了までお待ちください。"
      : state.prompt_timing === "resume" ? "同じ実行を再開したときに Junie に渡します。"
      : "現在の作業区間が終わると Junie に渡します。";
    updateSendButton();
    prompt.disabled = !state.can_prompt || sending;
    cancelEdit.disabled = !editPrompt || sending;
  }

  async function display(data, expectedRevision = revision) {
    if (expectedRevision !== revision) return;
    const previous = state;
    if (previous?.run_id !== data.run_id) {
      selectedTarget = undefined;
      phpSource = undefined;
      phpRequest++;
      phpEditor?.setValue("");
      language = data.targets.length ? "target" : "php";
      editPrompt = undefined;
      prompt.value = "";
      promptMode();
    }
    state = data;
    if (!data.run_id) {
      current = undefined;
      layout.hidden = true;
      find("#editor-empty").hidden = false;
      find("#editor-empty").textContent = "実行タブで移行を始めるか、保存済みの実行を開いてください。";
      return;
    }
    if (!editor) setText("#editor-empty", "エディタを読み込んでいます。");
    const monaco = await loadMonaco();
    if (expectedRevision !== revision || state !== data) return;
    layout.hidden = false;
    find("#editor-empty").hidden = true;
    if (!editor) {
      const theme = () => monaco.editor.setTheme(document.documentElement.dataset.theme === "dark" ? "vs-dark" : "vs");
      const options = {automaticLayout: true, minimap: {enabled: false}, bracketPairColorization: {enabled: true}};
      editor = monaco.editor.create(codeHost, {...options, model: null, ariaLabel: "移行先の下書き"});
      diff = monaco.editor.createDiffEditor(diffHost, {...options, renderSideBySide: false, ignoreTrimWhitespace: false});
      latest = monaco.editor.createModel("", "csharp");
      editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, saveDraft);
      diff.getModifiedEditor().addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, saveDraft);
      new MutationObserver(theme).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
      theme();
    }
    showTargets(data.targets);
    if (!data.targets.length) {
      current = undefined;
      language = "php";
      showEditor(false);
      updateChat();
      if (!phpSource) loadPhp();
      return;
    }
    const fileLanguage = data.target.toLowerCase().endsWith(".cshtml") ? "razor" : "csharp";
    const key = JSON.stringify([data.run_id, data.target]);
    let file = documents.get(key);
    const saved = data.edit?.content ?? (data.read_error ? file?.saved : data.content) ?? "";
    if (!file) {
      const model = monaco.editor.createModel(saved, fileLanguage);
      file = {model, saved, base: data.edit?.base ?? data.content, editId: data.edit?.id ?? null};
      documents.set(key, file);
      model.onDidChangeContent(() => { if (file === current) showSaveState(); });
    } else if (!data.read_error && readText(file.model) === file.saved) {
      file.saved = saved;
      file.base = data.edit?.base ?? data.content;
      file.editId = data.edit?.id ?? null;
      if (readText(file.model) !== saved) file.model.setValue(saved);
    }
    selectedTarget = data.target;
    if (latest.getLanguageId() !== fileLanguage) monaco.editor.setModelLanguage(latest, fileLanguage);
    if (readText(latest) !== data.content) latest.setValue(data.content);
    const different = current !== file;
    current = file;
    showEditor(!different && diffOpen);
    showSaveState();
    updateChat();
    if (language === "php" && !phpSource) loadPhp();
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
          showEditor(diffOpen);
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
    showEditor(!diffOpen);
    showSaveState();
  });
  targetButtons.addEventListener("click", event => {
    const target = event.target.dataset.target;
    if (!target) return;
    language = "target";
    if (selectedTarget !== target) {
      selectedTarget = target;
      revision++;
      report("#editor-error");
    }
    if (state.target === target) {
      showEditor(diffOpen);
      showSaveState();
    } else {
      refresh();
    }
  });
  phpButton.addEventListener("click", () => {
    language = "php";
    showEditor(diffOpen);
    if (!phpSource || phpSource.error) loadPhp(phpSource?.source);
    else showSaveState();
  });
  phpPicker.addEventListener("change", () => loadPhp(phpPicker.value));
  save.addEventListener("click", saveDraft);
  cancelEdit.addEventListener("click", finishPromptEdit);
  prompt.addEventListener("input", updateSendButton);
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
