@echo off
rem Sync + dashboard + one-pager in one go. Double-click friendly.
rem Order matters: sync also pulls the Xiaomi scale (weight / body fat / muscle /
rem water) and writes it back into the daily table, so report and card see it.
rem Keep comments ASCII-only: cmd reads this file as GBK and mangles UTF-8.
cd /d "%~dp0"
echo [1/3] syncing garmin (last 3 days) + xiaomi scale...
call "%~dp0run.bat" sync --days 3
if errorlevel 2 goto fail
echo.
echo [2/3] building dashboard...
call "%~dp0run.bat" report
if errorlevel 2 goto fail
echo.
echo [3/3] building one-pager card...
call "%~dp0run.bat" card
if errorlevel 2 echo   (card skipped - no Chrome/Edge found, dashboard is still fine)
echo.
echo done: output\latest.html   output\card.png
if "%~1"=="" pause
exit /b 0

:fail
echo.
echo failed - run "run.bat doctor" to check the environment.
if "%~1"=="" pause
exit /b 1
