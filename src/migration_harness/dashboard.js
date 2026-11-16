"use strict";

// ダッシュボードテンプレートの単一要素を取得する。
const element = selector => document.querySelector(selector);
const dialog = element("#review-dialog");
const note = element("#review-note");
const accept = element("#review-accept");
const resume = element("#review-fix");
const checkerInstruction = element("#checker-instruction");
const checkerOutput = element("#checker-review-output");
const checkerOutputStatus = element("#checker-output-status");
const checkerDownload = element("#checker-output-download");
const checkerDetails = element("#checker-review-details");
const findingSection = element(".review-finding");
const reviewNoteLabel = element('label[for="review-note"]');
let checkerOutputRequest = 0;
const viewer = element("#open-diff-viewer");
const dashboardViewer = element(".runbar [data-compare]");
for (const button of [viewer, dashboardViewer]) {
  button.title = "現在の参照PHPと最新の保存済み移行先を比較します";
}
const reviewButton = element("#toggle");
const stopButton = element("#stop");
const viewerPreview = element("#viewer-preview");
const viewerPreviewBody = viewerPreview.querySelector(".viewer-preview-body");
element("#viewer-preview-title").textContent = "移行ソースの差分比較";
viewerPreview.addEventListener("close", () => viewerPreviewBody.replaceChildren());
const notice = document.createElement("p");
notice.id = "viewer-status";
notice.className = "review-caveat";
notice.setAttribute("aria-live", "polite");
notice.hidden = true;
element(".review-actions").before(notice);
const activity = element("#activity");
const ledger = element("#ledger-list");
const steps = [...document.querySelectorAll(".step")];
steps[4].querySelector("h3").textContent = "ビルド";
steps[6].querySelector("h3").textContent = "人の確認";
const statusNames = {
  idle: "実行を選択してください", preparing: "準備中", prepared: "準備完了", junie_running: "Junieで移行中",
  junie_finished: "Junieの編集完了", editor_checkpoint: "編集・追加指示の反映", repairing: "Junieで修正中", building: "ビルド中",
  editor_followup: "Junieが追加指示を実行中",
  build_succeeded: "ビルド成功", build_failed: "ビルド失敗", checking: "チェッカー実行中",
  checker_needs_review: "人の確認待ち", checker_failed: "チェッカー実行失敗",
  checker_succeeded: "チェッカー成功", checker_accepted: "チェッカーの指摘を承認済み",
  script_running: "スクリプト実行中", script_failed: "スクリプトで停止",
  interrupted: "中断", junie_failed: "Junie実行失敗",
  preparation_interrupted: "準備を中断", preparation_failed: "準備に失敗",
};
const savedView = document.documentElement.hasAttribute("data-saved");
const verifiedStatuses = new Set(["checker_succeeded", "checker_accepted", "build_succeeded"]);
const statusStages = {
  preparing: 0, preparation_interrupted: 0, preparation_failed: 0,
  prepared: 2, junie_running: 3, junie_finished: 3, editor_checkpoint: 3, interrupted: 3, junie_failed: 3,
  editor_followup: 3,
  repairing: 3, building: 4, build_succeeded: 4, build_failed: 4, checking: 5, checker_failed: 5,
  checker_needs_review: 6, checker_succeeded: 6, checker_accepted: 6,
};
const scriptStages = {
  before_prepare: 0, after_prepare: 2, before_migration: 3, after_migration: 3,
  before_build: 4, after_build: 4, before_checker: 5, after_checker: 5,
};
let snapshot = {status: "preparing"};
let migrationCompletion;
let migrationLedgerRequest = 0;
let dashboardRevision = 0;
let submitting = false;
let connected = false;
let prompted = false;
let previousLog = "";
const seenActivities = new Set();
let renderedReview = "";
let stopPendingOperation;
let snapshotting = false;
let pendingOnly = false;

document.title = "移行ハーネスの進捗";
element("#elapsed").setAttribute("aria-label", "計測済み工程の合計経過時間");
element("#elapsed").title = "計測済み工程の合計（停止中の待ち時間を除く）";
element(".brand").href = "./";
element(".top-links").hidden = true;
const snapshotStatus = document.createElement("div");
snapshotStatus.className = "run-detail";
snapshotStatus.setAttribute("role", "status");
element(".run-detail").after(snapshotStatus);
const modalComparisonStatus = document.createElement("p");
modalComparisonStatus.className = "review-caveat";
modalComparisonStatus.setAttribute("role", "status");
modalComparisonStatus.hidden = true;
element(".review-actions").before(modalComparisonStatus);
const anchorButtons = document.querySelectorAll("[data-propose-anchors]");
for (const button of anchorButtons) {
  bindComparisonOptions(button, () => openSnapshot({withAnchors: true, fromReview: dialog.contains(button)}));
}
element(".controls").setAttribute("aria-label", "実行操作");
const allFilter = element('[data-filter="all"]');
const pendingFilter = element('[data-filter="judgment"]');
for (const button of element(".ledger-controls").children) {
  button.hidden = button !== allFilter && button !== pendingFilter;
}
const emptyFinding = element("#ledger-empty");
element("#ledger-title").textContent = "チェッカーの指摘";
activity.replaceChildren();
ledger.replaceChildren();
element(".work-summary .metric-label").textContent = "カプセル総数";
element(".work-summary .metric:nth-child(2) .metric-label").textContent = "移行済み";
reviewNoteLabel.textContent = "判断理由（必須）";
note.placeholder = "例：共通関数へ委譲していることを差分で確認済み";
note.required = true;
note.maxLength = 2000;
element("#review-demo-context").hidden = true;
element("#review-later").hidden = true;
accept.textContent = "問題なしとして記録";
resume.textContent = "同じ実行を再開";
const findings = document.createElement("select");
findings.className = "workspace-select";
findings.setAttribute("aria-label", "レビューする指摘");
const diagnostic = document.createElement("pre");
diagnostic.className = "review-code";
const findingLocation = document.createElement("div");
findingLocation.className = "review-location";
const locationCard = document.createElement("div");
const locationTitle = document.createElement("small");
locationTitle.textContent = "指摘箇所";
const locationPath = document.createElement("code");
const locationLine = document.createElement("small");
locationCard.append(locationTitle, locationPath, locationLine);
findingLocation.append(locationCard);
const reviewFiles = document.createElement("p");
reviewFiles.className = "review-caveat";
findingSection.replaceChildren(findings, findingLocation, diagnostic);
checkerDetails.append(reviewFiles);
const connection = document.createElement("span");
connection.setAttribute("role", "status");
element("#activity-title").after(connection);
const reviewTitle = element("#review-title");
const checkerReviewBody = document.createElement("div");
const modalHeading = dialog.querySelector(".review-heading");
checkerReviewBody.append(...Array.from(modalHeading.parentElement.children).slice(1));
modalHeading.after(checkerReviewBody);
const junieReviewBody = document.createElement("div");
const junieDetail = document.createElement("pre");
junieDetail.className = "review-code";
const junieChoices = document.createElement("div");
junieChoices.className = "review-actions junie-choices";
const junieNotice = document.createElement("p");
junieNotice.className = "review-caveat";
junieNotice.setAttribute("role", "status");
junieReviewBody.append(junieDetail, junieNotice, junieChoices);
checkerReviewBody.after(junieReviewBody);
junieReviewBody.hidden = true;
let junieRequestId;
let junieSubmitting;
const buildDialog = element("#build-review-dialog");
const buildTarget = element("#build-review-target");
const buildSource = element("#build-review-source");
const buildInstruction = element("#build-review-instruction");
const buildSubmit = element("#build-review-submit");
const buildReset = element("#build-review-reset");
const buildFeedback = element("#build-review-feedback");
const buildFileStatus = element("#build-review-file-status");
let buildViewRun;
let buildFile;
let buildRequest = 0;
let buildLoading = false;
let buildEditSubmitted = false;
const progressPrompt = bindProgressPromptDialog({
  open: refreshProgressPrompts, submit: submitProgressPrompt, inputChanged: syncProgressPrompts,
});
let progressPrompts;
let promptLoading = false;
let promptSaving = false;
let promptRequest = 0;

