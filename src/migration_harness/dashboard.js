"use strict";

const element = selector => document.querySelector(selector);
const dialog = element("#review-dialog");
const note = element("#review-note");
const accept = element("#review-accept");
const resume = element("#review-fix");
const reviewButton = element("#toggle");
const notice = element("#viewer-status");
const activity = element("#activity");
const ledger = element("#ledger-list");
const steps = [...document.querySelectorAll(".step")];
const statusNames = {
  preparing: "準備中", prepared: "準備完了", junie_running: "Junieで移行中",
  junie_finished: "移行完了", repairing: "Junieで修正中", building: "ビルド中",
  build_succeeded: "ビルド成功", build_failed: "ビルド失敗", checking: "チェッカー実行中",
  checker_needs_review: "人の確認待ち", checker_failed: "チェッカー実行失敗",
  checker_succeeded: "確認完了", checker_accepted: "人の判断を反映して完了",
  interrupted: "中断", junie_failed: "Junie実行失敗",
};
const resumable = new Set(["checker_needs_review", "checker_failed", "build_failed", "interrupted", "junie_failed"]);
const complete = new Set(["checker_succeeded", "checker_accepted", "build_succeeded"]);
const stage = {
  preparing: 0, prepared: 2, junie_running: 3, junie_finished: 3, interrupted: 3, junie_failed: 3,
  repairing: 3, building: 4, build_succeeded: 4, build_failed: 4, checking: 5, checker_failed: 5,
  checker_needs_review: 6, checker_succeeded: 6, checker_accepted: 6,
};
const archived = [];
let snapshot = {status: "preparing"};
let rev = 0;
let submitting = false;
let connected = false;
let prompted = false;
let previousLog = "";
let lastObserved = 0;

document.title = "移行ハーネスの進捗";
element(".brand").href = "./";
element(".top-links").hidden = true;
element(".demo-badge").textContent = "実行中のデータ";
element("#restart").hidden = true;
element(".controls").setAttribute("aria-label", "実行操作");
element(".ledger-controls").hidden = true;
element("#ledger-title").textContent = "チェッカーの指摘";
element("#activity").replaceChildren();
element("#ledger-list").replaceChildren();
element(".work-summary .metric-label").textContent = "カプセル総数";
element(".work-summary .metric:nth-child(2) .metric-label").textContent = "移行済み";
element(".review-eyebrow").hidden = true;
element(".review-intro").textContent = "指摘を選び、問題なしと判断した理由を記録してください。再開するとビルドとチェッカーを再実行します。";
element(".review-note-label").textContent = "判断理由（必須）";
note.placeholder = "例：共通関数へ委譲していることを差分で確認済み";
note.required = true;
note.maxLength = 2000;
element(".review-context").hidden = true;
element("#open-diff-viewer").hidden = true;
element("#review-later").hidden = true;
accept.textContent = "問題なしとして記録";
resume.textContent = "同じ実行を再開";
const findings = document.createElement("select");
findings.setAttribute("aria-label", "レビューする指摘");
const diagnostic = document.createElement("pre");
diagnostic.className = "review-code";
const reviewFiles = document.createElement("p");
reviewFiles.className = "review-caveat";
element(".review-finding").replaceChildren(findings, diagnostic, reviewFiles);
const connection = document.createElement("span");
connection.setAttribute("role", "status");
element("#activity-title").after(connection);

function showNotice(message) {
  notice.textContent = message;
  notice.hidden = !message;
}

function syncActions() {
  const canAct = connected && snapshot.ready && !submitting && resumable.has(snapshot.status);
  const selected = (snapshot.review?.findings || []).find(([id]) => id === findings.value);
  accept.disabled = !canAct || !selected || selected[2] || !note.value.trim();
  resume.disabled = !canAct;
  reviewButton.disabled = !canAct;
}

function selectFinding() {
  const selected = (snapshot.review?.findings || []).find(([id]) => id === findings.value);
  diagnostic.textContent = selected?.[1] || snapshot.review_error || "選択できる指摘はありません。";
  syncActions();
}

function openReview(id) {
  if (id) findings.value = id;
  selectFinding();
  if (!dialog.open) {
    dialog.showModal();
    element("#review-title").focus();
  }
}

function describe(state) {
  const label = statusNames[state.status] || state.status;
  if (state.status === "checker_needs_review" && !state.ready) return "チェッカー結果を処理中";
  if (state.status === "junie_running") return `${label} (${state.current_capsule ?? 1}/${state.context_capsules ?? "?"})`;
  if (state.status === "repairing") return `Junieで修正中（${state.repair_count}回目）`;
  return label;
}

