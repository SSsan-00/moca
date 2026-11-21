"use strict";

const numericSettings = {
  max_repair_attempts: [0, "自動修正回数"], max_context_chars: [1000, "カプセルの文字数上限"],
  max_steps: [1, "Junieのステップ数上限"], start_line: [1, "開始行"], end_line: [1, "終了行"],
};
const effortLabels = {none: "なし", minimal: "最小", low: "低", medium: "中", high: "高", xhigh: "非常に高い", max: "最大"};
const hasInputContent = item => Boolean(item.path?.trim() || item.description?.trim() || item.usage === "reference");
// 保存前にルールの読み込み方と必須の説明を検証する。
function validateRuleUsage(item) {
  const usage = item.usage === undefined ? "required" : item.usage;
  const description = item.description === undefined ? "" : item.description;
  if (!["required", "reference"].includes(usage)) {
    throw new Error("移行ルールの読み込み方（usage）は「毎回適用する必須事項」（required）または「必要時に読む詳細・例」（reference）を指定してください。");
  }
  if (typeof description !== "string") {
    throw new Error("移行ルールの「読む場面・内容」（description）は文字列で入力してください。");
  }
  if (usage === "reference" && !description.trim()) {
    throw new Error("「必要時に読む詳細・例」には「読む場面・内容」を入力してください。空白だけでは保存できません。");
  }
}

// 共通資料の入力項目を表示順にまとめる。
function* materialGroups(inputs) {
  for (let index = 0; index < inputs.length; index++) {
    const item = inputs[index];
    const group = [item];
    if (item.role === "reference_php") {
      while (/^reference_(csharp|razor)$/.test(inputs[index + 1]?.role)) group.push(inputs[++index]);
    }
    yield group;
  }
}

// 削除対象をタイトルとアクセシブル名で示す。
function removeButton(label, action) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "settings-remove";
  button.textContent = "×";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.addEventListener("click", action);
  return button;
}

// 入力欄と対応する削除ボタンを同じ行に置く。
function fieldWithRemove(label, remove) {
  const row = document.createElement("div");
  row.className = "settings-field-row";
  row.append(label, remove);
  return row;
}

// ルールの読み込み方と、詳細を読む条件の入力を結び付ける。
function showRuleDetails(card, item, ruleNumber, change) {
  const usage = document.createElement("select");
  usage.className = "workspace-select";
  usage.append(new Option("毎回適用する必須事項", "required"), new Option("必要時に読む詳細・例", "reference"));
  usage.value = item.usage === undefined ? "required" : item.usage;
  const usageLabel = document.createElement("label");
  usageLabel.append("読み込み方", usage);
  const descriptionLabel = document.createElement("label");
  const description = document.createElement("textarea");
  description.className = "settings-rule-description";
  description.rows = 2;
  description.placeholder = "例：DBアクセスを移行するときに読む、トランザクション処理の詳細と例";
  description.value = typeof item.description === "string" ? item.description : "";
  descriptionLabel.append("読む場面・内容", description);
  const descriptionHelp = document.createElement("p");
  descriptionHelp.className = "settings-material-range";
  descriptionHelp.id = `settings-rule-description-help-${ruleNumber}`;
  descriptionHelp.textContent = "必須です。どの作業で読むか、何が書かれているかを入力してください。";
  description.setAttribute("aria-describedby", descriptionHelp.id);
  const showDescription = () => {
    const reference = usage.value === "reference";
    descriptionLabel.hidden = descriptionHelp.hidden = !reference;
    description.setAttribute("aria-required", String(reference));
  };
  usage.addEventListener("change", () => {
    item.usage = usage.value;
    showDescription();
    change();
  });
  description.addEventListener("input", () => { item.description = description.value; change(); });
  showDescription();
  card.append(usageLabel, descriptionLabel, descriptionHelp);
}

