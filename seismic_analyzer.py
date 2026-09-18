"""
DRDO PXE — ADXL345 Data Acquisition System
seismic_analyzer.py  |  v3.0.2

WHAT THIS FILE DOES
-------------------
Provides two cooperating classes:

  SeismicAnalyzer — pure NumPy processing core (no Qt).
    • Receives DataHandler snapshots and runs the full seismic pipeline:
        demean → STA/LTA → FFT → event state machine
    • Every algorithm is fully vectorised — zero Python loops over sample data.

  SeismicTab — PyQt6 widget that PlotManager embeds as Tab 3.
    • Three sub-panels: STA/LTA ratio plot | FFT spectrum | event log table
    • Draggable trigger/detrigger InfiniteLines with strict bounds enforcement
    • Parameter spinboxes (STA, LTA, trigger, detrigger) wired live to the analyzer

WHY SEPARATE (analyzer vs tab)?
--------------------------------
SeismicAnalyzer holds zero Qt objects, so it can be unit-tested in a plain
Python environment without a display. SeismicTab is purely presentational — it
calls the analyzer and maps the outputs to pyqtgraph items. This also means the
algorithm can be swapped (e.g. recursive STA/LTA, machine-learning classifier)
without touching any Qt code.

BUGS FIXED vs v1/v2
--------------------
BUG-1  _stalta() had a Python for-loop over 40 000 samples
       → froze GUI for ~200 ms every refresh.
       FIX: fully vectorised with np.cumsum — runs in microseconds.

BUG-2  FillBetweenItem.setCurves() created new PlotDataItem objects
       every 80 ms → memory leak + AttributeError on older pyqtgraph.
       FIX: deleted FillBetweenItem; use fillLevel/fillBrush on the main
       FFT PlotDataItem (one call, no extra objects every frame).

BUG-3  ScatterPlotItem.clear() does not exist.
       FIX: replaced every .clear() with .setData(x=[], y=[]).

BUG-4  FillBetweenItem initialised with empty PlotDataItems never added to
       the plot → fill region never appeared.
       FIX: see BUG-2.

BUG-5  _run_detector stored acc = float(signal[i]) without abs().
       Signal is demeaned (can be negative after gravity removal) so
       peak_accel was wrong / negative.
       FIX: acc = abs(float(signal[i])) everywhere.

BUG-6  InfiniteLine.label.setFormat() called in four slot methods
       → AttributeError on pyqtgraph < 0.12.3.
       FIX: removed all label text from InfiniteLines; values shown in
       the spinboxes only.

BUG-7  _dominant_freq() called as an instance method on a @staticmethod
       → returned the bound method object, not a float.
       FIX: dominant frequency computed inline in update() and stored as
       self.dom_freq; no method call at display time.

SAMPLE-COUNT FIX (v3.0)
       Using snap["n"] as the detector start index made range(n, n) = empty
       once the deque filled at MAX_BUFFER — the detector went permanently
       deaf. FIX: use DataHandler.sample_count (an absolute counter that
       never resets) so the new-sample count is always correct.

BUG-LTA-DISP (v3.0.2)
       _on_lta() clamped lta_win_s but never updated the spinbox, leaving
       the display showing the wrong (unclamped) value.
       FIX: blockSignals → setValue → unblockSignals when clamping occurs.

BUG-DRAG-BOUNDS (v3.0.2)
       Unguarded drag positions could set trigger_ratio ≤ 0 (every sample
       triggers) or detrig_ratio < 0 (detector never detriggers).
       FIX: clamp raw drag value to spinbox bounds; snap line back if needed.

RATIO-SYNC FIX (v3.0)
       When trigger was lowered below or equal to detrigger, the state
       machine could simultaneously trigger and detrigger on the same sample,
       producing spurious duplicate events.
       FIX: _on_trig() and _on_det() enforce the invariant detrig < trig.
"""

from __future__ import annotations
from typing import NamedTuple

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore    import Qt
from responsive       import R   # all fixed px / pt go through R
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QLabel, QFrame, QDoubleSpinBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QPushButton,
)

# ── Constants ──────────────────────────────────────────────────────────────────

# Fallback sample rate used before enough timestamps arrive to estimate fs.

NOMINAL_FS_HZ  = 100.0

# STA/LTA parameter defaults — all adjustable live from the parameter bar.

STA_WIN_S      = 0.5      # Short-term window (seconds)
LTA_WIN_S      = 10.0     # Long-term window  (seconds)
TRIGGER_RATIO  = 4.0      # STA/LTA ON  threshold — event starts
DETRIG_RATIO   = 1.2      # STA/LTA OFF threshold — event ends
MAX_EVENTS     = 200      # Maximum rows kept in the event log (oldest dropped)

# FFT settings

FFT_WIN_S      = 5.0      # Analyse the last N seconds of signal
FFT_NFFT_MIN   = 512      # Minimum zero-pad length before rounding to power of 2
SEISMIC_LO_HZ  = 0.5     # Band-pass lower corner (Hz)
SEISMIC_HI_HZ  = 45.0    # Band-pass upper corner — below 50 Hz Nyquist at 100 Hz

