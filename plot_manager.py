"""
DRDO PXE — ADXL345 Data Acquisition System
plot_manager.py  |  v2.8.4

WHAT THIS FILE DOES
-------------------
Owns the QTabWidget that sits in the centre of the GUI and drives all live
graph rendering. Four tabs:

  Tab 1 ⚡ Acceleration X/Y/Z  — three EMA-smoothed curves, per-axis offsets
  Tab 2 📊 Vibration Analysis   — raw magnitude + draggable threshold line
  Tab 3 🌍 Seismic              — STA/LTA detector + FFT + event log
  Tab 4 📂 CSV Viewer           — browse any previous ADXL345 CSV file

PlotManager.refresh() is called at 12.5 Hz (every 80 ms) by DRDOController.
One snapshot is taken per tick and shared across all three live tabs —
avoiding three separate DataHandler lock acquisitions per frame.

The CSV Viewer tab (Tab 4) is static — it loads data on demand and has no
live refresh path. PlotManager.refresh() does not call it.

CHANGELOG
---------
v2.8.4  NEW: CSVViewerTab added as the fourth tab (📂 CSV Viewer).
        BUG-EMA-INIT fix: lfilter was called with zi=None (zero initial
          state). For a 9.81 m/s² gravity signal, y[0] = alpha×9.81 = 0.98
          instead of 9.81 — visible as a "ramp from zero" for ~0.5 s after
          START/CLEAR. Fix: use lfilter_zi(b,a)×arr[0] to warm-start the
          filter at steady-state for the first sample.
        BUG-LABEL fix: _on_thresh_spin() called InfiniteLine.label.setFormat()
          on a line created without label= → AttributeError on pyqtgraph<0.12.3.
          Fix: removed the setFormat() call; spinbox shows the value.
        BUG-DRAG fix: _on_thresh_dragged() allowed sub-minimum drag values.
          DataHandler received the raw out-of-range value, creating a silent
          display/data mismatch. Fix: clamp drag value to spinbox bounds.

v2.8.2  BUG-EMA fix: _ema() replaced 40 000-iteration Python for-loop with
        scipy.signal.lfilter — compiled C, effectively free.

v2.8.1  Corrected module-level docstring: LIVE mode uses LIVE_WINDOW_S (10 s),
        not a fixed [0, 300 s] axis.

v2.8.0  Time-axis overhaul: elapsed time from snap["start_time"]; LIVE mode
        scrolls a 10-second window; Full View auto-ranges to all data.
        FULL_BUF raised to 40 000. VIB_BUF removed — all tabs use FULL_BUF.

v2.7.1  AccelTab: Z-axis negated for display (raw data unchanged).
v2.7.0  VibrationTab: raw magnitude (no EMA); spinbox range 1–100 m/s².
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
import pyqtgraph.exporters
from scipy.signal import lfilter as _scipy_lfilter, lfilter_zi as _scipy_lfilter_zi

from PyQt6.QtCore    import Qt

# QFont removed — all font creation goes through R.font() from responsive.py

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QPushButton, QLabel, QFrame, QDoubleSpinBox,
    QFileDialog, QMessageBox, QCheckBox,
)

from data_handler     import DataHandler
from seismic_analyzer import SeismicTab
from csv_viewer       import CSVViewerTab
from responsive       import R   # all fixed dimensions go through R.px() / R.pt()

# ── Colour palette ─────────────────────────────────────────────────────────────

# All colours are centralised here so a single palette change propagates to
# every tab, every curve, and every widget in this file.

CLR_BG     = "#000000"    # Pure black — maximises graph contrast
CLR_AXIS   = "#1E4A7A"    # Axis pen + borders
CLR_BLUE   = "#4A90D9"    # Accel X
CLR_GREEN  = "#39FF14"    # Accel Y
CLR_RED    = "#E74C3C"    # Accel Z
CLR_MAG    = "#00BFFF"    # Magnitude in Vibration tab
CLR_THRESH = "#FF8C00"    # Threshold line (amber)
CLR_GOLD   = "#FFD700"    # Titles, stat strip values
CLR_WHITE  = "#E8F0FF"    # Axis labels, plot title
CLR_DIM    = "#3A6A9A"    # Crosshair lines

PLOT_PEN_W    = 2.3
EMA_ALPHA     = 0.10       # EMA smoothing factor for AccelTab curves.
                           # 0.10 means 10 % of each new sample + 90 % of history.
                           # This removes ~133 Hz sensor noise while preserving
                           # low-frequency motion trends visible at 12.5 Hz refresh.
LIVE_WINDOW_S = 10.0       # Seconds of data visible in LIVE (scrolling) mode
FULL_BUF      = 40_000     # Samples per snapshot — matches DataHandler.MAX_BUFFER


def _pen(color: str, width: float = PLOT_PEN_W):
    
    """Build a pyqtgraph pen from a hex colour string and width."""
    
    return pg.mkPen(color=color, width=width)


def _ema(arr: np.ndarray, alpha: float = EMA_ALPHA) -> np.ndarray:
    
    """
    Vectorised Exponential Moving Average via scipy.signal.lfilter.

    The EMA recurrence  y[n] = alpha * x[n] + (1-alpha) * y[n-1]  is
    mathematically an IIR filter with:
        b = [alpha]
        a = [1, -(1-alpha)]

    WHY scipy.signal.lfilter (not a Python for-loop)?
    BUG-EMA fix (v2.8.2): the old Python loop ran 40 000 iterations per axis
    per 80 ms tick — about 24 ms of wasted CPU per refresh, causing visible
    GUI stutter at full buffer. lfilter executes the same recurrence in
    compiled C in microseconds.

    WHY lfilter_zi warm-start?
    BUG-EMA-INIT fix (v2.8.4): calling lfilter with zi=None (default) sets
    the internal state y[-1] = 0. For any non-zero signal the very first
    output is y[0] = alpha × x[0], e.g. 0.10 × 9.81 = 0.98 m/s² instead
    of 9.81 m/s². Because gravity is always present on the Z axis, this
    appeared as a "ramp from zero" on all three curves for ~65 samples
    (~0.5 s) after every START or CLEAR.

    FIX: lfilter_zi(b, a) returns the steady-state initial conditions for
    unit input. Multiplying by arr[0] pretends the filter has been running
    on a constant arr[0] forever — so the very first output equals arr[0],
    exactly matching the behaviour of the old Python loop's  out[0] = arr[0].
    """
    if len(arr) < 2:
        return arr
    b  = np.array([alpha],                dtype=np.float64)
    a  = np.array([1.0, -(1.0 - alpha)], dtype=np.float64)
    zi = _scipy_lfilter_zi(b, a) * arr[0]         # warm-start to first value
    result, _ = _scipy_lfilter(b, a, arr, zi=zi)
    return result


def _elapsed(snap: dict) -> np.ndarray:
    
    """
    Convert raw Unix timestamps from snap["t"] to elapsed seconds from
    the acquisition start (snap["start_time"]).

    WHY subtract start_time (not snap["t"][0])?
    After the ring buffer is full (40 000 samples), snap["t"][0] is no longer
    the session start — it is the oldest sample still in memory. Subtracting
    it would make t=0 jump forward as the deque wraps. snap["start_time"] is
    the absolute timestamp of the very first ingested sample and never changes.
    """
    raw        = snap["t"]
    start_time = snap.get("start_time", 0.0)
    if start_time > 0.0 and len(raw) > 0:
        return raw - start_time
    
    # Fallback for stale snapshots that pre-date v2.8 (should not occur normally)
    
    return raw - raw[0] if len(raw) > 0 else raw


# ── Button style helpers ───────────────────────────────────────────────────────

def _live_style(active: bool) -> str:
    
    """Return the stylesheet for the ● LIVE button in active or inactive state."""
    
    if active:
        return ("QPushButton{background:#003320;color:#00FF88;"
                "border:2px solid #00FF88;border-radius:4px;"
                "font:9pt 'Segoe UI';font-weight:bold;padding:0 6px;}"
                "QPushButton:hover{background:#005530;}")
    return ("QPushButton{background:#0A1828;color:#3A6A9A;"
            "border:1px solid #1E4A7A;border-radius:4px;"
            "font:9pt 'Segoe UI';padding:0 6px;}"
            "QPushButton:hover{background:#1A3A5A;color:#E8F0FF;}")


def _full_style() -> str:
    
    """Stylesheet for the ⊕ Full View button."""
    
    return (f"QPushButton{{background:#050F1E;color:#6A9ACA;"
            f"border:1px solid {CLR_AXIS};border-radius:3px;font:8pt Consolas;}}"
            f"QPushButton:hover{{background:{CLR_AXIS};color:{CLR_WHITE};}}")


def _export_style() -> str:
    
    """Stylesheet for the ⬇ Export button."""
    
    return (f"QPushButton{{background:#050F1E;color:{CLR_GOLD};"
            f"border:1px solid {CLR_AXIS};border-radius:3px;font:8pt Consolas;}}"
            f"QPushButton:hover{{background:{CLR_AXIS};border-color:{CLR_GOLD};}}")


def _chk_style(color: str) -> str:
    
    """Checkbox stylesheet for axis visibility toggles (coloured per axis)."""
    
    return (f"QCheckBox{{color:{color};background:transparent;"
            f"font:9pt 'Segoe UI';font-weight:bold;}}"
            f"QCheckBox::indicator{{width:14px;height:14px;"
            f"border:2px solid {color};border-radius:3px;}}"
            f"QCheckBox::indicator:checked{{background:{color};"
            f"border:2px solid {color};border-radius:3px;}}")


def _off_spin(axis_label: str, color: str) -> tuple:
    
    """
    Build a (QFrame container, QDoubleSpinBox) pair for a per-axis display offset.

    WHY QFrame (not QWidget)?
    QFrame fills its background via stylesheets reliably without requiring
    setAutoFillBackground(True). Using the object-name CSS selector (#id) keeps
    the coloured border from leaking to child widgets (label + spinbox).

    The offset formula is:
        displayed = raw + offset   (for X and Y)
        displayed = -raw + offset  (for Z — raw is negated first)
    Raw sensor data, magnitude, vibration counting, and CSV are never affected.
    """
    obj_name  = f"offBox_{axis_label}"
    container = QFrame()
    container.setFrameShape(QFrame.Shape.NoFrame)
    container.setObjectName(obj_name)
    container.setStyleSheet(
        f"QFrame#{obj_name}{{"
        f"background:#0A0B10;"
        f"border:1px solid {color};"
        f"border-radius:3px;}}"
    )
    h = QHBoxLayout(container)
    h.setContentsMargins(5, 2, 5, 2)
    h.setSpacing(4)

    lbl = QLabel(axis_label)
    lbl.setFont(R.font("Segoe UI", 8, bold=True))
    lbl.setStyleSheet(f"color:{color};background:transparent;border:none;")

    spin = QDoubleSpinBox()
    spin.setRange(-9999.0, 9999.0)
    spin.setSingleStep(0.1)
    spin.setDecimals(2)
    spin.setValue(0.0)
    spin.setFixedWidth(R.px(72))
    spin.setFixedHeight(R.px(22))
    spin.setFont(R.font("Consolas", 8))
    spin.setToolTip(
        f"Offset added to displayed {axis_label.strip()} values.\n"
        f"Formula:  displayed_{axis_label.strip()} = raw_{axis_label.strip()} + offset\n"
        f"(For Z: negate raw first, then add offset)\n"
        f"Default = 0.0"
    )
    spin.setStyleSheet(
        f"QDoubleSpinBox{{background:#050F1E;color:{color};"
        f"border:1px solid {CLR_AXIS};border-radius:3px;padding:1px 3px;}}"
        f"QDoubleSpinBox:focus{{border:1px solid {color};}}"
        f"QDoubleSpinBox::up-button,QDoubleSpinBox::down-button"
        f"{{width:14px;background:#0A1828;border:none;}}"
    )
    h.addWidget(lbl)
    h.addWidget(spin)
    return container, spin


# ── Stat strip ─────────────────────────────────────────────────────────────────

class StatRow(QFrame):
    
    """
    Fixed-height strip at the bottom of each live plot tab.
    Shows MIN / MAX / MEAN / PEAK of the plotted data.

    WHY compute stats on the snapshot data (not from DataHandler)?
    The Seismic tab and AccelTab display transformed data (elapsed time, EMA,
    negated Z). Computing stats on the same data that is plotted ensures
    the numbers in the strip always match what the operator sees on screen.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(R.px(24))
        self.setStyleSheet(
            f"QFrame{{background:#030B16;border-top:1px solid {CLR_AXIS};}}")
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 0, 10, 0)
        h.setSpacing(0)
        self._min  = self._cell("MIN",  h)
        self._max  = self._cell("MAX",  h)
        self._mean = self._cell("MEAN", h)
        self._peak = self._cell("PEAK", h)
        h.addStretch()

    @staticmethod
    def _cell(name: str, layout) -> QLabel:
        
        """Build one label-value pair and add both to the layout."""
        
        k = QLabel(f"  {name}: ")
        k.setFont(R.font("Consolas", 8))
        k.setStyleSheet(f"color:{CLR_DIM};background:transparent;")
        v = QLabel("---  ")
        v.setFont(R.font("Consolas", 8, bold=True))
        v.setStyleSheet(f"color:{CLR_GOLD};background:transparent;")
        layout.addWidget(k)
        layout.addWidget(v)
        return v

    def update_stats(self, s: dict):
        
        """Update all four stat values from a {min, max, mean, peak} dict."""
        
        f = lambda x: f"{x:+.3f}  "
        self._min .setText(f(s.get("min",  0.0)))
        self._max .setText(f(s.get("max",  0.0)))
        self._mean.setText(f(s.get("mean", 0.0)))
        self._peak.setText(f(s.get("peak", 0.0)))

    def reset(self):
        
        """Called by PlotManager.clear_plots() to wipe stat values."""
        
        self.update_stats({"min": 0.0, "max": 0.0, "mean": 0.0, "peak": 0.0})


