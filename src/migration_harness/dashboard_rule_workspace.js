(() => {
  const node = id => document.querySelector(`#rules-${id}`);
  const tab = document.querySelector("#tab-rule-workspace");
  const live = document.documentElement.hasAttribute("data-live");
  const demo = live ? null : window.harnessRuleWorkspaceDemo;
  if (!tab || tab.hidden || (!live && !demo)) return;
  if (demo) {
    node("demo-note").hidden = false;
    node("archive").hidden = true;
  }
  let draft, source = "new", busy = false, timer, token, uploadedBytes;
  const status = text => { node("status").textContent = text; };
  const request = async (query, action) => {
    if (demo) return demo(query, action);
    const response = await fetch("rule-workspace" + (query ? "?" + new URLSearchParams(query) : ""), action ? {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(action),
    } : undefined);
    if (!response.ok) throw Error(await response.text());
    return response.json();
  };
  function display(data) {
    const was = draft?.id;
    draft = data;
    node("draft").hidden = !data || data.applied;
    if (!data || data.applied) return;
    const running = data.live?.status === "running";
    const options = [new Option("新しいMarkdown", "new"), ...data.files.map(file => new Option(file.name, file.source))];
    node("sources").replaceChildren(...options);
    node("sources").value = options.some(item => item.value === source) ? source : "new";
    node("history").replaceChildren(...data.messages.map(message => {
      const entry = document.createElement("p");
      entry.textContent = `${message.role === "operator" ? "あなた" : "Junie"}：${message.text}`;
      return entry;
    }));
    node("live").textContent = data.live?.text || "";
    node("unresolved").textContent = data.unresolved.join("／");
    node("stop").hidden = !running;
    for (const id of ["message", "optimize", "save-source", "save-classification", "preview", "apply", "accept"]) {
      node(id).disabled = running;
    }
    const permission = data.live?.permission;
    const oldPermission = node("permission").dataset.id;
    if (oldPermission !== (permission?.id || "")) {
      node("permission").dataset.id = permission?.id || "";
      node("permission").hidden = !permission;
      node("permission").replaceChildren();
      if (permission) {
        const detail = document.createElement("pre");
        detail.textContent = permission.detail;
        node("permission").append(detail);
        for (const option of permission.options) {
          const button = document.createElement("button");
          button.type = "button";
          button.textContent = option.label;
          button.addEventListener("click", () => submit("answer", {request: permission.id, option: option.id}));
          node("permission").append(button);
        }
      }
    }
    node("proposal").hidden = !data.proposal;
    node("proposal-text").textContent = data.proposal?.text || "";
    const preview = data.preview;
    node("review").hidden = !preview;
    if (token !== preview?.token || was !== data.id) node("confirm").checked = false;
    token = preview?.token;
    node("text-diff").textContent = preview?.text_diff || "本文の差分はありません。";
    node("toml-diff").textContent = preview?.toml_diff || "";
    const files = new Map(data.files.map(file => [file.source, file.name]));
    node("coverage").textContent = preview?.rules.map(rule => [
      `${rule.destination}：${rule.usage === "required" ? "必須" : rule.description}`,
      ...rule.parts.map(part => `  ${files.get(part.source)} L${part.start}–${part.end}`),
    ].join("\n")).join("\n\n") || "";
    clearTimeout(timer);
    if (running) timer = setTimeout(poll, 400);
    if (data.live?.status === "error") status(data.live.text);
    else if (data.live?.status === "ready") status(data.live.new_session ? "整理セッションを開始しました。" : "同じ整理セッションで回答しました。");
  }
  async function poll() {
    const id = draft?.id;
    try {
      const data = await request({id});
      if (draft?.id === id && !busy) display(data);
      else if (draft?.id === id) timer = setTimeout(poll, 400);
    } catch (error) {
      status(error.message);
    }
  }
  async function refresh() {
    const {drafts} = await request({});
    node("drafts").replaceChildren(new Option("選択してください", ""), ...drafts.map(item =>
      new Option(`${item.id.slice(0, 8)}（${item.files}件）`, item.id)));
    node("drafts").value = draft?.applied ? "" : draft?.id || "";
  }
  async function loadSource() {
    source = node("sources").value || "new";
    uploadedBytes = undefined;
    const entry = draft?.files.find(item => item.source === source);
    const id = draft?.id, current = source;
    node("name").value = entry?.name || "";
    node("name").readOnly = !!entry;
    node("usage").value = entry?.usage || "required";
    node("description").value = entry?.description || "";
    const response = entry ? await request({id, source}) : {text: ""};
    if (draft?.id === id && source === current) node("text").value = response.text;
  }
  async function submit(action, extra = {}) {
    if (busy) return;
    busy = true;
    try {
      const was = draft?.id;
      const result = await request(null, {action, id: was, revision: draft?.revision, ...extra});
      if (result.archives) return showArchives(result.archives);
      display(result);
      if (action === "start" || was !== result.id || action === "accept") await loadSource();
      if (["start", "apply"].includes(action)) await refresh();
      if (action === "apply") status(demo ? "デモの反映が完了しました。実際のTOMLは変わりません。" :
        "TOMLに反映しました。次のRunから適用されます。");
      if (action === "message" || action === "optimize") node("instruction").value = "";
    } catch (error) {
      status(error.message);
    } finally {
      busy = false;
    }
  }
  function showArchives(archives) {
    node("archives").replaceChildren(...archives.map(entry => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `${entry.id.slice(0, 8)} を${entry.archived ? "復元" : "退避"}`;
      button.addEventListener("click", () => {
        if (entry.archived) submit("restore", {id: entry.id});
        else if (window.confirm("別の設定や作業場所から参照していないことを確認して、生成物を退避しますか？")) {
          submit("archive", {id: entry.id, confirmed: true});
        }
      });
      return button;
    }));
    if (!archives.length) node("archives").textContent = "安全に整理できる旧版はありません。";
  }
  node("start").addEventListener("click", () => submit("start"));
  node("drafts").addEventListener("change", async () => {
    try {
      const id = node("drafts").value;
      if (id) { display(await request({id})); await loadSource(); } else display(null);
    } catch (error) { status(error.message); }
  });
  node("sources").addEventListener("change", () => loadSource().catch(error => status(error.message)));
  node("text").addEventListener("input", () => { uploadedBytes = undefined; });
  node("import").addEventListener("change", async event => {
    try {
      const file = event.target.files[0];
      if (!file) return;
      if (file.size > 2 * 1024 * 1024) throw Error("Markdownは2 MiBまでです。");
      const bytes = new Uint8Array(await file.arrayBuffer());
      const text = new TextDecoder("utf-8", {fatal: true}).decode(bytes);
      if (source === "new") node("name").value = file.name;
      node("text").value = text;
      const parts = [];
      for (let i = 0; i < bytes.length; i += 8192) parts.push(String.fromCharCode(...bytes.subarray(i, i + 8192)));
      uploadedBytes = btoa(parts.join(""));
    } catch (error) { status(error.message); }
    finally { event.target.value = ""; }
  });
  node("save-source").addEventListener("click", async () => {
    const create = source === "new";
    await submit("edit", {source, name: node("name").value, text: node("text").value, bytes: uploadedBytes});
    if (create && draft?.files.length) {
      node("sources").value = draft.files.at(-1).source;
      await loadSource().catch(error => status(error.message));
    }
  });
  node("save-classification").addEventListener("click", () => {
    const previous = draft?.files.find(file => file.source === source);
    const usage = node("usage").value;
    const confirm_scope = previous?.usage !== "required" || usage === "required"
      || window.confirm("既存の必須事項を詳細へ移しますか？適用範囲と例外を確認してください。");
    if (confirm_scope) submit("classification", {source, usage, description: node("description").value, confirm_scope});
  });
  for (const action of ["message", "optimize"]) node(action).addEventListener("click", () =>
    submit(action, {text: node("instruction").value}));
  for (const action of ["accept", "stop", "preview"]) node(action).addEventListener("click", () => submit(action));
  node("apply").addEventListener("click", () => submit("apply", {preview: token, confirmed: node("confirm").checked}));
  node("list-archive").addEventListener("click", async () => {
    try { showArchives((await request({archive: "1"})).archives); } catch (error) { status(error.message); }
  });
  workspaceTabs.addEventListener("harnesstab", () => {
    if (harnessTabs[activeHarnessTab] === tab) refresh().catch(error => status(error.message));
  });
})();