# Which axis feeds the detector. "mag" is safest for omnidirectional events.

ANALYSIS_AXIS  = "mag"

# Minimum samples needed before any processing. Below 50 the window estimates
# and cumsum computations produce unstable results.

MIN_SAMPLES    = 50

# ── Colour tokens ──────────────────────────────────────────────────────────────

_BG        = "#000000"
_GOLD      = "#FFD700"
_TEXT      = "#E8F0FF"
_AXIS      = "#1E4A7A"
_STALTA    = "#FFD700"    # STA/LTA ratio curve — gold
_TRIG      = "#FF4444"    # Trigger line         — red
_DETRIG    = "#FF8C00"    # Detrigger line        — amber
_FFT_LINE  = "#4A90D9"    # FFT curve             — blue
_DOMFREQ   = "#FFD700"    # Dominant freq marker  — gold
_MARKER    = "#FF4444"    # Event onset markers   — red
_HDR_BG    = "#030B16"
_PARAM_BG  = "#050F1E"
_PEN_W     = 2.0


# ── SeismicEvent — immutable record stored in the event log ───────────────────


class SeismicEvent(NamedTuple):
    
    """
    One detected seismic event. NamedTuple gives immutable, lightweight records
    and lets us access fields by name (e.g. e.peak_accel) in the table builder.
    """
    elapsed_s   : float   # seconds from acquisition start (t = 0)
    peak_accel  : float   # peak |acceleration| during event (m/s²)
    duration_s  : float   # trigger-ON to trigger-OFF (seconds)
    peak_freq   : float   # dominant FFT frequency at trigger time (Hz)
    stalta_peak : float   # maximum STA/LTA ratio during the event


# ── SeismicAnalyzer — pure NumPy processing core (no Qt) ─────────────────────

