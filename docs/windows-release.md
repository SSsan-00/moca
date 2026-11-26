# MoCAの版確認・更新・ロールバック（Windows）

## 今使っている版を記録する

MoCAの配布単位はPythonパッケージ `php-to-csharp-migration-harness` です。Gitから導入する現在の手順では、同じパッケージ版でもコミットが異なる場合があります。ハーネスを置いたフォルダで **パッケージ版とコミットSHAの両方** を確認します。

```powershell
.\.venv\Scripts\python.exe -c "from importlib.metadata import version; print(version('php-to-csharp-migration-harness'))"
git rev-parse HEAD
git status --short
```

- **安定版**: 必須CIと実機確認が終わり、所有者がGitHub Releasesで公開した `vX.Y.Z`。タグと `pyproject.toml` の版を一致させる。タグ公開後に同じ版の内容を差し替えず、修正版は新しい版にする。
- **開発版**: `main` 上の特定SHA、または明示的なプレリリース。運用品質の保証は安定版と分ける。公開済み安定版がない場合も、業務端末には確認済みSHAを固定し、無条件で `main` を追従しない。
- Windows向けで、既存のPython 3.11/3.13、Git、.NET 9 SDKを使う。Junieを実際に使うときだけ、同じWindowsユーザーでJunieを認証する。Node.jsを利用端末に追加する必要はない。ソース版の `.\moca` 起動と、wheelの `php-to-csharp-harness` エントリポイントは区別する。単体実行ファイルを配布しているという意味ではない。

## 更新前に保全する

1. ダッシュボードで未送信の下書きと指示を保存し、すべてのRunを完了または停止する。終了未確認と表示されたRunは、起動した端末で残存プロセスとログを確認し、自動再開しない。起動元のターミナルからハーネスを終了する。
2. 現版・SHA、設定ファイルとそこにある実際の `run_root`・`worktree_root`・アプリリポジトリを確認する。設定、Run保存先、編集済みworktreeを、会社の規則で認められたアクセス制御付き保存先に合わせて保全する。Gitのworktree管理情報は元のアプリリポジトリにもあるため、通常のディレクトリコピーだけで同じworktreeを再現できるとは限らない。復旧手順は同じパス構成の隔離環境で先に確認する。参照資料の位置と未反映の手編集も記録する。
3. 更新先のリリースノートで既存Runの互換性を確認する。不明な場合は、本番Runではなく合成サンプルで新しい版を確認する。設定にAPIキーを追加したり、Runを公開場所にアップロードしたりしない。

## 更新する

作業中の変更がない別のハーネス用チェックアウトに、承認済みタグまたはSHAを指定し、[Windows起動手順](windows-user-guide.md#1-ダッシュボードを起動する)に従って `.venv` と依存を準備する。同じ保存先を使う新旧ダッシュボードを同時に起動しない。開発版へ進める既存運用の `git pull --ff-only origin main` は `main` を追う選択で、安定版への更新ではない。

新版はまず設定を検証し、合成Runで進捗表示・保存・停止と再開を確認する。既存Runでは最初に読取りだけで記録・ログ・復旧状態を確認してから作業を再開する。保存形式の未知の版や壊れた記録は、書き換えて開くのではなく元の版で確認する（[保存形式の扱い](persisted-run-compatibility.md)）。

## 問題が起きたら

- 新版で処理を停止し、一時ファイルや記録を自己判断で削除しない。Run ID、正確な版とSHA、Python・Git・.NET・Junieの版、失敗工程、エラー、該当する合成例を記録する。業務コード、APIキー、認証・個人情報、今回のURLは共有しない。
- 旧版を別チェックアウトで起動する。新版で既存Runに書き込んでいない場合でも、互換性が不明なら保存済みの写しを使う。書込み後は、更新前にそろえた設定・Run・worktreeの組を隔離先で復元し、新版のデータを上書きしない。新旧で進めた変更は差分確認と人の判断を経て引き継ぐ。設定の相対パスも復元場所で確認する。
- 外部フックが呼び出すサービスや既に反映した成果物は、ハーネス本体の版を戻しても戻らない。[終了未確認と復旧](windows-user-guide.md#復旧の確認と結果不明のスクリプト)を確認し、必要な場合は別の新しいRunを起こす。

## メンテナー用のリリース前チェック

1. 対象コミット・版、互換性（旧Runの再開・未知／破損形式を上書きしない）、追加・削除機能、既知の制限、更新・ロールバック手順をリリースノートにまとめる。
2. WindowsのPython 3.11/3.13の必須CI、.NET 9を使う合成アプリ、[耐久試験](../evaluations/windows-endurance/README.md)、および認証済みJunieの確認状況を別々に記録する。CIのJunie起動確認だけを、ログインや移行成功の証拠にしない。
3. クリーンな固定コミットから、下記のように開発用Pythonでwheelをローカル生成する。比較する場合はPython、pip、ビルド環境のsetuptoolsの版を控える。同じSHAでも、時刻などが入る生成物のバイト一致までは保証しない。

   ```powershell
   $python = '.\.venv\Scripts\python.exe'
   & $python -m pip --version
   & $python -c "from importlib.metadata import version; print(version('setuptools'))"
   $output = Join-Path $env:TEMP ('moca-release-' + [guid]::NewGuid().ToString('N'))
   New-Item -ItemType Directory -Path $output | Out-Null
   & $python -m pip wheel --no-deps --no-build-isolation --wheel-dir $output .
   Get-ChildItem -LiteralPath $output -Filter '*.whl' | Get-FileHash -Algorithm SHA256
   ```

   開発側のビルド環境に `pyproject.toml` の `setuptools>=68` とwheelビルドを行えるpipを用意し、利用者の端末には追加しない。出力したwheelは新規仮想環境へインストールし、`php-to-csharp-harness --help`、静的アセット、設定例、ローカル起動を確認する。ソース版も別環境で `.\moca` の起動を確認する。ビルド環境・wheelまたはソースパッケージのSHA-256・試験結果を記録する。
   ソース配布を選ぶ場合は、手元の設定・Runを含めず、Gitに登録済みの固定版だけを `git archive --format=zip` で出力する。例:

   ```powershell
   $commit = git rev-parse HEAD
   $short = $commit.Substring(0, 12)
   $output = Join-Path $env:TEMP ('moca-source-' + [guid]::NewGuid().ToString('N'))
   New-Item -ItemType Directory -Path $output | Out-Null
   $archive = Join-Path $output "moca-$short.zip"
   git archive --format=zip "--prefix=moca-$short/" "--output=$archive" $commit
   Get-FileHash -LiteralPath $archive -Algorithm SHA256
   ```

   基準 `91074035aa512b57e2b661e3c6aee5d49e44fc03` のソースZIPは Git 2.50.0（macOS補助環境）で同一プレフィックス `moca-9107403/` を使って2回作成し、双方の SHA-256 は `83f0c48d65c5c529ce1269b5c08c9a2ab3d4f154e3231e11f182b9301d18a0ed`。起動用スクリプト・設定例・デモ・ライセンス・静的アセットの素材を確認し、ローカル設定・Run保存データを含まないことを確認した。wheel生成・新規環境へのインストールは別の確認で、この結果には含まない。
4. 公開は所有者の承認後。GitHub上で確認済みコミットにタグを付け、版とSHAが一致する成果物・ハッシュ・手順のみを公開する。秘密情報・設定・業務データ・未確認の成果物を添付しない。この文書や耐久試験から自動公開しない。