// 判断に関する通知はメッセージがあるときだけ表示する。
function showNotice(message) {
  notice.textContent = message;
  notice.hidden = !message;
}

// 接続と実行状態に応じて、端末で受け付ける操作だけを有効にする。
function syncActions() {
  const canAct = connected && snapshot.ready && !submitting;
  workspaceControls();
  const canReview = snapshot.status === "checker_needs_review";
  const selected = snapshot.review?.findings[findings.selectedIndex];
  accept.disabled = !canAct || !canReview || !selected || selected[2] || !note.value.trim();
  const instruction = checkerInstruction.value.trim();
  const instructionTooLong = new TextEncoder().encode(instruction).length > 4_000;
  resume.disabled = !canAct || !snapshot.resumable || instructionTooLong;
  resume.textContent = instruction ? "指示を送って再開" : "同じ実行を再開";
  checkerInstruction.readOnly = !canAct || !snapshot.resumable;
  element("#checker-open-editor").disabled = !canAct;
  element("#checker-instruction-status").textContent = instructionTooLong
    ? "追加指示を 4,000 バイト以下にしてください。"
    : "指示を送ると、Junie が出力と指示を確認してから再ビルドします。";
  reviewButton.disabled = snapshot.junie_request ? !connected
    : !canAct || !(canReview || snapshot.resumable || snapshot.status === "build_failed");
  for (const button of junieChoices.children) {
    button.disabled = !connected || junieSubmitting === junieRequestId || !!snapshot.stop_requested;
  }
  const stopping = snapshot.stop_requested || (stopPendingOperation !== undefined && stopPendingOperation === snapshot.operation_id);
  stopButton.hidden = !snapshot.cancellable && !stopping;
  stopButton.disabled = !connected || submitting || !snapshot.cancellable || stopping;
  stopButton.textContent = stopping ? "停止を待っています" : "停止";
  migrationLedgerButton.disabled = !connected || !snapshot.worktree_path;
  dashboardViewer.disabled = viewer.disabled = !connected || !snapshot.worktree_path || !snapshot.diff_viewer_enabled || snapshotting;
  element("#migration-ledger-compare").disabled = dashboardViewer.disabled;
  for (const button of anchorButtons) {
    button.disabled = dashboardViewer.disabled || !snapshot.ready || submitting;
  }
  syncBuildReview(canAct);
  syncProgressPrompts();
}

function syncProgressPrompts() {
  progressPrompt.button.hidden = !snapshot.worktree_path;
  progressPrompt.button.disabled = !connected;
  progressPrompt.render(progressPrompts, {available: connected, loading: promptLoading, saving: promptSaving});
}

async function refreshProgressPrompts() {
  if (!progressPrompt.dialog.open || !connected || promptLoading || promptSaving) return;
  const request = ++promptRequest;
  const runId = snapshot.run_id;
  promptLoading = true;
  syncProgressPrompts();
  try {
    const data = await workspaceRequest("prompts");
    if (request === promptRequest && data.run_id === runId) progressPrompts = data;
  } catch (error) {
    if (request === promptRequest) {
      progressPrompts = undefined;
      progressPrompt.showError(error);
    }
  } finally {
    promptLoading = false;
    syncProgressPrompts();
  }
}

async function submitProgressPrompt(text) {
  const runId = snapshot.run_id;
  const request = ++promptRequest;
  promptSaving = true;
  progressPrompt.showError();
  syncProgressPrompts();
  try {
    const data = await workspaceRequest("prompts", {run_id: runId, text});
    if (request === promptRequest && data.run_id === runId) {
      progressPrompts = data;
      progressPrompt.input.value = "";
    }
  } catch (error) {
    if (request === promptRequest) progressPrompt.showError(error);
  } finally {
    promptSaving = false;
    syncProgressPrompts();
  }
}

function buildEditIsDirty() {
  return !!buildFile && buildSource.value !== buildFile.content;
}

function syncBuildReview(canAct) {
  const edited = buildEditIsDirty();
  const instructed = !!buildInstruction.value.trim();
  const source = edited ? buildSource.value : "";
  const tooLarge = edited && source.length * 3 + buildFile.bomBytes > buildFile.maxBytes && (
    source.length + buildFile.bomBytes > buildFile.maxBytes
    || new TextEncoder().encode(buildFile.crlf ? source.replaceAll("\n", "\r\n") : source).length
      + buildFile.bomBytes > buildFile.maxBytes
  );
  buildTarget.disabled = submitting || edited;
  buildSource.readOnly = buildInstruction.readOnly = submitting || !snapshot.resumable;
  buildReset.hidden = !edited;
  buildReset.disabled = submitting;
  buildSubmit.disabled = !canAct || !snapshot.resumable || buildLoading || tooLarge;
  buildSubmit.textContent = instructed
    ? (edited ? "保存して Junie に修正を依頼" : "Junie に修正を依頼して再開")
    : (edited ? "保存して再ビルド" : "再ビルド");
  if (buildFile) {
    buildFileStatus.textContent = tooLarge ? `${buildFile.maxBytes / (1024 * 1024)} MiB を超えています。作業ツリーで編集してください。`
      : edited ? "未保存の編集があります。別のファイルを選ぶには、保存するか最新の内容を読み込み直してください。" : "";
  }
}

