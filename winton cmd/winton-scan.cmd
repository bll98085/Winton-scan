@echo off
setlocal EnableExtensions
title Winton Scan

rem PDF source folder (PDF filenames are discovered at run time).
set "WINTON_BASE=C:\Users\Eagle\Desktop\Winton-scan"

rem Check the requested source folder before starting anything.
if not exist "%WINTON_BASE%\" (
    echo [ERROR] PDF source folder does not exist:
    echo "%WINTON_BASE%"
    goto failed
)

rem Find the original scanner: beside this CMD, in CODEX_HOME, or in user profile.
set "WINTON_PS1="
if exist "%~dp0scripts\start_winton_scan.ps1" set "WINTON_PS1=%~dp0scripts\start_winton_scan.ps1"
if not defined WINTON_PS1 (
    if defined CODEX_HOME (
        if exist "%CODEX_HOME%\skills\winton-scan\scripts\start_winton_scan.ps1" set "WINTON_PS1=%CODEX_HOME%\skills\winton-scan\scripts\start_winton_scan.ps1"
    )
)
if not defined WINTON_PS1 (
    if exist "%USERPROFILE%\.codex\skills\winton-scan\scripts\start_winton_scan.ps1" set "WINTON_PS1=%USERPROFILE%\.codex\skills\winton-scan\scripts\start_winton_scan.ps1"
)
if not defined WINTON_PS1 (
    echo [ERROR] Original scanner was not found:
    echo scripts\start_winton_scan.ps1
    echo Check this CMD folder or the Codex skill installation.
    goto failed
)

echo.
echo PDF source folder: "%WINTON_BASE%"
echo PDF files found now:
dir /b /a:-d "%WINTON_BASE%\*.pdf" 2>nul
if errorlevel 1 echo [INFO] No PDF file in source folder. Existing scanner state may still resume.
echo.

rem Launch the ORIGINAL scanner with the requested source folder explicitly.
rem No PDFs are renamed, moved, or deleted by this CMD.
pushd "%WINTON_BASE%"
if errorlevel 1 (
    echo [ERROR] Cannot enter PDF source folder.
    goto failed
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%WINTON_PS1%" -ScanDir "%WINTON_BASE%" %*
set "WINTON_EXIT_CODE=%ERRORLEVEL%"
popd

echo.
if "%WINTON_EXIT_CODE%"=="0" (
    echo [INFO] Original scanner returned success.
) else (
    echo [ERROR] Original scanner returned exit code %WINTON_EXIT_CODE%.
)
echo Press any key to close this window.
pause >nul
exit /b %WINTON_EXIT_CODE%

:failed
echo.
echo No scanning was started.
echo Press any key to close this window.
pause >nul
exit /b 2
