"use strict";

// ダッシュボードテンプレートの単一要素を取得する。
const element = selector => document.querySelector(selector);
const dialog = element("#review-dialog");
const note = element("#review-note");
const accept = element("#review-accept");
const resume = element("#review-fix");
const viewer = element("#open-diff-viewer");
const dashboardViewer = element(".runbar [data-compare]");
const reviewButton = element("#toggle");
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
for (const step of steps) step.querySelector(".step-elapsed").hidden = true;
const statusNames = {
  idle: "実行を選択してください", preparing: "準備中", prepared: "準備完了", junie_running: "Junieで移行中",
  junie_finished: "Junieの編集完了", repairing: "Junieで修正中", building: "ビルド中",
  build_succeeded: "ビルド成功", build_failed: "ビルド失敗", checking: "チェッカー実行中",
  checker_needs_review: "人の確認待ち", checker_failed: "チェッカー実行失敗",
  checker_succeeded: "チェッカー成功", checker_accepted: "チェッカーの指摘を承認済み",
  script_running: "スクリプト実行中", script_failed: "スクリプトで停止",
  interrupted: "中断", junie_failed: "Junie実行失敗",
};
const savedView = document.documentElement.hasAttribute("data-saved");
const verifiedStatuses = new Set(["checker_succeeded", "checker_accepted", "build_succeeded"]);
const stage = {
  preparing: 0, prepared: 2, junie_running: 3, junie_finished: 3, interrupted: 3, junie_failed: 3,
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
let rev = 0;
let submitting = false;
let connected = false;
let prompted = false;
let previousLog = "";
let lastObserved = 0;
let snapshotting = false;
let pendingOnly = false;

document.title = "移行ハーネスの進捗";
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
element(".review-eyebrow").hidden = true;
element(".review-intro").textContent = "指摘を選び、問題なしと判断した理由を記録してください。再開するとビルドとチェッカーを再実行します。";
element(".review-note-label").textContent = "判断理由（必須）";
note.placeholder = "例：共通関数へ委譲していることを差分で確認済み";
note.required = true;
note.maxLength = 2000;
element(".review-context").hidden = true;
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
element(".review-finding").replaceChildren(findings, findingLocation, diagnostic, reviewFiles);
const connection = document.createElement("span");
connection.setAttribute("role", "status");
element("#activity-title").after(connection);

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
  resume.disabled = !canAct || !snapshot.resumable;
  reviewButton.disabled = !canAct || !(canReview || snapshot.resumable);
  migrationLedgerButton.disabled = !connected || !snapshot.worktree_path;
  dashboardViewer.disabled = viewer.disabled = !connected || !snapshot.worktree_path || !snapshot.diff_viewer_enabled || snapshotting;
  element("#migration-ledger-compare").disabled = dashboardViewer.disabled;
  for (const button of anchorButtons) {
    button.disabled = dashboardViewer.disabled || !snapshot.ready || submitting;
  }
}

// 選んだ指摘の診断箇所と操作可否を同期する。
function selectFinding() {
  const selected = snapshot.review?.findings[findings.selectedIndex];
  diagnostic.textContent = selected?.[1] || snapshot.review_error || "選択できる指摘はありません。";
  const reported = snapshot.review?.locations?.[findings.selectedIndex];
  findingLocation.hidden = !reported || Number(reported.line) < 1;
  locationPath.textContent = reported?.path || "";
  locationLine.textContent = reported ? `チェッカー報告 · ${reported.line}行目` : "";
  syncActions();
}

