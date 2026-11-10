@echo off
rem Double-click to sign in. Same as "run.bat login" in a terminal.
rem
rem It opens a real browser: Garmin moved the CN web login to a portal SSO
rem behind Cloudflare, so the password can no longer be posted by a plain
rem HTTP client. You sign in once in that window; the cookies land in
rem data/session.json. See garmin_cn/browser_login.py for the details.
chcp 65001 >nul 2>&1
cd /d "%~dp0"
call "%~dp0run.bat" login %*
set "RC=%errorlevel%"
echo.
if "%~1"=="" (
    echo [i] 窗口停在这里是在等按键，不是卡死。按任意键关闭。
    pause >nul
)
exit /b %RC%
