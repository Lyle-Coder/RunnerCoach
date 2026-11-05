@echo off
rem Windows version of sync_python.sh: copy ..\garmin_cn into the app source set.
rem Run before every build (Gradle does not watch the parent directory).
rem ASCII only: cmd.exe parses .bat files in the OEM codepage.
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

if exist "app\src\main\python\garmin_cn" rmdir /s /q "app\src\main\python\garmin_cn"
if not exist "app\src\main\python" mkdir "app\src\main\python"

xcopy "..\garmin_cn" "app\src\main\python\garmin_cn\" /E /I /Y /Q >nul
if errorlevel 1 (
    echo [x] copy failed
    exit /b 1
)

for /d /r "app\src\main\python" %%D in (__pycache__) do @if exist "%%D" rmdir /s /q "%%D"

set N=0
for /f %%F in ('dir /b /s "app\src\main\python\*.py" 2^>nul') do set /a N+=1
echo   staged %N% .py files