# ── Base plot tab ──────────────────────────────────────────────────────────────

class PlotTab(QWidget):
    
    """
    Base class for all live plot tabs.

    Provides:
      • pg.PlotWidget with configured grid, axes, and title
      • ● LIVE button — scrolls X to show the last 10 seconds; auto-scales Y
      • ⊕ Full View  — fits all data collected so far on screen
      • Manual pan/zoom detection → exits LIVE mode automatically
      • Crosshair with live t/y readout
      • StatRow strip at the bottom
      • ⬇ Export PNG button

    LIVE MODE IMPLEMENTATION:
    _apply_live_range() is called after every curve update. It computes the
    desired X window (t_end - 10 s → t_end) and programs ViewBox.setXRange().
    The last programmed X window is saved in _last_live_x so that
    _on_manual_range() can distinguish "user moved X" from "user adjusted Y only":
      • X changed → exit LIVE so the user can inspect a historical region
      • Y only changed → stay in LIVE but honour the user's Y scale
    """

    def __init__(self, title: str, y_label: str = "", parent=None):
        super().__init__(parent)
        self.title           = title
        self._paused         = False
        self._live_mode      = True
        
        # Re-entry guard: prevents _on_manual_range from firing during our own
        # programmatic setXRange() calls inside _apply_live_range().
        
        self._updating_range = False
        
        # True when the user has manually zoomed/panned Y while in LIVE mode.
        # While True, _apply_live_range skips Y auto-scale to honour their choice.
        # Reset when LIVE is re-activated.
        
        self._user_y_set     = False
        self._last_live_x    = (0.0, LIVE_WINDOW_S)   # last X window we set

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(4, 4, 4, 0)
        self._outer.setSpacing(4)

        # ── Top bar ────────────────────────────────────────────────────────
        
        self._top = QHBoxLayout()
        lbl = QLabel(f"  {title.upper()}")
        lbl.setFont(R.font("Segoe UI", 9, bold=True))
        lbl.setStyleSheet(f"color:{CLR_GOLD};")
        self._top.addWidget(lbl)
        self._top.addStretch()

        self._btn_live = QPushButton("● LIVE")
        self._btn_live.setCheckable(True)
        self._btn_live.setChecked(True)
        self._btn_live.setFixedSize(R.px(74), R.px(26))
        self._btn_live.setStyleSheet(_live_style(True))
        self._btn_live.setToolTip(
            "LIVE: scrolls to show last 10 s of data\n"
            "All history is retained — use Full View to see it\n"
            "Pan/zoom exits live mode\n"
            "Click again to return to live edge")
        self._btn_live.toggled.connect(self._on_live_toggled)
        self._top.addWidget(self._btn_live)

        btn_full = QPushButton("⊕ Full View")
        btn_full.setFixedSize(R.px(90), R.px(26))
        btn_full.setStyleSheet(_full_style())
        btn_full.setToolTip("Show all data from t = 0 to now")
        btn_full.clicked.connect(self._full_view)
        self._top.addWidget(btn_full)

        self.btn_export = QPushButton("⬇ Export")
        self.btn_export.setFixedSize(R.px(80), R.px(26))
        self.btn_export.setStyleSheet(_export_style())
        self.btn_export.clicked.connect(self._export_png)
        self._top.addWidget(self.btn_export)

        self._outer.addLayout(self._top)

        # ── Plot widget ────────────────────────────────────────────────────
        
        self.plot_widget = pg.PlotWidget(background=CLR_BG)
        self._configure_plot(y_label)
        self._outer.addWidget(self.plot_widget, stretch=1)

        # ── Stat strip ─────────────────────────────────────────────────────
        
        self.stat_row = StatRow()
        self._outer.addWidget(self.stat_row)

        # ── Crosshair ──────────────────────────────────────────────────────
        
        # Two invisible InfiniteLines (dashed V + H) track the mouse position.
        # The TextItem shows the exact (t, y) coordinates near the cursor.
        
        dash = Qt.PenStyle.DashLine
        self._vline = pg.InfiniteLine(angle=90, movable=False,
            pen=pg.mkPen(CLR_DIM, width=1, style=dash))
        self._hline = pg.InfiniteLine(angle=0, movable=False,
            pen=pg.mkPen(CLR_DIM, width=1, style=dash))
        self.plot_widget.addItem(self._vline, ignoreBounds=True)
        self.plot_widget.addItem(self._hline, ignoreBounds=True)
        self._coord = pg.TextItem("", color=CLR_GOLD, anchor=(0, 1))
        self._coord.setFont(R.font("Consolas", 8))
        self.plot_widget.addItem(self._coord)
        self.plot_widget.scene().sigMouseMoved.connect(self._on_mouse_moved)

        # Detect user-initiated pan/zoom to auto-exit LIVE mode.
        
        self.plot_widget.getViewBox().sigRangeChangedManually.connect(
            self._on_manual_range)

    def _configure_plot(self, y_label: str):
        
        """Apply the standard axis, grid, and title configuration to the plot widget."""
        
        pw = self.plot_widget
        pw.setMenuEnabled(False)     # right-click context menu disabled
        pw.setClipToView(True)       # don't render curve segments outside the viewport
        try:
            pw.setDownsampling(auto=True, mode="peak")
        except Exception:
            pass    # older pyqtgraph versions may not support all modes
        pi = pw.getPlotItem()
        pi.setLabel("left",   y_label,    color=CLR_WHITE)
        pi.setLabel("bottom", "Time (s)", color=CLR_WHITE)
        pi.showGrid(x=True, y=True, alpha=0.15)
        pi.getAxis("left").setPen(CLR_AXIS)
        pi.getAxis("bottom").setPen(CLR_AXIS)
        pi.getAxis("left").setTextPen(CLR_WHITE)
        pi.getAxis("bottom").setTextPen(CLR_WHITE)
        pi.setTitle(title=self.title, color=CLR_GOLD, size="9pt")
        pw.getViewBox().enableAutoRange(axis="y")

    def _insert_top(self, widget):
        
        """
        Insert a widget into the top bar just before the ● LIVE button.

        Used by AccelTab to add checkboxes and offset spinboxes dynamically.
        n-3 places the new widget before [LIVE, Full View, Export].
        """
        n = self._top.count()
        self._top.insertWidget(n - 3, widget)

    def _on_live_toggled(self, checked: bool):
        """Handle ● LIVE button toggle — switch mode and update button style."""
        self._live_mode = checked
        self._btn_live.setStyleSheet(_live_style(checked))
        if checked:
            
            # Re-entering LIVE: clear any manual Y scale the user set.
            
            self._user_y_set = False
            self.plot_widget.getViewBox().enableAutoRange(axis="y")

    def _on_manual_range(self):
        
        """
        Fired when the user manually pans or zooms (sigRangeChangedManually).

        STRATEGY:
          X moved → exit LIVE mode so the user can freely inspect history.
          Y only  → stay in LIVE but remember the user's Y scale so we don't
                    reset it every refresh tick.

        We detect X movement by comparing the current viewport X range against
        _last_live_x (what _apply_live_range last programmed). A 2-second
        tolerance absorbs Qt padding and floating-point rounding without
        being so wide that large scrolls are missed.
        """
        if not self._live_mode or self._updating_range:
            return
        vb   = self.plot_widget.getViewBox()
        xr   = vb.viewRange()[0]   # current [x_min, x_max]
        tol  = 2.0
        x_moved = (
            abs(xr[0] - self._last_live_x[0]) > tol or
            abs(xr[1] - self._last_live_x[1]) > tol
        )
        if x_moved:
            
            # User scrolled X — leave LIVE mode so we stop chasing the edge.
            
            self._live_mode = False
            self._btn_live.blockSignals(True)
            self._btn_live.setChecked(False)
            self._btn_live.blockSignals(False)
            self._btn_live.setStyleSheet(_live_style(False))
        else:
            
            # Only Y changed — stay in LIVE, honour the user's Y scale.
            
            self._user_y_set = True

    def _apply_live_range(self, t: np.ndarray):
        
        """
        Scroll the X axis to show the last LIVE_WINDOW_S seconds of data.

        Called at the end of every update_data() after curve data is pushed.
        Does nothing if LIVE mode is off (user is reviewing history).

        Y auto-scale: enabled unless the user has manually adjusted Y while
        staying in LIVE mode (_user_y_set=True), in which case their scale
        is preserved across refresh ticks.
        """
        if not self._live_mode or len(t) < 2:
            return
        t_end   = float(t[-1])
        t_start = max(0.0, t_end - LIVE_WINDOW_S)
        vb = self.plot_widget.getViewBox()
        
        # Set the guard before calling setXRange so _on_manual_range does
        # not fire and immediately exit LIVE mode on our own programmatic range change.
        
        self._updating_range = True
        vb.setXRange(t_start, t_end, padding=0.02)
        self._last_live_x    = (t_start, t_end)
        if not self._user_y_set:
            vb.enableAutoRange(axis="y")
        self._updating_range = False

    def _full_view(self):
        
        """
        Auto-fit to show all data from t=0 to now.

        Exits LIVE mode first so the view does not snap back to the live edge
        on the next refresh tick. Clears _user_y_set so the next LIVE session
        starts fresh with auto Y-scaling.
        """
        self._live_mode  = False
        self._user_y_set = False
        self._btn_live.blockSignals(True)
        self._btn_live.setChecked(False)
        self._btn_live.blockSignals(False)
        self._btn_live.setStyleSheet(_live_style(False))
        vb = self.plot_widget.getViewBox()
        vb.enableAutoRange()
        vb.autoRange()

    def _on_mouse_moved(self, pos):
        
        """Move the crosshair lines and update the coordinate readout label."""
        
        vb = self.plot_widget.getViewBox()
        if self.plot_widget.sceneBoundingRect().contains(pos):
            mp = vb.mapSceneToView(pos)
            self._vline.setPos(mp.x())
            self._hline.setPos(mp.y())
            self._coord.setText(f"  t={mp.x():.2f}s  y={mp.y():.4f}")
            self._coord.setPos(mp.x(), mp.y())

    def _export_png(self):
        
        """Export the current plot view as a PNG image file."""
        
        path, _ = QFileDialog.getSaveFileName(
            self, "Export",
            f"DRDO_{self.title.replace(' ', '_')}.png", "PNG (*.png)")
        if not path:
            return
        try:
            pg.exporters.ImageExporter(self.plot_widget.plotItem).export(path)
        except Exception as e:
            QMessageBox.warning(self, "Export Error", str(e))

    def set_paused(self, v: bool):
        
        """Called by PlotManager.set_paused(). Freezes updates when True."""
        
        self._paused = v


