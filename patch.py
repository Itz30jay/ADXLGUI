"""
DRDO PXE — ADXL345 Data Acquisition System
patch.py  |  Environment Sanity Checker

WHAT THIS FILE DOES
-------------------
Verifies that every required Python package is installed at the correct
minimum version before the operator attempts to run the GUI.

Running `python patch.py` prints a clear pass/fail table and exits with
code 0 (all OK) or 1 (missing/outdated). Intended to be run once after
`pip install -r requirements.txt` to confirm the environment is healthy.

WHY A SEPARATE SCRIPT (not just requirements.txt)?
---------------------------------------------------
pip install does not always upgrade an existing package to the minimum
version — it only installs the package if it is absent. If an operator
already has pyqtgraph 0.12.3 installed system-wide, pip may do nothing.
This script detects that case explicitly and tells the operator what to do.

PACKAGE HISTORY
---------------
Patch 1 (v2.2): QKeySequence / QShortcut moved from PyQt6.QtCore → PyQt6.QtGui
  in ui_mainwindow.py. Already correct in v2.2+; no runtime fix needed here.

Patch 2 (v2.8): `import pyqtgraph.exporters` added to plot_manager.py.
  Already present in v2.8+; no runtime fix needed here.

Both original patches have been applied permanently in the source.
This script only validates the installed packages.
"""

from __future__ import annotations

import sys
import importlib.metadata


# ── Required packages ─────────────────────────────────────────────────────────

# Any package listed here must be present AND at the stated minimum version.
# Raising a minimum version here should be accompanied by a requirements.txt
# update and a changelog entry.

REQUIRED: dict[str, str] = {
    "PyQt6":     "6.7.0",     # GUI framework — signals, widgets, threading
    "pyqtgraph": "0.13.7",    # Hardware-accelerated real-time plot rendering
    "numpy":     "1.26.0",    # Vectorised signal processing and statistics
    
    # scipy is REQUIRED (not optional). plot_manager._ema() uses
    # scipy.signal.lfilter to replace a Python loop over 40 000 samples
    # (BUG-EMA fix, v2.8.2). Removing it here would cause an ImportError
    # at the first plot refresh tick.
    
    "scipy":     "1.12.0",
}

# ── Optional packages ─────────────────────────────────────────────────────────

# Present but not imported by any GUI module. Useful for post-processing
# the exported CSV files in a separate analysis environment.

OPTIONAL: dict[str, str] = {
    "pandas": "Post-processing CSV files in Jupyter / standalone scripts only."
              " Not imported by the GUI.",
}


def _version_ok(name: str, minimum: str) -> bool:
    
    """
    Return True if `name` is installed at >= `minimum`.
    Compares the first three version tuple components so "6.7.0.1" passes
    the "6.7.0" check (distutils-style build suffixes are ignored).
    Returns False if the package is not installed at all.
    """
    try:
        installed = importlib.metadata.version(name)
        iv = tuple(int(x) for x in installed.split(".")[:3])
        mv = tuple(int(x) for x in minimum.split(".")[:3])
        return iv >= mv
    except importlib.metadata.PackageNotFoundError:
        return False


def main() -> int:
    
    """
    Print a formatted dependency table and return 0 (all OK) or 1 (failures).
    The structured output makes CI integration straightforward:
        python patch.py || exit 1
    """
    print("\nDRDO PXE — ADXL345 GUI  |  Environment Sanity Check\n")
    print("─" * 62)

    all_ok = True

    # ── Required package table ─────────────────────────────────────────────────
    
    for pkg, minimum in REQUIRED.items():
        try:
            ver = importlib.metadata.version(pkg)
            ok  = _version_ok(pkg, minimum)
            tag = "  ✓" if ok else "  ✗"
            print(
                f"{tag}  {pkg:<18}  installed={ver:<14}  required>={minimum}")
            if not ok:
                all_ok = False
        except importlib.metadata.PackageNotFoundError:
            print(
                f"  ✗  {pkg:<18}  NOT INSTALLED                required>={minimum}")
            all_ok = False

    print()

    # ── Optional package table ─────────────────────────────────────────────────
    
    for pkg, note in OPTIONAL.items():
        try:
            ver = importlib.metadata.version(pkg)
            print(
                f"  ○  {pkg:<18}  installed={ver:<14}  optional — {note}")
        except importlib.metadata.PackageNotFoundError:
            print(
                f"  ○  {pkg:<18}  not installed                optional — {note}")

    print("\n" + "─" * 62)

    if all_ok:
        print("  All required packages are present. Launch with:\n")
        print("    python main.py\n")
    else:
        print("  One or more required packages are missing or outdated.\n")
        print("    pip install -r requirements.txt --upgrade\n")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
