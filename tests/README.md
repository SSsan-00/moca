# 回帰テスト

リポジトリ直下から実行します。

| フォルダ | 主な検証対象 |
| --- | --- |
| `browser/` | Chromium のダッシュボード操作、Node.js の画面回帰、関連スクリプト |
| `editor/` | HTTP エディタ、編集の復旧、型診断 |
| `junie/` | 起動、ACP、会話、モデル、ログ・通知 |
| `migration/` | 変換フロー、設定・入力、カプセル、参照資料 |
| `quality/` | アーキテクチャ、CI のテスト分割、変異テスト |
| `rules/` | 移行ルールの整理、検索、適用根拠 |
| `runs/` | Run の状態・並行制御、スクリプト、保存と再開 |
| `verification/` | ビルド、チェッカー、診断、修復 |
| `support/` | 複数のテストで共用するフィクスチャ |

```powershell
python -m unittest discover -s tests -t . -v
python -m unittest tests.runs.test_run_state_models -v
python tests/run_shard.py full --require-ui --timings
```

CI の各シャードも `run_shard.py` を使います。Issue 自動化が追加できるテストは従来どおり直下の `test_*.py` に限られ、同じ入口から検出されます。
