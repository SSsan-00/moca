"use strict";

window.harnessDemoTransport = (() => {
  const first = "run-2026-11-14-001";
  const target = "Pages/Orders/Index.cshtml.cs";
  const settings = createDemoSettings();
  const accepted = new Set();
  const finding = "日付の表示形式が元のPHPと一致するか確認してください。";
  const usage = [{phase: "migration", capsule: 1, input: 18420, output: 3240, responses: 1}];
  let stream, revision = 0;
  const runs = createDemoRuns(first, settings.units, notify, () => settings.read().form.junie);

  function snapshot(run) {
    const review = run.status === "checker_needs_review" && !accepted.has(run.run_id);
    const stage = {preparing: 0, prepared: 2, junie_running: 3, building: 4, checking: 5}[run.status] ?? 6;
    return {...run, status: run.status === "checker_needs_review" && !review ? "checker_accepted" : run.status,
      operation_id: run.run_id + "-operation", observed_at: Date.now() / 1000,
      target, token_usage: run.tick > 3 ? usage : [], diff_viewer_enabled: false,
      stage_timings: {[stage]: {elapsed_seconds: run.elapsed_seconds}}, build_resumable: !!(run.ready && run.worktree_path),
      ...(review ? {checker_exit_code: 0, checker_log_path: "demo/checker.log",
        review: {findings: [["format-date", finding, false]], locations: [{path: target, line: 10}]}} : {})};
  }

  function notify(run) {
    if (!stream || (stream.runId && stream.runId !== run.run_id)) return;
    stream.onmessage?.({lastEventId: String(++revision), data: JSON.stringify(snapshot(run))});
  }

  function events(path) {
    const id = new URL(path, document.baseURI).searchParams.get("run_id") || first;
    const subscription = {runId: id, close() { if (stream === subscription) stream = null; }};
    stream = subscription;
    queueMicrotask(async () => {
      const {runs: visible} = await runs.read();
      if (stream !== subscription) return;
      if (!visible.some(item => item.run_id === id)) {
        subscription.readyState = EventSource.CLOSED;
        subscription.onerror?.();
        return;
      }
      subscription.onopen?.();
      runs.select(id);
    });
    return subscription;
  }

  const controls = new Set(["run", "prepare", "stop", "cancel-queued", "hide-run", "resume-run"]);
  async function control(body, run) {
    const action = body.action === "resume" ? "resume-run" : body.action;
    if (action === "accept" && run?.status === "checker_needs_review" && body.finding_id === "format-date" && body.reason?.trim()) {
      accepted.add(run.run_id);
      runs.select(run.run_id);
    } else if (controls.has(action)) {
      if (body.start_stage) throw Error("工程指定での再開はローカルのハーネスで実行してください。");
      await runs.execute(action, body);
    } else {
      throw Error("この操作にはローカルのハーネスが必要です。デモでは実行しません。");
    }
    return {run_id: body.run_id};
  }

  function ledger(run) {
    const verified = ["checker_succeeded", "checker_accepted"].includes(snapshot(run).status);
    return {run_id: run.run_id, target, targets: [target, "Pages/Orders/Index.cshtml"],
      completion: {complete: false, verification_passed: verified, reviewed: 0, total: run.context_capsules},
      capsules: Array.from({length: run.context_capsules}, (_, i) => ({index: i + 1,
        sources: [{id: (i + 1) + ":1", path: "php/orders.php", start_line: i * 2 + 1, end_line: i * 2 + 2}]})),
      unresolved_junie_memos: verified ? [] : [{text: "日付の表示形式を原文と照合する。"}], unresolved_operator_notes: []};
  }

  const get = new Set(["workspace", "token-runs", "junie-models", "settings", "junie-requests", "junie-thoughts",
    "recovery", "migration-ledger", "checker-output", "prompts", "resume-plan", "rule-review"]);
  const post = new Set(["settings", "control", "prompts"]);
  const json = (value, status = 200) => new Response(JSON.stringify(value), {status, headers: {"Content-Type": "application/json"}});
  async function request(path, init = {}) {
    const url = new URL(path, document.baseURI);
    const base = new URL("./", document.baseURI);
    const route = url.pathname.slice(base.pathname.length);
    const method = init.method || "GET";
    const allowed = method === "GET" ? get : method === "POST" ? post : null;
    if (url.origin !== base.origin || !url.pathname.startsWith(base.pathname) || !allowed?.has(route)) {
      return new Response("この操作はローカルのハーネスでのみ実行できます。", {status: 404});
    }
    try {
      const body = init.body ? JSON.parse(init.body) : null;
      const query = url.searchParams;
      const workspace = await runs.read();
      const id = body?.run_id || query.get("run_id") || new Headers(init.headers).get("X-Run-ID") || runs.selected;
      const run = workspace.runs.find(item => item.run_id === id);
      switch (route) {
        case "workspace": return json({...workspace, runs: workspace.runs.map(snapshot)});
        case "token-runs": return json({runs: workspace.runs.map(snapshot)});
        case "settings": return json(!body ? settings.read() : body.mode === "preview" ? settings.preview(body.content, body.changes) : settings.save(body));
        case "junie-models": return json({models: [{id: "junie-lite", name: "Junie Lite（サンプル）", efforts: ["none"]},
          {id: "gpt-6-luna", name: "GPT-6 Luna（サンプル）", efforts: ["low", "medium", "high"]}]});
        case "junie-requests": return json([]);
        case "control": return json(await control(body, run));
      }
      if (!run) return new Response("サンプルのRunが見つかりません。", {status: 404});
      switch (route) {
        case "migration-ledger": return json(ledger(run));
        case "rule-review": return json({run_id: id, version: "sample", rules: []});
        case "recovery": return json({run_id: id, available: !!run.worktree_path && run.ready, stage: run.status,
          status: run.status, completed: run.completed_capsules, total: run.context_capsules,
          saved_targets: !!run.worktree_path, pending_edits: 0, pending_prompts: 0});
        case "prompts": {
          if (run.run_id !== window.harnessEditorDemo.progress.run_id) throw Error("表示中のRunが変わりました。");
          const editor = await window.harnessEditorDemo.request("editor", body && {kind: "prompt", run_id: id, text: body.text});
          return json({run_id: id, timing: !editor.can_prompt ? null : editor.queued ? "checkpoint" : "resume", prompts: editor.prompts});
        }
        case "junie-thoughts": {
          const {log_language, log_tone} = settings.read().form.junie;
          return json({entries: Number(query.get("cursor")) ? [] : [
            {title: "入力を確認", thought: "移行元のPHPと仮C#を確認しました。", at: "2026-11-14T01:42:03Z"}],
            cursor: 1, live: !run.ready && !run.queued, more: false, log_language, log_tone});
        }
        case "checker-output": return json({output: finding, truncated: false});
        case "resume-plan": return json({version: "demo-checkpoint", targets: {[target]: "sample"}, edits: {},
          invalidates: ["build", "checker", "reviews"], repair: false});
      }
    } catch (error) {
      return new Response(error.message, {status: 400});
    }
  }
  return {fetch: request, events};
})();
