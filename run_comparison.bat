@echo off
setlocal

cd /d "%~dp0"

echo ============================================
echo  ADS-B Source Comparison - Setup + Run
echo ============================================
echo.

REM --- Find Python (try common commands in order) ---
python --version >nul 2>&1
if not errorlevel 1 (
    set PYTHON=python
    goto :deps
)

py --version >nul 2>&1
if not errorlevel 1 (
    set PYTHON=py
    goto :deps
)

REM --- Python not found: try winget ---
echo Python not found on PATH.
echo.
echo Attempting install via winget (Windows Package Manager)...
winget install --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements --silent
if errorlevel 1 (
    echo.
    echo winget failed or is not available on this machine.
    echo.
    echo Please install Python 3 manually:
    echo   1. Go to https://www.python.org/downloads/
    echo   2. Download and run the installer
    echo   3. Check "Add Python to PATH" on the first screen
    echo   4. Close this window and run again
    pause
    exit /b 1
)
echo.
echo Python installed successfully.
echo PATH changes require a new terminal session.
echo Close this window and run run_comparison.bat again.
pause
exit /b 0

:deps
echo Python: %PYTHON%
%PYTHON% --version
echo.

REM --- Check / install requests ---
%PYTHON% -c "import requests" >nul 2>&1
if not errorlevel 1 goto :run

echo Installing requests...
%PYTHON% -m pip install --quiet requests
if errorlevel 1 (
    echo.
    echo Failed to install requests. Check pip is working:
    echo   %PYTHON% -m pip --version
    pause
    exit /b 1
)
echo requests installed.
echo.

:run
echo Starting ADS-B comparison.
echo.
echo Output: comparison_output\comparison_log.csv
echo         comparison_output\raw\
echo.
echo Press Ctrl+C to stop.
echo.
%PYTHON% compare_adsb.py
echo.
echo Comparison ended.
pause
