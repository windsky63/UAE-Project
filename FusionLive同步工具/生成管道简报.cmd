@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"
python main.py briefing %*
if errorlevel 1 echo 简报生成失败，请查看以上错误信息。
popd
pause
