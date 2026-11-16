# plan.jsonの形式

`PACK/plan.json` はUTF-8（BOM可）のJSON。`documents` に出力する文書と原文の行範囲を記述する。本文をJSONへ書き直さず、`parts` で固定した `sources/` の原文を指定する。

```json
{
  "format_version": 2,
  "documents": [
    {
      "path": "Agent.md",
      "title": "必須事項",
      "parts": [
        { "source": "original.md", "start": 1, "end": 4 }
      ]
    },
    {
      "path": "details/database.md",
      "title": "DBの詳細",
      "description": "DBアクセスを移行/修正するとき",
      "parts": [
        { "source": "original.md", "start": 5, "end": 12 }
      ]
    }
  ]
}
```

この例は `sources/original.md` が12行で、4行目と5行目の間で意味を切り離せる場合のみ有効。実際の行数・文脈を読んで作り直す。

| 項目 | 意味 |
| --- | --- |
| `format_version` | 新しい計画では `2`。提供済み本文を再取得させない参照案内を生成する。省略または `1` は旧PACKの再検証用で、既存計画・生成物は書き換えない。 |
| `documents` | 出力文書の一覧。必須事項の `Agent.md` と、必要な詳細文書を指定する。 |
| `path` | `generated/` からの相対出力パス。`Agent.md` または `details/<用途>.md`。用途名は英小文字・数字で始め、英小文字・数字・`_`・`-` のみ。`nul`・`con` 等のWindows予約名は不可。 |
| `title` | 生成時に加える見出し。空でない1行の文字列。原文を置き換える要約ではない。 |
| `description` | 詳細を読む具体的な場面。詳細文書に必須で、空でない1行の文字列。 |
| `parts` | 文書に並べる原文範囲の一覧。列挙順にコピーする。 |
| `source` | `sources/` からの相対パス。例: `team-a/original.md`。同名入力を区別し、絶対パス・親への移動を使わない。 |
| `start`, `end` | **1始まり、両端を含む**行番号。空行を数え、`sources.json` の行数に合わせる。 |

全入力の全行が出力全体でちょうど1回必要。同じ文章が異なる原文行に存在しても両方残す。異なる出力に同じ原文範囲を重ねたり、空行・重複文・見出しを落としたりしない。範囲の切れ目は段落・表・コードフェンス・条件と例外・定義の関係を壊さない場所にする。

全体の義務や適用場面が曖昧な範囲は `Agent.md` に入れる。複数の用途にまたがる必須事項を詳細に複製せず、必須側で1回保持する。解決していない矛盾も原文のまま保持する。

## 生成物と設定断片

`build` が出典と詳細への案内を追加するため、Agentの索引を原文のコピーや独自の相対リンクで手作業追記しない。設定断片の形は次のとおり（パスは生成された実際の絶対パスを使う）。

```toml
[[project.migration_rules]]
path = "C:/work/harness/rule-workspace/LegacyApp/generated/Agent.md"
usage = "required"

[[project.migration_rules]]
path = "C:/work/harness/rule-workspace/LegacyApp/generated/details/database.md"
usage = "reference"
description = "DBアクセスを移行/修正するとき"
```

この断片だけで `harness.local.toml` を置き換えない。設定反映の手順は [SKILL.md](../SKILL.md) に従う。

現在のRunでは、選択済みの詳細本文が `Applicable details` として起動時に提供される。その本文を確認すればよく、同じ資料をファイルから重ねて読む必要はない。索引だけが提供される場合は、上の絶対パスが `Detail and example references` の `source` に対応し、同じ項目の `file` が読む場所となる。番号やPACKの相対パスがRunでも同じだとは考えない。いずれも説明文を原文の代わりにせず、条件・例外を確認する。
