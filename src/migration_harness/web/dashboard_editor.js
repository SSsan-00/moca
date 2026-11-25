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
  const promptList = find("#editor-prompts");
  const attachButton = find("#editor-attach-selection");
  const selectionCard = find("#editor-selection-card");
  const selectionMeta = find("#editor-selection-meta");
  const selectionCode = find("#editor-selection-code");
  const removeSelection = find("#editor-remove-selection");
  const selectionWarning = find("#editor-selection-warning");
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
  let promptEdits = 0;
  let refreshing;
  let saving = false;
  let sending = false, attachment;
  let revision = 0;
  const diagnostics = find("#editor-diagnostics");
  const diagnosticList = find("#editor-diagnostic-list");
  const diagnosticRetry = find("#editor-diagnostic-retry");
  diagnosticRetry.addEventListener("click", scheduleDiagnostics);
  const diagnosticLimit = 2;
  const diagnosticBusyDelays = [500, 1000, 2000, 4000, 8000, 8000];
  let diagnosticGeneration = 0, diagnosticTimer, diagnosticInFlight = 0, pendingDiagnosis, markedModel;
  let semanticTimer, contextTimer, semanticInFlight = false, pendingSemantic;

  function startPendingDiagnosis() {
    if (!pendingDiagnosis || diagnosticInFlight >= diagnosticLimit) return;
    const diagnose = pendingDiagnosis;
    pendingDiagnosis = undefined;
    return diagnose();
  }

  function startPendingSemantic() {
    if (semanticInFlight || !pendingSemantic) return;
    const diagnose = pendingSemantic;
    pendingSemantic = undefined;
    return diagnose();
  }

  function renderDiagnostics(file, payload, result, valid) {
    const findings = [...result.findings, ...result.razor];
    const markers = findings.map(item => {
      const parts = payload.content.slice(item.start, item.start + item.length).split("\n");
      return {severity: window.monaco.MarkerSeverity.Error, code: item.code, message: item.message,
        startLineNumber: item.line, startColumn: item.column, endLineNumber: item.line + parts.length - 1,
        endColumn: parts.length === 1 ? item.column + Math.max(1, parts[0].length) : parts.at(-1).length + 1};
    });
    window.monaco.editor.setModelMarkers(file.model, "syntax", markers);
    markedModel = file.model;
    diagnosticList.replaceChildren(...markers.map(marker => {
      const li = document.createElement("li");
      const button = Object.assign(document.createElement("button"), {type: "button",
        textContent: `${marker.startLineNumber}行 ${marker.startColumn}列 · ${marker.code}  ${marker.message}`});
      button.addEventListener("click", () => {
        if (!valid()) return;
        const view = diffOpen ? diff.getModifiedEditor() : editor;
        const position = {lineNumber: marker.startLineNumber, column: marker.startColumn};
        view.setPosition(position);
        view.revealPositionInCenter(position);
        view.focus();
      });
      li.append(button);
      return li;
    }));
    return findings.length;
  }

  function scheduleDiagnostics() {
    const generation = ++diagnosticGeneration;
    clearTimeout(diagnosticTimer);
    clearTimeout(semanticTimer);
    clearTimeout(contextTimer);
    pendingDiagnosis = pendingSemantic = undefined;
    if (markedModel) window.monaco.editor.setModelMarkers(markedModel, "syntax", []);
    markedModel = undefined;
    diagnosticList.replaceChildren();
    diagnosticRetry.hidden = true;
    const file = current, run = state?.run_id, target = state?.target;
    const valid = () => generation === diagnosticGeneration && current === file && !switching && !state?.read_error
      && !!file && language === "target" && run === progress().run_id && target === selectedTarget
      && tab.getAttribute("aria-selected") === "true";
    diagnostics.hidden = !valid();
    if (diagnostics.hidden) return;
    setText("#editor-diagnostic-status", " · 診断待ち");
    setText("#editor-diagnostic-note", "入力が止まると、保存せずにこの下書きを診断します。");
    let busyRetries = 0, semanticRetries = 0, projectChangeRetried = false, payload;
    function enqueue(delay) {
      diagnosticTimer = setTimeout(() => {
        if (!valid()) return;
        pendingDiagnosis = diagnose;
        if (diagnosticInFlight >= diagnosticLimit) setText("#editor-diagnostic-status", " · 空き待ち");
        return startPendingDiagnosis();
      }, delay);
    }
    function enqueueSemantic(delay = 0) {
      semanticTimer = setTimeout(() => {
        if (!valid()) return;
        pendingSemantic = diagnoseSemantic;
        return startPendingSemantic();
      }, delay);
    }

    function showSemanticUnavailable(reason, retry = false) {
      const notes = {
        project_required: "対象を含む単一の .NET 9 C# プロジェクトが必要です。正式ビルドで確認してください。",
        project_configuration: "このプロジェクト構成の型・参照を安全に再現できません。正式ビルドで確認してください。",
        project_too_large: "エディタで確認できるプロジェクトの上限を超えています。",
        project_changed: "診断中に作業ツリーが変わりました。最新版で再診断できます。",
        context_unavailable: "型・参照の依存版を確認できません。再確認を待つか、正式ビルドで確認してください。",
        razor_context: "この Razor の生成条件を再現できません。正式ビルドで確認してください。",
        razor_unmapped_errors: "Razor のモデル型や生成された C# を一部確認できませんでした。正式ビルドで確認してください。",
        analyzer_busy: "ほかの型・参照診断を実行中です。",
        timeout: "型・参照の診断が制限時間内に終わりませんでした。"
      };
      diagnosticRetry.hidden = !retry;
      setText("#editor-diagnostic-status", " · 構文: 指摘なし / 型・参照: 未確認");
      setText("#editor-diagnostic-note", notes[reason] || "型・参照の解析器を利用できません。正式ビルドで確認してください。");
    }

    async function lookupContext(verifyRevision) {
      const result = await request("roslyn-diagnostics", {
        run_id: run, target, version: payload.version, mode: "context",
        ...(verifyRevision ? {verify_revision: verifyRevision} : {}),
      });
      if (!valid()) return;
      if (result.version !== payload.version || result.target !== target) throw Error("診断の依存版を確認できません。");
      return result;
    }

    function watchContext(expected, wasUnconfirmed = false) {
      contextTimer = setTimeout(async () => {
        if (!valid()) return;
        try {
          const latest = await lookupContext();
          if (!latest) return;
          if (latest.reason === "analyzer_busy") {
            if (!wasUnconfirmed) {
              renderDiagnostics(file, payload, {findings: [], razor: []}, valid);
              showSemanticUnavailable(latest.reason, true);
            }
            watchContext(expected, true);
          } else if (wasUnconfirmed || latest.context_revision !== expected) {
            scheduleDiagnostics();
          } else {
            watchContext(expected);
          }
        } catch {
          if (!valid()) return;
          renderDiagnostics(file, payload, {findings: [], razor: []}, valid);
          showSemanticUnavailable("context_unavailable", true);
          watchContext(expected, true);
        }
      }, 2250);
    }

    async function diagnoseSemantic() {
      if (!valid()) return;
      semanticInFlight = true;
      try {
        const result = await request("roslyn-diagnostics", {...payload, mode: "semantic"});
        if (!valid() || result.version !== payload.version || result.target !== target) return;
        if (result.status === "unavailable" && result.reason === "analyzer_busy" &&
            semanticRetries < diagnosticBusyDelays.length) {
          enqueueSemantic(diagnosticBusyDelays[semanticRetries++]);
          return;
        }
        if (result.reason === "project_changed" && !projectChangeRetried) {
          projectChangeRetried = true;
          enqueueSemantic(diagnosticBusyDelays[0]);
          return;
        }
        if (result.context_revision) {
          const latest = await lookupContext(result.context_proof ? result.context_revision : null);
          if (!latest) return;
          if (latest.reason === "analyzer_busy") {
            showSemanticUnavailable(latest.reason, true);
            watchContext(result.context_revision, true);
            return;
          }
          if (result.context_proof && latest.reason) {
            renderDiagnostics(file, payload, {findings: [], razor: []}, valid);
            showSemanticUnavailable(latest.reason, true);
            watchContext(result.context_revision);
            return;
          }
          if (latest.context_revision !== result.context_revision ||
              (result.context_proof && latest.context_proof !== result.context_proof)) {
            scheduleDiagnostics();
            return;
          }
          watchContext(result.context_revision);
        }
        if (!["clear", "errors", "partial"].includes(result.status)) {
          showSemanticUnavailable(result.reason,
            !["project_required", "project_configuration", "project_too_large", "razor_context",
              "razor_unmapped_errors"].includes(result.reason));
          return;
        }
        const count = renderDiagnostics(file, payload, result, valid);
        const partial = result.status === "partial";
        setText("#editor-diagnostic-status", count
          ? ` · 型・参照: ${count}件${result.truncated ? "以上" : ""}${partial ? " / 一部未確認" : ""}`
          : " · 構文・型・参照: 指摘なし");
        const checked = target.toLowerCase().endsWith(".cshtml")
          ? "元の Razor に対応する C# を確認しました。タグヘルパー等を含む全体は正式ビルドで確認してください。"
          : "この構成で確認できる型・参照を診断しました。最終結果は正式ビルドで確認してください。";
        setText("#editor-diagnostic-note", (result.truncated ? "表示上限に達しました。" : "")
          + (partial ? "元の Razor の位置を特定できない指摘があります。正式ビルドで確認してください。" : checked));
      } catch {
        if (valid()) showSemanticUnavailable(null, true);
      } finally {
        semanticInFlight = false;
        startPendingSemantic();
      }
    }

    async function diagnose() {
      if (!valid()) return;
      diagnosticInFlight++;
      try {
        payload ??= {run_id: run, target, version: String(generation), content: readText(file.model)};
        setText("#editor-diagnostic-status", " · 診断中");
        const result = await request("roslyn-diagnostics", payload);
        if (!valid() || result.version !== payload.version || result.target !== target) return;
        if (result.status === "unavailable" && result.reason === "analyzer_busy" && busyRetries < diagnosticBusyDelays.length) {
          setText("#editor-diagnostic-status", " · 空き待ち");
          setText("#editor-diagnostic-note", "ほかの診断が終わり次第、最新版をもう一度診断します。");
          enqueue(diagnosticBusyDelays[busyRetries++]);
          return;
        }
        const count = renderDiagnostics(file, payload, result, valid);
        if (result.supports_semantic && result.status === "clear" && !count) {
          setText("#editor-diagnostic-status", " · 構文: 指摘なし / 型・参照: 確認中");
          setText("#editor-diagnostic-note", "作業ツリーから型・参照を確認しています。編集中でもそのまま続けられます。");
          enqueueSemantic();
          return;
        }
        const unavailable = result.status === "unavailable" || result.status === "blocked";
        diagnosticRetry.hidden = !unavailable || result.reason === "demo";
        const status = unavailable ? "診断できません" : count ? `${count}件${result.truncated ? "以上" : ""}` : "指摘なし";
        setText("#editor-diagnostic-status", " · " + status);
        const reasons = {demo: "静的デモでは診断を実行しません。", timeout: "診断が時間内に終了しませんでした。",
          analyzer_busy: "ほかの診断を実行中です。"};
        setText("#editor-diagnostic-note", unavailable ? reasons[result.reason] || "解析器を利用できません。正式ビルドで確認してください。"
          : result.truncated ? "表示件数の上限に達しました。まず表示された箇所を確認してください。"
          : count ? "現在の下書きの構文診断です。構文エラーを修正してから型・参照を確認します。"
          : "現在の下書きの構文診断です。型や参照の正しさは正式ビルドで確認してください。");
      } catch (error) {
        if (!valid()) return;
        setText("#editor-diagnostic-status", " · 診断できません");
        diagnosticRetry.hidden = false;
        setText("#editor-diagnostic-note", error.message);
      } finally {
        diagnosticInFlight--;
        startPendingDiagnosis();
      }
    }
    enqueue(500);
  }

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
    updateSendButton();
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
      : current.adoption ? "採用済みの変更は残し、追加の編集を次の反映時に合流します。"
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
    if (!resolving && file.adoption) body.adoption = file.adoption;
    if (!file.pendingSave || file.pendingSave.adoption !== body.adoption
        || Object.entries(body).some(([key, value]) => file.pendingSave[key] !== value)) {
      file.pendingSave = {...body, save_id: crypto.randomUUID().replaceAll("-", "")};
    }
    const attempt = file.pendingSave;
    file.unconfirmedSaves.set(attempt.save_id, content);
    saving = true;
    const version = ++revision;
    showSaveState();
    report("#editor-error");
    try {
      const data = await request("editor", attempt);
      file.pendingSave = undefined;
      if (!data.accepted_edit_id) file.unconfirmedSaves.clear();
      file.saved = content;
      file.adoption = undefined;
      file.editId = data.edit?.id ?? null;
      file.queuedSaveId = data.edit?.save_id === attempt.save_id ? attempt.save_id : undefined;
      file.base = data.edit?.base ?? data.content;
      if (file === current) await display(data, version);
    } catch (error) {
      report("#editor-error", error);
    } finally {
      saving = false;
      showSaveState();
    }
  }

  function activeSelectionEditor() {
    if (!editor || language !== "target" || !state?.target || state.read_error || selectedTarget !== state.target) return;
    const view = diffOpen ? diff.getModifiedEditor() : editor;
    if (current && view.getModel() === current.model) return view;
  }

  function oneRange(view) {
    const ranges = view?.getSelections?.() || [];
    return ranges.length === 1 && !ranges[0].isEmpty() ? ranges[0] : undefined;
  }

  function selectionLabel({path, start_line, end_line}) {
    const span = start_line === end_line ? String(start_line) : start_line + "–" + end_line;
    return path + " · " + span + "行";
  }

  function attachmentWarning() {
    if (!attachment) return "";
    if (progress().junie_request || state?.operator_question) {
      return "確認への回答は、添付を外してから送信してください。";
    }
    if (attachment.invalid || attachment.runId !== state?.run_id || attachment.selection.path !== selectedTarget
        || state.read_error || current?.model !== attachment.model || attachment.version !== attachment.model.getVersionId()
        || attachment.contentVersion !== state.content_version) {
      return "ファイルまたは内容が変わりました。選択し直すか添付を外してください。";
    }
    return "";
  }

  function canCompose() {
    return !switching && state?.run_id === progress().run_id && state?.can_prompt && !sending;
  }

  function updateSendButton() {
    const warning = attachmentWarning();
    sendButton.disabled = !canCompose() || Boolean(warning) || (!attachment && !prompt.value.trim());
    attachButton.disabled = !canCompose() || !oneRange(activeSelectionEditor());
    if (selectionWarning.textContent !== warning) selectionWarning.textContent = warning;
    selectionWarning.hidden = !warning;
    selectionCard.hidden = !attachment;
    if (!attachment) selectionMeta.textContent = selectionCode.textContent = "";
    removeSelection.disabled = sending;
  }

  function attachSelection(view) {
    if (!canCompose() || view !== activeSelectionEditor()) return;
    const range = oneRange(view);
    if (!range) return;
    const model = view.getModel();
    const text = model.getValueInRange(range, window.monaco.editor.EndOfLinePreference.LF);
    const start = range.startLineNumber;
    const end = range.endLineNumber - Number(range.endColumn === 1 && range.endLineNumber > start);
    if (!text.trim() || end - start >= 20 || new TextEncoder().encode(text).length > 2000) {
      report("#editor-prompt-error", new Error("空白だけでない、最大20行・2,000バイトの範囲を選んでください。"));
      return;
    }
    const selection = {path: state.target, start_line: start, end_line: end, text};
    attachment = {selection, model, version: model.getVersionId(), runId: state.run_id,
      contentVersion: state.content_version};
    selectionMeta.textContent = selectionLabel(selection);
    selectionCode.textContent = text;
    report("#editor-prompt-error");
    updateSendButton();
    prompt.focus();
  }

  function registerSelectionAction(view, monaco) {
    view.onDidChangeCursorSelection?.(updateSendButton);
    view.addAction?.({
      id: "harness.attachSelection", label: "選択範囲を Junie に添付",
      contextMenuGroupId: "navigation", contextMenuOrder: 1.8, precondition: "editorHasSelection",
      keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyMod.Alt | monaco.KeyCode.KeyJ],
      run: () => attachSelection(view),
    });
  }

  // 送信した指示と、その応答を表示する行を作る。
  function chatTurn(text, selection, sender = "ハーネス") {
    const item = document.createElement("li");
    const message = junieChatBubble("あなた", text, true);
    if (selection) {
      const details = Object.assign(document.createElement("details"), {className: "editor-selection-history"});
      const summary = Object.assign(document.createElement("summary"), {textContent: selectionLabel(selection)});
      const code = Object.assign(document.createElement("pre"), {textContent: selection.text, tabIndex: 0});
      details.append(summary, code);
      message.append(details);
    }
    const reply = junieChatBubble(sender);
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
    const turns = [...(state.prompts || []).filter(message => !ids.has(message.id)).map(message => {
      const {item, reply} = chatTurn(message.text, message.selection);
      const label = {queued: "送信待ち", steering: "Junie に送信・処理中", applying: "反映確認中", sending: "処理中", sent: "送信済み"}[message.status];
      reply.append(Object.assign(document.createElement("p"), {textContent: label}));
      return item;
    }), ...conversation.map(message => {
      const sender = message.selection && message.status === "done" ? "Junie" : "ハーネス";
      const {item, reply} = chatTurn(message.text, message.selection, sender);
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
    find("#editor-prompt-timing").textContent = !state.can_prompt ? "現在の工程では指示を登録できません。" : "";
    updateSendButton();
    prompt.disabled = switching || state.run_id !== progress().run_id || !state.can_prompt || sending;
  }

  // 保存版と未保存編集を照合して、表示中のRunの画面を更新する。
  async function display(data, expectedRevision = revision) {
    if (expectedRevision !== revision || switching || (data.run_id && data.run_id !== progress().run_id)) return;
    const previous = state;
    if (previous?.run_id !== data.run_id) {
      if (previous?.run_id) runDrafts.set(previous.run_id, {prompt: prompt.value});
      const draft = runDrafts.get(data.run_id) || {};
      selectedTarget = undefined;
      attachment = undefined;
      phpSource = undefined;
      phpRequest++;
      phpEditor?.setValue("");
      language = data.targets.length ? "target" : "php";
      prompt.value = draft.prompt || "";
    }
    state = data;
    if (!data.run_id) {
      current = undefined;
      scheduleDiagnostics();
      layout.hidden = true;
      find("#editor-empty").hidden = false;
      find("#editor-empty").textContent = "実行タブで移行を始めるか、保存済みの実行を開いてください。";
      return;
    }
    if (data.status_only) {
      current = undefined;
      scheduleDiagnostics();
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
      registerSelectionAction(editor, monaco);
      registerSelectionAction(diff.getModifiedEditor(), monaco);
      new MutationObserver(theme).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
      theme();
    }
    showTargets(data.targets);
    if (!data.targets.length) {
      current = undefined;
      language = "php";
      scheduleDiagnostics();
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
      file = {model, saved, base: data.edit?.base ?? data.content, editId: data.edit?.id ?? null, unconfirmedSaves: new Map()};
      documents.set(key, file);
      model.onDidChangeContent(() => {
        file.pendingSave = undefined;
        if (file === current) { showSaveState(); scheduleDiagnostics(); }
      });
    } else if (!data.read_error) {
      const receipt = data.edit?.save_id ?? data.accepted_save_id;
      const queuedSave = file.queuedSaveId && receipt === file.queuedSaveId;
      const sent = file.unconfirmedSaves.get(receipt);
      const submitted = sent ?? file.saved;
      const text = readText(file.model);
      const adopted = !data.edit && data.accepted_edit_id && data.accepted_version
        && (queuedSave ? data.accepted_edit_id === file.editId : sent !== undefined);
      const merged = adopted && saved !== submitted && text !== submitted;
      const accepted = adopted || !data.edit && queuedSave && saved === file.saved || sent !== undefined;
      if (accepted || text === file.saved) {
        const sameQueuedSave = data.edit?.id === file.editId && receipt === file.queuedSaveId;
        file.queuedSaveId = data.edit && (accepted || sameQueuedSave) ? receipt : undefined;
        file.adoption = merged ? {save_id: receipt, edit_id: data.accepted_edit_id, version: data.accepted_version} : undefined;
        file.saved = merged ? submitted : saved;
        file.base = merged ? submitted : data.edit?.base ?? data.content;
        file.editId = data.edit?.id ?? null;
        if (accepted) {
          file.pendingSave = undefined;
          file.unconfirmedSaves.clear();
          report("#editor-error");
        }
        if ((!accepted || adopted && text === submitted) && text !== saved) file.model.setValue(saved);
      }
    }
    selectedTarget = data.target;
    if (latest.getLanguageId() !== fileLanguage) monaco.editor.setModelLanguage(latest, fileLanguage);
    const baseline = data.comparison_content ?? data.content;
    if (readText(latest) !== baseline) latest.setValue(baseline);
    const different = current !== file;
    current = file;
    if (different || Boolean(previous?.read_error) !== Boolean(data.read_error)) scheduleDiagnostics();
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
      if (attachment) attachment.invalid = true;
      updateSendButton();
      revision++;
      report("#editor-error");
    }
    scheduleDiagnostics();
    if (state.target === target) {
      showEditor(diffOpen);
      showSaveState();
    } else {
      refresh();
    }
  });
  phpButton.addEventListener("click", () => {
    language = "php";
    scheduleDiagnostics();
    showEditor(diffOpen);
    updateSendButton();
    if (!phpSource || phpSource.error) loadPhp(phpSource?.source);
    else showSaveState();
  });
  phpPicker.addEventListener("change", () => loadPhp(phpPicker.value));
  save.addEventListener("click", saveDraft);
  attachButton.addEventListener("click", () => attachSelection(activeSelectionEditor()));
  removeSelection.addEventListener("click", () => {
    attachment = undefined;
    updateSendButton();
    prompt.focus();
  });
  prompt.addEventListener("input", () => {
    promptEdits++;
    updateSendButton();
  });
  const submittedMessages = new Map();
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const input = prompt.value, text = input.trim();
    if (!canCompose() || attachmentWarning() || (!text && !attachment)) return;
    const selected = attachment;
    const selectionFields = selected ? {selection: selected.selection} : {};
    const submissionKey = JSON.stringify([text, selectionFields]);
    const runId = state.run_id, editCount = promptEdits;
    const showingRun = () => !switching && state?.run_id === runId && progress().run_id === runId;
    const ownsDraft = () => editCount === promptEdits && input === prompt.value;
    sending = true;
    revision++;
    updateChat();
    report("#editor-prompt-error");
    try {
      if (window.harnessRunModelReady && !await window.harnessRunModelReady(runId)) return;
      if (!showingRun()) return;
      if (selected !== attachment || attachmentWarning()) return;
      if (state.conversation) {
        let submission = submittedMessages.get(runId);
        if (submission?.key !== submissionKey) {
          const body = {id: crypto.randomUUID().replaceAll("-", ""), text,
            status_only: layout.dataset.statusOnly === "true", ...selectionFields};
          submission = {key: submissionKey, body};
          submittedMessages.set(runId, submission);
        }
        const entry = await request("editor", {kind: "conversation", run_id: runId, ...submission.body});
        if (!showingRun()) return;
        if (entry?.id !== submission.body.id || !Array.isArray(state.conversation)) {
          throw new Error("指示の受付を確認できません。再送すると同じ指示IDを使います。");
        }
        revision++;
        state.conversation = [...state.conversation.filter(item => item.id !== entry.id), entry];
        if (ownsDraft()) prompt.value = "";
        if (attachment === selected) attachment = undefined;
        submittedMessages.delete(runId);
        return;
      }
      const data = await request("editor", {kind: "prompt", run_id: runId, text, ...selectionFields});
      if (!showingRun()) return;
      revision++;
      state.prompts = data.prompts;
      if (attachment === selected) attachment = undefined;
      if (ownsDraft()) prompt.value = "";
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
    const draft = displayed ? {prompt: prompt.value} : (runDrafts.get(runId) || {});
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
    attachment = undefined;
    scheduleDiagnostics();
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
  workspaceTabs.addEventListener("harnesstab", () => { scheduleDiagnostics(); refresh(); });
})();

