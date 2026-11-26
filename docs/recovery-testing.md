# Windowsの保存障害と再起動を検査する

[Runの状態とロック契約](run-state-contracts.md) および [有限状態モデル](run-state-models.md) を補完する。モデルは指定した安全条件を全探索し、ここでは重要な保存境界でWindows上のテスト所有プロセスを実際に終了する。製品には障害注入のフラグや実行時依存を追加しない。

## 障害点と確認する状態

| 境界 | 実プロセス／既存試験 | 障害後の期待値と正常対照 |
| --- | --- | --- |
| 一時JSON作成後、本体置換の直前・直後 | [test_windows_recovery_faults.py](../tests/test_windows_recovery_faults.py) の `test_atomic_json_survives_termination_before_and_after_replace` | 終了が置換前なら旧版、置換後なら新版を読める。途中JSONは本体として読まず、保存済み停止理由は保持。別プロセスにより次の更新を正常に保存する。 |
| OS共有違反（一時／継続） | 同ファイルの `test_real_windows_sharing_violation_retries_or_leaves_old_record` | Win32の読取ハンドルでは削除共有を許可せず、製品の置換がOSから拒否される。最初の再試行境界でハンドルを解放すると成功、保持したままなら失敗が表面化して旧JSONが残る。解除後の再更新は成功する。 |
| 反映予定、2対象の各置換、反映完了記録のすべての前後 | 同ファイルの `test_two_target_adoption_restarts_at_each_durable_cut` と [APPLYモデル対照](../tests/test_run_state_model_traces.py) | 予定を保存する前なら書かない。保存後は再読込で未反映分だけを処理し、完了時に実ファイルのハッシュと照合する。中断直後の外部手修正は上書きせず、旧停止理由・反映予定を維持する。別のRunや実業務のファイルは使わない。 |
| 待機列の「開始印」保存の直前・直後 | 同ファイルの `test_queue_restart_only_launches_waiting_operations`、[test_parallel_dashboard.py](../tests/test_parallel_dashboard.py) の `test_failed_start_save_leaves_job_waiting_across_shutdown` | まだ待機として保存された依頼だけを元の操作ID・FIFO順で起動。開始印付きは処理未着手でも自動再実行しない。通常の2件開始も対照で確認。 |
| 編集処理・編集履歴保存 | [test_editor_save_recovery.py](../tests/test_editor_save_recovery.py) の `test_normal_resume_keeps_operator_history_after_failed_save_and_resave`、`test_normal_resume_recovers_between_agent_and_worktree_replacement`、`test_successful_save_retry_and_external_change_do_not_rewrite_history` | 例外注入。未反映の編集、採用済みの内容、保存後に加えた手修正を区別し、新しい履歴として二重に適用しない。 |
| 一度だけのフック実行後、結果保存前 | [test_windows_recovery_faults.py](../tests/test_windows_recovery_faults.py) の `test_unknown_once_hook_survives_process_death_without_implicit_replay`、[test_script_recovery.py](../tests/test_script_recovery.py) | 合成カウンタを増やす外部コマンドが終了してからRunを強制終了する。結果不明の試行証跡が残り、通常再開は実行しない。版を確認した明示再試行だけが副作用を1回進め、その成功を以後再利用する。 |
| 修正枠の確定・終了未確認・元の停止理由 | [test_checker_resume_review_dashboard.py](../tests/test_checker_resume_review_dashboard.py) の `test_interrupted_repair_can_restart_with_a_one_attempt_limit`、`test_usage_save_failure_preserves_only_unconfirmed_repairs_for_explicit_resume`、[test_junie_conversation.py](../tests/test_junie_conversation.py) の停止と再起動試験 | 例外注入と制御応答。未完了の修正を成功・失敗確定にせず、終了未確認なら自動再試行しない。後処理エラーがあっても元の理由を保持。 |

## 再現

通常のWindows開発環境で、既存依存を準備したあとに実行:

```powershell
python -m unittest discover -s tests -p test_windows_recovery_faults.py -v
```

子プロセスは同じPythonで動き、対象はテストが所有する一時ディレクトリのみ。標準出力に正確な境界到達を通知し、親が確認してからその子だけを終了する。再起動の判定は別の子プロセスで行う。固定の待ち時間を故障条件には使わず、期限は検査を失敗させるための上限。例外時も親が子を回収する。両Python版の必須CIに含め、欠落やスキップを合格にしない。

プロセスの強制終了は停電、ファイルシステム／媒体の破損、永続証跡がすべて失われた場合を再現しない。副作用が実行済みか判断できなければ通常再開を止め、保存済みの試行・ログ・残存プロセスを確認して明示的に再試行する。業務アプリ、実Junie、DBは起動しない。

## 限定ミューテーション検査（#306）

[変異プローブ](../tests/test_workflow_mutations.py) は、製品のソースを一時領域へコピーして1箇所ずつ故意に変える。元と変異後を別々のPythonプロセスで実行し、コピーのハッシュと import 先、選んだテストID、変異行まで実行が届いたこと、意図したアサーションのファイル・文・失敗内容を照合する。元のチェックアウトや配布物には変異を適用しない。

| 変異 | 検出する回帰条件 |
| --- | --- |
| `stale-mapping` | 移行先編集後に古い対応証跡を有効と表示しない。 |
| `stale-verification` | 古い対象版の検証記録で完了と表示しない。 |
| `repair-charged-twice` | 1回の修正試行を履歴に二重計上しない。 |
| `legacy-rule-newline` | 旧Runの必須資料の元の改行を勝手に変換しない。 |

変異が正しく適用され、正常版が通り、意図したアサーションだけが失敗したものを検出成功とする。構文エラー・無関係な例外・タイムアウト・対象行未実行は検出に数えない。変異後も通る場合は未検出として、テスト不足と等価な変異を人が区別する。カバレッジや全体の変異スコアの保証ではない。実ファイル置換・終了未確認の安全条件は上の実プロセス／既存回帰で別に検査し、今回はその変異率を計算しない。

Windowsの必須CIには最初の変異と、無関係なエラーを誤って検出としない分類テストを含める。4変異すべては次で再現する。

```powershell
python tests/test_workflow_mutations.py --full
```
