@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
pushd "%~dp0"
python -u main.py scheduler --force-stop
set "STOP_EXIT=%ERRORLEVEL%"
popd
pause
exit /b %STOP_EXIT%
