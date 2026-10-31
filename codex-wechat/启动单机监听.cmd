@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0..\.venv-analysis\Scripts\python.exe" goto missing
if not exist "wechat.codex.json" goto missing
"%~dp0..\.venv-analysis\Scripts\python.exe" -m wechat_local --check
if errorlevel 1 goto failed
"%~dp0..\.venv-analysis\Scripts\python.exe" -m wechat_local
if errorlevel 1 goto failed
exit /b 0
:missing
echo Run the installer and configure wechat.codex.json first.
:failed
pause
exit /b 1
