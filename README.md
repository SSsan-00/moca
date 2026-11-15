# PHP → C# Migration Harness

Pythonで、PHPからASP.NET Core Razor Pages（現状.NET 9）への移行を支援するローカル実行用ハーネスです。コード移行はJunieに依頼し、ハーネスが対象範囲、作業場所、実行記録を管理します。

Windowsで実案件を試す手順は、[Windowsユーザーマニュアル](docs/windows-user-guide.md)を参照してください。

## 現在の実装

- 移行単位ごとに、移行元PHPと移行先をTOMLで指定する
- 移行元を行番号付きコンテキストにまとめ、設定上限ごとのカプセルに分割する
- アプリ用のGit worktreeを、指定した基準リビジョンから作成する
- `run`では、設定した移行先ファイルと、その回のカプセルを渡し、Junie CLIを順番に起動する
- 各Junie呼び出し後に編集を許可した移行先をworktreeへ反映し、実行記録を保存する
- 全カプセルの反映後にworktreeで `dotnet build` を実行し、結果を `run.json`、出力を `build.log` に保存する
- ビルド成功後に設定した独自チェッカーを実行し、終了コードを `run.json`、標準出力と標準エラーを `checker.log` に保存する
- 移行先に限定できるビルドエラーや対応形式のチェッカー指摘を、設定した回数内でJunieに渡し、修正後に再検証する
- 人が指摘を問題なしと判断した場合は理由を記録し、同じRun IDで確認を続ける
- `resume`では、既存の実行記録・worktree・agent workspaceを使い、未完了カプセルから再開する
- JunieにPHPとC#の対応行を提案させ、アンカー付きのDiff ViewerワークスペースJSONを保存する

アンカーの座標は保存前に検証します。PHPとC#の意味上の対応はDiff Viewerで確認できます。`resume` は `run.json`、保存済みのカプセル、作業ツリーを使います。

Junieへ渡す情報と作業フォルダはハーネスで絞りますが、別の絶対パスへのアクセスをOSレベルで遮断するものではありません。Junieを使うと、選択したコンテキストがJunieのサービスへ送られます。生成後は`run.json`とworktreeの差分を人が確認してください。

## ハーネスの回帰テスト

ハーネスのリポジトリで、Python 3.11以上とGitが利用できる環境から実行します。

```console
python -m pip install -e .
python -m unittest discover -s tests -v
```

テストは一時ディレクトリにGitリポジトリとRunを作ります。準備処理はPythonとGitで実行し、制御のテストではJunieや.NETなどをテスト用の処理に置き換えます。GitHub ActionsはmainへのpushとPRで、WindowsとLinuxのPython 3.11・3.13を検証します。

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

`pip install -e .`はハーネスのコマンドを仮想環境に登録し、Pythonの依存パッケージをインストールします。`-e`はソースをclone先に置いたまま使う開発向け設定で、ハーネスを改造すると次回実行から変更が反映されます。Python、Git、Junie CLI自体はこのコマンドではインストールされません。

`harness.local.toml`を編集して、アプリのGitリポジトリ、基準リビジョン、worktree保存先、実行データ保存先、移行単位を指定します。worktreeと実行データはアプリリポジトリおよびこのハーネスリポジトリの外に置いてください。設定例のパスはプレースホルダーです。

たとえば、メニュー機能を移行する単位は次のように設定します。

```toml
[[units]]
id = "メニュー"
source = "php/menu.php"
targets = ["Pages/Menu.cshtml.cs", "Pages/Menu.cshtml"]
```

`id` は設定ファイル内で一意の機能名です。`"メニュー"` や `"menu"` のように、日本語でも英数字でも指定できます。`source` にはアプリリポジトリからの相対パスを指定します。ハーネスはPHPの全文を固定し、1行目からファイル末尾までをJunieに渡します。移行先が1ファイルなら `target`、画面とPageModelを一緒に編集するなら `targets` を使います。同じRun IDで再開するときは、Run作成時と同じ移行先を指定します。チェッカーの指摘はファイルごとに追跡します。

反映中に停止した場合は、内容のハッシュを照合して同じRun IDから残りを反映します。移行先や保存済みのJunie出力に外部編集があった場合は、該当ファイルを照合してから再開してください。全カプセルの反映後にビルドします。

