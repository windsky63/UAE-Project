@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "TOOL_PYTHON=python"
if exist "%~dp0..\..\.venv-analysis\Scripts\python.exe" set "TOOL_PYTHON=%~dp0..\..\.venv-analysis\Scripts\python.exe"
"%TOOL_PYTHON%" "%~dp0wechat_local\desktop_guard.py" --resume
if errorlevel 1 pause
