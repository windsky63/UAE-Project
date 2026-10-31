@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0.."
if errorlevel 1 exit /b 1
python -m pip install -r requirements.txt
if errorlevel 1 goto failed
python -B -c "import requests, sqlite3, tkinter, uiautomation, win32clipboard, win32gui, psutil, pyperclip, py7zr; tkinter.Tcl(); print('Dependencies OK')"
if errorlevel 1 goto failed
echo Installation complete.
popd
pause
exit /b 0
:failed
echo Installation failed. Check Python, pip, network, and Tcl/Tk.
echo If Tcl/Tk cannot initialize, repair Python with the Tcl/Tk component selected.
popd
pause
exit /b 1
