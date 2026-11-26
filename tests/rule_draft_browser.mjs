import assert from "node:assert/strict";

const a = "rule-0001.md", b = "rule-0002.md";
const idSelector = "rules-drafts", sourceSelector = "rules-sources";
const saveButton = "rules-save-source", discardButton = "rules-discard-source", input = "rules-text";

export async function startRuleDraft({evaluate, wait, field}) {
  try {
    const {previous} = await wait(`(() => {
      const button = ${field("rules-start")};
      if (!button?.matches(":enabled")) return false;
      const previous = ${field(idSelector)}.value;
      button.click();
      return {previous};
    })()`, undefined, "click the enabled new rule draft button");
    const id = await wait(`(() => {
      const selected = ${field(idSelector)}.value;
      return selected && selected !== ${JSON.stringify(previous)} && !${field(saveButton)}.disabled && selected;
    })()`, undefined, "wait for the new rule draft and its source editor");
    return {id, previous};
  } catch (error) {
    const state = await evaluate(`({
      draft: ${field(idSelector)}?.value, startDisabled: ${field("rules-start")}?.disabled,
      saveDisabled: ${field(saveButton)}?.disabled, status: ${field("rules-status")}?.textContent,
      stopError: ${field("rules-stop-error")}?.textContent,
    })`).catch(() => null);
    throw new Error(error.message + "; last rule draft state: " + JSON.stringify(state), {cause: error});
  }
}

async function selectRuleSource({wait, field}, picker, item) {
  await wait(`(() => {
    const select = ${field(picker)}, value = ${JSON.stringify(item)};
    if (!select || select.disabled || !Array.from(select.options).some(option => option.value === value)) return false;
    select.value = value;
    select.dispatchEvent(new Event("change", {bubbles: true}));
    return true;
  })()`, undefined, "choose an available rule draft or source: " + picker);
  await wait(`${field(picker)}.value === ${JSON.stringify(item)} && !${field(saveButton)}.disabled`,
    undefined, "wait for the selected rule draft or source: " + picker);
}

async function withDraft(browser, check) {
  const {evaluate, wait, field, change, click} = browser;
  const ready = () => wait("!" + field(saveButton) + ".disabled");
  const {id, previous} = await startRuleDraft(browser);
  const start = async () => (await startRuleDraft(browser)).id;
  const select = (picker, item) => selectRuleSource(browser, picker, item);
  const type = text => change(input, text, "input");
  const save = async () => { await click(saveButton); await ready(); };
  const get = source => evaluate(`fetch(${JSON.stringify("rule-workspace?" + new URLSearchParams({id, ...(source ? {source} : {})}))})
    .then(r => r.json())`);
  const history = () => evaluate("ruleDraftIO.posts");
  await evaluate("(" + function () {
    const original = window.fetch;
    const confirm = window.confirm;
    const io = window.ruleDraftIO = {posts: [], permit: false, fetch: original.bind(window),
      restore: () => { window.fetch = original; window.confirm = confirm; }};
    window.confirm = () => io.permit;
    window.fetch = async (url, options) => {
      const body = url === "rule-workspace" && options?.body && JSON.parse(options.body);
      if (body?.action === "edit" && io.failNext) {
        io.failNext = false;
        return new Response("fixture save failed", {status: 409});
      }
      const response = await original(url, options);
      if (body) io.posts.push({body, status: response.status});
      if (body?.action === "edit" && response.ok && io.dropNext) {
        io.dropNext = false;
        await response.arrayBuffer();
        throw new TypeError("fixture lost saved response");
      }
      return response;
    };
  }.toString() + ")()");
  try { await check({id, get, select, ready, start, type, save, history}); }
  finally {
    await evaluate("ruleDraftIO.restore()");
    if (previous) await select(idSelector, previous);
  }
}