// 選択した指摘を見せ、別の指摘の判断理由が残らないようにする。
function openReview(index = findings.selectedIndex) {
  if (index !== findings.selectedIndex) {
    findings.selectedIndex = index;
    note.value = "";
  }
  selectFinding();
  if (!dialog.open) {
    dialog.showModal();
    element("#review-title").focus();
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
  const requestedState = snapshot;
  const request = ++migrationLedgerRequest;
  try {
    const response = await fetch("migration-ledger", {cache: "no-store"});
    if (!response.ok) throw new Error();
    const ledger = await response.json();
    if (snapshot !== requestedState || request !== migrationLedgerRequest) return;
    migrationCompletion = ledger.completion;
    render();
    if (!migrationLedgerDialog.open) return;
    showMigrationLedger(ledger, snapshot, id => {
      const url = new URL(`${comparisonPath(ledger.run_id, id)}.html`, document.baseURI);
      displayComparison(url.href, reserveComparisonTab("比較を読み込み中"));
    });
  } catch {
    if (snapshot !== requestedState || request !== migrationLedgerRequest) return;
    migrationCompletion = undefined;
    render();
    if (!migrationLedgerDialog.open) return;
    migrationLedgerMessage.hidden = false;
    migrationLedgerMessage.textContent = "移行台帳を読み込めませんでした。画面を閉じてから、もう一度お試しください。";
  }
}

migrationLedgerButton.addEventListener("click", () => {
  openMigrationLedger("移行台帳を読み込み中");
  loadMigrationLedger();
});
window.addEventListener("focus", () => {
  if (snapshot.worktree_path && (snapshot.ready || migrationLedgerDialog.open)) loadMigrationLedger();
});

async function workspaceRequest(path, payload) {
  const message = "操作できませんでした。接続と入力内容を確認して、もう一度お試しください。";
  let response;
  try {
    response = await fetch(path, payload === undefined ? {cache: "no-store"} : {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
    });
  } catch {
    throw new Error(message);
  }
  if (!response.ok) {
    throw new Error(response.headers.get("Content-Type")?.startsWith("text/plain") ? await response.text() : message);
  }
  return response.json();
}

bindHarnessWorkspace({
  readRuns: () => workspaceRequest("workspace"),
  readSettings: () => workspaceRequest("settings"),
  previewSettings: (content, changes) => workspaceRequest("settings", {mode: "preview", content, changes}),
  saveSettings: (content, version) => workspaceRequest("settings", {content, version, revision: rev}),
  execute: (action, payload) => workspaceRequest("control", {action, revision: rev, ...payload}),
  canAct: () => connected && snapshot.ready && !submitting,
});
workspaceTabs.addEventListener("harnesstab", render);
if (document.documentElement.hasAttribute("data-workspace")) switchHarnessTab(1);

// 最新のSSE状態を工程・入力・指摘に反映し、確認待ちを案内する。
function render() {
  const data = snapshot;
  if (element("#tab-tokens").getAttribute("aria-selected") === "true") {
    const tokenPhases = {build_repair: "ビルドの自動修正", checker_repair: "チェッカーの自動修正", anchors: "アンカー生成"};
    const tokenRows = (data.token_usage || []).map(row => ({
      ...row, label: row.phase === "migration" ? `カプセル ${row.capsule}` : tokenPhases[row.phase],
    }));
    const usingJunie = !data.ready && (["junie_running", "repairing"].includes(data.status) || data.generating_anchors);
    renderTokenUsage(tokenRows, data.run_id ? `${data.unit_id} の移行 · Run ID ${data.run_id}` : "",
      usingJunie ? "Junieからの使用量の報告を待っています。" :
        data.run_id ? "このRunには使用量の記録がありません。" : "「実行」タブからRunを選択してください。", usingJunie);
  }
  document.documentElement.toggleAttribute("data-viewer-enabled", !!(data.worktree_path && data.diff_viewer_enabled));
  const showReview = data.status === "checker_needs_review" && (data.ready || data.generating_anchors || dialog.open);
  const verificationFinished = data.ready && verifiedStatuses.has(data.status);
  const stopped = data.ready && !verificationFinished
    && (data.resumable || showReview || ["checker_failed", "script_failed"].includes(data.status));
  const done = verificationFinished && migrationCompletion?.complete;
  element(".demo-badge").textContent = savedView && data.ready ? "保存済みの実行" : "実行中のデータ";
  element("#elapsed").style.display = savedView && data.ready ? "none" : "";
  element(".run-name").textContent = data.unit_id ? `${data.unit_id} の移行` : data.status === "idle" ? "移行ハーネス" : "移行を準備中";
  element(".run-detail").textContent = data.run_id ? `Run ID ${data.run_id} · ${data.target || ""}` : "";
  element("#workspace-progress-feedback").textContent = data.message || "";
  element(".run-detail").title = data.worktree_path || "";
  let label = describe(data);
  if (verificationFinished && migrationCompletion) {
    label = done ? "移行完了" : migrationCompletion.verification_passed ? "移行台帳の確認待ち" : "ビルド・チェックの再確認が必要";
  }
  element("#state-label").textContent = label;
  const state = element("#run-state");
  state.classList.toggle("is-running", !stopped && !verificationFinished);
  state.dataset.state = done ? "complete" : showReview || verificationFinished ? "waiting" : stopped ? "paused" : "running";
  reviewButton.textContent = showReview ? "判断が必要です" : verificationFinished ? "ビルド・チェックを再実行" : "同じ実行を再開";
  if (showReview) reviewButton.setAttribute("aria-haspopup", "dialog");
  else reviewButton.removeAttribute("aria-haspopup");
  reviewButton.hidden = !showReview && !((data.ready || data.generating_anchors) && data.resumable);
  const current = verificationFinished ? 6 : data.status === "checker_needs_review" && !showReview ? 5
    : scriptStages[data.script_phase] ?? stage[data.status] ?? 0;
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
    item.querySelector(".step-status").textContent = skipped ? "省略" : finished ? "完了" : index === current ? (stopped || verificationFinished ? "確認待ち" : "処理中") : "待機中";
  });
  const index = Math.max(0, (data.current_capsule ?? 1) - 1);
  const chars = data.context_part_chars?.[index] ?? 0;
  const limit = data.max_context_chars ?? 0;
  const percent = limit ? Math.round(chars * 100 / limit) : 0;
  element("#capsule-call").textContent = total ? `カプセル ${index + 1}/${total}` : "入力を準備中";
  element(".capsule-top > span").textContent = limit ? `上限 ${limit.toLocaleString("ja-JP")}文字` : "上限 —";
  element("#char-count").textContent = chars.toLocaleString("ja-JP");
  element("#capsule-percent").textContent = `上限の${percent}%`;
  element("#capsule-meter").setAttribute("aria-valuemax", String(limit || 1));
  element("#capsule-meter").setAttribute("aria-valuenow", String(chars));
  element("#meter-fill").style.width = `${percent}%`;

  const items = data.review?.findings || [];
  element(".work-summary .metric-value").textContent = String(total);
  element("#checked-count").textContent = `${completed} / ${total}`;
  const pendingCount = items.filter(item => !item[2]).length;
  element("#pending-count").textContent = String(pendingCount);
  pendingFilter.classList.toggle("has-pending", pendingCount > 0);
  const previousText = findings.options[findings.selectedIndex]?.text;
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
  findings.hidden = !items.length;
  reviewFiles.textContent = data.checker_log_path ? `チェッカーログ：${data.checker_log_path}` : "";
  const selected = [...findings.options].findIndex(option => option.text === previousText);
  const nextPending = items.findIndex(item => !item[2]);
  if (data.message === "判断理由を記録しました。" && nextPending !== -1) findings.selectedIndex = nextPending;
  else if (selected !== -1) findings.selectedIndex = selected;
  selectFinding();
  showNotice(data.message || data.review_error || (data.review?.unrecognized ? "一覧に表示できない出力があります。実行ログを確認してください。" : ""));
  if (showReview && data.ready && !prompted && activeHarnessTab === 0) {
    prompted = true;
    openReview();
  } else if (!showReview) {
    prompted = false;
    if (dialog.open) dialog.close();
  }
  syncActions();
}

