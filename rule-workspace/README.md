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
