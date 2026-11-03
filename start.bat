@echo off
setlocal
chcp 65001 >nul
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
set "PYTHONPATH="
set "PYTHON_EXE="
if not "%~1"=="" set "PYTHON_EXE=%~f1"
if not defined PYTHON_EXE if exist "%~dp0envs\venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0envs\venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%~dp0envs\python\python.exe" set "PYTHON_EXE=%~dp0envs\python\python.exe"
if not defined PYTHON_EXE (
    echo No local environment. Preparing one now...
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_env.ps1"
    if errorlevel 1 goto failed
    set "PYTHON_EXE=%~dp0envs\python\python.exe"
)
"%PYTHON_EXE%" -s -B "%~dp0fish.py"
if errorlevel 1 goto failed
exit /b 0
:failed
echo Launch failed. Read the error above.
pause
exit /b 1