async function fetchBuildReview(runId, target) {
  const query = new URLSearchParams({run_id: runId});
  if (target) query.set("target", target);
  const response = await fetch(`build-review?${query}`, {cache: "no-store"});
  if (!response.ok) throw await responseError(response);
  return response.json();
}

async function loadBuildSource() {
  const request = ++buildRequest;
  const target = buildTarget.value;
  buildFile = undefined;
  buildSource.value = "";
  buildSource.hidden = element("#build-review-source-label").hidden = true;
  buildFileStatus.textContent = target ? "ファイルを読み込んでいます…" : "";
  buildLoading = !!target;
  syncActions();
  if (!target) return;
  try {
    const file = await fetchBuildReview(buildViewRun, target);
    if (request !== buildRequest) return;
    buildSource.value = file.content;
    buildFile = {
      target, version: file.version, content: buildSource.value,
      maxBytes: file.max_bytes, crlf: file.crlf, bomBytes: file.bom_bytes,
    };
    buildSource.hidden = element("#build-review-source-label").hidden = false;
  } catch (error) {
    if (request === buildRequest) buildFileStatus.textContent = `ファイルを開けませんでした。\n${error.message}`;
  } finally {
    if (request === buildRequest) {
      buildLoading = false;
      syncActions();
    }
  }
}

async function openBuildReview() {
  const request = ++buildRequest;
  const runId = snapshot.run_id;
  const newRun = buildViewRun !== runId;
  buildViewRun = runId;
  if (newRun) {
    buildFile = undefined;
    buildInstruction.value = buildSource.value = "";
    element("#build-review-editor").open = false;
  }
  const errors = element("#build-review-errors");
  const logSection = element("#build-review-log-section");
  const editor = element("#build-review-editor");
  element("#build-review-intro").textContent = Number.isInteger(snapshot.build_exit_code)
    ? `ビルドの終了コード ${snapshot.build_exit_code}。独自チェッカーの前で停止しました。`
    : "ビルド工程で停止しました。エラーの詳細を確認してください。";
  errors.textContent = "ビルド出力を読み込んでいます…";
  logSection.hidden = editor.hidden = true;
  buildFeedback.textContent = snapshot.message || (!snapshot.resumable
    ? "再開するには、前回の実行で使用したチェッカーを設定してください。" : "");
  buildLoading = true;
  buildDialog.showModal();
  element("#build-review-title").focus();
  syncActions();
  try {
    const review = await fetchBuildReview(runId);
    if (request !== buildRequest) return;
    errors.textContent = review.errors.join("\n") || snapshot.error || "エラーの詳細はビルド出力を確認してください。";
    element("#build-review-log").textContent = review.log;
    element("#build-review-log-label").textContent = review.truncated ? "ビルド出力（末尾 64 KiB）" : "ビルド出力";
    logSection.hidden = !review.log;
    const selected = buildFile?.target || (!newRun && buildTarget.value) || review.preferred_target;
    buildTarget.replaceChildren(new Option("ファイルを選択", ""), ...review.targets.map(path => new Option(path, path)));
    buildTarget.value = review.targets.includes(selected) ? selected : "";
    editor.hidden = !review.targets.length;
    if (!buildEditIsDirty()) await loadBuildSource();
  } catch (error) {
    if (request === buildRequest) {
      errors.textContent = snapshot.error || "ビルド工程で停止しました。";
      buildFeedback.textContent = `ビルド出力を読み込めませんでした。実行ログを確認するか、画面を開き直してください。\n${error.message}`;
    }
  } finally {
    if (request === buildRequest) {
      buildLoading = false;
      syncActions();
    }
  }
}

// 選んだ指摘の診断とソース位置を表示する。
function renderSelectedFinding() {
  const selected = snapshot.review?.findings[findings.selectedIndex];
  diagnostic.textContent = selected?.[1] || snapshot.review_error || "選択できる指摘はありません。";
  const reported = snapshot.review?.locations?.[findings.selectedIndex];
  findingLocation.hidden = !reported || Number(reported.line) < 1;
  locationPath.textContent = reported?.path || "";
  locationLine.textContent = reported ? `チェッカー報告 · ${reported.line}行目` : "";
}

// 選択した指摘を見せ、別の指摘の判断理由が残らないようにする。
function openReview(index = findings.selectedIndex) {
  if (snapshot.junie_request) {
    openJunieReview();
    return;
  }
  if (index !== findings.selectedIndex) {
    findings.selectedIndex = index;
    note.value = "";
  }
  renderSelectedFinding();
  syncActions();
  if (!dialog.open) {
    dialog.showModal();
    reviewTitle.focus();
    loadCheckerOutput();
  }
}

async function loadCheckerOutput() {
  const request = ++checkerOutputRequest;
  const path = `checker-output?run_id=${encodeURIComponent(snapshot.run_id)}`;
  checkerOutput.textContent = "";
  checkerDownload.hidden = true;
  checkerOutputStatus.textContent = "出力を読み込んでいます…";
  try {
    const data = await workspaceRequest(path);
    if (request !== checkerOutputRequest) return;
    checkerOutput.textContent = data.output || "チェッカーの出力はありません。";
    checkerOutputStatus.textContent = data.truncated ? "出力の末尾 64 KiB を表示しています。" : "";
    checkerDownload.href = `${path}&download=1`;
    checkerDownload.hidden = !data.output;
  } catch {
    if (request === checkerOutputRequest) checkerOutputStatus.textContent =
      "出力を読み込めませんでした。画面を開き直すか、実行ログを確認してください。";
  }
}

function openJunieReview() {
  if (!dialog.open) dialog.showModal();
  reviewTitle.focus();
}

function renderJunieReview(request) {
  if (request?.id === junieRequestId) return;
  junieRequestId = request?.id;
  checkerReviewBody.hidden = !!request;
  junieReviewBody.hidden = !request;
  reviewTitle.textContent = request?.title || "チェッカーの指摘を確認";
  junieNotice.textContent = "";
  junieChoices.replaceChildren();
  if (!request) {
    if (dialog.open) dialog.close();
    return;
  }
  junieDetail.textContent = request.detail;
  const labels = {
    allow_once: "今回のみ許可", reject_once: "今回のみ拒否",
    allow_always: "今後も許可", reject_always: "今後も拒否",
  };
  for (const choice of request.options) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "review-action";
    button.textContent = request.title === "Junie の操作を確認" && labels[choice.kind]
      ? `${labels[choice.kind]}（${choice.label}）` : choice.label;
    button.addEventListener("click", () => answerJunie(request.id, choice.id));
    junieChoices.append(button);
  }
  if (!progressPrompt.dialog.open) openJunieReview();
}

