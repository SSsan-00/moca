# 反復データの移行評価

固定の [`ordered-items.php`](app/ordered-items.php) を、1ユニットで `Pages/OrderedItems.cshtml` と `Pages/OrderedItems.cshtml.cs` に移行します。`single.toml` は1カプセル、`split.toml` は2カプセルに分けます。両設定で同じ入力・初期状態・チェッカーを使い、自動修正は0回に固定します。

ハーネスでビルドした .NET 10 アプリは、ループバックの空きポートで Razor Pages を起動し、`/OrderedItems` のHTTPレスポンスを標準出力に書き出します。`checker_command` はHTMLの各 `li` のテキストを出現順に取り出し、HTMLエンティティを復号して前後の空白を除き、[`expected.json`](app/expected.json) の24件と比較します。完全一致は終了コード `0`、列の不一致は `1`、実行エラーは `2` です。

## 準備

リポジトリのルートから Bash で実行します。Git、Python 3.11以上、.NET 10 SDK（`app/global.json` の `10.0.100` 以上の安定版10.0系）、ログイン済みのJunie CLI、対話端末が必要です。PHP CLIは期待JSONを再確認するときに使います。

```bash
harness_commit=$(git rev-parse HEAD)
fixture="$PWD/evaluations/ordered-items"
eval_root=$(mktemp -d)

cp -R "$fixture/app" "$fixture/single.toml" "$fixture/split.toml" "$eval_root/"
mkdir "$eval_root/harness"
git archive "$harness_commit" src pyproject.toml progress-demo.html harness.example.toml |
  tar -xf - -C "$eval_root/harness"
python3 -m venv "$eval_root/venv"
. "$eval_root/venv/bin/activate"
python3 -m pip install -e "$eval_root/harness"

git init -q --template= "$eval_root/app"
git -C "$eval_root/app" config core.autocrlf false
git -C "$eval_root/app" add -f -- ordered-items.php expected.json check_ordered_items.py \
  global.json OrderedItems.csproj Program.cs Pages/OrderedItems.cshtml Pages/OrderedItems.cshtml.cs
git -C "$eval_root/app" -c user.name='Ordered items fixture' -c user.email='fixture@example.invalid' \
  -c commit.gpgsign=false commit -qm 'Initialize ordered-items evaluation fixture'
fixture_tree=$(git -C "$eval_root/app" rev-parse 'HEAD^{tree}')
printf 'Evaluation directory: %s\nHarness commit: %s\nFixture tree: %s\n' "$eval_root" "$harness_commit" "$fixture_tree"
shasum -a 256 "$eval_root/single.toml" "$eval_root/split.toml"
(cd "$eval_root/app" && dotnet --version)
junie --version
```

固定アプリの初回コミットは `$eval_root/app` に作成します。ハーネスはその `HEAD` からworktreeを作成します。指示の変更を比較するときは、準備時に `harness_commit` へ評価対象のハーネスのコミットを指定します。

## 実行と記録

```bash
php-to-csharp-harness run --config "$eval_root/single.toml" --unit ordered-items
php-to-csharp-harness run --config "$eval_root/split.toml" --unit ordered-items
```

Junieが作業場所を確認する場合は、表示されたパスが今回のRunの `agent-workspace` であることを確認します。`JLine returned dumb` で停止した場合は、`TERM=xterm-256color` など実際の端末に合った設定で実行します。カプセル数は `run.json` の `context_capsules` で確認できます。分割位置は `capsule/manifest.json` に記録されます。中断時は同じ設定とRun IDで `php-to-csharp-harness resume --config "$eval_root/split.toml" --run-id RUN_ID` のように再開し、再開したことを結果に記録します。

実行ごとに日付、ハーネスのコミット、fixtureのGitツリーID、設定ファイルのSHA-256、Junieの版・モデル表示、.NET SDKの版と、下表の項目を記録します。取得できない版やモデルは「不明」と記録します。各Runの記録は `$eval_root/runs/<Run ID>/run.json`、`build.log`、`checker.log` にあります。`checker_succeeded` かつ採点終了コード `0` を成功とし、採点まで到達した非ゼロの結果を失敗とします。採点まで進まなかった場合は「採点未実行」とし、停止した工程と理由を残します。Junieやビルドの失敗を残したまま再試行する場合は、Run IDごとに区別します。

この固定ケースの少数の結果から、実案件の成功率や一般的な品質向上を断定しないでください。

## 初回の実測結果

2026-11-16（JST）、ハーネス `aee16f2b39ce1830f2b6323623da3d6e5952d0c0`、fixtureツリー `4dc9ccd73d7192bd93ac19d70f5b8980b043a01c`。Junie `26.11.1 (4020.10)` は全カプセルで Junie Lite（ログ上のモデルは `Qwen/Qwen3.6-27B-FP8`）を使用しました。.NET SDKは `10.0.201`、ASP.NET Coreランタイムは `10.0.5` です。

| 設定 | 実測時のSHA-256 | 現行のSHA-256 |
| --- | --- | --- |
| `single.toml` | `a86fd9af342417bedb95075c3bbf66de1588166df160fd252cc8c3417f75a8fb` | `8377dade746229bdbdb43edf6d787abf6091ba92c44aeebf9553a7331407b289` |
| `split.toml` | `55f3e4de1dcdfa2f34a3d091ebf160c859bac7ecc667009a7c43d8e41ccccf38` | `6bb9d8a6c484da267b345865109446d76f60be315e924f14182db2b755cee8d4` |

実測時の設定と現行設定の差分は、`base_ref = "FETCH_HEAD"` の有無だけです。現行設定は既定の `HEAD` を参照します。新しい手順で両設定を準備し、fixtureのGitツリーとカプセル・境界抜粋のハッシュが実測時と一致することを確認しました。さらに、下表の `split.toml` と `single.toml`（再試行）のJunie生成物を新しいworktreeでビルドし、両方で採点の終了コードと出力が実測時と一致しました。

| 設定 | Run ID | 完了カプセル | ビルド | 採点 |
| --- | --- | ---: | --- | --- |
| `single.toml` | `b5a34efe-7e0c-42de-aa29-145a41fbf5d0` | 1/1 | 失敗（`1`、`CS0246`。ビューがPageModelの名前空間を解決できず） | 未実行 |
| `split.toml` | `a539de32-f59f-4183-838f-3b2643bbce55` | 2/2 | 成功（`0`） | 成功（`0`、24件が順序も含めて一致） |
| `single.toml`（同条件で新規に再試行） | `cd86f032-7d25-4cd7-ac86-18df70deda85` | 1/1 | 成功（`0`） | 失敗（`1`、3件を二重にHTMLエスケープ） |

実測前の端末起動では `JLine returned dumb` が発生しました。`single.toml` の `5728d3bd-d973-4b3d-9a64-c1e6007f0ded` と `split.toml` の `5a20d50d-5b1d-425a-80d0-5bc0c9dba44e` はモデル呼び出し・ビルド・採点が未実行です。上表は端末設定を直して新規に実行した結果です。

## 通常CIと期待値の確認

通常CIは `unittest discover` で正答、値の変更、順序の入れ替え、重複の欠落、二重エスケープを採点します。Junie、.NET、PHPの起動はありません。PHPを使って期待JSONを確認するには、リポジトリのルートで次を実行します。

```bash
set -o pipefail
php evaluations/ordered-items/app/ordered-items.php |
  python3 evaluations/ordered-items/app/check_ordered_items.py --html-stdin
```
