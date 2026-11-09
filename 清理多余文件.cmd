@echo off
setlocal
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0"
if errorlevel 1 exit /b 1
if not exist "%~dp0.venv-analysis\Scripts\python.exe" goto no_python
"%~dp0.venv-analysis\Scripts\python.exe" -B "%~dp0tools\cleanup_deployment.py" %*
set "CLEANUP_RESULT=%errorlevel%"
goto finished
:no_python
echo Shared Python environment missing. Run install_python312.cmd first.
set "CLEANUP_RESULT=1"
:finished
popd
if not defined PROJECT_CLEANUP_NO_PAUSE pause
exit /b %CLEANUP_RESULT%