async function answerJunie(requestId, optionId) {
  if (!connected || snapshot.junie_request?.id !== requestId || junieSubmitting === requestId) return;
  junieSubmitting = requestId;
  junieNotice.textContent = "回答を送信しています。";
  syncActions();
  try {
    const response = await fetch("control", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: "junie-answer", revision: dashboardRevision,
        operation_id: snapshot.operation_id, request_id: requestId, option_id: optionId}),
    });
    if (!response.ok) throw await responseError(response);
    if (junieRequestId === requestId) junieNotice.textContent = "回答を送信しました。";
  } catch (error) {
    if (junieRequestId === requestId) {
      junieNotice.textContent = `回答を送信できませんでした。接続を確認して、もう一度お試しください。\n${error.message}`;
    }
    if (junieSubmitting === requestId) junieSubmitting = undefined;
    syncActions();
  }
}

// 判断状態で行を絞り込み、該当件数がない場合の表示を更新する。
function applyFindingFilter() {
  allFilter.setAttribute("aria-pressed", String(!pendingOnly));
  pendingFilter.setAttribute("aria-pressed", String(pendingOnly));
  let visible = 0;
  for (const row of ledger.querySelectorAll("[data-status]")) {
    row.hidden = pendingOnly && row.dataset.status !== "judgment";
    if (!row.hidden) visible++;
  }
  emptyFinding.hidden = visible > 0;
}

// 同じRun状態の説明を、実行ログと画面上部に使う。
function describe(state) {
  if (state.generating_anchors) return "Junieでアンカー生成中";
  const label = statusNames[state.status] || state.status;
  if (state.script_phase && scriptPhases[state.script_phase]) return `${scriptPhases[state.script_phase]}の${label}`;
  if (state.status === "checker_needs_review" && !state.ready) return "チェッカー結果を処理中";
  if (state.status === "junie_running") return `${label} (${state.current_capsule ?? 1}/${state.context_capsules ?? "?"})`;
  if (state.status === "repairing") return `Junieで修正中（${state.repair_count}回目）`;
  return label;
}

// 台帳と完了判定を、同じRunの最新情報から更新する。
async function loadMigrationLedger() {
  const request = ++migrationLedgerRequest;
  try {
    const response = await fetch("migration-ledger", {cache: "no-store"});
    if (!response.ok) throw await responseError(response);
    const ledger = await response.json();
    if (request !== migrationLedgerRequest || ledger.run_id !== snapshot.run_id) return;
    migrationCompletion = ledger.completion;
    render();
    if (!migrationLedgerDialog.open) return;
    showMigrationLedger(ledger, snapshot, id => {
      const url = new URL(`${comparisonPath(ledger.run_id, id)}.html`, document.baseURI);
      displayComparison(url.href, reserveComparisonTab("比較を読み込み中"));
    });
  } catch (error) {
    if (request !== migrationLedgerRequest) return;
    migrationCompletion = undefined;
    render();
    if (!migrationLedgerDialog.open) return;
    migrationLedgerMessage.hidden = false;
    migrationLedgerMessage.textContent = `移行台帳を読み込めませんでした。\n${error.message}`;
  }
}

migrationLedgerButton.addEventListener("click", () => {
  openMigrationLedger("移行台帳を読み込み中");
  loadMigrationLedger();
});
window.addEventListener("focus", () => {
  if (snapshot.worktree_path && (snapshot.ready || migrationLedgerDialog.open)) loadMigrationLedger();
});

async function responseError(response) {
  // サーバーの診断を捨てず、HTMLのエラー本文も文字列として表示する。
  return new Error(`HTTP ${response.status} ${response.statusText}\n${await response.text()}`);
}

async function workspaceRequest(path, payload) {
  const message = "操作できませんでした。接続と入力内容を確認して、もう一度お試しください。";
  let response;
  try {
    response = await fetch(path, payload === undefined ? {cache: "no-store"} : {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
    });
  } catch (error) {
    throw new Error(`${message}\n${path}: ${error.message}`);
  }
  if (!response.ok) {
    throw await responseError(response);
  }
  return response.json();
}

bindHarnessWorkspace({
  readRuns: () => workspaceRequest("workspace"),
  readSettings: () => workspaceRequest("settings"),
  previewSettings: (content, changes) => workspaceRequest("settings", {mode: "preview", content, changes}),
  saveSettings: (content, version) => workspaceRequest("settings", {content, version, revision: dashboardRevision}),
  execute: (action, payload) => workspaceRequest("control", {action, revision: dashboardRevision, ...payload}),
  canAct: () => connected && snapshot.ready && !submitting,
});
const tokenRuns = bindTokenRuns(run => {
  const phases = {rule_review: "詳細ルールの事前確認", build_repair: "ビルドの自動修正", checker_repair: "チェッカーの自動修正", anchors: "アンカー生成"};
  const rows = (run?.token_usage || []).map(row => ({
    ...row, label: row.phase === "migration" ? `カプセル ${row.capsule}` : phases[row.phase],
  }));
  renderTokenUsage(rows, run ? `${run.unit_id} の移行 · Run ID ${run.run_id}` : "",
    run?.running ? "Junieからの使用量の報告を待っています。" :
      run ? "このRunには使用量の記録がありません。" : "", run?.running);
});
let tokenListRequest = 0;
workspaceTabs.addEventListener("harnesstab", async () => {
  render();
  if (element("#tab-tokens").getAttribute("aria-selected") !== "true") return;
  const request = ++tokenListRequest;
  try {
    const {runs} = await workspaceRequest("token-runs");
    if (request === tokenListRequest) tokenRuns.setRuns(runs);
  } catch (error) {
    if (request === tokenListRequest) tokenRuns.setRuns([], `Run一覧を読み込めませんでした。\n${error.message}`);
  }
});
if (document.documentElement.hasAttribute("data-workspace")) switchHarnessTab(1);

function renderCapsuleProgress(data, total, completed) {
  const capsuleIndex = Math.max(0, (data.current_capsule ?? 1) - 1);
  const chars = data.context_part_chars?.[capsuleIndex] ?? 0;
  const limit = data.max_context_chars ?? 0;
  const percent = limit ? Math.round(chars * 100 / limit) : 0;
  element("#capsule-call").textContent = total ? `カプセル ${capsuleIndex + 1} / ${total}` : "未準備";
  element(".capsule-top > span").textContent = limit ? `上限 ${limit.toLocaleString("ja-JP")}文字` : "上限 —";
  element("#char-count").textContent = total ? chars.toLocaleString("ja-JP") : "—";
  element("#capsule-percent").textContent = total ? `上限の${percent}%` : "—";
  element("#capsule-meter").setAttribute("aria-valuemax", String(limit || 1));
  element("#capsule-meter").setAttribute("aria-valuenow", String(chars));
  element("#meter-fill").style.width = `${percent}%`;
  element(".work-summary .metric-value").textContent = String(total);
  element("#checked-count").textContent = `${completed} / ${total}`;
}

