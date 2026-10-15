@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing_python
".venv\Scripts\python.exe" "tools\x_doctor.py"
set "CHECK_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %CHECK_RESULT%
:missing_python
echo Run install dependencies first. See README.md.
pause
exit /b 1
