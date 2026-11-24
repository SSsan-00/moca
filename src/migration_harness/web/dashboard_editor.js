"use strict";

(() => {
  const find = selector => document.querySelector(selector);
  const tab = find("#tab-editor");
  if (!tab) return;
  const demo = window.harnessEditorDemo;
  const request = demo ? demo.request : workspaceRequest;
  const progress = demo ? () => demo.progress : () => snapshot;
  const assetPath = demo ? "./src/migration_harness/web/monaco/" : "monaco/";
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
  const updateFeed = bindJunieChat(find("#editor-chat-feed"), find("#editor-chat-latest"), form, prompt, sendButton);
  const documents = new Map();
  const runDrafts = new Map();
  let switching = false;
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
  let chatRun;
  let selectedTarget;
  let editPrompt;
  let promptEdits = 0;
  let newPromptDraft = "";
  let refreshing;
  let saving = false;
  let sending = false;
  let revision = 0;

  // エディタと会話の領域に必要な幅から切り替え位置を制限する。
  function splitBounds() {
    const style = getComputedStyle(layout);
    const width = layout.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const min = parseFloat(style.getPropertyValue("--editor-min-width"));
    const max = width - splitter.offsetWidth - parseFloat(style.getPropertyValue("--junie-min-width"));
    return {width, min, max};
  }

  // 利用できる幅へ分割位置を収め、操作後の表示を更新する。
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

  // エディタを最初に開くときだけ、配布済みのMonacoを読み込む。
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

  // エラーを文字列として表示し、解消時には行を閉じる。
  function report(selector, error) {
    const target = find(selector);
    target.hidden = !error;
    target.textContent = error ? error.message || String(error) : "";
  }

  // 同じ内容のDOM書き込みを省く。
  function setText(selector, text) {
    const target = find(selector);
    if (target.textContent !== text) target.textContent = text;
  }

  const progressBadge = find("#run-state");
  const editorBadge = find("#editor-junie-status");
  const progressReview = find("#toggle");
  // 進捗タブの工程と確認可能な操作をエディタへ同期する。
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

  // エディタ上の改行を保存要求用に統一する。
  function readText(model) {
    return model.getValue(window.monaco.editor.EndOfLinePreference.LF);
  }

  // サーバーから取得した保存版との差分を判定する。
  function dirty() {
    return current && readText(current.model) !== current.saved;
  }

  addEventListener("beforeunload", event => {
    if ([...documents.values()].some(file => readText(file.model) !== file.saved)) {
      event.preventDefault();
    }
  });

  // 比較を開いたかどうかに応じて、編集・差分の表示とモデルを切り替える。
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

  // 対象が変わった場合にだけファイル選択肢を作り直す。
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

  // 保留・競合・送信状態に応じて保存の可否と案内をそろえる。
  function showSaveState() {
    const hasDiff = current && !state.read_error
      && readText(current.model) !== (state.comparison_content ?? state.content);
    diffButton.textContent = state.comparison_label ?? "差分";
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
    save.disabled = switching || state.run_id !== progress().run_id || !state.can_save || saving || (!dirty() && !conflict);
    save.textContent = conflict ? "競合を解消して保存" : "保存";
    setText("#editor-save-status", state.read_error ? "ファイルを表示できません"
      : dirty() ? "未保存の変更あり"
      : conflict ? "競合の解消待ち"
      : state.edit ? "保存済み・ファイルへ反映待ち"
      : state.stopped_draft ? (state.exists ? "停止時の下書き" : "停止時に対象ファイルが欠損")
      : !state.exists ? "移行先ファイルは未作成です" : "ファイルに反映済み");
    setText("#editor-save-timing", !state.can_save ? "保存先を読み込めません。接続とエラー内容を確認してください。"
      : state.save_timing === "repair_restart" ? "編集を保存して追加指示を送ると、修正へ反映し、対応後に再ビルドします。"
      : state.save_timing === "checkpoint" ? "編集は保存できます。現在の作業区間が終わるとファイルに反映します。"
      : state.save_timing === "resume" ? "編集は保存できます。同じ実行を再開するとファイルに反映します。" : "");
  }

  // 保存済みPHPを取得し、Runまたは対象が切り替わった場合は結果を破棄する。
  async function loadPhp(source) {
    if (switching || !state?.run_id || state.run_id !== progress().run_id) return;
    const version = ++phpRequest;
    const runId = state.run_id;
    const query = new URLSearchParams({run_id: runId});
    if (source) query.set("source", source);
    phpSource = {...phpSource, sources: phpSource?.sources ?? [], loading: true};
    phpEditor?.setValue("");
    showSaveState();
    try {
      const data = await request("editor-php?" + query);
      if (version !== phpRequest || data.run_id !== runId || runId !== progress().run_id) return;
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

  // 編集の基準版を添えて保存し、同じRunの応答だけを反映する。
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
    if (!file.pendingSave || Object.entries(body).some(([key, value]) => file.pendingSave[key] !== value)) {
      file.pendingSave = {...body, save_id: crypto.randomUUID().replaceAll("-", "")};
    }
    saving = true;
    const version = ++revision;
    showSaveState();
    report("#editor-error");
    try {
      const data = await request("editor", file.pendingSave);
      file.pendingSave = undefined;
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

  // 現在のRunが指示を受理でき、本文がある場合に送信を許可する。
  function updateSendButton() {
    sendButton.disabled = switching || state?.run_id !== progress().run_id || !state?.can_prompt || sending || !prompt.value.trim();
  }

  // 新規指示と送信待ちの指示編集を、ラベルと操作で区別する。
  function promptMode() {
    const active = Boolean(editPrompt);
    prompt.setAttribute("aria-label", active ? "送信待ちの指示を編集" : "Junie に指示");
    sendButton.querySelector("[data-send-icon]").hidden = active;
    sendButton.querySelector("[data-send-label]").hidden = !active;
    if (active) sendButton.removeAttribute("aria-label");
    else sendButton.setAttribute("aria-label", "送信");
    find("#editor-chat-shortcut").textContent = active ? "Ctrl+Enter で変更を保存" : "Ctrl+Enter で送信";
    cancelEdit.hidden = !active;
    cancelEdit.disabled = !active || sending;
    updateSendButton();
  }

  // 保存または取消後に、編集中だった新規指示を入力欄へ戻す。
  function finishPromptEdit() {
    editPrompt = undefined;
    prompt.value = newPromptDraft;
    promptMode();
    prompt.focus();
  }

  // 送信した指示と、その応答を表示する行を作る。
  function chatTurn(text) {
    const item = document.createElement("li");
    const message = junieChatBubble("あなた", text, true);
    const reply = junieChatBubble("ハーネス");
    item.append(message, reply);
    return {item, message, reply};
  }

  // 会話履歴を描画し、入力中の操作や表示位置を保つ。
  function renderPrompts() {
    const key = JSON.stringify([state.run_id, state.prompts, state.conversation, state.can_prompt,
      progress().status, Boolean(progress().junie_request)]);
    if (key === shownPrompts) return;
    shownPrompts = key;
    const conversation = state.conversation || [];
    const ids = new Set(conversation.map(message => message.id));
    const lastVerification = conversation.findLast(entry => entry.result_status);
    const expanded = new Set([...promptList.querySelectorAll("details[open]")].map(item => item.dataset.logKey));
    const turns = [...state.prompts.filter(message => !ids.has(message.id)).map(message => {
      const {item, message: outgoing, reply} = chatTurn(message.text);
      const label = {queued: "送信待ち", steering: "Junie に送信・処理中", applying: "反映確認中", sending: "処理中", sent: "送信済み"}[message.status];
      reply.append(Object.assign(document.createElement("p"), {textContent: label}));
      if (message.status === "queued" && state.can_prompt) {
        const button = Object.assign(document.createElement("button"), {className: "button", type: "button", textContent: "編集"});
        button.addEventListener("click", () => {
          if (editPrompt || sending) return;
          editPrompt = message.id;
          newPromptDraft = prompt.value;
          prompt.value = message.text;
          promptMode();
          prompt.focus();
        });
        outgoing.append(button);
      }
      return item;
    }), ...conversation.map(message => {
      const {item, reply} = chatTurn(message.text);
      reply.append(Object.assign(document.createElement("p"), {
        textContent: [...new Set([message.question, message.answer && `回答：${message.answer}`,
          message.plan, message.message || "受け付けました。"].filter(Boolean))].join("\n"),
      }));
      const targets = Object.entries(message.built_targets || {});
      if (targets.length) {
        const version = Object.assign(document.createElement("p"), {className: "editor-muted",
          textContent: targets.map(([name, hash]) => `${name} · ${hash || "ファイルなし"}`).join("\n")});
        reply.append(version);
      }
      if (message.review_targets?.length) {
        reply.append(Object.assign(document.createElement("p"), {className: "editor-muted",
          textContent: message.review_targets.join("\n")}));
      }
      for (const [index, log] of (message.logs || []).entries()) {
        const details = document.createElement("details");
        details.dataset.logKey = JSON.stringify([state.run_id, message.id, index]);
        details.open = expanded.has(details.dataset.logKey);
        const summary = Object.assign(document.createElement("summary"), {
          textContent: (log.kind === "build" ? "ビルド出力" : "チェッカー出力") + (log.truncated ? "（末尾）" : ""),
        });
        const output = Object.assign(document.createElement("pre"), {textContent: log.text});
        details.append(summary, output);
        reply.append(details);
      }
      if (message === lastVerification
          && message.result_status === progress().status && !progress().junie_request) {
        const label = {build_failed: "ビルド失敗を確認", checker_needs_review: "チェッカー出力を確認",
          checker_failed: "チェッカー出力を確認"}[message.result_status];
        if (label) {
          const button = Object.assign(document.createElement("button"), {className: "button", type: "button", textContent: label});
          button.addEventListener("click", () => find("#toggle").click());
          reply.append(button);
        }
      }
      return item;
    })];
    updateFeed(() => {
      promptList.replaceChildren(...turns);
      find("#editor-chat-empty").hidden = Boolean(turns.length);
    }, chatRun !== state.run_id);
    chatRun = state.run_id;
  }

  // 保存状態を会話履歴と送信欄に反映する。
  function updateChat() {
    renderPrompts();
    const liveQuestion = progress().junie_request;
    const question = liveQuestion?.title || state.operator_question;
    const instructions = liveQuestion
      ? "\nこの欄に選択肢の文言を入力してください。\n" + liveQuestion.options.map(option => option.label).join("、")
      : "\n回答を Junie に送ってから、進捗タブで実行を再開してください。";
    setText("#editor-operator-question", question ? "Junie からの確認\n" + question + instructions : "");
    find("#editor-operator-question").hidden = !question;
    const showAgent = state.source === "agent-workspace";
    const snapshot = progress();
    const activity = find("#editor-junie-activity");
    activity.textContent = showAgent && !snapshot.ready ? snapshot.activity?.message || "" : "";
    activity.hidden = !activity.textContent;
    find("#editor-prompt-timing").textContent = state.status_only ? "準備完了までは工程を確認できます。"
      : state.conversation && !editPrompt ? "工程の確認、追加指示、ビルドやチェッカーの再実行を依頼できます。"
      : !state.can_prompt ? "現在の工程では指示を登録できません。"
      : state.prompt_timing === "repair_restart" ? "保存した編集を反映して Junie に指示を渡し直し、対応後に再ビルドします。"
      : state.prompt_timing === "immediate" ? "実行中の Junie にすぐ渡します。反映の完了までお待ちください。"
      : state.prompt_timing === "resume" ? "同じ実行を再開したときに Junie に渡します。"
      : "現在の作業区間が終わると Junie に渡します。";
    updateSendButton();
    prompt.disabled = switching || state.run_id !== progress().run_id || !state.can_prompt || sending;
    cancelEdit.disabled = !editPrompt || sending;
  }

  // 保存版と未保存編集を照合して、表示中のRunの画面を更新する。
  async function display(data, expectedRevision = revision) {
    if (expectedRevision !== revision || switching || (data.run_id && data.run_id !== progress().run_id)) return;
    const previous = state;
    if (previous?.run_id !== data.run_id) {
      if (previous?.run_id) runDrafts.set(previous.run_id, {prompt: prompt.value, editPrompt, newPromptDraft});
      const draft = runDrafts.get(data.run_id) || {};
      selectedTarget = undefined;
      phpSource = undefined;
      phpRequest++;
      phpEditor?.setValue("");
      language = data.targets.length ? "target" : "php";
      editPrompt = draft.editPrompt;
      newPromptDraft = draft.newPromptDraft || "";
      prompt.value = draft.prompt || "";
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
    if (data.status_only) {
      current = undefined;
      layout.dataset.statusOnly = "true";
      editorPane.hidden = splitter.hidden = true;
      updateChat();
      layout.hidden = false;
      find("#editor-empty").hidden = true;
      return;
    }
    if (!editor) setText("#editor-empty", "エディタを読み込んでいます。");
    const monaco = await loadMonaco();
    if (expectedRevision !== revision || state !== data) return;
    delete layout.dataset.statusOnly;
    editorPane.hidden = splitter.hidden = false;
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
      layout.hidden = false;
      find("#editor-empty").hidden = true;
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
      model.onDidChangeContent(() => {
        file.pendingSave = undefined;
        if (file === current) showSaveState();
      });
    } else if (!data.read_error && readText(file.model) === file.saved) {
      file.saved = saved;
      file.base = data.edit?.base ?? data.content;
      file.editId = data.edit?.id ?? null;
      if (readText(file.model) !== saved) file.model.setValue(saved);
    }
    selectedTarget = data.target;
    if (latest.getLanguageId() !== fileLanguage) monaco.editor.setModelLanguage(latest, fileLanguage);
    const baseline = data.comparison_content ?? data.content;
    if (readText(latest) !== baseline) latest.setValue(baseline);
    const different = current !== file;
    current = file;
    showEditor(!different && diffOpen);
    showSaveState();
    updateChat();
    if (language === "php" && !phpSource) loadPhp();
    if (data.read_error || previous?.read_error) report("#editor-error", data.read_error);
    layout.hidden = false;
    find("#editor-empty").hidden = true;
  }

  // 表示中のRunだけを取得し、変更がない場合は描画を省く。
  async function refresh() {
    if (tab.getAttribute("aria-selected") !== "true" || refreshing === revision || saving || sending || switching) return;
    const version = revision;
    refreshing = version;
    const runId = progress().run_id;
    const target = state?.run_id === runId && selectedTarget;
    const query = new URLSearchParams();
    if (runId) query.set("run_id", runId);
    if (target) query.set("target", target);
    const previous = state?.run_id === runId && state?.target === target ? state : undefined;
    if (!demo && previous) for (const key of ["view_version", "content_version", "edit_version", "history_version"]) {
      if (previous[key]) query.set(key, previous[key]);
    }
    try {
      const data = await request("editor" + (query.size ? "?" + query : ""));
      if (data.unchanged) {
        if (version === revision && previous?.can_save && !layout.hidden) report("#editor-error");
        return;
      }
      if (previous && previous.run_id === data.run_id && previous.target === data.target) {
        for (const key of ["content", "edit", "conversation", "prompts"]) {
          if (!Object.hasOwn(data, key) && Object.hasOwn(previous, key)) data[key] = previous[key];
        }
        for (const [key, delta] of Object.entries(data.history_delta || {})) {
          const entries = new Map((data[key] || []).map(entry => [entry.id, entry]));
          for (const id of delta.removed) entries.delete(id);
          for (const entry of delta.changed) entries.set(entry.id, entry);
          data[key] = [...entries.values()];
          if (key === "conversation") data[key].sort((a, b) => (a.received_at || "").localeCompare(b.received_at || ""));
        }
        delete data.history_delta;
      }
      await display(data, version);
    } catch (error) {
      if (version === revision) {
        const empty = find("#editor-empty");
        if (layout.hidden) empty.textContent = error.message;
        else {
          state.view_version = undefined;
          state.can_save = state.can_prompt = false;
          showEditor(diffOpen);
          showSaveState();
          updateChat();
          report(state.status_only ? "#editor-prompt-error" : "#editor-error", error);
        }
      }
    } finally {
      if (refreshing === version) refreshing = undefined;
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
  prompt.addEventListener("input", () => {
    promptEdits++;
    updateSendButton();
  });
  const submittedMessages = new Map();
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const input = prompt.value, text = input.trim();
    if (!text || switching || state?.run_id !== progress().run_id || sending || !state?.can_prompt) return;
    const runId = state.run_id, editing = editPrompt, editCount = promptEdits;
    const showingRun = () => !switching && state?.run_id === runId && progress().run_id === runId;
    const ownsDraft = () => editing === editPrompt && editCount === promptEdits && input === prompt.value;
    sending = true;
    revision++;
    updateChat();
    report("#editor-prompt-error");
    try {
      if (state.conversation && !editing) {
        let submission = submittedMessages.get(runId);
        if (submission?.text !== text) {
          submission = {id: crypto.randomUUID().replaceAll("-", ""), text, status_only: layout.dataset.statusOnly === "true"};
          submittedMessages.set(runId, submission);
        }
        const entry = await request("editor", {kind: "conversation", run_id: runId, ...submission});
        if (!showingRun()) return;
        if (entry?.id !== submission.id || !Array.isArray(state.conversation)) {
          throw new Error("指示の受付を確認できません。再送すると同じ指示IDを使います。");
        }
        revision++;
        state.conversation = [...state.conversation.filter(item => item.id !== entry.id), entry];
        if (ownsDraft()) prompt.value = "";
        submittedMessages.delete(runId);
        return;
      }
      const data = await request("editor", {kind: "prompt", run_id: runId, id: editing, text});
      if (!showingRun()) return;
      revision++;
      state.prompts = data.prompts;
      if (ownsDraft()) {
        if (editing) finishPromptEdit();
        else prompt.value = "";
      }
    } catch (error) {
      if (showingRun()) report("#editor-prompt-error", error);
    } finally {
      sending = false;
      if (showingRun()) {
        updateChat();
        if (!prompt.disabled) prompt.focus();
      }
      if (state?.run_id !== progress().run_id || (selectedTarget && selectedTarget !== state?.target)) refresh();
    }
  });
  window.harnessEditorCanSwitch = () => !saving && !sending;
  window.harnessEditorStageDraft = (runId, text) => {
    if (switching || sending || runId !== progress().run_id) throw new Error("表示中の実行を選び直してください。");
    const displayed = state?.run_id === runId;
    const draft = displayed ? {prompt: prompt.value, editPrompt} : (runDrafts.get(runId) || {});
    if (draft.editPrompt) throw new Error("編集中の追加指示を閉じてから、もう一度実行してください。");
    const value = [draft.prompt, text].filter(Boolean).join("\n\n");
    if (new TextEncoder().encode(value).length > 4000) {
      throw new Error("追加指示が長すぎます。エディタ側の下書きを整理してください。");
    }
    if (displayed) {
      prompt.value = value;
      updateSendButton();
    } else {
      runDrafts.set(runId, {...draft, prompt: value});
    }
  };
  document.addEventListener("harnessrunselect", () => {
    switching = true;
    revision++;
    phpRequest++;
    phpSource = undefined;
    phpEditor?.setValue("");
    // 次のRunが読み込めるまで、前のRunの本文や指示を表示しない。下書きは保持する。
    layout.hidden = true;
    find("#editor-empty").hidden = false;
    setText("#editor-empty", "選択した実行を読み込んでいます…");
    report("#editor-error");
    report("#editor-prompt-error");
    if (state) { showSaveState(); updateChat(); }
  });
  document.addEventListener("harnessrunchange", () => { switching = false; revision++; refresh(); });
  setInterval(refresh, 750);
  workspaceTabs.addEventListener("harnesstab", refresh);
})();
