@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
pushd "%~dp0"
if errorlevel 1 goto path_error
if not exist "admin_launcher.py" goto missing_files
if exist ".venv-analysis\Scripts\python.exe" goto use_venv
py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 goto use_py
python -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 goto use_python
echo Python 3.11 or newer was not found. Install Python or restore .venv-analysis.
set "ADMIN_EXIT_CODE=1"
goto cleanup
:use_venv
".venv-analysis\Scripts\python.exe" -u "admin_launcher.py" --lan %*
goto finished
:use_py
py -3 -u "admin_launcher.py" --lan %*
goto finished
:use_python
python -u "admin_launcher.py" --lan %*
:finished
set "ADMIN_EXIT_CODE=%errorlevel%"
if not "%ADMIN_EXIT_CODE%"=="0" echo Management center failed. See the error above and runtime/launcher.log.
goto cleanup
:missing_files
echo Missing admin_launcher.py. Extract the COMPLETE update package into the project root.
set "ADMIN_EXIT_CODE=1"
:cleanup
popd
if not defined ADMIN_NO_PAUSE pause
exit /b %ADMIN_EXIT_CODE%
:path_error
echo Cannot open the project directory. Check that the project path exists and is accessible.
if not defined ADMIN_NO_PAUSE pause
exit /b 1
