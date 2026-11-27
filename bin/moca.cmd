@echo off
setlocal DisableDelayedExpansion
if not "%~1"=="" (
  echo Usage: moca 1>&2
  exit /b 2
)
"%~dp0..\moca.cmd"
