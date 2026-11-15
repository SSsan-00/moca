# PHP → C# Migration Harness

PythonとLangGraphで、PHPからASP.NET Core Razor Pages（現状.NET 9）への移行を支援するローカル実行用ハーネスです。コード移行はJunieに依頼し、ハーネスが対象範囲、作業場所、実行記録を管理します。

## 現在の実装

- 移行単位ごとに、入力ファイルと行範囲をTOMLで明示する
- 指定行だけを行番号付きコンテキストにまとめ、設定上限ごとのカプセルに分割する
- アプリ用のGit worktreeを、指定した基準リビジョンから作成する
- `run`では、対象ファイルとカプセルを1つずつ渡し、Junie CLIを順番に起動する
- 各Junie呼び出し後に対象ファイルをworktreeへ反映し、実行記録を保存する
- 全カプセルの反映後にworktreeで `dotnet build` を実行し、結果を `run.json`、出力を `build.log` に保存する
- ビルド成功後に設定した独自チェッカーを実行し、終了コードを `run.json`、標準出力と標準エラーを `checker.log` に保存する
- 有効にした場合は、チェッカーの指摘をJunieに渡し、修正後にビルドとチェッカーを再実行する
- 人が指摘を問題なしと判断した場合は理由を記録し、同じRun IDで確認を続ける
- `resume`では、既存の実行記録・worktree・agent workspaceを使い、未完了カプセルから再開する

テストの実行とDiff Viewer用アンカーJSONは今後の実装です。SQLiteチェックポイントは準備グラフの状態を保存しますが、`resume`は `run.json` と作成済みファイルを使います。

Junieへ渡す情報と作業フォルダはハーネスで絞りますが、別の絶対パスへのアクセスをOSレベルで遮断するものではありません。Junieを使うと、選択したコンテキストがJunieのサービスへ送られます。生成後は`run.json`とworktreeの差分を人が確認してください。

## セットアップ（Windows）

Python 3.11以上、Git、ログイン済みのJunie CLIが必要です。作業フォルダでPowerShellを開き、リポジトリをcloneして仮想環境を作成します。

```powershell
git clone https://github.com/SSsan-00/php-to-csharp-migration-harness.git
Set-Location php-to-csharp-migration-harness
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item harness.example.toml harness.local.toml
```

`pip install -e .`はハーネスのコマンドを仮想環境に登録し、LangGraphなどのPython依存パッケージをインストールします。`-e`はソースをclone先に置いたまま使う開発向け設定で、ハーネスを改造すると次回実行から変更が反映されます。Python、Git、Junie CLI自体はこのコマンドではインストールされません。

`harness.local.toml`を編集して、アプリのGitリポジトリ、基準リビジョン、worktree保存先、実行データ保存先、移行単位と入力範囲を指定します。worktreeと実行データはアプリリポジトリおよびこのハーネスリポジトリの外に置いてください。設定例のパスと行範囲はプレースホルダーです。

入力には、移行元PHP、コメントだけを追加した注釈版PHP、仮C#、移行ルールの該当箇所、必要な既存C#例などを指定できます。各入力はファイルと開始・終了行を明示します。`max_context_chars`の既定値は100,000文字です。これは1回のJunie呼び出しに渡すコンテキストカプセルごとの上限であり、トークン数の厳密な上限ではありません。範囲が上限を超えると現在は行境界で分割し、`run`は順番にJunieへ渡します。関数や制御構文の境界は現在判定しません。1行自体が上限を超える場合は文字位置を記録して分割し、隣接する断片の前後512文字（上限の小さい設定ではその5%）を読み取り用に重ねます。AIには重なり部分を再移行せず、指定されたfocus範囲だけを処理するよう指示します。各入力の文字コードは`encoding`で指定できます。`auto`ではUTF-8 BOMを優先し、BOMなしの場合はUTF-8とEUC-JPを厳密に判定します。両方で異なる文字列として読める曖昧な入力や、どちらでも読めない入力は自動選択せず、`encoding = "utf-8"`または`encoding = "euc-jp"`の指定を求めます。対象ファイル全体や会話履歴・ツール出力は文字数に含みません。

### 大規模ソースの情報欠落を防ぐ設計

