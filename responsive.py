"""
DRDO PXE — ADXL345 GUI  |  responsive.py  |  v1.0.0

WHY THIS FILE EXISTS
--------------------
The original GUI was designed on a 1366×768 laptop at 96 DPI. Every dimension
was a hardcoded integer: sidebars at 290 px and 220 px, buttons at 44 px tall,
fonts at 9 pt. On that reference screen everything looked perfect.

On a large external monitor (e.g. 2560×1440, 27") at 100 % OS scaling, those
hardcoded values produce two problems:
  1. The window fills the entire screen, stretching the layout wall-to-wall
     ("football pitch" effect) — empty space dominates.
  2. Fixed sidebar widths become disproportionately narrow (290/2560 = 11 %
     instead of the intended 21 %), making the plot area look cavernous.

HOW THIS IS FIXED
-----------------
  • UIScale reads the primary screen's available geometry and logical DPI once,
    immediately after QApplication is created.
  • It computes a single float `_f` that blends area-ratio and DPI-ratio.
  • Every widget dimension is passed through R.px(n) or R.pt(n) instead of
    being a bare integer literal.
  • Window width is hard-capped at 2 000 px (height at 1 200 px) so the GUI
    never stretches full-screen on a 4K display.
  • Sidebars are proportional to screen width with explicit min/max bounds,
    so they widen naturally on large displays without becoming absurd.

USAGE
-----
    # in main.py, right after QApplication(sys.argv):
    from responsive import R
    R.init()

    # anywhere else:
    from responsive import R
    btn.setFixedHeight(R.px(44))
    lbl.setFont(R.font("Segoe UI", 9, bold=True))
"""

from __future__ import annotations
import math
from PyQt6.QtCore import QSize
from PyQt6.QtGui  import QFont


# ── Reference display — the machine the GUI was originally designed for ────────

_REF_W   = 1366    # available screen width  (px)
_REF_H   = 768     # available screen height (px)
_REF_DPI = 96.0    # Windows default logical DPI

# Largest window the GUI will ever open, regardless of monitor size.
# This is THE key guard against the football-pitch effect.

_MAX_WIN_W = 2000
_MAX_WIN_H = 1200
_MIN_WIN_W = 1100
_MIN_WIN_H = 680

# Sidebar proportions as fractions of available screen width.
# On 1366 px screen:  left = 1366 * 0.19 ≈ 260 → clamped to min 230 px.
# On 2560 px screen:  left = 2560 * 0.19 ≈ 486 → clamped to max 430 px.

_LEFT_FRAC  = 0.19
_RIGHT_FRAC = 0.13
_LEFT_MIN,  _LEFT_MAX  = 230, 430
_RIGHT_MIN, _RIGHT_MAX = 180, 340

# Scale factor clamp — prevents bizarre layouts on virtual/exotic displays.

_F_MIN = 0.80
_F_MAX = 2.20


