import assert from "node:assert/strict";
import {createServer} from "node:http";

export async function checkBuildReviewDrafts({url, evaluate, click, change, value, wait}) {
  const targets = ["Main.cs", "Other.cs"];
  const primary = targets[0];
  const versions = new Map();
  const posts = [];
  let holdRead, holdPost;
  const server = createServer(async (req, res) => {
    res.setHeader("Access-Control-Allow-Origin", new URL(url).origin);
    res.setHeader("Access-Control-Allow-Headers", "X-Run-ID, Content-Type");
    if (req.method === "OPTIONS") return res.end();
    const route = new URL(req.url, "http://localhost");
    const id = req.headers["x-run-id"];
    const target = route.searchParams.get("target");
    const version = versions.get(id + target) || "1".repeat(64);
    const json = (body, status = 200) => {
      res.writeHead(status, {"Content-Type": "application/json"});
      res.end(JSON.stringify(body));
    };
    if (req.method === "GET") {
      if (id !== route.searchParams.get("run_id")) return json({}, 409);
      if (target && holdRead) { const hold = holdRead; holdRead = null; await hold(); }
      return json(target ? {content: id + ":" + target + ":" + version[0], version, max_bytes: 4096,
        bom_bytes: 0, crlf: true} : {targets, preferred_target: primary, errors: ["fixture"], log: "",
        review_kind: id === "A" ? "build" : "roslyn_preflight"});
    }
    let body = "";
    for await (const part of req) body += part;
    const data = JSON.parse(body);
    posts.push({id, data});
    if (route.pathname === "/editor") {
      const expected = versions.get(id + data.target) || "1".repeat(64);
      if (data.run_id !== id || data.base_version !== expected) return json({}, 409);
      versions.set(id + data.target, "4".repeat(64));
      return json({run_id: id, content: data.content, accepted_save_id: data.save_id, queued: false});
    }
    const expected = versions.get(id + data.edit?.target) || "1".repeat(64);
    if (holdPost) { const hold = holdPost; holdPost = null; await hold(); }
    return json({}, data.run_id === id && (!data.edit || data.edit.version === expected) ? 202 : 409);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    const origin = "http://127.0.0.1:" + server.address().port + "/";
    await evaluate(`stream.close(); const actualReviewFetch = dashboardIO.fetch;
      dashboardIO.fetch = (path, options) => /^build-(review|response)\\b/.test(path)
        || (path === "editor" && options?.body && JSON.parse(options.body).base_version)
        ? fetch(${JSON.stringify(origin)} + path, options) : actualReviewFetch(path, options);`);
    let revision = 100;
    const select = (id, extra = {}) => evaluate(`receiveProgress({lastEventId: ${++revision},
      data: ${JSON.stringify(JSON.stringify({run_id: id, ready: true, resumable: true, status: "build_failed",
        review_kind: id === "A" ? "build" : "roslyn_preflight", ...extra}))}})`);
    const field = part => "build-review-" + part;
    const open = async () => {
      await click("toggle");
      await wait("document.getElementById('build-review-dialog').open && !document.getElementById('build-review-submit').disabled");
    };
    const close = () => click(field("close"));
    const type = (part, text) => change(field(part), text, "input");
    const expect = async (text, instruction) => {
      assert.equal(await value(field("source")), text);
      assert.equal(await value(field("instruction")), instruction);
    };
    const deferred = () => {
      let started, resume;
      const called = new Promise(resolve => { started = resolve; });
      const release = new Promise(resolve => { resume = resolve; });
      return {called, resume, hold: () => { started(); return release; }};
    };
    for (const id of ["A", "B"]) {
      await select(id);
      await open();
      await expect(id + ":Main.cs:1", "");
      await type("source", id + " draft");
      await type("instruction", id + " instruction");
      await close(); await open();
      await expect(id + " draft", id + " instruction");
      await close();
    }
    versions.set("AMain.cs", "2".repeat(64));
    await select("A"); await open();
    await expect("A draft", "A instruction");
    assert.equal(await evaluate("document.getElementById('build-review-target').disabled"), false);
    await change(field("target"), targets[1]);
    await wait("document.getElementById('build-review-source').value === 'A:Other.cs:1'");
    await type("source", "A second draft");
    await change(field("target"), primary);
    await expect("A draft", "A instruction");
    await click(field("submit"));
    await wait("document.getElementById('build-review-feedback').textContent.includes('409')");
    assert.equal(posts.at(-1).id, "A");
    assert.deepEqual(posts.at(-1).data.edit, {target: primary, content: "A draft", version: "1".repeat(64)});
    assert.equal(posts.at(-1).data.instruction, "A instruction");
    await expect("A draft", "A instruction");
    await close(); await select("B"); await open();
    await expect("B draft", "B instruction");
    const delayed = deferred();
    holdPost = delayed.hold;
    await click(field("submit")); await delayed.called;
    await close(); await select("A");
    delayed.resume();
    await wait("!document.getElementById('toggle').disabled"); await open();
    await wait("!document.getElementById('build-review-submit').disabled");
    assert.equal(await evaluate("document.getElementById('build-review-dialog').open"), true);
    assert.equal(posts.at(-1).id, "B");
    assert.equal(posts.at(-1).data.run_id, "B");
    assert.deepEqual(posts.at(-1).data.edit, {target: primary, content: "B draft", version: "1".repeat(64)});
    assert.equal(posts.at(-1).data.instruction, "B instruction");
    await expect("A draft", "A instruction");
    await close(); await select("B", {error_details: "late worker failure", message: "not saved"}); await open();
    await expect("B draft", "B instruction");
    await click(field("submit"));
    await wait("!document.getElementById('build-review-dialog').open");
    await select("B", {ready: false, queued: true}); await select("B"); await open();
    await expect("B draft", "B instruction");
    const lateSuccess = deferred();
    holdPost = lateSuccess.hold;
    await click(field("submit")); await lateSuccess.called;
    await select("B", {ready: false, status: "repairing", operation_id: "fixture-B"});
    versions.set("BMain.cs", "3".repeat(64));
    await select("B"); lateSuccess.resume();
    await wait("!document.getElementById('toggle').disabled"); await open();
    await expect("B:Main.cs:3", "");
    const slow = deferred();
    holdRead = slow.hold;
    await change(field("target"), targets[1]); await slow.called;
    await close(); await select("A"); await open();
    slow.resume();
    await type("instruction", "later A instruction");
    await change(field("target"), targets[1]);
    await expect("A second draft", "later A instruction");
    await click(field("submit"));
    await wait("!document.getElementById('build-review-dialog').open");
    assert.equal(posts.at(-1).data.edit.target, targets[1]);
    const completed = {status: "build_succeeded"};
    const unloadBlocked = () => evaluate("!dispatchEvent(new Event('beforeunload', {cancelable: true}))");
    const recover = async () => {
      assert.equal(await evaluate("snapshot.status"), "build_succeeded");
      assert(await evaluate("!document.getElementById('open-build-drafts')?.hidden && !!document.getElementById('open-build-drafts')"),
        "the successful Run must expose its remaining draft");
      await click("open-build-drafts");
      await wait("document.getElementById('build-review-dialog').open && !document.getElementById('build-review-submit').disabled");
      assert.equal(await value(field("target")), primary);
    };
    await select("A", completed);
    assert(await unloadBlocked(), "the browser protects drafts outside Monaco");
    await recover();
    await expect("A draft", "");
    await click(field("submit"));
    await wait("document.getElementById('build-review-feedback').textContent.includes('409')");
    assert.equal(posts.at(-1).id, "A");
    assert.equal(posts.at(-1).data.base_version, "1".repeat(64));
    assert.equal(posts.at(-1).data.content, "A draft");
    assert.equal(await evaluate("snapshot.status"), "build_succeeded");
    await expect("A draft", "");
    await close();
    await select("B", completed);
    assert.equal(await evaluate("document.getElementById('open-build-drafts').hidden"), true);
    await select("A", completed); await recover();
    await evaluate("window.confirm = () => false");
    await click(field("reset"));
    await expect("A draft", "");
    await evaluate("window.confirm = () => true");
    await click(field("reset"));
    await wait("!document.getElementById('build-review-dialog').open");
    assert.equal(await unloadBlocked(), false);

    await select("C"); await open();
    await type("source", "C first file");
    await change(field("target"), targets[1]);
    await wait("document.getElementById('build-review-source').value === 'C:Other.cs:1'");
    await type("source", "C second file");
    await click(field("submit"));
    await wait("!document.getElementById('build-review-dialog').open");
    assert.equal(posts.at(-1).data.edit.target, targets[1]);
    await select("C", completed);
    await recover();
    await expect("C first file", "");
    await click(field("submit"));
    await wait("!document.getElementById('build-review-dialog').open");
    assert.equal(posts.at(-1).id, "C");
    assert.equal(posts.at(-1).data.kind, "save");
    assert.equal(posts.at(-1).data.target, primary);
    assert.equal(posts.at(-1).data.base_version, "1".repeat(64));
    assert.equal(posts.at(-1).data.content, "C first file");
    assert.equal(await evaluate("snapshot.status"), "build_succeeded");
    assert.equal(await evaluate("document.getElementById('open-build-drafts').hidden"), true);
    assert.match(await evaluate("document.getElementById('state-label').textContent"), /要再検証/);
    assert.equal(await unloadBlocked(), false);
    console.log("Browser build review: per-Run/per-target drafts, original versions, rejected/late POST and stale reads");
  } finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}