# ── Acceleration tab ───────────────────────────────────────────────────────────

class AccelTab(PlotTab):
    
    """
    Three overlapping EMA-smoothed curves on one canvas:
        X = blue    Y = green    Z = red (negated for display — see note below)

    Per-axis checkboxes toggle curve visibility.
    Per-axis spinboxes add a display offset (raw data is never modified).

    Z SIGN CONVENTION (v2.7.1):
    The ADXL345 with its face up reads ~+9.81 m/s² on Z (gravity).
    Negating Z for display puts it below the zero line, matching the negative
    Z values shown in the AXIS Z sidebar box (DataHandler.get_axis_stats()
    also negates current/mean for display). Raw data, magnitude, vibration
    counting, and CSV columns remain unsigned.
    """

    def __init__(self, parent=None):
        super().__init__("Acceleration  X / Y / Z", "Acceleration  (m/s²)", parent)

        # Per-axis display offsets (m/s²). 0.0 by default.
        
        self._xoff = 0.0
        self._yoff = 0.0
        self._zoff = 0.0

        # ── Axis visibility checkboxes ──────────────────────────────────────
        
        for color, label, attr in [
            (CLR_BLUE,  "X", "_chk_x"),
            (CLR_GREEN, "Y", "_chk_y"),
            (CLR_RED,   "Z", "_chk_z"),
        ]:
            chk = QCheckBox(f"  {label}")
            chk.setChecked(True)
            chk.setStyleSheet(_chk_style(color))
            setattr(self, attr, chk)
            self._insert_top(chk)

        sep1 = QLabel("  |  ")
        sep1.setStyleSheet(f"color:{CLR_DIM};background:transparent;")
        self._insert_top(sep1)

        # ── Per-axis offset spinboxes ───────────────────────────────────────
        
        wx, self._spin_xoff = _off_spin("X", CLR_BLUE)
        self._insert_top(wx)
        self._spin_xoff.valueChanged.connect(self._on_xoff)

        wy, self._spin_yoff = _off_spin("Y", CLR_BLUE)
        self._insert_top(wy)
        self._spin_yoff.valueChanged.connect(self._on_yoff)

        wz, self._spin_zoff = _off_spin("Z", CLR_BLUE)
        self._insert_top(wz)
        self._spin_zoff.valueChanged.connect(self._on_zoff)

        sep2 = QLabel("  |  ")
        sep2.setStyleSheet(f"color:{CLR_DIM};background:transparent;")
        self._insert_top(sep2)

        # ── PlotDataItem curves (no legend widget — checkboxes serve that role)
        
        self._cv_x = self.plot_widget.plot(pen=_pen(CLR_BLUE,  PLOT_PEN_W + 0.2))
        self._cv_y = self.plot_widget.plot(pen=_pen(CLR_GREEN, PLOT_PEN_W + 0.2))
        self._cv_z = self.plot_widget.plot(pen=_pen(CLR_RED,   PLOT_PEN_W + 0.2))

        # Wire checkboxes directly to the curve's setVisible slot.
        
        self._chk_x.toggled.connect(self._cv_x.setVisible)
        self._chk_y.toggled.connect(self._cv_y.setVisible)
        self._chk_z.toggled.connect(self._cv_z.setVisible)

    # Offset slot handlers — store latest value so update_data() can use them.
    
    def _on_xoff(self, v: float): self._xoff = v
    def _on_yoff(self, v: float): self._yoff = v
    def _on_zoff(self, v: float): self._zoff = v

    def update_data(self, snap: dict, **_):
        
        """
        Push new curve data for all three axes.

        Only visible curves are updated to avoid computing and uploading EMA
        arrays for hidden channels (a small but measurable saving at 12.5 Hz
        when, e.g., the operator has Z hidden during a horizontal-only test).

        Z offset is applied after negation: displayed = -raw + offset.
        """
        if self._paused or snap["n"] < 2:
            return
        t = _elapsed(snap)

        if self._cv_x.isVisible():
            self._cv_x.setData(t, _ema(snap["ax"] + self._xoff))
        if self._cv_y.isVisible():
            self._cv_y.setData(t, _ema(snap["ay"] + self._yoff))
        if self._cv_z.isVisible():
            # Negate first (display convention), then apply offset.
            self._cv_z.setData(t, _ema(-snap["az"] + self._zoff))

        self._apply_live_range(t)

        # Stat strip computes on magnitude so all three axes contribute equally.
        
        mag = snap["mag"]
        self.stat_row.update_stats({
            "min":  float(mag.min()),
            "max":  float(mag.max()),
            "mean": float(mag.mean()),
            "peak": float(np.abs(mag).max()),
        })

    def clear(self):
        
        """Wipe curve data and reset the stat strip (called by PlotManager.clear_plots)."""
        
        for cv in (self._cv_x, self._cv_y, self._cv_z):
            cv.setData([], [])
        self.stat_row.reset()