const checkerReviewStatuses = new Set(["checker_needs_review", "checker_failed"]);
const checkerReviewGuidance = {
  finding_repeated: ["修正後も同じチェッカーの指摘が残っているため、自動修正を止めました。"],
  repair_limit: ["この実行の自動修正回数が上限に達しました。"],
  repair_disabled: ["この実行では自動修正が無効に設定されています。"],
  outside_targets: ["チェッカーが、設定された移行対象以外のファイルを指摘しました。",
    "チェッカー出力で対象ファイルを確認し、必要に応じて対象ファイルや設定を見直してください。"],
  output_too_large: ["チェッカー出力が、自動修正で扱える量を超えました。",
    "チェッカー出力をダウンロードし、修正が必要な箇所を絞って「Junie への追加指示」に入力してください。"],
  output_encoding: ["チェッカー出力を UTF-8 のテキストとして読み取れませんでした。",
    "元の出力をダウンロードして確認し、チェッカーの出力文字コードを UTF-8 に設定してください。"],
  output_unreadable: ["チェッカーの出力から、修正に使える内容を読み取れませんでした。",
    "チェッカー出力と詳細ログを確認し、チェッカーの実行や出力の設定を見直してください。"],
  output_error: ["チェッカー出力や指摘先のファイルを確認できませんでした。",
    "詳細ログで読み込めなかったファイルを確認し、出力やファイルの状態を確認してください。"],
  unchanged: ["Junie が移行対象のファイルを変更しなかったため、自動修正を止めました。"],
  repair_incomplete: ["Junie の修正完了を確認できなかったため、自動修正を止めました。"],
  unexpected_changes: ["Junie が修正対象外のファイルを変更したため、修正の反映を止めました。",
    "詳細ログを確認し、修正対象と変更内容を指定して「指示を送って再開」を選んでください。"],
  repair_interrupted: ["Junie の修正が途中で停止しました。"],
  junie_unavailable: ["Junie を起動できなかったか、実行中に接続やセッションが終了しました。",
    "詳細ログで Junie の起動や接続の状態を確認し、復旧後に「同じ実行を再開」を選んでください。"],
  checker_incomplete: ["チェッカーが正常に完了しなかったため、自動修正に進めませんでした。",
    "元のチェッカー出力と詳細ログで実行環境や設定を確認し、原因を直してから再開してください。"],
  checker_interrupted: ["チェッカーが途中で停止したため、自動修正に進めませんでした。",
    "出力や停止時の状況を確認し、検査を続ける場合は「同じ実行を再開」を選んでください。"],
  repair_error: ["自動修正の途中でエラーが発生しました。"],
  review_required: ["チェッカーの指摘が残っているため、確認が必要です。"],
};
const checkerLegacyReasons = [
  [/a previous checker finding is still present/, "finding_repeated"],
  [/automatic repair limit reached/, "repair_limit"],
  [/outside the configured migration targets/, "outside_targets"],
  [/too (?:large|much text) for automatic repair/, "output_too_large"],
  [/unrecognized checker output encoding/, "output_encoding"],
  [/no readable diagnostic|no supported findings/, "output_unreadable"],
  [/Junie left the repair targets unchanged/, "unchanged"],
  [/Junie did not record a completed repair/, "repair_incomplete"],
  [/Junie changed files outside the repair targets/, "unexpected_changes"],
  [/repair interrupted|Junie による修正を停止/, "repair_interrupted"],
  [/Junie(?:Launch|Session|Connection)Error:/, "junie_unavailable"],
  [/checker interrupted by user/, "checker_interrupted"],
  [/checker did not complete normally/, "checker_incomplete"],
];

function checkerReviewCopy(data) {
  const legacy = checkerLegacyReasons.find(([pattern]) => pattern.test(data.error || ""))?.[1];
  const fallback = data.status === "checker_failed" ? "checker_incomplete" : "review_required";
  const [reason, next] = checkerReviewGuidance[data.checker_review_reason || legacy] || checkerReviewGuidance[fallback];
  const action = !data.resumable ? "チェッカーの設定と詳細ログを確認してください。"
    : next || "チェッカー出力を確認し、修正方針を「Junie への追加指示」に入力して「指示を送って再開」を選ぶか、「エディタで手修正」を選んでください。";
  return {reason, action};
}

function renderCheckerStatus(data) {
  if (!data.ready) return;
  const {reason, action} = checkerReviewCopy(data);
  element("#checker-review-reason").textContent = reason;
  element("#checker-review-next").textContent = action;
  const diagnostics = [...new Set([data.error, data.review_error, data.error_details].filter(Boolean))];
  if (Number.isInteger(data.checker_exit_code)) diagnostics.unshift(`チェッカーの終了コード ${data.checker_exit_code}`);
  element("#checker-review-technical").textContent = diagnostics.join("\n\n") || "詳細ログはありません。";
}

function progressFeedback(data) {
  if (data.ready && checkerReviewStatuses.has(data.status)) {
    return data.message?.split("\n", 1)[0] || checkerReviewCopy(data).reason;
  }
  return data.message || (data.ready ? data.error || data.review_error : "") || "";
}

