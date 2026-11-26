import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";

export async function checkRuleProposalAdoption({evaluate, wait, field, change, click, value}, config, draftId) {
  const project = path.dirname(config);
  const root = path.join(project, "runs", ".rule-workspace");
  const [pack] = await fs.readdir(root);
  const draft = path.join(root, pack, draftId);
  const stateFile = path.join(draft, "state.json");
  const readState = async () => JSON.parse(await fs.readFile(stateFile, "utf8"));
  const writeState = state => fs.writeFile(stateFile, JSON.stringify(state));
  const bytes = file => fs.readFile(path.join(draft, "sources", file));
  const text = async file => (await bytes(file)).toString("utf8").replace(/^\uFEFF/, "").replaceAll("\r\n", "\n");
  const {files: [{source: a}, {source: b}]} = await readState();
  const originals = ["app/z-first.md", "app/a-second.md", path.basename(config)].map(name => path.join(project, name));
  const before = await Promise.all(originals.map(name => fs.readFile(name)));
  const [originalA, originalB] = await Promise.all([text(a), text(b)]);
  const source = "rules-sources", input = "rules-text", accept = "rules-accept";
  const ready = () => wait("!" + field("rules-save-source") + ".disabled");
  const save = async () => { await click("rules-save-source"); await ready(); };
  const manualA = "# 手入力した未保存のA\n";
  const proposedB = "# Junieの提案を採用したB\n";
  const present = async ({source: target, name, text: proposal}, selected) => {
    const state = await readState();
    state.proposal = {source: target, name, text: proposal};
    await writeState(state);
    await change("rules-drafts", "");
    await change("rules-drafts", draftId);
    await wait("!" + field("rules-proposal") + ".hidden");
    await ready();
    await change(source, selected);
    await ready();
  };
  await evaluate("(" + function () {
    const original = window.fetch;
    const confirm = window.confirm;
    const io = window.ruleProposalIO = {accepts: [], prompts: [], permit: false,
      restore: () => { window.fetch = original; window.confirm = confirm; }};
    window.confirm = message => { io.prompts.push(message); return io.permit; };
    window.fetch = async (url, options) => {
      const response = await original(url, options);
      if (url === "rule-workspace" && options?.body && JSON.parse(options.body).action === "accept") {
        io.accepts.push({revision: JSON.parse(options.body).revision, status: response.status});
        if (io.holdNext) {
          io.holdNext = false;
          return new Promise(resolve => { io.release = () => resolve(response); });
        }
      }
      return response;
    };
  }.toString() + ")()");
  try {
    await present({source: b, text: proposedB}, a);
    await change(input, manualA, "input");
    await evaluate("ruleProposalIO.holdNext = true");
    await click(accept);
    await wait("!!ruleProposalIO.release");
    assert(await evaluate([source, input, "rules-drafts", "rules-save-source"].map(id => field(id) + ".disabled").join(" && ")));
    await click(accept);
    assert.deepEqual(await evaluate("ruleProposalIO.accepts"), [{revision: 0, status: 200}]);
    assert.equal(await value(source), a);
    assert.equal(await value(input), manualA);
    await evaluate("ruleProposalIO.release()");
    await ready();
    assert.equal(await value(source), a);
    assert.equal(await value(input), manualA, "adopting B must not replace unsaved text in A");
    assert.equal(await text(a), originalA);
    assert.notEqual(proposedB, originalB);
    assert.equal(await text(b), proposedB);
    assert.equal(await evaluate("ruleProposalIO.prompts.length"), 0);
    await save();
    assert.equal(await text(a), manualA);
    assert.equal(await text(b), proposedB);

    const proposedA = "# Junieの提案を採用したA\n";
    await present({source: a, text: proposedA}, a);
    await change(input, "# 同じファイルの未保存本文\n", "input");
    await click(accept);
    assert.equal(await evaluate("ruleProposalIO.accepts.length"), 1);
    assert.match((await evaluate("ruleProposalIO.prompts"))[0], /未保存/);
    assert.equal(await value(input), "# 同じファイルの未保存本文\n");
    assert.equal(await text(a), manualA);
    await evaluate("ruleProposalIO.permit = true");
    const changed = await readState();
    changed.revision++;
    await writeState(changed);
    await click(accept);
    await wait(field("rules-status") + ".textContent.includes('再読み込み')");
    assert.equal((await evaluate("ruleProposalIO.accepts.at(-1)")).status, 409);
    assert.equal(await value(input), "# 同じファイルの未保存本文\n");
    assert.equal(await text(a), manualA);
    await present({source: a, text: proposedA}, a);
    await change(input, "# 明示的に破棄できる編集\n", "input");
    await click(accept);
    await wait(field(input) + ".value === " + JSON.stringify(proposedA));
    await ready();
    assert.equal(await text(a), proposedA);

    await present({source: "new", name: "proposed.md", text: "# 提案による新規作成\n"}, "new");
    const manualBytes = "\ufeff# 新規の手入力\r\n";
    await evaluate(`(() => {
      const transfer = new DataTransfer();
      transfer.items.add(new File([${JSON.stringify(manualBytes)}], "operator.md"));
      const picker = ${field("rules-import")};
      picker.files = transfer.files;
      picker.dispatchEvent(new Event("change"));
    })()`);
    await wait(field("rules-name") + ".value === 'operator.md'");
    await ready();
    await click(accept);
    await ready();
    assert.equal(await value(source), "new");
    assert.equal(await value("rules-name"), "operator.md");
    assert.equal(await value(input), "# 新規の手入力\n");
    await save();
    assert.deepEqual(await bytes(await value(source)), Buffer.from(manualBytes));
    const proposal = (await readState()).files.find(file => file.name === "proposed.md");
    assert.equal(await text(proposal.source), "# 提案による新規作成\n");
    assert.deepEqual(await Promise.all(originals.map(name => fs.readFile(name))), before);
    console.log("Browser RuleWorkspace: real proposal adoption preserves other and new Markdown, confirms same-file edits and keeps text on conflicts");
  } finally {
    await evaluate("ruleProposalIO.release?.(); ruleProposalIO.restore()");
  }
}
