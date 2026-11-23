# Run の状態とロック契約

工程の順序・保存形式・通常の再開条件は [技術仕様](../SPEC.md) を参照。この文書は、ダッシュボードの会話・編集・並行処理に関わる現在の状態と、変更時に守る条件をまとめる。

## 保存状態と責務

| 領域 | 現行の責務・保存境界 |
| --- | --- |
| Run工程 | [workflow.py](../src/migration_harness/workflow.py) が `run.json` の準備、カプセル、ビルド、チェッカー、修正、検証記録を更新する。ダッシュボード表示用の `ready` や `queued` は工程の成否とは別。実行待ちは [RunManager](../src/migration_harness/runs/run_manager.py) が FIFO 順に扱い、`dashboard-queue.json` に操作IDと起動済み状態を保存する。 |
| 編集 | [dashboard.py](../src/migration_harness/web/dashboard.py) と [editor_queue.py](../src/migration_harness/runs/editor_queue.py) が Run・対象・編集版を照合し、手修正と追加指示を `editor.json` に保存する。即時保存できない編集は保留され、workflow の境界で適用する。対象の版が異なる場合は上書きせず停止し、競合と編集内容を残す。Junie結果の複数ファイル反映は `target_apply_pending` のハッシュで中断後の続きと外部変更を識別する。 |
| 会話 | [junie_conversation.py](../src/migration_harness/junie/junie_conversation.py) が受信本文を `editor.json` に記録してからHTTP 202を返す。照会、確認への回答、ビルド後チェッカー、Junieへの追加指示を振り分ける。回答は元の質問ID、検証依頼は対象版と先行ビルドの操作IDに結び付ける。実行開始は `running`、結果は指示ID・対象ハッシュ・ログとともに保存する。 |
| 検証・完了 | workflow がビルドと必要なチェッカーを別々に記録する。 [migration_ledger](../src/migration_harness/workflow.py) は対象ハッシュ、構成、フック、カプセル、検証結果と理由付きの範囲レビューを照合する。検証成功だけでは移行完了にならず、全範囲の確認または理由付き除外が必要。 |

待機ジョブは再起動時に未起動分だけ復元する。起動済みジョブは再実行せず、保存されたRunから利用者が再開する。実行中にJunieの終了を確認できなかった場合、同じRunの会話依頼を保留し、`blocked_conversations` に理由を保存する。保留依頼の失敗記録を書けない間もメモリ上の保留を解除せず、保存を再試行する。保留理由または起動済み印が残っていれば再起動後もそのRunを止める。両方の永続記録が失われた場合の再起動後の保証はないため、残存プロセスと記録を確認してから新しい依頼を送る。別Runは個別の保存が可能なら継続する。

## 重要な順序と不変条件

- 条件付きチェッカー依頼は、受付時点でRunManagerのジョブに保存された先行ビルド操作IDを使う。後から「現在のビルド」を推測しない。通常の終了コード0以外も先行ビルドの終了として識別するが、キャンセル、ビルド本体の停止、`before_build` での停止は条件を満たさない。依頼時に対象とした版を検証する。
- 取消・再起動をまたいで、別の操作IDや過去の成功結果を流用しない。対応付けを復元できない旧形式依頼は安全側に失敗させる。状態照会は工程を再実行しない。
- Junieの終了未確認は、使用量保存など別の後始末エラーの `__cause__` / `__context__` に含まれていても上位へ伝える。ビルド修正・チェッカー修正のどちらでも、修正枠の予約を保持して未完了の試行を履歴へ追加せず、安全を確認した利用者の明示的な再開を待つ。終了未確認の下書きの採用、後続工程、通常の自動再試行は止める。
- 終了未確認後の保留はRun単位にする。履歴保存が失敗している間は同じRunの依頼を起動せず、回復時もまず失敗記録を保存してから保留を解除する。別Runの処理は不必要に止めない。
- ジョブの終了時や起動直前に共有キューの保存が失敗した場合、会話スレッドの巡回で再試行する。障害が続く間は間隔を1秒から最大8秒まで広げる。対象Runの会話履歴の保存を待たず、共有キューが回復すれば空き枠にFIFO順で別Runを割り当てる。起動印を保存できなかったジョブは、待機位置と画面の待機状態を保持する。
- 完了判定は対象版と必要な検証の記録に加え、すべてのPHP範囲に有効な確認または理由付き除外があることを要求する。移行先版が変わったレビューは再確認が必要。

Junie のセッションは `_run_session()` がプロセス、資源、終了確認をまとめて管理します。対話と ACP は完了記録と対象がそろうと早期終了でき、非対話は自然終了を待ちます。ACP の送信期限と追加指示の受理・完了は別に確認されます。終了 observer は子プロセス群の終了確認後に呼び、後始末で残った使用量を回収します。