// ルールと参考実装の編集を、共通フォームの下書きにつなぐ。
function bindSettingsMaterials(getForm, newDraft, changeMaterials) {
  const rules = document.querySelector("#settings-rules");
  const references = document.querySelector("#settings-references");
  const addRule = document.querySelector("#settings-rule-add");
  const addReference = document.querySelector("#settings-reference-add");
  // 資料の並びと追加・削除を描画し、入力のフォーカス先を返す。
  function showMaterials(inputs, kind = "references") {
    const common = kind === "migrationRules";
    const list = common ? rules : references;
    list.replaceChildren();
    const change = () => changeMaterials(kind);
    const rendered = new Map();
    // 資料の種類と順序が分かる見出しを持つ入力グループを作る。
    function addCard(list, title) {
      const card = document.createElement("fieldset");
      card.className = "settings-material";
      const heading = document.createElement("legend");
      heading.textContent = title;
      card.append(heading);
      list.append(card);
      return card;
    }
    // 参考実装を追加する操作を、フォーム送信と区別する。
    function button(label, action) {
      const control = document.createElement("button");
      control.type = "button";
      control.className = "button";
      control.textContent = label;
      control.addEventListener("click", action);
      return control;
    }
    // 参照先の拡張子に合わせて資料の役割を更新してから差分を通知する。
    function inputField(container, item, title, removal) {
      const label = document.createElement("label");
      label.textContent = title;
      const input = document.createElement("input");
      input.autocomplete = "off";
      input.spellcheck = false;
      input.placeholder = {
        migration_rules: "例：docs/migration/common.md",
        reference_php: "例：docs/migration/examples/orders/orders.php",
        reference_csharp: "例：docs/migration/examples/orders/Orders.cshtml.cs",
        reference_razor: "例：docs/migration/examples/orders/Orders.cshtml",
      }[item.role];
      input.value = item.path;
      input.addEventListener("input", () => {
        item.path = input.value.trim();
        if (item.role === "reference_csharp" || item.role === "reference_razor") {
          item.role = /\.(?:cshtml|razor)$/i.test(item.path) ? "reference_razor" : "reference_csharp";
        }
        change();
      });
      label.append(input);
      if (removal) container = container.appendChild(fieldWithRemove(label, removal));
      else container.append(label);
      rendered.set(item, input);
    }
    // 削除した資料をフォームに反映し、残った入力へフォーカスを移す。
    function remove(items, focusTarget) {
      for (const item of items) inputs.splice(inputs.indexOf(item), 1);
      const remaining = showMaterials(inputs, kind);
      (remaining.get(focusTarget) || focusTarget || addReference).focus();
      change();
    }
    let ruleNumber = 0;
    let pairNumber = 0;
    for (const group of materialGroups(inputs)) {
      const [item] = group;
      if (!item.role) continue;
      if (item.role === "migration_rules") {
        const title = `移行ルール ${++ruleNumber}`;
        const card = addCard(rules, title);
        inputField(card, item, "ルールファイル", removeButton(`${title}を削除`, () => remove([item], addRule)));
        showRuleDetails(card, item, ruleNumber, change);
        continue;
      }

      const pair = item.role === "reference_php";
      const title = pair ? `参考実装ペア ${++pairNumber}` : "参考実装ファイル";
      const card = addCard(references, title);
      for (const [position, entry] of group.entries()) {
        const label = entry.role === "reference_php" ? "参考実装の元PHP" : "移行済みC#／Razor";
        const removal = group.length > 2 && entry !== item
          ? removeButton(`${title}の移行済みファイル ${position}を削除`, () => remove([entry], item))
          : undefined;
        inputField(card, entry, label, removal);
      }
      const actions = document.createElement("div");
      actions.className = "workspace-actions";
      if (pair) actions.append(button("移行済みファイルを追加", () => {
        const entry = newDraft({role: "reference_csharp", path: ""});
        inputs.splice(inputs.indexOf(group.at(-1)) + 1, 0, entry);
        showMaterials(inputs).get(entry).focus();
        change();
      }));
      actions.append(removeButton(`${title}を削除`, () => remove(group, addReference)));
      card.append(actions);
    }
    return rendered;
  }

  addRule.addEventListener("click", () => {
    const inputs = getForm().migrationRules;
    const entry = newDraft({role: "migration_rules", path: "", usage: "required"});
    inputs.push(entry);
    showMaterials(inputs, "migrationRules").get(entry).focus();
    changeMaterials("migrationRules");
  });
  addReference.addEventListener("click", () => {
    const inputs = getForm().references;
    const source = newDraft({role: "reference_php", path: ""});
    inputs.push(source, newDraft({role: "reference_razor", path: ""}));
    showMaterials(inputs).get(source).focus();
    changeMaterials("references");
  });
  return showMaterials;
}

