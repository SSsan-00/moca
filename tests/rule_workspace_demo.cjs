const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function load(live) {
  const window = {};
  vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), {
    document: {documentElement: {hasAttribute: () => live}},
    window,
    structuredClone,
  });
  return window.harnessRuleWorkspaceDemo;
}

assert.equal(load(true), undefined);
const demo = load(false);
const start = demo(null, {action: "start"});
const {id} = start;
const action = (name, extra = {}) => demo(null, {id, action: name, ...extra});
assert.match(demo({id, source: "orders"}).text, /確定済み/);
const answer = action("optimize", {text: "例外を整理して"});
assert.match(answer.messages[1].text, /サンプル応答/);
assert.doesNotMatch(demo({id, source: "orders"}).text, /決済前/);
action("accept");
assert.match(demo({id, source: "orders"}).text, /決済前/);
const preview = action("preview").preview;
assert.match(preview.text_diff, /決済前/);
assert.match(preview.text_diff, /^ # 注文ステータス$/m);
assert.match(preview.text_diff, /^ - 確定済みの注文は取り消せない。$/m);
assert.match(preview.text_diff, /^\+- ただし決済前/m);
assert.doesNotMatch(preview.text_diff, /^- # 注文ステータス$/m);
assert.match(preview.toml_diff, /project\.migration_rules/);
assert.throws(() => action("apply", {preview: preview.token, confirmed: false}), /チェック/);
assert.equal(action("apply", {preview: preview.token, confirmed: true}).applied, true);
assert.equal(demo({}).drafts.length, 0);
assert.doesNotMatch(demo(null, {action: "start"}).files[0].text, /決済前/);
