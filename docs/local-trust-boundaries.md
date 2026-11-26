# ローカル実行の信頼境界と通信

MoCAはWindows上で、利用者が登録した移行先・参考資料・工程を管理するローカルツールです。以下の制限はMoCA内の処理に適用され、Windows全体や子プロセスを隔離するサンドボックスではありません。移行で使ってよいソース、ルール、作業フォルダーと実行コマンドを登録してください。資格情報、業務DB、業務アプリの起動は、MoCA自身の移行対象ではありません。

| 境界 | 仕様と確認箇所 | 関連する回帰 |
| --- | --- | --- |
| ブラウザーとローカルHTTP | サーバーはループバックを使用します。共通受付でHost、URLの接頭辞、状態を変更するリクエストのOriginと本文条件を確認し、Runを特定してから機能へ振り分けます。Windows通知から開く専用リンクは無効化・期限を確認し、開くだけではJunieへの回答になりません。 [dashboard_http.py](../src/migration_harness/web/dashboard_http.py) | [通知と回答の照合](../tests/test_junie_notifications.py)、[別Runのエディタと入力上限](../tests/test_dashboard_editor.py)、[Run別の比較](../tests/test_dashboard_comparison_routes.py) |
| Run・作業ツリー・対象の版 | 同じ名前でもRunと設定された移行対象を区別します。人の編集は保存前に元の版を照合し、作業ツリー内で許可された対象だけを更新します。中断後の反映も、保存された版と一致したものだけ進めます。パスの包含・リンク成分は [files.py](../src/migration_harness/files.py)、反映と版照合は [worktree_targets.py](../src/migration_harness/runs/worktree_targets.py) を参照してください。一般のローカルプロセスが作業ツリーを並行変更できないことや、すべてのWindowsのリンク種別の保証を意味しません。 | [編集の再送・競合・復旧](../tests/test_editor_save_recovery.py)、[Run切り替えと下書き](../tests/test_editor_run_isolation.py) |
| Junieの資料と確認 | MoCAによるACPの固定資料の直接読み取りは `[junie]` の `pinned_reference_reads` を有効にしたときだけ使います（既定は無効）。この方法で供給する対象は、今回の作業場所に固定した資料、選択されたルール、保存した版、許容する読取り範囲に限ります。一般の操作は、この固定資料の読取りとは分けて扱います。回答はRun・操作・質問と結び付けます。ダッシュボードのBraveは対象の操作で利用者が都度選び、ACPで有効を確認できた場合だけ使います。 [acp_permissions.py](../src/migration_harness/junie/acp_permissions.py) · [acp_session.py](../src/migration_harness/junie/acp_session.py) | [固定資料・セッションと許可](../tests/test_acp_permissions.py)、[Junieの確認](../tests/test_junie_dashboard_acp.py)、[Braveのキュー](../tests/test_junie_brave_queue.py) |
| 原文、ルール、診断、工程スクリプト | プロンプトは原文や診断出力を命令ではなくデータとして扱い、ルールは設定上の役割に従って使うよう指定します。これはモデルの動作を強制するOS権限ではありません。ビルド、フック、チェッカーは登録したコマンドであり、Junieと同様に起動ユーザーの権限と利用中の構成に従います。信頼できる実行ファイル・スクリプトを登録してください。 [migration_session.py](../src/migration_harness/junie/migration_session.py) · [script_handoff.py](../src/migration_harness/verification/script_handoff.py) · [stage_scripts.py](../src/migration_harness/runs/stage_scripts.py) | [ドライラン](../tests/test_dry_run.py)、[工程スクリプトの復旧](../tests/test_script_recovery.py) |
| ログ・エラーと通知 | ローカルのRunログには処理情報が残ります。診断表示では対象となるコマンド引数や確認できる値を伏せ字にし、例外のフレームから環境全体・ソース行・ローカル変数を列挙しません。これは第三者ツールの任意の標準出力、未識別の値、パス、ログファイル全体を匿名化する機能ではありません。共有前に確認してください。Windows通知処理へは表示に必要な情報と不透明なローカルリンクを渡し、通知内で承認しません。 [errors.py](../src/migration_harness/errors.py) · [junie_thoughts.py](../src/migration_harness/junie/junie_thoughts.py) · [junie_notifications.py](../src/migration_harness/junie/junie_notifications.py) | [合成値の伏せ字](../tests/test_error_reporting.py)、[Run別の詳細ログ](../tests/test_junie_thoughts.py)、[不透明な通知リンク](../tests/test_junie_notifications.py) |

