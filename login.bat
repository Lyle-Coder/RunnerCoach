@echo off
rem Double-click to sign in. Same as "run.bat login" in a terminal.
cd /d "%~dp0"
call "%~dp0run.bat" login %*
echo.
if "%~1"=="" pause
