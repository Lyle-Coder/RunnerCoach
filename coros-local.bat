@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"

rem 账号密码**不写在这个文件里**。这个文件在仓库里，而仓库有公开镜像（Gitee、
rem GitHub），写进来就等于把账号口令公开——2026-11 已经发生过一次。
rem
rem 用法：同目录新建 coros-local.private.bat（已在 .gitignore），写两行：
rem     set "COROS_USER=you@example.com"
rem     set "COROS_PASSWORD=your-password"
rem 口令里有 percent 号要写成两个，整行用双引号包住，其余字符照原样。
set "COROS_USER=YOUR_COROS_EMAIL"
set "COROS_PASSWORD=YOUR_COROS_PASSWORD"

if exist "coros-local.private.bat" call "coros-local.private.bat"

if "%COROS_USER%"=="YOUR_COROS_EMAIL" (
    echo 先把账号密码写进 coros-local.private.bat，模板见本文件开头的注释。
    exit /b 1
)

rem Pass credentials through environment variables to avoid CALL expansion.
py -3 -c "import os, runpy, sys; sys.argv = ['cli.py', 'coros', 'login', '--user', os.environ['COROS_USER'], '--password', os.environ['COROS_PASSWORD']]; runpy.run_path('cli.py', run_name='__main__')"
if errorlevel 1 exit /b 1
call run.bat coros sync --days 30
if errorlevel 1 exit /b 1
call run.bat report --open
exit /b %errorlevel%
