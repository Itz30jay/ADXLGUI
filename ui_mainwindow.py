"""
DRDO PXE — ADXL345 Data Acquisition System
ui_mainwindow.py  |  v2.3.0  (responsive)

WHAT THIS FILE DOES
-------------------
Builds every visual widget in the application. MainWindow is a "dumb view" —
it constructs and exposes widgets but contains zero business logic. Every user
action is forwarded to DRDOController in main.py via Qt signals.

RESPONSIVE LAYOUT
-----------------
Every hardcoded pixel or font-point value has been replaced with R.px() or
R.pt() from responsive.py. The QSplitter uses R.splitter_sizes() instead of
fixed pixel widths so the three panes stay proportional at any resolution.

The window opens at min(2000, 85 % screen width) × min(1200, 88 % screen
height) — the hard cap on 2000 px is what prevents the "football pitch" look
on large monitors.

BUG-SHADOW fix (v2.2.2):
AxisStatBox.set_stats() / VibrationStatsBox.set_stats() — renamed from
update() which silently shadowed QWidget.update() (no-arg repaint scheduler).
"""

from __future__ import annotations

import os
from datetime import datetime

from PyQt6.QtCore    import Qt, QTimer
from PyQt6.QtGui     import (
    QColor, QPalette, QPixmap, QIcon, QShortcut, QKeySequence,
)
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QGroupBox, QFrame,
    QSplitter, QCheckBox, QSpinBox, QScrollArea, QMessageBox,
)

from responsive import R   # all dimensions go through R.px() / R.pt()

# ── Military-blue palette ──────────────────────────────────────────────────────

C_BG        = "#162840"
C_PANEL     = "#0D1F35"
C_HEADER    = "#091828"
C_BORDER    = "#1E4A7A"
C_GOLD      = "#FFD700"
C_GREEN     = "#00FF88"
C_RED       = "#FF4444"
C_WHITE     = "#E8F0FF"
C_DIM       = "#6A8AAA"
C_CYAN      = "#00E5FF"
C_THRESH    = "#FF8C00"
C_BLUE      = "#4A90D9"
C_GRNAXIS   = "#39FF14"
C_REDAXIS   = "#E74C3C"
C_BTN_START = "#003320"
C_BTN_STOP  = "#330A0A"
C_BTN_SAVE  = "#2A1E00"
C_BTN_CLR   = "#0A1828"


# ── Widget factory helpers ─────────────────────────────────────────────────────

# Builds a styled QLabel without repeating font/stylesheet setup everywhere.
# Uses R.pt() so font sizes scale correctly on high-DPI / large displays.

def _lbl(text, pt=9, bold=False, color=C_WHITE, family="Segoe UI") -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(R.font(family, pt, bold))
    lbl.setStyleSheet(f"color:{color};background:transparent;")
    return lbl

# Creates a 1-px horizontal rule between sidebar sections.

def _sep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet(f"color:{C_BORDER};")
    f.setFixedHeight(1)
    return f


# Creates a styled QGroupBox with a gold title — used for all sidebar sections.

def _group(title: str) -> QGroupBox:
    gb = QGroupBox(title)
    gb.setFont(R.font("Segoe UI", 8, bold=True))
    gb.setStyleSheet(f"""
        QGroupBox {{
            color:{C_GOLD}; border:1px solid {C_BORDER};
            border-radius:4px; margin-top:{R.px(10)}px; padding:{R.px(4)}px;
        }}
        QGroupBox::title {{
            subcontrol-origin:margin; subcontrol-position:top left;
            padding:0 4px; color:{C_GOLD}; background:{C_PANEL};
        }}
    """)
    return gb


# ── LED status indicator ───────────────────────────────────────────────────────

# 16×16 circular LED. Pure stylesheet (border-radius = half size) so no
# custom paintEvent is required. R.px(16) scales to the display density.