// 同じログの連続表示を抑え、画面に直近100件を残す。
function addEvent(data) {
  if (savedView && !data.ready && !activity.childElementCount) return;
  const title = describe(data);
  const detail = [data.message || (data.ready ? data.error || data.review_error : "")];
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
  const row = document.createElement("div");
  row.className = "event";
  const time = document.createElement("time");
  time.textContent = new Date(data.observed_at * 1000).toLocaleTimeString("ja-JP");
  const pin = document.createElement("span");
  pin.className = "event-pin";
  const text = document.createElement("div");
  text.className = "event-text";
  const strong = document.createElement("strong");
  strong.textContent = title;
  text.append(strong);
  for (const line of detail) if (line) text.append(document.createElement("br"), line);
  row.append(time, pin, text);
  activity.append(row);
  if (activity.childElementCount > 100) activity.firstElementChild.remove();
  activity.scrollTop = savedView && activity.childElementCount === 1 ? 0 : activity.scrollHeight;
}

// 受信済みの連番を付けて利用者の操作を端末へ送る。
async function send(action) {
  if (submitting) return;
  submitting = true;
  syncActions();
  try {
    const response = await fetch("control", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action, revision: rev, finding_id: findings.value, reason: note.value.trim()}),
    });
    if (!response.ok) throw new Error();
  } catch {
    submitting = false;
    syncActions();
    const message = "操作を送信できませんでした。接続と実行状態を確認してから、もう一度お試しください。";
    if (dialog.open) showNotice(message);
    else connection.textContent = message;
  }
}