目標は、全ソースを一度にJunieへ渡すことではなく、必要な原文を後から取得でき、未処理範囲を見逃さないことです。現行実装は指定行を文字数上限に合わせて分割し、`run`で順番にJunieへ渡します。原文は切り捨てず、`capsule/manifest.json`に各カプセルの文字数、文字コード、元ファイル・行範囲、行内のfocus範囲と重なり範囲を記録します。巨大な1行では前後の文字列を重ねて渡し、AIが境界付近の文脈を参照できるようにします。複数カプセルになった場合は、構文境界を判定していないため、`run.json`の確認フラグに従って分割位置をレビューしてください。関数などの構文境界を安全に判定する機能、移行元の項目と移行先の対応、依存資料、未処理状態を追跡する台帳は今後の設計です。

この台帳、依存資料の追加取得、関数などの論理境界に基づく分割、全範囲の網羅性確認はまだ未実装です。台帳の単位と状態は仕様ページの設計案を参照してください。

## 実行

Windowsでは、リポジトリ内の`h.cmd`を短いランチャーとして使えます。仮想環境の有効化や`--config`指定は不要です。

```powershell
.\h.cmd p example-page
.\h.cmd r example-page
.\h.cmd s
```

`p`はprepare、`r`はrun、`s`はresumeです。resume時にRun IDを省くと入力を求めます。特定のRun IDを指定する場合は`.\h.cmd s <run-id>`とします。通常のCLIコマンドも引き続き使えます。

指定内容の検証、コンテキスト作成、worktree作成だけを行う場合:

```powershell
php-to-csharp-harness prepare --config harness.local.toml --unit example-page
```

Junieを起動して対象ファイルを移行する場合:

```powershell
php-to-csharp-harness run --config harness.local.toml --unit example-page
```

`run`には対話ターミナル、`PATH`上のJunie CLI、移行先に対応する.NET SDKが必要です。Junie用作業フォルダには、設定した対象ファイルとコンテキストを配置します。複数カプセルがある場合は、前の呼び出しで更新したファイルを次の呼び出しへ引き継ぎ、各呼び出し後にworktreeへ反映します。

Junieはカプセルの作業を終えると、作業フォルダに今回の起動専用の完了記録を残します。ハーネスが記録を検知すると対話画面を自動で閉じ、編集をworktreeへ反映して次のカプセルへ進みます。最後のカプセルなら実行を終了します。有効な記録がない状態でJunieを閉じた場合は、部分編集を保存して現在のカプセルを保留します。終了コードは診断用で、自動終了時にも非ゼロになる場合があります。完了記録はJunieの自己申告で、移行結果は最後にworktreeの差分で確認してください。

全カプセルが完了すると、worktreeのルートを作業ディレクトリとしてビルドします。`[build].project` にworktreeからの相対パスで `.csproj`、`.sln`、`.slnx` を指定できます。省略時はルートで `dotnet build` を実行します。

### 独自チェッカーを指定する

ビルド成功後、チェッカーをworktreeのルートで実行します。`--checker-command` はコマンドの最後に置き、実行ファイル名と引数を続けて指定します。たとえば、アプリ側の `tools/check_migration.py` をWindowsで呼ぶ場合は次のようにします。

```powershell
php-to-csharp-harness run --config harness.local.toml --unit example-page --checker-command py -3.11 tools/check_migration.py
```

`.\h.cmd r` や `.\h.cmd s` から使う場合は、`harness.local.toml` に設定します。CLIに指定したコマンドがTOMLより優先されます。指定を省略するとビルドで終了します。

```toml
[checker]
command = ["py", "-3.11", "tools/check_migration.py"]
max_repair_attempts = 2
```

### チェッカーの指摘を自動修正する

`max_repair_attempts` に正の整数を指定すると、1つのRunで実行するJunieの修正回数を制限できます。既定値の `0` では、チェッカーの出力を保存したところで人の確認に回します。

自動修正する場合、チェッカーは指摘があるときに非ゼロで終了し、指摘をUTF-8の `ルールID ファイル:行 warning: メッセージ` または `ルールID ファイル:行 error: メッセージ` で出力します。たとえば `MIG101 Pages/Example.cshtml:12 warning: 共通関数を使用してください` の形式です。既存のチェッカーが違う形式を使う場合は、利用者側のラッパーで整形できます。

Junieは設定済みの移行先ファイルだけを修正します。修正前のチェッカー出力は `checker-before-repair-N.log`、直近の結果は `checker.log`、試行履歴は `run.json` に残ります。過去に修正した指摘が残るか再発した場合は、行番号が変わっていてもルールID・対象ファイル・メッセージで照合し、人の確認に回します。上限、対象外のファイル、解析できない出力、Junieの未完了や変更なしでも、その理由を記録します。

