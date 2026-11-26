# 静的分岐検査の合成ケース

`decisions.txt` は拡張子が `.php` ではない固定PHPです。`incomplete.inc` と `Broken.cs` は意図的に構文が不完全です。どのソースも実行しません。判定と入力・戻り値の期待値は [テスト](../../../tests/test_migration_control_flow.py) にある独立した記述で照合します。`pairs.json` はどの関数同士を、どの版・行・型で比較するかだけを指定し、期待する判定は持ちません。
