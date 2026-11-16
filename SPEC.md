# PHP → C# 移行ハーネス 技術仕様

Python製のローカルハーネスが入力、作業場所、工程、レビュー記録を管理します。Junie CLIがPHPからC#／ASP.NET Core Razor Pagesへの変換と修正を担当し、ハーネスが別のGit worktreeでビルドと検証を行います。導入・操作手順は [README](README.md) と [Windowsユーザーガイド](docs/windows-user-guide.md) を参照してください。

## 実行環境と設定

ハーネスは Python 3.11 以上で動作します。準備には Git、移行には認証済みの Junie CLI、ビルドにはアプリが要求する .NET SDK が必要です。配布パッケージはダッシュボードの HTML・JavaScript と Monaco Editor を含みます。

| TOML | 契約 |
| --- | --- |
| `[project]` | `root` は既存のアプリ用Git作業ツリー。 `base_ref` の既定値は `HEAD`。 `worktree_root` と `run_root` はアプリの外に置き、互いに包含しない。相対パスはTOMLの置き場所を基準とする。 |
| `[[units]]` | 重複しない `id` と、`source` や `[[units.inputs]]` で入力を指定する。移行先は `target` または順序付き `targets` で指定する。Razorページでは同名の `.cshtml` と `.cshtml.cs` を両方指定する。 |
| `[[project.migration_rules]]`・`[[project.references]]` | 共通ルールと参考実装。 `usage = "required"` が既定。 `usage = "reference"` には適用場面を示す `description` が必要。 |
| `[junie]` | `max_steps` は任意の正の整数。指定時は各Junie呼び出しに渡す。 |
| `[build]` | 任意の `project` にアプリ内の `.csproj`、`.sln`、`.slnx` を指定する。省略時はworktree直下で `dotnet build` を実行する。 |
| `[checker]` | 任意の `command` を実行ファイルと個別の引数で指定する。 `max_repair_attempts` はビルド・チェッカーで共有し、既定はRunごとに2回。0で自動修正を無効にする。 |
| `[scripts]` | 各工程にコマンドの配列を指定する。詳細は下記の実行順序を参照。 |
| `[diff_viewer]` | `html` が指定され有効な場合に比較・アンカー生成を提供する。 `anchor_reference` はTOMLを基準とした相対パスまたは絶対パスの配列。従来の単一パスも受け付ける。 |

移行元、ルール、参考実装、ViewerのHTMLの相対パスはアプリルートを基準とし、外部の絶対パスも受け付けます。移行先とビルド対象はアプリ内に収め、実行時にworktree内のパスへ解決します。詳細な設定例は [harness.example.toml](harness.example.toml) を参照してください。

## 実行順序と再開

1. **準備**。新しいUUIDのRunを作り、アプリルートで `before_prepare` を実行します。入力を検証し、カプセルの作成が成功してから基準リビジョンのdetached worktreeを作成します。以降のスクリプトはworktreeで実行します。
2. **移行**。 `after_prepare`、`before_migration` の順に実行し、必要に応じて詳細ルールの適用判定を行います。Junieをカプセルごとに起動し、保存済みの移行先と今回の担当範囲を渡します。完了トークンと編集内容を検証し、worktreeに反映します。
3. **検証**。 `after_migration` の後、`before_build` → `dotnet build` → `after_build` の順に実行します。チェッカーを設定した場合は続けて `before_checker` → チェッカー → `after_checker` を実行します。 `after_checker` はチェッカーの終了コードを取得した場合に実行します。スクリプトが失敗した場合はその工程で止まります。
4. **修正とレビュー**。修正可能なビルドエラーやチェッカー出力をJunieへ渡し、修正後にビルドからやり直します。回数上限、対象外の指摘、診断の繰り返しなどで修正を止め、ログと理由を保存します。自動修正に渡すチェッカー出力は最大65,536バイト、構造化して抽出した診断は合計8,000文字までです。

`resume` はRunのUUID、アプリルート、移行単位、移行先、worktreeの場所を現在の設定と照合します。未完了のカプセルがあれば保存したカプセルと下書きから続けます。全カプセル処理後は原則としてビルドから検証し直します。保存済みのエディタ指示が残っている場合はビルド前に処理します。準備がworktree作成前に止まった場合は新しいRunが必要です。

