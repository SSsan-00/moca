import {beginPhase, cdp, evaluate, finishBrowser, navigate, reportBrowserFailure, startBrowser, wait}
  from "./chromium_browser.mjs";
import {checkEditorSemantics} from "./editor_semantic_browser.mjs";

const [executable, profile, url, run, config] = process.argv.slice(2);
beginPhase("Chromium startup");
const chrome = await startBrowser(executable, profile);
let success = false;
try {
  beginPhase("live editor navigation");
  await cdp("Emulation.setDeviceMetricsOverride", {width: 720, height: 480, deviceScaleFactor: 1, mobile: false});
  await navigate("Page.navigate", {url});
  await wait("document.documentElement.dataset.liveReady === 'true' && snapshot.run_id === " + JSON.stringify(run));
  await evaluate("document.getElementById('tab-editor').click()");
  const sourceEditor = "window.monaco?.editor.getEditors().find(e => document.getElementById('editor-code').contains(e.getDomNode()))";
  await wait(sourceEditor + "?.getModel()?.getLanguageId() === 'csharp'");
  beginPhase("live editor compiler and dependency versions");
  await checkEditorSemantics({evaluate, wait, sourceEditor, config, run});
  success = true;
} catch (error) {
  reportBrowserFailure(error);
  process.exitCode = 1;
} finally {
  await finishBrowser(chrome, success);
}
