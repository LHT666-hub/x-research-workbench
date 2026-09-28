@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "APP=%~dp0tools\x_keyword_ui.py"
if not exist "%PYTHON%" goto missing_python
"%PYTHON%" "%APP%"
echo.
echo X keyword post collector has stopped.
pause
exit /b 0

:missing_python
echo Python environment was not found:
echo %PYTHON%
pause
exit /b 1

