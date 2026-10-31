@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0"
if errorlevel 1 exit /b 1
python -u "%~dp0main.py" check %*
set "UPLOAD_EXIT=%ERRORLEVEL%"
popd
pause
exit /b %UPLOAD_EXIT%
