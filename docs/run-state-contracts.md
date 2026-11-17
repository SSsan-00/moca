# Run の状態とロック契約

[SPEC](../SPEC.md) の工程順序を変更する場合は、この文書の遷移表と回帰テストの対応も更新する。対象はダッシュボードと同じ保存先を開く CLI。

## 状態と保存境界

| 状態 | 現行の遷移・保存・復元 |
| --- | --- |
| 工程・実行枠 | `run.json` の `status`、カプセル番号、処理済み数、停止時点を [workflow](../src/migration_harness/workflow.py) が更新する。準備から Junie、検証へ進み、合流・競合・操作者への質問では `editor_checkpoint` で止まり得る。`RunManager.enqueue/start_pending/execute` は `dashboard-queue.json` に FIFO と起動済み印を保存し、未開始のジョブを復元する。起動済みは繰り返さず、保存チェックポイントを表示する。`ready` は画面の操作受付可否、`queued` は枠待ち、`stop_requested` は現在の操作に対する停止要求を表す。 |
| 編集と追加指示 | [dashboard](../src/migration_harness/dashboard.py) の `send_editor` は Run・対象・編集 ID を照合する。実行中、既存の保留あり、または基準版が異なるときは `editor.json` に基準・本文・編集 ID・`edit_revision` を保存する。待機中のファイル内容は元の版。即時保存可能なら worktree を更新してから応答する。ワーカーは境界で `_apply_editor_edits` または `_apply_verification_edits` を通し、競合を保存して停止する。反映途中の I/O エラーでは適用済み分と保留分が残り得る。Junie からの反映は `target_apply_pending` に前後のハッシュを先に書き、`finish_pending_targets` が照合して残りを回復する。追加指示は `dispatch_prompts/finish_prompts/LivePrompts` が配信・反映を追跡し、未適用分を保留へ戻す。 |
| 会話 | [junie_conversation](../src/migration_harness/junie_conversation.py) の `submit` が `editor.json` に `received` を保存して HTTP 202 を返す。準備中の照会は専用履歴へ保存する。 `advance` は照会、`clarification`、`waiting`、`steering` へ振り分ける。照会・既存確認への回答は新規16件枠を消費しない。回答は受付時の質問 ID を照合する。検証ワーカーの `run_instruction` は開始を `running` として保存してから workflow を呼ぶ。 `finish_instruction` は結果の指示 ID を照合し、結果・対象ハッシュ・ログを保存する。`done` には照会・回答・通常のビルド失敗も含まれる。取消済みは復元せず、起動済みで結果不明は `unknown`。未確認の条件は `failed` になり、再送を案内する。 |
| 検証・台帳 | `_run_verification` は実行するコマンドと設定上必要なチェッカーを別々に記録する。チェッカー省略のビルドも必要性を保持する。`_missing_required_checker/can_resume_run/_resume_with_junie` は設定が欠けた Run の再開を止める。`migration_ledger` の `verification_passed` は最新対象ハッシュ、構成、フック、全カプセル、ビルド成功、必要なチェッカーの成功または有効な承認を照合する。`complete` には全 PHP 範囲の理由付き確認・除外も必要。`review_migration_ranges` の確認は、照合した版が変わると台帳上で `stale` になる。 |

### 競合する操作の順序

- **条件付き依頼** — 枠待ちの時点で RunManager のジョブに操作 ID を採番・保存する。会話は現在のジョブからその ID を採取し、実行枠に入ると同じ ID を `ExecutionControl` に渡す。 `_build_worktree` の通常終了記録が ID と非負の終了コードで一致して初めて待機条件を満たす。通常の終了コード 1 も条件を満たし、後続版自身が成功してからその版のチェッカーへ進む。取消・ビルド本体の停止・`before_build` 中の停止・対応 ID を保存していない旧形式には、その操作の通常終了記録がない。復元時も過去の成功を流用しない。
- **停止と再開** — `Dashboard.request_stop` は表示版の更新をまたいでも操作 ID を照合する。会話による停止付き再ビルドは `ExecutionControl.request_build_stop` と `build_process_scope` がビルド本体の有効区間を共有する。自然終了が先なら後続は待機する。Junie は `_run_session` が同じ呼び出しのプロセス群を終了確認してから成果物の採用と次工程を許す。終了未確認は `JunieStopError` を通知し、その呼び出しの通常通信再試行や修正下書きの引継ぎを中止する。ダッシュボードでは同じ Run でその時点までに受け付けた実行待ちの依頼も失敗として記録する。診断保存などで別の例外が連鎖してもこの扱いを維持する。再開操作の前に残存プロセスの終了を利用者が確認する。

## ロックの保護範囲とネスト