export async function checkRuleDraftProtection(browser) {
  const {evaluate, wait, field, click, value} = browser;
  await withDraft(browser, async ({id, get, select, start, type, save, history}) => {
    const original = await get(a);
    const manual = "# 未保存でも原文の切り替えで残す\n";
    await type(manual);
    await select(sourceSelector, b);
    await select(sourceSelector, a);
    assert.equal(await value(input), manual);
    await select(sourceSelector, b);
    await click("rules-preview");
    await wait(field("rules-status") + ".textContent.includes('未保存')");
    assert(!(await history()).some(({body}) => body.action === "preview"));
    await select(sourceSelector, a);
    assert.equal(await value(input), manual);
    await start();
    assert.equal(await value(input), original.text.replaceAll("\r\n", "\n"));
    await select(idSelector, id);
    assert.equal(await value(input), manual);
    await evaluate("ruleDraftIO.failNext = true");
    await save();
    await wait(field("rules-status") + ".textContent.includes('fixture save failed')");
    assert.equal(await value(input), manual);
    assert.deepEqual(await get(a), original);
    await save();
    assert.equal((await get(a)).text.replaceAll("\r\n", "\n"), manual);
    await click("rules-preview");
    await wait("!" + field("rules-review") + ".hidden");
    assert((await get()).preview.text_diff.includes(manual.trim()));
    await type("# プレビュー後に手書きした内容\n");
    await click("rules-apply");
    await wait(field("rules-status") + ".textContent.includes('未保存')");
    assert(!(await history()).some(({body}) => body.action === "apply"));
    await click(discardButton);
    assert.equal(await value(input), "# プレビュー後に手書きした内容\n");
    await evaluate("ruleDraftIO.permit = true");
    await click(discardButton);
    await wait(field(input) + ".value === " + JSON.stringify(manual));
  });
  console.log("Browser RuleWorkspace: unsaved Markdown lives across source/draft/new switches and blocks preview or apply");
}

export async function checkRuleSaveRecovery(browser) {
  const {evaluate, wait, field, value} = browser;
  await withDraft(browser, async ({id, get, select, type, save, history}) => {
    const lose = async () => {
      await evaluate("ruleDraftIO.dropNext = true");
      await save();
      await wait(field("rules-status") + ".textContent.includes('fixture lost saved response')");
    };
    await type("# Saved A\n");
    await lose();
    assert.equal((await get()).revision, 1);
    await save();
    assert.equal((await get()).revision, 1);
    assert.deepEqual((await history()).slice(0, 2).map(item => item.status), [200, 200]);
    assert.deepEqual((await history())[0].body, (await history())[1].body);
    await type("# Saved B\n");
    await lose();
    await select(sourceSelector, b);
    await select(sourceSelector, a);
    await type("# Saved B\nPlus more\n");
    await save();
    const additional = (await history()).slice(2);
    assert.deepEqual(additional.map(item => item.status), [200, 200, 200]);
    assert.deepEqual(additional[0].body, additional[1].body);
    assert.notEqual(additional[1].body.save_id, additional[2].body.save_id);
    assert.equal((await get(a)).text.replaceAll("\r\n", "\n"), "# Saved B\nPlus more\n");

    await type("# Last accepted local revision\n");
    await lose();
    const server = await get(a);
    const foreign = {id, action: "edit", source: a, revision: server.revision, text: "# Foreign\n"};
    assert(await evaluate(`ruleDraftIO.fetch("rule-workspace", {method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify(${JSON.stringify(foreign)})}).then(r => r.ok)`));
    await type("# Newer unsaved draft\n");
    await save();
    assert.equal((await history()).at(-1).status, 409);
    assert.equal(await value(input), "# Newer unsaved draft\n");
    assert.equal((await get(a)).text.replaceAll("\r\n", "\n"), "# Foreign\n");

    await select(sourceSelector, "new");
    const raw = "\ufeff# Original imported bytes\r\nExtra\r\n";
    await evaluate(`(() => {
      const picker = ${field("rules-import")}, transfer = new DataTransfer();
      transfer.items.add(new File([${JSON.stringify(raw)}], "operator.md"));
      picker.files = transfer.files;
      picker.dispatchEvent(new Event("change"));
    })()`);
    await wait(field("rules-name") + ".value === 'operator.md'");
    await select(sourceSelector, b);
    await select(sourceSelector, "new");
    assert.equal(await value("rules-name"), "operator.md");
    assert.equal(await value(input), "# Original imported bytes\nExtra\n");
    await lose();
    await save();
    const state = await get();
    assert.equal(state.files.filter(file => file.name === "operator.md").length, 1);
    const recent = (await history()).slice(-2);
    assert.deepEqual(recent.map(item => item.status), [200, 200]);
    assert.deepEqual(recent[0].body, recent[1].body);
    assert.equal(recent[1].body.bytes, Buffer.from(raw).toString("base64"));
  });
  console.log("Browser RuleWorkspace: own lost receipts retry without incrementing twice; additive saves and foreign revisions stay distinct");
}
