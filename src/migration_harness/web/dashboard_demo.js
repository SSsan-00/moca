"use strict";

window.harnessDemoTransport = (() => {
  const first = "run-2026-11-14-001";
  const target = "Pages/Orders/Index.cshtml.cs";
  const settings = createDemoSettings();
  const accepted = new Set();
  const ledgerReviews = new Map();
  const ledgerVersions = new Map();
  const recoverySelections = new Set();
  const sampleRecovery = new URL(location.href).searchParams.get("recovery-demo");
  const finding = "日付の表示形式が元のPHPと一致するか確認してください。";
  const exchange = {status: "available", rate: 150, date: "2026-11-14", source: "サンプル"};
  const usage = [{phase: "migration", capsule: 1, input: 18420, output: 3240,
    cache_read: 3200, cache_write: 480, responses: 1, cost_usd: 0.07, cost_known: 1}];
  let stream, revision = 0;
  const runs = createDemoRuns(first, settings.units, notify, () => settings.read().form.junie);

  function snapshot(run) {
    const review = run.status === "checker_needs_review" && !accepted.has(run.run_id);
    const stage = {preparing: 0, prepared: 2, junie_running: 3, building: 4, checking: 5}[run.status] ?? 6;
    return {...run, status: run.status === "checker_needs_review" && !review ? "checker_accepted" : run.status,
      operation_id: run.run_id + "-operation", observed_at: Date.now() / 1000,
      target, token_usage: run.tick > 3 ? usage : [], cost_exchange: exchange, diff_viewer_enabled: false,
      stage_timings: {[stage]: {elapsed_seconds: run.elapsed_seconds}}, build_resumable: !!(run.ready && run.worktree_path),
      ...(review ? {checker_exit_code: 0, checker_log_path: "demo/checker.log",
        review: {findings: [["format-date", finding, false]], locations: [{path: target, line: 10}]}} : {})};
  }

  function notify(run) {
    if (!stream?.opened || stream.runId !== run?.run_id) return;
    const data = run ? snapshot(run) : {status: "idle", ready: true};
    stream.onmessage?.({lastEventId: String(++revision), data: JSON.stringify(data)});
  }

  function events(path) {
    const requested = new URL(path, document.baseURI).searchParams.get("run_id");
    const subscription = {close() { if (stream === subscription) stream = null; }};
    stream = subscription;
    queueMicrotask(async () => {
      const {runs: visible} = await runs.read();
      if (stream !== subscription) return;
      if (requested && !visible.some(item => item.run_id === requested)) {
        subscription.readyState = EventSource.CLOSED;
        subscription.close();
        subscription.onerror?.();
        return;
      }
      const id = requested || runs.selected || visible[0]?.run_id;
      Object.assign(subscription, {runId: id, opened: true});
      subscription.onopen?.();
      if (id) {
        runs.select(id);
      } else {
        window.harnessEditorDemo?.select();
        notify();
      }
    });
    return subscription;
  }

  function resumePlan(run, stage) {
    if (!run?.ready || !run.worktree_path) throw Error("停止・中断した実行を選び直してください。");
    const completed = run.completed_capsules === run.context_capsules;
    if (stage === "migration" && completed) throw Error("未完了の移行がないため、開始工程を選び直してください。");
    if (stage === "build-through" && !completed) throw Error("移行が完了してからビルド工程を選んでください。");
    if (stage === "checker" && (!completed || run.tick < 25)) throw Error("照合できるビルド成果物がありません。ビルドから再開してください。");
    const stages = {migration: ["build", "checker"], "build-through": ["build", "checker"], checker: ["checker"]}[stage];
    if (!stages) throw Error("再開する工程を選び直してください。");
    return {version: ["sample", run.run_id, stage, run.status, run.tick].join(":"),
      targets: {[target]: "sample"}, capsule_range: stage === "migration" ? [run.completed_capsules + 1, run.context_capsules] : null,
      invalidates: stages, repair: false, ...(stage === "build-through" ? {stages} : {})};
  }

  const controls = new Set(["run", "stop", "cancel-queued", "hide-run", "resume-run"]);
  async function control(body, run) {
    const action = body.action === "resume" ? "resume-run" : body.action;
    if (action === "accept" && run?.status === "checker_needs_review" && body.finding_id === "format-date" && body.reason?.trim()) {
      accepted.add(run.run_id);
      runs.select(run.run_id);
    } else if (controls.has(action)) {
      if (body.start_stage && (action !== "resume-run" || body.plan_version !== resumePlan(run, body.start_stage).version)) {
        throw Error("保存版が更新されました。再開計画を確認し直してください。");
      }
      await runs.execute(action, body);
    } else {
      throw Error("この操作にはローカルのハーネスが必要です。デモでは実行しません。");
    }
    return {run_id: body.run_id};
  }

  function ledger(run) {
    const verified = ["checker_succeeded", "checker_accepted"].includes(snapshot(run).status);
    const capsules = Array.from({length: run.context_capsules}, (_, i) => ({index: i + 1,
      sources: [{id: (i + 1) + ":1", path: "php/orders.php", start_line: i * 2 + 1, end_line: i * 2 + 2,
        ...(ledgerReviews.get(`${run.run_id}:${i + 1}:1`) ? {review: ledgerReviews.get(`${run.run_id}:${i + 1}:1`)} : {})}]}));
    const {id: source_id, start_line, end_line} = capsules[0].sources[0];
    const reviewed = capsules.filter(capsule => ["confirmed", "excluded"].includes(capsule.sources[0].review?.state)).length;
    return {run_id: run.run_id, target, targets: [target, "Pages/Orders/Index.cshtml"],
      review_version: `sample-${ledgerVersions.get(run.run_id) || 0}`,
      completion: {complete: verified && reviewed === capsules.length, verification_passed: verified, reviewed, total: capsules.length},
      capsules,
      unresolved_junie_memos: verified ? [] : [{source_id, start_line, end_line, note: "日付の表示形式を原文と照合する。"}],
      unresolved_operator_notes: []};
  }

  const get = new Set(["workspace", "token-runs", "junie-models", "settings", "junie-requests", "junie-thoughts",
    "recovery", "recovery-plan", "recovery-file", "migration-ledger", "checker-output", "prompts", "resume-plan", "rule-review"]);
  const post = new Set(["settings", "control", "prompts", "migration-ledger", "recovery-plan"]);
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
        case "migration-ledger": {
          if (!body) return json(ledger(run));
          const item = ledger(run).capsules.flatMap(capsule => capsule.sources).find(source => source.id === body.range_id);
          if (!run.ready || !item || !["confirmed", "excluded", "needs_review"].includes(body.state) || !body.reason?.trim()) {
            throw Error("処理が終わってから、範囲と判断理由を確認してください。");
          }
          if (body.version !== ledger(run).review_version) throw Error("台帳が更新されました。確認し直してください。");
          if (body.state === "confirmed" && (Number(body.range_id.split(":")[0]) > run.completed_capsules || !body.targets?.length)) {
            throw Error("処理済みの範囲と確認した移行先を選択してください。");
          }
          ledgerReviews.set(`${id}:${body.range_id}`, {state: body.state, reason: body.reason,
            ...(body.state === "confirmed" ? {targets: body.targets} : {})});
          ledgerVersions.set(id, (ledgerVersions.get(id) || 0) + 1);
          return json({});
        }
        case "rule-review": return json({run_id: id, version: "sample", rules: []});
        case "recovery": return json({run_id: id, available: !!run.worktree_path && run.ready, stage: run.status,
          status: run.status, completed: run.completed_capsules, total: run.context_capsules,
          saved_targets: !!run.worktree_path, pending_edits: 0, pending_prompts: 0,
          ...(sampleRecovery === "draft" ? {draft: "repair", draft_selected: recoverySelections.has(id)} : {}),
          ...(sampleRecovery === "script" && !recoverySelections.has(id)
            ? {unknown_script: {phase: "before_migration", number: 1, log: "before_migration.log"}} : {})});
        case "recovery-plan": {
          const kind = body?.kind || query.get("kind");
          if (!run.ready || kind !== sampleRecovery) throw Error("復旧する状態を選び直してください。");
          if (body) {
            if (body.version !== "a".repeat(64) || (kind === "script" && !body.confirmed)
                || (kind === "draft" && !body.choices?.[target])) throw Error("復旧内容を確認してください。");
            recoverySelections.add(id);
            notify(run);
            return json({}, kind === "script" ? 202 : 200);
          }
          return json(kind === "draft" ? {version: "a".repeat(64), draft: {[target]: "sample"}, worktree: {[target]: "sample"}}
            : {version: "a".repeat(64), phase: "before_migration", number: 1, log: "before_migration.log",
              warning: "実行済みの可能性があります。ログ・対象と残存プロセスを確認してください。", can_retry: true});
        }
        case "recovery-file": return json({target, draft: {text: "return FormatDate(order.CreatedAt);\n", truncated: false},
          worktree: {text: "return order.CreatedAt.ToString();\n", truncated: false}});
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
        case "resume-plan": return json(resumePlan(run, query.get("start_stage")));
      }
    } catch (error) {
      return new Response(error.message, {status: 400});
    }
  }
  return {fetch: request, events};
})();
