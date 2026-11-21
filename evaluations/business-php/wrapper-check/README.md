# 独自PHPラッパー呼び出しの静的チェッカー例

移行後の架空のDBラッパーを、Roslynで**ソースだけ**解析する例です。SDK付属のRoslynを使い、対象アセンブリの読み込み、PHPやC#業務メソッドの実行、DBへの接続は行いません。本プロジェクトを通常どおりビルドしたうえで、その後の静的チェッカーとして使用します。

規約 `WRAP001` は、指定したC#ソースに少なくとも1個の `Execute` 呼び出しがあり、すべてが固定した契約ソース上の `Synthetic.Legacy.PhpDb.Execute(long)` を呼ぶことを確認します。修飾名や型エイリアスを許容し、同名の別型・別オーバーロードは区別します。引数は暗黙変換の前の静的型が `long` でなければなりません。

- 終了コード `0`: 規約に適合。
- 終了コード `1`: 規約違反を確認。ハーネスが扱える `WRAP001 ファイル:行:列 error: 理由` 形式で出力。
- 終了コード `2`: 参照不足、動的な呼び出し、解析対象の欠落などで判定不能。位置・理由を `WRAP001 unknown` として出力し、合格にしない。違反と判定不能が混在する場合も終了コードは `2`。

.NET SDK 10で固定ケースを試せます。実行するのは静的チェッカーだけです。

```sh
python evaluations/business-php/wrapper-check/test_fixtures.py
```

ハーネスで試す場合は、このディレクトリを検証用worktreeに置き、チェッカーの作業ディレクトリから契約ソース・対象ソースへの相対パスを指定してください。以下はリポジトリのルートを作業ディレクトリにして合成の正例を解析する設定です。

```toml
[checker]
command = [
  "dotnet", "run", "--project", "evaluations/business-php/wrapper-check/WrapperCheck.csproj",
  "--verbosity", "quiet", "--",
  "evaluations/business-php/wrapper-check/fixtures/Contract.cs",
  "evaluations/business-php/wrapper-check/fixtures/Valid.cs",
]
```

実プロジェクト向けに拡張する際は、信頼した契約ソース、必要なコンパイル参照、対象範囲と呼び出し規約を明示してください。このサンプルは指定したファイルにある名前付きの直接呼び出しだけを検査します。委譲・リフレクション経由の呼び出しや実プロジェクトの参照は自動収集しません。PHPとの実行時等価性、クエリ実行結果、トランザクション・ロールバックは判定しません。