(() => {
  const groups = [...document.querySelectorAll(".run-model:has([data-run-model])")];
  if (!groups.length) return;
  const demo = window.harnessEditorDemo;
  const request = demo ? demo.request : workspaceRequest;
  const progress = demo ? () => demo.progress : () => snapshot;
  let catalog = [], current, selectedModel = "", selectedEffort = "", loading = false, generation = 0;
  const pendingSaves = new Map(), savedRevisions = new Map();
  let catalogWarning = "";

  function render(message = "") {
    const saving = pendingSaves.has(progress().run_id);
    const options = catalog.slice();
    if (selectedModel && !options.some(item => item.id === selectedModel)) options.push({id: selectedModel, name: selectedModel});
    const efforts = options.find(item => item.id === selectedModel)?.efforts || [];
    for (const group of groups) {
      const model = group.querySelector("[data-run-model]");
      const effort = group.querySelector("[data-run-effort]");
      model.replaceChildren(new Option("Junieの既定モデル", ""), ...options.map(item => new Option(item.name, item.id)));
      model.value = selectedModel;
      const values = efforts.filter(value => Object.hasOwn(effortLabels, value));
      effort.replaceChildren(new Option("モデルの既定値", ""), ...values.map(value => new Option(effortLabels[value], value)));
      if (selectedEffort && !values.includes(selectedEffort)) {
        const previous = new Option(`${selectedEffort}（未確認）`, selectedEffort);
        previous.disabled = true;
        effort.add(previous);
      }
      effort.value = selectedEffort;
      model.disabled = !current?.can_select || saving;
      effort.disabled = !current?.can_select || saving || !selectedModel || !values.length;
      group.querySelector(".run-model-status").textContent = [message, catalogWarning].filter(Boolean).join(" ");
    }
  }

  async function load() {
    const runId = progress().run_id;
    const version = ++generation;
    loading = true;
    current = undefined;
    render("モデル設定を読み込んでいます。");
    if (!runId) { loading = false; return; }
    try {
      if (pendingSaves.has(runId)) await pendingSaves.get(runId);
      if (version !== generation) return;
      const data = await request("run-model");
      if (version !== generation || data.run_id !== progress().run_id) return;
      current = data;
      savedRevisions.set(runId, data.revision ?? null);
      selectedModel = data.model || ""; selectedEffort = data.effort || "";
      render();
    } catch (error) {
      if (version === generation) render(error.message);
    } finally {
      if (version === generation) loading = false;
    }
  }

  function saveSelection(model, effort) {
    const runId = progress().run_id, previous = current;
    if (!previous?.can_select || pendingSaves.has(runId) || previous.run_id !== runId) return;
    selectedModel = model;
    selectedEffort = effort;
    const save = async () => {
      let message;
      try {
        const data = await request("editor", {kind: "model", run_id: runId, model, effort,
          model_revision: previous.revision});
        if (data.run_id !== runId || typeof data.junie_model_revision !== "string") {
          throw new Error("保存した実行と設定の版を確認できませんでした。");
        }
        savedRevisions.set(runId, data.junie_model_revision);
        if (current === previous) {
          current = {run_id: runId, can_select: true, model: data.junie_model, effort: data.junie_effort, revision: data.junie_model_revision};
        }
        return true;
      } catch (error) {
        if (current === previous) {
          selectedModel = current.model || "";
          selectedEffort = current.effort || "";
          message = "保存できませんでした。" + error.message;
        }
        return false;
      } finally {
        pendingSaves.delete(runId);
        if (progress().run_id === runId) render(message);
      }
    };
    pendingSaves.set(runId, Promise.resolve().then(save));
    render("モデル設定を保存しています。");
  }

  window.harnessRunModelReady = runId => pendingSaves.get(runId) ?? true;
  window.harnessRunModelRevision = runId => savedRevisions.get(runId);
  for (const group of groups) {
    group.querySelector("[data-run-model]").addEventListener("change", event => saveSelection(event.target.value, ""));
    group.querySelector("[data-run-effort]").addEventListener("change", event => saveSelection(selectedModel, event.target.value));
  }
  document.addEventListener("harnessrunselect", () => { generation++; current = undefined; render("実行を読み込んでいます。"); });
  document.addEventListener("harnessrunchange", load);
  setInterval(() => {
    if (!loading && !pendingSaves.has(progress().run_id) && !current?.can_select && progress().run_id && progress().worktree_path) load();
  }, 3000);
  render("モデル一覧を読み込んでいます。");
  (async () => {
    const fallback = [...document.querySelector("#settings-junie-model").options].filter(option => option.value)
      .map(option => ({id: option.value, name: option.text, ...(demo ? {efforts: option.value === "junie-lite" ? ["none"] : ["low", "medium", "high"]} : {})}));
    try {
      catalog = demo ? fallback : (await workspaceRequest("junie-models")).models;
      if (!catalog.length) throw new Error("モデル候補がありません。");
    } catch {
      catalog = fallback;
      catalogWarning = "モデル一覧を取得できないため、代表候補を表示しています。Junieを確認し、画面を開き直してください。";
    }
    await load();
  })();
})();