## 端末外との通信と利用者の判断

- **Junieとの移行・修正・相談:** MoCAは登録された入力、必要に応じて編集対象・指示・関連ルールや診断をJunie CLIに渡します。Junieや選択したモデルのサービスの処理・送信条件は、MoCAのローカルHTTPとは別です。利用するサービスと組織の許可を確認し、外部サービスで扱えない情報を移行資料や指示に含めないでください。MoCAで許可を確認した操作でも、第三者サービスの保存・送信設定を変更するわけではありません。
- **円換算:** Junieの利用量を保存するとき、Runに円換算の記録がなく、過去の応答数もない場合にだけ、Frankfurterの公開レートAPIへ固定のHTTPS GETを送ります（`https://api.frankfurter.dev/v2/rate/usd/jpy`、タイムアウト3秒）。ソース、検索内容、プロンプト、Run ID、利用量、認証情報は、MoCAが組み立てる要求のURL・ヘッダー・本文のいずれにも含めません。IPアドレスなど通信に伴う情報は接続先から見えます。取得に失敗した場合、または既に過去の利用量があるRunに円換算の記録がない場合は、当時の円換算を利用不可として扱い、保存済みの利用量は維持します。専用の許可画面や無効化設定はありません。 [run_records.py](../src/migration_harness/runs/run_records.py) · [通信しない模擬テスト](../tests/test_run_cost.py)
- **エディタの診断:** MoCAのRoslyn/Razor診断はローカルの .NET SDK を使います。この診断で起動するプロセスには .NET CLI のテレメトリーを無効にする環境変数を渡します。今回のコード確認でMoCA専用の診断ログアップロード機能やその許可画面は確認していません。Windows、Junie、通常のビルド、フック、チェッカーの診断設定には適用されません。それらに対する無通信の保証でもありません。

## 既存のACP確認件数を読む

ダッシュボード経由で計測される操作種類と理由の件数は、各Runの `run.json` の `junie_permission_counts` に保存されます。本文・ファイルパス・コマンド引数を件数のキーには使いません。固定資料の提供は、有効化されたRunで応答の送信が完了した場合だけ計上します。累計と、並行処理の影響・設定・Junieバージョンの違いを切り分けるためには、同じ合成入力・モデルで、新しいRunを固定資料の直接読み取りの無効／有効に分けて比べてください。

これは要求の観測件数であり、一般の許可要求を自動承認した履歴でも、削減効果を測った結果でもありません。許可画面の詳細表示と、件数に保存される情報も別です。操作承認、初回フォルダー信頼、停止・取消は従来の手順に従います。実Junieのバージョン・モデル別の実機比較は今回未確認です。 [migration_session.py](../src/migration_harness/junie/migration_session.py) · [acp_session.py](../src/migration_harness/junie/acp_session.py) · [集計と固定資料の模擬回帰](../tests/test_acp_permissions.py)

## 確認の範囲

上の表はコードと回帰の対応です。確認に実データ・実資格情報は不要です。今回の調査では、外部サービスや実Junieへの送信、通知ホスト・Windowsの設定変更、OS境界の検証、すべてのリンク・ファイルシステムの組み合わせは試していません。外部通信の項目はプログラムが組み立てる要求の確認であり、実通信を観測したものではありません。環境固有の利用可否は、所属組織のネットワーク・データ取扱いルールと使用するツールの設定で判断してください。