class SeismicAnalyzer:
    
    """
    Stateful signal processor. Call update(snap) at 12.5 Hz; read the
    public attributes (elapsed_s, stalta, fft_freqs, fft_power, dom_freq,
    events) to feed the plot widgets.

    WHY STATEFUL (not a pure function)?
    The event state machine (_in_event, _event_start_s, …) must persist
    across calls. A pure function would reset the detector every 80 ms,
    missing any event longer than one refresh tick. The last-sample counter
    (_last_sample_count) also persists so we only scan truly new samples.
    """

    def __init__(
        self,
        sta_win_s     : float = STA_WIN_S,
        lta_win_s     : float = LTA_WIN_S,
        trigger_ratio : float = TRIGGER_RATIO,
        detrig_ratio  : float = DETRIG_RATIO,
    ):
        self.sta_win_s     = sta_win_s
        self.lta_win_s     = lta_win_s
        self.trigger_ratio = trigger_ratio
        self.detrig_ratio  = detrig_ratio

        # ── Algorithm outputs (written by update(), read by SeismicTab) ───────
        
        self.elapsed_s  : np.ndarray      = np.empty(0)
        self.stalta     : np.ndarray      = np.empty(0)
        self.fft_freqs  : np.ndarray      = np.empty(0)
        self.fft_power  : np.ndarray      = np.empty(0)
        self.dom_freq   : float           = 0.0
        self.fs         : float           = NOMINAL_FS_HZ   # updated each tick

        # ── Event state machine ───────────────────────────────────────────────
        
        self._in_event       : bool               = False
        self._event_start_s  : float              = 0.0
        self._event_peak_acc : float              = 0.0
        self._event_peak_frq : float              = 0.0
        self._event_sl_peak  : float              = 0.0
        self.events          : list[SeismicEvent] = []

        # SAMPLE-COUNT FIX: use DataHandler.sample_count (absolute, never resets)
        # instead of snap["n"] (pinned at MAX_BUFFER once the deque fills).
        # Without this the new-sample slice becomes range(n, n) = empty and the
        # detector processes zero samples per tick — it goes completely deaf.
        
        self._last_sample_count : int = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    def update(self, snap: dict) -> bool:
        
        """
        Run the full seismic processing pipeline on one DataHandler snapshot.
        Returns True if there is enough data to refresh the plots, False otherwise.
        Called at 12.5 Hz by SeismicTab.update_data().
        """
        n = snap.get("n", 0)
        if n < MIN_SAMPLES:
            return False

        t_raw      = snap["t"]
        start_time = snap.get("start_time", 0.0)
        elapsed    = (t_raw - start_time) if start_time > 0.0 else (t_raw - t_raw[0])

        # ── Step 1: Choose & demean signal ─────────────────────────────────────
        
        # Demeaning removes the ~9.81 m/s² gravity DC offset. Without this the
        # characteristic function (signal²) is dominated by gravity (~96 m²/s⁴)
        # and a real blast barely changes the STA/LTA ratio.
        signal = self._demean(snap)

        # ── Step 2: Estimate live sample rate ──────────────────────────────────
        
        # Use the most recent 200 timestamps to stay responsive to rate changes.
        # Clip the result to 1–3200 Hz to reject wildly invalid values that
        # would make nsta/nlta nonsensically large or zero.
        
        tail = t_raw[-min(200, n):]
        if len(tail) >= 10:
            dts = np.diff(tail)
            dts = dts[dts > 1e-6]
            if len(dts) >= 5:
                self.fs = float(np.clip(1.0 / np.median(dts), 1.0, 3200.0))

        # ── Step 3: STA/LTA (fully vectorised — BUG-1 fix) ────────────────────
        
        nsta   = max(2, int(self.sta_win_s * self.fs))
        nlta   = max(nsta + 1, int(self.lta_win_s * self.fs))
        cf     = signal ** 2               # characteristic function: energy proxy
        stalta = self._stalta(cf, nsta, nlta)

        # ── Step 4: FFT on the last FFT_WIN_S seconds ─────────────────────────
        
        fft_n       = max(int(FFT_WIN_S * self.fs), MIN_SAMPLES)
        freqs, psd  = self._fft_psd(signal[-fft_n:], self.fs)

        # ── Step 5: Dominant frequency as a plain float (BUG-7 fix) ───────────
        
        # Stored in self.dom_freq so _run_detector can read it without a method call.
        
        if len(psd) > 0 and float(psd.max()) > 0.0:
            self.dom_freq = float(freqs[int(np.argmax(psd))])
        else:
            self.dom_freq = 0.0

        # ── Step 6: Event detector — only scans truly new samples ──────────────
        
        # SAMPLE-COUNT FIX: snap["sample_count"] is DataHandler's absolute counter
        # (never resets, grows beyond MAX_BUFFER). snap["n"] stays pinned at
        # MAX_BUFFER after the deque fills, so using it as a start index would
        # give range(40000, 40000) = empty every tick — the detector goes deaf.
        
        current_count = int(snap.get("sample_count", n))
        new_samples   = max(0, current_count - self._last_sample_count)
        start_i       = max(0, n - new_samples)
        self._run_detector(elapsed, stalta, signal, self.dom_freq, start_i)
        self._last_sample_count = current_count

        # ── Step 7: Store outputs for the GUI ─────────────────────────────────
        
        self.elapsed_s = elapsed
        self.stalta    = stalta
        self.fft_freqs = freqs
        self.fft_power = psd
        return True

    def reset_events(self):
        
        """Clear the event log and reset the state machine without touching signal buffers."""
        
        self.events          = []
        self._in_event       = False
        self._event_start_s  = 0.0
        self._event_peak_acc = 0.0
        self._event_peak_frq = 0.0
        self._event_sl_peak  = 0.0

    def clear(self):
        
        """Full reset — called when the GUI CLEAR ALL button fires."""
        
        self.elapsed_s          = np.empty(0)
        self.stalta             = np.empty(0)
        self.fft_freqs          = np.empty(0)
        self.fft_power          = np.empty(0)
        self.dom_freq           = 0.0
        self._last_sample_count = 0
        self.reset_events()

    @property
    def is_active(self) -> bool:
        
        """
        True while a seismic event is currently in progress.
        Exposes the private _in_event flag as a read-only property so SeismicTab
        can query it without reaching into private state directly.
        """
        return self._in_event

    # ── Signal processing ──────────────────────────────────────────────────────

    @staticmethod
    def _demean(snap: dict) -> np.ndarray:
        
        """
        Return the chosen axis with its DC component (gravity) subtracted.

        WHY DEMEANING IS CRITICAL:
        At rest the ADXL345 reads ~9.81 m/s² on the magnitude axis.
        The STA/LTA characteristic function is signal². With gravity present
        the DC energy (~96 m²/s⁴) swamps everything — a 1.5 m/s² blast only
        shifts CF from 96 → 128 giving STA/LTA ≈ 1.03, never triggering.
        After removing the mean, that same blast gives STA/LTA ≈ 19.

        The signal is kept signed (no abs()) so the FFT produces correct
        frequencies — abs()-rectification would double every spectral peak.
        """
        key = {"mag": "mag", "z": "az", "x": "ax", "y": "ay"}.get(
            ANALYSIS_AXIS, "mag")
        v = snap[key].copy()
        v -= v.mean()
        return v

    @staticmethod
    def _stalta(cf: np.ndarray, nsta: int, nlta: int) -> np.ndarray:
        
        """
        Fully vectorised STA/LTA via cumulative sum — O(N) NumPy, zero Python loop.

        BUG-1 fix: the original implementation looped over 40 000 samples in
        Python, consuming ~200 ms per refresh and stalling the GUI. NumPy's
        cumsum lets us compute every STA and LTA value in a single slice
        operation — effectively free compared to the GUI render cost.

        ALGORITHM:
            cs[i] = sum(cf[0..i-1])    (padded by one for 1-indexed indexing)
            STA[i] = (cs[i+1] - cs[i+1-nSTA]) / nSTA
            LTA[i] = (cs[i+1] - cs[i+1-nLTA]) / nLTA
        """
        n     = len(cf)
        ratio = np.ones(n, dtype=np.float64)
        if n < nlta:
            return ratio

        # Padded cumulative sum: cs[i+1] - cs[i+1-k] = sum of k elements ending at i.
        
        cs      = np.empty(n + 1, dtype=np.float64)
        cs[0]   = 0.0
        np.cumsum(cf, out=cs[1:])

        # Compute STA and LTA for ALL valid indices in one vectorised operation.
        
        idx   = np.arange(nlta, n)
        sta   = (cs[idx + 1] - cs[idx + 1 - nsta]) / nsta
        lta   = (cs[idx + 1] - cs[idx + 1 - nlta]) / nlta
        valid = lta > 1e-20          # guard against division by near-zero LTA
        ratio[idx[valid]] = sta[valid] / lta[valid]
        return ratio

    @staticmethod
    def _fft_psd(signal: np.ndarray, fs: float):
        
        """
        Single-sided power spectral density, band-limited to the seismic band.

        Steps:
          1. Apply a Hann window to suppress spectral leakage from edge discontinuities.
          2. Zero-pad to the next power of 2 for maximum FFT speed (Cooley-Tukey).
          3. Compute the one-sided PSD: multiply non-DC, non-Nyquist bins by 2.
          4. Mask to SEISMIC_LO_HZ – SEISMIC_HI_HZ so only physically meaningful
             frequencies appear in the plot.
        """
        n = len(signal)
        if n < 4:
            return np.empty(0), np.empty(0)
        n_pad  = max(n, FFT_NFFT_MIN)
        nfft   = 1 << (n_pad - 1).bit_length()    # next power of 2 >= n_pad
        win    = np.hanning(n)
        spec   = np.fft.rfft(signal * win, n=nfft)
        psd    = (np.abs(spec) ** 2) / (fs * nfft)
        psd[1:-1] *= 2                             # single-sided correction
        freqs  = np.fft.rfftfreq(nfft, d=1.0 / fs)
        mask   = (freqs >= SEISMIC_LO_HZ) & (freqs <= SEISMIC_HI_HZ)
        return freqs[mask].copy(), psd[mask].copy()

    def _run_detector(
        self,
        elapsed : np.ndarray,
        stalta  : np.ndarray,
        signal  : np.ndarray,
        dom_freq: float,
        start_i : int,
    ):
        
        """
        Rising-edge / falling-edge event state machine over new samples only.

        Only iterates from start_i to end of buffer — the slice of samples
        that arrived since the last call. At 12.5 Hz refresh and 100 Hz
        sensor rate, this is typically 8 samples per call. The loop is
        intentionally kept — it only runs over new samples, never over the
        full 40 000-element buffer.

        BUG-5 fix: signal is demeaned so values can be negative. Always use
        abs(signal[i]) when tracking peak amplitude, otherwise a downward
        spike records a negative peak_accel — meaningless as a magnitude.

        Events shorter than 50 ms are discarded as sub-threshold noise glitches.
        """
        for i in range(start_i, len(stalta)):
            r   = float(stalta[i])
            t   = float(elapsed[i])
            acc = abs(float(signal[i]))      # BUG-5 fix: abs() required on demeaned signal

            if not self._in_event:
                if r >= self.trigger_ratio:
                    self._in_event       = True
                    self._event_start_s  = t
                    self._event_peak_acc = acc
                    self._event_peak_frq = dom_freq
                    self._event_sl_peak  = r
            else:
                # Track running maxima while inside the event.
                
                if acc > self._event_peak_acc: self._event_peak_acc = acc
                if r   > self._event_sl_peak:  self._event_sl_peak  = r
                if r <= self.detrig_ratio:
                    dur = t - self._event_start_s
                    if dur >= 0.05:            # ignore sub-50 ms glitches
                        self.events.append(SeismicEvent(
                            elapsed_s   = self._event_start_s,
                            peak_accel  = self._event_peak_acc,
                            duration_s  = dur,
                            peak_freq   = self._event_peak_frq,
                            stalta_peak = self._event_sl_peak,
                        ))
                        if len(self.events) > MAX_EVENTS:
                            self.events.pop(0)   # drop oldest to cap memory
                    self._in_event = False


