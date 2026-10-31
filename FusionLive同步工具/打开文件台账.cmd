@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0"
if errorlevel 1 exit /b 1
python "%~dp0main.py" preflight --gui
set "LAUNCH_EXIT=%ERRORLEVEL%"
if not "%LAUNCH_EXIT%"=="0" goto finished
python "%~dp0main.py" gui %*
set "LAUNCH_EXIT=%ERRORLEVEL%"
:finished
popd
if not "%LAUNCH_EXIT%"=="0" pause
exit /b %LAUNCH_EXIT%
