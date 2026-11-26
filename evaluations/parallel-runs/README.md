# 5件並列ダッシュボードの実行確認

2026-11-17に、独立したPHP→C#の6単位を本番用のローカルダッシュボードからまとめて実行した。実データを使わない小さなアプリで、Junie CLI、.NET SDK 10、独自チェッカーを実際に動かした。

```sh
python evaluations/parallel-runs/create_fixture.py /tmp/harness-parallel-example
php-to-csharp-harness dashboard --config /tmp/harness-parallel-example/harness.toml
```

新規の空ディレクトリを指定する。ブラウザーの実行タブで6単位を選び、「選択した6件を実行」を押す。Junieへのログインと.NET SDK 10が必要。

## 確認した結果

| 移行単位 | Run ID | ビルド終了コード | チェッカー終了コード | 最終状態 |
| --- | --- | --- | --- | --- |
| 注文一覧 | `1c8dd543-13a7-442d-b95d-acd00e8c4ec4` | 0 | 0 | `checker_succeeded` |
| 顧客詳細 | `197e20dc-0895-4e20-8807-57a306062795` | 0 | 0 | `checker_succeeded` |
| 売上集計 | `b2dda442-078c-49b5-b2cd-1424afa40fcf` | 0 | 0 | `checker_succeeded` |
| 在庫照会 | `8afa0aa2-3a79-4c52-b0a5-6fe7472eadac` | 0 | 0 | `checker_succeeded` |
| 請求一覧 | `db8a3248-cd82-4e96-929a-357f4f06b934` | 0 | 0 | `checker_succeeded` |
| 入金消込 | `969d09e4-5cb3-44a8-b506-e588abd6a35d` | 0 | 0 | `checker_succeeded` |

- ブラウザーとworkspace APIで最初の5件が同時に実行され、6件目は待機することを確認した。枠が空くと6件目が自動で開始し、6件とも成功した。監視中の最大実行数は5件だった。
- 各Runは別のworktree、移行先、ログ、質問待ち状態を使用した。画面の同時実行数・概要・登録後の案内はAPIの `parallel_limit` と一致した。
- この環境のJunie ACPには利用可能なモデルがなかったため、各Runの確認画面で「今回のみ非対話モードで再試行」を選択した。移行・ビルド・チェッカーはモックに置き換えていない。
- 成功は生成・ビルド・チェッカーの確認を示す。移行台帳の人による確認完了や、大規模アプリの負荷試験を意味しない。

## 自動・画面確認

`tests/runs/test_parallel_dashboard.py` は実際のGit worktreeと制御できるJunie代替を使い、上限5件、6件目の待機・自動開始、個別停止・再開、同じRunのCLI二重実行拒否、重複登録の一括拒否、待機取消、終了後の待機復元、Run別のHTTPエディタ・質問・指示、設定保存先の切り替えを検証する。新規・再開の待機順と、再開の取消で保存済みの状態へ戻ることも確認する。

`tests/browser/test_parallel_demo.py` はNode.jsが利用できる環境でデモのJavaScriptを動かし、5件の上限、新規・再開のFIFO順、待機取消、終了までの自動繰り上げを確認する。

Chromeで1440・768・390px幅の実行一覧・進捗・エディタを確認した。ページ全体の横はみ出しやJavaScript例外はなく、Run A→B→Aの切り替えで未保存コードと未送信指示を保持した。本番用画面の6件一括登録でもJavaScript例外はなかった。

## 2件並列だった旧版の記録

同日に2件並列・1件待機で実行した初期版の記録を残す。現在の再現用スクリプトと上限は上記の6単位・5並列へ更新済み。

| 移行単位 | Run ID | ビルド終了コード | チェッカー終了コード | 最終状態 |
| --- | --- | --- | --- | --- |
| 注文一覧 | `101cf5d8-29c5-4607-89a3-64eb58eb568f` | 0 | 0 | `checker_succeeded` |
| 顧客詳細 | `1cfeb58c-a680-41d8-8c20-7200196f778f` | 0 | 0 | `checker_succeeded` |
| 売上集計 | `59f009bf-ce43-4c0a-9034-77478055b9be` | 0 | 0 | `checker_succeeded` |