`[[units.inputs]]` では、注釈版PHPや仮C#などのファイルと開始・終了行を指定できます。参照資料として `source` に追加することも、従来どおり `[[units.inputs]]` だけで移行範囲を指定することもできます。`max_context_chars`の既定値は100,000文字です。これは1回のJunie呼び出しに渡すコンテキストカプセルごとの上限であり、トークン数の厳密な上限ではありません。範囲が上限を超えると現在は行境界で分割し、`run`は順番にJunieへ渡します。関数や制御構文の境界は現在判定しません。1行自体が上限を超える場合は文字位置を記録して分割し、隣接する断片の前後512文字（上限の小さい設定ではその5%）を読み取り用に重ねます。AIには重なり部分を再移行せず、指定されたfocus範囲だけを処理するよう指示します。文字コードを明示する場合は、`source` に対しては同じ `[[units]]` に `source_encoding`、`[[units.inputs]]` に対しては `encoding` を指定します。`auto`ではUTF-8 BOMを優先し、BOMなしの場合はUTF-8とEUC-JPを厳密に判定します。両方で異なる文字列として読める曖昧な入力や、どちらでも読めない入力は自動選択せず、`"utf-8"` や `"euc-jp"` の明示指定を求めます。対象ファイル全体や会話履歴・ツール出力は文字数に含みません。

プロジェクト共通のルールは `[[project.migration_rules]]`、参考実装は `[[project.references]]` にファイルごとに指定します。ルールは全カプセルに含め、参考実装は行番号付きでRunに別途保存します。Junieは目次から対応するPHPとC#／Razorを探し、関連する範囲を読みます。行範囲を省略するとファイル全体が参照可能になります。修正や再開にも準備時の資料を使用します。移行単位だけで使う他の参照入力は `[[units.inputs]]` に `shared = true` を指定できます。ルールを複数指定する例と、移行済みソースを元PHPと組にして指定する例は、[Windowsユーザーマニュアル](docs/windows-user-guide.md#3-移行ルール参考実装独自チェッカーを追加する)を参照してください。準備時に共通入力と見出し分の文字数を確保し、残りに移行対象を詰めます。共通入力だけで上限を超える場合や対象を入れる余裕がない場合は、準備時に理由を表示します。再開時は保存済みのカプセルを使います。

原文の改行（LF、CRLF、CR）は抜粋にも保持します。生成するMarkdownの区切りはLFで、manifestの文字数は保存するUTF-8本文を文字単位で数えます。

繰り返し呼び出しや長い定数表を移行するときは、Junieが担当するPHPの行から値と並び順をコマンドで抽出し、移行先のデータを展開した結果と照合するよう指示します。チェッカーがこの種の不一致を報告したときは、修正にも保存済みのカプセルを使い、指摘箇所の前から続く処理を順に照合します。

### 大規模ソースの情報欠落を防ぐ設計

移行対象を文字数上限に合わせて分割し、`run`で順番にJunieへ渡します。`capsule/manifest.json`に各カプセルの文字数、文字コード、元ファイル・行範囲、行内のfocus範囲と重なり範囲を記録します。巨大な1行では前後の文字列を重ねて渡し、Junieが境界付近の文脈を参照できるようにします。同じ入力のカプセル間では前後最大12行ずつを文字数を制限した読み取り用抜粋として渡します。Junieは移行先にある前回までの編集と照合し、今回の担当範囲を処理します。複数カプセルになった場合は、`run.json`の確認フラグに従って分割位置をレビューしてください。

ダッシュボードの **移行台帳** では、Run IDごとに、カプセルに含まれるPHPの行範囲、設定した移行先ファイル、処理状態を確認できます。範囲は `capsule/manifest.json`、完了数と処理中のカプセルは `run.json` に保存され、同じRun IDで再開した後も確認できます。**処理済み** はJunieの完了記録が反映された状態です。処理の対応関係はDiff Viewerでレビューします。

準備時に参照PHPの原文をRun配下の `capsule/php-sources/` に保存します。Diff Viewerとアンカー生成は、このコピーをmanifestの文字コードとSHA-256で照合して使います。この仕組みを導入する前のRunでは、manifestのパスと文字コードで現在のプロジェクトから読みます。移行時点の原文を確認するには、元の版でRunを作り直してください。

ダッシュボード上部の **比較ツールを開く** メニューから **Junieで対応行を提案して開く** を選ぶと、ハーネスが処理済みのPHP範囲に対応する候補をアンカーから割り出し、同じRun IDの `run.json` に自動で保存します。候補のファイルと行範囲を確認し、**候補をDiff Viewerで開く** から元のスナップショットを見直せます。

