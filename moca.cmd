@echo off
setlocal

rem 呼び出し元のディレクトリによらず、このスクリプトに隣接する環境と設定を使う。
set "HARNESS=%~dp0.venv\Scripts\php-to-csharp-harness.exe"
set "CONFIG=%~dp0harness.local.toml"
if not exist "%HARNESS%" (
  echo ハーネスが未インストールです。次を実行してください:
  echo py -3 -m venv .venv
  echo .venv\Scripts\python -m pip install -e .
  exit /b 2
)
if not "%~1"=="" (
  echo Usage: moca
  exit /b 2
)
set "MOCA_LAUNCHER_BANNER=1"
"%HARNESS%" dashboard --config "%CONFIG%" --open-browser
exit /b %errorlevel%
