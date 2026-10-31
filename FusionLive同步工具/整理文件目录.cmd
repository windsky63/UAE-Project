@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "MAINT_RESULT=1"
pushd "%~dp0"
if errorlevel 1 goto bad_folder
if not exist "runtime" mkdir "runtime" 2>nul
set "MAINT_PYTHON=%~dp0..\.venv-analysis\Scripts\python.exe"
if not exist "%MAINT_PYTHON%" goto missing_python
if not exist "%~dp0app\path_maintenance_launcher.py" goto missing_launcher
"%MAINT_PYTHON%" -u "%~dp0app\path_maintenance_launcher.py" %*
set "MAINT_RESULT=%ERRORLEVEL%"
echo Exit code: %MAINT_RESULT%>>"runtime\path_maintenance_launcher.log"
goto finish
:missing_python
echo Shared Python environment was not found.
echo Run install_python312.cmd in the project root first.
echo ERROR: Shared Python environment was not found.>>"runtime\path_maintenance_launcher.log"
goto finish
:missing_launcher
echo Missing app\path_maintenance_launcher.py. Install the complete update package.
echo ERROR: Missing path_maintenance_launcher.py.>>"runtime\path_maintenance_launcher.log"
:finish
echo Startup log: runtime\path_maintenance_launcher.log
popd
if "%FUSION_MAINTENANCE_NO_PAUSE%"=="1" goto done
pause
:done
exit /b %MAINT_RESULT%
:bad_folder
echo Cannot open the tool directory.
if "%FUSION_MAINTENANCE_NO_PAUSE%"=="1" exit /b 1
pause
exit /b 1
