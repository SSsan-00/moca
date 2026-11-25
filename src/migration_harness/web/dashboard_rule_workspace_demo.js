(() => {
  if (document.documentElement.hasAttribute("data-live")) return;

  const original = "# 注文ステータス\n- 確定済みの注文は取り消せない。\n";
  const addition = "- ただし決済前は、担当者が理由を記録した場合に限り取り消せる。\n";
  const file = {source: "orders", name: "orders.md", text: original, usage: "required", description: ""};
  const drafts = new Map();
  let sequence = 0;

  function diffFile(before, after, name) {
    if (before === after) return "";
    const lines = text => text.match(/[^\n]*\n|[^\n]+$/g) || [];
    const old = lines(before), next = lines(after);
    let start = 0, oldEnd = old.length, newEnd = next.length;
    while (start < oldEnd && start < newEnd && old[start] === next[start]) start++;
    while (oldEnd > start && newEnd > start && old[oldEnd - 1] === next[newEnd - 1]) { oldEnd--; newEnd--; }
    const a = old.slice(start, oldEnd), b = next.slice(start, newEnd);
    const result = old.slice(0, start).map(line => [" ", line]);
    let grouped = false;
    if (!a.length || !b.length) {
      for (const line of a) result.push(["-", line]);
      for (const line of b) result.push(["+", line]);
    } else if (a.length * b.length <= 250_000) {
      const lcs = Array.from({length: a.length + 1}, () => new Uint32Array(b.length + 1));
      for (let i = a.length - 1; i >= 0; i--) for (let j = b.length - 1; j >= 0; j--) {
        lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
      }
      let i = 0, j = 0;
      while (i < a.length || j < b.length) {
        if (i < a.length && j < b.length && a[i] === b[j]) { result.push([" ", a[i++]]); j++; }
        else if (i < a.length && (j === b.length || lcs[i + 1][j] >= lcs[i][j + 1])) result.push(["-", a[i++]]);
        else result.push(["+", b[j++]]);
      }
    } else {
      // shortcut: very large demo edits are shown as one replaced block; use a scalable diff if needed.
      grouped = true;
      for (const line of a) result.push(["-", line]);
      for (const line of b) result.push(["+", line]);
    }
    for (let i = oldEnd; i < old.length; i++) result.push([" ", old[i]]);
    return `--- before/${name}\n+++ after/${name}\n@@ -1,${old.length} +1,${next.length} @@\n`
      + (grouped ? "\\ 変更範囲が大きいため、変更前後をまとめて表示します。\n" : "")
      + result.map(([prefix, line]) => prefix + line + (line.endsWith("\n") ? "" : "\n\\ No newline at end of file\n")).join("");
  }

  window.harnessRuleWorkspaceDemo = (query, action) => {
    if (!action && query?.registered === "1") return {registered: {name: file.name, text: file.text}};
    if (!action && !query?.id) {
      return {drafts: [...drafts.values()].filter(draft => !draft.applied)
        .map(draft => ({id: draft.id, files: draft.files.length}))};
    }
    if (action?.action === "start") {
      const id = "demo-" + String(++sequence).padStart(3, "0");
      const draft = {id, revision: 0, applied: false, files: [{...file}], messages: [], unresolved: []};
      drafts.set(id, draft);
      return structuredClone(draft);
    }
    const draft = drafts.get(action?.id ?? query?.id);
    if (!draft || draft.applied) throw Error("デモの下書きがありません。");
    const entry = draft.files.find(item => item.source === (action?.source ?? query?.source));
    if (!action) return query.source ? {text: entry.text, revision: draft.revision} : structuredClone(draft);

    switch (action.action) {
      case "edit":
        if (entry) entry.text = action.text;
        else {
          if (!action.name?.trim()) throw Error("ファイル名を入力してください。");
          draft.files.push({...file, source: "file-" + draft.revision, name: action.name, text: action.text});
        }
        break;
      case "classification":
        if (!entry) throw Error("原文を選んでください。");
        entry.usage = action.usage;
        entry.description = action.description;
        break;
      case "model":
        draft.junie_model = action.model || null;
        draft.junie_effort = action.effort || null;
        break;
      case "message":
      case "optimize":
        draft.messages.push({role: "operator", text: action.text || "注文取消のルールを整理して"});
        draft.messages.push({role: "junie", text: "（サンプル応答）通常の取消禁止と、決済前の例外を分ける案です。例外を適用する条件と、理由の記録は残します。"});
        draft.proposal = {source: draft.files[0].source, text: addition};
        break;
      case "accept":
        if (draft.proposal) {
          const target = draft.files.find(item => item.source === draft.proposal.source);
          if (!target.text.includes(addition)) target.text = target.text.trimEnd() + "\n" + addition;
          draft.proposal = null;
        }
        break;
      case "preview": {
        const rules = draft.files.map(item => ({
          destination: "rules/generated/" + item.name,
          usage: item.usage,
          description: item.description,
          parts: [{source: item.source, start: 1, end: Math.max(1, item.text.trimEnd().split("\n").length)}],
        }));
        const toml = item => "[[project.migration_rules]]\npath = " + JSON.stringify(item.destination)
          + "\nusage = " + JSON.stringify(item.usage);
        draft.preview = {
          token: String(draft.revision),
          text_diff: draft.files.map(item => diffFile(item.source === "orders" ? original : "", item.text, item.name)).join(""),
          toml_diff: rules.map(toml).join("\n\n"),
          rules,
        };
        break;
      }
      case "apply":
        if (!draft.preview || action.preview !== draft.preview.token || !action.confirmed) {
          throw Error("差分を確認し、チェックを入れてください。");
        }
        draft.applied = true;
        break;
      default:
        throw Error("この操作はデモでは使えません。");
    }
    if (!["preview", "apply"].includes(action.action)) draft.preview = null;
    draft.revision++;
    return structuredClone(draft);
  };
})();
