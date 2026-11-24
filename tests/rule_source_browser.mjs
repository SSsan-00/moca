import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";

export async function checkRuleSourceEditor({evaluate, wait, field, change, click, value}, config, draftId) {
  const sources = "rules-sources", text = "rules-text", save = "rules-save-source";
  const a = "rule-0001.md", b = "rule-0002.md";
  const root = path.dirname(config);
  const workspace = path.join(root, "runs", ".rule-workspace");
  const [pack] = await fs.readdir(workspace);
  const draft = name => fs.readFile(path.join(workspace, pack, draftId, "sources", name));
  const original = name => fs.readFile(path.join(root, "app", name));
  const [originalA, originalB, toml] = await Promise.all([
    original("z-first.md"), original("a-second.md"), fs.readFile(config),
  ]);
  const select = source => change(sources, source);
  const ready = () => wait("!" + field(save) + ".disabled");
  const status = field("rules-status") + ".textContent";
  await evaluate("(" + function () {
    const original = window.fetch;
    const io = window.ruleIO = {writes: []};
    io.restore = () => { window.fetch = original; };
    window.fetch = async (url, options) => {
      if (String(url).includes("rule-workspace?") && String(url).includes("source=") && io.nextRead) {
        const mode = io.nextRead;
        io.nextRead = null;
        if (mode === "fail") return new Response("fixture read error", {status: 500});
        const result = await original(url, options);
        return new Promise(resolve => { io.release = () => resolve(result); });
      }
      if (url === "rule-workspace" && options?.body) {
        const action = JSON.parse(options.body);
        if (action.action === "edit") {
          io.writes.push(action);
          const mode = io.nextWrite;
          io.nextWrite = null;
          if (mode === "offline") throw Error("fixture offline");
          if (mode) return new Response("fixture " + mode, {status: mode});
        }
      }
      return original(url, options);
    };
  }.toString() + ")()");
  try {
    await evaluate("ruleIO.nextRead = 'delay'");
    await select(b);
    await wait("!!ruleIO.release");
    assert(await evaluate(field(save) + ".disabled"));
    await evaluate(field(save) + ".dispatchEvent(new Event('click'))");
    assert.equal(await evaluate("ruleIO.writes.length"), 0);
    await select(a);
    await ready();
    await evaluate("ruleIO.nextRead = 'fail'");
    await select(b);
    await wait(status + " === 'fixture read error'");
    assert(await evaluate(field(save) + ".disabled"));
    await evaluate(field(save) + ".dispatchEvent(new Event('click'))");
    assert.equal(await evaluate("ruleIO.writes.length"), 0);
    assert.deepEqual(await draft(b), originalB);
    await select(a);
    await ready();
    await select(b);
    await ready();
    assert.equal(await value(text), "# 二番目\n");
    await change(text, "B の改訂\n", "input");
    await click(save);
    await ready();
    await evaluate("ruleIO.release()");
    assert.equal(await value(text), "B の改訂\n");
    assert.equal((await draft(b)).toString(), "B の改訂\n");

    await select("new");
    await ready();
    await evaluate("(" + function () {
      const transfer = new DataTransfer();
      transfer.items.add(new File(["\ufeff追加\r\n例外\r\n"], "import.md"));
      const picker = document.querySelector("#rules-import");
      picker.files = transfer.files;
      picker.dispatchEvent(new Event("change"));
    }.toString() + ")()");
    await wait(field("rules-name") + ".value === 'import.md'");
    const body = await value(text);
    assert.equal(body, "追加\n例外\n");
    for (const [name, failure] of [["", null], ["bad.exe", null], ["ok.md", 409], ["ok.md", 500], ["ok.md", "offline"]]) {
      await change("rules-name", name, "input");
      await evaluate("ruleIO.nextWrite = " + JSON.stringify(failure));
      await click(save);
      await wait(status + ".includes(" + JSON.stringify(failure ? "fixture " + failure : "Markdownまたは") + ")");
      await ready();
      assert.equal(await value(sources), "new");
      assert.equal(await value("rules-name"), name);
      assert.equal(await value(text), body);
    }
    await click(save);
    await wait(field(sources) + ".selectedOptions[0]?.textContent === 'ok.md'");
    await ready();
    const bytes = Buffer.from("\ufeff追加\r\n例外\r\n");
    const attempts = await evaluate("ruleIO.writes.filter(item => item.source === 'new')");
    assert.equal(attempts.length, 6);
    assert(attempts.every(item => item.bytes === bytes.toString("base64")));
    assert.deepEqual(await draft(await value(sources)), bytes);
    assert.deepEqual(await draft(a), originalA);
    assert.equal((await draft(b)).toString(), "B の改訂\n");
    assert.deepEqual(await original("z-first.md"), originalA);
    assert.deepEqual(await original("a-second.md"), originalB);
    assert.deepEqual(await fs.readFile(config), toml);
    const selected = await value(sources);
    assert(await evaluate(`(async () => {
      const id = ${JSON.stringify(draftId)}, source = ${JSON.stringify(selected)};
      const {revision} = await (await fetch("rule-workspace?id=" + id)).json();
      return (await fetch("rule-workspace", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({action: "edit", id, revision, source, text: "別の画面から更新"})})).ok;
    })()`));
    await change(text, "古い版からの編集", "input");
    await click(save);
    await ready();
    assert((await evaluate(status)).includes("再読み込み"));
    assert.equal(await value(text), "古い版からの編集");
    await select(a);
    await ready();
    await select(selected);
    await ready();
    assert.equal(await value(text), "別の画面から更新");
    await change(text, "最新の版から編集", "input");
    await click(save);
    await ready();
    assert.equal((await draft(selected)).toString("utf8"), "\ufeff最新の版から編集");
  } finally {
    await evaluate("ruleIO.restore()");
  }
}
