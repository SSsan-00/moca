# Runの有限状態モデル検査

[Run の状態とロック契約](run-state-contracts.md) から、独立した小さな仕様モデルを作り、初期状態から可能な操作順序を幅優先で探索する。実装の関数をモデルの遷移規則には使わない。モデル検査で設計上の安全条件を、実装の回帰テストで実際の保存・処理順を確認する。製品実装全体や、変換後の業務コードの意味的な正しさの証明ではない。

## モデルの範囲

| モデル | 有限化と確認内容 | 到達状態 / 遷移 |
| --- | --- | ---: |
| 実行枠と停止 | Run 2件・枠1。開始印の保存、停止、終了未確認、再起動、明示復旧、別Runの進行を確認。再起動で開始印または保留理由が残る場合を扱う。 | 110 / 384 |
| 編集と検証 | Run ID 2種（各Runを独立に探索し、別Runの応答を与える）。対象は3版、ビルド操作は最大3件、保留編集・受付済みチェッカーは各1件。正しい版／操作の成功や、理由付き承認の場合だけ、必要な検証を満たしたものとして完了できる。全カプセルは完了済みと仮定する。 | 必須: 1,358 / 5,560、任意: 1,604 / 7,156 |
| 途中反映と一度だけのフック | 独立した2つの小モデル。移行先2ファイル（元、採用候補、外部手修正）、保存中断と再読込。フック1コマンド・試行2回まで（最初と明示再試行）。結果不明は勝手に再実行／成功扱いしない。 | 反映: 32 / 110、フック: 22 / 62 |

同じハッシュ可能状態は1回だけ展開する。有効操作の存在も確認し、すべての操作を止めれば成功する検査にはしない。二重開始、終了未確認時に保留しない処理、終了未確認後の採用、旧版成功の流用、終了結果を残せなかったフックの再実行をモデルに意図的に加えると、条件ID、初期状態からの最短操作列、違反状態を表示する。状態数5,000または2秒の上限に達すると未完了として失敗させる。例外も失敗となる。

実際の並列上限5、OSのロック・Job Object、通信・タイミングは実装側のテストで確認する。双方の永続記録を失う障害、手修正と置換のOSレベルでの競合、任意の永続媒体の破損、外部サービス、副作用の絶対的な一度限り実行、活性・公平性はモデルの保証対象外。

## 実装との照合

| 条件ID | 実際の保存・回帰テスト |
| --- | --- |
| `RUN-*` | `runs/run_manager.py` の `start_pending()` と `_prepare_saved_jobs()`。[test_parallel_dashboard.py](../tests/test_parallel_dashboard.py) の `test_five_workers_queue_stop_isolation_and_resume`、`test_queue_survives_shutdown_but_started_runs_do_not_restart`、`test_failed_start_save_leaves_job_waiting_across_shutdown`。[test_junie_conversation.py](../tests/test_junie_conversation.py) の `test_stop_fence_survives_restart_from_saved_block`、`test_stop_fence_survives_restart_from_running_job_when_queue_write_also_fails`。 |
| `VERIFY-*` | [台帳 `migration_ledger()`](../src/migration_harness/runs/migration_ledger.py) と会話の `submit()` / `advance()`。[test_migration_ledger.py](../tests/test_migration_ledger.py) の `test_expected_file_change_deletion_and_legacy_receipt_require_reverification`、`test_completion_requires_verification_record_but_no_worktree_fingerprint`、[test_junie_conversation.py](../tests/test_junie_conversation.py) の `test_queued_predecessor_identity_survives_restart_or_fails_on_cancellation`。 |
| `APPLY-*` | `runs/worktree_targets.py` の `apply_agent_targets()` と `finish_pending_targets()`。[実ファイルと実Run JSONの対応トレース](../tests/test_run_state_model_traces.py) で2番目の置換時に中断し、再読込後に未反映分だけを採用する場合と、手修正を残して停止する場合を確かめる。 |
| `HOOK-*` | [stage_scripts.py](../src/migration_harness/runs/stage_scripts.py) の `run_script_phase()` と `script_recovery()`。[test_script_recovery.py](../tests/test_script_recovery.py) の `test_crash_after_success_stops_all_resume_paths_and_requires_current_explicit_retry` と `test_launch_is_not_attempted_if_start_record_cannot_be_written` は、実ファイル・保存済みの試行・プロセス起動回数を照合する。 |

モデルが失敗した場合は操作列と条件IDを上表に照らし、同じ操作を通る製品テストで保存内容を確認する。モデルの前提誤りと、製品の動作不良は分けて扱う。

## 実行

標準ライブラリのみでモデル検査9件（2026-11-26のローカルPython 3.13で約0.06秒）:

```text
python -m unittest discover -s tests -p test_run_state_models.py -v
```

Windowsで製品側のトレースも含めて10件実行する場合、通常のテスト準備後に実行:

```text
python -m unittest discover -s tests -p "test_run_state_model*.py" -v
```

Windows CIのPython 3.11 / 3.13それぞれの `remaining` に10件すべてが入り、欠落・スキップ・失敗を成功と扱わない。
