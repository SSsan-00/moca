globalThis.harnessLogPresentation = (language, tone, count, waiting = false) => {
  if (language === "en") {
    const polite = tone === "polite";
    const entries = count === 1 ? "1 entry" : `${count} entries`;
    return {
      title: "Junie detailed log",
      description: polite
        ? "You can review the entries below. Junie is asked to use your style in new progress updates and reports."
        : "Junie is asked to use your style in new progress updates and reports. Existing entries are not rewritten.",
      rawLabel: "Junie log entries",
      summary: ({
        polite: `Showing ${entries}.${waiting ? " Please check back for new entries while Junie is running." : ""}`,
        concise: `${entries}${waiting ? " · Running" : ""}`,
        friendly: `You’ve got ${entries} so far.${waiting ? " More may arrive as Junie works." : ""}`,
      })[tone] ?? `${entries} shown.${waiting ? " Junie is running." : ""}`,
      empty: polite ? "No entries to show yet." : "No log entries."
    };
  }
  const polite = tone === "polite";
  return {
    title: "Junie の詳細ログ",
    description: polite
      ? "新しい進捗や報告を選択した口調で書くよう、Junie に依頼します。過去のログは書き換えません。"
      : "新しい進捗や報告の文体を Junie に指示します。過去のログは書き換えません。",
    rawLabel: "Junie のログ",
    summary: ({
      polite: `${count} 件のログを表示しています。${waiting ? "Junie は実行中です。" : ""}`,
      concise: `${count} 件${waiting ? "・実行中" : ""}`,
      friendly: `ここまでのログは ${count} 件です。${waiting ? "Junie は作業を続けています。" : ""}`,
    })[tone] ?? `表示中のログ：${count} 件。${waiting ? "Junie は実行中。" : ""}`,
    empty: polite ? "表示できるログはまだありません。" : "ログなし。"
  };
};

// Junie の既知の進捗ラッパーだけを解釈し、不完全な内容や一般の XML は原文のままにする。
globalThis.harnessLogSections = text => {
  const opener = /^\s*<UPDATE>/.exec(text);
  if (!opener) return null;
  const token = /\s*(?:<\/UPDATE>|<(PREVIOUS_STEP|EXTERNAL)>([\s\S]*?)<\/\1>)/gy;
  token.lastIndex = opener[0].length;
  const sections = [];
  while (true) {
    const match = token.exec(text);
    if (!match) return null;
    const [, key, content] = match;
    if (!key) return sections.length ? {sections, trailing: text.slice(token.lastIndex)} : null;
    if (sections.some(item => item.key === key) || content.includes("<" + key + ">")) return null;
    sections.push({key, content: content.replace(/^\r?\n|\r?\n$/g, "")});
  }
};