function renderCheckerReview(data, showReview) {
  const items = data.review?.findings || [];
  const reviewKey = JSON.stringify([data.run_id, data.review, data.review_error, data.checker_log_path]);
  if (reviewKey !== renderedReview) {
    renderedReview = reviewKey;
    const pendingCount = items.filter(item => !item[2]).length;
    element("#pending-count").textContent = String(pendingCount);
    pendingFilter.classList.toggle("has-pending", pendingCount > 0);
    const previousId = findings.value;
    findings.replaceChildren();
    ledger.replaceChildren();
    for (const [index, [id, text, accepted]] of items.entries()) {
      findings.add(new Option(`${accepted ? "記録済み · " : ""}${text}`, id));
      const row = document.createElement("li");
      row.className = "ledger-row";
      row.dataset.status = accepted ? "done" : "judgment";
      const button = document.createElement("button");
      button.className = "ledger-name ledger-review-trigger";
      button.type = "button";
      button.textContent = text;
      button.setAttribute("aria-haspopup", "dialog");
      row.addEventListener("click", () => openReview(index));
      const label = document.createElement("span");
      label.className = "ledger-state";
      label.textContent = accepted ? "記録済み" : "判断待ち";
      row.append(button, label);
      ledger.append(row);
    }
    emptyFinding.textContent = showReview && !items.length ? "ログで確認が必要です。" : "表示する指摘はありません。";
    ledger.append(emptyFinding);
    applyFindingFilter();
    findingSection.hidden = !items.length;
    reviewNoteLabel.hidden = note.hidden = accept.hidden = !items.length;
    reviewFiles.textContent = data.checker_log_path ? `チェッカーログ：${data.checker_log_path}` : "";
    const selected = [...findings.options].findIndex(option => option.value === previousId);
    const nextPending = items.findIndex(item => !item[2]);
    if (data.message === "判断理由を記録しました。" && nextPending !== -1) findings.selectedIndex = nextPending;
    else if (selected !== -1) findings.selectedIndex = selected;
    renderSelectedFinding();
  }
  if (showReview) renderCheckerStatus(data);
  showNotice(showReview ? data.message?.split("\n", 1)[0] : data.message);
  if (data.junie_request) return;
  if (showReview && data.ready && !prompted && activeHarnessTab === 0 && !progressPrompt.dialog.open) {
    prompted = true;
    openReview();
  } else if (!showReview) {
    prompted = false;
    if (dialog.open) dialog.close();
  }
}

function renderTokens() {
  const data = snapshot;
  const usingJunie = !data.ready && (["junie_running", "editor_followup", "repairing"].includes(data.status) || data.generating_anchors);
  renderRunTokenSummary(data.token_usage, usingJunie);
  if (element("#tab-tokens").getAttribute("aria-selected") === "true") {
    tokenRuns.setActive(data.run_id ? {
      run_id: data.run_id, unit_id: data.unit_id, created_at: data.created_at,
      token_usage: data.token_usage, running: Boolean(usingJunie),
    } : undefined);
  }
}

// 最新のSSE状態を工程・入力・指摘に反映し、確認待ちを案内する。
function render() {
  const data = snapshot;
  renderTokens();
  document.documentElement.toggleAttribute("data-viewer-enabled", !!(data.worktree_path && data.diff_viewer_enabled));
  const showReview = checkerReviewStatuses.has(data.status)
    && (data.ready || data.generating_anchors || dialog.open);
  const showBuildReview = data.ready && data.status === "build_failed";
  if (!showBuildReview && buildDialog.open) buildDialog.close();
  if (data.ready && buildEditSubmitted) {
    buildEditSubmitted = false;
    if (!data.error_details && !data.message) buildFile = undefined;
  }
  const waitingForJunie = !!data.junie_request;
  const verificationFinished = data.ready && verifiedStatuses.has(data.status);
  const stopped = data.ready && !verificationFinished && (data.resumable || showReview || showBuildReview
    || ["checker_failed", "script_failed", "interrupted", "junie_failed", "preparation_interrupted", "preparation_failed"].includes(data.status));
  const done = verificationFinished && migrationCompletion?.complete;
  element(".demo-badge").textContent = savedView && data.ready ? "保存済みの実行" : "実行中のデータ";
  element(".run-name").textContent = data.unit_id ? `${data.unit_id} の移行` : data.status === "idle" ? "移行ハーネス" : "移行を準備中";
  element(".run-detail").textContent = data.run_id ? `Run ID ${data.run_id} · ${data.target || ""}` : "";
  element("#workspace-progress-feedback").textContent = progressFeedback(data);
  element(".run-detail").title = data.worktree_path || "";
  let label = describe(data);
  if (verificationFinished && migrationCompletion) {
    label = done ? "移行完了" : migrationCompletion.verification_passed ? "移行台帳の確認待ち" : "ビルド・チェックの再確認が必要";
  }
  element("#state-label").textContent = data.stop_requested ? "停止待ち" : waitingForJunie ? "Junie の確認待ち" : label;
  element("#run-state").title = element("#state-label").textContent;
  const state = element("#run-state");
  state.classList.toggle("is-running", !data.ready);
  element("#flow").classList.toggle("is-paused", !!data.ready);
  state.dataset.state = done ? "complete" : showReview || waitingForJunie || verificationFinished ? "waiting" : stopped ? "paused" : "running";
  reviewButton.textContent = waitingForJunie ? "Junie の確認を開く" : showBuildReview ? "ビルド失敗を確認"
    : showReview ? "チェッカー出力を確認" : verificationFinished ? "ビルド・チェックを再実行" : "同じ実行を再開";
  if (showReview || showBuildReview || waitingForJunie) reviewButton.setAttribute("aria-haspopup", "dialog");
  else reviewButton.removeAttribute("aria-haspopup");
  if (showReview && data.status === "checker_needs_review" && !waitingForJunie && !data.stop_requested) {
    reviewButton.setAttribute("aria-controls", "review-dialog");
  } else {
    reviewButton.removeAttribute("aria-controls");
  }
  reviewButton.hidden = !waitingForJunie && !showReview && !showBuildReview
    && !((data.ready || data.generating_anchors) && data.resumable);
  const current = verificationFinished ? 6 : data.status === "checker_needs_review" && !showReview ? 5
    : data.active_stage ?? scriptStages[data.script_phase] ?? statusStages[data.status] ?? 0;
  const total = data.context_capsules ?? 0;
  const completed = data.completed_capsules ?? 0;
  updateMigrationLedgerStatuses(data);
  element("#step-count").textContent = `${completed} / ${total || "—"} カプセル`;
  steps.forEach((item, index) => {
    const finished = index < current || (index === current && !!done);
    item.classList.toggle("is-done", finished);
    item.classList.toggle("is-active", index === current && !finished && !done && (!stopped || showReview));
    item.querySelector(".node").textContent = finished ? "✓" : String(index + 1);
    const skipped = data.status === "build_succeeded" && index === 5 && migrationCompletion?.verification_passed;
    item.querySelector(".step-status").textContent = skipped ? "省略" : finished ? "完了" : index === current
      ? stopped && !showReview ? "停止中" : data.ready ? "確認待ち" : "処理中" : "待機中";
  });
  renderCapsuleProgress(data, total, completed);
  renderJunieReview(data.junie_request);
  renderCheckerReview(data, showReview);
  syncActions();
  renderElapsed();
}

