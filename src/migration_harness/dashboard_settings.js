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
  function showMaterials(inputs, kind = "references") {
    const common = kind === "migrationRules";
    const list = common ? rules : references;
    list.replaceChildren();
    const change = () => changeMaterials(kind);
    const rendered = new Map();
    function addCard(list, title) {
      const card = document.createElement("fieldset");
      card.className = "settings-material";
      const heading = document.createElement("legend");
      heading.textContent = title;
      card.append(heading);
      list.append(card);
      return card;
    }
    function button(label, action) {
      const control = document.createElement("button");
      control.type = "button";
      control.className = "button";
      control.textContent = label;
      control.addEventListener("click", action);
      return control;
    }
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

  function selectModel(value) {
    if (value && ![...modelSelect.options].some(option => option.value === value)) {
      modelSelect.add(new Option(`${value}（指定済み）`, value));
    }
    modelSelect.value = value;
  }

  function showEfforts(value = effortSelect.value, preserve = true) {
    const model = modelSelect.value;
    const supported = modelCatalog.find(item => item.id === model)?.efforts
      ?? (model === "junie-lite" ? ["none"] : ["low", "medium", "high"]);
    const values = model ? supported.filter(effort => Object.hasOwn(effortLabels, effort)) : [];
    effortSelect.replaceChildren(new Option(model ? "モデルの既定値" : "モデルを選択してください", ""),
      ...values.map(effort => new Option(`${effortLabels[effort]}（${effort}）`, effort)));
    if (preserve && value && !values.includes(value)) {
      effortSelect.add(new Option(`${effortLabels[value] ?? value}（指定済み）`, value));
    }
    effortSelect.value = preserve || values.includes(value) ? value : "";
    effortSelect.disabled = !model;
  }

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
  }
  workspaceControls = () => {
    const available = canConfigure() && !loadingOrSavingSettings;
    runsUI.sync();
    settings.querySelector('button[type="submit"]').disabled = !available || previewPending || !settingsVersion
      || !workspaceConfig.value.trim() || !settingsForm;
    workspaceConfig.readOnly = !available || !settingsVersion || (previewPending && !readingTOML);
    editToggle.disabled = !available || !settingsVersion || previewPending || (!workspaceConfig.hidden && !settingsForm);
    for (const fieldset of fields.querySelectorAll("fieldset")) fieldset.disabled = !available || readingTOML;
    reload.disabled = loadingOrSavingSettings;
  };

  function newDraft(entry) {
    added.add(entry);
    return entry;
  }

  function planSave() {
    const command = settingsForm.checker.command;
    if (Array.isArray(command) && command.length && !command[0]?.trim()) {
      throw new Error("チェッカーの1行目に実行ファイルを入力してください。");
    }
    const cleanup = {};
    const removeEmptyUnits = [];
    for (const [index, entry] of settingsForm.units.entries()) {
      const checker = entry.checker_command;
      if (Array.isArray(checker) && checker.length && !checker[0]?.trim()) {
        throw new Error(`移行単位 ${index + 1} のチェッカーの1行目に実行ファイルを入力してください。`);
      }
      if (!added.has(entry)) continue;
      const {id, source, targets, draft_imports} = entry;
      const hasContext = Object.values(entry.target_context ?? {}).some(value => value !== "");
      if (!id.trim() && !source.trim() && !targets.length && !checker?.length && !draft_imports.length && !hasContext) removeEmptyUnits.push(index);
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

  function changeMaterials(field) {
    settingsChanges[field] = settingsForm[field];
    schedulePreview();
  }

  function changeScripts() {
    settingsChanges.scripts = settingsForm.scripts.filter(script => script.phase);
    schedulePreview();
  }

  function commandLines(text) {
    const lines = text.split("\n");
    lines[0] = lines[0].trim();
    return lines;
  }

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

  function showUnitOptions(index = Number(unit.value)) {
    unit.replaceChildren(...settingsForm.units.map(({id}, i) => new Option(id || `移行単位 ${i + 1}`, i)));
    if (settingsForm.units.length) unit.value = String(Math.min(index, settingsForm.units.length - 1));
    showUnit();
    showContext();
  }

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

  function showViewerState() {
    const disabled = viewerMode.value === "false";
    viewerHTML.disabled = viewerReference.disabled = disabled;
    document.querySelector("#settings-viewer-state").textContent = disabled ? "無効"
      : viewerHTML.value.trim() ? "有効" : "無効（HTML 未設定）";
  }

  function editContext(key, value) {
    settingsChanges.target_context ??= {};
    settingsChanges.target_context[key] = settingsForm.target_context[key] = value;
    schedulePreview();
  }

  function showContext() {
    const roots = settingsForm.target_context.roots;
    rootMode.value = Array.isArray(roots) ? (roots.length ? "custom" : "none") : "default";
    rootChoices.hidden = rootMode.value !== "custom";
    rootChecks.forEach(input => { input.checked = Array.isArray(roots) && roots.includes(input.value); });
    const folders = settingsForm.target_context.folders;
    document.querySelector("#settings-context-folders").value = Array.isArray(folders) ? folders.join("\n") : "";
    workspaceControls();
  }

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
      showEfforts(effortSelect.value, false);
      settingsChanges.junie.effort = settingsForm.junie.effort = effortSelect.value;
    }
    schedulePreview();
  });
  for (const field of unitFields) field.addEventListener("input", () => {
    const index = Number(unit.value);
    const key = field.dataset.setting;
    const value = key === "targets" || key === "draft_imports" ? field.value.split("\n").map(path => path.trim()).filter(Boolean)
      : key === "checker_command" ? (field.value.trim() ? commandLines(field.value) : []) : field.value;
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
    const draft = newDraft({id: "", source: "", targets: [], checker_command: [], draft_imports: []});
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

"use strict";

// ダッシュボードは分割マーカーより前のHTMLとスクリプトを配信する。
// 本番とデモの経過時間を、24時間を超えても折り返さない表記にする。
function formatDuration(milliseconds) {
  const seconds = Math.floor(milliseconds / 1000);
  const hours = String(Math.floor(seconds / 3600)).padStart(2, "0");
  const minutes = String(Math.floor(seconds % 3600 / 60)).padStart(2, "0");
  const remainder = String(seconds % 60).padStart(2, "0");
  return `${hours}:${minutes}:${remainder}`;
}


// デモと本番でテーマ切り替えの動きと配色を共有する。
function toggleHarnessTheme(event) {
  const themeToggle = event.currentTarget;
  const page = themeToggle.ownerDocument;
  const root = page.documentElement;
  const view = page.defaultView;
  const next = root.dataset.theme === "dark" ? "light" : "dark";
  // テーマ、ブラウザーの配色、切り替えボタンの説明を同時に変更する。
  const switchTheme = () => {
    root.dataset.theme = next;
    page.querySelector('meta[name="theme-color"]').content = view.getComputedStyle(root).getPropertyValue("--canvas").trim();
    themeToggle.setAttribute("aria-label", `${next === "dark" ? "ライト" : "ダーク"}テーマに切り替え`);
    themeToggle.innerHTML = next === "dark"
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20.6 15.3A8.7 8.7 0 0 1 8.7 3.4 8.7 8.7 0 1 0 20.6 15.3Z"/></svg>';
  };
  if (view.matchMedia("(prefers-reduced-motion: reduce)").matches || !page.startViewTransition) {
    switchTheme();
    return;
  }
  const rect = themeToggle.getBoundingClientRect();
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  const reach = Math.hypot(Math.max(x, view.innerWidth - x), Math.max(y, view.innerHeight - y)) * 1.3;
  root.style.setProperty("--wipe-x", `${x}px`);
  root.style.setProperty("--wipe-y", `${y}px`);
  root.style.setProperty("--wipe-radius", `${reach}px`);
  const starFrom = Array(10).fill(`${x}px ${y}px`).join(",");
  const starTo = Array.from({length: 10}, (_, i) => {
    const angle = -Math.PI / 2 + i * Math.PI / 5;
    const radius = reach * (i % 2 ? .58 : 1.28);
    return `${x + Math.cos(angle) * radius}px ${y + Math.sin(angle) * radius}px`;
  }).join(",");
  root.style.setProperty("--star-from", starFrom);
  root.style.setProperty("--star-to", starTo);
  root.dataset.wipe = next === "dark" ? "circle" : "star";
  themeToggle.disabled = true;
  const finish = () => {
    themeToggle.disabled = false;
    delete root.dataset.wipe;
  };
  try {
    const transition = page.startViewTransition(switchTheme);
    transition.finished.then(finish, () => { switchTheme(); finish(); });
  } catch {
    switchTheme();
    finish();
  }
}
document.querySelector("#theme-toggle").addEventListener("click", toggleHarnessTheme);

// 比較メニューの項目選択・外側クリック・Escape時の閉じ方を揃える。
function bindComparisonOptions(button, compare) {
  const options = button.closest("details");
  const summary = options.querySelector("summary");
  button.addEventListener("click", () => {
    options.open = false;
    summary.focus();
    compare();
  });
  document.addEventListener("click", event => {
    if (options.open && !options.contains(event.target)) options.open = false;
  });
  options.addEventListener("keydown", event => {
    if (event.key !== "Escape" || !options.open) return;
    event.preventDefault();
    options.open = false;
    summary.focus();
  });
}

const migrationLedgerButton = document.querySelector("#open-migration-ledger");
const migrationLedgerDialog = document.querySelector("#migration-ledger-dialog");
const migrationLedgerList = document.querySelector("#migration-ledger-list");
const migrationLedgerMessage = document.querySelector("#migration-ledger-message");
const migrationLedgerDetails = document.querySelector("#migration-ledger-details");
const migrationCandidateComparison = document.querySelector("#migration-candidate-comparison");
// 受信したカプセルの状態を台帳の既存行に反映する。
function updateMigrationLedgerStatuses(snapshot) {
  if (!migrationLedgerDialog.open) return;
  for (const badge of migrationLedgerList.querySelectorAll("[data-capsule]")) {
    const index = Number(badge.dataset.capsule);
    const current = index === snapshot.current_capsule;
    let status = "pending";
    let label = "未処理";
    if (index <= snapshot.completed_capsules) {
      status = "complete";
      label = "処理済み";
    } else if (current && snapshot.status === "junie_running") {
      status = "running";
      label = "処理中";
    } else if (current && ["interrupted", "junie_failed"].includes(snapshot.status)) {
      status = "paused";
      label = snapshot.status === "interrupted" ? "中断" : "失敗";
    }
    badge.dataset.state = status;
    badge.closest(".migration-ledger-row").dataset.state = status;
    badge.textContent = label;
  }
}

// 対応候補と人の判断を区別し、記録済みの理由を表示する。
function migrationMapping({mapping, review}) {
  const entry = document.querySelector("#migration-mapping-template").content.firstElementChild.cloneNode(true);
  const status = entry.querySelector(".migration-mapping-status");
  const location = entry.querySelector(".migration-mapping-location");
  const confirmation = review?.state || (mapping?.valid || mapping?.snapshot_id ? "candidate" : mapping ? "stale" : "needs_review");
  status.dataset.confirmation = confirmation;
  status.textContent = {confirmed: "人の確認済み", candidate: "候補あり", excluded: "対象外", needs_review: "要確認", stale: "要再確認"}[confirmation];
  if (review?.targets) {
    location.textContent = ` · ${review.targets.join(" / ")}`;
  } else if (mapping) {
    const lines = mapping.start_line === mapping.end_line ? `${mapping.start_line}行目` : `${mapping.start_line}–${mapping.end_line}行`;
    location.textContent = ` · ${mapping.path} ${lines}`;
    if (mapping.snapshot_id) location.append(`（アンカー ${mapping.anchors}件）`);
  }
  if (review) {
    entry.dataset.reviewed = "";
    location.append(` · 理由 ${review.reason}`);
  }
  return entry;
}

// カプセル内のPHP範囲を、元ファイルの行番号とともに並べる。
function migrationLedgerRow(capsule) {
  const row = document.createElement("li");
  row.className = "migration-ledger-row";
  const header = document.createElement("div");
  const title = document.createElement("h3");
  title.textContent = `カプセル ${capsule.index}`;
  const badge = document.createElement("span");
  badge.className = "migration-ledger-state";
  badge.dataset.capsule = capsule.index;
  header.append(title, badge);
  const ranges = document.createElement("ul");
  ranges.setAttribute("role", "list");
  for (const source of capsule.sources) {
    const range = document.createElement("li");
    const path = document.createElement("code");
    path.textContent = source.path;
    const lines = source.start_line === source.end_line
      ? `${source.start_line}行目`
      : `${source.start_line}–${source.end_line}行`;
    const text = document.createElement("span");
    text.append(path, ` · ${lines}`);
    if (source.symbol) text.append(` · ${source.symbol}`);
    if (source.id) text.append(` [${source.id}]`);
    for (const slice of source.character_slices || []) {
      text.append(`（${slice.line}行目の${slice.focus_start_column}–${slice.focus_end_column}文字目）`);
    }
    range.append(text);
    range.append(migrationMapping(source));
    ranges.append(range);
  }
  if (!capsule.sources.length) {
    const range = document.createElement("li");
    range.textContent = "PHPの入力範囲なし";
    ranges.append(range);
  }
  row.append(header, ranges);
  return row;
}


// 前のRunの表示をクリアし、内容を読み込む間も見出しにフォーカスを置く。
function openMigrationLedger(message = "") {
  migrationLedgerList.replaceChildren();
  migrationLedgerDetails.hidden = true;
  migrationLedgerMessage.textContent = message;
  migrationLedgerMessage.hidden = !message;
  migrationLedgerDialog.showModal();
  document.querySelector("#migration-ledger-title").focus();
}

// 閲覧用の台帳と、その対応候補を作った比較への導線を表示する。
function showMigrationLedger(ledger, snapshot, compare) {
  document.querySelector("#migration-ledger-run-id").textContent = ledger.run_id;
  document.querySelector("#migration-ledger-target").textContent = (ledger.targets || [ledger.target]).join(" / ");
  const {complete, verification_passed, reviewed, total} = ledger.completion;
  const progress = `確認済み・対象外 ${reviewed} / ${total} 範囲`;
  const verification = verification_passed ? "ビルド・チェック確認済み" : "ビルド・チェックの確認が必要";
  document.querySelector("#migration-ledger-completion").textContent =
    `${complete ? "移行完了" : "移行未完了"} · ${progress} · ${verification}`;
  migrationLedgerList.replaceChildren(...ledger.capsules.map(migrationLedgerRow));
  migrationLedgerDetails.hidden = false;
  migrationLedgerMessage.hidden = true;
  const snapshotId = ledger.capsules.flatMap(capsule => capsule.sources)
    .find(source => source.mapping?.snapshot_id)?.mapping.snapshot_id;
  migrationCandidateComparison.hidden = !snapshotId;
  migrationCandidateComparison.querySelector("button").onclick = snapshotId ? () => compare(snapshotId) : null;
  updateMigrationLedgerStatuses(snapshot);
}

  function bindProgressPromptDialog({open, submit, inputChanged}) {
    const button = document.querySelector("#open-progress-prompt");
    const dialog = document.querySelector("#progress-prompt-dialog");
    const input = document.querySelector("#progress-prompt");
    const send = document.querySelector("#progress-prompt-submit");
    const timingNote = document.querySelector("#progress-prompt-timing");
    const feedback = document.querySelector("#progress-prompt-feedback");
    const list = document.querySelector("#progress-prompt-list");
    const labels = {queued: "送信待ち", steering: "Junie に送信・処理中", applying: "反映確認中", sending: "Junie が実行中", sent: "反映済み"};
    let renderedPrompts = "";
    function showError(error) {
      feedback.textContent = error?.message || "";
      feedback.hidden = !error;
    }
    function render({timing, prompts = []} = {}, {available = true, loading = false, saving = false} = {}) {
      if (!dialog.open) return;
      const bytes = new TextEncoder().encode(input.value.trim()).length;
      send.disabled = !available || !timing || saving || !bytes || bytes > 4_000;
      input.readOnly = saving;
      const message = bytes > 4_000 ? "指示を 4,000 バイト以下にしてください。"
        : timing === "repair_restart" ? "保存した編集を反映して Junie に指示を渡し直し、対応後に再ビルドします。"
        : timing === "immediate" ? "実行中の Junie にすぐ渡します。反映の完了までお待ちください。"
        : timing === "checkpoint" ? "現在の作業区間が終わると Junie に渡します。"
        : timing === "resume" ? "同じ実行を再開したときに Junie に渡します。"
        : loading ? "指示を読み込んでいます…" : "現在の工程では指示を登録できません。";
      if (timingNote.textContent !== message) timingNote.textContent = message;
      const key = JSON.stringify(prompts);
      if (key === renderedPrompts) return;
      renderedPrompts = key;
      list.replaceChildren(...prompts.map(({text, status}) => {
        const item = document.createElement("li");
        item.append(
          Object.assign(document.createElement("span"), {className: "editor-muted", textContent: labels[status]}),
          Object.assign(document.createElement("p"), {textContent: text}),
        );
        return item;
      }));
      list.hidden = !prompts.length;
    }
    button.addEventListener("click", () => {
      showError();
      dialog.showModal();
      input.focus();
      open();
    });
    document.querySelector("#close-progress-prompt").addEventListener("click", () => dialog.close());
    input.addEventListener("input", inputChanged);
    document.querySelector("#progress-prompt-form").addEventListener("submit", event => {
      event.preventDefault();
      if (!send.disabled) submit(input.value.trim());
    });
    return {button, dialog, input, render, showError};
  }