class LEDIndicator(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        d = R.px(16)
        self.setFixedSize(d, d)
        self._on = False
        self._repaint(C_RED)

    def set_state(self, on: bool):
        self._on = on
        self._repaint(C_GREEN if on else C_RED)

    def _repaint(self, color: str):
        r  = R.px(8)
        bd = C_GREEN if self._on else "#882222"
        self.setStyleSheet(
            f"QLabel{{background:{color};border-radius:{r}px;"
            f"border:1px solid {bd};}}")


# ── Per-axis stat box ──────────────────────────────────────────────────────────

# Fixed-size box showing Real / Peak / Mean for one axis (X, Y, or Z).
# Coloured border matches the plot curve for instant visual association.
# Renamed set_stats() (was update()) to avoid shadowing QWidget.update().

class AxisStatBox(QFrame):
    _COLORS = {"X": C_BLUE, "Y": C_BLUE, "Z": C_BLUE}

    def __init__(self, axis: str, parent=None):
        super().__init__(parent)
        color = self._COLORS.get(axis.upper(), C_WHITE)
        self.setStyleSheet(
            f"QFrame{{background:#000000;border:1px solid {color};"
            f"border-radius:{R.px(5)}px;}}")
        v = QVBoxLayout(self)
        v.setContentsMargins(R.px(10), R.px(7), R.px(10), R.px(7))
        v.setSpacing(R.px(4))

        hdr = QLabel(f"AXIS  {axis.upper()}")
        hdr.setFont(R.font("Segoe UI", 11, bold=True))
        hdr.setStyleSheet(f"color:{C_WHITE};background:transparent;")
        hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(hdr)

        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet(f"color:{color};")
        div.setFixedHeight(1)
        v.addWidget(div)

        self._lbl_real = self._row(v, "Real Value",  C_WHITE)
        self._lbl_peak = self._row(v, "Peak Value",  "#FF8C00")
        self._lbl_mean = self._row(v, "Mean Value",  C_CYAN)

        unit = QLabel("m / s²")
        unit.setFont(R.font("Segoe UI", 9))
        unit.setStyleSheet(f"color:{C_WHITE};background:transparent;")
        unit.setAlignment(Qt.AlignmentFlag.AlignRight)
        v.addWidget(unit)

    # Adds one key-value row to the stat box and returns the value label.
    
    @staticmethod
    def _row(layout, label: str, vc: str) -> QLabel:
        h = QHBoxLayout()
        k = QLabel(label)
        k.setFont(R.font("Segoe UI", 8))
        k.setStyleSheet(f"color:{C_WHITE};background:transparent;")
        h.addWidget(k)
        h.addStretch()
        val = QLabel("---")
        val.setFont(R.font("Consolas", 11, bold=True))
        val.setStyleSheet(f"color:{vc};background:transparent;")
        val.setAlignment(Qt.AlignmentFlag.AlignRight)
        h.addWidget(val)
        layout.addLayout(h)
        return val

    # Receives dict {"current", "peak", "mean"} from DRDOController at 2 Hz.
    
    def set_stats(self, s: dict):
        fmt = lambda v: f"{v:+.3f}"
        self._lbl_real.setText(fmt(s.get("current", 0.0)))
        self._lbl_peak.setText(fmt(s.get("peak",    0.0)))
        self._lbl_mean.setText(fmt(s.get("mean",    0.0)))


# ── Vibration monitor box ──────────────────────────────────────────────────────

# Shows vibration event count (large) plus magnitude Min/Max/Mean.
# Amber border matches the threshold line in the Vibration plot tab.

class VibrationStatsBox(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"QFrame{{background:#0A0F05;border:1px solid {C_BLUE};"
            f"border-radius:{R.px(5)}px;}}")
        v = QVBoxLayout(self)
        v.setContentsMargins(R.px(10), R.px(8), R.px(10), R.px(8))
        v.setSpacing(R.px(5))

        title = QLabel("VIBRATION MONITOR")
        title.setFont(R.font("Segoe UI", 9, bold=True))
        title.setStyleSheet(f"color:{C_WHITE};background:transparent;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)

        # Large event count — 22 pt Consolas is legible across the room.
        
        self._count = QLabel("0")
        self._count.setFont(R.font("Consolas", 18, bold=True))
        self._count.setStyleSheet(f"color:{C_WHITE};background:transparent;")
        self._count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._count)

        note = QLabel("Vibrations Detected")
        note.setFont(R.font("Segoe UI", 9, bold=True))
        note.setStyleSheet(f"color:{C_WHITE};background:transparent;")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(note)

        v.addWidget(_sep())
        self._min  = AxisStatBox._row(v, "Min  |a|", C_WHITE)
        self._max  = AxisStatBox._row(v, "Max  |a|", "#FF8C00")
        self._mean = AxisStatBox._row(v, "Mean |a|", C_CYAN)

        """unit = QLabel("m / s²")
        unit.setFont(R.font("Segoe UI", 9))
        unit.setStyleSheet(f"color:{C_WHITE};background:transparent;")
        unit.setAlignment(Qt.AlignmentFlag.AlignRight)
        v.addWidget(unit)"""

    # Receives vibration stats dict from DRDOController at 2 Hz.
    # Shows "---" when count = 0 to avoid confusing zero values before any events.
    
    def set_stats(self, s: dict):
        self._count.setText(str(s.get("count", 0)))
        nz  = s.get("count", 0) > 0
        fmt = lambda v: f"{v:.3f}" if nz else "---"
        self._min .setText(fmt(s.get("min",  0.0)))
        self._max .setText(fmt(s.get("max",  0.0)))
        self._mean.setText(fmt(s.get("mean", 0.0)))


# ── Right stats sidebar panel ─────────────────────────────────────────────────

# 220 px wide on the reference display; scales proportionally via
# R.right_sidebar_w(). Contains X/Y/Z stat boxes + vibration monitor.

class AxisStatsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        w = R.right_sidebar_w()
        self.setMinimumWidth(R.px(180))
        self.setMaximumWidth(R.px(340))
        self.setFixedWidth(w)
        self.setStyleSheet(
            f"background:{C_PANEL};border-left:1px solid {C_BORDER};")
        v = QVBoxLayout(self)
        v.setContentsMargins(R.px(10), R.px(10), R.px(10), R.px(10))
        v.setSpacing(R.px(8))

        title = QLabel("LIVE STATISTICS")
        title.setFont(R.font("Segoe UI", 11, bold=True))
        title.setStyleSheet(f"color:{C_GOLD};background:transparent;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)
        v.addWidget(_sep())

        self._box_x = AxisStatBox("X")
        self._box_y = AxisStatBox("Y")
        self._box_z = AxisStatBox("Z")
        v.addWidget(self._box_x)
        v.addWidget(self._box_y)
        v.addWidget(self._box_z)
        v.addStretch()
        v.addWidget(_sep())
        self._vib_box = VibrationStatsBox()
        v.addWidget(self._vib_box)

    def update_stats(self, axis_stats: dict):
        self._box_x.set_stats(axis_stats.get("x", {}))
        self._box_y.set_stats(axis_stats.get("y", {}))
        self._box_z.set_stats(axis_stats.get("z", {}))

    def update_vibration(self, vib_stats: dict):
        self._vib_box.set_stats(vib_stats)


# ── Main Window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    
    """
    Top-level application window — purely presentational.

    RESPONSIVE CHANGES (v2.3.0):
    • Window size is computed by R.window_size() — 85 % of screen, capped
      at 2000×1200.  This is the primary fix for the 'football pitch' effect.
    • QSplitter uses R.splitter_sizes() — proportional, not hardcoded pixels.
    • Sidebars have min/max width bounds so they stay sensible at every resolution.
    • All setFixedHeight(), setFixedSize(), font(), QPixmap.scaled() calls use
      R.px() / R.pt() / R.font() / R.sz().
    • Window is centred on the primary screen after construction.
    """

    def __init__(self):
        super().__init__()
        self._setup_window()
        self._apply_palette()
        self._build_ui()
        self._setup_shortcuts()
        self._start_clock()
        self._centre_on_screen()

    # ── Window-level setup ─────────────────────────────────────────────────────

    # Sets the title, responsive minimum/initial size, and taskbar icon.
    
    def _setup_window(self):
        self.setWindowTitle(
            "DRDO PXE — ADXL345 Real-Time Acceleration Acquisition  v3.0")
        mw, mh = R.min_window_size()
        self.setMinimumSize(mw, mh)
        w, h = R.window_size()
        self.resize(w, h)
        logo = os.path.join(os.path.dirname(__file__), "resources", "drdo_logo.png")
        if os.path.exists(logo):
            self.setWindowIcon(QIcon(logo))

    # Applies the dark military-blue QPalette so all standard Qt widgets
    # inherit the theme even without an explicit stylesheet.
    
    def _apply_palette(self):
        pal = QPalette()
        pal.setColor(QPalette.ColorRole.Window,        QColor(C_BG))
        pal.setColor(QPalette.ColorRole.WindowText,    QColor(C_WHITE))
        pal.setColor(QPalette.ColorRole.Base,          QColor(C_PANEL))
        pal.setColor(QPalette.ColorRole.AlternateBase, QColor(C_BG))
        pal.setColor(QPalette.ColorRole.Text,          QColor(C_WHITE))
        pal.setColor(QPalette.ColorRole.Button,        QColor(C_PANEL))
        pal.setColor(QPalette.ColorRole.ButtonText,    QColor(C_WHITE))
        self.setPalette(pal)

    # Centres the window on the primary screen after resize() sets the size.
    # frameGeometry() is used so the title bar is included in the calculation.
    
    def _centre_on_screen(self):
        from PyQt6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen:
            fg = self.frameGeometry()
            fg.moveCenter(screen.availableGeometry().center())
            self.move(fg.topLeft())

    # ── Full UI assembly ───────────────────────────────────────────────────────

    # Three-row stack: header (fixed height) → body (stretches) → status bar.
    
    def _build_ui(self):
        c = QWidget()
        self.setCentralWidget(c)
        root = QVBoxLayout(c)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())
        root.addWidget(self._build_body(), stretch=1)
        root.addWidget(self._build_statusbar_widget())

    # ── Header bar ─────────────────────────────────────────────────────────────

    # 60-px gradient header with logo, instrument title, clock, user, and LED.
    # R.px(60) ensures the header is proportionally taller on 4K displays.
    
    def _build_header(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(R.px(60))
        bar.setStyleSheet(
            f"QFrame{{background:qlineargradient("
            f"x1:0,y1:0,x2:1,y2:0,"
            f"stop:0 #050F1E,stop:0.4 {C_HEADER},stop:1 #050F1E);"
            f"border-bottom:2px solid {C_BORDER};}}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(R.px(12), R.px(4), R.px(12), R.px(4))
        h.setSpacing(R.px(14))

        logo_lbl = QLabel()
        lp = os.path.join(os.path.dirname(__file__), "resources", "drdo_logo.png")
        if os.path.exists(lp):
            logo_lbl.setPixmap(
                QPixmap(lp).scaled(R.sz(44, 44),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
        else:
            logo_lbl.setText("DRDO")
            logo_lbl.setFont(R.font("Segoe UI", 10, bold=True))
            logo_lbl.setStyleSheet(f"color:{C_GOLD};")
        h.addWidget(logo_lbl)

        org = QVBoxLayout()
        org.setSpacing(0)
        org.addWidget(_lbl("DEFENCE RESEARCH & DEVELOPMENT ORGANISATION",
                           8, True, C_GOLD))
        org.addWidget(_lbl(
            "Proof Experimental Establishment — PXE  |  ADXL345 DAQ v3.0",
            7, False, C_DIM))
        h.addLayout(org)
        h.addStretch()

        ctitle = _lbl("ADXL345  REAL-TIME  ACCELERATION  ACQUISITION",
                      11, True, C_GOLD)
        ctitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(ctitle)
        h.addStretch()

        rc = QVBoxLayout()
        rc.setSpacing(R.px(2))
        self.lbl_datetime = _lbl("", 8, False, C_WHITE, "Consolas")
        self.lbl_datetime.setAlignment(Qt.AlignmentFlag.AlignRight)
        rc.addWidget(self.lbl_datetime)

        ur = QHBoxLayout()
        ur.setSpacing(R.px(4))
        ur.addWidget(_lbl("USER:", 7, False, C_DIM))
        self.lbl_user = _lbl("Jaykishan", 7, True, C_GREEN)
        ur.addWidget(self.lbl_user)
        rc.addLayout(ur)

        sr = QHBoxLayout()
        sr.setSpacing(R.px(6))
        self.led_status = LEDIndicator()
        sr.addWidget(self.led_status)
        self.lbl_conn_status = _lbl("OFFLINE", 8, True, C_RED)
        sr.addWidget(self.lbl_conn_status)
        rc.addLayout(sr)
        h.addLayout(rc)
        return bar

    # ── Body — three-pane responsive splitter ─────────────────────────────────

    # The QSplitter uses R.splitter_sizes() which computes proportional widths
    # from the current screen size — proportional, not fixed pixels.
    # This eliminates the 'football pitch' effect on wide displays.
    
    def _build_body(self) -> QSplitter:
        sp = QSplitter(Qt.Orientation.Horizontal)
        sp.setStyleSheet(f"QSplitter::handle{{background:{C_BORDER};width:2px;}}")
        sp.addWidget(self._build_sidebar())
        sp.addWidget(self._build_centre())
        sp.addWidget(self._build_stats_sidebar())
        # Proportional split: side bars widen/narrow with the window.
        w, _ = R.window_size()
        sp.setSizes(R.splitter_sizes(w))
        sp.setCollapsible(0, False)
        sp.setCollapsible(1, False)
        sp.setCollapsible(2, False)
        return sp

    # ── Left sidebar ───────────────────────────────────────────────────────────

    # Left control pane. R.left_sidebar_w() replaces the hardcoded 290 px —
    # it computes ~19 % of screen width, clamped to [230, 430] px.
    # A QScrollArea prevents controls from being clipped on small displays.
    
    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        w = R.left_sidebar_w()
        side.setMinimumWidth(R.px(230))
        side.setMaximumWidth(R.px(430))
        side.setFixedWidth(w)
        side.setStyleSheet(
            f"background:{C_PANEL};border-right:1px solid {C_BORDER};")

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea{border:none;background:transparent;}"
            "QScrollBar:vertical{background:#091828;width:6px;border-radius:3px;}"
            "QScrollBar::handle:vertical{background:#1E4A7A;border-radius:3px;"
            "min-height:20px;}"
            "QScrollBar::add-line:vertical,"
            "QScrollBar::sub-line:vertical{height:0;}")

        inner = QWidget()
        inner.setStyleSheet(f"background:{C_PANEL};")
        v = QVBoxLayout(inner)
        v.setContentsMargins(R.px(8), R.px(8), R.px(8), R.px(8))
        v.setSpacing(R.px(8))

        v.addWidget(self._build_udp_group())
        v.addWidget(self._build_user_group())
        v.addWidget(self._build_file_group())
        v.addWidget(self._build_acq_group())
        v.addStretch()
        v.addWidget(self._build_action_buttons())

        scroll.setWidget(inner)
        ol = QVBoxLayout(side)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.addWidget(scroll)
        return side

    # UDP connection controls — port, auto-reconnect, CONNECT button.
    
    def _build_udp_group(self) -> QGroupBox:
        from PyQt6.QtWidgets import QGridLayout
        g = QGridLayout()
        g.setSpacing(R.px(5))
        g.setColumnStretch(1, 1)

        g.addWidget(_lbl("Protocol:", 8), 0, 0)
        g.addWidget(_lbl("UDP (Ethernet)", 9, True, C_CYAN, "Consolas"), 0, 1, 1, 2)

        g.addWidget(_lbl("Listen IP:", 8), 1, 0)
        g.addWidget(_lbl("0.0.0.0  (all interfaces)", 8, False, C_DIM, "Consolas"),
                    1, 1, 1, 2)

        g.addWidget(_lbl("UDP Port:", 8), 2, 0)
        self.spin_udp_port = QSpinBox()
        self.spin_udp_port.setRange(1024, 65535)
        self.spin_udp_port.setValue(8888)
        self.spin_udp_port.setFont(R.font("Consolas", 9))
        self.spin_udp_port.setStyleSheet(self._spin_s())
        g.addWidget(self.spin_udp_port, 2, 1, 1, 2)

        self.chk_auto_reconnect_udp = QCheckBox("Auto Reconnect")
        self.chk_auto_reconnect_udp.setChecked(True)
        self.chk_auto_reconnect_udp.setStyleSheet(self._chk_s())
        g.addWidget(self.chk_auto_reconnect_udp, 3, 0, 1, 3)

        self.btn_connect_udp = QPushButton("⬛  CONNECT UDP")
        self.btn_connect_udp.setFixedHeight(R.px(32))
        self.btn_connect_udp.setStyleSheet(
            f"QPushButton{{background:#001A22;color:{C_CYAN};"
            f"border:1px solid {C_CYAN};border-radius:3px;"
            f"font:{R.pt(9)}pt 'Segoe UI';font-weight:bold;}}"
            f"QPushButton:hover{{background:{C_CYAN};color:#000;}}"
            f"QPushButton:disabled{{background:#0A1828;"
            f"color:#1E4A7A;border-color:#0D2035;}}")
        g.addWidget(self.btn_connect_udp, 4, 0, 1, 3)

        pkt = _lbl("Packet: 6 B  struct{int16 x, y, z}", 7, False, C_DIM, "Consolas")
        pkt.setWordWrap(True)
        g.addWidget(pkt, 5, 0, 1, 3)

        gb = _group("ETHERNET UDP — ADXL345")
        gb.setLayout(g)
        return gb

    # Operator name field — embedded in auto-generated CSV filenames.
    
    def _build_user_group(self) -> QGroupBox:
        h = QHBoxLayout()
        h.addWidget(_lbl("Name:", 8))
        self.edit_username = QLineEdit("Jaykishan")
        self.edit_username.setFont(R.font("Consolas", 9))
        self.edit_username.setStyleSheet(self._edit_s())
        h.addWidget(self.edit_username)
        gb = _group("OPERATOR")
        gb.setLayout(h)
        return gb

    # Output folder picker + live filename preview.
    
    def _build_file_group(self) -> QGroupBox:
        vl = QVBoxLayout()
        vl.setSpacing(R.px(5))

        row = QHBoxLayout()
        row.addWidget(_lbl("Dir:", 8))
        self.edit_folder = QLineEdit(os.path.expanduser("~"))
        self.edit_folder.setFont(R.font("Consolas", 8))
        self.edit_folder.setStyleSheet(self._edit_s())
        row.addWidget(self.edit_folder)

        self.btn_browse = QPushButton("…")
        self.btn_browse.setFixedSize(R.px(28), R.px(26))
        self.btn_browse.setStyleSheet(
            f"QPushButton{{background:#050F1E;color:{C_GOLD};"
            f"border:1px solid {C_BORDER};border-radius:3px;"
            f"font:{R.pt(10)}pt Consolas;}}"
            f"QPushButton:hover{{background:{C_GOLD};color:#000;}}")
        row.addWidget(self.btn_browse)
        vl.addLayout(row)

        vl.addWidget(_lbl("Auto filename:", 7, False, C_DIM))
        self.lbl_filename = QLabel("ADXL345_Jaykishan_2026-01-01.csv")
        self.lbl_filename.setFont(R.font("Consolas", 7))
        self.lbl_filename.setStyleSheet(
            f"color:{C_CYAN};background:#050F1E;"
            f"padding:{R.px(3)}px;border-radius:2px;")
        self.lbl_filename.setWordWrap(True)
        vl.addWidget(self.lbl_filename)

        gb = _group("OUTPUT FILE")
        gb.setLayout(vl)
        return gb

    # CSV logging and pause-plot checkboxes.
    
    def _build_acq_group(self) -> QGroupBox:
        vl = QVBoxLayout()
        vl.setSpacing(R.px(5))

        self.chk_logging = QCheckBox("Enable Real-time CSV Logging")
        self.chk_logging.setChecked(True)
        self.chk_logging.setStyleSheet(self._chk_s())
        vl.addWidget(self.chk_logging)

        self.chk_pause_plot = QCheckBox("Pause Plot  (keep logging)")
        self.chk_pause_plot.setChecked(False)
        self.chk_pause_plot.setStyleSheet(self._chk_s())
        vl.addWidget(self.chk_pause_plot)

        gb = _group("ACQUISITION OPTIONS")
        gb.setLayout(vl)
        return gb

    # Five primary action buttons pinned to sidebar bottom.
    # All heights use R.px() so they scale correctly on 4K displays.
    
    def _build_action_buttons(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame{{background:{C_PANEL};"
            f"border-top:1px solid {C_BORDER};}}")
        v = QVBoxLayout(frame)
        v.setContentsMargins(R.px(8), R.px(10), R.px(8), R.px(10))
        v.setSpacing(R.px(6))

        self.btn_start = self._act_btn(
            "▶  START ACQUISITION", C_BTN_START, "#00BB00", "#00FF88")
        self.btn_stop  = self._act_btn(
            "■   STOP ACQUISITION",  C_BTN_STOP,  "#AA0000", "#FF4444")
        self.btn_save  = self._act_btn(
            "⬇  SAVE  DATA",         C_BTN_SAVE,  "#AA7700", C_GOLD)
        self.btn_clear = self._act_btn(
            "✕  CLEAR  ALL",         C_BTN_CLR,   "#1E4A7A", "#6A9ACA")

        self.btn_stop.setEnabled(False)

        v.addWidget(self.btn_start)
        v.addWidget(self.btn_stop)
        v.addWidget(self.btn_save)
        v.addWidget(self.btn_clear)
        v.addWidget(_sep())

        self.btn_disconnect = QPushButton("⬜   DISCONNECT")
        self.btn_disconnect.setFixedHeight(R.px(34))
        self.btn_disconnect.setStyleSheet(
            "QPushButton{background:#1A0808;color:#CC4444;"
            "border:1px solid #553333;border-radius:4px;"
            f"font:{R.pt(9)}pt 'Segoe UI';font-weight:bold;}}"
            "QPushButton:hover{background:#2A0F0F;"
            f"border-color:{C_RED};color:{C_RED};}}"
            "QPushButton:disabled{color:#2A1010;border-color:#1A0808;}")
        self.btn_disconnect.setEnabled(False)
        v.addWidget(self.btn_disconnect)

        hint = _lbl("Space=Start/Stop  Ctrl+S=Save  Ctrl+L=Clear",
                    6, False, C_DIM)
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(hint)
        return frame

    # Builds a single action button with three visual states (normal/hover/pressed).
    
    @staticmethod
    def _act_btn(label: str, bg: str, border: str, glow: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setFixedHeight(R.px(44))
        btn.setFont(R.font("Segoe UI", 9, bold=True))
        btn.setStyleSheet(
            f"QPushButton{{background:{bg};color:{glow};"
            f"border:1px solid {border};border-radius:4px;letter-spacing:1px;}}"
            f"QPushButton:hover{{background:{border};"
            f"border-color:{glow};color:#FFF;}}"
            f"QPushButton:pressed{{background:{glow};color:#000;}}"
            f"QPushButton:disabled{{background:#091828;color:#1E4A7A;"
            f"border-color:#0D2035;}}")
        return btn

    # ── Centre pane ────────────────────────────────────────────────────────────

    # Black container. PlotManager is injected here by DRDOController.
    
    def _build_centre(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background:#000000;")
        v = QVBoxLayout(w)
        v.setContentsMargins(R.px(4), R.px(4), R.px(4), R.px(4))
        v.setSpacing(0)
        v.addWidget(self._build_plots_placeholder(), stretch=1)
        return w

    # Placeholder label shown before PlotManager is injected.
    
    def _build_plots_placeholder(self) -> QWidget:
        self.plot_container   = QWidget()
        self.plot_container.setStyleSheet("background:#000000;")
        self._plot_layout     = QVBoxLayout(self.plot_container)
        self._plot_layout.setContentsMargins(0, 0, 0, 0)
        self._placeholder_lbl = _lbl(
            "[ PLOT AREA — CONNECT UDP THEN PRESS START ]",
            10, True, C_BORDER)
        self._placeholder_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._plot_layout.addWidget(self._placeholder_lbl)
        return self.plot_container

    # Replaces the placeholder label with the real PlotManager widget.
    
    def inject_plot_widget(self, pw: QWidget):
        if self._placeholder_lbl:
            self._placeholder_lbl.setParent(None)
            self._placeholder_lbl = None
        self._plot_layout.addWidget(pw)

    # ── Right stats sidebar ────────────────────────────────────────────────────

    def _build_stats_sidebar(self) -> AxisStatsPanel:
        self._stats_panel = AxisStatsPanel()
        return self._stats_panel

    # ── Status bar ─────────────────────────────────────────────────────────────

    # 24-px dark bar always visible at the bottom. R.px(24) scales it on 4K.
    
    def _build_statusbar_widget(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(R.px(24))
        bar.setStyleSheet(
            f"QFrame{{background:#050F1E;border-top:1px solid {C_BORDER};}}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(R.px(10), 0, R.px(10), 0)
        h.setSpacing(0)

        def seg(txt="", mn=0) -> QLabel:
            lb = QLabel(txt)
            lb.setFont(R.font("Consolas", 8))
            lb.setStyleSheet(f"color:{C_DIM};background:transparent;padding:0 {R.px(8)}px;")
            if mn:
                lb.setMinimumWidth(R.px(mn))
            return lb

        def vsep() -> QFrame:
            f = QFrame()
            f.setFrameShape(QFrame.Shape.VLine)
            f.setStyleSheet(f"color:{C_BORDER};")
            return f

        self.sb_conn_type  = seg("OFFLINE",       120)
        self.sb_samples    = seg("Samples: 0",    140)
        self.sb_rate       = seg("Rate: — Hz",    100)
        self.sb_datarate   = seg("Data: 0 B/s",   110)
        self.sb_file       = seg("File: —",        260)
        self.sb_logging    = seg("LOGGING: OFF",  120)
        self.sb_vib_count  = seg("Vibrations: 0", 130)

        for w in (self.sb_conn_type, vsep(), self.sb_samples, vsep(),
                  self.sb_rate,      vsep(), self.sb_datarate, vsep(),
                  self.sb_file,      vsep(), self.sb_logging,  vsep(),
                  self.sb_vib_count):
            h.addWidget(w)

        h.addStretch()
        self.sb_msg = seg("SYSTEM READY")
        self.sb_msg.setStyleSheet(
            f"color:{C_GREEN};background:transparent;padding:0 {R.px(8)}px;")
        h.addWidget(self.sb_msg)
        return bar

    # ── Live clock ─────────────────────────────────────────────────────────────

    # 500 ms timer keeps the header clock accurate within one second.
    
    def _start_clock(self):
        t = QTimer(self)
        t.timeout.connect(
            lambda: self.lbl_datetime.setText(
                datetime.now().strftime("%Y-%m-%d   %H:%M:%S")))
        t.start(500)
        self.lbl_datetime.setText(datetime.now().strftime("%Y-%m-%d   %H:%M:%S"))

    # ── Keyboard shortcuts ─────────────────────────────────────────────────────

    def _setup_shortcuts(self):
        def toggle():
            if self.btn_start.isEnabled():
                self.btn_start.click()
            elif self.btn_stop.isEnabled():
                self.btn_stop.click()
        QShortcut(QKeySequence("Space"),  self).activated.connect(toggle)
        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self.btn_save.click)
        QShortcut(QKeySequence("Ctrl+L"), self).activated.connect(self.btn_clear.click)

    # ── Public update helpers called by DRDOController ─────────────────────────

    # Updates LED, connection label, CONNECT/DISCONNECT states, status bar segment.
    
    def update_connection_ui(self, connected: bool, msg: str):
        self.led_status.set_state(connected)
        clr = C_GREEN if connected else C_RED
        self.lbl_conn_status.setText("ONLINE" if connected else "OFFLINE")
        self.lbl_conn_status.setStyleSheet(
            f"color:{clr};background:transparent;")
        self.sb_conn_type.setText(msg[:55] if connected else "OFFLINE")
        self.sb_conn_type.setStyleSheet(
            f"color:{clr};background:transparent;padding:0 {R.px(8)}px;")
        self.btn_disconnect.setEnabled(connected)
        self.sb_msg.setText(msg)

    def update_stats_panel(self, axis_stats: dict):
        self._stats_panel.update_stats(axis_stats)

    def update_vibration_box(self, vib_stats: dict):
        self._stats_panel.update_vibration(vib_stats)

    def update_status_bar(self, samples, logging, file, vib_count,
                          data_rate, sample_hz="— Hz"):
        self.sb_samples  .setText(f"Samples: {samples:,}")
        self.sb_rate     .setText(sample_hz)
        self.sb_logging  .setText("LOGGING: ON" if logging else "LOGGING: OFF")
        self.sb_logging  .setStyleSheet(
            f"color:{C_GREEN if logging else C_DIM};"
            f"background:transparent;padding:0 {R.px(8)}px;")
        self.sb_file     .setText(f"File: {os.path.basename(file) if file else '—'}")
        self.sb_vib_count.setText(f"Vibrations: {vib_count}")
        self.sb_datarate .setText(f"Data: {data_rate}")

    def update_filename_preview(self, name: str):
        self.lbl_filename.setText(name)

    def set_acquisition_active(self, active: bool):
        self.btn_start.setEnabled(not active)
        self.btn_stop .setEnabled(active)

    # Shows a styled QMessageBox that matches the dark application theme.
    
    def show_message(self, title: str, text: str, level: str = "info"):
        mb = QMessageBox(self)
        mb.setWindowTitle(title)
        mb.setText(text)
        mb.setStyleSheet(
            f"QMessageBox{{background:{C_PANEL};color:{C_WHITE};}}"
            f"QPushButton{{background:#050F1E;color:{C_GOLD};"
            f"border:1px solid {C_BORDER};padding:4px 14px;"
            f"border-radius:3px;}}")
        icons = {"info": QMessageBox.Icon.Information,
                 "warning": QMessageBox.Icon.Warning,
                 "error": QMessageBox.Icon.Critical}
        mb.setIcon(icons.get(level, QMessageBox.Icon.Information))
        mb.exec()

    # ── Shared stylesheet helpers ──────────────────────────────────────────────

    @staticmethod
    def _edit_s() -> str:
        return (f"QLineEdit{{background:#050F1E;color:{C_WHITE};"
                f"border:1px solid {C_BORDER};border-radius:3px;"
                f"padding:{R.px(3)}px;}}"
                f"QLineEdit:focus{{border-color:{C_GOLD};}}")

    @staticmethod
    def _spin_s() -> str:
        return (f"QSpinBox{{background:#050F1E;color:{C_WHITE};"
                f"border:1px solid {C_BORDER};border-radius:3px;"
                f"padding:{R.px(2)}px;}}")

    @staticmethod
    def _chk_s() -> str:
        return (f"QCheckBox{{color:{C_WHITE};background:transparent;"
                f"font:{R.pt(8)}pt 'Segoe UI';}}"
                f"QCheckBox::indicator:checked{{background:{C_GREEN};"
                f"border:1px solid {C_GREEN};border-radius:2px;}}"
                f"QCheckBox::indicator{{width:{R.px(12)}px;height:{R.px(12)}px;"
                f"border:1px solid {C_BORDER};border-radius:2px;}}")