# ── SeismicTab — PyQt6 widget ─────────────────────────────────────────────────

class SeismicTab(QWidget):
    
    """
    Three-panel display injected into PlotManager as Tab 3.

    Panel layout (resizable QSplitter):
      Top    — STA/LTA ratio with draggable trigger/detrigger lines + event markers
      Middle — Real-time FFT PSD with dominant-frequency vertical marker
      Bottom — Detected-event log table (auto-scrolls to newest event)
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._analyzer = SeismicAnalyzer()
        self._paused   = False
        self._n_shown  = 0    # rows currently in the event table (for change detection)
        self._build_ui()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)
        root.addWidget(self._build_param_bar())
        sp = QSplitter(Qt.Orientation.Vertical)
        sp.setStyleSheet("QSplitter::handle{background:#1E4A7A;height:3px;}")
        sp.addWidget(self._build_stalta_panel())
        sp.addWidget(self._build_fft_panel())
        sp.addWidget(self._build_table_panel())
        sp.setSizes([R.px(260), R.px(200), R.px(160)])
        root.addWidget(sp)
        root.addWidget(self._build_status_bar())

    def _build_param_bar(self) -> QFrame:
        
        """
        Horizontal parameter bar — STA, LTA, Trigger, Detrigger spinboxes + Clear button.
        Fixed height of 40 px so it never eats into the plot area.
        """
        bar = QFrame()
        bar.setFixedHeight(R.px(40))
        bar.setStyleSheet(
            f"QFrame{{background:{_PARAM_BG};"
            "border:1px solid #1E4A7A;border-radius:3px;}}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 2, 8, 2)
        h.setSpacing(10)

        lbl_ss  = (f"color:{_FFT_LINE};font:8pt 'Segoe UI';"
                   "background:transparent;border:none;")
        spin_ss = ("QDoubleSpinBox{background:#0A1828;color:#E8F0FF;"
                   "border:1px solid #1E4A7A;border-radius:2px;"
                   "font:8pt 'Segoe UI';padding:1px 3px;}"
                   "QDoubleSpinBox:focus{border-color:#4A90D9;}")

        h.addWidget(_lbl("STA (s):", lbl_ss))
        self._sp_sta = _spin(0.1, 5.0, STA_WIN_S, 0.1, 1, spin_ss)
        self._sp_sta.valueChanged.connect(self._on_sta)
        h.addWidget(self._sp_sta)

        h.addWidget(_lbl("LTA (s):", lbl_ss))
        self._sp_lta = _spin(2.0, 60.0, LTA_WIN_S, 1.0, 1, spin_ss)
        self._sp_lta.valueChanged.connect(self._on_lta)
        h.addWidget(self._sp_lta)

        h.addWidget(_lbl("Trigger:", lbl_ss))
        self._sp_trig = _spin(1.5, 20.0, TRIGGER_RATIO, 0.5, 1, spin_ss)
        self._sp_trig.valueChanged.connect(self._on_trig)
        h.addWidget(self._sp_trig)

        h.addWidget(_lbl("Detrigger:", lbl_ss))
        self._sp_det = _spin(0.5, 5.0, DETRIG_RATIO, 0.1, 1, spin_ss)
        self._sp_det.valueChanged.connect(self._on_det)
        h.addWidget(self._sp_det)

        h.addStretch()

        btn = QPushButton("Clear Events")
        btn.setFixedHeight(26)
        btn.setStyleSheet(
            "QPushButton{background:#050F1E;color:#E74C3C;"
            "border:1px solid #1E4A7A;border-radius:3px;"
            "font:8pt Consolas;padding:0 8px;}"
            "QPushButton:hover{background:#1A3A5A;border-color:#E74C3C;}")
        btn.clicked.connect(self._on_clear)
        h.addWidget(btn)
        return bar

    def _build_stalta_panel(self) -> QWidget:
        
        """
        STA/LTA ratio plot with:
          • Gold ratio curve
          • Red dashed trigger line (draggable)
          • Amber dotted detrigger line (draggable)
          • Red triangle markers at event onset positions

        BUG-3 fix: event markers use ScatterPlotItem + setData(x=[],y=[]) not .clear().
        BUG-6 fix: InfiniteLines have no label= — avoids AttributeError on older pyqtgraph.
        """
        w  = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        vl.addWidget(_header("  STA / LTA Ratio — Seismic Event Detector"))

        self._pw_sl = pg.PlotWidget(background=_BG)
        self._pw_sl.setMenuEnabled(False)
        self._pw_sl.setClipToView(True)
        try:
            self._pw_sl.setDownsampling(auto=True, mode="peak")
        except Exception:
            pass
        self._pw_sl.setLabel("left",   "STA / LTA",  color=_TEXT)
        self._pw_sl.setLabel("bottom", "Elapsed (s)", color=_TEXT)
        self._pw_sl.showGrid(x=True, y=True, alpha=0.15)
        _style_axes(self._pw_sl)

        self._cv_sl = self._pw_sl.plot(pen=pg.mkPen(_STALTA, width=_PEN_W))

        # Draggable trigger line — no label= avoids BUG-6 AttributeError.
        
        self._ln_trig = pg.InfiniteLine(
            pos=TRIGGER_RATIO, angle=0, movable=True,
            pen=pg.mkPen(_TRIG, width=1.5, style=Qt.PenStyle.DashLine))
        self._ln_trig.sigPositionChangeFinished.connect(self._on_trig_drag)
        self._pw_sl.addItem(self._ln_trig)

        self._ln_det = pg.InfiniteLine(
            pos=DETRIG_RATIO, angle=0, movable=True,
            pen=pg.mkPen(_DETRIG, width=1.5, style=Qt.PenStyle.DotLine))
        self._ln_det.sigPositionChangeFinished.connect(self._on_det_drag)
        self._pw_sl.addItem(self._ln_det)

        # BUG-3 fix: ScatterPlotItem — always use setData(x=[], y=[]) to clear.
        
        self._sc_ev = pg.ScatterPlotItem(
            size=12, pen=pg.mkPen(None),
            brush=pg.mkBrush(_MARKER), symbol="t")
        self._pw_sl.addItem(self._sc_ev)

        vl.addWidget(self._pw_sl)
        return w

    def _build_fft_panel(self) -> QWidget:
        
        """
        FFT power spectrum with:
          • Blue filled-area PSD curve
          • Gold vertical dominant-frequency marker

        BUG-2 / BUG-4 fix: fillLevel + fillBrush ON the PlotDataItem itself —
        no FillBetweenItem, no setCurves(), no new objects every frame.
        """
        w  = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        vl.addWidget(_header(
            f"  Frequency Spectrum  "
            f"({SEISMIC_LO_HZ}–{SEISMIC_HI_HZ} Hz  |  last {FFT_WIN_S:.0f} s  |  Hann window)"))

        self._pw_fft = pg.PlotWidget(background=_BG)
        self._pw_fft.setMenuEnabled(False)
        self._pw_fft.setClipToView(True)
        try:
            self._pw_fft.setDownsampling(auto=True, mode="peak")
        except Exception:
            pass
        self._pw_fft.setLabel("left",   "PSD  ((m/s²)²/Hz)", color=_TEXT)
        self._pw_fft.setLabel("bottom", "Frequency (Hz)",     color=_TEXT)
        self._pw_fft.showGrid(x=True, y=True, alpha=0.15)
        _style_axes(self._pw_fft)

        # Single PlotDataItem with fill — the correct fix for BUG-2 and BUG-4.
        
        self._cv_fft = self._pw_fft.plot(
            pen=pg.mkPen(_FFT_LINE, width=_PEN_W),
            fillLevel=0.0,
            fillBrush=pg.mkBrush(26, 58, 106, 120))

        self._ln_dom = pg.InfiniteLine(
            pos=5.0, angle=90, movable=False,
            pen=pg.mkPen(_DOMFREQ, width=1.5, style=Qt.PenStyle.DashLine))
        self._pw_fft.addItem(self._ln_dom)

        vl.addWidget(self._pw_fft)
        return w

    def _build_table_panel(self) -> QWidget:
        
        """Scrolling event log — five columns, auto-scrolls to the newest row."""
        
        w  = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        vl.addWidget(_header("  Detected Seismic Events"))

        self._tbl = QTableWidget(0, 5)
        self._tbl.setHorizontalHeaderLabels([
            "Time (s)", "Peak Accel (m/s²)", "Duration (s)",
            "Peak Freq (Hz)", "STA/LTA Peak"])
        self._tbl.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self._tbl.horizontalHeader().setStyleSheet(
            f"QHeaderView::section{{background:#0A1828;color:{_GOLD};"
            "border:1px solid #1E4A7A;font:8pt 'Segoe UI';"
            "font-weight:bold;padding:3px;}}")
        self._tbl.setStyleSheet(
            f"QTableWidget{{background:{_BG};color:{_TEXT};"
            "border:1px solid #1E4A7A;gridline-color:#0A1E35;"
            "font:8pt 'Segoe UI';selection-background-color:#0F2840;}}"
            "QTableWidget::item{border:none;padding:2px 4px;}")
        self._tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.verticalHeader().setVisible(False)
        vl.addWidget(self._tbl)
        return w

    def _build_status_bar(self) -> QFrame:
        """Footer bar showing live Fs estimate, event count, dominant freq, and STA/LTA value."""
        bar = QFrame()
        bar.setFixedHeight(R.px(28))
        bar.setStyleSheet(
            f"QFrame{{background:{_HDR_BG};"
            "border:1px solid #1E4A7A;border-radius:2px;}}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 0, 8, 0)
        h.setSpacing(20)
        ss = f"color:{_FFT_LINE};font:8pt 'Segoe UI';background:transparent;border:none;"

        self._lb_fs   = _lbl("Fs: — Hz",        ss)
        self._lb_ev   = _lbl("Events: 0",        ss)
        self._lb_dom  = _lbl("Peak freq: — Hz",  ss)
        self._lb_sl   = _lbl("STA/LTA: —",       ss)
        for lb in (self._lb_fs, self._lb_ev, self._lb_dom, self._lb_sl):
            h.addWidget(lb)
        h.addStretch()

        # Visible only during an active event — draws the operator's attention.
        
        self._lb_active = QLabel("● EVENT ACTIVE")
        self._lb_active.setStyleSheet(
            f"color:{_TRIG};font:9pt 'Segoe UI';font-weight:bold;"
            "background:transparent;border:none;")
        self._lb_active.setVisible(False)
        h.addWidget(self._lb_active)
        return bar

    # ── Data update ────────────────────────────────────────────────────────────

    def update_data(self, snap: dict):
        
        """
        Called at 12.5 Hz by PlotManager.refresh().
        Delegates all computation to SeismicAnalyzer.update(), then maps
        the results to pyqtgraph items and Qt labels.
        """
        if self._paused:
            return
        if not self._analyzer.update(snap):
            return

        an = self._analyzer

        # ── STA/LTA plot ───────────────────────────────────────────────────────
        
        n = len(an.elapsed_s)
        if n >= 2 and len(an.stalta) == n:
            self._cv_sl.setData(an.elapsed_s, an.stalta)

            # BUG-3 fix: always use setData(x,y) — ScatterPlotItem has no .clear().
            
            if an.events:
                self._sc_ev.setData(
                    x=[e.elapsed_s for e in an.events],
                    y=[an.trigger_ratio + 0.5] * len(an.events))
            else:
                self._sc_ev.setData(x=[], y=[])

            self._lb_sl.setText(f"STA/LTA: {an.stalta[-1]:.2f}")
            self._lb_active.setVisible(an.is_active)

        # ── FFT plot ───────────────────────────────────────────────────────────
        
        # BUG-2/BUG-4 fix: one setData call. No FillBetweenItem, no new objects.
        if len(an.fft_freqs) >= 2:
            self._cv_fft.setData(an.fft_freqs, an.fft_power)
            if an.dom_freq > 0.0:
                self._ln_dom.setValue(an.dom_freq)
                self._lb_dom.setText(f"Peak freq: {an.dom_freq:.1f} Hz")

        # ── Event table (only rebuild when count changes) ──────────────────────
        
        n_ev = len(an.events)
        if n_ev != self._n_shown:
            self._rebuild_table()
            self._n_shown = n_ev
            self._lb_ev.setText(f"Events: {n_ev}")

        self._lb_fs.setText(f"Fs: {an.fs:.1f} Hz")

    def _rebuild_table(self):
        
        """Repopulate the event table from analyzer.events. Only called when count changes."""
        
        evts = self._analyzer.events
        self._tbl.setRowCount(len(evts))
        for row, e in enumerate(evts):
            for col, txt in enumerate([
                f"{e.elapsed_s:.2f}",
                f"{e.peak_accel:.4f}",
                f"{e.duration_s:.3f}",
                f"{e.peak_freq:.1f}",
                f"{e.stalta_peak:.2f}",
            ]):
                item = QTableWidgetItem(txt)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._tbl.setItem(row, col, item)
        if self._tbl.rowCount() > 0:
            self._tbl.scrollToBottom()

    # ── Spinbox / drag slots ───────────────────────────────────────────────────

    def _on_sta(self, v: float):
        
        """Update analyzer's STA window. Minimum 0.05 s to avoid zero-length windows."""
        
        self._analyzer.sta_win_s = max(0.05, v)

    def _on_lta(self, v: float):
        
        """
        BUG-LTA-DISP fix (v3.0.2): clamp LTA to be at least STA + 0.1 s.
        Without the spinbox update, the operator sees the unclamped value (e.g. "2.0 s")
        while the analyzer silently uses the clamped value (e.g. "5.1 s") — an invisible
        parameter mismatch impossible to diagnose without reading source code.
        """
        min_lta = round(self._analyzer.sta_win_s + 0.1, 1)
        if v < min_lta:
            v = max(float(self._sp_lta.minimum()), min_lta)
            self._sp_lta.blockSignals(True)
            self._sp_lta.setValue(v)
            self._sp_lta.blockSignals(False)
        self._analyzer.lta_win_s = v

    def _on_trig(self, v: float):
        
        """
        RATIO-SYNC fix: if trigger is lowered to or below detrigger, pull detrigger
        down to (trigger - 0.1). Without this, the state machine can simultaneously
        satisfy both conditions on the same sample, producing spurious duplicate events.
        """
        self._analyzer.trigger_ratio = v
        self._ln_trig.setValue(v)
        if self._analyzer.detrig_ratio >= v:
            new_det = max(float(self._sp_det.minimum()), round(v - 0.1, 1))
            self._sp_det.blockSignals(True)
            self._sp_det.setValue(new_det)
            self._sp_det.blockSignals(False)
            self._analyzer.detrig_ratio = new_det
            self._ln_det.setValue(new_det)

    def _on_det(self, v: float):
        
        """
        RATIO-SYNC fix: clamp detrigger to always be strictly below trigger.
        Keeps the state machine invariant: detrig < trig.
        """
        v = min(v, round(self._analyzer.trigger_ratio - 0.1, 1))
        self._sp_det.blockSignals(True)
        self._sp_det.setValue(v)
        self._sp_det.blockSignals(False)
        self._analyzer.detrig_ratio = v
        self._ln_det.setValue(v)

    def _on_trig_drag(self):
        
        """
        BUG-DRAG-BOUNDS fix (v3.0.2): clamp raw drag position to [1.5, 20.0].
        An unguarded negative trigger_ratio makes (r >= trigger_ratio) always true
        — every single sample fires an event, flooding the table with 200 rows
        in milliseconds. Clamp first, snap the line back if out of bounds, then
        mirror the same RATIO-SYNC logic as _on_trig().
        """
        raw = self._ln_trig.value()
        lo  = float(self._sp_trig.minimum())    # 1.5
        hi  = float(self._sp_trig.maximum())    # 20.0
        v   = round(max(lo, min(hi, raw)), 1)
        if abs(raw - v) > 1e-6:
            self._ln_trig.setValue(v)           # snap line back into valid range
        self._sp_trig.blockSignals(True)
        self._sp_trig.setValue(v)
        self._sp_trig.blockSignals(False)
        self._analyzer.trigger_ratio = v
        if self._analyzer.detrig_ratio >= v:
            new_det = max(float(self._sp_det.minimum()), round(v - 0.1, 1))
            self._sp_det.blockSignals(True)
            self._sp_det.setValue(new_det)
            self._sp_det.blockSignals(False)
            self._analyzer.detrig_ratio = new_det
            self._ln_det.setValue(new_det)

    def _on_det_drag(self):
        
        """
        BUG-DRAG-BOUNDS fix (v3.0.2): clamp raw drag position to
        [spinbox_min, trigger - 0.1]. A negative detrig_ratio makes
        (r <= detrig_ratio) permanently false — the detector stays locked
        in 'event active' and never records the end of any event.
        """
        raw = self._ln_det.value()
        lo  = float(self._sp_det.minimum())                     # 0.5
        hi  = round(self._analyzer.trigger_ratio - 0.1, 1)     # must be < trigger
        v   = round(max(lo, min(hi, raw)), 1)
        if abs(raw - v) > 1e-6:
            self._ln_det.setValue(v)            # snap line back into valid range
        self._sp_det.blockSignals(True)
        self._sp_det.setValue(v)
        self._sp_det.blockSignals(False)
        self._analyzer.detrig_ratio = v

    def _on_clear(self):
        
        """Clear Events button — wipes the log, resets state machine, clears markers."""
        
        self._analyzer.reset_events()
        self._n_shown = 0
        self._tbl.setRowCount(0)
        self._sc_ev.setData(x=[], y=[])    # BUG-3 fix: setData not .clear()
        self._lb_ev.setText("Events: 0")
        self._lb_active.setVisible(False)

    # ── PlotManager hooks ──────────────────────────────────────────────────────

    def clear(self):
        
        """Called by PlotManager.clear_plots() when the operator clicks CLEAR ALL."""
        
        self._analyzer.clear()
        self._cv_sl.setData([], [])
        self._cv_fft.setData([], [])
        self._sc_ev.setData(x=[], y=[])    # BUG-3 fix
        self._tbl.setRowCount(0)
        self._n_shown = 0
        self._lb_fs.setText("Fs: — Hz")
        self._lb_ev.setText("Events: 0")
        self._lb_dom.setText("Peak freq: — Hz")
        self._lb_sl.setText("STA/LTA: —")
        self._lb_active.setVisible(False)

    def set_paused(self, v: bool):
        
        """Called by PlotManager.set_paused() — freeze updates without losing data."""
        
        self._paused = v


