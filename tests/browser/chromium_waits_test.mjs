import assert from "node:assert/strict";
import {test} from "node:test";
import vm from "node:vm";
import {beginPhase, eventually} from "./chromium_browser.mjs";
import {startRuleDraft} from "./rule_draft_browser.mjs";

function ruleFixture({reject = false, hold = false} = {}) {
  const begin = {disabled: hold, matches() { return !this.disabled; }, click() {
    if (this.disabled) return;
    submissions++;
    if (reject) status.textContent = "fixture rejected start";
    else {
      picker.value = "new-draft";
      save.disabled = true;
    }
  }};
  const picker = {value: "previous-draft"};
  const save = {disabled: false}, status = {textContent: ""};
  let submissions = 0, blockedPolls = 0, editorPolls = 0;
  const nodes = new Map([["rules-start", begin], ["rules-drafts", picker], ["rules-save-source", save], ["rules-status", status]]);
  const evaluate = expression => vm.runInNewContext(expression, {document: {getElementById: id => nodes.get(id)}});
  const browser = {
    field: id => "document.getElementById(" + JSON.stringify(id) + ")",
    evaluate: async expression => evaluate(expression),
    async wait(expression, unused, label) {
      for (let count = 0; count < 5; count++) {
        const result = evaluate(expression);
        if (result) return result;
        if (begin.disabled && ++blockedPolls === 2) begin.disabled = false;
        if (save.disabled && ++editorPolls === 2) save.disabled = false;
      }
      throw new Error("Timed out: " + label);
    },
  };
  return {browser, begin, picker, counts: () => [submissions, blockedPolls, editorPolls]};
}

test("a busy draft button does not consume the only click or accept an editor still loading", async () => {
  const fixture = ruleFixture({hold: true});
  fixture.begin.click();
  assert.deepEqual(fixture.counts(), [0, 0, 0], "the old unconditional click would be dropped");
  assert.deepEqual(await startRuleDraft(fixture.browser), {previous: "previous-draft", id: "new-draft"});
  assert.deepEqual(fixture.counts(), [1, 2, 2]);
});

test("a failed draft submission reports the actual unchanged selection and status without retrying", async () => {
  const fixture = ruleFixture({reject: true});
  await assert.rejects(startRuleDraft(fixture.browser), error => {
    assert.match(error.message, /wait for the new rule draft and its source editor/);
    assert.match(error.message, /"draft":"previous-draft"/);
    assert.match(error.message, /"status":"fixture rejected start"/);
    return true;
  });
  assert.deepEqual(fixture.counts(), [1, 0, 0]);
});

test("a stalled condition is printed before its final failure", async () => {
  const log = [];
  const original = console.log;
  console.log = value => log.push(value);
  try {
    beginPhase("fixture operation");
    await assert.rejects(eventually(() => false, "fixture condition", 23), /waited [0-9.]+s/);
  } finally {
    console.log = original;
  }
  assert(log.some(line => line.includes("start fixture operation")));
  assert(log.some(line => line.includes("still waiting in fixture operation: fixture condition")));
});