人が確認した後は、同じRun IDで `resume` を実行できます。修正ループに入った実行はビルドからやり直し、過去の修正回数と指摘の履歴を維持します。CLIでのみチェッカーを指定した場合は、`resume` にも `--checker-command` を指定します。

### 指摘を「問題なし」として記録する

`review` で最新の指摘とそのIDを確認します。差分とチェッカーの出力を確認し、偽陽性などと判断した指摘のIDと理由を指定します。

```powershell
$runId = Read-Host "Run ID"
php-to-csharp-harness review --config harness.local.toml --run-id $runId
$findingId = Read-Host "Finding ID"
php-to-csharp-harness review --config harness.local.toml --run-id $runId --accept $findingId --reason "共通関数に委譲していることを確認済み"
php-to-csharp-harness resume --config harness.local.toml --run-id $runId
```

判断は `run.json` に保存します。再開するとビルドとチェッカーを再実行し、同じファイル内容に対する承認済みの指摘だけが残っている場合は `checker_accepted` として完了します。チェッカーが返した終了コードはそのまま残ります。行・列のずれを許容しますが、対象ファイルを編集した場合は再確認が必要です。新しい指摘や解釈できない出力が残る場合は通常の確認フローに戻ります。同じIDの指摘が複数ある場合は、区別できるようにチェッカーのメッセージを整えてください。

### 中断・失敗から再開する

保留した実行や失敗したビルドは、表示されたRun IDを使って `.\h.cmd s` で再開できます。ビルドが失敗した場合は、`build.log` を確認して原因を直し、ビルドから再開します。ログは直近の試行の内容に更新されます。通常のCLIから再開する場合は次のコマンドを使います。

```powershell
$runId = Read-Host "Run ID"
php-to-csharp-harness resume --config harness.local.toml --run-id $runId
```

Junieがステップ上限に達した場合は、同じ対話画面で続きを依頼できます。ハーネスがCtrl+Cを受け取った場合も、部分編集と実行記録を保存します。`resume`は、完了済みのカプセルを飛ばし、新しいJunieセッションに未完了カプセルと編集済みの対象ファイルを渡します。Junieは原文と保存済みの編集を照合して残りの移行を進めます。再開時にworktreeの対象ファイルが記録後に変更されている場合は、上書きせず停止します。

`[junie].max_steps`は任意の正の整数です。未設定の場合は`--max-steps`をJunieに渡しません。これは1つのタスクの手順数上限で、トークン数や料金の上限ではありません。複数カプセルでは呼び出しごとに適用されます。Junieが上限に達したら同じ対話画面で続きを依頼できます。クォータ切れなどJunie側で続行できない場合は、利用枠が戻るまで待つ必要があります。ハーネスは自動再試行せず状態を保存し、利用者が`resume`を実行した時に未完了カプセルから再開します。

実行データとコンテキストには利用者のソースが含まれる場合があります。ローカルで適切に管理してください。設定範囲の指定は、JunieのOSレベルのファイルアクセス制御ではありません。

## 仕様と参考資料

- [仕様・実装状況HTML](migration-harness-spec.html)
- [進捗画面デモ（模擬データ）](progress-demo.html)
- GitHub Pages: <https://sssan-00.github.io/php-to-csharp-migration-harness/>

以下は設計・実装の参考資料です。いずれもこのハーネスの実行時依存として導入せず、必要な考え方を参考にします。

- ハーネスの構成・エージェント運用: [takt](https://github.com/nrslib/takt)、[Codex](https://github.com/openai/codex)、[ECC](https://github.com/affaan-m/ECC)
- YAGNI・小さな変更・実装レビュー: [Ponytail](https://github.com/dietrichgebert/ponytail)
- 大規模コードの関係索引・ナレッジグラフ: [Graphify](https://github.com/Graphify-Labs/graphify)
- 実行結果に基づくスキル・ルール改善の評価方法: [Microsoft SkillOpt](https://github.com/microsoft/skillopt)
- 差分レビューとアンカー設定: [diff-viewer](https://github.com/SSsan-00/diff-viewer)

## ライセンス

このリポジトリの内容には[MIT License](LICENSE)を適用します。社内で使う場合、変更内容を公開する義務はありません。ソフトウェアを複製して社内配布する場合は、コピーに著作権表示とMITライセンス本文を含めてください。

MITライセンスは利用者が用意するPHP/C#ソース、移行ルール、社内スクリプトには適用されません。
