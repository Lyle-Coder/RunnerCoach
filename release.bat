@echo off
rem RunnerCoach release helper. Keep ASCII-only: cmd.exe reads .bat in the
rem OEM codepage and UTF-8 Chinese comments break parsing on a GBK console.
rem
rem   release.bat status
rem   release.bat check
rem   release.bat prepare
rem   release.bat tag --push
rem   release.bat upload --tag v0.1.4 --dry-run
setlocal
chcp 65001 >nul 2>&1
pushd "%~dp0"

set "PYCMD="
for %%C in (
    "py -3"
    "python"
    "python3"
) do (
    if not defined PYCMD (
        %%~C -c "import sys" >nul 2>&1
        if not errorlevel 1 set "PYCMD=%%~C"
    )
)

if not defined PYCMD (
    echo [x] No usable Python 3.11+ found.
    popd
    exit /b 9009
)

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
%PYCMD% tools\release.py %*
set "RC=%errorlevel%"
popd
exit /b %RC%