# ── Vibration tab ──────────────────────────────────────────────────────────────

class VibrationTab(PlotTab):
    
    """
    Plots RAW (unsmoothed) magnitude |a| = sqrt(ax² + ay² + az²).

    WHY raw (no EMA)?
    EMA smoothing would visually lower short-duration peaks below the threshold
    line. If the operator sees a raw peak just above the line but EMA brings it
    below, the plot would appear to contradict the vibration counter — a
    confusing mismatch. Raw data ensures the displayed signal matches the
    counting logic in DataHandler exactly.

    DEFAULT THRESHOLD: 12.0 m/s² — above the ~9.81 m/s² gravity baseline,
    so a clear 0.22 g seismic event or mechanical shock triggers a count.

    The amber threshold line is draggable. Dragging updates both the spinbox
    and DataHandler simultaneously so all three sources stay in sync.
    """

    def __init__(self, data_handler: DataHandler, parent=None):
        super().__init__("Vibration Analysis", "Magnitude  |a|  (m/s²)", parent)
        self._dh        = data_handler
        self._threshold = data_handler.vibration_threshold

        # Raw magnitude curve (no EMA — intentional, see class docstring).
        
        self._curve = self.plot_widget.plot(
            pen=_pen(CLR_MAG, PLOT_PEN_W))

        # Draggable threshold line — no label= to avoid the BUG-LABEL issue
        # (InfiniteLine.label raises AttributeError on pyqtgraph < 0.12.3).
        # The threshold value is shown in the spinbox below.
        
        self._thresh_line = pg.InfiniteLine(
            pos=self._threshold, angle=0, movable=True,
            pen=pg.mkPen(CLR_THRESH, width=2.0, style=Qt.PenStyle.DashLine),
            label=f"Threshold: {self._threshold:.1f} m/s²",
            labelOpts={"color": CLR_THRESH,
                       "fill": pg.mkBrush(40, 20, 0, 160)},
        )
        self._thresh_line.sigPositionChangeFinished.connect(self._on_thresh_dragged)
        self.plot_widget.addItem(self._thresh_line)

        # Threshold spinbox + vibration count label row (between toolbar and plot).
        self.thresh_spin = QDoubleSpinBox()
        self.thresh_spin.setRange(1.0, 100.0)
        self.thresh_spin.setSingleStep(0.5)
        self.thresh_spin.setValue(self._threshold)
        self.thresh_spin.setSuffix("  m/s²")
        self.thresh_spin.setFont(R.font("Consolas", 9))
        self.thresh_spin.setStyleSheet(
            f"QDoubleSpinBox{{background:#050F1E;color:{CLR_GOLD};"
            f"border:1px solid {CLR_AXIS};padding:3px;border-radius:3px;}}")
        self.thresh_spin.valueChanged.connect(self._on_thresh_spin)

        self.count_label = QLabel("Vibrations:   0")
        self.count_label.setFont(R.font("Consolas", 12, bold=True))
        self.count_label.setStyleSheet(
            f"color:{CLR_THRESH};background:#0A0500;"
            f"border:1px solid {CLR_THRESH};"
            f"border-radius:3px;padding:3px 12px;")
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        info = QHBoxLayout()
        k = QLabel("Threshold:")
        k.setFont(R.font("Segoe UI", 9))
        k.setStyleSheet(f"color:{CLR_WHITE};background:transparent;")
        info.addWidget(k)
        info.addWidget(self.thresh_spin)
        info.addStretch()
        info.addWidget(self.count_label)
        
        # insertLayout(1, …) places the row after the top bar (index 0) and
        # before the plot widget (which shifts from index 1 to index 2).
        
        self._outer.insertLayout(1, info)

    def update_data(self, snap: dict, **_):
        
        """Push raw magnitude data and update the vibration count label."""
        
        if self._paused or snap["n"] < 2:
            return
        t   = _elapsed(snap)
        mag = snap["mag"]   # intentionally raw — no EMA

        self._curve.setData(t, mag)
        self._apply_live_range(t)

        self.stat_row.update_stats({
            "min":  float(mag.min()),
            "max":  float(mag.max()),
            "mean": float(mag.mean()),
            "peak": float(np.abs(mag).max()),
        })
        self.count_label.setText(f"Vibrations:   {self._dh.vibration_count}")

    def _on_thresh_spin(self, v: float):
        
        """
        Spinbox value changed → move the threshold line and update DataHandler.

        BUG-LABEL fix (v2.8.4): the previous version called
            self._thresh_line.label.setFormat(…)
        InfiniteLine has no label= argument here, so .label is a dummy that
        raises AttributeError on pyqtgraph < 0.12.3. The value is already
        visible in the spinbox — no label update is needed.
        """
        self._threshold = v
        self._thresh_line.setValue(v)
        self._dh.set_vibration_threshold(v)

    def _on_thresh_dragged(self):
        
        """
        Threshold line dragged → clamp to spinbox bounds and sync everything.

        BUG-DRAG fix (v2.8.4): before this fix, dragging the line below the
        spinbox minimum (1.0 m/s²) passed the raw position (possibly negative)
        directly to DataHandler.set_vibration_threshold(). The spinbox clamped
        visually but DataHandler used the wrong value — a hidden display/data
        mismatch. Now we clamp first and snap the line back if needed.
        """
        raw = self._thresh_line.value()
        lo  = self.thresh_spin.minimum()    # 1.0 m/s²
        hi  = self.thresh_spin.maximum()    # 100.0 m/s²
        v   = round(max(lo, min(hi, raw)), 1)
        if abs(raw - v) > 1e-6:
            self._thresh_line.setValue(v)   # snap line back into valid range
        self._threshold = v
        
        # blockSignals prevents _on_thresh_spin from firing again (infinite loop).
        
        self.thresh_spin.blockSignals(True)
        self.thresh_spin.setValue(v)
        self.thresh_spin.blockSignals(False)
        self._dh.set_vibration_threshold(v)

    def clear(self):
        """Wipe curve data, reset count label and stat strip."""
        self._curve.setData([], [])
        self.count_label.setText("Vibrations:   0")
        self.stat_row.reset()


