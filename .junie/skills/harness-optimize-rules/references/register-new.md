# 新しい原文を登録する場合だけ読む

既存TOMLのルールを見直す場合は、この手順を使わず [読み込み方の見直し](loading-policy.md) の `start` → `apply` を使う。以下は、まだ登録していない原文を整理して設定へ追加する場合の手順。

**書き込む前に、意味レビューで見つけた矛盾・未解決事項を確認する。** 同じ対象の相反する数値指定や義務・禁止などが未解決なら、整理済みファイルと設定案だけを残し、既存TOMLを変更せず利用者に判断を求める。たとえば同じタイムアウトに30秒と60秒の指定があれば、単に別ファイルへ配置しても解決していない。

先に以下を実行する。設定とPACKのパスは実際の対象へ置き換える。`config.before.toml` が既にある場合は別名にし、既存バックアップを上書きしない。**コピーの成功とバイト一致を確認してからTOMLを編集する。**

```text
python -c "from pathlib import Path; p=Path('harness.local.toml'); b=Path('rule-workspace/LegacyApp/config.before.toml'); data=p.read_bytes(); f=b.open('xb'); f.write(data); f.close(); assert b.read_bytes()==data; print('backup OK')"
```

1. 同じ原文の登録が既にないか確認する。既存パスはアプリルート基準で解決し、コピー元の絶対パスとバイト単位のSHA-256を両方照合する。ファイル名だけで判定しない。変換した入力は変換前後の対応と内容確認も必要。対応する旧登録があれば、新しいPACKで登録済みルールの見直しへ戻り、全ページ確認後に置き換える。
2. `tomlkit` または対象箇所だけのパッチで、新しい登録を追加する。設定全体を生成し直さず、`rules.toml` で上書きしない。既存の登録を変更・重複させない。
3. バックアップとの比較で、対象外のルール、チェッカー、スクリプト、参考実装、移行単位、その他の設定とコメントの保持を確認する。案件外の設定不備も勝手に修正しない。
4. ハーネスをインストールしたPythonで次を実行し、`load_config` で検証する。失敗時はエラーを報告し、今回の設定変更を戻す。設定確認を理由に準備・移行を実行しない。

```text
python -c "from pathlib import Path; from migration_harness.config import load_config; load_config(Path('harness.local.toml')); print('config OK')"
```
