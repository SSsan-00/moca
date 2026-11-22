# 約2万行の業務PHPを.NET 9へ移行する通し検証

`business-php` の合成注文処理を物理20,000行に拡張する。空行や未使用データの水増しではなく、183契約の見積もり関数と実際に呼ばれる分岐を展開する。注文・入金・取消・返金・集計、テナント分離、在庫更新、ロールバックを含む。実業務コードや外部DBは使用しない。

## 合格条件

- Windowsの通常ダッシュボードから実Junieで移行し、全カプセル、.NET 9ビルド、チェッカーを通す。
- 元PHPと移行先C#を同じ187シナリオで実行し、955件の応答と全シナリオ終了時のSQLiteテーブル内容が一致する。JSONの値と型、項目の欠落、配列の並び、nullと空文字を区別する。
- 全183契約を実行する。注文・返金の繰り返し、書き込み直後の例外とロールバック、別テナントの同じ注文ID、在庫不足、32ビット上限を超える金額、Unicodeを含む。
- 未実装C#をチェッカーが拒否する。比較器の自己テストは応答型・null・SQLiteの状態差を故意に作って検出を確かめる。
- 詳細資料は金額と取引処理を採用し、メール配信は除外する。最終の移行台帳確認は自動検査とは別の操作。

## 実行方法

PHP CLI・PDO SQLite、Git、.NET 9ランタイムとnet9.0をビルドできるSDK、認証済みJunie CLI、Pythonの通常依存、PlaywrightとChromeは検証を行う環境にだけ必要。移行ハーネスの実行時依存ではない。Windowsの公式PHP ZIPを使用する場合、検証生成器が同梱のPDO SQLiteを評価プロセスにだけ読み込む。

PowerShellからリポジトリのルートで実行する。

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
python evaluations/business-php-20k/test_benchmark.py
python evaluations/business-php-20k/create_fixture.py --php "C:/path/to/php.exe"
# 表示された検証用ディレクトリを指定
python evaluations/business-php/run_dashboard.py "<fixture>" "<fixture>/evidence"
# 既存Runが停止した場合は、同じIDを指定して保存版から再開
python evaluations/business-php/run_dashboard.py "<fixture>" "<fixture>/resumed-evidence" "<Run ID>"
```

PHPは先に合成データだけで実行し、比較用の入力・期待結果を独立した `validation/` に置く。ハーネスに渡すGitプロジェクト `app/` に期待結果は入れない。移行後のC#だけをビルドして、別プロセスのチェッカーが同じスキーマ・データを使って実行する。

この検証は、特定の合成入力での移行を対象とする。任意のレガシーPHP、実際のDBドライバー、同時更新、移行先アプリUIの等価性は保証しない。

## Windows実機の結果

同じRunを再開して4区間すべてを完了。最終の .NET 9 ビルドとチェッカーは終了コード0、955操作の応答と187シナリオのSQLite状態がすべて一致した。採用ルールは金額・取引の2件で、メール配信は除外された。ブラウザのJavaScriptエラーはなかった。入力と出力のSHA-256は [results.json](results.json) を参照。

受入中に、WindowsでJunieの初期接続が30秒で打ち切られるケースを再現し、初期接続だけ60秒に延ばした。既存の記録から再開でき、資料の採否も引き継がれた。検証スクリプトでは、Windowsの一時的な共有ロック・重複クリック・再開時の既存モーダルに対処した。合成Runの完了記録と追加指示の受領記録だけを一時許可し、恒久的な許可は追加していない。

比較結果を自由文で標準出力した最初の試行では、955操作が一致していてもハーネスが修正へ進んだ。検証器を既定の合格応答だけを返す形に変更し、件数はビルド成果物ディレクトリの behavior-results.json に分離した。製品側の合否基準は緩めず、同じRunのC#を再ビルド・再確認して正常終了を確認した。
