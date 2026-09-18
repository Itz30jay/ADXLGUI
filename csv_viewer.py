"""
DRDO PXE — ADXL345 Data Acquisition System
csv_viewer.py  |  v1.0.1

WHAT THIS FILE DOES
-------------------
A self-contained PyQt6 tab that lets the operator browse, load, and inspect
any ADXL345 CSV file previously saved by DataHandler (via real-time logging
or the SAVE DATA snapshot button).

WHY A SEPARATE TAB (not an external tool)?
------------------------------------------
After a test session the operator often wants to immediately review the data
without opening Excel or a Python script. Having the viewer in the same GUI
window means zero context-switch: click the 📂 tab, Browse, View, and the
same four colour-coded curves appear on the same style of plot. The viewer
shares the pyqtgraph theme and colour palette with the live tabs so the two
views look consistent.

COLUMN MATCHING
---------------
Column detection is case-insensitive and substring-based so the reader handles
the current DataHandler headers, hand-edited variants, and legacy files:
    Time_s | Accel_X(m/s²) | Accel_Y(m/s²) | Accel_Z(m/s²) | Magnitude(m/s²)
    time_s | Accel_X | ax | AccelX | magnitude | mag  …

ENCODING DETECTION
------------------
Files are probed with four encodings in order (utf-8-sig → utf-8 → cp1252 →
latin-1). latin-1 never raises, so it is the unconditional final fallback that
ensures every file opens without a UnicodeDecodeError.

BUG FIXED in v1.0.1
--------------------
Redundant `self._filepath = path` assignment removed from _on_view().
_filepath is set in _on_browse(). Since _path_edit is ReadOnly the text never
changes between browse and view, so the second assignment was dead code.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime

import numpy as np
import pyqtgraph as pg
import pyqtgraph.exporters

from PyQt6.QtCore    import Qt
from responsive       import R   # all fixed px / pt go through R
from PyQt6.QtGui     import QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox,
    QFrame, QFileDialog, QMessageBox,
)

# ── Palette — identical to plot_manager.py ────────────────────────────────────

_BG    = "#000000"
_AXIS  = "#1E4A7A"
_GOLD  = "#FFD700"
_WHITE = "#E8F0FF"
_DIM   = "#3A6A9A"
_C_X   = "#4A90D9"     # Accel X   — blue
_C_Y   = "#39FF14"     # Accel Y   — green
_C_Z   = "#E74C3C"     # Accel Z   — red
_C_MAG = "#00BFFF"     # Magnitude — cyan
_PEN_W = 2.3


def _pen(color: str, w: float = _PEN_W):
    """Build a pyqtgraph pen. Centralised so colour/width changes happen in one place."""
    return pg.mkPen(color=color, width=w)


def _chk_style(color: str) -> str:
    """Axis-coloured checkbox stylesheet — applied to each of the four visibility toggles."""
    return (
        f"QCheckBox{{color:{color};font:9pt 'Segoe UI';background:transparent;}}"
        f"QCheckBox::indicator{{width:13px;height:13px;border-radius:2px;}}"
        f"QCheckBox::indicator:checked{{background:{color};border:1px solid {color};}}"
        f"QCheckBox::indicator:unchecked{{background:#0A1828;border:1px solid #1E4A7A;}}"
    )


def _btn_style(fg: str, border: str) -> str:
    """Reusable flat-button stylesheet — applied to Browse, Clear, and Export buttons."""
    return (
        f"QPushButton{{background:#0A1828;color:{fg};"
        f"border:1px solid {border};border-radius:3px;"
        f"font:9pt 'Segoe UI';padding:0 8px;}}"
        f"QPushButton:hover{{background:#1A3A5A;border-color:{fg};}}"
        f"QPushButton:pressed{{background:#0D2440;}}"
        f"QPushButton:disabled{{color:#3A5A4A;border-color:#1A3A2A;}}"
    )


# ── Column resolver ────────────────────────────────────────────────────────────

def _find_col(headers: list[str], *candidates: str) -> str | None:
    """
    Return the first header that case-insensitively CONTAINS any candidate string.
    Exact match is tried first (faster, avoids false positives), then substring.
    Example: "Accel_X(m/s²)" is matched by candidate "accel_x".
    Returns None if no candidate matches — the caller raises a descriptive ValueError.
    """
    lower_map = {h.lower(): h for h in headers}
    for c in candidates:
        cl = c.lower()
        if cl in lower_map:
            return lower_map[cl]
        for k, orig in lower_map.items():
            if k.startswith(cl) or cl in k:
                return orig
    return None


# ── Main widget ────────────────────────────────────────────────────────────────

class CSVViewerTab(QWidget):
    
    """
    Static data viewer — plugs into PlotManager as Tab 4.
    Does not subscribe to live DataHandler; loads its own data from a CSV file on demand.

    LIVE mode is intentionally disabled (● LIVE button greyed out).
    CSV data is static by definition — scrolling to a "live edge" is meaningless.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Data arrays populated by _load_and_plot(); empty until first load.
        
        self._t   : np.ndarray = np.empty(0)
        self._ax  : np.ndarray = np.empty(0)
        self._ay  : np.ndarray = np.empty(0)
        self._az  : np.ndarray = np.empty(0)
        self._mag : np.ndarray = np.empty(0)
        
        # Path of the currently loaded file — set in _on_browse(), read in _on_view().
        
        self._filepath = ""
        self._build_ui()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)
        root.addWidget(self._build_header_bar())
        root.addWidget(self._build_plot_area(), stretch=1)
        root.addWidget(self._build_stats_bar())

    def _build_header_bar(self) -> QFrame:
        
        """
        Top bar: title label + four per-axis visibility checkboxes + view controls.
        Fixed at 36 px so it does not eat into the plot space.
        """
        bar = QFrame()
        bar.setFixedHeight(R.px(36))
        bar.setStyleSheet(
            "QFrame{background:#030B16;border:none;"
            "border-bottom:1px solid #1E4A7A;}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 0, 8, 0)
        h.setSpacing(14)

        lbl = QLabel("  CSV FILE VIEWER")
        lbl.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        lbl.setStyleSheet(f"color:{_GOLD};background:transparent;")
        h.addWidget(lbl)
        h.addSpacing(12)

        # Per-curve visibility checkboxes wired in _build_plot_area().
        
        self._chk_x   = QCheckBox("  X")
        self._chk_y   = QCheckBox("  Y")
        self._chk_z   = QCheckBox("  Z")
        self._chk_mag = QCheckBox("  Mag")
        for chk, clr in (
            (self._chk_x,   _C_X),
            (self._chk_y,   _C_Y),
            (self._chk_z,   _C_Z),
            (self._chk_mag, _C_MAG),
        ):
            chk.setChecked(True)
            chk.setStyleSheet(_chk_style(clr))
            h.addWidget(chk)

        h.addStretch()

        # LIVE button — greyed out and disabled. CSV data is static; LIVE is N/A.
        
        self._btn_live = QPushButton("● LIVE")
        self._btn_live.setCheckable(True)
        self._btn_live.setChecked(False)
        self._btn_live.setFixedSize(R.px(74), R.px(26))
        self._btn_live.setEnabled(False)
        self._btn_live.setToolTip(
            "LIVE scrolling is not applicable to static CSV data.\n"
            "Use ⊕ Full View to fit all data, or pan/zoom freely.")
        self._btn_live.setStyleSheet(
            "QPushButton{background:#050F1E;color:#3A5A4A;"
            "border:1px solid #1E3A2A;border-radius:3px;font:9pt 'Segoe UI';}")
        h.addWidget(self._btn_live)

        btn_full = QPushButton("⊕ Full View")
        btn_full.setFixedSize(R.px(90), R.px(26))
        btn_full.setStyleSheet(
            "QPushButton{background:#0A1828;color:#4A90D9;"
            "border:1px solid #1E4A7A;border-radius:3px;font:9pt 'Segoe UI';}"
            "QPushButton:hover{background:#1A3A5A;}")
        btn_full.setToolTip("Auto-fit to show all loaded data")
        btn_full.clicked.connect(self._full_view)
        h.addWidget(btn_full)

        btn_export = QPushButton("⬇ Export")
        btn_export.setFixedSize(R.px(80), R.px(26))
        btn_export.setStyleSheet(
            "QPushButton{background:#0A1828;color:#A0C0E0;"
            "border:1px solid #1E4A7A;border-radius:3px;font:9pt 'Segoe UI';}"
            "QPushButton:hover{background:#1A3A5A;}")
        btn_export.setToolTip("Export plot as PNG")
        btn_export.clicked.connect(self._export_png)
        h.addWidget(btn_export)
        return bar

    def _build_file_bar(self) -> QFrame:
        
        """
        File picker row: path display + Browse + ▶ View + ✕ Clear buttons.
        Path display is ReadOnly — the only way to change it is Browse.
        View button is disabled until a file is chosen (textChanged fires on setText).
        """
        
        bar = QFrame()
        bar.setFixedHeight(R.px(44))
        bar.setStyleSheet(
            "QFrame{background:#050F1E;"
            "border:1px solid #1E4A7A;border-radius:4px;}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(10, 4, 10, 4)
        h.setSpacing(8)

        ico = QLabel("📂")
        ico.setStyleSheet("font:12pt;background:transparent;border:none;")
        ico.setFixedWidth(20)
        h.addWidget(ico)

        lbl = QLabel("File:")
        lbl.setStyleSheet(
            f"color:{_C_X};font:9pt 'Segoe UI';background:transparent;border:none;")
        lbl.setFixedWidth(32)
        h.addWidget(lbl)

        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText(
            "Browse or paste an ADXL345 .csv path here…")
        self._path_edit.setReadOnly(True)
        self._path_edit.setStyleSheet(
            "QLineEdit{background:#0A1828;color:#E8F0FF;"
            "border:1px solid #1E4A7A;border-radius:3px;"
            "font:9pt Consolas;padding:2px 6px;}"
            "QLineEdit:focus{border-color:#4A90D9;}")
        
        # textChanged enables/disables the View button. Note: in PyQt6, textChanged
        # does NOT fire on ReadOnly QLineEdit when setText() is called programmatically,
        # so _on_browse() also calls setEnabled(True) explicitly.
        
        self._path_edit.textChanged.connect(
            lambda txt: self._btn_view.setEnabled(bool(txt.strip())))
        h.addWidget(self._path_edit, stretch=1)

        btn_browse = QPushButton("📂  Browse")
        btn_browse.setFixedSize(R.px(105), R.px(30))
        btn_browse.setStyleSheet(_btn_style(_C_X, _AXIS))
        btn_browse.clicked.connect(self._on_browse)
        h.addWidget(btn_browse)

        self._btn_view = QPushButton("▶  View")
        self._btn_view.setFixedSize(R.px(88), R.px(30))
        self._btn_view.setEnabled(False)
        self._btn_view.setStyleSheet(
            "QPushButton{background:#061A0E;color:#00CC66;"
            "border:1px solid #007740;border-radius:3px;"
            "font:9pt 'Segoe UI';font-weight:bold;}"
            "QPushButton:hover{background:#0A2A18;border-color:#00FF88;}"
            "QPushButton:pressed{background:#040F08;}"
            "QPushButton:disabled{color:#2A4A36;border-color:#1A2A22;}")
        self._btn_view.clicked.connect(self._on_view)
        h.addWidget(self._btn_view)

        btn_clear = QPushButton("✕  Clear")
        btn_clear.setFixedSize(R.px(80), R.px(30))
        btn_clear.setStyleSheet(_btn_style("#E74C3C", "#7A1E1E"))
        btn_clear.clicked.connect(self._on_clear)
        h.addWidget(btn_clear)
        return bar

    def _build_plot_area(self) -> QWidget:
        
        """
        Plot canvas with legend, four PlotDataItem curves, and a crosshair.

        WHY addLegend() on the PlotWidget?
        Unlike the live tabs (which use per-axis checkboxes as visual legend),
        the CSV viewer has all four curves always available. A legend makes it
        immediately clear which colour belongs to which channel.
        """
        
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        v.addWidget(self._build_file_bar())

        self._pw = pg.PlotWidget(background=_BG)
        self._pw.setMenuEnabled(False)
        self._pw.setClipToView(True)
        try:
            self._pw.setDownsampling(auto=True, mode="peak")
        except Exception:
            pass
        pi = self._pw.getPlotItem()
        pi.setLabel("left",   "Acceleration  (m/s²)", color=_WHITE)
        pi.setLabel("bottom", "Time  (s)",             color=_WHITE)
        pi.showGrid(x=True, y=True, alpha=0.15)
        for side in ("left", "bottom", "top", "right"):
            pi.getAxis(side).setPen(_AXIS)
            pi.getAxis(side).setTextPen(_WHITE)
        pi.setTitle("CSV File View", color=_GOLD, size="9pt")

        self._pw.addLegend(
            offset=(12, 12),
            labelTextColor=_WHITE,
            brush=pg.mkBrush("#030B16CC"),
            pen=pg.mkPen(_AXIS))

        # Four curves — Z is negated on setData() to match the live Acceleration tab.
        
        self._cv_x   = self._pw.plot(pen=_pen(_C_X),            name="Accel X")
        self._cv_y   = self._pw.plot(pen=_pen(_C_Y),            name="Accel Y")
        self._cv_z   = self._pw.plot(pen=_pen(_C_Z),            name="Accel Z  (inv)")
        self._cv_mag = self._pw.plot(pen=_pen(_C_MAG, _PEN_W + 0.4), name="Magnitude")

        # Checkbox visibility toggles wired directly to the curve's setVisible slot.
        
        self._chk_x  .toggled.connect(self._cv_x  .setVisible)
        self._chk_y  .toggled.connect(self._cv_y  .setVisible)
        self._chk_z  .toggled.connect(self._cv_z  .setVisible)
        self._chk_mag.toggled.connect(self._cv_mag.setVisible)

        # Crosshair — two dashed InfiniteLines tracking the mouse + text readout.
        
        dash = Qt.PenStyle.DashLine
        self._vline = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(_DIM, width=1, style=dash))
        self._hline = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(_DIM, width=1, style=dash))
        self._pw.addItem(self._vline, ignoreBounds=True)
        self._pw.addItem(self._hline, ignoreBounds=True)
        self._coord = pg.TextItem("", color=_GOLD, anchor=(0, 1))
        self._coord.setFont(QFont("Consolas", 8))
        self._pw.addItem(self._coord)
        self._pw.scene().sigMouseMoved.connect(self._on_mouse_moved)

        v.addWidget(self._pw, stretch=1)
        return container

    def _build_stats_bar(self) -> QFrame:
        
        """
        Stats strip at the bottom — sample count, duration, estimated Fs, peak magnitude.
        Updated by _update_stats() after a successful load; reset by _reset_stats().
        """
        bar = QFrame()
        bar.setFixedHeight(R.px(30))
        bar.setStyleSheet(
            "QFrame{background:#030B16;"
            "border:1px solid #1E4A7A;border-radius:3px;}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(12, 0, 12, 0)
        h.setSpacing(28)
        ss = (f"color:{_C_X};font:8pt 'Segoe UI';"
              "background:transparent;border:none;")

        self._lb_file     = QLabel("No file loaded")
        self._lb_samples  = QLabel("Samples: —")
        self._lb_duration = QLabel("Duration: —")
        self._lb_fs       = QLabel("Fs: —")
        self._lb_peak     = QLabel("Peak |a|: —")
        for lb in (self._lb_file, self._lb_samples,
                   self._lb_duration, self._lb_fs, self._lb_peak):
            lb.setStyleSheet(ss)
            h.addWidget(lb)
        h.addStretch()
        return bar

    # ── Slots ──────────────────────────────────────────────────────────────────

    def _on_browse(self):
        
        """
        Open file dialog, store the path in self._filepath, show it in the
        path widget, and explicitly enable the View button.

        WHY setEnabled(True) explicitly?
        PyQt6's ReadOnly QLineEdit does NOT emit textChanged when setText() is
        called programmatically. Without the explicit call the View button stays
        disabled even after Browse succeeds.
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "Open ADXL345 CSV",
            os.path.expanduser("~"),
            "CSV Files (*.csv);;All Files (*.*)")
        if not path:
            return
        self._filepath = path
        self._path_edit.setText(path)
        # Explicit enable — textChanged does NOT fire on ReadOnly QLineEdit in PyQt6.
        self._btn_view.setEnabled(True)
        self._lb_file.setText(f"Selected: {os.path.basename(path)}")
        self._lb_file.setStyleSheet(
            f"color:{_GOLD};font:8pt 'Segoe UI';"
            "background:transparent;border:none;")

    def _on_view(self):
        
        """
        ▶ View button — parse self._filepath and render the curves.

        self._filepath is already set by _on_browse(). The path_edit is ReadOnly
        so there is no way the text can differ from self._filepath at this point.
        No reassignment needed.
        """
        if not self._filepath:
            return
        self._load_and_plot()

    def _on_clear(self):
        
        """Reset all curves, state, and stats back to the empty initial state."""
        
        self._filepath = ""
        self._path_edit.clear()
        self._btn_view.setEnabled(False)
        self._t   = np.empty(0)
        self._ax  = np.empty(0)
        self._ay  = np.empty(0)
        self._az  = np.empty(0)
        self._mag = np.empty(0)
        for cv in (self._cv_x, self._cv_y, self._cv_z, self._cv_mag):
            cv.setData([], [])
        self._pw.getPlotItem().setTitle("CSV File View", color=_GOLD, size="9pt")
        self._reset_stats()

    def _full_view(self):
        
        """Auto-fit the viewport to show all currently loaded data."""
        
        vb = self._pw.getViewBox()
        vb.enableAutoRange()
        vb.autoRange()

    def _export_png(self):
        
        """Export the current plot state to a PNG file via a save dialog."""
        
        if len(self._t) == 0:
            QMessageBox.information(self, "No Data", "Load a CSV file first.")
            return
        default = (
            f"DRDO_CSV_{os.path.splitext(os.path.basename(self._filepath))[0]}.png"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Plot", default, "PNG Images (*.png)")
        if not path:
            return
        try:
            pg.exporters.ImageExporter(self._pw.plotItem).export(path)
        except Exception as e:
            QMessageBox.warning(self, "Export Error", str(e))

    def _on_mouse_moved(self, pos):
        
        """Update the crosshair InfiniteLines and coordinate text on mouse movement."""
        
        vb = self._pw.getViewBox()
        if self._pw.sceneBoundingRect().contains(pos):
            mp = vb.mapSceneToView(pos)
            self._vline.setPos(mp.x())
            self._hline.setPos(mp.y())
            self._coord.setText(f"  t={mp.x():.3f}s   y={mp.y():.4f}")
            self._coord.setPos(mp.x(), mp.y())

    # ── CSV loading ────────────────────────────────────────────────────────────

    def _load_and_plot(self):
        
        """
        Parse self._filepath, store results in instance arrays, draw curves.
        All errors produce a human-readable error message in the stats bar and
        a popup dialog for long messages — the plot is never left in a half-drawn state.
        """
        try:
            t, ax, ay, az, mag = self._parse_csv(self._filepath)
        except Exception as exc:
            self._show_error(str(exc))
            return

        if len(t) < 2:
            self._show_error("No valid rows parsed — check the error dialog for details.")
            return

        self._t, self._ax, self._ay, self._az, self._mag = t, ax, ay, az, mag

        # Z is negated for display — matches the live Acceleration tab convention.
        
        self._cv_x  .setData(self._t, self._ax)
        self._cv_y  .setData(self._t, self._ay)
        self._cv_z  .setData(self._t, -self._az)
        self._cv_mag.setData(self._t, self._mag)

        # Restore checkbox-driven visibility in case any were toggled while loading.
        
        self._cv_x  .setVisible(self._chk_x  .isChecked())
        self._cv_y  .setVisible(self._chk_y  .isChecked())
        self._cv_z  .setVisible(self._chk_z  .isChecked())
        self._cv_mag.setVisible(self._chk_mag.isChecked())

        self._full_view()
        name = os.path.basename(self._filepath)
        self._pw.getPlotItem().setTitle(f"CSV: {name}", color=_GOLD, size="9pt")
        self._update_stats(name)

    @staticmethod
    def _detect_encoding(filepath: str) -> str:
        
        """
        Probe four encodings in order; return the first that reads the file cleanly.
        latin-1 is the unconditional fallback — it never raises a UnicodeDecodeError
        because it maps every byte 0x00–0xFF to the same Unicode code point.
        """
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                with open(filepath, newline="", encoding=enc) as fh:
                    fh.read()
                return enc
            except (UnicodeDecodeError, LookupError):
                continue
        return "latin-1"

    @staticmethod
    def _parse_csv(filepath: str):
        
        """
        Read an ADXL345 CSV and return five float64 numpy arrays:
            (t, ax, ay, az, mag)  — all aligned by row index.

        WHY two passes over the file?
        Pass 1: sniff the header row and detect column names (DictReader, then close).
        Pass 2: iterate rows and parse values.
        Two passes avoid holding the entire file in memory (important for very long
        sessions) and let us fail fast with a descriptive error if the header is
        malformed, before attempting to parse any data rows.

        t is normalised to t=0 at the first valid sample so the x-axis matches
        what the live plots show.
        """
        enc = CSVViewerTab._detect_encoding(filepath)

        with open(filepath, newline="", encoding=enc) as fh:
            reader  = csv.DictReader(fh)
            headers = list(reader.fieldnames or [])

        if not headers:
            raise ValueError("File appears empty (no header row found).")

        col_t   = _find_col(headers, "time_s",    "time(s)", "time")
        col_ax  = _find_col(headers, "accel_x",   "ax",      "x")
        col_ay  = _find_col(headers, "accel_y",   "ay",      "y")
        col_az  = _find_col(headers, "accel_z",   "az",      "z")
        col_mag = _find_col(headers, "magnitude",  "mag")

        missing = [n for n, c in [
            ("Time_s",  col_t), ("Accel_X", col_ax),
            ("Accel_Y", col_ay), ("Accel_Z", col_az),
        ] if c is None]
        if missing:
            raise ValueError(
                f"Required columns not found: {missing}\n\n"
                f"Headers detected: {headers}\n\n"
                "Expected (case-insensitive): Time_s, Accel_X, Accel_Y, Accel_Z"
            )

        def _parse_time(raw: str) -> float:
            
            """
            Return time as float seconds.
            Tries plain float first (handles Time_s column like 0.0000, 0.0075).
            Falls back to ISO-8601 datetime formats for Timestamp-style columns.
            """
            raw = raw.strip()
            try:
                return float(raw)
            except ValueError:
                pass
            for fmt in (
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S",
            ):
                try:
                    return datetime.strptime(raw, fmt).timestamp()
                except ValueError:
                    continue
            raise ValueError(f"Cannot parse time value: {raw!r}")

        t_l, ax_l, ay_l, az_l, mag_l = [], [], [], [], []
        skipped   = 0
        first_err = None
        t0        = None    # absolute time of first valid sample (normalise to t=0)

        with open(filepath, newline="", encoding=enc) as fh:
            for row_num, row in enumerate(csv.DictReader(fh), start=2):
                try:
                    tv  = _parse_time(row[col_t])
                    axv = float(row[col_ax])
                    ayv = float(row[col_ay])
                    azv = float(row[col_az])
                    mgv = float(row[col_mag]) if col_mag else float(
                        np.sqrt(axv**2 + ayv**2 + azv**2))
                    if t0 is None:
                        t0 = tv     # anchor t=0 to the first valid row
                    t_l .append(tv - t0)
                    ax_l.append(axv)
                    ay_l.append(ayv)
                    az_l.append(azv)
                    mag_l.append(mgv)
                except (ValueError, KeyError) as e:
                    if first_err is None:
                        first_err = (row_num, str(e), dict(row))
                    skipped += 1

        if len(t_l) < 2:
            
            # Produce a fully diagnostic error message so the operator can fix
            # the file or re-export without guessing what went wrong.
            
            msg = (
                f"No valid data rows could be parsed.\n\n"
                f"Encoding detected : {enc}\n"
                f"Headers found     : {headers}\n"
                f"Columns mapped    :\n"
                f"  Time   -> {col_t}\n"
                f"  Accel X-> {col_ax}\n"
                f"  Accel Y-> {col_ay}\n"
                f"  Accel Z-> {col_az}\n"
                f"  Mag    -> {col_mag}\n"
                f"Rows skipped      : {skipped}\n"
            )
            if first_err:
                msg += (
                    f"\nFirst bad row (row {first_err[0]}):\n"
                    f"  Error : {first_err[1]}\n"
                    f"  Values: {first_err[2]}"
                )
            raise ValueError(msg)

        return (
            np.array(t_l,   dtype=np.float64),
            np.array(ax_l,  dtype=np.float64),
            np.array(ay_l,  dtype=np.float64),
            np.array(az_l,  dtype=np.float64),
            np.array(mag_l, dtype=np.float64),
        )

    # ── Stats helpers ──────────────────────────────────────────────────────────

    def _update_stats(self, filename: str):
        """Populate the bottom stats bar with statistics from the newly loaded data."""
        n   = len(self._t)
        dur = float(self._t[-1] - self._t[0]) if n > 1 else 0.0
        fs  = float(n / dur) if dur > 0 else 0.0
        pk  = float(self._mag.max()) if n > 0 else 0.0

        green = f"color:{_C_Y};font:8pt 'Segoe UI';background:transparent;border:none;"
        dim   = f"color:{_C_X};font:8pt 'Segoe UI';background:transparent;border:none;"

        self._lb_file    .setStyleSheet(green)
        self._lb_file    .setText(f"✓  {filename}")
        self._lb_samples .setText(f"Samples: {n:,}")
        self._lb_duration.setText(f"Duration: {dur:.2f} s")
        self._lb_fs      .setText(f"Fs ≈ {fs:.1f} Hz")
        self._lb_peak    .setText(f"Peak |a|: {pk:.3f} m/s²")
        for lb in (self._lb_samples, self._lb_duration, self._lb_fs, self._lb_peak):
            lb.setStyleSheet(dim)

    def _reset_stats(self):
        """Restore all stats labels to their initial placeholder state."""
        dim = f"color:{_C_X};font:8pt 'Segoe UI';background:transparent;border:none;"
        self._lb_file    .setText("No file loaded")
        self._lb_file    .setStyleSheet(dim)
        self._lb_samples .setText("Samples: —")
        self._lb_duration.setText("Duration: —")
        self._lb_fs      .setText("Fs: —")
        self._lb_peak    .setText("Peak |a|: —")
        for lb in (self._lb_samples, self._lb_duration, self._lb_fs, self._lb_peak):
            lb.setStyleSheet(dim)

    def _show_error(self, message: str):
        """Display a short error in the stats bar; a full dialog for long messages."""
        red = "color:#FF4444;font:8pt 'Segoe UI';background:transparent;border:none;"
        self._lb_file.setStyleSheet(red)
        self._lb_file.setText(f"⚠  {message[:120]}")
        if len(message) > 80:
            QMessageBox.warning(self, "CSV Load Error", message)

    # ── PlotManager hooks ──────────────────────────────────────────────────────

    def clear(self):
        """
        Called by PlotManager.clear_plots() when the operator clicks CLEAR ALL.
        We intentionally keep the loaded CSV data on screen — the operator may
        want to continue reviewing a file while starting a new live session.
        """
        # Deliberate no-op: preserving loaded CSV across CLEAR is the intended UX.

    def set_paused(self, v: bool):
        """No-op — CSV viewer has no live refresh loop to pause."""
