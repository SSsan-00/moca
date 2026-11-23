"use strict";

function createDemoSettings() {
  let demoSettings = `[project]
root = "C:/apps/orders"
worktree_root = "C:/migration/worktrees"
run_root = "C:/migration/runs"

[[units]]
id = "注文履歴"
source = "php/orders.php"
targets = ["Pages/Orders/Index.cshtml.cs", "Pages/Orders/Index.cshtml"]
draft_imports = ["C:/migration/draft/Orders.cshtml", "C:/migration/draft/Orders.cshtml.cs"]
`;
  for (const [name, file] of [["顧客詳細", "Customers"], ["売上集計", "Sales"], ["在庫照会", "Stock"], ["商品検索", "Products"], ["取引先一覧", "Partners"], ["請求一覧", "Invoices"], ["入金照合", "Payments"]]) {
    demoSettings += `\n[[units]]\nid = "${name}"\nsource = "php/${file}.php"\ntargets = ["Pages/${file}.cs"]\n`;
  }
  const demoUnits = () => [...demoSettings.matchAll(/^\s*id\s*=\s*"([^"\r\n]+)"/gm)].map(match => match[1]);
  function draftExtension(path) {
    const name = path.split(/[\\/]/).at(-1).toLowerCase();
    if (name.endsWith(".cshtml.cs")) return ".cshtml.cs";
    return name.lastIndexOf(".") > 0 ? name.slice(name.lastIndexOf(".")) : "";
  }

  function validateDemoDraftImports({id, targets, draft_imports: sources}) {
    const scope = `移行単位「${id}」の仮C#取り込み`;
    if (!Array.isArray(sources) || sources.some(path => typeof path !== "string" || !path.trim())) {
      throw new Error(`${scope}はコピー元パスの配列で指定してください。`);
    }
    const usedTargets = new Set();
    for (const source of sources) {
      const extension = draftExtension(source);
      let matches = targets.length === 1 ? targets : targets.filter(path => extension && draftExtension(path) === extension);
      if (!matches.length && extension === ".cs") matches = targets.filter(path => draftExtension(path) === ".cshtml.cs");
      if (matches.length !== 1) {
        const reason = matches.length ? "複数あります" : "ありません";
        throw new Error(`${scope}で、コピー元「${source}」の拡張子に対応する移行先が${reason}。コピー元と移行先の拡張子を確認し、対応する移行先を1つにしてください。`);
      }
      const [target] = matches;
      if (usedTargets.has(target)) {
        throw new Error(`${scope}で、移行先「${target}」に対応するコピー元が複数あります。コピー元を1つにしてください。`);
      }
      usedTargets.add(target);
    }
  }

  function demoSettingsDraft(content, changes = {}) {
    const blocks = content.split(/(?=^[\t ]*\[)/m);
    const unitBlocks = () => blocks.flatMap((block, index) => /^\s*\[\[units\]\]/.test(block) ? [index] : []);
    const materialBlocks = table => blocks.flatMap((block, index) =>
      block.trimStart().startsWith(`[[project.${table}]]`) ? [index] : []);
    const units = unitBlocks();
    const fieldPattern = key => new RegExp(`^\\s*${key}\\s*=([^\\r\\n]*)`, "m");
    // デモの編集対象の項目だけを書き換え、ほかの行を残す。
    function patchBlock(block, fields) {
      for (const [key, value] of Object.entries(fields)) {
        if (value === undefined) continue;
        let literal = key === "enabled" ? "false" : key === "recursive" ? value : JSON.stringify(value);
        if (key in numericSettings) {
          const [minimum, label] = numericSettings[key];
          if (!/^[0-9]+$/.test(value) || BigInt(value) < BigInt(minimum)) {
            throw new Error(`${label}は${minimum}以上の整数で入力してください。`);
          }
          literal = BigInt(value).toString();
        }
        const assignment = `${key} = ${literal}`;
        const pattern = fieldPattern(key);
        block = pattern.test(block)
          ? block.replace(pattern, match => match.replace(/\S.*$/, () => assignment))
          : `${block.trimEnd()}\n${assignment}\n\n`;
      }
      return block;
    }
    const findTable = section => blocks.findIndex(block => block.trimStart().startsWith(`[${section}]`));
    for (const section of ["project", "build", "checker", "junie", "diff_viewer", "target_context"]) {
      for (const [key, value] of Object.entries(changes[section] || {})) {
        let index = findTable(section);
        if ((value === "" || Array.isArray(value) && !value.length && key !== "roots")
          && !["root", "worktree_root", "run_root"].includes(key)) {
          if (index >= 0) blocks[index] = blocks[index].replace(fieldPattern(key), "");
          continue;
        }
        if (index < 0) index = blocks.push(`\n[${section}]\n`) - 1;
        blocks[index] = patchBlock(blocks[index], {[key]: value});
      }
    }
    if (changes.scripts) {
      let scripts = blocks.findIndex(block => /^\s*\[scripts\]/.test(block));
      if (scripts < 0 && changes.scripts.length) scripts = blocks.push("\n[scripts]\n") - 1;
      if (scripts >= 0) {
        for (const phase of Object.keys(scriptPhases)) {
          const commands = changes.scripts.filter(script => script.phase === phase).map(script => script.command);
          blocks[scripts] = commands.length ? patchBlock(blocks[scripts], {[phase]: commands})
            : blocks[scripts].replace(fieldPattern(phase), "");
        }
      }
    }
    // デモで変更された資料ブロックを既存の位置へ置き換える。
    function replaceInputBlocks(previous, inputs, heading, insertion, commonRule = false) {
      const updated = inputs.map(({index, ...fields}) => {
        let block = index === undefined ? `\n${heading}\n` : blocks[previous[index]];
        if (commonRule) {
          validateRuleUsage(fields);
          delete fields.role;
        }
        for (const key of ["start_line", "end_line", "encoding", "symbol"]) {
          if (fields[key] === "") {
            block = block.replace(fieldPattern(key), "");
            delete fields[key];
          }
        }
        return patchBlock(block, fields);
      });
      for (const position of previous.reverse()) blocks.splice(position, 1);
      blocks.splice(insertion, 0, ...updated);
    }
    function unitContext(index) {
      for (let next = index + 1; next < blocks.length && blocks[next].trimStart().startsWith("[") &&
              !blocks[next].trimStart().startsWith("[[units]]"); next++) {
        if (blocks[next].trimStart().startsWith("[units.target_context]")) return next;
      }
      return -1;
    }
    const editedUnits = Object.entries(changes.units || {});
    for (const [index] of editedUnits) units[index] ??= blocks.push("\n[[units]]\n") - 1;
    // 後ろの単位から編集し、テーブル追加による添字の変化を避ける。
    for (const [index, fields] of editedUnits.reverse()) {
      if ("targets" in fields) blocks[units[index]] = blocks[units[index]].replace(/^\s*target\s*=.*$/m, "");
      const {target_context, ...edited} = fields;
      if (target_context) {
        let position = unitContext(units[index]);
        if (position < 0) {
          position = units[index] + 1;
          blocks.splice(position, 0, "\n[units.target_context]\n");
        }
        for (const [key, value] of Object.entries(target_context)) {
          blocks[position] = value === "" ? blocks[position].replace(fieldPattern(key), "")
            : patchBlock(blocks[position], {[key]: value});
        }
      }
      if (edited.draft_imports?.length === 0) {
        blocks[units[index]] = blocks[units[index]].replace(fieldPattern("draft_imports"), "");
        delete edited.draft_imports;
      }
      blocks[units[index]] = patchBlock(blocks[units[index]], edited);
    }
    for (const index of [...(changes.removeEmptyUnits || [])].sort((a, b) => b - a)) {
      const position = unitBlocks()[index];
      const context = unitContext(position);
      if (context >= 0) blocks.splice(context, 1);
      blocks.splice(position, 1);
    }
    for (const [field, table] of [["migrationRules", "migration_rules"], ["references", "references"]]) {
      if (!changes[field]) continue;
      const previous = materialBlocks(table);
      replaceInputBlocks(previous, changes[field], `[[project.${table}]]`, previous[0] ?? findTable("project") + 1, table === "migration_rules");
    }
    const read = (index, key) => {
      const value = blocks[index]?.match(fieldPattern(key))?.[1];
      if (value === undefined) return undefined;
      if (key in numericSettings) return value.split("#")[0].trim().replaceAll("_", "");
      return JSON.parse(value.trim());
    };
    function readMaterials(positions, commonRule = false) {
      return positions.map((block, index) => {
        const role = commonRule ? "migration_rules" : read(block, "role");
        const path = read(block, "path");
        return typeof path === "string"
          ? {index, role: role ?? "", path, ...Object.fromEntries(
            ["start_line", "end_line", "encoding", "symbol", ...(commonRule ? ["usage", "description"] : [])].map(key => [key, read(block, key)]),
          )}
          : {index};
      });
    }
    const readContext = index => Object.fromEntries(
      ["roots", "folders", "exclude", "recursive", "max_files", "max_bytes", "max_file_bytes"]
        .flatMap(key => {
          const value = read(index, key);
          return value === undefined ? [] : [[key, key === "recursive" ? String(value) : value]];
        }),
    );
    const updated = blocks.join("");
    try {
      const scripts = blocks.findIndex(block => /^\s*\[scripts\]/.test(block));
      const checker = findTable("checker");
      return {content: updated, form: {
        project: Object.fromEntries(["root", "worktree_root", "run_root", "base_ref", "max_context_chars"].map(key =>
          [key, read(findTable("project"), key)])),
        build: {project: read(findTable("build"), "project") ?? ""},
        checker: {
          command: read(checker, "command") ?? [],
          max_repair_attempts: read(checker, "max_repair_attempts") ?? "",
        },
        junie: {
          ...Object.fromEntries(["max_steps", "model", "effort"].map(key => [key, read(findTable("junie"), key)])),
          log_language: read(findTable("junie"), "log_language") ?? "ja",
          log_tone: read(findTable("junie"), "log_tone") ?? "neutral",
          log_tone_instruction: read(findTable("junie"), "log_tone_instruction") ?? "",
        },
        diff_viewer: {
          html: read(findTable("diff_viewer"), "html"),
          anchor_reference: read(findTable("diff_viewer"), "anchor_reference"),
          enabled: read(findTable("diff_viewer"), "enabled") === false ? "false" : "",
        },
        target_context: readContext(findTable("target_context")),
        migrationRules: readMaterials(materialBlocks("migration_rules"), true),
        references: readMaterials(materialBlocks("references")),
        scripts: Object.keys(scriptPhases).flatMap(phase => (read(scripts, phase) || []).map(command => ({phase, command}))),
        units: unitBlocks().map(index => ({id: read(index, "id"), source: read(index, "source"),
          draft_imports: read(index, "draft_imports") ?? [],
          target_context: readContext(unitContext(index)),
          targets: read(index, "targets") || [read(index, "target")].filter(Boolean)})),
      }};
    } catch {
      return {content, form: null};
    }
  }

  let version = 1;
  function read() {
    return {path: "harness.local.toml（デモ内）", version: String(version), ...demoSettingsDraft(demoSettings)};
  }
  function save({content, version: expected}) {
    if (String(version) !== expected) throw new Error("設定が更新されました。読み込み直してください。");
    const form = demoSettingsDraft(content).form;
    if (!form) throw new Error("TOMLの書式を修正してください。");
    form.migrationRules.forEach(validateRuleUsage);
    form.units.forEach(validateDemoDraftImports);
    const {model = "", effort = ""} = form.junie;
    if (typeof model !== "string" || /^-|[\s\x00-\x1f\x7f]/u.test(model)) {
      throw new Error("Junieのモデルは、空白を含まないモデルIDで指定してください。既定値に戻す場合は空欄にしてください。");
    }
    if (typeof effort !== "string" || (effort && !Object.hasOwn(effortLabels, effort))) {
      throw new Error("推論の強さは none・minimal・low・medium・high・xhigh・max のいずれかを指定してください。");
    }
    if (effort && !model) throw new Error("推論の強さを指定する場合は、Junieで使うモデルも選択してください。");
    const {log_language = "", log_tone = ""} = form.junie;
    if (!["ja", "en"].includes(log_language) || !["neutral", "polite", "concise", "friendly", "custom"].includes(log_tone)) {
      throw new Error("詳細ログの案内には指定できる言語と口調を選んでください。");
    }
    const custom = form.junie.log_tone_instruction;
    if (typeof custom !== "string" || [...custom].length > 300 || (log_tone === "custom" && !custom.trim())) {
      throw new Error("口調のカスタム指示を300文字以内で入力してください。");
    }
    demoSettings = content;

    version++;
    return {version: String(version)};
  }
  return {read, preview: demoSettingsDraft, save, units: demoUnits};
}
