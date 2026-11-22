# 詳細ルール採否の根拠を比較する

Windows実機と認証済みJunieで実行する任意評価。製品や通常CIの依存は増えない。合成PHP、詳細ルール3件、必須方針、PHP/C#の参考実装ペア、C#の対象側設定だけを用いる。業務コードは送らない。

`compare.py` はハーネス本体の詳細ルール選別を起動する。採否と根拠の元資料・行範囲・SHA-256、所要時間、Junieが報告した使用量、保存済み判定の再利用を記録する。旧版と新版それぞれのソースを `PYTHONPATH` などで選び、同じスクリプト・同じ認証済みモデルで実行する。例:

~~~powershell
python evaluations/rule-evidence/compare.py before "$env:TEMP/rule-evidence-before.json" false true not-ready
python evaluations/rule-evidence/compare.py after "$env:TEMP/rule-evidence-after.json" not-ready true false
~~~

| ケース | 対象フラグ | C#参考実装 | 期待採用 |
| --- | --- | --- | --- |
| true | true | BADGE_READY | BadgeFormat、AuditTag |
| false | false | BADGE_READY | AuditTag |
| not-ready | true | BADGE_DISABLED | AuditTag |

SMTPルールは全ケースで非該当。複数資料に依存する採否では、保存済み必須方針、C#参考実装、対象C#の根拠をそれぞれ識別できるかを確認する。出力は根拠のファイルと範囲の照合であり、別案件での採否精度を保証しない。

最初の試行を再現する `original-true` / `original-false` は、PHPヘルパーの本体をあえて含めない。呼出し先が不明なままの場合はSMTPを安全側で保留・採用することがあり、通常の比較では副作用のないヘルパー定義を両方の版に供給する。

## 実機での比較結果

2026-11-23（日本時間）、Windows、Junie 26.11.2、`gpt-6-luna`（medium）。旧版はベースと同じソース。下の件数は **BadgeFormat 1件の採否から参照できた、異なる資料の数**。判定が保存されたケースはいずれも全3件の期待採否と一致し、保存済み判定を読み直す際にJunie使用量の増加はなかった。

| 同一入力での比較 | 変更前 | 変更後 |
| --- | --- | --- |
| original-true | 採用・1資料（115.116秒） | 採用・3資料（95.549秒） |
| original-false | 非採用・1資料 | 1回再試行後も応答形式エラー |
| false | 1回再試行後も応答形式エラー | 非採用・3資料（90.430秒） |
| not-ready | 非採用・1資料（71.510秒） | 非採用・3資料（97.990秒） |

変更後の3資料は必須方針、C#参考実装、移行対象のC#。確認できた効果は根拠の追跡性で、速度やエージェント応答の安定性の改善は未確認。