findings.addEventListener("change", () => {
  note.value = "";
  selectFinding();
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
resume.addEventListener("click", () => send("resume"));

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
  const index = fromReview ? findings.selectedIndex : snapshot.review?.findings.findIndex(item => !item[2]);
  const reported = snapshot.status === "checker_needs_review" ? snapshot.review?.locations?.[index] : null;
  const line = Number(reported?.line);
  const focus = Number.isSafeInteger(line) && line > 0 ? {path: reported.path, line} : undefined;
  const progress = withAnchors
    ? "Junieでアンカーを生成中です。確認が表示されたら起動元の端末で操作してください。"
    : "スナップショットを取得中";
  const popup = reserveComparisonTab(progress);
  snapshotting = true;
  syncActions();
  showComparisonStatus(progress);
  try {
    const request = withAnchors ? {action: "anchors", revision: rev, focus} : focus ? {focus} : null;
    const response = await fetch(withAnchors ? "control" : "comparison", request ? {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(request),
    } : {method: "POST"});
    if (!response.ok) throw new Error(await response.text());
    const {run_id: runId, id, count} = await response.json();
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
    showComparisonStatus(...(withAnchors && count === 0 ? ["対応行の候補はありませんでした。 "] : []), open, " · ", download);
    download.click();
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
reviewButton.addEventListener("click", () => snapshot.status === "checker_needs_review" ? openReview() : send("resume"));
for (const selector of ["#review-close", "#review-later"]) element(selector).addEventListener("click", () => dialog.close());
element("#theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
  element("#theme-toggle").setAttribute("aria-label", root.dataset.theme === "dark" ? "ライトテーマに切り替え" : "ダークテーマに切り替え");
});
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
  const {token_usage: previousUsage, observed_at: previousTime, ...previous} = snapshot;
  const {token_usage: usage, observed_at: time, ...current} = data;
  const progressChanged = JSON.stringify(previous) !== JSON.stringify(current);
  snapshot = data;
  if (progressChanged) migrationCompletion = undefined;
  rev = Number(event.lastEventId);
  lastObserved = snapshot.observed_at * 1000;
  submitting = false;
  if (snapshot.message === "判断理由を記録しました。") note.value = "";
  addEvent(snapshot);
  render();
  if (progressChanged && snapshot.worktree_path && (snapshot.ready || migrationLedgerDialog.open)) loadMigrationLedger();
};
setInterval(() => {
  if (!snapshot.created_at) return;
  const start = Date.parse(snapshot.created_at);
  const end = snapshot.ready || !connected ? lastObserved : Date.now();
  element("#elapsed-value").textContent = formatDuration(Math.max(0, end - start));
}, 250);
render();
document.documentElement.dataset.liveReady = "true";
