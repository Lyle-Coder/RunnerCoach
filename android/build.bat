@echo off
rem One-click APK build. Keep this file ASCII-only: cmd.exe reads .bat in the
rem OEM codepage and UTF-8 Chinese comments break parsing on a GBK console.
rem
rem   build.bat                  -> debug apk
rem   build.bat assembleRelease  -> release apk
rem
rem Toolchain resolution (first match wins):
rem   1) android/toolchain.local.bat  (optional, git-ignored, for local paths)
rem   2) ANDROID_TOOLCHAIN env var    -> <dir>/jdk-*, <dir>/gradle-*, <dir>/sdk
rem   3) system JAVA_HOME / ANDROID_HOME / gradle on PATH
rem
rem Note: avoid parenthesised blocks here. A system JAVA_HOME such as
rem "C:\Program Files (x86)\Java\..." breaks cmd parsing inside (...).
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

if exist "%~dp0toolchain.local.bat" call "%~dp0toolchain.local.bat"

if not defined ANDROID_TOOLCHAIN goto :resolve
rem An explicitly configured portable toolchain always overrides the system JDK.
for /d %%D in ("%ANDROID_TOOLCHAIN%\jdk-*") do set "JAVA_HOME=%%D"
for /d %%D in ("%ANDROID_TOOLCHAIN%\gradle-*") do set "GRADLE=%%D\bin\gradle.bat"
set "ANDROID_HOME=%ANDROID_TOOLCHAIN%\sdk"
set "ANDROID_SDK_ROOT=%ANDROID_TOOLCHAIN%\sdk"

:resolve
if not defined JAVA_HOME goto :nojdk
if not exist "%JAVA_HOME%\bin\java.exe" goto :nojdk
if not defined GRADLE set "GRADLE=gradle"
if "%GRADLE%"=="gradle" goto :ready
if not exist "%GRADLE%" goto :nogradle

:ready
echo [1/2] staging python sources...
call sync_python.bat
if errorlevel 1 exit /b 1

set "TASK=%~1"
if "%TASK%"=="" set "TASK=assembleDebug"

echo [2/2] building %TASK% ...
call "%GRADLE%" --no-daemon --console=plain %TASK%
if errorlevel 1 exit /b 1

echo.
echo APK:
if exist "app\build\outputs\apk\debug\app-debug.apk" echo   app\build\outputs\apk\debug\app-debug.apk
if exist "app\build\outputs\apk\release\app-release.apk" echo   app\build\outputs\apk\release\app-release.apk
goto :eof

:nojdk
echo [x] JDK 17 not found. Set ANDROID_TOOLCHAIN or JAVA_HOME first.
echo     See android\README.md, section "before you build".
exit /b 1

:nogradle
echo [x] Gradle not found. Set ANDROID_TOOLCHAIN or put gradle on PATH.
exit /b 1
