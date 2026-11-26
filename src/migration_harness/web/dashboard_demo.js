"use strict";

window.harnessDemoTransport = (() => {
  const first = "run-2026-11-14-001";
  const target = "Pages/Orders/Index.cshtml.cs";
  const settings = createDemoSettings();
  const accepted = new Set();
  const draftRun = "demo-repair-recovery";
  const scriptRun = "demo-script-recovery";
  const requestedRecovery = new URL(location.href).searchParams.get("recovery-demo");
  const recoveryTargets = {
    [target]: {draft: "public int @event = 1;\n", worktree: "public int EventCode = 1;\n"},
    "Pages/Orders/Index.cshtml": {draft: "<p>@Model.EventCode</p>\n", worktree: "<p>注文履歴</p>\n"},
  };
  const scriptAttempt = {phase: "before_migration", number: 1, log: "before_migration.log"};
  const recovery = {choices: null, revision: 0, keywordBlocked: false};
  const finding = "日付の表示形式が元のPHPと一致するか確認してください。";
  const exchange = {status: "available", rate: 150, date: "2026-11-14", source: "サンプル"};
  const usage = [{phase: "migration", capsule: 1, input: 18420, output: 3240,
    cache_read: 3200, cache_write: 480, responses: 1, cost_usd: 0.07, cost_known: 1}];
  let stream, revision = 0;
  const initialRun = requestedRecovery === "draft" ? draftRun : requestedRecovery === "script" ? scriptRun : first;
  const runs = createDemoRuns(first, settings.units, notify, () => settings.read().form.junie, initialRun);

  const stoppedRepair = run => run.run_id === draftRun && run.status === "checker_needs_review" && run.ready;
  const interruptedScript = run => run.run_id === scriptRun && run.ready && run.status === "script_failed";

  function snapshot(run) {
    const review = run.status === "checker_needs_review" && !accepted.has(run.run_id);
    const stage = {preparing: 0, prepared: 2, junie_running: 3, interrupted: 3, script_failed: 3, script_running: 3,
      building: 4, checking: 5}[run.status] ?? 6;
    return {...run, status: run.status === "checker_needs_review" && !review ? "checker_accepted" : run.status,
      operation_id: run.run_id + "-operation", observed_at: Date.now() / 1000,
      target, token_usage: run.tick > 3 ? usage : [], cost_exchange: exchange, diff_viewer_enabled: false,
      stage_timings: {[stage]: {elapsed_seconds: run.elapsed_seconds}}, build_resumable: !!(run.ready && run.worktree_path),
      ...(stoppedRepair(run) ? {recovery_revision: recovery.revision,
        checker_review_reason: recovery.keywordBlocked ? "stopped_draft_keyword" : "repair_interrupted",
        message: recovery.keywordBlocked ? "Pages/Orders/Index.cshtml.cs:1: 予約語 @event が残っています。" : "修正の途中で停止しました。"} : {}),
      ...(interruptedScript(run) ? {script_phase: scriptAttempt.phase, message: "停止前に実行したスクリプトの結果を確認できません。"} : {}),
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
    if (interruptedScript(run)) throw Error("実行結果が不明なスクリプトを、進捗画面で先に確認してください。");
    const completed = run.completed_capsules === run.context_capsules;
    if (stage === "migration" && completed) throw Error("未完了の移行がないため、開始工程を選び直してください。");
    if (stage === "build-through" && !completed) throw Error("移行が完了してからビルド工程を選んでください。");
    if (stage === "checker" && (!completed || run.tick < 25)) throw Error("照合できるビルド成果物がありません。ビルドから再開してください。");
    const stages = {migration: ["build", "checker"], "build-through": ["build", "checker"], checker: ["checker"]}[stage];
    if (!stages) throw Error("再開する工程を選び直してください。");
    return {version: ["sample", run.run_id, stage, run.status, run.tick, stoppedRepair(run) ? recovery.revision : 0].join(":"),
      targets: {[target]: "sample"}, capsule_range: stage === "migration" ? [run.completed_capsules + 1, run.context_capsules] : null,
      invalidates: stages, repair: false, ...(stage === "build-through" ? {stages} : {})};
  }

  const controls = new Set(["run", "stop", "cancel-queued", "hide-run", "resume-run"]);
  const validations = new Map();
  function validate({unit_id, request_id}) {
    const {units, project} = settings.read().form;
    const unit = units.find(item => item.id === unit_id);
    if (!unit || !request_id || validations.has(request_id)) throw Error("移行単位を選び直してください。");
    const checks = [
      {phase: "移行元", state: unit.source ? "unchecked" : "error",
        detail: unit.source ? "デモに設定したパス" : "移行元が未設定です。", path: unit.source},
      {phase: "移行先", state: unit.targets.length ? "unchecked" : "error",
        detail: unit.targets.length ? "デモに設定したパス" : "移行先が未設定です。", path: unit.targets.join(" / ")},
      {phase: "実行環境", state: "unchecked", detail: "Git、Junie、ビルド、チェッカーは起動しません。"},
    ];
    return new Promise(resolve => {
      const finish = result => {
        clearTimeout(timer);
        validations.delete(request_id);
        resolve(result);
      };
      const timer = setTimeout(() => finish({simulated: true, unit_id, base_ref: project.base_ref || "HEAD", checks}), 1500);
      validations.set(request_id, () => finish({cancelled: true}));
    });
  }

  async function control(body, run) {
    const action = body.action === "resume" ? "resume-run" : body.action;
    if (action === "dry-run") return validate(body);
    if (action === "dry-run-cancel") {
      validations.get(body.request_id)?.();
      return {};
    }
    if (action === "accept" && run?.status === "checker_needs_review" && body.finding_id === "format-date" && body.reason?.trim()) {
      accepted.add(run.run_id);
      runs.select(run.run_id);
    } else if (controls.has(action)) {
      if (action === "resume-run" && run && interruptedScript(run)) {
        throw Error("実行結果が不明なスクリプトを、進捗画面で先に確認してください。");
      }
      if (body.start_stage && (action !== "resume-run" || body.plan_version !== resumePlan(run, body.start_stage).version)) {
        throw Error("保存版が更新されました。再開計画を確認し直してください。");
      }
      if (action === "resume-run" && run && stoppedRepair(run)) {
        if (!recovery.choices || recovery.choices[target] === "draft") {
          if (!recovery.choices) throw Error("復元する版を選んでから再開してください。");
          recovery.keywordBlocked = true;
          recovery.revision++;
          notify(run);
          throw Error("停止時の下書きに予約語が残っています。復元する版を選び直してください。");
        }
      }
      await runs.execute(action, body);
    } else {
      throw Error("この操作にはローカルのハーネスが必要です。デモでは実行しません。");
    }
    return {run_id: body.run_id};
  }

  function recoveryStatus(run) {
    const draft = stoppedRepair(run);
    const script = interruptedScript(run);
    const available = run.ready && !!run.worktree_path && ["checker_needs_review", "interrupted", "script_failed"].includes(run.status);
    return {run_id: run.run_id, available, stage: script ? scriptAttempt.phase : run.status,
      status: run.status, completed: run.completed_capsules, total: run.context_capsules,
      saved_targets: !!run.worktree_path, pending_edits: 0, pending_prompts: 0,
      draft: draft ? "repair" : null, draft_selected: draft && !!recovery.choices,
      draft_keyword_blocked: draft && recovery.keywordBlocked, unknown_script: script ? scriptAttempt : null};
  }

  function recoveryPlan(run, kind) {
    if (run.ready && kind === "draft" && stoppedRepair(run)) {
      const hashes = Object.fromEntries(Object.keys(recoveryTargets).map(path => [path, "sample"]));
      return {version: recovery.revision.toString(16).padStart(64, "a"), draft: hashes, worktree: hashes};
    }
    if (run.ready && kind === "script" && interruptedScript(run)) {
      return {version: "b".repeat(64), ...scriptAttempt, can_retry: true,
        warning: "実行済みの可能性があります。ログ・対象と残存プロセスを確認してください。"};
    }
    throw Error("停止中のサンプルRunを選び直してください。");
  }

  async function saveRecovery(run, body) {
    if (body.version !== recoveryPlan(run, body.kind).version) throw Error("復旧計画を確認し直してください。");
    if (body.kind === "draft") {
      if (!Object.keys(recoveryTargets).every(path => ["draft", "worktree"].includes(body.choices?.[path]))) {
        throw Error("ファイルごとに復元する版を選択してください。");
      }
      recovery.choices = body.choices;
      recovery.keywordBlocked = false;
      recovery.revision++;
      notify(run);
      return json({});
    }
    if (body.confirmed !== true) throw Error("再試行前の確認が必要です。");
    await runs.execute("recover-script", {run_id: run.run_id});
    return json({}, 202);
  }

  function ledger(run) {
    const verified = ["build_succeeded", "checker_succeeded", "checker_accepted"].includes(snapshot(run).status)
      && run.completed_capsules === run.context_capsules;
    const capsules = Array.from({length: run.context_capsules}, (_, i) => ({index: i + 1,
      sources: [{id: (i + 1) + ":1", path: "php/orders.php", start_line: i * 2 + 1, end_line: i * 2 + 2}]}));
    const {id: source_id, start_line, end_line} = capsules[0].sources[0];
    return {run_id: run.run_id, target, targets: [target, "Pages/Orders/Index.cshtml"],
      completion: {complete: verified && capsules.length > 0, verification_passed: verified, total: capsules.length},
      capsules,
      unresolved_junie_memos: verified ? [] : [{source_id, start_line, end_line, note: "日付の表示形式を原文と照合する。"}],
      unresolved_operator_notes: []};
  }

  const get = new Set(["workspace", "token-runs", "junie-models", "settings", "junie-requests", "junie-thoughts",
    "recovery", "recovery-plan", "recovery-file", "migration-ledger", "checker-output", "prompts", "resume-plan"]);
  const post = new Set(["settings", "control", "prompts", "recovery-plan"]);
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
        case "recovery": return json(recoveryStatus(run));
        case "recovery-plan": return body ? await saveRecovery(run, body) : json(recoveryPlan(run, query.get("kind")));
        case "recovery-file": {
          const path = query.get("target");
          if (query.get("version") !== recoveryPlan(run, "draft").version || !Object.hasOwn(recoveryTargets, path)) {
            throw Error("復旧計画または閲覧するファイルを選び直してください。");
          }
          const {draft, worktree} = recoveryTargets[path];
          return json({target: path, draft: {text: draft, truncated: false}, worktree: {text: worktree, truncated: false}});
        }
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