| 保護 | 実際の取得・解放と代表経路 |
| --- | --- |
| [`execution_lock`](../src/migration_harness/run_records.py) | 同じ `run_root` の別 CLI にも有効な OS ファイルロック。`resume_with_junie` と指摘を承認する `review_checker` は Run 単位で取得し、工程または承認が終わるまで保持する。新規・準備は Run → 同じ保存先の共有 `prepare` の順で取得し、共有入力の準備後に `prepare` を解放する。Run と `dashboard` 所有は競合時に即時拒否。 `prepare` は短い再試行間隔で取消確認を行う。`RunManager.configure/close` は `dashboard` 所有を設定の切替またはワーカー終了まで保持する。 |
| `run.lock` / `editor.lock` | `lock_run_record` は JSON と別のロックファイルを使用。 `write_run_record/edit_run_record` と [`editor_queue`](../src/migration_harness/editor_queue.py) が担当ファイルの読取・変更・一時ファイル置換を保護する。POSIX は `flock` の待機、Windows は `msvcrt.locking` のバイト範囲ロックと OS エラー・再試行の挙動に従う。工程保存はレビューや使用量を最新記録から引き継ぎ、**run.lock 解放後**に observer から `Dashboard.observe/publish` の condition に入る。台帳の PHP 範囲レビューは Run 実行ロックを取らず、 `edit_run_record` 内で工程を再確認する。 `load_saved_run` の独立した読取は置換済みの JSON を読む。 |
| `RunManager.condition` | 1 ダッシュボード内の全 Run で共有する再入可能な `Condition`。view、ジョブ、枠、操作 ID、画面イベントを保護する。会話の通常受付とディスパッチは editor.lock → condition と進み、 `advance` から `enqueue/persist/start_pending` を呼ぶ。新規スレッドは condition 内で起動してもそこで join しない。準備中照会の専用履歴は condition → 専用履歴ロックで、通常の editor.lock とは別。 `hide_run` は condition から非待機で Run 実行ロックを試す。 |
| [`worktree_source_lock`](../src/migration_harness/worktree_targets.py) | 同一プロセスで全 Run に共通の `RLock`。HTTP 編集保存は editor.lock → worktree → condition と取り、直前の状態を再確認する。`save_target_edit` は worktree ロックを再入取得する。会話ワーカーのファイル適用は editor.lock → worktree、Junie 結果反映は worktree → run.lock の保存後に observer → condition と進み得る。別 CLI や直接編集にはこのメモリロックは効かないため、保存した版と現物を照合する。 |
| [ビルド停止用ロック](../src/migration_harness/execution.py) | `ExecutionControl` ごとの `Lock`。会話側は condition 内で `request_build_stop` を呼ぶ。ワーカーは `build_process_scope` の入口と出口でビルド本体のフラグ・取消を照合する。ロック内で外部コマンド終了・observer・操作者の入力を待たない。 |

SSE の `Condition.wait_for` は待機中に condition を解放し、復帰時に再取得する。Junie の承認は `Future` の完了を確認してから結果を読み、信頼確認の待機は取消を定期確認する。アンカー生成の HTTP 側 `Future.result()` と、 `RunManager.close` のワーカー・ディスパッチャへの join は condition の外。回答確定と枠待ちアンカーの取消では条件内で `Future` を決着させるため、同期 callback から別のロックを取る変更は再検討が必要。工程の実行ロックは外部コマンドや操作者の応答を待つ間も保持する。

## 不変条件と横断回帰（#37）

「会話」は [test_junie_conversation.py](../tests/test_junie_conversation.py) を指す。着手時の main `c017acb` に #31（`477b5ec`, `1ac2c97`）、#32（`78bf039`, `da8d56d`, `170a50a`）、#33（`4addbe1`）の変更が含まれる。#34 の実行枠待ちと #35 の親先行終了は、同じ main で再現した残存経路。

