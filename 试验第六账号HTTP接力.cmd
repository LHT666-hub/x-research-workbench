@echo off
setlocal
cd /d "%~dp0"
echo Running the isolated sixth-account HTTP handoff pilot.
echo Working directory: %CD%
echo Do not enter a password in this terminal. Complete login and 2FA in the visible Chrome window.
echo.
py -3 tools\x_browserless_handoff.py --keyword GPT --date 2026-01-01
echo.
echo Pilot exited with code %ERRORLEVEL%. The window will stay open so you can read the result.
pause
