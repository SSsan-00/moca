# Run の状態とロック契約

工程の順序・保存形式・通常の再開条件は [技術仕様](../SPEC.md) を参照。この文書は、ダッシュボードの会話・編集・並行処理に関わる現在の状態と、変更時に守る条件をまとめる。

## 保存状態と責務

| 領域 | 現行の責務・保存境界 |
| --- | --- |
| Run工程 | [workflow.py](../src/migration_harness/workflow.py) が `run.json` の準備、カプセル、ビルド、チェッカー、修正、検証記録を更新する。ダッシュボード表示用の `ready` や `queued` は工程の成否とは別。実行待ちは [RunManager](../src/migration_harness/run_manager.py) が FIFO 順に扱い、`dashboard-queue.json` に操作IDと起動済み状態を保存する。 |
| 編集 | [dashboard.py](../src/migration_harness/dashboard.py) と [editor_queue.py](../src/migration_harness/editor_queue.py) が Run・対象・編集版を照合し、手修正と追加指示を `editor.json` に保存する。即時保存できない編集は保留され、workflow の境界で適用する。対象の版が異なる場合は上書きせず停止し、競合と編集内容を残す。Junie結果の複数ファイル反映は `target_apply_pending` のハッシュで中断後の続きと外部変更を識別する。 |
| 会話 | [junie_conversation.py](../src/migration_harness/junie_conversation.py) が受信本文を `editor.json` に記録してからHTTP 202を返す。照会、確認への回答、ビルド後チェッカー、Junieへの追加指示を振り分ける。回答は元の質問ID、検証依頼は対象版と先行ビルドの操作IDに結び付ける。実行開始は `running`、結果は指示ID・対象ハッシュ・ログとともに保存する。 |
| 検証・完了 | workflow がビルドと必要なチェッカーを別々に記録する。 [migration_ledger](../src/migration_harness/workflow.py) は対象ハッシュ、構成、フック、カプセル、検証結果と理由付きの範囲レビューを照合する。検証成功だけでは移行完了にならず、全範囲の確認または理由付き除外が必要。 |

待機ジョブは再起動時に未起動分だけ復元する。起動済みジョブは再実行せず、保存されたRunから利用者が再開する。実行中にJunieの終了を確認できなかった場合、同じRunの会話依頼を保留し、`blocked_conversations` に理由を保存する。保留依頼の失敗記録を書けない間もメモリ上の保留を解除せず、保存を再試行する。保留理由または起動済み印が残っていれば再起動後もそのRunを止める。両方の永続記録が失われた場合の再起動後の保証はないため、残存プロセスと記録を確認してから新しい依頼を送る。別Runは個別の保存が可能なら継続する。

## 重要な順序と不変条件

- 条件付きチェッカー依頼は、受付時点でRunManagerのジョブに保存された先行ビルド操作IDを使う。後から「現在のビルド」を推測しない。通常の終了コード0以外も先行ビルドの終了として識別するが、キャンセル、ビルド本体の停止、`before_build` での停止は条件を満たさない。依頼時に対象とした版を検証する。
- 取消・再起動をまたいで、別の操作IDや過去の成功結果を流用しない。対応付けを復元できない旧形式依頼は安全側に失敗させる。状態照会は工程を再実行しない。
- Junieの終了未確認は、使用量保存など別の後始末エラーの `__cause__` / `__context__` に含まれていても上位へ伝える。ビルド修正・チェッカー修正のどちらでも、修正枠の予約を保持して未完了の試行を履歴へ追加せず、安全を確認した利用者の明示的な再開を待つ。終了未確認の下書きの採用、後続工程、通常の自動再試行は止める。
- 終了未確認後の保留はRun単位にする。履歴保存が失敗している間は同じRunの依頼を起動せず、回復時もまず失敗記録を保存してから保留を解除する。別Runの処理は不必要に止めない。
- ジョブの終了時や起動直前に共有キューの保存が失敗した場合、会話スレッドの巡回で再試行する。障害が続く間は間隔を1秒から最大8秒まで広げる。対象Runの会話履歴の保存を待たず、共有キューが回復すれば空き枠にFIFO順で別Runを割り当てる。起動印を保存できなかったジョブは、待機位置と画面の待機状態を保持する。
- 完了判定は対象版と必要な検証の記録に加え、すべてのPHP範囲に有効な確認または理由付き除外があることを要求する。移行先版が変わったレビューは再確認が必要。

## ロック範囲