# ── Plot manager ───────────────────────────────────────────────────────────────

class PlotManager(QTabWidget):
    
    """
    The QTabWidget that occupies the centre of the GUI.

    Owns all four plot tabs and drives the three live ones from refresh().
    The CSV Viewer tab (Tab 4) is static and not included in refresh().

    refresh() is called at 12.5 Hz by DRDOController._on_plot_tick().
    It takes a single DataHandler snapshot per tick and passes it to all
    three live tabs — avoiding three separate lock acquisitions per frame.
    """

    def __init__(self, data_handler: DataHandler, parent=None):
        super().__init__(parent)
        self._dh     = data_handler
        self._paused = False
        self._setup_style()

        # Tab 1 — live acceleration curves (EMA-smoothed X, Y, Z)
        
        self.tab_accel = AccelTab()
        self.addTab(self.tab_accel, "⚡ Acceleration  X / Y / Z")

        # Tab 2 — raw magnitude + vibration threshold and event count
        
        self.tab_vib = VibrationTab(data_handler=self._dh)
        self.addTab(self.tab_vib, "📊 Vibration")

        # Tab 3 — STA/LTA seismic detector + FFT + event log
        
        self.tab_seismic = SeismicTab()
        self.addTab(self.tab_seismic, "🌍 Seismic")

        # Tab 4 — static CSV viewer (browse + plot any saved ADXL345 CSV)
        
        self.tab_csv = CSVViewerTab()
        self.addTab(self.tab_csv, "📂 CSV Viewer")

    def _setup_style(self):
        
        """Apply the military-blue tab bar stylesheet."""
        
        self.setStyleSheet("""
            QTabWidget::pane {
                background: #030B16;
                border: 1px solid #1E4A7A;
                border-radius: 2px;
            }
            QTabBar::tab {
                background: #0A1E35; color: #3A6A9A;
                border: 1px solid #1E4A7A;
                padding: 6px 24px; font: 9pt "Segoe UI"; min-width: 140px;
            }
            QTabBar::tab:selected {
                background: #0F2840; color: #FFD700;
                border-bottom: 2px solid #FFD700;
            }
            QTabBar::tab:hover { background: #1A3A5A; color: #E8F0FF; }
        """)

    def refresh(self):
        
        """
        Called at 12.5 Hz to push fresh data to the three live plot tabs.

        One snapshot per tick is shared across all three tabs. This is correct
        because all tabs display the same FULL_BUF = 40 000 samples — taking
        three separate snapshots would be redundant and wasteful.

        The CSV Viewer tab has no live refresh path and is excluded here.
        """
        if self._paused:
            return
        snap = self._dh.get_snapshot(FULL_BUF)
        if snap["n"] < 2:
            return
        self.tab_accel.update_data(snap)
        self.tab_vib.update_data(snap)
        self.tab_seismic.update_data(snap)

    def clear_plots(self):
        
        """Wipe all curve data on the three live tabs. CSV Viewer is left intact."""
        
        self.tab_accel.clear()
        self.tab_vib.clear()
        self.tab_seismic.clear()
        self.tab_csv.clear()    # no-op by design: keeps loaded CSV on screen

    def set_paused(self, v: bool):
        
        """Propagate pause state to all four tabs."""
        
        self._paused = v
        self.tab_accel.set_paused(v)
        self.tab_vib.set_paused(v)
        self.tab_seismic.set_paused(v)
        self.tab_csv.set_paused(v)   # no-op — CSV viewer has no live refresh
