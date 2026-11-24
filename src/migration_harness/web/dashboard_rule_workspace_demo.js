(() => {
  if (document.documentElement.hasAttribute("data-live")) return;

  const original = "# 注文ステータス\n- 確定済みの注文は取り消せない。\n";
  const addition = "- ただし決済前は、担当者が理由を記録した場合に限り取り消せる。\n";
  const file = {source: "orders", name: "orders.md", text: original, usage: "required", description: ""};
  const drafts = new Map();
  let sequence = 0;

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
    if (!action) return query.source ? {text: entry?.text ?? ""} : structuredClone(draft);

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
      case "message":
      case "optimize":
        draft.messages.push({role: "operator", text: action.text || "注文取消のルールを整理して"});
        draft.messages.push({role: "junie", text: "（サンプル応答）通常の取消禁止と、決済前の例外を分ける案です。例外を適用する条件と、理由の記録は残します。"});
        draft.proposal = {text: addition};
        break;
      case "accept":
        if (draft.proposal) {
          const target = draft.files[0];
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
          text_diff: draft.files.length === 1 && draft.files[0].text === original ? "" : [
            "--- 登録済みの原文",
            "+++ 下書きの原文",
            ...original.trimEnd().split("\n").map(line => "- " + line),
            ...draft.files.flatMap(item => item.text.trimEnd().split("\n").map(line => "+ " + line)),
          ].join("\n"),
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
