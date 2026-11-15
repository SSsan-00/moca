# WindowsでPHPからC#へ移行する手順

Windowsのダッシュボードから、実アプリのPHPをC#やRazorへ移行します。この手順では、メニュー機能のPHP全体を1つの移行単位にし、既存の移行ルールと独自チェッカーを使います。

## 1. ダッシュボードを起動する

Windows TerminalなどでPowerShellを開き、`py -3.11 --version`、`git --version`、`junie --version`、`dotnet --list-sdks` を実行します。この手順ではPython 3.11を使います。.NET SDKは移行先プロジェクトに合わせます。Junieは、同じWindowsユーザーでCLIからログインできる状態にします。ログインが必要ならPowerShellで `junie` を起動し、画面の案内に従います。 [Junie CLIのセットアップ](https://junie.jetbrains.com/docs/junie-cli.html)

初回は作業フォルダで次を実行します。

```powershell
git clone https://github.com/SSsan-00/php-to-csharp-migration-harness.git
Set-Location .\php-to-csharp-migration-harness
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\h.cmd d
```

次回以降はハーネスのフォルダで `.\h.cmd d` を実行します。PowerShell側に表示された `http://127.0.0.1:.../` のURL全体をブラウザーで開きます。ブラウザーで操作している間も、起動したPowerShellは開いたままにします。Junieの入力や確認はそこで行います。

## 2. 移行元と移行先を設定する

ダッシュボードの **設定** タブを開きます。初回は設定例が表示されます。

| フォームの項目 | 入力例と役割 |
| --- | --- |
| アプリのリポジトリ | `C:/work/LegacyApp`。PHP、C#のビルド対象、参照資料を置くGitリポジトリのルートです。 |
| 作業ツリーの保存先 | `C:/migration/worktrees`。Runごとに移行先の作業コピーを作ります。 |
| 実行データの保存先 | `C:/migration/runs`。Run IDごとのカプセル、台帳、実行ログが入ります。作業ツリーとは別のフォルダを使います。 |
| 機能名（ID） | `メニュー` や `menu` など、設定内で一意の名前です。 |
| 移行元PHP | `legacy/menu.php`。アプリのリポジトリからの相対パスです。ファイル全体が移行範囲になります。 |
| 移行先（1行に1ファイル） | `src/Web/Pages/Menu.cshtml.cs` と `src/Web/Pages/Menu.cshtml` を別々の行に入力します。Junieが編集するファイルをここで指定します。 |

保存先の2つのフォルダは、アプリとハーネスの両リポジトリの外に置き、互いに入れ子にせず指定します。新しい機能は **移行単位を追加** から入力できます。既存の移行単位は **編集する移行単位** で選びます。

左側の **プロジェクトの詳細設定** を開くと、リポジトリ、保存先、ビルドとチェッカーの設定を編集できます。作業ツリーは既定でアプリの現在の `HEAD` から作ります。必要に応じて **作業ツリーの基準リビジョン** にブランチ名、タグ名、コミットIDを指定します。同じ欄でカプセルの文字数上限、Junieのステップ数上限、差分ビューアーも設定できます。

PHP原文と参照資料は準備時点のアプリフォルダから読み取ります。ビルド対象やチェッカースクリプト、既存の移行先ファイルは基準コミットに含めておきます。移行先には新規ファイルも指定できます。

## 3. 移行ルール、参考実装、独自チェッカーを追加する

プロジェクト固有のルールは、アプリ側のMarkdownファイルに保存します。たとえば共通メソッドの使い方を `docs/migration/common.md`、画面の表示条件やRazorの規約を `docs/migration/razor.md` に書きます。

**設定** の **プロジェクト共通の移行ルール** で **移行ルールを追加** を押して **ルールファイル** に相対パスを入力します。複数ある場合はファイルごとに追加します。不要な項目は、そのルールの **×** で設定から外せます。入力を確認し、**設定を保存** を押します。

**設定** 左側の **プロジェクトの詳細設定** を開き、**ビルド・独自チェッカー** を入力します。

| 項目 | 入力方法 |
| --- | --- |
| ビルド対象 | `src/Web/Web.csproj` など、作業ツリーからの相対パスを指定します。空欄なら作業ツリーのルートでビルドします。 |
| チェッカーの実行ファイルと引数 | 1行目から順に `py`、`-3.11`、`-X`、`utf8`、`tools/check_migration.py` を入力します。空欄ならチェッカーは実行しません。 |
| 自動修正回数 | 1つのRunでJunieが修正を試みる上限です。空欄は2回、`0` は自動修正なしです。 |

**設定を保存** を押すと、フォームで入力した内容が次の形式で保存されます。

```toml
[project]
root = "C:/work/LegacyApp"
worktree_root = "C:/migration/worktrees"
run_root = "C:/migration/runs"

[[project.migration_rules]]
path = "docs/migration/common.md"

[[project.migration_rules]]
path = "docs/migration/razor.md"

[build]
project = "src/Web/Web.csproj"

[checker]
command = ["py", "-3.11", "-X", "utf8", "tools/check_migration.py"]
max_repair_attempts = 2

[[units]]
id = "メニュー"
source = "legacy/menu.php"
targets = ["src/Web/Pages/Menu.cshtml.cs", "src/Web/Pages/Menu.cshtml"]
```

- `[[project.migration_rules]]` は、Junieがすべての移行単位の全カプセルで参照する規約です。例のように行範囲を省略すると、各ルールファイルの全文が渡ります。
- `[build].project` は作業ツリーのルートからの相対パスです。`.csproj`、`.sln`、`.slnx` を使えます。省略時はルートで `dotnet build` を実行します。
- `[checker].command` はアプリ全体の設定です。実行ファイルと引数を配列の別々の要素にします。ビルド成功後に作業ツリーのルートで実行されるため、スクリプトが依存するファイルやランタイムもその環境から利用できるようにします。

チェッカーは、合格なら終了コード `0`、指摘があれば正の終了コードを返すようにします。ダッシュボードで指摘箇所の表示や判断を使う場合は、UTF-8で次の形式に揃えます。既存チェッカーの形式が異なる場合は、案件側のラッパーで変換できます。

```text
MIG101 src/Web/Pages/Menu.cshtml.cs:12 warning: 共通メソッドを使ってください
```

`max_repair_attempts` を省略すると、1つのRunにつき最大2回の自動修正を試み、その都度ビルドとチェッカーを実行します。`0` を指定すると指摘を保存して人の確認を待ちます。自動修正に渡す指摘は設定済みの移行先を指す必要があります。指摘と混ざるサマリーなどの出力は、必要に応じて別ログに分けます。

移行元PHPの文字コードは既定で自動判定します。既存の設定に `source_encoding` や移行単位の `[[units.inputs]]` がある場合も読み込めます。

### 移行済みのPHPとC#／Razorを参考にする

**設定** の **プロジェクト共通の参考実装** で **参考実装ペアを追加** を押し、**参考実装の元PHP** と **移行済みC#／Razor** にそれぞれ相対パスを入力します。元PHPに対応するRazorページ本体（`.cshtml`）とPageModel（`.cshtml.cs`）を登録する場合は、ペア内の **移行済みファイルを追加** で入力欄を増やし、1ファイルずつ指定します。セットごと設定から外す場合は、ペアの末尾にある **×** を押し、**設定を保存** します。設定から外したファイルはリポジトリに残ります。

元PHPと対応する移行済みファイルを例ごとに同じフォルダに置くと、Junieが対応を読み取りやすくなります。下は2ペアを登録したときのTOML設定例です。

```toml
[[project.references]]
role = "reference_php"
path = "docs/migration/examples/orders/orders.php"

[[project.references]]
role = "reference_razor"
path = "docs/migration/examples/orders/Orders.cshtml"

[[project.references]]
role = "reference_csharp"
path = "docs/migration/examples/orders/Orders.cshtml.cs"

[[project.references]]
role = "reference_php"
path = "docs/migration/examples/search/search.php"

[[project.references]]
role = "reference_razor"
path = "docs/migration/examples/search/Search.cshtml"
```

参考実装はすべての移行単位の全カプセルに同梱されます。参考用のPHPは移行台帳の対象外です。ルールや参考資料の一部だけを渡す場合は、各ファイルの **ファイルの詳細設定** で **開始行** と **終了行** を両方指定します。文字コードとシンボル名も同じ欄で設定できます。共通資料は毎回の入力上限に含まれるため、大きい実装例は必要な範囲に絞れます。既存の移行単位に登録したルールと参考実装は、設定画面で共通欄に引き継がれます。保存すると設定ファイルも共通形式に更新されます。

## 4. 独自スクリプトを使う

**設定** の **自作スクリプト** で **スクリプトを追加** を押し、実行工程を選びます。コマンドの1行目に実行ファイル、2行目以降に引数を1つずつ入力します。たとえばPowerShellで前処理を実行する場合は、工程に **準備前** を選び、次のように入力します。

```text
powershell.exe
-NoProfile
-File
tools/prepare-legacy.ps1
```

設定は全移行単位に適用されます。同じ工程を選んだスクリプトは、フォームの上から順に実行します。相対パスの基準は工程ごとに異なります。

| 工程 | 実行場所とタイミング |
| --- | --- |
| 準備前 | アプリのリポジトリで実行します。PHPや参照資料を生成する場合はここを選びます。 |
| 準備後、Junie移行前、Junie移行後 | Runの作業ツリーで各工程につき1回実行します。準備後にはカプセルが作成済みです。移行先を変換してからビルドしたい場合はJunie移行後を選びます。 |
| ビルド前、ビルド成功後 | Runの作業ツリーで、再検証や自動修正に伴うビルドでも実行します。 |
| チェッカー実行前、チェッカー実行後 | チェッカーを設定した場合にRunの作業ツリーで実行します。実行後のスクリプトはチェッカーが指摘を返した場合も実行します。 |

終了コードが非ゼロならその場で停止します。**進捗** の実行ログから工程とログファイルを確認してください。準備前のスクリプトで停止したときは修正後に準備か移行を開始し直します。準備後以降は同じRunを再開できます。再開時は失敗した工程のスクリプトを先頭から実行します。ビルド以降の失敗はビルド工程から再検証します。

作業ツリーで使うスクリプトは基準コミットに含めます。指摘を画面に表示し、Junieによる自動修正の対象にする検証は、**チェッカーの実行ファイルと引数** に設定します。

PowerShellのラッパーをチェッカーにする場合は、同じ欄に以下を入力します。`tools/check-migration.ps1` と呼び出すスクリプトは、作業ツリーの基準コミットに含めます。

```text
powershell.exe
-NoProfile
-File
tools/check-migration.ps1
```

## 5. ダッシュボードから実行する

1. **設定を保存** を押し、保存できたことを確認します。
2. **実行** タブで移行単位を選び、**移行を実行** を押します。初回は **準備** を選ぶと、Junieを起動する前に作業ツリーとカプセルを確認できます。そのRunで続けるときは、**進捗** の **同じ実行を再開** か、**保存済みの実行** の **移行を再開** を使います。実行時には、対象のソースと参照資料がJunieのサービスに送られます。
3. 起動元のPowerShellに初回のプロジェクト確認が出たら、対象がRunの `agent-workspace` であることを確かめて応答します。長い入力は複数カプセルに分かれ、Junieが順番に処理します。
4. **進捗** でビルドとチェッカーの結果を確認します。中断した場合は **実行** タブで同じRun IDを指定して **移行を再開** を押します。処理済みのカプセルを飛ばし、未完了のカプセルから続けます。移行元や移行ルールを変えた場合は新しいRunを開始します。再開は準備時に保存したカプセルを使います。

| 止まった場所 | 確認するもの |
| --- | --- |
| 準備 | 移行元や資料の相対パス、`start_line` と `end_line`、文字コードを確認します。 |
| Junie | 起動元のPowerShellに入力や確認が出ていないか見ます。画面のRun IDで中断後も続けられます。 |
| ビルド | Runの `build.log` を読み、SDKやビルド対象を確認します。必要な修正後、同じRunで再開します。 |
| チェッカー | Runの `checker.log` を読み、コマンドとその終了コードを確認します。修正後は **ビルド・チェックを再実行**、または状態に応じて **同じ実行を再開** を使います。 |

## 6. 差分を確認して移行を完了する

ダッシュボードの **移行台帳** でPHPの範囲と範囲IDを確認します。**差分を開く** を使う場合は、付属のDiff Viewerをアプリ側へコピーし、**プロジェクトの詳細設定** の **差分ビューアーのHTML** に `tools/dist/index.min.html` を指定します。次はハーネスのフォルダからコピーする例です。

```powershell
$viewerDir = 'C:\work\LegacyApp\tools\dist'
New-Item -ItemType Directory -Path $viewerDir -Force | Out-Null
Copy-Item -LiteralPath .\tools\dist\index.min.html -Destination $viewerDir
```

**差分ビューアー** で **使用しない** を選ぶか、HTMLを空欄にすると比較とアンカー生成が無効になります。設定したHTMLのパスを残したまま切り替えられます。準備、移行、ビルド、チェッカーは実行できます。作業ツリーで `git status --short` と `git diff` を使って差分を確認できます。

ビューアーを有効にして **Junieで対応行を提案して開く** を選ぶと、今回の比較から対応行を提案します。同じRunに確認済みの対応例があれば自動で参照します。この操作には移行とは別のJunie呼び出しが発生します。

移行台帳は閲覧用です。判断はハーネスのフォルダからCLIで記録します。次の例の `RUN_ID` と `1:1` は画面の値に置き換えます。同じ理由と移行先で複数範囲を確認した場合は、`--range` を繰り返します。

```powershell
.\.venv\Scripts\php-to-csharp-harness.exe ledger-review --config harness.local.toml --run-id RUN_ID --range 1:1 --state confirmed --target src/Web/Pages/Menu.cshtml.cs --target src/Web/Pages/Menu.cshtml --reason "元PHPと画面およびPageModelの対応を確認"
```

対象外の範囲を記録する場合は `--state excluded` と `--reason` を指定します。ビルドと設定済みチェッカーの条件を満たし、全範囲が確認済みか理由付きの対象外になると、画面に **移行完了** が表示されます。確認後に移行先を変更すると、対応する範囲が **要再確認** に戻ります。

チェッカーの指摘を修正不要と判断した場合は、進捗画面の確認ダイアログで指摘を選び、理由を入力して **問題なしとして記録** します。**同じ実行を再開** し、承認済みの指摘だけが残る場合もチェッカーの条件を満たします。

## Junieに設定手順を案内させる

ハーネスのリポジトリに [操作案内スキル](../.junie/skills/harness-setup/SKILL.md) が入っています。ハーネスのフォルダで `junie` を起動し、`/skills` で `harness-setup` を確認します。`/harness-setup` または `$harness-setup` を使い、アプリの場所、移行元、移行先、ルールファイル、チェッカーの起動方法を伝えると、案件に合った設定案と操作手順を案内できます。リポジトリにあるスキルはそのプロジェクトのJunieから使えます。 [JunieのAgent Skills](https://junie.jetbrains.com/docs/agent-skills.html)

ハーネスが実際の移行で起動するJunieは、Runごとの `agent-workspace` をプロジェクトとして開きます。案件の移行ルールを各カプセルに渡すときは、このガイドの `[[project.migration_rules]]` を使います。