| ロック | 保護範囲と注意点 |
| --- | --- |
| `execution_lock` ([run_records.py](../src/migration_harness/run_records.py)) | OSファイルロック。同じRunの再開・レビューの競合を防ぐ。準備はRunロックの後に共有 `prepare` ロックを取り、共有入力の固定後に解放する。RunManagerの `dashboard` 所有ロックは同じ保存先のダッシュボードを1プロセスに制限する。 |
| `run.lock` / `editor.lock` | JSON本体とは別のファイルをロックし、読取・変更・置換を直列化する。Run observerへの通知は `run.lock` の解放後。WindowsのJSON置換は共有違反に対して短い上限付き再試行を行う。 |
| `RunManager.condition` | 1ダッシュボード内のRun一覧、ジョブ、操作ID、会話スレッドと画面イベントを守る。待機依頼の保存・登録は会話履歴ロックからconditionへ進む。conditionを保持したまま外部コマンドやスレッド終了を待たない。 |
| `worktree_source_lock` ([worktree_targets.py](../src/migration_harness/worktree_targets.py)) | 同一プロセス内で編集・反映を直列化する再入可能ロック。別プロセスには効かないため、保存版と現物のハッシュを照合する。 |
| ビルド停止ロック ([execution.py](../src/migration_harness/execution.py)) | 個々の `ExecutionControl` で停止要求とビルド本体の有効期間を同期する。ロック中に外部コマンドや操作者の応答を待たない。 |

JSON用ロックと工程ロックは別物。ファイルのロック順やconditionを保持する範囲を変える場合は、CLI・HTTP・ワーカー・再起動の各経路でデッドロックと更新消失を再評価する。

## 回帰テストとの対応

| 条件 | 主な既存テスト |
| --- | --- |
| 照会と受付済み版、先行ビルドの識別 | [test_junie_conversation.py](../tests/test_junie_conversation.py): `test_verification_status_questions_only_read_saved_information`、`test_conditional_checker_waits_for_build_and_verifies_saved_edit`、`test_conditional_checker_binds_to_build_waiting_for_a_real_worker_slot`、`test_queued_predecessor_identity_survives_restart_or_fails_on_cancellation` |
| 回答・取消・再読込と検証要件 | [test_junie_conversation.py](../tests/test_junie_conversation.py): `test_full_conversation_allows_status_and_existing_answer`、`test_full_conversation_runs_checker_once_after_reload_and_answer`、`test_full_conversation_allows_cancellation_and_frees_one_slot`、`test_interrupted_followup_keeps_required_checker_across_restart` |
| 停止・通常失敗・保存障害の分離 | [test_junie_conversation.py](../tests/test_junie_conversation.py): `test_wrapped_unconfirmed_stop_blocks_build_repair_followup`、`test_wrapped_unconfirmed_stop_blocks_checker_repair_followup`、`test_ordinary_repair_cleanup_failure_allows_waiting_build`、`test_stop_fence_retries_failed_editor_write_and_keeps_other_run_available`、`test_stop_fence_survives_restart_from_saved_block`、`test_stop_fence_survives_restart_from_running_job_when_queue_write_also_fails` |
| 実子プロセスの終了確認 | [test_junie_process.py](../tests/test_junie_process.py): `test_natural_launcher_exit_confirms_no_late_writes_and_preserves_an_unrelated_process`、`test_launcher_exit_without_waitid_preserves_group_ownership`、`test_unconfirmed_exit_blocks_result_adoption_and_retry`、`test_windows_batch_launcher_owns_descendants_after_it_exits` |
| 並行Runと同時編集 | [test_parallel_dashboard.py](../tests/test_parallel_dashboard.py): `test_five_workers_queue_stop_isolation_and_resume`、`test_same_target_runs_keep_editor_files_php_and_prompts_separate`。 [test_junie_conversation.py](../tests/test_junie_conversation.py): `test_razor_pair_verifies_both_saved_targets_while_another_run_builds` |
| 台帳とレビューの版照合 | [test_migration_ledger.py](../tests/test_migration_ledger.py): `test_keeps_review_reason_and_flags_stale_after_edit`、`test_anchor_usage_keeps_reviews_written_by_another_cli`、`test_completion_requires_verification_record_but_no_worktree_fingerprint` |

横断ケースはHTTP受付・RunManager・保存と再読込・workflowを通す。Junieや.NETの実サービスは置換する統合テストがあるため、実環境の検証結果とは区別する。Windows Job Object、macOSの `waitid` 不在時の `/bin/ps` 観測などOS固有の保証は、実行したOSとPython版の結果に限る。
