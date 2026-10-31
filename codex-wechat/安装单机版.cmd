@echo off
setlocal
cd /d "%~dp0"
call "%~dp0..\install_python312.cmd"
if errorlevel 1 goto failed
if not exist "wechat.codex.json" copy /y "wechat.codex.example.json" "wechat.codex.json" >nul
"%~dp0..\.venv-analysis\Scripts\python.exe" -m wechat_local --check
if errorlevel 1 goto configure
echo Configure wechat.codex.json, install Codex CLI and run codex login before listening.
pause
exit /b 0
:configure
echo Dependencies installed. Resolve the environment or configuration errors above before listening.
pause
exit /b 1
:failed
echo Installation failed.
pause
exit /b 1