# ── Module-level widget helpers (keep tab code DRY) ───────────────────────────

def _lbl(text: str, style: str) -> QLabel:
    
    """Single-line label helper — used for parameter bar labels."""
    
    w = QLabel(text)
    w.setStyleSheet(style)
    return w


def _header(text: str) -> QLabel:
    
    """Gold-on-dark panel header strip used above each sub-panel."""
    
    w = QLabel(text)
    w.setStyleSheet(
        f"color:{_GOLD};font:9pt 'Segoe UI';font-weight:bold;"
        f"background:{_HDR_BG};border:none;padding:2px 4px;")
    return w


def _spin(lo, hi, default, step, decimals, style) -> QDoubleSpinBox:
    
    """Compact spinbox factory used in the parameter bar."""
    
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setValue(default)
    s.setSingleStep(step)
    s.setDecimals(decimals)
    s.setFixedWidth(R.px(68))
    s.setStyleSheet(style)
    return s


def _style_axes(pw: pg.PlotWidget):
    
    """Apply uniform axis pen and text-pen to all four sides of a PlotWidget."""
    
    for ax in ("left", "bottom", "top", "right"):
        pw.getAxis(ax).setPen(pg.mkPen(_AXIS))
        pw.getAxis(ax).setTextPen(pg.mkPen(_TEXT))
