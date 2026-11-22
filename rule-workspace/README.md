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

## 詳細ルールの読み始めの案内

新しい Run を準備するとき、移行対象の PHP と登録済みルールの版を固定します。対象 PHP の識別子や語を、日本語の語の一部も含めて、固定した参照用の詳細ルールと照合します。識別子の一致を優先し、関数ごとの BM25 の順位で最大 16 件の読み始めの候補を記録します。検索はローカルで行い、事前の索引作成、追加インストール、追加設定は不要です。再開時はその Run の記録を使い、新しい Run では最新の登録内容から計算し直します。

候補なし、検索語なし、一致なし、検索補助の失敗や処理量の上限、保存した候補と詳細資料の不一致、Junie への入力予算不足のときは、案内だけを省きます。案内は採用・除外の根拠ではなく、必須事項の適用も詳細ルール全件の適用判定も変わりません。必須入力のエラーや停止要求は通常どおり処理します。
