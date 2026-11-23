globalThis.harnessLogPresentation = (language, tone, count, waiting = false) => {
  if (language === "en") {
    const polite = tone === "polite";
    const entries = count === 1 ? "1 entry" : `${count} entries`;
    return {
      title: "Junie detailed log",
      description: polite
        ? "You can review Junie’s original entries below. Your preferences only change the dashboard summary."
        : "Dashboard summaries follow these settings. Original Junie entries are unchanged.",
      rawLabel: "Original Junie entries",
      summary: polite
        ? `Showing ${entries}.${waiting ? " Please check back for new entries while Junie is running." : ""}`
        : `${entries} shown.${waiting ? " Junie is running." : ""}`,
      empty: polite ? "No entries to show yet." : "No log entries."
    };
  }
  const polite = tone === "polite";
  return {
    title: "Junie の詳細ログ",
    description: polite
      ? "設定はダッシュボードの状況説明だけに反映します。Junie のログは原文でご確認いただけます。"
      : "設定は状況説明のみに適用。Junie のログは原文のまま表示。",
    rawLabel: "Junie のログ原文",
    summary: polite
      ? `${count} 件のログを表示しています。${waiting ? "Junie は実行中です。" : ""}`
      : `表示中のログ：${count} 件。${waiting ? "Junie は実行中。" : ""}`,
    empty: polite ? "表示できるログはまだありません。" : "ログなし。"
  };
};
