@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0"
if errorlevel 1 exit /b 1
python "%~dp0main.py" preflight --gui --wechat
set "CHECK_EXIT=%ERRORLEVEL%"
if not "%CHECK_EXIT%"=="0" goto check_failed
python "%~dp0main.py" push_setup
set "CHECK_EXIT=%ERRORLEVEL%"
if not "%CHECK_EXIT%"=="0" goto setup_cancelled
python -u "%~dp0main.py" scheduler %*
set "LAUNCH_EXIT=%ERRORLEVEL%"
popd
pause
exit /b %LAUNCH_EXIT%
:setup_cancelled
echo Startup cancelled. No download or WeChat send was started.
popd
exit /b %CHECK_EXIT%
:check_failed
echo Startup check failed. Scheduler was NOT started. Review the errors above.
popd
pause
exit /b %CHECK_EXIT%
