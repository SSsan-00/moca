# PHP → C# Migration Harness

PythonとLangGraphで、PHPからASP.NET Core Razor Pages（現状.NET 9）への移行を支援するローカル実行用ハーネスです。コード移行はJunieに依頼し、ハーネスが対象範囲、作業場所、実行記録を管理します。

## 現在の実装

- 移行単位ごとに、入力ファイルと行範囲をTOMLで明示する
- 指定行だけを行番号付きコンテキストにまとめ、設定上限ごとのカプセルに分割する
- アプリ用のGit worktreeを、指定した基準リビジョンから作成する
- `run`では、対象ファイルとカプセルを1つずつ渡し、Junie CLIを順番に起動する
- 各Junie呼び出し後に対象ファイルをworktreeへ反映し、実行記録を保存する

ビルド、テスト、独自チェッカー、修正ループ、Diff Viewer用アンカーJSONは未実装です。SQLiteチェックポイントは現在、準備グラフの状態を保存しますが、ハーネスから処理を再開する機能はありません。

Junieへ渡す情報と作業フォルダはハーネスで絞りますが、別の絶対パスへのアクセスをOSレベルで遮断するものではありません。Junieを使うと、選択したコンテキストがJunieのサービスへ送られます。生成後は`run.json`とworktreeの差分を人が確認してください。

## セットアップ（Windows）

Python 3.11以上、Git、ログイン済みのJunie CLIが必要です。作業フォルダでPowerShellを開き、リポジトリをcloneして仮想環境を作成します。

```powershell
git clone https://github.com/SSsan-00/php-to-csharp-migration-harness.git
Set-Location php-to-csharp-migration-harness
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item harness.example.toml harness.local.toml
```

`pip install -e .`はハーネスのコマンドを仮想環境に登録し、LangGraphなどのPython依存パッケージをインストールします。`-e`はソースをclone先に置いたまま使う開発向け設定で、ハーネスを改造すると次回実行から変更が反映されます。Python、Git、Junie CLI自体はこのコマンドではインストールされません。

`harness.local.toml`を編集して、アプリのGitリポジトリ、基準リビジョン、worktree保存先、実行データ保存先、移行単位と入力範囲を指定します。worktreeと実行データはアプリリポジトリおよびこのハーネスリポジトリの外に置いてください。設定例のパスと行範囲はプレースホルダーです。

入力には、移行元PHP、コメントだけを追加した注釈版PHP、仮C#、移行ルールの該当箇所、必要な既存C#例などを指定できます。各入力はファイルと開始・終了行を明示します。`max_context_chars`の既定値は100,000文字です。これは1回のJunie呼び出しに渡すコンテキストカプセルごとの上限であり、トークン数の厳密な上限ではありません。範囲が上限を超えると現在は行境界で分割し、`run`は順番にJunieへ渡します。関数や制御構文の境界は現在判定しません。1行自体が上限を超える場合は文字位置を記録して分割し、隣接する断片の前後512文字（上限の小さい設定ではその5%）を読み取り用に重ねます。AIには重なり部分を再移行せず、指定されたfocus範囲だけを処理するよう指示します。各入力の文字コードは`encoding`で指定できます。`auto`ではUTF-8 BOMを優先し、BOMなしの場合はUTF-8とEUC-JPを厳密に判定します。両方で異なる文字列として読める曖昧な入力や、どちらでも読めない入力は自動選択せず、`encoding = "utf-8"`または`encoding = "euc-jp"`の指定を求めます。対象ファイル全体や会話履歴・ツール出力は文字数に含みません。

### 大規模ソースの情報欠落を防ぐ設計

目標は、全ソースを一度にJunieへ渡すことではなく、必要な原文を後から取得でき、未処理範囲を見逃さないことです。現行実装は指定行を文字数上限に合わせて分割し、`run`で順番にJunieへ渡します。原文は切り捨てず、`capsule/manifest.json`に各カプセルの文字数、文字コード、元ファイル・行範囲、行内のfocus範囲と重なり範囲を記録します。巨大な1行では前後の文字列を重ねて渡し、AIが境界付近の文脈を参照できるようにします。複数カプセルになった場合は、構文境界を判定していないため、`run.json`の確認フラグに従って分割位置をレビューしてください。関数などの構文境界を安全に判定する機能、移行元の項目と移行先の対応、依存資料、未処理状態を追跡する台帳は今後の設計です。

この台帳、依存資料の追加取得、関数などの論理境界に基づく分割、全範囲の網羅性確認はまだ未実装です。台帳の単位と状態は仕様ページの設計案を参照してください。

## 実行

指定内容の検証、コンテキスト作成、worktree作成だけを行う場合:

```powershell
php-to-csharp-harness prepare --config harness.local.toml --unit example-page
```

Junieを起動して対象ファイルを移行する場合:

```powershell
php-to-csharp-harness run --config harness.local.toml --unit example-page
```

`run`には対話ターミナルと`PATH`上のJunie CLIが必要です。Junie用作業フォルダには、設定した対象ファイルとコンテキストだけを配置します。複数カプセルがある場合は、1つのカプセルを渡すたびにJunie CLIを起動し、前の呼び出しで更新した対象ファイルを次の呼び出しへ引き継ぎます。各呼び出し後にworktreeへ反映します。ビルドやチェッカーは起動しません。

`[junie].max_steps`は任意の正の整数です。未設定の場合は`--max-steps`をJunieに渡しません。これは1回のJunie呼び出しの手順数上限で、トークン数や料金の上限ではありません。複数カプセルでは呼び出しごとに適用されるため、実行全体の手順数上限はカプセル数に応じて増えます。Junieがこの上限で止まったとき、同じセッションを再開する機能は未実装です。

実行データとコンテキストには利用者のソースが含まれる場合があります。ローカルで適切に管理してください。設定範囲の指定は、JunieのOSレベルのファイルアクセス制御ではありません。

## 仕様と参考資料

- [仕様・実装状況HTML](migration-harness-spec.html)
- [進捗画面デモ（模擬データ）](progress-demo.html)
- GitHub Pages: <https://sssan-00.github.io/php-to-csharp-migration-harness/>

以下は設計・実装の参考資料です。いずれもこのハーネスの実行時依存として導入せず、必要な考え方を参考にします。

- ハーネスの構成・エージェント運用: [takt](https://github.com/nrslib/takt)、[Codex](https://github.com/openai/codex)、[ECC](https://github.com/affaan-m/ECC)
- YAGNI・小さな変更・実装レビュー: [Ponytail](https://github.com/dietrichgebert/ponytail)
- 大規模コードの関係索引・ナレッジグラフ: [Graphify](https://github.com/Graphify-Labs/graphify)
- 実行結果に基づくスキル・ルール改善の評価方法: [Microsoft SkillOpt](https://github.com/microsoft/skillopt)
- 差分レビューとアンカー設定: [diff-viewer](https://github.com/SSsan-00/diff-viewer)

## ライセンス

このリポジトリの内容には[MIT License](LICENSE)を適用します。社内で使う場合、変更内容を公開する義務はありません。ソフトウェアを複製して社内配布する場合は、コピーに著作権表示とMITライセンス本文を含めてください。

MITライセンスは利用者が用意するPHP/C#ソース、移行ルール、社内スクリプトには適用されません。
