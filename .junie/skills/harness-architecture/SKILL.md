---
name: harness-architecture
description: PHP→C# Migration Harness のフォルダ構成、設定の意味、内部アーキテクチャ、起動から移行完了までの処理、Run の再開や失敗理由について、現在のチェックアウトの実装とテストを調べて回答する。
---

# 移行ハーネスについて実装を根拠に回答する

ハーネスのチェックアウトを基準に、質問に関係するコードと設定を確認して日本語で回答する。質問への回答や原因調査は読み取りで行う。設定の変更、Run の開始、フックやチェッカーの実行は、それ自体を依頼された場合に扱う。環境構築や操作の実施は [harness-setup](../harness-setup/SKILL.md)、ルールの整理・反映は [harness-optimize-rules](../harness-optimize-rules/SKILL.md) を参照する。

## 根拠の確認

1. [アーキテクチャガイド](../../../docs/architecture.md) で関連する工程を見つける。設定契約は [SPEC.md](../../../SPEC.md) と [harness.example.toml](../../../harness.example.toml)、操作例は [Windowsユーザーガイド](../../../docs/windows-user-guide.md) を参照する。
2. 現在の `src/migration_harness/` で該当する関数を特定する。入口から呼び出し先、設定の解決、分岐、保存と読み戻しまで、回答で述べる挙動に必要な範囲を追う。既定値やステータスの意味は定義箇所と判定箇所を確認する。
3. 関連する `tests/` のアサーションを読み、正常系に加えて質問に該当する失敗・再開経路を照合する。テストの記述から分かること、実際に今回実行して確認した結果、実装からの推定を区別する。静的デモと `evaluations/` の個別条件は、本番の処理経路・既定値と区別する。
4. 資料と現行コードに差がある場合は、質問の経路を実装に基づいて説明し、差が出る文書の箇所と実装の根拠を示す。ローカル設定、Run の記録、OS や Junie のバージョンで結果が変わる場合は、読み取れた範囲と未確認の条件を明記する。調査に必要な設定キーやログだけを参照し、認証情報や移行元の本文を回答へ転載しない。

## 質問から実装を引く

表の Python ファイルは `src/migration_harness/` 配下。必要な経路だけを読む。

| 質問 | 主にたどる実装・資料 |
| --- | --- |
| フォルダの役割、起動、設定、CLI と画面の差 | [README.md](../../../README.md)、[pyproject.toml](../../../pyproject.toml)、[h.cmd](../../../h.cmd)、`cli.main`、`config.load_config`、`web/dashboard.run_dashboard`、`web/settings_form.py` |
| PHP の文字コード、分割と固定、ルール、参照ファイル | `workflow._prepare_run` → `context/capsules.prepare_context`、`rules/rule_materials.py`、`workflow._review_detail_applicability`、`context/source_context.stage_target_context` |
| Junie の資料・起動・停止、手修正、移行先の反映 | `workflow._run_prepared_with_junie`、`junie/junie_cli.run_interactive` と `_run_session`、`execution.py`、`runs/editor_queue.py`、`runs/worktree_targets.apply_agent_targets` と `finish_pending_targets` |
| Run の失敗・再開、ビルドやチェッカー、修正回数 | `runs/run_records.load_saved_run`、`workflow._resume_with_junie` と `_run_verification`、`verification/checker_output.py`、該当 Run の `run.json` と工程ログ |
| 画面の実行枠、会話、イベント、同時操作 | `web/dashboard.py`、`web/dashboard*.js`、`runs/run_manager.RunManager`、`junie/junie_conversation.JunieConversations`、[Run の状態とロック契約](../../../docs/run-state-contracts.md) |
| 比較、アンカー、レビュー、移行完了の条件 | `web/diff_viewer.py`、`web/review_anchors.generate_anchors`、`workflow.migration_ledger` と `review_migration_ranges` |

## 回答の組み立て

結論の後に、適用条件、主な処理順、結果の保存先を必要な分だけ示す。根拠はリポジトリ相対の `パス:行番号` またはファイルへのリンクと関数名を添える。複数工程の関係を問われたときは Mermaid を使い、図に含める分岐も呼び出し元と実装で確認する。

- 「完了」はカプセルの完了、ビルド・チェッカーの結果、移行台帳の `completion.complete` のどれかを明確にする。台帳の完了条件を尋ねられたら、`complete` の式、`verification_passed` の前提と後続の照合、`reviewed` の加算条件まで確認する。「全条件」を列挙するときは、反映保留、版、設定、理由なども含めて判定式の各項目と照合する。
- 「再開」は CLI の直接実行とダッシュボードでの表示・操作を区別し、例示するコマンドは引数解析と実際の分岐まで確認する。
- 特定の Run の質問には、Run ID の保存記録で現在の工程を確認する。一般仕様はその Run の実行結果と区別し、次に確認するキー・ファイル・ログを示す。
