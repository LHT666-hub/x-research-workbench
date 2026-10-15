@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "APP=%~dp0tools\x_keyword_ui.py"
if not exist "%PYTHON%" goto missing_python
"%PYTHON%" "%~dp0tools\x_doctor.py"
if errorlevel 1 goto check_failed
"%PYTHON%" "%APP%"
set "APP_RESULT=%ERRORLEVEL%"
echo.
echo X keyword post collector has stopped.
pause
exit /b %APP_RESULT%

:check_failed
echo Fix the environment checks above, then try again. See docs\TROUBLESHOOTING.md.
pause
exit /b 1

:missing_python
echo Python environment was not found:
echo %PYTHON%
pause
exit /b 1

