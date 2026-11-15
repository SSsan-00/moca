@echo off
setlocal

rem 呼び出し元のディレクトリによらず、このスクリプトに隣接する環境と設定を使う。
set "HARNESS=%~dp0.venv\Scripts\php-to-csharp-harness.exe"
set "CONFIG=%~dp0harness.local.toml"
set "ACTION=%~1"

if /I "%ACTION%"=="p" set "ACTION=prepare"
if /I "%ACTION%"=="r" set "ACTION=run"
if /I "%ACTION%"=="s" set "ACTION=resume"
if /I "%ACTION%"=="v" set "ACTION=view"

if not exist "%HARNESS%" (
  echo ハーネスが未インストールです。次を実行してください:
  echo py -3.11 -m venv .venv
  echo .venv\Scripts\python -m pip install -e .
  exit /b 2
)
if not exist "%CONFIG%" (
  echo harness.local.tomlがありません。harness.example.tomlをコピーして設定してください。
  exit /b 2
)

if /I "%ACTION%"=="prepare" goto unit
if /I "%ACTION%"=="run" goto unit
if /I "%ACTION%"=="resume" goto saved
if /I "%ACTION%"=="view" goto saved
goto usage

:unit
if "%~2"=="" goto usage
"%HARNESS%" %ACTION% --config "%CONFIG%" --unit "%~2"
exit /b %errorlevel%

:saved
rem CLIには小文字のコマンド名を渡し、Run IDがなければ対話で受け取る。
if /I "%ACTION%"=="resume" set "ACTION=resume"
if /I "%ACTION%"=="view" set "ACTION=view"
set "RUN_ID=%~2"
if not defined RUN_ID set /p "RUN_ID=Run IDを入力: "
if not defined RUN_ID exit /b 2
"%HARNESS%" %ACTION% --config "%CONFIG%" --run-id "%RUN_ID%"
exit /b %errorlevel%

:usage
echo 使い方: h.cmd p ^| r ^<unit-id^> ^| s [run-id] ^| v [run-id]
echo   p  移行準備
echo   r  Junieで移行
echo   s  中断した実行を再開。Run IDを省くと入力を求める
echo   v  保存済みの実行を表示。Run IDを省くと入力を求める
exit /b 2