会話の `submit()` は準備中の履歴と保存済み Run の履歴で、それぞれのロック内で同一 ID・本文を照合します。保存済み Run の受付時点で、対象版と条件付き依頼の先行ビルド操作 ID を記録します。`advance()` はキュー、RunManager の順にロックし、開始済みの指示を照合してから、受理済みの照会・回答を処理し、待機中の指示を実行キューへ登録・保存します。

## ロック範囲

| ロック | 保護範囲と注意点 |
| --- | --- |
| `execution_lock` ([run_records.py](../src/migration_harness/runs/run_records.py)) | OSファイルロック。同じRunの再開・レビューの競合を防ぐ。準備はRunロックの後に共有 `prepare` ロックを取り、共有入力の固定後に解放する。RunManagerの `dashboard` 所有ロックは同じ保存先のダッシュボードを1プロセスに制限する。 |
| `run.lock` / `editor.lock` | JSON本体とは別のファイルをロックし、読取・変更・置換を直列化する。Run observerへの通知は `run.lock` の解放後。WindowsのJSON置換は共有違反に対して短い上限付き再試行を行う。 |
| `RunManager.condition` | 1ダッシュボード内のRun一覧、ジョブ、操作ID、会話スレッドと画面イベントを守る。待機依頼の保存・登録は会話履歴ロックからconditionへ進む。conditionを保持したまま外部コマンドやスレッド終了を待たない。 |
| `worktree_source_lock` ([worktree_targets.py](../src/migration_harness/runs/worktree_targets.py)) | 同一プロセス内で編集・反映を直列化する再入可能ロック。別プロセスには効かないため、保存版と現物のハッシュを照合する。 |
| ビルド停止ロック ([execution.py](../src/migration_harness/execution.py)) | 個々の `ExecutionControl` で停止要求とビルド本体の有効期間を同期する。ロック中に外部コマンドや操作者の応答を待たない。 |

JSON用ロックと工程ロックは別物。ファイルのロック順やconditionを保持する範囲を変える場合は、CLI・HTTP・ワーカー・再起動の各経路でデッドロックと更新消失を再評価する。

移行の再開では `_prepare_migration_workspace()` が途中反映を復旧してから対象ハッシュと停止下書きを照合します。`_execute_migration_capsule()` は完了トークンと全対象の存在を確認してから区間の完了を保存します。確認済み停止下書きの任意メモは、保存した原文範囲、対象ファイルのハッシュ、ルール判定の版が一致する再開でだけ、未確認の手がかりとして渡します。メモの破損は対象ファイルの復元や完了判定と分離します。停止下書き、確認レポートの異常、保存失敗は `FollowupDraftRecoveryTests` と移行サイクルの回帰テストで確認します。

HTTP エディタは取得時に Run ID と表示版を照合します。同じ版なら移行先の本文を再読込せず `unchanged` を返します。編集時はキュー、対象ファイル、Run の順にロックし、保存直前に Run の状態を再確認します。編集キューの処理を終えてから HTTP 応答を返します。会話は受理内容を永続化して 202 を返し、その応答処理の後にディスパッチへ通知します。


任意の出力判定を工程へ反映する場合、機械的に照合できる根拠がなければ合格にしません。一度だけの工程スクリプトが実行済みで判定保留なら、保存したログ範囲と対象版が一致する場合だけその判定を再開し、コマンドを再実行しません。通常の検証で対象内の診断を確認できたチェッカーと、繰り返し検証する `before_build`・`after_build`・`before_checker`・`after_checker` の修正は、共有の回数上限と停滞検知に従い、ビルドから再検証します。自由文は別途有効にした根拠照合で、固定原文と現在の移行先の引用を照合できた場合だけ修正候補になります。曖昧な診断や環境エラーでは自動修正しません。準備前などの成功したフックの申し送りは初回カプセルの参照情報であり、検証結果を更新しません。フック実行前後の対象版の一致を証明できない保存診断は、対象を元の版に戻しても通常再開には使わず、明示的なビルドで新しい出力を取得します。 フック終了後に対象を読み取れない場合も元の出力を保存し、ログを照合できなければ同じ Run では再実行しません。

繰り返し検証する工程スクリプトでは、判定が途中で止まった場合、通常再開で同じ対象版・コマンド・出力を照合し、保存した出力から判定を再開します。フック実行中に移行先が変わった場合、旧版の出力は変更後の版の修正や合格には使いません。元の出力を確認し、明示的にビルドし直してください。判定が完了して非0終了となったフックは、依存関係などを直した後、移行先を変更しなくても明示的なビルドで再検証できます。いずれの場合も元の出力と判定履歴は残り、新しい実行結果で判定します。新しい記録ではコマンドの出力範囲全体を SHA-256 で照合します。32 KiB の送信上限を超えた元ログも、全範囲が一致すれば、対象編集後は明示ビルドで再検証できます。元ログの一部だけをモデルへ送信したり、元の失敗を合格にしたりはしません。先頭のハッシュしかない旧形式の長文記録では全体の一致を証明できないため、新しい Run が必要です。