`after_prepare`・`before_migration`・`after_migration` は完了記録を利用します。途中で失敗した場合はコマンドの一致する成功済みの並びを飛ばして再開します。Junieのモデル通信失敗には1秒・3秒の間隔で最大2回再試行します。自動修正の中断、起動失敗、モデル通信失敗は修正回数を消費しません。

## カプセルとJunieの作業領域

- `source` は拡張子に関係なくPHPとして扱います。入力は `auto`、`utf-8`、`utf-8-sig`、`euc-jp` に対応します。自動判定はUTF-8 BOMを優先し、残りはUTF-8とEUC-JPで厳密に検査します。両方で異なる文字列になる場合は明示指定が必要です。PHPの元バイト列とSHA-256を準備時に固定します。
- `max_context_chars` は1カプセルのMarkdown本文の文字数で、既定500,000・最小1,000です。見出しと共通入力の分を先に確保し、残りを移行対象に割り当てます。原文の改行を保持し、行単位で分割します。1行が収まらない場合は行内の担当範囲を分割し、前後を参照用に重ねます。同じ入力の隣接カプセルには、別ファイルで前後最大12行ずつの読み取り用抜粋を添えます。分割は行番号・文字位置に基づきます。
- 必須ルール・詳細ルール・参考実装はカプセル本文の予算とは別に保存します。必須ルールは全文をガイドラインへ入れます。詳細ルールは初回編集前にJunieが全移行範囲と説明を照合し、全件のID・適用可否・理由を検証してRunに保存します。適用対象と、対象ファイルが明記された詳細の原文を作業領域に渡します。参考実装は索引から必要箇所を読めます。
- 移行・修正の呼び出しでは、短い追加資料をガイドラインにまとめ、上限を超える場合は保存済みのファイルを参照させます。移行中の累積照合には処理中の区間までの抜粋を渡します。固定文字列用の抽出補助は行範囲を検証し、対応するPHPリテラルを取り出します。移行の成立と振る舞いはビルド・テスト・チェッカー・人のレビューで確認します。
- `agent-workspace` に移行先、指定の移行資料、移行先周辺の参照ファイルを置きます。参照ファイルは、移行先の親ディレクトリ、隣接する `inc`、その一つ上の階層の `Libs`・`Extensions` から集めて保存します。Junie呼び出しの前後で編集対象外のファイルを照合し、設定した移行先の変更だけをworktreeに反映します。ファイル単位で置換する前に新旧ハッシュを保存し、途中で止まっても外部変更を検出しながら残りを適用できます。
- ブラウザーの手修正や追加指示はRunに保存します。Junieの区間終了後に変更を取り込み、同じ箇所の競合は人の解消を待ちます。保存済みの手修正は差分としてJunieに渡します。Junieが所定のJSONで質問を返したときは反映を保留し、回答を追加指示として受け付けます。

## 保存データと照合

| 保存場所 | 主な内容 |
| --- | --- |
| `<run_root>/<Run ID>/run.json` | 工程の状態、カプセルの完了数、移行先のハッシュ、工程時間、トークン、修正履歴、レビュー判断、検証時の照合情報。別CLIからの判断や使用量の更新と競合しないよう、ロックを使って置換する。 |
| `capsule/manifest.json` | カプセル本文のパス・文字数・ハッシュ、原文のファイル名と行範囲、長い行の担当・重複範囲、固定済み資料。 |
| `capsule/part-*/`、`capsule/php-sources/`、`capsule/materials/` | 行番号付き本文と境界抜粋、固定済みPHP、共通ルール・参考実装。 |
| `capsule/target-context/` | Junieの作業領域に復元する移行先周辺の参照ファイルと、専用のmanifest。 |
| `editor.json` | エディタの編集・手修正の差分・追加指示。 |
| `build.log`、`checker.log`、`<工程>.log` | ビルド・チェッカーの直近出力と設定スクリプトの出力。自動修正前の対象ログも別ファイルに退避する。 |
| `review-snapshots/<ID>.json` | 固定済みPHPと取得時点の移行先のコピー、ファイル境界、任意の行アンカー。 |
| `<worktree_root>/<Run ID>/` | ビルド・チェッカーが参照する移行後のアプリ。 |

