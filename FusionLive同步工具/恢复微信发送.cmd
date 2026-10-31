@echo off
chcp 65001 >nul
setlocal
if not exist "%~dp0..\.venv-analysis\Scripts\python.exe" (
  echo Shared Python environment not found. Run install_python312.cmd first.
  pause
  exit /b 1
)
set PYTHONUTF8=1
"%~dp0..\.venv-analysis\Scripts\python.exe" "%~dp0wechat_push\desktop_guard.py" --resume
if errorlevel 1 pause