候補を絞れない範囲は **要確認**、候補の保存後に移行先が変わった範囲は **要再確認** と表示します。以前に記録した対応は、移行先の内容が一致していれば **人の確認済み** と表示します。進捗デモでも、同じ操作で模擬データを確認できます。

入力設定に `symbol = "get_status_label"` を指定すると、台帳に元のシンボル名を表示します。移行台帳では記録済みの判断と理由を確認でき、**差分を開く** で移行先を見直せます。

CLIを使う場合は、台帳に表示した範囲IDを指定します。

```powershell
php-to-csharp-harness ledger-review --config harness.local.toml --run-id RUN_ID --range 1:1 --range 2:1 --state excluded --reason "旧画面でのみ使用"
```

`--state` は `confirmed`（確認済み）、`excluded`（理由付きの対象外）、`needs_review`（要確認）から選びます。現在の対応候補を確認した場合はそのまま `confirmed` にできます。候補を作れない範囲や複数ファイルにまたがる範囲では、Diff Viewerで確認した移行先を `--target` で指定します。複数の範囲をまとめて指定すると、同じ移行先で確認を記録します。

```powershell
php-to-csharp-harness ledger-review --config harness.local.toml --run-id RUN_ID --range 1:1 --state confirmed --target Pages/Menu.cshtml.cs --target Pages/Menu.cshtml --reason "原文と画面・PageModelの対応を確認"
```

**移行完了** になるのは、台帳の全PHP範囲が確認済みか理由付きの対象外になり、現在の移行先に対してビルドと設定済みのチェッカーが成功しているときです。チェッカーの指摘を理由付きで承認し、再実行でも承認済みの指摘だけが残った場合も条件を満たします。確認した移行先が変わった範囲は、理由を残して **要再確認** に戻ります。対象外の判断は保持します。

移行先が変わったら、差分を確認してCLIで判断を登録し、ダッシュボードの **ビルド・チェックを再実行** を選びます。CLIからは同じRun IDで `resume` できます。検証時のハッシュが記録されていない旧Runでも、同じ操作で現在の内容を検証できます。

関数などの論理境界に基づく分割、依存資料の追加取得、対応の自動検証は今後の設計です。

## 実行

### ダッシュボードから管理する

WindowsのPowerShellで `.\h.cmd d` を実行し、端末に表示されたローカルURLを開きます。通常のCLIでは `php-to-csharp-harness dashboard --config harness.local.toml` を使います。

- **設定** のフォームでプロジェクト、移行単位、移行ルール、参考実装のペア、自作スクリプト、ビルド対象、独自チェッカーを編集して保存します。リポジトリと保存先、ビルド対象、チェッカー、基準リビジョン、Junieの上限、差分ビューアーは **プロジェクトの詳細設定** にあります。初回は設定例を表示し、内容を検証してから保存します。
- **実行** で移行単位を選んで準備や移行を開始します。保存済みのRunは **移行を再開** または **進捗を表示** で操作します。
- **進捗** でログ、差分、移行台帳を確認し、必要に応じて同じRunを再開します。Junieが対話を求めたときは、ダッシュボードを起動した端末で応答します。

進捗デモでも3つのタブと台帳を試せます。デモでの設定編集はページ内の模擬データに反映されます。

### コマンドから実行する

Windowsでは、リポジトリ内の`h.cmd`を短いランチャーとして使えます。仮想環境の有効化や`--config`指定は不要です。

```powershell
.\h.cmd p "メニュー"
.\h.cmd r "メニュー"
.\h.cmd s
```

`p`はprepare、`r`はrun、`s`はresumeです。resume時にRun IDを省くと入力を求めます。特定のRun IDを指定する場合は`.\h.cmd s <run-id>`とします。通常のCLIコマンドも引き続き使えます。

指定内容の検証、コンテキスト作成、worktree作成だけを行う場合:

```powershell
php-to-csharp-harness prepare --config harness.local.toml --unit "メニュー"
```

Junieを起動して対象ファイルを移行する場合:

```powershell
php-to-csharp-harness run --config harness.local.toml --unit "メニュー"
```

`run`には対話ターミナル、`PATH`上のJunie CLI、移行先に対応する.NET SDKが必要です。Junie用作業フォルダには、設定した対象ファイルとコンテキストを配置します。複数カプセルがある場合は、前の呼び出しで更新したファイルを次の呼び出しへ引き継ぎ、各呼び出し後にworktreeへ反映します。

