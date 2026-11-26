const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const timers = new Map();
let timerSequence = 0;
function load(live) {
  const window = {};
  vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), {
    document: {documentElement: {hasAttribute: () => live}},
    window,
    structuredClone,
    setTimeout: action => { timers.set(++timerSequence, action); return timerSequence; },
    clearTimeout: timer => timers.delete(timer),
  });
  return window.harnessRuleWorkspaceDemo;
}

assert.equal(load(true), undefined);
const demo = load(false);
const start = demo(null, {action: "start"});
const {id} = start;
const action = (name, extra = {}) => demo(null, {id, action: name, ...extra});
assert.match(demo({id, source: "orders"}).text, /確定済み/);
const running = action("optimize", {text: "例外を整理して"});
assert.equal(running.live.status, "running");
assert.equal(action("stop").live, null);
assert.match(demo({}).stop_error, /終了を確認できません/);
assert.throws(() => action("message", {text: "再開"}), /再確認/);
assert.equal(timers.size, 0);
assert.equal(action("retry-stop").stop_error, undefined);
assert.equal(action("optimize", {text: "例外を整理して"}).live.status, "running");
const [timer, reply] = [...timers][0];
timers.delete(timer);
reply();
const answer = demo({id});
assert.match(answer.messages.at(-1).text, /サンプル応答/);
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
