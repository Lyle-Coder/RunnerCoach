@echo off
rem ============================================================
rem  Garmin dashboard launcher - picks a working Python.
rem
rem  Why this file exists:
rem  "python" in PATH points to a 0-byte Microsoft Store stub
rem  (WindowsApps\python.exe). It prints nothing and exits 9009,
rem  so "python cli.py login" looks like it does nothing at all.
rem
rem  Usage:
rem    run.bat login               sign in
rem    run.bat sync --days 30      pull Garmin data + Xiaomi scale (--no-body to skip)
rem    run.bat report --open       build dashboard
rem    run.bat card --open         build vertical one-pager PNG
rem    run.bat brief               last run as a WeChat-ready text (+ card PNG)
rem    run.bat watch --hours 3     check for a newly finished run, print brief
rem    run.bat body                weight / body composition trend
rem    run.bat body --import f.csv import Mi Fitness or Zepp Life export
rem    run.bat body --mi           pull scale data from Xiaomi health cloud
rem    run.bat nutri               how much to eat today (kcal + macros)
rem    run.bat ingest              photos/videos in data/inbox -> video
rem    run.bat motion --date 2026-11-03   animated GPS replay (1080x1920 html)
rem    run.bat motion --id 658100399 --mp4  same, plus MP4 (needs ffmpeg)
rem    run.bat motion --mp4 --keep-frames   keep the per-frame jpgs (~450 MB)
rem    run.bat motion --mp4 --mute          drop the live-clip audio
rem    run.bat photo --id 658100399 check photo EXIF / GPS before replay
rem    run.bat mi login            sign in to Xiaomi account (handles SMS 2FA)
rem    run.bat mi login --json-file mi_login.json   import browser-pasted login JSON
rem    run.bat doctor              environment self-check
rem    run.bat key --set KEY       save Amap static-map key (Web service)
rem    run.bat                     no args = status
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
pushd "%~dp0"

set "PYCMD="
for %%C in (
    "py -3"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "python"
) do (
    if not defined PYCMD (
        %%~C -c "import sys" >nul 2>&1
        if not errorlevel 1 set "PYCMD=%%~C"
    )
)

if not defined PYCMD (
    echo [x] No usable Python interpreter found.
    echo     Install Python 3.11+ or edit PYCMD in this file.
    popd
    exit /b 9009
)

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if "%~1"=="" (
    %PYCMD% cli.py status
    set "RC=!errorlevel!"
) else (
    %PYCMD% cli.py %*
    set "RC=!errorlevel!"
)

popd
exit /b !RC!
