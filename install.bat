@echo off
title DRDO PXE — ADXL345 DAQ System Installer v2.0
color 0A

echo.
echo =============================================================
echo   DRDO PXE — ADXL345 REAL-TIME ACCELERATION DAQ SYSTEM
echo   Installer Script v2.0
echo   Defence Research and Development Organisation — PXE
echo =============================================================
echo.

:: ── Check Python availability ────────────────────────────────────────────────
echo [1/5]  Checking Python installation...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ERROR: Python not found in PATH.
    echo  Please install Python 3.10 or later from https://www.python.org
    echo  Ensure "Add Python to PATH" is checked during installation.
    echo.
    pause
    exit /b 1
)
python --version
echo  Python OK.
echo.

:: ── Upgrade pip ─────────────────────────────────────────────────────────────
echo [2/5]  Upgrading pip...
python -m pip install --upgrade pip --quiet
if errorlevel 1 (
    echo  WARNING: pip upgrade failed. Continuing with current version.
) else (
    echo  pip upgrade OK.
)
echo.

:: ── Install dependencies ─────────────────────────────────────────────────────
echo [3/5]  Installing Python dependencies...
echo.
echo  Packages to install:
echo    PyQt6          ^>=6.7.0   — GUI framework
echo    pyqtgraph      ^>=0.13.7  — Real-time plots
echo    numpy          ^>=1.26.0  — Numerical arrays
echo    scipy          ^>=1.12.0  — Vectorised EMA filter (plot_manager)
echo.
echo  NOTE: pyserial is NOT required (UDP uses Python built-in socket).
echo  NOTE: pandas is NOT required (CSV export uses the standard csv module).
echo.

python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo  ERROR: Dependency installation failed.
    echo  Check your network connection and try again.
    echo.
    pause
    exit /b 1
)
echo.
echo  All dependencies installed successfully.
echo.

:: ── Verify key imports ────────────────────────────────────────────────────────
echo [4/5]  Verifying imports...
python -c "import PyQt6; import pyqtgraph; import numpy; import scipy; print('  All imports OK.')"
if errorlevel 1 (
    echo  WARNING: Import verification failed. Try running main.py manually.
) else (
    echo  Import verification passed.
)
echo.

:: ── Create launch shortcut hint ───────────────────────────────────────────────
echo [5/5]  Setup complete.
echo.
echo =============================================================
echo   INSTALLATION COMPLETE
echo =============================================================
echo.
echo  To start the application:
echo    Option A:  Double-click  launch.bat
echo    Option B:  python main.py
echo.
echo  Hardware setup:
echo    - ADXL345 sensor connected to Arduino / ESP32
echo    - Arduino sends UDP packets to this PC on port 8888
echo    - Packet format: struct { int16_t x; int16_t y; int16_t z; }
echo      (values pre-scaled by 100, little-endian)
echo    - In the GUI: click CONNECT UDP in the left sidebar
echo.
echo  Keyboard shortcuts:
echo    Space    — Toggle START / STOP acquisition
echo    Ctrl+S   — Save snapshot CSV
echo    Ctrl+L   — Clear buffer and graphs
echo.
pause