既存Runに固定済みPHPがなければ、比較・エディタはmanifestのパスから現在の入力を読みます。その場合は準備時の原文と異なる可能性があります。

移行台帳の公開範囲IDは、カプセル番号とその中の1始まりの入力範囲番号を `1:1` のように結合します。アンカーは対応候補となり、`ledger-review` で理由付きの `confirmed`・`excluded`・`needs_review` を記録します。確認済みの移行先が変われば要再確認になります。

台帳での完了には、全PHP範囲の確認または理由付きの除外、全カプセルの処理、現在の入力に対する検証の成功が必要です。検証では、対象ファイル、Git管理下のソース・一般的なビルド入力、ビルド先、チェッカーと検証スクリプトを照合します。チェッカーの指摘は `review` で理由と対象ファイルの版を記録でき、再実行後に承認済みの指摘だけが残る場合も検証成功として扱います。

## CLIとダッシュボード

| コマンド | 役割 |
| --- | --- |
| `dashboard` | 設定の編集、準備・実行、保存済みRunの選択。 |
| `prepare --unit ID`・`run --unit ID` | 準備のみ、または準備から移行と検証まで実行。 |
| `resume --run-id UUID`・`view --run-id UUID` | 同じRunを再開、またはダッシュボードで表示。 |
| `review --run-id UUID`・`ledger-review --run-id UUID` | チェッカーの指摘とPHP範囲のレビュー。 |
| `anchors --run-id UUID` | Junieに対応行を提案させ、座標を検証して比較JSONを保存。 |

全コマンドで `--config` が必要です。 `run`・`resume` は `--dashboard` に対応します。 `run`・`resume`・`view` の `--checker-command` は後続の引数すべてをチェッカーに渡すため末尾に置きます。

ダッシュボードは `127.0.0.1` の空きポートにバインドし、ランダムなURL接頭辞の下に画面とAPIを配信します。進捗は `GET …/events` のSSEで送り、再接続時は直近100件の範囲で再送します。操作のPOSTは `Host`・`Origin` を照合します。ブラウザー操作時のJunieはACPで連携し、プロジェクトの信頼・操作の許可を画面で受け付けます。端末から直接実行する移行はJunieの対話CLIを使います。

Junieには資料と書き戻すファイルを限定します。プロセス自体には起動ユーザーのOS権限があり、Junieのサービスへ移行コンテキストが送信されます。Runの保存データには原文・移行先・診断が含まれます。

## 実装の入口

| モジュール | 担当 |
| --- | --- |
| [cli.py](src/migration_harness/cli.py)・[config.py](src/migration_harness/config.py) | コマンドの入口、TOML検証と正規化。 |
| [workflow.py](src/migration_harness/workflow.py) | 準備・移行・再開の順序、Junieへの指示、ビルドとチェッカー、移行台帳。 |
| [capsules.py](src/migration_harness/capsules.py)・[rule_materials.py](src/migration_harness/rule_materials.py) | 入力の固定、分割、資料・manifestの生成と読み戻し。 |
| [run_records.py](src/migration_harness/run_records.py)・[timings.py](src/migration_harness/timings.py) | Runの検証・保存と工程計測。 |
| [worktree_targets.py](src/migration_harness/worktree_targets.py)・[source_context.py](src/migration_harness/source_context.py) | 移行先への反映と復旧、周辺の参照ファイルの固定と復元。 |
| [junie_cli.py](src/migration_harness/junie_cli.py)・[execution.py](src/migration_harness/execution.py) | Junieの起動・ACP・イベント・通信再試行、外部プロセスの実行とキャンセル。 |
| [dashboard.py](src/migration_harness/dashboard.py)・[editor_queue.py](src/migration_harness/editor_queue.py) | ローカルHTTPとSSE、エディタの保存・指示待ち・競合処理。 |
| [diff_viewer.py](src/migration_harness/diff_viewer.py)・[review_anchors.py](src/migration_harness/review_anchors.py) | 一貫した比較スナップショットの作成、対応行の提案と検証。 |

回帰テストは `python -m pip install -e .` の後に `python -m unittest discover -s tests -v` で実行します。入力規模、ルール、再開・修正の検証シナリオは [evaluations/](evaluations/) にあります。