Junieはカプセルの作業を終えると、作業フォルダに今回の起動専用の完了記録を残します。ハーネスが記録を検知すると対話画面を自動で閉じ、編集をworktreeへ反映して次のカプセルへ進みます。最後のカプセルなら実行を終了します。有効な記録がない状態でJunieを閉じた場合は、部分編集を保存して現在のカプセルを保留します。終了コードは診断用で、自動終了時にも非ゼロになる場合があります。完了記録はJunieの自己申告で、移行結果は最後にworktreeの差分で確認してください。

全カプセルが完了すると、worktreeのルートを作業ディレクトリとしてビルドします。`[build].project` にworktreeからの相対パスで `.csproj`、`.sln`、`.slnx` を指定できます。省略時はルートで `dotnet build` を実行します。

### 独自チェッカーを指定する

ビルド成功後、チェッカーをworktreeのルートで実行します。`--checker-command` はコマンドの最後に置き、実行ファイル名と引数を続けて指定します。たとえば、アプリ側の `tools/check_migration.py` をWindowsで呼ぶ場合は次のようにします。

```powershell
php-to-csharp-harness run --config harness.local.toml --unit "メニュー" --checker-command py -3.11 tools/check_migration.py
```

`.\h.cmd r` や `.\h.cmd s` から使う場合は、`harness.local.toml` に設定します。CLIに指定したコマンドがTOMLより優先されます。指定を省略するとビルドで終了します。

```toml
[checker]
command = ["py", "-3.11", "tools/check_migration.py"]
max_repair_attempts = 2
```

### ビルドエラーとチェッカーの指摘を自動修正する

`max_repair_attempts` は、1つのRunで実行するJunieのビルド・チェッカー修正の合計上限です。既定値は `2` で、`0` は自動修正を無効にします。ビルドでは、移行先を指すC#・Razorのコンパイラエラーを修正し、再ビルドします。修正前のログは `build-before-repair-N.log` に保存します。同じ指摘が残る場合、移行先外の指摘、依存関係などのエラーは人が確認します。

自動修正する場合、チェッカーは指摘があるときに非ゼロで終了し、指摘をUTF-8の `ルールID ファイル:行 warning: メッセージ` または `ルールID ファイル:行 error: メッセージ` で出力します。たとえば `MIG101 Pages/Example.cshtml:12 warning: 共通関数を使用してください` の形式です。既存のチェッカーが違う形式を使う場合は、利用者側のラッパーで整形できます。

自動修正の前に、移行単位で指定した全ファイルを最新のworktreeからJunie用フォルダへ同期します。Junieが編集できるのは指摘のあったファイルです。その他の移行先は参照資料として渡します。修正前のチェッカー出力は `checker-before-repair-N.log`、直近の結果は `checker.log`、試行履歴は `run.json` に残ります。過去に修正した指摘が残るか再発した場合は、行番号が変わっていてもルールID・対象ファイル・メッセージで照合し、人の確認に回します。上限、対象外のファイル、解析できない出力、Junieの未完了や変更なしでも、その理由を記録します。

人が確認した後は、同じRun IDで `resume` を実行できます。チェッカーの段階から再開する場合も先にビルドし、現在のビルドが成功してからチェッカーを実行します。修正回数と指摘の履歴は維持します。CLIでのみチェッカーを指定した場合は、`resume` にも `--checker-command` を指定します。

解釈できない行が混在する場合は、修正回数を使わず人の確認に回し、元の `checker.log` を残します。ログが65,536バイトを超える場合やJunieに渡す指摘が8,000文字を超える場合も自動修正を保留します。手動レビューではこれらの上限に関わらず、読み取れた指摘を一覧できます。

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

### ブラウザーで進捗と指摘を確認する

```powershell
php-to-csharp-harness run --config harness.local.toml --unit "メニュー" --dashboard
```

ターミナルに表示された `http://127.0.0.1:...` のURLを開きます。完了後を含む保存済みの実行は、次のコマンドで開きます。Windowsでは `.\h.cmd v` も使えます。

```powershell
php-to-csharp-harness view --config harness.local.toml --run-id RUN_ID
```

`view` と `resume --dashboard` は保存された結果を表示します。再開するときは画面の **同じ実行を再開** を選びます。`--checker-command` を指定するときは、コマンドと引数を末尾に置きます。

人の確認待ちになるとモーダルが開きます。指摘を選んで判断理由を入力し、**問題なしとして記録**を押します。必要な指摘を記録したら、**同じ実行を再開**を押します。Junieが必要な場合は起動元のターミナルで対話できます。

