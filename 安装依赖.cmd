@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 goto missing_python

if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
if errorlevel 1 goto failed

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed

echo.
echo Installation completed. Double-click 打开X关键词帖子采集器.cmd to start.
pause
exit /b 0

:missing_python
echo Python 3 was not found. Install Python 3.11 or newer, then run this file again.
pause
exit /b 1

:failed
echo Installation failed. Review the error above.
pause
exit /b 1
