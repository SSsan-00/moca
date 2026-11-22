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