class _UIScale:
    
    """
    Singleton scaling engine.

    After R.init() the single float _f drives every dimension helper.
    The blend (60 % area-ratio + 40 % DPI-ratio) ensures the GUI scales
    sensibly on both high-resolution low-DPI monitors (like a 27" 1440p at
    100 % scaling) and high-DPI compact screens (like a 13" Retina at 200 %).

    All helpers are classmethods — no instance needed, just import R.
    """

    _f    : float = 1.0
    _dpi  : float = _REF_DPI
    _sw   : int   = _REF_W      # available screen width after taskbar
    _sh   : int   = _REF_H      # available screen height after taskbar
    _ready: bool  = False

    # ── Initialisation ─────────────────────────────────────────────────────────

    @classmethod
    def init(cls) -> None:
        
        """
        Read primary screen metrics from QApplication and compute _f.
        Must be called once after QApplication() is created, before any widget
        is constructed.  Safe to call multiple times (idempotent after first call).
        """
        if cls._ready:
            return
        from PyQt6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if not screen:
            cls._ready = True
            return

        geom     = screen.availableGeometry()   # excludes taskbar
        cls._sw  = geom.width()
        cls._sh  = geom.height()
        cls._dpi = screen.logicalDotsPerInch()

        # area_ratio: how much bigger is this screen vs the reference?
        
        area_ratio = math.sqrt((cls._sw / _REF_W) * (cls._sh / _REF_H))
        dpi_ratio  = cls._dpi / _REF_DPI

        # Weighted blend keeps laptop and desktop in harmony.
        
        raw    = area_ratio * 0.60 + dpi_ratio * 0.40
        cls._f = max(_F_MIN, min(_F_MAX, raw))
        cls._ready = True

    @classmethod
    def _ensure(cls) -> None:
        
        """Lazy-init guard — used internally by every helper."""
        
        if not cls._ready:
            cls.init()

    # ── Core numeric helpers ───────────────────────────────────────────────────

    @classmethod
    def f(cls) -> float:
        
        """Raw scale factor. Prefer px() / pt() in widget code."""
        
        cls._ensure()
        return cls._f

    @classmethod
    def px(cls, n: int) -> int:
        
        """
        Scale a pixel dimension by the UI scale factor.
        Always returns at least 1 to prevent zero-height/width widgets.
        """
        cls._ensure()
        return max(1, round(n * cls._f))

    @classmethod
    def pt(cls, n: float) -> int:
        
        """
        Scale a font point size.
        Clamped to minimum 6 pt — anything smaller is unreadable.
        """
        cls._ensure()
        return max(6, round(n * cls._f))

    @classmethod
    def sz(cls, w: int, h: int) -> QSize:
        
        """Scale both dimensions of a 2D size in one call."""
        
        cls._ensure()
        return QSize(cls.px(w), cls.px(h))

    # ── Font factory ───────────────────────────────────────────────────────────

    @classmethod
    def font(cls, family: str = "Segoe UI",
             pt: float = 9, bold: bool = False) -> QFont:
        
        """
        Build a fully-scaled QFont in one expression.
        Replaces all QFont("Segoe UI", 9, QFont.Weight.Bold) literals
        scattered through the UI files with a single, DPI-aware call.
        """
        cls._ensure()
        weight = QFont.Weight.Bold if bold else QFont.Weight.Normal
        return QFont(family, cls.pt(pt), weight)

    # ── Layout dimension helpers ───────────────────────────────────────────────

    @classmethod
    def window_size(cls) -> tuple[int, int]:
        
        """
        Optimal initial window dimensions.

        Formula: 85 % of available screen width / 88 % of height,
        hard-capped at _MAX_WIN_W × _MAX_WIN_H.

        The cap is the primary fix for the 'football pitch' complaint:
        on a 2560×1440 display the window opens at 2000×1200 instead
        of filling the entire screen.  The operator can still maximise
        manually if they want, but the *default* opening size is sane.
        """
        cls._ensure()
        w = min(_MAX_WIN_W, max(_MIN_WIN_W, int(cls._sw * 0.85)))
        h = min(_MAX_WIN_H, max(_MIN_WIN_H, int(cls._sh * 0.88)))
        return w, h

    @classmethod
    def min_window_size(cls) -> tuple[int, int]:
        
        """
        Minimum window size — scaled from the reference minimum so the GUI
        stays usable even on low-resolution or small secondary displays.
        """
        cls._ensure()
        return cls.px(_MIN_WIN_W), cls.px(_MIN_WIN_H)

    @classmethod
    def left_sidebar_w(cls) -> int:
        
        """
        Left sidebar width: _LEFT_FRAC of screen width, clamped to
        [_LEFT_MIN, _LEFT_MAX].  On a 1366-px laptop this yields ~260 px;
        on a 2560-px desktop it yields 430 px — both proportional, neither
        dominates the layout.
        """
        cls._ensure()
        raw = int(cls._sw * _LEFT_FRAC)
        return max(cls.px(_LEFT_MIN), min(cls.px(_LEFT_MAX), raw))

    @classmethod
    def right_sidebar_w(cls) -> int:
        
        """
        Right sidebar width: proportional and clamped, same logic as left.
        The stat boxes have a fixed-character maximum width so the clamp
        prevents them from becoming absurdly wide on 4K displays.
        """
        cls._ensure()
        raw = int(cls._sw * _RIGHT_FRAC)
        return max(cls.px(_RIGHT_MIN), min(cls.px(_RIGHT_MAX), raw))

    @classmethod
    def splitter_sizes(cls, total_w: int) -> list[int]:
        
        """
        Three-pane proportional split: [left, centre, right].
        Centre absorbs all remaining space after the two sidebars are placed.
        Guarantees centre is never narrower than 400 px even on tiny displays.
        """
        cls._ensure()
        left   = cls.left_sidebar_w()
        right  = cls.right_sidebar_w()
        centre = max(400, total_w - left - right)
        return [left, centre, right]

    @classmethod
    def screen_info(cls) -> str:
        
        """Diagnostic string — shown by patch.py and used in debug logging."""
        
        cls._ensure()
        return (f"{cls._sw}×{cls._sh} px  "
                f"{cls._dpi:.0f} DPI  "
                f"scale={cls._f:.3f}  "
                f"window={cls.window_size()[0]}×{cls.window_size()[1]}")


# Public singleton — import everywhere as: from responsive import R

R = _UIScale
