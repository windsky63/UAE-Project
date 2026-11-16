@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
pushd "%~dp0"
echo 正在请求安全退出整个工具，请等待下载收尾、注销和后台分析当前文件处理完成。
python -u "%~dp0main.py" scheduler --stop
set result=%errorlevel%
popd
pause
exit /b %result%
