# CodexでIssueを1件ずつ処理する

`Codex issue` は、`main` の手動実行で未解決Issueを1件受け取り、Codexの修正案をDraft PRとして公開します。GitHub上でCI、Codexレビュー、担当者の承認がそろうと、その実行が候補PRをsquash mergeします。導入用PRはこのフローの対象外です。

## 所有者が行う初期設定

1. 対象リポジトリだけに専用の非公開GitHub Appをインストールします。Repository permissionsは **Contents: Read and write**、**Pull requests: Read and write**、自動付与される **Metadata: Read** とします。管理、Workflows、Checks、Issuesの書き込み権限や、ルールのバイパス権限は付けません。
2. GitHubの **Settings → Environments** で `codex-issue-write` を作り、デプロイ可能なブランチを `main` のみに限定します。Appの秘密鍵はこの環境のsecret `CODEX_ISSUE_APP_PRIVATE_KEY` に、Client IDは環境のvariable `CODEX_ISSUE_APP_CLIENT_ID` に登録します。登録済みのリポジトリsecret `OPENAI_API_KEY` はパッチ生成ジョブのCodex Actionにだけ渡します。
3. `main` に適用される **Active** なブランチrulesetを作成し、次の条件を設定します。バイパスリストは空のままにし、squash mergeを許可します。このワークフローはGitHubの **Allow auto-merge** 設定を使わず、条件がそろった時点でマージを要求します。
   - **Require a pull request before merging**。承認1件以上、新しいコミットで古い承認を取り消す設定、およびすべての会話の解決を必須にします。
   - **Require status checks to pass** と **Require branches to be up to date before merging**。次の5チェックすべての提供元を **GitHub Actions** に指定します。選択肢に出ない場合は最近成功した `Harness tests` の実行を確認します。
     - `regression (ubuntu-latest, 3.11)`
     - `regression (ubuntu-latest, 3.13)`
     - `regression (windows-latest, 3.11)`
     - `regression (windows-latest, 3.13)`
     - `windows-junie`
   - このフローではmerge queueは使えません。追加の保護ルールを設定した場合も、マージはGitHubの判定に従います。
4. Actionsのリポジトリvariable `CODEX_ISSUE_DISPATCHERS` に手動起動を許可する担当者、`CODEX_ISSUE_REVIEWERS` にPR承認を許可する担当者のGitHubログインを、それぞれカンマ区切りで設定します。リポジトリの書き込み権限と手動起動できる利用者を絞り、GitHub Appの設定とrulesetを管理できる人も限定します。
5. Codex設定でこのリポジトリを接続し、**Review code → Automatic review** を有効にします。有効になるレビューの種類と、担当者が手動でレビューを依頼できることを確認します。

