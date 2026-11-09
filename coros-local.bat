@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"

rem Replace the placeholders below with your COROS email and password.
rem Keep this file private. For a literal percent sign, write %% in this file.
set "COROS_USER=1749836239@qq.com"
set "COROS_PASSWORD=Yy15155658416"

if "%COROS_USER%"=="YOUR_COROS_EMAIL" (
    echo Please edit coros-local.bat and fill in your COROS email and password first.
    exit /b 1
)

rem Pass credentials through environment variables to avoid CALL expansion.
py -3 -c "import os, runpy, sys; sys.argv = ['cli.py', 'coros', 'login', '--user', os.environ['COROS_USER'], '--password', os.environ['COROS_PASSWORD']]; runpy.run_path('cli.py', run_name='__main__')"
if errorlevel 1 exit /b 1
call run.bat coros sync --days 30
if errorlevel 1 exit /b 1
call run.bat report --open
exit /b %errorlevel%
