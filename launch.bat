@echo off
title DRDO PXE — ADXL345 DAQ System v2.0
color 0A

echo.
echo =============================================================
echo   DRDO PXE — ADXL345 REAL-TIME ACCELERATION DAQ SYSTEM
echo   Launching v2.0 ...
echo =============================================================
echo.

:: Change to script directory so relative imports work
cd /d "%~dp0"

:: Quick check Python exists
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found. Run install.bat first.
    pause
    exit /b 1
)

echo  Starting GUI — close this window to stop the application.
echo.

python main.py

if errorlevel 1 (
    echo.
    echo  Application exited with an error.
    echo  Check the output above for details.
    pause
)
