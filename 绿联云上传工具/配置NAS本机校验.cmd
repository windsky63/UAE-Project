@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0"
if errorlevel 1 exit /b 1
set "NAS_SETUP_PYTHON=python"
if exist "%~dp0..\.venv-analysis\Scripts\python.exe" set "NAS_SETUP_PYTHON=%~dp0..\.venv-analysis\Scripts\python.exe"
"%NAS_SETUP_PYTHON%" -u "%~dp0setup_nas_ssh.py" %* <CONIN$ >CONOUT$ 2>&1
set "NAS_SETUP_EXIT=%ERRORLEVEL%"
popd
pause
exit /b %NAS_SETUP_EXIT%
