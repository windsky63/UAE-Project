@echo off
setlocal
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0"
if errorlevel 1 exit /b 1
py -3.12 -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>&1
if not errorlevel 1 goto use_py
python -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>&1
if not errorlevel 1 goto use_python
echo Python 3.12 was not found. Install it with pip, venv and Tcl/Tk enabled.
set "SETUP_RESULT=1"
goto finished
:use_py
py -3.12 -u "%~dp0setup_python312.py"
set "SETUP_RESULT=%errorlevel%"
goto finished
:use_python
python -u "%~dp0setup_python312.py"
set "SETUP_RESULT=%errorlevel%"
:finished
if not "%SETUP_RESULT%"=="0" echo Setup failed. See the error above.
popd
pause
exit /b %SETUP_RESULT%
