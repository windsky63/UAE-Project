@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"
python main.py drawing_briefing %*
popd
pause
