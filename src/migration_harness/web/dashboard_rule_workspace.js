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
  let registeredRequest = 0, draftRequest = 0, sourceRequest = 0, editor;
  const selectedSources = new Map();
  let renderedMessages = [];
  const input = node("instruction");
  const send = node("message");
  const composer = node("composer");
  const history = node("history");
  const updateFeed = bindJunieChat(node("chat-feed"), node("chat-latest"), composer, input, send);
  const syncSend = () => {
    send.disabled = busy || !draft || draft.live?.status === "running" || !input.value.trim();
  };
  const status = text => { node("status").textContent = text; };
  const canEditSource = () => hasDraft() && !busy && draft.live?.status !== "running"
    && editor?.id === draft.id && editor.source === source;
  function syncEditor() {
    for (const id of ["text", "name", "import", "save-source"]) node(id).disabled = !canEditSource();
    for (const id of ["sources", "drafts", "start"]) node(id).disabled = busy;
  }
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
    if (!hasDraft() || was !== data.id) {
      ++sourceRequest;
      editor = null;
    }
    syncEditor();
    node("draft").hidden = !hasDraft();
    if (hasDraft()) node("registered").hidden = true;
    if (!data || data.applied) {
      syncSend();
      return;
    }
    const running = data.live?.status === "running";
    const options = [new Option("新しいMarkdown", "new"), ...data.files.map(file => new Option(file.name, file.source))];
    if (was !== data.id) source = selectedSources.get(data.id) ?? data.files[0]?.source ?? "new";
    if (!options.some(item => item.value === source)) source = data.files[0]?.source ?? "new";
    node("sources").replaceChildren(...options);
    node("sources").value = source;
    updateFeed(() => {
      renderHistory(data.messages, was !== data.id);
      node("chat-empty").hidden = running || Boolean(data.messages.length);
      node("live-turn").hidden = !running;
      const liveText = data.live?.text || "返信を待っています…";
      if (running && node("live").textContent !== liveText) node("live").textContent = liveText;
      showPermission(data.live?.permission);
      node("proposal").hidden = !data.proposal;
      const proposalText = data.proposal?.text || "";
      if (node("proposal-text").textContent !== proposalText) node("proposal-text").textContent = proposalText;
    }, was !== data.id);
    syncSend();
    node("unresolved").textContent = data.unresolved.join("／");
    node("stop").hidden = !running;
    for (const id of ["optimize", "save-classification", "preview", "apply", "accept"]) {
      node(id).disabled = running;
    }
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
  function renderHistory(messages, reset) {
    let unchanged = 0;
    if (!reset) {
      while (unchanged < messages.length && unchanged < renderedMessages.length &&
             messages[unchanged].role === renderedMessages[unchanged].role &&
             messages[unchanged].text === renderedMessages[unchanged].text) unchanged++;
    }
    while (history.children.length > unchanged) history.lastElementChild.remove();
    for (const {role, text} of messages.slice(unchanged)) {
      const entry = document.createElement("li");
      const outgoing = role === "operator";
      entry.append(junieChatBubble(outgoing ? "あなた" : "Junie", text, outgoing));
      history.append(entry);
    }
    renderedMessages = messages;
  }
  function showPermission(permission) {
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
  }
  async function poll() {
    const id = draft?.id;
    const previous = draft;
    try {
      const data = await request({id});
      if (draft === previous && !busy) display(data);
      else if (draft?.id === id) timer = setTimeout(poll, 400);
    } catch (error) {
      status(error.message);
    }
  }
  const hasDraft = () => draft && !draft.applied;
  async function refreshRegistered() {
    if (hasDraft()) return;
    const generation = ++registeredRequest;
    const isCurrent = () => generation === registeredRequest && !hasDraft();
    try {
      const {registered} = await request({registered: "1"});
      if (!isCurrent()) return;
      node("registered").hidden = !registered;
      node("registered-name").textContent = registered?.name || "";
      node("registered-text").textContent = registered?.text || "";
    } catch (error) {
      if (!isCurrent()) return;
      node("registered").hidden = true;
      status(error.message);
    }
  }
  async function refresh() {
    const {drafts} = await request({});
    node("drafts").replaceChildren(new Option("選択してください", ""), ...drafts.map(item =>
      new Option(`${item.id.slice(0, 8)}（${item.files}件）`, item.id)));
    node("drafts").value = draft?.applied ? "" : draft?.id || "";
    await refreshRegistered();
  }
  function selectSource(entry) {
    source = entry?.source ?? "new";
    node("sources").value = source;
    selectedSources.set(draft.id, source);
    node("name").value = entry?.name || "";
    node("name").readOnly = !!entry;
    node("usage").value = entry?.usage || "required";
    node("description").value = entry?.description || "";
  }
  async function loadSource() {
    const generation = ++sourceRequest;
    editor = null;
    uploadedBytes = undefined;
    node("text").value = "";
    syncEditor();
    const entry = draft.files.find(item => item.source === node("sources").value);
    selectSource(entry);
    try {
      const response = entry ? await request({id: draft.id, source}) : {text: "", revision: draft.revision};
      if (generation !== sourceRequest) return;
      node("text").value = response.text;
      editor = {id: draft.id, source, revision: response.revision};
      syncEditor();
    } catch (error) {
      if (generation === sourceRequest) status(error.message);
    }
  }
  async function submit(action, extra = {}) {
    if (busy) return;
    busy = true;
    syncEditor();
    syncSend();
    try {
      const was = draft?.id;
      const revision = draft?.revision;
      const result = await request(null, {action, id: was, revision, ...extra});
      if (result.archives) return showArchives(result.archives);
      if (editor?.id === result.id && editor.revision === (extra.revision ?? revision)) editor.revision = result.revision;
      display(result);
      if (action === "start" || was !== result.id || action === "accept") await loadSource();
      if (["start", "apply"].includes(action)) await refresh();
      if (action === "apply") status(demo ? "デモの反映が完了しました。実際のTOMLは変わりません。" :
        "TOMLに反映しました。次のRunから適用されます。");
      if (["message", "optimize"].includes(action) && input.value === extra.text) input.value = "";
      return result;
    } catch (error) {
      status(error.message);
    } finally {
      busy = false;
      syncEditor();
      syncSend();
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
  node("start").addEventListener("click", () => { ++draftRequest; return submit("start"); });
  node("drafts").addEventListener("change", async () => {
    if (busy) return;
    const generation = ++draftRequest;
    const id = node("drafts").value;
    display(null);
    try {
      if (!id) return await refreshRegistered();
      const data = await request({id});
      if (generation !== draftRequest) return;
      display(data);
      await loadSource();
    } catch (error) { if (generation === draftRequest) status(error.message); }
  });
  node("sources").addEventListener("change", () => { if (hasDraft() && !busy) return loadSource(); });
  node("text").addEventListener("input", () => { if (canEditSource()) uploadedBytes = undefined; });
  node("import").addEventListener("change", async event => {
    const file = event.target.files[0];
    if (!file || !canEditSource()) return;
    const generation = ++sourceRequest;
    const target = editor;
    editor = null;
    syncEditor();
    try {
      if (file.size > 2 * 1024 * 1024) throw Error("Markdownは2 MiBまでです。");
      const bytes = new Uint8Array(await file.arrayBuffer());
      if (generation !== sourceRequest) return;
      const text = new TextDecoder("utf-8", {fatal: true}).decode(bytes);
      if (source === "new") node("name").value = file.name;
      node("text").value = text;
      const parts = [];
      for (let i = 0; i < bytes.length; i += 8192) parts.push(String.fromCharCode(...bytes.subarray(i, i + 8192)));
      uploadedBytes = btoa(parts.join(""));
    } catch (error) { if (generation === sourceRequest) status(error.message); }
    finally {
      if (generation === sourceRequest) {
        editor = target;
        syncEditor();
      }
      event.target.value = "";
    }
  });
  node("save-source").addEventListener("click", async () => {
    if (!canEditSource()) return;
    ++sourceRequest;
    const existing = source === "new" ? new Set(draft.files.map(file => file.source)) : null;
    const result = await submit("edit", {source, revision: editor.revision, name: node("name").value,
      text: node("text").value, bytes: uploadedBytes});
    const created = existing && result?.files.find(file => !existing.has(file.source));
    if (created) {
      selectSource(created);
      editor.source = source;
    }
    if (result) status("下書きに保存しました。");
  });
  node("save-classification").addEventListener("click", () => {
    const previous = draft?.files.find(file => file.source === source);
    const usage = node("usage").value;
    const confirm_scope = previous?.usage !== "required" || usage === "required"
      || window.confirm("既存の必須事項を詳細へ移しますか？適用範囲と例外を確認してください。");
    if (confirm_scope) submit("classification", {source, usage, description: node("description").value, confirm_scope});
  });
  input.addEventListener("input", syncSend);
  composer.addEventListener("submit", event => {
    event.preventDefault();
    if (!send.disabled) submit("message", {text: input.value});
  });
  node("optimize").addEventListener("click", () => submit("optimize", {text: input.value}));
  for (const action of ["accept", "stop", "preview"]) node(action).addEventListener("click", () => submit(action));
  node("apply").addEventListener("click", () => submit("apply", {preview: token, confirmed: node("confirm").checked}));
  node("list-archive").addEventListener("click", async () => {
    try { showArchives((await request({archive: "1"})).archives); } catch (error) { status(error.message); }
  });
  workspaceTabs.addEventListener("harnesstab", () => {
    if (harnessTabs[activeHarnessTab] === tab) refresh().catch(error => status(error.message));
  });
})();