標準の `GITHUB_TOKEN` で作成したPRでは、PR起点のActionsが承認待ちになることがあります。専用Appの短命なインストールトークンでブランチをpushし、Draft PRを作成します。最初の許可済み試行では、候補のpushとPRの両方から既存の `Harness tests` が実際に起動し、同じ候補SHAで必須5チェックとWindowsの分割ジョブ4件（各Pythonの `conversation-editor` と `remaining`）がすべて成功することを確認してください。設定前に本番の生成やマージを試す必要はありません。 [GitHubのトークンと起動イベント](https://docs.github.com/en/actions/concepts/security/github_token) · [GitHub Appのトークン発行](https://github.com/actions/create-github-app-token) · [GitHub ruleset](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)

## 手動実行と承認

1. **Actions → Codex issue → Run workflow** で `main` を選択し、Issue番号を半角数字で1つ入力します。作成したDraft PRへのリンクが実行サマリーに表示されます。修正案が出ない場合は `proposal.json` の調査結果と残作業を確認します。
2. PRの **Files changed** と実行アーティファクトの `proposal.json` を確認します。調査内容とテスト計画はCodexの提案です。テストの実績は **Checks** とActionsのpush実行を確認してください。差分にテストの無効化、検証結果の偽装、予想外の処理や外部通信、秘密情報へのアクセスがあれば承認せず、PRをDraftのままにするか閉じます。
3. 適切な候補を **Ready for review** にします。自動レビューが始まらない場合は、接続済みの担当者がPRに `@codex review` とコメントします。PRでCodexのレビューが終了し、対象の最終コミットが一致することを確認します。未着・失敗・未解決の重大な指摘があれば承認しません。Codexが指摘しなかった場合も、実装の完全性は担当者が確認します。 [CodexのGitHubレビュー](https://developers.openai.com/codex/integrations/github/)
4. Issueの完了条件、差分、CI、Codexレビューを確認した担当者は、**Files changed → Review changes → Approve** から次の2行を承認本文に記入します。実Junieが必要な場合は、次節の検証を行い `not-required` を `passed` に置き換えます。未完了、未検証、判断不能の場合はこの承認を出しません。

   ```text
   Codex-Issue: verified
   Junie: not-required
   ```

5. マージ結果はActionsのサマリーに表示されます。ワークフローはIssueへの書き込み権限を持ちません。マージ後、担当者がマージされた変更とIssue全体の完了を確認できた場合に、結果をコメントしてIssueを閉じます。部分対応、失敗、未検証のIssueは開いたままにします。

## 実Junieが必要なIssue

Windowsの `windows-junie` はCLIの起動スモークです。移行や停止・再開を伴うIssueでは、担当者が差分を確認してから専用の認証済み環境でPRの最終SHAをチェックアウトし、[Windowsユーザーガイド](windows-user-guide.md)に従って対象操作を再現します。検証用のリポジトリとデータを使い、Junie・.NETなどのバージョン、検証SHA、実行手順、観測した結果をPRに記録します。停止・再開を要する場合は、停止後に対象Runを再開し、期待した処理と記録を確認します。認証情報や機密ログは添付しません。認証済み環境が使えない場合は承認を保留します。

## 停止時の確認

- 同じIssueの同時実行は直列化されます。先行実行のPRやブランチが残っている場合は生成を止めます。pushだけ成功した場合もブランチが残ります。別の手動PRによる対応状況も起動前に確認してください。
- CIが欠落・失敗・キャンセルした場合、またはマトリクスの一部だけが再実行された場合は停止します。必要に応じて対象SHAの **Re-run all jobs** を実行し、元のCodex実行で **Re-run failed jobs** を選びます。CI、レビューと承認の待機は最大55分です。前回の実行に必要な出力が残っている場合、失敗したジョブから再開できます。
- CodexのGitHubレビューは、既知のBotが投稿した `codex-review-state:v1` サマリーで、コードレビューの完全なSHAが候補の最終SHAと一致し、完了かつ指摘なし・未解決なしの場合に受理します。従来の定型コメントも、最終SHAと一致する10桁以上の識別子があれば受理します。未知の形式、不正な状態、別SHA、レビュー未完了・指摘ありの場合は承認へ進みません。Actionsの `verify` ログとPR上のレビュー結果を確認してください。
- `main`、Issue本文・コメント、候補SHA、PR本文が変わった場合や、マージ競合がある場合は自動処理を続けません。元のPRとブランチを確認し、再生成する場合は担当者が候補PRを閉じて、不要になった候補ブランチを削除します。最新 `main` から新規実行し、新しいSHAで全条件を満たしてください。
- マージ直前に条件を再取得し、GitHubのマージAPIに期待するhead SHAを渡します。並行する `main` 更新はGitHubの厳格な必須チェックで止まります。Issueコメントや外部レビューはGitHubのマージトランザクションと一体で更新できないため、マージ後に担当者が改めて完了を確認します。ルールや担当者の設定を自動変更しないでください。 [チェックと最終コミット](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/troubleshooting-required-status-checks) · [マージAPI](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)

## 信頼境界と費用

Issue・コメント・コードは未信頼データです。CodexはGitHub書き込み権限なしで、公式Actionの `drop-sudo`、読み取り専用の権限プロファイル、APIキープロキシを使います。Codexは差分をテキストで提案し、生成コードの実行はAPIキーのない既存CIに任せます。公開側は固定された `main` のコードを使い、実行内の指定アーティファクトだけを取り込み、SHA-256を照合して、パッチをGitインデックスに適用します。差分のパス、モード、文字コードを検査し、ワークフロー、マージ条件、パッケージ設定、スモークスクリプトへの変更を拒否します。公開・マージ側は候補コードを実行せず、checkoutに認証情報を残しません。アプリやテスト内に隠れた検証回避を静的なパス検査だけで判定することはできないため、担当者の差分確認も必須です。 [Codex GitHub Action](https://developers.openai.com/codex/github-action/) · [Codexの非対話実行](https://developers.openai.com/codex/noninteractive/)

Issue本文・コメント・調査対象コードがOpenAI APIに送られます。公開リポジトリのActionsログ、PR、アーティファクトへ出せる情報だけを扱ってください。手動実行や再実行にOpenAI API料金が発生し、CIやレビュー待機を含むジョブがGitHub Actionsの利用枠を消費します。実Junie検証には別途Junieの利用枠や費用が関係する場合があります。
