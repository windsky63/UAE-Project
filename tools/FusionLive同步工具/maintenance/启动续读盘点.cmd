@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0.."
if errorlevel 1 exit /b 1
python main.py preflight
set "CHECK_EXIT=%ERRORLEVEL%"
if not "%CHECK_EXIT%"=="0" goto check_failed
python -u main.py scheduler --resume-now %*
set "LAUNCH_EXIT=%ERRORLEVEL%"
popd
pause
exit /b %LAUNCH_EXIT%
:check_failed
echo Startup check failed. Scheduler was NOT started. Review the errors above.
popd
pause
exit /b %CHECK_EXIT%
