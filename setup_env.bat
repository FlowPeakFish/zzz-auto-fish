@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_env.ps1"
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" echo Setup failed. Read the error above and run this script again.
if /I not "%~1"=="--no-pause" pause
exit /b %RESULT%
