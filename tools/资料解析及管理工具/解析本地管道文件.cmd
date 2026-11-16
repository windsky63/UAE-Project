@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"
python main.py analysis_worker --watch --resume %*
popd
pause