| 守る条件・シナリオ | 既存の根拠 | 今回確認する不足分 |
| --- | --- | --- |
| 読取専用照会・受付した版と結果の対応（T1） | 会話 `test_receipt_precedes_interpretation_and_query_does_not_stop_a_build`、`test_verification_status_questions_only_read_saved_information`、`test_stopping_before_build_does_not_use_the_preceding_operations_exit_code` | 会話 `test_conditional_checker_waits_for_build_and_verifies_saved_edit` に再送、再読込、チェッカー中の未検証、版ごとのハッシュ、最終レビュー後の完了を追加。 `test_conditional_checker_binds_to_build_waiting_for_a_real_worker_slot` と `test_queued_predecessor_identity_survives_restart_or_fails_on_cancellation` で枠待ちからの照合を検証。 |
| 16件上限、確認への回答・取消、再送（T2） | 会話 `test_an_answer_cannot_be_applied_to_a_replacement_question`、`test_conversation_and_confirmation_are_isolated_between_runs` | `test_full_conversation_allows_status_and_existing_answer` と `test_full_conversation_allows_cancellation_and_frees_one_slot` に再読込を追加。 `test_full_conversation_runs_checker_once_after_reload_and_answer` がチェッカー回答、呼出し回数、残る15件を確認。 |
| 検証要件の保持（T3） | 会話 `test_build_only_does_not_restore_a_null_cli_checker`、`test_restarted_conversation_cannot_drop_the_cli_checker_requirement`、`test_interrupted_followup_keeps_required_checker_across_restart`、`test_interrupted_followup_can_resume_when_no_checker_was_configured` | 既存テストが TOML 既定値上書き、CLI のみ、チェッカー不要、通常再開と会話再実行をカバーするため再利用。台帳の版失効と独立更新は [test_migration_ledger.py](../tests/test_migration_ledger.py) の `test_keeps_review_reason_and_flags_stale_after_edit` と `test_anchor_usage_keeps_reviews_written_by_another_cli`。 |
| 停止・自然終了・再送・復元（T4） | 会話 `test_stop_and_rebuild_uses_saved_edit_once`、`test_restart_recovers_unexecuted_but_never_restarts_started_instructions`、`test_cancelling_queued_verification_cannot_enqueue_it_again`、[test_execution_control.py](../tests/test_execution_control.py) の `test_stop_is_bound_to_operation_even_after_activity_revision_changes` | `test_stop_after_natural_build_exit_waits_without_cancelling_checker` に待機時と再起動後の同一 ID 再送を追加し、実行中に停止できた場合も再起動後に同じハッシュを取得する。停止競合の合図は実フック処理に入る直前を同期し、実停止判定と workflow はそのまま使用。 |
| 保存版の競合と Run 分離（T5） | 会話 `test_newer_saved_edit_invalidates_a_waiting_rebuild`、`test_conflicting_saved_edit_is_preserved_and_does_not_build`、[test_dashboard_editor.py](../tests/test_dashboard_editor.py) の `test_operation_starting_during_save_keeps_the_edit_without_changing_running_inputs`、`test_razor_pair_saves_both_targets_at_the_checkpoint`、[test_parallel_dashboard.py](../tests/test_parallel_dashboard.py) の `test_same_target_runs_keep_editor_files_php_and_prompts_separate` | `test_razor_pair_verifies_both_saved_targets_while_another_run_builds` で2ファイルの保存、古い依頼の拒否、最新2ファイルのビルドとチェッカー、並行 Run の継続および再読込後の台帳を確認。 |
| 終了確認（#35） | [test_junie_activity.py](../tests/test_junie_activity.py) の `test_real_posix_launcher_and_child_stop_on_dashboard_cancellation`、[test_junie_dashboard_acp.py](../tests/test_junie_dashboard_acp.py) の `test_permission_choices_are_bound_to_the_current_request_and_returned_to_junie` | [test_junie_process.py](../tests/test_junie_process.py) の `test_natural_launcher_exit_confirms_no_late_writes_and_preserves_an_unrelated_process`、`test_cancel_when_launcher_exits_still_confirms_child_exit`、`test_unconfirmed_exit_blocks_result_adoption_and_retry`、`test_connection_retry_starts_only_after_the_previous_child_exits`。端末入力と Ctrl+C は `test_terminal_input_ctrl_c_and_foreground_restoration`、Windows バッチ継承は `test_windows_batch_launcher_owns_descendants_after_it_exits`。会話の `test_unconfirmed_junie_stop_does_not_continue_with_a_waiting_build` が修正結果の未採用と待機ジョブの中止を確認し、`test_unconfirmed_junie_stop_blocks_waiting_build_even_when_saving_diagnostics_fails` が診断保存に失敗した場合も検証。 |

横断テストでは実 HTTP、RunManager、保存・再読込、workflow と台帳を通す。.NET とチェッカーの外部コマンドだけを同期・版検査付きの成功／失敗結果に置き換え、T3 の中断と完了には Junie の呼出し境界で停止または完了トークンを返す。#35 は実 Python 親子と ACP 代替プロセスを使う。

## 保証の範囲

通常の子は POSIX の専用プロセスグループ、Windows の呼び出し専用 Job Object を継承する。POSIX はリーダーを未回収のまま同じグループの終了を確認し、対話時は前景端末を子グループに渡し、終了時に戻す。Windows はランチャーが動く前に Job へ所属させ、常駐メンバーがゼロになるまで確認する。独立したセッションやグループへの離脱、OS 全体の封じ込め、対話端末の Ctrl+Z によるジョブ制御は保証範囲外。会話のクラッシュをまたぐ完全な一度限りの実行や、worktree 全体のスナップショット分離も保証しない。別 OS の実行結果とスキップは、検証したコミットごとに報告する。
