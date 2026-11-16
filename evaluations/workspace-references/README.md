# 同フォルダの類似実装とincの共通関数を使う検証

`python evaluations/workspace-references/create_fixture.py` で一時Gitアプリを作る。表示されたフォルダの `harness.toml` を使ってダッシュボードを開き、`workspace-references` を実行する。実アプリのファイルは変更しない。

移行先は `Pages/Invoices/InvoicePage.cs`。Junieには次の4ファイルが同じ相対パスで渡る。これらは参照専用で、修正対象は `InvoicePage.cs` だけ。

| 資料 | 確認する内容 |
| --- | --- |
| 同フォルダの `HistoryPage.cs` | 類似ページの処理順序、名前空間、共通関数の呼び出し方 |
| 同フォルダの `InvoiceRow.cs` | 返却するデータ型 |
| `Pages/inc/InvoicePricing.cs` | 金額の単位、割引率の単位、負数を含む端数処理、追加割引 |
| `Pages/inc/Formatting/InvoiceFormat.cs` | 金額の書式、日本語・英語の表示名 |

移行後スクリプトが `Net(...)` を、ビルドは通るが金額が違う `PreviewNet(...)` に置換する。独立したPythonチェッカーが差を検出し、通常の自動修正フローを起動する。修正方法を人が追加入力する必要はない。PHPの元処理も `app/legacy` を作業ディレクトリとして `php invoice` で実行できる。

## 実際のJunieでの結果

2026-11-16、macOS、Junie 26.11.1（4020.10、Junie Lite）、.NET SDK 10.0.201／net9.0。Run ID: `7e5e10ba-f3e3-4e4d-92a3-71140e371282`。

- 本番ダッシュボードから開始し、1カプセル＋自動修正1回で `checker_succeeded`。再ビルドとチェッカーの終了コードはともに0。
- 移行時のJunieログで4資料の読み取りを確認。修正時にも金額・表示の共通関数を読み直し、`InvoicePricing.PreviewNet(...)` を `InvoicePricing.Net(...)` に戻した。
- 12件×追加割引2条件×表示言語2条件の48件が一致。金額、負数・端数、重複、順序、表示名を検査した。PHPの実行結果も同じ独立基準値に一致した。
- 4資料のハッシュは実行前後で一致。共通ルールの読み取り記録は移行時・修正時それぞれ1/1。
- 保存された結果を本番ダッシュボードで確認。JavaScriptエラーなし。

記録された入力合計は585,349トークン（通常92,293、キャッシュ読み込み493,056、キャッシュ作成0）、出力9,387。工程時間の合計は約129秒。単一ケースの測定であり、他のプロジェクトの性能やすべての規約の遵守を保証するものではない。
