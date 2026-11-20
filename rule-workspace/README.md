# 案件ごとのルール整理用フォルダ

散在する移行ルールの固定コピー、分割計画、生成した必須事項と詳細をここに保存します。アプリのリポジトリやRunの `agent-workspace` とは別の、ハーネス側の作業場所です。

```text
rule-workspace/
  LegacyApp/                 # 1つのPACK
    sources/                 # 全入力のコピー。snapshot後は変更しない
      original.md
    sources.json             # snapshotが記録するSHA-256・行数
    plan.json                # 原文を振り分ける行範囲
    generated/               # buildの出力。直接編集しない
      Agent.md               # 毎回適用する必須事項
      details/
        database.md          # 用途別の詳細・例
      coverage.json          # 原文と生成物の対応
      rules.toml             # 絶対パスの設定断片
```

Junieに任せる場合は、ハーネス直下で `junie` を起動し、`/skills` で `harness-optimize-rules` が有効か確認して、次をJunieの入力欄に送ります。

```text
/harness-optimize-rules C:/migration/loose-rules の全ルールを、情報を落とさず Agent.md と用途別の詳細へ整理してください。
保存先は rule-workspace/LegacyApp、既存設定は harness.local.toml です。TOMLへの反映もお願いします。
```

設定を変えたくない場合は末尾を「TOMLは変更せず、設定方法だけ案内してください」に置き換えます。詳細な手順は [整理スキル](../.junie/skills/harness-optimize-rules/SKILL.md)、JSONの書式は [plan.jsonの形式](../.junie/skills/harness-optimize-rules/references/plan-format.md) を参照してください。

登録済みのルールから見直す場合は、原文フォルダの代わりに設定を指定できます。

```text
/harness-optimize-rules harness.local.toml の「プロジェクト共通の移行ルール」の読み込み方を最適化して、設定へ反映してください。
```

すでに内容が分離されていれば、本文を保持して必須／詳細と読む場面の設定だけを更新します。必須と詳細が混在する場合は上の構成で分割します。設定のバックアップと見直し結果もこの作業領域に残します。

## 手動で進める場合

全入力の実ファイルを `LegacyApp/sources/` にコピーし、コピー元のパスを記録します。入力はUTF-8（BOM可）の `.md`・`.markdown`・`.txt`。他の形式は元ファイルを残して変換し、内容を確認してから進めます。

```powershell
.\.venv\Scripts\python.exe tools/rule_pack.py snapshot rule-workspace/LegacyApp
# 原文の全行を振り分けるplan.jsonを作成する
.\.venv\Scripts\python.exe tools/rule_pack.py build rule-workspace/LegacyApp
.\.venv\Scripts\python.exe tools/rule_pack.py verify rule-workspace/LegacyApp
```

全行の一致と、条件・例外・優先順位の意味を確認してから、生成された `rules.toml` を参考に対象のルール登録だけ置き換えます。これは設定の断片なので、既存TOML全体を上書きしないでください。設定確認だけのために準備・移行は実行しません。

採用済みの原文と生成物は保持します。変更時は `LegacyApp-r2` のような新しい作業領域を作成してください。登録したファイルは絶対パスで参照するため、移動・削除しないでください。このフォルダ内の案件データはGitの対象外です。共有・バックアップが必要なら別途保管します。

## 詳細ルールのローカル意味検索（任意）

移行単位の対象が明確な詳細ルールに限り、言い換えや日英の表現差で関連する原文を探せます。通常の詳細レビューに候補行を案内するだけで、必須事項・採用済み詳細・既存索引は置き換えません。検索の有無は適用・除外や検証成功の根拠ではありません。

ハーネスの仮想環境に `python -m pip install -e ".[rule-search]"` で追加依存を導入し、アプリリポジトリ外に [E5 small ONNXモデル](https://huggingface.co/intfloat/multilingual-e5-small/blob/main/onnx/model_qint8_avx512_vnni.onnx) を `model.onnx`、[同じ配布元のtokenizer](https://huggingface.co/intfloat/multilingual-e5-small/blob/main/tokenizer.json) を `tokenizer.json` として配置します。ハーネスは既知のSHA-256を照合し、モデルをダウンロードしたり、埋め込み用に資料を外部に送ったりしません。Junieによる既存のレビューとは別です。

```toml
[rule_search]
model_dir = "rule-workspace/_models/e5" # 設定ファイルからの相対パス

[[rule_search.units]]
unit = "orders"
query = "返品処理、文字列のゼロ、払い戻し上限" # 任意。省略時は単位・移行先・入力のシンボルを使用
references = ["C:/migration/Orders-r3/generated/details/returns.md"]
```

`references` はその移行単位で参照可能な、既に `usage = "reference"` で登録したファイルへの明示範囲です。相対パスは `project.root` が基準です。版や対象が違う資料は指定しないでください。原文・生成物の検証と設定反映のあと、通常のCLIから索引を更新します。

```powershell
.\.venv\Scripts\python.exe -m migration_harness.cli rule-search-index --config harness.local.toml --unit orders
```

更新前の索引は、元ファイル・登録・モデルの版が一致しなくなれば使われません。生成先が次版へ移ったときは `references` も新しい版を指すよう確認します。索引はRun保存先の `.rule-search` に置き、新しいRunでのみ検索を行って候補を固定します。索引や追加依存がない、破損、不一致、または読込に失敗した場合は従来どおりです。
