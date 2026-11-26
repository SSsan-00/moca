# Windowsでの長時間運転試験 (#322)

Windows 11の実機で、同じダッシュボードを保ったまま合成Runを5件ずつ処理する。各回で実Windows Jobに5組の所有ランチャーと子を同時に入れ、1件だけ停止し、その組が終了するまで他を解放しない。別の1件は代替Junieの通信エラーで保留し、停止したRunとともに明示的に再開する。他のRunに影響を与えず全件が最後まで通ること、SSEの切断・再接続で対象Runの新しいイベントが届くことを確認する。

この試験は実行開始と終了を繰り返す検査であり、保存境界で外部からプロセスを強制終了する [#305の異常試験](../../docs/recovery-testing.md)とは別。Junieの認証・通信・再試行、実.NETビルド、業務PHP・C#、DBは使わない。通信失敗は本番のエラー型をワークフロー境界で注入し、Git worktree、ダッシュボードHTTP、Windows Job、Run保存は実際に使う。

## 実行する

依存をインストール済みのハーネスのフォルダで、既存の Windows Python 3.11 または 3.13 とGitを使う。CIの通常回帰からは実行しない。まず短い確認を行い、次に他の性能試験と重ならない時間帯で長時間版を実行する。

```powershell
$env:PYTHONPATH = 'src;tests'
$python = '.\.venv\Scripts\python.exe'
& $python evaluations/windows-endurance/soak.py --seconds 120 --max-cycles 2
& $python evaluations/windows-endurance/soak.py --seconds 600 --max-cycles 100
```

指定秒数または回数に達したら、進行中の1波を終えて停止する（どちらか早い方）。所有したWindows Jobの外側から、初期化・最後の1波・後片付けに備えた全体上限（指定秒数＋180秒）もかける。各待機に上限があり、エラー時は非ゼロで終了する。対象ファイルは新規の一時フォルダでだけ生成する。期限を超えた場合に止めるのも試験のJob内のプロセスだけで、業務や他のJunieプロセスを探したり終了したりしない。

## 結果の読み方

- `SOAK_SAMPLE` は各波のピークではなく、全5組起動中と、Runがすべて落ち着いた直後のサンプル。ダッシュボードのWindowsハンドル数、Pythonスレッド数、ワーキングセット・コミット済みプライベートバイト、Run・worktree・ログや残された一時ファイルを記録する。起動中ハンドルには10個の試験用監視ハンドルを含む。
- テスト用の2プロセスは動いている時点でハンドルを取得し、完了・停止後に終了を確認する。PIDの名前検索や時間差での生存推定は使わない。再開用プロセスの終了は通常の同期実行の完了確認で検査する。
- 移行記録、Git worktree、保存ログはRunの数とともに増えてよい。SSE接続や作業スレッド、Jobハンドルの増加が落ち着いた後も戻らない場合は調査する。Pythonアロケーター・生存中のRun表示・保持対象ファイルと混同しない。
- `SOAK_RESULT` は全体時間、波数、開始前とサーバー終了後にスレッドが落ち着いた時点の資源、合成作業領域が片付いたかを記録する。再現報告にはGit SHA、Windows・Python・Gitの版、波数と実測時間、利用した代替処理を添える。Windows 3.11や実Junieで未実施なら「未検証」と記載する。

Windows API: [GetProcessHandleCount](https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesshandlecount)、[GetProcessMemoryInfo](https://learn.microsoft.com/windows/win32/api/psapi/nf-psapi-getprocessmemoryinfo)。導入と更新は [Windows向けの版確認・復旧手順](../../docs/windows-release.md)。

## 実機記録（2026-11-27 JST）

Windows 11（10.0.22000、4論理CPU）、Python 3.13.17、Git 2.53.0.windows.3。製品コード `91074035aa512b57e2b661e3c6aee5d49e44fc03`、試験コード `3ce2492` の `soak.py`（SHA-256 `a2be4dac310f37673ad7ff08f1fdbc040f6b5ad81baefad55226edae5dcbea0d`）。2波の短い確認を12.959秒で完了後、`--seconds 600 --max-cycles 100` を実施。最後の波と終了処理を含め615.691秒、76波・380 Runを完了。各波で1件を停止・1件を通信失敗とし、2件とも再開して完了した。所有ランチャー・子の終了を計760件確認し、残った一時JSONは0件。専用の一時作業領域は終了時に削除された。

| 指標 | 開始前 | 5組起動中の範囲 | 各波アイドルの範囲 | サーバー終了後 |
|---|---:|---:|---:|---:|
| Windowsハンドル数 | 132 | 233–244 | 204–215 | 197 |
| Pythonスレッド数 | 1 | 8 | 3–4 | 1 |
| ワーキングセット | 34.9 MiB | 40.2–105.2 MiB | 43.7–105.4 MiB | 105.3 MiB |
| プライベートバイト | 25.9 MiB | 28.7–94.1 MiB | 31.4–94.3 MiB | 94.1 MiB |

最終波の保存は `runs` 5,756,657バイト（合成ログ20,520バイトを含む）、380 worktree 68,400バイト。各波で取得したプロセスハンドルは毎回閉じ、波ごとにハンドルとスレッドが積み上がる傾向はこの試行では見られなかった。プロセス初期値と終了値の差は未切り分け。メモリは画面が完了済みRunも保持する状況で増え、終了時の計測でも試験コードが `RunManager` への参照を保持している。一定件数での長期運転・メモリ解放の検証は行っておらず、リークなしとは結論しない。

Windows/Python 3.11、認証済みJunie、実.NETビルド、電源断や数時間以上の運転はこの記録の対象外。wheelのビルド・配布と必須CIは公開候補の検証として別に残す。