// 記録済みの時間に実行中の区間だけ加算し、再開までの待ち時間は含めない。
function renderElapsed() {
  let total = 0;
  let measured = false;
  const hasTimings = Object.keys(snapshot.stage_timings || {}).length > 0;
  steps.forEach((step, index) => {
    const timing = snapshot.stage_timings?.[String(index)];
    const time = step.querySelector(".step-elapsed");
    if (!Number.isFinite(timing?.elapsed_seconds)) {
      setElapsed(time, null, hasTimings && index !== 6 ? "--:--:--" : "未計測");
      return;
    }
    let seconds = Math.max(0, timing.elapsed_seconds);
    const end = !snapshot.ready && snapshot.active_stage === index
      ? connected ? Date.now() / 1000 : snapshot.observed_at : snapshot.timing_updated_at;
    if (Number.isFinite(timing.started_at) && Number.isFinite(end)) {
      seconds += Math.max(0, end - timing.started_at);
    }
    total += seconds;
    measured = true;
    setElapsed(time, seconds);
  });
  setElapsed(element("#elapsed"), measured ? total : null, "未計測", element("#elapsed-value"));
}

function setElapsed(time, seconds, missing = "未計測", label = time) {
  const text = seconds === null ? missing : formatDuration(seconds * 1000);
  if (label.textContent !== text) label.textContent = text;
  if (seconds === null) time.removeAttribute("datetime");
  else time.dateTime = `PT${Math.floor(seconds)}S`;
}

// 同じログの連続表示を抑え、画面に直近100件を残す。
function addEvent(data) {
  if (savedView && !data.ready && !activity.childElementCount) return;
  const title = describe(data);
  const detail = [progressFeedback(data)];
  if (data.ready) {
    const logs = [];
    for (const [label, code, log] of [
      ["ビルド", data.build_exit_code, data.build_log_path],
      ["チェッカー", data.checker_exit_code, data.checker_log_path],
    ]) {
      if (Number.isInteger(code)) detail.push(`${label}の終了コード ${code}`);
      if (log) logs.push(`${label}ログ ${log}`);
    }
    for (const [phase, path] of Object.entries(data.script_logs || {})) {
      logs.push(`${scriptPhases[phase]}のスクリプトログ ${path}`);
    }
    detail.push(...logs);
  }
  const key = [title, ...detail].join("\n");
  if (key === previousLog) return;
  previousLog = key;
  appendEvent(data.observed_at, title, detail);
}

function addActivity(data) {
  const event = data.activity;
  if (!event || !Number.isFinite(event.sequence) || typeof event.message !== "string") return;
  const key = JSON.stringify([data.run_id, event.sequence, event.at]);
  if (seenActivities.has(key)) return;
  seenActivities.add(key);
  if (seenActivities.size > 100) seenActivities.delete(seenActivities.values().next().value);
  appendEvent(event.at, event.kind === "heartbeat" ? "稼働確認" : "実行状況", [event.message], "activity");
}

function appendEvent(at, title, detail, kind = "state") {
  const follow = activity.scrollHeight - activity.scrollTop - activity.clientHeight < 32;
  const row = document.createElement("div");
  row.className = "event";
  row.dataset.eventKind = kind;
  const time = document.createElement("time");
  time.textContent = Number.isFinite(at) ? new Date(at * 1000).toLocaleTimeString("ja-JP") : "—";
  const pin = document.createElement("span");
  pin.className = "event-pin";
  pin.setAttribute("aria-hidden", "true");
  const text = document.createElement("div");
  text.className = "event-text";
  const strong = document.createElement("strong");
  strong.textContent = title;
  text.append(strong);
  for (const line of detail) if (line) text.append(document.createElement("br"), line);
  row.append(time, pin, text);
  activity.append(row);
  if (activity.childElementCount > 100) activity.firstElementChild.remove();
  if (follow) activity.scrollTop = savedView && activity.childElementCount === 1 ? 0 : activity.scrollHeight;
}

// 受信済みの連番を付けて利用者の操作を端末へ送る。
async function send(action, fields = {finding_id: findings.value, reason: note.value.trim()}) {
  if (submitting) return;
  if (action === "stop") {
    if (!connected || !snapshot.cancellable || snapshot.stop_requested || stopPendingOperation !== undefined) return;
    stopPendingOperation = snapshot.operation_id;
  }
  submitting = true;
  syncActions();
  try {
    const response = await fetch(action === "build-response" ? "build-response" : "control", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(action === "stop"
        ? {action, operation_id: snapshot.operation_id, revision: dashboardRevision}
        : {action, revision: dashboardRevision, ...fields}),
    });
    if (!response.ok) throw await responseError(response);
    return true;
  } catch (error) {
    if (action === "stop") stopPendingOperation = undefined;
    const message = `操作を送信できませんでした。接続を確認して、もう一度お試しください。\n${error.message}`;
    if (action === "build-response" && buildDialog.open) buildFeedback.textContent = message;
    else if (dialog.open) showNotice(message);
    else element("#workspace-progress-feedback").textContent = message;
  } finally {
    submitting = false;
    syncActions();
  }
}

findings.addEventListener("change", () => {
  note.value = "";
  renderSelectedFinding();
  syncActions();
});
allFilter.addEventListener("click", () => {
  pendingOnly = false;
  applyFindingFilter();
});
pendingFilter.addEventListener("click", () => {
  pendingOnly = !pendingOnly;
  applyFindingFilter();
});
note.addEventListener("input", syncActions);
accept.addEventListener("click", () => send("accept"));
checkerInstruction.addEventListener("input", syncActions);
resume.addEventListener("click", async () => {
  const text = checkerInstruction.value.trim();
  if (await send("resume", text ? {checker_instruction: text} : {})) {
    checkerInstruction.value = "";
    syncActions();
  }
});
element("#checker-open-editor").addEventListener("click", () => {
  dialog.close();
  switchHarnessTab(harnessTabs.indexOf(element("#tab-editor")), true);
});
stopButton.addEventListener("click", () => send("stop"));

function comparisonPath(runId, snapshotId) {
  return `review/${runId}/${snapshotId}`;
}

// 比較の進捗と保存リンクを上部と確認モーダルに揃えて表示する。
function showComparisonStatus(...content) {
  snapshotStatus.replaceChildren(...content);
  modalComparisonStatus.replaceChildren(...Array.from(snapshotStatus.childNodes, node => node.cloneNode(true)));
  modalComparisonStatus.hidden = !content.length;
}

// ブラウザーが操作を許すクリック直後に比較先のタブを確保する。
function reserveComparisonTab(progress) {
  try {
    const popup = window.open("about:blank", "_blank");
    if (!popup || popup.closed) return null;
    popup.opener = null;
    popup.document.title = "差分比較の準備中";
    popup.document.body.textContent = progress;
    return popup;
  } catch {
    return null;
  }
}

