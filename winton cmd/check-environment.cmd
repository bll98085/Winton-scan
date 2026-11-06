@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\check_environment.ps1" %*
set "WINTON_EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %WINTON_EXIT_CODE%
