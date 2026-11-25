import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";

export async function checkEditorSemantics({evaluate, wait, sourceEditor, config, run}) {
  const status = "document.getElementById('editor-diagnostic-status').textContent";
  const note = "document.getElementById('editor-diagnostic-note').textContent";
  const markers = "monaco.editor.getModelMarkers({owner:'syntax'})";
  const list = "document.getElementById('editor-diagnostic-list').textContent";
  const source = () => evaluate(sourceEditor + ".getValue()");
  const set = text => evaluate(sourceEditor + ".setValue(" + JSON.stringify(text) + ")");
  const select = async (file, text) => {
    await evaluate("document.querySelector('#editor-target-buttons [data-target="
      + JSON.stringify(file) + "]').click()");
    await wait(sourceEditor + "?.getValue() === " + JSON.stringify(text));
  };
  const tree = path.join(path.dirname(config), "trees", run);
  const savedCSharp = await source();
  const razorPath = "Pages/Index.cshtml";
  const codeBehindPath = razorPath + ".cs";
  const [savedRazor, savedCodeBehind, descriptor] = await Promise.all(
    [razorPath, codeBehindPath, "App.csproj"].map(file => fs.readFile(path.join(tree, file), "utf8")));

  const assertMarker = async (code, line, column) => {
    await wait(list + ".includes(" + JSON.stringify(code) + ")");
    assert.deepEqual(await evaluate(markers + ".map(m => [m.code, m.startLineNumber, m.startColumn])"),
      [[code, line, column]]);
  };

  await set("class Working { string Text => 42; }");
  await assertMarker("CS0029", 1, 32);
  assert.equal(await evaluate("fetch('editor').then(r => r.json()).then(d => d.content)"), savedCSharp);
  await set(savedCSharp);
  await select(codeBehindPath, savedCodeBehind);
  await set("namespace Sample; public class IndexModel { public BogusType Value => default; }");
  await assertMarker("CS0246", 1, 52);
  await set(savedCodeBehind);

  await select(razorPath, savedRazor);
  await wait(status + ".includes('構文・型・参照: 指摘なし')");
  await set(savedRazor.replace("@model IndexModel", "@model Sample.MissingModel"));
  await wait(status + ".includes('型・参照: 未確認')");
  assert.match(await evaluate(note), /Razor のモデル型/);
  assert.equal(await evaluate(markers + ".length"), 0);
  for (const [directive, code] of [["@inject Sample.MissingService Service", "CS0234"],
    ["@inherits MissingRazorBase", "CS0246"]]) {
    await set(savedRazor.replace("@page\n", "@page\n" + directive + "\n"));
    await wait(list + ".includes(" + JSON.stringify(code) + ")");
    assert(await evaluate(markers + ".some(m => m.startLineNumber === 2 && m.code === " + JSON.stringify(code) + ")"));
  }
  await set(savedRazor);
  await wait(status + ".includes('構文・型・参照: 指摘なし')");
  const badView = "@page\n@model IndexModel\n<p>😀 @Model.MissingMember</p>\n";
  await set(badView);
  await assertMarker("CS1061", 3, "<p>😀 @Model.".length + 1);
  const details = "document.getElementById('editor-diagnostics')";
  await evaluate(details + ".open = true");
  await wait("!!document.querySelector('#editor-code .squiggly-error')");
  await evaluate("document.querySelector('#editor-diagnostic-list button').click()");
  assert.deepEqual(await evaluate(sourceEditor + ".getPosition()"), {lineNumber: 3, column: "<p>😀 @Model.".length + 1});

  await evaluate("(" + function () {
    const original = window.fetch.bind(window);
    const io = window.editorSemanticTest = {syntaxReady: false, restore: () => { window.fetch = original; }};
    window.fetch = async (route, options) => {
      const body = route === "roslyn-diagnostics" && options?.body && JSON.parse(options.body);
      const response = await original(route, options);
      if (body?.mode === "semantic" && body.content.includes("OldMember")) {
        io.oldCode = (await response.clone().json()).findings[0]?.code;
        return new Promise(resolve => { io.releaseOld = () => resolve(response); });
      }
      if (body && !body.mode && body.content.includes("NewMember")) io.syntaxReady = true;
      if (body?.mode === "semantic" && body.content.includes("NewMember")) {
        return new Promise(resolve => { io.releaseNew = () => resolve(response); });
      }
      return response;
    };
  }.toString() + ")()");
  try {
    await set(badView.replace("MissingMember", "OldMember"));
    await wait("!!editorSemanticTest.releaseOld");
    assert.equal(await evaluate("editorSemanticTest.oldCode"), "CS1061");
    await set(badView.replace("MissingMember", "NewMember"));
    await wait("editorSemanticTest.syntaxReady");
    await evaluate("editorSemanticTest.releaseOld()");
    await wait("!!editorSemanticTest.releaseNew");
    assert.equal(await evaluate(markers + ".length"), 0);
    await select(codeBehindPath, savedCodeBehind);
    await evaluate("editorSemanticTest.releaseNew()");
    await wait(status + ".includes('構文・型・参照: 指摘なし')");
    assert.equal(await evaluate(markers + ".length"), 0);
  } finally {
    await evaluate("editorSemanticTest.releaseOld?.(); editorSemanticTest.releaseNew?.(); editorSemanticTest.restore()");
  }
  await set(savedCodeBehind);
  await select(razorPath, badView.replace("MissingMember", "NewMember"));
  await set(savedRazor);
  await wait(status + ".includes('構文・型・参照: 指摘なし')");
  try {
    await fs.writeFile(path.join(tree, "App.csproj"), descriptor.replace("</Project>",
      '<ItemGroup><PackageReference Include="Unavailable.Fixture" Version="1.0.0"/></ItemGroup></Project>'));
    await set(badView);
    await wait(status + ".includes('型・参照: 未確認')");
    assert.match(await evaluate(note), /正式ビルド/);
    assert.equal(await evaluate(markers + ".length"), 0);
  } finally {
    await fs.writeFile(path.join(tree, "App.csproj"), descriptor);
  }
  await set(savedRazor);
  await select("Example.cs", savedCSharp);
  const renamedCodeBehind = savedCodeBehind.replace("Number", "Rename");
  const dependency = path.join(tree, codeBehindPath);
  const unchangedBody = savedCSharp + "\n// Unsaved text stays in the editor";
  const retained = () => wait(sourceEditor + "?.getValue() === " + JSON.stringify(unchangedBody));
  await set(unchangedBody);
  await wait(status + ".includes('構文・型・参照: 指摘なし')");
  try {
    await fs.writeFile(dependency, renamedCodeBehind);
    await assertMarker("CS1061", 1, savedCSharp.lastIndexOf("Number") + 1);
    await retained();
    await fs.writeFile(dependency, savedCodeBehind);
    await wait(status + ".includes('構文・型・参照: 指摘なし')");
    await retained();

    await evaluate("(" + function () {
      const original = window.fetch.bind(window);
      const status = document.getElementById("editor-diagnostic-status");
      const receipts = window.contextTest = {oldClear: false, announcedClear: false};
      const record = new MutationObserver(() => {
        if (receipts.released && status.textContent.includes("構文・型・参照: 指摘なし"))
          receipts.announcedClear = true;
      });
      record.observe(status, {childList: true, characterData: true, subtree: true});
      receipts.restore = () => { record.disconnect(); window.fetch = original; };
      window.fetch = async (route, options) => {
        const body = route === "roslyn-diagnostics" && options?.body && JSON.parse(options.body);
        const response = await original(route, options);
        if (!receipts.release && body?.mode === "semantic" && body.content.includes("Hold old dependency result")) {
          receipts.oldClear = (await response.clone().json()).status === "clear";
          return new Promise(resolve => { receipts.release = () => { receipts.released = true; resolve(response); }; });
        }
        return response;
      };
    }.toString() + ")()");
    try {
      const lateDraft = unchangedBody + "\n// Hold old dependency result";
      await set(lateDraft);
      await wait("!!contextTest.release");
      assert(await evaluate("contextTest.oldClear"));
      await fs.writeFile(dependency, renamedCodeBehind);
      await evaluate("contextTest.release()");
      await assertMarker("CS1061", 1, savedCSharp.lastIndexOf("Number") + 1);
      assert.equal(await source(), lateDraft);
      assert.equal(await evaluate("contextTest.announcedClear"), false);
    } finally {
      await evaluate("contextTest.release?.(); contextTest.restore()");
    }
  } finally {
    await fs.writeFile(dependency, savedCodeBehind);
    await set(savedCSharp);
  }
  await wait(status + ".includes('構文・型・参照: 指摘なし')");
  const dotted = path.join(tree, ".Hidden.cs");
  const dottedDraft = "class Dotted { HiddenEditorDependency Value = new(); }";
  await fs.writeFile(dotted, "public class HiddenEditorDependency {}");
  try {
    await set(dottedDraft);
    await wait(status + ".includes('構文・型・参照: 指摘なし')");
    await fs.unlink(dotted);
    await assertMarker("CS0246", 1, dottedDraft.indexOf("HiddenEditorDependency") + 1);
    assert.equal(await source(), dottedDraft);
  } finally {
    await fs.rm(dotted, {force: true});
    await set(savedCSharp);
  }
  console.log("Browser editor: a dotted C# file resolves a real draft type and removal refreshes its marker");
  console.log("Browser editor: external dependency changes invalidate even an in-flight result without losing an unsaved draft");
  assert.deepEqual(await Promise.all([razorPath, codeBehindPath, "Example.cs"].map(file =>
    fs.readFile(path.join(tree, file), "utf8"))), [savedRazor, savedCodeBehind, savedCSharp]);
  const entries = await fs.readdir(tree);
  assert(!entries.includes("bin") && !entries.includes("obj"));
  console.log("Browser editor: real C#/Razor semantics, source position, stale version and file switching, unresolved context, and no save or build outputs");
}