// 確保したタブを使い、利用できない環境では画面内で比較を開く。
function displayComparison(url, popup) {
  try {
    if (popup && !popup.closed) {
      popup.location.replace(url);
      return;
    }
  } catch {
    // アプリ内ブラウザーでは別タブのハンドルが使えないことがある。
  }
  const frame = document.createElement("iframe");
  frame.title = "Diff Viewer";
  frame.setAttribute("sandbox", "allow-scripts allow-downloads");
  frame.src = url;
  viewerPreviewBody.replaceChildren(frame);
  viewerPreview.showModal();
}

// 現在の指摘を基準にスナップショットを作り、Viewerで開けるようにする。
async function openSnapshot({withAnchors = false, fromReview = false} = {}) {
  if (snapshotting) return;
  const sourceRunId = snapshot.run_id;
  const index = fromReview ? findings.selectedIndex : snapshot.review?.findings.findIndex(item => !item[2]);
  const reported = snapshot.status === "checker_needs_review" ? snapshot.review?.locations?.[index] : null;
  const line = Number(reported?.line);
  const focus = Number.isSafeInteger(line) && line > 0 ? {path: reported.path, line} : undefined;
  const progress = withAnchors
    ? "Junieでアンカーを生成中です。確認が必要な場合は、進捗画面のダイアログで回答してください。"
    : "現在の参照PHPと最新の保存済み移行先を取得中";
  const popup = reserveComparisonTab(progress);
  snapshotting = true;
  syncActions();
  showComparisonStatus(progress);
  try {
    const request = withAnchors ? {action: "anchors", revision: dashboardRevision, focus} : {run_id: sourceRunId, focus};
    const response = await fetch(withAnchors ? "control" : "comparison", request ? {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(request),
    } : {method: "POST"});
    if (!response.ok) throw new Error(await response.text());
    const {run_id: runId, id, count} = await response.json();
    if (snapshot.run_id !== sourceRunId) {
      popup?.close();
      return;
    }
    const path = comparisonPath(runId, id);
    const open = document.createElement("a");
    open.href = `${path}.html`;
    open.target = "_blank";
    open.rel = "noopener";
    open.textContent = withAnchors && count ? `今回の比較を開く（対応行の候補 ${count}件）` : "今回の比較を開く";
    const download = document.createElement("a");
    download.href = `${path}.json`;
    download.download = "";
    download.textContent = "ワークスペースJSONを保存";
    showComparisonStatus(...(withAnchors && count === 0 ? ["対応行の候補はありませんでした。 "] : []),
      ...(!withAnchors ? ["現在の参照PHPと最新の移行先（保存済みの編集） · "] : []), open, " · ", download);
    displayComparison(open.href, popup);
    if (withAnchors && migrationLedgerDialog.open) {
      await loadMigrationLedger();
      if (count === 0) {
        migrationLedgerMessage.textContent = "対応行の候補はありませんでした。";
        migrationLedgerMessage.hidden = false;
      }
    }
  } catch (error) {
    popup?.close();
    const message = `比較を開けませんでした。${error.message}`;
    showComparisonStatus(message);
    if (migrationLedgerDialog.open) {
      migrationLedgerMessage.textContent = message;
      migrationLedgerMessage.hidden = false;
    }
  } finally {
    snapshotting = false;
    syncActions();
  }
}

viewer.addEventListener("click", () => openSnapshot({fromReview: true}));
dashboardViewer.addEventListener("click", () => openSnapshot());
element("#migration-ledger-compare").addEventListener("click", () => openSnapshot());
reviewButton.addEventListener("click", () => {
  if (snapshot.junie_request) openJunieReview();
  else if (snapshot.status === "build_failed") openBuildReview();
  else if (checkerReviewStatuses.has(snapshot.status)) openReview();
  else send("resume");
});
element("#build-review-close").addEventListener("click", () => buildDialog.close());
buildTarget.addEventListener("change", loadBuildSource);
buildReset.addEventListener("click", () => {
  loadBuildSource();
  buildTarget.focus();
});
buildSource.addEventListener("input", syncActions);
buildInstruction.addEventListener("input", syncActions);
element("#build-review-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (buildSubmit.disabled) return;
  const edit = buildEditIsDirty() ? {
    target: buildFile.target, version: buildFile.version, content: buildSource.value,
  } : null;
  buildEditSubmitted = !!edit;
  if (await send("build-response", {instruction: buildInstruction.value.trim(), edit})) {
    if (buildDialog.open) buildDialog.close();
  } else {
    buildEditSubmitted = false;
  }
});
for (const selector of ["#review-close", "#review-later"]) element(selector).addEventListener("click", () => dialog.close());
const stream = new EventSource("events");
stream.onopen = () => {
  connected = true;
  connection.textContent = "";
  syncActions();
};
stream.onerror = () => {
  connected = false;
  connection.textContent = "接続を再試行中";
  syncActions();
};
stream.onmessage = event => {
  const data = JSON.parse(event.data);
  // 時間・使用量・稼働通知だけの更新は、台帳や入力中の一覧を作り直さない。
  const {token_usage: previousUsage, observed_at: previousTime, stage_timings: previousTimings,
    timing_updated_at: previousTimingTime, activity: previousActivity, ...previous} = snapshot;
  const {token_usage: usage, observed_at: time, stage_timings: timings,
    timing_updated_at: timingTime, activity: currentActivity, ...current} = data;
  const progressChanged = JSON.stringify(previous) !== JSON.stringify(current);
  const accepted = data.message === "判断理由を記録しました。"
    && snapshot.review?.findings.some(item => item[0] === findings.value && !item[2])
    && data.review?.findings.some(item => item[0] === findings.value && item[2]);
  if (snapshot.run_id !== data.run_id) {
    checkerOutputRequest++;
    checkerDetails.open = false;
    checkerInstruction.value = "";
    dialog.close();
    promptRequest++;
    progressPrompts = undefined;
    progressPrompt.input.value = "";
    progressPrompt.dialog.close();
    showComparisonStatus();
    activity.replaceChildren();
    seenActivities.clear();
    previousLog = "";
  }
  snapshot = data;
  if (progressChanged) {
    migrationLedgerRequest++;
    migrationCompletion = undefined;
  }
  dashboardRevision = Number(event.lastEventId);
  if (snapshot.ready || snapshot.operation_id !== stopPendingOperation) stopPendingOperation = undefined;
  if (accepted) note.value = "";
  if (progressChanged) {
    addEvent(snapshot);
    render();
  } else {
    if (JSON.stringify(previousUsage) !== JSON.stringify(usage)) renderTokens();
    renderElapsed();
  }
  addActivity(snapshot);
  if (progressChanged && snapshot.worktree_path && (snapshot.ready || migrationLedgerDialog.open)) loadMigrationLedger();
};
setInterval(renderElapsed, 1000);
setInterval(refreshProgressPrompts, 1000);
render();
document.documentElement.dataset.liveReady = "true";
