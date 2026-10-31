@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0.."
python -B main.py analysis_worker --watch --resume %*
if errorlevel 1 echo 本地分析失败，请查看以上错误信息。
popd
pause
