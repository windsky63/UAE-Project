@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "PYTHONHOME="
set "PYTHONPATH="
pushd "%~dp0"
if errorlevel 1 exit /b 1
set "ADMIN_DIR="
for /d %%D in ("%~dp0*") do if exist "%%~fD\project_layout.py" set "ADMIN_DIR=%%~fD"
if not defined ADMIN_DIR goto missing
if exist ".venv-analysis\Scripts\python.exe" goto use_venv
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 goto use_py312
py -3 -c "import sys; raise SystemExit(sys.version_info < (3,12))" >nul 2>&1
if not errorlevel 1 goto use_py
python -c "import sys; raise SystemExit(sys.version_info < (3,12))" >nul 2>&1
if not errorlevel 1 goto use_python
echo Python 3.12 or newer is required.
set "ADMIN_EXIT_CODE=1"
goto cleanup
:use_venv
".venv-analysis\Scripts\python.exe" -X utf8 -u "%ADMIN_DIR%\tools\project_integrity.py" %*
goto finished
:use_py312
py -3.12 -X utf8 -u "%ADMIN_DIR%\tools\project_integrity.py" %*
goto finished
:use_py
py -3 -X utf8 -u "%ADMIN_DIR%\tools\project_integrity.py" %*
goto finished
:use_python
python -X utf8 -u "%ADMIN_DIR%\tools\project_integrity.py" %*
:finished
set "ADMIN_EXIT_CODE=%errorlevel%"
goto cleanup
:missing
echo Missing management centre. Extract the complete deployment package.
set "ADMIN_EXIT_CODE=1"
:cleanup
popd
if not defined ADMIN_NO_PAUSE pause
exit /b %ADMIN_EXIT_CODE%
