# 2件並列ダッシュボードの実行確認

2026-11-17に、独立したPHP→C#の3単位を本番のローカルダッシュボードからまとめて実行した。実データを使わない小さな検証用アプリで、Junie CLI、.NET SDK 10、独自チェッカーを実際に動かした。

```sh
python evaluations/parallel-runs/create_fixture.py /tmp/harness-parallel-example
php-to-csharp-harness dashboard --config /tmp/harness-parallel-example/harness.toml
```

新規の空ディレクトリを指定する。ブラウザーの実行タブで3単位を選び、「選択した3件を実行」を押す。Junieへのログインと.NET SDK 10が必要。

## 確認した結果

| 移行単位 | Run ID | ビルド終了コード | チェッカー終了コード | 最終状態 |
| --- | --- | --- | --- | --- |
| 注文一覧 | `101cf5d8-29c5-4607-89a3-64eb58eb568f` | 0 | 0 | `checker_succeeded` |
| 顧客詳細 | `1cfeb58c-a680-41d8-8c20-7200196f778f` | 0 | 0 | `checker_succeeded` |
| 売上集計 | `59f009bf-ce43-4c0a-9034-77478055b9be` | 0 | 0 | `checker_succeeded` |

- ブラウザーとworkspace APIで、最初は実行中2件・待機1件だった。注文一覧と顧客詳細はそれぞれ03:25:23 UTCに準備され、同時にJunieを実行した。枠が空いた03:27:58 UTCに売上集計が自動で準備・実行された。
- 各Runは別のworktree、移行先、ログ、質問待ち状態を使用した。
- この環境のJunie ACPには利用可能なモデルがなかったため、既存の確認画面で各Runに「今回のみ非対話モードで再試行」を選択した。移行・ビルド・チェッカーはモックに置き換えていない。
- 成功は生成・ビルド・チェッカーの確認を示す。移行台帳の人による確認完了や、本格的なアプリの性能評価を意味しない。

## 自動・画面確認

`tests/test_parallel_dashboard.py` は実際のGit worktreeと制御できるJunie代替を使い、上限2件、3件目の待機・自動開始、個別停止・再開、同じRunのCLI二重実行拒否、重複登録の一括拒否、待機取消、終了後の待機復元、Run別のHTTPエディタ・質問・指示、設定保存先の切り替えを検証する。

Chromeでデモの1440・768・390px幅を確認。実行一覧・進捗・エディタにページ全体の横はみ出しやJavaScript例外はなかった。検索の空結果、Run A→B→Aの切り替えで未保存コードと未送信指示が保持され、別Runに混ざらないことも確認した。