// 本番とデモで、設定フォームと読み込み・プレビュー・保存を接続する。
function bindHarnessWorkspace({readRuns, readSettings, readModels, previewSettings, saveSettings, execute, canAct, canConfigure = canAct, preparationChat = false}) {
  const runsUI = bindRunManager({readRuns, execute, canAct, preparationChat});
  window.harnessRuns = runsUI;
  const workspaceConfig = document.querySelector("#workspace-config");
  const workspaceConfigFeedback = document.querySelector("#workspace-config-feedback");
  const settings = document.querySelector("#workspace-config-form");
  const reload = document.querySelector("#workspace-config-reload");
  const editToggle = document.querySelector("#workspace-config-toggle");
  const fields = document.querySelector("#workspace-settings-fields");
  const projectFields = [...fields.querySelectorAll("#settings-project-fields [data-setting]:not([data-section])")];
  const tableFields = [...fields.querySelectorAll("[data-section][data-setting]")];
  const unitFields = [...fields.querySelectorAll("#settings-unit-inputs [data-setting]")];
  const unit = document.querySelector("#settings-unit");
  const scriptList = document.querySelector("#settings-scripts");
  const addScript = document.querySelector("#settings-script-add");
  const materialFields = ["migrationRules", "references"];
  const modelSelect = document.querySelector("#settings-junie-model");
  const modelStatus = document.querySelector("#settings-junie-model-status");
  const effortSelect = document.querySelector("#settings-junie-effort");
  const effortStatus = document.querySelector("#settings-junie-effort-status");
  let unsupportedEffort = false;
  const fallbackModels = [...modelSelect.options].slice(1).map(option => ({id: option.value, name: option.text}));
  let modelCatalog = fallbackModels;
  const added = new WeakSet();
  const invalidTOMLMessage = form => form ? "" : "フォームを表示するには、TOMLの書式を修正してください。";
  const emptyChanges = () => ({project: {}, units: {}});
  let settingsVersion;
  let settingsForm;
  let settingsChanges = emptyChanges();
  let previewTimer;
  let draftRevision = 0;
  let previewPending = false;
  let readingTOML = false;
  let loadingOrSavingSettings = false;
  let modelRequest = 0;

  // 一覧にない保存済みモデルIDも、選択できるよう保持する。
  function selectModel(value) {
    if (value && ![...modelSelect.options].some(option => option.value === value)) {
      modelSelect.add(new Option(`${value}（指定済み）`, value));
    }
    modelSelect.value = value;
  }

  // 設定読込時は保存値を保持し、モデル変更時は対応する強さに絞る。
  function showEfforts(value = effortSelect.value) {
    const model = modelSelect.value;
    const supported = modelCatalog.find(item => item.id === model)?.efforts;
    const values = model ? (supported ?? []).filter(effort => Object.hasOwn(effortLabels, effort)) : [];
    const unavailable = Boolean(value && !values.includes(value));
    unsupportedEffort = unavailable && Array.isArray(supported);
    effortSelect.replaceChildren(new Option(model ? "モデルの既定値" : "モデルを選択してください", ""),
      ...values.map(effort => new Option(effortLabels[effort] + "（" + effort + "）", effort)));
    if (unavailable) {
      const previous = new Option((effortLabels[value] ?? value) + (unsupportedEffort ? "（非対応）" : "（未確認）"), value);
      previous.disabled = true;
      effortSelect.add(previous);
    }
    effortSelect.value = model ? value : "";
    effortSelect.disabled = !model;
    effortStatus.textContent = unsupportedEffort
      ? "このモデルでは現在の推論レベルを使えません。対応する値か「モデルの既定値」を選び直してください。"
      : model && !Array.isArray(supported)
        ? "対応する推論レベルを取得できていません。新しく指定する場合はモデル一覧を読み込み直してください。"
        : "";
    effortStatus.hidden = !effortStatus.textContent;
  }

  // 取得に失敗したときも、保存済みの選択値と代表候補を表示する。
  async function loadModels() {
    if (!readModels) return;
    const request = ++modelRequest;
    modelStatus.hidden = false;
    modelStatus.textContent = "Junieのモデル候補を読み込んでいます。";
    let models = fallbackModels;
    let message = "";
    try {
      const data = await readModels();
      if (!data.models.length) throw new Error("empty models");
      models = data.models;
    } catch {
      message = "一覧を取得できないため、代表的な候補を表示しています。Junieのインストール・バージョンを確認して「読み込み直す」を押してください。";
    }
    // 直近の再読込だけがモデル候補と選択値を更新する。
    if (request !== modelRequest) return;
    const selected = modelSelect.value;
    modelCatalog = models;
    modelSelect.replaceChildren(new Option("Junieの既定モデル", ""), ...models.map(model => new Option(model.name, model.id)));
    selectModel(selected);
    showEfforts();
    modelStatus.textContent = message;
    modelStatus.hidden = !message;
    workspaceControls();
  }
  workspaceControls = () => {
    const available = canConfigure() && !loadingOrSavingSettings;
    runsUI.sync();
    settings.querySelector('button[type="submit"]').disabled = !available || previewPending || !settingsVersion
      || !workspaceConfig.value.trim() || !settingsForm || unsupportedEffort;
    workspaceConfig.readOnly = !available || !settingsVersion || (previewPending && !readingTOML);
    editToggle.disabled = !available || !settingsVersion || previewPending || (!workspaceConfig.hidden && !settingsForm);
    for (const fieldset of fields.querySelectorAll("fieldset")) fieldset.disabled = !available || readingTOML;
    reload.disabled = loadingOrSavingSettings;
  };

  // 保存前に空欄を掃除できるよう、新しく追加した行を識別する。
  function newDraft(entry) {
    added.add(entry);
    return entry;
  }

  // 空の新規行を掃除し、不完全な入力は保存前に報告する。
  function planSave() {
    const command = settingsForm.checker.command;
    if (Array.isArray(command) && command.length && !command[0]?.trim()) {
      throw new Error("チェッカーの1行目に実行ファイルを入力してください。");
    }
    const cleanup = {};
    const removeEmptyUnits = [];
    for (const [index, entry] of settingsForm.units.entries()) {
      if (!added.has(entry)) continue;
      const {id, source, targets, draft_imports} = entry;
      const hasContext = Object.values(entry.target_context ?? {}).some(value => value !== "");
      if (!id.trim() && !source.trim() && !targets.length && !draft_imports.length && !hasContext) removeEmptyUnits.push(index);
      else if (!id.trim() || !source.trim() || !targets.length) {
        throw new Error(`移行単位 ${index + 1} の機能名、移行元PHP、移行先を指定してください。`);
      }
    }
    if (removeEmptyUnits.length) cleanup.removeEmptyUnits = removeEmptyUnits;

    const rules = settingsForm.migrationRules.filter(item => !added.has(item) || hasInputContent(item));
    if (rules.some(item => item.role && !item.path.trim())) {
      throw new Error("移行ルールのファイルパスを指定してください。");
    }
    rules.filter(item => item.role === "migration_rules").forEach(validateRuleUsage);
    if (rules.length !== settingsForm.migrationRules.length) cleanup.migrationRules = rules;

    const references = [];
    for (const group of materialGroups(settingsForm.references)) {
      const [source, ...targets] = group;
      const filled = group.filter(item => !added.has(item) || hasInputContent(item));
      if (!filled.length) continue;
      if (filled.some(item => item.role && !item.path.trim()) ||
          (source.role === "reference_php" && added.has(source)
            && (!source.path.trim() || !targets.some(item => item.path.trim())))) {
        throw new Error("入力途中の参考実装ペアに、元PHPと移行済みC#／Razorのパスを指定してください。");
      }
      references.push(...filled);
    }
    if (references.length !== settingsForm.references.length) cleanup.references = references;

    const scripts = settingsForm.scripts.filter(entry =>
      !added.has(entry) || entry.phase || entry.command.some(argument => argument.trim()));
    if (scripts.some(entry => !entry.phase || !entry.command[0]?.trim())) {
      throw new Error("入力途中のスクリプトの実行工程と実行ファイルを指定してください。");
    }
    const selected = Number(unit.value);
    return {
      cleanup,
      refresh: Object.keys(cleanup).length > 0 || scripts.length !== settingsForm.scripts.length,
      unitIndex: selected - removeEmptyUnits.filter(index => index < selected).length,
    };
  }

  // 資料の並びごと差分へ渡し、未知の設定をサーバー側に保持させる。
  function changeMaterials(field) {
    settingsChanges[field] = settingsForm[field];
    schedulePreview();
  }

  // 工程が選択されたコマンドだけを設定の差分に含める。
  function changeScripts() {
    settingsChanges.scripts = settingsForm.scripts.filter(script => script.phase);
    schedulePreview();
  }

  // 実行ファイル名の余白だけを除き、引数の空白を保持する。
  function commandLines(text) {
    const lines = text.split("\n");
    lines[0] = lines[0].trim();
    return lines;
  }

  // 工程と引数を描画し、追加時のフォーカスに使う選択欄を返す。
  function showScripts() {
    scriptList.replaceChildren();
    return settingsForm.scripts.map((script, index) => {
      const card = document.createElement("fieldset");
      card.className = "settings-material";
      const title = document.createElement("legend");
      title.textContent = `スクリプト ${index + 1}`;
      const remove = removeButton(`${title.textContent}を削除`, () => {
        settingsForm.scripts.splice(index, 1);
        const next = showScripts()[index];
        (next ?? addScript).focus();
        changeScripts();
      });
      scriptList.append(card);
      const phase = document.createElement("select");
      phase.className = "workspace-select";
      phase.append(new Option("工程を選択", ""), ...Object.entries(scriptPhases).map(([key, name]) => new Option(name, key)));
      phase.value = script.phase;
      phase.addEventListener("change", () => { script.phase = phase.value; changeScripts(); });
      const command = document.createElement("textarea");
      command.className = "settings-command settings-script-command";
      command.spellcheck = false;
      command.placeholder = "py\n-3.11\ntools/export_results.py";
      command.value = script.command.join("\n");
      command.addEventListener("input", () => {
        script.command = commandLines(command.value);
        changeScripts();
      });
      const phaseLabel = document.createElement("label");
      phaseLabel.append("実行工程", phase);
      const commandLabel = document.createElement("label");
      commandLabel.append("1行目に実行ファイル、2行目以降に引数を1つずつ入力", command);
      card.append(title, phaseLabel, fieldWithRemove(commandLabel, remove));
      return phase;
    });
  }

  addScript.addEventListener("click", () => {
    settingsForm.scripts.push(newDraft({phase: "", command: [""]}));
    showScripts().at(-1).focus();
    changeScripts();
  });


  const showMaterials = bindSettingsMaterials(() => settingsForm, newDraft, changeMaterials);

  // 選んだ移行単位の既存値を入力欄へ反映する。
  function showUnit() {
    const current = settingsForm.units[Number(unit.value)];
    document.querySelector("#settings-unit-inputs").hidden = !current;
    for (const field of unitFields) {
      const value = current?.[field.dataset.setting];
      field.value = Array.isArray(value) ? value.join("\n") : value ?? "";
    }
  }

  // 再描画後も近くの移行単位を選び直せるようにする。
  function showUnitOptions(index = Number(unit.value)) {
    unit.replaceChildren(...settingsForm.units.map(({id}, i) => new Option(id || `移行単位 ${i + 1}`, i)));
    if (settingsForm.units.length) unit.value = String(Math.min(index, settingsForm.units.length - 1));
    showUnit();
    showContext();
  }

  // フォームと直接編集の表示を切り替え、操作名も変更する。
  function showTOMLEditor(editing) {
    fields.hidden = editing || !settingsForm;
    workspaceConfig.hidden = !editing;
    editToggle.textContent = editing ? "入力フォームに戻る" : "TOMLを直接編集";
    editToggle.setAttribute("aria-expanded", String(editing));
  }

  editToggle.addEventListener("click", () => {
    showTOMLEditor(workspaceConfig.hidden);
    if (!workspaceConfig.hidden) workspaceConfig.focus();
  });

  const rootMode = document.querySelector("#settings-context-roots");
  const rootChoices = document.querySelector("#settings-context-root-choices");
  const rootChecks = [...rootChoices.querySelectorAll("input")];
  const viewerMode = document.querySelector("#settings-viewer-enabled");
  const viewerHTML = document.querySelector("#settings-viewer-html");
  const viewerReference = document.querySelector("#settings-anchor-reference");

  // 無効時のパスを保持しながら、関連欄の編集可否を更新する。
  function showViewerState() {
    const disabled = viewerMode.value === "false";
    viewerHTML.disabled = viewerReference.disabled = disabled;
    document.querySelector("#settings-viewer-state").textContent = disabled ? "無効"
      : viewerHTML.value.trim() ? "有効" : "無効（HTML 未設定）";
  }

  // 共通の参照資料設定の差分だけを更新する。
  function editContext(key, value) {
    settingsChanges.target_context ??= {};
    settingsChanges.target_context[key] = settingsForm.target_context[key] = value;
    schedulePreview();
  }

  // 共通の参照起点を、既定・選択・空の表示に反映する。
  function showContext() {
    const roots = settingsForm.target_context.roots;
    rootMode.value = Array.isArray(roots) ? (roots.length ? "custom" : "none") : "default";
    rootChoices.hidden = rootMode.value !== "custom";
    rootChecks.forEach(input => { input.checked = Array.isArray(roots) && roots.includes(input.value); });
    const folders = settingsForm.target_context.folders;
    document.querySelector("#settings-context-folders").value = Array.isArray(folders) ? folders.join("\n") : "";
    workspaceControls();
  }

  // 既定の起点と、起点なしを表す空配列を区別して保存する。
  function saveRootChoices() {
    rootChoices.hidden = rootMode.value !== "custom";
    const chosen = rootChecks.filter(input => input.checked).map(input => input.value);
    editContext("roots", rootMode.value === "default" ? "" : rootMode.value === "none" ? [] : chosen);
  }
  rootMode.addEventListener("change", () => {
    if (rootMode.value === "custom" && !rootChecks.some(input => input.checked)) {
      rootChecks.forEach(input => { input.checked = true; });
    }
    saveRootChoices();
    if (!rootChoices.hidden) rootChecks[0].focus();
  });
  rootChecks.forEach(input => input.addEventListener("change", saveRootChoices));
  viewerMode.addEventListener("input", showViewerState);
  viewerHTML.addEventListener("input", showViewerState);

  // プレビュー結果でTOMLを更新し、必要な場合だけフォームを描き直す。
  function showSettingsDraft(data, showFields, unitIndex) {
    workspaceConfig.value = data.content;
    if (!showFields) return;
    settingsForm = data.form;
    fields.hidden = !settingsForm || !workspaceConfig.hidden;
    if (settingsForm) {
      for (const field of projectFields) field.value = settingsForm.project[field.dataset.setting] ?? "";
      for (const field of tableFields) {
        const value = settingsForm[field.dataset.section]?.[field.dataset.setting];
        if (field === modelSelect) selectModel(value ?? "");
        else if (field !== effortSelect) field.value = Array.isArray(value) ? value.join("\n") : value ?? "";
      }
      settingsForm.target_context ??= {};
      document.querySelector("#settings-junie-log-custom").hidden = settingsForm.junie.log_tone !== "custom";
      showViewerState();
      showEfforts(settingsForm.junie.effort ?? "");
      showMaterials(settingsForm.migrationRules, "migrationRules");
      showMaterials(settingsForm.references);
      showUnitOptions(unitIndex);
      showScripts();
    } else {
      showTOMLEditor(true);
    }
  }

  // 入力が落ち着いてからプレビューを要求し、古い応答を破棄する。
  function schedulePreview(fromTOML = false) {
    clearTimeout(previewTimer);
    // 最新の入力に対する応答だけが下書きを更新し、差分を消去する。
    const revision = ++draftRevision;
    previewPending = true;
    readingTOML = fromTOML;
    workspaceConfigFeedback.textContent = "";
    workspaceControls();
    previewTimer = setTimeout(async () => {
      try {
        const draft = await previewSettings(workspaceConfig.value, fromTOML ? {} : settingsChanges);
        if (revision !== draftRevision) return;
        showSettingsDraft(draft, fromTOML);
        if (!fromTOML && draft.form) {
          for (const field of materialFields) {
            settingsForm[field].forEach((input, index) => {
              input.index = draft.form[field][index].index;
            });
          }
        }
        settingsChanges = emptyChanges();
        previewPending = false;
        workspaceConfigFeedback.textContent = invalidTOMLMessage(draft.form);
      } catch (error) {
        if (revision !== draftRevision) return;
        workspaceConfigFeedback.textContent = error.message;
      } finally {
        if (revision === draftRevision) {
          readingTOML = fromTOML && previewPending;
          workspaceControls();
        }
      }
    }, 160);
  }

  // 未反映プレビューを無効にして、保存版とフォームを読み直す。
  async function loadSettings(force = false) {
    if (loadingOrSavingSettings || (settingsVersion && !force)) return;
    loadingOrSavingSettings = true;
    clearTimeout(previewTimer);
    ++draftRevision;
    workspaceConfigFeedback.textContent = "設定を読み込んでいます。";
    workspaceControls();
    try {
      const data = await readSettings();
      if (!settingsVersion && data.version === "missing") {
        document.querySelector("#settings-project-details").open = true;
      }
      settingsVersion = data.version;
      showSettingsDraft(data, true);
      loadModels();
      settingsChanges = emptyChanges();
      previewPending = readingTOML = false;
      document.querySelector("#workspace-config-path").textContent = data.path;
      workspaceConfigFeedback.textContent = invalidTOMLMessage(data.form);
    } catch (error) {
      workspaceConfigFeedback.textContent = error.message;
    } finally {
      loadingOrSavingSettings = false;
      workspaceControls();
    }
  }

  // 空の行を掃除する必要がある場合だけプレビューを作ってから、読み込み時の版で保存する。
  async function saveSettingsDraft(event) {
    event.preventDefault();
    if (settings.querySelector('button[type="submit"]').disabled) return;
    loadingOrSavingSettings = true;
    workspaceConfigFeedback.textContent = "設定を保存しています。";
    workspaceControls();
    try {
      const plan = planSave();
      const cleaned = plan.refresh ? await previewSettings(workspaceConfig.value, plan.cleanup) : null;
      const saved = await saveSettings(cleaned?.content ?? workspaceConfig.value, settingsVersion);
      settingsVersion = saved.version;
      if (cleaned) showSettingsDraft(cleaned, true, plan.unitIndex);
      workspaceConfigFeedback.textContent = "設定を保存しました。実行タブから移行単位を選べます。";
    } catch (error) {
      workspaceConfigFeedback.textContent = error.message;
    } finally {
      loadingOrSavingSettings = false;
      workspaceControls();
    }
  }
  settings.addEventListener("submit", saveSettingsDraft);
  for (const field of projectFields) field.addEventListener("input", () => {
    const key = field.dataset.setting;
    settingsChanges.project[key] = settingsForm.project[key] = field.value;
    schedulePreview();
  });
  for (const field of tableFields) field.addEventListener("input", () => {
    const {section, setting} = field.dataset;
    let value = field.value.trim();
    if (setting === "command") value = value ? commandLines(field.value) : [];
    if (setting === "anchor_reference") value = field.value.split("\n").map(path => path.trim()).filter(Boolean);
    if (section === "target_context") {
      editContext(setting, value ? value.split("\n").map(path => path.trim()).filter(Boolean) : "");
      return;
    }
    settingsChanges[section] ??= {};
    settingsForm[section] ??= {};
    settingsChanges[section][setting] = settingsForm[section][setting] = value;
    if (field === modelSelect) {
      showEfforts(value ? effortSelect.value : "");
      settingsChanges.junie.effort = settingsForm.junie.effort = effortSelect.value;
    } else if (field === effortSelect) {
      showEfforts(value);
    } else if (setting === "log_tone") {
      document.querySelector("#settings-junie-log-custom").hidden = value !== "custom";
    }
    schedulePreview();
  });
  for (const field of unitFields) field.addEventListener("input", () => {
    const index = Number(unit.value);
    const key = field.dataset.setting;
    const value = key === "targets" || key === "draft_imports" ? field.value.split("\n").map(path => path.trim()).filter(Boolean)
      : field.value;
    settingsChanges.units[index] ??= {};
    settingsChanges.units[index][key] = settingsForm.units[index][key] = value;
    if (key === "id") {
      unit.selectedOptions[0].text = value || `移行単位 ${index + 1}`;
    }
    schedulePreview();
  });
  unit.addEventListener("change", showUnit);
  document.querySelector("#settings-unit-add").addEventListener("click", () => {
    const index = settingsForm.units.length;
    const draft = newDraft({id: "", source: "", targets: [], draft_imports: []});
    settingsChanges.units[index] = {...draft};
    settingsForm.units.push(draft);
    showUnitOptions(index);
    unitFields[0].focus();
    schedulePreview();
  });
  workspaceConfig.addEventListener("input", () => schedulePreview(true));
  reload.addEventListener("click", () => loadSettings(true));
  workspaceTabs.addEventListener("harnesstab", () => {
    if (harnessTabs[activeHarnessTab].id === "tab-settings") loadSettings();
  });
  workspaceControls();
}