画面はPython標準ライブラリのHTTPサーバーで配信し、進捗の受信にはSSEを使います。URLは起動した端末で利用できます。ブラウザーが再接続すると、同じサーバーの直近100件の範囲で未受信のイベントを再送します。実行結果と判断理由は `run.json` に残ります。画面を終了するときは、ハーネスのターミナルで Ctrl+C を押します。

指摘が残ったら、**判断が必要です** または指摘の行を押すと確認モーダルが開きます。**判断待ち** で未判断の指摘だけを表示できます。チェッカーの出力から取得できたファイル名と行番号は、モーダルの **指摘箇所** に表示されます。

### Diff Viewerで差分を確認する

ダッシュボードでDiff Viewerを使う場合は、移行プロジェクトに単一HTMLを置き、**プロジェクトの詳細設定** の **差分ビューアーのHTML** に相対パスを指定します。たとえば `tools/dist/index.min.html` を指定します。HTMLの空欄や **使用しない** の選択で比較とアンカー生成が無効になり、移行と検証は実行できます。

worktreeと対象ファイルがそろったら、進捗ダッシュボード上部の **比較ツールを開く** をいつでも押せます。確認待ちのモーダルからも同じ操作ができます。左ペインにはこのRunの入力に指定したPHPファイル、右ペインにはworktreeの移行先を表示します。Razorページは `.cshtml.cs`、`.cshtml` の順です。

確認モーダルから開くと、選択中の指摘の報告行を右ペインに表示します。判断待ちのときにダッシュボード上部から開くと、最初の未判断の指摘を表示します。行番号が取れない場合や、報告行が比較対象の範囲外の場合は通常の位置で開きます。

**比較ツールを開く** を押すと、Diff Viewerが別タブで開き、取得時点のワークスペースJSONが自動でダウンロードされます。別タブを開けない場合は、ダッシュボード内のダイアログで比較できます。同じJSONを実行ディレクトリの `review-snapshots/<ID>.json` に保存します。実行終了後も、保存済みJSONをローカルのDiff Viewerでインポートして確認できます。スナップショットにはソース全文が含まれ、Viewerで扱うのはそのコピーです。

公開の進捗デモでは、同じ画面内で模擬データの比較とJSONのダウンロードを試せます。「人の確認待ち」になったら、画面上部と確認モーダルのどちらから開いても右ペインに指摘行を表示します。デモHTMLをブラウザーで直接開いた場合は、ダウンロードされたJSONをViewerの **インポート** から読み込みます。

#### Razorページに複数の指摘がある例

サンプルのチェッカーが次の順で指摘を出した場合、比較ツールは操作に応じて右ペインの表示位置を選びます。行番号は各ファイル内の番号です。

```text
MIG401 Pages/Review.cshtml.cs:5 warning: Confirm PageModel return value.
MIG402 Pages/Review.cshtml:3 warning: Confirm rendered result.
```

| 操作 | 右ペインの表示位置 |
| --- | --- |
| 2件とも未判断の状態で、画面上部から開く | `Pages/Review.cshtml.cs` の5行目 |
| 確認モーダルで `MIG402` を選んで開く | `Pages/Review.cshtml` の3行目 |
| 確認モーダルで `MIG401` を選んで開く | `Pages/Review.cshtml.cs` の5行目 |
| `MIG401` を **問題なしとして記録** し、画面上部から開く | `Pages/Review.cshtml` の3行目 |

### Junieでアンカー付きの比較JSONを作る

差分ビューアーを有効にして、ダッシュボード上部または指摘の確認モーダルで **比較ツールを開く** の右にある `▾` から **Junieで対応行を提案して開く** を選びます。今回の比較と、同じRunに保存されている確認済みの対応例を使います。Junieが確認を求めたら、ハーネスを起動した端末で操作します。生成が完了すると、別タブのDiff Viewerに結果が表示され、同じJSONがダウンロードされます。

コマンドから生成する場合はRun IDを指定します。手動アンカー付きのワークスペースJSONを明示して使う場合は、任意の `--reference` を追加します。

```powershell
php-to-csharp-harness anchors --config harness.local.toml --run-id $runId --reference .\previous.workspace.json
```

Junieは対応行の例と今回のスナップショットのコピーを読み、行の対応を提案します。ハーネスは行番号の範囲・重複・順序を検証し、アンカー付きのワークスペースJSONを `review-snapshots` に新規保存します。元のソース、参照JSON、既存のスナップショットはそのままです。

コマンドが表示するJSONをDiff Viewerのワークスペース **インポート** から開き、提案された対応行を確認・調整します。

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