function render() {
  const data = snapshot;
  const showReview = data.status === "checker_needs_review" && (data.ready || dialog.open);
  const done = data.ready && complete.has(data.status);
  const stopped = data.ready && resumable.has(data.status);
  element(".run-name").textContent = data.unit_id ? `${data.unit_id} の移行` : "移行を準備中";
  element(".run-detail").textContent = data.run_id ? `Run ID ${data.run_id} · ${data.target || ""}` : "実行情報を取得中";
  element(".run-detail").title = data.worktree_path || "";
  element("#state-label").textContent = describe(data);
  const state = element("#run-state");
  state.classList.toggle("is-running", !stopped && !done);
  state.dataset.state = done ? "complete" : stopped ? (showReview ? "waiting" : "paused") : "running";
  reviewButton.textContent = showReview ? "指摘を確認" : "同じ実行を再開";
  reviewButton.hidden = !stopped;
  const current = data.status === "checker_needs_review" && !showReview ? 5 : stage[data.status] ?? 0;
  const total = data.context_capsules ?? 0;
  const completed = data.completed_capsules ?? 0;
  element("#step-count").textContent = `${completed} / ${total || "—"} カプセル`;
  steps[4].querySelector("h3").textContent = "ビルド";
  steps[6].querySelector("h3").textContent = "人の確認";
  steps.forEach((item, index) => {
    const finished = index < current || (index === current && done);
    item.classList.toggle("is-done", finished);
    item.classList.toggle("is-active", index === current && !done && (!stopped || showReview));
    item.querySelector(".node").textContent = finished ? "✓" : String(index + 1);
    item.querySelector(".step-status").textContent = finished ? "完了" : index === current ? (stopped ? "確認待ち" : "処理中") : "待機中";
    item.querySelector(".step-elapsed").hidden = true;
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
  element("#pending-count").textContent = String(items.filter(item => !item[2]).length);
  const previousId = findings.value;
  findings.replaceChildren();
  ledger.replaceChildren();
  for (const [id, text, accepted] of items) {
    findings.add(new Option(`${accepted ? "記録済み · " : ""}${text}`, id));
    const row = document.createElement("li");
    row.className = "ledger-row";
    const button = document.createElement("button");
    button.className = "ledger-name ledger-review-trigger";
    button.type = "button";
    button.textContent = text;
    button.setAttribute("aria-haspopup", "dialog");
    button.addEventListener("click", () => openReview(id));
    const label = document.createElement("span");
    label.className = "ledger-state";
    label.textContent = accepted ? "記録済み" : "判断待ち";
    row.append(button, label);
    ledger.append(row);
  }
  if (!items.length) {
    const empty = document.createElement("li");
    empty.className = "ledger-row";
    empty.textContent = showReview ? "ログで確認が必要です。" : "確認待ちの指摘はありません。";
    ledger.append(empty);
  }
  findings.hidden = !items.length;
  reviewFiles.textContent = data.checker_log_path ? `チェッカーログ：${data.checker_log_path}` : "";
  const selected = items.find(([id]) => id === previousId);
  const nextPending = items.find(item => !item[2]);
  if (data.message === "判断理由を記録しました。" && nextPending) findings.value = nextPending[0];
  else if (selected) findings.value = selected[0];
  selectFinding();
  showNotice(data.message || data.review_error || (data.review?.unrecognized ? "一覧に表示できない出力があります。実行ログを確認してください。" : ""));
  if (showReview && data.ready && !prompted) {
    prompted = true;
    openReview();
  } else if (!showReview) {
    prompted = false;
    if (dialog.open) dialog.close();
  }
  syncActions();
}

function addEvent(data) {
  const title = describe(data);
  const detail = data.message || (data.ready ? data.error || data.review_error : "");
  const key = `${title}\n${detail || ""}`;
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
  if (detail) text.append(document.createElement("br"), detail);
  row.append(time, pin, text);
  activity.append(row);
  archived.push(row);
  if (archived.length > 100) archived.shift().remove();
  activity.scrollTop = activity.scrollHeight;
}

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
note.addEventListener("input", syncActions);
accept.addEventListener("click", () => send("accept"));
resume.addEventListener("click", () => send("resume"));
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
  snapshot = JSON.parse(event.data);
  rev = Number(event.lastEventId);
  lastObserved = snapshot.observed_at * 1000;
  submitting = false;
  if (snapshot.message === "判断理由を記録しました。") note.value = "";
  addEvent(snapshot);
  render();
};
setInterval(() => {
  if (!snapshot.created_at) return;
  const start = Date.parse(snapshot.created_at);
  const end = snapshot.ready || !connected ? lastObserved : Date.now();
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  element("#elapsed-value").textContent = [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
    .map(value => String(value).padStart(2, "0")).join(":");
}, 250);
render();
document.documentElement.dataset.liveReady = "true";
