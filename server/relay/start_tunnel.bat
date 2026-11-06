@echo off
rem Start the RunnerCoach tunnel client (keeps running in this window).
rem Usage: start_tunnel.bat "<share link with ?sharecode=...>"
chcp 65001 >nul 2>&1
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: start_tunnel.bat "https://xxx.workbuddy.host/?sharecode=XXXX"
  exit /b 1
)
python "%~dp0..\..\tools\relay_client.py" "%~1" http://127.0.0.1:8300