## 回帰テストとの対応

| 条件 | 主な既存テスト |
| --- | --- |
| 照会と受付済み版、先行ビルドの識別 | [test_junie_conversation.py](../tests/test_junie_conversation.py): `test_verification_status_questions_only_read_saved_information`、`test_conditional_checker_waits_for_build_and_verifies_saved_edit`、`test_conditional_checker_binds_to_build_waiting_for_a_real_worker_slot`、`test_queued_predecessor_identity_survives_restart_or_fails_on_cancellation` |
| 回答・取消・再読込と検証要件 | [test_junie_conversation.py](../tests/test_junie_conversation.py): `test_full_conversation_allows_status_and_existing_answer`、`test_full_conversation_runs_checker_once_after_reload_and_answer`、`test_full_conversation_allows_cancellation_and_frees_one_slot`、`test_interrupted_followup_keeps_required_checker_across_restart` |
| 停止・通常失敗・保存障害の分離 | [test_junie_conversation.py](../tests/test_junie_conversation.py): `test_wrapped_unconfirmed_stop_blocks_build_repair_followup`、`test_wrapped_unconfirmed_stop_blocks_checker_repair_followup`、`test_ordinary_repair_cleanup_failure_allows_waiting_build`、`test_stop_fence_retries_failed_editor_write_and_keeps_other_run_available`、`test_stop_fence_survives_restart_from_saved_block`、`test_stop_fence_survives_restart_from_running_job_when_queue_write_also_fails` |
| 実子プロセスの終了確認 | [test_junie_process.py](../tests/test_junie_process.py): `test_natural_launcher_exit_confirms_no_late_writes_and_preserves_an_unrelated_process`、`test_connection_retry_starts_only_after_the_previous_child_exits`、`test_unconfirmed_exit_blocks_result_adoption_and_retry`、`test_windows_batch_launcher_owns_descendants_after_it_exits` |
| 並行Runと同時編集 | [test_parallel_dashboard.py](../tests/test_parallel_dashboard.py): `test_five_workers_queue_stop_isolation_and_resume`、`test_same_target_runs_keep_editor_files_php_and_prompts_separate`。 [test_junie_conversation.py](../tests/test_junie_conversation.py): `test_razor_pair_verifies_both_saved_targets_while_another_run_builds` |
| 台帳とレビューの版照合 | [test_migration_ledger.py](../tests/test_migration_ledger.py): `test_keeps_review_reason_and_flags_stale_after_edit`、`test_anchor_usage_keeps_reviews_written_by_another_cli`、`test_completion_requires_verification_record_but_no_worktree_fingerprint` |

横断ケースはHTTP受付・RunManager・保存と再読込・workflowを通す。Junieや.NETの実サービスは置換する統合テストがあるため、実環境の検証結果とは区別する。外部プロセスの停止は、WindowsのJob Objectでランチャーとその子孫を管理し、実プロセスを使ったテストで確認する。

明示再開は `_validate_resume_plan()` が保存版と実行条件を再計算し、`_load_resume_context()` がカプセルと進捗を照合してから工程を選びます。移行の巻き戻し、修正、停止下書きは `_save_target_snapshot()` と同じ形式を共有します。復元前に記録、内容ハッシュ、復元対象を確認します。

## 結果不明のスクリプト

再開可能な一度限りのフック（`after_prepare`／`before_migration`／`after_migration`）は、コマンド起動前に試行IDとコマンド版を `script_inflight` として保存する。起動後に終了結果を確定できなければ記録を残し、通常・明示再開とも止める。通常終了したコマンドは終了結果を確定し、成功済みのコマンドと自由文判定待ちは既存の条件で追跡する。明示的な `script-recovery --retry VERSION` は Run の実行ロック内で対象と版を照合し、その1コマンドだけを再試行する。外部の副作用とは同じトランザクションではない。

## 明示的な再開要求

通常の再開は、同じ Run と worktree の保存状態から未完了の工程を選ぶ。明示的な再開では、開始工程、対象ファイルとその内容のハッシュ、保留編集の ID と版、区間、後続結果の無効化を一つの要求に記録する。実行直前に保存版を照合し、差があれば編集を保持したまま実行を中止する。

移行完了後の「ビルドのみ」は、設定済みの全移行先と保存済み手修正を対象とする。未反映の Junie 指示がある場合は通常の再開で処理する。以前のビルド・チェッカー結果を無効化してビルドし、チェッカーと自動修正は起動しない。通常の再開ボタンと旧 Run の扱いは従来どおり。

区間を指定する要求は、開始位置、終点、対象版、対象外の編集、無効化する区間以降の完了記録・検証・レビューを同時に照合する。複数の区間が同じファイルを編集する場合に必要な復元地点と編集範囲の照合がそろうまで、区間の変更は提供しない。チェッカー単独の再開には、現在の対象版に対応するビルド成果物の照合が必要となる。
